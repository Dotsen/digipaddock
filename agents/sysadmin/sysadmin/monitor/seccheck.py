"""Security checks: pure parsers and observations.

The state is the dict state["sec"] (see sec_state), the baseline is Baseline. No SSH."""
import fnmatch
import json
import re
from datetime import datetime, timedelta
from typing import Dict, List, NamedTuple, Optional, Set, Tuple

from .baseline import REMOVED, Baseline, is_weak, stage_ip
from .checks import _MONTHS, _obs, clean
from kd import clock  # .checks puts common/ on the path
from .model import Observation

SECTIONS = ("users", "groups", "keys", "sudoers", "shadow", "sshd", "cron", "persist", "units", "fw")
# the snapshot's reference sections are not compared with the baseline themselves; they explain keys and logins
# (which key, whose)
INFO_SECTIONS = ("keyprints", "pubkeys")
KEY_FLAGS = {"command": "принудительная команда", "from": "только с адресов from", "cert-authority": "центр сертификации"}
KEYS_SHOWN = 3
COMMENT_MAX = 80

ISO_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:[+-]\d{2}:?\d{2}|Z))\s+(.*)$")
OLD_TS = re.compile(r"^([A-Z][a-z]{2})\s+(\d{1,2}) (\d{2}):(\d{2}):(\d{2})\s+(.*)$")
# The patterns are anchored at the start of the message (after the host name) and applied through .match: sshd logs
# the client's text before authorization ("Invalid user …", "Received disconnect …: …"), and a search anywhere in the
# line can be forged.
SSHD = r"^\S+ sshd(?:-session)?\[\d+\]: "  # OpenSSH ≥ 9.8 logs as sshd-session
ACCEPT = re.compile(SSHD + r"Accepted (\S+) for (\S+) from (\S+) port \d+(?: ssh2)?(?::\s+(\S+) (SHA256:\S+))?")
# the user name is the client's text (it may contain spaces and "from"): the address is the last "from … port", sshd
# writes it; the "Invalid user" of the same attempt is not counted
# rsyslog folds repeats: "message repeated K times: [ Failed … ]" is K more attempts
FAILED = re.compile(SSHD + r"(?:message repeated (\d+) times: \[ )?Failed \S+ for .* from (\S+) port \d+")
DENY = re.compile(r"^\S+ agent-ro-gate(?:\[\d+\])?: deny ")
LAST_LINES_MAX = 500


def sec_state(state: dict) -> dict:
    sec = state.get("sec")
    if not isinstance(sec, dict):
        sec = state["sec"] = {}
    for k in ("logins", "pending", "week", "updates", "updates_sec", "reboot", "cores", "load_streak", "current", "fails",
              "units_seen", "units_down", "expect", "plan_windows"):
        if not isinstance(sec.get(k), dict):
            sec[k] = {}
    for k in ("accepted", "untrusted"):
        if not isinstance(sec.get(k), list):
            sec[k] = []
    return sec


def note_accepted(sec: dict, now: datetime, text: str) -> None:
    sec["accepted"].append({"at": now.isoformat(), "text": text})


def note_untrusted(sec: dict, now: datetime, text: str) -> None:
    if not any(u["text"] == text for u in sec["untrusted"]):
        sec["untrusted"].append({"at": now.isoformat(), "text": text})
        del sec["untrusted"][:-200]


# --- auth.log / journal sshd --------------------------------------------------------

class Login(NamedTuple):
    ts: datetime
    method: str
    user: str
    ip: str
    identity: str


class AuthResult(NamedTuple):
    logins: List[Login]
    failed: int
    failed_ips: Set[str]
    denies: int
    last_ts: Optional[str]
    last_lines: List[str]
    first_ts: Optional[str]  # the oldest timestamp in the window: the runner sees a window overflow by it
    lines: int  # how many lines with a timestamp were parsed


def parse_ts(line: str, now: datetime, shift: timedelta = timedelta(0), zone=None) -> Tuple[Optional[datetime], str]:
    """(the timestamp in the host's zone, the rest of the line); no timestamp — (None, line).
    The traditional format without a zone is the server's local time: in its zone `zone` (the inventory's "tz", right
    across a clock change too), without one — shift = local − the host's zone (checks.server_shift)."""
    m = ISO_TS.match(line)
    if m:
        raw = m.group(1).replace("Z", "+00:00")
        if re.search(r"[+-]\d{4}$", raw):
            raw = raw[:-2] + ":" + raw[-2:]
        try:
            return datetime.fromisoformat(raw).astimezone(clock.zone()), m.group(2)
        except ValueError:
            return None, line
    m = OLD_TS.match(line)
    if m and m.group(1) in _MONTHS:
        # the year is chosen by the server's clock
        local_now = (now.astimezone(zone) if zone is not None else now.astimezone(clock.zone()) + shift).replace(
            tzinfo=None)
        try:
            dt = datetime(local_now.year, _MONTHS[m.group(1)], int(m.group(2)), int(m.group(3)), int(m.group(4)),
                          int(m.group(5)))
            if dt > local_now + timedelta(days=1):
                dt = dt.replace(year=dt.year - 1)
        except ValueError:
            return None, line
        if zone is not None:
            return dt.replace(tzinfo=zone).astimezone(clock.zone()), m.group(6)
        return dt.replace(tzinfo=clock.zone()) - shift, m.group(6)
    return None, line


def identity_of(method: str, user: str, keytype: Optional[str], fp: Optional[str]) -> str:
    if method == "publickey" and fp:
        return f"{user}@{keytype} {fp}"
    if method == "publickey":
        return f"{user}@key ?"
    return f"{user}@password"  # password, keyboard-interactive and the rest without a key


def parse_auth(output: str, last_ts: Optional[str], last_lines, now: datetime,
               shift: timedelta = timedelta(0), zone=None) -> AuthResult:
    """The new lines after (last_ts, last_lines): logins, failed attempts, gateway refusals; timestamps as in `parse_ts`."""
    edge = datetime.fromisoformat(last_ts) if last_ts else None
    seen = set(last_lines or ())
    logins: List[Login] = []
    failed, failed_ips, denies, count = 0, set(), 0, 0
    new_edge, new_lines, first = edge, set(seen), None
    for ln in clean(output).splitlines():
        ts, rest = parse_ts(ln, now, shift, zone)
        if ts is None:
            continue
        ts = ts.replace(microsecond=0)  # the boundary is kept in seconds; `seen` tells the lines of one second apart
        count += 1
        if first is None or ts < first:
            first = ts
        if edge is not None and (ts < edge or (ts == edge and ln in seen)):
            continue
        if new_edge is None or ts > new_edge:
            new_edge, new_lines = ts, {ln}
        elif ts == new_edge:
            new_lines.add(ln)
        m = ACCEPT.match(rest)
        if m:
            method, user, ip, keytype, fp = m.groups()
            logins.append(Login(ts, method, user, ip, identity_of(method, user, keytype, fp)))
            continue
        m = FAILED.match(rest)
        if m:
            failed += int(m.group(1)) if m.group(1) else 1
            failed_ips.add(m.group(2))
            continue
        if DENY.match(rest):
            denies += 1
    return AuthResult(logins, failed, failed_ips, denies, new_edge.isoformat() if new_edge else last_ts,
                      sorted(new_lines)[:LAST_LINES_MAX], first.isoformat() if first else None, count)


def _week_login(week: dict, name: str, ip: str) -> None:
    if name == "stage":
        return  # service logins (agent-ro, <prefix>-exec) are not people
    w = week["logins"].setdefault(name, {"n": 0, "ips": []})
    w["n"] += 1
    if ip not in w["ips"] and len(w["ips"]) < 20:
        w["ips"].append(ip)


def logins_obs(server: str, auth: AuthResult, sec: dict, baseline: Baseline, cfg: dict, now: datetime) -> List[Observation]:
    """The rules in order: a trusted IP → silence (the key is enrolled); known here → silence (from an untrusted address
    — a note); known on another server → enrolled with a note on an ordinary server, ⚠️ until accept on a critical one;
    otherwise — a sticky problem until accept. Identities without a key are never enrolled. The factory host's IP is
    trusted only for agent-ro."""
    c = cfg["security"]
    crit = server in c["critical_servers"]
    srv = baseline.server(server)
    pending = sec["pending"].setdefault(server, {})
    week = sec["week"].setdefault(server, {"logins": {}, "failed": 0, "failed_ips": [], "denies": 0})
    for lg in auth.logins:
        weak = is_weak(lg.identity)
        known = lg.identity in srv["identities"] and not weak
        label = baseline.is_trusted(lg.ip)
        if label == "stage" and lg.user != "agent-ro":
            label = None  # from the factory host's IP without agent-ro — as untrusted: a web shell there must not stay silent
        name = srv["identities"].get(lg.identity) or lg.identity
        if known:
            _week_login(week, name, lg.ip)
            if label is None and not (name == "stage" and lg.ip == stage_ip()):  # the exec key from the factory host is a service one
                note_untrusted(sec, now, f"{server}: «{name}» с {lg.ip}")
            continue
        if label is not None:
            if not weak:
                baseline.record_identity(server, lg.identity, label)
                note_accepted(sec, now, f"{server}: ключ {lg.user} ({lg.identity.split('@', 1)[1]}) занесён — "
                                        f"вход с доверенного адреса {lg.ip} («{label}»)")
            _week_login(week, label, lg.ip)
            continue
        other = None if weak else baseline.label_elsewhere(lg.identity, server)
        if other is not None and not crit:
            baseline.record_identity(server, lg.identity, other[0])
            note_accepted(sec, now, f"{server}: ключ «{other[0] or lg.identity}» впервые на {server} с {lg.ip} "
                                    f"(принят на {other[1]} серв.)")
            _week_login(week, other[0] or lg.identity, lg.ip)
            continue
        p = pending.setdefault(lg.identity, {"first": lg.ts.isoformat(), "last": lg.ts.isoformat(), "ips": [],
                                             "count": 0, "user": lg.user, "label": "", "elsewhere": False})
        p["last"] = lg.ts.isoformat()
        p["count"] += 1
        if lg.ip not in p["ips"] and len(p["ips"]) < c["pending_ips_max"]:
            p["ips"].append(lg.ip)
    for ident in [i for i in pending if i in srv["identities"] and not is_weak(i)]:
        del pending[ident]  # accepted through accept/baseline or enrolled by this tick — no longer waiting
    for ident, p in pending.items():  # "accepted on other servers" — by the baseline as it is now: accepted later or already removed
        other = None if is_weak(ident) else baseline.label_elsewhere(ident, server)
        p["elsewhere"], p["label"] = other is not None, (other[0] if other else "")
    week["failed"] += auth.failed
    for ip in sorted(auth.failed_ips):
        if ip not in week["failed_ips"] and len(week["failed_ips"]) < 1000:
            week["failed_ips"].append(ip)
    week["denies"] += auth.denies
    res, places = [], None
    for ident, p in sorted(pending.items()):
        how = "по паролю" if ident.endswith("@password") else "по ключу " + ident.split("@", 1)[1]
        ips = ", ".join(p["ips"])
        named = f" «{p['label']}»" if p.get("label") else ""
        hint = f" — ключ{named} принят на других серверах" if p.get("elsewhere") else ""
        fp = "" if is_weak(ident) else ident.rsplit(" ", 1)[-1]
        if fp.startswith("SHA256:") and places is None:
            places = key_places(sec["current"])
        where = _where(server, places.get(fp, [])) if fp.startswith("SHA256:") else ""
        level = "warn" if p.get("elsewhere") or not crit else "crit"
        res.append(_obs(server, "logins", ident, level, f"с {ips} ({p['count']} раз)",
                        f"вход {p['user']} {how} с {ips} (первый {p['first'][11:16]}, всего {p['count']}){hint}{where}"))
    return res


# --- key fingerprints -------------------------------------------------------------------

class KeyPlace(NamedTuple):
    server: str
    section: str  # keyprints — may log in (authorized_keys), pubkeys — lies there (~/.ssh/*.pub)
    path: str
    kind: str
    fp: str
    flags: str
    comment: str


def _printable(text: str, limit: int = COMMENT_MAX) -> str:
    """The file's owner writes a key's comment (and the snapshot comes from the target): only printable ASCII goes into a problem's text."""
    return re.sub(r"[^\x20-\x7e]", "?", text)[:limit]


def _key_item(section: str, item: str, value: str):
    """An object `path type fingerprint` of the section keyprints|pubkeys → (path, type, fingerprint, options, comment);
    None — the line of the file itself (keyprints:<path>) or not a key."""
    parts = item.rsplit(" ", 2)
    if len(parts) != 3 or not parts[2].startswith("SHA256:"):
        return None
    if section == "pubkeys":
        flags, comment = "-", ("" if value == "-" else value)
    else:
        flags, _, comment = value.partition(" ")
    return parts[0], parts[1], parts[2], flags, _printable(comment)


def _key_note(flags: str, comment: str) -> str:
    marks = [KEY_FLAGS.get(f, f) for f in flags.split(",") if f and f != "-"]
    return (f" «{comment}»" if comment else "") + (f" ({', '.join(marks)})" if marks else "")


def key_places(current: dict) -> Dict[str, List[KeyPlace]]:
    """{fingerprint → where it is in the servers' latest snapshots (sec["current"])}: where it may log in, where it lies."""
    out: Dict[str, List[KeyPlace]] = {}
    for server, cur in sorted((current or {}).items()):
        parsed = parse_snapshot(str(cur.get("text", ""))) if isinstance(cur, dict) else None
        for key, value in sorted((parsed or ({}, {}))[0].items()):
            section, item = key.split(":", 1)
            p = _key_item(section, item, value) if section in INFO_SECTIONS else None
            if p:
                out.setdefault(p[2], []).append(KeyPlace(server, section, *p))
    return out


def allowed_paths(cur, fingerprint: str) -> Tuple[Optional[List[str]], str]:
    """(the authorized_keys files of one server's snapshot (sec["current"][server]) that still let this key
    in, "") — or (None, why) when the snapshot cannot prove the key gone: none or unreadable, a failed section, sshd
    letting keys in past the files the snapshot reads (a keys command, a CA, an AuthorizedKeysFile outside the homes),
    or a key file whose keys it could not read (a symlink, too big, unreadable)."""
    parsed = parse_snapshot(str(cur.get("text", ""))) if isinstance(cur, dict) else None
    if parsed is None:
        return None, "нет снимка ключей"
    items, errors = parsed
    failed = [s for s in ("keys", "keyprints", "sshd") if s in errors]
    if failed:
        return None, f"секция {failed[0]} снимка с ошибкой"
    for key in ("authorizedkeyscommand", "trustedusercakeys"):
        if items.get(f"sshd:{key}", "-").lower() not in ("-", "none"):
            return None, f"sshd пускает не только по authorized_keys ({key})"
    for rel in items.get("sshd:authorizedkeysfile", "").split():
        rel = rel.replace("%h/", "")
        if rel.startswith("/") or "%" in rel:
            return None, f"AuthorizedKeysFile {rel} снимок не читает"
    paths, read = [], set()
    for key, value in sorted(items.items()):
        section, item = key.split(":", 1)
        if section != "keyprints":
            continue
        p = _key_item(section, item, value)
        if p is None:
            read.add(item)  # the file's own line: its keys were read
        elif p[2] == fingerprint:
            paths.append(p[0])
    for key, value in sorted(items.items()):
        section, item = key.split(":", 1)
        if section == "keys" and value != "-" and item not in read:
            return None, f"ключи {item} снимок не прочитал ({value[:40]})"
    return paths, ""


def _where(server: str, places: List[KeyPlace]) -> str:
    """For a login by key: which authorized_keys of this server lets it in, and on which servers its .pub lies."""
    own = [pl for pl in places if pl.section == "keyprints" and pl.server == server]
    held = [pl for pl in places if pl.section == "pubkeys"]
    parts = [f"ключ из {pl.path}{_key_note(pl.flags, pl.comment)}" for pl in own[:KEYS_SHOWN]]
    parts += [f"лежит на {pl.server}: {pl.path}{_key_note(pl.flags, pl.comment)}" for pl in held[:KEYS_SHOWN]]
    more = len(own) + len(held) - len(parts)
    return "".join("; " + p for p in parts) + (f" и ещё {more}" if more > 0 else "")


def accepted_keys(baseline: Baseline) -> Dict[str, List[Tuple[str, str, str, str]]]:
    """{fingerprint → [(server, user, type, label)]} — the logins accepted into the baseline (identities with a key)."""
    out: Dict[str, List[Tuple[str, str, str, str]]] = {}
    if baseline.broken:
        return out
    for alias, srv in sorted(baseline.data["servers"].items()):
        for ident, label in sorted(srv["identities"].items()):
            user, _, key = ident.partition("@")
            kind, _, fp = key.partition(" ")
            if fp.startswith("SHA256:"):
                out.setdefault(fp, []).append((alias, user, kind, label))
    return out


def keys_report(places: Dict[str, List[KeyPlace]], accepted: dict, query: str) -> List[str]:
    """`kd-monitor keys [query]`: per key — where it may log in, where it lies, where its login is accepted. The query is
    a server, a part of a fingerprint, path, comment or label; a key is shown whole, with all its places."""
    def kind(fp):
        return places[fp][0].kind if places.get(fp) else accepted[fp][0][2]

    q = query.strip().lower()
    lines = []
    for fp in sorted(set(places) | set(accepted), key=lambda f: (kind(f), f)):
        pls, acc = places.get(fp, []), accepted.get(fp, [])
        words = [fp] + [w for pl in pls for w in (pl.server, pl.path, pl.comment)] + [w for a in acc for w in (a[0], a[1], a[3])]
        if q and not any(q in w.lower() for w in words if w):
            continue
        lines.append(f"{kind(fp)} {fp}")
        for pl in sorted(pls, key=lambda pl: (pl.section != "keyprints", pl.server, pl.path)):
            verb = "входит на" if pl.section == "keyprints" else "лежит на"
            lines.append(f"  {verb} {pl.server}: {pl.path}{_key_note(pl.flags, pl.comment)}")
        if acc:
            lines.append("  вход принят: " + ", ".join(f"{a[0]} {a[1]}" + (f" «{a[3]}»" if a[3] else "") for a in acc))
    return lines


def accept_keyprints(baseline: Baseline, server: str, path: str, value: str, current) -> None:
    """After an accept of the keys of the file path: the baseline's fingerprints come from the latest snapshot if it is
    of the same file version (value); otherwise the baseline has no fingerprints of this file — the next change will
    say "cannot tell"."""
    snap = baseline.server(server)["snapshot"]
    for key in _file_prints(snap, path):
        del snap[key]
    parsed = parse_snapshot(str(current.get("text", ""))) if isinstance(current, dict) else None
    items = parsed[0] if parsed and "keyprints" not in parsed[1] else {}
    if items.get("keys:" + path) == value and items.get("keyprints:" + path) == value:
        snap.update(_file_prints(items, path))
    baseline.dirty = True


def _file_prints(snapshot: dict, path: str) -> Dict[str, str]:
    """The keyprints lines of the file path from a snapshot or a baseline: the file itself (`keyprints:<path>`) and its keys."""
    head = "keyprints:"
    return {k: v for k, v in snapshot.items() if k == head + path or (
        k.startswith(head + path + " ") and (_key_item("keyprints", k[len(head):], v) or ("",))[0] == path)}


def _keys_detail(path: str, old, new, items: dict, base: dict, errors: dict) -> str:
    """Which keys of the file were added and removed — by the fingerprints of the same file version as the keys value
    (the line `keyprints:<path>` equals it); "" — the snapshot has none (an old gateway, a section with an error, not a
    regular file)."""
    if "keyprints" in errors:
        return ""

    def side(snapshot, value):
        if value is None or value == "-":
            return {}
        if not SHA_LINES.match(value) or snapshot.get("keyprints:" + path) != value:
            return None
        found = (_key_item("keyprints", k[len("keyprints:"):], v) for k, v in _file_prints(snapshot, path).items())
        return {f"{p[1]} {p[2]}": (p[3], p[4]) for p in found if p}

    after = side(items, new)
    if after is None:
        return ""
    before = side(base, old)
    if before is None:  # the file is gone — all its keys went, and which ones the baseline lacked cannot be told anyway
        return "" if new in (None, "-") else "; какие ключи изменились — не определить: в эталоне нет их отпечатков"

    def listed(one, many, names, prints):
        shown = [name + _key_note(*prints[name]) for name in names[:KEYS_SHOWN]]
        more = f" и ещё {len(names) - KEYS_SHOWN}" if len(names) > KEYS_SHOWN else ""
        return (f"{one} {shown[0]}" if len(names) == 1 else f"{many}: " + ", ".join(shown)) + more

    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(k for k in set(after) & set(before) if after[k] != before[k])
    parts = []
    if added:
        parts.append(listed("добавлен ключ", "добавлены ключи", added, after))
    if removed:
        parts.append(listed("удалён ключ", "удалены ключи", removed, before))
    if changed:
        parts.append(listed("изменены опции или комментарий ключа", "изменены опции или комментарии ключей",
                            changed, after))
    return "; " + "; ".join(parts or ["набор ключей тот же"])


# --- listeners / WireGuard peers ---------------------------------------------------------

SS_LINE = re.compile(r"^(tcp|udp)\s+\S+\s+\d+\s+\d+\s+(\S+)\s+\S+\s*(.*)$")
PROC = re.compile(r'\("([^"]+)",pid=')  # every owner in `users:(("a",pid=…),("b",pid=…))`
WG_KEY = re.compile(r"^[A-Za-z0-9+/]{42,43}=$")
EPHEMERAL_FROM = 32768  # ip_local_port_range: a random port is an object by its process, not its number
LOOPBACK = ("127.", "[::1]", "[::ffff:127.", "localhost")


def _proc_name(name: str) -> str:
    """The process sets its own name (prctl PR_SET_NAME) and ss does not escape it: printable ASCII only, ≤ 32."""
    return re.sub(r"[^\x20-\x7e]", "?", name)[:32]


def parse_listeners(output: str) -> Dict[str, str]:
    """`ss -tlnpu` → {proto:address:port → process}; `*:port` → `[::]:port`; UDP without a process (the kernel's
    WireGuard, random ports) is skipped; a port ≥ 32768 → `proto:address:eph:<process>` (runners, cache servers).
    Several owners of a socket (their order in ss is unstable) → sorted unique names joined by `+`."""
    out: Dict[str, str] = {}
    for ln in clean(output).splitlines():
        m = SS_LINE.match(ln.strip())
        if not m:
            continue
        proto, local, rest = m.groups()
        if local.startswith("*:"):
            local = "[::]" + local[1:]
        users = rest[rest.find("users:("):] if "users:(" in rest else ""
        proc = "+".join(sorted(set(_proc_name(n) for n in PROC.findall(users)))) or "-"
        if proto == "udp" and proc == "-":
            continue
        addr, _, port = local.rpartition(":")
        if port.isdigit() and int(port) >= EPHEMERAL_FROM:
            if proc == "-":
                continue
            local = f"{addr}:eph:{proc}"
        out[f"{proto}:{local}"] = proc
    return out


def _ignored(obj: str, patterns) -> bool:
    return any(fnmatch.fnmatchcase(obj, p) for p in patterns)


def ports_obs(server, listeners: Dict[str, str], base: Dict[str, str], cfg) -> List[Observation]:
    """Only new listeners and a change of the process on a known port; a disappearance is no alarm.
    A new listener on loopback only — ⚠️ even on a critical server."""
    c = cfg["security"]
    crit = server in c["critical_servers"]
    res = []
    for obj, proc in sorted(listeners.items()):
        if _ignored(obj, c.get("ports_ignore", ())):
            continue
        old = base.get(obj)
        local = obj.split(":", 1)[1]
        level = "crit" if crit and not local.startswith(LOOPBACK) else "warn"
        if old is None:
            res.append(_obs(server, "ports", obj, level, proc, f"новый слушатель {obj} ({proc})"))
        elif old != proc and "-" not in (old, proc):
            res.append(_obs(server, "ports", obj, level, proc, f"{obj} теперь слушает {proc} вместо {old}"))
    return res


def parse_wgpeers(output: str) -> List[str]:
    """`wg show all allowed-ips` → sorted `interface:public_key`."""
    out = set()
    for ln in clean(output).splitlines():
        f = ln.split()
        if len(f) >= 2 and WG_KEY.match(f[1]):
            out.add(f"{f[0]}:{f[1]}")
    return sorted(out)


def wgpeers_obs(server, peers: List[str], base) -> List[Observation]:
    known = set(base)
    res = []
    for p in peers:
        if p in known:
            continue
        iface, key = p.split(":", 1)
        res.append(_obs(server, "wgpeers", p, "warn", "новый", f"новый пир {iface}: {key[:12]}…"))
    return res


# --- the snapshot -----------------------------------------------------------------------

def parse_snapshot(output: str):
    """→ ({section:object → value}, {section → the error's reason, 'truncated' included}); None — the output is not a snapshot."""
    lines = clean(output).splitlines()
    if not lines or not lines[0].startswith("# agent-sec-snapshot"):
        return None
    items: Dict[str, str] = {}
    errors: Dict[str, str] = {}
    for ln in lines[1:]:
        if ln.startswith("#"):
            continue
        f = ln.split("\t")
        if len(f) != 3:
            continue
        section, item, value = f
        if item == "error":
            errors[section] = value
        else:
            items[f"{section}:{item}"] = value
    return items, errors


_BACKDOOR_KEYS = ("authorizedkeyscommand", "authorizedkeyscommanduser", "trustedusercakeys", "authorizedprincipalsfile")
_YES_IS_WEAK = ("permitrootlogin", "passwordauthentication", "permitemptypasswords", "kbdinteractiveauthentication",
                "challengeresponseauthentication", "permituserenvironment")


def _weaker(item, old, new) -> bool:
    """An sshd setting became weaker — 🔴 on any server."""
    if item in _YES_IS_WEAK:
        return new == "yes" and old != "yes"
    if item in ("allowusers", "denyusers", "allowgroups"):
        return old not in (None, "-") and new in (None, "-")
    if item in _BACKDOOR_KEYS:  # a login past authorized_keys: a command, a CA, principals
        return new not in (None, "-", "none") and old != new
    if item == "port":
        return old is not None and new is not None and old != new
    return False


def _sec_level(section, item, old, new, crit) -> str:
    if section in ("users", "groups", "keys", "sudoers"):
        return "crit" if crit else "warn"
    if section == "sshd":
        if item.startswith("file:"):
            return "crit" if crit else "warn"
        return "crit" if _weaker(item, old, new) else "warn"
    if section == "persist":
        return "crit" if item == "/etc/ld.so.preload" else "warn"
    if section == "fw":
        # transitional states (activating, reloading…) — ⚠️; switching off — as in the posture; None — the line is gone
        if item in ("ufw", "fail2ban") and old == "active" and new in (None, "inactive", "absent", "failed"):
            return "crit" if crit else "warn"
        if item == "ufw:default" and new and new.startswith("allow (incoming)"):
            return "crit"
        return "warn"
    return "warn"  # shadow, cron, units, ufw rules


def dangerous(section: str, item: str, old: Optional[str], new: Optional[str]) -> bool:
    """a change a plan step's window never takes into the baseline on its own, on any server (the owner's
    list of 03.11.2026): login keys, sudo rules, admin groups, a uid 0 account, a weaker sshd, ld.so.preload, the
    firewall switched off or open by default. It stays an incident the owner accepts."""
    if section in ("keys", "sudoers", "groups"):
        return True
    if section == "users":
        return (new or "").startswith("0 ") and not (old or "").startswith("0 ")
    if section == "sshd":
        return not item.startswith("file:") and _weaker(item, old, new)
    if section == "persist":
        return item == "/etc/ld.so.preload"
    if section == "fw":
        return _sec_level(section, item, old, new, True) == "crit"
    return False


def unit_of(item: str) -> str:
    """The systemd unit a units object belongs to: a `<target>.wants|requires/<unit>` link and a `<unit>.d/<conf>`
    drop-in are their unit's; a file in /etc/systemd/system is itself."""
    head, _, tail = item.partition("/")
    if not tail:
        return item
    if head.endswith((".wants", ".requires")):
        return tail
    if head.endswith(".d"):
        return head[:-2]
    return item


SHA = re.compile(r"^[0-9a-f]{64}$")
SHA_LINES = re.compile(r"^[0-9a-f]{64} \d+$")  # keys: `sha256 line_count`
_FILE_SECTIONS = ("keys", "sudoers", "shadow", "cron", "persist", "units")


def _show(value: str) -> str:
    """A value for the text: a sha256 — shortened, the rest (symlink -> …, big N, error X, other) — as it is."""
    if SHA.match(value) or SHA_LINES.match(value):
        return value[:12] + "…" + value[64:]
    return value


def _is_file(section, item) -> bool:
    return section in _FILE_SECTIONS or (section == "sshd" and item.startswith("file:"))


def _absent(section, item, value) -> bool:
    """For file objects `-` (no file) and a missing line are the same thing: there is no file."""
    return value is None or (value == "-" and _is_file(section, item))


def same_value(section, item, a, b) -> bool:
    """Two values of one snapshot object are the same: equal, or both say the file is not there."""
    return a == b or (_absent(section, item, a) and _absent(section, item, b))


# what the object is — in human words, for a problem's text
_LABEL = {"users": "пользователь", "groups": "группа", "sudoers": "правила sudo", "shadow": "пароли",
          "cron": "задание cron", "persist": "автозапуск", "units": "юнит systemd", "fw": "файрвол"}


def _subject(section, item) -> str:
    if section == "keys":  # /home/<user>/.ssh/authorized_keys → "SSH login keys of the user <user> (path)"
        parts = item.strip("/").split("/")
        user = "root" if parts[0] == "root" else parts[1] if parts[0] == "home" and len(parts) > 2 else ""
        return f"ключи входа SSH пользователя {user} ({item})" if user else f"ключи входа SSH {item}"
    return f"{_LABEL.get(section, section)} {item}"


def _describe(section, item, old, new) -> str:
    """The text of a deviation. For file objects `-` (no file) is described as absence."""
    if _is_file(section, item):
        old, new = (None if v == "-" else v for v in (old, new))
    what = "добавлено" if old is None else "удалено" if new is None else "изменено"  # no grammatical gender: keys, a group, a unit
    if old and new:
        if SHA_LINES.match(old) and SHA_LINES.match(new):
            return f"{what}, строк {old.split()[-1]} → {new.split()[-1]}"
        if (SHA.match(old) and SHA.match(new)) or (section == "sshd" and item.startswith("file:")):
            return f"{what} содержимое"
        return f"{what}, {_show(old)} → {_show(new)}"
    value = old or new
    if SHA_LINES.match(value):
        return f"{what}, строк {value.split()[-1]}"
    if SHA.match(value):
        return what
    return f"{what}, {_show(value)}"


def _units_ignored(item: str, patterns) -> bool:
    name = item.rsplit("/", 1)[-1]  # `multi-user.target.wants/snap.x.service` → by the unit's name
    return _ignored(name, patterns) or _ignored(item, patterns)


def snapshot_obs(server, items: dict, errors: dict, base: dict, cfg) -> List[Observation]:
    """The snapshot's deviations from the baseline; sections with an error are skipped (the runner marks their source
    ran=False). A disabled ufw prints no policy or rules — they are not compared, the observation `ufw` is enough."""
    c = cfg["security"]
    crit = server in c["critical_servers"]
    ufw_off = items.get("fw:ufw") != "active"
    res = []
    units: Dict[str, Dict[str, Tuple[Optional[str], Optional[str]]]] = {}
    for key in sorted(set(items) | set(base)):
        section, item = key.split(":", 1)
        if section in errors or section in INFO_SECTIONS:
            continue
        if section == "units" and _units_ignored(item, c.get("units_ignore", ())):
            continue
        if ufw_off and key.startswith("fw:ufw:"):
            continue
        old, new = base.get(key), items.get(key)
        # a new home without authorized_keys (or a home without the file is gone) — access neither grew nor shrank
        if old == new or (_absent(section, item, old) and _absent(section, item, new)):
            continue
        if section == "units":  # a unit with its links and drop-ins is one incident
            units.setdefault(unit_of(item), {})[item] = (old, new)
            continue
        detail = _keys_detail(item, old, new, items, base, errors) if section == "keys" else ""
        res.append(_obs(server, "sec:" + section, item, _sec_level(section, item, old, new, crit),
                        REMOVED if new is None else new,
                        f"{_subject(section, item)}: {_describe(section, item, old, new)}{detail}"))
    for unit, objects in sorted(units.items()):
        value = units_value({item: new for item, (_, new) in objects.items()})
        res.append(_obs(server, "sec:units", unit, "warn", value, f"{_subject('units', unit)}: {_unit_text(objects)}"))
    return res


def units_value(objects: Dict[str, Optional[str]]) -> str:
    """The value of a unit's incident: each changed object of the unit and its new value (None — removed),
    as canonical JSON — its hash is what the owner signs, Baseline.accept takes every object from it."""
    return json.dumps(objects, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _unit_text(objects) -> str:
    """What changed in a unit, in words: its file, the targets that start it, its drop-ins."""
    parts = []
    for item, (old, new) in sorted(objects.items(), key=lambda kv: (kv[0] != unit_of(kv[0]), kv[0])):
        head, _, tail = item.partition("/")
        if not tail:
            parts.append(_describe("units", item, old, new))
        elif head.endswith((".wants", ".requires")):
            target = head.rsplit(".", 1)[0]
            parts.append(f"включён в {target}" if old is None else f"выключен из {target}" if new is None
                         else f"{target}: {_describe('units', item, old, new)}")
        else:
            parts.append(f"drop-in {tail}: {_describe('units', item, old, new)}")
    return "; ".join(parts)


def posture_obs(server, items: dict, cfg) -> List[Observation]:
    """Static rules over the snapshot; the ok observations are needed for a problem to close after a fix."""
    g = items.get
    password = g("sshd:passwordauthentication") == "yes" or g("sshd:challengeresponseauthentication") == "yes" \
        or g("sshd:kbdinteractiveauthentication") == "yes"
    rules = [
        ("ssh-password", password, "sshd пускает по паролю (PasswordAuthentication/KbdInteractiveAuthentication/"
                                   "ChallengeResponseAuthentication yes) — предложить no"),
        ("ssh-root", g("sshd:permitrootlogin") == "yes",
         "sshd: PermitRootLogin yes — предложить prohibit-password (вход по ключу остаётся)"),
        ("ufw", g("fw:ufw") == "inactive", "ufw установлен, но выключен"),
        ("fail2ban", g("fw:fail2ban") in ("inactive", "absent", "failed"), f"fail2ban: {g('fw:fail2ban', '?')}"),
    ]
    return [_obs(server, "posture", s, "warn" if bad else "ok", "плохо" if bad else "ок", t) for s, bad, t in rules]


# --- updates / reboot / automatic updates / end of support ------------------------------

APT_LINE = re.compile(r"^([^/\s]+)/(\S+)\s+\S+\s+\S+\s+\[upgradable")
DNF_NAME = re.compile(r"^(\S+)\.(?:x86_64|aarch64|noarch|i686)$")


def parse_apt_security(output: str) -> Dict[str, bool]:
    """`apt list --upgradable` → {package → security?} (a source with "security", e.g. noble-security)."""
    out: Dict[str, bool] = {}
    for ln in clean(output).splitlines():
        m = APT_LINE.match(ln)
        if m:
            out[m.group(1)] = any("security" in s for s in m.group(2).split(","))
    return out


def parse_dnf_security(output: str) -> Dict[str, bool]:
    """`dnf -q -C check-update --security` (code 100): `package.arch version repo`; dnf wraps a long name onto a
    line of its own (the version and the repo on the next, indented), a long version — with the repo on the next.
    All lines are security."""
    out: Dict[str, bool] = {}
    for ln in clean(output).splitlines():
        if ln[:1].isspace():
            continue  # the tail of a wrap: the version and the repo
        f = ln.split()
        m = DNF_NAME.match(f[0]) if f else None
        if m and len(f) in (1, 2, 3):
            out[m.group(1)] = True
    return out


def updates_obs(server, pending: Dict[str, bool], sec: dict, cfg, now) -> List[Observation]:
    """One problem per server (the object security); the age counts from the package's first appearance in sec["updates"].
    Security packages are tracked, and critical ones (the critical_packages globs) even without the security mark:
    third-party repositories (MySQL, nginx.org, the php PPA) set no "-security" suite.
    ⚠️: security > warn_days, critical security > critical_warn_days, critical without the mark ≥ critical_any_days;
    🔴: only critical security > crit_days (the only reason worth a wake at night)."""
    c = cfg["security"]
    u = c["updates"]
    first = sec["updates"].setdefault(server, {})
    secpk = {p for p, s in pending.items() if s}
    critpk = {p for p in pending if any(fnmatch.fnmatchcase(p, pat) for pat in c["critical_packages"])}
    tracked = secpk | critpk
    sec["updates_sec"][server] = sorted(secpk)  # the weekly line "security updates: N" counts only these
    for p in tracked:
        first.setdefault(p, now.isoformat())
    for p in list(first):
        if p not in tracked:
            del first[p]
    if not tracked:
        return [_obs(server, "updates", "security", "ok", "0", "security-обновлений нет")]
    ages = {p: (now - datetime.fromisoformat(first[p])).days for p in tracked}
    oldest = max(ages.values())
    crit_sec = sorted(critpk & secpk)
    crit_any = sorted(critpk - secpk)
    sec_oldest = max((ages[p] for p in secpk), default=0)
    if any(ages[p] > u["crit_days"] for p in crit_sec):
        level = "crit"
    elif sec_oldest > u["warn_days"] or any(ages[p] > u["critical_warn_days"] for p in crit_sec) \
            or any(ages[p] >= u["critical_any_days"] for p in crit_any):
        level = "warn"
    else:
        level = "ok"
    names = crit_sec + crit_any + sorted(secpk - critpk)
    value = f"{len(secpk)} security" + (f" + {len(crit_any)} критичных без пометки" if crit_any else "") \
        + f", старейшее {oldest} дн"
    text = f"security-обновлений {len(secpk)}" + (f", критичных без пометки {len(crit_any)}" if crit_any else "") \
        + f", ждут до {oldest} дн: " + ", ".join(names[:5]) + (" …" if len(names) > 5 else "")
    return [_obs(server, "updates", "security", level, value, text)]


def parse_needrestart(output: str) -> Optional[bool]:
    """True — the kernel is outdated (KSTA 2/3), False — it is not (1), None — cannot tell (0 or junk)."""
    for ln in clean(output).splitlines():
        if ln.startswith("NEEDRESTART-KSTA:"):
            v = ln.split(":", 1)[1].strip()
            return {"1": False, "2": True, "3": True}.get(v)
    return None


def parse_needs_restarting(rc: int, output: str) -> Optional[bool]:
    """`dnf needs-restarting -r`: code 1 + "Reboot is required" — needed; code 0 — not; otherwise — cannot tell."""
    low = clean(output).lower()
    if rc == 1 and "reboot is required" in low:
        return True
    if rc == 0 and "reboot" in low:
        return False
    return None


def reboot_obs(server, pending: bool, sec: dict, cfg, now) -> List[Observation]:
    first = sec["reboot"]
    if not pending:
        first.pop(server, None)
        return [_obs(server, "reboot", "-", "ok", "нет", "перезагрузка не требуется")]
    since = first.setdefault(server, now.isoformat())
    days = (now - datetime.fromisoformat(since)).days
    level = "warn" if days >= cfg["security"]["reboot_warn_days"] else "ok"
    return [_obs(server, "reboot", "-", level, f"{days} дн", f"ядро обновлено, перезагрузка ждёт {days} дн")]


def autoupdates_obs(server, enabled_out: str, conf_out: str, cfg, dnf: bool = False) -> List[Observation]:
    """Ubuntu: `systemctl is-enabled apt-daily.timer apt-daily-upgrade.timer unattended-upgrades` (a line per unit)
    + `APT::Periodic::Unattended-Upgrade "N"` ≥ 1. AlmaLinux: `systemctl is-enabled dnf-automatic-install.timer
    dnf-automatic.timer` (the first installs by itself; the second only with apply_updates = yes in /etc/dnf/automatic.conf)."""
    states = [ln.strip() for ln in clean(enabled_out).splitlines()]
    if dnf:
        install_timer = states[:1] == ["enabled"]
        plain_timer = states[1:2] == ["enabled"]
        apply = re.search(r"^\s*apply_updates\s*=\s*yes", clean(conf_out), re.M | re.I) is not None
        ok = install_timer or (plain_timer and apply)
        text = "dnf-automatic ставит обновления сам" if ok else \
            "dnf-automatic не ставит обновления (нет dnf-automatic-install.timer, apply_updates ≠ yes)"
    else:
        timers = states[:2] == ["enabled", "enabled"]
        # from the start of the line: `// APT::Periodic::…` is a comment; on repeats apt takes the last value
        vals = re.findall(r'^\s*APT::Periodic::Unattended-Upgrade\s+"(\d+)"', clean(conf_out), re.M)
        on = bool(vals) and int(vals[-1]) >= 1
        ok = timers and on
        text = "unattended-upgrades включён" if ok else \
            "unattended-upgrades выключен" + ("" if timers else " (apt-daily*.timer)") + ("" if on else " (20auto-upgrades)")
    return [_obs(server, "autoupdates", "-", "ok" if ok else "warn", "вкл" if ok else "выкл", text)]


def parse_os_release(output: str) -> Tuple[str, str]:
    d = {}
    for ln in clean(output).splitlines():
        if "=" in ln:
            k, v = ln.split("=", 1)
            d[k.strip()] = v.strip().strip('"')
    return d.get("ID", "").lower(), d.get("VERSION_ID", "")


def eol_obs(server, os_id: str, version: str, cfg, now) -> List[Observation]:
    """The end of support by the table security.eol.dates (the exact version, then the major one); not in the table — "n/a"."""
    c = cfg["security"]["eol"]
    table = c["dates"].get(os_id, {})
    date = table.get(version) or table.get(version.split(".")[0])
    if not date:
        return [_obs(server, "eol", "-", "ok", "н/д", f"{os_id} {version}: нет в таблице конца поддержки")]
    left = (datetime.fromisoformat(date).replace(tzinfo=clock.zone()) - now).days
    level = "warn" if left <= c["warn_days"] else "ok"
    tail = f"осталось {left} дн" if left >= 0 else f"истекла {-left} дн назад"
    return [_obs(server, "eol", "-", level, f"{left} дн", f"{os_id} {version}: поддержка до {date} ({tail})")]


# --- load --------------------------------------------------------------------------------

def parse_cores(output: str) -> int:
    return sum(1 for ln in clean(output).splitlines() if re.match(r"^processor\s*:", ln))


def parse_load1(uptime_out: str) -> Optional[float]:
    m = re.search(r"load averages?:\s*(\d+[.,]\d+)", uptime_out)
    return float(m.group(1).replace(",", ".")) if m else None


def load_obs(server, load1: Optional[float], sec: dict, cfg) -> List[Observation]:
    """⚠️ at load1 > warn_x × cores for `streak` ticks in a row; 🔴 on critical servers at > crit_x ×."""
    cores = sec["cores"].get(server)
    if load1 is None or not cores:
        return []
    c = cfg["security"]["load"]
    ratio = load1 / cores
    streak = sec["load_streak"]
    streak[server] = streak.get(server, 0) + 1 if ratio > c["warn_x"] else 0
    high = server in cfg["security"]["critical_servers"] and ratio > c["crit_x"]
    level = ("crit" if high else "warn") if streak[server] >= c["streak"] else "ok"
    return [_obs(server, "load", "-", level, f"{load1:g} на {cores} ядер",
                 f"нагрузка {load1:g} при {cores} ядрах ({ratio:.1f}×), {streak[server]} тиков подряд")]
