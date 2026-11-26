"""A delivery to an agent's "Monitor" thread names its space; the host launches that session with it."""

import json

import pytest

from kd import session_config
from kd.session_host import claude, core, protocol

A = "a" * 32


def _line(**over):
    fields = {"op": "deliver", "command": "1" * 32, "session": A, "agent": "sysadmin", "thread": 9, "card": "",
              "text": "[монитор] P-7", "resume": ""}
    fields.update(over)
    return json.dumps(fields).encode() + b"\n"


def test_protocol_accepts_the_monitor_space_and_its_absence():
    assert protocol.parse(_line(space="monitor"))["space"] == "monitor"
    assert "space" not in protocol.parse(_line())


@pytest.mark.parametrize("over", [{"space": "chat"}, {"space": ""}, {"space": 1}, {"space": "monitor", "card": "KD-1"}],
                         ids=repr)
def test_protocol_refuses_any_other_space(over):
    with pytest.raises(protocol.ProtocolError):
        protocol.parse(_line(**over))


def test_the_monitor_session_spawns_with_its_space():
    host = core.Host(session_config.HostConfig({"sysadmin": 1}, 600), {"sysadmin": "digi-sysadmin"}, lambda: 1000.0)
    _, actions = host.deliver(json.loads(_line(space="monitor")))
    [spawn] = [a for a in actions if isinstance(a, core.Spawn)]
    assert (spawn.thread, spawn.card, spawn.space) == (9, "", "monitor")
    host2 = core.Host(session_config.HostConfig({"sysadmin": 1}, 600), {"sysadmin": "digi-sysadmin"}, lambda: 1000.0)
    _, actions = host2.deliver(json.loads(_line()))
    assert [a.space for a in actions if isinstance(a, core.Spawn)] == [""]


SID = "0b9c3f1e-1111-2222-3333-444455556666"
NEW = "7d2e4a10-aaaa-bbbb-cccc-ddddeeeeffff"
B, C = "b" * 32, "c" * 32


class _Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def _launch(spawn):
    return claude.argv(spawn.agent, spawn.user, spawn.unit, spawn.resume,
                       claude.context_args(spawn.session, spawn.thread, spawn.card, spawn.space))


def _spawns(actions):
    return [a for a in actions if isinstance(a, core.Spawn)]


def test_each_monitor_wake_starts_a_new_conversation_while_chat_and_card_resume():
    """KD-9459: the cabinet sends no resume id for a "Monitor" delivery, so a wake after its process closed starts
    claude without --resume; the chat and a card resume theirs. A wake that comes while the process lives goes into
    it."""
    clock = _Clock()
    host = core.Host(session_config.HostConfig({"sysadmin": 3}, 600), {"sysadmin": "digi-sysadmin"}, clock)
    _, actions = host.deliver(json.loads(_line(space="monitor")))
    [first] = _spawns(actions)
    assert first.resume == "" and _launch(first)[-1] == "monitor"
    host.runtime(A, SID)  # the process reported its Claude session
    host.output(A)
    host.turn_ended(A, ok=True)
    # a wake while the process lives: written into it, no new process
    _, actions = host.deliver(json.loads(_line(space="monitor", command="2" * 32)))
    assert _spawns(actions) == [] and [type(a).__name__ for a in actions] == ["Report", "Write"]
    host.output(A)
    host.turn_ended(A, ok=True)
    clock.t += 600
    assert [type(a).__name__ for a in host.tick()] == ["Close"]  # idle past the limit
    host.exited(A, 0)  # no input left: the host forgets the session
    _, actions = host.deliver(json.loads(_line(space="monitor", command="3" * 32)))
    [again] = _spawns(actions)
    assert again.resume == "" and SID not in _launch(again)
    # the chat and a card: the cabinet sends their ids, the launch resumes them
    _, actions = host.deliver(json.loads(_line(session=B, thread=10, command="4" * 32, text="[chat] …",
                                               resume=SID)))
    [chat] = _spawns(actions)
    assert chat.resume == SID and _launch(chat)[-2:] == ["chat", SID]
    _, actions = host.deliver(json.loads(_line(session=C, thread=11, card="KD-1", command="5" * 32,
                                               text="[board] KD-1", resume=NEW)))
    [card] = _spawns(actions)
    assert card.resume == NEW and _launch(card)[-2:] == ["KD-1", NEW]
