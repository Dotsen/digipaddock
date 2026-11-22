"""Stages of a brief.

A large brief splits into stages: each a self-contained task with its role, specialization, text, criteria and the
stages it waits for. They are part of the brief's versioned scope (like "Operations"): the analyst writes them with
``kd-task edit --stages``, the version's hash covers them, and the owner's one approval of the version turns them
into child cards — one per ``(card, version, key)`` row of the ``stages`` table, so a repeated approval or a retry
after a failure adds only what is missing. The parent is then an aggregate: no session of its own, its next step is
"waits for stages", Review when every child is Done.

Here — the shape and its checks; the materialization and the aggregate's rules live in this module too and are
added task by task.
"""

import re
from typing import Collection, List, Mapping, Optional

KEY = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
MAX_DEFAULT = 5  # the plan limit without delegation.json
FIELDS = ("key", "title", "role", "spec", "description", "acceptance", "depends", "project")
TITLE_MAX = 120


class StageError(ValueError):
    """The plan of stages is not acceptable; the message names the stage and the rule."""


def parse(value, *, roles: Collection[str], specs: Mapping[str, Collection[str]], projects: Collection[str],
          limit: int, keep: Collection[str] = ()) -> List[dict]:
    """``value`` as a normalized list of stages, or ``StageError``.

    Rules: a list of at most ``limit`` mappings; keys unique and of ``KEY``; a stage depends only on stages listed
    before it (no cycles, no forward or self references); ``role`` in ``roles``; ``spec`` empty or one of
    ``specs[role]``; a title and at least one criterion; ``project`` empty or one of ``projects``; every key of
    ``keep`` (stages already materialized) stays in the plan.
    """
    if not isinstance(value, (list, tuple)):
        raise StageError("stages must be a list")
    if len(value) > limit:
        raise StageError(f"at most {limit} stages")
    out: List[dict] = []
    seen: List[str] = []
    for i, raw in enumerate(value, 1):
        if not isinstance(raw, Mapping):
            raise StageError(f"stage {i}: not an object")
        key = str(raw.get("key", "")).strip()
        if not KEY.match(key):
            raise StageError(f"stage {i}: key {key!r} must be a-z, 0-9 and '-', up to 32 characters")
        if key in seen:
            raise StageError(f"{key}: listed twice")
        title = str(raw.get("title", "")).strip()
        if not title or len(title) > TITLE_MAX:
            raise StageError(f"{key}: a title of 1..{TITLE_MAX} characters is required")
        role = str(raw.get("role", "")).strip()
        if role and not role.startswith("@") and "@" + role in roles:
            role = "@" + role  # a bare name in a brief: the agent it names
        if role not in roles:
            raise StageError(f"{key}: role {role!r} is not one of {', '.join(sorted(roles))}")
        spec = str(raw.get("spec", "") or "").strip()
        if spec and spec not in (specs.get(role) or ()):
            raise StageError(f"{key}: specialization {spec!r} is not in the set of {role}")
        acceptance = [_plain(key, "acceptance", a) for a in (raw.get("acceptance") or ()) if str(a).strip()]
        if not acceptance:
            raise StageError(f"{key}: at least one acceptance criterion is required")
        if len(acceptance) > ACCEPTANCE_MAX:
            raise StageError(f"{key}: at most {ACCEPTANCE_MAX} acceptance criteria")
        description = str(raw.get("description", "") or "").strip()
        if description:
            description = _plain(key, "description", description)
        depends = [str(d).strip() for d in (raw.get("depends") or ())]
        for dep in depends:
            if dep not in seen:
                raise StageError(f"{key}: depends on {dep!r}, which is not an earlier stage")
        project = str(raw.get("project", "") or "").strip()
        if project and project not in projects:
            raise StageError(f"{key}: project {project!r} is unknown")
        out.append({"key": key, "title": title, "role": role, "spec": spec,
                    "description": description, "acceptance": acceptance,
                    "depends": depends, "project": project})
        seen.append(key)
    missing = [k for k in keep if k not in seen]
    if missing:
        raise StageError(f"{missing[0]}: materialized stages cannot be dropped — close the child card instead")
    return out


def _plain(key: str, field: str, value) -> str:
    """A stage's text under the rules every agent-written card text follows (``worker_board._text``)."""
    from kd import worker_board

    try:
        return worker_board._text(value)
    except worker_board.Refused as exc:
        raise StageError(f"{key}: {field}: {exc}")


def normalized(ctx, card, value) -> List[dict]:
    """An agent's "Stages" for ``card`` (``api._worker``): parsed against the host — the agents a card can be started
    on, their specializations, the board's projects and the stage limit — and the stages already materialized on
    the card; refused as ``ApiError("invalid")`` with the rule."""
    from kd.cabinet import api, flow

    try:
        return parse(value, roles=flow.takers(ctx), specs=ctx.specs(), projects=frozenset(ctx.routing().projects),
                     limit=ctx.stage_limit(), keep=materialized_keys(ctx.store, card.id))
    except StageError as exc:
        raise api.ApiError("invalid", f"stages: {exc}")


def rows(store, card_id: str) -> List[dict]:
    """The stages of ``card_id`` as the table holds them — the newest version's row per key, in the order of the
    keys' first appearance — each with its child's status, assignee and title (empty for an intent without a card)."""
    found = store._conn.execute(
        "SELECT s.key, s.version, s.child, s.state, s.created, s.updated, c.status AS child_status,"
        " c.assignee AS child_assignee, c.title AS child_title FROM stages s LEFT JOIN cards c ON c.id = s.child"
        " WHERE s.card=? ORDER BY s.version DESC, s.rowid", (card_id,)
    ).fetchall()
    newest: dict = {}
    for row in found:
        newest.setdefault(row["key"], dict(row))
    first = store._conn.execute(
        "SELECT key, MIN(rowid) AS first FROM stages WHERE card=? GROUP BY key ORDER BY first", (card_id,)
    ).fetchall()
    out = []
    for row in first:
        item = newest[row["key"]]
        for key in ("child_status", "child_assignee", "child_title"):
            item[key] = item[key] or ""
        out.append(item)
    return out


def is_aggregate(store, card_id: str) -> bool:
    """A card at least one of whose stages has its child card: it is worked through them, not on itself."""
    return store._conn.execute(
        "SELECT 1 FROM stages WHERE card=? AND child != '' LIMIT 1", (card_id,)
    ).fetchone() is not None


def is_chain(store, card_id: str) -> bool:
    """A chain plan: its stages are one role's in one repository, run one after another on one branch and one
    pull request of the parent, and no stage waits for the owner's acceptance. Decided when the first children
    are made and kept for the plan's life."""
    return store._conn.execute(
        "SELECT 1 FROM stages WHERE card=? AND chain=1 LIMIT 1", (card_id,)
    ).fetchone() is not None


def chain_shape(ctx, record, version: Mapping) -> bool:
    """Whether the version's stages can run as a chain: one role, one project, and that project is bound to a
    repository (the stages share its branch and its pull request). The repository registry is the truth of that —
    a project root bound by hand has nothing in its own row; an unreadable registry binds nothing."""
    from kd.cabinet import api, projects_api  # at call time: it imports the API

    found = version.get("stages") or ()
    roles = {stage["role"] for stage in found}
    projects = {stage.get("project") or record.project for stage in found}
    if len(roles) != 1 or len(projects) != 1:
        return False
    try:
        return next(iter(projects)) in projects_api.bound_repos(ctx.repos_path)
    except api.ApiError:
        return False


def chain_eligible(ctx, record, version: Mapping, lead: str = "") -> bool:
    """A new plan is a chain when its shape allows it, its stages' role is the agent that leads the plan (the one
    pull request is the plan's card's: that agent checks it, publishes it and merges it), the pipeline enforces (the
    cabinet starts the turns a chain needs) and the root has not switched chains off. ``lead`` is that agent when
    the card is not on it yet (a start's hint while the analyst holds it); by default the card's assignee."""
    config = ctx.pipeline()
    lead = lead or record.assignee
    led = all(stage["role"] == lead for stage in version.get("stages") or ())
    return config.enforcing and config.chain and led and chain_shape(ctx, record, version)


def _child_of(store, card_id: str, key: str) -> str:
    """The child an older version of the plan already made for this key (a re-approval reuses it)."""
    row = store._conn.execute(
        "SELECT child FROM stages WHERE card=? AND key=? AND child != '' ORDER BY version DESC LIMIT 1",
        (card_id, key)).fetchone()
    return row["child"] if row else ""


def check_projects(ctx, record, version: Mapping, principal) -> None:
    """A stage outside the parent's project creates a card there: the approver must hold ``cards.transition``
    in that project too (a partner scoped to one project cannot reach another through a stage, review F10)."""
    from kd.cabinet import api

    for stage in version.get("stages") or ():
        project = stage.get("project") or record.project
        if project != record.project and not api.allowed(ctx, principal, "cards.transition", project):
            raise api.ApiError("refused", f"stage {stage['key']}: no permission to start work in project {project}")


def materialize(ctx, record, version: Mapping, *, by: str) -> List[dict]:
    """The children of ``version``'s stages, created once each, inside the caller's transaction.

    One row per ``(card, version, key)``: a row with a child is kept; a missing row or one without a child
    creates the child — a key an older version already made is reused — and records it, with an event
    ``stage.materialized``. A stage without dependencies starts in To Do (the board's rule wakes its agent),
    one with them in Blocked on the real ids of its dependencies. In a chain plan (``is_chain``; a plan without
    children yet becomes one when ``chain_eligible``) a new stage also waits for the stage before it in the plan:
    one branch has one writer at a time. Returns ``rows``."""
    store = ctx.store
    chain = is_chain(store, record.id) if is_aggregate(store, record.id) else chain_eligible(ctx, record, version)
    previous = ""
    created_by = api_owner() if by.startswith("user:") else by
    made: dict = {r["key"]: r["child"] for r in rows(store, record.id) if r["child"]}
    now = store._clock()
    number = int(version["version"])
    for stage in version.get("stages") or ():
        key = stage["key"]
        row = store._conn.execute("SELECT child FROM stages WHERE card=? AND version=? AND key=?",
                                  (record.id, number, key)).fetchone()
        if row is not None and row["child"]:
            made[key] = previous = row["child"]
            continue
        child = made.get(key) or _child_of(store, record.id, key)
        if child:
            _retext(store, child, stage, by)  # the approved text wins over an older version's (review F8)
        else:
            deps = [made[d] for d in stage.get("depends") or () if d in made]
            if chain and previous and previous not in deps:
                deps.append(previous)
            open_deps = [d for d in deps if not _done(store, d)]
            values = {
                "title": stage["title"], "project": stage.get("project") or record.project,
                "status": "Blocked" if open_deps else "To Do", "assignee": stage["role"],
                "labels": [f"spec:{stage['spec']}"] if stage.get("spec") else [],
                "description": stage.get("description", ""),
                "acceptance": [{"text": a, "checked": False} for a in stage.get("acceptance") or ()],
                "dependencies": deps, "parent": record.id, "created_by": created_by,
            }
            child = store.create_card(values, actor=by).id
        if row is None:
            store._conn.execute(
                "INSERT INTO stages (card, version, key, child, state, created, updated, chain)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (record.id, number, key, child, "created", now, now, int(chain)))
        else:
            store._conn.execute("UPDATE stages SET child=?, state='created', updated=?, chain=? WHERE card=?"
                                " AND version=? AND key=?", (child, now, int(chain), record.id, number, key))
        store._event(by, "stage.materialized", record.id, {"version": number, "key": key, "child": child},
                     record.thread)
        made[key] = previous = child
    return rows(store, record.id)


def _retext(store, child_id: str, stage: Mapping, by: str) -> None:
    """An open child whose approved description or criteria changed takes the new text (its ticks are reset with
    the criteria); a closed child is history."""
    try:
        child = store.card(child_id)
    except Exception:  # noqa: BLE001 — a child gone with its board rows: nothing to update
        return
    if child.status in (DONE, CANCELLED):
        return
    changes = {}
    if (stage.get("description") or "") != child.description:
        changes["description"] = stage.get("description") or ""
    wanted = list(stage.get("acceptance") or ())
    if wanted != [a.text for a in child.acceptance]:
        changes["acceptance"] = [{"text": a, "checked": False} for a in wanted]
    if changes:
        store.edit_card(child_id, None, changes, actor=by)


def _done(store, card_id: str) -> bool:
    """Done, and a result: a card closed as a duplicate (``duplicate-of:``) completes nothing (as waits judge)."""
    try:
        card = store.card(card_id)
    except Exception:  # noqa: BLE001 — a dependency that is gone never completes
        return False
    return card.status == DONE and not any(v.startswith(DUPLICATE) for v in card.labels)


def api_owner() -> str:
    from kd.cabinet import api

    return api.OWNER


# --- the parent as an aggregate ------------------------------------------------------------------------------
DONE, CANCELLED, BLOCKED, TO_DO, IN_PROGRESS, REVIEW = "Done", "Cancelled", "Blocked", "To Do", "In Progress", "Review"
DUPLICATE = "duplicate-of:"  # as ``waits.DUPLICATE``: a card closed as a duplicate completes nothing
ACCEPTANCE_MAX = 20
SYSTEM = "system"
# the wake of a chain plan's agent when its last stage is closed: the plan's pull requests are still drafts
FINAL = (
    "[board] {id}: every stage is handed in — publish every pull request of the plan (kd-forge publish in each"
    " repository it has one), lead each to the owner's decision, then merge them in the order the brief names"
)


def final_text(store, parent_id: str) -> str:
    """``FINAL`` with the plan's open pull requests at the wake — ``<project>#<number>`` by project and number, the
    repository for a row with no project — so no repository is missed; with none open, ``FINAL`` as it is."""
    pulls = sorted((pr["project"] or pr["repo"], pr["number"]) for pr in store.prs(card=parent_id, state="open"))
    named = ", ".join(f"{project}#{number}" for project, number in pulls)
    return FINAL.format(id=parent_id) + (f": {named}" if named else "")


def facts(store, card_id: str) -> Optional[dict]:
    """The aggregate's stages for its page and snapshot — how many, how many Done, each with its child's state
    and, in a chain plan, its word at the gate and why it is held (``stage_gate.labels``) — or None for a card
    that is no aggregate."""
    found = [r for r in rows(store, card_id) if r["child"]]
    if not found:
        return None
    chain, gates = is_chain(store, card_id), {}
    if chain:  # where each stage stands between its hand-in and the next stage's start
        from kd.cabinet import stage_gate  # at call time: it imports this module

        gates = stage_gate.labels(store, card_id)
    items = [{"key": r["key"], "child": r["child"], "status": r["child_status"], "role": r["child_assignee"],
              "title": r["child_title"], **gates.get(r["child"], {"gate": "", "reason": ""})} for r in found]
    return {"total": len(items), "done": sum(1 for i in items if i["status"] == DONE),
            "cancelled": sum(1 for i in items if i["status"] == CANCELLED), "chain": chain, "items": items}


def open_children(store, card_id: str) -> List[str]:
    """The ids of the stages' children not closed yet."""
    return [r["child"] for r in rows(store, card_id)
            if r["child"] and r["child_status"] not in (DONE, CANCELLED)]


def parent_of(store, child_id: str) -> str:
    """The plan a stage's card belongs to, or '' for any other card."""
    row = store._conn.execute("SELECT card FROM stages WHERE child=? LIMIT 1", (child_id,)).fetchone()
    return row["card"] if row else ""


def on_card_changed(ctx, event: Mapping) -> None:
    """A stage's child moved (``card.changed``, inside the write's transaction): Done frees the siblings that waited
    only for it (Blocked → To Do, the board's rule wakes their agent) and, when every stage is Done, puts the parent
    in Review for the owner — a chain plan's agent is woken instead (``FINAL``): its pull requests go to the
    council and the owner's decision first. Cancelled, or Done as a duplicate (``duplicate-of:``), raises
    ``stage_cancelled`` on the parent (the plan has a hole) — also when the label comes after the Done. A closed
    stage of a chain plan that goes back to work raises ``stage_reopened`` (the stages after it build on its
    commits); its next Done settles that."""
    payload = event.get("payload") or {}
    was, now = payload.get("before") or {}, payload.get("after") or {}
    before, after = was.get("status"), now.get("status")

    def marked(side):
        return any(str(v).startswith(DUPLICATE) for v in side.get("labels") or ())
    child_id = event.get("card")
    if child_id and before == DONE and after not in (None, DONE, CANCELLED):
        _reopened(ctx, child_id)
        return
    late_duplicate = before == after == DONE and marked(now) and not marked(was)
    if not child_id or after not in (DONE, CANCELLED) or (before == after and not late_duplicate):
        return
    parent_id = parent_of(ctx.store, child_id)
    if not parent_id:
        return
    s = ctx.store
    try:
        parent = s.card(parent_id)
    except Exception:  # noqa: BLE001 — a parent gone with its board rows: nothing to move
        return
    from kd.cabinet import problems

    duplicate = next((v[len(DUPLICATE):] for v in s.card(child_id).labels if v.startswith(DUPLICATE)), None)
    if after == CANCELLED or duplicate is not None:
        problems.raise_problem(ctx, key=f"stage-cancelled:{child_id}", record=parent, kind="stage_cancelled",
                               agent=parent.assignee[1:] if parent.assignee.startswith("@") else "",
                               detail={"child": child_id, **({"duplicate": duplicate} if duplicate else {})})
        return
    chain = is_chain(s, parent_id)
    if chain:  # the stage that was reopened is closed again
        problems.resolve(s, parent_id, ("stage_reopened",), "stage_done", prefix=f"stage-reopened:{child_id}:")
    for sibling_id in open_children(s, parent_id):
        sibling = s.card(sibling_id)
        if sibling.status != BLOCKED or not sibling.dependencies:
            continue
        if all(_done(s, d) for d in sibling.dependencies):
            s.edit_card(sibling_id, None, {"status": TO_DO, "comment": {
                "text": f"[{SYSTEM}] зависимости выполнены — этап можно брать", "author": SYSTEM}}, actor=SYSTEM)
    made = [r["child"] for r in rows(s, parent_id) if r["child"]]
    if parent.status == IN_PROGRESS and made and all(_done(s, c) for c in made):
        done = made  # every stage a real Done: a cancelled or duplicate one keeps the plan open (review F2)
        agent = parent.assignee[1:] if parent.assignee.startswith("@") else ""
        if chain and agent and ctx.pipeline().enforcing:
            _final(ctx, parent, agent, done)
            return
        s.edit_card(parent_id, None, {"status": REVIEW, "comment": {
            "text": f"[{SYSTEM}] этапы выполнены: {', '.join(done)} — карточка на приёмке", "author": SYSTEM}},
            actor=SYSTEM)


def _handins(store, column: str, card_id: str) -> int:
    """The newest hand-in of a stage (``card``) or of a plan (``parent``), 0 before the first one."""
    return store._conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM stage_reviews WHERE {column}=?", (card_id,)).fetchone()[0]


def _final(ctx, parent, agent: str, done: List[str]) -> None:
    """Every stage of a chain plan is closed: its agent is woken to publish the plan's pull request in every
    repository for the council and the owner's decision on each, merge them and hand the plan in — once per state
    of the stages."""
    from kd.cabinet import continuations

    s = ctx.store
    key = f"chain-final:{parent.id}:{len(done)}:{_handins(s, 'parent', parent.id)}"
    if continuations._get(s, key) is not None:
        return
    continuations.launch(ctx, key=key, record=parent, agent=agent, source="stage", text=final_text(s, parent.id))
    s.add_message(parent.id, author=SYSTEM, kind="text", body=(
        f"[{SYSTEM}] этапы выполнены: {', '.join(done)} — исполнитель публикует PR плана в каждом репозитории"
        " на совет и решение владельца"))


def _reopened(ctx, child_id: str) -> None:
    """A closed stage of a chain plan went back to work: the coordinator is asked once per closing."""
    from kd.cabinet import problems

    s = ctx.store
    parent_id = parent_of(s, child_id)
    if not parent_id or not is_chain(s, parent_id):
        return
    try:
        parent = s.card(parent_id)
    except Exception:  # noqa: BLE001 — a parent gone with its board rows: nobody to ask about
        return
    problems.raise_problem(ctx, key=f"stage-reopened:{child_id}:{_handins(s, 'card', child_id)}", record=parent,
                           kind="stage_reopened", agent=parent.assignee[1:] if parent.assignee.startswith("@") else "",
                           detail={"child": child_id})


def _status(store, card_id: str) -> str:
    try:
        return store.card(card_id).status
    except Exception:  # noqa: BLE001 — a dependency that is gone never completes
        return ""


def sweep(ctx) -> int:
    """Every card whose latest brief version is approved with stages: a stage without its child is a problem
    ``stage_missing`` for the coordinator (once per card, version and key); one materialized since resolves it.
    Returns how many problems were raised."""
    from kd.cabinet import briefs, problems

    s, raised = ctx.store, 0
    cards = s._conn.execute(
        "SELECT v.card FROM brief_versions v WHERE v.approved_by != '' AND v.stages != '' AND v.version ="
        " (SELECT MAX(version) FROM brief_versions WHERE card = v.card)"
        " AND v.card IN (SELECT id FROM cards WHERE status NOT IN ('Done', 'Cancelled'))"
    ).fetchall()
    with s.transaction():
        for (card_id,) in cards:
            version = briefs.latest(s, card_id)
            made = {r["key"] for r in rows(s, card_id) if r["child"]}
            missing = [st["key"] for st in version["stages"] if st["key"] not in made]
            if not missing:
                if problems.resolve(s, card_id, ["stage_missing"], by="materialized"):
                    continue
                continue
            record = s.card(card_id)
            for key in missing:
                if problems.raise_problem(ctx, key=f"stage-missing:{card_id}:{version['version']}:{key}",
                                          record=record, kind="stage_missing",
                                          agent=record.assignee[1:] if record.assignee.startswith("@") else "",
                                          detail={"version": version["version"], "key": key}):
                    raised += 1
    return raised


def materialized_keys(store, card_id: str) -> List[str]:
    """The keys of ``card_id``'s stages whose child card exists and is not closed: the plan must keep them."""
    rows = store._conn.execute(
        "SELECT s.key FROM stages s JOIN cards c ON c.id = s.child WHERE s.card=? AND s.child != ''"
        " AND c.status NOT IN ('Done', 'Cancelled') ORDER BY s.version, s.rowid", (card_id,)
    ).fetchall()
    return [r[0] for r in rows]
