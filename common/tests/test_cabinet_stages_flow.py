"""Stages of a brief, materialized: the owner's one approval creates the child cards once; the parent is
an aggregate — unblocking, Review, its problems."""

import pytest

from kd import session_config
from kd.cabinet import api, briefs, flow, stages, users
from cabinet_env import CabinetEnv

STAGE_A = {"key": "a", "title": "Модель", "role": "@dev", "spec": "implementer", "description": "Схема и модуль.",
           "acceptance": ["Тест модели проходит"], "depends": [], "project": ""}
STAGE_B = {"key": "b", "title": "Веб", "role": "@dev", "spec": "docs", "description": "Вкладка.",
           "acceptance": ["Vitest проходит", "Типы сходятся"], "depends": ["a"], "project": ""}
STAGE_C = {"key": "c", "title": "Протокол", "role": "@sysadmin", "spec": "", "description": "",
           "acceptance": ["Протокол записан"], "depends": ["a", "b"], "project": ""}
SPECS = {"@dev": frozenset({"implementer", "docs"}), "@analyst": frozenset({"task-brief"}),
         "@sysadmin": frozenset(), "@boss": frozenset()}
HOSTED = session_config.HostConfig({"analyst": 1, "dev": 2, "boss": 1, "sysadmin": 1}, 600)


@pytest.fixture
def cab(tmp_path):
    e = CabinetEnv(tmp_path)
    users.Users(e.store).create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    api.install_host(e.ctx, lambda: HOSTED)
    e.ctx._specs = SPECS
    return e


def owner():
    return api.Principal("user", -1, "owner")


def planned(cab, stage_list=(STAGE_A, STAGE_B)):
    """A card the analyst handed to @dev in Inbox with a ready brief of these stages (v1)."""
    card = cab.card(status="Inbox", assignee="@analyst", labels=["spec:task-brief"])
    return cab.store.edit_card(card.id, None, {
        "description": "Цель.", "acceptance": [{"text": "Всё вместе работает", "checked": False}],
        "stages": list(stage_list), "assignee": "@dev", "labels_remove": ["spec:task-brief"],
        "labels_add": ["spec:implementer", "brief-ready"]}, actor="agent:analyst")


def act(cab, card_id, action, **extra):
    revision = cab.store.card(card_id).revision
    return api.dispatch(cab.ctx, owner(), {"op": "user.card.act", "card": card_id, "action": action,
                                           "revision": revision, **extra})


def children(cab, parent_id):
    return {r["key"]: cab.store.card(r["child"]) for r in stages.rows(cab.store, parent_id) if r["child"]}


def queued(cab, agent):
    return [c["card"] for c in cab.store.commands(agent=agent, states=["queued"])]


def test_start_with_stages_creates_the_children_and_wakes_nobody_on_the_parent(cab):
    card = planned(cab)
    out = act(cab, card.id, "start", brief_version=1)
    kids = children(cab, card.id)
    a, b = kids["a"], kids["b"]
    assert (a.status, a.assignee, a.parent, a.labels) == ("To Do", "@dev", card.id, ("spec:implementer",))
    assert a.description == STAGE_A["description"] and [x.text for x in a.acceptance] == STAGE_A["acceptance"]
    assert a.title == "Модель" and a.project == "digipaddock" and a.created_by == "@owner"
    assert (b.status, b.dependencies, b.labels) == ("Blocked", (a.id,), ("spec:docs",))
    parent = cab.store.card(card.id)
    assert parent.status == "In Progress" and parent.assignee == "@dev"
    assert briefs.latest(cab.store, card.id)["approved_by"] == "user:owner"
    assert queued(cab, "dev") == [a.id]  # the parent wakes nobody; a's assignment does
    assert [(c["agent"], c["kind"]) for c in out["commands"]] == [("dev", "assigned")]
    rows = stages.rows(cab.store, card.id)
    assert [(r["key"], r["version"], r["state"]) for r in rows] == [("a", 1, "created"), ("b", 1, "created")]
    kinds = [e for e in cab.store.events_since(0) if e["kind"] == "stage.materialized"]
    assert [(e["card"], e["payload"]["key"], e["payload"]["child"]) for e in kinds] == [
        (card.id, "a", a.id), (card.id, "b", b.id)]
    assert stages.is_aggregate(cab.store, card.id)
    comment = [m for m in cab.store.thread_messages(parent.thread) if m["kind"] == "text"][-1]
    assert a.id in comment["body"] and b.id in comment["body"]


def test_materialize_again_adds_only_the_missing_child(cab):
    card = planned(cab)
    act(cab, card.id, "start", brief_version=1)
    before = children(cab, card.id)
    # the intent of b left without its card (a failure between the two creates)
    cab.store._conn.execute("UPDATE stages SET child='', state='intent' WHERE card=? AND key='b'", (card.id,))
    cab.store._conn.commit()
    with cab.store.transaction():
        stages.materialize(cab.ctx, cab.store.card(card.id), briefs.latest(cab.store, card.id), by="user:owner")
    after = children(cab, card.id)
    assert after["a"].id == before["a"].id and after["b"].id != before["b"].id
    assert len(cab.store.cards(parent=card.id)) == 3  # the orphan of the failed run stays visible, no second group
    with cab.store.transaction():
        stages.materialize(cab.ctx, cab.store.card(card.id), briefs.latest(cab.store, card.id), by="user:owner")
    assert len(cab.store.cards(parent=card.id)) == 3


def test_a_boss_promote_approved_by_the_owner_materializes_like_a_start(cab):
    card = planned(cab)
    cab.call("digi-cabinet", {"op": "card.promote", "card": card.id, "revision": card.revision, "reason": "go"})
    kids = children(cab, card.id)
    assert kids["a"].status == "To Do" and kids["b"].status == "Blocked"
    assert cab.store.card(card.id).status == "In Progress"
    assert queued(cab, "dev") == [kids["a"].id]


def test_a_new_version_with_one_more_stage_is_approved_in_progress_and_adds_it_once(cab):
    card = planned(cab)
    act(cab, card.id, "start", brief_version=1)
    cab.store.edit_card(card.id, None, {"stages": [STAGE_A, STAGE_B, STAGE_C], "labels_add": ["brief-ready"]},
                        actor="agent:analyst")
    actions = {a["id"]: a for a in flow.actions(cab.ctx, owner(), cab.store.card(card.id))}
    assert actions["approve_stages"]["brief_version"] == 2
    assert actions["approve_stages"]["stages"] == [STAGE_A, STAGE_B, STAGE_C]
    with pytest.raises(api.ApiError, match="brief_version"):
        act(cab, card.id, "approve_stages", brief_version=1)
    act(cab, card.id, "approve_stages", brief_version=2)
    kids = children(cab, card.id)
    assert set(kids) == {"a", "b", "c"} and kids["c"].status == "Blocked"
    assert kids["c"].dependencies == (kids["a"].id, kids["b"].id) and kids["c"].assignee == "@sysadmin"
    assert len(cab.store.cards(parent=card.id)) == 3
    assert briefs.latest(cab.store, card.id)["approved_by"] == "user:owner"
    assert "approve_stages" not in {a["id"] for a in flow.actions(cab.ctx, owner(), cab.store.card(card.id))}
    # dropping a live stage is refused at the edit
    with pytest.raises(api.ApiError, match="materialized"):
        cab.call("digi", {"op": "worker.edit", "agent": "analyst", "card": card.id,
                         "revision": cab.store.card(card.id).revision, "stages": [STAGE_A, STAGE_B]})


def test_a_start_from_to_do_with_stages_is_an_aggregate_too(cab):
    card = planned(cab)
    cab.store.edit_card(card.id, None, {"status": "To Do"}, actor="user:owner")
    act(cab, card.id, "start", brief_version=1)
    assert cab.store.card(card.id).status == "In Progress"
    assert len(children(cab, card.id)) == 2


# --- the parent as an aggregate ----------------------------------------------------------------------------------
from kd import pipeline_config  # noqa: E402
from kd.cabinet import outcomes, problems, snapshot  # noqa: E402


def enforce(cab):
    cab.ctx.pipeline = lambda: pipeline_config.Config(mode="enforce", since="2000-01-01T00:00:00Z")
    cab.store._conn.execute("UPDATE meta SET value='2000-01-01T00:00:00Z' WHERE key='pipeline_since'")


def started(cab, stage_list=(STAGE_A, STAGE_B)):
    card = planned(cab, stage_list)
    act(cab, card.id, "start", brief_version=1)
    return cab.store.card(card.id), children(cab, card.id)


def close(cab, child, status="Done", **extra):
    return cab.store.edit_card(child.id, None, {"status": status, **extra}, actor="agent:dev")


def test_a_done_child_unblocks_the_next_and_the_last_puts_the_parent_in_review(cab):
    parent, kids = started(cab)
    close(cab, kids["a"])
    b = cab.store.card(kids["b"].id)
    assert b.status == "To Do"
    assert sorted(queued(cab, "dev")) == sorted([kids["a"].id, b.id])  # a's start and b's
    assert cab.store.card(parent.id).status == "In Progress"
    close(cab, b)
    after = cab.store.card(parent.id)
    assert after.status == "Review"
    comment = [m for m in cab.store.thread_messages(after.thread) if m["kind"] == "text"][-1]
    assert kids["a"].id in comment["body"] and b.id in comment["body"] and "этапы выполнены" in comment["body"]
    assert stages.facts(cab.store, parent.id)["done"] == 2


def test_a_cancelled_child_raises_stage_cancelled_and_unblocks_nothing(cab):
    enforce(cab)
    parent, kids = started(cab)
    close(cab, kids["a"], "Cancelled", outcome="cancelled", comment={"text": "не нужно", "author": "@dev"})
    assert cab.store.card(kids["b"].id).status == "Blocked"
    [problem] = problems.open_for(cab.store, parent.id)
    assert (problem["kind"], problem["agent"]) == ("stage_cancelled", "dev")
    boss = [c for c in cab.store.commands(agent="boss", states=["queued"]) if "stage" in c["text"]]
    assert len(boss) == 1 and boss[0]["card"] == parent.id and "closed without the work" in boss[0]["text"]
    assert cab.store.card(parent.id).status == "In Progress"


def test_sweep_raises_stage_missing_for_an_approved_stage_without_its_child(cab):
    enforce(cab)
    parent, kids = started(cab)
    cab.store._conn.execute("DELETE FROM stages WHERE card=? AND key='b'", (parent.id,))
    cab.store._conn.commit()
    assert stages.sweep(cab.ctx) == 1
    [problem] = problems.open_for(cab.store, parent.id)
    assert problem["kind"] == "stage_missing" and problem["detail"] == {"version": 1, "key": "b"}
    assert stages.sweep(cab.ctx) == 0  # once
    with cab.store.transaction():
        stages.materialize(cab.ctx, cab.store.card(parent.id), briefs.latest(cab.store, parent.id), by="user:owner")
    assert stages.sweep(cab.ctx) == 0
    assert problems.open_for(cab.store, parent.id) == []  # materialized since: resolved
    assert cab.call("digi-cabinet", {"op": "pipeline.sweep"})["stages"] == 0


def test_the_aggregate_waits_and_its_snapshot_names_the_stages(cab):
    parent, kids = started(cab)
    assert outcomes.next_state(cab.ctx, cab.store.card(parent.id)) == "waiting"
    assert outcomes.card_facts(cab.ctx, cab.store.card(parent.id))["stages"] == {
        "total": 2, "done": 0, "cancelled": 0, "chain": False,
        "items": [{"key": "a", "child": kids["a"].id, "status": "To Do", "role": "@dev", "title": "Модель",
                   "gate": "", "reason": ""},
                  {"key": "b", "child": kids["b"].id, "status": "Blocked", "role": "@dev", "title": "Веб",
                   "gate": "", "reason": ""}]}
    assert outcomes.card_facts(cab.ctx, kids["a"])["stages"] is None
    found = snapshot.facts(cab.ctx, cab.store.card(parent.id))
    nxt = snapshot.next_step(found)
    assert (nxt["what"], nxt["who"]) == ("wait_stages", "-") and "0 of 2" in nxt["text"]
    text = snapshot.render(snapshot.build(cab.ctx, cab.store.card(parent.id)))
    assert "next (derived): wait_stages" in text
    assert f"stages: 0 of 2 done — a {kids['a'].id} To Do @dev, b {kids['b'].id} Blocked @dev" in text
    close(cab, kids["a"])
    assert outcomes.next_state(cab.ctx, cab.store.card(parent.id)) == "waiting"
    # the sweep leaves the aggregate alone: nobody owes it a turn
    enforce(cab)
    assert cab.call("digi-cabinet", {"op": "pipeline.sweep"})["problems"] == 0


def test_done_of_the_parent_is_refused_while_a_child_is_open(cab):
    enforce(cab)
    parent, kids = started(cab)
    why = outcomes.done_refusal(cab.ctx, cab.store.card(parent.id), "dev")
    assert why  # the closing policy holds the aggregate too (its children are open conditions)


# --- the final review's findings (24.10) ---
def test_the_owner_approves_boss_s_real_promote_decision_on_a_staged_brief(cab):
    """Review F1: the approval of a promote decision goes through card.promote with ``promoting``; the decision
    ends approved, not revoked by the parent leaving Inbox."""
    from kd import boss_board, boss_promote
    from kd.cabinet import decisions

    card = planned(cab)
    ledger = decisions.Decisions(cab.store)
    proposal = boss_promote.propose(boss_board.card_from(cab.store.card(card.id).to_dict()), cab.ctx.routing(),
                                    card.revision, "ТЗ готово", now=cab.epoch(), root=cab.promote.proposals,
                                    decisions=cab.promote.decisions)
    d = cab.call("digi", {"op": "decision.open", "type": "promote", "agent": "boss", "card": card.id,
                         "payload": {"proposal_id": proposal.id, "proposal_sha256": proposal.sha256}})["decision"]
    out = cab.call("digi-cabinet", {"op": "decision.approve", "ids": [d["id"]], "user": "owner",
                                   "revisions": {card.id: card.revision}})
    assert out["results"][0]["ok"] if "results" in out else out
    assert ledger.get(d["id"])["state"] == "approved"
    kids = children(cab, card.id)
    assert set(kids) == {"a", "b"} and cab.store.card(card.id).status == "In Progress"


def test_a_cancelled_stage_keeps_the_parent_out_of_review_and_the_count_shows_it(cab):
    """Review F2: a plan with a hole is never accepted by itself; the facts count the cancelled stage."""
    enforce(cab)
    parent, kids = started(cab)
    close(cab, kids["a"], "Cancelled", outcome="cancelled", comment={"text": "не нужно", "author": "@dev"})
    cab.store.edit_card(kids["b"].id, None, {"status": "To Do", "dependencies": []}, actor="user:owner")
    close(cab, cab.store.card(kids["b"].id))
    assert cab.store.card(parent.id).status == "In Progress"
    facts = stages.facts(cab.store, parent.id)
    assert (facts["total"], facts["done"], facts["cancelled"]) == (2, 1, 1)
    assert [p["kind"] for p in problems.open_for(cab.store, parent.id)] == ["stage_cancelled"]


def test_a_duplicate_closed_dependency_unblocks_nothing(cab):
    """Review F2: Done with ``duplicate-of:`` is no result (as waits treat it)."""
    parent, kids = started(cab)
    close(cab, kids["a"], labels_add=["duplicate-of:KD-1"])
    assert cab.store.card(kids["b"].id).status == "Blocked"
    assert cab.store.card(parent.id).status == "In Progress"


def test_a_new_stage_after_done_ones_starts_in_to_do_and_a_reused_key_takes_the_new_text(cab):
    """Review F8: materialize on a re-plan — dependencies already Done do not block; the approved text wins."""
    parent, kids = started(cab)
    close(cab, kids["a"])
    changed_b = {**STAGE_B, "description": "Вкладка, новая редакция.", "acceptance": ["Новый критерий"]}
    cab.store.edit_card(parent.id, None, {"stages": [STAGE_A, changed_b, {**STAGE_C, "depends": ["a"]}],
                                          "labels_add": ["brief-ready"]}, actor="agent:analyst")
    act(cab, parent.id, "approve_stages", brief_version=2)
    after = children(cab, parent.id)
    assert after["c"].status == "To Do" and after["c"].dependencies == (kids["a"].id,)
    b = cab.store.card(kids["b"].id)
    assert b.description == "Вкладка, новая редакция." and [a.text for a in b.acceptance] == ["Новый критерий"]


def test_the_analyst_replans_an_aggregate_through_kd_task(cab):
    """Review F8: the re-plan is reachable — the analyst writes stages and brief-ready on the In Progress parent."""
    parent, kids = started(cab)
    revision = cab.store.card(parent.id).revision
    out = cab.call("digi", {"op": "worker.edit", "agent": "analyst", "card": parent.id, "revision": revision,
                           "stages": [STAGE_A, STAGE_B, STAGE_C], "add_label": ["brief-ready"]})
    assert out["card"]["labels"].count("brief-ready") == 1
    assert briefs.latest(cab.store, parent.id)["version"] == 2
    with pytest.raises(api.ApiError):  # dev, the holder, does not re-plan
        cab.call("digi", {"op": "worker.edit", "agent": "dev", "card": parent.id,
                         "revision": cab.store.card(parent.id).revision, "stages": [STAGE_A, STAGE_B, STAGE_C]})


def test_a_stage_in_another_project_needs_the_approver_s_right_there(cab):
    """Review F10: a person scoped to one project cannot create cards in another through a stage."""
    users.Users(cab.store).create("anna", "Анна", roles=[("owner", ["digipaddock"])], actor="root")
    cab.store.seed_projects([{"id": "other", "name": "Other", "route": "@dev"}])
    card = planned(cab, (STAGE_A, {**STAGE_B, "project": "other"}))
    anna = api.Principal("user", -1, "anna")
    with pytest.raises(api.ApiError, match="other"):
        api.dispatch(cab.ctx, anna, {"op": "user.card.act", "card": card.id, "action": "start",
                                     "revision": cab.store.card(card.id).revision, "brief_version": 1})
    assert children(cab, card.id) == {}
    act(cab, card.id, "start", brief_version=1)  # the owner may
    assert children(cab, card.id)["b"].project == "other"


# --- a stage closed as a duplicate or cancelled ---------------------------------------------------------------------------------------------
def test_a_stage_closed_as_a_duplicate_raises_stage_cancelled(cab):
    """A duplicate completes nothing: the plan has a hole, BOSS hears it as for a cancelled stage."""
    enforce(cab)
    parent, kids = started(cab)
    close(cab, kids["a"], labels_add=["duplicate-of:KD-1"])
    [problem] = problems.open_for(cab.store, parent.id)
    assert (problem["kind"], problem["key"]) == ("stage_cancelled", f"stage-cancelled:{kids['a'].id}")
    assert problem["detail"] == {"child": kids["a"].id, "duplicate": "KD-1"}


def test_a_done_stage_marked_a_duplicate_later_raises_stage_cancelled(cab):
    """Review M7: the label comes after the Done, in an edit with no status change — still a hole in the plan."""
    enforce(cab)
    parent, kids = started(cab)
    close(cab, kids["a"])
    assert problems.open_for(cab.store, parent.id) == []
    cab.store.edit_card(kids["a"].id, None, {"labels_add": ["duplicate-of:KD-1"]}, actor="agent:dev")
    [problem] = problems.open_for(cab.store, parent.id)
    assert problem["kind"] == "stage_cancelled" and problem["detail"] == {"child": kids["a"].id, "duplicate": "KD-1"}


def test_review_of_an_aggregate_with_open_stages_is_refused(cab):
    """Someone else's wake gives the parent's agent a turn: its review would jump the stages."""
    from test_cabinet_outcomes import finish, started as running

    enforce(cab)
    parent, kids = started(cab)
    running(cab, parent)
    with pytest.raises(api.ApiError) as exc:
        finish(cab, parent, outcome="review", note="готово")
    assert exc.value.kind == "refused" and kids["a"].id in str(exc.value) and kids["b"].id in str(exc.value)
    # review M8: what to do instead — the stages move it, the turn ends without an outcome
    assert "end the turn without an outcome" in str(exc.value)
