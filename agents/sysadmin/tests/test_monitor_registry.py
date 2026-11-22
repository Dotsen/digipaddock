import json
import os
import stat
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from sysadmin.monitor.model import CheckResult, Observation, load_config
from sysadmin.monitor.registry import Registry, format_list

UTC3 = timezone(timedelta(hours=3))
T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC3)
CONFIG = Path(__file__).resolve().parent / "fixtures" / "monitor-config.json"


def obs(level, server="srvA", check="disk", subject="/", value="90%", text="диск"):
    return Observation(server, check, subject, level, value, text)


def res(*observations, server="srvA", check="disk", ran=True):
    return CheckResult(server, check, ran, list(observations))


def reg():
    return Registry.load(Path("/нет/такого/state.json"), date(2026, 10, 5))


def step(r, level, i=0, **kw):
    return r.observe([res(obs(level, **kw))], T0 + timedelta(minutes=5 * i))


def test_key():
    assert obs("warn").key == "srvA:disk:/"


def test_open_then_repeat_no_events():
    r = reg()
    ev = step(r, "warn")
    assert [(e.kind, e.problem_id) for e in ev] == [("open", "P-1")]
    assert ev[0].at.endswith("+03:00") and ev[0].repeats == 0
    assert step(r, "warn", 1) == []


def test_confirmations_count_real_bad_observations_and_survive_restart(tmp_path):
    r = reg()
    step(r, "warn")
    step(r, "warn")  # the same observation cannot count twice
    r.observe([res(obs("warn"), ran=False)], T0 + timedelta(minutes=3))
    step(r, "crit", 1)
    p = r.open_problems()[0]
    assert p["confirmation_count"] == 2 and p["confirmation_started_at"] == T0.isoformat()
    path = tmp_path / "state.json"
    r.save(path)
    loaded = Registry.load(path, T0.date())
    step(loaded, "warn", 2)
    assert loaded.open_problems()[0]["confirmation_count"] == 3
    step(loaded, "ok", 3)
    assert loaded.open_problems()[0]["confirmation_count"] == 3


def test_legacy_incident_starts_counting_at_first_new_bad_run():
    r = reg()
    step(r, "warn")
    p = r.open_problems()[0]
    del p["confirmation_count"], p["confirmation_started_at"]
    step(r, "warn", 1)
    assert p["confirmation_count"] == 1
    assert p["confirmation_started_at"] == (T0 + timedelta(minutes=5)).isoformat()


def test_cumulative_confirmation_count_survives_offline_sync_compaction():
    from sysadmin.monitor import cabinet

    r = reg()
    for index in range(3):
        at = T0 + timedelta(minutes=5 * index)
        step(r, "warn", index)
        item = cabinet.sync_item([res(obs("warn"))], r.state, ["srvA"], {}, at)
        cabinet.enqueue(r.state, [item], at)
    assert len(r.state["cabinet_queue"]) == 1
    count = r.state["cabinet_queue"][0]["confirmations"][0]
    assert count["count"] == 3 and count["at"] == (T0 + timedelta(minutes=10)).isoformat()


def test_worse_once_and_flapping():
    r = reg()
    step(r, "warn")
    assert [e.kind for e in step(r, "crit", 1)] == ["worse"]
    kinds = [e.kind for i, lv in enumerate(["warn", "crit", "warn", "crit"]) for e in step(r, lv, 2 + i)]
    assert kinds == []
    r2 = reg()
    kinds = [e.kind for i, lv in enumerate(["warn", "crit", "warn", "crit"]) for e in step(r2, lv, i)]
    assert kinds == ["open", "worse"]


def test_close_after_three_ok():
    r = reg()
    step(r, "warn")
    assert step(r, "ok", 1) == [] and step(r, "ok", 2) == []
    ev = step(r, "ok", 3)
    assert [e.kind for e in ev] == ["close"] and ev[0].problem_id == "P-1"
    assert r.open_problems() == [] and "srvA:disk:/" in r.state["closed"]


def test_periodic_source_closes_on_first_ok():
    """A source from every_min (once an hour/a day) closes on the first "ok" — three runs kept updates open for three
    days; gate:X and sec:*/posture — by the schedule's name; health and the rest — by close_after, as before."""
    every = {"updates": 1440, "sec": 60}
    for check, subject in (("updates", "security"), ("gate:updates", "-"), ("sec:units", "x.service"), ("posture", "fw")):
        r = Registry.load(Path("/нет/такого/state.json"), date(2026, 10, 5), every_min=every)
        r.observe([res(obs("warn", check=check, subject=subject), check=check)], T0)
        ev = r.observe([res(check=check)], T0 + timedelta(days=1))
        assert [e.kind for e in ev] == ["close"], check
    for check, subject in (("disk", "/"), ("health", "updates")):
        r = Registry.load(Path("/нет/такого/state.json"), date(2026, 10, 5), every_min=every)
        r.observe([res(obs("warn", check=check, subject=subject), check=check)], T0)
        kinds = [[e.kind for e in r.observe([res(check=check)], T0 + timedelta(minutes=5 * i))] for i in (1, 2, 3)]
        assert kinds == [[], [], ["close"]], check


def test_flap_ok_resets_streak():
    r = reg()
    kinds = [e.kind for i, lv in enumerate(["warn", "ok", "warn", "ok", "warn"]) for e in step(r, lv, i)]
    assert kinds == ["open"]


def test_not_ran_neither_closes_nor_opens():
    r = reg()
    step(r, "warn")
    for i in range(3):
        assert r.observe([res(ran=False)], T0 + timedelta(minutes=i)) == []
    assert len(r.open_problems()) == 1
    r2 = reg()
    assert r2.observe([res(obs("crit"), ran=False)], T0) == []
    assert r2.open_problems() == []


def test_source_closes_foreign_key_without_observation():
    r = reg()
    hb = Observation("node-a", "heartbeat", "-", "warn", "99 мин", "молчит")
    r.observe([CheckResult("example-prod", "heartbeat", True, [hb])], T0)
    assert r.open_problems()[0]["source"] == ["example-prod", "heartbeat"]
    kinds = []
    for i in range(1, 4):
        kinds += [e.kind for e in r.observe([CheckResult("example-prod", "heartbeat", True, [])], T0 + timedelta(minutes=i))]
    assert kinds == ["close"]


def test_other_source_does_not_close():
    r = reg()
    step(r, "warn")
    for i in range(1, 6):
        r.observe([res(server="srvB")], T0 + timedelta(minutes=i))
    assert len(r.open_problems()) == 1
    assert r.open_problems()[0]["ok_streak"] == 0


def _cycle(r):
    step(r, "warn")
    for i in range(1, 4):
        step(r, "ok", i)


def test_repeats_within_window():
    r = reg()
    _cycle(r)
    ev = r.observe([res(obs("warn"))], T0 + timedelta(days=2))
    assert ev[0].kind == "open" and ev[0].repeats == 1
    assert r.open_problems()[0]["repeats"] == 1


def test_repeats_expire():
    r = reg()
    _cycle(r)
    ev = r.observe([res(obs("warn"))], T0 + timedelta(days=8))
    assert ev[0].repeats == 0
    assert r.state["closed"] == {}


def test_ack():
    r = reg()
    step(r, "warn")
    assert r.ack("P-1", None) is True
    assert r.open_problems()[0]["acked_until"] == "close"
    until = T0 + timedelta(hours=4)
    assert r.ack("P-1", until) is True
    assert r.open_problems()[0]["acked_until"] == until.isoformat()
    assert r.ack("P-99", None) is False


def test_save_load_roundtrip(tmp_path):
    p = tmp_path / "sub" / "state.json"
    r = reg()
    step(r, "warn")
    r.save(p)
    assert [f.name for f in p.parent.iterdir()] == ["state.json"]
    r2 = Registry.load(p, date(2026, 10, 5))
    assert r2.state == r.state


def test_save_mode_0600(tmp_path):
    """state.json — 0600: it holds untrusted addresses and security snapshots."""
    p = tmp_path / "state.json"
    reg().save(p)
    if os.name != "nt":
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert json.loads(p.read_text(encoding="utf-8"))["problems"] == {}


def test_corrupt_file(tmp_path, capsys):
    p = tmp_path / "state.json"
    p.write_text("{не json", encoding="utf-8")
    r = Registry.load(p, date(2026, 10, 5))
    assert r.open_problems() == [] and r.state["last_morning"] == "2026-10-05"
    assert r.state["next_id"] == 1
    assert not p.exists()
    assert len(list(tmp_path.glob("state.json.bad-*"))) == 1
    assert f"kd-monitor: реестр повреждён, начат заново: {p}" in capsys.readouterr().err


def test_missing_file_new_registry():
    r = reg()
    assert r.state["last_morning"] == "2026-10-05"
    for k in ("problems", "closed", "next_id", "outbox", "wakes", "unreachable", "monitoring_ts", "last_run", "recent"):
        assert k in r.state


def test_new_state_has_sec_and_close_now_without_event():
    r = reg()
    assert r.state["sec"] == {}
    step(r, "warn")
    assert len(r.state["outbox"]) == 1
    p = r.close_now("P-1", T0.isoformat())
    assert p["key"] == "srvA:disk:/" and r.open_problems() == []
    assert r.state["closed"]["srvA:disk:/"] == [T0.isoformat()] and r.state["outbox"] == []
    assert r.state["recent"]["P-1"]["closed_at"] == T0.isoformat()
    assert r.close_now("P-1", T0.isoformat()) is None


def test_format_list():
    assert format_list([]) == "открытых проблем нет"
    r = reg()
    step(r, "warn")
    out = format_list(r.open_problems())
    assert out.splitlines()[0].split() == ["ID", "уровень", "сервер", "проверка", "объект", "значение", "с", "принято",
                                           "статус"]
    assert "P-1" in out and "srvA" in out and out.splitlines()[1].endswith(" -")


def test_config_keys():
    cfg = load_config(CONFIG)
    for k in ["close_after", "repeat_window_days", "quiet_hours", "morning_at", "max_wakes_per_hour",
              "fallback_after_min", "parallel", "tick_budget_s", "call_timeout_s",
              "message_max_chars", "external_text_max", "unreachable_after", "critical_unreachable",
              "every_min", "disk_pct", "mem_available_pct", "mem_psi", "swap_io", "units_ignore",
              "units_required", "units_required_after",
              "wg_stale_min", "vpn_nodes", "heartbeat", "mysql", "certs", "backup", "monitoring",
              "security"]:
        assert k in cfg
    assert "web_dir" not in cfg  # no static pages
    assert "board_tasks_dir" not in cfg  # the cards come from the cabinet
    assert cfg["disk_pct"] == {"warn": 85, "crit": 95}
    assert len(cfg["vpn_nodes"]) == 11
    json.dumps(cfg)


def test_event_and_list_split_gate_key_by_source():
    r = reg()
    ev = r.observe([res(obs("warn", server="node-f", check="gate:wg", subject="-", value="отказ", text="запрещено"),
                        server="node-f", check="gate:wg")], T0)[0]
    assert (ev.server, ev.check, ev.subject) == ("node-f", "gate:wg", "-")
    row = format_list(r.open_problems()).splitlines()[1].split()
    assert row[2:5] == ["node-f", "gate:wg", "-"]


def test_event_split_for_foreign_server_and_colon_subject():
    r = reg()
    ev = r.observe([res(obs("warn", server="node-a", check="heartbeat", subject="-"),
                        server="example-prod", check="heartbeat")], T0)[0]
    assert (ev.server, ev.check, ev.subject) == ("node-a", "heartbeat", "-")
    ev = r.observe([res(obs("warn", server="p", check="monitoring_file", subject="vpn_country:ZZ"),
                        server="p", check="monitoring_file")], T0)[0]
    assert (ev.server, ev.check, ev.subject) == ("p", "monitoring_file", "vpn_country:ZZ")


# --- history and recently closed ---

def kinds(p):
    return [(h["kind"], h["level"], h["value"]) for h in p["history"]]


def test_history_open_level_both_ways_and_close():
    r = reg()
    step(r, "warn", value="88%")
    step(r, "warn", 1, value="89%")  # the same level — no record
    step(r, "crit", 2, value="96%")
    step(r, "warn", 3, value="90%")  # down — a record too
    for i in (4, 5, 6):
        step(r, "ok", i, value=f"7{i}%")
    assert r.open_problems() == []
    rec = r.state["recent"]["P-1"]
    # a close — the level ok and the value of the last good observation
    assert kinds(rec) == [("open", "warn", "88%"), ("worse", "crit", "96%"), ("better", "warn", "90%"),
                          ("close", "ok", "76%")]
    assert rec["history"][0]["at"] == T0.isoformat()
    assert rec["closed_at"] == rec["history"][-1]["at"] == (T0 + timedelta(minutes=30)).isoformat()
    assert rec["key"] == "srvA:disk:/" and r.state["closed"]["srvA:disk:/"] == [rec["closed_at"]]  # repeats — as it was


def test_history_close_without_ok_observation_has_empty_value():
    r = reg()
    hb = Observation("node-a", "heartbeat", "-", "warn", "99 мин", "молчит")
    r.observe([CheckResult("example-prod", "heartbeat", True, [hb])], T0)
    for i in range(1, 4):  # the source is silent about the key — there is no ok observation
        r.observe([CheckResult("example-prod", "heartbeat", True, [])], T0 + timedelta(minutes=i))
    assert kinds(r.state["recent"]["P-1"])[-1] == ("close", "ok", "")


def test_history_capped_at_20_keeps_open_entry():
    r = reg()
    for i, lv in enumerate(["warn", "crit"] * 15):
        step(r, lv, i, value=str(i))
    h = r.open_problems()[0]["history"]
    assert len(h) == 20 and h[0]["kind"] == "open" and h[0]["value"] == "0"
    assert [x["value"] for x in h[1:]] == [str(i) for i in range(11, 30)]


def test_history_value_is_truncated():
    r = reg()
    step(r, "warn", value="x" * 500)
    assert len(r.open_problems()[0]["history"][0]["value"]) == 200


def test_recent_kept_14_days_then_purged():
    r = reg()
    _cycle(r)  # opened at T0, closed at T0+15 min
    r.observe([res(server="srvB")], T0 + timedelta(days=14))
    assert "P-1" in r.state["recent"] and r.state["closed"] == {}  # closed — 7 days, recent — 14
    r.observe([res(server="srvB")], T0 + timedelta(days=14, minutes=16))
    assert r.state["recent"] == {}


def test_reopen_after_close_gets_new_id_and_old_page_stays():
    r = reg()
    _cycle(r)
    later = T0 + timedelta(hours=2)
    ev = r.observe([res(obs("crit", value="97%"))], later)
    assert ev[0].problem_id == "P-2" and ev[0].repeats == 1
    assert r.state["recent"]["P-1"]["key"] == "srvA:disk:/"
    assert r.open_problems()[0]["history"] == [{"at": later.isoformat(), "kind": "open", "level": "crit", "value": "97%"}]


def test_sticky_security_problem_history_and_accept_into_recent():
    """sec:*/logins are sticky: the source reports the object as "bad" until it is accepted — the problem does not close by itself."""
    r = reg()

    def sec(level, i):
        o = obs(level, check="sec:keys", subject="/root/.ssh/authorized_keys", value="изменён", text="ключи")
        return r.observe([res(o, check="sec:keys")], T0 + timedelta(minutes=5 * i))

    for i in range(5):
        sec("warn", i)
    assert [h["kind"] for h in r.open_problems()[0]["history"]] == ["open"]
    sec("crit", 5)
    assert [h["kind"] for h in r.open_problems()[0]["history"]] == ["open", "worse"]
    at = (T0 + timedelta(minutes=35)).isoformat()
    assert r.close_now("P-1", at)["id"] == "P-1" and r.open_problems() == []
    rec = r.state["recent"]["P-1"]
    assert rec["closed_at"] == at and kinds(rec)[1:] == [("worse", "crit", "изменён"), ("accept", "crit", "изменён")]


def test_old_state_without_history_and_recent_loads(tmp_path):
    p = tmp_path / "state.json"
    old = {"problems": {"srvA:disk:/": {"id": "P-7", "key": "srvA:disk:/", "source": ["srvA", "disk"], "level": "warn",
                                        "max_level": "warn", "value": "88%", "text": "t", "opened_at": T0.isoformat(),
                                        "last_seen": T0.isoformat(), "ok_streak": 0, "acked_until": None, "repeats": 0}},
           "closed": {}, "next_id": 8, "outbox": [], "wakes": [], "last_morning": "2026-10-05", "unreachable": {},
           "monitoring_ts": None, "last_run": None, "sec": {}}
    p.write_text(json.dumps(old), encoding="utf-8")
    r = Registry.load(p, date(2026, 10, 5))
    assert r.state["recent"] == {}
    r.observe([res(obs("crit", value="96%"))], T0 + timedelta(minutes=5))
    assert [h["kind"] for h in r.open_problems()[0]["history"]] == ["worse"]
    for i in (2, 3, 4):
        r.observe([res()], T0 + timedelta(minutes=5 * i))
    assert [h["kind"] for h in r.state["recent"]["P-7"]["history"]] == ["worse", "close"]


def test_recent_not_a_dict_is_reset(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"problems": {}, "recent": []}), encoding="utf-8")
    assert Registry.load(p, date(2026, 10, 5)).state["recent"] == {}


# --- the status "being checked": fixed → a recheck, one "ok" closes ---

FIX_AT = T0 + timedelta(minutes=10)
RECHECK = FIX_AT + timedelta(minutes=5)


def upd(level, value="3", ran=True):
    return res(obs(level, check="updates", subject="security", value=value, text="обновления"), check="updates", ran=ran)


def opened_fixed():
    r = reg()
    r.observe([upd("warn")], T0)
    return r, r.fix("P-1", "KD-81", FIX_AT.isoformat())


def test_fix_sets_flag_and_history():
    r, p = opened_fixed()
    assert p["fixed"] == {"at": FIX_AT.isoformat(), "card": "KD-81"}
    assert kinds(p) == [("open", "warn", "3"), ("fixed", "warn", "KD-81")]
    assert r.fix("P-9", "KD-81", FIX_AT.isoformat()) is None
    assert r.state["outbox"][-1]["kind"] == "open"  # a mark produces no events


def test_fixed_one_ok_closes_immediately():
    r, _ = opened_fixed()
    ev = r.observe([upd("ok", value="0")], RECHECK)
    assert [(e.kind, e.problem_id) for e in ev] == [("close", "P-1")] and r.open_problems() == []
    assert r.state["outbox"][-1]["kind"] == "close"  # the close goes to the sysadmin as usual
    rec = r.state["recent"]["P-1"]
    assert kinds(rec)[-2:] == [("fixed", "warn", "KD-81"), ("close", "ok", "0")]
    assert "fixed" not in rec and rec["closed_at"] == RECHECK.isoformat()
    assert r.state["closed"]["srvA:updates:security"] == [RECHECK.isoformat()]


def test_fixed_bad_recheck_is_fix_failed_and_clears_flag():
    r, _ = opened_fixed()
    ev = r.observe([upd("warn", value="4")], RECHECK)
    assert [(e.kind, e.problem_id, e.level, e.value) for e in ev] == [("fix_failed", "P-1", "warn", "4")]
    assert r.state["outbox"][-1]["kind"] == "fix_failed"
    p = r.open_problems()[0]
    assert "fixed" not in p and kinds(p)[-1] == ("fix_failed", "warn", "4")
    # without a mark — the usual order again: a close only after close_after "ok" in a row
    assert r.observe([upd("ok")], RECHECK + timedelta(minutes=5)) == []
    assert r.observe([upd("ok")], RECHECK + timedelta(minutes=10)) == []
    assert [e.kind for e in r.observe([upd("ok")], RECHECK + timedelta(minutes=15))] == ["close"]


def test_fixed_recheck_worse_gives_fix_failed_then_worse():
    r, _ = opened_fixed()
    ev = r.observe([upd("crit", value="9")], RECHECK)
    assert [(e.kind, e.level) for e in ev] == [("fix_failed", "crit"), ("worse", "crit")]
    assert [h["kind"] for h in r.open_problems()[0]["history"]] == ["open", "fixed", "fix_failed", "worse"]


def test_fixed_kept_when_check_did_not_run_or_other_source():
    r, _ = opened_fixed()
    assert r.observe([upd("ok", ran=False)], RECHECK) == []  # the server did not answer / the check did not run
    assert r.observe([res(server="srvB", check="updates")], RECHECK) == []
    p = r.open_problems()[0]
    assert p["fixed"]["card"] == "KD-81" and p["ok_streak"] == 0
    assert [e.kind for e in r.observe([upd("ok")], RECHECK + timedelta(minutes=5))] == ["close"]


def test_fixed_ignores_poll_started_before_the_mark():
    """The mark was set during a poll: the poll's data is older than the fix — neither a close at the first "ok" nor fix_failed."""
    r, _ = opened_fixed()
    before = FIX_AT - timedelta(minutes=1)
    assert r.observe([upd("warn")], before) == []
    assert r.open_problems()[0]["fixed"]["card"] == "KD-81"
    assert r.observe([upd("ok")], before) == [] and r.open_problems()[0]["ok_streak"] == 1
    assert [e.kind for e in r.observe([upd("ok")], RECHECK)] == ["close"]


def test_fix_refuses_sticky_security_problems():
    """sec:* and logins close only by accept on the owner's word — "fixed" does not apply to them."""
    r = reg()
    r.observe([res(obs("warn", check="sec:keys", subject="/root/.ssh/authorized_keys"), check="sec:keys"),
               res(obs("crit", check="logins", subject="root@ED25519 SHA256:k"), check="logins"),
               res(obs("warn", check="gate:sec", subject="-"), check="gate:sec")], T0)
    for pid in ("P-1", "P-2"):
        with pytest.raises(ValueError, match="accept"):
            r.fix(pid, "KD-81", FIX_AT.isoformat())
    assert not any("fixed" in p for p in r.open_problems()[:2])
    assert r.fix("P-3", "KD-81", FIX_AT.isoformat())["fixed"]["card"] == "KD-81"  # a gateway refusal is not sticky


def test_fix_refuses_oom_counted_over_24h():
    """OOM is counted over a day: after a correct fix the nearest tick still sees the OOM — "did not help" would be false."""
    r = reg()
    r.observe([res(obs("warn", check="oom", subject="-", value="2"), check="oom")], T0)
    with pytest.raises(ValueError, match="OOM считается за 24 ч — закроется сама"):
        r.fix("P-1", "KD-81", FIX_AT.isoformat())
    assert "fixed" not in r.open_problems()[0] and kinds(r.open_problems()[0]) == [("open", "warn", "2")]


def test_format_list_shows_fixed_status():
    r, _ = opened_fixed()
    r.observe([res(obs("crit", server="srvB"), server="srvB")], T0)
    lines = format_list(r.open_problems()).splitlines()
    assert lines[1].startswith("P-1") and lines[1].endswith("проверяется KD-81")
    assert lines[2].startswith("P-2") and lines[2].endswith(" -")
