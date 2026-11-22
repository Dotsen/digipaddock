import io
import json
import time
import os
import signal

import pytest

from gate.agent_ro_gate import Result
from sysadmin import denylist, execute, inventory


@pytest.fixture(autouse=True)
def all_servers_enabled(monkeypatch, enabled_all):
    monkeypatch.setattr(inventory, "ENABLED", enabled_all)


class Runner:
    def __init__(self, result=None, raises=None):
        self.result = result or Result(0, b"done\n", False, False)
        self.raises = raises
        self.calls, self.feeds = [], []

    def __call__(self, argv, timeout, limit, kill_on_limit, feed=None):
        self.calls.append((argv, timeout, limit, kill_on_limit))
        self.feeds.append(list(feed) if feed else None)
        if self.raises:
            raise self.raises
        return self.result


def parse(args):
    """ssh-exec's argument handling, for the step runs: the ``--agent`` label, one server or a group, the command.
    the command line itself only refuses — kd-admin-run and kd-admin-critical call ``run_step``."""
    args, labels = list(args), []
    while args and args[0] == "--agent":
        labels.append(args[1])
        del args[:2]
    label = labels[0] if labels else "unknown"
    agent = label if execute.AGENT_RE.fullmatch(label) else "unknown"
    spec, command = args[0], inventory.join_command(args[1:])
    if "," in spec or spec == "all":
        servers = inventory.exec_targets(spec, None, inventory.ENABLED)
    else:
        servers = [inventory.get(spec, None, inventory.ENABLED)]
    return agent, servers, command


def run(args, tmp_path, runner=None, notify_ok=True, clock=None, log=None):
    runner = runner or Runner()
    sent, out, err = [], io.BytesIO(), io.StringIO()
    log = log or tmp_path / "exec.jsonl"

    def notify(level, key, params, card):
        sent.append((level, key, params, card))
        return notify_ok

    try:
        agent, servers, command = parse(args)
    except inventory.InventoryError as exc:
        print(f"ssh-exec: {exc}", file=err)
        code = 2
    else:
        timing = {"clock": clock} if clock is not None else {}
        code = execute.run_step(agent, servers, command, log_path=log, runner=runner, notify=notify, out=out, err=err,
                                **timing)
    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    return code, out.getvalue(), err.getvalue(), runner.calls, sent, records


def test_runs_command_over_exec_key_and_logs_start_and_result(tmp_path):
    code, out, err, calls, sent, log = run(
        ["--agent", "sysadmin", "example-stage", "systemctl restart testapi-partner.example.test"], tmp_path)
    assert code == 0 and out == b"done\n"
    (argv, timeout, limit, kill_on_limit), = calls
    assert argv[:5] == ["/usr/bin/ssh", "-F", "/dev/null", "-i", "/home/digi-exec/.ssh/agent_exec_ed25519"]
    assert "UserKnownHostsFile=/home/digi-exec/.ssh/known_hosts" in argv
    assert argv[-3:] == ["root@203.0.113.10", "--", "systemctl restart testapi-partner.example.test"]
    assert timeout == 600 and limit == 1024 * 1024
    assert kill_on_limit is False  # truncating the output does not kill an approved command
    assert [r["event"] for r in log] == ["start", "result"]
    assert log[0]["id"] == log[1]["id"] and log[0]["agent"] == "sysadmin" and log[0]["server"] == "example-stage"
    assert log[1]["rc"] == 0 and log[1]["head"] == "done"
    assert sent[0][:2] == ("info", "notice.exec.result") and sent[0][2]["server"] == "example-stage"


def test_denied_command_never_reaches_ssh(tmp_path):
    code, out, err, calls, sent, log = run(["--agent", "sysadmin", "example-prod", "iptables -F"], tmp_path)
    assert code == 126 and calls == []
    assert [r["event"] for r in log] == ["denied"] and log[0]["rule"] == "firewall-flush"
    assert "firewall-flush" in err
    assert sent[0][:2] == ("warn", "notice.exec.denied") and sent[0][2]["rule"] == "firewall-flush"


def test_denylist_failure_fails_closed(tmp_path, monkeypatch):
    def broken(command, server):
        raise RuntimeError("сбой")

    monkeypatch.setattr(denylist, "check", broken)
    code, _, err, calls, sent, log = run(["--agent", "sysadmin", "example-stage", "uptime"], tmp_path)
    assert code == 126 and calls == []
    assert [r["event"] for r in log] == ["denied"] and log[0]["rule"] == "ошибка проверки"
    assert "ошибка проверки" in err
    assert sent[0][1] == "notice.exec.denied" and sent[0][2]["rule"] == "ошибка проверки"


def test_unknown_server_is_refused_before_anything(tmp_path):
    code, _, err, calls, sent, log = run(["--agent", "sysadmin", "node-mrs1", "uptime"], tmp_path)
    assert code == 2 and calls == [] and sent == [] and log == []
    assert "нет в инвентаре" in err


def test_empty_command_is_refused(tmp_path):
    code, _, _, calls, _, log = run(["example-stage"], tmp_path)
    assert code == 2 and calls == [] and log == []


def test_jed1_runs_as_ubuntu_through_sudo(tmp_path):
    _, _, _, calls, _, _ = run(["--agent", "sysadmin", "node-a", "systemctl restart wg-quick@wg0"], tmp_path)
    assert calls[0][0][-3:] == ["ubuntu@203.0.113.30", "--", "sudo -n bash -c 'systemctl restart wg-quick@wg0'"]


def test_argv_form_is_quoted_like_ssh_ro(tmp_path):
    _, _, _, calls, _, log = run(["--agent", "sysadmin", "example-stage", "journalctl", "--since", "1 hour ago"],
                                 tmp_path)
    assert calls[0][0][-1] == "journalctl --since '1 hour ago'"
    assert log[0]["command"] == "journalctl --since '1 hour ago'"


def test_denylist_knows_the_server(tmp_path):
    assert run(["--agent", "sysadmin", "example-stage", "reboot"], tmp_path)[0] == 126
    assert run(["--agent", "sysadmin", "example-prod", "reboot"], tmp_path)[0] == 0


def test_timeout_is_reported(tmp_path):
    code, out, _, _, sent, log = run(["--agent", "sysadmin", "example-stage", "sleep 9999"], tmp_path,
                                     Runner(Result(-9, b"", False, True)))
    assert code == 124 and log[1]["timed_out"] is True
    assert sent[0][1] == "notice.exec.timeout" and sent[0][2]["server"] == "example-stage"
    assert "таймаут".encode() in out


def test_notice_failure_does_not_change_result(tmp_path):
    code, out, *_ = run(["--agent", "sysadmin", "example-stage", "true"], tmp_path, notify_ok=False)
    assert code == 0 and out == b"done\n"


def test_a_step_with_a_card_posts_its_result_to_the_card(tmp_path):
    # the run of a plan step (kd-admin-run, kd-admin-critical): ``extra`` names the card, the notice goes to its thread
    runner, sent, out, err = Runner(), [], io.BytesIO(), io.StringIO()
    notify = lambda level, key, params, card: sent.append((level, key, params, card)) or True  # noqa: E731
    code = execute._execute("sysadmin", inventory.get("example-stage", None, inventory.ENABLED), "uptime", tmp_path / "exec.jsonl", runner,
                            execute.reporter(notify, "KD-7"), out, err,
                            extra={"kind": "admin", "task_id": "KD-7", "grant_id": "g1"})
    assert code == 0 and len(sent) == 1
    level, key, params, card = sent[0]
    assert (level, key, card) == ("info", "notice.exec.result", "KD-7")
    assert params["agent"] == "@sysadmin" and params["server"] == "example-stage" and params["command"] == "uptime"


def test_default_poster_posts_as_exec_and_only_with_a_card(monkeypatch):
    calls = []
    monkeypatch.setattr(execute.cabinet_client, "post_notice",
                        lambda *args, **kwargs: calls.append((args, kwargs)) or True)
    assert execute.post_notice("info", "notice.exec.result", {"x": 1}, None) is False and calls == []
    assert execute.post_notice("info", "notice.exec.result", {"x": 1}, "KD-7") is True
    assert calls == [(("exec", "info", "notice.exec.result", {"x": 1}), {"card": "KD-7"})]


def test_bad_agent_label_becomes_unknown(tmp_path):
    *_, log = run(["--agent", "Evil Name", "example-stage", "true"], tmp_path)
    assert log[0]["agent"] == "unknown"


def test_second_agent_label_cannot_override_wrapper(tmp_path):
    *_, log = run(["--agent", "sysadmin", "--agent", "other", "example-stage", "true"], tmp_path)
    assert log[0]["agent"] == "sysadmin"


def test_server_without_gate_is_refused(tmp_path, monkeypatch):
    only_stage = tmp_path / "only-stage"
    only_stage.write_text("example-stage\n", encoding="utf-8")
    monkeypatch.setattr(inventory, "ENABLED", only_stage)
    code, _, err, calls, _, log = run(["--agent", "sysadmin", "example-prod", "uptime"], tmp_path)
    assert code == 2 and calls == [] and log == [] and "не подключён" in err


def test_truncated_output_is_marked(tmp_path):
    code, out, _, _, _, log = run(["--agent", "sysadmin", "example-stage", "journalctl"], tmp_path,
                                  Runner(Result(0, b"x" * 10, True, False)))
    assert code == 0 and "обрезан".encode() in out and log[1]["truncated"] is True


def test_interrupted_run_is_journaled_and_reported(tmp_path):
    runner = Runner(raises=execute.Interrupted("SIGTERM"))
    code, _, err, _, sent, log = run(["--agent", "sysadmin", "example-stage", "apt-get -y upgrade"], tmp_path, runner)
    assert code == 130
    assert [r["event"] for r in log] == ["start", "result"] and log[1]["interrupted"] == "SIGTERM"
    assert sent[0][1] == "notice.exec.interrupted" and sent[0][2]["reason"] == "SIGTERM"
    assert "прервано" in err


def test_unwritable_journal_blocks_execution(tmp_path):
    runner = Runner()
    with pytest.raises(OSError):
        run(["example-stage", "true"], tmp_path, runner, log=tmp_path / "missing" / "exec.jsonl")
    assert runner.calls == []


@pytest.fixture
def real_signal_handlers():
    names = [n for n in ("SIGTERM", "SIGHUP", "SIGINT") if hasattr(signal, n)]
    saved = {n: signal.getsignal(getattr(signal, n)) for n in names}
    execute.install_signal_handlers()
    yield
    for n, handler in saved.items():
        signal.signal(getattr(signal, n), handler)
    execute._state.update(armed=False, pending=None)  # a pending from a real signal must not live into another test


def test_real_signal_during_runner_is_journaled(tmp_path, real_signal_handlers):
    class Raiser(Runner):
        def __call__(self, *a, **k):
            signal.raise_signal(signal.SIGTERM)

    code, _, _, _, sent, log = run(["--agent", "sysadmin", "example-stage", "sleep 5"], tmp_path, Raiser())
    assert code == 130 and [r["event"] for r in log] == ["start", "result"]
    assert log[1]["interrupted"] == "SIGTERM" and sent[0][1] == "notice.exec.interrupted"


def test_second_signal_during_handling_keeps_single_result(tmp_path, real_signal_handlers):
    class Raiser(Runner):
        def __call__(self, *a, **k):
            signal.raise_signal(signal.SIGTERM)

    sent, out, err = [], io.BytesIO(), io.StringIO()
    log = tmp_path / "exec.jsonl"

    def notify(*notice):
        signal.raise_signal(signal.SIGTERM)  # a second signal while the first is being handled
        sent.append(notice)
        return True

    code = execute.run_step("sysadmin", [inventory.get("example-stage", None, inventory.ENABLED)], "sleep 5", log_path=log, runner=Raiser(),
                            notify=notify, out=out, err=err)
    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert code == 130 and [r["event"] for r in records] == ["start", "result"] and len(sent) == 1


def test_signal_after_runner_returns_still_writes_result(tmp_path, real_signal_handlers):
    class Late(Runner):
        def __call__(self, *a, **k):
            return Result(0, b"done\n", False, False)

    sent, log = [], tmp_path / "exec.jsonl"

    def notify(*notice):
        sent.append(notice)
        return True

    original = execute.journal

    def journal_with_signal(path, **record):
        if record["event"] == "start":
            original(path, **record)
            return
        signal.raise_signal(signal.SIGTERM)  # a signal right before the outcome is written
        original(path, **record)

    execute.journal, saved = journal_with_signal, execute.journal
    try:
        code = execute.run_step("sysadmin", [inventory.get("example-stage", None, inventory.ENABLED)], "true", log_path=log, runner=Late(),
                                notify=notify, out=io.BytesIO(), err=io.StringIO())
    finally:
        execute.journal = saved
    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [r["event"] for r in records] == ["start", "result"]
    assert code == 130


def test_signal_during_denylist_check_is_not_a_denial(tmp_path, monkeypatch, real_signal_handlers):
    def check(command, server):
        signal.raise_signal(signal.SIGTERM)

    monkeypatch.setattr(denylist, "check", check)
    code, _, err, calls, sent, log = run(["--agent", "sysadmin", "example-stage", "uptime"], tmp_path)
    assert code == 130 and calls == [] and log == [] and "прервано" in err


def test_sigint_is_handled_like_sigterm(tmp_path, real_signal_handlers):
    class Raiser(Runner):
        def __call__(self, *a, **k):
            signal.raise_signal(signal.SIGINT)

    code, _, _, _, _, log = run(["--agent", "sysadmin", "example-stage", "sleep 5"], tmp_path, Raiser())
    assert code == 130 and log[1]["interrupted"] == "SIGINT"


def test_non_utf8_command_is_journaled_and_sent(tmp_path):
    code, *_, sent, log = run(["--agent", "sysadmin", "example-stage", "echo \udcff"], tmp_path)
    assert code == 0 and [r["event"] for r in log] == ["start", "result"] and sent


def test_non_utf8_denied_command_is_journaled_and_sent(tmp_path):
    code, _, _, calls, sent, log = run(["--agent", "sysadmin", "example-prod", "iptables -F \udcff"], tmp_path)
    assert code == 126 and log[0]["event"] == "denied" and sent[0][1] == "notice.exec.denied"


def test_agent_label_with_trailing_newline_is_unknown(tmp_path):
    *_, log = run(["--agent", "sysadmin\n", "example-stage", "true"], tmp_path)
    assert log[0]["agent"] == "unknown"


def test_result_write_failure_still_returns_output_and_warns(tmp_path, monkeypatch):
    original = execute.journal

    def flaky(path, **record):
        if record["event"] == "result":
            raise OSError("диск полон")
        original(path, **record)

    monkeypatch.setattr(execute, "journal", flaky)
    code, out, err, _, sent, _ = run(["--agent", "sysadmin", "example-stage", "true"], tmp_path)
    assert code == 1 and out == b"done\n" and sent and "журнал не записан" in err


def test_single_server_records_have_no_batch_fields(tmp_path):
    *_, log = run(["--agent", "sysadmin", "example-stage", "true"], tmp_path)
    assert [r["event"] for r in log] == ["start", "result"]
    assert all(not {"batch", "batch_index", "batch_size"} & set(r) for r in log)


# --- the group ssh-exec: one card for one change on several servers ---------------------------

GROUP = "node-f,node-h,node-g"
HOSTS = ["root@203.0.113.35", "root@203.0.113.37", "root@203.0.113.36"]
CMD = "systemctl restart danted"
OK = Result(0, b"done\n", False, False)


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class Seq(Runner):
    """Outcomes in turn (a Result or an exception; after them — OK); spend — how many seconds each call "takes"."""

    def __init__(self, *outcomes, clock=None, spend=()):
        super().__init__()
        self.outcomes, self.clock, self.spend = list(outcomes), clock, list(spend)

    def __call__(self, argv, timeout, limit, kill_on_limit):
        i = len(self.calls)
        self.calls.append((argv, timeout, limit, kill_on_limit))
        if self.clock is not None and i < len(self.spend):
            self.clock.now += self.spend[i]
        outcome = self.outcomes[i] if i < len(self.outcomes) else OK
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def run_group(spec, tmp_path, runner=None, clock=None, command=CMD, log=None):
    runner = runner if runner is not None else Seq()
    code, out, err, calls, sent, records = run(["--agent", "sysadmin", spec, command], tmp_path, runner, clock=clock,
                                               log=log)
    return code, out.decode("utf-8"), err, calls, sent, records


def test_group_runs_servers_in_order_under_one_batch(tmp_path):
    code, out, err, calls, sent, log = run_group(GROUP, tmp_path)
    assert code == 0
    assert [c[0][-3] for c in calls] == HOSTS
    assert all(c[0][-1] == CMD and c[2] == 1024 * 1024 and c[3] is False for c in calls)
    assert 590 < calls[0][1] <= 600  # the shared budget is 600 s: the first server gets almost all of it
    assert [r["event"] for r in log] == ["start", "result"] * 3 + ["batch_result"]
    batch = log[0]["batch"]
    assert batch and all(r["batch"] == batch for r in log)
    assert [r["batch_index"] for r in log[:-1]] == [1, 1, 2, 2, 3, 3]
    assert all(r["batch_size"] == 3 for r in log[:-1])
    starts = [r for r in log if r["event"] == "start"]
    assert [r["server"] for r in starts] == GROUP.split(",")
    assert all(r["agent"] == "sysadmin" and r["command"] == CMD for r in starts)
    assert [log[i]["id"] for i in (0, 2, 4)] == [log[i]["id"] for i in (1, 3, 5)]
    assert len({log[i]["id"] for i in (0, 2, 4)}) == 3
    assert {k: v for k, v in log[-1].items() if k != "ts"} == {
        "event": "batch_result", "batch": batch, "ok": GROUP.split(","), "failed": None, "not_run": [],
        "reason": None}
    assert out == "=== node-f\ndone\n=== node-h\ndone\n=== node-g\ndone\nитог: успешно 3 из 3\n"
    assert len(sent) == 1 and sent[0][:2] == ("info", "notice.exec.batch_ok") and sent[0][2]["count"] == 3
    assert sent[0][2]["servers"] == "node-f, node-h, node-g"


def test_group_stops_at_first_failure(tmp_path):
    runner = Seq(OK, Result(1, b"Job failed", False, False))
    code, out, err, calls, sent, log = run_group(GROUP, tmp_path, runner)
    assert code == 1 and len(calls) == 2
    assert out == ("=== node-f\ndone\n=== node-h\nJob failed\n(код 1)\n"
                   "итог: остановлено на node-h (код 1); выполнено: node-f; не выполнялось: node-g\n")
    assert [r["event"] for r in log] == ["start", "result", "start", "result", "batch_result"]
    assert log[3]["rc"] == 1
    assert (log[-1]["ok"], log[-1]["failed"], log[-1]["not_run"]) == (["node-f"], "node-h", ["node-g"])
    assert len(sent) == 1 and sent[0][1] == "notice.exec.batch_failed"
    assert [sent[0][2][k] for k in ("failed", "why", "done", "not_run")] == ["node-h", "код 1", "node-f",
                                                                          "node-g"]


def test_group_failure_on_first_server_runs_nothing_else(tmp_path):
    code, out, _, calls, sent, log = run_group(GROUP, tmp_path, Seq(Result(3, b"", False, False)))
    assert code == 3 and len(calls) == 1
    assert out.endswith("итог: остановлено на node-f (код 3); выполнено: —; не выполнялось: node-h, node-g\n")
    assert log[-1]["not_run"] == ["node-h", "node-g"] and log[-1]["ok"] == []


def test_group_denylist_hit_on_any_server_runs_nothing(tmp_path):
    code, out, err, calls, sent, log = run_group("node-f,example-stage,node-h", tmp_path, command="reboot")
    assert code == 126 and calls == [] and out == ""
    assert [r["event"] for r in log] == ["denied"]
    assert (log[0]["server"], log[0]["rule"]) == ("example-stage", "stage-power")
    assert log[0]["servers"] == ["node-f", "example-stage", "node-h"] and log[0]["command"] == "reboot"
    assert len(sent) == 1 and sent[0][:2] == ("warn", "notice.exec.batch_denied")
    assert (sent[0][2]["rule"], sent[0][2]["server"]) == ("stage-power", "example-stage")
    assert "stage-power" in err and "example-stage" in err


def test_group_denylist_failure_fails_closed(tmp_path, monkeypatch):
    def broken(command, server):
        raise RuntimeError("сбой")

    monkeypatch.setattr(denylist, "check", broken)
    code, _, _, calls, sent, log = run_group(GROUP, tmp_path)
    assert code == 126 and calls == []
    assert [r["event"] for r in log] == ["denied"] and log[0]["rule"] == "ошибка проверки"
    assert (sent[0][1], sent[0][2]["rule"], sent[0][2]["server"]) == ("notice.exec.batch_denied", "ошибка проверки",
                                                                       "node-f")


SIXTEEN = ",".join(f"node-x{i}" for i in range(16))


@pytest.mark.parametrize("spec,why", [
    ("example-prod,node-f", "example-prod — только последним в списке"),
    ("node-f,example-prod,node-h", "example-prod — только последним в списке"),
    ("all", "all для ssh-exec не принимается"),
    ("node-f,all", "all для ssh-exec не принимается"),
    ("node-f,node-h,node-f", "дважды"),
    ("node-f,,node-h", "пустой"),
    ("node-f,", "пустой"),
    (",node-f", "пустой"),
    (SIXTEEN, "от 2 до 15"),
    ("node-f,Node-b2", "неверное имя"),
    ("node-f,node-mrs1", "'node-mrs1' нет в инвентаре"),
])
def test_group_bad_list_is_refused_before_anything(spec, why, tmp_path):
    code, out, err, calls, sent, log = run_group(spec, tmp_path)
    assert code == 2 and calls == [] and sent == [] and log == [] and out == ""
    assert why in err


def test_group_with_server_without_gate_is_refused_before_anything(tmp_path, monkeypatch):
    on = tmp_path / "on"
    on.write_text("node-f\nnode-g\n", encoding="utf-8")
    monkeypatch.setattr(inventory, "ENABLED", on)
    code, _, err, calls, sent, log = run_group(GROUP, tmp_path)
    assert code == 2 and calls == [] and sent == [] and log == []
    assert "'node-h'" in err and "не подключён" in err


def test_group_needs_command(tmp_path):
    code, _, _, calls, _, log = run_group(GROUP, tmp_path, command=" ")
    assert code == 2 and calls == [] and log == []


def test_group_with_prod_last_runs_prod_last(tmp_path):
    code, _, _, calls, _, _ = run_group("node-f,example-prod", tmp_path)
    assert code == 0 and [c[0][-3] for c in calls] == ["root@203.0.113.35", "root@203.0.113.20"]


def test_group_keeps_sudo_for_jed1(tmp_path):
    _, _, _, calls, _, _ = run_group("node-f,node-a", tmp_path)
    assert calls[0][0][-1] == CMD
    assert calls[1][0][-3:] == ["ubuntu@203.0.113.30", "--", f"sudo -n bash -c '{CMD}'"]


# --- an envelope goes to the target's exec gate ------------------------------------------------------------

def test_an_envelope_goes_to_the_gate_word_without_sudo(tmp_path):
    """With an envelope the remote command is the gate's word and the envelope is fed on stdin; the gate sudo's
    itself on a non-root exec user, so the legacy sudo wrapping is not applied."""
    runner, out, err, log = Runner(), io.BytesIO(), io.StringIO(), tmp_path / "exec.jsonl"
    server = inventory.get("node-a", None, inventory.ENABLED)
    code = execute._execute("sysadmin", server, "systemctl restart wg-quick@wg0", log, runner,
                            lambda *notice: True, out, err, envelope=b'{"kind": "plan"}')
    assert code == 0, err.getvalue()
    argv = runner.calls[0][0]
    assert argv[-3:] == ["ubuntu@203.0.113.30", "--", "agent-exec-gate run"] and runner.feeds == [[b'{"kind": "plan"}']]
    start, result = [json.loads(line) for line in log.read_text("utf-8").splitlines()]
    assert start["envelope"] is True and start["command"] == "systemctl restart wg-quick@wg0" and result["rc"] == 0
    # without an envelope: as before, no feed, the legacy record says so
    runner, log = Runner(), tmp_path / "exec2.jsonl"
    execute._execute("sysadmin", server, CMD, log, runner, lambda *notice: True, io.BytesIO(), io.StringIO())
    assert runner.calls[0][0][-1] == f"sudo -n bash -c '{CMD}'" and runner.feeds == [None]
    assert json.loads(log.read_text("utf-8").splitlines()[0])["envelope"] is False


def test_a_group_builds_one_envelope_per_server(tmp_path):
    runner, out, err, log = Runner(), io.BytesIO(), io.StringIO(), tmp_path / "exec.jsonl"
    servers = [inventory.get(alias, None, inventory.ENABLED) for alias in ("node-f", "node-a")]
    code = execute._execute_batch("sysadmin", servers, CMD, log, runner, lambda *notice: True, out, err,
                                  time.monotonic, envelopes=lambda server: b"envelope for " + server.alias.encode())
    assert code == 0, err.getvalue()
    assert [c[0][-1] for c in runner.calls] == ["agent-exec-gate run"] * 2
    assert runner.feeds == [[b"envelope for node-f"], [b"envelope for node-a"]]
    starts = [json.loads(line) for line in log.read_text("utf-8").splitlines() if '"start"' in line]
    assert [s["server"] for s in starts] == ["node-f", "node-a"] and all(s["envelope"] is True for s in starts)


def test_group_shares_one_time_budget(tmp_path):
    clock = Clock()
    runner = Seq(clock=clock, spend=[250, 350])
    code, out, _, calls, sent, log = run_group(GROUP, tmp_path, runner, clock)
    assert [c[1] for c in calls] == [600, 350]  # the second gets the rest of the shared budget
    assert code == 124
    assert [r["duration_s"] for r in log if r["event"] == "result"] == [250.0, 350.0]
    assert out.endswith("итог: остановлено; выполнено: node-f, node-h; не выполнялось (время вышло): node-g\n")
    assert (log[-1]["ok"], log[-1]["failed"], log[-1]["not_run"], log[-1]["reason"]) == (
        ["node-f", "node-h"], None, ["node-g"], "время вышло")
    assert len(sent) == 1 and sent[0][1] == "notice.exec.batch_stopped"
    assert [sent[0][2][k] for k in ("why", "done", "not_run")] == ["время вышло", "node-f, node-h", "node-g"]


def test_group_timeout_on_a_server_stops_the_rest(tmp_path):
    clock = Clock()
    runner = Seq(OK, Result(-9, b"partial", False, True), clock=clock, spend=[100, 500])
    code, out, _, calls, sent, log = run_group(GROUP, tmp_path, runner, clock)
    assert code == 124 and [c[1] for c in calls] == [600, 500]
    assert "=== node-h\npartial\n[ssh-exec: прервано по таймауту 500 с]\n(код 124)\n" in out
    assert out.endswith("итог: остановлено на node-h (код 124); выполнено: node-f; не выполнялось: node-g\n")
    assert log[3]["timed_out"] is True and log[3]["rc"] == 124
    assert len(sent) == 1 and (sent[0][1], sent[0][2]["failed"], sent[0][2]["why"]) == (
        "notice.exec.batch_failed", "node-h", "таймаут")


def test_group_truncated_output_is_marked(tmp_path):
    _, out, _, _, _, log = run_group(GROUP, tmp_path, Seq(Result(0, b"x" * 10, True, False)))
    assert out.startswith("=== node-f\nxxxxxxxxxx\n[ssh-exec: вывод обрезан до 1024 КБ]\n=== node-h\n")
    assert log[1]["truncated"] is True


def test_group_interrupt_during_second_server(tmp_path):
    runner = Seq(OK, execute.Interrupted("SIGTERM"))
    code, out, err, calls, sent, log = run_group(GROUP, tmp_path, runner)
    assert code == 130 and len(calls) == 2
    assert [r["event"] for r in log] == ["start", "result", "start", "result", "batch_result"]
    assert (log[3]["rc"], log[3]["interrupted"], log[3]["batch_index"]) == (None, "SIGTERM", 2)
    assert (log[-1]["ok"], log[-1]["failed"], log[-1]["not_run"]) == (["node-f"], "node-h", ["node-g"])
    assert "=== node-h\n[ssh-exec: прервано (SIGTERM); команда могла не завершиться — проверьте через ssh-ro]\n" in out
    assert out.endswith("итог: остановлено на node-h (код 130); выполнено: node-f; не выполнялось: node-g\n")
    assert len(sent) == 1 and (sent[0][1], sent[0][2]["failed"], sent[0][2]["why"]) == (
        "notice.exec.batch_failed", "node-h", "прервано, SIGTERM")
    assert "могла не завершиться" in err


def test_group_real_signal_during_second_server(tmp_path, real_signal_handlers):
    class SignalOnSecond(Seq):
        def __call__(self, *a, **k):
            result = super().__call__(*a, **k)
            if len(self.calls) == 2:
                signal.raise_signal(signal.SIGTERM)
            return result

    code, out, _, calls, sent, log = run_group(GROUP, tmp_path, SignalOnSecond())
    assert code == 130 and len(calls) == 2
    assert [r["event"] for r in log] == ["start", "result", "start", "result", "batch_result"]
    assert log[3]["interrupted"] == "SIGTERM" and log[-1]["not_run"] == ["node-g"]
    assert len(sent) == 1 and sent[0][1] == "notice.exec.batch_failed"


def test_group_signal_between_servers_stops_before_next(tmp_path, monkeypatch, real_signal_handlers):
    original = execute.journal

    def journal_with_signal(path, **record):
        if record["event"] == "result" and record["batch_index"] == 1:
            signal.raise_signal(signal.SIGTERM)  # server 1 has already finished, the signal comes before server 2 starts
        original(path, **record)

    monkeypatch.setattr(execute, "journal", journal_with_signal)
    code, out, _, calls, sent, log = run_group(GROUP, tmp_path)
    assert code == 130 and len(calls) == 1
    assert [r["event"] for r in log] == ["start", "result", "batch_result"]
    assert (log[-1]["ok"], log[-1]["failed"], log[-1]["not_run"]) == (["node-f"], None, ["node-h", "node-g"])
    assert log[-1]["reason"] == "прервано, SIGTERM"
    assert out.endswith("не выполнялось (прервано, SIGTERM): node-h, node-g\n")
    assert len(sent) == 1 and (sent[0][2]["why"], sent[0][2]["not_run"]) == ("прервано, SIGTERM",
                                                                            "node-h, node-g")


def test_group_signal_during_denylist_check_runs_nothing(tmp_path, monkeypatch, real_signal_handlers):
    def check(command, server):
        signal.raise_signal(signal.SIGTERM)

    monkeypatch.setattr(denylist, "check", check)
    code, _, err, calls, sent, log = run_group(GROUP, tmp_path)
    assert code == 130 and calls == [] and log == [] and sent == []
    assert "прервано" in err and "node-f, node-h, node-g" in err


def test_group_unwritable_journal_stops_before_next_server(tmp_path, monkeypatch):
    original = execute.journal

    def flaky(path, **record):
        if record["event"] == "start" and record["batch_index"] == 2:
            raise OSError("диск полон")
        original(path, **record)

    monkeypatch.setattr(execute, "journal", flaky)
    code, out, err, calls, sent, log = run_group(GROUP, tmp_path)
    assert code == 1 and len(calls) == 1
    assert [r["event"] for r in log] == ["start", "result", "batch_result"]
    assert log[-1]["not_run"] == ["node-h", "node-g"] and log[-1]["reason"] == "журнал не пишется"
    assert out.endswith("не выполнялось (журнал не пишется): node-h, node-g\n")
    assert len(sent) == 1 and sent[0][2]["why"] == "журнал не пишется" and "журнал" in err


def test_group_unwritable_journal_from_the_start_runs_nothing(tmp_path):
    runner = Seq()
    code, _, _, calls, sent, _ = run_group(GROUP, tmp_path, runner, log=tmp_path / "missing" / "exec.jsonl")
    assert code == 1 and calls == []
    assert len(sent) == 1 and (sent[0][2]["why"], sent[0][2]["not_run"]) == ("журнал не пишется",
                                                                            "node-f, node-h, node-g")


def test_group_result_write_failure_returns_1_and_warns(tmp_path, monkeypatch):
    original = execute.journal

    def flaky(path, **record):
        if record["event"] == "result":
            raise OSError("диск полон")
        original(path, **record)

    monkeypatch.setattr(execute, "journal", flaky)
    code, out, err, calls, sent, _ = run_group(GROUP, tmp_path)
    assert code == 1 and len(calls) == 3 and "журнал не записан" in err
    assert out.endswith("итог: успешно 3 из 3\n") and sent[0][1] == "notice.exec.batch_ok"


# --- review round 1 of the group ssh-exec ---

def _get_with_signal(monkeypatch):
    original = inventory.get

    def get_with_signal(alias, *a, **k):
        signal.raise_signal(signal.SIGTERM)  # before arming: it used to get lost, while the handler had already set SIG_IGN
        return original(alias, *a, **k)

    monkeypatch.setattr(inventory, "get", get_with_signal)


def test_signal_during_argument_checks_is_not_forgotten(tmp_path, monkeypatch, real_signal_handlers):
    _get_with_signal(monkeypatch)
    code, _, err, calls, sent, log = run(["--agent", "sysadmin", "example-stage", "uptime"], tmp_path)
    assert code == 130 and calls == [] and log == [] and sent == []
    assert "прервано (SIGTERM) до запуска команды; на example-stage ничего не выполнено" in err


def test_group_signal_during_argument_checks_runs_nothing(tmp_path, monkeypatch, real_signal_handlers):
    _get_with_signal(monkeypatch)
    code, out, err, calls, sent, log = run_group(GROUP, tmp_path)
    assert code == 130 and calls == [] and log == [] and sent == [] and out == ""
    assert "прервано (SIGTERM) до запуска команды; на node-f, node-h, node-g ничего не выполнено" in err


def test_install_signal_handlers_resets_state():
    execute._state.update(armed=True, pending="SIGTERM")
    names = [n for n in ("SIGTERM", "SIGHUP", "SIGINT") if hasattr(signal, n)]
    saved = {n: signal.getsignal(getattr(signal, n)) for n in names}
    try:
        execute.install_signal_handlers()
        assert execute._state == {"armed": False, "pending": None}
    finally:
        for n, handler in saved.items():
            signal.signal(getattr(signal, n), handler)
        execute._state.update(armed=False, pending=None)


def test_group_does_not_start_a_server_with_less_than_a_minute_left(tmp_path):
    clock = Clock()
    runner = Seq(clock=clock, spend=[541])  # 59 s are left
    code, out, _, calls, sent, log = run_group(GROUP, tmp_path, runner, clock)
    assert code == 124 and len(calls) == 1
    assert out.endswith("итог: остановлено; выполнено: node-f; не выполнялось (время вышло): node-h, node-g\n")
    assert (log[-1]["ok"], log[-1]["not_run"], log[-1]["reason"]) == (["node-f"], ["node-h", "node-g"],
                                                                      "время вышло")
    assert len(sent) == 1 and (sent[0][2]["why"], sent[0][2]["not_run"]) == ("время вышло", "node-h, node-g")


def test_group_starts_a_server_with_exactly_a_minute_left(tmp_path):
    clock = Clock()
    code, _, _, calls, _, _ = run_group(GROUP, tmp_path, Seq(clock=clock, spend=[540, 0]), clock)
    assert code == 0 and [c[1] for c in calls] == [600, 60, 60]


def test_group_runner_error_after_a_server_ran_is_still_reported(tmp_path):
    runner = Seq(OK, FileNotFoundError(2, "No such file or directory", "/usr/bin/ssh"))
    code, out, err, calls, sent, log = run_group(GROUP, tmp_path, runner)
    assert code == 1 and len(calls) == 2
    assert [r["event"] for r in log] == ["start", "result", "start", "result", "batch_result"]
    assert log[3]["rc"] is None and log[3]["error"].startswith("FileNotFoundError: ") and log[3]["batch_index"] == 2
    assert (log[-1]["ok"], log[-1]["failed"], log[-1]["not_run"], log[-1]["reason"]) == (
        ["node-f"], "node-h", ["node-g"], "ошибка исполнителя: FileNotFoundError")
    assert "=== node-h\n[ssh-exec: ошибка исполнителя (FileNotFoundError)]\n(код 1)\n" in out
    assert out.endswith("итог: остановлено на node-h (код 1); выполнено: node-f; не выполнялось: node-g\n")
    assert len(sent) == 1 and sent[0][1] == "notice.exec.batch_failed"
    assert [sent[0][2][k] for k in ("failed", "why", "done", "not_run")] == [
        "node-h", "ошибка исполнителя: FileNotFoundError", "node-f", "node-g"]
    assert "ошибка исполнителя на node-h" in err and "/usr/bin/ssh" in err


def test_group_executor_error_before_a_server_starts_runs_nothing_more(tmp_path):
    class BrokenClock(Clock):
        ticks = 0

        def __call__(self):
            self.ticks += 1
            if self.ticks == 3:  # 1 — the group's deadline, 2 — what server 1 has left, 3 — before the "start" record
                raise RuntimeError("часы")
            return self.now

    code, out, err, calls, sent, log = run_group(GROUP, tmp_path, Seq(), BrokenClock())
    assert code == 1 and calls == []
    assert [r["event"] for r in log] == ["batch_result"]
    assert (log[0]["failed"], log[0]["not_run"], log[0]["reason"]) == (None, GROUP.split(","),
                                                                       "ошибка исполнителя: RuntimeError")
    assert len(sent) == 1 and sent[0][2]["why"] == "ошибка исполнителя: RuntimeError"


def test_single_server_runner_error_still_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        run(["--agent", "sysadmin", "example-stage", "true"], tmp_path, Runner(raises=FileNotFoundError("ssh")))


@pytest.mark.skipif(not hasattr(os, "fchmod") or os.name == "nt", reason="права файлов — только POSIX")
def test_journal_is_created_with_mode_0640(tmp_path):
    old = os.umask(0)
    try:
        execute.journal(tmp_path / "j.jsonl", event="x")
    finally:
        os.umask(old)
    assert (tmp_path / "j.jsonl").stat().st_mode & 0o777 == 0o640
