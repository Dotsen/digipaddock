"""The monitor's tick runs by the owner's settings from the cabinet over config.json."""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from sysadmin.monitor import cabinet as mc, checks, deliver, runner
from sysadmin.monitor.model import CheckResult, load_config
from sysadmin.monitor.registry import Registry

UTC3 = timezone(timedelta(hours=3))
CONFIG = Path(__file__).resolve().parent / "fixtures" / "monitor-config.json"
DAY = datetime(2026, 10, 7, 14, 0, tzinfo=UTC3)
DF = ("Filesystem 1024-blocks Used Available Capacity Mounted on\n"
      "/dev/sda1 100 82 18 82% /\n")


class Session:
    def __init__(self):
        self.calls = []

    def __call__(self, agent, text, *, urgent=False, **kw):  # space="monitor" since the change
        self.calls.append(text)
        return "sent"


@pytest.fixture
def cfg(tmp_path):
    c = load_config(CONFIG)
    c["web_dir"] = str(tmp_path / "web")
    return c


@pytest.fixture
def sp(tmp_path):
    Registry.load(tmp_path / "state.json", date(2026, 10, 7)).save(tmp_path / "state.json")
    return tmp_path / "state.json"


@pytest.fixture
def owner(monitor_cabinet):
    from kd.cabinet import api

    env = monitor_cabinet.env
    env.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")

    def save(*changes, signed=True):
        rev = env.call("root", {"op": "monitor.settings.get"})["revision"]
        return api.dispatch(env.ctx, api.Principal("user", -1, "owner"),
                            {"op": "monitor.settings.save", "revision": rev, "changes": list(changes),
                             "signed": signed})
    return save


def disk_collect(monkeypatch, seen=None):
    """The poll as far as the disk of example-prod: the real parser over 82 % of / with the tick's config."""
    def collect(cfg, now, state, deadline, **kw):
        if seen is not None:
            seen.append(cfg)
        return [CheckResult("example-prod", "disk", True, checks.disk("example-prod", DF, cfg, now))]
    monkeypatch.setattr(deliver, "collect", collect)


def tick(cfg, sp, now, session=None):
    return deliver.tick(cfg, sp, now, wake_fn=session or Session(), ssh=None, clock=lambda: 0.0, agent="sysadmin",
                        servers=["example-prod"])


def state(sp):
    return json.loads(sp.read_text(encoding="utf-8"))


def incidents(cabinet):
    return {i["id"]: i for i in cabinet.env.client("root").call({"op": "incident.list"})["items"]}


# --- AC1: an edit in the cabinet steers the next tick ------------------------------------------------------------
def test_the_next_tick_opens_a_problem_by_the_edited_threshold(cfg, sp, monkeypatch, monitor_cabinet, owner):
    disk_collect(monkeypatch)
    tick(cfg, sp, DAY)
    assert state(sp)["problems"] == {}  # 82 % under the default warn 85
    owner({"key": "disk_pct.warn", "value": 80})
    h = Session()
    tick(cfg, sp, DAY + timedelta(minutes=5), h)
    assert state(sp)["problems"]["example-prod:disk:/"]["level"] == "warn"
    assert "P-1 открыта" in h.calls[0]
    assert incidents(monitor_cabinet)["P-1"]["status"] == "open"
    assert state(sp)["cabinet_settings"]["revision"] == 1
    # back to the default: the next tick goes by config.json again and the problem closes in its time
    owner({"key": "disk_pct.warn", "reset": True})
    for i in range(2, 5):
        tick(cfg, sp, DAY + timedelta(minutes=5 * i))
    assert state(sp)["problems"] == {} and incidents(monitor_cabinet)["P-1"]["status"] == "resolved"


# --- AC2: without the cabinet the last set, without it config.json ------------------------------------------------
def test_without_the_cabinet_the_tick_keeps_the_last_set_and_without_one_config_json(
        cfg, sp, monkeypatch, monitor_cabinet, owner, capsys):
    seen = []
    disk_collect(monkeypatch, seen)
    owner({"key": "disk_pct.warn", "value": 80}, {"key": "max_wakes_per_hour", "value": 10})
    tick(cfg, sp, DAY)
    monitor_cabinet.up = False
    assert tick(cfg, sp, DAY + timedelta(minutes=5)) == 0
    assert seen[-1]["disk_pct"]["warn"] == 80 and seen[-1]["max_wakes_per_hour"] == 10
    assert "по последнему набору" in capsys.readouterr().err
    assert cfg["disk_pct"]["warn"] == 85  # config.json itself is never changed
    # no set received ever: config.json as it is
    st = state(sp)
    del st["cabinet_settings"]
    sp.write_text(json.dumps(st), encoding="utf-8")
    assert tick(cfg, sp, DAY + timedelta(minutes=10)) == 0
    assert seen[-1]["disk_pct"]["warn"] == 85


def test_a_set_the_monitor_cannot_take_never_breaks_the_tick(cfg, sp, monkeypatch, monitor_cabinet):
    seen = []
    disk_collect(monkeypatch, seen)
    monitor_cabinet.up = False
    st = state(sp)
    st["cabinet_settings"] = {"revision": 3, "items": [
        {"key": "disk_pct.warn", "server": "", "value": "lots"},  # a bad value: skipped
        {"key": "parallel", "server": "", "value": 1},  # not a setting: skipped
        {"key": "mem_psi.some_avg300", "server": "", "value": 20}]}
    sp.write_text(json.dumps(st), encoding="utf-8")
    assert tick(cfg, sp, DAY) == 0
    assert seen[-1]["disk_pct"]["warn"] == 85 and seen[-1]["parallel"] == cfg["parallel"]
    assert seen[-1]["mem_psi"]["some_avg300"] == 20
    st = state(sp)
    st["cabinet_settings"]["items"] = [{"key": "disk_pct.warn", "server": "", "value": 99}]  # crit 95: not stricter
    sp.write_text(json.dumps(st), encoding="utf-8")
    assert tick(cfg, sp, DAY + timedelta(minutes=5)) == 0
    assert seen[-1]["disk_pct"]["warn"] == 85


# --- AC7: a check switched off on a server ------------------------------------------------------------------------
def test_a_switched_off_check_does_not_run_and_health_keeps_quiet_about_it(cfg):
    calls = []

    def ssh(server, argv, timeout):
        calls.append(argv[0])
        if argv[0] == "uptime":
            return 0, " 14:00:00 up 1 day,  load average: 0.10, 0.10, 0.10\n", ""
        if argv[0] == "df":
            return 0, DF, ""
        return 1, "", "нет"

    def failing():
        return {"sec": {"fails": {"example-prod": {"certs": {"n": 9, "why": "x"}}}}}

    def health(res):
        return {o.subject for r in res if r.check == "health" for o in r.observations}

    on = runner.collect(cfg, DAY, failing(), 100.0, ssh=ssh, servers=["example-prod"], clock=lambda: 0.0)
    assert "df" in calls and "certbot" in calls and "certs" in health(on)
    assert any(r.check == "disk" and r.ran for r in on)
    off = dict(cfg, disabled_checks={"example-prod": ["disk", "certs"]})
    calls.clear()
    state = failing()
    res = runner.collect(off, DAY, state, 100.0, ssh=ssh, servers=["example-prod"], clock=lambda: 0.0)
    assert "df" not in calls and "certbot" not in calls and "uptime" in calls
    assert [r.ran for r in res if r.check == "disk"] == [False]
    assert "certs" not in health(res)
    assert "certs" not in state["sec"]["fails"]["example-prod"]  # switched on later, its count starts afresh


def test_switching_a_check_off_closes_its_problem_as_disabled_and_on_brings_it_back(
        cfg, sp, monkeypatch, monitor_cabinet, owner):
    disk_collect(monkeypatch)
    owner({"key": "disk_pct.warn", "value": 80}, signed=False)
    h = Session()
    tick(cfg, sp, DAY, h)
    assert incidents(monitor_cabinet)["P-1"]["status"] == "open"
    owner({"key": "disabled_checks", "server": "example-prod", "value": ["disk"]})
    ran = []

    def collect(cfg, now, state, deadline, **kw):  # the poll as the runner makes it: the switched-off check is out
        off = cfg.get("disabled_checks", {}).get("example-prod", [])
        ran.append("disk" not in off)
        return [] if "disk" in off else [
            CheckResult("example-prod", "disk", True, checks.disk("example-prod", DF, cfg, now))]
    monkeypatch.setattr(deliver, "collect", collect)
    h = Session()
    tick(cfg, sp, DAY + timedelta(minutes=5), h)
    assert ran == [False] and state(sp)["problems"] == {}
    assert h.calls == []  # the owner switched it off himself: nobody is woken
    assert state(sp)["recent"]["P-1"]["history"][-1]["kind"] == "disabled"
    assert incidents(monitor_cabinet)["P-1"]["status"] == "resolved"
    timeline = monitor_cabinet.env.client("root").call({"op": "incident.get", "incident": 1})["timeline"]
    assert timeline[-1]["kind"] == "incident.resolved" and timeline[-1]["payload"]["how"] == "disabled"
    # switched on: the check runs again and the problem opens anew
    owner({"key": "disabled_checks", "server": "example-prod", "reset": True}, signed=False)
    tick(cfg, sp, DAY + timedelta(minutes=10))
    assert ran[-1] is True
    assert state(sp)["problems"]["example-prod:disk:/"]["id"] == "P-2"


def test_a_disabled_close_goes_to_the_cabinet_as_resolve_disabled():
    p = {"key": "node-c:disk:/", "opened_at": DAY.isoformat()}
    items = mc.items_from([({"kind": "disabled", "at": DAY.isoformat(), "level": "warn", "value": "88%"}, p)])
    assert items[0]["kind"] == "resolve" and items[0]["how"] == "disabled"
