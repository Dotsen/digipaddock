"""Help inside an approved task: the card's lead asks another role for one bounded step.

The lead keeps its card; the helper does one step of a kind the catalog names
(``common/coordination/assist.json``: the kind and the roles that may do it) and answers on the lead's card. The
kinds are reads, but for ``server_change``: there the sysadmin opens its own admin card with a plan made
from the lead's rollout sheet and answers with that card's number — the plan waits for the owner's signature like
any other, so the request itself still changes nothing. The
request, BOSS's signal and, later, the helper's command are written in one transaction each, so a crash never leaves
a request nobody hears of. The states::

    requested -> assigned -> accepted -> done | failed
    (any open state) -> cancelled          the card left the lead or its work (the pipeline, Task 2)

A request is keyed by the card, the lead and the lead's own key: the same request again answers the row, another
payload under the same key is a conflict. BOSS assigns the role (``assist.assign``); the helper's command is a signal
in the lead's card thread with the card as the lead held it (``basis``), so the dispatcher drops it by itself when
the card moved on. The helper reads the request (``assist.show``: accepted) and answers once (``assist.reply``); an
answer after the request was cancelled is kept as evidence in the thread and changes nothing. Nothing here grants a
right: the helper keeps its own policy and the lead's approvals stay where they were.
"""

import hashlib
import json
import re
import secrets
from datetime import timedelta
from pathlib import Path
from typing import List, Mapping, Optional

from kd import boss_board, worker_board
from kd.cabinet import api, briefs, continuations, schema, store as store_mod

CATALOG = Path(__file__).resolve().parents[3] / "common" / "coordination" / "assist.json"
OPEN = ("requested", "assigned", "accepted")
FINAL = ("done", "failed", "cancelled")
LEAD_STATUSES = ("To Do", "In Progress", "Blocked", "Review")
HELPER_REASONS = tuple(r for r in schema.ASSIST_FAIL_REASONS if r != "expired")  # expired is the sweep's
STEP_MAX, EVIDENCE_MAX, LIMIT_MAX, REASON_MAX = 500, 2000, 500, 300
ASSIST_ID = re.compile(r"[0-9a-f]{32}")
KEY = re.compile(r"[A-Za-z0-9._:-]{1,64}")
SESSION = re.compile(r"[0-9a-f]{12}")
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f​-‏ -‮⁠-⁩﻿]")
REQUEST_SIGNAL = "[assist] {card}: @{lead} asks for help {short} — run kd-boss-board assist-list"
HELP_SIGNAL = "[assist] {card}: help {short} for @{lead} — run kd-work assist show {id}"


def _invalid(message):
    return api.ApiError("invalid", message)


def _refused(message):
    return api.ApiError("refused", message)


def load_catalog(path=None) -> dict:
    """The kinds of help and the roles that may do each: ``{kind: frozenset(roles)}``."""
    try:
        data = json.loads(Path(path or CATALOG).read_text(encoding="utf-8"))
        if data.get("schemaVersion") != 1 or not isinstance(data.get("kinds"), dict):
            raise ValueError("schema")
        out = {}
        for kind, entry in data["kinds"].items():
            roles = entry["roles"]
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", kind) or not roles or not all(
                    isinstance(r, str) and r for r in roles):
                raise ValueError(kind)
            out[kind] = frozenset(roles)
        return out
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise api.ApiError("unavailable", f"the help catalog is not readable ({exc.__class__.__name__})")


def _line(value, name: str, limit: int, *, empty: bool = False) -> str:
    """One line of plain text, at most ``limit`` characters."""
    if not isinstance(value, str) or _CONTROL.search(value) or len(value.strip()) > limit or (
            not empty and not value.strip()):
        raise _invalid(f"{name} is one line of {0 if empty else 1}..{limit} characters")
    return value.strip()


def row(store, assist_id: str) -> Optional[dict]:
    found = store._conn.execute("SELECT * FROM assists WHERE id=?", (assist_id,)).fetchone()
    if found is None:
        return None
    out = dict(found)
    out["basis"] = json.loads(out["basis"] or "{}")
    return out


def open_for(store, card: str, lead: Optional[str] = None) -> List[dict]:
    """The card's open help requests (of ``lead``), oldest first."""
    rows = store._conn.execute(
        f"SELECT id FROM assists WHERE card=? AND state IN ({','.join('?' * len(OPEN))}) ORDER BY created, rowid",
        (card, *OPEN)).fetchall()
    found = [row(store, r[0]) for r in rows]
    return [r for r in found if lead is None or r["lead"] == lead]


def _find(store, request) -> dict:
    assist_id = request.get("id")
    found = row(store, assist_id) if isinstance(assist_id, str) and ASSIST_ID.fullmatch(assist_id) else None
    if found is None:
        raise api.ApiError("not_found", "no such help request")
    return found


def _role(value) -> str:
    if not isinstance(value, str):
        raise _invalid("the role is an agent's name")
    return value[1:] if value.startswith("@") else value


def _set(store, assist_id: str, **fields) -> None:
    fields["updated"] = store._clock()
    names = ", ".join(f"{k}=?" for k in fields)
    store._conn.execute(f"UPDATE assists SET {names} WHERE id=?", (*fields.values(), assist_id))


def event(store, found: dict, what: str, actor: str, **extra) -> None:
    store._event(actor, f"assist.{what}", found["card"], {"assist": found["id"], **extra}, found["thread"])


# --- the lead asks ------------------------------------------------------------------------------
def _request(ctx, principal, request):
    from kd.cabinet import outcomes  # at call time: outcomes loads the API this module registers on

    agent = api._executor_agent(ctx, principal, request, coordinator=False)
    record = api._card(ctx, request.get("card"))
    catalog = load_catalog()
    kind = request.get("kind")
    if kind not in catalog:
        raise _invalid(f"kind is one of {', '.join(sorted(catalog))}")
    role = _role(request.get("to"))
    if role == agent:
        raise _refused("help is from another role: the card is yours, do this step yourself")
    if role not in catalog[kind]:
        raise _refused(f"{kind} is done by {', '.join('@' + r for r in sorted(catalog[kind]))}")
    step = _line(request.get("step"), "--step", STEP_MAX)
    ac = request.get("ac", 0)
    if type(ac) is not int or not 0 <= ac <= len(record.acceptance):
        raise _invalid(f"--ac is the number of one of the card's {len(record.acceptance)} criteria (0: none)")
    key = request.get("key")
    if not isinstance(key, str) or not KEY.fullmatch(key):
        raise _invalid("--key is 1..64 characters of letters, digits and . _ : -")
    session = request.get("session", "")
    if session != "" and (not isinstance(session, str) or not SESSION.fullmatch(session)):
        raise _invalid("session is the caller's session: 12 hex characters of its unit")
    if record.assignee != "@" + agent or record.status not in LEAD_STATUSES:
        raise _refused(f"help is asked by the card's lead on a card in work; {record.id} is {record.status}, "
                       f"held by {record.assignee or 'nobody'}")
    api._agent_rate_limit(ctx, agent, "assist")
    s = ctx.store
    row_key = hashlib.sha256(f"{record.id}\0{agent}\0{key}".encode("utf-8")).hexdigest()
    payload_sha = hashlib.sha256(json.dumps([kind, role, step, ac], ensure_ascii=False).encode("utf-8")).hexdigest()
    with s.transaction():
        command = outcomes.running_command(s, record, agent)
        if command is None or (session and not command["session"].startswith(session)):
            raise _refused(f"no turn of yours is running on {record.id}: ask for help from your turn on the card")
        known = s._conn.execute("SELECT id, payload_sha FROM assists WHERE key=?", (row_key,)).fetchone()
        if known is not None:
            if known["payload_sha"] != payload_sha:
                raise api.ApiError("conflict", f"--key {key} was used for another help request on {record.id}")
            return {"assist": row(s, known["id"]), "unchanged": True}
        assist_id = secrets.token_hex(16)
        if record.status == "Review":  # the lead found work not done: the card goes back to work (spec §6)
            plan = worker_board.plan("edit", {"status": "In Progress"}, agent=agent,
                                     card=boss_board.card_from(record.to_dict()), targets=ctx.targets(), specs=None)
            record = api._apply(ctx, plan, f"agent:{agent}", {"reason": f"help {assist_id[:8]}: {step}"})
        now = s._clock()
        basis = continuations.basis_of(s, record)
        s._conn.execute(
            "INSERT INTO assists (id, key, payload_sha, card, thread, lead, kind, role, step, ac, state, basis,"
            " signalled, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,'requested',?,?,?,?)",
            (assist_id, row_key, payload_sha, record.id, record.thread, agent, kind, role, step, ac,
             json.dumps(basis, ensure_ascii=False, sort_keys=True), now, now, now))
        found = row(s, assist_id)
        event(s, found, "requested", f"agent:{agent}", kind=kind, role=role, ac=ac)
        signal_boss(s, found, f"assist:{assist_id}:request")
        return {"assist": row(s, assist_id), "unchanged": False}


def signal_boss(store, found: dict, key: str) -> dict:
    return store._enqueue_in_tx({
        "key": key, "agent": "boss", "thread": found["thread"], "card": found["card"], "kind": "signal",
        "initiator": "system",
        "text": REQUEST_SIGNAL.format(card=found["card"], lead=found["lead"], short=found["id"][:8])})


# --- BOSS assigns -------------------------------------------------------------------------------
def _list(ctx, principal, request):
    api._executor_agent(ctx, principal, request, coordinator=True)
    rows = ctx.store._conn.execute(
        f"SELECT id FROM assists WHERE state IN ({','.join('?' * len(OPEN))}) ORDER BY created, rowid LIMIT 200",
        OPEN).fetchall()
    return {"assists": [row(ctx.store, r[0]) for r in rows]}


def _assign(ctx, principal, request):
    api._executor_agent(ctx, principal, request, coordinator=True)
    role = _role(request.get("to"))
    reason = _line(request.get("reason"), "--reason", REASON_MAX)
    catalog = load_catalog()
    s = ctx.store
    with s.transaction():
        found = _find(s, request)
        if found["state"] not in ("requested", "assigned"):
            raise _refused(f"help {found['id'][:8]} is {found['state']}: it is assigned only before its helper took it")
        if role == found["lead"]:
            raise _refused(f"@{role} is the card's lead: help comes from another role")
        if role not in catalog.get(found["kind"], ()):
            raise _refused(f"{found['kind']} is done by {', '.join('@' + r for r in sorted(catalog.get(found['kind'], ())))}")
        if not ctx.host_config().hosted(role):
            raise _refused(f"@{role} is not on the session host")
        if found["state"] == "assigned" and found["helper"] == role:
            return {"assist": found, "unchanged": True}
        _set(s, found["id"], state="assigned", helper=role)
        found = row(s, found["id"])
        event(s, found, "assigned", "agent:boss", helper=role, reason=reason)
        s._enqueue_in_tx({
            "key": f"assist:{found['id']}:help:{role}:{found['updated']}", "agent": role, "thread": found["thread"],
            "card": found["card"], "kind": "signal", "initiator": "agent:boss", "basis": found["basis"],
            "text": HELP_SIGNAL.format(card=found["card"], lead=found["lead"], short=found["id"][:8], id=found["id"])})
        return {"assist": found, "unchanged": False}


# --- the helper ---------------------------------------------------------------------------------
def _helper(ctx, principal, request) -> str:
    return api._executor_agent(ctx, principal, request, coordinator=False)


def _show(ctx, principal, request):
    agent = _helper(ctx, principal, request)
    s = ctx.store
    with s.transaction():
        found = _find(s, request)
        if found["helper"] != agent:
            raise _refused("this help request is not assigned to you")
        if found["state"] == "assigned":
            _set(s, found["id"], state="accepted")
            found = row(s, found["id"])
            event(s, found, "accepted", f"agent:{agent}")
    record = s.card(found["card"])
    ac_text = record.acceptance[found["ac"] - 1].text if 0 < found["ac"] <= len(record.acceptance) else ""
    return {"assist": found, "card": api.compact(record), "ac_text": ac_text}


def _reply(ctx, principal, request):
    agent = _helper(ctx, principal, request)
    state = request.get("state")
    if state not in ("done", "failed"):
        raise _invalid("--state is done or failed")
    reason = request.get("reason", "") or ""
    if state == "failed" and reason not in HELPER_REASONS:
        raise _invalid(f"a failed help names --reason: one of {', '.join(HELPER_REASONS)}")
    if state == "done" and reason:
        raise _invalid("--reason goes with --state failed")
    evidence = _line(request.get("evidence"), "--evidence", EVIDENCE_MAX)
    limit = _line(request.get("limit", ""), "--limit", LIMIT_MAX, empty=state == "failed")
    plan_id = request.get("card", "") or ""
    if plan_id and (state != "done" or not isinstance(plan_id, str) or not api.CARD.fullmatch(plan_id)):
        raise _invalid("--card KD-<m> goes with --state done: the card you opened for this help")
    api._agent_rate_limit(ctx, agent, "assist")
    s = ctx.store
    with s.transaction():
        found = _find(s, request)
        if found["helper"] != agent:
            raise _refused("this help request is not assigned to you")
        plan = _plan_card(s, found, plan_id, agent) if plan_id else None
        # an answer after the request was withdrawn or expired is kept once, as evidence; it resumes nothing
        late = found["state"] == "cancelled" or (found["state"] == "failed" and found["reason"] == "expired")
        if late and found["evidence"]:
            if (found["evidence"], found["proof_limit"]) == (evidence, limit):
                return {"assist": found, "unchanged": True, "late": True}
            raise _refused(f"help {found['id'][:8]} has its late answer already")
        if found["state"] in ("done", "failed") and not late:
            if (found["state"], found["reason"], found["evidence"], found["proof_limit"]) == (
                    state, reason, evidence, limit):
                return {"assist": found, "unchanged": True, "late": False}
            raise _refused(f"help {found['id'][:8]} is answered already")
        payload = {"assist": found["id"], "state": state, "reason": reason, "limit": limit, "source": "reported",
                   "late": late, **({"card": plan.id} if plan is not None else {})}
        s._add_message(found["thread"], f"agent:{agent}", "assist", evidence, payload, "", via="assist")
        if late:
            _set(s, found["id"], evidence=evidence, proof_limit=limit)
            found = row(s, found["id"])
            event(s, found, "late", f"agent:{agent}", state=state, reason=reason)
            return {"assist": found, "unchanged": False, "late": True}
        _set(s, found["id"], state=state, reason=reason, evidence=evidence, proof_limit=limit)
        found = row(s, found["id"])
        # before the done event: the lead's wait is the plan card's from here on. A lead that has not ended its
        # turn on the help yet waits on the card when it does (``plan_of``, ``outcomes._assist_waits``)
        followed = _follow(ctx, found, plan) if plan is not None else False
        event(s, found, state, f"agent:{agent}", reason=reason, **({"card": plan.id} if plan is not None else {}))
        if state == "failed":
            _failed_problem(ctx, found)
        return {"assist": found, "unchanged": False, "late": False, "followed": followed}


def plan_of(store, found: Mapping) -> str:
    """The plan card a done ``server_change`` help was answered with (``--card``), or ''."""
    if found["state"] != "done" or found["kind"] != "server_change":
        return ""
    for (payload,) in store._conn.execute(
            "SELECT payload FROM events WHERE kind='assist.done' AND card=? ORDER BY seq DESC", (found["card"],)):
        data = json.loads(payload)
        if data.get("assist") == found["id"]:
            return data.get("card") or ""
    return ""


def _plan_card(store, found: dict, plan_id: str, agent: str):
    """The card a ``server_change`` helper opened for the help: its own card — created by it after the help was
    asked and still held by it — another card than the lead's, still open. A mistyped number is refused: it would
    move the lead's wait onto a card that has nothing to do with the rollout."""
    if found["kind"] != "server_change":
        raise _refused(f"help {found['id'][:8]} is a read ({found['kind']}): no card of yours goes with it")
    try:
        plan = store.card(plan_id)
    except store_mod.NotFound:
        raise api.ApiError("not_found", f"{plan_id} not found")
    mine = plan.created_by == f"@{agent}" and plan.assignee == f"@{agent}" and plan.created >= found["created"]
    if plan.id == found["card"] or plan.terminal or not mine:
        raise _refused(f"--card names the open card you opened for this help (kd-task create after it was asked, "
                       f"held by @{agent}), not {plan.id} ({plan.status}, {plan.assignee or 'nobody'})")
    return plan


def _follow(ctx, found: dict, plan) -> bool:
    """A rollout whose plan waits for the owner's signature has no outcome yet: the lead's wait on the help goes
    on as a wait on the helper's card (its Done wakes the lead once, a cancel asks BOSS to route it), and the
    lead's card is Blocked on that card — what the lead did by hand on such an answer before (KD-9460). A lead
    that does not wait on this help reads the answer itself; its card is left as it is."""
    from kd.cabinet import waits  # at call time: it loads the API this module registers on

    s = ctx.store
    if not waits.follow(s, found["card"], "assist", found["id"], "card", plan.id):
        return False
    record = s.card(found["card"])
    deps = list(record.dependencies) + ([] if plan.id in record.dependencies else [plan.id])
    change = {"dep": deps, **({"status": "Blocked"} if record.status != "Blocked" else {})}
    try:
        with s.transaction():  # a savepoint: a refused edit leaves nothing of its own behind
            edit = worker_board.plan("edit", change, agent=found["lead"],
                                     card=boss_board.card_from(record.to_dict()), targets=ctx.targets(), specs=None)
            api._apply(ctx, edit, f"agent:{found['lead']}",
                       {"reason": f"help {found['id'][:8]}: waits for {plan.id}"})
    except (worker_board.Refused, boss_board.Refused, api.ApiError):
        pass  # the wait holds the card already (``outcomes.next_state``); the status is only the board's view
    return True


def _failed_problem(ctx, found: dict) -> None:
    """A help that ended without a result: the lead is woken by its wait; BOSS sorts out the cause (spec §8.3)."""
    from kd.cabinet import problems

    problems.raise_problem(ctx, key=f"assist-failed:{found['id']}", record=ctx.store.card(found["card"]),
                           kind="assist_failed", agent=found["lead"],
                           detail={"assist": found["id"], "reason": found["reason"]})


# --- the card moved on, time passed (the pipeline) ----------------------------------------------
WORK = ("To Do", "In Progress", "Blocked")
RESIGNAL_AFTER, RESIGNAL_EVERY = timedelta(minutes=10), timedelta(minutes=30)
STALLED_AFTER, EXPIRES_AFTER = timedelta(minutes=60), timedelta(hours=24)


def _cancel_reason(store, record, found: dict) -> Optional[str]:
    if record.terminal:
        return "card_closed"
    if record.assignee != "@" + found["lead"]:
        return "reassigned"
    if record.status not in WORK:
        return "withdrawn"
    if briefs.scope_sha(briefs.scope(record))[:16] != found["basis"].get("scope"):
        return "scope_changed"
    return None


def cancel_for_card(ctx, card_id: str) -> int:
    """The card's open help its lead no longer needs — the card closed, went to someone else, left its work or
    changed its scope — becomes ``cancelled``; how many. The helper's command drops by its ``basis``."""
    s = ctx.store
    found_all = open_for(s, card_id)
    if not found_all:
        return 0
    record = s.card(card_id)
    done = 0
    for found in found_all:
        why = _cancel_reason(s, record, found)
        if why:
            _set(s, found["id"], state="cancelled", reason=why)
            event(s, row(s, found["id"]), "cancelled", "system", reason=why)
            done += 1
    return done


def sweep(ctx) -> dict:
    """What time decides (inside ``pipeline.sweep``): an unassigned request is signalled to BOSS again, an assigned
    or taken one without an answer for an hour is a problem, any open one past a day fails ``expired``."""
    from kd.cabinet import users  # at call time: it loads the API this module registers on

    s, now = ctx.store, users.parse_time(ctx.now())
    out = {"resignalled": 0, "stalled": 0, "expired": 0}
    rows = s._conn.execute(
        f"SELECT id FROM assists WHERE state IN ({','.join('?' * len(OPEN))}) ORDER BY created LIMIT 500",
        OPEN).fetchall()
    for (assist_id,) in rows:
        found = row(s, assist_id)
        if now - users.parse_time(found["created"]) >= EXPIRES_AFTER:
            _set(s, assist_id, state="failed", reason="expired")
            found = row(s, assist_id)
            event(s, found, "failed", "system", reason="expired")
            _failed_problem(ctx, found)
            out["expired"] += 1
        elif found["state"] == "requested" and now - users.parse_time(found["signalled"]) >= (
                RESIGNAL_AFTER if found["signalled"] == found["created"] else RESIGNAL_EVERY):
            stamp = s._clock()
            signal_boss(s, found, f"assist:{assist_id}:request:{stamp}")
            _set(s, assist_id, signalled=stamp)
            out["resignalled"] += 1
        elif found["state"] in ("assigned", "accepted") and now - users.parse_time(found["updated"]) >= STALLED_AFTER:
            from kd.cabinet import problems  # an assigned one: its helper never took it (a failed or dropped wake)

            if problems.raise_problem(ctx, key=f"assist-stalled:{assist_id}:{found['state']}",
                                      record=s.card(found["card"]),
                                      kind="assist_stalled", agent=found["lead"], detail={"assist": assist_id}):
                out["stalled"] += 1
    return out


OPS = {
    "assist.request": (frozenset({"executor"}), _request),
    "assist.list": (frozenset({"executor"}), _list),
    "assist.assign": (frozenset({"executor"}), _assign),
    "assist.show": (frozenset({"executor"}), _show),
    "assist.reply": (frozenset({"executor"}), _reply),
}
api.OPS.update(OPS)  # the board API serves it (api imports this module at its end)
