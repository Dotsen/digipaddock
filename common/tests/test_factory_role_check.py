"""a role's reinstall is refused only by its own problems; a failure says what failed (stage, batch unit,
masked output, the roles left on the old version, the retry) and its journal is readable by the card's lead."""

import asyncio
import io
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from kd import host_read, install_apply, install_plan, operation_runner
from kd.cabinet import api, operations

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_cabinet_operations import granted, lead, report  # noqa: E402,F401
from test_cabinet_factory_plan import env  # noqa: E402,F401
from test_host_read import _catalog, _entry  # noqa: E402
from test_session_host_operation import (  # noqa: E402
    CABINET_UID, OP, REV, Cabinet, Units, _deliver, _final, _operation, _server, _until)

NEW_AGENT = "/opt/kd/common/install/new_agent.sh"
HOST_SETUP = "/opt/kd/common/install/session_host_setup.sh"
MESSAGES = Path(__file__).resolve().parents[1] / "cabinet" / "web" / "messages"


class Release:
    """A release that changed the texts of analyst and dev: a role differs until its own install; ``broken`` roles
    keep a problem of their own after it. ``--check`` is install_plan's real runtime check over these findings."""

    def __init__(self, broken=(), output=""):
        self.installed, self.broken, self.output, self.calls = set(), set(broken), output, []

    def findings(self, repo):
        found = [(a, "playbooks/spec/implementer.md is not the registry text (new_agent.sh --install " + a + ")")
                 for a in ("analyst", "dev") if a not in self.installed]
        return found + [(a, "compiled policy: unsupported schema_version") for a in sorted(self.broken)]

    def capture(self, argv):
        self.calls.append(tuple(argv[1:]))
        if argv[0] == HOST_SETUP:
            return 0, "session host ok\n"
        stage, name = argv[1][2:], argv[2]
        if stage == "verify":
            return (0 if name in self.installed and name not in self.broken else 3), ""
        if stage == "install":
            self.installed.add(name)
            return 0, "installed\n"
        err = io.BytesIO()
        code = install_plan.runtime_check(Path("/repo"), name, err, findings=self.findings)
        return code, self.output + err.getvalue().decode()


def _tree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "REVISION").write_text(REV + "\n")
    return repo


def _runner(tmp_path, box, n):
    return operation_runner.run(OP, state_dir=str(tmp_path / "state"), repo=_tree(tmp_path), run_cmd=None,
                                capture=box.capture, host_slots=lambda: {"analyst": 1, "dev": 2},
                                release_active=lambda: False, new_agent=NEW_AGENT, host_setup=HOST_SETUP, batch=n)


# --- AC1-AC3: the check refuses only on its own role --------------------------------------------------------
def test_the_check_warns_about_another_role_and_refuses_on_its_own():
    found = [("dev", "playbooks/spec/implementer.md is missing"), ("analyst", "compiled policy: bad")]
    err = io.BytesIO()
    assert install_plan.runtime_check(Path("/repo"), "boss", err, findings=lambda repo: found) == 0
    assert err.getvalue().decode().count("warning: generated runtime of") == 2
    err = io.BytesIO()
    assert install_plan.runtime_check(Path("/repo"), "dev", err, findings=lambda repo: found) == 1
    text = err.getvalue().decode()
    assert "check failed: generated runtime refused: dev: playbooks/spec/implementer.md is missing" in text
    assert "warning: generated runtime of analyst" in text and "refused: analyst" not in text


def test_an_unreadable_registry_refuses_the_check():
    def broken(repo):
        raise ValueError("agents.json: not json")

    err = io.BytesIO()
    assert install_plan.runtime_check(Path("/repo"), "dev", err, findings=broken) == 1
    assert "registry: agents.json: not json" in err.getvalue().decode()


def test_analyst_installs_while_dev_waits_in_a_later_batch(tmp_path):
    """AC1: two batches, analyst then dev (dev works a long turn) — analyst's check passes while dev still differs."""
    cabinet, units, wall, box = Cabinet(), Units(), [5000.0], Release()

    async def scenario():
        host = _server(tmp_path, cabinet, units, wall)
        await host.start()
        await host.request(_deliver("a" * 32), CABINET_UID)
        await _until(lambda: host.procs)  # dev's long turn
        await host.request(_operation(agents=["analyst", "dev"]), CABINET_UID)
        await host.operation_tick()
        first = _runner(tmp_path, box, 1)
        units.active.clear()
        await host.operation_tick()
        host.procs["a" * 32].process.kill()
        await _until(lambda: host.core.drained("dev"))
        await host.operation_tick()
        second = _runner(tmp_path, box, 2)
        units.active.clear()
        await host.operation_tick()
        await _until(lambda: _final(cabinet))
        paused = host.core.status()["paused"]
        await host.close()
        return first, second, paused

    first, second, paused = asyncio.run(scenario())
    assert first["state"] == "succeeded" and first["agents"]["analyst"]["state"] == "ok"
    assert second["state"] == "succeeded" and second["agents"]["dev"]["state"] == "ok"
    final = _final(cabinet)[0]
    assert final["state"] == "succeeded" and paused == {}
    assert {a: v["state"] for a, v in final["agents"].items()} == {"analyst": "ok", "dev": "ok"}


def test_one_batch_of_analyst_and_dev_checks_each_by_its_own_role(tmp_path):
    """AC2: analyst is checked before dev is installed — dev's problem is a warning in analyst's output."""
    box = Release()
    state = tmp_path / "state" / "operations"
    state.mkdir(parents=True)
    (state / f"{OP}.request.json").write_text(json.dumps({
        "operation": OP, "kind": "agent.reinstall", "agents": ["analyst", "dev"], "revision": REV,
        "batch": {"n": 1, "agents": ["analyst", "dev"]}, "batches": [["analyst", "dev"]], "done": {}}))
    outputs = []
    capture = box.capture
    box.capture = lambda argv: outputs.append((tuple(argv[1:]), capture(argv))) or outputs[-1][1]
    result = _runner(tmp_path, box, 1)
    assert result["state"] == "succeeded"
    assert {a: v["state"] for a, v in result["agents"].items()} == {"analyst": "ok", "dev": "ok"}
    [(_, (code, text))] = [o for o in outputs if o[0] == ("--check", "analyst")]
    assert code == 0 and "warning: generated runtime of dev" in text
    box2 = Release(broken=("analyst",))
    result = _runner(tmp_path, box2, 1)
    assert result["agents"]["analyst"]["state"] == "failed" and result["agents"]["analyst"]["stage"] == "check"
    assert result["agents"]["dev"]["state"] == "skipped"


def test_the_release_postflight_still_refuses_any_differing_role(monkeypatch, capsys):
    """AC3: kd-release's whole-host check is unchanged — one differing role fails it."""
    monkeypatch.setattr(install_apply, "generated_runtime_findings",
                        lambda repo, **kw: [("dev", "playbooks/spec/implementer.md is missing")])
    assert install_apply.report_generated_runtime("/repo") == 1
    assert "generated runtime refused: dev: playbooks/spec/implementer.md is missing" in capsys.readouterr().err
    monkeypatch.setattr(install_apply, "generated_runtime_findings", lambda repo, **kw: [])
    assert install_apply.report_generated_runtime("/repo") == 0


# --- AC4, AC6, AC7: what a failure keeps ---------------------------------------------------------------------
@pytest.mark.parametrize("restart", [False, True])
def test_a_failed_check_keeps_stage_tail_and_batch_through_the_host(tmp_path, restart):
    """AC4/AC6: analyst fails its own check in batch 1, dev is skipped: analyst stays paused, dev works on the old
    version; the masked tail and the batch reach the host's result and its report — also across a host restart."""
    cabinet, units, wall = Cabinet(), Units(), [5000.0]
    box = Release(broken=("analyst",), output="token DB_PASSWORD=hunter2 leaked\n")

    async def launch():
        host = _server(tmp_path, cabinet, units, wall)
        await host.start()
        await host.request(_operation(agents=["analyst", "dev"]), CABINET_UID)
        await host.operation_tick()
        return host

    async def scenario():
        host = await launch()
        if restart:
            await host.close()
        ran = _runner(tmp_path, box, 1)
        units.active.clear()
        if restart:
            host = _server(tmp_path, cabinet, units, wall)
            await host.start()
        await host.operation_tick()
        await _until(lambda: _final(cabinet))
        paused = host.core.status()["paused"]
        await host.close()
        return ran, paused

    ran, paused = asyncio.run(scenario())
    analyst = ran["agents"]["analyst"]
    assert analyst["stage"] == "check" and analyst["batch"] == 1
    assert "hunter2" not in analyst["tail"] and "DB_PASSWORD=***" in analyst["tail"]
    assert paused == {"analyst": OP}
    final = _final(cabinet)[0]
    assert final["state"] == "failed" and final["kept"] == ["analyst"]
    assert final["agents"]["dev"]["state"] == "skipped"
    assert final["agents"]["analyst"]["tail"] == analyst["tail"] and final["agents"]["analyst"]["batch"] == 1
    saved = json.loads((tmp_path / "state" / "operations" / f"{OP}.result.json").read_text())
    assert saved["agents"]["analyst"]["tail"] == analyst["tail"] and saved["agents"]["analyst"]["batch"] == 1
    assert "hunter2" not in json.dumps(saved)


def test_the_tail_is_cut_to_its_limit_after_masking(tmp_path):
    box = Release(broken=("analyst",), output="x" * 5000 + "\nDB_PASSWORD=hunter2\n")
    state = tmp_path / "state" / "operations"
    state.mkdir(parents=True)
    (state / f"{OP}.request.json").write_text(json.dumps({
        "operation": OP, "kind": "agent.reinstall", "agents": ["analyst"], "revision": REV,
        "batch": {"n": 3, "agents": ["analyst"]}, "batches": [["analyst"]] * 3, "done": {}}))
    tail = _runner(tmp_path, box, 3)["agents"]["analyst"]["tail"]
    assert len(tail) == operation_runner.TAIL_MAX and "hunter2" not in tail


@pytest.mark.parametrize("masks", [True, False])
def test_the_units_journal_gets_only_masked_output(monkeypatch, capsys, masks):
    """AC7: the stage's raw output never reaches the unit's journal; without masks, the placeholder."""
    raw = b"connecting with DB_PASSWORD=hunter2\n"
    monkeypatch.setattr(operation_runner.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, raw, None))
    mask = None if masks else (lambda text: None)
    code, out = operation_runner._capture([NEW_AGENT, "--check", "dev"], mask)
    printed = capsys.readouterr().out
    assert code == 1 and "hunter2" not in printed
    assert ("DB_PASSWORD=***" in printed) if masks else (printed.strip() == operation_runner.UNMASKED)
    assert operation_runner._tail(out, mask or operation_runner._mask) == (
        operation_runner._mask(out) if masks else operation_runner.UNMASKED)


# --- AC5, AC7: the notice ----------------------------------------------------------------------------------
def _notice(env, card="KD-50"):
    return [m for m in env.store.messages(card) if m["kind"] == "notice"][-1]["payload"]


def _render(key, params):
    for lang in ("ru", "en"):
        template = json.loads((MESSAGES / f"{lang}.json").read_text(encoding="utf-8"))[key]
        missing = set(re.findall(r"\{(\w+)\}", template)) - set(params)
        assert not missing, (lang, key, missing)
    return template.format(**params)


def test_the_failure_notice_names_stage_unit_output_old_roles_and_the_retry(granted):
    op = lead(granted, agents=("boss", "dev"))
    tail = "check failed: generated runtime refused: boss: compiled policy: bad\nDB_PASSWORD=***\n"
    report(granted, op, "failed", phase="installing", reason="install_failed", kept=["boss"],
           agents={"boss": {"state": "failed", "stage": "check", "error": "new_agent.sh --check: exit 1",
                            "tail": tail, "batch": 2},
                   "dev": {"state": "skipped", "stage": "", "error": "install_failed"}})
    row = operations.row(granted.store, op["id"])
    assert row["result"]["agents"]["boss"]["tail"] == tail and row["result"]["agents"]["boss"]["batch"] == 2
    notice = _notice(granted)
    assert notice["key"] == "notice.factory.failed" and notice["level"] == "critical"
    params = notice["params"]
    assert params["failed"] == "boss" and params["stage"] == "check" and params["batch"] == "2"
    assert params["unit"] == f"digipaddock-operation-{op['id'][:12]}-2"
    assert params["old"] == "dev" and params["tail"] == tail.rstrip()
    assert params["retry"] == f"kd-factory reinstall KD-50 --agents boss,dev --key retry-{op['id'][:12]}"
    text = _render("notice.factory.failed", params)
    assert params["unit"] in text and params["retry"] in text and "compiled policy: bad" in text
    assert f"kd-host-read journalctl --operation {op['id'][:12]}" in text


def test_a_failed_host_check_names_its_batch_unit(tmp_path):
    """Review I1: no role failed, the host's check did — the notice still names the batch's real unit."""
    cabinet, units, wall = Cabinet(), Units(), [5000.0]

    async def scenario():
        host = _server(tmp_path, cabinet, units, wall)
        await host.start()
        await host.request(_operation(agents=["analyst", "dev"]), CABINET_UID)
        await host.operation_tick()
        (tmp_path / "state" / "operations" / f"{OP}.batch1.json").write_text(json.dumps({
            "operation": OP, "batch": 1, "state": "failed", "reason": "host_check_failed",
            "agents": {"analyst": {"state": "ok", "stage": "verify", "error": ""},
                       "dev": {"state": "unchanged", "stage": "verify", "error": ""}}}))
        units.active.clear()
        await host.operation_tick()
        await _until(lambda: _final(cabinet))
        await host.close()

    asyncio.run(scenario())
    final = _final(cabinet)[0]
    assert {a: v["batch"] for a, v in final["agents"].items()} == {"analyst": 1, "dev": 1}
    agents = {a: {k: v[k] for k in ("state", "stage", "error", "batch")} for a, v in final["agents"].items()}
    params = operations._failure({"id": OP, "card": "KD-50", "agents": ["analyst", "dev"]},
                                 operations._per_agent(agents), final["kept"])
    assert params["unit"] == f"digipaddock-operation-{OP[:12]}-1" and params["batch"] == "1"
    assert params["retry"] == f"kd-factory reinstall KD-50 --agents analyst,dev --key retry-{OP[:12]}"


def test_the_retry_key_is_new_and_the_grant_still_decides(granted):
    """AC5: the retry's key was never used; kd-factory asks under the card's grant as always."""
    op = lead(granted, agents=("dev",))
    report(granted, op, "failed", phase="installing", reason="install_failed",
           agents={"dev": {"state": "failed", "stage": "install", "error": "exit 1", "batch": 1}})
    retry = _notice(granted)["params"]["retry"]
    key = retry.split("--key ")[1]
    assert operations.request_row(granted.store, key) is None
    again = lead(granted, key=key, agents=("dev",))
    assert again["id"] != op["id"] and again["grant_id"] == op["grant_id"]


def test_a_failure_without_a_card_offers_only_roots_retry(granted):
    op = granted.call("root", {"op": "operation.request", "kind": "agent.reinstall", "agents": ["analyst"],
                               "key": "root:probe"})["operation"]
    report(granted, op, "failed", phase="installing", reason="install_failed",
           agents={"analyst": {"state": "failed", "stage": "check", "error": "exit 1", "tail": "bad", "batch": 1}})
    [notice] = [e["payload"] for e in granted.store.events_since(0) if e["kind"] == "notice"]
    assert notice["key"] == "notice.factory.failed_root"
    text = _render("notice.factory.failed_root", notice["params"])
    assert "kd-factory" not in text and "kd-cabinet operation request --agents analyst" in text


def test_a_report_tail_is_text_and_a_batch_a_number(granted):
    op = lead(granted)
    for bad in ({"tail": 5}, {"batch": "1"}, {"batch": 0}, {"batch": True}):
        with pytest.raises(api.ApiError):
            report(granted, op, "failed", agents={"dev": {"state": "failed", "stage": "check", "error": "", **bad}})


# --- AC8: the operation's journal ---------------------------------------------------------------------------
def _read(granted, prefix, agent):
    answer = granted.call("digi-observe", {"op": "operation.reader", "operation": prefix, "agent": agent})
    return {k: v for k, v in answer.items() if k != "ok"}


def test_the_cabinet_says_who_leads_the_operations_card(granted):
    op = lead(granted)  # KD-50, @dev
    assert _read(granted, op["id"], "dev")["allowed"] is True
    assert _read(granted, op["id"][:12], "dev") == {"allowed": True, "reason": "", "operation": op["id"]}
    assert _read(granted, op["id"], "analyst")["allowed"] is False
    granted.put("KD-50", assignee="@analyst")
    assert _read(granted, op["id"], "dev")["allowed"] is False  # the initiator who no longer leads it
    assert _read(granted, op["id"], "analyst")["allowed"] is True
    granted.put("KD-51", project="digipaddock", assignee="@boss", status="In Progress")
    operations._remember(granted.store, "joined:1", op["id"], "KD-51", "agent:boss", "0" * 64)
    assert _read(granted, op["id"], "boss")["allowed"] is True  # the card of a joined request
    assert _read(granted, "f" * 12, "dev") == {"allowed": False, "reason": "unknown_operation"}


def test_an_operation_without_a_card_has_no_reader(granted):
    op = granted.call("root", {"op": "operation.request", "kind": "agent.reinstall", "agents": ["analyst"],
                               "key": "root:probe"})["operation"]
    assert _read(granted, op["id"], "analyst")["allowed"] is False


def test_only_the_host_reader_and_root_ask_and_the_reader_reads_no_board(granted):
    op = lead(granted)
    with pytest.raises(api.ApiError):
        granted.call("digi", {"op": "operation.reader", "operation": op["id"], "agent": "dev"})
    with pytest.raises(api.ApiError):
        granted.call("digi-dev", {"op": "operation.reader", "operation": op["id"], "agent": "dev"})
    with pytest.raises(api.ApiError):
        granted.call("digi-observe", {"op": "card.get", "id": "KD-50"})
    with pytest.raises(api.ApiError):
        _read(granted, "abc", "dev")
    assert granted.call("root", {"op": "operation.reader", "operation": op["id"], "agent": "dev"})["allowed"]


def _journal(argv, agent, answer):
    out, seen, asked = io.StringIO(), [], []

    def journal(cmd):
        seen.append(cmd)
        return iter([_entry("installing dev", unit=f"digipaddock-operation-{OP[:12]}-2.service")])

    def reader(operation, who):
        asked.append((operation, who))
        if isinstance(answer, Exception):
            raise answer
        return answer

    code = host_read.main(argv, agent=agent, catalog=_catalog(), journal=journal, out=out, operation_reader=reader)
    return code, out.getvalue(), seen, asked


def test_the_lead_reads_every_batch_unit_of_its_operation():
    code, text, seen, asked = _journal(["journalctl", "--operation", OP[:12], "-n", "50"], "dev", "")
    assert code == 0 and "installing dev" in text and asked == [(OP[:12], "dev")]
    argv = seen[0]
    units = [a.split("=", 1)[1] for a in argv if a.startswith("_SYSTEMD_UNIT=")]
    assert set(units) == {f"digipaddock-operation-{OP[:12]}.service"} | {
        f"digipaddock-operation-{OP[:12]}-{n}.service" for n in range(1, 17)}
    assert f"UNIT=digipaddock-operation-{OP[:12]}-2.service" in argv


def test_sysadmin_reads_an_operation_without_asking():
    code, text, seen, asked = _journal(["journalctl", "--operation", OP], "sysadmin", "not_your_card")
    assert code == 0 and asked == [] and "installing dev" in text


@pytest.mark.parametrize("answer", ["not_your_card", "unknown_operation", "the cabinet did not answer"])
def test_another_agent_or_no_cabinet_is_refused(answer):
    code, text, seen, asked = _journal(["journalctl", "--operation", OP], "analyst", answer)
    assert code == host_read.EXIT_DENIED and seen == [] and answer in text


def test_the_reader_refuses_when_the_cabinet_does_not_answer(monkeypatch):
    from kd.cabinet import client as cabinet_client

    class Down:
        def call(self, request):
            assert request == {"op": "operation.reader", "operation": OP, "agent": "dev"}
            raise cabinet_client.CabinetError("unavailable", "down")

    monkeypatch.setattr(cabinet_client, "Client", Down)
    assert host_read._operation_reader(OP, "dev") == "the cabinet did not answer"

    class No:
        def call(self, request):
            return {"allowed": False, "reason": "not_your_card; rm -rf /"}

    monkeypatch.setattr(cabinet_client, "Client", No)
    assert host_read._operation_reader(OP, "dev") == "refused"


@pytest.mark.parametrize("argv", [
    ["journalctl", "--operation", "xyz"],
    ["journalctl", "--operation", OP[:11]],
    ["journalctl", "--operation", OP, "-u", "nginx"],
    ["journalctl", "--operation", OP, "--operation", OP],
    ["journalctl", "--operation", OP, "-n", "0"],  # the cabinet is asked only once every option is valid
])
def test_operation_forms_outside_the_rule_are_refused(argv):
    code, text, seen, asked = _journal(argv, "dev", "")
    assert code == host_read.EXIT_USAGE and seen == [] and asked == []
