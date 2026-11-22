"""the wake of an approved critical command lives 15 minutes, so the host starts it at once — over the
agent's slots and through a factory pause while the role only drains; never while its install runs."""

import json

import pytest

from kd import session_config
from kd.session_host import core, operation, protocol

OP = "e" * 32
REV = "b" * 40
A, B, C = "a" * 32, "b" * 32, "c" * 32


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _host(slots=1):
    clock = Clock()
    config = session_config.HostConfig({"sysadmin": slots}, 600)
    return core.Host(config, {"sysadmin": "digi-sysadmin"}, clock), clock


_n = [0]


def _req(session, urgent=False, card="KD-1", thread=7):
    _n[0] += 1
    req = {"op": "deliver", "command": f"{_n[0]:032x}", "session": session, "agent": "sysadmin", "thread": thread,
           "card": card, "text": "[board] KD-1 wake", "resume": ""}
    if urgent:
        req["urgent"] = True
    return req


def kinds(actions):
    return [type(a).__name__ for a in actions]


def _spawns(actions):
    return [a for a in actions if isinstance(a, core.Spawn)]


# --- slots ------------------------------------------------------------------------------------------
def test_urgent_starts_over_the_agents_slots_while_another_turn_works():
    host, _ = _host()
    host.deliver(_req(A, card="KD-2", thread=8))  # a long turn of another card holds the only slot
    ack, actions = host.deliver(_req(B, urgent=True))
    assert ack == {"state": "starting", "urgent": True}
    [spawn] = _spawns(actions)
    assert spawn.session == B
    assert "Write" in kinds(actions) and "Close" not in kinds(actions)  # the working turn is never cut
    assert host.status()["slots"]["sysadmin"]["used"] == 2
    assert {s["session"]: s["slot"] for s in host.status()["sessions"]} == {A: "sysadmin/1", B: "sysadmin/2"}


def test_an_ordinary_wake_still_waits_for_a_free_slot():
    host, _ = _host()
    host.deliver(_req(A, card="KD-2", thread=8))
    ack, actions = host.deliver(_req(B))
    assert ack == {"state": "queued", "position": 1} and _spawns(actions) == []


def test_the_extra_slot_lets_no_ordinary_session_in():
    host, _ = _host()
    host.deliver(_req(A, card="KD-2", thread=8))
    host.deliver(_req(B, urgent=True))
    host.turn_ended(B, True)
    host.exited(B, 0)  # ended between turns: the urgent process is gone
    ack, actions = host.deliver(_req(C, card="KD-3", thread=9))
    assert ack["state"] == "queued" and _spawns(actions) == []  # A still holds the agent's one slot
    assert "Close" in kinds(host.turn_ended(A, True))
    actions = host.exited(A, 0)
    assert [s.session for s in _spawns(actions)] == [C]


def test_urgent_idle_process_closes_when_others_wait():
    host, _ = _host()
    host.deliver(_req(A, card="KD-2", thread=8))
    host.deliver(_req(B, urgent=True))
    host.deliver(_req(C, card="KD-3", thread=9))  # waits
    actions = host.turn_ended(B, True)
    assert "Close" in kinds(actions)  # over the limit and others wait: the extra slot goes at once
    assert _spawns(host.exited(B, 0)) == []  # A still holds the agent's one slot


def test_urgent_for_a_session_that_waits_takes_it_out_of_the_queue():
    host, _ = _host()
    host.deliver(_req(A, card="KD-2", thread=8))
    host.deliver(_req(B))  # queued behind A
    ack, actions = host.deliver(_req(B, urgent=True))
    assert ack["state"] == "starting" and [s.session for s in _spawns(actions)] == [B]
    assert host.status()["slots"]["sysadmin"]["waiting"] == 0


def test_urgent_for_a_working_session_waits_behind_its_own_turn():
    host, _ = _host()
    host.deliver(_req(A))
    ack, actions = host.deliver(_req(A, urgent=True))
    assert ack == {"state": "running", "position": 1} and actions == []
    assert "Write" in kinds(host.turn_ended(A, True))


def test_a_closing_session_with_an_urgent_input_respawns_over_the_slots():
    host, clock = _host(slots=1)
    host.deliver(_req(B))
    host.turn_ended(B, True)
    clock.t += 600
    assert kinds(host.tick()) == ["Close"]  # its idle time ran out: closing
    ack, _ = host.deliver(_req(B, urgent=True))
    assert ack["state"] == "starting"
    ack, _ = host.deliver(_req(A, card="KD-2", thread=8))  # takes the slot as soon as B is gone
    assert ack["state"] == "queued"
    actions = host.exited(B, 0)
    assert [s.session for s in _spawns(actions)] == [A, B]
    assert host.status()["slots"]["sysadmin"]["used"] == 2


def test_an_idle_extra_process_never_holds_an_ordinary_wake_for_its_idle_time():
    """Review: with an urgent process idle over the limit, one preemption is not enough — the slot must free."""
    host, _ = _host()
    host.deliver(_req(A, card="KD-2", thread=8))
    host.deliver(_req(B, urgent=True))
    host.turn_ended(B, True)  # nobody waits: B stays idle over the limit
    host.turn_ended(A, True)  # A idle too
    ack, actions = host.deliver(_req(C, card="KD-3", thread=9))
    assert ack["state"] == "queued" and kinds(actions) == ["Close", "Report"]
    closed = actions[0].session
    actions = host.exited(closed, 0)
    other = B if closed == A else A
    assert kinds(actions) == ["Report", "Close"] and actions[1].session == other  # the second idle one goes too
    actions = host.exited(other, 0)
    assert [s.session for s in _spawns(actions)] == [C]


def test_an_urgent_input_runs_before_older_inputs_of_its_session():
    host, _ = _host()
    host.urgent_in_pause = lambda agent: True
    host.deliver(_req(A))
    ordinary, urgent = _req(A), _req(A, urgent=True)
    host.deliver(ordinary)
    host.deliver(urgent)
    host.pause("sysadmin", OP)
    actions = host.turn_ended(A, True)
    [write] = [a for a in actions if isinstance(a, core.Write)]
    assert write.command == urgent["command"]
    assert "Close" in kinds(host.turn_ended(A, True))  # the ordinary one waits for the resume


def test_only_urgent_waiters_close_no_idle_process():
    host, clock = _host()
    host.deliver(_req(A, card="KD-2", thread=8))  # the agent's one slot
    host.deliver(_req(B, urgent=True))  # over it
    host.turn_ended(B, True)  # idle, nobody waits
    clock.t += 600
    host.tick()  # B's idle time ran out: closing
    host.deliver(_req(B, urgent=True))  # a second approval while it closes
    host.turn_ended(A, True)  # A idle: nobody ordinary waits for it
    actions = host.exited(B, 0)
    assert [s.session for s in _spawns(actions)] == [B]
    assert "Close" not in kinds(actions)  # A is not closed for a session that starts without its slot


def test_cancel_forgets_an_urgent_input():
    host, _ = _host()
    host.deliver(_req(A))
    request = _req(A, urgent=True)
    host.deliver(request)
    assert host.cancel(request["command"])[0] == {"cancelled": True}
    assert host._urgent == set()


def test_the_seen_answer_is_the_same_for_a_repeated_urgent_delivery():
    host, _ = _host()
    host.deliver(_req(A, card="KD-2", thread=8))
    request = _req(B, urgent=True)
    first, _ = host.deliver(request)
    again, actions = host.deliver(request)
    assert again == first and actions == []


# --- the factory's pause ------------------------------------------------------------------------------
def test_urgent_goes_through_a_pause_that_only_drains():
    host, _ = _host()
    host.urgent_in_pause = lambda agent: True
    host.pause("sysadmin", OP)
    ack, actions = host.deliver(_req(B, urgent=True))
    assert ack["state"] == "starting" and [s.session for s in _spawns(actions)] == [B]
    assert not host.drained("sysadmin")  # the drain waits for this turn as for any other
    assert "Close" in kinds(host.turn_ended(B, True))  # paused: the process goes right after its turn
    host.exited(B, 0)
    assert host.drained("sysadmin")


def test_urgent_waits_while_the_roles_install_runs():
    host, _ = _host()
    host.urgent_in_pause = lambda agent: False
    host.pause("sysadmin", OP)
    ack, actions = host.deliver(_req(B, urgent=True))
    assert ack == {"state": "queued", "paused": True} and _spawns(actions) == []
    actions = host.resume("sysadmin")
    assert [s.session for s in _spawns(actions)] == [B]


def test_resume_starts_an_urgent_input_even_when_the_slot_is_taken():
    """Review: after an install the urgent wake must not wait behind the ordinary one the resume started."""
    host, _ = _host()
    host.pause("sysadmin", OP)
    host.deliver(_req(A, card="KD-2", thread=8))
    host.deliver(_req(B, urgent=True))
    actions = host.resume("sysadmin")
    assert [s.session for s in _spawns(actions)] == [A, B]


def test_an_ordinary_wake_waits_in_a_drain_as_before():
    host, _ = _host()
    host.urgent_in_pause = lambda agent: True
    host.pause("sysadmin", OP)
    ack, actions = host.deliver(_req(B))
    assert ack == {"state": "queued", "paused": True} and _spawns(actions) == []


def test_a_paused_working_session_runs_its_urgent_input_after_the_turn():
    host, _ = _host()
    host.urgent_in_pause = lambda agent: True
    host.deliver(_req(A))
    host.pause("sysadmin", OP)
    host.deliver(_req(A, urgent=True))
    actions = host.turn_ended(A, True)
    assert "Write" in kinds(actions) and "Close" not in kinds(actions)
    assert "Close" in kinds(host.turn_ended(A, True))


def test_by_default_a_pause_holds_urgent_too():
    host, _ = _host()
    host.pause("sysadmin", OP)
    ack, _ = host.deliver(_req(B, urgent=True))
    assert ack == {"state": "queued", "paused": True}


def _ops(tmp_path, agents=("sysadmin",)):
    host, clock = _host()
    ops = operation.Operations(str(tmp_path), host)
    ack, _ = ops.request({"op": "operation", "operation": OP, "kind": "agent.reinstall", "agents": list(agents),
                          "revision": REV, "timeout": 1800, "start": True}, lambda a: True, clock())
    assert ack["state"] == "running"
    return host, ops


def test_operations_let_urgent_through_only_while_the_role_drains(tmp_path):
    host, ops = _ops(tmp_path)
    assert host.urgent_in_pause("sysadmin") is True
    ops.launch(["sysadmin"])  # its batch installs now
    assert host.urgent_in_pause("sysadmin") is False
    ack, actions = host.deliver(_req(B, urgent=True))
    assert ack == {"state": "queued", "paused": True} and _spawns(actions) == []


def test_a_role_kept_paused_after_a_failed_install_holds_urgent(tmp_path):
    host, ops = _ops(tmp_path)
    ops.launch(["sysadmin"])
    ops.adopt({"state": "failed", "reason": "install_failed",
               "agents": {"sysadmin": {"state": "failed", "stage": "install", "error": "x"}}})
    assert host.paused == {"sysadmin": OP}  # kept for root's resume
    assert host.urgent_in_pause("sysadmin") is False


def test_a_role_not_in_the_operation_is_not_paused(tmp_path):
    host, ops = _ops(tmp_path)
    assert host.urgent_in_pause("dev") is False


# --- the protocol ------------------------------------------------------------------------------------
def _line(**fields):
    return json.dumps(fields).encode() + b"\n"


def _deliver(**over):
    fields = {"op": "deliver", "command": "1" * 32, "session": A, "agent": "sysadmin", "thread": 7, "card": "KD-1",
              "text": "[board] KD-1 wake", "resume": ""}
    fields.update(over)
    return _line(**fields)


def test_protocol_accepts_urgent_true_and_its_absence():
    assert protocol.parse(_deliver(urgent=True))["urgent"] is True
    assert "urgent" not in protocol.parse(_deliver())


@pytest.mark.parametrize("value", [False, 1, "yes", None])
def test_protocol_refuses_any_other_urgent(value):
    with pytest.raises(protocol.ProtocolError):
        protocol.parse(_deliver(urgent=value))


def test_protocol_still_needs_every_deliver_field():
    fields = json.loads(_deliver(urgent=True))
    del fields["resume"]
    with pytest.raises(protocol.ProtocolError):
        protocol.parse(_line(**fields))
