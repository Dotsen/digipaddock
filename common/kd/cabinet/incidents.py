"""The monitor's servers, checks and incidents P-n in the cabinet.

kd-monitor (its tick runs as the sysadmin agent's own user) writes here through the socket; the cabinet is the source
of truth for the numbers P-n and for an incident's acknowledgement and mute, which the monitor reads back every tick.

- ``monitor.push {items}`` — the tick's changes in order, in one transaction: ``sync`` (the polled servers, the last
  result of every check that ran, the status changes the monitor saw), ``open`` (a new problem gets the next number;
  a key that has an incident open keeps that one), ``update`` (a new level, a fix mark, a failed fix), ``resolve``.
  Each item carries its own ``key``: an item applied once answers its first result again (a batch resent after a lost
  answer, the monitor's queue after an outage). An item the cabinet cannot take answers its ``error`` and is skipped,
  so one bad item never holds the queue behind it.
- ``incident.import {items, next_id}`` — the monitor's state.json, once: its numbers, statuses and history; the
  counter continues the monitor's own. A number already here is left as it is, so a repeat adds nothing.
- ``incident.list``, ``incident.get``, ``server.list``, ``server.get`` — reading, with ``servers.read``; a server
  comes with its week of status changes, its incidents and cards, the trusted lists of the last sync (``trust``:
  the shared addresses and the server's identities, ports and peers) and its last "Check now"; an incident —
  with its cards (the label ``P-n``, the fixed card).
- ``server.check.request {alias}`` ("Check now", ``incidents.ack``): one active request per server, a second
  press answers the same one; ``server.check.take`` and ``server.check.done`` — the monitor's timer takes the queued
  ones and reports each; nobody finished in 10 minutes — failed. Every step is a ``server.check`` event.
- A ``sync`` with ``partial`` (a tick of some servers) never switches the unlisted servers off.
- ``incident.ack``, ``incident.mute``, ``incident.unmute`` — what ``kd-monitor ack`` always did: acknowledged silences
  the opening and the closing until it resolves, a mute — until its time; a worse level and a failed fix still wake
  (``incidents.ack``).

Writes are the monitor's (the sysadmin agent's user) and root's; their events are the monitor's own actor, never the
agent's, so the agent's board-write limit does not count them. ``monitor.purge`` (the scheduler) drops the check
history older than 7 days and the applied keys older than 8 (the monitor's queue holds 7).
"""

import json
import re
import uuid
from datetime import datetime, timedelta

from kd import clock, ids
from kd.cabinet import api

MONITOR_AGENT = "sysadmin"
MONITOR_ACTOR = "system:monitor"
READ_CAPABILITY = "monitor.read"  # an agent's right to the reads through kd-monitor-read
LEVELS = {"ok": 0, "warn": 1, "crit": 2}
STATUSES = ("open", "acknowledged", "resolved")
CHANGES = ("worse", "better", "fixed", "fix_failed")
# disabled: the owner switched its check off on the server; revoke: a login's access is gone
HOW = ("close", "accept", "disabled", "revoke")
HISTORY_DAYS = 7
HISTORY_MAX = 50000  # a server page's week of status changes; only a guard of the answer's size
APPLIED_DAYS = 8
REQUEST_TTL_MIN = 10  # a "Check now" nobody finished by then is an "error"
ACTIVE = ("queued", "running")
_ACTIVE_SQL = "'queued', 'running'"
ITEMS_MAX = 5000
LIST_MAX = 500
NAME_MAX = 200
KEY_MAX = 600
VALUE_MAX = 200
TEXT_MAX = 2000
ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
CARD = ids.CARD
# a history entry of the monitor's state.json -> the event of the incident's timeline
IMPORTED = {"open": "opened", "worse": "worse", "better": "better", "close": "resolved", "accept": "resolved",
            "disabled": "resolved", "revoke": "resolved", "fixed": "fixed", "fix_failed": "fix_failed"}


class ItemError(Exception):
    """An item the cabinet cannot take: its ``error`` in the answer, the rest of the batch goes on."""


# --- who ------------------------------------------------------------------------------------------
def _monitor(principal) -> bool:
    return principal.kind == "agent" and principal.agent == MONITOR_AGENT


def _writer(principal) -> None:
    if principal.kind != "root" and not _monitor(principal):
        raise api.ApiError("refused", "only the monitor (the sysadmin agent) and root write servers and incidents")


def _reader(ctx, principal, request=None) -> None:
    """Who reads servers and incidents. ``request`` is given by the reads only: there a board executor
    (kd-monitor-read) reads for the agent it names, when that agent's manifest holds monitor.read."""
    if principal.kind in ("root", "cabinet") or _monitor(principal):
        return
    if principal.kind == "user" and api.allowed(ctx, principal, "servers.read"):
        return
    if principal.kind == "executor" and request is not None:
        api.executor_capability(ctx, principal, request, READ_CAPABILITY)
        return
    raise api.ApiError("refused", "no permission servers.read")


def _acker(ctx, principal) -> None:
    if principal.kind == "root" or _monitor(principal):
        return
    if principal.kind == "user" and api.allowed(ctx, principal, "incidents.ack"):
        return
    raise api.ApiError("refused", "no permission incidents.ack")


# --- values ---------------------------------------------------------------------------------------
def utc(value, name="time") -> str:
    """An ISO moment with its offset -> the cabinet's ``YYYY-MM-DDTHH:MM:SSZ``."""
    if not isinstance(value, str) or not value:
        raise ItemError(f"{name} is an ISO time")
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ItemError(f"{name} is an ISO time")
    if moment.tzinfo is None:
        raise ItemError(f"{name} carries its offset")
    return clock.iso(moment)


def _name(value, name, limit=NAME_MAX, empty=False) -> str:
    if not isinstance(value, str) or (not value and not empty) or len(value) > limit:
        raise ItemError(f"{name} is text of at most {limit} characters")
    return value


def _free(value, limit) -> str:
    """Text the monitor took from a server: never a reason to refuse, cut to its limit."""
    return str("" if value is None else value)[:limit]


def _digest(value) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ItemError("value_hash is a SHA256 digest")
    return value


def _level(value, name="level") -> str:
    if value not in LEVELS:
        raise ItemError(f"{name} is one of ok, warn, crit")
    return value


def _count(value, name) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ItemError(f"{name} is a count")
    return value


def _number(request) -> int:
    n = request.get("incident")
    if isinstance(n, str) and n.startswith("P-"):
        n = n[2:]
        n = int(n) if n.isdigit() else None
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise api.ApiError("invalid", "incident is a number P-<n>")
    return n


def _row(row) -> dict:
    data = dict(row)
    data["id"] = f"P-{data['n']}"
    return data


def _worse(a: str, b: str) -> str:
    return a if LEVELS.get(a, 0) >= LEVELS.get(b, 0) else b


# --- the store ------------------------------------------------------------------------------------
def _get(s, n: int):
    return s._conn.execute("SELECT * FROM incidents WHERE n=?", (n,)).fetchone()


def _find(s, key: str, opened_at: str = ""):
    """The incident of a monitor problem: the one opened at that moment, else the key's open one."""
    if opened_at:
        found = s._conn.execute("SELECT * FROM incidents WHERE key=? AND opened_at=? ORDER BY n DESC LIMIT 1",
                                (key, opened_at)).fetchone()
        if found is not None:
            return found
    return s._conn.execute("SELECT * FROM incidents WHERE key=? AND status!='resolved' ORDER BY n DESC LIMIT 1",
                           (key,)).fetchone()


def _next(s) -> int:
    row = s._conn.execute("SELECT value FROM meta WHERE key='next_incident'").fetchone()
    top = s._conn.execute("SELECT COALESCE(MAX(n), 0) FROM incidents").fetchone()[0]
    return max(int(row[0]) if row else 1, top + 1)


def _set_next(s, value: int) -> None:
    s._conn.execute("INSERT INTO meta (key, value) VALUES ('next_incident', ?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(value),))


def _event(s, actor, kind, n, **payload) -> int:
    return s._event(actor, f"incident.{kind}", "", {"incident": n, "id": f"P-{n}", **payload})


def _update(s, n: int, **fields) -> None:
    sets = ", ".join(f'"{k}"=?' for k in fields)
    s._conn.execute(f"UPDATE incidents SET {sets} WHERE n=?", (*fields.values(), n))


# --- monitor.push -----------------------------------------------------------------------------------
def _trust(s, trust, now) -> None:
    """The trusted lists of the monitor's baseline: the shared addresses replace the old ones, a listed
    server's identities, ports and peers replace its own; a server the sync does not list keeps what it had."""
    if not isinstance(trust, dict):
        raise ItemError("trust is an object")
    lists = []
    trusted = trust.get("trusted")
    if trusted is not None:
        if not isinstance(trusted, dict):
            raise ItemError("trust.trusted is {address: label}")
        lists.append(("", [("trusted", _name(k, "address", 64), _free(v, NAME_MAX)) for k, v in trusted.items()]))
    servers = trust.get("servers") or {}
    if not isinstance(servers, dict):
        raise ItemError("trust.servers is {alias: lists}")
    for alias, lists_of in servers.items():
        if not isinstance(alias, str) or not ALIAS.fullmatch(alias) or not isinstance(lists_of, dict):
            raise ItemError("trust.servers is {alias: {identities, ports, wgpeers}}")
        rows = []
        for kind, field in (("identity", "identities"), ("port", "ports"), ("snapshot", "snapshot")):
            values = lists_of.get(field) or {}
            if not isinstance(values, dict):
                raise ItemError(f"trust {field} is an object")
            rows += [(kind, _name(k, field, KEY_MAX), _free(v, NAME_MAX)) for k, v in values.items()]
        peers = lists_of.get("wgpeers") or []
        if not isinstance(peers, list):
            raise ItemError("trust wgpeers is a list")
        rows += [("wgpeer", _name(p, "wgpeer", KEY_MAX), "") for p in peers]
        try:  # when the server's baseline was taken; none from an older monitor, a bad one is not shown
            rows.append(("baseline", utc(lists_of.get("taken_at"), "taken_at"), ""))
        except ItemError:
            pass
        lists.append((alias, rows))
    for scope, rows in lists:
        previous = s._conn.execute("SELECT MAX(at) FROM server_trust WHERE scope=?", (scope,)).fetchone()[0]
        if previous and previous > now:
            continue
        s._conn.execute("DELETE FROM server_trust WHERE scope=?", (scope,))
        s._conn.executemany("INSERT OR REPLACE INTO server_trust (scope, kind, item, label, at) VALUES (?,?,?,?,?)",
                            [(scope, kind, key, label, now) for kind, key, label in rows])


def _changed(s, server, check, subject, row, observed_at) -> None:
    """An open difference from the baseline whose content changes again (another hash) — the owner
    accepts what is there now, so the timeline says so (``incident.changed``, silent). A threshold's value moving
    within its level stays the snapshot's."""
    from kd.cabinet import monitor_trust

    value_hash = _digest(row.get("value_hash"))
    for found in s._conn.execute(
            'SELECT n, "check", subject, value_hash FROM incidents WHERE server=? AND "check"=? AND subject IN (?,?)'
            " AND status!='resolved' AND last_seen<=?",
            (server, check, subject, "-" if not subject else subject, observed_at)).fetchall():
        if (found["value_hash"] and value_hash and value_hash != found["value_hash"]
                and not monitor_trust.acceptable(found["check"], found["subject"])):
            _event(s, MONITOR_ACTOR, "changed", found["n"], value=_free(row.get("value"), VALUE_MAX), at=observed_at)


def _sync(s, item, now) -> dict:
    at = utc(item.get("at"), "at")
    full = s._conn.execute("SELECT value FROM meta WHERE key='monitor_full_at'").fetchone()
    if not item.get("partial") and full and full[0] > at:
        return {"stale": True}
    if (not full or at >= full[0]) and item.get("baseline_state") in ("available", "unavailable"):
        s._conn.execute("INSERT INTO meta(key,value) VALUES('monitor_baseline_state',?)"
                        " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (item["baseline_state"],))
    if (not full or at >= full[0]) and isinstance(item.get("diagnostics"), dict):
        diagnostics = {name: _count(item["diagnostics"].get(name, 0), name)
                       for name in ("queued_alerts", "queued_changes")}
        s._conn.execute("INSERT INTO meta(key,value) VALUES('monitor_diagnostics',?)"
                        " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(diagnostics),))
    servers = item.get("servers")
    if item.get("trust") is not None:
        _trust(s, item["trust"], at)
    if servers is not None:
        if not isinstance(servers, list):
            raise ItemError("servers is a list")
        names = []
        for row in servers:
            if not isinstance(row, dict):
                raise ItemError("a server is an object")
            alias = _name(row.get("alias"), "alias", 64)
            if not ALIAS.fullmatch(alias):
                raise ItemError("alias is a server alias")
            count = _count(row.get("unreachable_count", 0), "unreachable_count")
            names.append((alias, _free(row.get("host"), NAME_MAX), count))
        for alias, host, count in names:
            source_server = next(r for r in servers if r["alias"] == alias)
            polled_at = utc(source_server.get("at", at), "server.at")
            last_reply = source_server.get("last_reply_at", polled_at)
            last_reply = utc(last_reply, "last_reply_at") if last_reply else ""
            previous = s._conn.execute("SELECT polled_at FROM servers WHERE alias=?", (alias,)).fetchone()
            if previous and previous[0] > polled_at:
                continue
            reachable = 1 if count == 0 else 0
            s._conn.execute(
                "INSERT INTO servers (alias, host, enabled, reachable, unreachable_count, last_ok, updated)"
                " VALUES (?,?,1,?,?,?,?) ON CONFLICT(alias) DO UPDATE SET host=excluded.host, enabled=1,"
                " reachable=excluded.reachable, unreachable_count=excluded.unreachable_count,"
                " last_ok=CASE WHEN excluded.reachable=1 AND excluded.last_ok!='' AND excluded.last_ok>=servers.last_ok"
                " THEN excluded.last_ok ELSE servers.last_ok END,"
                " updated=excluded.updated",
                (alias, host, reachable, count, last_reply if reachable else "", now))
            s._conn.execute("UPDATE servers SET polled_at=? WHERE alias=?", (polled_at, alias))
        known = [a for a, _, _ in names]
        marks = ",".join("?" * len(known))
        if not item.get("partial"):  # a tick of some servers ("Check now") never switches the rest off
            s._conn.execute(f"UPDATE servers SET enabled=0, updated=? WHERE enabled=1"
                            + (f" AND alias NOT IN ({marks})" if known else ""), (now, *known))
            s._conn.execute("INSERT INTO meta(key,value) VALUES('monitor_full_at',?)"
                            " ON CONFLICT(key) DO UPDATE SET value=MAX(value,excluded.value)", (at,))
    checks = item.get("checks", [])
    if not isinstance(checks, list):
        raise ItemError("checks is a list")
    moved = []  # (n, value, value_hash) of the open incidents whose object this sync moved on
    for source in checks:
        if not isinstance(source, dict) or not isinstance(source.get("rows"), list):
            raise ItemError("a check is {server, check, rows}")
        server, check = _name(source.get("server"), "server"), _name(source.get("check"), "check")
        observed_at = utc(source.get("at", at), "check.at")
        previous = s._conn.execute('SELECT MAX(at) FROM server_checks WHERE server=? AND "check"=?',
                                  (server, check)).fetchone()[0]
        if previous and previous > observed_at:
            continue
        s._conn.execute('DELETE FROM server_checks WHERE server=? AND "check"=?', (server, check))
        for row in source["rows"]:
            if not isinstance(row, dict):
                raise ItemError("a check row is an object")
            s._conn.execute(
                'INSERT OR REPLACE INTO server_checks (server, "check", subject, level, value, text, at, timestamp_verified,value_hash)'
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (server, check, _name(row.get("subject", ""), "subject", KEY_MAX, empty=True),
                 _level(row.get("level")), _free(row.get("value"), VALUE_MAX), _free(row.get("text"), TEXT_MAX), observed_at,
                 int("at" in source), _digest(row.get("value_hash"))))
            subject = row.get("subject", "")
            if row.get("level") in ("warn", "crit"):
                _changed(s, server, check, subject, row, observed_at)
                value, value_hash = _free(row.get("value"), VALUE_MAX), _digest(row.get("value_hash"))
                which = (server, check, subject, "-" if not subject else subject, observed_at)
                moved.extend((r["n"], value, value_hash) for r in s._conn.execute(
                    'SELECT n FROM incidents WHERE server=? AND "check"=? AND subject IN (?,?) AND status!=\'resolved\''
                    " AND last_seen<=? AND (value IS NOT ? OR COALESCE(value_hash,'') IS NOT ?)",
                    which + (value, value_hash or "")).fetchall())
                # A new value within the same severity updates the snapshot, not the event log.
                s._conn.execute('UPDATE incidents SET value=?, text=?, last_seen=?,value_hash=? WHERE server=? AND "check"=?'
                                " AND subject IN (?,?) AND status!='resolved' AND last_seen<=?",
                                (value, _free(row.get("text"), TEXT_MAX), observed_at, value_hash) + which)
    from kd.cabinet import monitor_trust

    monitor_trust.expire_moved(s, moved, now)  # an accept of the old value would be refused by the monitor
    confirmations = item.get("confirmations", [])
    if not isinstance(confirmations, list) or not all(isinstance(c, dict) for c in confirmations):
        raise ItemError("confirmations is a list of observations")
    for confirmation in confirmations:
        found = _find(s, _name(confirmation.get("problem_key"), "problem_key", KEY_MAX),
                      utc(confirmation.get("opened_at"), "opened_at"))
        count = _count(confirmation.get("count"), "confirmation.count")
        started = utc(confirmation.get("started_at"), "confirmation.started_at")
        confirmed = utc(confirmation.get("at"), "confirmation.at")
        if found and count > 0 and found["opened_at"] <= started <= confirmed and (
            found["status"] != "resolved" or confirmed <= found["resolved_at"]
        ) and confirmed >= found["confirmation_at"] and (
            started > found["confirmation_started_at"] or count >= found["confirmation_count"]
        ):
            _update(s, found["n"], confirmation_count=count,
                    confirmation_started_at=started, confirmation_at=confirmed)
    for run in item.get("runs", []):
        state = run.get("state")
        if state not in ("success", "not_due", "failed", "disabled", "budget_exhausted", "unsupported"):
            raise ItemError("invalid check run state")
        run_at = utc(run.get("at", at), "run.at")
        s._conn.execute('INSERT INTO check_runs(server,"check",attempted_at,state,detail) VALUES(?,?,?,?,?)'
                        ' ON CONFLICT(server,"check") DO UPDATE SET attempted_at=excluded.attempted_at,'
                        " state=excluded.state,detail=excluded.detail WHERE check_runs.attempted_at<=excluded.attempted_at",
                        (_name(run.get("server"), "server"), _name(run.get("check"), "check"), run_at, state,
                         _free(run.get("detail"), TEXT_MAX)))
    changes = item.get("changes", [])
    if not isinstance(changes, list):
        raise ItemError("changes is a list")
    for change in changes:
        if not isinstance(change, dict):
            raise ItemError("a change is an object")
        s._conn.execute(
            'INSERT INTO check_history (server, "check", subject, level, prev, value, text, at, created)'
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (_name(change.get("server"), "server"), _name(change.get("check"), "check"),
             _name(change.get("subject", ""), "subject", KEY_MAX, empty=True), _level(change.get("level")),
             _level(change.get("prev"), "prev"), _free(change.get("value"), VALUE_MAX),
             _free(change.get("text"), TEXT_MAX), utc(change.get("at"), "at"), now))
    return {}


def _problem(value) -> dict:
    if not isinstance(value, dict):
        raise ItemError("problem is an object")
    level = _level(value.get("level"))
    return {
        "key": _name(value.get("key"), "key", KEY_MAX),
        "server": _name(value.get("server"), "server"),
        "check": _name(value.get("check"), "check"),
        "subject": _name(value.get("subject", ""), "subject", KEY_MAX, empty=True),
        "level": level,
        "max_level": _worse(_level(value.get("max_level", level), "max_level"), level),
        "value": _free(value.get("value"), VALUE_MAX),
        "value_hash": _digest(value.get("value_hash")),
        "text": _free(value.get("text"), TEXT_MAX),
        "opened_at": utc(value.get("opened_at"), "opened_at"),
        "repeats": _count(value.get("repeats", 0), "repeats"),
    }


def _open(s, item, now) -> dict:
    p = _problem(item.get("problem"))
    found = _find(s, p["key"])
    if found is not None:
        return {"incident": found["n"], "new": False}
    n = _next(s)
    s._conn.execute(
        'INSERT INTO incidents (n, key, server, "check", subject, level, max_level, value, text, status, opened_at,'
        " last_seen, repeats, updated) VALUES (?,?,?,?,?,?,?,?,?,'open',?,?,?,?)",
        (n, p["key"], p["server"], p["check"], p["subject"], p["level"], p["max_level"], p["value"], p["text"],
         p["opened_at"], p["opened_at"], p["repeats"], now))
    _set_next(s, n + 1)
    count = _count(item["problem"].get("confirmation_count", 0), "confirmation_count")
    started = item["problem"].get("confirmation_started_at", "")
    confirmed = item["problem"].get("confirmation_at", p["opened_at"])
    if count:
        started = utc(started, "confirmation_started_at")
        confirmed = utc(confirmed, "confirmation_at")
        if not p["opened_at"] <= started <= confirmed:
            raise ItemError("confirmation dates are out of order")
    _update(s, n, value_hash=p["value_hash"], confirmation_count=count,
            confirmation_started_at=started if count else "", confirmation_at=confirmed if count else "")
    _event(s, MONITOR_ACTOR, "opened", n, key=p["key"], server=p["server"], check=p["check"], subject=p["subject"],
           level=p["level"], value=p["value"], at=p["opened_at"])
    from kd.cabinet import monitor_windows

    monitor_windows.link_marks(s, n, p["server"], p["check"], p["subject"], now)
    return {"incident": n, "new": True}


def _target(s, item):
    key = _name(item.get("problem_key"), "problem_key", KEY_MAX)
    opened_at = utc(item["opened_at"], "opened_at") if item.get("opened_at") else ""
    found = _find(s, key, opened_at)
    if found is None:
        raise ItemError(f"no incident of {key}")
    return found


def _change(s, item, now) -> dict:
    found = _target(s, item)
    change = item.get("change")
    if change not in CHANGES:
        raise ItemError(f"change is one of {', '.join(CHANGES)}")
    at = utc(item.get("at"), "at")
    level = _level(item.get("level", found["level"]))
    fields = {"level": level, "max_level": _worse(found["max_level"], level), "last_seen": at, "updated": now}
    if "value" in item:
        fields["value"] = _free(item.get("value"), VALUE_MAX)
        fields["value_hash"] = _digest(item.get("value_hash"))
    if "text" in item:
        fields["text"] = _free(item.get("text"), TEXT_MAX)
    extra = {}
    if change == "fixed":
        card = item.get("card")
        if not isinstance(card, str) or not CARD.fullmatch(card):
            raise ItemError("card is KD-<n>")
        fields["fixed_card"] = extra["card"] = card
        if s._conn.execute("SELECT id FROM cards WHERE id=?", (card,)).fetchone():
            s._conn.execute("INSERT OR IGNORE INTO incident_cards VALUES (?,?,?,?)",
                            (found["n"], card, MONITOR_ACTOR, now))
    elif change == "fix_failed":
        fields["fixed_card"] = ""
    if found["status"] == "resolved":  # a late change of a problem that closed meanwhile: nothing to move
        return {"incident": found["n"]}
    if at >= found["last_seen"]:
        _update(s, found["n"], **fields)
        if "value" in fields:
            from kd.cabinet import monitor_trust

            monitor_trust.expire_moved(s, [(found["n"], fields["value"], fields["value_hash"])], now)
    _event(s, MONITOR_ACTOR, change, found["n"], level=level, value=fields.get("value", found["value"]), at=at,
           **extra)
    return {"incident": found["n"]}


def _resolve(s, item, now) -> dict:
    found = _target(s, item)
    how = item.get("how", "close")
    if how not in HOW:
        raise ItemError("how is " + ", ".join(HOW))
    if found["status"] == "resolved":
        return {"incident": found["n"]}
    at = utc(item.get("at"), "at")
    _update(s, found["n"], status="resolved", resolved_at=at, last_seen=at, updated=now, resolution=how)
    _event(s, MONITOR_ACTOR, "resolved", found["n"], how=how, value=_free(item.get("value"), VALUE_MAX), at=at)
    from kd.cabinet import monitor_trust

    monitor_trust.expire_for(s, found["n"], now)  # an accept nobody signed before it closed is moot
    return {"incident": found["n"]}


KINDS = {"sync": _sync, "open": _open, "update": _change, "resolve": _resolve}


def _items(request, empty=False) -> list:
    items = request.get("items")
    if not isinstance(items, list) or (not items and not empty) or len(items) > ITEMS_MAX:
        raise api.ApiError("invalid", f"items is a list of {0 if empty else 1} to {ITEMS_MAX} items")
    return items


def _push(ctx, principal, request):
    _writer(principal)
    items = _items(request)
    s, now, results = ctx.store, ctx.now(), []
    with s.transaction():
        for item in items:
            key = item.get("key") if isinstance(item, dict) else None
            if not isinstance(key, str) or not 1 <= len(key) <= 128:
                results.append({"key": key if isinstance(key, str) else "", "error": "an item has its key"})
                continue
            done = s._conn.execute("SELECT result FROM monitor_applied WHERE key=?", (key,)).fetchone()
            if done is not None:
                results.append({"key": key, **json.loads(done[0]), "repeat": True})
                continue
            handler = KINDS.get(item.get("kind"))
            if handler is None:
                results.append({"key": key, "error": f"kind is one of {', '.join(KINDS)}"})
                continue
            s._conn.execute("SAVEPOINT item")
            try:
                out = handler(s, item, now)
            except ItemError as exc:
                s._conn.execute("ROLLBACK TO item")
                s._conn.execute("RELEASE item")
                results.append({"key": key, "error": str(exc)})
                continue
            s._conn.execute("RELEASE item")
            s._conn.execute("INSERT INTO monitor_applied (key, result, created) VALUES (?,?,?)",
                            (key, json.dumps(out, sort_keys=True), now))
            results.append({"key": key, **out})
    return {"results": results}


# --- incident.import -------------------------------------------------------------------------------
def _import_one(s, item, now) -> bool:
    if not isinstance(item, dict):
        raise ItemError("an incident is an object")
    n = item.get("number")
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise ItemError("number is the incident's n")
    if _get(s, n) is not None:
        return False
    p = _problem(item)
    closed_at = utc(item["closed_at"], "closed_at") if item.get("closed_at") else ""
    acked = item.get("acked_until")
    status, muted = "open", ""
    if closed_at:
        status = "resolved"
    elif acked == "close":
        status = "acknowledged"
    elif acked:
        muted = utc(acked, "acked_until")
    card = item.get("fixed_card") or ""
    if card and not CARD.fullmatch(str(card)):
        card = ""
    last_seen = utc(item["last_seen"], "last_seen") if item.get("last_seen") else p["opened_at"]
    s._conn.execute(
        'INSERT INTO incidents (n, key, server, "check", subject, level, max_level, value, text, status, muted_until,'
        " opened_at, last_seen, resolved_at, fixed_card, repeats, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (n, p["key"], p["server"], p["check"], p["subject"], p["level"], p["max_level"], p["value"], p["text"], status,
         muted, p["opened_at"], last_seen, closed_at, card, p["repeats"], now))
    history = item.get("history") or []
    _update(s, n, value_hash=p["value_hash"])
    for entry in history if isinstance(history, list) else []:
        if not isinstance(entry, dict) or entry.get("kind") not in IMPORTED:
            continue
        try:
            at = utc(entry.get("at"), "at")
        except ItemError:
            continue
        extra = {"how": entry["kind"]} if IMPORTED[entry["kind"]] == "resolved" else {}
        _event(s, MONITOR_ACTOR, IMPORTED[entry["kind"]], n, level=_free(entry.get("level"), 8),
               value=_free(entry.get("value"), VALUE_MAX), at=at, imported=True, **extra)
        if status == "resolved" and extra:
            _update(s, n, resolution=entry["kind"])
    if status == "acknowledged":
        _event(s, MONITOR_ACTOR, "acknowledged", n, at=now, imported=True)
    elif muted:
        _event(s, MONITOR_ACTOR, "muted", n, until=muted, at=now, imported=True)
    return True


def _import(ctx, principal, request):
    _writer(principal)
    items = _items(request, empty=True)  # a monitor with no problems still brings its counter
    next_id = request.get("next_id", 1)
    if isinstance(next_id, bool) or not isinstance(next_id, int) or next_id < 1:
        raise api.ApiError("invalid", "next_id is the monitor's next number")
    s, now = ctx.store, ctx.now()
    added, kept, errors = [], [], []
    with s.transaction():
        for item in items:
            s._conn.execute("SAVEPOINT item")
            try:
                fresh = _import_one(s, item, now)
            except ItemError as exc:
                s._conn.execute("ROLLBACK TO item")
                s._conn.execute("RELEASE item")
                errors.append({"number": item.get("number") if isinstance(item, dict) else None, "error": str(exc)})
                continue
            s._conn.execute("RELEASE item")
            (added if fresh else kept).append(item["number"])
        _set_next(s, max(_next(s), next_id))
    return {"added": added, "kept": kept, "errors": errors, "next": _next(s)}


# --- reading -----------------------------------------------------------------------------------------
def _list(ctx, principal, request):
    _reader(ctx, principal, request)
    status = request.get("status", "")
    where, args = [], []
    if status == "active":
        where.append("status!='resolved'")
    elif status:
        if status not in STATUSES:
            raise api.ApiError("invalid", "status is active, open, acknowledged or resolved")
        where.append("status=?")
        args.append(status)
    server = request.get("server")
    if server is not None:
        if not isinstance(server, str):
            raise api.ApiError("invalid", "server is an alias")
        where.append("server=?")
        args.append(server)
    before = request.get("before")
    if before is not None:
        if isinstance(before, bool) or not isinstance(before, int):
            raise api.ApiError("invalid", "before is a number")
        where.append("n<?")
        args.append(before)
    limit = request.get("limit", LIST_MAX)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise api.ApiError("invalid", "limit is a count")
    sql = "SELECT * FROM incidents" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY n DESC LIMIT ?"
    rows = ctx.store._conn.execute(sql, (*args, min(limit, LIST_MAX))).fetchall()
    return {"items": [_row(r) for r in rows]}


def timeline(s, n: int) -> list:
    """The incident's events and those of the monitor's trust in its object (proposed, signed, applied …)."""
    rows = s._conn.execute(
        "SELECT seq, ts, actor, kind, payload FROM events WHERE json_extract(payload, '$.incident')=?"
        " AND (kind LIKE 'incident.%' OR kind LIKE 'monitor_trust.%') ORDER BY seq", (n,)).fetchall()
    identity = _get(s, n)
    context = {k: identity[k] for k in ("server", "check", "subject")} if identity else {}
    return [{"seq": r["seq"], "ts": r["ts"], "actor": r["actor"], "kind": r["kind"],
             "payload": {**context, **json.loads(r["payload"])}} for r in rows]


def _cards(ctx, principal, sql, args) -> list:
    """The cards the reader may see among ``sql``'s ids, newest first."""
    out = []
    for (card_id,) in ctx.store._conn.execute(sql, args).fetchall():
        try:
            record = ctx.store.card(card_id)
        except Exception:  # noqa: BLE001 — a fixed_card that names no card here is not an error of the page
            continue
        if api.visible(ctx, principal, record.project):
            out.append({**{k: getattr(record, k) for k in ("id", "title", "status", "project", "assignee")},
                        "summary": record.summary,
                        "pending_decision": bool(ctx.store._conn.execute(
                            "SELECT 1 FROM decisions WHERE card=? AND state='open' LIMIT 1", (record.id,)).fetchone())})
    return sorted(out, key=lambda c: int(c["id"].split("-")[1]), reverse=True)


def _incident(ctx, principal, request):
    _reader(ctx, principal, request)
    found = _get(ctx.store, _number(request))
    if found is None:
        raise api.ApiError("not_found", "no such incident")
    from kd.cabinet import monitor_trust

    from kd.cabinet import monitoring

    view = monitoring.incident_view(ctx, principal, _row(found), monitoring.config(ctx))
    return {"incident": view, "timeline": timeline(ctx.store, found["n"]), "cards": view["cards"],
            "trust": monitor_trust.for_incident(ctx.store, found["n"]),
            "acceptable": not monitor_trust.acceptable(found["check"], found["subject"]) and not view["accepted"],
            # a login by a key may close as "access revoked" — the monitor checks the key is gone, trusts nothing
            "revocable": not monitor_trust.revocable(found["check"], found["subject"]) and not view["accepted"]}


def worst(s, alias: str) -> str:
    """The worst level among a server's last results: crit, warn, ok, or "" — nothing checked yet."""
    levels = {r[0] for r in s._conn.execute("SELECT DISTINCT level FROM server_checks WHERE server=?", (alias,))}
    return next((level for level in ("crit", "warn", "ok") if level in levels), "")


def _servers(ctx, principal, request):
    _reader(ctx, principal, request)
    s, now = ctx.store, ctx.now()
    items = []
    for row in s._conn.execute("SELECT * FROM servers ORDER BY alias").fetchall():
        server = dict(row)
        levels = dict(s._conn.execute("SELECT level, COUNT(*) FROM server_checks WHERE server=? GROUP BY level",
                                      (server["alias"],)).fetchall())
        server["checks"] = {level: levels.get(level, 0) for level in LEVELS}
        server["worst"] = worst(s, server["alias"])
        server["incidents"] = s._conn.execute(
            "SELECT COUNT(*) FROM incidents WHERE server=? AND status!='resolved'", (server["alias"],)).fetchone()[0]
        server["check_now"] = _last_request(s, server["alias"], now)
        items.append(server)
    from kd.cabinet import monitoring

    cfg = monitoring.config(ctx)
    return {"items": [monitoring.server_view(ctx, row, cfg) for row in items]}


def _trusted(s, alias: str) -> dict:
    rows = s._conn.execute("SELECT scope, kind, item, label, at FROM server_trust WHERE scope IN ('', ?)"
                           " ORDER BY kind, item", (alias,)).fetchall()
    out = {"trusted": [], "identities": [], "ports": [], "wgpeers": [], "at": "", "taken_at": "", "snapshot": {}}
    field = {"trusted": "trusted", "identity": "identities", "port": "ports", "wgpeer": "wgpeers"}
    for r in rows:
        if r["kind"] == "baseline":
            out["taken_at"] = r["item"]
        elif r["kind"] == "snapshot":
            out["snapshot"][r["item"]] = r["label"]
        else:
            out[field[r["kind"]]].append({"item": r["item"], "label": r["label"]})
        out["at"] = max(out["at"], r["at"])
    return out


def _server(ctx, principal, request):
    _reader(ctx, principal, request)
    alias = request.get("alias")
    s, now = ctx.store, ctx.now()
    found = s._conn.execute("SELECT * FROM servers WHERE alias=?", (alias,)).fetchone() if isinstance(
        alias, str) else None
    if found is None:
        raise api.ApiError("not_found", "no such server")
    checks = s._conn.execute('SELECT * FROM server_checks WHERE server=? ORDER BY "check", subject',
                             (alias,)).fetchall()
    # the whole week the scheduler keeps (not the newest 500 rows); HISTORY_MAX only guards the answer
    history = s._conn.execute("SELECT * FROM check_history WHERE server=? AND at>=? ORDER BY at DESC, id DESC LIMIT ?",
                              (alias, _shift(now, HISTORY_DAYS), HISTORY_MAX)).fetchall()
    rows = s._conn.execute("SELECT * FROM incidents WHERE server=? ORDER BY status='resolved', n DESC LIMIT ?",
                           (alias, LIST_MAX)).fetchall()
    cards = _cards(ctx, principal, "SELECT card FROM card_servers WHERE server=?", (alias,))
    from kd.cabinet import monitor_trust

    from kd.cabinet import monitoring

    cfg = monitoring.config(ctx)
    full = s._conn.execute("SELECT value FROM meta WHERE key='monitor_full_at'").fetchone()
    diagnostics = s._conn.execute("SELECT value FROM meta WHERE key='monitor_diagnostics'").fetchone()
    from kd import monitor_settings as ms

    present = {monitoring.schedule(r["check"], r["subject"]) for r in checks}
    missing = sorted(ms.planned(cfg, alias) - present - ms.disabled(cfg, alias)) if cfg else []
    return {"server": monitoring.server_view(ctx, {**dict(found), "worst": worst(s, alias)}, cfg),
            "checks": [monitoring.check_view(ctx, dict(r), cfg) for r in checks],
            "monitor_full_at": full[0] if full else "",
            "missing_checks": missing,
            "diagnostics": json.loads(diagnostics[0]) if diagnostics else {},
            "history": [dict(r) for r in history], "incidents": [_row(r) for r in rows], "cards": cards,
            "trust": _trusted(s, alias), "check_now": _last_request(s, alias, now),
            "proposals": monitor_trust.for_server(s, alias)}


# --- "Check now" ----------------------------------------------------------------------------------------
def _request_row(row, now: str):
    """A request as the page shows it; an active one past REQUEST_TTL_MIN is a failure even before the purge
    writes it so."""
    if row is None:
        return None
    data = dict(row)
    if data["state"] in ACTIVE and data["created"] < _shift_min(now, REQUEST_TTL_MIN):
        data.update(state="failed", detail="timeout")
    return data


def _last_request(s, alias: str, now: str):
    return _request_row(s._conn.execute("SELECT * FROM check_requests WHERE server=? ORDER BY created DESC, rowid DESC"
                                        " LIMIT 1", (alias,)).fetchone(), now)


def _check_event(s, actor, row) -> None:
    s._event(actor, "server.check", "", {"server": row["server"], "id": row["id"], "state": row["state"],
                                         "detail": row["detail"]})


def _expire(s, now: str) -> int:
    """Requests no monitor finished within REQUEST_TTL_MIN: failed, so the page stops waiting."""
    stale = s._conn.execute(f"SELECT * FROM check_requests WHERE state IN ({_ACTIVE_SQL}) AND created<?",
                            (_shift_min(now, REQUEST_TTL_MIN),)).fetchall()
    for row in stale:
        s._conn.execute("UPDATE check_requests SET state='failed', detail='timeout', finished=? WHERE id=?",
                        (now, row["id"]))
        _check_event(s, MONITOR_ACTOR, {**dict(row), "state": "failed", "detail": "timeout"})
    return len(stale)


def _check_request(ctx, principal, request):
    """``server.check.request {alias}`` → the server's active request: a new one, or the one already waiting."""
    _acker(ctx, principal)
    alias = request.get("alias")
    s, now = ctx.store, ctx.now()
    with s.transaction():
        _expire(s, now)
        found = s._conn.execute("SELECT * FROM servers WHERE alias=?", (alias,)).fetchone() if isinstance(
            alias, str) else None
        if found is None:
            raise api.ApiError("not_found", "no such server")
        if not found["enabled"]:
            raise api.ApiError("invalid", f"{alias} is not checked by the monitor")
        active = s._conn.execute(f"SELECT * FROM check_requests WHERE server=? AND state IN ({_ACTIVE_SQL})",
                                 (alias,)).fetchone()
        if active is not None:
            return {"request": dict(active), "repeat": True}
        row = {"id": uuid.uuid4().hex[:16], "server": alias, "state": "queued",
               "requested_by": api.card_actor(principal), "detail": "", "created": now}
        s._conn.execute("INSERT INTO check_requests (id, server, state, requested_by, detail, created)"
                        " VALUES (:id, :server, :state, :requested_by, :detail, :created)", row)
        _check_event(s, api.card_actor(principal), row)
    return {"request": dict(s._conn.execute("SELECT * FROM check_requests WHERE id=?", (row["id"],)).fetchone()),
            "repeat": False}


def _check_take(ctx, principal, request):
    """``server.check.take`` (the monitor's timer) → the queued requests, now running."""
    _writer(principal)
    s, now = ctx.store, ctx.now()
    with s.transaction():
        _expire(s, now)
        rows = s._conn.execute("SELECT * FROM check_requests WHERE state='queued' ORDER BY created").fetchall()
        for row in rows:
            s._conn.execute("UPDATE check_requests SET state='running', started=? WHERE id=?", (now, row["id"]))
            _check_event(s, MONITOR_ACTOR, {**dict(row), "state": "running"})
    return {"items": [{"id": r["id"], "server": r["server"]} for r in rows]}


def _check_done(ctx, principal, request):
    """``server.check.done {id, ok, detail?}`` — the monitor's report: done, or failed with why."""
    _writer(principal)
    ok = request.get("ok")
    if not isinstance(ok, bool):
        raise api.ApiError("invalid", "ok is true or false")
    detail = _free(request.get("detail"), VALUE_MAX)
    s, now = ctx.store, ctx.now()
    with s.transaction():
        row = s._conn.execute("SELECT * FROM check_requests WHERE id=?", (request.get("id"),)).fetchone() if (
            isinstance(request.get("id"), str)) else None
        if row is None:
            raise api.ApiError("not_found", "no such check request")
        if row["state"] not in ACTIVE:  # timed out meanwhile: the late report changes nothing
            return {"request": dict(row)}
        state = "done" if ok else "failed"
        s._conn.execute("UPDATE check_requests SET state=?, detail=?, finished=? WHERE id=?",
                        (state, detail, now, row["id"]))
        _check_event(s, MONITOR_ACTOR, {**dict(row), "state": state, "detail": detail})
    return {"request": dict(s._conn.execute("SELECT * FROM check_requests WHERE id=?", (row["id"],)).fetchone())}


# --- the inbox and the overview -----------------------------------------------------------------
def alarms(ctx, principal) -> list:
    """Open crit incidents nobody acknowledged or muted: the inbox's and the overview's "Problems" — for who reads
    the servers, else none."""
    if principal.kind == "user" and not api.allowed(ctx, principal, "servers.read"):
        return []
    rows = ctx.store._conn.execute("SELECT * FROM incidents WHERE status='open' AND level='crit'"
                                   " AND (muted_until='' OR muted_until<=?) ORDER BY n DESC", (ctx.now(),)).fetchall()
    return [_row(r) for r in rows]


def collapse(rows: list) -> list:
    """the rows one tick opened for one check — one row, the first's fields with ``members`` (P-n), the
    ``servers`` and the ``group`` (as the query's: id, size, the servers and subjects counted); a lone row stays as
    it is."""
    groups = {}
    for row in rows:
        groups.setdefault((row["opened_at"], row["check"]), []).append(row)
    out = []
    for (at, check), members in groups.items():
        if len(members) == 1:
            out.append(members[0])
            continue
        servers = sorted({m["server"] for m in members})
        out.append({**members[0], "members": [m["id"] for m in members], "servers": servers,
                    "group": {"id": f"{at}|{check}", "size": len(members), "servers": len(servers),
                              "subjects": len({m["subject"] for m in members})}})
    return out


def active_count(ctx, principal):
    """Incidents not resolved yet — the incidents page's default «active»: the menu's count; None for who
    may not read the servers."""
    if principal.kind == "user" and not api.allowed(ctx, principal, "servers.read"):
        return None
    return ctx.store._conn.execute("SELECT COUNT(*) FROM incidents WHERE status!='resolved'").fetchone()[0]


def tile(ctx, principal):
    """The overview's "Servers": how many checked servers are down, at crit or at warn by their worst check; None
    for who may not read the servers."""
    if principal.kind == "user" and not api.allowed(ctx, principal, "servers.read"):
        return None
    s = ctx.store
    counts = {"servers": 0, "crit": 0, "warn": 0, "unreachable": 0}
    for row in s._conn.execute("SELECT alias, reachable FROM servers WHERE enabled=1").fetchall():
        counts["servers"] += 1
        if not row["reachable"]:
            counts["unreachable"] += 1
            continue
        level = worst(s, row["alias"])
        if level in ("crit", "warn"):
            counts[level] += 1
    return counts


# --- acknowledgement and mute -------------------------------------------------------------------------
def _active(ctx, request):
    found = _get(ctx.store, _number(request))
    if found is None:
        raise api.ApiError("not_found", "no such incident")
    if found["status"] == "resolved":
        raise api.ApiError("invalid", f"P-{found['n']} is resolved")
    return found


def _ack(ctx, principal, request):
    _acker(ctx, principal)
    s = ctx.store
    with s.transaction():
        found = _active(ctx, request)
        _update(s, found["n"], status="acknowledged", updated=ctx.now())
        _event(s, api.card_actor(principal), "acknowledged", found["n"], at=ctx.now())
    return {"incident": _row(_get(s, found["n"]))}


def _mute(ctx, principal, request):
    _acker(ctx, principal)
    try:
        until = utc(request.get("until"), "until")
    except ItemError as exc:
        raise api.ApiError("invalid", str(exc))
    s = ctx.store
    with s.transaction():
        found = _active(ctx, request)
        _update(s, found["n"], muted_until=until, updated=ctx.now())
        _event(s, api.card_actor(principal), "muted", found["n"], until=until, at=ctx.now())
    return {"incident": _row(_get(s, found["n"]))}


def _unmute(ctx, principal, request):
    _acker(ctx, principal)
    s = ctx.store
    with s.transaction():
        found = _active(ctx, request)
        _update(s, found["n"], status=found["status"] if request.get("preserve_ack") else "open",
                muted_until="", updated=ctx.now())
        _event(s, api.card_actor(principal), "unmuted", found["n"], at=ctx.now())
    return {"incident": _row(_get(s, found["n"]))}


def _unack(ctx, principal, request):
    _acker(ctx, principal)
    with ctx.store.transaction():
        found = _active(ctx, request)
        _update(ctx.store, found["n"], status="open", updated=ctx.now())
        _event(ctx.store, api.card_actor(principal), "unacknowledged", found["n"], at=ctx.now())
    return {"incident": _row(_get(ctx.store, found["n"]))}


# --- the scheduler -------------------------------------------------------------------------------------
def _shift(now: str, days: int) -> str:
    moment = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ") - timedelta(days=days)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _shift_min(now: str, minutes: int) -> str:
    moment = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ") - timedelta(minutes=minutes)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _purge(ctx, principal, request):
    s, now = ctx.store, ctx.now()
    with s.transaction():
        history = s._conn.execute("DELETE FROM check_history WHERE at<?", (_shift(now, HISTORY_DAYS),)).rowcount
        applied = s._conn.execute("DELETE FROM monitor_applied WHERE created<?", (_shift(now, APPLIED_DAYS),)).rowcount
        expired = _expire(s, now)
        s._conn.execute("DELETE FROM check_requests WHERE created<?", (_shift(now, HISTORY_DAYS),))
        from kd.cabinet import monitor_windows

        monitor_windows.purge(s, now, HISTORY_DAYS)
    return {"history": history, "applied": applied, "checks_expired": expired}


WRITERS = frozenset({"root", "agent"})
READERS = frozenset({"root", "cabinet", "agent", "user", "executor"})  # executor: reads only
OPS = {
    "monitor.push": (WRITERS, _push),
    "incident.import": (WRITERS, _import),
    "incident.list": (READERS, _list),
    "incident.get": (READERS, _incident),
    "server.list": (READERS, _servers),
    "server.get": (READERS, _server),
    "incident.ack": (frozenset({"root", "agent", "user"}), _ack),
    "incident.mute": (frozenset({"root", "agent", "user"}), _mute),
    "incident.unmute": (frozenset({"root", "agent", "user"}), _unmute),
    "incident.unack": (frozenset({"root", "agent", "user"}), _unack),
    "server.check.request": (frozenset({"root", "agent", "user"}), _check_request),
    "server.check.take": (WRITERS, _check_take),
    "server.check.done": (WRITERS, _check_done),
    "monitor.purge": (api.ROOTISH, _purge),
}
api.OPS.update(OPS)  # the board API serves them (api imports this module at its end)
