"""The board's changes (root): what an event means for the agents — the executor's wake (``wake_items``, which the
cabinet turns into commands), the coordinator's events (``coordinator_events``, which kick watch). the board
is the cabinet; ``sync`` reads its events from a cursor in the cabinet's state (``changes_from``). the Pult
is gone — no lines, no digest. Titles are other people's data: cleaned, escaped and cut, never commands."""
import re
import sys
import unicodedata

from kd import ids
from kd.cabinet import client as cabinet_client

AGENT_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")  # fullmatch only: "$" would let a trailing \n through
OWNER = "owner"
COORDINATOR = "boss"
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_ESC = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])")


def _plain(text) -> str:
    """Without control characters, ANSI, invisible format characters (Cf: U+202E, zero width), "¦" and „“
    (the line's quotes stay paired), spaces collapsed; not escaped."""
    s = _ESC.sub("", str(text))
    s = "".join(c for c in s if unicodedata.category(c) != "Cf")
    s = _CTRL.sub(" ", s).replace("¦", "|").replace("„", '"').replace("“", '"')
    return " ".join(s.split())


MESSAGE_WAKE = "[board] {id}: a message for you in the thread — read it with kd-task view {id} and answer there"
ANSWER_WAKE = "[board] {id}: your question {decision} has an answer — read it with kd-work answer {decision}"
# a person's question on an agent's open decision: the decision stays open, the agent answers in the thread
ASKED_WAKE = ("[board] {id}: a question on your {type} {decision} in the thread — read it with kd-task view {id} and "
              "answer there; the decision stays open")
CONTINUE_WAKE = "[board] {id}: back in work — read the thread for what to do next"
BRIEF_WAKE = "[board] {id}: the brief is ready — check it and propose the start"
# the owner's signed answer on a pull request in the cabinet: the mode he chose, never his words
PR_WAKE = {
    "approve": "[board] {id}: the owner approved PR #{pr} on {sha7} in the cabinet — approve only: merge when he "
               "says so in the thread",
    "merge": "[board] {id}: the owner approved PR #{pr} on {sha7} in the cabinet and asks to merge it — kd-forge pr "
             "for the full head, then kd-merge",
    "release": "[board] {id}: the owner approved PR #{pr} on {sha7} in the cabinet and asks to merge and release it "
               "— kd-forge pr for the full head, then kd-merge --release",
    # a chain plan with other pull requests still open: the release is one, after the plan's last merge
    "release_plan": "[board] {id}: the owner approved PR #{pr} on {sha7} in the cabinet and asks to merge and release "
                    "— a chain plan with other pull requests open: merge every pull request of the plan in the "
                    "brief's order with kd-merge (no --release), then release once: kd-merge --release on this "
                    "pull request, merged by then",
}
PR_REMARKS_WAKE = ("[board] {id}: the owner sent remarks on PR #{pr} — read the thread and kd-forge comments, fix and "
                   "publish again")
# the owner's signed approval of the sysadmin's plan or critical command: on a card already in work no
# status
# changes, so the approval itself is the cue
PLAN_WAKE = ("[board] {id}: the owner approved your plan in the cabinet — take the card with kd-task claim {id} if it "
             "is in To Do, then run its steps")
# the owner's signed approval of the developer's factory plan: the operations it may now ask for
FACTORY_PLAN_WAKE = ("[board] {id}: the owner approved your factory operations in the cabinet — request them with "
                     "kd-factory reinstall {id} --agents <names> --key <key>, then end the turn waiting for it")
ACTION_WAKE = ("[board] {id}: the owner approved your critical command {action} in the cabinet — run it once with "
               "kd-admin-critical --task {id} --action {action}")
ADMIN_APPROVALS = ("admin_plan", "admin_critical")
ACTION_ID = re.compile(r"[0-9a-f]{16}")
SHA40 = re.compile(r"[0-9a-f]{40}")
BRIEF_READY = "brief-ready"
BRIEF_SPEC = "spec:task-brief"  # the analyst's order: its brief is written in Inbox
BACK_TO_WORK = ("Review", "Blocked")
DECISION_ID = re.compile(r"[0-9a-f]{32}")


# The board's wakes as a table: a direct change — one meant for its agent — and the command it makes:
# (the command's kind, its text from the change). A direct change is always its agent's cue, reaches the
# coordinator too, and the coordinator's watch leaves it alone: the agent already has it.
WAKES = {
    "message": ("message", lambda e: MESSAGE_WAKE.format(id=e["id"])),
    "answer": ("answer", lambda e: ANSWER_WAKE.format(id=e["id"], decision=e["decision"])),
    "asked": ("message", lambda e: ASKED_WAKE.format(id=e["id"], type=e["type"], decision=e["decision"])),
    "continue": ("continue", lambda e: CONTINUE_WAKE.format(id=e["id"])),
    "brief": ("signal", lambda e: BRIEF_WAKE.format(id=e["id"])),
    "pr": ("message", lambda e: PR_WAKE["release_plan" if e.get("plan") and e["mode"] == "release" else e["mode"]]
           .format(id=e["id"], pr=e["pr"], sha7=e["head"][:7])),
    "pr_remarks": ("message", lambda e: PR_REMARKS_WAKE.format(id=e["id"], pr=e["pr"])),
    "plan": ("assigned", lambda e: (FACTORY_PLAN_WAKE if e.get("policy") == "factory" else PLAN_WAKE)
             .format(id=e["id"])),
    # the approval lives 15 minutes — the host starts this wake at once
    "action": ("urgent", lambda e: ACTION_WAKE.format(id=e["id"], action=e["action"])),
}
# the «to work» changes: the card's status decides whether they are a cue (``_due``); they read as an assignment.
# ``wake`` — the owner's «take it into work» — is a cue in any status, but never the coordinator's
TO_WORK = ("new", "status", "assignee", "wake")
ASSIGNED = ("assigned", lambda e: f"[board] {e['id']} assigned to you")


def _due(e: dict) -> bool:
    """Is this change the agent's cue: a direct one (``WAKES``), the owner's «take it into work», a card that
    became «to work» (To Do: new, approved, reassigned), or a card handed to it in any live status (a person's
    assignment anywhere, an agent's — outside Inbox, where the coordinator only routes, unless it orders a brief,
    which is Inbox work)."""
    kind, status = e["kind"], e["status"]
    if kind in WAKES or kind == "wake":
        return True
    if kind in ("new", "status", "assignee") and status == "To Do":
        return True
    if kind == "assignee" and status not in ("Done", "Cancelled"):  # a closed card wakes nobody
        return (status != "Inbox" or str(e.get("by", "")).startswith("user:")
                or BRIEF_SPEC in (e.get("labels") or ()))
    return False


def wake_items(events: list) -> list:
    """{agent, card, kind, text} for the tasks that became «to work» for an agent (``_due``), worded by ``WAKES``;
    the cabinet turns the same items into commands for agents on the session host (``kd.cabinet.commands``)."""
    out = []
    for e in events:
        if _due(e) and len(e["assignees"]) == 1:
            agent = e["assignees"][0]
            direct = e["kind"] in WAKES
            if (agent != OWNER and (agent != COORDINATOR or direct) and AGENT_RE.fullmatch(agent)
                    and ids.CARD.fullmatch(str(e["id"]))):
                kind, text = WAKES.get(e["kind"], ASSIGNED)
                out.append({"agent": agent, "card": e["id"], "kind": kind, "text": text(e)})
    return out


def coordinator_events(events: list) -> bool:
    """Whether the diff has something the coordinator handles at once: a card assigned to it, a new one without an executor, Blocked."""
    for e in events:
        if e["kind"] in WAKES or e["kind"] in ("gone", "wake"):
            # a wake reaches its agent directly
            continue
        if (COORDINATOR in e["assignees"] or (e["kind"] == "new" and not e["assignees"])
                or (e["status"] == "Blocked" and e["old_status"] != "Blocked")):
            return True
    return False


SYNC_KEY = "sync"  # the cabinet state key of sync's event cursor
EVENTS_BATCH = 500


def _names(assignee) -> list:
    return [assignee.lstrip("@")] if isinstance(assignee, str) and assignee else []


def _pr_change(e: dict, p: dict, by: str):
    """The owner's answer on a PR for the agent that asked: ``pr`` with the mode he signed, or
    ``pr_remarks``; None when the event does not read as one."""
    agent = str(p.get("requested_by", ""))
    number = p.get("pr")
    if not agent.startswith("agent:") or isinstance(number, bool) or not isinstance(number, int):
        return None
    change = {"id": e["card"], "title": "", "status": "", "assignees": [agent[len("agent:"):]], "old_status": None,
              "old_assignees": [], "by": by, "pr": number}
    if e.get("kind") == "decision.rejected":
        return {**change, "kind": "pr_remarks"}
    if p.get("mode") not in PR_WAKE or not SHA40.fullmatch(str(p.get("head", ""))):
        return None
    return {**change, "kind": "pr", "mode": p["mode"], "head": p["head"], "plan": p.get("plan") is True}


def _change(kind: str, e: dict, by: str, assignees: list, **fields) -> dict:
    return {"kind": kind, "id": e["card"], "title": "", "status": "", "assignees": assignees, "old_status": None,
            "old_assignees": [], "by": by, **fields}


def _requester(p: dict) -> list:
    """The agent that asked for the decision of the event, as a change's assignees; [] when no agent asked."""
    who = str(p.get("requested_by", ""))
    return [who[len("agent:"):]] if who.startswith("agent:") else []


def _created(e: dict, p: dict, by: str) -> list:
    return [_change("new", e, by, _names(p.get("assignee")), title=p.get("title", ""), status=p.get("status", ""))]


def _woken(e: dict, p: dict, by: str) -> list:
    """The owner's click in the cabinet: the named agent is woken."""
    return [_change("wake", e, by, _names(p.get("agent")), title=p.get("title", ""))]


def _mentioned(e: dict, p: dict, by: str) -> list:
    """A person's thread message naming agents: one change per agent; voice.ready — a person's message
    with audio, now transcribed, for whom it is meant."""
    if not by.startswith("user:"):
        return []
    return [_change("message", e, by, _names(agent), title=p.get("title", "")) for agent in p.get("mentions") or []]


def _answered(e: dict, p: dict, by: str) -> list:
    """A person answered an agent's question: the agent that asked is woken, even if no longer waiting."""
    agent = _requester(p)
    if p.get("type") != "question" or not agent or not DECISION_ID.fullmatch(str(p.get("decision"))):
        return []
    return [_change("answer", e, by, agent, decision=p["decision"])]


def _asked(e: dict, p: dict, by: str) -> list:
    """A person asked about an agent's open decision: the agent that asked for it answers in the thread."""
    agent = _requester(p)
    if (not by.startswith("user:") or not agent or not DECISION_ID.fullmatch(str(p.get("decision")))
            or not AGENT_RE.fullmatch(str(p.get("type")))):
        return []
    return [_change("asked", e, by, agent, decision=p["decision"], type=p["type"])]


def _pr_answered(e: dict, p: dict, by: str) -> list:
    pr = _pr_change(e, p, by) if p.get("type") == "pr_approve" else None
    return [pr] if pr is not None else []


def _approved(e: dict, p: dict, by: str) -> list:
    """The owner's signed approval of the sysadmin's plan or critical command, of dev's factory plan."""
    agent = _requester(p)
    if p.get("type") not in ADMIN_APPROVALS or not agent:
        return []
    if p["type"] == "admin_plan":
        return [_change("plan", e, by, agent, **({"policy": p["policy"]} if p.get("policy") else {}))]
    if not ACTION_ID.fullmatch(str(p.get("action", ""))):
        return []
    return [_change("action", e, by, agent, action=p["action"])]


def _changed(e: dict, p: dict, by: str) -> list:
    """A new status or a new assignee, and a ready brief."""
    if p.get("wake") is False:  # the Inbox -> To Do of an approved plan: the approval wakes the agent
        return []
    before, after = p.get("before") or {}, p.get("after") or {}
    out = []
    if (BRIEF_READY in (after.get("labels") or []) and BRIEF_READY not in (before.get("labels") or [])
            and after.get("status") == "Inbox"):
        # the analyst's brief is ready: the coordinator checks it and proposes the start at once
        out.append(_change("brief", e, by, [COORDINATOR], title=p.get("title", ""), status="Inbox",
                           old_status=before.get("status")))
    if before.get("status") == after.get("status") and before.get("assignee") == after.get("assignee"):
        return out
    if before.get("status") != after.get("status"):
        # a person sending a card back from Review or Blocked: the assignee continues by the thread
        back = (by.startswith("user:") and before.get("status") in BACK_TO_WORK
                and after.get("status") == "In Progress")
        kind = "continue" if back else "status"
    else:
        kind = "assignee"
    out.append(_change(kind, e, by, _names(after.get("assignee")), title=p.get("title", ""),
                       status=after.get("status", ""), old_status=before.get("status"),
                       old_assignees=_names(before.get("assignee")), labels=list(after.get("labels") or [])))
    return out


# The board's rules as a list: which cabinet events a rule reads, and the rule — the event's changes.
# The rules of one event are disjoint (a decision's type, the actor), so their order changes nothing.
EVENT_RULES = (
    (("card.created", "card.imported"), _created),
    (("card.wake",), _woken),
    (("message.added", "voice.ready"), _mentioned),
    (("decision.approved",), _answered),
    (("decision.asked",), _asked),
    (("decision.approved", "decision.rejected"), _pr_answered),
    (("decision.approved",), _approved),
    (("card.changed",), _changed),
)


def changes_from(events: list) -> list:
    """The cabinet's events as the board's changes (``EVENT_RULES``): a new card (created or imported), a new status
    or a new assignee, a ``card.wake`` (the owner's click in the cabinet: the named agent is woken), a person's
    thread message naming agents (``mentions``: one ``message`` change per agent), a person's answer to an agent's
    question or question on its decision, the owner's answer on a PR, a plan or a critical command, and a ready
    brief; every other event (an agent's comment, a label, a text edit) is no change. ``by`` is the actor."""
    out = []
    for e in events:
        p = e.get("payload") or {}
        by = str(e.get("actor") or "")
        for kinds, rule in EVENT_RULES:
            if e.get("kind") in kinds:
                out.extend(rule(e, p, by))
    return out


def sync(client, *, on_events=None, err=None) -> int:
    """0 — every new event is read and the cursor moved; 1 — the cabinet failed (unit failed → kd-alert). The wakes
    are the cabinet's commands, made with the event (``kd.cabinet.commands``); sync only hands the changes to
    ``on_events`` (kd-board starts watch for the coordinator's events) and moves its cursor."""
    err = err or sys.stderr
    try:
        cursor = client.call({"op": "state.get", "key": SYNC_KEY}).get("value") or {"seq": 0}
        seq = cursor["seq"]
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            raise ValueError("bad sync cursor")
        while True:
            events = client.call({"op": "events.since", "seq": seq, "limit": EVENTS_BATCH})["events"]
            if not events:
                return 0
            changes = changes_from(events)
            if changes and on_events is not None:
                on_events(changes)
            seq = events[-1]["seq"]
            client.call({"op": "state.set", "key": SYNC_KEY, "value": {"seq": seq}})
            if len(events) < EVENTS_BATCH:
                return 0
    except (cabinet_client.CabinetError, KeyError, TypeError, ValueError) as exc:
        print(f"kd-board: кабинет недоступен ({exc}) — прогон пропущен, курсор не тронут", file=err)
        return 1
