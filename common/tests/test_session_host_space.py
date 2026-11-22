"""A delivery to an agent's "Monitor" thread names its space; the host launches that session with it."""

import json

import pytest

from kd import session_config
from kd.session_host import core, protocol

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
