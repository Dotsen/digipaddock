"""a card's delivery may name the model and effort its session runs on; the host starts the process on
them, restarts it when they change, and drops the history once when a new model refuses the old context."""

import json

import pytest

from kd import session_config
from kd.session_host import core, protocol

A = "a" * 32
SID = "0b9c3f1e-1111-2222-3333-444455556666"
SPEC = {"id": "or-x", "provider": "openrouter", "upstream": "anthropic/claude-sonnet-4.5", "base_url": ""}


def _line(**over):
    fields = {"op": "deliver", "command": "1" * 32, "session": A, "agent": "dev", "thread": 9, "card": "KD-7",
              "text": "[board] KD-7 assigned to you", "resume": ""}
    fields.update(over)
    return json.dumps(fields).encode() + b"\n"


def _req(**over):
    return json.loads(_line(**over))


def _host():
    return core.Host(session_config.HostConfig({"dev": 1}, 600), {"dev": "digi-dev"}, lambda: 1000.0)


def kinds(actions):
    return [type(a).__name__ for a in actions]


def reports(actions):
    return [a.item for a in actions if isinstance(a, core.Report)]


def _spawn(actions):
    [spawn] = [a for a in actions if isinstance(a, core.Spawn)]
    return spawn


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setattr(core.llm_models, "key_present", lambda *a, **k: True)


# --- the protocol -------------------------------------------------------------------------------------------
def test_protocol_accepts_model_and_effort_with_a_card_only():
    parsed = protocol.parse(_line(model=SPEC, effort="high"))
    assert parsed["model"] == SPEC and parsed["effort"] == "high"
    assert "model" not in protocol.parse(_line()) and "effort" not in protocol.parse(_line())


@pytest.mark.parametrize("over", [
    {"model": SPEC, "card": ""}, {"effort": "high", "card": ""}, {"effort": "ultra"}, {"effort": ""},
    {"model": {**SPEC, "extra": 1}}, {"model": {**SPEC, "base_url": "https://x"}}, {"model": {**SPEC, "id": "sonnet"}},
    {"model": {**SPEC, "provider": "together"}}, {"model": "or-x"}, {"model": None},
], ids=repr)
def test_protocol_refuses_a_bad_model_or_effort(over):
    with pytest.raises(protocol.ProtocolError):
        protocol.parse(_line(**over))


# --- the spawn ----------------------------------------------------------------------------------------------
def test_the_cards_session_spawns_with_model_and_effort():
    _, actions = _host().deliver(_req(model=SPEC, effort="high"))
    spawn = _spawn(actions)
    assert spawn.model == SPEC and spawn.effort == "high"
    assert reports(actions)[0] == {"type": "session", "session": A, "state": "starting", "slot": "dev/1",
                                   "model": "or-x", "effort": "high"}
    _, actions = _host().deliver(_req())
    spawn = _spawn(actions)
    assert spawn.model is None and spawn.effort == ""
    assert reports(actions)[0] == {"type": "session", "session": A, "state": "starting", "slot": "dev/1",
                                   "model": "", "effort": ""}


def test_no_key_refuses_the_delivery(monkeypatch):
    monkeypatch.setattr(core.llm_models, "key_present", lambda *a, **k: False)
    host = _host()
    with pytest.raises(protocol.ProtocolError) as exc:
        host.deliver(_req(model=SPEC))
    assert exc.value.kind == "refused" and str(exc.value).startswith("no_key: openrouter")
    assert host.status()["sessions"] == []  # nothing queued, nothing started
    _, actions = host.deliver(_req())  # the subscription needs no key
    assert "Spawn" in kinds(actions)


# --- a change of model or effort ---------------------------------------------------------------------------
def test_an_idle_process_is_closed_when_the_model_changes():
    host = _host()
    host.deliver(_req(model=SPEC))
    host.output(A)
    host.turn_ended(A, True)
    assert host._sessions[A].proc == "idle"
    ack, actions = host.deliver(_req(command="2" * 32))  # the subscription now
    assert "Close" in kinds(actions) and "Write" not in kinds(actions)
    assert [c for c, _ in host._sessions[A].inputs] == ["2" * 32]
    actions = host.exited(A, 0)
    spawn = _spawn(actions)
    assert spawn.model is None and spawn.effort == ""
    [write] = [a for a in actions if isinstance(a, core.Write)]
    assert write.command == "2" * 32


def test_a_working_process_finishes_its_turn_then_restarts():
    host = _host()
    host.deliver(_req(model=SPEC))
    host.output(A)
    host.runtime(A, SID)
    ack, actions = host.deliver(_req(command="2" * 32, model=SPEC, effort="max"))
    assert "Close" not in kinds(actions) and ack["state"] == "running"
    actions = host.turn_ended(A, True)
    assert "Close" in kinds(actions) and "Write" not in kinds(actions)
    actions = host.exited(A, 0)
    spawn = _spawn(actions)
    assert (spawn.model, spawn.effort, spawn.resume) == (SPEC, "max", SID)


def test_the_same_model_and_effort_keep_the_process():
    host = _host()
    host.deliver(_req(model=SPEC, effort="high"))
    host.output(A)
    host.turn_ended(A, True)
    ack, actions = host.deliver(_req(command="2" * 32, model=dict(SPEC), effort="high"))
    assert ack == {"state": "running"} and "Close" not in kinds(actions) and "Write" in kinds(actions)


# --- the history across a switch ---------------------------------------------------------------------------
def test_history_dropped_once_after_a_switch():
    host = _host()
    request = _req(resume=SID, model=SPEC)
    host.deliver(request)
    host.output(A)
    actions = host.turn_ended(A, False, error="api 400", assistant=False, rejected=True)
    assert not [r for r in reports(actions) if r.get("phase") == "ended"]  # the turn runs again, once
    [status] = [r for r in reports(actions) if r.get("type") == "message"]
    assert status["kind"] == "status" and status["payload"]["key"] == "sys.session.history_dropped"
    assert status["payload"]["params"] == {"model": "or-x"} and status["command"] == request["command"]
    assert "Close" in kinds(actions)
    actions = host.exited(A, 0)
    spawn = _spawn(actions)
    assert spawn.resume == "" and spawn.model == SPEC
    [write] = [a for a in actions if isinstance(a, core.Write)]
    assert write.command == request["command"]
    host.output(A)
    again = reports(host.turn_ended(A, False, error="api 400", assistant=False, rejected=True))
    assert {"type": "turn", "session": A, "command": request["command"], "phase": "ended", "ok": False,
            "error": "api 400"} in again
    assert again[-1]["state"] == "idle"


def test_no_drop_when_the_model_answered_or_nothing_switched():
    host = _host()
    host.deliver(_req(resume=SID, model=SPEC))
    host.output(A)
    ended = reports(host.turn_ended(A, False, error="api 500", assistant=True))  # it read the history: a real error
    assert [r for r in ended if r.get("phase") == "ended"]
    host = _host()
    host.deliver(_req(resume=SID))  # the subscription resumed on the subscription
    host.output(A)
    ended = reports(host.turn_ended(A, False, error="api 400", assistant=False, rejected=True))
    assert [r for r in ended if r.get("phase") == "ended"]
    host = _host()  # final review (Important 1): a failure that is not a rejection (a 500, a timeout) keeps the history
    host.deliver(_req(resume=SID, model=SPEC))
    host.output(A)
    ended = reports(host.turn_ended(A, False, error="api 529 overloaded", assistant=False, rejected=False))
    assert [r for r in ended if r.get("phase") == "ended"]


# --- a model of the Claude subscription -------------------------------------------------------------
SONNET = {"id": "claude:sonnet", "provider": "subscription", "upstream": "sonnet", "base_url": ""}
OPUS = {"id": "claude:opus", "provider": "subscription", "upstream": "opus", "base_url": ""}


def test_protocol_accepts_a_subscription_spec_of_the_catalog_only():
    assert protocol.parse(_line(model=SONNET))["model"] == SONNET
    for bad in ({**SONNET, "upstream": "opus"}, {**SONNET, "id": "claude:gpt"}, {**SONNET, "base_url": "https://x"}):
        with pytest.raises(protocol.ProtocolError):
            protocol.parse(_line(model=bad))


def test_a_subscription_model_starts_without_a_kinds_key(monkeypatch):
    monkeypatch.setattr(core.llm_models, "key_present", lambda *a, **k: False)
    _, actions = _host().deliver(_req(model=SONNET, effort="low"))
    spawn = _spawn(actions)
    assert spawn.model == SONNET and spawn.effort == "low"
    # the cabinet hears which model the process runs on; the usage ledger's slug stays the subscription's (none)
    assert reports(actions)[0] == {"type": "session", "session": A, "state": "starting", "slot": "dev/1",
                                   "model": "claude:sonnet", "effort": "low"}


def test_another_subscription_model_restarts_the_process_and_keeps_the_history():
    """opus → sonnet is a new process with the same history: both are the subscription's, so a first failure is the
    turn's own and never drops the history as a switch of provider would."""
    host = _host()
    host.deliver(_req(model=OPUS))
    host.output(A)
    host.runtime(A, SID)
    host.turn_ended(A, True)
    _, actions = host.deliver(_req(command="2" * 32, model=SONNET))
    assert "Close" in kinds(actions)
    spawn = _spawn(host.exited(A, 0))
    assert (spawn.model, spawn.resume) == (SONNET, SID)
    host.output(A)
    ended = reports(host.turn_ended(A, False, error="api 400", assistant=False, rejected=True))
    assert [r for r in ended if r.get("phase") == "ended"]
    assert not [r for r in ended if r.get("type") == "message"]
