"""Pipeline problems: a card's work that stopped where nobody holds it.

A problem is a row, raised once by its key (the turn, the limit or the wait it is about) with a ``problem.raised``
event. In ``observe`` that is all: the row and the event show what the pipeline would have asked. In ``enforce``
the cabinet asks in the same write: BOSS by a signal in the card's thread (BOSS reads the card and routes it), or —
when the card is BOSS's own or BOSS is not on the session host — the owner, by a notice of source ``pipeline`` on
the card. A later fact that settles it (a turn that recorded its next step, a new verified result) resolves it.
"""

import json
from typing import Iterable, List, Mapping, Optional

from kd.cabinet import commands, notices, schema

BOSS = "boss"
PROBLEM_WAKE = {
    "missing_next_step": "[pipeline] {id}: a turn of @{agent} ended without a next step — read kd-task view {id} and route it",
    "continuation_limit": "[pipeline] {id}: automatic continuations of @{agent} stopped at the limit — read kd-task view {id}",
    "wait_needs_routing": (
        "[pipeline] {id}: what @{agent} waits for is gone or no real result — read kd-task view {id} and route it"
    ),
    "outcome_refused": "[pipeline] {id}: the outcome @{agent} recorded could not apply — read kd-task view {id}",
    "assist_failed": "[pipeline] {id}: help @{agent} asked for ended without a result — read kd-task view {id}",
    "assist_stalled": (
        "[pipeline] {id}: help @{agent} asked for has no answer — read kd-task view {id} and kd-boss-board assist-list"
    ),
    # the stages of an approved brief and the health of a release
    "stage_missing": "[pipeline] {id}: an approved stage has no child card — read kd-task view {id} and tell the owner",
    "stage_cancelled": (
        "[pipeline] {id}: a stage's card was closed without the work — read kd-task view {id}: the plan has a hole"
    ),
    "stage_reopened": (
        "[pipeline] {id}: a closed stage of the chain was reopened — read kd-task view {id}: pause the stages that"
        " build on it"
    ),
    "release_degraded": (
        "[pipeline] {id}: the release of this card is degraded — read kd-task view {id}; its lead @{agent} holds it"
    ),
    # the council of models
    "council_failed": (
        "[pipeline] {id}: no critic of the council could start — read kd-task view {id}; the work goes on without it"
    ),
    "council_run_failed": "[pipeline] {id}: a critic of the council failed — read kd-task view {id}",
    "council_stalled": (
        "[pipeline] {id}: @{agent} has not answered the council's findings — read kd-task view {id}"
    ),
    "council_unanswered": (
        "[pipeline] {id}: @{agent} handed the work in with council findings unanswered — read kd-task view {id}"
    ),
}


def _row(row) -> dict:
    data = dict(row)
    data["detail"] = json.loads(data["detail"] or "{}")
    return data


def raise_problem(ctx, *, key: str, record, kind: str, agent: str = "", command: str = "",
                  detail: Optional[Mapping] = None) -> Optional[dict]:
    """The problem once: the row, its event and, in ``enforce``, the question to BOSS or the owner; None if the
    key was raised already."""
    if kind not in schema.PROBLEM_KINDS:
        raise ValueError(f"unknown problem kind {kind!r}")
    s = ctx.store
    with s.transaction():
        if s._conn.execute("SELECT 1 FROM problems WHERE key=?", (key,)).fetchone() is not None:
            return None
        now = s._clock()
        problem_id = s._conn.execute(
            "INSERT INTO problems (key, card, kind, agent, command, detail, created, updated) VALUES (?,?,?,?,?,?,?,?)",
            (key, record.id, kind, agent, command, json.dumps(dict(detail or {}), ensure_ascii=False, sort_keys=True),
             now, now),
        ).lastrowid
        s._event("system", "problem.raised", record.id,
                 {"problem": problem_id, "kind": kind, "agent": agent, "command": command}, record.thread)
        if ctx.pipeline().enforcing:
            _ask(ctx, problem_id, record, kind, agent)
        return _row(s._conn.execute("SELECT * FROM problems WHERE id=?", (problem_id,)).fetchone())


def _ask(ctx, problem_id: int, record, kind: str, agent: str) -> None:
    if ctx.host_config().hosted(BOSS) and record.assignee != "@" + BOSS:
        commands.enqueue_signal(ctx.store, agent=BOSS, card=record.id, initiator="system", key=f"problem:{problem_id}",
                                text=PROBLEM_WAKE[kind].format(id=record.id, agent=agent or "?"))
        return
    notices.post_in_tx(ctx.store, source="pipeline", level="warn", key=f"notice.pipeline.{kind}",
                       params={"agent": "@" + (agent or "?")}, card=record.id)


def resolve(store, card: str, kinds: Iterable[str], by: str, *, agents: Optional[Iterable[str]] = None,
            prefix: str = "") -> int:
    """The card's open problems of these kinds (of these agents, when named; with a key of this ``prefix``, when
    given) settled by ``by`` (a turn, a result, the card's move); how many."""
    kinds = tuple(kinds)
    if not kinds:
        return 0
    with store.transaction():
        rows = [r for r in store._conn.execute(
            f"SELECT id, kind, agent, key FROM problems WHERE card=? AND state='open' AND kind IN "
            f"({','.join('?' * len(kinds))}) ORDER BY id",
            (card, *kinds),
        ).fetchall() if (agents is None or r["agent"] in set(agents)) and r["key"].startswith(prefix)]
        if not rows:
            return 0
        now = store._clock()
        thread = store.card(card).thread
        for row in rows:
            store._conn.execute("UPDATE problems SET state='resolved', resolved_by=?, updated=? WHERE id=?",
                                (by, now, row["id"]))
            store._event("system", "problem.resolved", card, {"problem": row["id"], "kind": row["kind"], "by": by},
                         thread)
        return len(rows)


def open_for(store, card: str) -> List[dict]:
    rows = store._conn.execute("SELECT * FROM problems WHERE card=? AND state='open' ORDER BY id", (card,)).fetchall()
    return [_row(r) for r in rows]


def open_rows(store, cards: Iterable[str]) -> List[dict]:
    """The open problems of these cards (the overview's), oldest first."""
    cards = list(cards)
    if not cards:
        return []
    rows = store._conn.execute(
        "SELECT * FROM problems WHERE state='open' AND card IN (SELECT value FROM json_each(?)) ORDER BY id",
        (json.dumps(cards),),
    ).fetchall()
    return [_row(r) for r in rows]
