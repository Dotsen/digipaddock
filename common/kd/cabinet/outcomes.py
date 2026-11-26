"""A turn's outcome: how an agent's turn on its own card ends.

``kd-work finish KD-n --outcome … --note …`` records it for the turn running now — the agent's ``started``
command in that card's thread; the cabinet finds it, nothing the agent passes names a command or a session. The
outcome is ``pending`` until the turn ends: then the cabinet applies it (a continuation, the waits, Review or
Done) or voids it with the turn. A finish again in the same turn replaces the outcome and its waits.

The outcomes: ``continue`` (the agent has its next step itself; its open questions stay non-blocking waits),
``wait_user`` (the decisions it waits for: named, or its open ones on the card), ``wait_dependency`` (cards of the
card's structured dependencies: named, or all not yet Done; or the operations, help, check contours and deploys it
names),
``wait_until`` (the card put off until ``until``, a moment with its time zone 10 minutes to 7 days away —
``defers``), ``review`` (from In Progress) and ``done``
(from In Progress or Review, within the closing policy). Handing work to another agent comes with package C.
"""

import json
import re
from typing import List, Mapping, Optional

from kd import boss_board, worker_board
from kd import contours as contour_file
from kd.cabinet import (
    api, briefs, continuations, councils, defers, deploys, problems, schema, stages, store as store_mod, waits,
)

WORK = ("To Do", "In Progress", "Blocked")
REVIEW, DONE, IN_PROGRESS, BLOCKED = "Review", "Done", "In Progress", "Blocked"
LIVE = ("queued", "delivered", "started")
SETTLED = ("missing_next_step", "outcome_refused")  # what an applied outcome or a turn that left a next step settles
NOTE_MAX = 300
REFS_MAX = 20
SESSION = re.compile(r"[0-9a-f]{12}")


def owes_outcome(record, agent: str) -> bool:
    """Whether a turn of ``agent`` on the card must end with an outcome: its own card, in work (or its brief)."""
    if agent == "owner" or record.assignee != "@" + agent:
        return False
    return record.status in WORK or (record.status == "Inbox" and briefs.BRIEF_SPEC in record.labels)


def may_finish(record, agent: str) -> bool:
    """``owes_outcome``, and a card in Review too: its holder closes it there (Done after the merge)."""
    return owes_outcome(record, agent) or (record.assignee == "@" + agent and record.status == REVIEW)


def running_command(store, record, agent: str) -> Optional[dict]:
    """The agent's turn running now in the card's thread: its ``started`` command."""
    rows = store.commands(thread=record.thread, agent=agent, states=["started"], limit=1)
    return rows[0] if rows else None


CONDITIONS_MAX = 10  # ids per kind in a refusal; the rest is counted


def _listed(name: str, items: List[str]) -> str:
    shown = ", ".join(items[:CONDITIONS_MAX])
    more = len(items) - CONDITIONS_MAX
    return f"{name} {shown}" + (f" (+{more})" if more > 0 else "")


def done_refusal(ctx, record, agent: str) -> Optional[str]:
    """Why the agent may not close the card itself, or None (the closing policy — pipeline spec 5.5): Done on a
    project that does not name the agent (its ``done_by``) is the owner's acceptance, and nothing on the
    card may still be open — its children, its dependencies, its decisions, its PR. Applied in enforce; the owner's
    «Accept» is not."""
    project = ctx.store.project(record.project)
    if not agent or (project or {}).get("done_by") != agent:
        return f"Done on {record.project} is the owner's acceptance: finish with --outcome review"
    from kd.cabinet import stage_gate  # at call time: it loads the API this module registers on

    if stage_gate.is_stage(ctx.store, record.id):  # its code review and the coordinator's gate close it
        return f"{record.id} is a stage of a chain plan, closed by its gate: finish with --outcome review"
    s, parts = ctx.store, []
    children = [r[0] for r in s._conn.execute(
        "SELECT id FROM cards WHERE parent=? AND status NOT IN (?, ?) ORDER BY n", (record.id, DONE, "Cancelled"))]
    if children:
        parts.append(_listed("children", children))
    deps = [d for d in record.dependencies if _not_done(s, d)]
    if deps:
        parts.append(_listed("dependencies", deps))
    decided = [f"{r[0]} {r[1][:8]}" for r in s._conn.execute(
        "SELECT type, id FROM decisions WHERE card=? AND state='open' ORDER BY created, id", (record.id,))]
    if decided:
        parts.append(_listed("decisions", decided))
    parts.extend(f"PR #{pr['number']}" for pr in s.prs(card=record.id, state="open"))
    helped = _open_help(s, record)
    if helped:
        parts.append(_listed("help", [h[:8] for h in helped]))
    return f"{record.id} has open conditions: {'; '.join(parts)}" if parts else None


def _invalid(message):
    return api.ApiError("invalid", message)


def _refused(message):
    return api.ApiError("refused", message)


def _note(value) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > NOTE_MAX or "\n" in value or "\r" in value:
        raise _invalid(f"--note is one line of 1..{NOTE_MAX} characters: what is next and why")
    return value.strip()


def _ids(request, name: str, pattern) -> List[str]:
    value = request.get(name) or []
    if (not isinstance(value, list) or len(value) > REFS_MAX
            or not all(isinstance(v, str) and pattern.fullmatch(v) for v in value)):
        raise _invalid(f"{name} is a list of at most {REFS_MAX} ids")
    return sorted(set(value))


def _open_decisions(store, record, agent: str) -> List[str]:
    rows = store._conn.execute(
        "SELECT id FROM decisions WHERE card=? AND state='open' AND requested_by=? ORDER BY created, id",
        (record.id, f"agent:{agent}"),
    ).fetchall()
    return [r[0] for r in rows]


def _decision_waits(store, record, agent: str, named: List[str]) -> List[dict]:
    if not named:
        named = _open_decisions(store, record, agent)
        if not named:
            held = [c for c in councils.of_card(store, record.id, "code") if c["state"] in councils.OPEN]
            if held:  # the pull request's decision opens when its council is over
                found = held[0]
                raise _refused(
                    f"no open decision of yours on {record.id}: its pull request is with council {found['id'][:8]}"
                    f" ({found['state']}) — wait for it (--outcome wait_dependency --council {found['id']}), or, the"
                    f" round being over, answer its findings (kd-work council {record.id}) and publish the fix"
                    " (kd-forge publish)")
            raise _refused(f"no open decision of yours on {record.id}: ask first (kd-work ask) or publish the PR")
    for decision_id in named:
        row = store._conn.execute("SELECT card FROM decisions WHERE id=?", (decision_id,)).fetchone()
        if row is None or row[0] != record.id:
            raise _invalid(f"decision {decision_id} is not on {record.id}")
    return [{"kind": "decision", "ref": d, "blocking": 1} for d in named]


def _card_waits(store, record, named: List[str]) -> List[dict]:
    deps = list(record.dependencies)
    if named:
        for card_id in named:
            if card_id not in deps:
                raise _refused(f"{card_id} is not a dependency of {record.id}: add it with kd-task edit --dep")
    else:
        named = []
        for card_id in deps:
            try:
                if store.card(card_id).status != DONE:
                    named.append(card_id)
            except store_mod.NotFound:
                named.append(card_id)  # a dependency gone is a wait that needs routing
        if not named:
            raise _refused(f"{record.id} has no open structured dependency")
    return [{"kind": "card", "ref": c, "blocking": 1} for c in sorted(named)]


def _operation_waits(store, record, agent: str, named: List[str]) -> List[dict]:
    """factory operations of the card (or asked for by the agent) the turn waits for — by id, or by the key
    of a request: a known key waits on its operation; a release's key before the release could ask
    (``release:<rev>``) waits for it to come."""
    from kd.cabinet import operations  # at call time: it loads the API this module registers on

    refs = []
    for ref in named:
        if not api.OPERATION_ID.fullmatch(ref):
            found = operations.request_row(store, ref)
            if found is None:
                if not waits.RELEASE_KEY.fullmatch(ref):
                    raise _invalid(f"no operation request {ref}: wait on an operation id or a release key")
                refs.append(ref)
                continue
            ref = found["operation"]
        row = store._conn.execute("SELECT card, requested_by FROM operations WHERE id=?", (ref,)).fetchone()
        if row is None or (row[0] != record.id and row[1] != f"agent:{agent}"
                           and not operations.linked(store, ref, card=record.id)):
            raise _invalid(f"operation {ref} is not on {record.id}")
        refs.append(ref)
    return [{"kind": "operation", "ref": o, "blocking": 1} for o in sorted(set(refs))]


def _assist_waits(store, record, agent: str, named: List[str]) -> List[dict]:
    """help requests of the card's lead the turn waits for — by id, or (none named) all its open ones."""
    from kd.cabinet import assists  # at call time: it loads the API this module registers on

    if not named:
        named = [a["id"] for a in assists.open_for(store, record.id, agent)]
    specs = []
    for assist_id in sorted(set(named)):
        found = assists.row(store, assist_id)
        if found is None or found["card"] != record.id or found["lead"] != agent:
            raise _invalid(f"help {assist_id} is not yours on {record.id}")
        # a rollout answered with the helper's plan card while the turn ran: the wait is on that card, as the
        # answer would have carried it had the turn ended first (``assists._follow``, KD-9460)
        plan = assists.plan_of(store, found)
        specs.append({"kind": "card", "ref": plan, "blocking": 1} if plan else
                     {"kind": "assist", "ref": assist_id, "blocking": 1})
    return specs


def _contour_waits(ctx, named: List[str]) -> List[dict]:
    """check contours of the release tree the turn waits for — out of maintenance (root's release)."""
    known = ctx.contours()
    for name in named:
        if name not in known:
            raise _invalid(f"no contour {name} in the release tree")
    return [{"kind": "contour", "ref": c, "blocking": 1} for c in named]


def _council_waits(store, record, named: List[str]) -> List[dict]:
    """a turn waits for a round of its own card's council — never another card's."""
    for council_id in named:
        found = councils.row(store, council_id)
        if found is None or found["card"] != record.id:
            raise _invalid(f"{council_id[:8]} is not a council of {record.id}")
    return [{"kind": "council", "ref": council_id, "blocking": 1} for council_id in named]


def _deploy_waits(store, record, named: List[str]) -> List[dict]:
    """runs of the projects' environments that hold a commit — named, or the card's last merged pull request's."""
    refs = sorted({deploys.wait_ref(store, record, target) for target in named})
    return [{"kind": "deploy", "ref": ref, "blocking": 1} for ref in refs]


def _open_help(store, record, agent: Optional[str] = None) -> List[str]:
    from kd.cabinet import assists  # at call time: it loads the API this module registers on

    return [a["id"] for a in assists.open_for(store, record.id, agent)]


def _specs(ctx, record, agent: str, outcome: str, request) -> List[dict]:
    from kd.cabinet import assists  # at call time: it loads the API this module registers on

    named_decisions = _ids(request, "decisions", api.DECISION_ID)
    named_cards = _ids(request, "cards", api.CARD)
    named_operations = _ids(request, "operations", api.OPERATION_REF)
    named_assists = _ids(request, "assists", assists.ASSIST_ID)
    named_contours = _ids(request, "contours", contour_file.NAME_RE)
    named_councils = _ids(request, "councils", councils.COUNCIL_ID)
    named_deploys = _ids(request, "deploys", deploys.TARGET)
    if named_decisions and outcome != "wait_user":
        raise _invalid("--decision goes with --outcome wait_user")
    if ((named_cards or named_operations or named_assists or named_contours or named_councils or named_deploys)
            and outcome != "wait_dependency"):
        raise _invalid("--card, --operation, --assist, --contour, --council and --deploy go with --outcome "
                       "wait_dependency")
    if outcome in ("review", "done", "wait_user") or named_councils:  # not past the council's findings
        from kd.cabinet import council_rounds  # at call time: it loads the jobs and the API

        council_rounds.on_finish(ctx, record, agent, outcome, bool(named_councils))
    if (request.get("until") is not None) != (outcome == "wait_until"):
        raise _invalid("--until goes with --outcome wait_until, and wait_until needs it")
    if outcome == "wait_until" and request.get("any", False):
        raise _invalid("--any does not go with --outcome wait_until")
    s = ctx.store
    if outcome == "wait_until":  # the card put off until a moment, its own time wait
        return [{"kind": "time", "ref": defers.parse_until(request["until"], ctx.now(), "agent"), "blocking": 1}]
    if outcome == "wait_user":
        return _decision_waits(s, record, agent, named_decisions)
    if outcome == "wait_dependency":
        if named_cards or named_operations or named_assists or named_contours or named_councils or named_deploys:
            cards = _card_waits(s, record, named_cards) if named_cards else []
            return (cards + _operation_waits(s, record, agent, named_operations)
                    + (_assist_waits(s, record, agent, named_assists) if named_assists else [])
                    + _contour_waits(ctx, named_contours) + _council_waits(s, record, named_councils)
                    + _deploy_waits(s, record, named_deploys))
        helped = _assist_waits(s, record, agent, [])
        if helped and not any(_not_done(s, d) for d in record.dependencies):
            return helped
        return _card_waits(s, record, []) + helped
    if outcome == "continue":
        return [{"kind": "decision", "ref": d, "blocking": 0} for d in _open_decisions(s, record, agent)]
    if outcome in ("review", "done"):
        helped = _open_help(s, record, agent)
        if helped:
            raise _refused(f"{record.id} has open help {', '.join(h[:8] for h in helped)}: wait for it "
                           "(--outcome wait_dependency) or let it end")
    if outcome == "review" and record.status != IN_PROGRESS:
        raise _refused(f"review is from In Progress; {record.id} is {record.status}")
    if outcome == "review":
        staged = stages.open_children(s, record.id)
        if staged:  # an aggregate goes to Review by itself when its stages are Done
            raise _refused(f"{record.id} has open conditions: {_listed('children', staged)}; its stages move it to"
                           " Review — end the turn without an outcome")
    if outcome == "done":
        if record.status not in (IN_PROGRESS, REVIEW):
            raise _refused(f"done is from In Progress or Review; {record.id} is {record.status}")
        why = done_refusal(ctx, record, agent) if ctx.pipeline().enforcing else None
        if why:
            raise _refused(why)
    return []


def _outcome_row(store, command: str) -> Optional[dict]:
    row = store._conn.execute("SELECT * FROM turn_outcomes WHERE command=?", (command,)).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["basis"] = json.loads(data["basis"] or "{}")
    return data


def _same(known: Optional[Mapping], outcome: str, note: str, mode: str, specs: List[dict], current) -> bool:
    if known is None or (known["outcome"], known["note"], known["mode"]) != (outcome, note, mode):
        return False
    have = sorted((w["kind"], w["ref"], w["blocking"]) for w in current if w["state"] == "pending")
    return have == sorted((w["kind"], w["ref"], w["blocking"]) for w in specs)


def _finish(ctx, principal, request):
    requested = request.get("agent")
    agent = api._executor_agent(ctx, principal, request, coordinator=requested == "boss")
    record = api._card(ctx, request.get("card"))
    outcome = request.get("outcome")
    if outcome == "wait_handoff":
        raise _invalid("handoffs come with package C: for now hand the card over with kd-task handoff")
    if outcome not in schema.OUTCOMES:
        raise _invalid(f"--outcome is one of {', '.join(schema.OUTCOMES)}")
    if agent == problems.BOSS and outcome in ("review", "done"):  # its authority: it never sets Review or Done
        raise _refused("Done of the coordinator's card is the owner's, and its Review goes through kd-boss-board "
                       "review with the source: finish with continue, wait_user or wait_dependency")
    note = _note(request.get("note"))
    any_ = request.get("any", False)
    if not isinstance(any_, bool):
        raise _invalid("any is true or false")
    mode = "any" if any_ else "all"
    if not may_finish(record, agent):
        raise _refused(f"finish is for the card's assignee on a card in work; {record.id} is {record.status}, "
                       f"held by {record.assignee or 'nobody'}")
    if record.status == REVIEW and outcome != "done":
        raise _refused(f"{record.id} is in Review: only --outcome done (after the merge) applies there")
    session = request.get("session", "")
    if session != "" and (not isinstance(session, str) or not SESSION.fullmatch(session)):
        raise _invalid("session is the caller's session: 12 hex characters of its unit")
    s = ctx.store
    with s.transaction():
        command = running_command(s, record, agent)
        if command is None or (session and not command["session"].startswith(session)):
            raise _refused(f"no turn of yours is running on {record.id}: finish ends the turn the cabinet started "
                           "for you")
        specs = _specs(ctx, record, agent, outcome, request)
        known = _outcome_row(s, command["id"])
        if known is not None and known["state"] != "pending":
            raise _refused(f"this turn's outcome on {record.id} is applied already")
        current = waits.of_command(s, command["id"])
        if _same(known, outcome, note, mode, specs, current):
            return {"outcome": known, "waits": current, "unchanged": True}
        row, recorded = _record(s, record, command["id"], agent, outcome, mode, note, specs, actor=f"agent:{agent}")
    return {"outcome": row, "waits": recorded}


def _record(store, record, command: str, agent: str, outcome: str, mode: str, note: str, specs: List[dict], *,
            actor: str, **extra):
    """The turn's outcome as ``pending`` with its waits, in place of what the turn recorded before."""
    now = store._clock()
    basis = json.dumps(continuations.basis_of(store, record), ensure_ascii=False, sort_keys=True)
    store._conn.execute(
        "INSERT INTO turn_outcomes (command, card, thread, agent, outcome, mode, note, basis, state, created,"
        " updated) VALUES (?,?,?,?,?,?,?,?,'pending',?,?) ON CONFLICT(command) DO UPDATE SET"
        " outcome=excluded.outcome, mode=excluded.mode, note=excluded.note, basis=excluded.basis,"
        " updated=excluded.updated",
        (command, record.id, record.thread, agent, outcome, mode, note, basis, now, now),
    )
    row = _outcome_row(store, command)
    recorded = waits.record_pending(store, row, specs)
    store._event(actor, "turn.finish", record.id,
                 {"command": command, "outcome": outcome, "waits": len(recorded), **extra}, record.thread)
    return row, recorded


def _implied(ctx, record, command: Mapping) -> Optional[dict]:
    """a turn that asked for help and ended without an outcome waits for that help — the cabinet records
    it as the agent should have (``wait_dependency`` on its open help), so the answer wakes the lead instead of the
    card being left with nobody on it. The row, or None: no open help of this agent on its card in work, or the
    card is held already (a wait recorded earlier, an open decision) — an applied outcome would replace that set."""
    s, agent = ctx.store, command["agent"]
    if not owes_outcome(record, agent) or _stopped(s, record, agent, command):  # the owner's stop waits for him
        return None
    if next_state(ctx, record) != "missing":  # what the lead recorded before still holds the card: keep that set
        return None
    helped = _open_help(s, record, agent)
    if not helped:
        return None
    specs = [{"kind": "assist", "ref": a, "blocking": 1} for a in sorted(helped)]
    return _record(s, record, command["id"], agent, "wait_dependency", "all", "", specs, actor="system",
                   implied=True)[0]


# --- the turn's end (Task 5) --------------------------------------------------------------------------------
def _set(store, command: str, state: str, reason: str = "") -> None:
    row = _outcome_row(store, command)
    store._conn.execute("UPDATE turn_outcomes SET state=?, reason=?, updated=? WHERE command=?",
                        (state, reason, store._clock(), command))
    store._event("system", "turn.outcome", row["card"],
                 {"command": command, "outcome": row["outcome"], "state": state, "reason": reason}, row["thread"])


def void(ctx, command_id: str, reason: str) -> None:
    """A turn that failed or was cut by the host: its pending outcome and waits never apply."""
    s = ctx.store
    with s.transaction():
        row = _outcome_row(s, command_id)
        if row is None or row["state"] != "pending":
            return
        s._conn.execute("UPDATE waits SET state='cancelled', reason=?, updated=? WHERE command=? AND state='pending'",
                        (reason, s._clock(), command_id))
        _set(s, command_id, "void", reason)


def next_state(ctx, record) -> str:
    """Who moves the card now: ``running`` (a command of its agent), ``waiting`` (a recorded wait, an open
    decision of the agent, Blocked on a dependency), ``review``, ``done``, ``owner`` (no agent holds it) or
    ``missing`` — nobody."""
    if record.terminal:  # Done, or closed without the work: nobody is owed a move
        return "done"
    if record.status == REVIEW:
        return "review"
    if not record.assignee.startswith("@") or record.assignee == "@owner":
        return "owner"
    agent, conn = record.assignee[1:], ctx.store._conn
    live = conn.execute(
        f"SELECT 1 FROM commands WHERE thread=? AND agent=? AND state IN ({','.join('?' * len(LIVE))}) LIMIT 1",
        (record.thread, agent, *LIVE),
    ).fetchone()
    if live is not None:
        return "running"
    if conn.execute("SELECT 1 FROM waits WHERE card=? AND agent=? AND state='open' AND blocking=1 LIMIT 1",
                    (record.id, agent)).fetchone() is not None:
        return "waiting"
    if _open_decisions(ctx.store, record, agent):
        return "waiting"
    if record.status == BLOCKED and any(_not_done(ctx.store, d) for d in record.dependencies):
        return "waiting"
    if stages.open_children(ctx.store, record.id):  # an aggregate: its stages move it
        return "waiting"
    if defers.active(ctx.store, record.id) is not None:  # put off until a time: the time wakes it
        return "waiting"
    return "missing"


def _not_done(store, card_id: str) -> bool:
    try:
        return store.card(card_id).status != DONE
    except store_mod.NotFound:
        return True


def _move(ctx, record, agent: str, status: str) -> Optional[str]:
    """The card to ``status`` as the agent would move it (its transition table); why not, or None."""
    if record.status == status:  # moved there during the turn already (by hand, or by the owner)
        return None
    helped = _open_help(ctx.store, record, agent)  # asked for after the outcome was recorded (H06)
    if helped:
        return f"{record.id} has open help {', '.join(h[:8] for h in helped)}: wait for it or let it end"
    if status == DONE and ctx.pipeline().enforcing:
        why = done_refusal(ctx, record, agent)
        if why:
            return why
    try:
        plan = worker_board.plan("edit", {"status": status}, agent=agent,
                                 card=boss_board.card_from(record.to_dict()), targets=ctx.targets(), specs=None)
        api._apply(ctx, plan, f"agent:{agent}")
    except (worker_board.Refused, boss_board.Refused) as exc:
        return str(exc)
    except api.ApiError as exc:
        return str(exc)
    return None


def _apply(ctx, record, row: Mapping, command: Mapping) -> Optional[str]:
    """The pending outcome's effects; the reason it could not apply, or None."""
    s, agent, outcome = ctx.store, row["agent"], row["outcome"]
    basis = row["basis"]
    if basis.get("assignee") != record.assignee:
        s._conn.execute("UPDATE waits SET state='cancelled', reason='reassigned', updated=? "
                        "WHERE command=? AND state='pending'", (s._clock(), row["command"]))
        _set(s, row["command"], "superseded", "reassigned")
        return None
    if basis.get("scope") != briefs.scope_sha(briefs.scope(record))[:16]:
        s._conn.execute("UPDATE waits SET state='cancelled', reason='scope_changed', updated=? "
                        "WHERE command=? AND state='pending'", (s._clock(), row["command"]))
        _set(s, row["command"], "superseded", "scope_changed")
        return None
    s._conn.execute("UPDATE waits SET state='cancelled', reason='superseded', updated=? WHERE card=? AND agent=? "
                    "AND state='open' AND command!=?", (s._clock(), record.id, agent, row["command"]))
    own = defers.active(s, record.id)
    if own is not None and own["source"] == "agent" and outcome != "wait_until":
        defers.clear(ctx, record.id, how="superseded")  # the agent's next outcome is its next step now
    why = None
    if outcome == "wait_until":
        _defer(ctx, record, row)
        return None
    if outcome == "continue":
        waits.activate(ctx, row["command"])
        launched = continuations.launch(ctx, key=f"cont:{row['command']}", record=record, agent=agent,
                                        source="turn", text=continuations.CONTINUE.format(id=record.id))
        if launched["state"] == "stopped":  # the owner's stop beat it: not applied, "Continue" is next
            _set(s, row["command"], "void", "stopped")
            return None
    elif outcome in ("wait_user", "wait_dependency"):
        waits.activate(ctx, row["command"])
        _set(s, row["command"], "final")
        waits.settle(ctx, row["command"])  # what came during the turn may have completed the set already
        return None
    elif outcome == "review":
        why = _move(ctx, record, agent, REVIEW)
    elif outcome == "done":
        why = _move(ctx, record, agent, DONE)
    _set(s, row["command"], "final", why or "")
    return why


def _defer(ctx, record, row: Mapping) -> None:
    """``wait_until`` applied — the card put off until its time wait's moment, in place of the active defer;
    the agent's third defer in a row without ``kd-work progress`` asks BOSS to look (``continuation_limit``)."""
    s, agent = ctx.store, row["agent"]
    waits.activate(ctx, row["command"])
    timed = [w for w in waits.of_command(s, row["command"]) if w["kind"] == "time"]
    streak = defers.agent_streak(s, record.id, agent)
    placed = defers.place(ctx, record, until=timed[0]["ref"], source="agent", by=f"agent:{agent}",
                          command=row["command"], streak=streak)
    _set(s, row["command"], "final")
    if streak >= defers.STREAK:
        problems.raise_problem(ctx, key=f"defer:{placed['id']}", record=record, kind="continuation_limit",
                               agent=agent, command=row["command"], detail={"reason": "defer", "streak": streak})
    waits.settle(ctx, row["command"])  # the moment may have passed while the turn ran


def finalize(ctx, command: Mapping, ok: bool) -> None:
    """The turn of ``command`` ended (the host's report, in its transaction): apply its outcome, or see that a
    turn which owed one left the card with nobody on it."""
    if not command.get("card") or command["kind"] == "stop":
        return
    s = ctx.store
    try:
        record = s.card(command["card"])
    except store_mod.NotFound:
        record = None
    if not ok:
        void(ctx, command["id"], "turn_failed")
        if record is not None:
            _failed(ctx, record, command)
        return
    if record is None:
        return
    row = _outcome_row(s, command["id"]) or _implied(ctx, record, command)
    refused, applied = None, False
    if row is not None and row["state"] == "pending":  # a repeated report finds it final already
        refused = _apply(ctx, record, row, command)
        record, row = s.card(record.id), _outcome_row(s, command["id"])
        applied = refused is None and row["state"] == "final"
    state = next_state(ctx, record)
    if applied or state != "missing":
        problems.resolve(s, record.id, SETTLED, by=f"turn:{command['id']}")
    agent = command["agent"]
    if refused:
        problems.raise_problem(ctx, key=f"refused:{command['id']}", record=record, kind="outcome_refused",
                               agent=agent, command=command["id"], detail={"reason": refused[:300]})
    elif ((row is None or row["state"] != "final") and owes_outcome(record, agent) and state == "missing"
          and not _stopped(s, record, agent, command)):
        # no outcome, or one the card's change superseded: either way nobody is on the card now
        problems.raise_problem(ctx, key=f"missing:{command['id']}", record=record, kind="missing_next_step",
                               agent=agent, command=command["id"])


def _stopped(store, record, agent: str, command: Mapping) -> bool:
    """The turn ended by the owner's stop: the card waits for him ("Continue"), not for a route."""
    last = _last_command(store, record, agent)
    return command.get("error") == "stopped" or (last is not None and last["kind"] == "stop")


def _failed(ctx, record, command: Mapping) -> None:
    """A failed turn of the holder left nobody on the card — the same problem as a turn without an
    outcome, unless the owner stopped it."""
    agent = command["agent"]
    if not owes_outcome(record, agent) or _stopped(ctx.store, record, agent, command):
        return
    if next_state(ctx, record) == "missing":
        problems.raise_problem(ctx, key=f"missing:{command['id']}", record=record, kind="missing_next_step",
                               agent=agent, command=command["id"])


# problems a turn of the holder settles; the held ones end only with their own fact or the card's close
# so do a council's — raised about the writer while another agent holds the card, they end with the
# council's own next fact (its next round, its end), never with whoever takes a turn on the card
HELD = ("release_degraded", "stage_missing", "stage_cancelled", "stage_reopened",
        "council_failed", "council_run_failed", "council_stalled", "council_unanswered")
TURN_RESOLVES = tuple(k for k in schema.PROBLEM_KINDS if k not in HELD)


def started(ctx, command: Mapping) -> None:
    """A new turn of the card's holder started: whoever raised its last problem, someone is on it now."""
    if not command.get("card"):
        return
    try:
        record = ctx.store.card(command["card"])
    except store_mod.NotFound:
        return
    if record.assignee == "@" + command["agent"]:
        problems.resolve(ctx.store, record.id, TURN_RESOLVES, by=f"turn:{command['id']}")


def released(ctx, card_id: str) -> None:
    """The card changed: the problems of an agent that owes no outcome on it any more (the card left work, went
    to someone else) are settled — the next step is no longer that agent's."""
    from kd.cabinet import assists  # at call time: it loads the API this module registers on

    s = ctx.store
    assists.cancel_for_card(ctx, card_id)  # help the lead no longer needs: the card left it, its work or its scope
    rows = problems.open_for(s, card_id)
    if not rows:
        return
    record = s.card(card_id)
    gone = sorted({p["agent"] for p in rows if p["agent"] and not owes_outcome(record, p["agent"])})
    if gone:
        kinds = schema.PROBLEM_KINDS if record.terminal else TURN_RESOLVES
        problems.resolve(s, card_id, kinds, by=f"card:{record.status}:{record.assignee}", agents=gone)


# --- what a reader sees (Task 10) -------------------------------------------------------------------------
FACTS_WAITS = 50
FINE = ("ok", "unchanged")


def operation_view(store, ref: str) -> dict:
    """An operation wait's source as the page shows it: its roles, state, phase and the roles done; a
    release's key not asked for yet is ``awaited``."""
    from kd.cabinet import operations  # at call time: it loads the API this module registers on

    if not operations.OPERATION_ID.fullmatch(ref):
        found = operations.request_row(store, ref)
        ref = found["operation"] if found else ""
    op = operations.latest(store, ref) if ref else None
    if op is None:
        return {"id": "", "agents": [], "state": "awaited", "phase": "", "done": [], "component": ""}
    done = op["result"].get("agents") or {}
    params = op.get("params") or {}
    component = f"{params['component']} {params.get('mode', '')}".strip() if params.get("component") else ""
    return {"id": op["id"], "agents": op["agents"], "state": op["state"], "phase": op["phase"],
            "done": sorted(a for a, v in done.items() if v.get("state") in FINE), "component": component}


ASSISTS_SHOWN = 10


def assist_view(store, ref: str) -> dict:
    """A help request as the page and the snapshot show it: who asked, which role, the step, its state."""
    from kd.cabinet import assists  # at call time: it loads the API this module registers on

    found = assists.row(store, ref)
    if found is None:
        return {"id": ref, "kind": "", "lead": "", "role": "", "helper": "", "step": "", "state": "gone",
                "reason": "", "evidence": "", "limit": "", "updated": ""}
    return {"id": found["id"], "kind": found["kind"], "lead": "@" + found["lead"], "role": "@" + found["role"],
            "helper": "@" + found["helper"] if found["helper"] else "", "step": found["step"],
            "state": found["state"], "reason": found["reason"], "evidence": found["evidence"],
            "limit": found["proof_limit"], "updated": found["updated"]}


def assists_of(store, card: str) -> List[dict]:
    """The card's help requests, open first, newest first, at most ``ASSISTS_SHOWN``."""
    rows = store._conn.execute(
        "SELECT id FROM assists WHERE card=? ORDER BY state IN ('requested','assigned','accepted') DESC, created DESC,"
        " rowid DESC LIMIT ?", (card, ASSISTS_SHOWN)).fetchall()
    return [assist_view(store, r[0]) for r in rows]


PAUSE_SCAN = 200  # the newest operations that decide whether a role is held


def pauses(store, seen=None) -> List[dict]:
    """The roles a factory operation holds: the newest operation of each role decides — one running
    holds the roles it has not installed yet (``held: operation``), with the drain's deadline and the role's own turns
    it waits for; a failed one holds what its result kept until root resumes them (``held: failed``). ``seen(card)``
    hides the cards of projects the reader does not see."""
    from kd.cabinet import operations

    def show(card):
        return card if card and (seen is None or seen(card)) else ""

    decided, out = set(), []
    for (operation_id,) in store._conn.execute(
            "SELECT id FROM operations WHERE state IN ('running','succeeded','failed','superseded') AND kind!=?"
            " ORDER BY created DESC, rowid DESC LIMIT ?", (operations.DEVCHECK, PAUSE_SCAN)).fetchall():
        op = operations.row(store, operation_id)
        fresh = [a for a in op["agents"] if a not in decided]
        decided.update(op["agents"])
        if op["state"] == "running":
            done = op["result"].get("agents") or {}
            held, kind, until = [a for a in fresh if a not in done], "operation", op["result"].get("until", "")
        elif op["state"] == "failed":
            held, kind, until = [a for a in fresh if a in operations.held(op)], "failed", ""
        else:
            continue
        for agent in held:
            busy = [{"agent": "@" + agent, "card": show(r[0] or "")} for r in store._conn.execute(
                "SELECT t.card FROM sessions x JOIN threads t ON t.id = x.thread WHERE x.state IN"
                " ('starting','working') AND x.agent=? ORDER BY t.card", (agent,)).fetchall()]                 if kind == "operation" else []
            out.append({"agent": "@" + agent, "held": kind, "operation": op["id"], "card": show(op["card"]),
                        "phase": op["phase"], "until": until, "busy": busy})
    return sorted(out, key=lambda p: p["agent"])


def card_facts(ctx, record, principal=None) -> dict:
    """The card's pipeline facts for its page and its snapshot: the mode, the last recorded outcome, the open
    waits (and the pending ones of that outcome), blocking first, the open problems, oldest first, and the roles
    the factory holds (cards of projects ``principal`` does not see hidden)."""
    s = ctx.store
    from kd.cabinet import stage_gate  # at call time: it loads the API, which loads this module

    def seen(card):
        try:
            return principal is None or api.visible(ctx, principal, s.card(card).project)
        except store_mod.NotFound:
            return False
    last = s._conn.execute("SELECT * FROM turn_outcomes WHERE card=? ORDER BY created DESC, rowid DESC LIMIT 1",
                           (record.id,)).fetchone()
    outcome = None
    if last is not None:
        outcome = {k: last[k] for k in ("outcome", "note", "state", "reason", "created")}
        outcome["agent"] = "@" + last["agent"]
    where = "card=? AND (state='open' OR (state='pending' AND command=?))"
    args = (record.id, last["command"] if last is not None else "")
    rows = s._conn.execute(f"SELECT id, kind, ref, state, blocking, reason FROM waits WHERE {where} "
                           "ORDER BY blocking DESC, created DESC, id DESC LIMIT ?", (*args, FACTS_WAITS)).fetchall()
    total = s._conn.execute(f"SELECT COUNT(*) FROM waits WHERE {where}", args).fetchone()[0]
    return {
        "mode": ctx.pipeline().mode,
        "outcome": outcome,
        "waits": [{"id": r["id"], "kind": r["kind"], "ref": r["ref"], "state": r["state"],
                   "blocking": bool(r["blocking"]), "reason": r["reason"],
                   **({"operation": operation_view(s, r["ref"])} if r["kind"] == "operation" else {}),
                   **({"assist": assist_view(s, r["ref"])} if r["kind"] == "assist" else {})} for r in rows],
        "waits_total": total,
        "problems": [{"kind": p["kind"], "agent": "@" + p["agent"] if p["agent"] else "", "created": p["created"]}
                     for p in problems.open_for(s, record.id)],
        "pauses": pauses(s, seen),
        "assists": assists_of(s, record.id),
        "stages": stages.facts(s, record.id),  # the aggregate's stages, None for a plain card
        "stage": stage_gate.fact(s, record),  # a chain plan's stage at its gate, None for any other card
        "release": _release_state(s, record.id),
        "defer": defers.view(defers.active(s, record.id)),  # put off until a time, None when not
    }


def _release_state(s, card_id: str) -> dict:
    from kd.cabinet import releases  # at call time: releases imports problems, which this module imports too

    return releases.state(s, card_id)


# --- the sweep and the status (Task 8) ---------------------------------------------------------------------
PROTOCOL = 1
SWEEP_CARDS = 500


def _since(ctx) -> str:
    """From when turns owe an outcome: the moment enforce was set, or the base's first pipeline moment."""
    config = ctx.pipeline()
    since = ctx.store.meta("pipeline_since") or ""
    if config.enforcing and config.since:
        since = max(since, config.since)
    return since


def _last_command(store, record, agent: str) -> Optional[dict]:
    rows = store.commands(thread=record.thread, agent=agent, limit=1)
    return rows[0] if rows else None


def sweep(ctx) -> dict:
    """What the events may have missed: waits whose source moved, outcomes of commands that died, and cards in
    work whose holder's last command ended after ``since`` with nobody on the card now."""
    s = ctx.store
    rechecked = voided = raised = 0
    with s.transaction():
        sets = []
        for wait in [dict(r) for r in s._conn.execute("SELECT * FROM waits WHERE state='open' LIMIT 1000")]:
            state = waits.source_state(ctx, wait)
            if state != "open":
                waits._move(s, wait, state)
                rechecked += 1
                if wait["command"] not in sets:
                    sets.append(wait["command"])
        for command in sets:
            waits.settle(ctx, command)
        deferred = defers.fire_due(ctx)  # the cards whose time came, after their time waits settled
        from kd.cabinet import assists  # at call time: it loads the API this module registers on

        helped = assists.sweep(ctx)
        staged = stages.sweep(ctx)  # approved stages without their child
        from kd.cabinet import releases

        degraded = releases.sweep(ctx)  # degraded releases: BOSS looks again
        from kd.cabinet import council_rounds

        gathered = council_rounds.sweep(ctx)  # lost critics, silent writers
        from kd.cabinet import stage_gate

        gated = stage_gate.sweep(ctx)  # a chain stage's reviewer that waited for its head; a silent coordinator
        from kd.cabinet import decisions

        withdrawn = decisions.Decisions(s).sweep_closed(ctx.now())  # nothing to decide on a closed card
        for row in s._conn.execute(
            "SELECT o.command, c.state FROM turn_outcomes o JOIN commands c ON c.id = o.command "
            "WHERE o.state='pending' AND c.state IN ('failed','cancelled')"
        ).fetchall():
            void(ctx, row[0], f"turn_{row[1]}")
            voided += 1
        since = _since(ctx)
        hosted = ctx.host_config().hosted
        rows = s._conn.execute(
            "SELECT id FROM cards WHERE assignee LIKE '@%' AND (status IN ('To Do','In Progress','Blocked') OR "
            "(status='Inbox' AND labels LIKE ?)) ORDER BY n LIMIT ?",
            (f'%"{briefs.BRIEF_SPEC}"%', SWEEP_CARDS),
        ).fetchall()
        for (card_id,) in rows:
            record = s.card(card_id)
            agent = record.assignee[1:]
            if not hosted(agent) or not owes_outcome(record, agent):
                continue
            last = _last_command(s, record, agent)
            if last is None or last["state"] not in ("done", "cancelled", "failed") or last["updated"] < since \
                    or _stopped(s, record, agent, last):
                continue  # a stop is the owner's: the card waits for him ("Continue"), not for a route
            known = _outcome_row(s, last["id"])
            if last["state"] == "done" and known is not None and known["state"] == "final":
                continue  # its outcome applied: the waits, the continuation or a problem carry it
            if next_state(ctx, record) != "missing":
                continue
            if problems.raise_problem(ctx, key=f"missing:{last['id']}", record=record, kind="missing_next_step",
                                      agent=agent, command=last["id"]):
                raised += 1
    return {"rechecked": rechecked, "voided": voided, "problems": raised, "help": helped, "stages": staged,
            "releases": degraded, "decisions": withdrawn, "deferred": deferred, "councils": gathered,
            "gate": gated}


def status(ctx) -> dict:
    s, config = ctx.store, ctx.pipeline()
    count = lambda sql, *args: s._conn.execute(sql, args).fetchone()[0]  # noqa: E731
    hour_ago = continuations._since(ctx, 3600)
    return {
        "protocol": PROTOCOL,
        "schema": schema.SCHEMA_VERSION,
        "mode": config.mode,
        "since": config.since or s.meta("pipeline_since") or "",
        "pending": count("SELECT COUNT(*) FROM turn_outcomes WHERE state='pending'"),
        "open_waits": count("SELECT COUNT(*) FROM waits WHERE state='open'"),
        "open_problems": count("SELECT COUNT(*) FROM problems WHERE state='open'"),
        "continuations_hour": count("SELECT COUNT(*) FROM continuations WHERE state='queued' AND created>=?", hour_ago),
    }


OPS = {
    "turn.finish": (frozenset({"executor"}), _finish),
    "pipeline.sweep": (frozenset({"cabinet", "root"}), lambda ctx, principal, request: sweep(ctx)),
    "pipeline.status": (frozenset({"cabinet", "root"}), lambda ctx, principal, request: status(ctx)),
    "pipeline.inventory": (frozenset({"cabinet", "root"}), lambda ctx, principal, request: _inventory(ctx)),
}


def _inventory(ctx) -> dict:
    """``pipeline.inventory`` → ``{"items": [...]}`` — the read-only inventory."""
    from kd.cabinet import inventory  # at call time: it imports this module

    return {"items": inventory.collect(ctx)}
api.OPS.update(OPS)  # the board API serves it (api imports this module at its end)
