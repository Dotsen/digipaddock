"""the backend's actions on a card (remediation spec 3.1, 3.2) — what a person may do next, the checks,
and ``user.card.act``: the card, the person's comment and the agent's command in one write."""

from pathlib import Path
import re

import pytest

from kd import session_config
from kd.cabinet import api, briefs, decisions, flow, i18n, schema, store, users
from cabinet_env import CabinetEnv

HOSTED = session_config.HostConfig({"analyst": 1, "dev": 1, "boss": 1}, 600)
# who holds which specialization (the registry's answer, kd.specializations.agent_specs)
SPECS = {
    "@dev": frozenset({"implementer", "test-author", "reviewer", "tester", "docs"}),
    "@analyst": frozenset({"task-brief"}),
    "@sysadmin": frozenset(),
    "@boss": frozenset(),
}


@pytest.fixture
def env(tmp_path):
    e = CabinetEnv(tmp_path)
    u = users.Users(e.store)
    u.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    u.create("vera", "Вера", roles=[("viewer", "*")], actor="user:owner")
    api.install_host(e.ctx, lambda: HOSTED)
    e.ctx._specs = SPECS
    return e


def person(user="owner"):
    return api.Principal("user", -1, user)


def _actions(env, card_id, user="owner"):
    return api.dispatch(env.ctx, person(user), {"op": "card.get", "card": card_id})["actions"]


def _ids(actions):
    return [(a["id"], a["primary"], a["enabled"], a["reason"]) for a in actions]


def _act(env, card_id, action, user="owner", **extra):
    revision = env.store.card(card_id).revision
    return api.dispatch(
        env.ctx, person(user), {"op": "user.card.act", "card": card_id, "action": action, "revision": revision, **extra}
    )


def _refused(env, card_id, action, **extra):
    with pytest.raises(api.ApiError) as exc:
        _act(env, card_id, action, **extra)
    return exc.value


# --- the table: one row per status ------------------------------------------------------------
def test_inbox_offers_start_first_and_a_brief(env):
    card = env.card(status="Inbox", assignee="@dev")
    actions = _actions(env, card.id)
    assert _ids(actions) == [("start", True, True, ""), ("brief", False, True, "")]
    assert [a["agent"] for a in actions] == ["@dev", "@analyst"]
    assert all(a["comment"] == "none" for a in actions)


def test_inbox_with_a_legacy_brief_offers_its_start_and_a_rework(env):
    card = env.card(status="Inbox", assignee="@dev", labels=["spec:implementer", "brief-ready"])
    [start, brief] = _actions(env, card.id)
    assert (start["id"], start["primary"], start["label"]) == ("start", True, "action.start_legacy")
    assert (brief["id"], brief["label"], brief["comment"]) == ("brief", "action.brief_rework", "required")


def _analyst_works(env, card):
    """The analyst's turn on the card is running (a started command), as the session host reports it."""
    command = env.store.enqueue({"key": f"work-{card.id}", "agent": "analyst", "thread": card.thread, "card": card.id,
                                 "kind": "assigned", "text": f"[board] {card.id} assigned to you",
                                 "initiator": "user:owner"})
    env.store.command_state(command["id"], "delivered")
    env.store.command_state(command["id"], "started")


def test_inbox_while_the_analyst_writes_the_brief_starts_only_on_purpose(env):
    card = env.card(status="Inbox", assignee="@analyst", labels=["spec:task-brief"])
    _analyst_works(env, card)
    [start] = _actions(env, card.id)
    assert _ids([start]) == [("start", False, True, "brief_writing")] and start["agent"] == "@dev"


def test_an_analyst_card_nobody_woke_offers_the_retry(env):
    card = env.card(status="Inbox", assignee="@analyst", labels=["spec:task-brief"])
    assert (briefs.state(env.store, card)["state"], briefs.state(env.store, card)["error"]) == (
        "failed", "brief_not_started")
    start, retry = _actions(env, card.id)
    assert (start["id"], start["primary"]) == ("start", False)
    assert (retry["id"], retry["label"], retry["primary"]) == ("brief", "action.brief_retry", False)


def test_to_do_offers_start_again_and_a_brief(env):
    card = env.card(status="To Do", assignee="@dev")
    actions = _actions(env, card.id)
    assert [(a["id"], a["label"]) for a in actions] == [("start", "action.start_again"), ("brief", "action.brief")]


def test_in_progress_offers_nothing(env):
    assert _actions(env, env.card(status="In Progress").id) == []


def test_review_offers_accept_first_and_a_return_with_a_comment(env):
    actions = _actions(env, env.card(status="Review").id)
    assert _ids(actions) == [("accept", True, True, ""), ("return", False, True, "")]
    assert [a["comment"] for a in actions] == ["none", "required"]


def test_blocked_offers_resume(env):
    assert _ids(_actions(env, env.card(status="Blocked").id)) == [("resume", True, True, "")]


def test_done_offers_reopen_with_an_optional_comment(env):
    [reopen] = _actions(env, env.card(status="Done").id)
    assert (reopen["id"], reopen["comment"], reopen["agent"]) == ("reopen", "optional", "@dev")


def test_a_viewer_gets_no_actions(env):
    card = env.card(status="Review")
    assert _actions(env, card.id, user="vera") == []
    with pytest.raises(api.ApiError) as exc:
        _act(env, card.id, "accept", user="vera")
    assert exc.value.kind == "refused"


# --- checks -----------------------------------------------------------------------------------
def test_resume_is_disabled_while_a_dependency_is_open(env):
    dep = env.card(status="In Progress")
    card = env.card(status="Blocked", dependencies=[dep.id])
    [resume] = _actions(env, card.id)
    assert (resume["enabled"], resume["reason"], resume["params"]) == (False, "deps_open", {"cards": dep.id})
    error = _refused(env, card.id, "resume")
    assert (error.kind, error.reason) == ("refused", "deps_open") and dep.id in str(error)
    env.store.edit_card(dep.id, None, {"status": "Done"}, actor="user:owner")
    assert _act(env, card.id, "resume")["card"]["status"] == "In Progress"


def test_start_refuses_a_spec_the_taker_does_not_hold(env):
    card = env.card(status="Inbox", assignee="@sysadmin", project="admin", labels=["spec:implementer"])
    [start, _] = _actions(env, card.id)
    assert (start["enabled"], start["reason"]) == (False, "spec_mismatch")
    assert _refused(env, card.id, "start").reason == "spec_mismatch"
    assert env.store.card(card.id).status == "Inbox"


def test_start_with_nobody_to_take_it_is_refused(env):
    card = env.card(status="Inbox", assignee="", project="web")  # routed to the owner
    assert _refused(env, card.id, "start").reason == "no_agent"


def test_start_while_the_brief_is_written_hands_it_to_the_route_without_the_brief_spec(env):
    card = env.card(status="Inbox", assignee="@analyst", labels=["ui", "spec:task-brief"])
    after = _act(env, card.id, "start")["card"]
    assert (after["status"], after["assignee"], after["labels"]) == ("To Do", "@dev", ["ui"])


def test_start_revokes_an_open_promote_and_starts_once(env):
    card = env.card(status="Inbox", assignee="@dev", labels=["spec:implementer", "brief-ready"])
    ledger = decisions.Decisions(env.store)
    proposal = ledger.open("promote", card=card.id, payload={"why": "ТЗ готово"}, requested_by="agent:boss", now=env.now)
    out = _act(env, card.id, "start", brief_version=0)
    assert out["card"]["status"] == "To Do"
    closed = ledger.get(proposal["id"])
    assert closed["state"] == "revoked" and closed["decided_by"] == "user:owner"
    assert closed["reply"] == "the card left Inbox: To Do by @owner"
    assert [c["agent"] for c in out["commands"]] == ["dev"] and len(env.store.commands(card=card.id)) == 1
    with pytest.raises(decisions.DecisionError):
        ledger.approve(proposal["id"], user="owner", now=env.now)


@pytest.mark.parametrize("status, actor", [("In Progress", "root"), ("Done", "user:owner"), ("To Do", "agent:boss")])
def test_any_road_out_of_inbox_closes_an_open_promote(env, status, actor):
    """A card that left Inbox other than by "Take into work" kept BOSS's promote open, stale in the Inbox
    with nothing to press, until it expired three days later."""
    card = env.card(status="Inbox", assignee="@dev")
    ledger = decisions.Decisions(env.store)
    promote = ledger.open("promote", card=card.id, payload={"why": "ТЗ готово"}, requested_by="agent:boss", now=env.now)
    question = ledger.open("question", card=card.id, payload={"text": "?"}, requested_by="agent:dev", now=env.now)
    env.store.edit_card(card.id, None, {"status": status}, actor=actor)
    closed = ledger.get(promote["id"])
    assert (closed["state"], closed["decided_by"]) == ("revoked", actor)
    assert closed["reply"] == f"the card left Inbox: {status} by {actor if actor == 'root' else '@' + actor.split(':')[1]}"
    # only the promote is moot — unless the card closed, which withdraws every open decision
    assert ledger.get(question["id"])["state"] == ("revoked" if status == "Done" else "open")


def test_an_edit_within_inbox_closes_the_promote(env):
    """The approval binds the card's revision — after any edit it can never be given, and a promote left
    open showed no "Approve" and no reason (BOSS's own brief order changed the card)."""
    card = env.card(status="Inbox", assignee="@dev")
    ledger = decisions.Decisions(env.store)
    promote = ledger.open("promote", card=card.id, payload={"why": "ТЗ готово"}, requested_by="agent:boss", now=env.now)
    question = ledger.open("question", card=card.id, payload={"text": "?"}, requested_by="agent:dev", now=env.now)
    env.store.edit_card(card.id, None, {"assignee": "@analyst", "labels_add": ["spec:task-brief"]}, actor="agent:boss")
    closed = ledger.get(promote["id"])
    assert (closed["state"], closed["decided_by"]) == ("revoked", "agent:boss")
    assert closed["reply"] == "the card changed after the proposal (assignee, labels) by @boss: propose it again"
    said = [m for m in env.store.messages(card.id) if m["kind"] == "decision"][-1]
    assert said["payload"]["key"] == "sys.decision.superseded" and said["payload"]["decision"] == promote["id"]
    assert ledger.get(question["id"])["state"] == "open"  # only the promote is bound to the revision


def test_act_answers_with_the_commands_it_queued(env):
    card = env.card(status="Inbox", assignee="@dev")
    out = _act(env, card.id, "start")
    [command] = out["commands"]
    assert set(command) == {"id", "state", "agent", "kind"}
    assert (command["agent"], command["kind"], command["state"]) == ("dev", "assigned", "queued")
    assert [a["id"] for a in out["actions"]] == ["start", "brief"] and out["changed"]


def test_act_on_a_stale_revision_is_a_conflict(env):
    card = env.card(status="Review")
    with pytest.raises(api.ApiError) as exc:
        api.dispatch(env.ctx, person(), {"op": "user.card.act", "card": card.id, "action": "accept", "revision": 99})
    assert exc.value.kind == "conflict"


def test_an_action_the_status_does_not_offer_is_refused(env):
    card = env.card(status="In Progress")
    assert _refused(env, card.id, "accept").reason == "not_offered"
    assert _refused(env, card.id, "reboot").kind == "invalid"


# --- the moves ---------------------------------------------------------------------------------
def test_return_needs_a_comment_and_continues_by_it(env):
    card = env.card(status="Review")
    assert _refused(env, card.id, "return").kind == "invalid"
    out = _act(env, card.id, "return", comment="Добавь тест на пустой список")
    assert out["card"]["status"] == "In Progress"
    assert env.store.messages(card.id)[-1]["body"] == "Добавь тест на пустой список"
    assert [(c["agent"], c["kind"]) for c in out["commands"]] == [("dev", "continue")]


def test_accept_closes_the_card(env):
    card = env.card(status="Review")
    out = _act(env, card.id, "accept")
    assert out["card"]["status"] == "Done" and out["commands"] == []


def test_reopen_brings_the_card_back_to_its_agent(env):
    card = env.card(status="Done")
    out = _act(env, card.id, "reopen", comment="Снова падает")
    assert out["card"]["status"] == "To Do"
    assert [(c["agent"], c["kind"]) for c in out["commands"]] == [("dev", "assigned")]


def test_brief_writes_the_handoff_and_the_analysts_command_in_one_transaction(env, monkeypatch):
    card = env.card(status="Inbox", assignee="@dev")

    def broken(self, spec):
        raise store.Invalid("no room")

    monkeypatch.setattr(store.Store, "_enqueue_in_tx", broken)
    with pytest.raises(api.ApiError):
        _act(env, card.id, "brief")
    after = env.store.card(card.id)
    assert (after.assignee, after.revision, after.labels) == ("@dev", card.revision, ())
    monkeypatch.undo()
    out = _act(env, card.id, "brief")
    assert out["card"]["assignee"] == "@analyst" and out["card"]["labels"] == ["spec:task-brief"]
    assert [(c["agent"], c["kind"]) for c in out["commands"]] == [("analyst", "assigned")]


# --- the brief's version (R17, R18, AC18) --------------------------------------------------------------
def _by_id(env, card_id):
    return {a["id"]: a for a in _actions(env, card_id)}


def test_a_ready_brief_starts_with_its_version(env):
    card = env.briefed()
    start = _actions(env, card.id)[0]
    assert (start["id"], start["label"], start["brief_version"], start["enabled"]) == (
        "start", "action.start_brief", 1, True)
    begin = env.store.last_seq()
    out = _act(env, card.id, "start", brief_version=1)
    assert out["card"]["status"] == "To Do"
    [v1] = briefs.versions(env.store, card.id)
    assert (v1["approved_by"], v1["approved_at"]) == ("user:owner", env.now)
    assert [e["kind"] for e in env.store.events_since(begin)].count("brief.approved") == 1
    again = out["actions"][0]  # approved: a To Do start only brings the card to its agent again
    assert (again["label"], again["brief_version"]) == ("action.start_again", 1)


def test_a_stale_brief_offers_save_and_disables_start(env):
    card = env.briefed()
    env.store.edit_card(card.id, None, {"description": "Цель: отчёт и график."}, actor="user:owner")
    actions = _by_id(env, card.id)
    assert list(actions) == ["start", "brief", "save_brief"]
    start = actions["start"]
    assert (start["enabled"], start["reason"], start["brief_version"]) == (False, "brief_stale", None)
    assert start["label"] == "action.start_brief"  # the start would approve the new text: off until it is a version
    assert (actions["save_brief"]["enabled"], actions["save_brief"]["comment"]) == (True, "none")
    assert actions["save_brief"]["label"] == "action.save_brief"
    assert _refused(env, card.id, "start").reason == "brief_stale"
    assert env.store.card(card.id).status == "Inbox"


def test_save_brief_records_the_next_version_and_enables_start(env):
    card = env.briefed()
    env.store.edit_card(card.id, None, {"description": "Цель: отчёт и график."}, actor="user:owner")
    revision = env.store.card(card.id).revision
    out = _act(env, card.id, "save_brief")
    v2 = briefs.latest(env.store, card.id)
    assert (v2["version"], v2["origin"], v2["author"]) == (2, "person", "user:owner")
    assert v2["description"] == "Цель: отчёт и график."
    assert env.store.card(card.id).revision == revision == out["card"]["revision"]
    assert out["commands"] == []
    start = out["actions"][0]
    assert (start["enabled"], start["label"], start["brief_version"]) == (True, "action.start_brief", 2)
    assert "save_brief" not in [a["id"] for a in out["actions"]]
    assert _refused(env, card.id, "save_brief").reason == "not_offered"


def test_an_outdated_brief_version_is_a_conflict(env):
    card = env.briefed()
    seen = _actions(env, card.id)[0]["brief_version"]
    # meanwhile the analyst hands a new text with the label again: v2
    env.store.edit_card(card.id, None, {"description": "Цель: отчёт v2.", "labels_add": ["brief-ready"]},
                        actor="agent:analyst")
    assert (seen, briefs.latest(env.store, card.id)["version"]) == (1, 2)
    for sent in ({"brief_version": seen}, {}):  # a page that names no version has not seen v2 either
        error = _refused(env, card.id, "start", **sent)
        assert (error.kind, error.reason) == ("conflict", "brief_changed")
    assert _refused(env, card.id, "start", brief_version=True).kind == "invalid"
    assert env.store.card(card.id).status == "Inbox"
    assert [v["approved_by"] for v in briefs.versions(env.store, card.id)] == ["", ""]


def test_an_old_page_with_an_old_version_gets_brief_changed_not_a_bare_conflict(env):
    card = env.briefed()
    shown = env.store.card(card.id)  # the page showed v1 at this revision
    env.store.edit_card(card.id, None, {"description": "Цель: отчёт v2.", "labels_add": ["brief-ready"]},
                        actor="agent:analyst")
    with pytest.raises(api.ApiError) as exc:
        api.dispatch(env.ctx, person(), {"op": "user.card.act", "card": card.id, "action": "start",
                                         "revision": shown.revision, "brief_version": 1})
    assert (exc.value.kind, exc.value.reason) == ("conflict", "brief_changed")
    review = env.card(status="Review")
    env.store.edit_card(review.id, None, {"title": "Другое"}, actor="user:owner")
    with pytest.raises(api.ApiError) as exc:
        api.dispatch(env.ctx, person(), {"op": "user.card.act", "card": review.id, "action": "accept",
                                         "revision": review.revision})
    assert (exc.value.kind, exc.value.reason) == ("conflict", "")


def test_a_legacy_card_starts_with_confirmation(env):
    card = env.card(status="Inbox", assignee="@dev", labels=["spec:implementer", "brief-ready"], description="ТЗ")
    start = _actions(env, card.id)[0]
    assert (start["label"], start["brief_version"]) == ("action.start_legacy", 0)
    assert _act(env, card.id, "start", brief_version=0)["card"]["status"] == "To Do"
    [v1] = briefs.versions(env.store, card.id)
    assert (v1["origin"], v1["approved_by"], v1["description"]) == ("legacy", "user:owner", "ТЗ")


def test_rework_needs_a_comment_and_keeps_the_versions(env):
    card = env.briefed()
    brief = _by_id(env, card.id)["brief"]
    assert (brief["label"], brief["comment"], brief["agent"]) == ("action.brief_rework", "required", "@analyst")
    assert _refused(env, card.id, "brief").kind == "invalid"
    out = _act(env, card.id, "brief", comment="Добавь график")
    after = out["card"]
    assert (after["status"], after["assignee"], after["labels"]) == ("Inbox", "@analyst", ["spec:task-brief"])
    assert env.store.messages(card.id)[-1]["body"] == "[owner] ТЗ v1: на доработку — Добавь график"
    [v1] = briefs.versions(env.store, card.id)
    assert v1["description"] == "Цель: отчёт."
    assert [(c["agent"], c["kind"]) for c in out["commands"]] == [("analyst", "assigned")]
    legacy = env.card(status="Inbox", assignee="@dev", labels=["brief-ready"], description="ТЗ")
    _act(env, legacy.id, "brief", comment="Уточни сроки")
    assert env.store.messages(legacy.id)[-1]["body"] == "[owner] ТЗ: на доработку — Уточни сроки"


def test_a_failed_preparation_offers_retry_without_a_comment(env):
    card = env.card(status="Inbox", assignee="@analyst", labels=["spec:task-brief"])
    command = env.store.enqueue({"key": "t-fail", "agent": "analyst", "thread": card.thread, "card": card.id,
                                 "kind": "assigned", "text": f"[board] {card.id} assigned to you",
                                 "initiator": "user:owner"})
    env.store.command_state(command["id"], "failed", error="spawn_failed")
    card = env.store.card(card.id)
    brief = _by_id(env, card.id)["brief"]
    assert (brief["label"], brief["comment"], brief["agent"], brief["enabled"]) == (
        "action.brief_retry", "none", "@analyst", True)
    out = _act(env, card.id, "brief")
    assert out["card"]["revision"] == card.revision  # nothing to change: the analyst is only told again
    assert [c["agent"] for c in out["commands"]] == ["analyst"]
    assert briefs.state(env.store, env.store.card(card.id))["state"] == "queued"


def test_no_brief_action_while_the_analyst_prepares_or_asks(env):
    card = env.card(status="Inbox", assignee="@analyst", labels=["spec:task-brief"])
    _analyst_works(env, card)
    assert [a["id"] for a in _actions(env, card.id)] == ["start"]
    decisions.Decisions(env.store).open("question", card=card.id, payload={"text": "Как?"},
                                        requested_by="agent:analyst", now=env.now)
    assert briefs.state(env.store, card)["state"] == "waiting_user"
    assert [a["id"] for a in _actions(env, card.id)] == ["start"]
    assert _refused(env, card.id, "brief").reason == "not_offered"


def test_a_card_without_a_brief_starts_as_before(env):
    card = env.card(status="Inbox", assignee="@dev", description="Сделать X")
    start = _actions(env, card.id)[0]
    assert (start["label"], start["brief_version"]) == ("action.start", None)
    assert all(a["brief_version"] is None for a in _actions(env, card.id)[1:])
    assert _act(env, card.id, "start")["card"]["status"] == "To Do"
    assert briefs.versions(env.store, card.id) == []


def _brief_view(env, card_id, user="owner"):
    return api.dispatch(env.ctx, person(user), {"op": "card.brief", "card": card_id})


def test_brief_view_lists_versions_with_their_changes(env):
    card = env.briefed()
    env.store.edit_card(card.id, None, {"description": "Цель: отчёт и график.", "labels_add": ["brief-ready"]},
                        actor="agent:analyst")
    _act(env, card.id, "start", brief_version=2)
    view = _brief_view(env, card.id)
    assert (view["brief"]["state"], view["brief"]["version"], view["brief"]["approved"]) == ("ready", 2, True)
    v1, v2 = view["versions"]
    assert (v1["version"], v1["origin"], v1["author"], v1["approved_by"], v1["changes"]) == (
        1, "agent", "agent:analyst", "", [])
    assert (v2["version"], v2["approved_by"], v2["approved_at"], v2["created"]) == (2, "user:owner", env.now, env.now)
    assert (v2["description"], v2["acceptance"]) == ("Цель: отчёт и график.", ["Есть отчёт"])
    assert v2["changes"][:2] == [{"op": "-", "text": "Цель: отчёт."}, {"op": "+", "text": "Цель: отчёт и график."}]
    assert view["current"] == {"description": "Цель: отчёт и график.", "acceptance": ["Есть отчёт"]}
    assert view["changes"] == []


def test_brief_view_of_a_stale_brief_shows_the_changes_since_the_last_version(env):
    card = env.briefed()
    env.store.edit_card(card.id, None, {"description": "Цель: отчёт и график."}, actor="user:owner")
    view = _brief_view(env, card.id)
    assert view["brief"]["state"] == "stale"
    assert view["changes"] == [
        {"op": "-", "text": "Цель: отчёт."},
        {"op": "+", "text": "Цель: отчёт и график."},
        {"op": "=", "text": ""},
        {"op": "=", "text": "- Есть отчёт"},
    ]


def test_brief_view_of_a_card_of_another_project_is_not_found(env):
    users.Users(env.store).create("anna", "Анна", roles=[("partner", ["web"])], actor="user:owner")
    card = env.briefed()  # digipaddock
    with pytest.raises(api.ApiError) as exc:
        _brief_view(env, card.id, user="anna")
    assert exc.value.kind == "not_found"
    plain = env.card(status="Inbox", assignee="@dev")
    view = _brief_view(env, plain.id)
    assert (view["brief"]["state"], view["versions"], view["changes"]) == ("not_requested", [], [])
    with pytest.raises(api.ApiError) as exc:
        env.call("digi", {"op": "card.brief", "card": card.id})
    assert exc.value.kind == "refused"


# --- a person's edit -----------------------------------------------------------------------------
def _edit(env, card, changes, **extra):
    return api.dispatch(
        env.ctx,
        person(),
        {"op": "user.card.edit", "card": card.id, "revision": env.store.card(card.id).revision, "changes": changes,
         **extra},
    )


def test_a_status_edit_without_override_is_refused(env):
    card = env.card(status="Review")
    with pytest.raises(api.ApiError) as exc:
        _edit(env, card, {"status": "Done"})
    assert (exc.value.kind, exc.value.reason) == ("refused", "use_action")
    assert _edit(env, card, {"status": "Review", "title": "Same status"})["card"]["title"] == "Same status"


def test_an_override_needs_a_reason_and_is_recorded(env):
    card = env.card(status="Review")
    for bad in ("", " ", "x" * 501):
        with pytest.raises(api.ApiError) as exc:
            _edit(env, card, {"status": "Inbox"}, override_reason=bad)
        assert exc.value.kind == "invalid"
    out = _edit(env, card, {"status": "Inbox"}, override_reason="ошибочно в Review")
    assert out["card"]["status"] == "Inbox"
    assert env.store.messages(card.id)[-1]["body"] == "[owner] вручную: Review → Inbox: ошибочно в Review"
    changed = [e for e in env.store.events_since(0) if e["kind"] == "card.changed"][-1]
    assert changed["payload"]["override"] is True


def test_owner_cancels_with_a_reason_and_can_restore_to_inbox(env):
    card = env.card(status="Inbox", assignee="@analyst")
    with pytest.raises(api.ApiError):
        _edit(env, card, {"status": "Cancelled"})
    with pytest.raises(api.ApiError):
        _edit(env, card, {"status": "Cancelled"}, override_reason=" ")
    assert env.store.card(card.id).status == "Inbox"
    out = _edit(env, card, {"status": "Cancelled"}, override_reason="Решили не делать")
    assert (out["card"]["status"], out["card"]["outcome"], out["card"]["canonical"]) == (
        "Cancelled", "cancelled", ""
    )
    assert env.store.card(card.id).terminal
    assert env.store.messages(card.id)[-1]["body"] == "[owner] вручную: Inbox → Cancelled: Решили не делать"
    assert [action["id"] for action in out["actions"]] == ["restore"]
    restored = _act(env, card.id, "restore")["card"]
    assert (restored["status"], restored["outcome"], restored["canonical"]) == ("Inbox", "", "")


def _last_changed(env):
    return [e for e in env.store.events_since(0) if e["kind"] == "card.changed"][-1]["payload"]


def test_a_ready_reason_goes_without_text_or_with_an_explanation(env):
    """a ready reason of the target status needs no text; an explanation rides along up to 500 chars."""
    card = env.card(status="Review")
    _edit(env, card, {"status": "Done"}, override_reason_code="done_outside")
    assert env.store.messages(card.id)[-1]["body"] == "[owner] вручную: Review → Done: Сделано вне системы"
    payload = _last_changed(env)
    assert (payload["override"], payload["reason_code"]) == (True, "done_outside")
    other = env.card(status="Review")
    with pytest.raises(api.ApiError) as exc:
        _edit(env, other, {"status": "Done"}, override_reason_code="done_outside", override_reason="x" * 501)
    assert exc.value.kind == "invalid" and env.store.card(other.id).status == "Review"
    _edit(env, other, {"status": "Done"}, override_reason_code="done_outside", override_reason=" в чате ")
    assert env.store.messages(other.id)[-1]["body"] == "[owner] вручную: Review → Done: Сделано вне системы — в чате"


def test_other_needs_its_own_text(env):
    card = env.card(status="Review")
    for bad in (None, "", "   "):
        extra = {} if bad is None else {"override_reason": bad}
        with pytest.raises(api.ApiError) as exc:
            _edit(env, card, {"status": "Inbox"}, override_reason_code="other", **extra)
        assert exc.value.kind == "invalid"
    assert env.store.card(card.id).status == "Review"
    _edit(env, card, {"status": "Inbox"}, override_reason_code="other", override_reason="своя причина")
    assert env.store.messages(card.id)[-1]["body"] == "[owner] вручную: Review → Inbox: своя причина"
    assert _last_changed(env)["reason_code"] == "other"


def test_a_code_of_another_status_and_a_stray_canonical_are_invalid(env):
    card = env.card(status="Review")
    original = env.card(status="Inbox")
    for extra in (
        {"override_reason_code": "not_relevant"},  # Cancelled's, not Done's
        {"override_reason_code": "nonsense"},
        {"override_reason_code": "done_outside", "override_canonical": original.id},
    ):
        with pytest.raises(api.ApiError) as exc:
            _edit(env, card, {"status": "Done"}, **extra)
        assert exc.value.kind == "invalid", extra
    with pytest.raises(api.ApiError) as exc:
        _edit(env, card, {"status": "Cancelled"}, override_reason_code="not_relevant", override_canonical=original.id)
    assert exc.value.kind == "invalid"
    after = env.store.card(card.id)
    assert (after.status, after.outcome) == ("Review", "")


def test_a_page_from_before_the_codes_is_taken_as_other(env):
    card = env.card(status="Review")
    _edit(env, card, {"status": "Done"}, override_reason="сделано руками")
    assert _last_changed(env)["reason_code"] == "other"
    assert env.store.messages(card.id)[-1]["body"] == "[owner] вручную: Review → Done: сделано руками"


def test_a_duplicate_names_a_live_original_it_sees(env):
    """Cancelled as a duplicate of a live card the person sees; the original gets nothing."""
    original = env.card(status="In Progress")
    card = env.card(status="Inbox")
    before = len(env.store.messages(original.id))
    out = _edit(env, card, {"status": "Cancelled"}, override_reason_code="duplicate",
                override_canonical=" " + original.id.lower() + " ", override_reason="то же самое")
    assert (out["card"]["status"], out["card"]["outcome"], out["card"]["canonical"]) == (
        "Cancelled", "duplicate", original.id
    )
    assert env.store.messages(card.id)[-1]["body"] == (
        f"[owner] вручную: Inbox → Cancelled: Дубль задачи ({original.id}) — то же самое"
    )
    assert _last_changed(env)["reason_code"] == "duplicate"
    assert len(env.store.messages(original.id)) == before


def test_a_duplicate_without_a_number_is_plainly_cancelled(env):
    card = env.card(status="Inbox")
    out = _edit(env, card, {"status": "Cancelled"}, override_reason_code="duplicate", override_canonical="")
    assert (out["card"]["outcome"], out["card"]["canonical"]) == ("cancelled", "")
    assert env.store.messages(card.id)[-1]["body"] == "[owner] вручную: Inbox → Cancelled: Дубль задачи"


def test_a_duplicate_of_a_missing_hidden_own_or_cancelled_card_is_refused(env):
    u = users.Users(env.store)
    u.set_role("mover", ["cards.read", "cards.transition"], actor="user:owner")
    u.create("anna", "Анна", roles=[("mover", ["web"])], actor="user:owner")
    card = env.card(status="Inbox", project="web")
    hidden = env.card(status="Inbox")  # digipaddock: anna does not see it
    closed = env.card(status="Inbox", project="web")
    _edit(env, closed, {"status": "Cancelled"}, override_reason_code="not_relevant")
    for canonical, kind in (
        ("KD-99999", "not_found"), (hidden.id, "not_found"), (card.id, "invalid"), (closed.id, "refused"),
        ("nonsense", "invalid"),
    ):
        with pytest.raises(api.ApiError) as exc:
            api.dispatch(env.ctx, person("anna"), {
                "op": "user.card.edit", "card": card.id, "revision": env.store.card(card.id).revision,
                "changes": {"status": "Cancelled"}, "override_reason_code": "duplicate",
                "override_canonical": canonical,
            })
        assert exc.value.kind == kind, canonical
    after = env.store.card(card.id)
    assert (after.status, after.outcome, after.canonical) == ("Inbox", "", "")
    # cards.transition alone closes it: the outcome is the move's own, not an edit
    original = env.card(status="Inbox", project="web")
    out = api.dispatch(env.ctx, person("anna"), {
        "op": "user.card.edit", "card": card.id, "revision": env.store.card(card.id).revision,
        "changes": {"status": "Cancelled"}, "override_reason_code": "duplicate", "override_canonical": original.id,
    })["card"]
    assert (out["outcome"], out["canonical"]) == ("duplicate", original.id)


def test_every_reason_has_its_labels_and_the_page_keeps_the_same_list():
    cats = i18n.load()
    codes = {code for reasons in api.OVERRIDE_REASONS.values() for code in reasons}
    for lang in ("ru", "en"):
        assert all(f"flow.override.code.{code}" in cats[lang] for code in codes), lang
    assert set(api.OVERRIDE_REASONS) == set(schema.STATUSES)
    assert all(reasons[-1] == "other" for reasons in api.OVERRIDE_REASONS.values())
    page = (Path(api.__file__).resolve().parents[2] / "cabinet/web/src/lib/overrideReasons.ts").read_text("utf-8")
    listed = dict(re.findall(r'"([A-Za-z ]+)": \[([^\]]*)\]', page))
    assert {status: tuple(re.findall(r'"(\w+)"', codes_)) for status, codes_ in listed.items()} == api.OVERRIDE_REASONS


def test_manual_cancellation_refuses_a_viewer_and_a_stale_revision(env):
    card = env.card(status="Inbox")
    request = {
        "op": "user.card.edit", "card": card.id, "revision": card.revision,
        "changes": {"status": "Cancelled"}, "override_reason": "Решили не делать",
    }
    with pytest.raises(api.ApiError) as exc:
        api.dispatch(env.ctx, person("vera"), request)
    assert exc.value.kind == "refused"
    env.store.edit_card(card.id, card.revision, {"title": "Updated"}, actor="user:owner")
    with pytest.raises(api.ApiError) as exc:
        api.dispatch(env.ctx, person(), request)
    assert exc.value.kind == "conflict"
    assert env.store.card(card.id).status == "Inbox"
    assert env.store.card(card.id).outcome == ""


def test_an_assignee_change_drops_specs_the_new_assignee_lacks(env):
    card = env.card(status="Inbox", assignee="@analyst", labels=["ui", "spec:task-brief"])
    out = _edit(env, card, {"assignee": "@dev"})["card"]
    assert out["assignee"] == "@dev" and out["labels"] == ["ui"]
    kept = env.card(status="Inbox", assignee="@dev", labels=["spec:reviewer"])
    assert _edit(env, kept, {"title": "Renamed"})["card"]["labels"] == ["spec:reviewer"]


@pytest.mark.parametrize("person", ["", "@owner"])
def test_a_card_handed_to_no_agent_keeps_its_specs(env, person):
    """Final review: spec 3.2 is about an agent that cannot hold the label — a card taken off the agents (no
    assignee, the owner) keeps the specialization it will be started with."""
    card = env.card(status="Inbox", assignee="@dev", labels=["ui", "spec:reviewer"])
    out = _edit(env, card, {"assignee": person})["card"]
    assert out["assignee"] == person and out["labels"] == ["ui", "spec:reviewer"]


def test_an_edit_that_changes_nothing_still_returns_the_actions(env):
    """Final review: the page sends the card back as it is (the same status) — it replaces the card with the
    answer, so the answer carries the big buttons too."""
    card = env.card(status="Review")
    out = _edit(env, card, {"status": "Review"})
    assert out["card"]["revision"] == card.revision
    assert [a["id"] for a in out["actions"]] == [a["id"] for a in _actions(env, card.id)] != []


def test_claim_refuses_open_dependencies(env):
    dep = env.card(status="In Progress", assignee="@sysadmin", project="admin")
    card = env.card(status="To Do", dependencies=[dep.id])
    with pytest.raises(api.ApiError) as exc:
        env.call("digi", {"op": "worker.claim", "agent": "dev", "card": card.id, "revision": card.revision})
    assert (exc.value.kind, exc.value.reason) == ("refused", "deps_open")
    env.store.edit_card(dep.id, None, {"status": "Done"}, actor="user:owner")
    claimed = env.call("digi", {"op": "worker.claim", "agent": "dev", "card": card.id, "revision": card.revision})
    assert claimed["card"]["status"] == "In Progress"


def test_every_reason_and_label_is_in_the_catalogs():
    from kd.cabinet import i18n

    for code in flow.REASONS:
        assert i18n.t("ru", f"flow.reason.{code}") != f"flow.reason.{code}", code
    for label in flow.LABELS:
        assert i18n.t("en", label) != label, label


def test_a_start_shows_the_operations_its_brief_grants(env):
    """The start that approves a brief carries the "Operations" of exactly that version — the page shows
    them before the approval; a brief without them carries none."""
    step = {"verb": "agent.reinstall", "target": "factory", "agents": ["dev"], "what": "Переустановить dev",
            "effect": "Новые инструкции dev", "risk": {"level": "low", "why": "Очередь сохраняется"},
            "rollback": "Предыдущая выкатка"}
    plain = env.briefed()
    assert _by_id(env, plain.id)["start"]["operations"] == []
    card = env.briefed()
    env.store.edit_card(card.id, None, {"operations": [step], "labels_add": ["brief-ready"]}, actor="agent:analyst")
    start = _by_id(env, card.id)["start"]
    assert start["brief_version"] == 2 and start["operations"] == [step]


def test_a_brief_takes_the_card_from_a_skilled_dev_without_a_dead_end(env):
    """The "Write the brief" action on a dev card with skill:frontend — the analyst holds no skills, the label goes."""
    from pathlib import Path

    env.ctx.repo_root = Path(__file__).parents[2]
    card = env.card(status="To Do", assignee="@dev", labels=["spec:implementer", "skill:frontend"])
    _act(env, card.id, "brief")
    after = env.store.card(card.id)
    assert after.assignee == "@analyst" and after.labels == ("spec:task-brief",)
