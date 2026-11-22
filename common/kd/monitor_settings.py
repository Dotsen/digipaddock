"""The monitor's settings the owner edits in the cabinet over config.json.

config.json of the release stays the defaults; the cabinet keeps the owner's edits (a key, a server or '' for the
whole monitor, a value) and the monitor merges them over its own config.json each tick. A key without an edit follows
the default of the release it runs; an edit lives until it is reset; an edit of a key the release no longer has is
not applied (``stale``).

Only the keys of the catalog below are edited: thresholds with their direction, the lists that hide or soften an
alarm, the monitor's quiet hours and wake limit, the critical servers, and per server the existing MB thresholds and
the switch of a check. Technical keys (parallel, budgets, paths, …) and where a check runs (``*.server``, vpn_nodes,
units_required, …) stay in config.json and are refused here.

A change is *weakening* when it can only make the monitor alarm later, less or never: a threshold moved towards
fewer alarms (each threshold has its direction), a longer interval, more ignored or confirm-first names, a new minute
of quiet hours, fewer wakes per hour, a server out of ``critical_unreachable``, a check switched off. Everything else
tightens. The cabinet saves a weakening only with the owner's step-up.

Pure logic shared by the cabinet and the monitor: no I/O, no cabinet imports.
"""

import copy
import math
import re

MISSING = object()

# the checks a server may switch off: the monitor's jobs (``runner._jobs``); reach (and load with it) always runs
CHECKS = ("disk", "mem", "oom", "units", "logins", "sec", "ports", "wgpeers", "updates", "reboot", "autoupdates",
          "eol", "cores", "wg", "heartbeat", "mysql", "certs", "backup", "monitoring", "consumers")
EVERY = ("certs", "backup", "sec", "ports", "wgpeers", "updates", "reboot", "autoupdates", "eol", "cores")

# key -> (type, min, max, weaker): "up" — a larger value alarms less, "down" — a smaller one does
NUMBERS = {
    "unreachable_after": ("int", 1, 100, "up"),
    "max_wakes_per_hour": ("int", 1, 60, "down"),
    "disk_pct.warn": ("int", 1, 100, "up"),
    "disk_pct.crit": ("int", 1, 100, "up"),
    "mem_available_pct.warn": ("num", 0, 100, "down"),
    "mem_available_pct.crit": ("num", 0, 100, "down"),
    "mem_psi.some_avg300": ("num", 0, 100, "up"),
    "mem_psi.full_avg60": ("num", 0, 100, "up"),
    "swap_io.warn_pages_s": ("num", 0, 1000000, "up"),
    "units_required_after": ("int", 1, 100, "up"),
    "wg_stale_min": ("int", 1, 1440, "up"),
    "heartbeat.max_age_min.default": ("int", 1, 10080, "up"),
    "mysql.query_warn_s": ("int", 1, 86400, "up"),
    "mysql.query_crit_s": ("int", 1, 86400, "up"),
    "mysql.mdl_crit_s": ("int", 1, 86400, "up"),
    "mysql.conn_warn_pct": ("num", 1, 100, "up"),
    "certs.warn_days": ("int", 1, 365, "down"),
    "certs.crit_days": ("int", 1, 365, "down"),
    "backup.warn_h": ("num", 1, 8760, "up"),
    "backup.crit_h": ("num", 1, 8760, "up"),
    "monitoring.max_age_min": ("int", 1, 10080, "up"),
    "monitoring.consumers_stale_min": ("int", 1, 10080, "up"),
    "security.reboot_warn_days": ("int", 1, 365, "up"),
    "security.updates.warn_days": ("int", 0, 365, "up"),
    "security.updates.critical_warn_days": ("int", 0, 365, "up"),
    "security.updates.critical_any_days": ("int", 0, 365, "up"),
    "security.updates.crit_days": ("int", 0, 365, "up"),
    "security.eol.warn_days": ("int", 1, 3650, "down"),
    "security.load.warn_x": ("num", 0.1, 100, "up"),
    "security.load.crit_x": ("num", 0.1, 100, "up"),
    "security.load.streak": ("int", 1, 100, "up"),
    "security.health_after": ("int", 1, 100, "up"),
}
NUMBERS.update({f"every_min.{c}": ("int", 5, 10080, "up") for c in EVERY})
# key -> weaker: "add" — a new name hides or softens an alarm, "remove" — a name taken out does
LISTS = {
    "units_ignore": "add",
    "units_confirm": "add",
    "security.units_ignore": "add",
    "security.ports_ignore": "add",
    "mysql.ignore_commands": "add",
    "mysql.ignore_users": "add",
    "mysql.ignore_info_prefixes": "add",
    "heartbeat.ignore_prefix": "add",
    "monitoring.ignore_id_prefixes": "add",
    "monitoring.critical_id_prefixes": "remove",
    "critical_unreachable": "remove",
}
QUIET = "quiet_hours"
GLOBAL = frozenset(NUMBERS) | frozenset(LISTS) | {QUIET}
# per server: the MB thresholds of a small machine (lower alarms less) and the checks switched off
MEM_MB = ("mem_available_mb.warn", "mem_available_mb.crit")
OFF = "disabled_checks"
SERVER = frozenset(MEM_MB) | {OFF}
# (lower, higher): the value at ``lower`` must stay below the one at ``higher`` (crit stricter than warn)
PAIRS = (
    ("disk_pct.warn", "disk_pct.crit"),
    ("mem_available_pct.crit", "mem_available_pct.warn"),
    ("mysql.query_warn_s", "mysql.query_crit_s"),
    ("certs.crit_days", "certs.warn_days"),
    ("backup.warn_h", "backup.crit_h"),
    ("security.load.warn_x", "security.load.crit_x"),
    ("security.updates.critical_warn_days", "security.updates.crit_days"),
)
LIST_MAX = 100
NAME_MAX = 200
SERVER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
HHMM = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])")
# the head of the monitor's weekly summary wake (the first) and of the daily one before it. The sysadmin's
# reply retells what is open: the cabinet attaches it to none of the incidents it names.
SUMMARY_HEADS = ("[монитор] сводка за неделю", "[монитор] утренняя сводка")


class Invalid(ValueError):
    """A value or key the catalog refuses; nothing is saved."""


# --- the catalog -----------------------------------------------------------------------------------
def scope(key: str) -> str:
    """``global`` or ``server``; Invalid for a key that is not edited here (technical, where a check runs, unknown)."""
    if key in GLOBAL:
        return "global"
    if key in SERVER:
        return "server"
    raise Invalid(f"{key} is not a setting edited in the cabinet")


def describe() -> list:
    """The catalog for the page: each key with its scope, type, range and which way weakens."""
    out = []
    for key in sorted(NUMBERS):
        kind, lo, hi, weaker = NUMBERS[key]
        out.append({"key": key, "scope": "global", "type": kind, "min": lo, "max": hi, "weaker": weaker})
    for key in sorted(LISTS):
        out.append({"key": key, "scope": "global", "type": "list", "weaker": LISTS[key]})
    out.append({"key": QUIET, "scope": "global", "type": "quiet", "weaker": "wider"})
    for key in MEM_MB:
        out.append({"key": key, "scope": "server", "type": "num", "min": 1, "max": 1000000, "weaker": "down"})
    out.append({"key": OFF, "scope": "server", "type": "checks", "weaker": "add", "checks": list(CHECKS)})
    return out


def _path(key: str, server: str) -> list:
    if key in MEM_MB:
        return ["mem_available_mb", server, key.split(".")[1]]
    if key == OFF:
        return [OFF, server]
    return key.split(".")


def lookup(cfg: dict, key: str, server: str = ""):
    """The value of a key in a config (the defaults or a merged one), or MISSING."""
    node = cfg
    for part in _path(key, server):
        if not isinstance(node, dict) or part not in node:
            return MISSING
        node = node[part]
    return node


def _put(cfg: dict, key: str, server: str, value) -> None:
    parts = _path(key, server)
    node = cfg
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = copy.deepcopy(value)


def stale(defaults: dict, key: str, server: str = "") -> bool:
    """An edit the release no longer applies: the key left the catalog, or a global key left config.json."""
    try:
        where = scope(key)
    except Invalid:
        return True
    return where == "global" and lookup(defaults, key) is MISSING


def merge(defaults: dict, edits) -> dict:
    """config.json with the edits over it; a stale edit is skipped. ``edits`` — ``[{key, server, value}]``."""
    cfg = copy.deepcopy(defaults)
    for edit in edits:
        key, server = edit.get("key"), edit.get("server") or ""
        if not isinstance(key, str) or stale(defaults, key, server):
            continue
        _put(cfg, key, server, edit.get("value"))
    return cfg


# --- values ----------------------------------------------------------------------------------------
def servers_of(defaults: dict) -> set:
    """The servers config.json itself names: they are known even before the monitor reports them."""
    found = set()
    for key in ("critical_unreachable", "vpn_nodes"):
        found |= {s for s in defaults.get(key) or [] if isinstance(s, str)}
    for key in ("mem_available_mb", "units_required"):
        found |= {s for s in (defaults.get(key) or {}) if isinstance(s, str)}
    for key in ("heartbeat", "mysql", "backup", "monitoring"):
        server = (defaults.get(key) or {}).get("server")
        if isinstance(server, str):
            found.add(server)
    found |= {s for s in (defaults.get("certs") or {}).get("servers") or [] if isinstance(s, str)}
    return found


def _number(key, value, kind, lo, hi):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Invalid(f"{key} is a number")
    if kind == "int" and not isinstance(value, int):
        raise Invalid(f"{key} is a whole number")
    if isinstance(value, float) and not math.isfinite(value):
        raise Invalid(f"{key} is a number")
    if not lo <= value <= hi:
        raise Invalid(f"{key} is from {lo} to {hi}")
    return value


def _names(key, value):
    if not isinstance(value, list) or len(value) > LIST_MAX:
        raise Invalid(f"{key} is a list of at most {LIST_MAX} names")
    for name in value:
        if not isinstance(name, str) or not name.strip() or len(name) > NAME_MAX:
            raise Invalid(f"{key}: a name is text of 1 to {NAME_MAX} characters")
    if len(set(value)) != len(value):
        raise Invalid(f"{key}: a name is listed twice")
    return list(value)


def _quiet(value):
    if not isinstance(value, dict) or set(value) != {"start", "end"}:
        raise Invalid("quiet_hours is {start, end}")
    for part in ("start", "end"):
        if not isinstance(value[part], str) or not HHMM.fullmatch(value[part]):
            raise Invalid(f"quiet_hours.{part} is HH:MM")
    return {"start": value["start"], "end": value["end"]}


def validate(key: str, server: str, value, servers) -> object:
    """The value as it is saved, or Invalid: its type and range, the server and the checks it names. ``servers`` —
    the known servers; None (the monitor re-checking what the cabinet already checked) — any well-formed alias."""

    def known(name):
        return isinstance(name, str) and SERVER_RE.fullmatch(name) and (servers is None or name in servers)

    where = scope(key)
    if where == "global" and server:
        raise Invalid(f"{key} is a setting of the whole monitor, not of a server")
    if where == "server" and not known(server):
        raise Invalid(f"{key}: unknown server {server!r}")
    if key in NUMBERS:
        kind, lo, hi, _ = NUMBERS[key]
        return _number(key, value, kind, lo, hi)
    if key in MEM_MB:
        return _number(key, value, "num", 1, 1000000)
    if key == QUIET:
        return _quiet(value)
    names = _names(key, value)
    if key == "critical_unreachable":
        unknown = [s for s in names if not known(s)]
        if unknown:
            raise Invalid(f"critical_unreachable: unknown server {unknown[0]!r}")
    if key == OFF:
        unknown = [c for c in names if c not in CHECKS]
        if unknown:
            raise Invalid(f"disabled_checks: unknown check {unknown[0]!r}")
    return names


def check_config(cfg: dict) -> None:
    """The merged config holds together: crit stricter than warn, a server's MB thresholds both set and 0 < crit < warn."""
    for lower, higher in PAIRS:
        a, b = lookup(cfg, lower), lookup(cfg, higher)
        if a is not MISSING and b is not MISSING and not a < b:
            raise Invalid(f"{lower} must stay below {higher}")
    for server, t in (cfg.get("mem_available_mb") or {}).items():
        ok = isinstance(t, dict) and set(t) == {"warn", "crit"}
        if not ok or not 0 < t["crit"] < t["warn"]:
            raise Invalid(f"mem_available_mb of {server}: both warn and crit, 0 < crit < warn")


# --- weakening -------------------------------------------------------------------------------------
def _minutes(q) -> set:
    """The minutes of the day quiet hours cover, as the monitor reads them (``deliver._is_night``)."""
    if not isinstance(q, dict):
        return set()
    try:
        (sh, sm), (eh, em) = (map(int, q[p].split(":")) for p in ("start", "end"))
    except (KeyError, AttributeError, ValueError):
        return set()
    start, end = sh * 60 + sm, eh * 60 + em
    if start <= end:
        return set(range(start, end))
    return set(range(start, 24 * 60)) | set(range(0, end))


def weakens(key: str, old, new) -> bool:
    """Whether moving ``key`` from ``old`` to ``new`` (either may be MISSING) can only make the monitor alarm less."""
    if key in LISTS or key == OFF:
        before = set(old) if isinstance(old, list) else set()
        after = set(new) if isinstance(new, list) else set()
        weaker = "add" if key == OFF else LISTS[key]
        return bool(after - before) if weaker == "add" else bool(before - after)
    if key == QUIET:
        return bool(_minutes(new) - _minutes(old))
    if old is MISSING or new is MISSING:  # a threshold appears or goes: which way it moves is unknown — sign it
        return old is not new
    weaker = "down" if key in MEM_MB else NUMBERS[key][3]
    return new > old if weaker == "up" else new < old


# --- the monitor's side ----------------------------------------------------------------------------
def job_of(check: str, subject: str = ""):
    """The switchable check a problem's source belongs to (``CHECKS``), or None (reach, load: never off)."""
    if check == "health":
        return job_of(subject)
    if check.startswith("gate:"):
        return job_of(check[5:])
    if check == "posture" or check.startswith("sec:"):
        return "sec"
    if check in ("monitoring_alive", "monitoring_file"):
        return "monitoring"
    return check if check in CHECKS else None


def planned(cfg: dict, server: str) -> set:
    """The switchable checks that run on a server, as ``runner._jobs`` plans them: a check that does not run there
    has nothing to switch off (heartbeat of every node, for one, runs on the database server)."""
    found = {"disk", "mem", "oom", "units", "logins", "sec", "ports", "updates", "reboot", "autoupdates", "eol",
             "cores"}
    vpn = server in (cfg.get("vpn_nodes") or [])
    if vpn or server in ((cfg.get("security") or {}).get("wg_servers") or []):
        found.add("wgpeers")
    if vpn:
        found.add("wg")
    for check, jobs in (("heartbeat", ("heartbeat",)), ("mysql", ("mysql",)), ("backup", ("backup",)),
                        ("monitoring", ("monitoring", "consumers"))):
        if (cfg.get(check) or {}).get("server") == server:
            found.update(jobs)
    if server in ((cfg.get("certs") or {}).get("servers") or []):
        found.add("certs")
    return found


def disabled(cfg: dict, server: str) -> set:
    """The checks switched off on a server in a merged config."""
    found = (cfg.get(OFF) or {}).get(server) or []
    return {c for c in found if c in CHECKS}
