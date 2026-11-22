"""The poll of the servers: commands through ssh-ro, parsing by the checks parsers, results for the registry.

SSH is injected as the parameter `ssh` (a fake in tests). The poll's time is bounded by one `deadline`
(by `clock`, time.monotonic by default)."""
import fnmatch
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import checks, seccheck
from .baseline import Baseline
from .model import CheckResult, Observation
from .registry import schedule_name, split_key

COMMON = str(Path(__file__).resolve().parents[4] / "common")
if COMMON not in sys.path:
    sys.path.append(COMMON)
from kd import monitor_settings  # noqa: E402

SSH_RO = "/usr/local/bin/ssh-ro"
GATE_FAIL = (2, 124, 255)  # disabled / timeout / SSH did not connect: the command never reached the gateway

DF = ["df", "-P", "-x", "tmpfs", "-x", "devtmpfs", "-x", "overlay", "-x", "squashfs"]
MYSQL_PROC = "SELECT id, user, command, time, state, LEFT(info, 40) FROM information_schema.processlist"
MYSQL_STATUS = 'SHOW GLOBAL STATUS LIKE "Threads_connected"'
MYSQL_VARS = 'SHOW VARIABLES LIKE "max_connections"'
# the tables come from the config (heartbeat.table, monitoring.consumers_table; load_config has checked them)
HEARTBEAT_SQL = "SELECT server_name, TIMESTAMPDIFF(MINUTE, MAX(checked_at), NOW()) FROM {} GROUP BY server_name"
CONSUMERS_SQL = "SELECT TIMESTAMPDIFF(MINUTE, MAX(last_heartbeat), NOW()) FROM {}"
SNAPSHOT = ["agent-sec-snapshot"]
SS_LISTEN = ["ss", "-tlnpu"]
WG_PEERS = ["wg", "show", "all", "allowed-ips"]
APT_UPGRADABLE = ["apt", "list", "--upgradable"]
DNF_SECURITY = ["dnf", "-q", "-C", "check-update", "--security"]
NEEDRESTART = ["needrestart", "-b", "-r", "l"]
REBOOT_REQUIRED = ["cat", "/run/reboot-required"]  # without needrestart: the kernel's postinst hooks put the file there
UNITS_FAILED = ["systemctl", "list-units", "--failed", "--no-legend", "--plain"]
UNITS_SHOW = ["systemctl", "show", "-p", "Id,LoadState,ActiveState"]  # + the server's required units
DNF_NEEDS_RESTARTING = ["dnf", "needs-restarting", "-r"]
APT_TIMERS = ["systemctl", "is-enabled", "apt-daily.timer", "apt-daily-upgrade.timer", "unattended-upgrades"]
DNF_TIMERS = ["systemctl", "is-enabled", "dnf-automatic-install.timer", "dnf-automatic.timer"]
AUTO_UPGRADES = ["cat", "/etc/apt/apt.conf.d/20auto-upgrades"]
DNF_AUTOMATIC = ["cat", "/etc/dnf/automatic.conf"]
OS_RELEASE = ["cat", "/etc/os-release"]
CPUINFO = ["cat", "/proc/cpuinfo"]
SEC_NAMES = ["sec:" + s for s in seccheck.SECTIONS]
JOURNAL_HINT = "Hint: You are currently not seeing messages"  # journalctl without rights: code 0 and nothing
GATE_MARK = "\n[agent-ro-gate: "  # the gateway's service lines after the command's output (agent_ro_gate.main)
TRUNC_MARK = "[agent-ro-gate: вывод обрезан до "  # the gateway kept the start of an output > 1 MiB, code 0


def run_ssh_ro(alias: str, argv: List[str], timeout: float) -> Tuple[int, str, str]:
    """ssh-ro <alias> <argv…> → (rc, stdout, stderr). A timeout → 124."""
    try:
        p = subprocess.run([SSH_RO, alias] + list(argv), capture_output=True, timeout=max(timeout, 1))
    except subprocess.TimeoutExpired:
        return 124, "", "таймаут ssh-ro"
    except OSError as exc:
        return 127, "", f"ssh-ro не запущен: {exc}"
    return (p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace"))


def _first_line(text: str, limit: int = 200) -> str:
    for ln in text.splitlines():
        if ln.strip():
            return ln.strip()[:limit]
    return "отказ шлюза"


class _Ctx:
    """What one poll shares: the config, the time, the ssh call bounded by deadline, the baseline and state["sec"]."""

    def __init__(self, cfg, now, state, deadline, ssh, clock, baseline, force=False):
        self.cfg, self.now, self.state = cfg, now, state
        self.deadline, self.ssh, self.clock = deadline, ssh, clock
        self.baseline = baseline
        self.sec = seccheck.sec_state(state)
        self.fixed = _fixed_marks(state)
        self.force = force
        self.starved = set()  # servers whose call did not happen in this poll: the budget ran out
        self.zones = {}  # server → its zone (the inventory's "tz"); none — the host's zone and the clock shift

    def call(self, server, argv) -> Optional[Tuple[int, str, str]]:
        left = self.deadline - self.clock()
        if left <= 0:
            self.starved.add(server)
            return None
        return self.ssh(server, argv, min(self.cfg["call_timeout_s"], left))

    def due(self, server, check) -> bool:
        """Due by every_min — or off schedule: an open problem of this check has a "fixed" mark newer than the last run
        (after the recheck last_run is newer than the mark — on schedule again)."""
        if self.force:
            return True
        every = self.cfg["every_min"].get(check)
        last = self.state["last_run"].get(f"{server}:{check}") if self.state.get("last_run") else None
        if not every or not last:
            return True
        try:
            last = datetime.fromisoformat(last)
        except ValueError:
            return True
        fixed = self.fixed.get(f"{server}:{check}")
        try:
            if fixed is not None and last < fixed:
                return True
        except TypeError:  # the mark's time has no zone — the mark is ignored rather than running the check every tick
            pass
        return self.now - last >= timedelta(minutes=every)


def _fixed_marks(state) -> dict:
    """{"server:check" (as in last_run) → the latest time of a "fixed" mark} over the open problems.
    The server is the source's (the heartbeat of every node is read on the database server). Broken records are skipped."""
    out = {}
    problems = state.get("problems")
    for p in problems.values() if isinstance(problems, dict) else ():
        if not isinstance(p, dict) or not isinstance(p.get("fixed"), dict):
            continue
        try:
            at = datetime.fromisoformat(p["fixed"]["at"])
            check, subject = split_key(p)[1:]
            name = f"{p['source'][0]}:{schedule_name(check, subject)}"
            if name not in out or at > out[name]:
                out[name] = at
        except (AttributeError, KeyError, IndexError, TypeError, ValueError):
            continue
    return out


def _gate_results(server, check, rc, err) -> List[CheckResult]:
    """The source result `gate:<check>`: a gateway refusal opens a problem, a regular answer closes it."""
    src = "gate:" + check
    if rc == 126 and "нет команды" not in err:
        o = Observation(server, src, "-", "warn", "отказ", _first_line(err))
        return [CheckResult(server, src, True, [o])]
    if rc in GATE_FAIL:
        return []
    return [CheckResult(server, src, True, [])]


def _skipped(rc, err) -> bool:
    """The check gave no data: not 0, or a refusal / "no such command"."""
    return rc != 0


def _simple(ctx: _Ctx, server, check, argv, parse, **kw) -> List[CheckResult]:
    if not ctx.due(server, check):
        return [CheckResult(server, check, False, [])]
    r = ctx.call(server, argv)
    if r is None:
        return [CheckResult(server, check, False, [])]
    rc, out, err = r
    gate = _gate_results(server, check, rc, err)
    if _skipped(rc, err):
        return [CheckResult(server, check, False, [])] + gate
    ctx.state["last_run"][f"{server}:{check}"] = ctx.now.isoformat()
    return [CheckResult(server, check, True, parse(server, out, ctx.cfg, ctx.now, **kw))] + gate


def _units(ctx: _Ctx, server) -> List[CheckResult]:
    """Failed units. The units_confirm globs (cron-*: the next run usually mends one failed run) count only when the
    unit had failed in the previous run too or the problem is already open; the previous run is in sec.units_seen."""
    res = _simple(ctx, server, "units", UNITS_FAILED, checks.units)
    if not res[0].ran:
        return res
    pats = ctx.cfg.get("units_confirm", [])
    confirm = [o for o in res[0].observations if any(fnmatch.fnmatchcase(o.subject, p) for p in pats)]
    seen = set(ctx.sec["units_seen"].get(server, []))
    ctx.sec["units_seen"][server] = sorted(o.subject for o in confirm)
    drop = {o.subject for o in confirm if o.subject not in seen and o.key not in ctx.state["problems"]}
    obs = [o for o in res[0].observations if o.subject not in drop]
    required = checks.required_units(server, ctx.cfg)
    if not required:
        return [CheckResult(server, "units", True, obs)] + res[1:]
    return _required(ctx, server, required, obs, res[1:])


def _required(ctx: _Ctx, server, required, obs, gate) -> List[CheckResult]:
    """The required units: one systemctl show call per server. No data (or not about every unit) — the whole units check
    is ran=False (otherwise the open problems of required units would close without data) and a failure for health."""
    r = ctx.call(server, UNITS_SHOW + required)
    if r is None:
        return [CheckResult(server, "units", False, [])] + gate
    rc, out, err = r
    states = checks.unit_states(out) if rc == 0 else {}
    lost = [n for n in required if n not in states]
    if rc != 0 or lost:  # the unit is missing from the output — there is no data about it: "healthy" would close the problem blindly
        _fail(ctx, server, "units", _first_line(err or out) if rc != 0 else "нет в выводе systemctl show: " + ", ".join(lost))
        refused = _gate_results(server, "units", rc, err)
        return [CheckResult(server, "units", False, [])] + (refused if any(g.observations for g in refused) else gate)
    _ok(ctx, server, "units")
    down = ctx.sec["units_down"].setdefault(server, {})
    for name in required:
        if states[name].get("ActiveState") != "active":
            down[name] = down.get(name, 0) + 1
        else:
            down.pop(name, None)
    for name in [n for n in down if n not in required]:
        del down[name]
    extra = checks.required_obs(server, states, down, ctx.cfg, skip={o.subject for o in obs},
                                open_keys=ctx.state["problems"])
    return [CheckResult(server, "units", True, obs + extra)] + gate


def _mysql(ctx: _Ctx, server) -> List[CheckResult]:
    parts = []
    for sql in (MYSQL_PROC, MYSQL_STATUS, MYSQL_VARS):
        r = ctx.call(server, ["mysql", "-N", "-e", sql])
        if r is None:
            return [CheckResult(server, "mysql", False, [])]
        rc, out, err = r
        if rc == 1 and any(f"ERROR {n}" in out + err for n in (2002, 2003, 2013)):
            return [CheckResult(server, "mysql", True, checks.mysql_down(server, ctx.cfg))] \
                + _gate_results(server, "mysql", 0, "")
        if rc != 0:
            return [CheckResult(server, "mysql", False, [])] + _gate_results(server, "mysql", rc, err)
        parts.append(out)
    return [CheckResult(server, "mysql", True,
                        checks.mysql(server, checks.MYSQL_SEP.join(parts), ctx.cfg, ctx.now))] \
        + _gate_results(server, "mysql", 0, "")


def _mem(ctx: _Ctx, server) -> List[CheckResult]:
    """meminfo + vmstat are required; PSI — when the kernel provides it (AlmaLinux 8 does not).
    The samples of the swap counter are in state["swap_io"][server]."""
    r = ctx.call(server, ["cat", "/proc/meminfo", "/proc/vmstat"])
    if r is None:
        return [CheckResult(server, "mem", False, [])]
    rc, out, err = r
    gate = _gate_results(server, "mem", rc, err)
    if rc != 0:
        return [CheckResult(server, "mem", False, [])] + gate
    p = ctx.call(server, ["cat", "/proc/pressure/memory"])
    psi = p[1] if p and p[0] == 0 else ""
    rate, ctx.state["swap_io"][server] = checks.swap_rate(ctx.state["swap_io"].get(server), out, ctx.now, ctx.cfg)
    return [CheckResult(server, "mem", True,
                        checks.mem(server, out, ctx.cfg, ctx.now, psi=psi, swap_rate=rate))] + gate


def _monitoring(ctx: _Ctx, server) -> List[CheckResult]:
    c = ctx.cfg["monitoring"]
    r = ctx.call(server, ["cat", c["problems_file"]])
    none = [CheckResult(server, "monitoring_alive", False, []),
            CheckResult(server, "monitoring_file", False, [])]
    if r is None:
        return none
    rc, out, err = r
    gate = _gate_results(server, "monitoring", rc, err)
    if rc == 0:
        alive = checks.monitoring_alive(server, out, ctx.cfg, ctx.now, source="file", zone=ctx.zones.get(server))
        fresh = checks.monitoring_file(server, out, ctx.cfg, ctx.now, last_ts=ctx.state.get("monitoring_ts"),
                                       zone=ctx.zones.get(server))
        file_res = CheckResult(server, "monitoring_file", fresh is not None, fresh or [])
        if fresh is not None:
            ctx.state["monitoring_ts"] = checks.parse_problems_file(out)["ts"]
        return [CheckResult(server, "monitoring_alive", True, alive), file_res] + gate
    if rc == 1 and "No such file" in out + err:
        r = ctx.call(server, ["tail", "-n", "50", c["alert_log"]])
        if r is None or r[0] != 0:
            return none + (_gate_results(server, "monitoring", r[0], r[2]) if r else [])
        alive = checks.monitoring_alive(server, r[1], ctx.cfg, ctx.now, source="log", zone=ctx.zones.get(server))
        return [CheckResult(server, "monitoring_alive", True, alive),
                CheckResult(server, "monitoring_file", False, [])] + _gate_results(server, "monitoring", 0, "")
    return none + gate


def _mark(ctx: _Ctx, server, check) -> None:
    ctx.state["last_run"][f"{server}:{check}"] = ctx.now.isoformat()


def _fail(ctx: _Ctx, server, check, why: str = "") -> None:
    """The check gave no data through the server's or the gateway's fault (not by schedule and not by budget): the counter for health."""
    f = ctx.sec["fails"].setdefault(server, {}).setdefault(check, {"n": 0, "why": ""})
    f["n"] += 1
    f["why"] = why[:160]


def _ok(ctx: _Ctx, server, check) -> None:
    ctx.sec["fails"].get(server, {}).pop(check, None)


def _health(ctx: _Ctx, server) -> CheckResult:
    """⚠️ for each check that has not worked for health_after runs in a row (the snapshot is not installed, there is no
    auth.log, dnf hangs). The server ran out of budget — some checks never reached it and the failure counters are
    old: health did not run (otherwise the recheck of a "fixed" health would give a false "did not help")."""
    if server in ctx.starved:
        return CheckResult(server, "health", False, [])
    limit = ctx.cfg["security"]["health_after"]
    off = monitor_settings.disabled(ctx.cfg, server)  # one the owner disabled is not "not working"
    obs = [checks._obs(server, "health", check, "warn", f"{f['n']} раз", f"проверка {check} не работает: {f['why'] or 'нет данных'}")
           for check, f in sorted(ctx.sec["fails"].get(server, {}).items())
           if f["n"] >= limit and monitor_settings.job_of(check) not in off]
    return CheckResult(server, "health", True, obs)


def _dnf(ctx: _Ctx, server) -> bool:
    return server in ctx.cfg["security"]["dnf_servers"]


def _wg_server(cfg, server) -> bool:
    return server in cfg["vpn_nodes"] or server in cfg["security"]["wg_servers"]


def _auth_argv(cfg, server, journal: bool, n: int) -> List[str]:
    if journal or server in cfg["security"]["journal_servers"]:
        return ["journalctl", "_COMM=sshd", "+", "_COMM=sshd-session", "-n", str(n), "--no-pager", "-o", "short-iso"]
    return ["tail", "-n", str(n), "/var/log/auth.log"]


def _auth_text(raw: str) -> Tuple[str, bool]:
    """(only whole lines of the journal, whether the gateway cut the output). The gateway's service lines are cut off;
    a last line without `\\n` is torn (the cut is by bytes) and dropped: a fragment `SHA256:ab` would give an invented identity."""
    i = raw.find(GATE_MARK)
    body = raw if i < 0 else raw[:i]
    if not body.endswith("\n"):
        body = body[:body.rfind("\n") + 1]
    return body, TRUNC_MARK in raw


def _gated(ctx: _Ctx, server, check, argv, build, due=True) -> List[CheckResult]:
    """A check against the baseline: without a baseline — ran=False; then as _periodic."""
    if not ctx.baseline.has(server):
        return [CheckResult(server, check, False, [])]
    return _periodic(ctx, server, check, argv, build, due)


def _periodic(ctx: _Ctx, server, check, argv, build, due=True) -> List[CheckResult]:
    """A periodic check: not due — ran=False; build(out) → observations; a failure — _fail and ran=False."""
    if due and not ctx.due(server, check):
        return [CheckResult(server, check, False, [])]
    r = ctx.call(server, argv)
    if r is None:
        return [CheckResult(server, check, False, [])]
    rc, out, err = r
    gate = _gate_results(server, check, rc, err)
    _mark(ctx, server, check)  # on a failure too: the repeat comes after the period, not every 5 minutes
    if rc != 0:
        _fail(ctx, server, check, _first_line(err or out))
        return [CheckResult(server, check, False, [])] + gate
    _ok(ctx, server, check)
    return [CheckResult(server, check, True, build(out))] + gate


def _logins(ctx: _Ctx, server, shift) -> List[CheckResult]:
    """Every tick. No auth.log — the journal next. A window that does not overlap last_ts — a repeat with a larger window,
    otherwise the observation `gap` (⚠️, 🔴 on critical servers)."""
    if not ctx.baseline.has(server):
        return [CheckResult(server, "logins", False, [])]
    c = ctx.cfg["security"]
    st = ctx.sec["logins"].setdefault(server, {"last_ts": None, "last_lines": [], "journal": False})
    r = ctx.call(server, _auth_argv(ctx.cfg, server, st["journal"], c["auth_log_lines"]))
    if r is None:
        return [CheckResult(server, "logins", False, [])]
    rc, out, err = r
    if rc != 0 and not st["journal"] and "No such file" in out + err:
        st["journal"] = True
        r = ctx.call(server, _auth_argv(ctx.cfg, server, True, c["auth_log_lines"]))
        if r is None:
            return [CheckResult(server, "logins", False, [])]
        rc, out, err = r
    gate = _gate_results(server, "logins", rc, err)
    if rc != 0 or JOURNAL_HINT in out:
        _fail(ctx, server, "logins", _first_line(err or out))
        return [CheckResult(server, "logins", False, [])] + gate
    zone = ctx.zones.get(server)
    auth = seccheck.parse_auth(_auth_text(out)[0], st["last_ts"], st["last_lines"], ctx.now, shift, zone)
    extra = []
    if st["last_ts"] and auth.first_ts and auth.first_ts > st["last_ts"] and auth.lines >= c["auth_log_lines"]:
        r2 = ctx.call(server, _auth_argv(ctx.cfg, server, st["journal"], c["auth_log_lines_max"]))
        text2, cut2 = _auth_text(r2[1]) if r2 is not None else ("", True)
        if r2 is not None and r2[0] == 0 and not cut2:  # cut by the gateway — no new lines: the same as a failed repeat
            auth = seccheck.parse_auth(text2, st["last_ts"], st["last_lines"], ctx.now, shift, zone)
        if auth.first_ts and auth.first_ts > st["last_ts"]:
            since = st["last_ts"][11:16]
            extra.append(Observation(server, "logins", "gap", "crit" if server in c["critical_servers"] else "warn",
                                     f"с {since}", f"журнал входов переполнен: часть входов после {since} не разобрана"))
    st["last_ts"], st["last_lines"] = auth.last_ts, auth.last_lines
    _ok(ctx, server, "logins")
    _mark(ctx, server, "logins")
    obs = seccheck.logins_obs(server, auth, ctx.sec, ctx.baseline, ctx.cfg, ctx.now) + extra
    return [CheckResult(server, "logins", True, obs)] + gate


def _in_plan_window(ctx: _Ctx, server) -> bool:
    """A step of an approved plan is changing the server: its changes come as the window's."""
    return server in ctx.sec.get("plan_windows", {})


def _ports(ctx: _Ctx, server) -> List[CheckResult]:
    if _in_plan_window(ctx, server):
        return [CheckResult(server, "ports", False, [])]
    return _gated(ctx, server, "ports", SS_LISTEN, lambda out: seccheck.ports_obs(
        server, seccheck.parse_listeners(out), ctx.baseline.server(server)["ports"], ctx.cfg))


def _wgpeers(ctx: _Ctx, server) -> List[CheckResult]:
    if _in_plan_window(ctx, server):
        return [CheckResult(server, "wgpeers", False, [])]
    return _gated(ctx, server, "wgpeers", WG_PEERS, lambda out: seccheck.wgpeers_obs(
        server, seccheck.parse_wgpeers(out), ctx.baseline.server(server)["wgpeers"]))


def expect_window(sec: dict, server, cfg, now) -> Optional[dict]:
    """The server's open release window (kd-monitor expect begin) or None: there is none, it expired or is broken."""
    w = (sec.get("expect") or {}).get(server)
    try:
        age = now - datetime.fromisoformat(w["at"])
    except (TypeError, KeyError, ValueError):
        return None
    return w if age <= timedelta(minutes=cfg["security"]["expect_max_min"]) else None


def take_snapshot(cfg, server, ssh=run_ssh_ro):
    """One security snapshot outside a tick (a release window): ((objects, sections with an error), "") or (None, the reason)."""
    rc, out, err = ssh(server, SNAPSHOT, cfg["call_timeout_s"])
    parsed = seccheck.parse_snapshot(out) if rc == 0 else None
    if parsed is None:
        return None, _first_line(err or out)
    return parsed, ""


def _sec(ctx: _Ctx, server) -> List[CheckResult]:
    """The snapshot: a source per section (a section's error — ran=False for it alone) + the posture; the snapshot itself goes to sec.current."""
    names = SEC_NAMES + ["posture"]
    none = [CheckResult(server, n, False, []) for n in names]
    if expect_window(ctx.sec, server, ctx.cfg, ctx.now) or _in_plan_window(ctx, server)             or not ctx.due(server, "sec"):  # a release window, a plan step's window
        return none
    r = ctx.call(server, SNAPSHOT)
    if r is None:
        return none
    rc, out, err = r
    gate = _gate_results(server, "sec", rc, err)
    _mark(ctx, server, "sec")  # on a failure too: the repeat comes in an hour
    parsed = seccheck.parse_snapshot(out) if rc == 0 else None
    if parsed is None:
        _fail(ctx, server, "sec", _first_line(err or out))
        return none + gate
    _ok(ctx, server, "sec")
    items, errors = parsed
    for s in seccheck.SECTIONS:
        if s in errors:
            _fail(ctx, server, "sec:" + s, errors[s])
        else:
            _ok(ctx, server, "sec:" + s)
    ctx.sec["current"][server] = {"at": ctx.now.isoformat(), "text": out}
    has = ctx.baseline.has(server)
    diff = seccheck.snapshot_obs(server, items, errors, ctx.baseline.server(server)["snapshot"], ctx.cfg) if has else []
    res = [CheckResult(server, "sec:" + s, has and s not in errors, [o for o in diff if o.check == "sec:" + s])
           for s in seccheck.SECTIONS]
    posture_ok = not ({"sshd", "fw"} & set(errors))
    res.append(CheckResult(server, "posture", posture_ok, seccheck.posture_obs(server, items, ctx.cfg) if posture_ok else []))
    return res + gate


def _updates(ctx: _Ctx, server) -> List[CheckResult]:
    if not ctx.due(server, "updates"):
        return [CheckResult(server, "updates", False, [])]
    dnf = _dnf(ctx, server)
    r = ctx.call(server, DNF_SECURITY if dnf else APT_UPGRADABLE)
    if r is None:
        return [CheckResult(server, "updates", False, [])]
    rc, out, err = r
    gate = _gate_results(server, "updates", rc, err)
    _mark(ctx, server, "updates")  # on a failure too: dnf on a dead mirror must not be poked every 5 minutes
    if rc not in ((0, 100) if dnf else (0,)):
        _fail(ctx, server, "updates", _first_line(err or out))
        return [CheckResult(server, "updates", False, [])] + gate
    pending = (seccheck.parse_dnf_security(out) if rc == 100 else {}) if dnf else seccheck.parse_apt_security(out)
    _ok(ctx, server, "updates")
    return [CheckResult(server, "updates", True, seccheck.updates_obs(server, pending, ctx.sec, ctx.cfg, ctx.now))] + gate


def _reboot_required(ctx: _Ctx, server, gate) -> List[CheckResult]:
    """Without needrestart (it is not wanted — it restarts services itself): the kernel's postinst hooks
    (unattended-upgrades, update-notifier) put the file /run/reboot-required. Present — needed, "No such file" — not needed."""
    r = ctx.call(server, REBOOT_REQUIRED)
    if r is None:
        return [CheckResult(server, "reboot", False, [])] + gate
    rc, out, err = r
    if rc == 0:
        pending = True
    elif rc == 1 and "No such file" in out + err:
        pending = False
    else:
        _fail(ctx, server, "reboot", "нет needrestart, а /run/reboot-required не читается: " + _first_line(err or out))
        return [CheckResult(server, "reboot", False, [])] + gate  # an old gateway's refusal is already in health's text
    _ok(ctx, server, "reboot")
    return [CheckResult(server, "reboot", True, seccheck.reboot_obs(server, pending, ctx.sec, ctx.cfg, ctx.now))] + gate


def _reboot(ctx: _Ctx, server) -> List[CheckResult]:
    if not ctx.due(server, "reboot"):
        return [CheckResult(server, "reboot", False, [])]
    dnf = _dnf(ctx, server)
    r = ctx.call(server, DNF_NEEDS_RESTARTING if dnf else NEEDRESTART)
    if r is None:
        return [CheckResult(server, "reboot", False, [])]
    rc, out, err = r
    gate = _gate_results(server, "reboot", rc, err)
    _mark(ctx, server, "reboot")
    if not dnf and rc == 126 and "нет команды" in err:
        return _reboot_required(ctx, server, gate)
    pending = seccheck.parse_needs_restarting(rc, out) if dnf else (seccheck.parse_needrestart(out) if rc == 0 else None)
    if pending is None:
        _fail(ctx, server, "reboot", _first_line(err or out))
        return [CheckResult(server, "reboot", False, [])] + gate
    _ok(ctx, server, "reboot")
    return [CheckResult(server, "reboot", True, seccheck.reboot_obs(server, pending, ctx.sec, ctx.cfg, ctx.now))] + gate


def _autoupdates(ctx: _Ctx, server) -> List[CheckResult]:
    if not ctx.due(server, "autoupdates"):
        return [CheckResult(server, "autoupdates", False, [])]
    dnf = _dnf(ctx, server)
    r = ctx.call(server, DNF_TIMERS if dnf else APT_TIMERS)
    if r is None:
        return [CheckResult(server, "autoupdates", False, [])]
    rc, out, err = r
    gate = _gate_results(server, "autoupdates", rc, err)
    _mark(ctx, server, "autoupdates")
    if rc in GATE_FAIL or rc == 126:  # is-enabled → code 1 for disabled or a missing unit: that is data, not a refusal
        _fail(ctx, server, "autoupdates", _first_line(err or out))
        return [CheckResult(server, "autoupdates", False, [])] + gate
    r2 = ctx.call(server, DNF_AUTOMATIC if dnf else AUTO_UPGRADES)
    if r2 is None:
        return [CheckResult(server, "autoupdates", False, [])]
    rc2, out2, err2 = r2
    missing = rc2 == 1 and "No such file" in out2 + err2  # no config is data: automatic updates are not set up
    if rc2 != 0 and not missing:  # a gateway refusal, 127…: an empty conf would give a false "disabled"
        _fail(ctx, server, "autoupdates", _first_line(err2 or out2))
        return [CheckResult(server, "autoupdates", False, [])] + _gate_results(server, "autoupdates", rc2, err2)
    _ok(ctx, server, "autoupdates")
    conf = out2 if rc2 == 0 else ""
    return [CheckResult(server, "autoupdates", True, seccheck.autoupdates_obs(server, out, conf, ctx.cfg, dnf=dnf))] + gate


def _cores(ctx: _Ctx, server) -> List[CheckResult]:
    if not ctx.due(server, "cores"):
        return [CheckResult(server, "cores", False, [])]
    r = ctx.call(server, CPUINFO)
    if r is None:
        return [CheckResult(server, "cores", False, [])]
    rc, out, err = r
    gate = _gate_results(server, "cores", rc, err)
    _mark(ctx, server, "cores")  # on a failure too: the repeat comes in a day
    n = seccheck.parse_cores(out) if rc == 0 else 0
    if not n:
        _fail(ctx, server, "cores", _first_line(err or out) if rc != 0 else "нет строк processor")
        return [CheckResult(server, "cores", False, [])] + gate
    ctx.sec["cores"][server] = n
    _ok(ctx, server, "cores")
    return [CheckResult(server, "cores", True, [])] + gate


def _eol(ctx: _Ctx, server) -> List[CheckResult]:
    return _periodic(ctx, server, "eol", OS_RELEASE, lambda out: seccheck.eol_obs(
        server, *seccheck.parse_os_release(out), ctx.cfg, ctx.now))


def _crash(server, check, exc) -> None:
    print(f"kd-monitor: проверка {server}:{check} упала: {type(exc).__name__}: {exc}", file=sys.stderr)


def _reach(ctx: _Ctx, server):
    try:
        return _reach_raw(ctx, server)
    except Exception as exc:  # noqa: BLE001 — one check must not crash the poll
        _crash(server, "reach", exc)
        return [CheckResult(server, "reach", False, []), CheckResult(server, "load", False, [])], False, timedelta(0)


def _safe(server, names, fn) -> List[CheckResult]:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        _crash(server, names[0], exc)
        return [CheckResult(server, n, False, []) for n in names]


def _jobs(ctx: _Ctx, server, shift) -> List[Callable[[], List[CheckResult]]]:
    """The jobs of a server's checks; each one is isolated: a failure gives ran=False and a line on stderr. A check the
    owner disabled on this server (disabled_checks) does not run: ran=False."""
    cfg = ctx.cfg
    jobs = []
    off = monitor_settings.disabled(cfg, server)
    for name in [n for n in ctx.sec["fails"].get(server, {}) if monitor_settings.job_of(n) in off]:
        _ok(ctx, server, name)  # When enabled later, it starts a fresh failure count.

    def add(names, fn):
        if monitor_settings.job_of(names[0]) in off:
            jobs.append(lambda: [CheckResult(server, n, False, []) for n in names])
            return
        jobs.append(lambda: _safe(server, names, fn))

    def simple(check, argv, parse, **kw):
        add([check], lambda: _simple(ctx, server, check, argv, parse, **kw))

    simple("disk", DF, checks.disk)
    add(["mem"], lambda: _mem(ctx, server))
    simple("oom", ["dmesg", "-T", "--level=err,warn"], checks.oom, shift=shift, zone=ctx.zones.get(server))
    add(["units"], lambda: _units(ctx, server))
    add(["logins"], lambda: _logins(ctx, server, shift))
    add(SEC_NAMES + ["posture"], lambda: _sec(ctx, server))
    add(["ports"], lambda: _ports(ctx, server))
    if _wg_server(cfg, server):
        add(["wgpeers"], lambda: _wgpeers(ctx, server))
    add(["updates"], lambda: _updates(ctx, server))
    add(["reboot"], lambda: _reboot(ctx, server))
    add(["autoupdates"], lambda: _autoupdates(ctx, server))
    add(["eol"], lambda: _eol(ctx, server))
    add(["cores"], lambda: _cores(ctx, server))
    if server in cfg["vpn_nodes"]:
        simple("wg", ["wg", "show", "all", "latest-handshakes"], checks.wg)
    if server == cfg["heartbeat"]["server"]:
        simple("heartbeat", ["mysql", "-N", "-e", HEARTBEAT_SQL.format(cfg["heartbeat"]["table"])], checks.heartbeat, aliases=ctx.aliases)
    if server == cfg["mysql"]["server"]:
        add(["mysql"], lambda: _mysql(ctx, server))
    if server in cfg["certs"]["servers"]:
        simple("certs", ["certbot", "certificates"], checks.certs)
    if server == cfg["backup"]["server"]:
        simple("backup", ["ls", "-l", "--time-style=+%s", cfg["backup"]["path"]], checks.backup)
    if server == cfg["monitoring"]["server"]:
        add(["monitoring_alive", "monitoring_file"], lambda: _monitoring(ctx, server))
        simple("consumers", ["mysql", "-N", "-e", CONSUMERS_SQL.format(cfg["monitoring"]["consumers_table"])], checks.consumers)
    return jobs


def _planned_names(cfg, server) -> List[str]:
    names = ["disk", "mem", "oom", "units"]
    names += ["logins", "ports", "updates", "reboot", "autoupdates", "eol", "cores", "posture", "health"] + SEC_NAMES
    if _wg_server(cfg, server):
        names.append("wgpeers")
    if server in cfg["vpn_nodes"]:
        names.append("wg")
    if server == cfg["heartbeat"]["server"]:
        names.append("heartbeat")
    if server == cfg["mysql"]["server"]:
        names.append("mysql")
    if server in cfg["certs"]["servers"]:
        names.append("certs")
    if server == cfg["backup"]["server"]:
        names.append("backup")
    if server == cfg["monitoring"]["server"]:
        names += ["monitoring_alive", "monitoring_file", "consumers"]
    return names


def _reach_raw(ctx: _Ctx, server):
    """(the reach results, whether the server answered, the shift of its local time)."""
    no_load = CheckResult(server, "load", False, [])
    r = ctx.call(server, ["uptime"])
    if r is None:
        return [CheckResult(server, "reach", False, []), no_load], False, timedelta(0)
    rc, out, err = r
    un = ctx.state["unreachable"]
    if rc == 0:
        un[server] = 0
        load = seccheck.load_obs(server, seccheck.parse_load1(out), ctx.sec, ctx.cfg)
        return ([CheckResult(server, "reach", True, checks.reach(server, out, ctx.cfg, ctx.now)),
                 CheckResult(server, "load", bool(load), load)]
                + _gate_results(server, "reach", 0, ""),
                True, checks.server_shift(out, ctx.now))
    if rc in (124, 255):
        un[server] = un.get(server, 0) + 1
        obs = checks.unreachable(server, un[server], ctx.cfg)
        return [CheckResult(server, "reach", bool(obs), obs), no_load], False, timedelta(0)
    if rc == 2:  # disabled by the owner: not an outage, but nothing to poll either
        return [CheckResult(server, "reach", False, []), no_load], False, timedelta(0)
    return ([CheckResult(server, "reach", False, []), no_load] + _gate_results(server, "reach", rc, err),
            rc not in GATE_FAIL, timedelta(0))


def collect(cfg, now, state, deadline: float, ssh=run_ssh_ro, servers=None,
            clock=time.monotonic, baseline=None, aliases=None, force=False) -> List[CheckResult]:
    """One poll. `deadline` — by `clock`. `servers` defaults to the inventory ∩ servers.enabled.
    `baseline` — the security baseline (none — the checks against it do not run). `aliases` — all servers for the
    nodes' heartbeat (the polled ones by default; a partial tick passes all attached ones).
    Writes state["unreachable"], state["monitoring_ts"], state["last_run"], state["swap_io"], state["sec"]."""
    if servers is None:
        from sysadmin import inventory
        servers = sorted(set(inventory.load()) & inventory.enabled())
    servers = list(servers)
    for key, default in (("unreachable", {}), ("last_run", {}), ("swap_io", {})):
        if not isinstance(state.get(key), dict):
            state[key] = default
    ctx = _Ctx(cfg, now, state, deadline, ssh, clock, baseline or Baseline.empty(), force=force)
    ctx.aliases = set(servers) | set(aliases or ())
    from sysadmin import inventory
    ctx.zones = inventory.zones()
    workers = max(1, int(cfg["parallel"]))
    results: List[CheckResult] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        reached = list(pool.map(lambda sv: _reach(ctx, sv), servers))
        futures = []
        for sv, (res, up, shift) in zip(servers, reached):
            results += res
            if up:
                futures += [pool.submit(j) for j in _jobs(ctx, sv, shift)]
            else:
                results += [CheckResult(sv, n, False, []) for n in _planned_names(cfg, sv)]
        for f in futures:
            results += f.result()
        for sv, (res, up, shift) in zip(servers, reached):
            if up:
                results.append(_health(ctx, sv))
    return results


def _exec_users() -> dict:
    """{server → exec user} from the inventory (ubuntu on some nodes); the inventory cannot be read — {} (root)."""
    from sysadmin import inventory
    try:
        return {alias: s.exec_user for alias, s in inventory.load().items()}
    except inventory.InventoryError:
        return {}


def _capture_one(ctx: _Ctx, baseline: Baseline, sv: str, exec_user: str = "root"):
    """(the lines of the printout, the baseline dict or None). The factory host's IP is trusted only for agent-ro and the exec user."""
    cfg, now = ctx.cfg, ctx.now
    c = cfg["security"]
    lines = []
    r = ctx.call(sv, SNAPSHOT)
    parsed = seccheck.parse_snapshot(r[1]) if r is not None and r[0] == 0 else None
    if parsed is None:
        why = "нет времени" if r is None else _first_line(r[2] or r[1])
        return [f"{sv}: эталон НЕ снят — snapshot: {why}"], None
    items, errors = parsed
    r = ctx.call(sv, SS_LISTEN)
    if r is None or r[0] != 0:
        return [f"{sv}: эталон НЕ снят — ports: {'нет времени' if r is None else _first_line(r[2] or r[1])}"], None
    listeners = seccheck.parse_listeners(r[1])
    old = baseline.server(sv) if baseline.has(sv) else None
    if errors and old:
        for key, value in old["snapshot"].items():
            if key.split(":", 1)[0] in errors:
                items.setdefault(key, value)
    peers = []
    if _wg_server(cfg, sv):
        r = ctx.call(sv, WG_PEERS)
        if r is not None and r[0] == 0:
            peers = seccheck.parse_wgpeers(r[1])
        else:  # an empty list would replace the baseline's peers altogether — every peer of the node would become "new"
            peers = list(old["wgpeers"]) if old else []
            lines.append("  пиры не прочитаны" + (" (взяты из прежнего эталона)" if old else "") + ": "
                         + ("нет времени" if r is None else _first_line(r[2] or r[1])))
    identities, rejected = {}, {}
    journal = sv in c["journal_servers"]
    r = ctx.call(sv, _auth_argv(cfg, sv, journal, c["auth_log_lines"]))
    if r is not None and r[0] != 0 and not journal and "No such file" in r[1] + r[2]:
        r = ctx.call(sv, _auth_argv(cfg, sv, True, c["auth_log_lines"]))
    if r is not None and r[0] == 0 and JOURNAL_HINT not in r[1]:
        for lg in seccheck.parse_auth(_auth_text(r[1])[0], None, [], now).logins:
            label = baseline.is_trusted(lg.ip)
            if lg.user == "agent-ro" and lg.ip != seccheck.stage_ip():
                label = None
            if label == "stage" and lg.user not in ("agent-ro", exec_user):
                label = None  # a web shell on the factory host: a foreign key from its IP is "NOT accepted"
            if label is not None:
                if not seccheck.is_weak(lg.identity):  # a login without a key is never enrolled in the baseline
                    identities[lg.identity] = label
            else:
                rejected.setdefault(lg.identity, set()).add(lg.ip)
    else:
        lines.append(f"  входы не прочитаны: {'нет времени' if r is None else _first_line(r[2] or r[1])}")
    data = Baseline.make_server(identities, listeners, peers, items, now)
    per = Counter(k.split(":", 1)[0] for k in items)
    sections = [s for s in seccheck.SECTIONS if per[s]] + sorted(set(per) - set(seccheck.SECTIONS))
    head = [f"{sv}: эталон снят — личностей {len(identities)}, слушателей {len(listeners)}, пиров {len(peers)}, "
            f"объектов снимка {len(items)}" + (" (" + ", ".join(f"{s} {per[s]}" for s in sections) + ")" if per else "")]
    head += lines  # warnings go right under the summary, before the lists
    if errors:
        head.append("  секции с ошибкой" + (" (взяты из прежнего эталона)" if old else "") + ": " + ", ".join(sorted(errors)))
    if old:
        diff = [o.text for o in seccheck.snapshot_obs(sv, items, {}, old["snapshot"], cfg)]
        diff += [o.text for o in seccheck.ports_obs(sv, listeners, old["ports"], cfg)]
        diff += [o.text for o in seccheck.wgpeers_obs(sv, peers, old["wgpeers"])]
        diff += [f"личность {i} ({lb}) новая" for i, lb in sorted(identities.items()) if i not in old["identities"]]
        if diff:
            head.append("  отличия от прежнего эталона: " + "; ".join(diff[:20]) + (" …" if len(diff) > 20 else ""))
    for ident, label in sorted(identities.items()):
        head.append(f"  личность «{label}»: {ident}")
    for obj, proc in sorted(listeners.items()):
        head.append(f"  слушатель {obj} ({proc})")
    known = old["identities"] if old else {}
    for ident in sorted(i for i in rejected if i not in identities and i not in known):
        head.append(f"  НЕ принята: {ident} с {', '.join(sorted(rejected[ident]))}")
    return head, data


def capture(cfg, now, baseline: Baseline, servers, ssh=run_ssh_ro, clock=time.monotonic, deadline=None,
            exec_users=None):
    """Capture a baseline: the same commands as the checks, the servers in parallel. → (the lines of the printout,
    {server: baseline}). The baseline is not changed here — the CLI applies it under the lock. `exec_users` —
    {server → exec user}, from the inventory by default; a server missing from the map — root."""
    deadline = clock() + cfg["tick_budget_s"] if deadline is None else deadline
    exec_users = _exec_users() if exec_users is None else exec_users
    ctx = _Ctx(cfg, now, {"unreachable": {}, "last_run": {}}, deadline, ssh, clock, baseline)
    servers = list(servers)
    lines, captured = [], {}
    with ThreadPoolExecutor(max_workers=max(1, int(cfg["parallel"]))) as pool:
        for sv, (ls, data) in zip(servers, pool.map(
                lambda s: _capture_one(ctx, baseline, s, exec_users.get(s, "root")), servers)):
            lines += ls
            if data is not None:
                captured[sv] = data
    return lines, captured
