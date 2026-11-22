"""a factory operation on the session host — the role is paused with its queue kept, drained (both dev
slots, its running jobs left to finish), the root runner's unit installs it, the result comes back as a report."""

import asyncio
import json
import os
import sys

import pytest

from kd import session_config
from kd.session_host import core, operation, protocol, server

CABINET_UID = 1003
OP = "e" * 32
REV = "b" * 40


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _core(slots=None):
    clock = Clock()
    config = session_config.HostConfig(slots or {"analyst": 1, "dev": 2}, 600)
    host = core.Host(config, {"analyst": "digi-analyst", "dev": "digi-dev"}, clock,
                     job_slots=lambda agent: ["dev/worker1"] if agent == "dev" else [])
    return host, clock


_n = [0]


def _req(session, agent="dev", text="[board] KD-1 assigned to you"):
    _n[0] += 1
    return {"op": "deliver", "command": f"{_n[0]:032x}", "session": session, "agent": agent, "thread": 7,
            "card": "KD-1", "text": text, "resume": ""}


def _job(job_id="c" * 32, agent="dev"):
    return {"op": "job", "job": job_id, "agent": agent,
            "definition": {"id": job_id, "agent": agent, "timeout": 600}}


def kinds(actions):
    return [type(a).__name__ for a in actions]


def _working(host, session, agent="dev"):
    """A session whose process runs a turn."""
    _, actions = host.deliver(_req(session, agent))
    assert "Spawn" in kinds(actions)
    return actions


# --- the core: pause, drain, resume ---------------------------------------------------------------
def test_pause_holds_new_input_and_keeps_queue():
    host, _ = _core()
    _working(host, "a" * 32)
    host.turn_ended("a" * 32, True)  # idle
    actions = host.pause("dev", OP)
    assert kinds(actions) == ["Close"]  # the idle process goes at once
    ack, actions = host.deliver(_req("b" * 32))
    assert ack == {"state": "queued", "paused": True} and not any(isinstance(a, (core.Spawn, core.Write))
                                                                 for a in actions)
    host.exited("a" * 32, 0)
    assert host.drained("dev")
    status = host.status()
    assert status["paused"] == {"dev": OP}
    assert {s["session"]: s["queued_inputs"] for s in status["sessions"]} == {"b" * 32: 1}


def test_pause_waits_both_dev_slots():
    host, _ = _core()
    _working(host, "a" * 32)
    _working(host, "b" * 32)
    assert host.pause("dev", OP) == []  # working turns are never cut
    ack, _ = host.deliver(_req("a" * 32))  # input for a working session waits for the resume
    assert ack == {"state": "queued", "paused": True}
    actions = host.turn_ended("a" * 32, True)
    assert "Write" not in kinds(actions) and "Close" in kinds(actions)
    host.exited("a" * 32, 0)
    assert not host.drained("dev")  # the second slot still works
    assert "Close" in kinds(host.turn_ended("b" * 32, True))
    host.exited("b" * 32, 0)
    assert host.drained("dev")


def test_pause_does_not_kill_running_job():
    host, _ = _core()
    ack, actions = host.job(_job())
    assert ack == {"state": "starting"}
    actions = host.pause("dev", OP)
    assert not any(isinstance(a, core.KillJob) for a in actions)
    assert not host.drained("dev")
    ack, actions = host.job(_job("d" * 32))
    assert ack["state"] == "queued" and actions == []
    host.job_line("c" * 32, {"state": "done", "reason": "", "comment": "", "usage": None, "result": None})
    actions = host.job_exited("c" * 32, 0)
    assert not any(isinstance(a, core.SpawnJob) for a in actions)  # the waiting job stays for the resume
    assert host.drained("dev")


def test_resume_starts_waiting_sessions_and_jobs():
    host, _ = _core()
    host.pause("dev", OP)
    host.deliver(_req("a" * 32))
    host.deliver(_req("b" * 32))
    host.job(_job())
    assert host.drained("dev")
    actions = host.resume("dev")
    assert kinds(actions).count("Spawn") == 2 and kinds(actions).count("SpawnJob") == 1
    assert host.status()["paused"] == {}


def test_other_roles_not_paused():
    host, _ = _core()
    host.pause("dev", OP)
    ack, actions = host.deliver(_req("a" * 32, agent="analyst"))
    assert ack == {"state": "starting"} and "Spawn" in kinds(actions)
    assert host.drained("dev") and not host.drained("analyst")


# --- the protocol ---------------------------------------------------------------------------------
def _line(**fields):
    return json.dumps(fields).encode() + b"\n"


def _operation(**over):
    fields = {"op": "operation", "operation": OP, "kind": "agent.reinstall", "agents": ["dev"], "revision": REV,
              "timeout": 1800, "start": True}
    fields.update(over)
    return _line(**fields)


@pytest.mark.parametrize("over", [
    {"kind": "shell"}, {"agents": []}, {"agents": ["../dev"]}, {"agents": ["dev", "dev"]}, {"revision": "HEAD"},
    {"timeout": 5}, {"operation": "x"}, {"start": "yes"}, {"command": "rm -rf /"},
])
def test_operation_request_checked(over):
    with pytest.raises(protocol.ProtocolError):
        protocol.parse(_operation(**over))


# --- the server ------------------------------------------------------------------------------------
class Config:
    def __init__(self):
        self.value = session_config.HostConfig({"analyst": 1, "dev": 2}, 600)

    def get(self):
        return self.value


class Cabinet:
    """The cabinet as the host sees it: reports, and the authorization of each batch."""

    def __init__(self, allowed=True, reason=""):
        self.reports, self.authorized = [], []
        self.allowed, self.reason = allowed, reason

    def call(self, request):
        if request["op"] == "operation.authorize":
            self.authorized.append((request["operation"], list(request["agents"])))
            if isinstance(self.allowed, Exception):
                raise self.allowed
            return {"allowed": self.allowed, "reason": self.reason}
        self.reports.append(list(request["items"]))
        return {"applied": len(request["items"])}

    def items(self, kind="operation"):
        return [i for batch in self.reports for i in batch if i.get("type") == kind]


class Units:
    """systemd as the host sees it: the runner's unit is active until the test ends it."""

    def __init__(self):
        self.launched, self.active = [], set()

    async def spawn(self, *argv, **kwargs):
        if "--unit" in argv:  # the runner's unit; a session's is --unit=<name>
            self.launched.append(list(argv))
            self.active.add(argv[argv.index("--unit") + 1])
            return await asyncio.create_subprocess_exec(sys.executable, "-c", "pass", **kwargs)
        # a session process that works until its input ends
        return await asyncio.create_subprocess_exec(sys.executable, "-c", "import sys; sys.stdin.read()", **kwargs)

    async def is_active(self, unit):
        return unit in self.active


def _server(tmp_path, cabinet, units, wall):
    async def stop_unit(unit):
        pass

    return server.HostServer(
        config=Config(), users=lambda agent: f"digi-{agent}", cabinet=cabinet, spawn=units.spawn, stop_unit=stop_unit,
        allowed_uid=CABINET_UID, events_dir=str(tmp_path / "events"), journal_path=str(tmp_path / "host.jsonl"),
        state_dir=str(tmp_path / "state"), batch_interval=0.01, retry_delays=(0.01,), tick_interval=3600,
        job_slots=lambda agent: ["dev/worker1"] if agent == "dev" else [], unit_active=units.is_active,
        wall=lambda: wall[0])


def _result(tmp_path, state="succeeded", agents=None, reason="", n=1):
    """The runner's result of batch ``n``."""
    path = tmp_path / "state" / "operations" / f"{OP}.batch{n}.json"
    path.write_text(json.dumps({"operation": OP, "batch": n, "state": state, "reason": reason,
                                "agents": agents or {"dev": {"state": "ok", "stage": "check", "error": ""}}}))


async def _until(predicate, timeout=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not predicate():
        if loop.time() > end:
            raise AssertionError("timed out")
        await asyncio.sleep(0.02)


def test_operation_request_idempotent(tmp_path):
    cabinet, units, wall = Cabinet(), Units(), [5000.0]

    async def scenario():
        host = _server(tmp_path, cabinet, units, wall)
        await host.start()
        first = json.loads(await host.request(_operation(), CABINET_UID))
        again = json.loads(await host.request(_operation(), CABINET_UID))
        await host.operation_tick()  # nothing runs on dev: drained, the runner starts
        await host.operation_tick()  # once
        polled = json.loads(await host.request(_operation(start=False), CABINET_UID))
        other = json.loads(await host.request(_operation(operation="f" * 32), CABINET_UID))
        lost = json.loads(await host.request(_operation(operation="f" * 32, start=False), CABINET_UID))
        await host.close()
        return first, again, polled, other, lost

    first, again, polled, other, lost = asyncio.run(scenario())
    assert first["state"] == "running" and first["report"]["phase"] == "draining" and again == first
    assert polled["report"]["phase"] == "installing"
    assert len(units.launched) == 1
    assert units.launched[0] == operation.argv(OP, 1)
    assert other == {"ok": True, "state": "busy", "operation": OP}
    assert lost == {"ok": True, "state": "unknown"}
    request = json.loads((tmp_path / "state" / "operations" / f"{OP}.request.json").read_text())
    assert request["agents"] == ["dev"] and request["revision"] == REV and request["phase"] == "installing"


def test_drain_timeout_fails_and_resumes(tmp_path):
    cabinet, units, wall = Cabinet(), Units(), [5000.0]

    async def scenario():
        host = _server(tmp_path, cabinet, units, wall)
        await host.start()
        await host.request(_line(op="deliver", command="1" * 32, session="a" * 32, agent="dev", thread=7, card="KD-1",
                                 text="[board] KD-1 go", resume=""), CABINET_UID)
        await _until(lambda: host.procs)  # its turn never ends: the drain cannot
        await host.request(_operation(timeout=60), CABINET_UID)
        await host.operation_tick()
        wall[0] += 61
        await host.operation_tick()
        await _until(lambda: any(i["state"] == "failed" for i in cabinet.items()))
        answer = json.loads(await host.request(_operation(start=False), CABINET_UID))
        paused = host.core.status()["paused"]
        await host.close()
        return answer, paused

    answer, paused = asyncio.run(scenario())
    assert units.launched == []
    assert answer["state"] == "failed" and answer["report"]["reason"] == "drain_timeout"
    assert paused == {}
    assert not os.path.exists(tmp_path / "state" / "paused.json") or json.loads(
        (tmp_path / "state" / "paused.json").read_text()) == {}


def test_restart_keeps_pause_and_adopts_result(tmp_path):
    cabinet, units, wall = Cabinet(), Units(), [5000.0]

    async def first():
        host = _server(tmp_path, cabinet, units, wall)
        await host.start()
        await host.request(_operation(), CABINET_UID)
        await host.operation_tick()  # the runner starts
        await host.close()

    async def second():
        host = _server(tmp_path, cabinet, units, wall)
        paused = host.core.status()["paused"]
        await host.start()
        ack = json.loads(await host.request(
            _line(op="deliver", command="2" * 32, session="a" * 32, agent="dev", thread=7, card="KD-1",
                  text="[board] KD-1 go", resume=""), CABINET_UID))
        await host.operation_tick()  # the runner still runs: no second launch
        _result(tmp_path)
        units.active.clear()
        await host.operation_tick()
        await _until(lambda: any(i["state"] == "succeeded" for i in cabinet.items()))
        await _until(lambda: host.procs)  # the waiting session started on the resume
        answer = json.loads(await host.request(_operation(start=False), CABINET_UID))
        await host.close()
        return paused, ack, answer

    asyncio.run(first())
    paused, ack, answer = asyncio.run(second())
    assert paused == {"dev": OP} and ack["state"] == "queued" and ack["paused"] is True
    assert len(units.launched) == 1
    assert answer["state"] == "succeeded" and answer["report"]["agents"]["dev"]["state"] == "ok"


@pytest.mark.parametrize("state, dev, paused", [
    ("succeeded", "ok", {}),
    ("failed", "failed", {"dev": OP}),
])
def test_result_written_while_the_host_was_down_is_adopted_at_start(tmp_path, state, dev, paused):
    """Review C1: the runner finished between two hosts — the next one lifts the pauses its result allows."""
    cabinet, units, wall = Cabinet(), Units(), [5000.0]

    async def first():
        host = _server(tmp_path, cabinet, units, wall)
        await host.start()
        await host.request(_operation(), CABINET_UID)
        await host.operation_tick()
        await host.close()

    async def second():
        host = _server(tmp_path, cabinet, units, wall)
        await host.start()
        await host.operation_tick()  # the batch's result is adopted on the first tick
        found = host.core.status()["paused"], json.loads((tmp_path / "state" / "paused.json").read_text())
        answer = json.loads(await host.request(_operation(start=False), CABINET_UID))
        await host.close()
        return found, answer

    asyncio.run(first())
    _result(tmp_path, state, {"dev": {"state": dev, "stage": "check", "error": ""}})
    units.active.clear()
    (core_paused, saved), answer = asyncio.run(second())
    assert core_paused == paused and saved == paused
    assert answer["state"] == state


def test_failed_install_keeps_role_paused(tmp_path):
    cabinet, units, wall = Cabinet(), Units(), [5000.0]

    async def scenario():
        host = _server(tmp_path, cabinet, units, wall)
        await host.start()
        await host.request(_operation(agents=["analyst", "dev"]), CABINET_UID)
        await host.operation_tick()
        _result(tmp_path, "failed", {"analyst": {"state": "ok", "stage": "check", "error": ""},
                                     "dev": {"state": "failed", "stage": "check", "error": "check failed"}},
                reason="install_failed")
        units.active.clear()
        await host.operation_tick()
        await _until(lambda: any(i["state"] == "failed" for i in cabinet.items()))
        paused = host.core.status()["paused"]
        saved = json.loads((tmp_path / "state" / "paused.json").read_text())
        await host.close()
        return paused, saved

    paused, saved = asyncio.run(scenario())
    assert paused == {"dev": OP} and saved == {"dev": OP}
    final = [i for i in cabinet.items() if i["state"] == "failed"][0]
    assert final["agents"]["dev"]["state"] == "failed" and final["reason"] == "install_failed"


def test_lost_runner_fails_and_keeps_roles_paused(tmp_path):
    cabinet, units, wall = Cabinet(), Units(), [5000.0]

    async def scenario():
        host = _server(tmp_path, cabinet, units, wall)
        await host.start()
        await host.request(_operation(), CABINET_UID)
        await host.operation_tick()
        units.active.clear()  # gone without a result
        await host.operation_tick()
        await _until(lambda: any(i["state"] == "failed" for i in cabinet.items()))
        paused = host.core.status()["paused"]
        await host.close()
        return paused

    assert asyncio.run(scenario()) == {"dev": OP}
    assert [i for i in cabinet.items() if i["state"] == "failed"][0]["reason"] == "runner_lost"


def test_root_may_resume_only(tmp_path):
    cabinet, units, wall = Cabinet(), Units(), [5000.0]
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "paused.json").write_text(json.dumps({"dev": OP}))

    async def scenario():
        host = _server(tmp_path, cabinet, units, wall)
        await host.start()
        refused = json.loads(await host.request(_operation(), 0))
        resumed = json.loads(await host.request(_line(op="resume", agent="dev"), 0))
        stranger = json.loads(await host.request(_line(op="resume", agent="dev"), 4242))
        paused = host.core.status()["paused"]
        await host.close()
        return refused, resumed, stranger, paused

    refused, resumed, stranger, paused = asyncio.run(scenario())
    assert refused["ok"] is False and refused["error"] == "refused"
    assert resumed == {"ok": True, "resumed": True}
    assert stranger["ok"] is False
    assert paused == {} and json.loads((tmp_path / "state" / "paused.json").read_text()) == {}


def test_resume_refused_while_an_operation_holds_the_role(tmp_path):
    cabinet, units, wall = Cabinet(), Units(), [5000.0]

    async def scenario():
        host = _server(tmp_path, cabinet, units, wall)
        await host.start()
        await host.request(_operation(), CABINET_UID)
        answer = json.loads(await host.request(_line(op="resume", agent="dev"), 0))
        await host.close()
        return answer

    answer = asyncio.run(scenario())
    assert answer["ok"] is False and "operation" in answer["message"]


def test_unhosted_agent_refused(tmp_path):
    cabinet, units, wall = Cabinet(), Units(), [5000.0]

    async def scenario():
        host = _server(tmp_path, cabinet, units, wall)
        await host.start()
        answer = json.loads(await host.request(_operation(agents=["boss"]), CABINET_UID))
        await host.close()
        return answer

    answer = asyncio.run(scenario())
    assert answer["ok"] is False and answer["error"] == "refused"
    assert not (tmp_path / "state" / "operations" / f"{OP}.request.json").exists()


def _cli():
    import importlib.machinery
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "bin" / "kd-session-host"
    loader = importlib.machinery.SourceFileLoader("kd_session_host_cli", str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("kd_session_host_cli", loader))
    loader.exec_module(module)
    return module


def test_root_resume_command(monkeypatch, capsys):
    """Review minor 4: root lifts a role's pause from the host's own CLI."""
    cli = _cli()
    asked = []
    monkeypatch.setattr(cli, "ask", lambda request, path=None: asked.append(request) or {"ok": True, "resumed": True})
    assert cli.main(["resume", "dev"]) == 0
    assert asked == [{"op": "resume", "agent": "dev"}] and "dev: resumed" in capsys.readouterr().out
    assert cli.main(["resume", "../dev"]) == 2


def test_argv_is_the_runner_unit():
    """Review minor 3: the runner's unit has a time limit — a hung install ends as a lost runner."""
    assert operation.argv(OP, 2) == ["systemd-run", "--unit", f"digipaddock-operation-{OP[:12]}-2", "--collect", "--quiet",
                                     "-p", f"RuntimeMaxSec={operation.RUNNER_MAX_S}", "/usr/bin/python3", "-I",
                                     "/opt/digipaddock/common/bin/kd-operation-run", OP, "2"]


# --- a cancel while the roles drain (the owner revoked the grant) --------------------------------
def _cancel(operation=OP, reason="grant_revoked"):
    return _line(op="operation_cancel", operation=operation, reason=reason)


@pytest.mark.parametrize("bad", [{"reason": "rm -rf"}, {"reason": ""}, {"operation": "x"}])
def test_cancel_request_checked(bad):
    fields = {"op": "operation_cancel", "operation": OP, "reason": "grant_revoked", **bad}
    with pytest.raises(protocol.ProtocolError):
        protocol.parse(_line(**fields))


def test_cancel_while_draining_ends_it_and_lifts_the_pause(tmp_path):
    cabinet, units, wall = Cabinet(), Units(), [5000.0]

    async def scenario():
        host = _server(tmp_path, cabinet, units, wall)
        await host.start()
        await host.request(_operation(), CABINET_UID)  # draining: the tick has not launched the runner
        answer = json.loads(await host.request(_cancel(), CABINET_UID))
        again = json.loads(await host.request(_operation(start=False), CABINET_UID))
        paused = host.core.status()["paused"]
        await host.close()
        return answer, again, paused

    answer, again, paused = asyncio.run(scenario())
    assert answer["state"] == "failed" and answer["report"]["reason"] == "grant_revoked"
    assert answer["report"]["phase"] == "draining"
    assert answer["report"]["agents"] == {"dev": {"state": "skipped", "stage": "", "error": "grant_revoked"}}
    assert again["state"] == "failed" and paused == {} and units.launched == []


def test_cancel_after_the_install_began_is_too_late(tmp_path):
    cabinet, units, wall = Cabinet(), Units(), [5000.0]

    async def scenario():
        host = _server(tmp_path, cabinet, units, wall)
        await host.start()
        await host.request(_operation(), CABINET_UID)
        await host.operation_tick()  # drained: the runner is launched
        answer = json.loads(await host.request(_cancel(), CABINET_UID))
        unknown = json.loads(await host.request(_cancel(operation="a" * 32), CABINET_UID))
        await host.close()
        return answer, unknown

    answer, unknown = asyncio.run(scenario())
    assert answer["state"] == "running" and answer["report"]["phase"] == "installing"
    assert unknown["state"] == "unknown" and len(units.launched) == 1


# --- roles install as they drain, each batch authorized, a failed host check holds ----
def _deliver(session, agent="dev", card="KD-2"):
    return _line(op="deliver", command="9" * 32, session=session, agent=agent, thread=7, card=card,
                 text=f"[board] {card} go", resume="")


def _final(cabinet):
    return [i for i in cabinet.items() if i["state"] in ("succeeded", "failed")]


def test_a_drained_role_installs_while_another_still_works(tmp_path):
    """An incident seen on the factory host: the analyst is free, dev works a long turn on another card — the analyst's install
    neither waits for dev nor stays paused; dev installs once its turn ends."""
    cabinet, units, wall = Cabinet(), Units(), [5000.0]

    async def scenario():
        host = _server(tmp_path, cabinet, units, wall)
        await host.start()
        await host.request(_deliver("a" * 32), CABINET_UID)
        await _until(lambda: host.procs)  # dev's long turn
        await host.request(_operation(agents=["analyst", "dev"]), CABINET_UID)
        await host.operation_tick()
        first = [list(a) for a in units.launched]
        _result(tmp_path, agents={"analyst": {"state": "ok", "stage": "verify", "error": ""}}, n=1)
        units.active.clear()
        await host.operation_tick()
        between = dict(host.core.status()["paused"])
        await _until(lambda: any("analyst" in (i.get("agents") or {}) for i in cabinet.items()))
        progress = [i for i in cabinet.items() if "analyst" in (i.get("agents") or {})][0]
        host.procs["a" * 32].process.kill()  # dev's turn ends
        await _until(lambda: host.core.drained("dev"))
        await host.operation_tick()
        _result(tmp_path, agents={"dev": {"state": "ok", "stage": "verify", "error": ""}}, n=2)
        units.active.clear()
        await host.operation_tick()
        await _until(lambda: _final(cabinet))
        paused = host.core.status()["paused"]
        await host.close()
        return first, between, progress, paused

    first, between, progress, paused = asyncio.run(scenario())
    assert first == [operation.argv(OP, 1)] and units.launched[1] == operation.argv(OP, 2)
    request = json.loads((tmp_path / "state" / "operations" / f"{OP}.request.json").read_text())
    assert request["batches"] == [["analyst"], ["dev"]]
    assert between == {"dev": OP}  # the analyst works again while dev drains
    assert progress["agents"]["analyst"]["state"] == "ok" and progress["until"] == 5000 + 1800
    assert cabinet.authorized == [(OP, ["analyst"]), (OP, ["dev"])]
    final = _final(cabinet)[0]
    assert final["state"] == "succeeded" and set(final["agents"]) == {"analyst", "dev"} and paused == {}


def test_a_revoked_grant_stops_the_install_at_its_start(tmp_path):
    """R2: revoked while the roles drained — the last turn ends, the cabinet refuses the batch, nothing runs."""
    cabinet, units, wall = Cabinet(allowed=False, reason="grant_revoked"), Units(), [5000.0]

    async def scenario():
        host = _server(tmp_path, cabinet, units, wall)
        await host.start()
        await host.request(_operation(), CABINET_UID)
        await host.operation_tick()  # drained at once: the batch asks first
        await _until(lambda: _final(cabinet))
        paused = host.core.status()["paused"]
        await host.close()
        return paused

    assert asyncio.run(scenario()) == {}
    assert units.launched == [] and cabinet.authorized == [(OP, ["dev"])]
    final = _final(cabinet)[0]
    assert final["state"] == "failed" and final["reason"] == "grant_revoked"


def test_an_unreachable_cabinet_launches_nothing(tmp_path):
    cabinet, units, wall = Cabinet(allowed=OSError("down")), Units(), [5000.0]

    async def scenario():
        host = _server(tmp_path, cabinet, units, wall)
        await host.start()
        await host.request(_operation(timeout=60), CABINET_UID)
        await host.operation_tick()
        await host.operation_tick()
        still = host.ops.active is not None and host.core.status()["paused"] == {"dev": OP}
        wall[0] += 61
        await host.operation_tick()
        await _until(lambda: _final(cabinet))
        await host.close()
        return still

    assert asyncio.run(scenario()) is True
    assert units.launched == [] and _final(cabinet)[0]["reason"] == "drain_timeout"


def test_a_failed_host_check_keeps_the_roles_paused(tmp_path):
    """R3: the role installed, the host's check did not pass — no resume, here or after a restart."""
    cabinet, units, wall = Cabinet(), Units(), [5000.0]

    async def first():
        host = _server(tmp_path, cabinet, units, wall)
        await host.start()
        await host.request(_operation(), CABINET_UID)
        await host.operation_tick()
        _result(tmp_path, "failed", {"dev": {"state": "ok", "stage": "verify", "error": ""}},
                reason="host_check_failed")
        units.active.clear()
        await host.operation_tick()
        await _until(lambda: _final(cabinet))
        paused = host.core.status()["paused"]
        await host.close()
        return paused

    async def second():
        host = _server(tmp_path, cabinet, units, wall)
        paused = host.core.status()["paused"]
        await host.close()
        return paused

    async def third():
        host = _server(tmp_path, cabinet, units, wall)
        await host.start()
        answer = json.loads(await host.request(_line(op="resume", agent="dev"), 0))
        await _until(lambda: cabinet.items("operation_release"))
        await host.close()
        return answer

    assert asyncio.run(first()) == {"dev": OP}
    assert asyncio.run(second()) == {"dev": OP}
    final = _final(cabinet)[0]
    assert final["reason"] == "host_check_failed" and final["kept"] == ["dev"]
    assert asyncio.run(third()) == {"ok": True, "resumed": True}
    assert cabinet.items("operation_release") == [{"type": "operation_release", "operation": OP, "agent": "dev"}]


def test_the_runner_result_of_a_failed_host_check_holds_end_to_end(tmp_path):
    """R3 through the real runner: its batch result, adopted by the host, keeps the role paused."""
    from pathlib import Path

    from kd import operation_runner

    cabinet, units, wall = Cabinet(), Units(), [5000.0]
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "REVISION").write_text(REV[:7])

    def run_cmd(argv):
        return 1 if argv[-1] == "check" and argv[0].endswith("session_host_setup.sh") else 0

    async def scenario():
        host = _server(tmp_path, cabinet, units, wall)
        await host.start()
        await host.request(_operation(), CABINET_UID)
        await host.operation_tick()
        operation_runner.run(OP, state_dir=str(tmp_path / "state"), repo=Path(repo), batch=1, run_cmd=run_cmd,
                             host_slots=lambda: {"dev": 2}, release_active=lambda: False)
        units.active.clear()
        await host.operation_tick()
        await _until(lambda: _final(cabinet))
        paused = host.core.status()["paused"]
        await host.close()
        return paused

    assert asyncio.run(scenario()) == {"dev": OP}
    assert _final(cabinet)[0]["reason"] == "host_check_failed"


def test_an_old_failed_result_names_the_roles_it_keeps(tmp_path):
    """Review M5: a result written before ``kept`` existed answers the roles it holds, not an empty list."""
    cabinet, units, wall = Cabinet(), Units(), [5000.0]
    ops = tmp_path / "state" / "operations"
    ops.mkdir(parents=True)
    (ops / f"{OP}.result.json").write_text(json.dumps(
        {"operation": OP, "state": "failed", "reason": "runner_lost", "phase": "installing",
         "agents": {"dev": {"state": "skipped", "stage": "", "error": "runner_lost"}}}))

    async def scenario():
        host = _server(tmp_path, cabinet, units, wall)
        await host.start()
        answer = json.loads(await host.request(_operation(start=False), CABINET_UID))
        await host.close()
        return answer

    assert asyncio.run(scenario())["report"]["kept"] == ["dev"]


def test_a_failing_tick_does_not_stop_the_ticker(tmp_path):
    """Review I1: one tick that raises is logged; the next tick still runs."""
    cabinet, units, wall = Cabinet(), Units(), [5000.0]
    calls = []

    async def scenario():
        host = _server(tmp_path, cabinet, units, wall)
        host.tick_interval = 0.01

        async def tick():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")

        host.operation_tick = tick
        await host.start()
        await _until(lambda: len(calls) >= 2)
        await host.close()

    asyncio.run(scenario())
    assert len(calls) >= 2
