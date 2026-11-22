"""The session host runs a dev.check's root runner as ``digipaddock-operation-<id12>`` — no
pause, no drain, no grant. Its request is on disk before the unit starts; the runner's result is the final report;
a runner gone without one is ``host_lost``; a cancel stops the tests' unit, then the runner, and asks for the
contour's maintenance when a unit would not stop."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from kd.cabinet import devchecks as cabinet_devchecks
from kd.session_host import devcheck, protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_session_host_operation import CABINET_UID, Cabinet, Units, _server, _until  # noqa: E402

ID = "a" * 32
PARAMS = {"card": "KD-50", "project": "digipaddock", "repo": "acme/app", "pr": 7, "head": "b" * 40, "base_sha": "c" * 40, "target": "main",
          "profiles": ["unit"], "contour": "app-ci", "timeout_s": 1200, "set": {}}


def req(**over):
    value = {"op": "devcheck", "check": ID, "start": True, "timeout": 1800, "params": dict(PARAMS)}
    value.update(over)
    return value


def result(tmp_path, cls="passed", **extra):
    path = tmp_path / devcheck.DIR / f"{ID}.result.json"
    path.write_text(json.dumps({"check": ID, "class": cls, "profiles": [
        {"profile": "unit", "status": "success" if cls == "passed" else "failure", "counts": {"passed": 3},
         "duration_s": 12}], "log": f"devcheck/{ID}.log", **extra}), encoding="utf-8")


def launches(actions):
    return [a for a in actions if isinstance(a, devcheck.Launch)]


def reports(actions):
    return [a.item for a in actions if hasattr(a, "item")]


def test_units_and_argv():
    assert devcheck.unit(ID) == "digipaddock-operation-" + ID[:12]
    # infrastructure keeps the product's name: the protected units' digipaddock-* covers them
    assert devcheck.child_unit(ID) == "digipaddock-devcheck-" + ID[:12]
    assert devcheck.deps_unit(ID) == "digipaddock-devcheck-" + ID[:12] + "-deps"
    from kd import admin_operations
    assert admin_operations.protected_unit(devcheck.child_unit(ID) + ".service")
    assert devcheck.argv(ID, 1800) == ["systemd-run", "--unit", devcheck.unit(ID), "--collect", "--quiet",
                                       "-p", "RuntimeMaxSec=1800", devcheck.PYTHON, "-I", devcheck.RUNNER,
                                       "dev.check", ID]
    with pytest.raises(ValueError):
        devcheck.argv("../x", 1800)


def test_classes_are_the_cabinets():
    assert devcheck.CLASSES == cabinet_devchecks.CLASSES


def test_request_writes_the_request_before_the_launch(tmp_path, monkeypatch):
    checks = devcheck.Checks(str(tmp_path))
    seen = []
    real = devcheck.write_json
    monkeypatch.setattr(devcheck, "write_json", lambda path, value: (seen.append(path), real(path, value)))
    ack, actions = checks.request(req(), now=5000.0)
    assert ack == {"state": "running", "report": {"type": "devcheck", "check": ID, "state": "running"}}
    assert [a.argv for a in launches(actions)] == [devcheck.argv(ID, 1800)]
    assert seen == [str(tmp_path / devcheck.DIR / f"{ID}.request.json")]
    stored = json.loads((tmp_path / devcheck.DIR / f"{ID}.request.json").read_text(encoding="utf-8"))
    assert stored == {"check": ID, "params": PARAMS, "timeout": 1800, "started": 5000.0}
    assert checks.ids() == [ID]


def test_repeat_is_idempotent(tmp_path):
    checks = devcheck.Checks(str(tmp_path))
    first, _ = checks.request(req(), now=1.0)
    again, actions = checks.request(req(), now=2.0)
    polled, more = checks.request(req(start=False), now=3.0)
    assert again == first == polled and actions == [] and more == []


def test_poll_of_an_unknown_check_is_unknown(tmp_path):
    ack, actions = devcheck.Checks(str(tmp_path)).request(req(start=False), now=1.0)
    assert ack == {"state": "unknown"} and actions == []


def test_result_file_becomes_the_final_report(tmp_path):
    checks = devcheck.Checks(str(tmp_path))
    checks.request(req(), now=1.0)
    assert checks.tick(lambda unit: True) == []
    result(tmp_path, "checks_failed", error_short="FAILED test_x")
    final = reports(checks.tick(lambda unit: False))
    assert final == [{"type": "devcheck", "check": ID, "state": "failed", "class": "checks_failed",
                      "profiles": [{"profile": "unit", "status": "failure", "counts": {"passed": 3},
                                    "duration_s": 12}],
                      "log": f"devcheck/{ID}.log", "error_short": "FAILED test_x"}]
    assert checks.ids() == [] and checks.tick(lambda unit: False) == []
    ack, _ = checks.request(req(start=False), now=2.0)
    assert ack == {"state": "failed", "report": final[0]}


def test_an_invalid_result_is_no_result(tmp_path):
    checks = devcheck.Checks(str(tmp_path))
    checks.request(req(), now=1.0)
    (tmp_path / devcheck.DIR / f"{ID}.result.json").write_text(json.dumps({"check": ID, "class": "great"}))
    assert checks.tick(lambda unit: True) == []


def test_runner_gone_without_result_is_host_lost(tmp_path):
    checks = devcheck.Checks(str(tmp_path))
    checks.request(req(), now=1.0)
    actions = checks.tick(lambda unit: False)
    stops = [a for a in actions if isinstance(a, devcheck.Stop)]
    assert [s.units for s in stops] == [[devcheck.child_unit(ID), devcheck.deps_unit(ID), devcheck.unit(ID)]]
    assert checks.tick(lambda unit: False) == []  # its stop is under way
    final = reports(checks.stopped(ID, still=[], now=1.0 + devcheck.CRASH_S))
    assert final[0]["class"] == "host_lost" and final[0]["state"] == "failed" and "maintenance" not in final[0]


def test_a_runner_gone_soon_after_its_start_is_the_contours_maintenance(tmp_path):
    checks = devcheck.Checks(str(tmp_path))
    checks.request(req(), now=1.0)
    checks.tick(lambda unit: False)
    final = reports(checks.stopped(ID, still=[], now=1.0 + devcheck.CRASH_S - 1))[0]
    assert final["class"] == "host_lost" and "без результата" in final["maintenance"]


def test_restart_adopts_a_result_written_meanwhile(tmp_path):
    devcheck.Checks(str(tmp_path)).request(req(), now=1.0)
    result(tmp_path)
    checks = devcheck.Checks(str(tmp_path))
    ack, actions = checks.request(req(start=False), now=2.0)
    assert ack["state"] == "succeeded" and ack["report"]["class"] == "passed" and actions == []


def test_cancel_stops_the_child_then_the_runner(tmp_path):
    checks = devcheck.Checks(str(tmp_path))
    checks.request(req(), now=1.0)
    ack, actions = checks.cancel(ID, "card_left")
    assert ack == {"state": "stopping"}
    assert [a.units for a in actions] == [[devcheck.child_unit(ID), devcheck.deps_unit(ID), devcheck.unit(ID)]]
    assert checks.cancel(ID, "card_left") == ({"state": "stopping"}, [])  # one stop at a time
    stored = json.loads((tmp_path / devcheck.DIR / f"{ID}.request.json").read_text(encoding="utf-8"))
    assert stored["cancel"] == "card_left"
    final = reports(checks.stopped(ID, still=[]))
    assert final == [{"type": "devcheck", "check": ID, "state": "failed", "class": "cancelled", "profiles": [],
                      "log": "", "error_short": ""}]
    assert checks.cancel(ID, "card_left") == ({"state": "failed", "report": final[0]}, [])


def test_a_superseded_cancel_ends_superseded(tmp_path):
    checks = devcheck.Checks(str(tmp_path))
    checks.request(req(), now=1.0)
    checks.cancel(ID, "superseded")
    assert reports(checks.stopped(ID, still=[]))[0]["class"] == "superseded"


def test_the_runners_own_result_wins_over_the_cancel(tmp_path):
    checks = devcheck.Checks(str(tmp_path))
    checks.request(req(), now=1.0)
    checks.cancel(ID, "root")
    result(tmp_path)
    assert reports(checks.stopped(ID, still=[]))[0]["class"] == "passed"


def test_cancel_that_did_not_stop_asks_for_maintenance(tmp_path):
    checks = devcheck.Checks(str(tmp_path))
    checks.request(req(), now=1.0)
    checks.cancel(ID, "root")
    final = reports(checks.stopped(ID, still=[devcheck.child_unit(ID)]))[0]
    assert final["class"] == "cancelled" and devcheck.child_unit(ID) in final["maintenance"]


def test_a_cancel_of_an_unknown_check_is_unknown(tmp_path):
    assert devcheck.Checks(str(tmp_path)).cancel(ID, "root") == ({"state": "unknown"}, [])


def test_prune_removes_only_old_ended_checks(tmp_path):
    import os

    checks = devcheck.Checks(str(tmp_path))
    checks.request(req(), now=1.0)
    other = "f" * 32
    checks.request(req(check=other), now=1.0)
    result(tmp_path)
    old = tmp_path / devcheck.DIR / f"{ID}.result.json"
    os.utime(old, (1000.0, 1000.0))
    assert checks.prune(now=1000.0 + devcheck.KEEP_S - 1) == 0
    assert checks.prune(now=1000.0 + devcheck.KEEP_S + 1) == 1
    assert not old.exists() and not (tmp_path / devcheck.DIR / f"{ID}.request.json").exists()
    assert checks.ids() == [other]


def test_a_failed_launch_ends_the_check(tmp_path):
    checks = devcheck.Checks(str(tmp_path))
    checks.request(req(), now=1.0)
    final = reports(checks.launch_failed(ID))[0]
    assert final["class"] == "environment_not_ready" and "did not start" in final["error_short"]
    assert "раннер" in final["maintenance"]  # a host fault: every next check would fail the same way


# --- the protocol ---------------------------------------------------------------------------------
def test_devcheck_fields_are_exact():
    assert protocol.FIELDS["devcheck"] == {"op", "check", "start", "timeout", "params"}
    assert protocol.FIELDS["devcheck_cancel"] == {"op", "check", "reason"}
    line = json.dumps(req()).encode()
    assert protocol.parse(line)["check"] == ID
    assert protocol.parse(json.dumps({"op": "devcheck_cancel", "check": ID, "reason": "card_left"}).encode())


@pytest.mark.parametrize("over", [
    {"check": "x" * 32}, {"check": ID[:12]}, {"start": "yes"}, {"timeout": 5}, {"timeout": 99999}, {"extra": 1},
    {"params": {**PARAMS, "head": "HEAD"}}, {"params": {**PARAMS, "profiles": ["../x"]}},
    {"params": {**PARAMS, "repo": "a/../b"}}, {"params": {**PARAMS, "cmd": "rm"}},
    {"params": {**PARAMS, "set": {"x": "y"}}}, {"params": {**PARAMS, "cancel": "now"}}, {"params": "x"},
])
def test_check_request_checked(over):
    with pytest.raises(protocol.ProtocolError):
        protocol.parse(json.dumps(req(**over)).encode())


def test_check_id_is_hex32():
    for bad in ("A" * 32, "a" * 31, "../" + "a" * 29):
        with pytest.raises(protocol.ProtocolError):
            protocol.parse(json.dumps({"op": "devcheck_cancel", "check": bad, "reason": "root"}).encode())


# --- the server -----------------------------------------------------------------------------------
def test_server_runs_reports_and_cancels(tmp_path):
    cabinet, units, wall, stopped = Cabinet(), Units(), [5000.0], []

    async def scenario():
        host = _server(tmp_path, cabinet, units, wall)

        async def stop_unit(unit):
            stopped.append(unit)
            units.active.discard(unit)

        host.stop_unit = stop_unit
        await host.start()
        first = json.loads(await host.request(json.dumps(req()).encode(), CABINET_UID))
        status = json.loads(await host.request(json.dumps({"op": "status"}).encode(), 0))
        await host.devcheck_tick()
        cancel = json.dumps({"op": "devcheck_cancel", "check": ID, "reason": "root"}).encode()
        stopping = json.loads(await host.request(cancel, CABINET_UID))
        await _until(lambda: (tmp_path / "state" / devcheck.DIR / f"{ID}.result.json").exists())
        ended = json.loads(await host.request(cancel, CABINET_UID))
        await host.flush()
        await host.close()
        return first, status, stopping, ended

    first, status, stopping, ended = asyncio.run(scenario())
    assert first["state"] == "running" and units.launched == [devcheck.argv(ID, 1800)]
    assert [c["check"] for c in status["checks"]] == [ID]
    assert stopping["state"] == "stopping"
    assert stopped[:3] == [devcheck.child_unit(ID), devcheck.deps_unit(ID), devcheck.unit(ID)]
    assert ended["state"] == "failed" and ended["report"]["class"] == "cancelled"
    assert [i["class"] for i in cabinet.items("devcheck") if i["state"] == "failed"] == ["cancelled"]


def test_server_reports_a_runners_result_at_its_tick(tmp_path):
    cabinet, units, wall = Cabinet(), Units(), [5000.0]

    async def scenario():
        host = _server(tmp_path, cabinet, units, wall)
        await host.start()
        await host.request(json.dumps(req()).encode(), CABINET_UID)
        (tmp_path / "state" / devcheck.DIR / f"{ID}.result.json").write_text(json.dumps(
            {"check": ID, "class": "passed", "profiles": [], "log": f"devcheck/{ID}.log"}))
        units.active.discard(devcheck.unit(ID))
        await host.devcheck_tick()
        await host.flush()
        await host.close()

    asyncio.run(scenario())
    assert [i["class"] for i in cabinet.items("devcheck") if i["state"] != "running"] == ["passed"]
