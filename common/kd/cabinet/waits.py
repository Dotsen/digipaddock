"""What a turn waits for: a decision, a dependency card or a factory
operation, by its id — an operation superseded by a newer one is waited for through it; a check contour in
maintenance, by its name, until root releases it; a deploy — a finished run of a project's environment that holds
a commit (``deploys``), resolved when the forge reader reports it.

``kd-work finish`` records the waits of its turn as ``pending`` beside the outcome; a repeated finish in the same
turn replaces them. They count once the turn ends and the outcome applies (``activate``): each is checked against
its source right then, so an answer that came during the turn is not lost. From then on the cabinet's own events
resolve them in the event's transaction (``hook``): a decision decided, expired or revoked, a dependency Done.

A set — the blocking waits of one outcome — is complete when all of them resolved (one, with ``--any``), ended
when one was rejected or expired, and needs routing when what it waits for is gone (or a dependency closed as a
duplicate). A complete or ended set asks for one more turn of its agent (``wait:<command>``); one that needs
routing raises a ``wait_needs_routing`` problem. In ``enforce`` the old wake of an answer the set still waits on
past is held back — the agent wakes once, when the set is complete, and that wake carries its continuation.
"""

import re
import secrets
from datetime import timedelta
from typing import Callable, List, Mapping, Optional

from kd.cabinet import api, card_edits, commands, continuations, problems, schema, store as store_mod

ENDED_STATES = ("rejected", "expired")
_DECISION_STATE = {"approved": "resolved", "rejected": "rejected", "expired": "expired", "revoked": "expired"}
_OPERATION_STATE = {"succeeded": "resolved", "failed": "rejected"}
# help inside an approved task: a cancelled request (the card moved on) has no result — it needs routing
_ASSIST_STATE = {"done": "resolved", "failed": "rejected", "cancelled": "cancelled"}
# a help withdrawn because the card moved on needs nobody; only a changed scope asks BOSS to route the lead's work
_ASSIST_ROUTING = ("scope_changed",)
# a council's round: over once the critics are done — whatever they found, the writer goes on; a
# council no critic of which could read has no result
_COUNCIL_STATE = {"running": "open", "failed": "rejected"}
DUPLICATE = "duplicate-of:"
# a release asks for its reinstall after the cabinet's update when the old code could not take it; a turn
# waits on its key meanwhile, at most this long before the wait needs routing
RELEASE_KEY = re.compile(r"release:[0-9a-f]{7,40}(?:\+[0-9a-f]{7,40})?")  # <rev>+<deploy> of a pair
KEY_GRACE = timedelta(minutes=20)
_EVENT = {"resolved": "wait.resolved", "rejected": "wait.rejected", "expired": "wait.expired",
          "needs_routing": "wait.routing"}


def _rows(store, command: str) -> List[dict]:
    return [dict(r) for r in store._conn.execute(
        "SELECT * FROM waits WHERE command=? ORDER BY kind, ref", (command,)).fetchall()]


def of_command(store, command: str) -> List[dict]:
    return _rows(store, command)


def record_pending(store, outcome: Mapping, specs) -> List[dict]:
    """The outcome's waits as ``pending``, in place of the ones its turn recorded before."""
    with store.transaction():
        store._conn.execute("DELETE FROM waits WHERE command=? AND state='pending'", (outcome["command"],))
        now = store._clock()
        for spec in specs:
            if spec["kind"] not in schema.WAIT_KINDS:
                raise ValueError(f"unknown wait kind {spec['kind']!r}")
            store._conn.execute(
                "INSERT INTO waits (id, command, card, agent, kind, ref, ref_version, blocking, mode, created, updated)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (secrets.token_hex(16), outcome["command"], outcome["card"], outcome["agent"], spec["kind"],
                 spec["ref"], spec.get("ref_version", ""), int(bool(spec["blocking"])), outcome["mode"], now, now),
            )
        return _rows(store, outcome["command"])


def source_state(ctx, wait: Mapping) -> str:
    """What the wait's source says now: ``open``, ``resolved``, ``rejected``, ``expired`` or ``needs_routing``."""
    conn = ctx.store._conn
    if wait["kind"] == "decision":
        row = conn.execute("SELECT state FROM decisions WHERE id=?", (wait["ref"],)).fetchone()
        if row is None:
            return "needs_routing"
        return _DECISION_STATE.get(row[0], "open")
    if wait["kind"] == "operation":
        from kd.cabinet import operations, users  # at call time: they load the API this module registers on

        ref = wait["ref"]
        if not operations.OPERATION_ID.fullmatch(ref):
            found = operations.request_row(ctx.store, ref)
            if found is None:  # the release has not asked yet
                late = users.parse_time(ctx.now()) - users.parse_time(wait["created"]) > KEY_GRACE
                return "needs_routing" if late else "open"
            if found["card"] != wait["card"]:  # a key names a release, not a card: another card's request
                return "needs_routing"
            ref = found["operation"]
        op = operations.latest(ctx.store, ref)
        if op is None or op["state"] == "superseded":  # gone, or superseded by nothing that still exists
            return "needs_routing"
        return _OPERATION_STATE.get(op["state"], "open")
    if wait["kind"] == "assist":
        row = conn.execute("SELECT state, reason FROM assists WHERE id=?", (wait["ref"],)).fetchone()
        if row is None or (row[0] == "cancelled" and row[1] in _ASSIST_ROUTING):
            return "needs_routing"
        return _ASSIST_STATE.get(row[0], "open")
    if wait["kind"] == "contour":
        from kd.cabinet import devchecks  # at call time: it loads the API this module registers on
        try:
            known = ctx.contours()
        except api.ApiError:  # an unreadable tree says nothing: the next event or sweep looks again
            return "open"
        if wait["ref"] not in known:  # a release took it out
            return "needs_routing"
        return "open" if devchecks.contour_row(ctx.store, wait["ref"])["state"] == "maintenance" else "resolved"
    if wait["kind"] == "council":  # a round of the card's council
        row = conn.execute("SELECT state, end_reason FROM councils WHERE id=?", (wait["ref"],)).fetchone()
        if row is None:
            return "needs_routing"
        if row[0] == "cancelled":  # the card closed — nobody waits; taken off or approved early — go on
            return "cancelled" if row[1] == "card_closed" else "resolved"
        return _COUNCIL_STATE.get(row[0], "resolved")
    if wait["kind"] == "time":  # the moment the agent put its card off until (UTC stamps compare as text)
        return "resolved" if ctx.now() >= wait["ref"] else "open"
    if wait["kind"] == "deploy":  # a run of the environment that holds the commit
        from kd.cabinet import deploys  # at call time: it loads the API this module registers on

        return deploys.wait_state(ctx.store, wait["ref"])
    try:
        card = ctx.store.card(wait["ref"])
    except store_mod.NotFound:
        return "needs_routing"
    if card.status == "Cancelled":  # closed without the work: it never gets done — reroute it
        return "needs_routing"
    if card.status != "Done":
        return "open"
    return "needs_routing" if any(label.startswith(DUPLICATE) for label in card.labels) else "resolved"


def _move(s, wait: Mapping, state: str) -> None:
    s._conn.execute("UPDATE waits SET state=?, updated=? WHERE id=?", (state, s._clock(), wait["id"]))
    if wait["kind"] == "deploy" and state == "rejected":  # a failed run: why, in the wake, the thread and the inbox
        from kd.cabinet import deploys  # at call time: it loads the API this module registers on

        s._conn.execute("UPDATE waits SET reason=? WHERE id=?", (deploys.failed(s, wait), wait["id"]))
    if state in _EVENT:
        thread = s.card(wait["card"]).thread
        s._event("system", _EVENT[state], wait["card"],
                 {"command": wait["command"], "kind": wait["kind"], "ref": wait["ref"]}, thread)


def activate(ctx, command: str) -> None:
    """The outcome applied: its waits count from now, each as its source says right now."""
    s = ctx.store
    with s.transaction():
        for wait in [w for w in _rows(s, command) if w["state"] == "pending"]:
            _move(s, wait, source_state(ctx, wait))


def set_state(store, command: str) -> Optional[str]:
    """The outcome's blocking waits as a set: ``resolved``, ``ended``, ``routing`` or None (still waiting)."""
    rows = [w for w in _rows(store, command) if w["blocking"] and w["state"] not in ("pending", "cancelled")]
    if not rows:
        return None
    states = [w["state"] for w in rows]
    if rows[0]["mode"] == "any" and "resolved" in states:
        return "resolved"
    if any(state in ENDED_STATES for state in states):
        return "ended"
    if "needs_routing" in states:
        return "routing"
    return "resolved" if all(state == "resolved" for state in states) else None


def _ended_state(store, command: str) -> str:
    """How the set ended, as the wake says it: the first ended wait's reason (a failed deploy's run), or its state."""
    ended = [w for w in _rows(store, command) if w["blocking"] and w["state"] in ENDED_STATES]
    return (ended[0]["reason"] or ended[0]["state"]) if ended else "ended"


def settle(ctx, command: str, *, merged_into: str = "") -> Optional[str]:
    """What a complete set asks for: one more turn of its agent, or a problem for routing; the set's state."""
    s = ctx.store
    state = set_state(s, command)
    if state is None:
        return None
    outcome = s._conn.execute("SELECT card, agent, state FROM turn_outcomes WHERE command=?", (command,)).fetchone()
    if outcome is None or outcome["state"] != "final":
        return None
    try:
        record = s.card(outcome["card"])
    except store_mod.NotFound:
        return None
    if state == "routing":
        problems.raise_problem(ctx, key=f"routing:{command}", record=record, kind="wait_needs_routing",
                               agent=outcome["agent"], command=command)
        return state
    timed = all(w["kind"] == "time" for w in _rows(s, command) if w["blocking"] and w["state"] != "cancelled")
    text = (continuations.TIME.format(id=record.id) if state == "resolved" and timed
            else continuations.RESOLVED.format(id=record.id) if state == "resolved"
            else continuations.ENDED.format(id=record.id, state=_ended_state(s, command)))
    continuations.launch(ctx, key=f"wait:{command}", record=record, agent=outcome["agent"], source="wait",
                         text=text, merged_into=merged_into)
    return state


def covering(store, agent: str, card: str, decision_id: str) -> Optional[str]:
    """The outcome whose open blocking wait is on this decision of the agent's card."""
    row = store._conn.execute(
        "SELECT w.command FROM waits w JOIN turn_outcomes o ON o.command = w.command WHERE w.kind='decision'"
        " AND w.ref=? AND w.agent=? AND w.card=? AND w.blocking=1 AND w.state='open' AND o.state='final'"
        " ORDER BY w.created DESC LIMIT 1",
        (decision_id, agent, card),
    ).fetchone()
    return None if row is None else row[0]


def _touched(ctx, event: Mapping) -> List[dict]:
    """The open waits this event's source may have decided."""
    conn, payload = ctx.store._conn, event.get("payload") or {}
    decision_id = payload.get("decision")
    if str(event.get("kind", "")).startswith("decision.") and isinstance(decision_id, str) and decision_id:
        # the decision's own event, not the thread message written just before it in the same write
        rows = conn.execute("SELECT * FROM waits WHERE kind='decision' AND ref=? AND state='open'",
                            (decision_id,)).fetchall()
        return [dict(r) for r in rows]
    if str(event.get("kind", "")).startswith("operation.") and payload.get("operation"):
        # a superseded wait names the older operation: every open one is checked against the chain it ends in
        rows = conn.execute("SELECT * FROM waits WHERE kind='operation' AND state='open'").fetchall()
        return [dict(r) for r in rows]
    if str(event.get("kind", "")).startswith("assist.") and payload.get("assist"):
        rows = conn.execute("SELECT * FROM waits WHERE kind='assist' AND ref=? AND state='open'",
                            (payload["assist"],)).fetchall()
        return [dict(r) for r in rows]
    if str(event.get("kind", "")).startswith("contour.") and payload.get("contour"):
        rows = conn.execute("SELECT * FROM waits WHERE kind='contour' AND ref=? AND state='open'",
                            (payload["contour"],)).fetchall()
        return [dict(r) for r in rows]
    if str(event.get("kind", "")).startswith("council.") and payload.get("council"):
        rows = conn.execute("SELECT * FROM waits WHERE kind='council' AND ref=? AND state='open'",
                            (payload["council"],)).fetchall()
        return [dict(r) for r in rows]
    if (event["kind"] == "card.changed" and (payload.get("after") or {}).get("status") in ("Done", "Cancelled")
            and event.get("card")):
        rows = conn.execute("SELECT * FROM waits WHERE kind='card' AND ref=? AND state='open'",
                            (event["card"],)).fetchall()
        return [dict(r) for r in rows]
    return []


def hook(ctx) -> Callable:
    """The pipeline's part of ``commands.install``: the waits an event decided, and the old wakes it asked for."""

    def run(s, event: Mapping, specs: list, enqueue: Callable[[dict], dict]) -> None:
        if str(event.get("kind", "")).startswith(commands.OWN_EVENTS):
            for spec in specs:
                enqueue(spec)
            return
        if event.get("kind") == "card.changed" and event.get("card"):
            card_edits.after_event(ctx, event)  # released outcomes, stages, dev checks
            from kd.cabinet import council_rounds  # at call time: it loads the jobs and the API

            council_rounds.on_card_changed(ctx, event)  # the card's council follows the card
        if event.get("kind") == "brief.version" and event.get("card"):
            from kd.cabinet import autostart, council_rounds  # at call time: they load the flow and the API

            council_rounds.on_brief_version(ctx, event)  # the brief's council gathers first
            autostart.on_brief_version(ctx, event)  # a brief of size S starts by itself
        if event.get("kind") == "notice" or str(event.get("kind", "")).startswith("operation."):
            from kd.cabinet import releases  # the release's health and its operations

            releases.on_event(ctx, event)
        decision_id = (event.get("payload") or {}).get("decision") or ""
        if not str(event.get("kind", "")).startswith("decision."):
            decision_id = ""
        covered = {}  # (agent, card) -> the outcome whose open wait is on this decision, before it resolves
        if decision_id:
            for spec in specs:
                found = covering(s, spec["agent"], spec["card"], decision_id) if spec.get("card") else None
                if found:
                    covered[(spec["agent"], spec["card"])] = found
        sets = []
        for wait in _touched(ctx, event):
            state = source_state(ctx, wait)
            if state != "open":
                _move(s, wait, state)
                if wait["command"] not in sets:
                    sets.append(wait["command"])
        enforcing = ctx.pipeline().enforcing
        merged = {}
        for spec in specs:
            if spec.get("kind") == "message" and spec.get("card"):
                from kd.cabinet import defers  # at call time: it loads the API this module registers on

                defers.clear(ctx, spec["card"], how="message")  # a person's word to the agent wakes it now
        for spec in specs:
            command = covered.get((spec["agent"], spec.get("card")))
            if command and enforcing and spec.get("kind") == "answer" and set_state(s, command) is None:
                continue  # the set still waits past this answer: the agent wakes once, when it is complete
            queued = enqueue(spec)  # an approval's own wake (a plan, a PR, a command) is never held back
            if command and enforcing:
                merged[command] = queued["id"]
        for command in sets:
            settle(ctx, command, merged_into=merged.get(command, ""))

    return run


# --- kd-task waits (Task 10) -----------------------------------------------------------------------------
PAGE_LIMIT = 8192  # bytes, as every read of an agent
PAGE_BUDGET = PAGE_LIMIT - 256
PAGE_WAITS = 1000


def _wait_line(row) -> str:
    line = (f"wait {row['kind']} {row['ref']} {row['state']} {'blocking' if row['blocking'] else 'non-blocking'} "
            f"by @{row['agent']} since {row['created']}")
    return line + (f" — {row['reason']}" if row["reason"] else "")


def page_lines(store, record) -> List[str]:
    """The card's recorded waits, newest first, then its decisions: the open ones, then the decided, newest first."""
    from kd.cabinet import decisions, view  # at call time: they load the API this module registers on

    rows = store._conn.execute("SELECT * FROM waits WHERE card=? ORDER BY created DESC, id DESC LIMIT ?",
                               (record.id, PAGE_WAITS)).fetchall()
    out = [_wait_line(r) for r in rows]
    found = decisions.Decisions(store).list(card=record.id)
    out += [f"decision {d['type']} {d['id']} open by {view.who(d['requested_by'])} since {d['created']}"
            for d in found if d["state"] == "open"]
    decided = sorted((d for d in found if d["state"] != "open"), key=lambda d: d["decided_at"] or d["created"],
                     reverse=True)
    out += [f"decision {d['type']} {d['id']} {d['state']} by {view.who(d['decided_by']) or '-'} "
            f"at {d['decided_at'] or d['created']}" for d in decided]
    return out


def _page(ctx, principal, request) -> dict:
    """``card.waits {card, after?}`` → ``{text}``: a page of ``page_lines`` from item ``after`` in 8 KiB."""
    record = api._visible_card(ctx, principal, request.get("card"))
    after = request.get("after", 0)
    if not isinstance(after, int) or isinstance(after, bool) or after < 0:
        raise api.ApiError("invalid", "after is the number of items a page printed before")
    lines = page_lines(ctx.store, record)
    waits_n = sum(1 for line in lines if line.startswith("wait "))
    head = f"{record.id} waits: {waits_n} recorded, {len(lines) - waits_n} decisions — newest first"
    used, shown = len(head.encode("utf-8")) + 1, []
    for line in lines[after:]:
        cost = len(line.encode("utf-8")) + 1
        if shown and used + cost > PAGE_BUDGET:
            break
        shown.append(line[:2000])
        used += cost
    nxt = after + len(shown)
    tail = f"more: kd-task waits {record.id} --after {nxt}" if nxt < len(lines) else f"end ({len(lines)} items)"
    return {"text": "\n".join([head, *shown, tail]) + "\n"}


OPS = {"card.waits": (api.READERS, _page)}
api.OPS.update(OPS)  # the board API serves it (api imports this module through outcomes at its end)
