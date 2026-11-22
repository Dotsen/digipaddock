import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from gate import agent_ro_gate as gate
from sysadmin.monitor import checks, runner
from sysadmin.monitor.model import load_config

UTC3 = timezone(timedelta(hours=3))
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC3)
CFG = load_config(Path(__file__).resolve().parent / "fixtures" / "monitor-config.json")


def lv(observations):
    return {o.subject: o.level for o in observations}


# --- reach ----------------------------------------------------------------------

def test_server_shift_from_uptime_clock():
    up = " 09:00:12 up 3 days,  2:05,  1 user,  load average: 0.10, 0.05, 0.01\n"
    assert checks.server_shift(up, NOW) == timedelta(hours=-3)
    assert checks.server_shift(" 12:07:12 up 1 day,  1 user", NOW) == timedelta(0)  # 7 min → rounded to 0
    assert checks.server_shift(" 12:08:12 up 1 day", NOW) == timedelta(minutes=15)
    assert checks.server_shift("странный вывод", NOW) == timedelta(0)
    assert checks.server_shift(" 01:00:00 up 1 day", NOW) == timedelta(hours=-11)


def test_unreachable_threshold_and_level():
    assert checks.unreachable("node-f", 2, CFG) == []
    assert lv(checks.unreachable("node-f", 3, CFG)) == {"-": "warn"}
    assert lv(checks.unreachable("example-prod", 3, CFG)) == {"-": "crit"}


# --- disk / mem -----------------------------------------------------------------

DF = """Filesystem     1024-blocks      Used Available Capacity Mounted on
/dev/sda1         41152812  39500000   1000000      96% /
/dev/sdb1        100000000  86000000  14000000      86% /var/lib/docker
/dev/sdc1        100000000  10000000  90000000      10% /data
"""


def test_disk_levels():
    res = checks.disk("srv", DF, CFG, NOW)
    assert lv(res) == {"/": "crit", "/var/lib/docker": "warn", "/data": "ok"}
    assert res[0].key == "srv:disk:/"


def test_disk_edges():
    out = "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/a 1 1 1 85% /a\n/dev/b 1 1 1 95% /b\n/dev/c 1 1 1 84% /c\n"
    assert lv(checks.disk("s", out, CFG, NOW)) == {"/a": "warn", "/b": "crit", "/c": "ok"}


MEMINFO = """MemTotal:       16000000 kB
MemFree:          200000 kB
MemAvailable:     %d kB
SwapTotal:       %d kB
SwapFree:        %d kB
"""


def test_mem_levels():
    assert lv(checks.mem("s", MEMINFO % (640000, 0, 0), CFG, NOW)) == {"ram": "crit", "swap": "ok"}  # 4%
    assert lv(checks.mem("s", MEMINFO % (1280000, 0, 0), CFG, NOW))["ram"] == "warn"  # 8%
    assert lv(checks.mem("s", MEMINFO % (8000000, 0, 0), CFG, NOW))["ram"] == "ok"


def test_mem_absolute_threshold_per_server():
    """node-l (1 GB) normally keeps 110–130 MB free — its threshold is in MB, the others keep their percentages."""
    assert CFG["mem_available_mb"]["node-l"] == {"warn": 100, "crit": 50}
    node_l = "MemTotal:  1000000 kB\nMemAvailable:  %d kB\nSwapTotal: 0 kB\n"
    for kb, level in ((120 * 1024, "ok"), (110 * 1024, "ok"), (90 * 1024, "warn"), (40 * 1024, "crit")):
        obs = checks.mem("node-l", node_l % kb, CFG, NOW)
        assert lv(obs)["ram"] == level, kb
    assert subj(checks.mem("node-l", node_l % (90 * 1024), CFG, NOW), "ram").text == "свободной памяти 90 МБ (9%; порог 100 МБ)"
    assert lv(checks.mem("node-f", node_l % (120 * 1024), CFG, NOW))["ram"] == "ok"  # 12% > 10%
    assert lv(checks.mem("node-f", node_l % (90 * 1024), CFG, NOW))["ram"] == "warn"  # 9% — percentages, as before


@pytest.mark.parametrize("bad", [{"node-l": {"warn": 100}}, {"node-l": {"warn": 50, "crit": 50}},
                                 {"node-l": {"warn": "100", "crit": 50}}, {"node-l": {"warn": 100, "crit": 0}},
                                 {"node-l": {"warn": True, "crit": 50}}, ["node-l"]])
def test_mem_absolute_threshold_bad_config_refused(tmp_path, bad):
    cfg = dict(CFG, mem_available_mb=bad)
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    with pytest.raises(ValueError, match="mem_available_mb"):
        load_config(p)


def test_parsing_fail_not_forwarded():
    """parsing_fail: prod's monitoring_alert.py itself sends it to Telegram from the monitoring file."""
    assert "parsing_fail:" in CFG["monitoring"]["ignore_id_prefixes"]


VMSTAT = "nr_free_pages 50000\npswpin %d\npswpout %d\n"
PSI = ("some avg10=0.00 avg60=0.00 avg300=%.2f total=1\n"
       "full avg10=0.00 avg60=%.2f avg300=0.00 total=1\n")


def subj(observations, subject):
    return next(o for o in observations if o.subject == subject)


def test_mem_swap_full_is_context_not_alert():
    # node-c 05.10: an idle redroid holds swap 2/2 GB, there is enough memory — not a problem
    obs = checks.mem("s", MEMINFO % (8000000, 2097148, 3624), CFG, NOW)
    assert lv(obs) == {"ram": "ok", "swap": "ok"}
    assert subj(obs, "swap").value == "100%"


def test_mem_psi():
    out = MEMINFO % (8000000, 0, 0)
    assert lv(checks.mem("s", out, CFG, NOW, psi=PSI % (10.5, 0)))["psi"] == "warn"  # some avg300 > 10
    assert lv(checks.mem("s", out, CFG, NOW, psi=PSI % (0, 5.5)))["psi"] == "warn"  # full avg60 > 5
    assert lv(checks.mem("s", out, CFG, NOW, psi=PSI % (10, 5)))["psi"] == "ok"  # exactly at the thresholds
    assert "psi" not in lv(checks.mem("s", out, CFG, NOW))  # a kernel without PSI (AlmaLinux 8)
    assert "psi" not in lv(checks.mem("s", out, CFG, NOW, psi="мусор"))


def test_mem_swap_io():
    out = MEMINFO % (8000000, 4000000, 0)
    assert lv(checks.mem("s", out, CFG, NOW, swap_rate=(1001.0, 10.0)))["swapio"] == "warn"
    assert lv(checks.mem("s", out, CFG, NOW, swap_rate=(1000.0, 10.0)))["swapio"] == "ok"  # exactly the threshold
    assert "swapio" not in lv(checks.mem("s", out, CFG, NOW))  # the window has not filled yet
    o = subj(checks.mem("s", out, CFG, NOW, swap_rate=(2500.0, 15.0)), "swapio")
    assert o.value == "2500 стр/с"
    assert "в среднем за 15 мин" in o.text and "swap занят на 100%" in o.text


def test_swap_rate_window():
    def ago(m):
        return (NOW - timedelta(minutes=m)).isoformat()

    def out(n):
        return MEMINFO % (8000000, 0, 0) + VMSTAT % (0, n)

    rate, s = checks.swap_rate(None, out(1000), NOW, CFG)
    assert rate is None and s == [[NOW.isoformat(), 1000]]  # the first sample
    assert checks.swap_rate([[ago(5), 0]], out(1000), NOW, CFG)[0] is None  # younger than the 10 min window
    # the base is the freshest sample no younger than the window: 10 min ago 300000 → 600000 pages over 600 s
    rate, s = checks.swap_rate([[ago(15), 0], [ago(10), 300000], [ago(5), 500000]], out(900000), NOW, CFG)
    assert rate == (1000.0, 10.0)
    assert s == [[ago(15), 0], [ago(10), 300000], [ago(5), 500000], [NOW.isoformat(), 900000]]
    # older than two windows — dropped; a smaller counter (a reboot) — dropped; junk — skipped
    rate, s = checks.swap_rate([[ago(25), 0], [ago(12), 5000], ["x", 1], [ago(11)], None], out(100), NOW, CFG)
    assert rate is None and s == [[NOW.isoformat(), 100]]
    assert checks.swap_rate([[ago(10), 0]], MEMINFO % (8000000, 0, 0), NOW, CFG) == (None, [])  # no counters
    assert checks.swap_rate(7, out(5), NOW, CFG) == (None, [[NOW.isoformat(), 5]])  # a corrupted state


# --- oom --------------------------------------------------------------------------

DMESG = """[Sat Oct  3 02:11:09 2026] Out of memory: Killed process 777 (java)
[Sun Oct  4 10:00:00 2026] Out of memory: Killed process 4242 (php-fpm) total-vm:1234kB
[Sun Oct  4 10:00:01 2026] oom_reaper: reaped process 4242 (php-fpm)
[Mon Oct  5 09:30:00 2026] eth0: link up
"""


def test_oom_uses_server_clock_shift():
    # the server is in UTC (UTC+3 − 3 h): 10:00 UTC = 13:00 UTC+3 on 4 October — inside 24 h
    res = checks.oom("s", DMESG, CFG, NOW, shift=timedelta(hours=-3))
    assert lv(res) == {"-": "warn"} and res[0].value == "1"
    assert "php-fpm" in res[0].text
    # without the shift the same line (10:00) is older than 24 h
    assert lv(checks.oom("s", DMESG, CFG, NOW)) == {"-": "ok"}


BERLIN = ZoneInfo("Europe/Berlin")
AFTER_DST = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)  # Berlin left summer time at 01:00 UTC that day


def test_oom_reads_a_line_from_before_the_hour_change_in_the_servers_zone():
    """10.9: 13:30 in Berlin on the 24th is 11:30 UTC (summer time) — 24.5 h ago; now's shift alone makes it 23.5 h."""
    out = "[Sat Oct 24 13:30:00 2026] Out of memory: Killed process 1 (x)\n"
    shift = timedelta(hours=-2)  # Berlin now (+1) less the host (+3)
    assert lv(checks.oom("s", out, CFG, AFTER_DST, shift=shift, zone=BERLIN)) == {"-": "ok"}
    assert lv(checks.oom("s", out, CFG, AFTER_DST, shift=shift)) == {"-": "warn"}  # no zone in the inventory


def test_an_applications_naive_time_is_in_the_servers_zone():
    assert checks._parse_ts("2026-10-24T13:30:00", BERLIN) == datetime(2026, 10, 24, 11, 30, tzinfo=timezone.utc)
    assert checks._parse_ts("2026-10-24T13:30:00") == datetime(2026, 10, 24, 10, 30, tzinfo=timezone.utc)  # the host's
    assert checks._parse_ts("2026-10-24T13:30:00+00:00", BERLIN) == datetime(2026, 10, 24, 13, 30, tzinfo=timezone.utc)


def test_oom_kill_line_and_empty():
    out = "[Mon Oct  5 11:00:00 2026] oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null)\n"
    assert lv(checks.oom("s", out, CFG, NOW)) == {"-": "warn"}
    assert lv(checks.oom("s", "", CFG, NOW)) == {"-": "ok"}


# --- units / wg -------------------------------------------------------------------

def test_units_filter_and_levels():
    out = ("danted.service loaded failed failed SOCKS (v4/v5) proxy server\n"
           "digipaddock-alert@digipaddock-board.service loaded failed failed App alert\n"
           "digipaddock-monitor.service loaded failed failed Монитор\n")
    res = checks.units("node-b", out, CFG, NOW)
    assert lv(res) == {"danted.service": "warn"}
    assert "SOCKS" in res[0].text
    assert checks.units("s", "", CFG, NOW) == []


def test_units_bullet_prefix():
    assert lv(checks.units("s", "● nginx.service loaded failed failed A web server\n", CFG, NOW)) == {"nginx.service": "warn"}


STAGE_REQUIRED = ["hindsight-api.service", "hindsight-worker.service", "postgresql@18-main.service",
                  "digipaddock-cabinet.socket", "digipaddock-cabinet-http.socket", "digipaddock-cabinet-push.service",
                  "digipaddock-session-host.socket", "digipaddock-review.socket",
                  "digipaddock-board-sync.path", "digipaddock-board-sync.timer", "digipaddock-board-watch.timer", "digipaddock-board-deliver.timer",
                  "digipaddock-repo-sync.timer", "digipaddock-cabinet-export.timer", "digipaddock-usage.timer"]


def test_units_required_config_stage_only():
    """Exactly the 15 required units of example-stage from the brief, no other servers."""
    assert CFG["units_required"] == {"example-stage": STAGE_REQUIRED}
    assert checks.required_units("example-stage", CFG) == STAGE_REQUIRED
    assert checks.required_units("example-prod", CFG) == []


def _show(**states):
    """The output of systemctl show -p Id,LoadState,ActiveState: {unit: "load/active"} (the properties in systemd's order)."""
    blocks = []
    for name, st in states.items():
        load, active = st.split("/")
        blocks.append(f"Id={name}\nLoadState={load}\nActiveState={active}\n")
    return "\n".join(blocks)


def test_unit_states_parses_blocks_and_skips_junk():
    out = _show(**{"a.service": "loaded/active", "b.timer": "not-found/inactive"}) + "\n[agent-ro-gate: x]\n\nmusor\n"
    assert checks.unit_states(out) == {
        "a.service": {"Id": "a.service", "LoadState": "loaded", "ActiveState": "active"},
        "b.timer": {"Id": "b.timer", "LoadState": "not-found", "ActiveState": "inactive"}}
    assert checks.unit_states("") == {}


def test_required_obs_thresholds_and_texts():
    cfg = dict(CFG, units_required={"s": ["a.service", "b.timer", "c.socket", "d.service"]})
    states = checks.unit_states(_show(**{"a.service": "loaded/inactive", "b.timer": "not-found/inactive",
                                         "c.socket": "loaded/failed", "d.service": "loaded/active"}))
    one = {"a.service": 1, "b.timer": 1, "c.socket": 1}
    assert lv(checks.required_obs("s", states, one, cfg)) == {"c.socket": "warn"}  # failed — at once
    two = {"a.service": 2, "b.timer": 2, "c.socket": 2}
    res = {o.subject: o for o in checks.required_obs("s", states, two, cfg)}
    assert sorted(res) == ["a.service", "b.timer", "c.socket"]
    assert res["a.service"].text == "юнит a.service остановлен (inactive)" and res["a.service"].level == "warn"
    assert res["b.timer"].text == "юнит b.timer не найден"
    assert res["c.socket"].text == "юнит c.socket упал"
    # an already open problem holds without the threshold; a unit from list-units --failed is not reported a second time
    assert lv(checks.required_obs("s", states, one, cfg, open_keys={"s:units:a.service": {}})) == \
        {"a.service": "warn", "c.socket": "warn"}
    assert lv(checks.required_obs("s", states, two, cfg, skip={"c.socket"})) == {"a.service": "warn", "b.timer": "warn"}


def test_units_ignore_does_not_hide_required():
    """A units_ignore glob that matches a required unit does not hide it."""
    cfg = dict(CFG, units_ignore=["digipaddock-*"], units_required={"s": ["digipaddock-cabinet.socket"]})
    out = "digipaddock-cabinet.socket loaded failed failed Cabinet\ndigipaddock-other.service loaded failed failed X\n"
    assert lv(checks.units("s", out, cfg, NOW)) == {"digipaddock-cabinet.socket": "warn"}
    assert lv(checks.units("t", out, cfg, NOW)) == {}


def _wg(*stamps, iface="wg0"):
    return "".join(f"{iface}\tKEY{i}=\t{t}\n" for i, t in enumerate(stamps))


def test_wg_all_zero_warn():
    assert lv(checks.wg("node-d", _wg(0, 0, 0), CFG, NOW)) == {"wg0": "warn"}


def test_wg_stale_and_fresh():
    t = int(NOW.timestamp())
    assert lv(checks.wg("n", _wg(t - 600, t - 900), CFG, NOW)) == {"wg0": "warn"}  # older than 5 min
    assert lv(checks.wg("n", _wg(t - 600, t - 30, 0), CFG, NOW)) == {"wg0": "ok"}  # at least one fresh
    both = _wg(0, iface="wg0") + _wg(t - 10, iface="wg1")
    assert lv(checks.wg("n", both, CFG, NOW)) == {"wg0": "warn", "wg1": "ok"}


# --- heartbeat ----------------------------------------------------------------------

ALIASES = {"node-a", "node-f", "node-c", "example-prod", "example-stage"}


def test_heartbeat_mapping():
    out = ("node-a\t232068\nNodeC\t39\nmobile-kit3\t9999\nprod_01\t3\nnode-f\t4\n"
           "stranger\t500\nnode-z9\t500\n")
    res = checks.heartbeat("example-prod", out, CFG, NOW, aliases=ALIASES)
    got = {o.server: o.level for o in res}
    assert got == {"node-a": "warn", "node-c": "ok", "example-prod": "ok", "node-f": "ok"}
    assert all(o.check == "heartbeat" and o.subject == "-" for o in res)
    assert res[0].key == "node-a:heartbeat:-"


def test_heartbeat_thresholds():
    """prod_01 sends its report at the end of a run every 15 min, NodeC — once an hour: the threshold = the period + a margin."""
    out = "node-f\t16\nNodeC\t61\nnode-a\t15\nprod_01\t20\n"
    got = {o.server: o.level for o in checks.heartbeat("p", out, CFG, NOW, aliases=ALIASES)}
    assert got == {"node-f": "warn", "node-c": "ok", "node-a": "ok", "example-prod": "ok"}
    out = "NodeC\t76\nprod_01\t21\n"
    got = {o.server: o.level for o in checks.heartbeat("p", out, CFG, NOW, aliases=ALIASES)}
    assert got == {"node-c": "warn", "example-prod": "warn"}


# --- mysql --------------------------------------------------------------------------

def _mysql(proc, cur="25", mx="500"):
    return checks.MYSQL_SEP.join([proc, f"Threads_connected\t{cur}\n", f"max_connections\t{mx}\n"])


PROC_QUIET = ("5\tevent_scheduler\tDaemon\t146002\tWaiting on empty queue\n"
              "9\tapp\tSleep\t300\t\n"
              "11\tapp\tQuery\t2\tSending data\n")


def test_mysql_quiet():
    res = checks.mysql("example-prod", _mysql(PROC_QUIET), CFG, NOW)
    assert lv(res) == {"query": "ok", "mdl": "ok", "conn": "ok", "up": "ok"}


def test_mysql_long_query_levels():
    warn = _mysql("12\tapp\tQuery\t121\tSending data\n")
    crit = _mysql("12\tapp\tQuery\t601\tSending data\n")
    assert lv(checks.mysql("p", warn, CFG, NOW))["query"] == "warn"
    assert lv(checks.mysql("p", crit, CFG, NOW))["query"] == "crit"
    assert lv(checks.mysql("p", _mysql("12\tapp\tQuery\t120\tx\n"), CFG, NOW))["query"] == "ok"


def test_mysql_metadata_lock():
    mdl40 = _mysql("12\tapp\tQuery\t40\tWaiting for table metadata lock\n")
    mdl10 = _mysql("12\tapp\tQuery\t10\tWaiting for table metadata lock\n")
    r40 = lv(checks.mysql("p", mdl40, CFG, NOW))
    assert r40["mdl"] == "crit" and r40["query"] == "ok"
    assert lv(checks.mysql("p", mdl10, CFG, NOW))["mdl"] == "ok"


def test_mysql_connections():
    assert lv(checks.mysql("p", _mysql("", "401", "500"), CFG, NOW))["conn"] == "warn"
    assert lv(checks.mysql("p", _mysql("", "400", "500"), CFG, NOW))["conn"] == "ok"


def test_mysql_down_observation():
    res = checks.mysql_down("example-prod", CFG)
    assert lv(res) == {"up": "crit"} and "MySQL недоступен" in res[0].text


# --- certs / backup -------------------------------------------------------------------

CERTBOT = """Found the following certs:
  Certificate Name: example.test
    Serial Number: 4a1b
    Key Type: ECDSA
    Domains: example.test www.example.test
    Expiry Date: 2026-12-01 08:00:00+00:00 (VALID: 56 days)
    Certificate Path: /etc/letsencrypt/live/example.test/fullchain.pem
  Certificate Name: api.example.test
    Domains: api.example.test
    Expiry Date: 2026-10-07 08:00:00+00:00 (VALID: 2 days)
  Certificate Name: old.example.test
    Expiry Date: 2026-09-01 08:00:00+00:00 (INVALID: EXPIRED)
  Certificate Name: soon.example.test
    Expiry Date: 2026-10-15 08:00:00+00:00 (VALID: 10 days)
"""


def test_certs_levels():
    assert lv(checks.certs("example-prod", CERTBOT, CFG, NOW)) == {
        "example.test": "ok", "api.example.test": "crit", "old.example.test": "crit", "soon.example.test": "warn"}


def test_certs_none():
    assert checks.certs("s", "No certificates found.\n", CFG, NOW) == []


def _ls(hours_ago):
    ts = int(NOW.timestamp() - hours_ago * 3600)
    return (f"total 9000\ndrwxr-xr-x 2 root root 4096 {ts + 100} archive\n"
            f"-rw-r--r-- 1 root root 5242880 {ts - 90000} db-old.sql.gz\n"
            f"-rw-r--r-- 1 root root 5242880 {ts} db.sql.gz\n")


def test_backup_levels():
    assert lv(checks.backup("node-b", _ls(30), CFG, NOW)) == {"-": "warn"}
    assert lv(checks.backup("node-b", _ls(10), CFG, NOW)) == {"-": "ok"}
    assert lv(checks.backup("node-b", _ls(51), CFG, NOW)) == {"-": "crit"}


def test_backup_directories_ignored_and_empty():
    only_dir = f"total 4\ndrwxr-xr-x 2 root root 4096 {int(NOW.timestamp())} archive\n"
    assert lv(checks.backup("n", only_dir, CFG, NOW)) == {"-": "crit"}  # a fresh directory does not count
    assert lv(checks.backup("n", "total 0\n", CFG, NOW)) == {"-": "crit"}


# --- application monitoring ------------------------------------------------------------

def _pfile(minutes_ago, problems=None):
    ts = (NOW - timedelta(minutes=minutes_ago)).isoformat()
    probs = problems if problems is not None else [
        {"id": "vpn_country:ZZ", "level": "critical", "text": "VPN: ❌ZZ 0/6 (key: abc)"},
        {"id": "no_status", "level": "warn", "text": "no_status по 12 парам"},
        {"id": "disk:kit2:/", "level": "warn", "text": "диск 91%"},
        {"id": "api_health:https://api.example.test/health", "level": "critical", "text": "HTTP 502"},
    ]
    raw = json.dumps({"ts": ts, "problems": probs}, ensure_ascii=False, indent=1)
    return raw, ts


def test_monitoring_file_survives_gate_redaction_and_levels():
    raw, ts = _pfile(5)
    redacted = gate.redact(raw.encode("utf-8")).decode("utf-8")
    assert '"id": "vpn_country:ZZ"' in redacted  # the id is intact
    res = checks.monitoring_file("example-prod", redacted, CFG, NOW)
    assert lv(res) == {"vpn_country:ZZ": "crit", "no_status": "warn",
                       "api_health:https://api.example.test/health": "crit"}  # disk: — ignored
    assert res[0].key == "example-prod:monitoring_file:vpn_country:ZZ"


def test_monitoring_file_same_ts_none():
    raw, ts = _pfile(5)
    assert checks.monitoring_file("p", raw, CFG, NOW, last_ts=ts) is None
    assert checks.monitoring_file("p", raw, CFG, NOW, last_ts="другой") is not None


def test_monitoring_file_stale_or_broken_none():
    raw, _ = _pfile(40)
    assert checks.monitoring_file("p", raw, CFG, NOW) is None
    assert checks.monitoring_file("p", "{не json", CFG, NOW) is None


def test_monitoring_file_empty_list_is_valid():
    raw, _ = _pfile(1, problems=[])
    assert checks.monitoring_file("p", raw, CFG, NOW) == []


def test_alive_from_file():
    assert lv(checks.monitoring_alive("p", _pfile(5)[0], CFG, NOW)) == {"-": "ok"}
    stale = checks.monitoring_alive("p", _pfile(40)[0], CFG, NOW)
    assert lv(stale) == {"-": "crit"} and "мониторинг молчит" in stale[0].text
    broken = checks.monitoring_alive("p", "{oops", CFG, NOW)
    assert lv(broken) == {"-": "warn"} and "не читается" in broken[0].text


def test_alive_from_log():
    def log(minutes_ago):
        t = (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")
        return (f"{t} INFO monitoring_alert: start\n{t} INFO monitoring_alert: ok\n")
    assert lv(checks.monitoring_alive("p", log(7), CFG, NOW, source="log")) == {"-": "ok"}
    assert lv(checks.monitoring_alive("p", log(90), CFG, NOW, source="log")) == {"-": "crit"}
    assert lv(checks.monitoring_alive("p", "пусто\n", CFG, NOW, source="log")) == {"-": "crit"}


def test_consumers():
    assert lv(checks.consumers("p", "16\n", CFG, NOW)) == {"-": "crit"}
    assert lv(checks.consumers("p", "15\n", CFG, NOW)) == {"-": "ok"}
    assert lv(checks.consumers("p", "NULL\n", CFG, NOW)) == {"-": "crit"}
    assert checks.consumers("p", "", CFG, NOW) == []


def test_clean_ignores_gate_and_apt_lines():
    out = "WARNING: apt does not have a stable CLI interface.\nMemTotal: 100 kB\n[agent-ro-gate: скрыто секретов: 2]\n"
    assert checks.clean(out).strip() == "MemTotal: 100 kB"


# --- runner -------------------------------------------------------------------------------

class FakeSsh:
    """A fake ssh-ro: answers by the command line (or by a prefix), a log of the calls."""

    def __init__(self, table, default=(0, "", "")):
        self.table, self.default, self.calls = table, default, []

    def __call__(self, alias, argv, timeout):
        cmd = " ".join(argv)
        self.calls.append((alias, cmd, timeout))
        for key in ((alias, cmd), alias, cmd):
            if key in self.table:
                v = self.table[key]
                return v(cmd) if callable(v) else v
        for key, v in self.table.items():
            if isinstance(key, tuple) and key[0] == alias and cmd.startswith(key[1]):
                return v
        return self.default


def fresh_state():
    return {"problems": {}, "unreachable": {}, "monitoring_ts": None, "last_run": None}


def far():
    return runner.time.monotonic() + 1000


UPTIME = (0, " 12:00:05 up 3 days,  1 user,  load average: 0.1, 0.1, 0.1\n", "")


def find(results, server, check):
    return [r for r in results if r.server == server and r.check == check]


def test_collect_reach_ok_and_checks_run():
    ssh = FakeSsh({"uptime": UPTIME, ("srv", "df"): (0, DF, "")})
    st = fresh_state()
    res = runner.collect(CFG, NOW, st, far(), ssh=ssh, servers=["srv"])
    assert find(res, "srv", "reach")[0].ran and st["unreachable"]["srv"] == 0
    disk = find(res, "srv", "disk")[0]
    assert disk.ran and lv(disk.observations)["/"] == "crit"
    assert st["last_run"]["srv:disk"] == NOW.isoformat()
    # an ordinary server has no special checks
    assert not find(res, "srv", "wg") and not find(res, "srv", "certs")


def test_collect_uses_specific_commands():
    ssh = FakeSsh({})
    runner.collect(CFG, NOW, fresh_state(), far(), ssh=ssh, servers=["node-b", "example-prod"])
    cmds = {(a, c) for a, c, _ in ssh.calls}
    assert ("node-b", "wg show all latest-handshakes") in cmds
    assert ("node-b", "ls -l --time-style=+%s /backups") in cmds
    assert ("node-b", "systemctl list-units --failed --no-legend --plain") in cmds
    assert ("node-b", "dmesg -T --level=err,warn") in cmds
    assert ("example-prod", "certbot certificates") in cmds
    assert ("example-prod", "cat /var/log/app/monitoring_problems.json") in cmds
    assert not any(a == "example-prod" and c == "wg show all latest-handshakes" for a, c, _ in ssh.calls)


def test_collect_cron_unit_needs_two_ticks_in_a_row():
    """A cron unit that failed on one run and came up on the next opens no problem (node-j)."""
    units = "systemctl list-units --failed --no-legend --plain"
    cron = "cron-vpn-node-health-root-0.service loaded failed failed [Cron] health.py\n"
    plain = "nginx.service loaded failed failed nginx\n"

    def tick(st, out, i):
        res = runner.collect(CFG, NOW + timedelta(minutes=5 * i), st, far(),
                             ssh=FakeSsh({"uptime": UPTIME, ("a", units): (0, out, "")}), servers=["a"])
        return sorted(o.subject for o in find(res, "a", "units")[0].observations)

    st = fresh_state()
    assert tick(st, cron + plain, 0) == ["nginx.service"]  # an ordinary unit — at once
    assert tick(st, "", 1) == []  # cron's next run passed
    assert tick(st, cron, 2) == []
    assert tick(st, cron, 3) == ["cron-vpn-node-health-root-0.service"]  # two ticks in a row
    st2 = fresh_state()  # an already open problem holds without the "first time"
    st2["problems"]["a:units:cron-vpn-node-health-root-0.service"] = {"id": "P-1"}
    assert tick(st2, cron, 0) == ["cron-vpn-node-health-root-0.service"]


UNITS_SHOW_REQ = "systemctl show -p Id,LoadState,ActiveState"


def _req_tick(st, show, i, failed="", cfg=None, server="example-stage"):
    cfg = cfg or dict(CFG, units_required={server: ["hindsight-worker.service", "digipaddock-usage.timer"]})
    units = "systemctl list-units --failed --no-legend --plain"
    ssh = FakeSsh({"uptime": UPTIME, (server, units): (0, failed, ""), (server, UNITS_SHOW_REQ): show})
    res = runner.collect(cfg, NOW + timedelta(minutes=5 * i), st, far(), ssh=ssh, servers=[server])
    return res, ssh


def _ok_show(**states):
    return (0, _show(**states), "")


def test_collect_required_unit_stopped_two_ticks_then_closes():
    """A stopped required unit — warn from the second tick in a row; active closes it by the rules."""
    from sysadmin.monitor.registry import Registry, _new_state
    stopped = _ok_show(**{"hindsight-worker.service": "loaded/inactive", "digipaddock-usage.timer": "loaded/active"})
    up = _ok_show(**{"hindsight-worker.service": "loaded/active", "digipaddock-usage.timer": "loaded/active"})
    st = _new_state(NOW.date())
    registry = Registry(st, close_after=CFG["close_after"], every_min=CFG["every_min"])

    def observe(res):  # into the registry — only the source units
        return registry.observe([r for r in res if r.check == "units"], NOW)

    res, ssh = _req_tick(st, stopped, 0)
    assert ("example-stage", UNITS_SHOW_REQ + " hindsight-worker.service digipaddock-usage.timer") in \
        {(a, c) for a, c, _ in ssh.calls}
    assert find(res, "example-stage", "units")[0].observations == []  # one tick — no problem
    assert observe(res) == []
    res, _ = _req_tick(st, stopped, 1)
    obs = find(res, "example-stage", "units")[0].observations
    assert [(o.subject, o.level, o.text) for o in obs] == \
        [("hindsight-worker.service", "warn", "юнит hindsight-worker.service остановлен (inactive)")]
    assert [e.kind for e in observe(res)] == ["open"]
    res, _ = _req_tick(st, stopped, 2)  # not fixed — it holds
    assert observe(res) == []
    kinds = []
    for i in range(3, 3 + CFG["close_after"]):
        res, _ = _req_tick(st, up, i)
        assert find(res, "example-stage", "units")[0].ran
        kinds += [e.kind for e in observe(res)]
    assert kinds == ["close"] and not st["problems"]


def test_collect_required_unit_one_tick_flap_no_problem():
    st = fresh_state()
    stopped = _ok_show(**{"hindsight-worker.service": "loaded/activating", "digipaddock-usage.timer": "loaded/active"})
    up = _ok_show(**{"hindsight-worker.service": "loaded/active", "digipaddock-usage.timer": "loaded/active"})
    for i, show in enumerate((stopped, up, stopped, up)):
        res, _ = _req_tick(st, show, i)
        assert find(res, "example-stage", "units")[0].observations == []


def test_collect_required_unit_not_found_two_ticks():
    """A required unit not found on two ticks running."""
    st = fresh_state()
    show = _ok_show(**{"hindsight-worker.service": "loaded/active", "digipaddock-usage.timer": "not-found/inactive"})
    assert find(_req_tick(st, show, 0)[0], "example-stage", "units")[0].observations == []
    obs = find(_req_tick(st, show, 1)[0], "example-stage", "units")[0].observations
    assert [(o.subject, o.value, o.text) for o in obs] == [("digipaddock-usage.timer", "not-found", "юнит digipaddock-usage.timer не найден")]


def test_collect_required_unit_failed_at_once_single_observation():
    """failed — at once; list-units --failed and systemctl show give one problem per unit."""
    st = fresh_state()
    show = _ok_show(**{"hindsight-worker.service": "loaded/failed", "digipaddock-usage.timer": "loaded/active"})
    failed = "hindsight-worker.service loaded failed failed Hindsight worker\n"
    obs = find(_req_tick(st, show, 0, failed=failed)[0], "example-stage", "units")[0].observations
    assert [(o.subject, o.text) for o in obs] == [("hindsight-worker.service", "юнит hindsight-worker.service упал: Hindsight worker")]
    obs = find(_req_tick(fresh_state(), show, 0)[0], "example-stage", "units")[0].observations
    assert [(o.subject, o.value) for o in obs] == [("hindsight-worker.service", "failed")]


def test_collect_required_units_ignore_mask_does_not_hide():
    """In a run: a units_ignore glob matches a required unit."""
    cfg = dict(CFG, units_ignore=["hindsight-*"], units_required={"example-stage": ["hindsight-worker.service"]})
    show = _ok_show(**{"hindsight-worker.service": "loaded/inactive"})
    st = fresh_state()
    _req_tick(st, show, 0, cfg=cfg)
    obs = find(_req_tick(st, show, 1, cfg=cfg)[0], "example-stage", "units")[0].observations
    assert [o.subject for o in obs] == ["hindsight-worker.service"]
    failed = "hindsight-worker.service loaded failed failed W\nhindsight-api.service loaded failed failed A\n"
    obs = find(_req_tick(fresh_state(), _ok_show(**{"hindsight-worker.service": "loaded/failed"}), 0,
                         failed=failed, cfg=cfg)[0], "example-stage", "units")[0].observations
    assert [o.subject for o in obs] == ["hindsight-worker.service"]


def test_collect_required_units_only_on_listed_server():
    res, ssh = _req_tick(fresh_state(), (0, "", ""), 0, server="node-f",
                         cfg=dict(CFG, units_required={"example-stage": ["x.service"]}))
    assert not any(c.startswith(UNITS_SHOW_REQ) for _, c, _ in ssh.calls)
    assert find(res, "node-f", "units")[0].ran


def test_collect_required_show_refused_keeps_problems_and_counts_failure():
    """systemctl show gave no data: units did not run (open problems do not close), a gateway refusal — the problem
    gate:units, a failure — into the health counter; the tick counters do not move."""
    st = fresh_state()
    stopped = _ok_show(**{"hindsight-worker.service": "loaded/inactive", "digipaddock-usage.timer": "loaded/active"})
    _req_tick(st, stopped, 0)
    res, _ = _req_tick(st, (126, "", "agent-ro-gate: запрещено: systemctl"), 1)
    assert find(res, "example-stage", "units")[0].ran is False
    gate_obs = [o for r in find(res, "example-stage", "gate:units") for o in r.observations]
    assert [o.value for o in gate_obs] == ["отказ"]
    assert len(find(res, "example-stage", "gate:units")) == 1
    assert st["sec"]["fails"]["example-stage"]["units"]["n"] == 1
    assert st["sec"]["units_down"]["example-stage"] == {"hindsight-worker.service": 1}
    res, _ = _req_tick(st, (124, "", ""), 2)
    assert find(res, "example-stage", "units")[0].ran is False
    assert not any(r.observations for r in find(res, "example-stage", "gate:units"))
    res, _ = _req_tick(st, stopped, 3)
    assert [o.subject for o in find(res, "example-stage", "units")[0].observations] == ["hindsight-worker.service"]
    assert "units" not in st["sec"]["fails"]["example-stage"]


def test_collect_required_unit_missing_from_output_is_no_data():
    st = fresh_state()
    st["problems"]["example-stage:units:digipaddock-usage.timer"] = {"id": "P-1"}
    res, _ = _req_tick(st, _ok_show(**{"hindsight-worker.service": "loaded/active"}), 0)
    assert find(res, "example-stage", "units")[0].ran is False
    assert "digipaddock-usage.timer" in st["sec"]["fails"]["example-stage"]["units"]["why"]


def test_required_units_command_passes_gate():
    """The check's command passes the gateway's whitelist."""
    gate.check(" ".join(runner.UNITS_SHOW + STAGE_REQUIRED))


def test_collect_mem_swap_rate_over_ticks_and_optional_psi():
    meminfo = MEMINFO % (8000000, 2097148, 3624)

    def ssh(pages, psi):
        return FakeSsh({"uptime": UPTIME, ("a", "cat /proc/meminfo /proc/vmstat"): (0, meminfo + VMSTAT % (0, pages), ""),
                        ("a", "cat /proc/pressure/memory"): psi})

    st = fresh_state()
    no_psi = (1, "", "cat: /proc/pressure/memory: No such file or directory")
    res = runner.collect(CFG, NOW - timedelta(minutes=10), st, far(), ssh=ssh(0, no_psi), servers=["a"])
    mem = find(res, "a", "mem")[0]
    assert mem.ran and lv(mem.observations) == {"ram": "ok", "swap": "ok"}
    assert not any(r.observations for r in find(res, "a", "gate:mem"))
    s = ssh(1200000, (0, PSI % (0, 0), ""))
    res = runner.collect(CFG, NOW, st, far(), ssh=s, servers=["a"])
    obs = find(res, "a", "mem")[0].observations
    assert lv(obs) == {"ram": "ok", "swap": "ok", "psi": "ok", "swapio": "warn"}
    assert subj(obs, "swapio").value == "2000 стр/с"
    assert ("a", "cat /proc/pressure/memory") in {(a, c) for a, c, _ in s.calls}
    assert [n for _, n in st["swap_io"]["a"]] == [0, 1200000]


def test_collect_rc255_three_times_reach_levels():
    ssh = FakeSsh({"uptime": (255, "", "ssh: connect to host: timeout")})
    st = fresh_state()
    servers = ["node-f", "example-prod"]
    outs = [runner.collect(CFG, NOW, st, far(), ssh=ssh, servers=servers) for _ in range(3)]
    for early in outs[:2]:
        assert not any(r.ran for r in early)
    last = outs[2]
    assert lv(find(last, "node-f", "reach")[0].observations) == {"-": "warn"}
    assert lv(find(last, "example-prod", "reach")[0].observations) == {"-": "crit"}
    # the other checks of a server that did not answer neither ran nor were called
    assert find(last, "node-f", "disk")[0].ran is False
    assert all(c == "uptime" for _, c, _ in ssh.calls)
    # it answered → the counter is reset
    runner.collect(CFG, NOW, st, far(), ssh=FakeSsh({"uptime": UPTIME}), servers=servers)
    assert st["unreachable"]["node-f"] == 0


def test_collect_rc2_disabled_no_problem():
    ssh = FakeSsh({"uptime": (2, "", "сервер выключен")})
    st = fresh_state()
    res = runner.collect(CFG, NOW, st, far(), ssh=ssh, servers=["node-f"])
    assert not any(r.ran for r in res) and not any(r.observations for r in res)
    assert st["unreachable"].get("node-f", 0) == 0


def test_check_rc2_and_timeout_not_ran():
    ssh = FakeSsh({"uptime": UPTIME, ("s", "df"): (2, "", ""), ("s", "cat /proc/meminfo"): (124, "", ""),
                   ("s", "systemctl"): (255, "", ""), ("s", "dmesg"): (1, "", "boom")})
    res = runner.collect(CFG, NOW, fresh_state(), far(), ssh=ssh, servers=["s"])
    for name in ("disk", "mem", "units", "oom"):
        r = find(res, "s", name)[0]
        assert r.ran is False and r.observations == []
    assert not any(r.check.startswith("gate:") and r.observations for r in res)


def test_gate_no_command_is_not_a_problem():
    ssh = FakeSsh({"uptime": UPTIME, ("node-f", "wg"): (126, "", "agent-ro-gate: на этом сервере нет команды: wg")})
    res = runner.collect(CFG, NOW, fresh_state(), far(), ssh=ssh, servers=["node-f"])
    assert find(res, "node-f", "wg")[0].ran is False
    assert not any(r.observations for r in find(res, "node-f", "gate:wg"))


def test_gate_refusal_opens_then_closes_same_source():
    deny = {"uptime": UPTIME, ("node-f", "wg"): (126, "", "agent-ro-gate: команда запрещена\nвторая строка")}
    res = runner.collect(CFG, NOW, fresh_state(), far(), ssh=FakeSsh(deny), servers=["node-f"])
    g = find(res, "node-f", "gate:wg")
    assert len(g) == 1 and g[0].ran and lv(g[0].observations) == {"-": "warn"}
    assert g[0].observations[0].text == "agent-ro-gate: команда запрещена"
    assert g[0].observations[0].key == "node-f:gate:wg:-"
    assert find(res, "node-f", "wg")[0].ran is False
    ok = {"uptime": UPTIME, ("node-f", "wg"): (0, _wg(int(NOW.timestamp())), "")}
    res2 = runner.collect(CFG, NOW, fresh_state(), far(), ssh=FakeSsh(ok), servers=["node-f"])
    g2 = find(res2, "node-f", "gate:wg")
    assert len(g2) == 1 and g2[0].ran and g2[0].observations == []


def test_gate_close_flows_through_registry():
    from datetime import date
    from sysadmin.monitor.registry import Registry
    reg = Registry.load(Path("/нет/state.json"), date(2026, 10, 5))
    deny = {"uptime": UPTIME, ("node-f", "wg"): (126, "", "запрещено")}
    reg.observe(runner.collect(CFG, NOW, fresh_state(), far(), ssh=FakeSsh(deny), servers=["node-f"]), NOW)
    assert "node-f:gate:wg:-" in reg.state["problems"]
    ok = {"uptime": UPTIME, ("node-f", "wg"): (0, _wg(int(NOW.timestamp())), "")}
    for i in range(3):
        reg.observe(runner.collect(CFG, NOW, fresh_state(), far(), ssh=FakeSsh(ok), servers=["node-f"]), NOW)
    assert "node-f:gate:wg:-" not in reg.state["problems"]


def test_every_min_certs():
    ssh = FakeSsh({"uptime": UPTIME, ("example-prod", "certbot"): (0, CERTBOT, "")})
    st = fresh_state()
    r1 = runner.collect(CFG, NOW, st, far(), ssh=ssh, servers=["example-prod"])
    assert find(r1, "example-prod", "certs")[0].ran
    r2 = runner.collect(CFG, NOW + timedelta(minutes=5), st, far(), ssh=ssh, servers=["example-prod"])
    assert find(r2, "example-prod", "certs")[0].ran is False
    assert sum(1 for _, c, _ in ssh.calls if c == "certbot certificates") == 1
    r3 = runner.collect(CFG, NOW + timedelta(minutes=361), st, far(), ssh=ssh, servers=["example-prod"])
    assert find(r3, "example-prod", "certs")[0].ran


def fixed_problem(key, source, at, card="KD-81"):
    return {key: {"id": "P-1", "key": key, "source": source, "level": "warn", "fixed": {"at": at.isoformat(), "card": card}}}


def test_fixed_problem_rechecks_daily_check_on_next_tick_once():
    """An open problem marked "fixed" — its check (here once a day) goes into the nearest poll off schedule, once:
    after the recheck — by every_min again. The other daily checks are not touched."""
    ssh = FakeSsh({"uptime": UPTIME})
    st = fresh_state()

    def apt_calls():
        return sum(1 for _, c, _ in ssh.calls if c == "apt list --upgradable")

    def tick(minutes):
        return runner.collect(CFG, NOW + timedelta(minutes=minutes), st, far(), ssh=ssh, servers=["srv"])

    assert find(tick(0), "srv", "updates")[0].ran and apt_calls() == 1
    assert find(tick(5), "srv", "updates")[0].ran is False and apt_calls() == 1  # on schedule — once a day
    st["problems"] = fixed_problem("srv:updates:security", ["srv", "updates"], NOW + timedelta(minutes=7))
    before = {c for _, c, _ in ssh.calls}
    assert find(tick(10), "srv", "updates")[0].ran and apt_calls() == 2  # off schedule
    assert st["last_run"]["srv:updates"] == (NOW + timedelta(minutes=10)).isoformat()
    assert tick(15) and apt_calls() == 2  # already rechecked — on schedule from here
    reboot = [c for _, c, _ in ssh.calls if c == "needrestart -b -r l"]
    assert len(reboot) == 1 and "needrestart -b -r l" in before  # the neighbouring daily check is not off schedule


@pytest.mark.parametrize("key,source,name", [
    ("srv:posture:sshd-root", ["srv", "posture"], "sec"),  # the posture comes from the agent-sec-snapshot snapshot
    ("srv:gate:updates:-", ["srv", "gate:updates"], "updates"),  # a gateway refusal — ask the same command again
    ("srv:health:reboot", ["srv", "health"], "reboot"),  # "the check does not work" — rerun the check itself
    ("srv:health:sec:users", ["srv", "health"], "sec"),
    ("srv:certs:api.example.test", ["srv", "certs"], "certs"),
    ("node-a:heartbeat:-", ["example-prod", "heartbeat"], None),  # no every_min — it runs every tick anyway
])
def test_fixed_due_name_follows_problem_source(key, source, name):
    later = NOW + timedelta(minutes=5)
    names = ("sec", "updates", "reboot", "certs")
    st = fresh_state()
    st["last_run"] = {f"{s}:{n}": NOW.isoformat() for s in ("srv", "example-prod") for n in names}

    def ctx():
        return runner._Ctx(CFG, later, st, far(), None, runner.time.monotonic, Baseline.empty())

    server = source[0]
    assert not any(ctx().due(server, n) for n in names)
    st["problems"] = fixed_problem(key, source, NOW + timedelta(minutes=1))
    c = ctx()
    assert [n for n in names if c.due(server, n)] == ([name] if name else [])
    st["problems"][key]["fixed"]["at"] = (NOW - timedelta(minutes=1)).isoformat()  # already rechecked after the mark
    assert not any(ctx().due(server, n) for n in names)


def test_health_not_ran_when_server_starved_of_budget():
    """The tick's budget ran out and some of the server's calls did not go through: the failure counters are old — health
    did not run (ran=False), otherwise the recheck of a "fixed" would give a false "did not help". A server that got all
    its calls — health as usual."""
    def run(clock, deadline):
        st = fresh_state()
        runner.seccheck.sec_state(st)["fails"]["srv"] = {"wgpeers": {"n": 5, "why": "x"}}  # wgpeers does not run on srv
        return find(runner.collect(CFG, NOW, st, deadline, ssh=FakeSsh({"uptime": UPTIME}), servers=["srv"],
                                   clock=clock), "srv", "health")[0]

    full = run(runner.time.monotonic, far())
    assert full.ran and lv(full.observations) == {"wgpeers": "warn"}
    ticks = []

    def clock():  # the first call (uptime) is within the budget, after it the budget is over
        ticks.append(1)
        return 0.0 if len(ticks) == 1 else 10.0
    starved = run(clock, 5.0)
    assert starved.ran is False and starved.observations == []


@pytest.mark.parametrize("fixed", ["мусор", {"at": "не время", "card": "KD-1"}, {"card": "KD-1"}, None])
def test_fixed_broken_mark_does_not_force_or_crash(fixed):
    st = fresh_state()
    st["last_run"] = {"srv:updates": NOW.isoformat()}
    st["problems"] = {"srv:updates:security": {"id": "P-1", "key": "srv:updates:security", "source": ["srv", "updates"],
                                               "fixed": fixed}, "битая": "не словарь"}
    c = runner._Ctx(CFG, NOW + timedelta(minutes=5), st, far(), None, runner.time.monotonic, Baseline.empty())
    assert c.due("srv", "updates") is False


def test_collect_expired_deadline_not_ran():
    ssh = FakeSsh({"uptime": UPTIME})
    res = runner.collect(CFG, NOW, fresh_state(), runner.time.monotonic() - 1, ssh=ssh, servers=["s", "node-f"])
    assert ssh.calls == []
    assert res and not any(r.ran for r in res)


def test_explicit_check_refreshes_daily_sources_but_preserves_disabled_jobs_and_budget():
    cfg = dict(CFG, disabled_checks={"srv": ["disk"]})
    st = fresh_state()
    st["last_run"] = {"srv:reboot": NOW.isoformat()}
    scheduled = FakeSsh({"uptime": UPTIME})
    runner.collect(cfg, NOW, st, far(), ssh=scheduled, servers=["srv"])
    assert not any(command == "needrestart -b -r l" for _, command, _ in scheduled.calls)
    manual = FakeSsh({"uptime": UPTIME})
    runner.collect(cfg, NOW, st, far(), ssh=manual, servers=["srv"], force=True)
    assert any(command == "needrestart -b -r l" for _, command, _ in manual.calls)
    assert not any(command.startswith("df ") for _, command, _ in manual.calls)
    expired = FakeSsh({"uptime": UPTIME})
    results = runner.collect(cfg, NOW, st, 0, ssh=expired, servers=["srv"], clock=lambda: 1, force=True)
    assert expired.calls == [] and not any(result.ran for result in results)


def test_call_timeout_is_min_of_config_and_remaining():
    ssh = FakeSsh({"uptime": UPTIME})
    runner.collect(CFG, NOW, fresh_state(), runner.time.monotonic() + 5, ssh=ssh, servers=["s"])
    assert all(t <= 5 for _, _, t in ssh.calls)
    ssh2 = FakeSsh({"uptime": UPTIME})
    runner.collect(CFG, NOW, fresh_state(), far(), ssh=ssh2, servers=["s"])
    assert all(t == CFG["call_timeout_s"] for _, _, t in ssh2.calls)


def test_oom_gets_the_servers_zone_from_the_inventory(monkeypatch):
    from sysadmin import inventory

    monkeypatch.setattr(inventory, "zones", lambda path=None: {"s": BERLIN})
    out = "[Sat Oct 24 13:30:00 2026] Out of memory: Killed process 1 (x)\n"
    ssh = FakeSsh({"uptime": (0, " 13:00:05 up 3 days,\n", ""), ("s", "dmesg"): (0, out, "")})
    res = runner.collect(CFG, AFTER_DST, fresh_state(), far(), ssh=ssh, servers=["s"])
    assert lv(find(res, "s", "oom")[0].observations) == {"-": "ok"}


def test_oom_gets_shift_from_reach():
    ssh = FakeSsh({"uptime": (0, " 09:00:05 up 3 days,\n", ""), ("s", "dmesg"): (0, DMESG, "")})
    res = runner.collect(CFG, NOW, fresh_state(), far(), ssh=ssh, servers=["s"])
    assert lv(find(res, "s", "oom")[0].observations) == {"-": "warn"}


def test_heartbeat_via_runner_uses_inventory_aliases():
    hb = "node-a\t232068\nNodeC\t39\nmobile-kit3\t77\n"
    ssh = FakeSsh({"uptime": UPTIME, ("example-prod", 'mysql -N -e SELECT server_name'): (0, hb, "")})
    res = runner.collect(CFG, NOW, fresh_state(), far(), ssh=ssh, servers=["example-prod", "node-a", "node-c"])
    r = find(res, "example-prod", "heartbeat")[0]
    assert r.ran and {o.server: o.level for o in r.observations} == {"node-a": "warn", "node-c": "ok"}


def test_heartbeat_and_consumers_read_the_configs_tables():
    # the application's tables are the deployment's, not the code's
    ssh = FakeSsh({"uptime": UPTIME})
    runner.collect(CFG, NOW, fresh_state(), far(), ssh=ssh, servers=["example-prod"])
    sql = [cmd for alias, cmd, _ in ssh.calls if alias == "example-prod" and "TIMESTAMPDIFF" in cmd]
    assert any("FROM app_vpn_heartbeat GROUP BY server_name" in c for c in sql), sql
    assert any(c.endswith("FROM app_consumer_heartbeat") for c in sql), sql


@pytest.mark.parametrize("section,key,bad", [("heartbeat", "table", "t; DROP TABLE x"),
                                              ("heartbeat", "table", None),
                                              ("monitoring", "consumers_table", "a b"),
                                              ("monitoring", "consumers_table", "")])
def test_app_table_names_are_checked(tmp_path, section, key, bad):
    cfg = json.loads(json.dumps(CFG))
    if bad is None:
        del cfg[section][key]
    else:
        cfg[section][key] = bad
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    with pytest.raises(ValueError, match=f"{section}.{key}"):
        load_config(p)


MYSQL_TABLE = {
    ("example-prod", "mysql -N -e SELECT id, user"): (0, PROC_QUIET, ""),
    ("example-prod", "mysql -N -e SHOW GLOBAL STATUS"): (0, "Threads_connected\t25\n", ""),
    ("example-prod", "mysql -N -e SHOW VARIABLES"): (0, "max_connections\t500\n", ""),
}


def test_mysql_via_runner_ok_and_down():
    ssh = FakeSsh({**MYSQL_TABLE, "uptime": UPTIME})
    res = runner.collect(CFG, NOW, fresh_state(), far(), ssh=ssh, servers=["example-prod"])
    assert lv(find(res, "example-prod", "mysql")[0].observations)["up"] == "ok"
    down = FakeSsh({"uptime": UPTIME, ("example-prod", "mysql -N -e SELECT id, user"):
                    (1, "", "ERROR 2002 (HY000): Can't connect to local MySQL server")})
    res = runner.collect(CFG, NOW, fresh_state(), far(), ssh=down, servers=["example-prod"])
    my = find(res, "example-prod", "mysql")[0]
    assert my.ran and lv(my.observations) == {"up": "crit"}


def _mon_ssh(raw):
    return FakeSsh({**MYSQL_TABLE, "uptime": UPTIME, ("example-prod", "cat /var/log/app"): (0, raw, "")})


def test_monitoring_via_runner_new_ts_then_same_ts():
    raw, ts = _pfile(5)
    st = fresh_state()
    r1 = runner.collect(CFG, NOW, st, far(), ssh=_mon_ssh(raw), servers=["example-prod"])
    f1 = find(r1, "example-prod", "monitoring_file")[0]
    assert f1.ran and "vpn_country:ZZ" in lv(f1.observations) and st["monitoring_ts"] == ts
    assert find(r1, "example-prod", "monitoring_alive")[0].ran
    r2 = runner.collect(CFG, NOW + timedelta(minutes=1), st, far(), ssh=_mon_ssh(raw), servers=["example-prod"])
    assert find(r2, "example-prod", "monitoring_file")[0].ran is False
    assert lv(find(r2, "example-prod", "monitoring_alive")[0].observations) == {"-": "ok"}


def test_monitoring_via_runner_stale_ts():
    raw, _ = _pfile(40)
    st = fresh_state()
    res = runner.collect(CFG, NOW, st, far(), ssh=_mon_ssh(raw), servers=["example-prod"])
    assert lv(find(res, "example-prod", "monitoring_alive")[0].observations) == {"-": "crit"}
    assert find(res, "example-prod", "monitoring_file")[0].ran is False
    assert st["monitoring_ts"] is None


def test_monitoring_via_runner_missing_file_falls_back_to_log():
    t = (NOW - timedelta(minutes=8)).strftime("%Y-%m-%d %H:%M:%S")
    ssh = FakeSsh({**MYSQL_TABLE, "uptime": UPTIME,
        ("example-prod", "cat /var/log/app"): (1, "", "cat: /var/log/app/monitoring_problems.json: No such file or directory"),
        ("example-prod", "tail -n 50 /var/log/app/monitoring_alert.log"): (0, f"{t} INFO monitoring_alert: start\n", ""),
    })
    res = runner.collect(CFG, NOW, fresh_state(), far(), ssh=ssh, servers=["example-prod"])
    assert lv(find(res, "example-prod", "monitoring_alive")[0].observations) == {"-": "ok"}
    assert find(res, "example-prod", "monitoring_file")[0].ran is False


def test_default_server_list_is_inventory_intersect_enabled(monkeypatch):
    from sysadmin import inventory
    monkeypatch.setattr(inventory, "enabled", lambda *a: {"node-c", "node-f", "ghost"})
    ssh = FakeSsh({"uptime": UPTIME})
    runner.collect(CFG, NOW, fresh_state(), far(), ssh=ssh)
    assert {a for a, _, _ in ssh.calls} == {"node-c", "node-f"}  # ghost is not in the inventory


# --- review round 1 ------------------------------------------------------------------------

def test_monitoring_file_without_problems_key():
    out = json.dumps({"ts": (NOW - timedelta(minutes=2)).isoformat()})
    assert checks.monitoring_file("p", out, CFG, NOW) == []


def test_collect_isolates_failing_check(monkeypatch, capsys):
    def boom(*a, **kw):
        raise ValueError("сломан")
    monkeypatch.setattr(checks, "disk", boom)
    ssh = FakeSsh({"uptime": UPTIME, ("a", "cat /proc/meminfo"): (0, MEMINFO % (640000, 0, 0), "")})
    res = runner.collect(CFG, NOW, fresh_state(), far(), ssh=ssh, servers=["a", "b"])
    assert find(res, "a", "disk")[0].ran is False
    assert find(res, "a", "mem")[0].ran and lv(find(res, "a", "mem")[0].observations)["ram"] == "crit"
    assert find(res, "b", "mem")[0].ran and find(res, "b", "reach")[0].ran
    err = capsys.readouterr().err
    assert "kd-monitor: проверка a:disk упала: ValueError: сломан" in err


def test_collect_isolates_failing_reach(monkeypatch, capsys):
    def boom(server, out, cfg, now, **kw):
        if server == "a":
            raise KeyError("x")
        return []
    monkeypatch.setattr(checks, "reach", boom)
    res = runner.collect(CFG, NOW, fresh_state(), far(), ssh=FakeSsh({"uptime": UPTIME}), servers=["a", "b"])
    assert find(res, "a", "reach")[0].ran is False and find(res, "a", "disk")[0].ran is False
    assert find(res, "b", "disk")[0].ran
    assert "проверка a:reach упала: KeyError" in capsys.readouterr().err


def test_reach_ok_closes_gate_reach():
    res = runner.collect(CFG, NOW, fresh_state(), far(), ssh=FakeSsh({"uptime": UPTIME}), servers=["s"])
    g = find(res, "s", "gate:reach")
    assert len(g) == 1 and g[0].ran and g[0].observations == []


def test_units_ignores_junk_lines():
    out = ("systemctl: command failed\nWarning: foo bar baz qux\n"
           "danted.service loaded failed failed SOCKS\n"
           "x.service loaded active running y\n")
    assert lv(checks.units("s", out, CFG, NOW)) == {"danted.service": "warn"}


def test_certs_hours_left_is_crit():
    out = "  Certificate Name: a.co\n    Expiry Date: 2026-10-05 20:00:00+00:00 (VALID: 8 hour(s))\n"
    assert lv(checks.certs("s", out, CFG, NOW)) == {"a.co": "crit"}


def test_backup_unparsed_output_gives_no_verdict():
    assert checks.backup("n", "ls: cannot access '/backups': No such file or directory\n", CFG, NOW) == []
    assert checks.backup("n", "", CFG, NOW) == []


def test_consumers_skips_junk_first_line():
    assert lv(checks.consumers("p", "Warning: x\n20\n", CFG, NOW)) == {"-": "crit"}
    assert lv(checks.consumers("p", "note\nNULL\n", CFG, NOW)) == {"-": "crit"}
    assert checks.consumers("p", "junk only\n", CFG, NOW) == []


def test_alive_unparsable_ts_is_warn():
    out = json.dumps({"ts": "вчера", "problems": []})
    res = checks.monitoring_alive("p", out, CFG, NOW)
    assert lv(res) == {"-": "warn"} and "не читается" in res[0].text


def test_mysql_ignores_dump_select():
    row = "12\tbackup\tQuery\t1500\tSending data\tSELECT /*!40001 SQL_NO_CACHE */ * FROM `t`\n"
    res = checks.mysql("p", _mysql(row), CFG, NOW)
    assert lv(res)["query"] == "ok" and lv(res)["mdl"] == "ok"
    other = "12\tapp\tQuery\t1500\tSending data\tSELECT 1\n"
    assert lv(checks.mysql("p", _mysql(other), CFG, NOW))["query"] == "crit"


# --- security --------------------------------------------------------------------------

import copy  # noqa: E402
from sysadmin.monitor.baseline import Baseline  # noqa: E402

SNAP_OK = ("# agent-sec-snapshot v1\nusers\troot\t0 /bin/bash\ngroups\tsudo\t-\nkeys\t/root/.ssh/authorized_keys\taaaa 2\n"
           "sudoers\t/etc/sudoers\ts1\nshadow\t/etc/shadow\tsh1\nsshd\tport\t22\nsshd\tpermitrootlogin\tprohibit-password\n"
           "sshd\tpasswordauthentication\tno\ncron\t/etc/crontab\tc1\npersist\t/etc/ld.so.preload\t-\n"
           "units\tdigipaddock-monitor.service\tu1\nfw\tufw\tactive\nfw\tfail2ban\tactive\n")
SS_OK = ("Netid State  Recv-Q Send-Q Local Address:Port  Peer Address:Port Process\n"
         'tcp   LISTEN 0      4096   0.0.0.0:22          0.0.0.0:*     users:(("sshd",pid=800,fd=3))\n')
AUTH_OWNER = "2026-10-05T11:58:02.000000+03:00 h sshd[2]: Accepted publickey for root from 203.0.113.7 port 2 ssh2: ED25519 SHA256:own\n"
AUTH_EVIL = "2026-10-05T11:58:03.000000+03:00 h sshd[3]: Accepted publickey for root from 198.51.100.9 port 3 ssh2: ED25519 SHA256:evil\n"
CPU1 = "processor\t: 0\n"
UPTIME_HIGH = (0, " 12:00:05 up 3 days,  1 user,  load average: 9.00, 9.00, 9.00\n", "")
TAIL = "tail -n 1500 /var/log/auth.log"
JOURNAL = "journalctl _COMM=sshd + _COMM=sshd-session -n 1500 --no-pager -o short-iso"


def baseline_for(*servers, trusted=()):
    snap = {ln.split("\t")[0] + ":" + ln.split("\t")[1]: ln.split("\t")[2] for ln in SNAP_OK.splitlines()[1:]}
    b = Baseline.empty()
    for s in servers:
        b.set_server(s, Baseline.make_server({}, {"tcp:0.0.0.0:22": "sshd"}, [], snap, NOW))
    for ip, label in trusted:
        b.trust(ip, label)
    return b


def sec_table(auth=AUTH_OWNER, snap=SNAP_OK):
    return {"uptime": UPTIME, TAIL: (0, auth, ""), JOURNAL: (0, auth, ""),
            "agent-sec-snapshot": (0, snap, ""), "ss -tlnpu": (0, SS_OK, ""),
            "cat /proc/cpuinfo": (0, CPU1, ""), "cat /etc/os-release": (0, 'ID=ubuntu\nVERSION_ID="24.04"\n', ""),
            "apt list --upgradable": (0, "Listing...\n", ""), "needrestart -b -r l": (0, "NEEDRESTART-KSTA: 1\n", ""),
            "systemctl is-enabled apt-daily.timer apt-daily-upgrade.timer unattended-upgrades": (0, "enabled\nenabled\nenabled\n", ""),
            "cat /etc/apt/apt.conf.d/20auto-upgrades": (0, 'APT::Periodic::Unattended-Upgrade "1";\n', ""),
            "dnf -q -C check-update --security": (0, "", ""), "dnf needs-restarting -r": (0, "Reboot should not be necessary.\n", ""),
            "systemctl is-enabled dnf-automatic-install.timer dnf-automatic.timer": (0, "enabled\ndisabled\n", "")}


def test_security_checks_skip_without_baseline_but_updates_run():
    ssh = FakeSsh(sec_table())
    res = runner.collect(CFG, NOW, fresh_state(), far(), ssh=ssh, servers=["example-prod"])
    for name in ("logins", "ports", "sec:users", "sec:fw"):
        assert find(res, "example-prod", name)[0].ran is False
    for name in ("updates", "eol", "autoupdates", "reboot", "posture", "health"):
        assert find(res, "example-prod", name)[0].ran, name
    assert not any(a == "example-prod" and c == "ss -tlnpu" for a, c, _ in ssh.calls)


def test_security_commands_pass_gate_and_match_os():
    ssh = FakeSsh(sec_table())
    b = baseline_for("example-prod", "node-g", "node-f")
    runner.collect(CFG, NOW, fresh_state(), far(), ssh=ssh, servers=["example-prod", "node-g", "node-f"], baseline=b)
    cmds = {(a, c) for a, c, _ in ssh.calls}
    expected = [("example-prod", TAIL), ("example-prod", "agent-sec-snapshot"), ("example-prod", "ss -tlnpu"),
                ("example-prod", "wg show all allowed-ips"), ("example-prod", "apt list --upgradable"),
                ("example-prod", "needrestart -b -r l"),
                ("example-prod", "systemctl is-enabled apt-daily.timer apt-daily-upgrade.timer unattended-upgrades"),
                ("example-prod", "cat /etc/apt/apt.conf.d/20auto-upgrades"), ("example-prod", "cat /etc/os-release"),
                ("example-prod", "cat /proc/cpuinfo"), ("node-f", "wg show all allowed-ips"),
                ("node-g", JOURNAL), ("node-g", "dnf -q -C check-update --security"), ("node-g", "dnf needs-restarting -r"),
                ("node-g", "systemctl is-enabled dnf-automatic-install.timer dnf-automatic.timer"), ("node-g", "cat /etc/dnf/automatic.conf")]
    for pair in expected:
        assert pair in cmds, pair
        gate.check(pair[1])  # every new command passes the gateway's whitelist
    assert ("node-g", TAIL) not in cmds


def test_logins_trusted_silent_unknown_opens_crit():
    b = baseline_for("example-prod", trusted=[("203.0.113.0/24", "владелец")])
    st = fresh_state()
    res = runner.collect(CFG, NOW, st, far(), ssh=FakeSsh(sec_table()), servers=["example-prod"], baseline=b)
    lg = find(res, "example-prod", "logins")[0]
    assert lg.ran and lg.observations == []
    assert b.server("example-prod")["identities"] == {"root@ED25519 SHA256:own": "владелец"}
    assert st["sec"]["logins"]["example-prod"]["last_ts"] == "2026-10-05T11:58:02+03:00"
    res = runner.collect(CFG, NOW + timedelta(minutes=5), st, far(), ssh=FakeSsh(sec_table(auth=AUTH_OWNER + AUTH_EVIL)),
                         servers=["example-prod"], baseline=b)
    assert lv(find(res, "example-prod", "logins")[0].observations) == {"root@ED25519 SHA256:evil": "crit"}


def test_logins_falls_back_to_journal_when_no_auth_log():
    t = sec_table()
    t[TAIL] = (1, "", "tail: cannot open '/var/log/auth.log' for reading: No such file or directory")
    ssh = FakeSsh(t)
    b = baseline_for("node-e")
    st = fresh_state()
    res = runner.collect(CFG, NOW, st, far(), ssh=ssh, servers=["node-e"], baseline=b)
    assert find(res, "node-e", "logins")[0].ran and ("node-e", JOURNAL) in {(a, c) for a, c, _ in ssh.calls}
    assert st["sec"]["logins"]["node-e"]["journal"] is True
    ssh2 = FakeSsh(t)
    runner.collect(CFG, NOW + timedelta(minutes=5), st, far(), ssh=ssh2, servers=["node-e"], baseline=b)
    assert not any(c == TAIL for _, c, _ in ssh2.calls)  # then the journal at once


def test_logins_refetches_on_gap_and_warns():
    cfg = copy.deepcopy(CFG)
    cfg["security"]["auth_log_lines"], cfg["security"]["auth_log_lines_max"] = 2, 4
    line = "2026-10-05T11:5{m}:0{s}.000000+03:00 h sshd[1]: Accepted publickey for root from 203.0.113.7 port 1 ssh2: ED25519 SHA256:own\n"
    b = baseline_for("example-prod", trusted=[("203.0.113.0/24", "владелец")])
    st = fresh_state()
    small = "tail -n 2 /var/log/auth.log"
    big = "tail -n 4 /var/log/auth.log"
    t = {"uptime": UPTIME, small: (0, line.format(m=8, s=1) + line.format(m=8, s=2), ""), "agent-sec-snapshot": (0, SNAP_OK, "")}
    runner.collect(cfg, NOW, st, far(), ssh=FakeSsh(t), servers=["example-prod"], baseline=b)
    assert st["sec"]["logins"]["example-prod"]["last_ts"] == "2026-10-05T11:58:02+03:00"
    # the next window is entirely newer than last_ts and filled to the edge → a repeat with a larger window, where
    # there is an overlap
    t[small] = (0, line.format(m=9, s=5) + line.format(m=9, s=6), "")
    t[big] = (0, line.format(m=8, s=2) + line.format(m=9, s=4) + line.format(m=9, s=5) + line.format(m=9, s=6), "")
    ssh = FakeSsh(t)
    res = runner.collect(cfg, NOW + timedelta(minutes=5), st, far(), ssh=ssh, servers=["example-prod"], baseline=b)
    assert ("example-prod", big) in {(a, c) for a, c, _ in ssh.calls}
    assert lv(find(res, "example-prod", "logins")[0].observations) == {}
    # and there is no overlap there either → ⚠️/🔴 gap
    t[small] = (0, line.format(m=9, s=8) + line.format(m=9, s=9), "")
    t[big] = (0, line.format(m=9, s=7) + line.format(m=9, s=8) + line.format(m=9, s=9), "")
    res = runner.collect(cfg, NOW + timedelta(minutes=10), st, far(), ssh=FakeSsh(t), servers=["example-prod"], baseline=b)
    assert lv(find(res, "example-prod", "logins")[0].observations) == {"gap": "crit"}


def test_sec_section_error_gives_ran_false():
    snap = SNAP_OK.replace("sshd\tport\t22\nsshd\tpermitrootlogin\tprohibit-password\nsshd\tpasswordauthentication\tno\n",
                           "sshd\terror\tRuntimeError: sshd -T: код 1\n")
    b = baseline_for("example-prod")
    res = runner.collect(CFG, NOW, fresh_state(), far(), ssh=FakeSsh(sec_table(snap=snap)), servers=["example-prod"], baseline=b)
    assert find(res, "example-prod", "sec:sshd")[0].ran is False
    assert find(res, "example-prod", "sec:users")[0].ran and find(res, "example-prod", "sec:users")[0].observations == []
    assert find(res, "example-prod", "posture")[0].ran is False


def test_sec_diff_and_current_snapshot_saved():
    b = baseline_for("example-prod")
    st = fresh_state()
    snap = SNAP_OK + "users\tevil\t1001 /bin/bash\n"
    res = runner.collect(CFG, NOW, st, far(), ssh=FakeSsh(sec_table(snap=snap)), servers=["example-prod"], baseline=b)
    assert lv(find(res, "example-prod", "sec:users")[0].observations) == {"evil": "crit"}
    assert st["sec"]["current"]["example-prod"]["text"] == snap
    assert st["last_run"]["example-prod:sec"] == NOW.isoformat()


def test_sec_skipped_while_expect_window_open():
    """While a release window is open the server's snapshot is not compared; an expired window does not get in the way."""
    b = baseline_for("example-prod")
    st = fresh_state()
    st["sec"] = {"expect": {"example-prod": {"label": "x", "at": NOW.isoformat(), "opens": 1, "before": {}, "errors": []}}}
    snap = SNAP_OK + "users\tevil\t1001 /bin/bash\n"
    ssh = FakeSsh(sec_table(snap=snap))
    res = runner.collect(CFG, NOW + timedelta(minutes=10), st, far(), ssh=ssh, servers=["example-prod"], baseline=b)
    assert find(res, "example-prod", "sec:users")[0].ran is False and find(res, "example-prod", "posture")[0].ran is False
    assert not any(c == "agent-sec-snapshot" for _, c, _ in ssh.calls)
    late = NOW + timedelta(minutes=CFG["security"]["expect_max_min"] + 1)
    res = runner.collect(CFG, late, st, far(), ssh=FakeSsh(sec_table(snap=snap)), servers=["example-prod"], baseline=b)
    assert lv(find(res, "example-prod", "sec:users")[0].observations) == {"evil": "crit"}


def test_the_tick_skips_sec_ports_and_wgpeers_of_a_server_in_a_plan_window():
    """a step of a plan is changing the server — its snapshot, listeners and peers wait for the window."""
    b = baseline_for("example-prod")
    st = fresh_state()
    st["sec"] = {"plan_windows": {"example-prod": NOW.isoformat()}}
    ssh = FakeSsh(sec_table(snap=SNAP_OK + "users\tevil\t1001 /bin/bash\n"))
    res = runner.collect(CFG, NOW, st, far(), ssh=ssh, servers=["example-prod"], baseline=b)
    for check in ("sec:users", "posture", "ports", "wgpeers"):
        assert find(res, "example-prod", check)[0].ran is False, check
    assert not any(c in ("agent-sec-snapshot", "ss -tlnpu") or c.startswith("wg ") for _, c, _ in ssh.calls)


def test_health_warns_after_three_failures():
    t = sec_table()
    t["agent-sec-snapshot"] = (126, "", "agent-ro-gate: на этом сервере нет команды: agent-sec-snapshot")
    t["needrestart -b -r l"] = (126, "", "agent-ro-gate: на этом сервере нет команды: needrestart")
    t["cat /run/reboot-required"] = (126, "", "agent-ro-gate: отказ: путь вне разрешённых каталогов")  # an old gateway
    b = baseline_for("node-e")
    st = fresh_state()
    for i in range(3):
        res = runner.collect(CFG, NOW + timedelta(hours=i), st, far(), ssh=FakeSsh(t), servers=["node-e"], baseline=b)
        assert find(res, "node-e", "sec:users")[0].ran is False
    h = find(res, "node-e", "health")[0]
    assert h.ran and lv(h.observations) == {"sec": "warn"} and "нет команды: agent-sec-snapshot" in h.observations[0].text
    # reboot is checked once a day: three failures — after three days
    for i in range(1, 3):
        res = runner.collect(CFG, NOW + timedelta(days=i), st, far(), ssh=FakeSsh(t), servers=["node-e"], baseline=b)
    assert "reboot" in lv(find(res, "node-e", "health")[0].observations)
    t["agent-sec-snapshot"] = (0, SNAP_OK, "")
    res = runner.collect(CFG, NOW + timedelta(days=3), st, far(), ssh=FakeSsh(t), servers=["node-e"], baseline=b)
    assert "sec" not in lv(find(res, "node-e", "health")[0].observations)


def test_reboot_without_needrestart_reads_reboot_required():
    """node-a/node-e have no needrestart — /run/reboot-required: present — needed, no file — not needed."""
    t = sec_table()
    t["needrestart -b -r l"] = (126, "", "agent-ro-gate: на этом сервере нет команды: needrestart")
    t["cat /run/reboot-required"] = (0, "*** System restart required ***\n", "")
    st = fresh_state()
    res = runner.collect(CFG, NOW, st, far(), ssh=FakeSsh(t), servers=["node-e"])
    rb = find(res, "node-e", "reboot")[0]
    assert rb.ran and rb.observations[0].value == "0 дн" and st["sec"]["reboot"]["node-e"] == NOW.isoformat()
    t["cat /run/reboot-required"] = (1, "", "cat: /run/reboot-required: No such file or directory\n")
    res = runner.collect(CFG, NOW + timedelta(days=1), st, far(), ssh=FakeSsh(t), servers=["node-e"])
    rb = find(res, "node-e", "reboot")[0]
    assert rb.ran and lv(rb.observations) == {"-": "ok"} and "node-e" not in st["sec"]["reboot"]
    assert "reboot" not in st["sec"]["fails"].get("node-e", {})
    t["needrestart -b -r l"] = (0, "NEEDRESTART-KSTA: 1\n", "")  # needrestart is there — the file is not read
    ssh = FakeSsh(t)
    runner.collect(CFG, NOW + timedelta(days=2), st, far(), ssh=ssh, servers=["node-e"])
    assert not any(c == "cat /run/reboot-required" for _, c, _ in ssh.calls)


def test_ports_hourly_and_new_listener():
    b = baseline_for("node-f")
    st = fresh_state()
    t = sec_table()
    t["ss -tlnpu"] = (0, SS_OK + 'tcp   LISTEN 0      1      0.0.0.0:4444        0.0.0.0:*     users:(("nc",pid=9,fd=3))\n', "")
    ssh = FakeSsh(t)
    res = runner.collect(CFG, NOW, st, far(), ssh=ssh, servers=["node-f"], baseline=b)
    assert lv(find(res, "node-f", "ports")[0].observations) == {"tcp:0.0.0.0:4444": "warn"}
    res = runner.collect(CFG, NOW + timedelta(minutes=5), st, far(), ssh=ssh, servers=["node-f"], baseline=b)
    assert find(res, "node-f", "ports")[0].ran is False
    assert sum(1 for _, c, _ in ssh.calls if c == "ss -tlnpu") == 1


def test_load_needs_cores_then_streak():
    b = baseline_for("example-prod")
    st = fresh_state()
    t = sec_table()
    t["uptime"] = UPTIME_HIGH
    ssh = FakeSsh(t)
    first = runner.collect(CFG, NOW, st, far(), ssh=ssh, servers=["example-prod"], baseline=b)
    assert find(first, "example-prod", "load")[0].ran is False  # the cores are not known yet
    assert st["sec"]["cores"]["example-prod"] == 1
    for i in range(1, 4):
        last = runner.collect(CFG, NOW + timedelta(minutes=5 * i), st, far(), ssh=ssh, servers=["example-prod"], baseline=b)
    assert lv(find(last, "example-prod", "load")[0].observations) == {"-": "crit"}


# --- review round 1 of the security checks -------------------------------------------------

GATE_TRUNC = "\n[agent-ro-gate: вывод обрезан до 1024 КБ]\n"


def test_logins_truncated_refetch_counts_as_failed_and_gives_gap():
    cfg = copy.deepcopy(CFG)
    cfg["security"]["auth_log_lines"], cfg["security"]["auth_log_lines_max"] = 2, 4
    line = "2026-10-05T11:5{m}:0{s}.000000+03:00 h sshd[1]: Accepted publickey for root from {ip} port 1 ssh2: ED25519 SHA256:{fp}\n"
    own = dict(ip="203.0.113.7", fp="own")
    b = baseline_for("example-prod", trusted=[("203.0.113.0/24", "владелец")])
    st = fresh_state()
    small, big = "tail -n 2 /var/log/auth.log", "tail -n 4 /var/log/auth.log"
    t = {"uptime": UPTIME, small: (0, line.format(m=8, s=1, **own) + line.format(m=8, s=2, **own), "")}
    runner.collect(cfg, NOW, st, far(), ssh=FakeSsh(t), servers=["example-prod"], baseline=b)
    t[small] = (0, line.format(m=9, s=5, **own) + line.format(m=9, s=6, **own), "")
    # the gateway kept the beginning of a large window, cut a line at a byte and appended its mark; code 0
    cut = line.format(m=9, s=4, ip="198.51.100.9", fp="evil")[:-4]  # "… SHA256:e" without a line end
    t[big] = (0, line.format(m=8, s=2, **own) + cut + GATE_TRUNC, "")
    ssh = FakeSsh(t)
    res = runner.collect(cfg, NOW + timedelta(minutes=5), st, far(), ssh=ssh, servers=["example-prod"], baseline=b)
    assert ("example-prod", big) in {(a, c) for a, c, _ in ssh.calls}
    assert lv(find(res, "example-prod", "logins")[0].observations) == {"gap": "crit"}
    assert st["sec"]["logins"]["example-prod"]["last_ts"] == "2026-10-05T11:59:06+03:00"  # the edge of the small window
    assert set(b.server("example-prod")["identities"]) == {"root@ED25519 SHA256:own"}


def test_logins_drops_unterminated_last_line():
    b = baseline_for("example-prod", trusted=[("203.0.113.0/24", "владелец")])
    st = fresh_state()
    torn = "2026-10-05T11:58:03.000000+03:00 h sshd[3]: Accepted publickey for root from 203.0.113.7 port 3 ssh2: ED25519 SHA256:ab"
    res = runner.collect(CFG, NOW, st, far(), ssh=FakeSsh(sec_table(auth=AUTH_OWNER + torn)), servers=["example-prod"], baseline=b)
    assert find(res, "example-prod", "logins")[0].observations == []
    assert b.server("example-prod")["identities"] == {"root@ED25519 SHA256:own": "владелец"}
    assert st["sec"]["logins"]["example-prod"]["last_ts"] == "2026-10-05T11:58:02+03:00"


def test_cores_and_eol_failures_mark_run_and_count_for_health():
    t = sec_table()
    t["cat /proc/cpuinfo"] = (126, "", "agent-ro-gate: на этом сервере нет команды: cat")
    t["cat /etc/os-release"] = (126, "", "agent-ro-gate: на этом сервере нет команды: cat")
    st = fresh_state()
    for i in range(3):
        res = runner.collect(CFG, NOW + timedelta(days=i), st, far(), ssh=FakeSsh(t), servers=["node-e"])
        assert find(res, "node-e", "cores")[0].ran is False and find(res, "node-e", "eol")[0].ran is False
    assert {"cores", "eol"} <= set(lv(find(res, "node-e", "health")[0].observations))
    ssh = FakeSsh(t)
    runner.collect(CFG, NOW + timedelta(days=2, minutes=5), st, far(), ssh=ssh, servers=["node-e"])
    assert not any(c in ("cat /proc/cpuinfo", "cat /etc/os-release") for _, c, _ in ssh.calls)  # not every 5 minutes
    t["cat /proc/cpuinfo"] = (0, "мусор\n", "")
    res = runner.collect(CFG, NOW + timedelta(days=3), st, far(), ssh=FakeSsh(t), servers=["node-e"])
    assert find(res, "node-e", "cores")[0].ran is False and "node-e" not in st["sec"]["cores"]
    assert st["sec"]["fails"]["node-e"]["cores"]["why"] == "нет строк processor"


def test_autoupdates_second_cat_refused_is_not_ran_missing_file_is_data():
    t = sec_table()
    t["cat /etc/apt/apt.conf.d/20auto-upgrades"] = (126, "", "agent-ro-gate: отказ: только точные формы")
    res = runner.collect(CFG, NOW, fresh_state(), far(), ssh=FakeSsh(t), servers=["node-e"])
    assert find(res, "node-e", "autoupdates")[0].ran is False
    assert lv(find(res, "node-e", "gate:autoupdates")[0].observations) == {"-": "warn"}
    t["cat /etc/apt/apt.conf.d/20auto-upgrades"] = (1, "", "cat: /etc/apt/apt.conf.d/20auto-upgrades: No such file or directory")
    res = runner.collect(CFG, NOW, fresh_state(), far(), ssh=FakeSsh(t), servers=["node-e"])
    au = find(res, "node-e", "autoupdates")[0]
    assert au.ran and lv(au.observations) == {"-": "warn"}


# --- capturing a baseline (capture) ---------------------------------------------------------

def test_capture_builds_server_and_prints():
    b = baseline_for(trusted=[("203.0.113.0/24", "владелец")])
    t = sec_table(auth=AUTH_OWNER + AUTH_EVIL
                  + "2026-10-05T11:58:04+03:00 h sshd[4]: Accepted publickey for agent-ro from 203.0.113.10 port 4 ssh2: ED25519 SHA256:ro\n"
                  + "2026-10-05T11:58:05+03:00 h sshd[5]: Accepted publickey for root from 203.0.113.10 port 5 ssh2: ED25519 SHA256:exec\n"
                  + "2026-10-05T11:58:06+03:00 h sshd[6]: Accepted password for root from 203.0.113.7 port 6 ssh2\n"
                  + "2026-10-05T11:58:07+03:00 h sshd[7]: Accepted publickey for www-data from 203.0.113.10 port 7 ssh2: ED25519 SHA256:web\n")
    lines, captured = runner.capture(CFG, NOW, b, ["node-f"], ssh=FakeSsh(t), clock=runner.time.monotonic)
    srv = captured["node-f"]
    assert srv["identities"] == {"root@ED25519 SHA256:own": "владелец", "agent-ro@ED25519 SHA256:ro": "stage",
                                 "root@ED25519 SHA256:exec": "stage"}  # a password is not enrolled even from a trusted address
    # the factory host's IP is trusted only for agent-ro and the server's exec user: a web shell there — "NOT accepted"
    assert "НЕ принята: www-data@ED25519 SHA256:web с 203.0.113.10" in "\n".join(lines)
    assert srv["ports"] == {"tcp:0.0.0.0:22": "sshd"} and srv["snapshot"]["users:root"] == "0 /bin/bash"
    assert srv["wgpeers"] == [] and srv["taken_at"] == NOW.isoformat()
    assert not b.has("node-f")
    text = "\n".join(lines)
    assert "node-f: эталон снят" in text and "НЕ принята: root@ED25519 SHA256:evil с 198.51.100.9" in text
    assert "личность «владелец»: root@ED25519 SHA256:own" in text and "слушатель tcp:0.0.0.0:22 (sshd)" in text
    assert "root@password" not in text.split("НЕ принята")[0]  # the password is not in the accepted list


def test_capture_stage_ip_trusts_exec_user_from_inventory():
    """On node-a the exec user is ubuntu (inventory.json): its key from the factory host's IP — "stage", root from that IP — not."""
    auth = ("2026-10-05T11:58:05+03:00 h sshd[5]: Accepted publickey for ubuntu from 203.0.113.10 port 5 ssh2: ED25519 SHA256:exec\n"
            "2026-10-05T11:58:06+03:00 h sshd[6]: Accepted publickey for root from 203.0.113.10 port 6 ssh2: ED25519 SHA256:x\n")
    lines, captured = runner.capture(CFG, NOW, baseline_for(), ["node-a"], ssh=FakeSsh(sec_table(auth=auth)),
                                     clock=runner.time.monotonic)
    assert captured["node-a"]["identities"] == {"ubuntu@ED25519 SHA256:exec": "stage"}
    assert "НЕ принята: root@ED25519 SHA256:x с 203.0.113.10" in "\n".join(lines)
    # an explicit map of exec users (no inventory); a server missing from the map — root
    lines, captured = runner.capture(CFG, NOW, baseline_for(), ["node-a", "zz"], ssh=FakeSsh(sec_table(auth=auth)),
                                     clock=runner.time.monotonic, exec_users={"node-a": "root"})
    assert captured["node-a"]["identities"] == {"root@ED25519 SHA256:x": "stage"}
    assert captured["zz"]["identities"] == {"root@ED25519 SHA256:x": "stage"}


def test_capture_failure_skips_server_and_optional_parts_warn():
    t = sec_table()
    t["agent-sec-snapshot"] = (126, "", "agent-ro-gate: отказ: только точные формы")
    lines, captured = runner.capture(CFG, NOW, Baseline.empty(), ["node-f"], ssh=FakeSsh(t), clock=runner.time.monotonic)
    assert captured == {} and "node-f: эталон НЕ снят" in lines[0] and "snapshot" in lines[0]
    t = sec_table()
    t[TAIL] = (1, "", "No such file")
    t[JOURNAL] = (126, "", "agent-ro-gate: нет")
    t["wg show all allowed-ips"] = (126, "", "agent-ro-gate: на этом сервере нет команды: wg")
    lines, captured = runner.capture(CFG, NOW, Baseline.empty(), ["node-f"], ssh=FakeSsh(t), clock=runner.time.monotonic)
    assert captured["node-f"]["identities"] == {} and captured["node-f"]["wgpeers"] == []
    text = "\n".join(lines)
    assert "входы не прочитаны" in text and "пиры не прочитаны" in text


def test_capture_keeps_old_values_for_errored_sections_and_prints_diff():
    b = baseline_for("node-f")
    snap = SNAP_OK.replace("sshd\tport\t22\nsshd\tpermitrootlogin\tprohibit-password\nsshd\tpasswordauthentication\tno\n",
                           "sshd\terror\tRuntimeError: x\n") + "users\tevil\t1001 /bin/bash\n"
    t = sec_table(snap=snap)
    t["ss -tlnpu"] = (0, SS_OK + 'tcp   LISTEN 0      1      0.0.0.0:4444        0.0.0.0:*     users:(("nc",pid=9,fd=3))\n', "")
    lines, captured = runner.capture(CFG, NOW, b, ["node-f"], ssh=FakeSsh(t), clock=runner.time.monotonic)
    s = captured["node-f"]["snapshot"]
    assert s["sshd:port"] == "22" and s["sshd:permitrootlogin"] == "prohibit-password" and s["users:evil"] == "1001 /bin/bash"
    text = "\n".join(lines)
    assert "секции с ошибкой (взяты из прежнего эталона): sshd" in text
    assert "отличия от прежнего эталона" in text and "пользователь evil: добавлено, 1001 /bin/bash" in text and "новый слушатель tcp:0.0.0.0:4444" in text


def test_capture_runs_servers_in_parallel_within_deadline():
    t = sec_table()
    calls = []

    def ssh(alias, argv, timeout):
        calls.append(alias)
        return FakeSsh(t)(alias, argv, timeout)
    lines, captured = runner.capture(CFG, NOW, Baseline.empty(), ["a", "b", "c"], ssh=ssh, clock=runner.time.monotonic)
    assert set(captured) == {"a", "b", "c"} and len(lines) >= 3


# --- review round 1 of the capture ----------------------------------------------------------

AUTH_OWN_UNTRUSTED = ("2026-10-05T11:58:07+03:00 h sshd[7]: Accepted publickey for root from 198.51.100.20 port 7 ssh2: "
                      "ED25519 SHA256:own\n")
AUTH_PW = "2026-10-05T11:58:0{s}+03:00 h sshd[8]: Accepted password for root from {ip} port 8 ssh2\n"


def test_capture_rejected_excludes_accepted_and_old_identities():
    b = baseline_for(trusted=[("203.0.113.0/24", "владелец")])
    lines, captured = runner.capture(CFG, NOW, b, ["node-f"], ssh=FakeSsh(sec_table(auth=AUTH_OWNER + AUTH_OWN_UNTRUSTED)),
                                     clock=runner.time.monotonic)
    text = "\n".join(lines)
    assert captured["node-f"]["identities"] == {"root@ED25519 SHA256:own": "владелец"}
    assert "НЕ принята: root@ED25519 SHA256:own" not in text
    b = baseline_for("node-f")
    b.server("node-f")["identities"]["root@ED25519 SHA256:evil"] = "подрядчик"  # accepted earlier through accept
    lines, captured = runner.capture(CFG, NOW, b, ["node-f"], ssh=FakeSsh(sec_table(auth=AUTH_EVIL)),
                                     clock=runner.time.monotonic)
    assert "НЕ принята" not in "\n".join(lines)


def test_capture_wg_failure_keeps_old_peers():
    b = baseline_for("node-f")
    old_peers = ["wg0:" + "A" * 43 + "=", "wg0:" + "B" * 43 + "="]
    b.server("node-f")["wgpeers"] = list(old_peers)
    t = sec_table()
    t["wg show all allowed-ips"] = (126, "", "agent-ro-gate: на этом сервере нет команды: wg")
    lines, captured = runner.capture(CFG, NOW, b, ["node-f"], ssh=FakeSsh(t), clock=runner.time.monotonic)
    assert captured["node-f"]["wgpeers"] == old_peers
    text = "\n".join(lines)
    assert "пиры не прочитаны (взяты из прежнего эталона): agent-ro-gate" in text
    assert "новый пир" not in text


def test_capture_prints_section_counts_and_warnings_first():
    t = sec_table()
    t["wg show all allowed-ips"] = (126, "", "agent-ro-gate: на этом сервере нет команды: wg")
    lines, _ = runner.capture(CFG, NOW, Baseline.empty(), ["node-f"], ssh=FakeSsh(t), clock=runner.time.monotonic)
    assert lines[0].endswith("объектов снимка 13 (users 1, groups 1, keys 1, sudoers 1, shadow 1, sshd 3, cron 1, "
                             "persist 1, units 1, fw 2)")
    assert lines[1].startswith("  пиры не прочитаны")


def test_capture_weak_identity_rejected_only_from_untrusted():
    b = baseline_for(trusted=[("203.0.113.0/24", "владелец")])
    auth = AUTH_PW.format(s=8, ip="198.51.100.9") + AUTH_PW.format(s=9, ip="203.0.113.7")
    lines, captured = runner.capture(CFG, NOW, b, ["node-f"], ssh=FakeSsh(sec_table(auth=auth)), clock=runner.time.monotonic)
    text = "\n".join(lines)
    assert "НЕ принята: root@password с 198.51.100.9" in text and "203.0.113.7" not in text
    assert captured["node-f"]["identities"] == {}
