"""The autonomy design, piece 2: a card's delivery may name its probe exit; the host launches the card's session with
it (agent-launch --egress) and, like a change of model, closes a process whose exit is not the latest
delivery's — so a session whose consent ended runs no further turn with the exit."""

import json

import pytest

from kd import session_config
from kd.session_host import claude, core, protocol

A = "a" * 32
SID = "0b9c3f1e-1111-2222-3333-444455556666"


def _req(**over):
    fields = {
        "op": "deliver",
        "command": "1" * 32,
        "session": A,
        "agent": "dev",
        "thread": 9,
        "card": "KD-7",
        "text": "[board] KD-7 assigned to you",
        "resume": "",
    }
    fields.update(over)
    return protocol.parse(json.dumps(fields).encode() + b"\n")


def _host():
    return core.Host(
        session_config.HostConfig({"dev": 1}, 600), {"dev": "digi-dev"}, lambda: 1000.0
    )


def _spawns(actions):
    return [a for a in actions if isinstance(a, core.Spawn)]


def test_a_card_delivery_launches_with_its_exit():
    host = _host()
    _, actions = host.deliver(_req(egress="probe"))
    [spawn] = _spawns(actions)
    assert spawn.egress == "probe"
    context = claude.context_args(A, 9, "KD-7", egress="probe")
    assert context[-2:] == ["--egress", "probe"]
    assert "--egress" not in claude.context_args(A, 9, "KD-7")


@pytest.mark.parametrize(
    "over",
    [
        {"card": "", "egress": "probe"},
        {"egress": "Probe!"},
        {"egress": ""},
        {"egress": 1},
    ],
    ids=["chat", "bad-name", "empty", "not-a-string"],
)
def test_egress_is_refused_off_a_card_or_malformed(over):
    with pytest.raises(protocol.ProtocolError):
        _req(**over)


def test_context_args_refuse_an_exit_without_a_card():
    with pytest.raises(ValueError):
        claude.context_args(A, 9, "", egress="probe")


def _working(host):
    _, actions = host.deliver(_req(egress="probe"))
    host.runtime(A, SID)
    return actions


def test_a_lost_exit_closes_an_idle_process_and_the_next_starts_without_it():
    host = _host()
    _working(host)
    host.turn_ended(A, True)
    _, actions = host.deliver(
        _req(command="2" * 32)
    )  # the consent ended: no egress in this delivery
    assert any(isinstance(a, core.Close) for a in actions) and not _spawns(actions)
    actions = host.exited(A, 0)
    [spawn] = _spawns(actions)
    assert spawn.egress == "" and spawn.resume == SID


def test_a_lost_exit_closes_a_working_process_after_its_turn():
    host = _host()
    _working(host)
    _, actions = host.deliver(_req(command="2" * 32))
    assert not any(isinstance(a, (core.Close, core.Kill)) for a in actions)
    actions = host.turn_ended(A, True)
    assert any(isinstance(a, core.Close) for a in actions)


def test_the_same_exit_keeps_the_process():
    host = _host()
    _working(host)
    host.turn_ended(A, True)
    _, actions = host.deliver(_req(command="2" * 32, egress="probe"))
    assert not any(isinstance(a, core.Close) for a in actions)


def test_status_names_the_card_and_the_process_exit():
    host = _host()
    _working(host)
    [item] = host.status()["sessions"]
    assert item["card"] == "KD-7" and item["egress"] == "probe"


# --- revoke: the cabinet's sweep found the card no longer grants the process's exit -----------------------------


def test_revoke_cuts_a_working_turn_and_keeps_the_inputs_for_a_process_without_the_exit():
    # not a person's stop: the session lives on, the input that waits runs next without the exit
    host = _host()
    _working(host)
    host.deliver(
        _req(command="2" * 32, egress="probe")
    )  # an owner's message queued behind the turn
    actions = host.revoke(A, "probe")
    assert [type(a) for a in actions] == [core.Kill]
    actions = host.exited(A, -15)
    ended = [
        a.item
        for a in actions
        if isinstance(a, core.Report) and a.item.get("type") == "turn"
    ]
    assert ended == [
        {
            "type": "turn",
            "session": A,
            "command": "1" * 32,
            "phase": "ended",
            "ok": False,
            "error": core.EGRESS_ENDED,
        }
    ]
    [spawn] = _spawns(actions)
    assert spawn.egress == "" and spawn.resume == SID
    assert not [
        a
        for a in actions
        if isinstance(a, core.Report) and a.item.get("state") in ("stopped", "failed")
    ]


def test_revoke_closes_an_idle_process():
    host = _host()
    _working(host)
    host.turn_ended(A, True)
    assert [type(a) for a in host.revoke(A, "probe")] == [core.Close]
    actions = host.exited(A, 0)
    assert not _spawns(actions)
    assert [a.item["state"] for a in actions if isinstance(a, core.Report)] == [
        "exited"
    ]


def test_revoke_of_another_exit_or_an_unknown_session_does_nothing():
    host = _host()
    assert host.revoke(A, "probe") == []
    _working(host)
    assert host.revoke(A, "other") == []
    assert host.status()["sessions"][0]["egress"] == "probe"


# --- a session that waits for a slot holds its delivery's exit until it starts -------------------------

B = "b" * 32


def _waiting(host):
    """A holds the agent's only slot; B waits in the queue with the exit ``probe``."""
    _working(host)
    _, actions = host.deliver(_req(command="3" * 32, session=B, thread=10, card="KD-8", egress="probe"))
    assert not _spawns(actions)


def _item(host, session):
    [item] = [s for s in host.status()["sessions"] if s["session"] == session]
    return item


def test_status_names_the_exit_a_waiting_session_will_start_with():
    host = _host()
    _waiting(host)
    item = _item(host, B)
    assert item["state"] == "waiting" and item["egress"] == "probe" and item["card"] == "KD-8"


def test_revoke_clears_the_exit_of_a_waiting_session_and_it_starts_without_it():
    host = _host()
    _waiting(host)
    assert host.revoke(B, "probe") == []
    assert _item(host, B)["egress"] == ""
    host.turn_ended(A, True)
    host.stop(A)
    [spawn] = _spawns(host.exited(A, 0))
    assert spawn.session == B and spawn.egress == ""


def test_revoke_of_another_exit_keeps_the_waiting_session_exit():
    host = _host()
    _waiting(host)
    assert host.revoke(B, "other") == []
    assert _item(host, B)["egress"] == "probe"
    host.turn_ended(A, True)
    host.stop(A)
    [spawn] = _spawns(host.exited(A, 0))
    assert spawn.session == B and spawn.egress == "probe"


def test_revoke_is_a_protocol_op():
    request = protocol.parse(
        json.dumps({"op": "revoke", "session": A, "egress": "probe"}).encode() + b"\n"
    )
    assert request["egress"] == "probe"
    for bad in (
        {"op": "revoke", "session": A},
        {"op": "revoke", "session": A, "egress": "Probe!"},
    ):
        with pytest.raises(protocol.ProtocolError):
            protocol.parse(json.dumps(bad).encode() + b"\n")
