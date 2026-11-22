"""What a person may do with a card next, and doing it.

The backend is the one source of the card's actions: ``actions`` lists them for the page (the status's moves,
which is the main one, whom it hands the card to, why one is off, whether it takes a comment), and
``user.card.act`` runs one of them as one write — the card, the person's comment and the agent's command
(the command comes from the board's rules, ``kd.cabinet.commands``). A plain status edit is a manual override
with a reason (``api._user_card_edit``).
"""

from typing import List, Mapping, Optional, Tuple

from kd import boss_board, worker_board
from kd.cabinet import api, briefs, councils, decisions, prs_api, stages, store

ACTIONS = ("start", "brief", "save_brief", "accept", "return", "resume", "reopen", "restore", "approve_stages")
BY_STATUS = {
    "Inbox": ("start", "brief", "save_brief"),
    "To Do": ("start", "brief", "save_brief"),
    "In Progress": ("approve_stages",),  # an aggregate's new brief version with more stages
    "Review": ("accept", "return"),
    "Blocked": ("resume",),
    "Done": ("reopen",),
    "Cancelled": ("restore",),  # a card closed without the work goes back to Inbox, its outcome cleared
}
COMMENT = {"return": "required", "resume": "optional", "reopen": "optional", "restore": "optional"}
DEPS_OPEN, SPEC_MISMATCH, NO_AGENT, BRIEF_WRITING = "deps_open", "spec_mismatch", "no_agent", "brief_writing"
NOT_OFFERED, USE_ACTION, UNAVAILABLE = "not_offered", "use_action", "unavailable"
PR_OPEN = "pr_open"  # the card's pull request is not merged: its decision comes first
PR_APPROVED = "pr_approved"  # approved on its head, not merged yet: the merge is dev's on the owner's word
BRIEF_STALE, BRIEF_CHANGED = api.BRIEF_STALE, "brief_changed"
REASONS = (DEPS_OPEN, SPEC_MISMATCH, NO_AGENT, BRIEF_WRITING, NOT_OFFERED, USE_ACTION, UNAVAILABLE, BRIEF_STALE,
           BRIEF_CHANGED, PR_OPEN, PR_APPROVED)
LABELS = tuple(f"action.{a}" for a in ACTIONS) + (
    "action.start_brief", "action.start_legacy", "action.start_again", "action.brief_rework", "action.brief_retry",
)
DONE = "Done"


def _routes(ctx) -> dict:
    return ctx.routing().projects


def takers(ctx) -> frozenset:
    """The agents a card can be started on: not the owner, not the coordinator, not the brief writer."""
    return frozenset(ctx.routing().assignees) - {api.OWNER, worker_board.COORDINATOR, api.ANALYST}


def target(ctx, record: store.CardRecord) -> str:
    """Whom a start hands the card to: its assignee, or the project's route while the analyst holds it."""
    agent = record.assignee or _routes(ctx).get(record.project, "")
    if agent == api.ANALYST:
        agent = _routes(ctx).get(record.project, "")
    return agent


def open_dependencies(ctx, record: store.CardRecord) -> List[str]:
    """The card's dependencies not Done yet (a dependency that is gone never will be: it counts as open)."""
    out = []
    for dep in record.dependencies:
        try:
            if ctx.store.card(dep).status != DONE:
                out.append(dep)
        except store.NotFound:
            out.append(dep)
    return out


def foreign_specs(ctx, agent: str, labels) -> List[str]:
    """The ``spec:`` labels ``agent`` does not hold (spec 5.8.5): none at all needs no registry."""
    labelled = [v for v in labels if v.startswith(worker_board.SPEC_PREFIX)]
    if not labelled:
        return []
    held = ctx.specs().get(agent, ())
    return [v for v in labelled if v[len(worker_board.SPEC_PREFIX) :] not in held]


def refusal(message: str, reason: str, **params) -> api.ApiError:
    """A refused action: the machine ``reason`` and the page's parameters for ``flow.reason.<reason>``."""
    error = api.ApiError("refused", message, reason=reason)
    error.params = params
    return error


def deps_error(ctx, record: store.CardRecord) -> Optional[api.ApiError]:
    deps = open_dependencies(ctx, record)
    if not deps:
        return None
    cards = ", ".join(deps)
    return refusal(f"{record.id} waits for {cards} (not Done yet)", DEPS_OPEN, cards=cards)


def check_start(ctx, record: store.CardRecord, agent: str) -> Optional[api.ApiError]:
    """Why ``agent`` may not take the card into work now, or None."""
    if agent not in takers(ctx):
        return refusal(f"{record.id}: no agent takes it ({agent or 'nobody'})", NO_AGENT)
    found = deps_error(ctx, record)
    if found is not None:
        return found
    if agent == record.assignee:  # a hand-over drops what the new agent lacks (``start``); its own card must fit
        foreign = ", ".join(foreign_specs(ctx, agent, record.labels))
        if foreign:
            return refusal(f"{record.id}: {foreign} is not a specialization of {agent}", SPEC_MISMATCH,
                           specs=foreign, agent=agent)
    return None


def _item(action, *, agent="", primary=False, label="", error=None, reason="", comment="", brief_version=None,
          operations=(), stages=(), chain=False) -> dict:
    params = {}
    if error is not None:
        reason = error.reason or UNAVAILABLE
        params = dict(getattr(error, "params", {}))
    return {
        "id": action,
        "label": label or f"action.{action}",
        "primary": primary,
        "agent": agent,
        "enabled": error is None,
        "reason": reason,
        "params": params,
        "message": str(error) if error is not None else "",
        "comment": comment or COMMENT.get(action, "none"),
        "brief_version": brief_version,  # the version a start approves: the page sends back the one it showed
        "operations": [dict(op) for op in operations],  # what approving that version grants: shown first
        "stages": [dict(s) for s in stages],  # the child cards approving that version creates
        **({"chain": True} if chain else {}),  # those stages run as a chain: no acceptance of each by the owner
    }


def _checked(check) -> Optional[api.ApiError]:
    try:
        return check()
    except api.ApiError as exc:  # the routes or the specialization registry unreadable: the action waits
        return exc if exc.reason else api.ApiError(exc.kind, str(exc), reason=UNAVAILABLE)


def _start_label(record: store.CardRecord, brief: dict) -> Tuple[str, Optional[int]]:
    """The start's label and the brief version it approves (R17): the ready one, 0 for a legacy brief. A stale
    brief keeps the approving label (the start is off until the text is a version again)."""
    if brief["state"] == "ready" and brief["legacy"]:
        return "action.start_legacy", 0
    version = brief["version"] if brief["state"] in briefs.READY else None
    if (version is not None and not brief["approved"]) or (brief["state"] == "stale" and record.status != "To Do"):
        return "action.start_brief", version
    return ("action.start_again" if record.status == "To Do" else "action.start"), version


def _stale(record: store.CardRecord, brief: dict) -> Optional[api.ApiError]:
    if brief["state"] != "stale":
        return None
    return refusal(f"{record.id}: the brief changed after v{brief['version']}", BRIEF_STALE, version=brief["version"])


def _open_pr(ctx, record: store.CardRecord) -> Optional[api.ApiError]:
    """«Accept» while the card's PR is not merged would close work still waiting for its merge."""
    open_ = ctx.store.prs(card=record.id, state="open")
    if not open_:
        return None
    pr = open_[-1]
    number = pr["number"]
    approved = [d for d in prs_api.decisions_of(ctx, record.id, pr["repo"], number, state="approved")
                if d["payload"].get("head") == pr["head"]]
    if approved:
        return refusal(f"{record.id}: PR #{number} is approved, not merged yet", PR_APPROVED, pr=number)
    return refusal(f"{record.id}: PR #{number} is not merged", PR_OPEN, pr=number)


def _chain(ctx, record: store.CardRecord, version: Mapping, lead: str = "") -> bool:
    """Whether approving this version's stages runs them as a chain — one after another on one branch, without the
    owner's acceptance of each: the page says so before he approves. ``lead`` is the agent the start hands the card
    to (``target``): the decision is taken after that hand-over."""
    if stages.is_aggregate(ctx.store, record.id):
        return stages.is_chain(ctx.store, record.id)
    return stages.chain_eligible(ctx, record, version, lead)


def _offer(ctx, record: store.CardRecord, action: str, brief: dict) -> Optional[dict]:
    assignee = record.assignee
    if action == "start":
        agent = target(ctx, record)
        writing = assignee == api.ANALYST and api.BRIEF_SPEC in record.labels
        label, version = _start_label(record, brief)
        error = _checked(lambda: check_start(ctx, record, agent) or _stale(record, brief))
        steps, parts, chain = (), (), False
        if version is not None:
            approved = next((v for v in briefs.versions(ctx.store, record.id) if v["version"] == version), None)
            if approved is not None:
                steps, parts = approved["operations"], approved["stages"]
                chain = bool(parts) and _chain(ctx, record, approved, agent)
        item = _item(
            "start",
            agent=agent,
            primary=record.status == "Inbox" and not writing,
            label=label,
            error=error,
            reason=BRIEF_WRITING if writing else "",
            brief_version=version,
            operations=steps,
            stages=parts,
            chain=chain,
        )
        if brief["state"] == "in_council":  # the start ends the council where it is: the page warns
            item["council"] = {key: brief["council"][key] for key in ("round", "rounds")}
        return item
    if action == "brief":
        if api.ANALYST not in ctx.routing().assignees:
            return None
        if brief["state"] == "failed":
            return _item("brief", agent=api.ANALYST, label="action.brief_retry")
        if assignee == api.ANALYST:
            return None  # the analyst is on it (queued, writing, waiting for answers)
        if brief["version"] or brief["legacy"]:
            return _item("brief", agent=api.ANALYST, label="action.brief_rework", comment="required")
        return _item("brief", agent=api.ANALYST)
    if action == "save_brief":
        if brief["state"] != "stale" or assignee == api.ANALYST:
            return None
        return _item("save_brief")
    if action == "approve_stages":
        if (brief["state"] not in briefs.READY or brief["approved"] or not brief["version"]
                or not stages.is_aggregate(ctx.store, record.id)):
            return None
        version = briefs.latest(ctx.store, record.id)
        if not version["stages"]:
            return None
        return _item("approve_stages", primary=True, brief_version=version["version"],
                     operations=version["operations"], stages=version["stages"], chain=_chain(ctx, record, version))
    if action == "accept":
        error = _open_pr(ctx, record)
        return _item("accept", primary=error is None, error=error)
    if action == "return":
        return _item("return", agent=assignee)
    if action == "resume":
        return _item("resume", agent=assignee, primary=True, error=_checked(lambda: deps_error(ctx, record)))
    if action == "restore":
        return _item("restore", primary=True)
    return _item("reopen", agent=assignee)


def actions(ctx, principal, record: store.CardRecord) -> List[dict]:
    """The card's actions for this person, in the page's order; none without the right to move the card."""
    if principal.kind != "user" or not api.allowed(ctx, principal, "cards.transition", record.project):
        return []
    out = []
    brief = briefs.state(ctx.store, record)
    for action in BY_STATUS.get(record.status, ()):
        item = _offer(ctx, record, action, brief)
        if item is not None:
            out.append(item)
    return out


# --- doing one --------------------------------------------------------------------------------
def _comment(request, need: str) -> str:
    text = request.get("comment")
    if text is None or (need == "optional" and isinstance(text, str) and not text.strip()):
        if need == "required":
            raise api.ApiError("invalid", "this action needs a comment")
        return ""
    if need == "none":
        raise api.ApiError("invalid", "this action takes no comment")
    return api._text(text, "comment").strip()


def _edit(ctx, record, revision, changes, actor, extra=None, *, promoting=False) -> store.CardRecord:
    return api._apply(ctx, boss_board.Edit(record.id, revision, changes), actor, extra, promoting=promoting)


def _grant(ctx, record, actor) -> None:
    from kd.cabinet import effects

    try:
        effects.brief_grant(ctx, record, by=actor)  # the "Operations" of the approved brief
    except effects.EffectError as exc:
        raise api.ApiError(exc.kind, str(exc)) from exc  # the start is refused with its reason, in the same write


def approve_with_stages(ctx, record, approved, *, by: str, user: str, revision=None, promoting=False,
                        principal=None) -> Optional[store.CardRecord]:
    """A start that approves a brief with stages: the parent becomes the aggregate — In Progress on its
    agent with no wake of its own (its work is the children's) — and the children are materialized in the same
    write. None when the approved version has no stages: the plain start follows."""
    if approved is None or not approved.get("stages"):
        return None
    stages.check_projects(ctx, record, approved, principal or api.Principal("user", -1, user))
    if stages.is_chain(ctx.store, record.id) and not stages.chain_shape(ctx, record, approved):
        # its stages share one branch and one pull request: a stage of another role or project has no place there
        raise api.ApiError("refused", f"{record.id}: a chain plan keeps one role and one project — a stage of "
                                      "another one is a card of its own")
    made = stages.materialize(ctx, record, approved, by=by)
    named = ", ".join(r["child"] for r in made if r["child"])
    changes = {"comment": {"text": f"[{user}] этапы заведены: {named}", "author": by}}
    if record.status != "In Progress":
        changes["status"] = "In Progress"
    return _edit(ctx, record, revision if revision is not None else record.revision, changes, by, {"wake": False},
                 promoting=promoting)


def _end_council(ctx, record, actor) -> None:
    """The owner's word ends the brief's council where it is: approved before its end."""
    from kd.cabinet import council_rounds  # at call time: it loads the jobs, which load the API

    council_rounds.approved_early(ctx, record, actor)


def _start(ctx, principal, record, revision, text, actor, item):
    agent = item["agent"]
    _end_council(ctx, record, actor)
    approved = briefs.approve(ctx.store, record, by=actor)  # the version the person saw (``act`` checked it)
    changes = {}
    if agent != record.assignee:
        changes["assignee"] = agent
        foreign = foreign_specs(ctx, agent, record.labels)
        if foreign:
            changes["labels"] = [v for v in record.labels if v not in foreign]
    if approved is not None and approved.get("stages"):
        if changes:
            record = _edit(ctx, record, revision, changes, actor, {"wake": False})
        after = approve_with_stages(ctx, record, approved, by=actor, user=principal.user, principal=principal)
        _grant(ctx, ctx.store.card(record.id), actor)
        return after
    if record.status == "Inbox":
        changes["status"] = "To Do"
    if changes:
        changes["comment"] = {"text": f"[{principal.user}] To Do: в работу", "author": actor}
        after = _edit(ctx, record, revision, changes, actor)
    else:  # a To Do card on its agent: only brought to it again
        after = ctx.store.wake_card(record.id, actor=actor, agent=agent)
    _grant(ctx, ctx.store.card(record.id), actor)
    return after  # leaving Inbox closed BOSS's open promote in the same write (store.edit_card)


def _approve_stages(ctx, principal, record, revision, text, actor, item):
    """The aggregate's new brief version (more stages): approved, the added stages materialized once, the
    version's "Operations" granted as a start grants them."""
    _end_council(ctx, record, actor)
    approved = briefs.approve(ctx.store, record, by=actor)
    after = approve_with_stages(ctx, record, approved, by=actor, user=principal.user, revision=revision,
                                principal=principal)
    _grant(ctx, ctx.store.card(record.id), actor)
    return after if after is not None else ctx.store.card(record.id)


def _brief(ctx, principal, record, revision, text, actor, item):
    """The card goes to the analyst in Inbox with ``spec:task-brief`` and the analyst is told — one write
    (R12): no hand-over without its command. A rework keeps the versions and says in the thread what to change;
    a retry after a failure that has nothing to change only tells the analyst again."""
    labels = [  # a rework sizes the brief again: the old size goes with brief-ready
        v for v in record.labels
        if not v.startswith((worker_board.SPEC_PREFIX, briefs.SIZE)) and v != api.BRIEF_READY
    ] + [api.BRIEF_SPEC]
    changes = {}
    if record.assignee != api.ANALYST:
        changes["assignee"] = api.ANALYST
    if set(labels) != set(record.labels):
        changes["labels"] = labels
    if record.status != "Inbox":
        changes["status"] = "Inbox"
    if item["label"] == "action.brief_rework":
        last = briefs.latest(ctx.store, record.id)
        named = f"ТЗ v{last['version']}" if last else "ТЗ"
        changes["comment"] = {"text": f"[{principal.user}] {named}: на доработку — {text}", "author": actor}
    elif item["label"] == "action.brief":
        changes["comment"] = {"text": f"[{principal.user}] ТЗ: карточка передана {api.ANALYST}", "author": actor}
    if changes:
        _edit(ctx, record, revision, changes, actor)
    return ctx.store.wake_card(record.id, actor=actor, agent=api.ANALYST)


def _save_brief(ctx, principal, record, revision, text, actor, item):
    """The card's text as the brief's next version, a person's (R17): the card itself does not change."""
    current = ctx.store.card(record.id)
    if current.revision != revision:  # the text saved is the one the person saw
        raise api.ApiError("conflict", f"{record.id}: revision {revision} is not current ({current.revision})")
    briefs.add_version(ctx.store, current, origin="person", author=actor)
    return current


def _move(status):
    def run(ctx, principal, record, revision, text, actor, item):
        changes = {"status": status}
        if text:
            changes["comment"] = {"text": text, "author": actor}
        return _edit(ctx, record, revision, changes, actor)

    return run


_RUN = {
    "start": _start,
    "brief": _brief,
    "save_brief": _save_brief,
    "accept": _move(DONE),
    "return": _move("In Progress"),
    "resume": _move("In Progress"),
    "reopen": _move("To Do"),
    "restore": _move("Inbox"),
    "approve_stages": _approve_stages,
}


def _brief_version(request, item, record) -> None:
    """The brief version the page showed must be the action's (AC18): an older one does not start the new text."""
    sent = request.get("brief_version")
    if sent is not None and (isinstance(sent, bool) or not isinstance(sent, int)):
        raise api.ApiError("invalid", "brief_version must be an integer")
    if sent != item["brief_version"]:
        raise api.ApiError(
            "conflict",
            f"{record.id}: the brief is not the one shown (brief_version {sent} → {item['brief_version']}); "
            "read it again",
            reason=BRIEF_CHANGED,
        )


def act(ctx, principal, request) -> dict:
    """``user.card.act {card, action, revision, comment?, brief_version?}`` → the card, its new actions and the
    commands queued."""
    record = api._visible_card(ctx, principal, request.get("card"))
    api._require(ctx, principal, "cards.transition", record.project)
    action = request.get("action")
    if action not in ACTIONS:
        raise api.ApiError("invalid", f"unknown action {action!r}")
    revision = api._revision(request)
    item = next((a for a in actions(ctx, principal, record) if a["id"] == action), None)
    if revision != record.revision:
        if item is not None and request.get("brief_version") is not None:
            _brief_version(request, item, record)  # a page with an older brief hears that, not just «read again»
        raise api.ApiError("conflict", f"{record.id}: revision {revision} is not current ({record.revision})")
    if item is None:
        raise api.ApiError("refused", f"{record.id} is {record.status}: no {action} here", reason=NOT_OFFERED)
    if not item["enabled"]:
        raise api.ApiError("refused", item["message"], reason=item["reason"])
    text = _comment(request, item["comment"])
    _brief_version(request, item, record)
    actor = api.card_actor(principal)
    try:
        with ctx.store.transaction():
            after = _RUN[action](ctx, principal, record, revision, text, actor, item)
    except briefs.Stale as exc:  # the analyst handed a new text between the read and the write
        raise api.ApiError("conflict", str(exc), reason=BRIEF_CHANGED)
    except decisions.DecisionError as exc:
        raise api.ApiError(exc.kind, str(exc))
    except (store.Invalid, store.NotFound) as exc:
        raise api.ApiError("invalid", str(exc))
    queued = [{k: c[k] for k in ("id", "state", "agent", "kind")} for c in ctx.store.last_commands]
    return {**api._written(after), **api.for_people(ctx, principal, after), "commands": queued}


VERSION_FIELDS = ("version", "origin", "author", "created", "approved_by", "approved_at", "description", "acceptance",
                  "operations", "stages")


def brief_view(ctx, principal, request) -> dict:
    """``card.brief {card}`` → the brief's state, its versions (oldest first) each with what it changed from the
    one before and the council round that read it (None — none did), the card's text now and, for a stale
    brief, what changed since the last version (R17)."""
    record = api._visible_card(ctx, principal, request.get("card"))
    seen = briefs.state(ctx.store, record)
    read = councils.rounds_by_version(ctx.store, record.id)
    versions, previous = [], None
    for version in briefs.versions(ctx.store, record.id):
        text = {"description": version["description"], "acceptance": version["acceptance"],
                **({"operations": version["operations"]} if version["operations"] else {}),
                **({"stages": version["stages"]} if version["stages"] else {})}
        changes = briefs.diff(previous, text) if previous is not None else []
        versions.append({**{key: version[key] for key in VERSION_FIELDS}, "changes": changes,
                         "council": read.get(version["version"])})
        previous = text
    current = briefs.scope(record)
    changes = briefs.diff(previous, current) if seen["state"] == "stale" and previous is not None else []
    return {"brief": seen, "versions": versions, "current": current, "changes": changes}


def _alias(action):
    def handler(ctx, principal, request):
        return act(ctx, principal, {**request, "action": action})

    return handler


OPS = {
    "user.card.act": (frozenset({"user"}), act),
    "user.card.start": (frozenset({"user"}), _alias("start")),  # the page's older buttons
    "user.card.brief": (frozenset({"user"}), _alias("brief")),
    "card.brief": (frozenset({"user"}), brief_view),  # the brief tab: people read it
}
api.OPS.update(OPS)  # the board API serves them (api imports this module at its end)
