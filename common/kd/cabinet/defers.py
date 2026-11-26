"""A card put off until a time: it keeps its column, stays out of the inbox and the idle reminders, and
wakes by itself when the time comes.

Who puts it off: the card's agent — its own card in work, as the outcome of its turn (``kd-work finish --outcome
wait_until --until <moment>``, up to 7 days; the outcome waits on a ``time`` wait, so the card is not «nobody's»); the
owner — any card not closed, up to 90 days (``user.card.defer``); BOSS — any card not closed, up to 7 days, with its
reason, and the owner hears of it (``boss.defer``). A moment names its time zone and is at least 10 minutes away. A
new defer replaces the active one, except that the agent's own never replaces an owner's or BOSS's: that one
stands, and the agent's time wait wakes it no earlier than that defer's time.

Until the time an owner's or BOSS's defer holds the agent's wakes of the card that came from closed waits — answers
and continuations (``store`` queues them with ``next_try`` at the time): they go out at the time, or at once with the
owner's "Wake now" (``user.card.undefer``). A person's message to the agent in the card's thread wakes it at
once and clears any defer; Done and Cancelled clear it too.

When the time comes (the scheduler's sweep, every 30 s, and the first one after a restart): an agent's own defer
wakes it through its ``time`` wait; an owner's or BOSS's asks one continuation of the card's agent, or tells the
owner when no agent holds the card in work. An agent's third defer in a row with no ``kd-work progress`` between
(``turn.progress``) raises ``continuation_limit`` for BOSS.
"""

import secrets
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from kd.cabinet import api, notices, store as store_mod, users

MIN = timedelta(minutes=10)
LONGEST = {
    "agent": timedelta(days=7),
    "boss": timedelta(days=7),
    "owner": timedelta(days=90),
}
# the wakes from closed waits an owner's or BOSS's defer holds until its time (``store`` holds new ones the same way)
HELD = ("answer", "continue")
STREAK = 3  # an agent's defers in a row without progress that ask BOSS to look
REASON_MAX = 500
DUE_BATCH = 200
STAMP = "%Y-%m-%dT%H:%M:%SZ"
UNTIL_HELP = "--until is a moment with its time zone: 2026-11-08T12:00Z or the same with its offset"


def parse_until(value, now: str, source: str) -> str:
    """The moment as the cabinet's UTC stamp: with its time zone, 10 minutes away at least, at most the source's
    longest (an agent and BOSS — 7 days, the owner — 90). The refusals name the flag for the agents' commands and
    the moment itself for the owner's page."""
    name = "the moment" if source == "owner" else "--until"
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        raise api.ApiError("invalid", UNTIL_HELP.replace("--until", name))
    text = value.strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise api.ApiError("invalid", UNTIL_HELP.replace("--until", name))
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise api.ApiError(
            "invalid",
            f"{name} {value.strip()} has no time zone: "
            + UNTIL_HELP.replace("--until", name),
        )
    at = moment.astimezone(timezone.utc).replace(microsecond=0)
    start, longest = users.parse_time(now), LONGEST[source]
    if at < start + MIN:
        raise api.ApiError(
            "refused",
            f"{name} is less than {MIN.seconds // 60} minutes from now ({now})",
        )
    if at > start + longest:
        raise api.ApiError(
            "refused", f"{name} is more than {longest.days} days from now ({now})"
        )
    return at.strftime(STAMP)


def reason_of(value, *, required: bool) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise api.ApiError(
                "invalid", "--reason is required: why the card waits until then"
            )
        return ""
    return api._text(value, "reason", limit=REASON_MAX).strip()


def active(store, card: str) -> Optional[dict]:
    row = store._conn.execute(
        "SELECT * FROM defers WHERE card=? AND state='active'", (card,)
    ).fetchone()
    return None if row is None else dict(row)


def active_map(store) -> Dict[str, dict]:
    """{card: its active defer} of every card put off now."""
    return {
        r["card"]: dict(r)
        for r in store._conn.execute(
            "SELECT * FROM defers WHERE state='active'"
        ).fetchall()
    }


def woken_map(store) -> Dict[str, str]:
    """{card: when its last defer ended by a wake — its time, the owner's button, a person's message}."""
    return {
        r[0]: r[1]
        for r in store._conn.execute(
            "SELECT card, MAX(ended) FROM defers WHERE state='woken' GROUP BY card"
        ).fetchall()
    }


def view(row: Optional[dict]) -> Optional[dict]:
    """A defer as the pages and the snapshot show it."""
    if row is None:
        return None
    return {
        "until": row["until"],
        "source": row["source"],
        "by": row["by"],
        "reason": row["reason"],
        "created": row["created"],
    }


def local(stamp: str) -> str:
    """«08.11 15:00» in the host's time zone (UTC when the host names none readable)."""
    from kd import clock

    try:
        zone = clock.zone()
    except Exception:  # noqa: BLE001 — a notice never fails on the host's file
        zone = timezone.utc
    return users.parse_time(stamp).astimezone(zone).strftime("%d.%m %H:%M")


def _end(s, row: dict, state: str, how: str) -> None:
    s._conn.execute(
        "UPDATE defers SET state=?, ended_how=?, ended=? WHERE id=?",
        (state, how, s._clock(), row["id"]),
    )
    thread = s.card(row["card"]).thread
    s._event(
        "system",
        "card.undeferred",
        row["card"],
        {"defer": row["id"], "how": how, "state": state},
        thread,
    )


def place(
    ctx,
    record,
    *,
    until: str,
    source: str,
    by: str,
    reason: str = "",
    command: str = "",
    streak: int = 1,
) -> dict:
    """The card put off until ``until`` (a UTC stamp ``parse_until`` made), in place of its active defer; an
    owner's or BOSS's holds the agent's queued wakes of the card until then, and the wakes the replaced defer held
    move to the new one's time — earlier or later. The agent's own defer is the weaker: in place of another agent
    defer only — under an owner's or BOSS's it replaces nothing and releases nothing, and that defer is returned
    as it stands (the agent's turn keeps its time wait, whose wake that defer holds like any other)."""
    s = ctx.store
    with s.transaction():
        old = active(s, record.id)
        if old is not None and source == "agent" and old["source"] != "agent":
            return old
        defer_id, now = secrets.token_hex(16), s._clock()
        if old is not None:
            _end(s, old, "replaced", "replaced")
            if old["source"] != "agent":
                s._conn.execute(
                    f"UPDATE commands SET next_try=?, updated=? WHERE card=? AND state='queued' AND next_try=? "
                    f"AND kind IN ({','.join('?' * len(HELD))})",
                    (until, now, record.id, old["until"], *HELD),
                )
        s._conn.execute(
            "INSERT INTO defers (id, card, until, source, by, reason, command, streak, created) VALUES (?,?,?,?,?,?,?,?,?)",
            (defer_id, record.id, until, source, by, reason, command, streak, now),
        )
        if source != "agent":
            s._conn.execute(
                f"UPDATE commands SET next_try=?, updated=? WHERE card=? AND state='queued' AND next_try<? "
                f"AND kind IN ({','.join('?' * len(HELD))})",
                (until, now, record.id, until, *HELD),
            )
        s._event(
            by,
            "card.deferred",
            record.id,
            {"defer": defer_id, "until": until, "source": source, "reason": reason},
            record.thread,
        )
        return dict(
            s._conn.execute("SELECT * FROM defers WHERE id=?", (defer_id,)).fetchone()
        )


def clear(ctx, card: str, *, how: str) -> Optional[dict]:
    """The card's active defer ends before its time: ``owner`` (his "Wake now": the held wakes go now, an
    agent's own defer wakes it), ``message`` (a person wrote to the agent: that message wakes it), ``closed`` (Done,
    Cancelled) or ``superseded`` (the agent's next outcome). The defer ended, or None."""
    from kd.cabinet import (
        waits,
    )  # at call time: waits imports the API this module is loaded with

    s = ctx.store
    with s.transaction():
        row = active(s, card)
        if row is None:
            return None
        _end(s, row, "woken" if how in ("owner", "message") else "cleared", how)
        now = s._clock()
        if row["source"] != "agent":
            s._conn.execute(
                "UPDATE commands SET next_try=?, updated=? WHERE card=? AND state='queued' AND next_try=?",
                (now, now, card, row["until"]),
            )
            return row
        for wait in [
            dict(w)
            for w in s._conn.execute(
                "SELECT * FROM waits WHERE card=? AND kind='time' AND state='open' AND command=?",
                (card, row["command"]),
            ).fetchall()
        ]:
            if how == "owner":
                waits._move(s, wait, "resolved")
                waits.settle(ctx, wait["command"])
            else:
                s._conn.execute(
                    "UPDATE waits SET state='cancelled', reason=?, updated=? WHERE id=?",
                    (how, now, wait["id"]),
                )
        return row


def fire_due(ctx) -> int:
    """The defers whose time came (the sweep): an agent's own wakes through its ``time`` wait (the sweep settles it);
    an owner's or BOSS's asks one continuation of the card's agent, or tells the owner when no agent holds it in
    work. How many fired."""
    from kd.cabinet import (
        continuations,
        outcomes,
    )  # at call time: they import this module's neighbours

    s = ctx.store
    fired = 0
    with s.transaction():
        rows = [
            dict(r)
            for r in s._conn.execute(
                "SELECT * FROM defers WHERE state='active' AND until<=? ORDER BY until, id LIMIT ?",
                (ctx.now(), DUE_BATCH),
            ).fetchall()
        ]
        for row in rows:
            _end(s, row, "woken", "time")
            fired += 1
            if row["source"] == "agent":
                continue
            try:
                record = s.card(row["card"])
            except store_mod.NotFound:
                continue
            agent = record.assignee[1:] if record.assignee.startswith("@") else ""
            if (
                agent
                and agent != "owner"
                and ctx.host_config().hosted(agent)
                and outcomes.owes_outcome(record, agent)
            ):
                continuations.launch(
                    ctx,
                    key=f"time:{row['id']}",
                    record=record,
                    agent=agent,
                    source="time",
                    text=continuations.TIME.format(id=record.id),
                )
            else:
                notices.post_in_tx(
                    s,
                    source="pipeline",
                    level="info",
                    key="notice.defer.due",
                    card=record.id,
                    params={"until": local(row["until"])},
                )
    return fired


def agent_streak(store, card: str, agent: str) -> int:
    """The agent's defers of the card in a row with this one: 1, or one more than its last when no
    ``kd-work progress`` (``turn.progress``) came since."""
    last = store._conn.execute(
        "SELECT d.streak, e.seq FROM defers d JOIN events e ON e.card=d.card AND e.kind='card.deferred'"
        " AND json_extract(e.payload, '$.defer')=d.id WHERE d.card=? AND d.source='agent' AND d.by=?"
        " ORDER BY e.seq DESC LIMIT 1",
        (card, f"agent:{agent}"),
    ).fetchone()
    if last is None:
        return 1
    progressed = store._conn.execute(  # by the journal's order: the same second is no proof of either
        "SELECT 1 FROM events WHERE card=? AND kind='turn.progress' AND actor=? AND seq>? LIMIT 1",
        (card, f"agent:{agent}", last["seq"]),
    ).fetchone()
    return 1 if progressed is not None else last["streak"] + 1


# --- the operations ---------------------------------------------------------------------------------------
def _open_card(ctx, principal, request):
    record = api._visible_card(ctx, principal, request.get("card"))
    if record.terminal:
        raise api.ApiError(
            "refused", f"{record.id} is {record.status}: a closed card is not put off"
        )
    return record


def _defer(ctx, principal, request):
    """``user.card.defer {card, until, reason?}`` — the owner (or root from ``kd-cabinet defer``) puts any card not
    closed off, up to 90 days."""
    record = _open_card(ctx, principal, request)
    api._require(ctx, principal, "cards.transition", record.project)
    until = parse_until(request.get("until"), ctx.now(), "owner")
    reason = reason_of(request.get("reason"), required=False)
    row = place(
        ctx,
        record,
        until=until,
        source="owner",
        by=api.card_actor(principal),
        reason=reason,
    )
    return {"defer": view(row)}


def _undefer(ctx, principal, request):
    """``user.card.undefer {card}`` — "Wake now": the defer ends, the held wakes go at once."""
    record = api._visible_card(ctx, principal, request.get("card"))
    api._require(ctx, principal, "cards.transition", record.project)
    row = clear(ctx, record.id, how="owner")
    if row is None:
        raise api.ApiError("refused", f"{record.id} is not put off")
    return {"defer": None, "ended": view(row)}


def _boss_defer(ctx, principal, request):
    """``boss.defer {agent, task, until, reason}`` — BOSS puts a card not closed off, up to 7 days, with its reason;
    the owner hears of it in the card's thread."""
    api._executor_agent(ctx, principal, request, coordinator=True)
    value = request.get("task")
    if not isinstance(value, str) or not store_mod.CARD_ID.fullmatch(value):
        raise api.ApiError("invalid", "a card is KD-<n>")
    try:
        record = ctx.store.card(value)
    except store_mod.NotFound:
        raise api.ApiError("not_found", f"{value}: no such card")
    if record.terminal:
        raise api.ApiError(
            "refused", f"{record.id} is {record.status}: a closed card is not put off"
        )
    reason = reason_of(request.get("reason"), required=True)
    until = parse_until(request.get("until"), ctx.now(), "boss")
    with ctx.store.transaction():
        row = place(
            ctx, record, until=until, source="boss", by="agent:boss", reason=reason
        )
        notices.post_in_tx(
            ctx.store,
            source="pipeline",
            level="info",
            key="notice.defer.boss",
            card=record.id,
            params={"until": local(until), "reason": reason},
        )
    return {"defer": view(row)}


def _progress(ctx, principal, request):
    """``turn.progress {agent, card}`` — ``kd-work progress`` recorded the agent's verified action on its card:
    the count of its defers in a row starts again."""
    agent = api._executor_agent(
        ctx, principal, request, coordinator=request.get("agent") == "boss"
    )
    record = api._card(ctx, request.get("card"))
    if record.assignee != "@" + agent:
        raise api.ApiError("refused", f"{record.id} is not yours")
    with ctx.store.transaction():
        ctx.store._event(
            f"agent:{agent}", "turn.progress", record.id, {}, record.thread
        )
    return {"ok": True}


OPS = {
    # root — ``kd-cabinet defer`` for the maintainer's own cards (a step that waits a week), signed ``root``
    "user.card.defer": (frozenset({"user", "root"}), _defer),
    "user.card.undefer": (frozenset({"user", "root"}), _undefer),
    "boss.defer": (frozenset({"executor"}), _boss_defer),
    "turn.progress": (frozenset({"executor"}), _progress),
}
api.OPS.update(OPS)  # the board API serves them (api imports this module at its end)
