"""a card's delivery may name its project's memory bank; the host launches the card's session with it."""

import json

import pytest

from kd import session_config
from kd.session_host import claude, core, protocol

A = "a" * 32
BANK = "coding-agent::_Shop.v2"


def _line(**over):
    fields = {"op": "deliver", "command": "1" * 32, "session": A, "agent": "dev", "thread": 9, "card": "KD-7",
              "text": "[board] KD-7 assigned to you", "resume": ""}
    fields.update(over)
    return json.dumps(fields).encode() + b"\n"


def _host():
    return core.Host(session_config.HostConfig({"dev": 1}, 600), {"dev": "digi-dev"}, lambda: 1000.0)


def test_protocol_accepts_a_cards_bank_and_its_absence():
    assert protocol.parse(_line(memory=BANK))["memory"] == BANK
    assert "memory" not in protocol.parse(_line())


@pytest.mark.parametrize("over", [{"memory": ""}, {"memory": "a/b"}, {"memory": ".x"}, {"memory": "x" * 101},
                                  {"memory": 7}, {"memory": BANK, "card": ""}], ids=repr)
def test_protocol_refuses_a_bad_bank_or_one_without_a_card(over):
    with pytest.raises(protocol.ProtocolError):
        protocol.parse(_line(**over))


def test_the_cards_session_spawns_with_its_bank():
    _, actions = _host().deliver(json.loads(_line(memory=BANK)))
    [spawn] = [a for a in actions if isinstance(a, core.Spawn)]
    assert (spawn.card, spawn.memory) == ("KD-7", BANK)
    _, actions = _host().deliver(json.loads(_line()))
    assert [a.memory for a in actions if isinstance(a, core.Spawn)] == [""]


def test_a_new_bank_holds_for_the_next_process_of_the_session():
    """A live process keeps the bank it started with; the next one (resume) takes the bank of the latest delivery."""
    host = _host()
    host.deliver(json.loads(_line(memory=BANK)))
    host.deliver(json.loads(_line(command="2" * 32, memory="coding-agent::other")))
    assert host._sessions[A].memory == "coding-agent::other"
    host.deliver(json.loads(_line(command="3" * 32)))  # unbound meanwhile: the role bank next time
    assert host._sessions[A].memory == ""


def test_the_launch_names_the_bank_after_the_cards_context():
    context = claude.context_args(A, 7, "KD-7", memory=BANK)
    assert context == ["--context", A, "7", "KD-7", "--memory", BANK]
    assert claude.context_args(A, 7, "KD-7") == ["--context", A, "7", "KD-7"]
    for bad in ((A, 7, "", "", BANK), (A, 7, "", "monitor", BANK), (A, 7, "KD-7", "", "a b"),
                (A, 7, "KD-7", "", "--x")):
        with pytest.raises(ValueError):
            claude.context_args(*bad)
