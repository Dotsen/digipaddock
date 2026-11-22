"""the session host's pure core — slots per agent, a FIFO per session, one turn at a time, idle."""

import json

import pytest

from kd import session_config
from kd.session_host import core, protocol

SID = "0b9c3f1e-1111-2222-3333-444455556666"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _host(slots=1, idle=600):
    clock = Clock()
    config = session_config.HostConfig({"analyst": slots, "dev": 2}, idle)
    return core.Host(config, {"analyst": "digi-analyst", "dev": "digi-dev"}, clock), clock


def _req(session="s" * 32, command=None, text="[board] KD-1 assigned to you", agent="analyst", resume="",
         thread=7, card="KD-1"):
    _req.n += 1
    return {"op": "deliver", "command": command or f"{_req.n:032x}", "session": session, "agent": agent,
            "thread": thread, "card": card, "text": text, "resume": resume}


_req.n = 0


def kinds(actions):
    return [type(a).__name__ for a in actions]


def reports(actions):
    return [a.item for a in actions if isinstance(a, core.Report)]


def test_first_deliver_spawns_then_writes():
    host, _ = _host()
    ack, actions = host.deliver(_req(resume=SID))
    assert ack == {"state": "starting"}
    # «starting» goes out before the spawn: a spawn that fails reports after it, never under it (stage-1 review)
    assert kinds(actions) == ["Report", "Spawn", "Write"]
    spawn = actions[1]
    assert (spawn.agent, spawn.user, spawn.resume) == ("analyst", "digi-analyst", SID)
    assert spawn.unit.startswith("digipaddock-session-")
    assert reports(actions) == [{"type": "session", "session": "s" * 32, "state": "starting", "slot": "analyst/1",
                                 "model": "", "effort": ""}]
    assert actions[2].text == "[board] KD-1 assigned to you"


def test_turn_started_is_reported_at_the_first_output_line_not_at_the_write():
    """Stage-1 review: a process that hangs before its first line must read «start delayed», not «works»."""
    host, _ = _host()
    request = _req()
    host.deliver(request)
    assert reports(host.output("s" * 32)) == [
        {"type": "turn", "session": "s" * 32, "command": request["command"], "phase": "started"}]
    assert host.output("s" * 32) == []
    host.turn_ended("s" * 32, ok=True)
    assert host.output("s" * 32) == []  # idle: no turn to start


def test_the_next_input_starts_on_its_own_first_line():
    host, _ = _host()
    host.deliver(_req())
    second = _req(text="[board] KD-1: a message for you")
    host.deliver(second)
    host.output("s" * 32)
    actions = host.turn_ended("s" * 32, ok=True)
    assert kinds(actions) == ["Report", "Write"]
    assert reports(host.output("s" * 32))[0]["command"] == second["command"]


def test_inflight_lists_the_commands_written_to_a_process():
    host, _ = _host()
    request = _req()
    host.deliver(request)
    host.deliver(_req(text="[board] KD-1: a message for you"))  # waits: not written yet
    assert host.inflight() == [request["command"]]
    host.turn_ended("s" * 32, ok=True)
    assert len(host.inflight()) == 1 and host.inflight() != [request["command"]]


def test_current_is_the_running_turns_command():
    """a message of the session belongs to the turn written to its process, none between turns."""
    host, _ = _host()
    assert host.current("s" * 32) == ""
    request = _req()
    host.deliver(request)
    waiting = _req(text="[board] KD-1: a message for you")
    host.deliver(waiting)
    assert host.current("s" * 32) == request["command"]
    host.turn_ended("s" * 32, ok=True)
    assert host.current("s" * 32) == waiting["command"]
    host.turn_ended("s" * 32, ok=True)
    assert host.current("s" * 32) == "" and host.current("x" * 32) == ""


def test_an_ended_session_leaves_the_status():
    host, clock = _host(idle=60)
    host.deliver(_req())
    host.output("s" * 32)
    host.turn_ended("s" * 32, ok=True)
    clock.t += 61
    host.tick()
    host.exited("s" * 32, 0)
    assert host.status()["sessions"] == []
    failed, _ = _host()
    failed.deliver(_req(session="f" * 32))
    failed.exited("f" * 32, 1)
    assert failed.status()["sessions"] == []


def test_redeliver_same_command_is_idempotent():
    host, _ = _host()
    request = _req()
    ack, _ = host.deliver(request)
    again, actions = host.deliver(dict(request))
    assert again == ack and actions == []


def test_second_input_waits_for_turn_end():
    host, _ = _host()
    host.deliver(_req())
    second = _req(text="[board] KD-1: a message for you")
    ack, actions = host.deliver(second)
    assert ack == {"state": "running", "position": 1} and actions == []
    actions = host.turn_ended("s" * 32, ok=True)
    assert kinds(actions) == ["Report", "Write"]
    assert reports(actions)[0]["phase"] == "ended" and reports(actions)[0]["ok"] is True
    assert actions[1].command == second["command"]
    actions = host.turn_ended("s" * 32, ok=True)
    assert [r.get("state") for r in reports(actions)] == [None, "idle"]


def test_slot_full_queues_and_preempts_idle_of_same_agent():
    host, _ = _host(slots=1)
    host.deliver(_req(session="a" * 32))
    host.turn_ended("a" * 32, ok=True)  # a is idle, its process alive
    ack, actions = host.deliver(_req(session="b" * 32))
    assert ack == {"state": "queued", "position": 1}
    assert kinds(actions) == ["Close", "Report"] and actions[0].session == "a" * 32
    assert reports(actions) == [{"type": "session", "session": "b" * 32, "state": "queued", "position": 1}]
    actions = host.exited("a" * 32, 0)
    assert reports(actions)[0] == {"type": "session", "session": "a" * 32, "state": "exited"}
    assert "Spawn" in kinds(actions) and [a for a in actions if isinstance(a, core.Spawn)][0].session == "b" * 32


def test_working_session_never_preempted():
    host, _ = _host(slots=1)
    host.deliver(_req(session="a" * 32))
    ack, actions = host.deliver(_req(session="b" * 32))
    assert ack["state"] == "queued" and kinds(actions) == ["Report"]  # its place in the queue
    host.turn_ended("a" * 32, ok=True)
    assert host.status()["slots"]["analyst"] == {"used": 1, "max": 1, "waiting": 1}


def test_other_agents_slots_are_their_own():
    host, _ = _host(slots=1)
    host.deliver(_req(session="a" * 32))
    ack, actions = host.deliver(_req(session="d" * 32, agent="dev"))
    assert ack == {"state": "starting"} and "Spawn" in kinds(actions)


def test_exit_after_close_is_exited_resumable():
    host, clock = _host(idle=60)
    host.deliver(_req())
    host.turn_ended("s" * 32, ok=True)
    clock.t += 61
    assert kinds(host.tick()) == ["Close"]
    actions = host.exited("s" * 32, 0)
    assert reports(actions) == [{"type": "session", "session": "s" * 32, "state": "exited"}]
    ack, actions = host.deliver(_req(resume=SID))
    assert ack == {"state": "starting"} and actions[1].resume == SID


def test_input_arriving_while_closing_respawns_after_exit():
    host, clock = _host(idle=60)
    host.deliver(_req())
    host.turn_ended("s" * 32, ok=True)
    clock.t += 61
    host.tick()
    ack, actions = host.deliver(_req(resume=SID))
    assert ack == {"state": "starting"} and actions == []
    actions = host.exited("s" * 32, 0)
    assert kinds(actions)[:2] == ["Report", "Spawn"] and "Write" in kinds(actions)


def test_an_auth_failed_turn_fails_the_session_and_its_queue():
    """no turn of this process can authenticate — its queue ends auth_failed, the process closes, and its
    exit reports the session failed once (never «exited», which would clear the error)."""
    host, _ = _host()
    first = _req()
    host.deliver(first)
    waiting = _req(text="second")
    host.deliver(waiting)
    host.output("s" * 32)
    actions = host.turn_ended("s" * 32, ok=False, error=core.AUTH_FAILED)
    assert kinds(actions)[-1] == "Close"
    items = reports(actions)
    assert items == [
        {"type": "turn", "session": "s" * 32, "command": first["command"], "phase": "ended", "ok": False,
         "error": "auth_failed"},
        {"type": "turn", "session": "s" * 32, "command": waiting["command"], "phase": "ended", "ok": False,
         "error": "auth_failed"}]
    late = _req(text="third")
    host.deliver(late)  # it came while the process closes: it ends with the session
    items = reports(host.exited("s" * 32, 0))
    assert {"type": "turn", "session": "s" * 32, "command": late["command"], "phase": "ended", "ok": False,
            "error": "auth_failed"} in items
    assert [i for i in items if i["type"] == "session"] == [
        {"type": "session", "session": "s" * 32, "state": "failed", "error": "auth_failed"}]
    assert host.status()["slots"]["analyst"]["used"] == 0
    ack, actions = host.deliver(_req())  # a later command tries a fresh process (a new token may be there)
    assert ack == {"state": "starting"} and "Spawn" in kinds(actions)


@pytest.mark.parametrize("meanwhile", ["stop", "shutdown"])
def test_an_auth_failure_is_reported_though_a_stop_came_while_it_closed(meanwhile):
    """A stop (owner, release, --disable) or the host's shutdown between the auth failure's close and
    the exit still reports the session failed auth_failed — the owner learns why it cannot sign in."""
    host, _ = _host()
    host.deliver(_req())
    host.output("s" * 32)
    host.turn_ended("s" * 32, ok=False, error=core.AUTH_FAILED)
    host.stop("s" * 32) if meanwhile == "stop" else host.shutdown()
    items = reports(host.exited("s" * 32, 0))
    assert [i for i in items if i["type"] == "session"] == [
        {"type": "session", "session": "s" * 32, "state": "failed", "error": "auth_failed"}]


def test_another_failed_turn_keeps_the_session():
    host, _ = _host()
    host.deliver(_req())
    host.output("s" * 32)
    actions = host.turn_ended("s" * 32, ok=False, error="error_during_execution")
    assert "Close" not in kinds(actions)
    assert reports(actions)[-1] == {"type": "session", "session": "s" * 32, "state": "idle"}


def test_exit_while_working_fails_session_and_turn():
    host, _ = _host()
    first = _req()
    host.deliver(first)
    waiting = _req(text="second")
    host.deliver(waiting)
    actions = host.exited("s" * 32, 1)
    items = reports(actions)
    assert {"type": "turn", "session": "s" * 32, "command": first["command"], "phase": "ended", "ok": False,
            "error": "exit 1"} in items
    assert {"type": "turn", "session": "s" * 32, "command": waiting["command"], "phase": "ended", "ok": False,
            "error": "session_failed"} in items
    assert items[-1] == {"type": "session", "session": "s" * 32, "state": "failed", "error": "exit 1"}
    assert host.status()["slots"]["analyst"]["used"] == 0


def test_an_idle_process_killed_by_a_signal_exits_the_session_without_failing():
    """a reinstall's pkill ends an idle claude with 143 — nothing was lost, the next input resumes it."""
    host, _ = _host()
    host.deliver(_req())
    host.turn_ended("s" * 32, ok=True)
    items = reports(host.exited("s" * 32, 143))
    assert items == [{"type": "session", "session": "s" * 32, "state": "exited"}]
    assert host.status()["slots"]["analyst"]["used"] == 0


def test_an_idle_exit_frees_the_slot_for_the_next_session():
    host, _ = _host(slots=1)
    host.deliver(_req(session="a" * 32))
    host.turn_ended("a" * 32, ok=True)
    host.exited("a" * 32, 143)
    ack, actions = host.deliver(_req(session="b" * 32))
    assert ack == {"state": "starting"} and "Spawn" in kinds(actions)


def test_a_turn_cut_by_a_signal_still_fails():
    host, _ = _host()
    request = _req()
    host.deliver(request)
    items = reports(host.exited("s" * 32, 143))
    assert {"type": "turn", "session": "s" * 32, "command": request["command"], "phase": "ended", "ok": False,
            "error": "exit 143"} in items
    assert items[-1] == {"type": "session", "session": "s" * 32, "state": "failed", "error": "exit 143"}


def test_a_spawn_failure_still_fails():
    host, _ = _host()
    host.deliver(_req())
    host.turn_ended("s" * 32, ok=True)
    items = reports(host.exited("s" * 32, -1, error="spawn_failed"))
    assert items[-1] == {"type": "session", "session": "s" * 32, "state": "failed", "error": "spawn_failed"}


def test_idle_timeout_closes_only_idle_past_the_limit():
    host, clock = _host(idle=60)
    host.deliver(_req())
    clock.t += 120
    assert host.tick() == []  # working
    host.turn_ended("s" * 32, ok=True)
    clock.t += 30
    assert host.tick() == []
    clock.t += 31
    assert kinds(host.tick()) == ["Close"]
    assert host.tick() == []  # closed once


def test_a_running_turn_beats_on_every_tick():
    """the cabinet calls work unconfirmed after 2 min without a heartbeat; a long turn sends nothing else."""
    host, clock = _host(idle=60)
    host.deliver(_req())
    assert host.tick() == []  # written, not answered yet: «start delayed» must stay possible
    host.output("s" * 32)
    beat = {"type": "session", "session": "s" * 32, "state": "working"}
    assert reports(host.tick()) == [beat]
    clock.t += 600  # a long tool call: the process is alive, the turn runs
    assert reports(host.tick()) == [beat]
    host.turn_ended("s" * 32, ok=True)
    assert host.tick() == []  # idle: nothing to confirm
    host.deliver(_req(text="[board] KD-1: a message for you"))
    host.output("s" * 32)
    assert reports(host.tick()) == [beat]
    host.stop("s" * 32)
    assert host.tick() == []  # stopping: no beat for a process on its way out


def test_stop_kills_and_reports_stopped():
    host, _ = _host()
    request = _req()
    host.deliver(request)
    assert kinds(host.stop("s" * 32)) == ["Kill"]
    items = reports(host.exited("s" * 32, -15))
    assert {"type": "turn", "session": "s" * 32, "command": request["command"], "phase": "ended", "ok": False,
            "error": "stopped"} in items
    assert items[-1] == {"type": "session", "session": "s" * 32, "state": "stopped"}


def test_stop_of_a_waiting_or_unknown_session():
    host, _ = _host(slots=1)
    host.deliver(_req(session="a" * 32))
    waiting = _req(session="b" * 32)
    host.deliver(waiting)
    items = reports(host.stop("b" * 32))
    assert items[-1] == {"type": "session", "session": "b" * 32, "state": "stopped"}
    assert items[0]["command"] == waiting["command"] and items[0]["error"] == "stopped"
    assert host.status()["slots"]["analyst"]["waiting"] == 0
    assert reports(host.stop("c" * 32)) == [{"type": "session", "session": "c" * 32, "state": "stopped"}]


def test_freed_slot_spawns_next_waiting():
    host, _ = _host(slots=1)
    host.deliver(_req(session="a" * 32))
    host.deliver(_req(session="b" * 32))
    host.deliver(_req(session="c" * 32))
    actions = host.exited("a" * 32, 1)
    spawned = [a.session for a in actions if isinstance(a, core.Spawn)]
    assert spawned == ["b" * 32]
    assert host.status()["slots"]["analyst"] == {"used": 1, "max": 1, "waiting": 1}


def test_shutdown_leaves_unstarted_inputs_to_the_cabinet_and_idle_sessions_exit_clean():
    """The host stops (a restart, a release): an input that never reached the process is not reported (the cabinet
    keeps it delivered and the next host's start queues it again); an idle process exits resumable; a turn cut by the
    stop fails with host_restart; nothing new spawns."""
    host, _ = _host(slots=1)
    first = _req(session="a" * 32)
    host.deliver(first)  # working
    behind = _req(session="a" * 32, text="[board] KD-1: a message for you")
    host.deliver(behind)  # waits behind the turn
    host.deliver(_req(session="b" * 32))  # waits for the agent's slot
    actions = host.shutdown()
    assert kinds(actions) == ["Close"] and reports(actions) == []
    out = host.exited("a" * 32, -15)
    turns = [r for r in reports(out) if r["type"] == "turn"]
    assert turns == [{"type": "turn", "session": "a" * 32, "command": first["command"], "phase": "ended", "ok": False,
                      "error": "host_restart"}]
    assert behind["command"] not in json.dumps(reports(out))
    assert {"type": "session", "session": "a" * 32, "state": "failed", "error": "host_restart"} in reports(out)
    assert not any(isinstance(a, core.Spawn) for a in out)

    idle, _ = _host(slots=1)
    idle.deliver(_req(session="c" * 32))
    idle.turn_ended("c" * 32, True)
    idle.shutdown()
    assert reports(idle.exited("c" * 32, 0)) == [{"type": "session", "session": "c" * 32, "state": "exited"}]


def test_a_turn_that_ends_during_shutdown_writes_no_next_input():
    host, _ = _host(slots=1)
    host.deliver(_req(session="a" * 32))
    host.deliver(_req(session="a" * 32, text="[board] KD-1: a message for you"))
    host.shutdown()
    out = host.turn_ended("a" * 32, True)
    assert not any(isinstance(a, core.Write) for a in out)
    assert reports(host.exited("a" * 32, 0))[-1] == {"type": "session", "session": "a" * 32, "state": "exited"}


def test_cancel_takes_an_input_out_of_the_queues_until_it_starts():
    host, _ = _host(slots=1)
    first = _req(session="a" * 32)
    host.deliver(first)
    behind = _req(session="a" * 32, text="[board] KD-1: a message for you")
    host.deliver(behind)
    waiting = _req(session="b" * 32)
    host.deliver(waiting)
    assert host.cancel(behind["command"]) == ({"cancelled": True}, [])
    assert host.status()["sessions"][0]["queued_inputs"] == 0
    ack, actions = host.cancel(waiting["command"])
    assert ack == {"cancelled": True}
    assert reports(actions) == [{"type": "session", "session": "b" * 32, "state": "exited"}]
    assert host.status()["slots"]["analyst"]["waiting"] == 0
    assert host.cancel(first["command"]) == ({"cancelled": False}, [])  # it runs: too late
    assert host.cancel("f" * 32) == ({"cancelled": False}, [])
    out = host.turn_ended("a" * 32, True)  # nothing behind it any more: the session goes idle
    assert not any(isinstance(a, core.Write) for a in out)


def test_not_hosted_refused():
    host, _ = _host()
    with pytest.raises(protocol.ProtocolError) as exc:
        host.deliver(_req(agent="boss"))
    assert exc.value.kind == "refused"


def test_parse_rejects_bad_requests():
    good = _req(session="a" * 32)
    assert protocol.parse(json.dumps(good).encode()) == good
    assert protocol.parse(b'{"op": "status"}') == {"op": "status"}
    bad = [
        b"not json", b"[]", b'{"op": "reboot"}',
        json.dumps({**good, "command": "x"}).encode(),
        json.dumps({**good, "text": "two\nlines"}).encode(),
        json.dumps({**good, "text": "x" * 2001}).encode(),
        json.dumps({**good, "resume": "--resume"}).encode(),
        json.dumps({**good, "agent": "../etc"}).encode(),
        json.dumps({**good, "thread": True}).encode(),
        json.dumps({**good, "extra": 1}).encode(),
        json.dumps({"op": "stop"}).encode(),
    ]
    for line in bad:
        with pytest.raises(protocol.ProtocolError):
            protocol.parse(line)
    assert json.loads(protocol.ok(state="queued")) == {"ok": True, "state": "queued"}
    assert json.loads(protocol.error("refused", "no")) == {"ok": False, "error": "refused", "message": "no"}


def test_turn_ended_report_carries_usage():
    host, _ = _host()
    request = _req()
    host.deliver(request)
    used = {"models": {"m": {"in": 1, "out": 2, "cache_read": 3, "cache_write": 4, "cost": 0.5}}, "requests": 2}
    turn = [i for i in reports(host.turn_ended("s" * 32, ok=True, usage=used)) if i["type"] == "turn"][0]
    assert turn == {"type": "turn", "session": "s" * 32, "command": request["command"], "phase": "ended", "ok": True,
                    "usage": used}


# --- worker jobs --------------------------------------------------------------------
SLOTS = {"dev": ["dev/worker1", "dev/worker2"]}


def _jobs_host(slots=1):
    clock = Clock()
    config = session_config.HostConfig({"analyst": 1, "dev": slots}, 600)
    host = core.Host(config, {"analyst": "digi-analyst", "dev": "digi-dev"}, clock,
                     job_slots=lambda agent: SLOTS.get(agent, []))
    return host, clock


def _job(n=1, agent="dev"):
    job = f"{n:032x}"
    return {"op": "job", "job": job, "agent": agent,
            "definition": {"id": job, "agent": agent, "card": "KD-7", "spec": "implementer", "timeout": 3600}}


END = {"state": "done", "reason": "", "comment": "Готово.", "usage": None, "result": {"summary": "ok"}}


def test_a_job_takes_a_free_worker_slot():
    host, _ = _jobs_host()
    request = _job()
    ack, actions = host.job(request)
    assert ack == {"state": "starting"}
    assert kinds(actions) == ["Report", "SpawnJob"]
    assert reports(actions) == [{"type": "job", "job": request["job"], "state": "starting", "slot": "dev/worker1"}]
    spawn = actions[1]
    assert (spawn.job, spawn.agent, spawn.slot, spawn.unit, spawn.timeout) == (
        request["job"], "dev", "dev/worker1", "digipaddock-session-job-" + request["job"][:12], 3600)
    assert spawn.definition == request["definition"]


def test_jobs_beyond_the_worker_slots_wait_and_start_when_one_ends():
    host, _ = _jobs_host()
    first, second, third = _job(1), _job(2), _job(3)
    host.job(first)
    assert host.job(second)[1][1].slot == "dev/worker2"
    ack, actions = host.job(third)
    assert ack == {"state": "queued", "position": 1} and actions == []
    out = host.job_exited(first["job"], 0)
    assert reports(out)[0]["job"] == first["job"] and reports(out)[0]["state"] == "failed"
    spawn = [a for a in out if isinstance(a, core.SpawnJob)]
    assert [(s.job, s.slot) for s in spawn] == [(third["job"], "dev/worker1")]
    assert host.status()["worker_slots"]["dev"] == {"used": 2, "max": 2, "waiting": 0}


def test_a_job_repeated_is_the_job_already_known():
    host, _ = _jobs_host()
    request = _job()
    host.job(request)
    assert host.job(request) == ({"state": "running"}, [])
    host.job_exited(request["job"], 0)
    assert host.job(request) == ({"state": "ended"}, [])


def test_job_cancel_queued_and_running():
    host, _ = _jobs_host()
    running, other, queued = _job(1), _job(2), _job(3)
    for request in (running, other, queued):
        host.job(request)
    assert host.job_cancel(queued["job"]) == ({"cancelled": "queued"}, [])
    assert host.status()["worker_slots"]["dev"]["waiting"] == 0
    ack, actions = host.job_cancel(running["job"])
    assert ack == {"cancelled": "running"} and kinds(actions) == ["KillJob"]
    assert host.job_cancel("f" * 32) == ({"cancelled": "unknown"}, [])
    host.job_exited(running["job"], 143)
    assert host.job_cancel(running["job"]) == ({"cancelled": "ended"}, [])


def test_a_job_end_line_is_the_outcome():
    host, _ = _jobs_host()
    request = _job()
    host.job(request)
    host.job_line(request["job"], END)
    [item] = reports(host.job_exited(request["job"], 0))
    assert item == {"type": "job", "job": request["job"], **END}


def test_a_job_without_an_end_line_failed_with_its_exit():
    host, _ = _jobs_host()
    request = _job()
    host.job(request)
    [item] = reports(host.job_exited(request["job"], 1))
    assert (item["state"], item["reason"]) == ("failed", "exit 1")
    assert item["comment"] == "Рабочая сессия завершилась без результата (exit 1)."


def test_a_job_that_never_started_says_so():
    """The owner reads "did not start", not "exit spawn_failed"."""
    host, _ = _jobs_host()
    request = _job()
    host.job(request)
    [item] = reports(host.job_exited(request["job"], -1, error="spawn_failed"))
    assert (item["state"], item["reason"]) == ("failed", "spawn_failed")
    assert item["comment"] == "Рабочая сессия не запустилась."


def test_a_stopped_job_is_cancelled():
    host, _ = _jobs_host()
    request = _job()
    host.job(request)
    host.job_cancel(request["job"])
    [item] = reports(host.job_exited(request["job"], 143))
    assert (item["state"], item["reason"], item["comment"]) == ("cancelled", "stopped", "")
    other = _job(2)
    host.job(other)
    host.job_cancel(other["job"])
    host.job_line(other["job"], END)  # it ended before the stop reached it: its outcome stands
    assert reports(host.job_exited(other["job"], 0))[0]["state"] == "done"


def test_sessions_and_jobs_have_separate_pools():
    host, _ = _jobs_host(slots=1)
    assert host.deliver(_req(session="a" * 32, agent="dev"))[0] == {"state": "starting"}
    assert host.job(_job(1))[0] == {"state": "starting"}
    assert host.job(_job(2))[0] == {"state": "starting"}
    assert host.deliver(_req(session="b" * 32, agent="dev"))[0]["state"] == "queued"
    status = host.status()
    assert status["slots"]["dev"]["used"] == 1 and status["worker_slots"]["dev"]["used"] == 2
    assert [j["state"] for j in status["jobs"]] == ["running", "running"]


def test_running_job_is_journaled_inflight():
    host, _ = _jobs_host()
    first, second, queued = _job(1), _job(2), _job(3)
    for request in (first, second, queued):
        host.job(request)
    assert set(host.inflight()) == {first["job"], second["job"]}
    host.job_exited(first["job"], 0)
    assert first["job"] not in host.inflight()


def test_a_job_of_an_agent_without_worker_slots_is_refused():
    host, _ = _jobs_host()
    with pytest.raises(protocol.ProtocolError) as err:
        host.job(_job(agent="analyst"))
    assert err.value.kind == "refused"


def test_a_host_that_stops_drops_waiting_jobs_and_fails_running_ones():
    host, _ = _jobs_host()
    running = [_job(n) for n in (1, 2)]
    for request in running + [_job(3)]:
        host.job(request)
    host.shutdown()
    assert host.status()["worker_slots"]["dev"]["waiting"] == 0
    [item] = reports(host.job_exited(running[0]["job"], 143))
    assert (item["state"], item["reason"]) == ("failed", "host_restart")
    assert [a for a in host.job_exited(running[1]["job"], 143) if isinstance(a, core.SpawnJob)] == []


# --- a session is one thread of one agent, and the process knows which ---------------------------------
def _spawn(actions):
    [spawn] = [a for a in actions if isinstance(a, core.Spawn)]
    return spawn


def test_the_spawn_carries_the_sessions_thread_and_card():
    host, _ = _host()
    spawn = _spawn(host.deliver(_req(thread=7, card="KD-1"))[1])
    assert (spawn.session, spawn.thread, spawn.card) == ("s" * 32, 7, "KD-1")
    host, _ = _host()
    chat = _spawn(host.deliver(_req(session="c" * 32, thread=9, card="", text="[chat] a message"))[1])
    assert (chat.thread, chat.card) == (9, "")


@pytest.mark.parametrize("change, reason", [
    ({"agent": "dev"}, "агенту analyst"),
    ({"thread": 8}, "треду 7"),
    ({"card": "KD-2"}, "карточке KD-1"),
    ({"card": ""}, "карточке KD-1"),
], ids=["agent", "thread", "card", "card to chat"])
@pytest.mark.parametrize("live", ["working", "idle", "queued"])
def test_a_delivery_of_another_agent_thread_or_card_is_refused_before_any_turn(change, reason, live):
    host, _ = _host(slots=1)
    if live == "queued":  # the session waits for the slot another one holds
        host.deliver(_req(session="o" * 32, thread=3, card="KD-3"))
    host.deliver(_req())
    if live == "idle":
        host.turn_ended("s" * 32, ok=True)
    before = host.status()
    with pytest.raises(protocol.ProtocolError) as err:
        host.deliver(_req(**change))
    assert err.value.kind == "refused" and reason in str(err.value)
    assert host.status() == before  # nothing queued, spawned or written


def test_a_chat_session_takes_its_chat_deliveries_and_refuses_a_card():
    host, _ = _host()
    host.deliver(_req(session="c" * 32, thread=9, card="", text="[chat] one"))
    host.turn_ended("c" * 32, ok=True)
    ack, actions = host.deliver(_req(session="c" * 32, thread=9, card="", text="[chat] two"))
    assert ack == {"state": "running"} and kinds(actions) == ["Report", "Write"]
    with pytest.raises(protocol.ProtocolError, match="личному чату"):
        host.deliver(_req(session="c" * 32, thread=9, card="KD-1"))


def test_a_b_a_on_one_slot_resumes_each_with_its_own_runtime_id():
    """AC #2: B takes A's freed slot without A's Claude session; A comes back with its own."""
    a, b = "a" * 32, "b" * 32
    run_a, run_b = SID, "1b9c3f1e-1111-2222-3333-444455556666"
    host, _ = _host(slots=1)
    assert _spawn(host.deliver(_req(session=a, thread=7, card="KD-1"))[1]).resume == ""
    host.runtime(a, run_a)
    host.turn_ended(a, ok=True)
    host.deliver(_req(session=b, thread=8, card="KD-2"))  # the cabinet sends B's own (empty) resume id
    spawn_b = _spawn(host.exited(a, 0))  # A's idle process closed for B
    assert (spawn_b.session, spawn_b.resume, spawn_b.thread, spawn_b.card) == (b, "", 8, "KD-2")
    host.runtime(b, run_b)
    host.turn_ended(b, ok=True)
    host.deliver(_req(session=a, thread=7, card="KD-1", resume=run_a))
    spawn_a = _spawn(host.exited(b, 0))
    assert (spawn_a.session, spawn_a.resume, spawn_a.thread, spawn_a.card) == (a, run_a, 7, "KD-1")


def test_a_new_host_resumes_a_session_with_the_cabinets_runtime_id():
    """A restart forgets every session: the binding is the cabinet's, sent with each delivery."""
    host, _ = _host()
    spawn = _spawn(host.deliver(_req(thread=7, card="KD-1", resume=SID))[1])
    assert (spawn.resume, spawn.thread, spawn.card) == (SID, 7, "KD-1")


# --- a resume that cannot start ----------------------------------------------------------------------
def test_a_resumed_process_that_dies_before_output_restarts_fresh_once():
    """The Claude session it resumes is gone (its files were cleaned): the same input runs once without --resume;
    a second death fails as before."""
    host, _ = _host()
    request = _req(resume=SID)
    host.deliver(request)
    actions = host.exited("s" * 32, 1)
    assert not [r for r in reports(actions) if r.get("phase") == "ended"]
    spawn = _spawn(actions)
    assert spawn.resume == ""
    [write] = [a for a in actions if isinstance(a, core.Write)]
    assert (write.command, write.text) == (request["command"], request["text"])
    again = reports(host.exited("s" * 32, 1))
    assert {"type": "turn", "session": "s" * 32, "command": request["command"], "phase": "ended", "ok": False,
            "error": "exit 1"} in again
    assert again[-1]["state"] == "failed"


def test_a_fresh_retry_of_a_paused_role_waits_for_the_resume():
    """Review M4: a paused agent starts nothing — the retry runs without --resume once the pause lifts, and a
    delivery meanwhile does not bring the dead resume id back."""
    host, _ = _host()
    request = _req(resume=SID)
    host.deliver(request)
    host.pause("analyst", "op1")
    assert not [a for a in host.exited("s" * 32, 1) if isinstance(a, core.Spawn)]
    host.deliver(_req(resume=SID, text="[board] more"))
    actions = host.resume("analyst")
    assert _spawn(actions).resume == ""
    [write] = [a for a in actions if isinstance(a, core.Write)]
    assert write.command == request["command"]


def test_an_urgent_fresh_retry_starts_while_its_role_only_drains():
    host, _ = _host()
    host.urgent_in_pause = lambda agent: True
    host.deliver({**_req(resume=SID), "urgent": True})
    host.pause("analyst", "op1")
    assert _spawn(host.exited("s" * 32, 1)).resume == ""


def test_a_fresh_process_that_dies_before_output_fails_as_before():
    host, _ = _host()
    request = _req()
    host.deliver(request)
    items = reports(host.exited("s" * 32, 1))
    assert {"type": "turn", "session": "s" * 32, "command": request["command"], "phase": "ended", "ok": False,
            "error": "exit 1"} in items
    assert items[-1] == {"type": "session", "session": "s" * 32, "state": "failed", "error": "exit 1"}


def test_a_process_that_died_after_output_fails_as_before():
    host, _ = _host()
    request = _req(resume=SID)
    host.deliver(request)
    host.output("s" * 32)
    actions = host.exited("s" * 32, 1)
    assert not [a for a in actions if isinstance(a, core.Spawn)]
    assert reports(actions)[-1] == {"type": "session", "session": "s" * 32, "state": "failed", "error": "exit 1"}


def test_a_resumed_spawn_that_failed_to_start_is_not_retried():
    host, _ = _host()
    host.deliver(_req(resume=SID))
    actions = host.exited("s" * 32, -1, error="spawn_failed")
    assert not [a for a in actions if isinstance(a, core.Spawn)]
    assert reports(actions)[-1]["state"] == "failed"


# --- one turn each between the ready sessions of an agent ----------------------------------------------
A, B, C = "a" * 32, "b" * 32, "c" * 32
RUN = {A: SID, B: "1b9c3f1e-1111-2222-3333-444455556666", C: "2b9c3f1e-1111-2222-3333-444455556666"}
THREAD = {A: (7, "KD-1"), B: (8, "KD-2"), C: (9, "KD-3")}


class _Sim:
    """The server's side in memory: a spawned process reports its Claude session id, a closed one exits at once (its
    stdin's EOF between turns); every input written, every spawn and report is kept in order."""

    def __init__(self, host, close=True):
        self.host, self.close = host, close
        self.live, self.written, self.spawns, self.items, self.closing = set(), [], [], [], []

    def run(self, actions):
        for action in actions:
            if isinstance(action, core.Spawn):
                assert action.session not in self.live, "one session, one process"
                self.live.add(action.session)
                self.spawns.append(action)
                self.host.runtime(action.session, RUN[action.session])
            elif isinstance(action, core.Write):
                assert action.command not in self.written, "an input written twice"
                self.written.append(action.command)
            elif isinstance(action, core.Report):
                self.items.append(action.item)
            elif isinstance(action, core.Close):
                self.closing.append(action.session)
        while self.close and self.closing:
            self.exit(self.closing.pop(0))
        return actions

    def exit(self, session, rc=0, error=""):
        self.live.discard(session)
        return self.run(self.host.exited(session, rc, error))

    def send(self, session, name, **extra):
        thread, card = THREAD[session]
        request = _req(session=session, command=name.ljust(32, "0"), thread=thread, card=card,
                       resume=extra.pop("resume", ""), text=f"[board] {card}: {name}")
        request.update(extra)
        ack, actions = self.host.deliver(request)
        self.run(actions)
        return ack

    def end(self, session, ok=True):
        return self.run(self.host.turn_ended(session, ok))

    def order(self):
        return [c.rstrip("0") for c in self.written]

    def waiting(self, agent="analyst"):
        return self.host.status()["slots"][agent]["waiting"]


def test_one_slot_serves_ready_sessions_one_turn_each_in_turn():
    """AC #1: A runs a1 with a2, a3 behind it, B waits with b1, b2: a1, b1, a2, b2, a3; FIFO inside each thread."""
    host, _ = _host(slots=1)
    sim = _Sim(host)
    assert sim.send(A, "a1") == {"state": "starting"}
    sim.send(A, "a2")
    sim.send(A, "a3")
    assert sim.send(B, "b1") == {"state": "queued", "position": 1}
    sim.send(B, "b2")
    for session in (A, B, A, B, A):
        sim.end(session)
    assert sim.order() == ["a1", "b1", "a2", "b2", "a3"]
    # AC #4: each process of a thread resumes that thread's own Claude session, never another's
    assert [(s.session, s.resume, s.thread, s.card) for s in sim.spawns] == [
        (A, "", 7, "KD-1"), (B, "", 8, "KD-2"), (A, RUN[A], 7, "KD-1"), (B, RUN[B], 8, "KD-2"), (A, RUN[A], 7, "KD-1")]
    assert sim.waiting() == 0 and host.status()["sessions"][0]["queued_inputs"] == 0


def test_a_steady_inflow_to_one_thread_never_holds_a_ready_one():
    """AC #1: new messages keep coming to A; B, ready since a1, runs right after a1, and A's own stay in order."""
    host, _ = _host(slots=1)
    sim = _Sim(host)
    sim.send(A, "a1")
    sim.send(B, "b1")
    sim.send(A, "a2")
    sim.end(A)
    sim.send(A, "a3")
    sim.end(B)
    sim.send(A, "a4")
    sim.end(A)
    sim.end(A)
    assert sim.order() == ["a1", "b1", "a2", "a3", "a4"] and host.current(A) == "a4".ljust(32, "0")


def test_with_nobody_waiting_the_next_input_goes_to_the_same_process():
    """AC #2: no competitor — no close, no new spawn."""
    host, _ = _host(slots=1)
    sim = _Sim(host)
    sim.send(A, "a1")
    sim.send(A, "a2")
    actions = sim.end(A)
    assert kinds(actions) == ["Report", "Write"] and len(sim.spawns) == 1


def test_two_slots_take_a_free_one_first_and_rotate_only_when_both_are_taken():
    """AC #2: dev has two slots — B takes the free one while A keeps its process; C waits and gets one turn in turn;
    one session never runs in two processes (the simulation asserts it)."""
    host, _ = _host()
    sim = _Sim(host)
    sim.send(A, "a1", agent="dev")
    sim.send(A, "a2", agent="dev")
    assert sim.send(B, "b1", agent="dev") == {"state": "starting"}
    sim.send(B, "b2", agent="dev")
    sim.end(A)  # nobody waits for a slot: a2 in the same process
    assert len(sim.spawns) == 2 and sim.order() == ["a1", "b1", "a2"]
    sim.send(A, "a3", agent="dev")
    assert sim.send(C, "c1", agent="dev") == {"state": "queued", "position": 1}
    sim.end(A)  # A gives its slot to C
    sim.end(B)  # B gives its slot to A
    assert sim.order() == ["a1", "b1", "a2", "c1", "a3"]
    sim.end(C)
    sim.end(A)
    assert sim.order() == ["a1", "b1", "a2", "c1", "a3", "b2"]
    assert sim.waiting("dev") == 0


def test_an_urgent_input_behind_a_turn_never_gives_the_slot_up():
    host, _ = _host(slots=1)
    sim = _Sim(host)
    sim.send(A, "a1")
    sim.send(B, "b1")
    sim.send(A, "a2", urgent=True)
    sim.end(A)
    assert sim.order() == ["a1", "a2"]


def test_no_yield_that_frees_no_slot_for_the_one_waiting():
    """Review: an urgent process over the limit holds no slot of its own — A's close would free one only for the
    count, B would still wait; A keeps its process."""
    host, _ = _host(slots=1)
    sim = _Sim(host)
    sim.send(A, "a1")
    sim.send(A, "a2")
    sim.send(C, "c1", urgent=True)  # starts at once, over the slot
    sim.send(B, "b1")
    sim.end(A)
    assert sim.order() == ["a1", "c1", "a2"] and len(sim.spawns) == 2 and sim.waiting() == 1


def test_a_second_turn_end_while_the_first_yield_closes_keeps_its_process():
    """Review: two slots, one waiting C — A's yield already frees the slot C needs; B, ending its turn before A's
    exit, keeps its process (no needless restart)."""
    host, _ = _host()
    sim = _Sim(host, close=False)
    for session in (A, B):
        sim.send(session, session[0] + "1", agent="dev")
        sim.send(session, session[0] + "2", agent="dev")
    sim.send(C, "c1", agent="dev")
    sim.end(A)
    sim.end(B)
    assert sim.closing == [A] and sim.order() == ["a1", "b1", "b2"]
    sim.exit(sim.closing.pop())
    assert sim.order() == ["a1", "b1", "b2", "c1"] and [s.session for s in sim.spawns] == [A, B, C]


def test_a_waiting_session_reports_its_place_whenever_it_changes():
    """AC #5: the cabinet shows the place in the agent's queue; a session that gave its slot up comes in at the tail."""
    host, _ = _host(slots=1)
    sim = _Sim(host)
    sim.send(A, "a1")
    sim.send(A, "a2")
    sim.send(B, "b1")
    sim.send(C, "c1")
    queued = [i for i in sim.items if i.get("state") == "queued"]
    assert queued == [{"type": "session", "session": B, "state": "queued", "position": 1},
                      {"type": "session", "session": C, "state": "queued", "position": 2}]
    sim.items.clear()
    sim.end(A)
    assert [i for i in sim.items if i["type"] == "session"] == [
        {"type": "session", "session": B, "state": "starting", "slot": "analyst/1", "model": "", "effort": ""},
        {"type": "session", "session": C, "state": "queued", "position": 1},
        {"type": "session", "session": A, "state": "queued", "position": 2}]


def test_an_input_while_the_yielding_process_closes_keeps_one_place_in_the_queue():
    """AC #3: a3 comes while A closes after giving its slot up — A queues once, with a2 and a3 in order."""
    host, _ = _host(slots=1)
    sim = _Sim(host, close=False)
    sim.send(A, "a1")
    sim.send(A, "a2")
    sim.send(B, "b1")
    sim.end(A)
    assert sim.closing == [A]
    assert sim.send(A, "a3") == {"state": "starting"}
    sim.exit(sim.closing.pop())
    assert sim.waiting() == 1
    sim.close = True
    sim.end(B)
    sim.end(A)
    assert sim.order() == ["a1", "b1", "a2", "a3"] and sim.waiting() == 0


def test_a_stop_while_the_yielding_process_closes_ends_its_queue_once():
    """AC #3: the stop kills the closing process; its inputs end «stopped», it never queues again, B runs."""
    host, _ = _host(slots=1)
    sim = _Sim(host, close=False)
    sim.send(A, "a1")
    sim.send(A, "a2")
    sim.send(B, "b1")
    sim.end(A)
    assert kinds(host.stop(A)) == ["Kill"]
    sim.exit(sim.closing.pop())
    ended = [i for i in sim.items if i["type"] == "turn" and i.get("error") == "stopped"]
    assert [i["command"] for i in ended] == ["a2".ljust(32, "0")]
    assert {"type": "session", "session": A, "state": "stopped"} in sim.items
    assert sim.order() == ["a1", "b1"] and sim.waiting() == 0


def test_a_cancelled_input_of_a_yielded_session_leaves_no_entry_behind():
    """AC #3: the cabinet takes back a3 (its card moved on) while A waits: A leaves the queue, nothing is lost."""
    host, _ = _host(slots=1)
    sim = _Sim(host)
    sim.send(A, "a1")
    sim.send(A, "a2")
    sim.send(B, "b1")
    sim.send(B, "b2")
    sim.end(A)
    assert host.cancel("a2".ljust(32, "0"))[0] == {"cancelled": True}
    assert sim.waiting() == 0
    sim.end(B)  # nobody waits now: b2 in the same process
    assert sim.order() == ["a1", "b1", "b2"] and len(sim.spawns) == 2
    assert host.cancel("a2".ljust(32, "0"))[0] == {"cancelled": False}


def test_a_spawn_that_fails_after_a_yield_fails_only_its_own_session():
    """AC #3: B's process never starts: b1 fails, b2 with it; A, which gave its slot to B, runs a2 on its own id."""
    host, _ = _host(slots=1)
    sim = _Sim(host)
    sim.send(A, "a1")
    sim.send(A, "a2")
    sim.send(B, "b1")
    sim.send(B, "b2")
    sim.end(A)
    assert sim.order() == ["a1", "b1"]
    sim.exit(B, -1, error="spawn_failed")
    failed = {i["command"].rstrip("0"): i["error"] for i in sim.items if i["type"] == "turn" and not i.get("ok", True)}
    assert failed == {"b1": "spawn_failed", "b2": "session_failed"}
    assert sim.order() == ["a1", "b1", "a2"] and (sim.spawns[-1].session, sim.spawns[-1].resume) == (A, RUN[A])
    assert sim.waiting() == 0


def test_a_host_stop_while_a_session_waits_after_its_yield_starts_nothing():
    """AC #3: the host's stop drops the queued inputs for the cabinet to deliver again (the next host resumes them
    with the cabinet's runtime id); nothing spawns and no queue entry is left."""
    host, _ = _host(slots=1)
    sim = _Sim(host, close=False)
    sim.send(A, "a1")
    sim.send(A, "a2")
    sim.send(B, "b1")
    sim.end(A)
    sim.run(host.shutdown())
    sim.close = True
    sim.exit(sim.closing.pop(0))
    assert sim.order() == ["a1"] and len(sim.spawns) == 1 and sim.waiting() == 0
    assert all(s["queued_inputs"] == 0 for s in host.status()["sessions"])
