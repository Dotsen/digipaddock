"""An agent closes its card only on a project that names it (the project's ``done_by``),
and only with nothing open — children, dependencies, decisions, its PR — in either mode of the pipeline; the
owner's acceptance stays as before."""

import pytest
from cabinet_env import CabinetEnv

from kd import pipeline_config, session_config
from kd.cabinet import api, problems
from test_cabinet_outcomes import DEV, begin, card, end, finish, outcome_of, refused


@pytest.fixture
def cab(tmp_path):
    env = CabinetEnv(tmp_path)
    env.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    env.ctx.host_config = lambda: session_config.HostConfig({"boss": 1, "dev": 2, "analyst": 1})
    env.ctx.pipeline = lambda: pipeline_config.Config(mode="enforce")
    env.store.update_project("digipaddock", actor="root", done_by="dev")
    return env


def edit_done(cab, record):
    return cab.call(DEV, {"op": "worker.edit", "agent": "dev", "card": record.id, "status": "Done"})


def edit_refused(cab, record) -> str:
    with pytest.raises(api.ApiError) as exc:
        edit_done(cab, record)
    assert exc.value.kind == "refused", exc.value
    return str(exc.value)


def child(cab, record, status="In Progress"):
    part = cab.store.create_card({"title": "Part", "project": "digipaddock", "status": status, "assignee": "@dev",
                                  "created_by": "@owner"}, actor="user:owner")
    return cab.store.edit_card(part.id, None, {"parent": record.id}, actor="user:owner")


def pr(cab, record, state):
    cab.store.pr_put({"repo": "owner/repo", "project": "digipaddock", "number": 12, "card": record.id,
                      "title": f"{record.id}: a change", "url": "", "head": "a" * 40, "state": state, "files": [],
                      "cage": [], "diff": "", "truncated": False, "release": False}, actor="forge")


def test_done_with_open_children_is_refused(cab):
    record = card(cab)
    part = child(cab, record)
    begin(cab, record)
    why = refused(cab, record, outcome="done", note="all merged")
    assert f"{record.id} has open conditions" in why and f"children {part.id}" in why
    assert f"children {part.id}" in edit_refused(cab, record)
    assert cab.store.card(record.id).status == "In Progress"


def test_done_with_open_dependencies_and_decisions_is_refused(cab):
    dep = card(cab, status="To Do")
    record = card(cab, dependencies=[dep.id])
    from test_cabinet_outcomes import question

    asked = question(cab, record)
    why = edit_refused(cab, record)
    assert f"dependencies {dep.id}" in why and f"decisions question {asked[:8]}" in why


def test_done_with_an_open_pr_is_refused(cab):
    record = card(cab)
    pr(cab, record, "open")
    assert "PR #12" in edit_refused(cab, record)
    pr(cab, record, "merged")
    command, session = begin(cab, record)
    finish(cab, record, outcome="done", note="merged and released")
    end(cab, command, session)
    assert cab.store.card(record.id).status == "Done" and outcome_of(cab, command)["state"] == "final"


def test_done_outside_the_policy_is_refused(cab):
    record = cab.store.create_card({"title": "Ops", "project": "admin", "status": "In Progress", "assignee": "@dev",
                                    "created_by": "@owner"}, actor="user:owner")
    why = edit_refused(cab, record)
    assert why == "Done on admin is the owner's acceptance: finish with --outcome review"
    begin(cab, record)
    assert "finish with --outcome review" in refused(cab, record, outcome="done", note="done")


def test_the_project_names_one_closer(cab):
    """Another agent on digipaddock, and dev once digipaddock names sysadmin, close nothing themselves."""
    record = card(cab)
    cab.store.update_project("digipaddock", actor="root", done_by="sysadmin")
    assert edit_refused(cab, record) == "Done on digipaddock is the owner's acceptance: finish with --outcome review"


def test_a_condition_opened_during_the_turn_refuses_at_its_end(cab):
    record = card(cab)
    command, session = begin(cab, record)
    finish(cab, record, outcome="done", note="all done")
    part = child(cab, record)
    end(cab, command, session)
    assert cab.store.card(record.id).status == "In Progress"
    [problem] = problems.open_for(cab.store, record.id)
    assert problem["kind"] == "outcome_refused" and f"children {part.id}" in problem["detail"]["reason"]


def test_observe_holds_an_agents_done_to_the_policy_too(cab):
    """The kill switch (``pipeline_setup.sh disable``) stops continuations, not the closing policy: in observe an
    agent still closes only a card of a project that names it, with nothing open on it (KD-9479)."""
    cab.ctx.pipeline = lambda: pipeline_config.Config(mode="observe")
    record = card(cab)
    part = child(cab, record)
    assert f"children {part.id}" in edit_refused(cab, record)
    begin(cab, record)
    assert f"children {part.id}" in refused(cab, record, outcome="done", note="all done")
    cab.store.update_project("digipaddock", actor="root", done_by="sysadmin")
    other = card(cab)
    assert edit_refused(cab, other) == "Done on digipaddock is the owner's acceptance: finish with --outcome review"
    assert {cab.store.card(c.id).status for c in (record, other)} == {"In Progress"}


def test_observe_refuses_at_the_turns_end_a_done_the_policy_does_not_allow(cab):
    """A Done recorded while the policy allowed it is checked again when the turn ends, in observe too."""
    cab.ctx.pipeline = lambda: pipeline_config.Config(mode="observe")
    record = card(cab)
    command, session = begin(cab, record)
    finish(cab, record, outcome="done", note="all done")
    cab.store.update_project("digipaddock", actor="root", done_by="")
    end(cab, command, session)
    assert cab.store.card(record.id).status == "In Progress"
    row = outcome_of(cab, command)
    assert row["state"] == "final" and "the owner's acceptance" in row["reason"]


def test_owner_acceptance_is_unchanged(cab):
    record = card(cab, status="Review")
    child(cab, record)
    api.dispatch(cab.ctx, api.Principal("user", -1, "owner"),
                 {"op": "user.card.act", "card": record.id, "action": "accept",
                  "revision": cab.store.card(record.id).revision})
    assert cab.store.card(record.id).status == "Done"


def test_old_done_cards_are_left_alone(cab):
    record = card(cab, status="Done")
    child(cab, record)
    assert cab.call("digi-cabinet", {"op": "pipeline.sweep"})["problems"] == 0
    assert cab.store.card(record.id).status == "Done"
