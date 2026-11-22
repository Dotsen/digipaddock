"""Automatic continuations: the cabinet, not the agent, starts the next turn.

A turn that recorded ``continue``, or a wait that resolved, asks for one more turn of the same agent on the same
card. Each request is a row keyed by what asked for it (``cont:<command>``, ``wait:<command>``), so a repeat is the
same row: ``queued`` with its command, ``merged`` into a command already waiting (nothing new), ``observed`` in the
``observe`` mode (nothing queued), ``stopped`` (the owner's stop waits: it wins, nothing is queued after it) or
``limited``. Two limits hold, both read from these rows and so kept across a restart. At most ``per_window`` queued
in ``window_s``: one more waits in the queue for the window's end (reason ``window``), it is not dropped. And no
more than ``streak`` turn continuations (``cont:``) in a row while the card's verified mark stays the same, counted
since the agent's last command in the thread from someone else (the owner, BOSS) — a resolved wait is not the agent
going round, new input starts a new streak. The mark is what an agent cannot produce by talking: the
status, the PRs and their heads, done jobs, decided decisions, diagnostics, brief versions and finished dependencies
and subtasks — notes, ticks, comments and progress lines are not in it. A streak over the limit raises a
``continuation_limit`` problem, once per streak.

A queued continuation carries its basis — the card as it was — and the dispatcher checks it again before delivery.
"""

from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Optional

from kd.cabinet import briefs, problems, result

CONTINUE = "[board] {id}: continue your work — your last turn recorded a next step; read kd-task view {id}"
RESOLVED = "[board] {id}: what you waited for is resolved — read kd-task view {id} and continue"
ENDED = (
    "[board] {id}: what you waited for ended without the answer you needed ({state}) — read kd-task view {id} and "
    "decide the next step"
)
# the card was put off until a time, and the time has come
TIME = "[board] {id}: the time the card was put off until has come — read kd-task view {id} and continue"
STAMP = "%Y-%m-%dT%H:%M:%SZ"


def mark(store, record) -> str:
    """The card's verified progress as 16 hex characters."""
    conn, card = store._conn, record.id
    count = lambda sql, *args: conn.execute(sql, args).fetchone()[0]  # noqa: E731
    data = {
        "status": record.status,
        "prs": [list(r) for r in conn.execute(
            "SELECT repo, number, head, state FROM prs WHERE card=? ORDER BY repo, number", (card,)).fetchall()],
        "jobs": count("SELECT COUNT(*) FROM jobs WHERE card=? AND state='done'", card),
        "decisions": count("SELECT COUNT(*) FROM decisions WHERE card=? AND state!='open'", card),
        "diagnostics": count("SELECT COUNT(*) FROM messages WHERE thread=? AND kind='diagnostic'", record.thread),
        "briefs": count("SELECT COUNT(*) FROM brief_versions WHERE card=?", card),
        "done": count(
            "SELECT COUNT(*) FROM cards WHERE status='Done' AND (parent=? OR id IN "
            "(SELECT depends_on FROM card_deps WHERE card=?))", card, card),
    }
    text = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def basis_of(store, record) -> dict:
    """The card as a continuation found it: who held it, where, its scope, its review round and its grant."""
    row = store._conn.execute(
        "SELECT json_extract(payload, '$.grant') FROM events WHERE card=? AND json_extract(payload, '$.grant')"
        " IS NOT NULL ORDER BY seq DESC LIMIT 1",
        (record.id,),
    ).fetchone()
    return {
        "assignee": record.assignee,
        "status": record.status,
        "scope": briefs.scope_sha(briefs.scope(record))[:16],
        "iteration": len(result.reviews(store, record.id)),
        "grant": (row[0] if row is not None else "") or "",
    }


def _since(ctx, seconds: int) -> str:
    now = datetime.strptime(ctx.now(), STAMP).replace(tzinfo=timezone.utc)
    return (now - timedelta(seconds=seconds)).strftime(STAMP)


def _window(ctx, record) -> list:
    """When the queued continuations counted in the card's window were created, oldest first."""
    return [r[0] for r in ctx.store._conn.execute(
        "SELECT created FROM continuations WHERE card=? AND state='queued' AND created>=? ORDER BY created, rowid",
        (record.id, _since(ctx, ctx.pipeline().window_s)),
    ).fetchall()]


def _window_end(ctx, record) -> str:
    """When one more continuation fits in the window: the counted one it would push out leaves it."""
    config, created = ctx.pipeline(), _window(ctx, record)
    oldest = datetime.strptime(created[len(created) - config.per_window], STAMP).replace(tzinfo=timezone.utc)
    return (oldest + timedelta(seconds=config.window_s)).strftime(STAMP)


def _outside(store, record, agent: str) -> Optional[tuple]:
    """(created, id) of the agent's newest command in the card's thread from someone named — not the cabinet, not
    the agent itself — or None: the input the streak counts from."""
    row = store._conn.execute(
        "SELECT created, id FROM commands WHERE thread=? AND agent=? AND initiator NOT IN ('', 'system', ?)"
        " ORDER BY created DESC, rowid DESC LIMIT 1",
        (record.thread, agent, "agent:" + agent),
    ).fetchone()
    return None if row is None else (row[0], row[1])


def limit_reason(ctx, record, agent: str = "") -> Optional[str]:
    """``streak``, ``window`` or None: whether one more queued continuation of the card is over a limit (the streak
    first: it stops, the window only delays)."""
    config, conn = ctx.pipeline(), ctx.store._conn
    outside = _outside(ctx.store, record, agent or record.assignee.lstrip("@"))
    last = [r[0] for r in conn.execute(
        "SELECT mark FROM continuations WHERE card=? AND source='turn' AND state='queued' AND created>?"
        " ORDER BY created DESC, rowid DESC LIMIT ?",
        (record.id, outside[0] if outside else "", config.streak),
    ).fetchall()]
    if len(last) >= config.streak and set(last) == {mark(ctx.store, record)}:
        return "streak"
    if len(_window(ctx, record)) >= config.per_window:
        return "window"
    return None


def _live(store, record, agent: str) -> str:
    """A command of the agent in the card's thread not yet run (queued or delivered), or ''; a stop is not one —
    nothing merges into it."""
    row = store._conn.execute(
        "SELECT id FROM commands WHERE thread=? AND agent=? AND state IN ('queued','delivered') AND kind!='stop'"
        " ORDER BY created LIMIT 1",
        (record.thread, agent),
    ).fetchone()
    return row[0] if row is not None else ""


def _stopping(store, record, agent: str) -> bool:
    """The owner's stop is the agent's latest command in the card's thread — queued, or done while the turn that
    asks to continue ran: the owner's "Continue" is the next step."""
    rows = store.commands(thread=record.thread, agent=agent, limit=1)
    return bool(rows) and rows[0]["kind"] == "stop"


def _get(store, key: str) -> Optional[dict]:
    row = store._conn.execute("SELECT * FROM continuations WHERE key=?", (key,)).fetchone()
    return None if row is None else dict(row)


def launch(ctx, *, key: str, record, agent: str, source: str, text: str, merged_into: str = "") -> dict:
    """One continuation of ``agent`` on the card, once per ``key`` (a repeat returns the row already written)."""
    s = ctx.store
    with s.transaction():
        known = _get(s, key)
        if known is not None:
            return known
        current, command, reason = mark(s, record), merged_into, ""
        if merged_into:
            state = "merged"
        elif not ctx.pipeline().enforcing:
            state = "observed"
        elif _stopping(s, record, agent):
            state = "stopped"
        else:
            reason = limit_reason(ctx, record, agent) or ""
            live = _live(s, record, agent)
            if reason == "streak":
                state = "limited"
                outside = _outside(s, record, agent)
                problems.raise_problem(ctx, key=f"limit:{record.id}:{current}:{outside[1][:8] if outside else ''}",
                                       record=record, kind="continuation_limit", agent=agent,
                                       detail={"reason": reason, "key": key})
            elif live:  # the agent is woken on this card already: that turn is the next one (B01)
                command, state, reason = live, "merged", ""
            else:
                queued = s._enqueue_in_tx({"key": key, "agent": agent, "thread": record.thread,
                                           "card": record.id, "kind": "continue", "text": text,
                                           "initiator": "system", "basis": basis_of(s, record),
                                           "next_try": _window_end(ctx, record) if reason == "window" else ""})
                command = queued["id"]
                state = "queued" if queued["key"] == key else "merged"
        s._conn.execute(
            "INSERT INTO continuations (key, card, agent, source, command, state, mark, reason, created)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (key, record.id, agent, source, command, state, current, reason, s._clock()),
        )
        return _get(s, key)


def rows(store, card: str) -> list:
    """The card's continuations, oldest first (the status and the snapshot read them)."""
    return [dict(r) for r in store._conn.execute(
        "SELECT * FROM continuations WHERE card=? ORDER BY created, rowid", (card,)).fetchall()]

