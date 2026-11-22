"""The "Check now" button — a tick of some servers that changes nothing of the rest, and the timer's command."""
import contextlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from sysadmin.monitor import cabinet as mcab
from sysadmin.monitor import deliver
from sysadmin.monitor.baseline import Baseline
from sysadmin.monitor.model import CheckResult, Observation, load_config
from sysadmin.monitor.registry import Registry

from kd.cabinet import api  # noqa: E402 — common/ is on the path once the monitor is imported

UTC3 = timezone(timedelta(hours=3))
CONFIG = Path(__file__).resolve().parent / "fixtures" / "monitor-config.json"
DAY = datetime(2026, 10, 7, 14, 0, tzinfo=UTC3)


@pytest.fixture
def cfg():
    return load_config(CONFIG)


@pytest.fixture
def sp(tmp_path):
    return tmp_path / "state.json"


@pytest.fixture
def cab(monitor_cabinet):
    return monitor_cabinet


class Wake:
    def __init__(self):
        self.texts = []

    def __call__(self, agent, text, *, urgent=False, space="chat"):
        self.texts.append(text)
        return "sent"


def obs(server, check="disk", level="crit", value="96%", subject="/"):
    return Observation(server, check, subject, level, value, f"{subject}: {value}")


def seed(sp, last_morning="2026-10-07"):
    """An open crit on example-prod and a warn on node-c, both numbered by the cabinet."""
    reg = Registry.load(sp, date(2026, 10, 7), local_ids=False)
    reg.observe([CheckResult("example-prod", "disk", True, [obs("example-prod")]),
                 CheckResult("node-c", "mem", True, [obs("node-c", "mem", "warn", "91%", "ram")])], DAY - timedelta(days=1))
    reg.state["outbox"], reg.state["last_morning"] = [], last_morning
    reg.state["cabinet_imported"] = DAY.isoformat()
    reg.save(sp)
    return reg


def collected(results, seen=None):
    def collect(cfg, now, st, deadline, **kw):
        if seen is not None:
            seen.append(kw)
        return [r for r in results if kw.get("servers") is None or r.server in kw["servers"]]
    return collect


def owner(cab):
    env = cab.env
    if env.users.get("owner") is None:
        env.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    return api.Principal("user", 0, "owner")


def servers_in(cab, *aliases):
    cab.call({"op": "monitor.push", "items": [{"key": "seed-" + "-".join(aliases), "kind": "sync",
                                               "at": DAY.isoformat(), "checks": [], "changes": [],
                                               "servers": [{"alias": a, "host": "", "unreachable_count": 0}
                                                           for a in aliases]}]})


def press(cab, alias):
    return api.dispatch(cab.env.ctx, owner(cab), {"op": "server.check.request", "alias": alias})["request"]


# --- AC5: a tick of one server --------------------------------------------------------------------
def test_a_partial_tick_changes_nothing_of_the_other_servers_and_skips_the_morning(cfg, sp, cab, monkeypatch):
    servers_in(cab, "node-c", "example-prod")
    seed(sp, last_morning="2026-10-06")  # the morning is due at 14:00
    seen = []
    # the poll sees example-prod fine now, but the tick is node-c's only: prod's crit must stay open
    monkeypatch.setattr(deliver, "collect", collected([
        CheckResult("node-c", "mem", True, [obs("node-c", "mem", "ok", "40%", "ram")]),
        CheckResult("example-prod", "disk", True, [obs("example-prod", level="ok", value="50%")])], seen))
    monkeypatch.setattr(deliver, "_known_servers", lambda: ["node-c", "example-prod", "node-d"])
    wake = Wake()
    assert deliver.tick(cfg, sp, DAY, wake_fn=wake, clock=lambda: 0.0, servers=["node-c"], agent="sysadmin",
                        partial=True) == 0
    assert seen[0]["servers"] == ["node-c"] and seen[0]["aliases"] == ["node-c", "example-prod", "node-d"]
    st = json.loads(sp.read_text(encoding="utf-8"))
    assert "example-prod:disk:/" in st["problems"] and st["last_morning"] == "2026-10-06"
    assert not any("сводка за неделю" in t for t in wake.texts)
    listed = {s["alias"]: s["enabled"] for s in cab.call({"op": "server.list"})["items"]}
    assert listed == {"node-c": 1, "example-prod": 1}
    assert "node-c:mem:ram" in st["problems"]  # node-c's own problem moves by node-c's own results only (ok streak)


def test_a_partial_sync_lists_only_its_servers(cfg):
    st = {"unreachable": {"node-c": 0, "example-prod": 0, "node-d": 2}}
    item = mcab.sync_item([], st, {"node-c"}, {}, DAY, partial=True)
    assert item["partial"] is True and [s["alias"] for s in item["servers"]] == ["node-c"]
    full = mcab.sync_item([], st, {"node-c"}, {}, DAY)
    assert "partial" not in full and [s["alias"] for s in full["servers"]] == ["example-prod", "node-c", "node-d"]


def test_a_queued_partial_sync_keeps_the_full_one_it_merges_with(cfg):
    state = {}
    full = {"key": "f", "kind": "sync", "at": "a", "changes": [{"c": 1}],
            "servers": [{"alias": "node-c"}, {"alias": "node-d"}],
            "checks": [{"server": "node-c", "check": "disk", "rows": [1]}, {"server": "node-d", "check": "disk",
                                                                        "rows": [2]}],
            "trust": {"trusted": {"a": "x"}, "servers": {"node-c": {"ports": {}}, "node-d": {"ports": {}}}}}
    part = {"key": "p", "kind": "sync", "at": "b", "partial": True, "changes": [{"c": 2}],
            "servers": [{"alias": "node-c", "unreachable_count": 1}],
            "checks": [{"server": "node-c", "check": "disk", "rows": [3]}],
            "trust": {"trusted": {"a": "y"}, "servers": {"node-c": {"ports": {"tcp:22": "sshd"}}}}}
    mcab.enqueue(state, [full], DAY)
    mcab.enqueue(state, [part], DAY)
    (merged,) = state["cabinet_queue"]
    assert "partial" not in merged and merged["key"] == "p"
    assert merged["servers"] == [{"alias": "node-c", "unreachable_count": 1}, {"alias": "node-d"}]
    assert {(c["server"], c["rows"][0]) for c in merged["checks"]} == {("node-c", 3), ("node-d", 2)}
    assert [c["c"] for c in merged["changes"]] == [1, 2]
    assert merged["trust"] == {"trusted": {"a": "y"}, "servers": {"node-c": {"ports": {"tcp:22": "sshd"}},
                                                                  "node-d": {"ports": {}}}}
    mcab.enqueue(state, [dict(part, key="p2")], DAY)  # partial over the merged one: still the full list
    assert [s["alias"] for s in state["cabinet_queue"][0]["servers"]] == ["node-c", "node-d"]


def test_the_trusted_lists_ride_in_the_sync(cfg, sp, cab, monkeypatch):
    servers_in(cab, "node-c")
    seed(sp)
    base = Baseline.empty()
    base.trust("10.1.0.0/16", "офис")
    base.set_server("node-c", Baseline.make_server({"SHA256:k (ED25519)": "owner"}, {"tcp:22": "sshd"}, [], {}, DAY))
    base.save(sp.parent / "baseline.json")
    monkeypatch.setattr(deliver, "collect", collected([CheckResult("node-c", "mem", True, [])]))
    assert deliver.tick(cfg, sp, DAY, wake_fn=Wake(), clock=lambda: 0.0, servers=["node-c"], agent="sysadmin") == 0
    trust = cab.call({"op": "server.get", "alias": "node-c"})["trust"]
    assert {r["item"] for r in trust["trusted"]} >= {"10.1.0.0/16"}
    assert trust["identities"] == [{"item": "SHA256:k (ED25519)", "label": "owner"}]
    assert trust["taken_at"] == "2026-10-07T11:00:00Z"  # when the baseline was taken
    assert mcab.trust_of(Baseline({}, broken=True), {"node-c"}) is None


def test_a_partial_tick_leaves_the_kd_alert_fallback_to_the_scheduled_tick(cfg, sp, cab, monkeypatch, capsys):
    # kd-alert reads digipaddock-monitor.service's journal: a check-now must not mark a 🔴 as sent to it, nor "accepted"
    reg = Registry.load(sp, date(2026, 10, 7), local_ids=False)
    reg.state["cabinet_imported"] = DAY.isoformat()
    reg.state["outbox"] = [{"kind": "open", "problem_id": "P-1", "key": "example-prod:disk:/", "level": "crit",
                            "server": "example-prod", "check": "disk", "subject": "/", "value": "96%", "text": "t",
                            "opened_at": (DAY - timedelta(hours=1)).isoformat(),
                            "at": (DAY - timedelta(hours=1)).isoformat(), "repeats": 0}]
    reg.state.setdefault("sec", {})["accepted"] = [{"at": DAY.isoformat(), "text": "P-2: принят порт"}]
    reg.save(sp)
    monkeypatch.setattr(deliver, "collect", collected([]))
    monkeypatch.setattr(deliver, "_known_servers", lambda: ["node-c"])
    undelivered = lambda agent, text, urgent=False: "error"  # noqa: E731 — the wake does not get through
    assert deliver.tick(cfg, sp, DAY, wake_fn=undelivered, clock=lambda: 0.0, servers=["node-c"], agent="sysadmin",
                        partial=True) == 0
    st = json.loads(sp.read_text(encoding="utf-8"))
    assert not st["outbox"][0].get("fallback_sent") and not st["sec"]["accepted"][0].get("pult")
    assert "kd-monitor: example-prod" not in capsys.readouterr().out
    assert deliver.tick(cfg, sp, DAY, wake_fn=undelivered, clock=lambda: 0.0, agent="sysadmin") == 3
    out = capsys.readouterr().out
    assert "kd-monitor: example-prod · диск /: 96%" in out and "kd-monitor: принято: P-2: принят порт" in out


# --- AC5: the scheduled tick is not lost ------------------------------------------------------------
def test_the_tick_waits_for_a_running_tick_instead_of_skipping_it(cfg, sp, cab, monkeypatch, capsys):
    seed(sp)
    waits, orig = [], deliver._lock

    @contextlib.contextmanager
    def spy(path, wait_s):
        waits.append((path.name, wait_s))
        with orig(path, wait_s) as got:
            yield got
    monkeypatch.setattr(deliver, "_lock", spy)
    monkeypatch.setattr(deliver, "collect", collected([]))
    assert deliver.tick(cfg, sp, DAY, wake_fn=Wake(), clock=lambda: 0.0, agent="sysadmin") == 0
    # not the budget alone: a running tick may outlive it by one ssh call, then the cabinet and the wake
    assert waits[0] == ("tick.lock", cfg["tick_budget_s"] + cfg["call_timeout_s"] + 60)

    @contextlib.contextmanager
    def busy(path, wait_s):
        yield path.name != "tick.lock"
    monkeypatch.setattr(deliver, "_lock", busy)
    assert deliver.tick(cfg, sp, DAY, wake_fn=Wake(), clock=lambda: 0.0, agent="sysadmin") == 1
    assert "tick.lock занят дольше бюджета" in capsys.readouterr().err


def test_a_tick_that_waited_for_the_lock_polls_at_its_own_time(cfg, sp, cab, monkeypatch):
    seed(sp)
    times, ticks = [], iter([0.0, 40.0])
    monkeypatch.setattr(deliver, "collect", lambda cfg, now, st, deadline, **kw: times.append(now) or [])
    clock = lambda: next(ticks, 40.0)  # noqa: E731 — 40 s pass while waiting for tick.lock
    assert deliver.tick(cfg, sp, DAY, wake_fn=Wake(), clock=clock, agent="sysadmin") == 0
    assert times == [DAY + timedelta(seconds=40)]


# --- AC4: the timer's command -------------------------------------------------------------------------
def test_check_now_without_requests_only_asks_the_cabinet(cfg, sp, cab, monkeypatch):
    called = []
    monkeypatch.setattr(deliver, "tick", lambda *a, **k: called.append(k) or 0)
    assert deliver.check_now(cfg, sp, DAY, known=["node-c"]) == 0
    # without requests it asks for the signed baselines too: none — nothing else
    assert called == [] and cab.calls == ["server.check.take", "monitor.window.take", "monitor_trust.take"]


def test_check_now_ticks_the_requested_servers_and_reports_each(cfg, sp, cab, monkeypatch):
    servers_in(cab, "node-c", "node-d", "node-e")
    seed(sp)
    first, other = press(cab, "node-c"), press(cab, "node-d")
    stray = press(cab, "node-e")
    seen = []
    monkeypatch.setattr(deliver, "collect", collected([
        CheckResult("node-c", "disk", True, [obs("node-c", level="ok", value="40%")]),
        CheckResult("node-d", "disk", True, [obs("node-d", level="warn", value="88%")])], seen))
    monkeypatch.setattr(deliver, "_known_servers", lambda: ["node-c", "node-d"])
    assert deliver.check_now(cfg, sp, DAY, wake_fn=Wake(), clock=lambda: 0.0, agent="sysadmin",
                             known=["node-c", "node-d"]) == 0
    assert seen[0]["servers"] == ["node-c", "node-d"]
    assert seen[0]["force"] is True
    rows = {s["alias"]: s["check_now"] for s in cab.call({"op": "server.list"})["items"]}
    assert rows["node-c"]["id"] == first["id"] and rows["node-c"]["state"] == "done"
    assert rows["node-d"]["id"] == other["id"] and rows["node-d"]["state"] == "done"
    assert rows["node-e"]["id"] == stray["id"] and rows["node-e"]["state"] == "failed"
    assert rows["node-e"]["detail"] == "сервер не подключён к монитору"
    checks = cab.call({"op": "server.get", "alias": "node-d"})["checks"]
    assert [(c["check"], c["level"], c["value"]) for c in checks] == [("disk", "warn", "88%")]


def test_check_now_reports_a_failed_tick(cfg, sp, cab, monkeypatch):
    servers_in(cab, "node-c")
    press(cab, "node-c")
    monkeypatch.setattr(deliver, "tick", lambda *a, **k: 1)
    assert deliver.check_now(cfg, sp, DAY, known=["node-c"]) == 0
    row = cab.call({"op": "server.get", "alias": "node-c"})["check_now"]
    assert row["state"] == "failed" and "тик не прошёл" in row["detail"]


def test_check_now_with_the_cabinet_down_is_an_error_line(cfg, sp, cab, capsys):
    cab.up = False
    assert deliver.check_now(cfg, sp, DAY, known=["node-c"]) == 1
    assert "заявки проверки не прочитаны (unavailable)" in capsys.readouterr().err


def test_main_runs_check_now_with_the_agent(sp, monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(deliver, "check_now", lambda cfg, path, now, **kw: seen.update(kw) or 0)
    env = {"KD_MONITOR_STATE": str(sp), "KD_MONITOR_CONFIG": str(CONFIG), "KD_AGENT": "sysadmin"}
    assert deliver.main(["check-now"], env=env, now=DAY, servers=["node-c"]) == 0
    assert seen["agent"] == "sysadmin" and seen["known"] == ["node-c"]
