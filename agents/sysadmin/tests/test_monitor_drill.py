"""the owner's test critical alert — the scheduled tick raises it as P-n without waking the sysadmin,
then closes it the usual way and reports the test done."""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from sysadmin.monitor import deliver, drill
from sysadmin.monitor.model import CheckResult, Observation, load_config
from sysadmin.monitor.registry import CLOSE_AFTER, Registry

from kd.cabinet import api  # noqa: E402 — common/ is on the path once the monitor is imported

UTC3 = timezone(timedelta(hours=3))
CONFIG = Path(__file__).resolve().parent.parent / "monitor" / "config.example.json"  # config.json is the overlay's
DAY = datetime(2026, 10, 7, 14, 0, tzinfo=UTC3)


@pytest.fixture
def cfg():
    return load_config(CONFIG)


@pytest.fixture
def sp(tmp_path):
    reg = Registry.load(tmp_path / "state.json", date(2026, 10, 7), local_ids=False)
    reg.state["cabinet_imported"] = DAY.isoformat()
    reg.save(tmp_path / "state.json")
    return tmp_path / "state.json"


class Wake:
    def __init__(self):
        self.texts = []

    def __call__(self, agent, text, *, urgent=False, space="chat"):
        self.texts.append(text)
        return "sent"


def press(cab):
    env = cab.env
    if env.users.get("owner") is None:
        env.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    return api.dispatch(env.ctx, api.Principal("user", 0, "owner"), {"op": "monitor.test.request"})["test"]


def calm(monkeypatch):
    fine = [CheckResult("app1", "mem", True, [Observation("app1", "mem", "ram", "ok", "40%", "ram: 40%")])]
    monkeypatch.setattr(deliver, "collect", lambda cfg, now, st, deadline, **kw: list(fine))


def run(cfg, sp, at, wake, **kw):
    return deliver.tick(cfg, sp, at, wake_fn=wake, clock=lambda: 0.0, agent="sysadmin", **kw)


def test_the_test_alert_opens_a_crit_incident_wakes_nobody_and_closes_by_itself(cfg, sp, monitor_cabinet,
                                                                                monkeypatch):
    cab = monitor_cabinet
    calm(monkeypatch)
    test = press(cab)
    wake = Wake()
    assert run(cfg, sp, DAY, wake) == 0
    got = cab.call({"op": "incident.list"})["items"]
    assert [(i["server"], i["check"], i["level"], i["status"]) for i in got] == [("test", "alert", "crit", "open")]
    assert got[0]["value"] == drill.VALUE_CRIT
    opened = [e for e in cab.env.store.events_since(0) if e["kind"] == "incident.opened"]
    assert opened and opened[0]["payload"]["level"] == "crit"
    st = json.loads(sp.read_text(encoding="utf-8"))
    assert wake.texts == [] and not [e for e in st["outbox"] if e["key"] == drill.KEY]
    assert "test" not in {s["alias"] for s in cab.call({"op": "server.list"})["items"]}  # no server row
    assert cab.call({"op": "monitor.test.get"})["test"]["state"] == "running"
    for n in range(1, CLOSE_AFTER + 1):
        assert run(cfg, sp, DAY + timedelta(minutes=5 * n), wake) == 0
    got = cab.call({"op": "incident.list", "status": "resolved"})["items"]
    assert [(i["server"], i["status"]) for i in got] == [("test", "resolved")]
    done = cab.call({"op": "monitor.test.get"})["test"]
    assert done["id"] == test["id"] and done["state"] == "done"
    st = json.loads(sp.read_text(encoding="utf-8"))
    assert drill.STATE not in st and wake.texts == []


def test_the_morning_summary_and_the_list_leave_the_test_out(cfg, sp, tmp_path, capsys):
    reg = Registry.load(sp, date(2026, 10, 7), local_ids=False)
    reg.observe([CheckResult("app1", "disk", True, [Observation("app1", "disk", "/", "crit", "96%", "/: 96%")]),
                 drill.result(reg.state, {"id": "t1"}, DAY)], DAY)
    for p in reg.state["problems"].values():
        p["id"] = "P-1" if p["key"] == "app1:disk:/" else "P-2"
    reg.state["outbox"], reg.state["last_morning"] = [], "2026-10-06"
    reg.save(sp)
    monday = datetime(2026, 10, 12, 9, 5, tzinfo=UTC3)
    send, morning = deliver.plan(reg.state, [], monday, cfg)
    assert morning == "week" and [e["problem_id"] for e in send if e.get("kind") == "still"] == ["P-1"]
    cp = tmp_path / "config.json"
    cp.write_text(json.dumps(cfg), encoding="utf-8")
    assert deliver.main(["list"], env={"KD_MONITOR_STATE": str(sp), "KD_MONITOR_CONFIG": str(cp)}, now=DAY) == 0
    out = capsys.readouterr().out
    assert "P-1" in out and "P-2" not in out


def test_an_unwritten_done_is_retried_on_the_next_tick(cfg, sp, monitor_cabinet, monkeypatch):
    cab = monitor_cabinet
    calm(monkeypatch)
    test = press(cab)
    wake = Wake()
    for n in range(CLOSE_AFTER):
        run(cfg, sp, DAY + timedelta(minutes=5 * n), wake)
    real = cab.call

    def no_done(request):
        if request["op"] == "monitor.test.done":
            from kd.cabinet import client as cabinet_client

            raise cabinet_client.CabinetError("unavailable", "restarting")
        return real(request)

    monkeypatch.setattr(cab, "call", no_done)
    run(cfg, sp, DAY + timedelta(minutes=5 * CLOSE_AFTER), wake)  # the close goes out, the done does not
    assert cab.env.client("digi-sysadmin").call({"op": "monitor.test.get"})["test"]["state"] == "running"
    assert json.loads(sp.read_text(encoding="utf-8"))[drill.STATE]["id"] == test["id"]
    monkeypatch.setattr(cab, "call", real)
    run(cfg, sp, DAY + timedelta(minutes=5 * (CLOSE_AFTER + 1)), wake)
    assert cab.call({"op": "monitor.test.get"})["test"]["state"] == "done"
    assert drill.STATE not in json.loads(sp.read_text(encoding="utf-8"))
    assert len(cab.call({"op": "incident.list", "status": "resolved"})["items"]) == 1 and wake.texts == []


def test_a_new_test_starts_over_a_stale_one(cfg, sp, monitor_cabinet, monkeypatch):
    state = {"problems": {}, drill.STATE: {"id": "old", "phase": "ok", "at": DAY.isoformat()}}
    got = drill.result(state, {"id": "new"}, DAY)
    assert got.observations[0].level == "crit" and state[drill.STATE]["id"] == "new"
    assert drill.result(state, None, DAY).observations[0].level == "ok"


def test_a_failed_take_is_a_line_not_a_failed_tick(cfg, sp, monitor_cabinet, monkeypatch, capsys):
    class Down:
        def call(self, request):
            from kd.cabinet import client as cabinet_client

            raise cabinet_client.CabinetError("unavailable", "down")

    assert drill.take(Down()) is None
    drill.report(Down(), "x")
    err = capsys.readouterr().err
    assert "заявка тестового алерта не прочитана" in err and "итог тестового алерта не записан" in err


def test_a_partial_tick_leaves_the_test_for_the_scheduled_one(cfg, sp, monitor_cabinet, monkeypatch):
    cab = monitor_cabinet
    calm(monkeypatch)
    monkeypatch.setattr(deliver, "_known_servers", lambda: ["app1"])
    press(cab)
    assert run(cfg, sp, DAY, Wake(), servers=["app1"], partial=True) == 0
    assert cab.call({"op": "monitor.test.get"})["test"]["state"] == "queued"
    assert cab.call({"op": "incident.list"})["items"] == []


def test_the_cabinet_down_keeps_the_tick_and_the_test_waits(cfg, sp, monitor_cabinet, monkeypatch):
    cab = monitor_cabinet
    calm(monkeypatch)
    press(cab)
    cab.up = False
    run(cfg, sp, DAY, Wake())
    assert "monitor.test.take" not in cab.calls  # the cabinet did not answer the acks: not asked again
    cab.up = True
    assert cab.call({"op": "monitor.test.get"})["test"]["state"] == "queued"
    assert drill.STATE not in json.loads(sp.read_text(encoding="utf-8"))
