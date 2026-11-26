"""kd-session-host's I/O — requests from the cabinet only, processes, reports, the raw journal."""

import asyncio
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from kd import session_config
from kd.cabinet.client import CabinetError
from kd.session_host import server, worker

HERE = Path(__file__).resolve().parent
REPLAYER = str(HERE / "stream_replayer.py")
FIXTURES = HERE / "fixtures" / "stream_json"
CABINET_UID = 1003
SESSION = "a" * 32


class Config:
    def __init__(self, agents=None):
        self.value = session_config.HostConfig(agents or {"analyst": 1}, 600)

    def get(self):
        return self.value


class Cabinet:
    """The cabinet's local socket as the host sees it: records every report; fails as scripted."""

    def __init__(self, *failures):
        self.reports, self.failures = [], list(failures)

    def call(self, request):
        if self.failures:
            failure = self.failures.pop(0)
            if failure is not None:
                raise failure
        assert request["op"] == "host.report"
        self.reports.append(list(request["items"]))
        return {"applied": len(request["items"])}

    def items(self):
        return [i for batch in self.reports for i in batch]


RUNNER = """
import json, sys
definition = json.loads(sys.stdin.buffer.readline().decode("utf-8"))
open(sys.argv[1], "w", encoding="utf-8").write(json.dumps(definition))
if sys.argv[2] == "0":
    print(json.dumps({"type": "job", "phase": "ended", "state": "done", "reason": "", "comment": "Готово.",
                      "usage": None, "result": {"summary": "ok"}}))
sys.exit(int(sys.argv[2]))
"""


def _host(tmp_path, cabinet, fixture="two_turns", code=0, spawn=None, job_code=0):
    seen = []

    async def replay(*argv, **kwargs):
        seen.append(list(argv))
        if "/opt/digipaddock/common/bin/kd-worker-run" in argv:  # a worker job: a fake runner
            return await asyncio.create_subprocess_exec(sys.executable, "-c", RUNNER, str(tmp_path / "definition.json"),
                                                        str(job_code), **kwargs)
        return await asyncio.create_subprocess_exec(sys.executable, REPLAYER, str(FIXTURES / f"{fixture}.jsonl"),
                                                    str(code), **kwargs)

    stopped = []

    async def stop_unit(unit):
        stopped.append(unit)

    host = server.HostServer(
        config=Config(), users=lambda agent: f"digi-{agent}", cabinet=cabinet, spawn=spawn or replay, stop_unit=stop_unit,
        allowed_uid=CABINET_UID, events_dir=str(tmp_path / "events"), journal_path=str(tmp_path / "host.jsonl"),
        state_dir=str(tmp_path / "state"), batch_interval=0.01, retry_delays=(0.01, 0.01, 0.01),
        job_slots=lambda agent: ["dev/worker1"] if agent == "dev" else [],
    )
    host.argvs = seen
    host.stopped = stopped
    return host


def _deliver(command="1" * 32, text="[board] KD-1 assigned to you"):
    return json.dumps({"op": "deliver", "command": command, "session": SESSION, "agent": "analyst", "thread": 7,
                       "card": "KD-1", "text": text, "resume": ""}).encode() + b"\n"


async def _until(predicate, timeout=10.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not predicate():
        if loop.time() > end:
            raise AssertionError("timed out")
        await asyncio.sleep(0.02)


def test_the_process_is_launched_with_its_sessions_context_and_a_foreign_delivery_is_refused(tmp_path):
    """the launch names the session's thread and card; a delivery of another card to it never queues."""
    cabinet = Cabinet()
    foreign = json.dumps({"op": "deliver", "command": "2" * 32, "session": SESSION, "agent": "analyst", "thread": 8,
                          "card": "KD-2", "text": "[board] KD-2 assigned to you", "resume": ""}).encode() + b"\n"

    async def scenario():
        host = _host(tmp_path, cabinet)
        await host.start()
        await host.request(_deliver(), CABINET_UID)
        refused = json.loads(await host.request(foreign, CABINET_UID))
        await _until(lambda: any(i.get("state") == "idle" for i in cabinet.items()))
        await host.close()
        return host, refused

    host, refused = asyncio.run(scenario())
    [argv] = host.argvs
    assert argv[-5:] == ["--session", "--context", SESSION, "7", "KD-1"]
    assert refused["ok"] is False and refused["error"] == "refused" and "треду 7" in refused["message"]
    assert not [i for i in cabinet.items() if i.get("command") == "2" * 32]
    journal = [json.loads(line) for line in (tmp_path / "host.jsonl").read_text("utf-8").splitlines()]
    assert {"op": "refused", "session": SESSION, "agent": "analyst", "command": "2" * 32}.items() <= next(
        j for j in journal if j.get("command") == "2" * 32).items()


def test_the_journal_names_the_exit_of_each_delivery(tmp_path):
    """which exit (and so which country's tunnel) a delivery's session runs with is read in host.jsonl."""
    cabinet = Cabinet()
    with_exit = json.loads(_deliver())
    with_exit["egress"] = "web-ae"

    async def scenario():
        host = _host(tmp_path, cabinet)
        await host.start()
        await host.request(json.dumps(with_exit).encode() + b"\n", CABINET_UID)
        await _until(lambda: any(i.get("state") == "idle" for i in cabinet.items()))
        await host.request(_deliver(command="2" * 32), CABINET_UID)
        await host.close()

    asyncio.run(scenario())
    journal = [json.loads(line) for line in (tmp_path / "host.jsonl").read_text("utf-8").splitlines()]
    delivers = {j["command"]: j for j in journal if j.get("op") == "deliver"}
    assert delivers["1" * 32]["egress"] == "web-ae" and delivers["2" * 32]["egress"] == ""


def test_host_started_reported_first(tmp_path):
    cabinet = Cabinet()

    async def scenario():
        host = _host(tmp_path, cabinet)
        await host.start()
        await _until(lambda: cabinet.items())
        await host.close()

    asyncio.run(scenario())
    assert cabinet.items()[0] == {"type": "host", "phase": "started"}


def test_instruction_failure_does_not_deliver_task_to_model(tmp_path):
    cabinet = Cabinet()
    checked = []

    def refuse(client, agent, card, **kw):
        checked.append((agent, card))
        raise ValueError('installed skill changed; reinstall')

    async def scenario():
        host = _host(tmp_path, cabinet)
        host.instruction_check = refuse
        await host.start()
        await host.request(_deliver(), CABINET_UID)
        await _until(lambda: any(i.get('phase') == 'ended' for i in cabinet.items()))
        await host.close()

    asyncio.run(scenario())
    assert checked == [('analyst', 'KD-1')]
    ended = [i for i in cabinet.items() if i.get('type') == 'turn' and i.get('phase') == 'ended']
    assert ended[0]['ok'] is False and 'reinstall' in ended[0]['error']
    assert not [i for i in cabinet.items() if i.get('type') == 'message']


def test_a_slow_instruction_check_does_not_hold_the_host(tmp_path):
    """Fable review M-2: the check runs outside the host's lock — other requests are served meanwhile."""
    import threading

    cabinet = Cabinet()
    started, release = threading.Event(), threading.Event()

    def slow(client, agent, card, **kw):
        started.set()
        release.wait(10)
        return "checked\n"

    async def scenario():
        host = _host(tmp_path, cabinet)
        host.instruction_check = slow
        await host.start()
        try:
            delivered = asyncio.ensure_future(host.request(_deliver(), CABINET_UID))
            assert await asyncio.get_running_loop().run_in_executor(None, started.wait, 10)
            status = json.loads(await asyncio.wait_for(host.request(b'{"op": "status"}\n', CABINET_UID), 2))
            release.set()
            assert json.loads(await delivered)["ok"]
            await _until(lambda: any(i.get("phase") == "ended" for i in cabinet.items()))
            return status
        finally:
            release.set()
            await host.close()

    assert asyncio.run(scenario())["ok"]


def test_an_unexpected_check_failure_ends_the_turn_and_checks_are_not_kept(tmp_path):
    """Fable review round 2: a check's own bug fails the turn (never a turn in flight forever); a finished check
    task is not kept by the host."""
    cabinet = Cabinet()

    def broken(client, agent, card, **kw):
        raise KeyError("card")

    async def scenario():
        host = _host(tmp_path, cabinet)
        host.instruction_check = broken
        await host.start()
        try:
            await host.request(_deliver(), CABINET_UID)
            await _until(lambda: any(i.get("phase") == "ended" for i in cabinet.items()))
            await asyncio.sleep(0.05)
            return len(host._checks)
        finally:
            await host.close()

    assert asyncio.run(scenario()) == 0
    ended = [i for i in cabinet.items() if i.get("type") == "turn" and i.get("phase") == "ended"]
    assert ended[0]["ok"] is False and "instruction check failed" in ended[0]["error"]


def test_a_host_stop_waits_for_a_pending_check_before_closing_the_input(tmp_path):
    """Fable review round 2: the input checked before the stop reaches the process; the turn is not lost."""
    import threading

    cabinet = Cabinet()
    started = threading.Event()
    inputs = []

    async def spawn(*argv, **kwargs):
        process = await asyncio.create_subprocess_exec(
            sys.executable, REPLAYER, str(FIXTURES / "two_turns.jsonl"), "0", **kwargs)
        write = process.stdin.write
        process.stdin.write = lambda data: inputs.append(data) or write(data)
        return process

    def slow(client, agent, card, **kw):
        started.set()
        import time
        time.sleep(0.3)
        return "checked\n"

    async def scenario():
        host = _host(tmp_path, cabinet, spawn=spawn)
        host.instruction_check = slow
        await host.start()
        delivered = asyncio.ensure_future(host.request(_deliver(), CABINET_UID))
        await asyncio.get_running_loop().run_in_executor(None, started.wait, 10)
        await delivered
        await host.close()

    asyncio.run(scenario())
    assert len(inputs) == 1 and b"checked" in inputs[0]
    ended = [i for i in cabinet.items() if i.get("type") == "turn" and i.get("phase") == "ended"]
    assert [(i["command"], i["ok"]) for i in ended] == [("1" * 32, True)]  # it ran: no host_restart


def test_a_late_check_failure_does_not_end_the_next_turn(tmp_path):
    """Fable review round 3: the process of turn A died while A was being checked, turn B started in a new
    process; A's check failing afterwards leaves B alone."""
    import threading

    cabinet = Cabinet()
    calls, first_started, first_go, second_started, second_go = [], *(threading.Event() for _ in range(4))
    processes = []

    async def spawn(*argv, **kwargs):
        process = await asyncio.create_subprocess_exec(
            sys.executable, REPLAYER, str(FIXTURES / "two_turns.jsonl"), "0", **kwargs)
        processes.append(process)
        return process

    def check(client, agent, card, **kw):
        calls.append(card)
        if len(calls) == 1:
            first_started.set()
            first_go.wait(10)
            raise ValueError("installed skill changed; reinstall")
        second_started.set()
        second_go.wait(10)
        return "checked\n"

    def ended():
        return [(i["command"], i["ok"]) for i in cabinet.items() if i.get("type") == "turn" and i.get("phase") == "ended"]

    async def scenario():
        host = _host(tmp_path, cabinet, spawn=spawn)
        host.instruction_check = check
        await host.start()
        loop = asyncio.get_running_loop()
        try:
            await host.request(_deliver("1" * 32), CABINET_UID)
            assert await loop.run_in_executor(None, first_started.wait, 10)
            processes[0].kill()
            await _until(lambda: ("1" * 32, False) in ended())
            await host.request(_deliver("2" * 32), CABINET_UID)
            assert await loop.run_in_executor(None, second_started.wait, 10)
            first_go.set()
            await asyncio.sleep(0.3)
            second_go.set()
            await _until(lambda: any(c == "2" * 32 for c, _ in ended()))
            await asyncio.sleep(0.2)
        finally:
            first_go.set()
            second_go.set()
            await host.close()

    asyncio.run(scenario())
    assert [e for e in ended() if e[0] == "2" * 32] == [("2" * 32, True)]


def test_a_host_stop_waits_for_a_check_started_while_it_waited(tmp_path):
    """Fable review round 3: turn A's check fails during the stop's wait and turn B's check starts; B's input
    still goes in before the EOF."""
    cabinet = Cabinet()
    started = []

    def check(client, agent, card, **kw):
        import time
        started.append(card)
        time.sleep(0.3)
        if len(started) == 1:
            raise ValueError("installed skill changed; reinstall")
        return "checked\n"

    async def scenario():
        host = _host(tmp_path, cabinet)
        host.instruction_check = check
        await host.start()
        await host.request(_deliver("1" * 32), CABINET_UID)
        await host.request(_deliver("2" * 32), CABINET_UID)
        await _until(lambda: started)
        await host.close()

    asyncio.run(scenario())
    ended = [(i["command"], i["ok"]) for i in cabinet.items() if i.get("type") == "turn" and i.get("phase") == "ended"]
    assert ended == [("1" * 32, False), ("2" * 32, True)]


def test_a_host_stop_spends_one_grace_on_checks_and_processes(tmp_path):
    """Fable review round 3: a stuck check and a process that ignores the EOF share one grace time — the stop
    stays inside the unit's TimeoutStopSec."""
    import threading
    import time

    cabinet = Cabinet()
    stuck = threading.Event()
    processes = []

    async def spawn(*argv, **kwargs):
        process = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(30)", **kwargs)
        processes.append(process)
        return process

    async def scenario():
        host = _host(tmp_path, cabinet, spawn=spawn)
        host.grace = 1.0
        host.instruction_check = lambda *a, **kw: stuck.wait(30) and ""

        async def kill(unit):
            for process in processes:
                process.kill()
        host.stop_unit = kill
        await host.start()
        try:
            await host.request(_deliver(), CABINET_UID)
            await asyncio.sleep(0.1)
            began = time.monotonic()
            await host.close()
            return time.monotonic() - began
        finally:
            stuck.set()

    assert asyncio.run(scenario()) < 1.7


def test_a_live_process_receives_the_current_instructions_on_its_next_turn(tmp_path):
    cabinet = Cabinet()
    inputs, checked, processes = [], [], []
    selection = {"text": "frontend v1 sha256=first\n"}

    async def spawn(*argv, **kwargs):
        process = await asyncio.create_subprocess_exec(
            sys.executable, REPLAYER, str(FIXTURES / "two_turns.jsonl"), "0", **kwargs
        )
        processes.append(process)
        write = process.stdin.write

        def record(data):
            inputs.append(json.loads(data)["message"]["content"])
            write(data)

        process.stdin.write = record
        return process

    memories = []

    def instructions(client, agent, card, *, delivered):
        # review I3: one memory per process, so the full texts are not resent on every turn
        checked.append((agent, card))
        memories.append(delivered)
        return selection["text"]

    async def scenario():
        host = _host(tmp_path, cabinet, spawn=spawn)
        host.instruction_check = instructions
        await host.start()
        try:
            await host.request(_deliver(text="first task"), CABINET_UID)
            await _until(lambda: any(i.get("state") == "idle" for i in cabinet.items()))
            selection["text"] = "security-review v1 sha256=second\n"
            await host.request(_deliver("2" * 32, "second task"), CABINET_UID)
            await _until(lambda: sum(i.get("phase") == "ended" for i in cabinet.items()) == 2)
        finally:
            await host.close()

    asyncio.run(scenario())
    assert len(processes) == 1
    assert checked == [("analyst", "KD-1"), ("analyst", "KD-1")]
    assert memories[0] is memories[1] and isinstance(memories[0], dict)
    assert inputs == ["frontend v1 sha256=first\nfirst task", "security-review v1 sha256=second\nsecond task"]


def test_instruction_delivery_is_visible_once_per_process_selection(tmp_path):
    cabinet = Cabinet()
    chosen = {"spec": "docs", "version": 1, "sha256": "a" * 64, "skills": {}}

    def instructions(client, agent, card, *, delivered):
        delivered[card] = json.dumps(chosen)
        return "checked instruction texts\n"

    async def scenario():
        host = _host(tmp_path, cabinet)
        host.instruction_check = instructions
        await host.start()
        try:
            await host.request(_deliver(text="first task"), CABINET_UID)
            await _until(lambda: any(i.get("state") == "idle" for i in cabinet.items()))
            await host.request(_deliver("2" * 32, "second task"), CABINET_UID)
            await _until(lambda: sum(i.get("phase") == "ended" for i in cabinet.items()) == 2)
        finally:
            await host.close()

    asyncio.run(scenario())
    events = [i for i in cabinet.items() if i.get("payload", {}).get("key") == "sys.session.instructions_loaded"]
    assert len(events) == 1
    assert events[0]["command"] == "1" * 32 and events[0]["session"] == SESSION
    assert events[0]["payload"]["activity"] == {"type": "instructions", "state": "loaded", **chosen}


def test_deliver_runs_turn_and_reports_messages(tmp_path):
    cabinet = Cabinet()

    async def scenario():
        host = _host(tmp_path, cabinet)
        await host.start()
        answer = json.loads(await host.request(_deliver(), CABINET_UID))
        await _until(lambda: any(i.get("state") == "idle" for i in cabinet.items()))
        again = json.loads(await host.request(_deliver(), CABINET_UID))  # the same command: nothing new
        await host.close()
        return answer, again

    answer, again = asyncio.run(scenario())
    assert answer == {"ok": True, "state": "starting"} and again == answer
    items = cabinet.items()[1:]
    assert items[0] == {"type": "session", "session": SESSION, "state": "starting", "slot": "analyst/1", "model": "",
                        "effort": ""}
    assert items[1] == {"type": "turn", "session": SESSION, "command": "1" * 32, "phase": "started"}
    assert {"type": "session", "session": SESSION, "state": "working",
            "runtime_session_id": "0b9c3f1e-1111-2222-3333-444455556666"} in items
    texts = [i["body"] for i in items if i["type"] == "message"]
    assert texts == ["ok"]
    ended = [i for i in items if i.get("phase") == "ended"][0]
    assert {k: v for k, v in ended.items() if k != "usage"} == {
        "type": "turn", "session": SESSION, "command": "1" * 32, "phase": "ended", "ok": True}
    assert sum(1 for i in items if i.get("phase") == "started") == 1


def test_messages_carry_the_running_command(tmp_path):
    """the cabinet groups a session's messages into its turns by this command."""
    cabinet = Cabinet()

    async def scenario():
        host = _host(tmp_path, cabinet, fixture="tools")  # no result event: the turn is still running
        await host.start()
        await host.request(_deliver(), CABINET_UID)
        await _until(lambda: sum(1 for i in cabinet.items() if i.get("kind") == "tool_result") == 2)
        await host.close()

    asyncio.run(scenario())
    messages = [i for i in cabinet.items() if i["type"] == "message"]
    assert {m["kind"] for m in messages} >= {"tool_call", "tool_result"}
    assert all(m["command"] == "1" * 32 for m in messages)
    result = next(m for m in messages if m["kind"] == "tool_result")
    assert set(result["output"]) == {"text", "size", "truncated"}


def test_report_batches_stay_under_the_request_limit(tmp_path):
    """25 agent texts of 20000 Cyrillic characters and 25 output excerpts of 4000 are ~1.2 MB as one report: over
    the cabinet's 1 MiB line (UTF-8: two bytes a letter)."""
    from kd.cabinet import client as cabinet_client, server as cabinet_server

    cabinet = Cabinet()
    excerpt = {"text": "ж" * 4000, "size": 9000, "truncated": True}

    async def scenario():
        host = _host(tmp_path, cabinet)
        for n in range(50):
            if n % 2:
                host.queue({"type": "message", "session": SESSION, "command": "1" * 32, "kind": "text",
                            "body": "ж" * 20000, "payload": {"id": f"t{n}"}})
            else:
                host.queue({"type": "message", "session": SESSION, "command": "1" * 32, "kind": "tool_result",
                            "body": "", "payload": {"id": f"t{n}", "ok": True, "size": 9000}, "output": excerpt})
        await host.start(report_start=False)
        await _until(lambda: len(cabinet.items()) == 50)
        await host.close()

    asyncio.run(scenario())
    assert len(cabinet.reports) > 1
    for batch in cabinet.reports:
        size = len(cabinet_client.encode({"op": "host.report", "items": batch}))
        assert size <= server.REPORT_BYTES < cabinet_server.MAX_REQUEST
    assert [i["payload"]["id"] for i in cabinet.items()] == [f"t{n}" for n in range(50)]


def test_a_lone_surrogate_does_not_stop_the_reports(tmp_path):
    """Final review: Claude's JSON may carry a lone surrogate (a cut emoji, «\\ud83d») — UTF-8 cannot encode it, and
    the report task died on it: every later report stuck until the host restarted."""
    cabinet = Cabinet()

    async def scenario():
        host = _host(tmp_path, cabinet)
        host.queue({"type": "message", "session": SESSION, "command": "1" * 32, "kind": "text",
                    "body": "cut \ud83d emoji", "payload": {"id": "t0", "tool": "\udc80"}})
        host.queue({"type": "message", "session": SESSION, "command": "1" * 32, "kind": "text", "body": "next",
                    "payload": {"id": "t1"}})
        await host.start(report_start=False)
        await _until(lambda: len(cabinet.items()) == 2)
        await host.close()

    asyncio.run(scenario())
    first, second = cabinet.items()
    assert first["body"] == "cut ? emoji" and first["payload"]["tool"] == "?" and second["body"] == "next"



def test_turn_end_usage_goes_with_the_turn_report_not_summed(tmp_path):
    """each turn's report carries Claude's cumulative usage as reported; no session report has any."""
    cabinet = Cabinet()

    async def scenario():
        host = _host(tmp_path, cabinet)
        await host.start()
        await host.request(_deliver(), CABINET_UID)
        await host.request(_deliver("2" * 32, "[board] KD-1: a message for you"), CABINET_UID)
        await _until(lambda: sum(1 for i in cabinet.items() if i.get("phase") == "ended") == 2)
        await host.close()

    asyncio.run(scenario())
    items = cabinet.items()
    ended = [i for i in items if i.get("phase") == "ended"]
    costs = [i["usage"]["models"]["claude-sonnet-5-5"]["cost"] for i in ended]
    assert costs == [pytest.approx(0.0123), pytest.approx(0.0246)]
    assert [i["usage"]["requests"] for i in ended] == [1, 1]
    assert not any("usage" in i for i in items if i["type"] == "session")


def test_an_auth_failed_turn_is_no_heartbeat_of_its_closing_session(tmp_path):
    """The process that cannot sign in closes — the cabinet sees its session fail, never a fresh
    «working» pulse in between."""
    cabinet = Cabinet()

    async def scenario():
        host = _host(tmp_path, cabinet, fixture="auth_failed")
        await host.start()
        await host.request(_deliver(), CABINET_UID)
        await _until(lambda: any(i.get("state") == "failed" for i in cabinet.items()))
        await host.close()

    asyncio.run(scenario())
    items = cabinet.items()
    ended = next(n for n, i in enumerate(items) if i.get("phase") == "ended")
    assert items[ended]["error"] == "auth_failed"
    after = [i["state"] for i in items[ended:] if i["type"] == "session"]
    assert after[0] == "failed" and "working" not in after, after

def test_peer_other_than_cabinet_refused(tmp_path):
    cabinet = Cabinet()

    async def scenario():
        host = _host(tmp_path, cabinet)
        refused = json.loads(await host.request(_deliver(), 2004))
        root_deliver = json.loads(await host.request(_deliver(), 0))
        status = json.loads(await host.request(b'{"op": "status"}\n', 0))
        bad = json.loads(await host.request(b"nonsense\n", CABINET_UID))
        return refused, root_deliver, status, bad

    refused, root_deliver, status, bad = asyncio.run(scenario())
    assert refused["ok"] is False and refused["error"] == "refused"
    assert root_deliver["error"] == "refused"
    assert status["ok"] is True and status["slots"]["analyst"]["max"] == 1
    assert bad["error"] == "invalid"


def test_crash_fails_the_session(tmp_path):
    cabinet = Cabinet()

    async def scenario():
        host = _host(tmp_path, cabinet)
        await host.start()
        await host.request(_deliver(), CABINET_UID)
        await _until(lambda: any(i.get("state") == "idle" for i in cabinet.items()))
        await host.request(_deliver(command="2" * 32, text="crash"), CABINET_UID)
        await _until(lambda: any(i.get("state") == "failed" for i in cabinet.items()))
        await host.close()

    asyncio.run(scenario())
    items = cabinet.items()
    assert {"type": "turn", "session": SESSION, "command": "2" * 32, "phase": "ended", "ok": False,
            "error": "exit 3"} in items
    assert items[-1]["state"] == "failed" and items[-1]["error"] == "exit 3"


def test_events_file_and_journal_has_no_text(tmp_path):
    cabinet = Cabinet()

    async def scenario():
        host = _host(tmp_path, cabinet)
        await host.start()
        await host.request(_deliver(text="[board] KD-1 secret words"), CABINET_UID)
        await _until(lambda: any(i.get("state") == "idle" for i in cabinet.items()))
        await host.close()

    asyncio.run(scenario())
    raw = (tmp_path / "events" / f"{SESSION}.jsonl").read_text(encoding="utf-8")
    assert '"subtype": "init"' in raw
    journal = (tmp_path / "host.jsonl").read_text(encoding="utf-8")
    assert "secret words" not in journal and '"op": "deliver"' in journal
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(tmp_path / "events" / f"{SESSION}.jsonl").st_mode) == 0o600


def test_reporter_retries_until_cabinet_answers(tmp_path):
    cabinet = Cabinet(CabinetError("unavailable", "down"), CabinetError("unavailable", "down"))

    async def scenario():
        host = _host(tmp_path, cabinet)
        await host.start()
        await _until(lambda: cabinet.items())
        await host.close()

    asyncio.run(scenario())
    assert cabinet.items() == [{"type": "host", "phase": "started"}]


def test_invalid_batch_is_split_and_bad_item_dropped(tmp_path, capsys):
    class Picky(Cabinet):
        def call(self, request):
            if any(i.get("type") == "bogus" for i in request["items"]):
                raise CabinetError("invalid", "unknown report item")
            return super().call(request)

    cabinet = Picky()

    async def scenario():
        host = _host(tmp_path, cabinet)
        host.queue({"type": "bogus"})
        host.queue({"type": "host", "phase": "started"})
        await host.start(report_start=False)
        await _until(lambda: cabinet.items())
        await host.close()

    asyncio.run(scenario())
    assert cabinet.items() == [{"type": "host", "phase": "started"}]
    assert "dropped" in capsys.readouterr().err


def test_a_host_that_stops_lets_an_idle_session_exit_clean(tmp_path):
    """A restart is no failure: the idle session ends exited (resumable), no «exit 0» failure line."""
    cabinet = Cabinet()

    async def scenario():
        host = _host(tmp_path, cabinet)
        await host.start()
        await host.request(_deliver(), CABINET_UID)
        await _until(lambda: any(i.get("state") == "idle" for i in cabinet.items()))
        await host.close()

    asyncio.run(scenario())
    sessions = [i for i in cabinet.items() if i["type"] == "session"]
    assert sessions[-1] == {"type": "session", "session": SESSION, "state": "exited"}
    assert not any(i.get("state") == "failed" for i in sessions)


def test_cancel_request_from_the_cabinet(tmp_path):
    cabinet = Cabinet()

    async def scenario():
        host = _host(tmp_path, cabinet)
        await host.start()
        line = json.dumps({"op": "cancel", "session": SESSION, "command": "9" * 32}).encode() + b"\n"
        answer = json.loads(await host.request(line, CABINET_UID))
        bad = json.loads(await host.request(json.dumps({"op": "cancel", "session": SESSION}).encode() + b"\n",
                                            CABINET_UID))
        await host.close()
        return answer, bad

    answer, bad = asyncio.run(scenario())
    assert answer == {"ok": True, "cancelled": False}
    assert bad["ok"] is False and bad["error"] == "invalid"


def test_stop_stops_the_unit(tmp_path):
    cabinet = Cabinet()

    async def scenario():
        host = _host(tmp_path, cabinet)
        await host.start()
        await host.request(_deliver(), CABINET_UID)
        await _until(lambda: any(i.get("state") == "idle" for i in cabinet.items()))
        answer = json.loads(await host.request(json.dumps({"op": "stop", "session": SESSION}).encode() + b"\n",
                                               CABINET_UID))
        await host.close()
        return answer, host.stopped

    answer, stopped = asyncio.run(scenario())
    assert answer["ok"] is True and len(stopped) == 1 and stopped[0].startswith("digipaddock-session-")


def _inflight(tmp_path):
    path = tmp_path / "state" / "inflight.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def test_the_inflight_journal_holds_a_written_command_until_its_turn_ends(tmp_path):
    """Stage-1 review: the host writes the command it hands a process before the write; a crash before the turn's
    first line must fail that command, never deliver it a second time."""
    cabinet = Cabinet()

    async def scenario():
        host = _host(tmp_path, cabinet)
        await host.start()
        await host.request(_deliver(), CABINET_UID)
        await _until(lambda: any(i.get("state") == "idle" for i in cabinet.items()))
        await _until(lambda: _inflight(tmp_path) == [])  # once the cabinet took the turn's end
        after_turn = _inflight(tmp_path)
        await host.request(_deliver(command="2" * 32, text="hang"), CABINET_UID)
        await _until(lambda: _inflight(tmp_path) == ["2" * 32])
        await asyncio.sleep(0.2)
        started = [i for i in cabinet.items() if i.get("phase") == "started" and i.get("command") == "2" * 32]
        await host.close()
        return after_turn, started

    after_turn, started = asyncio.run(scenario())
    assert after_turn == [] and started == []
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(tmp_path / "state" / "inflight.json").st_mode) == 0o600


def test_a_restarted_host_reports_its_inflight_commands(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "inflight.json").write_text(json.dumps(["c" * 32]), encoding="utf-8")
    cabinet = Cabinet()

    async def scenario():
        host = _host(tmp_path, cabinet)
        await host.start()
        await _until(lambda: cabinet.items())
        await host.close()

    asyncio.run(scenario())
    assert cabinet.items()[0] == {"type": "host", "phase": "started", "inflight": ["c" * 32]}
    assert _inflight(tmp_path) == []


def test_a_spawn_failure_fails_the_session_and_closes_its_events_file(tmp_path, monkeypatch):
    opened, closed = [], []
    real_open, real_close = server._open_append, server.os.close
    monkeypatch.setattr(server, "_open_append", lambda *a, **k: opened.append(real_open(*a, **k)) or opened[-1])
    monkeypatch.setattr(server.os, "close", lambda fd: closed.append(fd) or real_close(fd))
    cabinet = Cabinet()

    async def spawn(*argv, **kwargs):
        raise OSError("no systemd-run")

    async def scenario():
        host = _host(tmp_path, cabinet, spawn=spawn)
        await host.start()
        await host.request(_deliver(), CABINET_UID)
        await _until(lambda: any(i.get("state") == "failed" for i in cabinet.items()))
        await host.close()

    asyncio.run(scenario())
    sessions = [(i["state"], i.get("error")) for i in cabinet.items() if i["type"] == "session"]
    # final review: the owner reads why — the session never started, not a process exit code
    assert sessions == [("starting", None), ("failed", "spawn_failed")]
    assert opened and set(opened) <= set(closed)


class Unreachable(Cabinet):
    """A cabinet that does not answer until it is told to."""

    def __init__(self):
        super().__init__()
        self.down = True

    def call(self, request):
        if self.down:
            raise CabinetError("unavailable", "down")
        return super().call(request)


def test_a_finished_turn_the_cabinet_has_not_heard_of_stays_journaled(tmp_path):
    """Final review (invariant 2): a turn ends while the cabinet is unreachable — the command stays in the journal
    until its report is taken, so a host killed meanwhile fails it at the next start instead of requeueing it."""
    cabinet = Unreachable()

    async def scenario():
        host = _host(tmp_path, cabinet)
        await host.start()
        await host.request(_deliver(), CABINET_UID)
        await _until(lambda: any(i.get("phase") == "ended" for i in host._pending))
        assert host.core.inflight() == []  # the turn is over for the host
        during = _inflight(tmp_path)
        cabinet.down = False
        await _until(lambda: any(i.get("phase") == "ended" for i in cabinet.items()))
        await _until(lambda: _inflight(tmp_path) == [])
        await host.close()
        return during

    assert asyncio.run(scenario()) == ["1" * 32]


def test_a_restart_not_yet_reported_keeps_its_inflight_journaled(tmp_path):
    """The previous host's commands stay journaled until the cabinet took the «host started» report naming them."""
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "inflight.json").write_text(json.dumps(["c" * 32]), encoding="utf-8")
    cabinet = Unreachable()

    async def scenario():
        host = _host(tmp_path, cabinet)
        await host.start()
        await asyncio.sleep(0.1)
        during = _inflight(tmp_path)
        cabinet.down = False
        await _until(lambda: cabinet.items())
        await _until(lambda: _inflight(tmp_path) == [])
        await host.close()
        return during

    assert asyncio.run(scenario()) == ["c" * 32]


def test_an_unwritable_journal_stops_the_session_instead_of_writing_the_input(tmp_path, monkeypatch):
    """Final review: without the journal a crash could repeat the command — the input is not written, the unit is
    stopped and its exit fails the turn."""
    cabinet = Cabinet()

    def broken(fd):
        raise OSError("disk full")

    async def scenario():
        host = _host(tmp_path, cabinet, fixture="two_turns")
        await host.start()
        monkeypatch.setattr(server.os, "fsync", broken)
        await host.request(_deliver(), CABINET_UID)
        await _until(lambda: host.stopped)
        await asyncio.sleep(0.2)
        started = [i for i in cabinet.items() if i["type"] == "turn" and i.get("phase") == "started"]
        await host.close()
        return host.stopped, started

    stopped, started = asyncio.run(scenario())
    assert len(stopped) == 1 and stopped[0].startswith("digipaddock-session-") and started == []
    journal = [json.loads(line) for line in (tmp_path / "host.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(line["op"] == "inflight_failed" for line in journal)


def test_old_raw_streams_are_pruned_live_ones_kept(tmp_path):
    events = tmp_path / "events"
    events.mkdir()
    old, fresh, live = events / ("o" * 32 + ".jsonl"), events / ("f" * 32 + ".stderr"), events / (SESSION + ".jsonl")
    for path in (old, fresh, live):
        path.write_text("{}\n", encoding="utf-8")
    month = 30 * 86400
    os.utime(old, (os.path.getmtime(old) - month,) * 2)
    os.utime(live, (os.path.getmtime(live) - month,) * 2)
    host = _host(tmp_path, Cabinet())
    host.procs[SESSION] = object()
    assert host.prune_events() == 1
    assert not old.exists() and fresh.exists() and live.exists()


JOB = "cd" * 16
DEFINITION = {"id": JOB, "agent": "dev", "card": "KD-7", "spec": "implementer", "timeout": 3600, "brief": "Сделай."}


def _job_request():
    return json.dumps({"op": "job", "job": JOB, "agent": "dev", "definition": DEFINITION}).encode() + b"\n"


def test_job_spawn_argv_and_definition_on_stdin(tmp_path):
    """the runner's unit gets the job on stdin; its end line is the outcome the cabinet hears."""
    cabinet = Cabinet()

    async def scenario():
        host = _host(tmp_path, cabinet)
        await host.start()
        answer = json.loads(await host.request(_job_request(), CABINET_UID))
        await _until(lambda: any(i.get("type") == "job" and i.get("state") == "done" for i in cabinet.items()))
        status = host.core.status()
        await host.close()
        return host, answer, status

    host, answer, status = asyncio.run(scenario())
    assert answer == {"ok": True, "state": "starting"}
    assert host.argvs == [worker.argv("digipaddock-session-job-" + JOB[:12], "dev/worker1", 3600)]
    assert json.loads((tmp_path / "definition.json").read_text(encoding="utf-8")) == DEFINITION
    jobs = [i for i in cabinet.items() if i.get("type") == "job"]
    assert jobs[0] == {"type": "job", "job": JOB, "state": "starting", "slot": "dev/worker1"}
    assert jobs[1] == {"type": "job", "job": JOB, "state": "done", "reason": "", "comment": "Готово.", "usage": None,
                       "result": {"summary": "ok"}}
    assert status["jobs"] == []
    assert (tmp_path / "events" / f"{JOB}.jsonl").exists()
    assert "Сделай" not in (tmp_path / "host.jsonl").read_text(encoding="utf-8")  # the journal keeps no texts


def test_job_report_items_reach_the_cabinet(tmp_path):
    """A runner that exits without its end line fails the job with its exit; a cancel of a running job stops its
    unit; the host's journal of jobs in flight holds the job until the cabinet heard its end."""
    cabinet = Cabinet()

    async def scenario():
        host = _host(tmp_path, cabinet, job_code=3)
        await host.start()
        await host.request(_job_request(), CABINET_UID)
        await _until(lambda: any(i.get("state") == "failed" for i in cabinet.items() if i.get("type") == "job"))
        cancel = json.loads(await host.request(json.dumps({"op": "job_cancel", "job": JOB}).encode() + b"\n",
                                               CABINET_UID))
        await host.close()
        return host, cancel

    host, cancel = asyncio.run(scenario())
    [failed] = [i for i in cabinet.items() if i.get("type") == "job" and i["state"] == "failed"]
    assert failed["reason"] == "exit 3" and "exit 3" in failed["comment"]
    assert cancel == {"ok": True, "cancelled": "ended"}
    assert json.loads((tmp_path / "state" / "inflight.json").read_text(encoding="utf-8")) == []


def test_job_cancel_of_a_running_job_stops_its_unit(tmp_path):
    cabinet = Cabinet()
    gate = []

    async def hold(*argv, **kwargs):
        gate.append(argv)
        return await asyncio.create_subprocess_exec(sys.executable, "-c", "import sys, time; sys.stdin.read();"
                                                    " time.sleep(30)", **kwargs)

    async def scenario():
        host = _host(tmp_path, cabinet, spawn=hold)
        host.grace = 0.2
        await host.start()
        await host.request(_job_request(), CABINET_UID)
        assert JOB in json.loads((tmp_path / "state" / "inflight.json").read_text(encoding="utf-8"))
        cancel = json.loads(await host.request(json.dumps({"op": "job_cancel", "job": JOB}).encode() + b"\n",
                                               CABINET_UID))
        await asyncio.sleep(0.1)
        stopped = list(host.stopped)
        for proc in list(host.job_procs.values()):
            proc.process.kill()
        await host.close()
        return cancel, stopped

    cancel, stopped = asyncio.run(scenario())
    assert cancel == {"ok": True, "cancelled": "running"}
    assert stopped[0] == "digipaddock-session-job-" + JOB[:12]
    [ended] = [i for i in cabinet.items() if i.get("type") == "job" and i["state"] not in ("starting",)]
    assert ended["state"] == "cancelled"


def test_status_names_the_jobs_whose_end_is_still_being_reported(tmp_path):
    """Final review: the cabinet's sweep must not take a job whose end report is on its way for a lost one."""

    async def scenario():
        host = _host(tmp_path, Cabinet())
        host.queue({"type": "job", "job": "c" * 32, "state": "done", "reason": "", "comment": "", "usage": None,
                    "result": None})
        host.queue({"type": "job", "job": "e" * 32, "state": "starting", "slot": "dev/worker1"})
        host.queue({"type": "turn", "session": SESSION, "command": "d" * 32, "phase": "started"})
        return json.loads(await host.request(b'{"op": "status"}\n', CABINET_UID))

    status = asyncio.run(scenario())
    assert status["ok"] is True and status["reporting"] == ["c" * 32]


def test_a_key_gone_before_the_sessions_spawn_fails_it_no_key(tmp_path, monkeypatch):
    """Final review (Critical 1): the kind's key file was there at the delivery; by the spawn it is gone (the
    runbook's kill switch) or malformed — the session fails no_key and claude never starts: a unit with the
    provider's base URL and no key would send the user's stored login to the provider."""
    monkeypatch.setattr(server.llm_models, "key_present", lambda provider, *a, **k: True)
    monkeypatch.setattr(server.llm_models, "read_secret", lambda provider, *a, **k: {})
    cabinet, seen = Cabinet(), []

    async def spawn(*argv, **kwargs):
        seen.append(list(argv))
        raise AssertionError("claude must not start without the key")

    async def scenario():
        host = _host(tmp_path, cabinet, spawn=spawn)
        await host.start()
        spec = {"id": "or-x", "provider": "openrouter", "upstream": "anthropic/claude-sonnet-4.5", "base_url": ""}
        line = json.loads(_deliver())
        line["model"] = spec
        answer = json.loads(await host.request(json.dumps(line).encode() + b"\n", CABINET_UID))
        await _until(lambda: any(i.get("state") == "failed" for i in cabinet.items()))
        await host.close()
        return answer

    assert asyncio.run(scenario()) == {"ok": True, "state": "starting"}
    sessions = [(i["state"], i.get("error")) for i in cabinet.items() if i["type"] == "session"]
    assert sessions == [("starting", None), ("failed", "no_key: openrouter")] and seen == []


def test_the_roles_context_limit_reaches_the_launch_as_claudes_auto_compact_window(tmp_path):
    """the delivery's ``context_limit`` is a plain variable of the unit, which agent-launch passes on to claude;
    a delivery without it sets nothing, and the journal names the window each process starts with."""
    cabinet = Cabinet()

    async def scenario():
        host = _host(tmp_path, cabinet)
        await host.start()
        line = json.loads(_deliver())
        line["context_limit"] = 400000
        answer = json.loads(await host.request(json.dumps(line).encode() + b"\n", CABINET_UID))
        await _until(lambda: any(i.get("state") == "idle" for i in cabinet.items()))
        await host.close()
        return host, answer

    host, answer = asyncio.run(scenario())
    assert answer == {"ok": True, "state": "starting"}
    [argv] = host.argvs
    assert "--setenv=CLAUDE_CODE_AUTO_COMPACT_WINDOW=400000" in argv
    assert argv.index("--setenv=CLAUDE_CODE_AUTO_COMPACT_WINDOW=400000") < argv.index("/usr/local/bin/agent-launch")
    journal = [json.loads(line) for line in (tmp_path / "host.jsonl").read_text("utf-8").splitlines()]
    assert [j["context_limit"] for j in journal if j.get("op") == "spawn"] == [400000]

    plain = Cabinet()

    async def without():
        host = _host(tmp_path / "plain", plain)
        await host.start()
        await host.request(_deliver(), CABINET_UID)
        await _until(lambda: any(i.get("state") == "idle" for i in plain.items()))
        await host.close()
        return host

    [argv] = asyncio.run(without()).argvs
    assert not any(part.startswith("--setenv=CLAUDE_CODE_AUTO_COMPACT_WINDOW") for part in argv)


def test_a_subscription_session_starts_on_the_host_token_with_the_alias(tmp_path, monkeypatch):
    """a card on ``claude:sonnet`` — ``--model sonnet`` on the host's own Claude token, no provider
    environment, no kind's key read; the turn's usage is under the names Claude Code reports, at their cost."""

    def no_secret(*a, **k):
        raise AssertionError("a subscription model reads no kind's key")

    monkeypatch.setattr(server.llm_models, "key_present", lambda *a, **k: False)
    monkeypatch.setattr(server.llm_models, "read_secret", no_secret)
    cabinet = Cabinet()

    async def scenario():
        host = _host(tmp_path, cabinet)
        await host.start()
        line = json.loads(_deliver())
        line["model"] = {"id": "claude:sonnet", "provider": "subscription", "upstream": "sonnet", "base_url": ""}
        answer = json.loads(await host.request(json.dumps(line).encode() + b"\n", CABINET_UID))
        await _until(lambda: any(i.get("phase") == "ended" for i in cabinet.items()))
        await host.close()
        return host, answer

    host, answer = asyncio.run(scenario())
    assert answer == {"ok": True, "state": "starting"}
    [argv] = host.argvs
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert "EnvironmentFile=-/etc/digipaddock/claude.env" in argv
    assert not any(part.startswith("--setenv=ANTHROPIC_") for part in argv)
    starting = [i for i in cabinet.items() if i["type"] == "session" and i["state"] == "starting"][0]
    assert starting["model"] == "claude:sonnet"
    [ended] = [i for i in cabinet.items() if i.get("phase") == "ended"]
    assert set(ended["usage"]["models"]) == {"claude-sonnet-5-5"}
    assert ended["usage"]["models"]["claude-sonnet-5-5"]["cost"] == pytest.approx(0.0123)
