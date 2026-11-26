"""An agent's state from facts (remediation spec §9.2, R23).

Not the age of its last progress line: what its sessions, commands and questions say, on the threads the person
may read — the cards they see and, of the agent's own threads, its chat with ``agents.chat`` and its «Monitor» with
``servers.read`` too. Every number is counted from those only (rights before counts, AC22): a session, a command or a
question on a card out of the person's sight is not in any of them. The threads are narrowed in the query itself,
so rows the person may not see never crowd out those they may.

- working — a session starting or at work whose heartbeat is fresh (``FRESH``); one whose heartbeat is older
  is ``stale``: nothing confirms it works, and with nothing else known the agent's state is ``unknown``;
- waiting_you — the agent's open questions, with the cards they are on;
- queued — threads where nothing runs and a request has not started yet (not handed over, or handed over and
  not begun), or a session waits for a free slot;
- failed — threads whose newest request failed and nothing runs there since: on the agent's chat, or on a card
  still the agent's and not Done (a failure on a card done since, or handed to another, is not its error);
- the card counts — every status but Done, the analyst's briefs in Inbox included;
- heartbeat, the last action (its newest message that is not progress or a tool call) and the last progress
  line, each apart.

The one badge (``state``) is a summary: ``error`` (or ``unavailable`` when the session host refused or was not
there) only while nothing works or waits for a slot; then working, waiting for you, queued, unknown, free. An
agent not on the session host is ``unknown``: the cabinet does not watch it.
"""

import json
from typing import Dict, Iterable, List, Mapping, Optional

from kd.cabinet import schema, users

FRESH = 120  # seconds: a newer heartbeat is confirmed work
HOST_DOWN = ("host_unavailable", "host_refused")
QUIET = ("progress", "tool_call", "tool_result")
ACTIONS = tuple(k for k in schema.MESSAGE_KINDS if k not in QUIET)
SCOPE = "(SELECT value FROM json_each(?))"
SHOWN = 5  # questions and failures listed
COLUMNS = {"Inbox": "inbox", "To Do": "to_do", "In Progress": "in_progress", "Review": "review", "Blocked": "blocked"}
STATES = ("working", "waiting_you", "queued", "free", "error", "unavailable", "unknown")


def _age(now: str, then: str) -> float:
    return (users.parse_time(now) - users.parse_time(then)).total_seconds()


def facts(store, name: str, *, cards: List[Mapping], spaces: Iterable[str], hosted: bool, now: str) -> Dict:
    """The facts of agent ``name`` as the person reading ``cards`` and the agent's own threads of ``spaces`` (its
    chat, its «Monitor»: ``api.agent_spaces``) may see them."""
    readable = {c["id"]: c for c in cards}
    conn = store._conn
    threads = [r[0] for r in conn.execute(f"SELECT id FROM threads WHERE card IN {SCOPE}", (json.dumps(list(readable)),))]
    kinds = [schema.AGENT_SPACES[space] for space in spaces]
    if kinds:
        threads += [r[0] for r in conn.execute(  # its chat and "Monitor", those the person reads
            f"SELECT id FROM threads WHERE kind IN {SCOPE} AND agent=?", (json.dumps(kinds), name)
        )]
    scope = json.dumps(threads)

    sessions = conn.execute(
        f"SELECT thread, state, heartbeat, updated FROM sessions WHERE agent=? AND thread IN {SCOPE}", (name, scope)
    ).fetchall()
    # each thread's newest request (not a stop), newest first
    newest = conn.execute(
        "SELECT thread, state, reason, error, updated, card FROM ("
        "SELECT c.thread, c.state, c.reason, c.error, c.updated, c.created, c.rowid AS rid, t.card AS card, "
        "ROW_NUMBER() OVER (PARTITION BY c.thread ORDER BY c.created DESC, c.rowid DESC) AS n "
        "FROM commands c JOIN threads t ON t.id = c.thread "
        f"WHERE c.agent=? AND c.kind != 'stop' AND c.thread IN {SCOPE}"
        ") WHERE n = 1 ORDER BY created DESC, rid DESC",
        (name, scope),
    ).fetchall()
    waiting = conn.execute(
        f"SELECT DISTINCT thread FROM commands WHERE agent=? AND state IN ('queued', 'delivered') AND thread IN {SCOPE}",
        (name, scope),
    ).fetchall()

    live = [s for s in sessions if s["state"] in ("starting", "working")]
    working = {s["thread"] for s in live if _age(now, s["heartbeat"] or s["updated"]) <= FRESH}
    stale = {s["thread"] for s in live} - working
    queued = {s["thread"] for s in sessions if s["state"] == "queued"} | {r["thread"] for r in waiting}
    queued -= {s["thread"] for s in live}  # a thread whose session runs is at work, not waiting
    failures = [
        {"card": c["card"], "title": _title(readable, c["card"]), "reason": c["reason"] or c["error"], "at": c["updated"]}
        for c in newest
        if c["state"] == "failed" and c["thread"] not in working and _theirs(readable, c["card"], name)
    ]
    host_down = bool(newest) and newest[0]["state"] == "failed" and newest[0]["reason"] in HOST_DOWN

    questions = [
        {"decision": r["id"], "card": r["card"], "title": _title(readable, r["card"])}
        for r in conn.execute(
            "SELECT id, card FROM decisions WHERE state='open' AND type='question' AND requested_by=? ORDER BY created",
            (f"agent:{name}",),
        ).fetchall()
        if r["card"] in readable
    ]
    counts = {column: 0 for column in COLUMNS.values()}
    for card in cards:
        if card["assignee"] == f"@{name}" and card["status"] in COLUMNS:
            counts[COLUMNS[card["status"]]] += 1

    heartbeat = max((s["heartbeat"] for s in sessions if s["heartbeat"]), default="")
    if not hosted:
        state = "unknown"
    elif failures and not working and not queued:
        state = "unavailable" if host_down else "error"
    elif working:
        state = "working"
    elif questions:
        state = "waiting_you"
    elif queued:
        state = "queued"
    elif stale:
        state = "unknown"
    else:
        state = "free"
    return {
        "state": state,
        "working": len(working),
        "waiting_you": len(questions),
        "queued": len(queued),
        "failed": len(failures),
        "stale": len(stale),
        "questions": questions[:SHOWN],
        "failures": failures[:SHOWN],
        "cards": counts,
        "heartbeat": heartbeat,
        "last_action": _last(store, name, scope, ACTIONS),
        "progress": (_last(store, name, scope, ("progress",)) or {}).get("at", ""),
    }


def _title(readable: Mapping, card: str) -> str:
    return readable[card]["title"] if card in readable else ""


def _theirs(readable: Mapping, card: Optional[str], name: str) -> bool:
    """A failure is the agent's while its thread is its chat or a card still assigned to it and not Done."""
    if not card:
        return True
    found = readable.get(card)
    return found is not None and found["status"] not in schema.TERMINAL and found["assignee"] == f"@{name}"


def _last(store, name: str, scope: str, kinds) -> Optional[dict]:
    """The agent's newest message of ``kinds`` on the threads in ``scope``."""
    marks = ",".join("?" * len(kinds))
    row = store._conn.execute(
        "SELECT m.created, m.body, t.card AS card FROM messages m JOIN threads t ON t.id = m.thread "
        f"WHERE m.author=? AND m.kind IN ({marks}) AND m.thread IN {SCOPE} ORDER BY m.id DESC LIMIT 1",
        (f"agent:{name}", *kinds, scope),
    ).fetchone()
    return {"at": row["created"], "card": row["card"] or "", "text": row["body"][:140]} if row else None
