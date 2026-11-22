"""The monitor writes its servers, checks and incidents P-n to the cabinet through the socket."""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from sysadmin.monitor import cabinet as mc, deliver
from sysadmin.monitor.model import CheckResult, Observation, load_config
from sysadmin.monitor.registry import Registry

UTC3 = timezone(timedelta(hours=3))
CONFIG = Path(__file__).resolve().parent / "fixtures" / "monitor-config.json"
DAY = datetime(2026, 10, 7, 14, 0, tzinfo=UTC3)


class Session:
    """kd.wake as the tick meets it: the word for its agent's command."""

    def __init__(self, status="sent"):
        self.status, self.calls = status, []

    def __call__(self, agent, text, *, urgent=False, space="chat"):
        assert space == "monitor"  # the monitor's wakes go to the "Monitor" thread
        self.calls.append(text)
        return self.status

    def prompts(self):
        return list(self.calls) if self.status == "sent" else []


@pytest.fixture
def cfg(tmp_path):
    c = load_config(CONFIG)
    c["web_dir"] = str(tmp_path / "web")
    return c


@pytest.fixture
def sp(tmp_path):
    return tmp_path / "state.json"


def obs(server="example-prod", check="disk", subject="/", level="crit", value="96%"):
    return Observation(server, check, subject, level, value, f"{subject}: {value}")


def result(*observations, server="example-prod", check="disk"):
    return [CheckResult(server, check, True, list(observations))]


def tick(cfg, sp, now, results, session=None, monkeypatch=None, clock=lambda: 0.0):
    monkeypatch.setattr(deliver, "collect", lambda *a, **k: results)
    return deliver.tick(cfg, sp, now, wake_fn=session or Session(), ssh=None, clock=clock, agent="sysadmin",
                        servers=["example-prod", "node-c"])


def state(sp):
    return json.loads(sp.read_text(encoding="utf-8"))


def incidents(cabinet, status=""):
    return {i["id"]: i for i in cabinet.env.client("root").call({"op": "incident.list", "status": status})["items"]}


def legacy_state(sp):
    """state.json as the monitor kept it before the change: its own numbers, an ack until close, a timed ack, a fix mark
    and a problem closed two days ago."""
    reg = Registry.load(sp, date(2026, 10, 7), close_after=1)
    reg.observe(result(obs(level="warn", value="88%"))
                + result(obs("node-c", "mem", "ram", "crit", "97%"), server="node-c", check="mem")
                + result(obs("node-c", "units", "nginx", "crit", "failed"), server="node-c", check="units")
                + result(obs("node-c", "certs", "a.example", "warn", "9 дн"), server="node-c", check="certs"),
                DAY - timedelta(days=3))
    reg.ack("P-1", None)
    reg.ack("P-2", DAY + timedelta(hours=5))
    reg.fix("P-3", "KD-77", (DAY - timedelta(days=1)).isoformat())
    reg.observe(result(obs("node-c", "certs", "a.example", "ok", "60 дн"), server="node-c", check="certs"),
                DAY - timedelta(days=2))
    reg.state["outbox"] = []
    reg.save(sp)
    return reg.state


# --- AC2: the first tick moves state.json into the cabinet ------------------------------------------------
def test_the_first_tick_imports_open_and_recent_problems_with_their_numbers_statuses_and_history(
        cfg, sp, monkeypatch, monitor_cabinet, capsys):
    before = legacy_state(sp)
    assert sorted(p["id"] for p in before["problems"].values()) == ["P-1", "P-2", "P-3"]
    assert list(before["recent"]) == ["P-4"]
    raw = sp.read_text(encoding="utf-8")
    tick(cfg, sp, DAY, [], monkeypatch=monkeypatch)
    assert (sp.parent / "state.json.pre-kd9054").read_text(encoding="utf-8") == raw
    got = incidents(monitor_cabinet)
    assert sorted(got) == ["P-1", "P-2", "P-3", "P-4"]
    assert got["P-1"]["status"] == "acknowledged"
    assert (got["P-2"]["status"], got["P-2"]["muted_until"]) == ("open", "2026-10-07T16:00:00Z")
    assert (got["P-3"]["fixed_card"], got["P-4"]["status"]) == ("KD-77", "resolved")
    timeline = monitor_cabinet.env.client("root").call({"op": "incident.get", "incident": 3})["timeline"]
    assert [e["kind"] for e in timeline] == ["incident.opened", "incident.fixed"]
    assert "перенесено 4" in capsys.readouterr().err
    assert state(sp)["cabinet_imported"]
    # a second tick does not import again; an explicit repeat adds nothing
    events = monitor_cabinet.env.store._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    tick(cfg, sp, DAY + timedelta(minutes=5), [], monkeypatch=monkeypatch)
    assert mc.import_state(sp, before, monitor_cabinet, lambda: 0.0, 100.0)
    assert monitor_cabinet.env.store._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events
    assert len(incidents(monitor_cabinet)) == 4


# --- AC3: a new problem's number comes from the cabinet ---------------------------------------------------
def test_a_new_problem_gets_the_number_after_the_largest_imported_one(cfg, sp, monkeypatch, monitor_cabinet):
    legacy_state(sp)
    h = Session()
    tick(cfg, sp, DAY, result(obs("example-prod", "mem", "ram", "crit", "99%"), check="mem"), h,
         monkeypatch=monkeypatch)
    assert "P-5 открыта" in h.prompts()[0]
    assert state(sp)["problems"]["example-prod:mem:ram"]["id"] == "P-5"
    assert state(sp)["next_id"] == 5  # the monitor's own counter no longer moves


def test_switched_off_plan_windows_are_not_asked_for(cfg, sp, monkeypatch, monitor_cabinet):
    cfg["security"]["plan_windows"] = False
    tick(cfg, sp, DAY, result(obs()), Session(), monkeypatch=monkeypatch)
    assert "monitor.window.active" not in monitor_cabinet.calls
    assert state(sp)["sec"]["plan_windows"] == {}


def test_a_window_opened_during_the_poll_keeps_its_servers_changes_out(cfg, sp, monkeypatch, monitor_cabinet):
    """The step began after the tick read the open windows."""
    card = monitor_cabinet.env.card(project="admin", assignee="@sysadmin", status="In Progress").id

    def collect(*a, **k):
        monitor_cabinet.env.call("digi-exec", {"op": "monitor.window.open", "server": "example-prod", "card": card,
                                               "step": "s", "run": "r"})
        return [CheckResult("example-prod", "sec:units", True,
                            [Observation("example-prod", "sec:units", "x.service", "warn", "v", "t")])]

    monkeypatch.setattr(deliver, "collect", collect)
    deliver.tick(cfg, sp, DAY, wake_fn=Session(), ssh=None, clock=lambda: 0.0, agent="sysadmin",
                 servers=["example-prod", "node-c"])
    assert "example-prod:sec:units:x.service" not in state(sp)["problems"]


# --- AC5: the cabinet is stopped ----------------------------------------------------------------------------
def test_without_the_cabinet_the_tick_queues_wakes_by_server_and_check_and_delivers_in_order_later(
        cfg, sp, monkeypatch, monitor_cabinet, capsys):
    Registry.load(sp, date(2026, 10, 7)).save(sp)
    monitor_cabinet.up = False
    h = Session()
    assert tick(cfg, sp, DAY, result(obs()), h, monkeypatch=monkeypatch) == 0
    text = h.prompts()[0]
    assert "проблема без номера открыта" in text and "example-prod" in text and "disk /" in text
    assert "P-" not in text
    st = state(sp)
    assert st["problems"]["example-prod:disk:/"]["id"] == ""
    assert [q["kind"] for q in st["cabinet_queue"]] == ["sync", "open"]
    # the signed trust, the settings and the acks are asked; the acks' refusal skips the tick's
    # other cabinet calls
    assert monitor_cabinet.calls == ["monitor_trust.take", "monitor.window.take", "monitor.settings.get",
                                     "monitor.window.active", "incident.list"]
    # it gets worse and the agent is not there: the kd-alert fallback names the server and the check, no number
    worse = result(obs(level="crit", value="99%"), obs(subject="/var", level="crit", value="97%"))
    assert tick(cfg, sp, DAY + timedelta(minutes=1), worse, Session("error"), monkeypatch=monkeypatch) == 0
    code = tick(cfg, sp, DAY + timedelta(minutes=32), worse, Session("error"), monkeypatch=monkeypatch)
    out = capsys.readouterr().out
    assert code == 3 and "kd-monitor: example-prod · диск" in out and "P-" not in out
    st = state(sp)
    assert [q["kind"] for q in st["cabinet_queue"]] == ["open", "open", "sync"]  # one sync, the opens kept
    # the cabinet is back: the queue goes in order, the numbers come, nothing twice
    monitor_cabinet.up = True
    h = Session()
    tick(cfg, sp, DAY + timedelta(minutes=37), worse, h, monkeypatch=monkeypatch)
    st = state(sp)
    assert st["cabinet_queue"] == []
    assert {k: p["id"] for k, p in st["problems"].items()} == {"example-prod:disk:/": "P-1",
                                                                "example-prod:disk:/var": "P-2"}
    assert sorted(incidents(monitor_cabinet)) == ["P-1", "P-2"]
    tick(cfg, sp, DAY + timedelta(minutes=42), worse, Session(), monkeypatch=monkeypatch)
    assert sorted(incidents(monitor_cabinet)) == ["P-1", "P-2"]
    assert monitor_cabinet.env.store._conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='incident.opened'").fetchone()[0] == 2


def test_a_merged_sync_stays_within_one_socket_request():
    st = {}
    change = {"server": "node-c", "check": "units", "subject": "x" * 100, "level": "crit", "prev": "ok",
              "value": "v" * 200, "text": "t" * 300, "at": DAY.isoformat()}
    for i in range(3000):
        mc.enqueue(st, [{"key": f"s{i}", "kind": "sync", "changes": [dict(change, at=str(i))]}], DAY)
    sync = st["cabinet_queue"][0]
    assert len(json.dumps(sync, ensure_ascii=False).encode("utf-8")) < mc.BATCH_BYTES
    assert sync["changes"][-1]["at"] == "2999"  # the newest are kept


def test_an_event_held_back_before_its_number_goes_out_with_it(cfg, sp, monkeypatch, monitor_cabinet):
    Registry.load(sp, date(2026, 10, 7)).save(sp)
    monitor_cabinet.up = False
    tick(cfg, sp, DAY, result(obs(level="warn", value="88%")), Session("error"), monkeypatch=monkeypatch)
    assert state(sp)["outbox"][0]["problem_id"] == ""
    monitor_cabinet.up = True
    h = Session()
    tick(cfg, sp, DAY + timedelta(minutes=5), result(obs(level="warn", value="88%")), h, monkeypatch=monkeypatch)
    assert "P-1 открыта" in h.prompts()[0] and state(sp)["outbox"] == []


def test_a_problem_closed_before_its_number_moves_to_its_number_in_recent(cfg, sp, monkeypatch, monitor_cabinet):
    Registry.load(sp, date(2026, 10, 7)).save(sp)
    monitor_cabinet.up = False
    tick(cfg, sp, DAY, result(obs(level="warn", value="88%")), monkeypatch=monkeypatch)
    ok = result(obs(level="ok", value="50%"))
    for i in range(1, 4):
        tick(cfg, sp, DAY + timedelta(minutes=5 * i), ok, monkeypatch=monkeypatch)
    assert [k[:1] for k in state(sp)["recent"]] == ["~"]
    monitor_cabinet.up = True
    tick(cfg, sp, DAY + timedelta(minutes=20), ok, monkeypatch=monkeypatch)
    assert list(state(sp)["recent"]) == ["P-1"] and state(sp)["recent"]["P-1"]["id"] == "P-1"
    assert incidents(monitor_cabinet)["P-1"]["status"] == "resolved"


def test_the_queue_stops_at_the_tick_budget(cfg, sp, monkeypatch, monitor_cabinet):
    queue = [{"key": "a", "kind": "sync", "at": DAY.isoformat(), "checks": []}]
    answers, why = mc.flush(queue, monitor_cabinet, lambda: 195.0, 200.0)
    assert answers == {} and why == "бюджет тика" and monitor_cabinet.calls == []


def test_a_long_outage_keeps_one_sync_and_every_open_and_resolve(monkeypatch):
    st = {}
    now = DAY
    for i in range(6000):
        mc.enqueue(st, [{"key": f"s{i}", "kind": "sync", "changes": [{"i": i}]},
                        {"key": f"u{i}", "kind": "update"}], now)
    mc.enqueue(st, [{"key": "o", "kind": "open"}, {"key": "r", "kind": "resolve"}], now)
    queue = st["cabinet_queue"]
    assert len(queue) <= mc.QUEUE_MAX and [q["kind"] for q in queue].count("sync") == 1
    assert {"o", "r"} <= {q["key"] for q in queue}
    sync = next(q for q in queue if q["kind"] == "sync")
    assert len(sync["changes"]) == mc.CHANGES_MAX and sync["changes"][-1] == {"i": 5999}
    mc.enqueue(st, [{"key": "late", "kind": "update"}], now + timedelta(days=8))
    assert {q["kind"] for q in st["cabinet_queue"]} == {"open", "resolve", "update"}
    assert [q["key"] for q in st["cabinet_queue"] if q["kind"] == "update"] == ["late"]


# --- AC6: the last result of every check and its status changes ---------------------------------------------
def test_after_a_tick_the_cabinet_has_the_last_result_of_every_check_and_ok_warn_ok_is_two_changes(
        cfg, sp, monkeypatch, monitor_cabinet):
    Registry.load(sp, date(2026, 10, 7)).save(sp)
    reach = [CheckResult("node-c", "reach", True, []), CheckResult("node-c", "load", False, [])]
    for i, level in enumerate(["ok", "warn", "ok"]):
        results = result(obs(level=level, value=f"{80 + i}%")) + reach
        tick(cfg, sp, DAY + timedelta(minutes=5 * i), results, monkeypatch=monkeypatch)
    root = monitor_cabinet.env.client("root")
    prod = root.call({"op": "server.get", "alias": "example-prod"})
    assert [(c["check"], c["subject"], c["level"], c["value"]) for c in prod["checks"]] == [
        ("disk", "/", "ok", "82%")]
    assert [(h["prev"], h["level"]) for h in prod["history"]] == [("warn", "ok"), ("ok", "warn")]
    node_c = root.call({"op": "server.get", "alias": "node-c"})
    assert [(c["check"], c["level"]) for c in node_c["checks"]] == [("reach", "ok")]  # load did not run: no row
    assert {s["alias"] for s in root.call({"op": "server.list"})["items"]} == {"node-c", "example-prod"}


# --- AC7: an acknowledged or muted incident, as today ----------------------------------------------------------
def test_an_incident_acknowledged_in_the_cabinet_does_not_wake_on_close_but_wakes_on_crit_and_fix_failed(
        cfg, sp, monkeypatch, monitor_cabinet):
    Registry.load(sp, date(2026, 10, 7)).save(sp)
    tick(cfg, sp, DAY, result(obs(level="warn", value="88%")), monkeypatch=monkeypatch)
    owner = monitor_cabinet.env
    owner.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    from kd.cabinet import api
    api.dispatch(owner.ctx, api.Principal("user", 0, "owner"), {"op": "incident.ack", "incident": "P-1"})
    h = Session()
    tick(cfg, sp, DAY + timedelta(minutes=5), result(obs(level="crit", value="97%")), h, monkeypatch=monkeypatch)
    assert state(sp)["problems"]["example-prod:disk:/"]["acked_until"] == "close"
    assert len(h.prompts()) == 1 and "P-1 ухудшилась" in h.prompts()[0]
    # the fix did not hold: wakes, acknowledged or not
    deliver.main(["fixed", "P-1", "KD-9"], env={"KD_MONITOR_STATE": str(sp), "KD_MONITOR_CONFIG": str(CONFIG)},
                 now=DAY + timedelta(minutes=6))
    h = Session()
    tick(cfg, sp, DAY + timedelta(minutes=10), result(obs(level="crit", value="97%")), h, monkeypatch=monkeypatch)
    assert "P-1 исправление не помогло" in h.prompts()[0]
    # closing an acknowledged one stays silent
    h = Session()
    ok = result(obs(level="ok", value="50%"))
    for i in range(1, 4):
        tick(cfg, sp, DAY + timedelta(minutes=10 + 5 * i), ok, h, monkeypatch=monkeypatch)
    assert state(sp)["problems"] == {} and h.prompts() == []
    timeline = owner.client("root").call({"op": "incident.get", "incident": 1})["timeline"]
    kinds = [e["kind"] for e in timeline]
    assert kinds[:2] == ["incident.opened", "incident.acknowledged"] and kinds[-1] == "incident.resolved"
    assert {"incident.worse", "incident.fixed", "incident.fix_failed"} <= set(kinds)


def test_a_mute_in_the_cabinet_silences_until_its_time_and_without_the_cabinet_the_last_known_holds(
        cfg, sp, monkeypatch, monitor_cabinet):
    Registry.load(sp, date(2026, 10, 7)).save(sp)
    tick(cfg, sp, DAY, result(obs(level="warn", value="88%")), monkeypatch=monkeypatch)
    root = monitor_cabinet.env.client("root")
    root.call({"op": "incident.mute", "incident": 1, "until": (DAY + timedelta(hours=1)).isoformat()})
    tick(cfg, sp, DAY + timedelta(minutes=5), result(obs(level="warn", value="88%")), monkeypatch=monkeypatch)
    muted = state(sp)["problems"]["example-prod:disk:/"]["acked_until"]
    assert datetime.fromisoformat(muted) == DAY + timedelta(hours=1)
    monitor_cabinet.up = False
    tick(cfg, sp, DAY + timedelta(minutes=10), result(obs(level="warn", value="88%")), monkeypatch=monkeypatch)
    assert state(sp)["problems"]["example-prod:disk:/"]["acked_until"] == muted
    monitor_cabinet.up = True
    root.call({"op": "incident.unmute", "incident": 1})
    tick(cfg, sp, DAY + timedelta(minutes=15), result(obs(level="warn", value="88%")), monkeypatch=monkeypatch)
    assert state(sp)["problems"]["example-prod:disk:/"]["acked_until"] is None


def test_kd_monitor_ack_goes_through_the_cabinet(cfg, sp, monkeypatch, monitor_cabinet, capsys):
    Registry.load(sp, date(2026, 10, 7)).save(sp)
    tick(cfg, sp, DAY, result(obs(level="warn", value="88%")), monkeypatch=monkeypatch)
    env = {"KD_MONITOR_STATE": str(sp), "KD_MONITOR_CONFIG": str(CONFIG)}
    assert deliver.main(["ack", "P-1", "--until", "2026-10-08T10:00"], env=env, now=DAY) == 0
    assert incidents(monitor_cabinet)["P-1"]["muted_until"] == "2026-10-08T07:00:00Z"
    monitor_cabinet.up = False
    assert deliver.main(["ack", "P-1"], env=env, now=DAY) == 1
    assert "кабинет не принял подтверждение" in capsys.readouterr().err
