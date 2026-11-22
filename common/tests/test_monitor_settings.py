"""The catalog of the monitor's settings — keys, values, merge over config.json, weakening."""

import json
from pathlib import Path

import pytest

from kd import monitor_settings as ms

CONFIG = Path(__file__).resolve().parents[2] / "agents" / "sysadmin" / "tests" / "fixtures" / "monitor-config.json"
SERVERS = {"example-prod", "example-stage", "node-c", "node-l"}


@pytest.fixture
def base():
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def test_the_page_names_every_key_and_check_of_the_catalog_in_both_languages():
    messages = Path(__file__).resolve().parents[2] / "common" / "cabinet" / "web" / "messages"
    for language in ("ru", "en"):
        catalog = json.loads((messages / f"{language}.json").read_text(encoding="utf-8"))
        missing = [e["key"] for e in ms.describe() if f"msettings.key.{e['key']}" not in catalog]
        missing += [c for c in ms.CHECKS if f"msettings.check.{c}" not in catalog]
        assert missing == [], language


def test_every_global_key_of_the_catalog_is_in_the_shipped_config(base):
    missing = [e["key"] for e in ms.describe() if e["scope"] == "global" and ms.lookup(base, e["key"]) is ms.MISSING]
    assert missing == []
    assert ms.lookup(base, "mem_available_mb.warn", "node-l") == 100


@pytest.mark.parametrize("key", ["parallel", "tick_budget_s", "call_timeout_s", "message_max_chars", "web_dir",
                                 "fallback_after_min", "home_server", "close_after", "repeat_window_days",
                                 "morning_at", "vpn_nodes", "units_required", "mysql.server", "certs.servers",
                                 "security.critical_servers", "security.journal_servers", "backup.path",
                                 "monitoring.problems_file", "every_min", "disk_pct", "nonsense"])
def test_technical_and_where_keys_are_not_edited(key):
    with pytest.raises(ms.Invalid):
        ms.validate(key, "", 1, SERVERS)


@pytest.mark.parametrize("key,server,value", [
    ("disk_pct.warn", "", "85"),  # not a number
    ("disk_pct.warn", "", True),
    ("disk_pct.warn", "", 85.5),  # a whole number
    ("disk_pct.warn", "", 0),
    ("disk_pct.warn", "", 101),
    ("mem_psi.some_avg300", "", float("nan")),
    ("disk_pct.warn", "node-c", 80),  # a global key under a server
    ("mem_available_mb.warn", "", 100),  # a server key without a server
    ("mem_available_mb.warn", "ghost", 100),  # unknown server
    ("disabled_checks", "node-c", ["disk", "ghost"]),  # unknown check
    ("disabled_checks", "node-c", ["reach"]),  # reach never stops
    ("disabled_checks", "node-c", ["disk", "disk"]),
    ("critical_unreachable", "", ["example-prod", "ghost"]),
    ("units_ignore", "", "cron-*"),
    ("units_ignore", "", [""]),
    ("quiet_hours", "", {"start": "23:00"}),
    ("quiet_hours", "", {"start": "24:00", "end": "08:00"}),
])
def test_bad_values_are_refused(key, server, value):
    with pytest.raises(ms.Invalid):
        ms.validate(key, server, value, SERVERS)


def test_good_values_come_back_as_saved():
    assert ms.validate("disk_pct.warn", "", 80, SERVERS) == 80
    assert ms.validate("mem_psi.full_avg60", "", 2.5, SERVERS) == 2.5
    assert ms.validate("disabled_checks", "node-c", ["certs", "disk"], SERVERS) == ["certs", "disk"]
    assert ms.validate("quiet_hours", "", {"start": "22:00", "end": "07:30"}, SERVERS) == {"start": "22:00",
                                                                                         "end": "07:30"}
    # the monitor re-checks without the cabinet's server list: any well-formed alias
    assert ms.validate("critical_unreachable", "", ["example-prod", "new-node"], None) == ["example-prod",
                                                                                            "new-node"]


def test_crit_must_stay_stricter_than_warn(base):
    ms.check_config(base)
    for key, value in (("disk_pct.warn", 95), ("mem_available_pct.crit", 10), ("certs.crit_days", 14),
                       ("mysql.query_warn_s", 600), ("backup.warn_h", 50), ("security.load.warn_x", 4)):
        with pytest.raises(ms.Invalid):
            ms.check_config(ms.merge(base, [{"key": key, "server": "", "value": value}]))
    with pytest.raises(ms.Invalid):  # a server's MB thresholds: both, 0 < crit < warn
        ms.check_config(ms.merge(base, [{"key": "mem_available_mb.warn", "server": "node-c", "value": 100}]))
    with pytest.raises(ms.Invalid):
        ms.check_config(ms.merge(base, [{"key": "mem_available_mb.crit", "server": "node-l", "value": 100}]))


def test_merge_keeps_the_defaults_of_keys_without_an_edit_and_skips_stale_edits(base):
    edits = [{"key": "disk_pct.warn", "server": "", "value": 80},
             {"key": "disabled_checks", "server": "node-c", "value": ["certs"]},
             {"key": "mem_available_mb.warn", "server": "node-l", "value": 120},
             {"key": "parallel", "server": "", "value": 99},  # not a catalog key
             {"key": "every_min.certs", "server": "", "value": 720}]
    gone = dict(base, every_min={k: v for k, v in base["every_min"].items() if k != "certs"})
    merged = ms.merge(gone, edits)
    assert merged["disk_pct"] == {"warn": 80, "crit": 95}
    assert merged["disabled_checks"] == {"node-c": ["certs"]}
    assert merged["mem_available_mb"]["node-l"] == {"warn": 120, "crit": 50}
    assert merged["parallel"] == base["parallel"]
    assert "certs" not in merged["every_min"]  # the release no longer has the key: the edit is stale
    assert ms.stale(gone, "every_min.certs") and not ms.stale(base, "every_min.certs")
    assert gone == dict(base, every_min=gone["every_min"])  # the defaults themselves stay untouched


@pytest.mark.parametrize("key,old,new", [
    ("disk_pct.warn", 85, 90),
    ("disk_pct.crit", 95, 99),
    ("mem_available_pct.warn", 10, 8),
    ("certs.warn_days", 14, 7),
    ("security.eol.warn_days", 90, 30),
    ("mem_available_mb.warn", 100, 80),
    ("every_min.certs", 360, 720),
    ("unreachable_after", 3, 5),
    ("max_wakes_per_hour", 6, 3),
    ("units_ignore", ["digipaddock-monitor.service"], ["digipaddock-monitor.service", "cron-*"]),
    ("units_confirm", ["cron-*"], ["cron-*", "x.service"]),
    ("security.units_ignore", [], ["snap.*"]),
    ("security.ports_ignore", [], ["tcp:8080"]),
    ("mysql.ignore_users", ["system user"], ["system user", "backup"]),
    ("heartbeat.ignore_prefix", ["mobile-"], ["mobile-", "test-"]),
    ("monitoring.ignore_id_prefixes", ["disk:"], ["disk:", "vpn:"]),
    ("critical_unreachable", ["example-prod", "example-stage"], ["example-prod"]),
    ("disabled_checks", [], ["disk"]),
    ("disabled_checks", ms.MISSING, ["disk"]),
    ("quiet_hours", {"start": "23:00", "end": "08:00"}, {"start": "22:59", "end": "08:00"}),
    ("quiet_hours", {"start": "23:00", "end": "08:00"}, {"start": "01:00", "end": "09:00"}),  # shorter, but a new hour
    ("mem_available_mb.warn", ms.MISSING, 100),  # a threshold appears: which way is unknown — signed
    ("mem_available_mb.warn", 100, ms.MISSING),
])
def test_each_kind_of_weakening(key, old, new):
    assert ms.weakens(key, old, new)


@pytest.mark.parametrize("key,old,new", [
    ("disk_pct.warn", 85, 80),
    ("mem_available_pct.crit", 5, 6),
    ("certs.crit_days", 3, 5),
    ("every_min.certs", 360, 60),
    ("max_wakes_per_hour", 6, 10),
    ("units_ignore", ["a", "b"], ["a"]),
    ("critical_unreachable", ["example-prod"], ["example-prod", "node-c"]),
    ("disabled_checks", ["disk", "certs"], ["certs"]),
    ("disabled_checks", ["disk"], ms.MISSING),
    ("quiet_hours", {"start": "23:00", "end": "08:00"}, {"start": "00:00", "end": "06:00"}),
    ("quiet_hours", {"start": "23:00", "end": "08:00"}, {"start": "23:00", "end": "23:00"}),  # none at all
    ("disk_pct.warn", 85, 85),
])
def test_tightening_and_no_change_are_not_weakening(key, old, new):
    assert not ms.weakens(key, old, new)


@pytest.mark.parametrize("check,subject,job", [
    ("disk", "/", "disk"), ("sec:users", "x", "sec"), ("posture", "-", "sec"), ("gate:certs", "-", "certs"),
    ("health", "updates", "updates"), ("health", "sec", "sec"), ("monitoring_file", "x", "monitoring"),
    ("monitoring_alive", "-", "monitoring"), ("reach", "-", None), ("load", "-", None), ("gate:reach", "-", None),
])
def test_a_problem_belongs_to_the_check_that_switches_it_off(check, subject, job):
    assert ms.job_of(check, subject) == job
