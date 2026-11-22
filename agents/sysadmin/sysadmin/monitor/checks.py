"""Pure parsers of ssh-ro output → the monitor's observations. No SSH and no state.

Every parser: checks.<name>(server, output, cfg, now, **ctx) -> list[Observation].
`now` is an aware datetime; time is compared in the host's zone (host.json timezone)."""
import fnmatch
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from .model import Observation

COMMON = str(Path(__file__).resolve().parents[4] / "common")
if COMMON not in sys.path:
    sys.path.append(COMMON)
from kd import clock  # noqa: E402
MYSQL_SEP = "\n=====\n"  # the separator of the three mysql outputs the runner joins

_GATE_LINE = re.compile(r"^\[agent-ro-gate:.*\]\s*$")
_MONTHS = {m: i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}


def clean(output: str) -> str:
    """Remove the gateway's service lines and `WARNING: apt …`."""
    keep = [ln for ln in output.splitlines()
            if not _GATE_LINE.match(ln) and not ln.startswith("WARNING: apt")]
    return "\n".join(keep)


def _obs(server, check, subject, level, value, text) -> Observation:
    return Observation(server, check, subject, level, str(value), text)


def _local_naive(now: datetime) -> datetime:
    return now.astimezone(clock.zone()).replace(tzinfo=None)


def _moment(local: datetime, zone, shift: timedelta) -> datetime:
    """A server's naive wall time as a moment: in its zone (inventory "tz") — right across an hour change; without
    one, in the host's zone less the server's clock shift of now (`server_shift`)."""
    return local.replace(tzinfo=zone) if zone is not None else local.replace(tzinfo=clock.zone()) - shift


# --- reach ------------------------------------------------------------------

def reach(server, output, cfg, now, **ctx) -> List[Observation]:
    return [_obs(server, "reach", "-", "ok", "ok", "сервер отвечает")]


def unreachable(server, count, cfg) -> List[Observation]:
    """The observation "does not answer" after `count` failures in a row; below the threshold — nothing."""
    if count < cfg["unreachable_after"]:
        return []
    level = "crit" if server in cfg["critical_unreachable"] else "warn"
    return [_obs(server, "reach", "-", level, f"{count} раз",
                 f"{server} не отвечает по SSH {count} раз подряд")]


def server_shift(output: str, now: datetime) -> timedelta:
    """The shift of the server's local time from the host's zone by the clock in `uptime` (up to 15 min); no clock — 0."""
    m = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s+up\b", output)
    if not m:
        return timedelta(0)
    local = timedelta(hours=int(m.group(1)), minutes=int(m.group(2)), seconds=int(m.group(3) or 0))
    n = _local_naive(now)
    ours = timedelta(hours=n.hour, minutes=n.minute, seconds=n.second)
    diff = (local - ours).total_seconds()
    diff = (diff + 12 * 3600) % (24 * 3600) - 12 * 3600  # into (-12h, +12h]
    return timedelta(seconds=round(diff / 900) * 900)


# --- disk / mem -------------------------------------------------------------

def disk(server, output, cfg, now, **ctx) -> List[Observation]:
    t = cfg["disk_pct"]
    res = []
    for ln in clean(output).splitlines():
        f = ln.split()
        if len(f) < 6 or not re.fullmatch(r"\d+%", f[4]):
            continue  # the header and junk
        pct = int(f[4][:-1])
        mount = " ".join(f[5:])
        level = "crit" if pct >= t["crit"] else "warn" if pct >= t["warn"] else "ok"
        res.append(_obs(server, "disk", mount, level, f"{pct}%", f"{mount}: занято {pct}%"))
    return res


def _numbers(output: str) -> Dict[str, int]:
    """`/proc/meminfo` (`Key:  N kB`) and `/proc/vmstat` (`key N`) → {key: N}."""
    info: Dict[str, int] = {}
    for ln in clean(output).splitlines():
        m = re.match(r"^(\w+):?\s+(\d+)", ln)
        if m:
            info[m.group(1)] = int(m.group(2))
    return info


def _psi(output: str) -> Optional[Dict[str, float]]:
    """`/proc/pressure/memory` → {"some_avg300": …, "full_avg60": …, …}; the needed fields are missing — None."""
    vals: Dict[str, float] = {}
    for ln in clean(output).splitlines():
        f = ln.split()
        if f and f[0] in ("some", "full"):
            for kv in f[1:]:
                k, _, v = kv.partition("=")
                try:
                    vals[f"{f[0]}_{k}"] = float(v)
                except ValueError:
                    continue
    return vals if "some_avg300" in vals and "full_avg60" in vals else None


def mem(server, output, cfg, now, psi: str = "", swap_rate=None, **ctx) -> List[Observation]:
    """output — `/proc/meminfo` and `/proc/vmstat`; psi — `/proc/pressure/memory` (empty — a kernel without PSI);
    swap_rate — (pages/s, minutes of the window) from swap_rate() or None. Used swap alone is no alarm
    (idle processes hold it, an emulator on one of the servers for instance): the alarms are low memory, PSI and
    active swapping, the swap percentage goes into their text as context."""
    info = _numbers(output)
    res, swap = [], ""
    st = info.get("SwapTotal")
    if st:
        used = (st - info.get("SwapFree", st)) * 100.0 / st
        swap = f"; swap занят на {used:.0f}%"
    total = info.get("MemTotal")
    if total and "MemAvailable" in info:
        pct = info["MemAvailable"] * 100.0 / total
        mb_t = cfg.get("mem_available_mb", {}).get(server)  # a small machine: the threshold is in MB, not in percent
        if mb_t:
            mb = info["MemAvailable"] / 1024.0
            level = "crit" if mb < mb_t["crit"] else "warn" if mb < mb_t["warn"] else "ok"
            res.append(_obs(server, "mem", "ram", level, f"{mb:.0f} МБ",
                            f"свободной памяти {mb:.0f} МБ ({pct:.0f}%; порог {mb_t['warn']} МБ){swap}"))
        else:
            t = cfg["mem_available_pct"]
            level = "crit" if pct < t["crit"] else "warn" if pct < t["warn"] else "ok"
            res.append(_obs(server, "mem", "ram", level, f"{pct:.0f}%", f"свободной памяти {pct:.0f}%{swap}"))
    if st == 0:
        res.append(_obs(server, "mem", "swap", "ok", "нет", "swap не настроен"))
    elif st:
        res.append(_obs(server, "mem", "swap", "ok", f"{used:.0f}%", f"swap занят на {used:.0f}%"))
    p = _psi(psi)
    if p:
        t = cfg["mem_psi"]
        bad = p["some_avg300"] > t["some_avg300"] or p["full_avg60"] > t["full_avg60"]
        res.append(_obs(server, "mem", "psi", "warn" if bad else "ok",
                        f"some {p['some_avg300']:.1f} / full {p['full_avg60']:.1f}",
                        f"давление на память (PSI): some avg300 {p['some_avg300']:.1f}%, full avg60 "
                        f"{p['full_avg60']:.1f}% (пороги {t['some_avg300']} и {t['full_avg60']}){swap}"))
    if swap_rate is not None:
        rate, mins = swap_rate
        t = cfg["swap_io"]
        res.append(_obs(server, "mem", "swapio", "warn" if rate > t["warn_pages_s"] else "ok", f"{rate:.0f} стр/с",
                        f"активная подкачка {rate:.0f} стр/с (~{rate * 4 / 1024:.1f} МБ/с) в среднем за "
                        f"{mins:.0f} мин (порог {t['warn_pages_s']}){swap}"))
    return res


def swap_rate(samples, output, now, cfg):
    """The average rate of swap traffic (pswpin + pswpout of `/proc/vmstat`) between polls.

    samples — [[iso, pages], …] from the state. → ((pages/s, minutes of the window) or None, the new samples).
    The base is the freshest sample no younger than `swap_io.window_min`. Samples older than two windows, from the
    future, or with a counter above the current one (the server rebooted) are dropped."""
    info = _numbers(output)
    if "pswpin" not in info or "pswpout" not in info:
        return None, []
    pages = info["pswpin"] + info["pswpout"]
    window = cfg["swap_io"]["window_min"] * 60
    keep, base = [], None  # base = (age, pages)
    for s in samples if isinstance(samples, list) else []:
        try:
            ts, n = s[0], int(s[1])
            age = (now - datetime.fromisoformat(ts)).total_seconds()
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if n > pages or not 0 < age <= 2 * window:
            continue
        keep.append([ts, n])
        if age >= window and (base is None or age < base[0]):
            base = (age, n)
    keep.append([now.isoformat(), pages])
    rate = ((pages - base[1]) / base[0], base[0] / 60.0) if base else None
    return rate, keep


# --- oom / units / wg -------------------------------------------------------

def oom(server, output, cfg, now, shift: timedelta = timedelta(0), zone=None, **ctx) -> List[Observation]:
    """`dmesg -T`: a line's time is the server's local time: in its zone `zone` (the inventory's "tz"), without one —
    shift = local − the host's zone (from `reach`)."""
    edge = now - timedelta(hours=24)
    count, last = 0, None
    for ln in clean(output).splitlines():
        m = re.match(r"^\[\w{3} (\w{3})\s+(\d+) (\d\d):(\d\d):(\d\d) (\d{4})\]\s*(.*)$", ln)
        if not m or m.group(1) not in _MONTHS:
            continue
        low = m.group(7).lower()
        if "out of memory" not in low and "oom-kill" not in low:
            continue
        local = datetime(int(m.group(6)), _MONTHS[m.group(1)], int(m.group(2)),
                         int(m.group(3)), int(m.group(4)), int(m.group(5)))
        moment = _moment(local, zone, shift)
        if moment >= edge:
            count += 1
            last = (moment, m.group(7))
    if not count:
        return [_obs(server, "oom", "-", "ok", "0", "OOM за 24 ч не было")]
    when = last[0].astimezone(clock.zone()).strftime("%d.%m %H:%M %Z")
    return [_obs(server, "oom", "-", "warn", str(count),
                 f"OOM-killer за 24 ч: {count}; последний {when}: {last[1][:120]}")]


def required_units(server, cfg) -> List[str]:
    """The server's required units (units_required): they must be active; units_ignore does not hide them."""
    return list(cfg.get("units_required", {}).get(server, []))


def units(server, output, cfg, now, **ctx) -> List[Observation]:
    required = set(required_units(server, cfg))
    res = []
    for ln in clean(output).splitlines():
        f = ln.lstrip(" ●*").split()
        if len(f) < 4 or f[2] != "failed":
            continue  # not a unit line (stderr, junk)
        name = f[0]
        if name not in required and any(fnmatch.fnmatchcase(name, p) for p in cfg["units_ignore"]):
            continue
        desc = " ".join(f[4:])
        res.append(_obs(server, "units", name, "warn", "failed",
                        f"юнит {name} упал" + (f": {desc}" if desc else "")))
    return res


def unit_states(output: str) -> Dict[str, Dict[str, str]]:
    """`systemctl show -p Id,LoadState,ActiveState <units…>` → {Id: {"LoadState": …, "ActiveState": …}}.
    Unit blocks are separated by an empty line; a block without an Id is skipped."""
    res: Dict[str, Dict[str, str]] = {}
    for block in re.split(r"\n\s*\n", clean(output)):
        props = dict(ln.strip().split("=", 1) for ln in block.splitlines() if "=" in ln)
        if props.get("Id"):
            res[props["Id"]] = props
    return res


def required_obs(server, states, down, cfg, skip=(), open_keys=()) -> List[Observation]:
    """Required units that are not active. states — from unit_states, down — {unit: ticks in a row not active}
    (this tick included). failed — at once; stopped or not found — from units_required_after ticks in a row, or when
    the problem is already open. skip — units list-units --failed has already reported (one problem per unit)."""
    after = cfg.get("units_required_after", 2)
    res = []
    for name in required_units(server, cfg):
        st = states.get(name)
        if st is None or name in skip:
            continue
        load, active = st.get("LoadState", ""), st.get("ActiveState", "")
        if active == "active":
            continue
        if active == "failed":
            res.append(_obs(server, "units", name, "warn", "failed", f"юнит {name} упал"))
            continue
        if down.get(name, 0) < after and f"{server}:units:{name}" not in open_keys:
            continue
        if load == "not-found":
            res.append(_obs(server, "units", name, "warn", "not-found", f"юнит {name} не найден"))
        else:
            res.append(_obs(server, "units", name, "warn", active or "?", f"юнит {name} остановлен ({active or '?'})"))
    return res


def wg(server, output, cfg, now, **ctx) -> List[Observation]:
    stale_s = cfg["wg_stale_min"] * 60
    now_ts = now.timestamp()
    peers: Dict[str, List[int]] = {}
    for ln in clean(output).splitlines():
        f = ln.split()
        if len(f) == 3 and f[2].isdigit():
            peers.setdefault(f[0], []).append(int(f[2]))
    res = []
    for iface, stamps in peers.items():
        fresh = [t for t in stamps if t and now_ts - t <= stale_s]
        if fresh:
            res.append(_obs(server, "wg", iface, "ok", f"{len(fresh)}/{len(stamps)}",
                            f"{iface}: свежих пиров {len(fresh)} из {len(stamps)}"))
        else:
            res.append(_obs(server, "wg", iface, "warn", f"0/{len(stamps)}",
                            f"{iface}: ни у одного из {len(stamps)} пиров нет свежего handshake "
                            f"(порог {cfg['wg_stale_min']} мин)"))
    return res


# --- the database -------------------------------------------------------------

def heartbeat(server, output, cfg, now, aliases=(), **ctx) -> List[Observation]:
    """Lines `server_name<TAB>minutes`. The observations belong to the node's alias server (the source is the database server)."""
    c = cfg["heartbeat"]
    to_alias = {name: alias for alias, name in c["names"].items()}
    res = []
    for ln in clean(output).splitlines():
        f = ln.split("\t") if "\t" in ln else ln.split()
        if len(f) < 2 or not f[1].strip().lstrip("-").isdigit():
            continue
        name, age = f[0].strip(), int(f[1])
        if any(name.startswith(p) for p in c["ignore_prefix"]):
            continue
        alias = to_alias.get(name) or (name if name in aliases else None)
        if alias is None:
            continue
        limit = c["max_age_min"].get(name, c["max_age_min"].get(alias, c["max_age_min"]["default"]))
        level = "warn" if age > limit else "ok"
        res.append(_obs(alias, "heartbeat", "-", level, f"{age} мин",
                        f"нода {alias} ({name}) не присылала heartbeat {age} мин (порог {limit})"))
    return res


def mysql(server, output, cfg, now, **ctx) -> List[Observation]:
    """output = three outputs joined by MYSQL_SEP: processlist, Threads_connected, max_connections."""
    c = cfg["mysql"]
    parts = (output.split(MYSQL_SEP) + ["", "", ""])[:3]
    longest = None  # (seconds, line)
    mdl = 0
    for ln in clean(parts[0]).splitlines():
        f = ln.split("\t")
        if len(f) < 4 or not f[3].strip().isdigit():
            continue
        user, command, secs = f[1], f[2], int(f[3])
        state = f[4] if len(f) > 4 else ""
        info = f[5] if len(f) > 5 else ""
        if any(info.startswith(x) for x in c.get("ignore_info_prefixes", ())):
            continue  # the nightly mysqldump
        if command in c["ignore_commands"] or user in c["ignore_users"]:
            continue
        if longest is None or secs > longest[0]:
            longest = (secs, f"id {f[0]}, {user}, {command}")
        if "metadata lock" in state.lower():
            mdl = max(mdl, secs)
    res = []
    q = longest[0] if longest else 0
    level = "crit" if q > c["query_crit_s"] else "warn" if q > c["query_warn_s"] else "ok"
    res.append(_obs(server, "mysql", "query", level, f"{q} с",
                    f"самый долгий запрос {q} с" + (f" ({longest[1]})" if longest else "")))
    res.append(_obs(server, "mysql", "mdl", "crit" if mdl >= c["mdl_crit_s"] else "ok", f"{mdl} с",
                    f"ожидание metadata lock {mdl} с"))
    cur = _second_int(parts[1])
    mx = _second_int(parts[2])
    if cur is not None and mx:
        pct = cur * 100.0 / mx
        res.append(_obs(server, "mysql", "conn", "warn" if pct > c["conn_warn_pct"] else "ok",
                        f"{cur}/{mx}", f"соединений {cur} из {mx} ({pct:.0f}%)"))
    res.append(_obs(server, "mysql", "up", "ok", "ok", "MySQL отвечает"))
    return res


def mysql_down(server, cfg) -> List[Observation]:
    return [_obs(server, "mysql", "up", "crit", "down", "MySQL недоступен")]


def _second_int(text: str) -> Optional[int]:
    for ln in clean(text).splitlines():
        f = ln.split()
        if len(f) >= 2 and f[1].isdigit():
            return int(f[1])
    return None


def consumers(server, output, cfg, now, **ctx) -> List[Observation]:
    limit = cfg["monitoring"]["consumers_stale_min"]
    val = next((ln.strip() for ln in clean(output).splitlines()
                if ln.strip().lstrip("-").isdigit() or ln.strip().upper() == "NULL"), "")
    if val.lstrip("-").isdigit():
        age = int(val)
        return [_obs(server, "consumers", "-", "crit" if age > limit else "ok", f"{age} мин",
                     f"консьюмеры не присылали heartbeat {age} мин (порог {limit})")]
    if val.upper() == "NULL":
        return [_obs(server, "consumers", "-", "crit", "нет данных", "в таблице heartbeat консьюмеров нет записей")]
    return []


# --- certs / backup -------------------------------------------------------------

def certs(server, output, cfg, now, **ctx) -> List[Observation]:
    c = cfg["certs"]
    res = []
    name = None
    for ln in clean(output).splitlines():
        m = re.match(r"\s*Certificate Name:\s*(\S+)", ln)
        if m:
            name = m.group(1)
            continue
        m = re.match(r"\s*Expiry Date:.*\((VALID: (\d+) days?|VALID: \d+ hours?(?:\(s\))?|INVALID[^)]*)\)", ln)
        if not m or name is None:
            continue
        if m.group(2) is None:
            res.append(_obs(server, "certs", name, "crit", m.group(1), f"сертификат {name}: {m.group(1)}"))
        else:
            d = int(m.group(2))
            level = "crit" if d < c["crit_days"] else "warn" if d < c["warn_days"] else "ok"
            res.append(_obs(server, "certs", name, level, f"{d} дн", f"сертификат {name} истекает через {d} дн"))
        name = None
    return res


def backup(server, output, cfg, now, **ctx) -> List[Observation]:
    c = cfg["backup"]
    newest = None  # (ts, name)
    has_total = False
    for ln in clean(output).splitlines():
        if re.match(r"^total \d+\s*$", ln):
            has_total = True
        m = re.match(r"^(-\S{9})[.+]?\s+\d+\s+\S+\s+\S+\s+\d+\s+(\d+)\s+(.+)$", ln)
        if m and (newest is None or int(m.group(2)) > newest[0]):
            newest = (int(m.group(2)), m.group(3))
    if newest is None:
        if not has_total:
            return []  # the output is not `ls` (an error and the like) — no verdict
        return [_obs(server, "backup", "-", "crit", "нет", f"в {c['path']} нет ни одного файла бэкапа")]
    age = (now.timestamp() - newest[0]) / 3600.0
    level = "crit" if age > c["crit_h"] else "warn" if age > c["warn_h"] else "ok"
    return [_obs(server, "backup", "-", level, f"{age:.0f} ч",
                 f"новейший бэкап {newest[1]} старше {age:.0f} ч")]


# --- application monitoring ----------------------------------------------------

def parse_problems_file(output: str) -> Optional[dict]:
    """The parsed monitoring_problems.json, or None when the output is not JSON of the needed shape."""
    try:
        data = json.loads(clean(output))
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("ts"), str) \
            or not isinstance(data.get("problems") or [], list):
        return None
    return data


def _parse_ts(ts: str, zone=None) -> Optional[datetime]:
    """An application's time; a naive one is in the server's zone (inventory "tz"), else in the host's."""
    try:
        dt = datetime.fromisoformat(ts.strip())
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=zone or clock.zone())


def monitoring_alive(server, output, cfg, now, source: str = "file", zone=None, **ctx) -> List[Observation]:
    """source="file": the output of `cat problems_file`; source="log": the output of `tail alert_log` (the file is not
    there yet); zone — the server's zone for the application's naive timestamps."""
    limit = cfg["monitoring"]["max_age_min"]

    def verdict(ts: Optional[datetime]):
        if ts is None:
            return [_obs(server, "monitoring_alive", "-", "crit", "нет данных",
                         "мониторинг молчит: нет отметки времени последнего прогона")]
        age = (now - ts).total_seconds() / 60.0
        if age > limit:
            return [_obs(server, "monitoring_alive", "-", "crit", f"{age:.0f} мин",
                         f"мониторинг молчит: последний прогон {age:.0f} мин назад (порог {limit})")]
        return [_obs(server, "monitoring_alive", "-", "ok", f"{age:.0f} мин", "мониторинг жив")]

    if source == "log":
        last = None
        for ln in clean(output).splitlines():
            if "monitoring_alert: start" in ln:
                m = re.search(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}", ln)
                last = _parse_ts(m.group(0).replace(" ", "T"), zone) if m else last
        return verdict(last)
    data = parse_problems_file(output)
    if data is None:
        return [_obs(server, "monitoring_alive", "-", "warn", "битый", "файл мониторинга не читается")]
    ts = _parse_ts(data["ts"], zone)
    if ts is None:
        return [_obs(server, "monitoring_alive", "-", "warn", "битый", "файл мониторинга не читается")]
    return verdict(ts)


def monitoring_file(server, output, cfg, now, last_ts=None, zone=None, **ctx) -> Optional[List[Observation]]:
    """The problems from the file. None — the file is no good as a source (broken, old or the same `ts`):
    the runner sets ran=False. Otherwise the list of observations; the runner remembers the new `ts`."""
    c = cfg["monitoring"]
    data = parse_problems_file(output)
    if data is None or data["ts"] == last_ts:
        return None
    ts = _parse_ts(data["ts"], zone)
    if ts is None or (now - ts).total_seconds() / 60.0 > c["max_age_min"]:
        return None
    res = []
    for p in data.get("problems") or []:
        if not isinstance(p, dict) or not isinstance(p.get("id"), str) or not p["id"]:
            continue
        pid = p["id"]
        if any(pid.startswith(x) for x in c["ignore_id_prefixes"]):
            continue
        level = "crit" if any(pid.startswith(x) for x in c["critical_id_prefixes"]) else "warn"
        res.append(_obs(server, "monitoring_file", pid, level, str(p.get("level", "")), str(p.get("text", ""))))
    return res
