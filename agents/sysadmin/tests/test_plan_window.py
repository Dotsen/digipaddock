"""the executor's window around a step of an approved plan — a snapshot before and after over the exec key,
the difference to the cabinet; a window that cannot open never stops the step."""

import io
import json
import re
import time

import pytest

from gate.agent_ro_gate import Result
from kd.cabinet import client as cabinet_client
from sysadmin import execute, inventory, plan_window

SNAP = "# agent-sec-snapshot v1\nunits\tx.service\t{unit}\nkeys\t/root/.ssh/authorized_keys\tk1 1\ncron\terror\tunreadable\n"
SS = (
    "Netid State Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
    'tcp LISTEN 0 511 0.0.0.0:{port} 0.0.0.0:* users:(("nginx",pid=1,fd=6))\n'
)


def answer(remote, unit="a" * 64, port=80, ss_rc=0, snap_rc=0, wg=""):
    nonce = re.search(r"@@kd-window-([0-9a-f]+)", remote).group(1)
    mark = "@@kd-window-" + nonce
    return (
        SNAP.format(unit=unit)
        + f"\n{mark} snapshot rc={snap_rc}\n"
        + SS.format(port=port)
        + f"\n{mark} ports rc={ss_rc}\n"
        + wg
        + f"\n{mark} wgpeers rc=0\n"
    ).encode()


class Runner:
    """The step's ssh and the window's: a snapshot call is told by its marker; ``snaps`` — kwargs per snapshot."""

    def __init__(self, snaps=(), step=Result(0, b"done\n", False, False)):
        self.snaps, self.step, self.calls = list(snaps), step, []

    def __call__(self, argv, timeout, limit, kill_on_limit):
        remote = argv[-1]
        if "@@kd-window-" in remote:
            self.calls.append("snapshot")
            kwargs = self.snaps.pop(0) if self.snaps else {}
            if isinstance(kwargs, BaseException):
                raise kwargs
            return Result(0, answer(remote, **kwargs), False, False)
        self.calls.append("step")
        return self.step


class Cabinet:
    def __init__(self, down=False):
        self.down, self.calls = down, []

    def call(self, request):
        if self.down:
            raise cabinet_client.CabinetError(
                "unavailable", "board unavailable (FileNotFoundError)"
            )
        self.calls.append(request)
        return {"ok": True, "id": f"w{len(self.calls)}", "state": "closed"}


@pytest.fixture
def cab(monkeypatch):
    cab = Cabinet()
    monkeypatch.setattr(execute, "WINDOWS", plan_window.Windows(client=cab))
    return cab


def server(alias="example-stage"):
    return inventory.get(alias, None, inventory.ENABLED)


@pytest.fixture(autouse=True)
def enabled(monkeypatch, enabled_all):
    monkeypatch.setattr(inventory, "ENABLED", enabled_all)


def test_capture_splits_the_three_parts_and_marks_a_failed_one():
    runner = Runner([{"ss_rc": 1}])
    items, errors, reason = plan_window.capture(server(), runner)
    assert reason == ""
    assert (
        items["units:x.service"] == "a" * 64
        and items["keys:/root/.ssh/authorized_keys"] == "k1 1"
    )
    assert errors == {"cron", "ports"}
    assert not any(k.startswith("ports:") for k in items)


def test_capture_takes_ports_and_peers_into_one_flat_map():
    wg = "wg0\t" + "A" * 43 + "=\t10.0.0.2/32\n"
    items, errors, reason = plan_window.capture(server(), Runner([{"wg": wg}]))
    assert (
        items["ports:tcp:0.0.0.0:80"] == "nginx"
        and items["wgpeers:wg0:" + "A" * 43 + "="] == ""
    )
    assert errors == {"cron"}


def test_capture_without_a_snapshot_is_a_reason_not_a_window():
    items, errors, reason = plan_window.capture(server(), Runner([{"snap_rc": 127}]))
    assert items is None and "снимок" in reason


def test_the_exec_users_sudo_wraps_the_snapshot():
    runner = Runner()
    seen = []
    runner_call = runner.__call__

    def spy(argv, timeout, limit, kill_on_limit):
        seen.append(argv[-1])
        return runner_call(argv, timeout, limit, kill_on_limit)

    plan_window.capture(server("node-a"), spy)
    assert seen[0].startswith("sudo -n bash -c ") and "agent-sec-snapshot" in seen[0]


def test_with_envelopes_on_the_snapshot_is_the_gates_word(monkeypatch):
    """on a host that sends envelopes the exec gate takes the snapshot itself (``agent-exec-gate snapshot
    <nonce>``, read-only, sudo inside the gate); its output carries the same markers."""
    from kd import exec_envelope

    monkeypatch.setattr(exec_envelope, "enabled", lambda path=None: True)
    seen = []

    def spy(argv, timeout, limit, kill_on_limit):
        seen.append(argv[-1])
        nonce = re.fullmatch(r"agent-exec-gate snapshot ([0-9a-f]{16})", argv[-1]).group(1)
        return Result(0, answer("@@kd-window-" + nonce), False, False)

    items, errors, reason = plan_window.capture(server("node-a"), spy)
    assert reason == "" and items["units:x.service"] == "a" * 64 and len(seen) == 1
    assert seen[0] == f"{exec_envelope.SNAPSHOT_RUN} {seen[0][-16:]}"


def test_diff_skips_sections_with_an_error_in_either_snapshot():
    before = {
        "units:x.service": "a",
        "units:y.service": "b",
        "cron:/etc/cron.d/x": "1",
        "ports:tcp:0.0.0.0:80": "nginx",
    }
    after = {
        "units:x.service": "c",
        "cron:/etc/cron.d/x": "2",
        "ports:tcp:0.0.0.0:80": "nginx",
        "ports:tcp:0.0.0.0:443": "nginx",
        "sshd:port": "22",
    }
    assert plan_window.diff(before, {"cron"}, after, {"sshd"}) == {
        "units:x.service": ["a", "c"],
        "units:y.service": ["b", None],
        "ports:tcp:0.0.0.0:443": [None, "nginx"],
    }


STEP = {"kind": "admin", "task_id": "KD-5", "grant_id": "g", "op_index": 1, "op_count": 1,
        "operation": {"server": "example-stage", "verb": "script", "script": "true"}}


def run(args, tmp_path, runner, clock=None, extra=STEP):
    """The run of one plan step (kd-admin-run, kd-admin-critical): ``args`` as ssh-exec took them — ``--task`` means
    the step belongs to a card (``extra``), ``a,b`` a group; ssh-exec --task itself is gone."""
    sent, out, err = [], io.BytesIO(), io.StringIO()
    log = tmp_path / "exec.jsonl"

    def notify(level, key, params, card):
        sent.append((level, key, params, card))
        return True

    args = list(args)
    agent = args[args.index("--agent") + 1] if "--agent" in args else "unknown"
    fields = extra if "--task" in args else {}
    target, command = args[-2], args[-1]
    report = execute.reporter(notify, fields.get("task_id"))
    try:
        if "," in target:
            servers = inventory.exec_targets(target, None, inventory.ENABLED)
            code = execute._execute_batch(agent, servers, command, log, runner, report, out, err,
                                          clock or time.monotonic, extra=fields)
        else:
            code = execute._execute(agent, inventory.get(target, None, inventory.ENABLED), command, log, runner,
                                    report, out, err, extra=fields)
    except execute.Interrupted:  # before the start: nothing ran (main says so)
        code = 130
    finally:
        execute._state["armed"] = False
    return code, out.getvalue(), err.getvalue(), sent


@pytest.fixture
def task():
    """The step belongs to a card: ``run`` passes STEP as the step's fields when ``--task`` is among the args."""


def test_a_step_with_a_task_opens_snapshots_and_closes_its_window(tmp_path, cab, task):
    runner = Runner([{}, {"unit": "b" * 64, "port": 443}])
    code, out, err, sent = run(
        [
            "--agent",
            "sysadmin",
            "--task",
            "KD-5",
            "example-stage",
            "apt-get -y upgrade",
        ],
        tmp_path,
        runner,
    )
    assert (
        code == 0
        and out == b"done\n"
        and runner.calls == ["snapshot", "step", "snapshot"]
    )
    opened, closed = cab.calls
    assert opened == {
        "op": "monitor.window.open",
        "server": "example-stage",
        "card": "KD-5",
        "step": "операция 1/1",
        "run": opened["run"],
        "id": opened["id"],
    }
    assert re.fullmatch(r"[0-9a-f]{16}", opened["id"])
    assert closed["op"] == "monitor.window.close" and closed["id"] == "w1"
    assert closed["changes"] == {
        "units:x.service": ["a" * 64, "b" * 64],
        "ports:tcp:0.0.0.0:80": ["nginx", None],
        "ports:tcp:0.0.0.0:443": [None, "nginx"],
    }
    assert closed["errors"] == ["cron"]


def test_a_step_without_a_task_opens_no_window(tmp_path, cab):
    runner = Runner()
    run(["--agent", "sysadmin", "example-stage", "true"], tmp_path, runner)
    assert runner.calls == ["step"] and cab.calls == []


def test_the_cabinet_down_still_runs_the_step(tmp_path, monkeypatch, task):
    monkeypatch.setattr(
        execute, "WINDOWS", plan_window.Windows(client=Cabinet(down=True))
    )
    runner = Runner()
    code, out, err, sent = run(
        ["--agent", "sysadmin", "--task", "KD-5", "example-stage", "true"],
        tmp_path,
        runner,
    )
    assert code == 0 and out == b"done\n" and runner.calls == ["step"]
    assert "окно шага плана на example-stage не открыто" in err
    assert ("info", "notice.exec.window_failed") in [s[:2] for s in sent]


def test_a_failed_snapshot_closes_the_window_as_failed_and_runs_the_step(
    tmp_path, cab, task
):
    runner = Runner([{"snap_rc": 1}])
    code, out, err, sent = run(
        ["--agent", "sysadmin", "--task", "KD-5", "example-stage", "true"],
        tmp_path,
        runner,
    )
    assert code == 0 and runner.calls == ["snapshot", "step"]
    assert (
        cab.calls[1]["op"] == "monitor.window.close"
        and "снимок" in cab.calls[1]["failed"]
    )


def test_a_failed_snapshot_after_the_step_closes_the_window_as_failed(
    tmp_path, cab, task
):
    runner = Runner([{}, {"snap_rc": 1}])
    code, out, err, sent = run(
        ["--agent", "sysadmin", "--task", "KD-5", "example-stage", "true"],
        tmp_path,
        runner,
    )
    assert code == 0 and runner.calls == ["snapshot", "step", "snapshot"]
    assert cab.calls[1]["failed"] and cab.calls[1]["changes"] == {}
    # review 4: the card hears that the step's changes are checked as usual
    assert [s[1] for s in sent if s[1].startswith("notice.exec.window")] == [
        "notice.exec.window_unclosed"
    ]
    assert "окно шага плана на example-stage не закрыто" in err


def test_an_interrupted_step_closes_its_window_as_failed(tmp_path, cab, task):
    runner = Runner([{}], step=None)

    def interrupted(argv, timeout, limit, kill_on_limit):
        if "@@kd-window-" in argv[-1]:
            return Runner.__call__(runner, argv, timeout, limit, kill_on_limit)
        raise execute.Interrupted("SIGTERM")

    code, out, err, sent = run(
        ["--agent", "sysadmin", "--task", "KD-5", "example-stage", "true"],
        tmp_path,
        interrupted,
    )
    assert code == 130 and cab.calls[1]["failed"] == "прервано"


def test_a_group_step_opens_a_window_per_server_with_one_run(tmp_path, cab, task):
    runner = Runner()
    code, out, err, sent = run(
        ["--agent", "sysadmin", "--task", "KD-5", "node-f,node-h", "true"],
        tmp_path,
        runner,
    )
    assert code == 0 and runner.calls == ["snapshot", "step", "snapshot"] * 2
    opens = [c for c in cab.calls if c["op"] == "monitor.window.open"]
    assert [o["server"] for o in opens] == ["node-f", "node-h"] and opens[0][
        "run"
    ] == opens[1]["run"]


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_a_short_budget_skips_the_window_not_the_step(tmp_path, cab, task):
    clock = Clock()

    class Spend(Runner):
        def __call__(self, argv, timeout, limit, kill_on_limit):
            result = super().__call__(argv, timeout, limit, kill_on_limit)
            if self.calls[-1] == "step":
                clock.now += (
                    execute.TIMEOUT - execute.MIN_SLOT - 30
                )  # the next server has 30 s over its minimum
            return result

    runner = Spend()
    code, out, err, sent = run(["--agent", "sysadmin", "--task", "KD-5", "node-f,node-h", "true"], tmp_path, runner,
                               clock=clock)
    assert code == 0 and runner.calls == ["snapshot", "step", "snapshot", "step"]
    assert [c["server"] for c in cab.calls if c["op"] == "monitor.window.open"] == [
        "node-f"
    ]
    # review 4: the skipped window is told, as one that did not open
    (failed,) = [a for a in sent if a[1] == "notice.exec.window_failed"]
    assert failed[2]["server"] == "node-h" and "мало времени" in failed[2]["reason"]
    assert "окно шага плана на node-h не открыто" in err


def test_windows_off_by_default_in_the_other_tests():
    assert isinstance(execute.WINDOWS, plan_window.Off)


def test_the_window_never_reaches_the_journal_head(tmp_path, cab, task):
    runner = Runner([{}, {}])
    run(
        ["--agent", "sysadmin", "--task", "KD-5", "example-stage", "true"],
        tmp_path,
        runner,
    )
    records = [
        json.loads(line)
        for line in (tmp_path / "exec.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [r["event"] for r in records] == ["start", "result"] and records[1][
        "head"
    ] == "done"


class SlowCabinet(Cabinet):
    """The open call takes ``spend`` seconds of the group's clock, then fails."""

    def __init__(self, clock, spend):
        super().__init__()
        self.clock, self.spend = clock, spend

    def call(self, request):
        if request["op"] == "monitor.window.open" and request["server"] == "node-h":
            self.clock.now += self.spend
            raise cabinet_client.CabinetError("unavailable", "timeout")
        return super().call(request)


def _group(tmp_path, monkeypatch, spend, task_runner=None):
    clock = Clock()
    monkeypatch.setattr(
        execute, "WINDOWS", plan_window.Windows(client=SlowCabinet(clock, spend))
    )
    timeouts = []

    class Spend(Runner):
        def __call__(self, argv, timeout, limit, kill_on_limit):
            result = super().__call__(argv, timeout, limit, kill_on_limit)
            if self.calls[-1] == "step":
                timeouts.append(timeout)
                if len(timeouts) == 1:
                    clock.now += (
                        execute.TIMEOUT - execute.MIN_SLOT - 100
                    )  # node-h starts with MIN_SLOT + 100 s
            return result

    runner = Spend()
    code, *_ = run(["--agent", "sysadmin", "--task", "KD-5", "node-f,node-h", "true"], tmp_path, runner, clock=clock)
    return code, timeouts, clock


def test_a_slow_failed_window_leaves_the_step_only_what_is_left(
    tmp_path, monkeypatch, task
):
    """Review 8: the time a failed open took comes out of the step's timeout."""
    code, timeouts, clock = _group(tmp_path, monkeypatch, spend=30)
    assert code == 0 and len(timeouts) == 2
    assert timeouts[1] <= execute.MIN_SLOT + 100 - 30


def test_a_window_that_ate_the_minute_stops_the_group(tmp_path, monkeypatch, task):
    code, timeouts, clock = _group(tmp_path, monkeypatch, spend=120)
    assert code == 124 and len(timeouts) == 1


def test_a_signal_during_the_open_call_closes_the_window_it_named(
    tmp_path, task, monkeypatch
):
    """Review 9: the executor names its window, so a signal inside the open call still closes it."""

    class Interrupting(Cabinet):
        def call(self, request):
            if request["op"] == "monitor.window.open":
                self.calls.append(request)
                raise execute.Interrupted("SIGTERM")
            return super().call(request)

    cab = Interrupting()
    monkeypatch.setattr(execute, "WINDOWS", plan_window.Windows(client=cab))
    runner = Runner()
    code, out, err, sent = run(
        ["--agent", "sysadmin", "--task", "KD-5", "example-stage", "true"],
        tmp_path,
        runner,
    )
    opened, closed = cab.calls
    assert code == 130 and runner.calls == []
    assert closed == {
        "op": "monitor.window.close",
        "id": opened["id"],
        "changes": {},
        "errors": [],
        "failed": "прервано до шага",
    }


# --- a window takes only what the step's kind may change (KD-9483) -----------------------------------------------
# Anything else the server changed while the step ran is no step's: it stays out of the baseline and is checked as
# usual. A step of the test zone carries no owner's signature and opens no window at all.

def _step(verb, **more):
    return dict(STEP, operation={"server": "example-stage", "verb": verb}, **more)


ROOT_CRON = "cron:/var/spool/cron/crontabs/root"


def test_a_typed_step_hands_over_only_what_its_kind_changes(tmp_path, cab, task):
    runner = Runner([{}, {"unit": "b" * 64, "port": 443}])  # a unit file changed too, meanwhile
    code, *_ = run(["--task", "KD-5", "example-stage", "systemctl restart nginx.service"], tmp_path, runner,
                   extra=_step("service.restart"))
    opened, closed = cab.calls
    assert code == 0 and runner.calls == ["snapshot", "step", "snapshot"]
    assert closed["changes"] == {"ports:tcp:0.0.0.0:80": ["nginx", None], "ports:tcp:0.0.0.0:443": [None, "nginx"]}


@pytest.mark.parametrize("extra", [
    _step("app.tool"), _step("app.flag"), _step("file.drop"), _step("gate.script"), _step("package.refresh"),
    _step("no.such.verb"), dict(STEP, operation=None),
    _step("service.reload", test_zone=True), _step("app.tool", test_zone=True), _step("script", test_zone=True),
])
def test_a_step_with_nothing_to_hand_over_opens_no_window(tmp_path, cab, task, extra):
    runner = Runner()
    code, *_ = run(["--task", "KD-5", "example-stage", "true"], tmp_path, runner, extra=extra)
    assert code == 0 and runner.calls == ["step"] and cab.calls == []


def test_a_script_and_a_critical_action_hand_over_every_object(tmp_path, cab, task):
    critical = {"kind": "critical", "task_id": "KD-5", "proposal_id": "p", "action_id": "a"}
    for extra in (STEP, critical):
        cab.calls.clear()
        runner = Runner([{}, {"unit": "b" * 64, "port": 443}])
        run(["--task", "KD-5", "example-stage", "true"], tmp_path, runner, extra=extra)
        assert set(cab.calls[1]["changes"]) == {"units:x.service", "ports:tcp:0.0.0.0:80", "ports:tcp:0.0.0.0:443"}


@pytest.mark.parametrize("verb, key, taken", [
    ("cron.line", ROOT_CRON, True),
    ("cron.line", "cron:/var/spool/cron/root", True),
    ("cron.line", "cron:/etc/cron.d/x", False),
    ("cron.line", "cron:/var/spool/cron/crontabs/www-data", False),
    ("cron.line", "units:x.service", False),
    ("package.install", "users:svc", True),
    ("package.install", "units:multi-user.target.wants/x.service", True),
    ("package.install", "cron:/etc/cron.d/x", True),
    ("package.install", "ports:tcp:0.0.0.0:80", True),
    ("package.install", "sshd:file:/etc/ssh/sshd_config", False),
    ("package.install", "persist:/etc/rc.local", False),
    ("package.upgrade", "shadow:/etc/shadow", True),
    ("service.start", "wgpeers:wg0:x", True),
    ("service.start", "fw:ufw", False),
    ("service.reload", "units:x.service", False),
])
def test_what_each_kind_may_hand_over(verb, key, taken):
    assert plan_window.within(key, plan_window.scope(_step(verb))) is taken
    assert plan_window.within(key, plan_window.scope(_step("script")))
