"""``component.install`` on the session host — one batch with no roles: at once, or for ``drain: all`` (the
host's own restart) once every hosted role is paused and drained; the runner's ``component`` comes back in the
final report, every pause it took is lifted, and a host restarted by the batch adopts its result."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from kd.session_host import operation, protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_session_host_operation import (  # noqa: E402
    CABINET_UID, OP, REV, Cabinet, Units, _deliver, _final, _line, _server, _until)

RAN = {"code": 0, "tail": "ok", "check": 0, "check_tail": "ok"}


def _component(**over):
    fields = {"op": "operation", "operation": OP, "kind": "component.install", "agents": [], "revision": REV,
              "timeout": 1800, "start": True, "component": "repo-attach", "mode": "install", "drain": ""}
    fields.update(over)
    return _line(**fields)


def _batch(tmp_path, state="succeeded", reason="", component=RAN):
    path = tmp_path / "state" / "operations" / f"{OP}.batch1.json"
    path.write_text(json.dumps({"operation": OP, "batch": 1, "state": state, "reason": reason, "agents": {},
                                "component": component}))


def test_component_request_parses():
    req = protocol.parse(_component())
    assert (req["component"], req["mode"], req["drain"]) == ("repo-attach", "install", "")


@pytest.mark.parametrize("over", [
    {"agents": ["dev"]}, {"component": "../x"}, {"mode": "install --force"}, {"drain": "some"},
    {"component": 1}, {"command": "id"},
])
def test_component_request_checked(over):
    with pytest.raises(protocol.ProtocolError):
        protocol.parse(_component(**over))


def test_a_reinstall_takes_no_component_fields():
    fields = {"op": "operation", "operation": OP, "kind": "agent.reinstall", "agents": ["dev"], "revision": REV,
              "timeout": 1800, "start": True, "component": "repo-attach"}
    with pytest.raises(protocol.ProtocolError):
        protocol.parse(_line(**fields))
    bare = {k: v for k, v in fields.items() if k != "component"}
    with pytest.raises(protocol.ProtocolError):  # a component without its component, mode and drain
        protocol.parse(_line(**{**bare, "kind": "component.install", "agents": []}))


def test_a_component_runs_at_once_and_pauses_nobody(tmp_path):
    cabinet, units, wall = Cabinet(), Units(), [5000.0]

    async def scenario():
        host = _server(tmp_path, cabinet, units, wall)
        await host.start()
        await host.request(_deliver("a" * 32), CABINET_UID)
        await _until(lambda: host.procs)  # dev works: a component without drain does not wait for it
        first = json.loads(await host.request(_component(), CABINET_UID))
        paused = dict(host.core.status()["paused"])
        await host.operation_tick()
        _batch(tmp_path)
        units.active.clear()
        await host.operation_tick()
        await _until(lambda: _final(cabinet))
        await host.close()
        return first, paused

    first, paused = asyncio.run(scenario())
    assert first["state"] == "running" and paused == {}
    assert units.launched == [operation.argv(OP, 1)] and cabinet.authorized == [(OP, [])]
    [final] = _final(cabinet)
    assert final["state"] == "succeeded" and final["component"] == RAN and final["kept"] == []
    request = json.loads((tmp_path / "state" / "operations" / f"{OP}.request.json").read_text())
    assert (request["component"], request["mode"], request["agents"]) == ("repo-attach", "install", [])


def test_the_host_restart_waits_for_every_role_and_survives_itself(tmp_path):
    """drain all: every hosted role is paused, the restart starts only when all are drained; the host it restarts
    keeps the pauses, adopts the batch's result and lifts them."""
    cabinet, units, wall = Cabinet(), Units(), [5000.0]
    seen = {}

    async def first():
        host = _server(tmp_path, cabinet, units, wall)
        await host.start()
        await host.request(_deliver("a" * 32), CABINET_UID)
        await _until(lambda: host.procs)  # dev's turn
        await host.request(_component(component="session-host", mode="restart", drain="all"), CABINET_UID)
        seen["paused"] = dict(host.core.status()["paused"])
        await host.operation_tick()
        seen["while_busy"] = list(units.launched)
        host.procs["a" * 32].process.kill()  # dev's turn ends
        await _until(lambda: host.core.drained("dev"))
        await host.operation_tick()
        seen["launched"] = list(units.launched)
        seen["urgent_passes"] = host.ops.drains("dev")
        await host.close()  # the runner restarts the host

    async def second():
        host = _server(tmp_path, cabinet, units, wall)
        await host.start()
        seen["after_restart"] = dict(host.core.status()["paused"])
        _batch(tmp_path)
        units.active.clear()
        await host.operation_tick()
        await _until(lambda: _final(cabinet))
        seen["end"] = dict(host.core.status()["paused"])
        await host.close()

    asyncio.run(first())
    asyncio.run(second())
    assert seen["paused"] == {"analyst": OP, "dev": OP}
    assert seen["while_busy"] == [] and seen["launched"] == [operation.argv(OP, 1)]
    assert seen["urgent_passes"] is False  # nothing starts while the restart runs
    assert seen["after_restart"] == {"analyst": OP, "dev": OP}
    assert seen["end"] == {}
    [final] = _final(cabinet)
    assert final["state"] == "succeeded" and final["agents"] == {} and final["kept"] == []


def test_the_restart_never_lifts_a_role_another_operation_keeps(tmp_path):
    """Review I1: analyst is kept paused after a failed reinstall — the restart neither takes its pause over nor
    lifts it at its end."""
    cabinet, units, wall = Cabinet(), Units(), [5000.0]
    held = "d" * 32

    async def scenario():
        host = _server(tmp_path, cabinet, units, wall)
        await host.start()
        host.core.pause("analyst", held)
        host.ops.save_paused()
        await host.request(_component(component="session-host", mode="restart", drain="all"), CABINET_UID)
        during = dict(host.core.status()["paused"])
        await host.operation_tick()
        _batch(tmp_path)
        units.active.clear()
        await host.operation_tick()
        await _until(lambda: _final(cabinet))
        end = dict(host.core.status()["paused"])
        await host.close()
        return during, end

    during, end = asyncio.run(scenario())
    assert during == {"analyst": held, "dev": OP}
    assert end == {"analyst": held}
    request = json.loads((tmp_path / "state" / "operations" / f"{OP}.request.json").read_text())
    assert request["agents"] == ["dev"]


def test_a_drain_past_the_timeout_restarts_nothing(tmp_path):
    cabinet, units, wall = Cabinet(), Units(), [5000.0]

    async def scenario():
        host = _server(tmp_path, cabinet, units, wall)
        await host.start()
        await host.request(_deliver("a" * 32), CABINET_UID)
        await _until(lambda: host.procs)
        await host.request(_component(component="session-host", mode="restart", drain="all", timeout=60),
                           CABINET_UID)
        await host.operation_tick()
        wall[0] += 61
        await host.operation_tick()
        await _until(lambda: _final(cabinet))
        paused = host.core.status()["paused"]
        await host.close()
        return paused

    assert asyncio.run(scenario()) == {}
    assert units.launched == [] and _final(cabinet)[0]["reason"] == "drain_timeout"


def test_a_failed_component_lifts_the_pauses_too(tmp_path):
    cabinet, units, wall = Cabinet(), Units(), [5000.0]

    async def scenario():
        host = _server(tmp_path, cabinet, units, wall)
        await host.start()
        await host.request(_component(component="session-host", mode="restart", drain="all"), CABINET_UID)
        await host.operation_tick()
        _batch(tmp_path, state="failed", reason="check_failed", component={**RAN, "check": 1})
        units.active.clear()
        await host.operation_tick()
        await _until(lambda: _final(cabinet))
        paused = host.core.status()["paused"]
        await host.close()
        return paused

    assert asyncio.run(scenario()) == {}
    [final] = _final(cabinet)
    assert final["reason"] == "check_failed" and final["component"]["check"] == 1 and final["kept"] == []


def test_a_lost_runner_fails_without_holding_roles(tmp_path):
    cabinet, units, wall = Cabinet(), Units(), [5000.0]

    async def scenario():
        host = _server(tmp_path, cabinet, units, wall)
        await host.start()
        await host.request(_component(), CABINET_UID)
        await host.operation_tick()
        units.active.clear()  # the unit ended without a result
        await host.operation_tick()
        await _until(lambda: _final(cabinet))
        await host.close()

    asyncio.run(scenario())
    [final] = _final(cabinet)
    assert final["state"] == "failed" and final["reason"] == operation.RUNNER_LOST and final["kept"] == []
