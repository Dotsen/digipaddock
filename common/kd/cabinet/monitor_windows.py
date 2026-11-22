"""The sysadmin's plan windows: a step of an approved plan changes a server on purpose, so its changes go
into the monitor's baseline instead of opening incidents.

- ``monitor.window.open {server, card, step, run}`` — the executor (or root), before the step: the window is open.
- ``monitor.window.close {id, changes, errors, failed?}`` — after the step: the objects the step changed
  (``{"<section>:<item>": [old, new]}``, a missing side is ``null``) and the snapshot's sections it could not compare;
  ``failed`` — the window holds nothing (the snapshot after the step was not taken).
- ``monitor.window.active`` — the monitor, before its security checks: the servers with an open window, which it
  skips this tick. A window open longer than 30 minutes expires here (the executor died mid-step): the changes are
  checked as usual.
- ``monitor.window.take`` / ``monitor.window.report {id, ok, accepted, dangerous, detail}`` — the monitor applies a
  closed window into its baseline and reports what it took and what it left: a dangerous object (keys, sudoers, a
  uid 0 user, a weaker sshd, …) still opens its incident, which the cabinet links to the window's card for 24 hours.

A run (one call of the executor, one or many servers) tells the card's thread once, after its last window settles.
A repeated close or report changes nothing, so the callers may repeat them after a lost answer.
"""

import json
import re
import secrets

from kd.cabinet import api, incidents, notices

STATES = ("open", "closed", "failed", "applied", "rejected", "expired")
EXPIRE_MIN = 30
MARK_MIN = 24 * 60
TAKE_MAX = 20
TEXT_MAX = 100
DETAIL_MAX = 500
CHANGES_MAX = 5000
CHANGES_BYTES = 1 << 20
SUBJECT_MAX = 512
LIST_MAX = 5  # objects named in a notice; the rest are counted
PARAM_MAX = 1000
WRITERS = frozenset({"exec", "root"})
MONITOR = frozenset({"agent", "root"})


def _invalid(message):
    return api.ApiError("invalid", message)


def _monitor(principal) -> None:
    if principal.kind != "root" and not incidents._monitor(principal):
        raise api.ApiError(
            "refused",
            "only the monitor (the sysadmin agent) and root read and settle windows",
        )


def _text(value, name, limit=TEXT_MAX, empty=False) -> str:
    if (
        not isinstance(value, str)
        or len(value) > limit
        or (not value and not empty)
        or "\n" in value
    ):
        raise _invalid(f"{name} is one line of at most {limit} characters")
    return value


WINDOW_ID = re.compile(r"[0-9a-f]{16}")


def _id(request) -> str:
    return _text(request.get("id"), "id", 32)


def _side(value):
    if value is not None and not isinstance(value, str):
        raise _invalid("a change is [old, new] of text or null")
    return value


def _changes(value) -> dict:
    if not isinstance(value, dict) or len(value) > CHANGES_MAX:
        raise _invalid(f"changes is an object of at most {CHANGES_MAX} objects")
    out = {}
    for key, pair in value.items():
        if not isinstance(key, str) or ":" not in key or len(key) > SUBJECT_MAX:
            raise _invalid("a change's key is <section>:<item>")
        if not isinstance(pair, list) or len(pair) != 2 or pair[0] == pair[1]:
            raise _invalid("a change is [old, new], and they differ")
        out[key] = [_side(pair[0]), _side(pair[1])]
    if len(json.dumps(out, ensure_ascii=False).encode("utf-8")) > CHANGES_BYTES:
        raise _invalid(f"changes are larger than {CHANGES_BYTES} bytes")
    return out


def _names(value, name) -> list:
    if not isinstance(value, list) or len(value) > CHANGES_MAX:
        raise _invalid(f"{name} is a list")
    return [_text(v, name, SUBJECT_MAX) for v in value]


def _pairs(value, name) -> list:
    if not isinstance(value, list) or len(value) > CHANGES_MAX:
        raise _invalid(f"{name} is a list of [check, subject]")
    out = []
    for pair in value:
        if not isinstance(pair, list) or len(pair) != 2:
            raise _invalid(f"{name} is a list of [check, subject]")
        out.append(
            [
                _text(pair[0], "check", 64),
                _text(pair[1], "subject", SUBJECT_MAX, empty=True),
            ]
        )
    return out


# --- the run's notice ----------------------------------------------------------------------------------------------
def _object(check, subject) -> str:
    return f"{check[4:] if check.startswith('sec:') else check}: {subject}"


def _listed(items) -> str:
    unique = list(dict.fromkeys(items))
    text = "; ".join(unique[:LIST_MAX]) + (
        f" +{len(unique) - LIST_MAX}" if len(unique) > LIST_MAX else ""
    )
    return text if len(text) <= PARAM_MAX else text[: PARAM_MAX - 1] + "…"


def _servers(rows) -> str:
    return _listed(r["server"] for r in rows).replace("; ", ", ")


def _settle(s, card, run) -> None:
    """The run's one notice, once none of its windows is open or closed; the failed and expired ones told their own."""
    left = s._conn.execute(
        "SELECT COUNT(*) FROM monitor_windows WHERE card=? AND run=? AND state IN ('open','closed')",
        (card, run),
    ).fetchone()[0]
    if left:
        return
    rows = [
        dict(r, result=json.loads(r["result"]))
        for r in s._conn.execute(
            "SELECT server, state, result FROM monitor_windows WHERE card=? AND run=? AND state IN ('applied','rejected')"
            " ORDER BY opened, rowid",
            (card, run),
        )
    ]
    applied = [r for r in rows if r["state"] == "applied"]
    took = [r for r in applied if r["result"].get("accepted")]
    still = [
        r
        for r in applied
        if not r["result"].get("accepted")
        and not r["result"].get("dangerous")
        and not r["result"].get("detail")
    ]
    kept = [
        r for r in applied if r["result"].get("detail")
    ]  # left to their incidents, with the reason
    rejected = [r for r in rows if r["state"] == "rejected"]
    posts = []
    if took:
        accepted = _listed(
            _object(c, x) for r in took for c, x in r["result"]["accepted"]
        )
        posts.append(
            (
                "info",
                "window_applied",
                {"servers": _servers(took), "accepted": accepted},
            )
        )
    if still:
        posts.append(("info", "window_unchanged", {"servers": _servers(still)}))
    dangerous = [
        f"{_object(c, x).replace(': ', ' ', 1)} ({r['server']})"
        for r in rows
        for c, x in r["result"].get("dangerous", [])
    ]
    if kept:
        detail = _listed(r["result"]["detail"] for r in kept)
        posts.append(
            ("info", "window_partial", {"servers": _servers(kept), "detail": detail})
        )
    if dangerous:
        posts.append(("warn", "window_dangerous", {"objects": _listed(dangerous)}))
    if rejected:
        detail = _listed(
            r["result"].get("detail", "") for r in rejected if r["result"].get("detail")
        )
        posts.append(
            (
                "warn",
                "window_rejected",
                {"servers": _servers(rejected), "detail": detail},
            )
        )
    for level, key, params in posts:
        notices.post_in_tx(
            s,
            source="monitor",
            level=level,
            key=f"notice.monitor.{key}",
            params=params,
            card=card,
        )


def _expire(s, now) -> None:
    for row in s._conn.execute(
        "SELECT id, server, card, run FROM monitor_windows WHERE state='open' AND opened<?",
        (incidents._shift_min(now, EXPIRE_MIN),),
    ).fetchall():
        s._conn.execute(
            "UPDATE monitor_windows SET state='expired', closed=? WHERE id=?",
            (now, row["id"]),
        )
        notices.post_in_tx(
            s,
            source="monitor",
            level="info",
            key="notice.monitor.window_expired",
            params={"server": row["server"]},
            card=row["card"],
        )
        _settle(s, row["card"], row["run"])


# --- operations ----------------------------------------------------------------------------------------------------
def _open(ctx, principal, request):
    server = request.get("server")
    if not isinstance(server, str) or not incidents.ALIAS.fullmatch(server):
        raise _invalid("server is a server's alias")
    card = request.get("card")
    if not isinstance(card, str) or not api.CARD.fullmatch(card):
        raise _invalid("card is KD-<n>")
    step, run = _text(request.get("step"), "step"), _text(request.get("run"), "run")
    # the executor names its window (review 9): a signal inside this call still lets it close the window
    wid = request.get("id") or secrets.token_hex(8)
    if not isinstance(wid, str) or not WINDOW_ID.fullmatch(wid):
        raise _invalid("id is 16 hex digits")
    s, now = ctx.store, ctx.now()
    with s.transaction():
        if (
            s._conn.execute("SELECT 1 FROM cards WHERE id=?", (card,)).fetchone()
            is None
        ):
            raise api.ApiError("not_found", f"{card} not found")
        same = s._conn.execute(
            "SELECT server, card, run FROM monitor_windows WHERE id=?", (wid,)
        ).fetchone()
        if (
            same is not None
        ):  # the same open again after a lost answer — the same window
            if tuple(same) != (server, card, run):
                raise api.ApiError("conflict", f"window {wid} is another step's")
            return {"id": wid}
        s._conn.execute(
            "INSERT INTO monitor_windows (id, server, card, step, run, state, opened)"
            " VALUES (?,?,?,?,?,'open',?)",
            (wid, server, card, step, run, now),
        )
    return {"id": wid}


def _close(ctx, principal, request):
    wid = _id(request)
    failed = request.get("failed") or ""
    if failed:
        failed, changes, errors = _text(failed, "failed", DETAIL_MAX), {}, []
    else:
        changes, errors = (
            _changes(request.get("changes")),
            _names(request.get("errors", []), "errors"),
        )
    s, now = ctx.store, ctx.now()
    with s.transaction():
        row = s._conn.execute(
            "SELECT state, card, run FROM monitor_windows WHERE id=?", (wid,)
        ).fetchone()
        if row is None:
            raise api.ApiError("not_found", f"no window {wid}")
        if (
            row["state"] != "open"
        ):  # a repeat, or the window expired meanwhile: its changes are checked as usual
            return {"state": row["state"]}
        state = "failed" if failed else "closed"
        s._conn.execute(
            "UPDATE monitor_windows SET state=?, changes=?, errors=?, failed=?, closed=? WHERE id=?",
            (
                state,
                json.dumps(changes, ensure_ascii=False, sort_keys=True),
                json.dumps(errors),
                failed,
                now,
                wid,
            ),
        )
        if failed:
            _settle(s, row["card"], row["run"])
    return {"state": state}


def _active(ctx, principal, request):
    _monitor(principal)
    s, now = ctx.store, ctx.now()
    with s.transaction():
        _expire(s, now)
        servers = [
            r[0]
            for r in s._conn.execute(
                "SELECT DISTINCT server FROM monitor_windows WHERE state='open' ORDER BY server"
            )
        ]
    return {"servers": servers}


def _take(ctx, principal, request):
    _monitor(principal)
    rows = ctx.store._conn.execute(
        "SELECT id, server, card, step, changes, errors, opened, closed FROM monitor_windows WHERE state='closed'"
        " ORDER BY closed, opened, rowid LIMIT ?",
        (TAKE_MAX,),
    ).fetchall()
    return {
        "windows": [
            dict(r, changes=json.loads(r["changes"]), errors=json.loads(r["errors"]))
            for r in rows
        ]
    }


def _report(ctx, principal, request):
    _monitor(principal)
    wid, ok = _id(request), request.get("ok")
    if not isinstance(ok, bool):
        raise _invalid("ok is true or false")
    accepted, dangerous = (
        _pairs(request.get("accepted", []), "accepted"),
        _pairs(request.get("dangerous", []), "dangerous"),
    )
    detail = _text(request.get("detail", ""), "detail", DETAIL_MAX, empty=True)
    s, now = ctx.store, ctx.now()
    with s.transaction():
        row = s._conn.execute(
            "SELECT state, server, card, run FROM monitor_windows WHERE id=?", (wid,)
        ).fetchone()
        if row is None:
            raise api.ApiError("not_found", f"no window {wid}")
        if row["state"] != "closed":  # reported already: a repeat after a lost answer
            return {"state": row["state"]}
        state = "applied" if ok else "rejected"
        result = {"accepted": accepted, "dangerous": dangerous, "detail": detail}
        s._conn.execute(
            "UPDATE monitor_windows SET state=?, result=?, applied=? WHERE id=?",
            (state, json.dumps(result, ensure_ascii=False), now, wid),
        )
        for check, subject in dangerous:
            s._conn.execute(
                'INSERT OR REPLACE INTO monitor_window_marks (server, "check", subject, card, at)'
                " VALUES (?,?,?,?,?)",
                (row["server"], check, subject, row["card"], now),
            )
        _settle(s, row["card"], row["run"])
    return {"state": state}


def link_marks(s, n, server, check, subject, now) -> None:
    """An incident opened on an object a plan step changed dangerously within 24 hours: linked to the step's card."""
    since = incidents._shift_min(now, MARK_MIN)
    for row in s._conn.execute(
        "SELECT m.card, m.at FROM monitor_window_marks m JOIN cards c ON c.id=m.card"
        ' WHERE m.server=? AND m."check"=? AND m.subject=? AND m.at>=? ORDER BY m.at',
        (server, check, subject, since),
    ).fetchall():
        if s._conn.execute(
            "INSERT OR IGNORE INTO incident_cards (incident, card, linked_by, linked_at)"
            " VALUES (?,?,'system:window',?)",
            (n, row["card"], now),
        ).rowcount:
            incidents._event(
                s, incidents.MONITOR_ACTOR, "window", n, card=row["card"], at=row["at"]
            )


def purge(s, now, days) -> None:
    s._conn.execute(
        "DELETE FROM monitor_window_marks WHERE at<?",
        (incidents._shift_min(now, MARK_MIN),),
    )
    s._conn.execute(
        "DELETE FROM monitor_windows WHERE state NOT IN ('open','closed') AND opened<?",
        (incidents._shift(now, days),),
    )


OPS = {
    "monitor.window.open": (WRITERS, _open),
    "monitor.window.close": (WRITERS, _close),
    "monitor.window.active": (MONITOR, _active),
    "monitor.window.take": (MONITOR, _take),
    "monitor.window.report": (MONITOR, _report),
}
api.OPS.update(OPS)  # the board API serves them (api imports this module at its end)
