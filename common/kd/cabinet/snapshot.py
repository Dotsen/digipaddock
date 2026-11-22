"""A card's operational snapshot: the facts a coordinator acts on, read from the cabinet's own records.

``facts`` gathers them uncompressed: the card's head, what it asks for (a hash and a size, not the text), the
newest request and its session, the open and answered decisions, the PRs, the last dev.check and the last
release notice, the dependencies and the children, the result so far and the significant messages of the thread — the agent's tool
traffic is not among them — and the pipeline's records: the last outcome, the waits, the problems.
``next_step`` says what moves the card and whose move it is: from a record when one says it (``recorded``: an open
problem, a blocking recorded wait), else derived from the facts.
"""

import copy
import hashlib
import json
from typing import Callable, List, Optional

from kd import council as _council
from kd.cabinet import api, briefs, councils, decisions, devchecks, llm_api, outcomes, overview, prs_api, result, view
from kd.cabinet.store import NotFound

Visible = Callable[[str], bool]  # may the reader see a card of this project

V = 1
LIMIT = 8192  # bytes of UTF-8: every form of the snapshot an agent reads fits in this
MESSAGES_SHOWN = 8
MESSAGE_CUT = 700  # bytes of one message's text
LIST_SHOWN = 10
TITLE_CUT = 100  # characters
SUMMARY_CUT = 400  # bytes
SUMMARY_FIT = 100  # bytes, when the summary is what the fit cuts
ANSWERED_SHOWN = 3
PRS_SHOWN = 3
LABELS_CUT = 300  # bytes of the labels joined by ", "; the rest is counted as "+N"
WHY_CUT = 400  # bytes of the release's reason; the notice itself reads on
HOST_CUT = 100  # bytes
NOTE_CUT = 300  # bytes of the outcome's note
WAITS_SHOWN = 5  # recorded waits; the rest reads kd-task waits
PROBLEMS_SHOWN = 3
FORMATS = ("text", "json")
SIGNIFICANT = ("text", "decision", "directive", "diagnostic", "notice", "file", "assist")
FAILED_KEYS = ("sys.session.failed", "sys.session.failed_restart", "sys.session.auth_failed",
               "sys.session.no_key", "sys.session.model_disabled", "sys.session.host_outdated")
ACTIVE = ("queued", "delivered", "started")
ENDED = ("done", "cancelled")
RUNNING = ("working", "queued", "delayed", "stale")
BRIEF_WRITING = ("queued", "in_progress", "waiting_user")
NEXT = (
    "done", "wait_decision", "brief_writing", "brief_ready", "brief_failed", "inbox", "run_working", "run_queued",
    "run_delayed", "run_stale", "run_failed", "review", "wait_deps", "blocked_unexplained", "no_request",
    "turn_ended", "not_started", "missing_next_step", "continuation_limit", "wait_needs_routing", "outcome_refused",
    "wait_deps", "wait_help", "assist_failed", "assist_stalled",
    "wait_stages", "stage_missing", "stage_cancelled", "stage_reopened", "release_degraded", "stage_review",
    "stage_gate",
    "council_failed", "council_run_failed", "council_stalled", "council_unanswered",
    "council_running", "council_writer",
)
PROBLEM_TEXT = {
    "stage_missing": "an approved stage has no child card ({since}): {by} tells the owner",
    "stage_cancelled": "a stage's card was closed without the work ({since}): {by} decides the plan's hole",
    "stage_reopened": "a closed stage of the chain was reopened ({since}): {by} pauses the stages that build on it",
    "release_degraded": "the release of this card is degraded ({since}): {agent} holds it, {by} checks on it",
    "missing_next_step": "{agent}'s turn ended {since} with no next step recorded: {by} routes it",
    "continuation_limit": "{agent}'s automatic continuations stopped at their limit {since}: {by} decides",
    "wait_needs_routing": "what {agent} waits for is gone or has no real result ({since}): {by} routes it",
    "outcome_refused": "the outcome {agent} recorded could not apply ({since}): {by} decides",
    "assist_failed": "help {agent} asked for ended without a result ({since}): {by} sorts out the cause",
    "assist_stalled": "help {agent} asked for has no answer ({since}): {by} checks on it",
    "council_failed": "no critic of the council could start ({since}): the work goes on without it, {by} checks why",
    "council_run_failed": "a critic of the council failed ({since}): {by} checks why",
    "council_stalled": "{agent} has not answered the council's findings ({since}): {by} checks on it",
    "council_unanswered": "{agent} handed the work in with council findings unanswered ({since}): {by} decides",
}
HELP_SHOWN = 5
STEP_CUT = 200  # bytes of a help request's step
DONE = "Done"
CANCELLED = "Cancelled"


def significant(message: dict) -> bool:
    """A message a reader of the card needs: words, decisions and notices, and a session's failure."""
    if message["kind"] in SIGNIFICANT:
        return True
    return message["kind"] == "status" and (message.get("payload") or {}).get("key") in FAILED_KEYS


def step_facts(ctx, record, visible: Optional[Visible] = None, pipeline: Optional[dict] = None) -> dict:
    """The facts ``next_step`` reads, and only those: the card's head, its brief, the newest request,
    the open decisions, the dependencies and the pipeline's records (``pipeline`` when the caller has them) — cheap
    enough for every read of the card's page."""
    seen = visible or (lambda project: True)
    store = ctx.store
    card = record.id
    brief = briefs.state(store, record)
    reading = councils.open_facts(store, card, "code")  # the council its pull request is with
    return {
        **({"code_council": reading} if reading else {}),
        "card": {
            "id": card, "title": record.title, "status": record.status, "assignee": record.assignee,
            "project": record.project, "revision": record.revision, "updated": record.updated,
            "labels": list(record.labels), "parent": record.parent,
            **({"outcome": record.outcome, "canonical": record.canonical} if record.outcome else {}),
        },
        "scope": {"brief": {"state": brief["state"], "version": brief["version"], "approved": brief["approved"],
                            **({"council": brief["council"]} if "council" in brief else {})}},
        "run": _run(ctx, card),
        "queued": sum(1 for c in store.commands(card=card, states=ACTIVE) if c["kind"] != "stop"),
        "waits": _waits(store, card),
        "deps": _deps(store, record, seen),
        "pipeline": outcomes.card_facts(ctx, record) if pipeline is None else pipeline,
    }


def facts(ctx, record, visible: Optional[Visible] = None) -> dict:
    """The uncompressed snapshot of ``record`` (a ``CardRecord``) in the store of ``ctx``; with ``visible`` the
    titles of dependencies and the children are only of cards the reader may see."""
    seen = visible or (lambda project: True)
    store = ctx.store
    card = record.id
    step = step_facts(ctx, record, visible)
    scope = view.scope_text(record.to_dict()).encode("utf-8")
    model, effort = llm_api.effective(store, record)  # what the card's sessions run on
    gathered = councils.facts(store, card)  # the card's last council, of any kind
    return {
        **({"council_round": gathered} if gathered is not None else {}),
        **({"code_council": step["code_council"]} if "code_council" in step else {}),  # for ``next_step`` only
        "v": V,
        "card": step["card"],
        "model": None if model is None else {k: model[k] for k in ("id", "provider", "title", "enabled")},
        "effort": effort,
        "council": councils.effective(store, record),  # who criticises its brief and code, {} — nobody
        "scope": {"sha": hashlib.sha256(scope).hexdigest()[:12], "bytes": len(scope),  # the cursor's
                  "brief": step["scope"]["brief"]},
        "iteration": len(result.reviews(store, card)),
        "watermark": store.last_seq(),
        "run": step["run"],
        "queued": step["queued"],
        "waits": step["waits"],
        "prs": _prs(ctx, card),
        "checks": _checks(store, card),
        "release": _release(store, card),
        "deps": step["deps"],
        "children": _cards([{"id": c.id, "status": c.status, "title": c.title} for c in store.cards(parent=card)
                            if seen(c.project)]),
        "result": {
            "ac_checked": sum(1 for a in record.acceptance if a.checked),
            "ac_total": len(record.acceptance),
            "summary": record.summary,
        },
        "messages": _messages(store.messages(card)),
        "pipeline": step["pipeline"],
    }


def _run(ctx, card: str) -> Optional[dict]:
    run = overview.runs_of(ctx.store, [card]).get(card)
    if run is None:
        return None
    return {
        "command": run["command"], "kind": run["kind"], "agent": run["agent"], "state": run["state"],
        "at": run["updated"], "session": run["session"] or "", "run": overview.run_state(run, ctx.now()),
        "reason": run.get("reason") or "",
    }


def _checks(store, card: str) -> dict:
    """The newest dev.check of the card; the older ones are counted (``kd-cabinet devcheck list --card``)."""
    found = devchecks.of_card(store, card)
    items = [{"id": op["id"], "state": op["state"], "profiles": list(op["params"]["profiles"]),
              "head": op["params"]["head"], "pr": op["params"]["pr"], "class": op["result"].get("class") or "",
              "log": op["result"].get("log") or ""} for op in found[:1]]
    return {"items": items, "total": len(found)}


def _waits(store, card: str) -> dict:
    """The card's decisions: the open ones with whether an agent asked for each and whether a person's question on
    it waits for that agent's answer (``asked``), the PR's number; the answered ones newest first."""
    ledger = decisions.Decisions(store)
    found = ledger.list(card=card)
    open_ = [{"decision": d["id"], "type": d["type"], "by": view.who(d["requested_by"]), "since": d["created"],
              "agent": d["requested_by"].startswith("agent:"), "asked": ledger.asked_open(d),
              **({"number": d["payload"].get("number")} if d["type"] == "pr_approve" else {})}
             for d in found if d["state"] == "open"]
    answered = sorted((d for d in found if d["state"] != "open"), key=lambda d: d["decided_at"] or d["created"],
                      reverse=True)
    return {
        "open": open_,
        "answered": [{"decision": d["id"], "type": d["type"], "state": d["state"], "by": view.who(d["decided_by"]),
                      "at": d["decided_at"]} for d in answered],
    }


def _prs(ctx, card: str) -> List[dict]:
    out = []
    for pr in reversed(ctx.store.prs(card=card)):
        approved = [d for d in prs_api.decisions_of(ctx, card, pr["repo"], pr["number"], state="approved")
                    if d["payload"].get("head") == pr["head"]]
        out.append({
            "number": pr["number"], "repo": pr["repo"], "state": pr["state"], "head": pr["head"],
            "approved_head": bool(approved), "mode": ((approved[-1]["answer"] or {}).get("mode") or "") if approved else "",
        })
    return out


def _release(store, card_id: str) -> Optional[dict]:
    """The release's state from the facts (``kd.cabinet.releases``); None without a release notice."""
    from kd.cabinet import releases

    found = releases.state(store, card_id)
    if found["state"] == "none":
        return None
    found.pop("operations", None)  # the operations are the waits' and the result's; the line names the state
    return found


def _deps(store, record, seen: Visible) -> dict:
    """All the dependencies count (they decide the next step); only the reader's own are listed."""
    items, statuses = [], []
    for dep in record.dependencies:
        try:
            found = store.card(dep)
        except NotFound:
            statuses.append("missing")
            items.append({"id": dep, "status": "missing", "title": ""})
            continue
        statuses.append(found.status)
        if seen(found.project):
            items.append({"id": dep, "status": found.status, "title": found.title})
    return {"total": len(statuses), "items": items, "open": sum(1 for s in statuses if s != DONE)}


def _cards(items: List[dict]) -> dict:
    return {"total": len(items), "items": items}


def _messages(messages: List[dict]) -> dict:
    found = [m for m in messages if significant(m)]
    return {
        "total": len(found),
        "items": [{"seq": m["seq"], "who": view.who(m["author"]), "kind": m["kind"], "at": m["created"],
                   "text": view.message_text(m, with_files=False),
                   "files": [view.file_line(f) for f in view.files_of(m)]} for m in reversed(found)],
    }


def _step(source, what, who, since, text, params=None, decision=None) -> dict:
    """One next step: ``params`` the page words it with, ``decision`` (``{id, type}``) the open decision it is."""
    out = {"source": source, "what": what, "who": who, "since": since, "text": text, "params": params or {}}
    if decision is not None:
        out["decision"] = {"id": decision["decision"], "type": decision["type"]}
    return out


DECISION_RANK = {"pr_approve": 2, "promote": 3}


def _first_decision(open_: List[dict]) -> dict:
    """The open decision that moves the card first: a person's question on it its agent has not answered
    (the agent's move), then an agent's question, a PR, BOSS's proposal, the rest — the oldest first in each."""
    def rank(d):
        if d["asked"]:
            return 0
        if d["type"] == "question":
            return 1 if d["agent"] else 4
        return DECISION_RANK.get(d["type"], 4)
    return min(open_, key=rank)  # the first of the lowest rank: the list is oldest first


def _decision_step(source, d: dict, since: str, text: Optional[str] = None) -> dict:
    params = {"agent": d["by"]} if d["asked"] or (d["type"] == "question" and d["agent"]) else {}
    if d["type"] == "pr_approve":
        params["number"] = d.get("number")
    if d["asked"]:
        return _step(source, "asked", d["by"], since,
                     f"{d['by']} answers the owner's question on {d['type']} {d['decision']}", params, d)
    return _step(source, "wait_decision", "@owner", since,
                 text or f"waits for the owner's answer to {d['type']} {d['decision']} since {d['since']}", params, d)


def next_step(found: dict) -> dict:
    """What moves the card and whose move it is, derived from ``facts`` (or ``step_facts``): the first rule that
    matches. The agents' snapshot and the card's page read this one step."""
    card, run, waits = found["card"], found["run"], found["waits"]
    status = card["status"]
    brief = found["scope"]["brief"]

    def step(what, who, since, text, params=None):
        return _step("derived", what, who, since, text, params)

    if status == DONE:
        return step("done", "-", card["updated"], "done")
    if status == CANCELLED:  # closed without the work: nothing moves it but the owner's restore
        why = f"duplicate of {card.get('canonical')}" if card.get("outcome") == "duplicate" else "cancelled"
        return step("closed", "-", card["updated"], f"closed without the work: {why}")
    recorded = _recorded(found)
    if recorded is not None:
        return recorded
    defer = (found.get("pipeline") or {}).get("defer")
    if defer:  # put off by the owner or BOSS: the time moves it, or the owner's "Wake now"
        return step("deferred", "-", defer["created"], f"put off until {defer['until']} by {view.who(defer['by'])}",
                    {"until": defer["until"]})
    staged = (found.get("pipeline") or {}).get("stages")
    if staged and staged["done"] < staged["total"] and status not in ("Review",):
        # an aggregate: its stages move it, nobody owes it a turn
        open_ = ", ".join(f"{i['key']} {i['child']} {i['status']}" for i in staged["items"] if i["status"] != DONE)
        return step("wait_stages", "-", card["updated"],
                    f"waits for stages {staged['done']} of {staged['total']} done: {open_}")
    if waits["open"]:
        first = _first_decision(waits["open"])
        return _decision_step("derived", first, first["since"])
    state = run["run"] if run else "none"
    reading = found.get("code_council")
    if reading and state not in ("stale", "failed"):  # its pull request is with the council
        return _council_step("derived", reading, card, card["updated"])
    if status == "Inbox" and state not in ("stale", "failed"):  # a dead session is a run's fact, not the brief's
        gathered = brief.get("council") or {}
        if brief["state"] in BRIEF_WRITING + ("in_council",) and gathered.get("state") == "needs_writer":
            return step("council_writer", briefs.ANALYST, card["updated"],
                        f"{briefs.ANALYST} answers the council: round {gathered['round']} of {gathered['rounds']}, "
                        f"{gathered['open_findings']} findings unanswered",
                        {"round": gathered["round"], "rounds": gathered["rounds"],
                         "findings": gathered["open_findings"]})
        if brief["state"] in BRIEF_WRITING:
            return step("brief_writing", briefs.ANALYST, card["updated"], f"{briefs.ANALYST} writes the brief")
        if brief["state"] == "in_council":  # the critics read: nobody's move till the round ends
            return step("council_running", "-", card["updated"],
                        f"the council reads the brief: round {gathered['round']} of {gathered['rounds']}, "
                        f"{gathered['done']} of {gathered['total']} critics done",
                        {key: gathered[key] for key in ("round", "rounds", "done", "total")})
        if brief["state"] == "ready":
            return step("brief_ready", "@owner", card["updated"],
                        f"brief v{brief['version']} is ready: the owner approves it to start",
                        {"version": brief["version"]})
        if brief["state"] == "failed":
            return step("brief_failed", "@boss", card["updated"], "the brief's preparation stopped: retry it")
        return step("inbox", "@boss", card["updated"], "in Inbox: the coordinator proposes it or asks for a brief")
    if state in RUNNING:
        who = "@" + run["agent"]
        text = {
            "working": f"{who} works on it ({run['kind']})",
            "queued": f"{who}'s {run['kind']} waits in the queue",
            "delayed": f"{who}'s {run['kind']} was delivered and has not started",
            "stale": f"{who}'s {run['kind']} started, no heartbeat confirms it works",
        }[state]
        return step("run_" + state, who, run["at"], f"{text} since {run['at']}")
    if state == "failed":
        return step("run_failed", "@boss", run["at"],
                    f"@{run['agent']}'s {run['kind']} failed {run['at']}: the coordinator decides a retry")
    if status == "Review":
        gate = (found.get("pipeline") or {}).get("stage") or {}  # a chain plan's stage: its gate comes first
        if gate.get("gate") == "reviewing":
            return step("stage_review", "-", card["updated"],
                        "handed in: the code review of its commits runs, the coordinator decides on its verdict")
        if gate.get("gate") in ("ready", "fixes", "skipped"):
            job = f" (job {gate['job']})" if gate.get("job") else ""  # what stage-return names
            return step("stage_gate", "@boss", card["updated"],
                        f"handed in, review {gate['verdict'] or 'none'}{job}: the coordinator passes it, returns "
                        "it or holds it for the owner", {"verdict": gate["verdict"] or "none"})
        if gate.get("gate") == "held":
            return step("review", "@owner", card["updated"],
                        f"in Review, held ({gate['reason']}): the owner accepts it or returns it")
        return step("review", "@owner", card["updated"], "in Review: the owner accepts it or returns it")
    if status == "Blocked":
        if found["deps"]["open"]:
            return step("wait_deps", "-", card["updated"], f"waits for {found['deps']['open']} open dependencies")
        return step("blocked_unexplained", "@boss", card["updated"],
                    "Blocked with no open dependency and no open question: the reason is not recorded")
    if status == "In Progress" and run is None:
        return step("no_request", "@boss", card["updated"],
                    "In Progress with no request recorded for any agent — nobody is on it")
    if run and run["state"] in ENDED and not found["queued"]:
        agent = {"agent": "@" + run["agent"]}
        if run["state"] == "cancelled":
            return step("turn_ended", "-", run["at"],
                        f"the last request was cancelled ({run.get('reason') or 'no reason'}) {run['at']}, "
                        "no continuation recorded — nobody is on it", agent)
        return step("turn_ended", "-", run["at"],
                    f"turn ended {run['at']}, no continuation recorded — nobody is on it", agent)
    who = card["assignee"] or "-"
    return step("not_started", who, card["updated"], f"{who} has not started it")


def _council_step(source: str, gathered: dict, card: dict, since: str) -> dict:
    """The card's pull request is with its council of the code: the critics read — nobody's move till
    the round ends — or the card's holder answers the findings."""
    if gathered["state"] == "needs_writer":
        who = card["assignee"] or "-"
        return _step(source, "council_writer", who, since,
                     f"{who} answers the council: round {gathered['round']} of {gathered['rounds']}, "
                     f"{gathered['open_findings']} findings unanswered",
                     {"round": gathered["round"], "rounds": gathered["rounds"],
                      "findings": gathered["open_findings"]})
    return _step(source, "council_running", "-", since,
                 f"the council reads pull request #{gathered['number']}: round {gathered['round']} of "
                 f"{gathered['rounds']}, {gathered['done']} of {gathered['total']} critics done",
                 {key: gathered[key] for key in ("round", "rounds", "done", "total")})


def _recorded(found: dict) -> Optional[dict]:
    """The next step a record says: an open problem (the coordinator's, or the owner's on the coordinator's own
    card), else the blocking waits the last outcome recorded (the owner's answer, or nobody's: a dependency)."""
    pipeline = found.get("pipeline") or {}
    outcome = pipeline.get("outcome") or {}
    if pipeline.get("problems"):
        first = pipeline["problems"][0]
        who = "@owner" if found["card"]["assignee"] == "@boss" else "@boss"
        by = "the owner" if who == "@owner" else "the coordinator"
        text = PROBLEM_TEXT[first["kind"]].format(agent=first["agent"] or "the agent", since=first["created"], by=by)
        return _step("recorded", first["kind"], who, first["created"], text, {"agent": first["agent"]})
    blocking = [w for w in pipeline.get("waits", []) if w["blocking"] and w["state"] == "open"]
    if not blocking:
        return None
    since = outcome.get("created") or found["card"]["updated"]
    reading = found.get("code_council")
    if reading and all(w["kind"] == "council" for w in blocking):  # the turn waits for its council
        return _council_step("recorded", reading, found["card"], since)
    asks = [w["ref"] for w in blocking if w["kind"] == "decision"]
    helped = [w["assist"] for w in blocking if w["kind"] == "assist" and w.get("assist")]
    if helped and not asks:  # help inside the approved task: BOSS assigns it, then its helper does it
        first = helped[0]
        who = "@boss" if first["state"] == "requested" else first["helper"] or first["role"]
        text = (f"waits for help of {first['helper'] or first['role']} ({first['state']}): "
                + (outcome.get("note") or first["step"]))
        return _step("recorded", "wait_help", who, since, text)
    text = outcome.get("note") or f"waits for {len(blocking)} recorded"
    if all(w["kind"] == "time" for w in blocking):  # put off by the agent itself until its moment
        return _step("recorded", "deferred", "-", since, f"put off until {blocking[0]['ref']}: {text}",
                     {"until": blocking[0]["ref"]})
    if not asks:
        return _step("recorded", "wait_deps", "-", since, text)
    waited = [d for d in found["waits"]["open"] if d["decision"] in asks]
    if waited:  # the recorded decision, named as the page names it
        return _decision_step("recorded", _first_decision(waited), since, text)
    return _step("recorded", "wait_decision", "@owner", since, text)


# --- the snapshot in LIMIT bytes -------------------------------------------------------------------------
def _cut_bytes(text: str, limit: int, tail: str) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", "ignore") + tail


def _cut_title(title: str) -> str:
    return title if len(title) <= TITLE_CUT else title[:TITLE_CUT] + "…"


def _cut_labels(labels: List[str]) -> List[str]:
    shown, used = [], 0
    for label in labels:
        cost = len(label.encode("utf-8")) + 2
        if used + cost > LABELS_CUT:
            return shown + [f"+{len(labels) - len(shown)}"]
        shown.append(label)
        used += cost
    return shown


def build(ctx, record, visible: Optional[Visible] = None) -> dict:
    """``facts`` with the derived next step, each list and long field at its own cap, the exact commands that
    read what is not shown (``more``), fitted into ``LIMIT - 1`` bytes of JSON — the line a reader prints ends
    with a newline; what the fit cut is named in ``trimmed``."""
    snap = facts(ctx, record, visible)
    card = record.id
    pipeline = snap["pipeline"]
    if pipeline["outcome"]:
        pipeline["outcome"]["note"] = _cut_bytes(pipeline["outcome"]["note"], NOTE_CUT, "…")
    snap["next"] = next_step(snap)
    snap.pop("code_council", None)  # ``council_round`` says it to the reader
    snap["card"]["title"] = _cut_title(snap["card"]["title"])
    snap["card"]["labels"] = _cut_labels(snap["card"]["labels"])
    if snap["release"]:
        release = snap["release"]
        release["why"] = _cut_bytes(release["why"], WHY_CUT, f"… (kd-task message {card} {release['seq']})")
        release["host"] = _cut_bytes(release["host"], HOST_CUT, "…")
    items = snap["messages"]["items"][:MESSAGES_SHOWN]
    for message in items:
        message["text"] = _cut_bytes(message["text"], MESSAGE_CUT, f"… (kd-task message {card} {message['seq']})")
    snap["messages"]["items"] = items
    for name in ("deps", "children"):
        snap[name]["items"] = [{**i, "title": _cut_title(i["title"])} for i in snap[name]["items"][:LIST_SHOWN]]
    snap["waits"]["answered"] = snap["waits"]["answered"][:ANSWERED_SHOWN]
    snap["prs"] = snap["prs"][:PRS_SHOWN]
    snap["result"]["summary"] = _cut_bytes(snap["result"]["summary"], SUMMARY_CUT, f"… (kd-task result {card})")
    snap["more"] = {"scope": f"kd-task scope {card}", "result": f"kd-task result {card}"}
    if snap.get("council_round"):
        snap["more"]["council"] = f"kd-work council {card}"  # the rounds, the findings, the answers
    pipeline["problems_total"] = len(pipeline["problems"])
    pipeline["problems"] = pipeline["problems"][:PROBLEMS_SHOWN]
    pipeline["waits"] = pipeline["waits"][:WAITS_SHOWN]
    if pipeline["waits_total"] > len(pipeline["waits"]):
        snap["more"]["waits"] = f"kd-task waits {card}"
    _point_to_older(snap)
    for name, command in (("deps", f"kd-task list --deps-of {card}"), ("children", f"kd-task list --parent {card}")):
        if snap[name]["total"] > len(snap[name]["items"]):
            snap["more"][name] = command
    snap["trimmed"] = []
    return fit(snap, LIMIT - 1)


def _point_to_older(snap: dict) -> None:
    """``more.thread``: the command that reads the messages older than the shown ones, when there are any."""
    card, messages = snap["card"]["id"], snap["messages"]
    if messages["total"] <= len(messages["items"]):
        snap["more"].pop("thread", None)
    elif messages["items"]:
        snap["more"]["thread"] = f"kd-task thread {card} --before {messages['items'][-1]['seq']}"
    else:
        snap["more"]["thread"] = f"kd-task thread {card}"


def _drop_message(snap: dict) -> bool:
    if not snap["messages"]["items"]:
        return False
    snap["messages"]["items"].pop()
    _point_to_older(snap)
    return True


def _drop_item(name: str, command: str) -> Callable[[dict], bool]:
    def drop(snap: dict) -> bool:
        if not snap[name]["items"]:
            return False
        snap[name]["items"].pop()
        snap["more"][name] = command.format(card=snap["card"]["id"])
        return True
    return drop


def _drop_recorded(snap: dict) -> bool:
    """Recorded waits past the first: the next step names what blocks; kd-task waits reads them all."""
    if len(snap["pipeline"]["waits"]) <= 1:
        return False
    snap["pipeline"]["waits"].pop()
    snap["more"]["waits"] = f"kd-task waits {snap['card']['id']}"
    return True


def _drop_answered(snap: dict) -> bool:
    if not snap["waits"]["answered"]:
        return False
    snap["waits"]["answered"].pop()
    return True


def _cut_summary(snap: dict) -> bool:
    summary = snap["result"]["summary"]
    cut = _cut_bytes(summary, SUMMARY_FIT, f"… (kd-task result {snap['card']['id']})")
    if len(cut.encode("utf-8")) >= len(summary.encode("utf-8")):
        return False
    snap["result"]["summary"] = cut
    return True


def _drop_open(snap: dict) -> bool:
    """The last resort: open decisions past the first — the next step already names the oldest one."""
    if len(snap["waits"]["open"]) <= 1:
        return False
    snap["waits"]["open"].pop()
    return True


TRIMS = (
    ("messages", _drop_message),
    ("children", _drop_item("children", "kd-task list --parent {card}")),
    ("deps", _drop_item("deps", "kd-task list --deps-of {card}")),
    ("answered", _drop_answered),
    ("recorded", _drop_recorded),
    ("summary", _cut_summary),
    ("open", _drop_open),
)


def _size(snap: dict) -> int:
    return len(json.dumps(snap, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _shrink(snap: dict, size: Callable[[dict], int], limit: int) -> dict:
    out = copy.deepcopy(snap)
    out.setdefault("trimmed", [])
    for name, trim in TRIMS:
        while size(out) > limit and trim(out):
            if name not in out["trimmed"]:
                out["trimmed"].append(name)
    return out


def fit(snap: dict, limit: int = LIMIT) -> dict:
    """``snap`` cut in ``TRIMS`` order (the oldest message first) until its compact JSON is ``limit`` bytes."""
    return _shrink(snap, _size, limit)


def _kb(size: int) -> str:
    return f"{size} B" if size < 1024 else f"{size / 1024:.1f} KB"


def _brief(brief: dict) -> str:
    if brief["state"] == "not_requested" and not brief["version"]:
        return "no brief"
    version = f" v{brief['version']}" if brief["version"] else ""
    return f"brief{version} {brief['state']}" + (", approved" if brief["approved"] else "")


def _run_line(snap: dict) -> str:
    run = snap["run"]
    if run is None:
        return f"run: none · queued {snap['queued']}"
    derived = f" ({run['run']})" if run["run"] != "none" else ""
    return (f"run: {run['kind']} {run['command'][:8]} by @{run['agent']} {run['state']}{derived} {run['at']} · "
            f"session {run['session'] or 'none'} · queued {snap['queued']}")


def _recorded_waits(pipeline: dict) -> str:
    """``blocking — decision 1a2b3c4d open; non-blocking — card KD-7 open`` of the recorded waits shown."""
    def ref(w):
        if w["kind"] in ("decision", "operation"):
            return f"{w['kind']} {w['ref'][:8]}"
        if w["kind"] == "assist":
            return f"help {w['ref'][:8]}"
        if w["kind"] in ("contour", "time"):
            return f"{w['kind']} {w['ref']}"
        return f"card {w['ref']}"

    groups = []
    for name, blocking in (("blocking", True), ("non-blocking", False)):
        shown = ", ".join(f"{ref(w)} {w['state']}" for w in pipeline.get("waits", []) if w["blocking"] is blocking)
        if shown:
            groups.append(f"{name} — {shown}")
    more = pipeline.get("waits_total", 0) - len(pipeline.get("waits", []))
    return "; ".join(groups) + (f" (+{more})" if more > 0 and groups else "")


def _waits_line(waits: dict, pipeline: Optional[dict] = None) -> str:
    open_ = ", ".join(f"{w['type']} {w['decision'][:8]} by {w['by']} since {w['since']}" for w in waits["open"])
    answered = ", ".join(f"{w['type']} {w['decision'][:8]} {w['state']} by {w['by']} {w['at']}"
                         for w in waits["answered"])
    recorded = _recorded_waits(pipeline or {})
    head = f"recorded: {recorded} · " if recorded else ""
    return f"waits: {head}{('open: ' + open_) if open_ else 'none open'} · answered: {answered or 'none'}"


HELP_ENDED_SHOWN = 2  # the last ended help requests: their state, reason and evidence stay readable


def _help_lines(pipeline: dict) -> List[str]:
    """One line per open help request of the card and the last ended ones with their evidence, the step
    and the evidence cut; the rest reads kd-task waits."""
    every = [a for a in pipeline.get("assists", []) if a["state"] != "gone"]
    open_ = [a for a in every if a["state"] in ("requested", "assigned", "accepted")][:HELP_SHOWN]
    ended = [a for a in every if a["state"] not in ("requested", "assigned", "accepted")][:HELP_ENDED_SHOWN]
    lines = []
    for a in open_ + ended:
        state = f"{a['state']} {a['reason']}" if a["reason"] else a["state"]
        line = (f"help {a['id'][:8]} {state} {a['lead']} → {a['helper'] or a['role']} {a['kind']}: "
                f"{_cut_bytes(a['step'], STEP_CUT, '…')}")
        if a in ended and a["evidence"]:
            line += f" — {_cut_bytes(a['evidence'], STEP_CUT, '…')}"
        lines.append(line)
    return lines


def _outcome_line(outcome: dict) -> str:
    state = outcome["state"] + (f", {outcome['reason']}" if outcome["reason"] else "")
    note = outcome["note"] or "recorded by the board: the turn asked for help and left no outcome"
    return f"outcome: {outcome['outcome']} by {outcome['agent']} {outcome['created']} — {note} ({state})"


def _defer_line(defer: dict) -> str:
    """``deferred: until <UTC> by <who> since <when> — <reason>``."""
    reason = f" — {_cut_bytes(defer['reason'], STEP_CUT, '…')}" if defer["reason"] else ""
    return f"deferred: until {defer['until']} by {view.who(defer['by'])} since {defer['created']}{reason}"


def _problems_line(pipeline: dict) -> str:
    shown = "; ".join(f"{p['kind']} {p['agent'] or '-'} since {p['created']}" for p in pipeline["problems"])
    more = pipeline.get("problems_total", len(pipeline["problems"])) - len(pipeline["problems"])
    return f"problems: {shown}" + (f" (+{more})" if more > 0 else "")


def _pr_line(prs: List[dict]) -> str:
    if not prs:
        return "PR: none"
    return "PR: " + "; ".join(
        f"#{p['number']} {p['state']} · head {p['head'][:7]} · "
        + (f"approved on this head ({p['mode'] or 'approve'})" if p["approved_head"] else "not approved on this head")
        for p in prs
    )


def _checks_line(checks: dict) -> str:
    if not checks["items"]:
        return "checks: none"
    c = checks["items"][0]
    line = f"checks: {c['state']} {','.join(c['profiles'])} on {c['head'][:7]} ({c['id'][:12]})"
    if c["state"] == "failed" and c["class"]:
        line += f" — {c['class']}"
    if c["log"]:
        line += f" · log kd-host-read tail -n 200 {c['log']}"
    older = checks["total"] - 1
    return line + (f" (+{older} earlier)" if older > 0 else "")


def _stages_line(staged: dict) -> str:
    shown = ", ".join(f"{i['key']} {i['child']} {i['status']} {i['role'] or '-'}"
                      + (f" ({i['gate']})" if i.get("gate") else "") for i in staged["items"])
    chain = "chain · " if staged.get("chain") else ""  # one branch and pull request per repository, no owner between
    return f"stages: {chain}{staged['done']} of {staged['total']} done — {shown}"


def _release_line(release: Optional[dict]) -> str:
    if release is None:
        return "release: none"
    rev = f"{release['rev']}+{release['deploy_rev']}" if release.get("deploy_rev") else release["rev"]
    where = f"{rev} on {release['host']} {release['at']}".replace(" on  ", " ")
    return {
        "merged": f"release: merged {release['at']}, no release after it",
        "code_switched": f"release: code_switched {where} — released, health not checked yet",
        "activating": f"release: activating {where} — healthy, operations of the release still running",
        "healthy": f"release: healthy {where}",
        "degraded": f"release: degraded {where} — check failed: {release['why']}",
        "refused": f"release: refused {release['at']}: {release['why']}",
    }[release["state"]]


def _list(items: List[dict], total: int) -> str:
    if not total:
        return "none"
    shown = ", ".join(f"{i['id']} {i['status']} {i['title']}" for i in items)
    return f"{total}: {shown}" if shown else str(total)


def _round_line(gathered: dict) -> str:
    reason = f" ({gathered['end_reason']})" if gathered["end_reason"] else ""
    pull = f" · PR #{gathered['number']} head {gathered['head'][:12]}" if gathered["kind"] == "code" else ""
    return (f"council round: {gathered['kind']} {gathered['round']}/{gathered['rounds']} {gathered['state']}{reason}, "
            f"open findings {gathered['open_findings']}{pull}")


def _text(snap: dict) -> str:
    card, scope, nxt, deps = snap["card"], snap["scope"], snap["next"], snap["deps"]
    head = f"{card['id']} · {card['status']} · {card['assignee'] or '-'} · {card['title']}"
    if card.get("parent"):
        head += f" · parent {card['parent']}"  # a stage of a plan: the whole picture is there
        if (snap.get("pipeline") or {}).get("stage"):
            head += (f" (chain: a branch agent/{card['parent']}-… and a pull request of {card['parent']}"
                     " in each repository)")
    if card["labels"]:
        head += f" [{', '.join(card['labels'])}]"
    who = f" {nxt['who']}" if nxt["who"] != "-" else ""
    deps_text = _list(deps["items"], deps["total"])
    if deps["total"]:
        deps_text = f"{deps['open']} open of {deps_text}"
    summary = snap["result"]["summary"] or "(none)"
    lines = [head]
    if snap.get("model"):  # the card's sessions run on a cabinet model, or on an effort — short, never cut
        model = snap["model"]
        lines.append(f"model: {model['id']} · {model['provider']}" + ("" if model["enabled"] else " · disabled"))
    if snap.get("effort"):
        lines.append(f"effort: {snap['effort']}")
    if snap.get("council"):  # the council in force — short, never cut
        lines.append(f"council: {_council.line(snap['council'])}")
    if snap.get("council_round"):  # where its last council stands — short, never cut
        lines.append(_round_line(snap["council_round"]))
    lines += [
        f"revision {card['revision']} · scope {scope['sha']} {_kb(scope['bytes'])} ({_brief(scope['brief'])}) · "
        f"iteration {snap['iteration']} · watermark {snap['watermark']} · updated {card['updated']}",
        f"next ({nxt['source']}): {nxt['what']}{who} — {nxt['text']}",
    ]
    pipeline = snap.get("pipeline") or {}
    if pipeline.get("defer"):
        lines.append(_defer_line(pipeline["defer"]))
    if pipeline.get("outcome"):
        lines.append(_outcome_line(pipeline["outcome"]))
    if pipeline.get("problems"):
        lines.append(_problems_line(pipeline))
    if pipeline.get("stages"):
        lines.append(_stages_line(pipeline["stages"]))
    lines += [
        _run_line(snap),
        _waits_line(snap["waits"], pipeline),
        *_help_lines(pipeline),
        _pr_line(snap["prs"]),
        _checks_line(snap["checks"]),
        _release_line(snap["release"]),
        f"deps: {deps_text} · children: {_list(snap['children']['items'], snap['children']['total'])}",
        f"result: AC {snap['result']['ac_checked']}/{snap['result']['ac_total']} checked · summary: {summary} · "
        f"{snap['more']['result']}",
    ]
    messages = snap["messages"]
    if messages["items"]:
        lines.append(f"messages ({len(messages['items'])} of {messages['total']} significant, newest first; "
                     "tool calls left out):")
        for m in messages["items"]:  # a message's files after its text, never cut: file get takes the id
            text = view.continued(m["text"])  # a body's lines indented: none mimics a header (review 04.11.2026)
            lines.append("\n  ".join([f"#{m['seq']} {m['who']} {m['at']} {m['kind']}: {text}", *m["files"]]))
    elif messages["total"]:
        lines.append(f"messages: {messages['total']} significant, none shown")
    else:
        lines.append("messages: none")
    if snap["trimmed"]:
        lines.append(f"trimmed to fit {LIMIT // 1024} KiB: {', '.join(snap['trimmed'])}")
    lines.append("more: " + " · ".join(f"{name} {command}" for name, command in snap["more"].items()))
    return "\n".join(lines) + "\n"


def render(snap: dict, limit: int = LIMIT) -> str:
    """The snapshot as text for an agent: state and next step first, cut in the fit's order to ``limit`` bytes."""
    text = _text(_shrink(snap, lambda s: len(_text(s).encode("utf-8")), limit))
    return _cut_bytes(text, limit - 4, "…\n")  # only when even the trimmed facts do not fit


def _snapshot(ctx, principal, request) -> dict:
    """``card.snapshot {card, format}`` → ``{text}`` or ``{snapshot}`` of a card the caller may read."""
    record = api._visible_card(ctx, principal, request.get("card"))
    form = request.get("format", "text")
    if form not in FORMATS:
        raise api.ApiError("invalid", "format must be text or json")
    snap = build(ctx, record, lambda project: api.visible(ctx, principal, project))
    return {"snapshot": snap} if form == "json" else {"text": render(snap)}


OPS = {"card.snapshot": (api.READERS, _snapshot)}
api.OPS.update(OPS)  # the board API serves it (api imports this module at its end)
