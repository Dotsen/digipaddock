"""A chain plan: stages of one role in one repository run one after another without the owner's acceptance of
each; the plan has one pull request and one acceptance."""

import pytest
import council_env as ce
from chain_env import (
    CRITICS,
    OPS,
    REPO,
    WEB,
    A,
    B,
    C,
    act,
    bind,
    cabinet,
    children,
    close,
    owner,
    passing,
    pipeline,
    planned,
    publish,
    reviewed,
    started,
)

from kd import board
from kd.cabinet import api, councils, flow, outcomes, problems, prs_api, stage_gate, stages


@pytest.fixture
def cab(tmp_path):
    return cabinet(tmp_path)


def test_stages_of_one_role_and_project_make_a_chain_run_one_at_a_time(cab):
    parent, kids = started(cab, (A, B, C))
    assert stages.is_chain(cab.store, parent.id)
    assert kids["a"].status == "To Do"
    assert (kids["b"].status, kids["b"].dependencies) == (
        "Blocked",
        (kids["a"].id,),
    )  # the previous stage
    assert (kids["c"].status, set(kids["c"].dependencies)) == (
        "Blocked",
        {kids["a"].id, kids["b"].id},
    )


def test_a_plan_with_another_role_is_no_chain(cab):
    parent, kids = started(cab, (A, OPS))
    assert not stages.is_chain(cab.store, parent.id)
    assert kids["ops"].status == "To Do" and kids["ops"].dependencies == ()  # as before


def test_a_plan_led_by_another_agent_than_its_stages_is_no_chain(cab):
    card = planned(cab, (A, B))
    cab.store.edit_card(
        card.id,
        None,
        {"assignee": "@sysadmin", "labels_remove": ["spec:implementer"]},
        actor="owner",
    )
    act(cab, card.id, "start", brief_version=1)
    assert not stages.is_chain(
        cab.store, card.id
    )  # the plan's pull request is led by the plan's own agent
    assert children(cab, card.id)["b"].status == "To Do"


def start_of(cab, card):
    found = flow.actions(cab.ctx, owner(), cab.store.card(card.id))
    return next(a for a in found if a["id"] == "start")


def test_a_plan_the_analyst_still_holds_says_chain_for_the_agent_its_start_hands_it_to(cab):
    card = planned(cab, (A, B))
    cab.store.edit_card(card.id, None, {"assignee": "@analyst"}, actor="owner")
    start = start_of(cab, card)
    assert (start["agent"], start["chain"]) == ("@dev", True)  # the route takes it from the analyst
    act(cab, card.id, "start", brief_version=1)
    assert stages.is_chain(cab.store, card.id)  # the hint and the decision agree
    assert children(cab, card.id)["b"].status == "Blocked"


def test_a_plan_on_another_agent_than_its_stages_says_no_chain(cab):
    card = planned(cab, (A, B))
    cab.store.edit_card(
        card.id,
        None,
        {"assignee": "@sysadmin", "labels_remove": ["spec:implementer"]},
        actor="owner",
    )
    assert "chain" not in start_of(cab, card)
    act(cab, card.id, "start", brief_version=1)
    assert not stages.is_chain(cab.store, card.id)


@pytest.mark.parametrize("off", ["no_repo", "observe", "switch"])
def test_no_chain_without_a_repository_enforce_or_with_the_switch_off(tmp_path, off):
    cab = cabinet(
        tmp_path,
        repo=off != "no_repo",
        mode="observe" if off == "observe" else "enforce",
        chain=off != "switch",
    )
    parent, kids = started(cab, (A, B))
    assert not stages.is_chain(cab.store, parent.id)
    assert kids["b"].status == "To Do" and kids["b"].dependencies == ()


@pytest.mark.parametrize("registry", ["{}", "not json"])
def test_a_project_the_registry_does_not_bind_runs_no_chain(cab, registry):
    cab.store.set_project_repo(
        "digipaddock", actor="root", repo=REPO, state="connected"
    )  # the row still says so
    bind(cab, registry)  # taken out of the registry by hand, or the file is unreadable
    parent, kids = started(cab, (A, B))
    assert not stages.is_chain(cab.store, parent.id)
    assert kids["b"].status == "To Do"


def test_a_chain_plan_stays_one_when_the_switch_goes_off_later(cab):
    parent, kids = started(cab, (A, B))
    pipeline(cab, chain=False)
    cab.store.edit_card(
        parent.id,
        None,
        {"stages": [A, B, C], "labels_add": ["brief-ready"]},
        actor="agent:analyst",
    )
    act(cab, parent.id, "approve_stages", brief_version=2)
    after = children(cab, parent.id)
    assert stages.is_chain(cab.store, parent.id)
    assert set(after["c"].dependencies) == {kids["a"].id, kids["b"].id}


def test_a_chain_plan_refuses_a_new_version_with_another_role(cab):
    parent, _ = started(cab, (A, B))
    cab.store.edit_card(
        parent.id,
        None,
        {"stages": [A, B, OPS], "labels_add": ["brief-ready"]},
        actor="agent:analyst",
    )
    with pytest.raises(api.ApiError, match="one role and one project"):
        act(cab, parent.id, "approve_stages", brief_version=2)
    assert set(children(cab, parent.id)) == {"a", "b"}


def test_a_plain_plan_does_not_become_a_chain_on_a_new_version(tmp_path):
    cab = cabinet(tmp_path, chain=False)
    parent, _ = started(cab, (A, B))
    pipeline(cab, chain=True)
    cab.store.edit_card(
        parent.id,
        None,
        {"stages": [A, B, C], "labels_add": ["brief-ready"]},
        actor="agent:analyst",
    )
    act(cab, parent.id, "approve_stages", brief_version=2)
    assert not stages.is_chain(cab.store, parent.id)
    assert children(cab, parent.id)["c"].dependencies == (
        children(cab, parent.id)["a"].id,
    )


def test_parent_of_names_the_plan_of_a_stage(cab):
    parent, kids = started(cab, (A, B))
    assert stages.parent_of(cab.store, kids["b"].id) == parent.id
    assert stages.parent_of(cab.store, parent.id) == ""


# --- the plan's pull request while stages are open -----------------------------------------------------------
HEAD = "a" * 40


def critics(cab, parent):
    cab.store.edit_card(parent.id, None, {"council": CRITICS}, actor="user:owner")


def test_a_publish_of_a_chain_plan_with_open_stages_opens_nothing(cab):
    parent, _ = started(cab, (A, B))
    critics(cab, parent)
    out = publish(cab, parent.id, HEAD)
    assert (out["decision"], out["council"], out["stage"]) == (None, None, True)
    assert cab.store.prs(card=parent.id, state="open")[-1]["head"] == HEAD
    assert councils.of_card(cab.store, parent.id, "code") == []
    assert ce.pr_decisions(cab, parent.id) == []


def test_the_same_head_after_the_last_stage_gathers_the_council(cab):
    parent, kids = started(cab, (A, B))
    critics(cab, parent)
    publish(cab, parent.id, HEAD)
    close(cab, kids["a"])
    close(cab, kids["b"])
    out = publish(cab, parent.id, HEAD)
    assert out["council"]["state"] in councils.OPEN and "stage" not in out
    assert out["decision"] is None  # held back by the council, as for any pull request


def test_without_critics_the_final_publish_opens_the_owners_decision(cab):
    parent, kids = started(cab, (A, B))
    publish(cab, parent.id, HEAD)
    close(cab, kids["a"])
    close(cab, kids["b"])
    out = publish(cab, parent.id, HEAD)
    assert (
        out["decision"]["state"] == "open"
        and out["decision"]["payload"]["head"] == HEAD
    )


def test_each_pull_request_of_a_finished_plan_gets_its_own_decision(cab):
    parent, kids = started(cab, (A, B))
    publish(cab, parent.id, HEAD)
    publish(cab, parent.id, "a" * 40, **WEB)
    assert ce.pr_decisions(cab, parent.id) == []
    close(cab, kids["a"])
    close(cab, kids["b"])
    publish(cab, parent.id, HEAD)
    publish(cab, parent.id, "a" * 40, **WEB)
    assert {
        (d["payload"]["repo"], d["payload"]["head"])
        for d in ce.pr_decisions(cab, parent.id)
    } == {(REPO, HEAD), (WEB["repo"], "a" * 40)}


def finished(cab, release=True):
    """A plan whose stages are closed, with its pull request published in two repositories: ``(parent,
    {project: the owner's open decision})``."""
    parent, kids = started(cab, (A, B))
    close(cab, kids["a"])
    close(cab, kids["b"])
    publish(cab, parent.id, HEAD, release=release)
    publish(cab, parent.id, "a" * 40, release=release, **WEB)
    return parent, {d["payload"]["project"]: d for d in ce.pr_decisions(cab, parent.id)}


def approved_wake(cab, parent, number):
    """The agent's wake on the owner's signed answer on pull request ``number``."""
    [text] = [
        c["text"]
        for c in cab.store.commands(agent="dev", card=parent.id)
        if f"the owner approved PR #{number} " in c["text"]
    ]
    return text


def approve(cab, decision, mode):
    card = cab.store.card(decision["card"])
    binding = {"expires": "2026-10-14T10:05:00Z", "nonce": "00" * 16, "mode": mode}
    return cab.call(
        "digi-cabinet",
        {
            "op": "decision.approve",
            "ids": [decision["id"]],
            "user": "owner",
            "revisions": {card.id: card.revision},
            "signature": {"id": "key", "binding": binding},
            "credential": None,
            "review": {"id": 77, "head": decision["payload"]["head"]},
        },
    )


def test_a_release_signed_on_one_pull_request_of_a_plan_waits_for_its_last_merge(cab):
    """KD-9408: the tick is on each pull request; a release after the first merge would roll out half the plan."""
    parent, found = finished(cab)
    assert prs_api.plan_pulls(cab.store, parent.id) == 2
    approve(cab, found["digipaddock"], "release")
    event = [e for e in cab.store.events_since(0) if e["kind"] == "decision.approved"][-1]
    assert event["payload"]["mode"] == "release" and event["payload"]["plan"] is True
    assert approved_wake(cab, parent, 7) == board.PR_WAKE["release_plan"].format(
        id=parent.id, pr=7, sha7=HEAD[:7]
    )
    approve(cab, found["web"], "merge")  # a merge signature is a merge, in a plan too
    assert approved_wake(cab, parent, 3) == board.PR_WAKE["merge"].format(
        id=parent.id, pr=3, sha7="a" * 7
    )


def test_the_plans_only_open_pull_request_is_released_as_any(cab):
    parent, found = finished(cab)
    publish(cab, parent.id, "a" * 40, release=True, state="merged", **WEB)
    assert prs_api.plan_pulls(cab.store, parent.id) == 1
    approve(cab, found["digipaddock"], "release")
    assert approved_wake(cab, parent, 7) == board.PR_WAKE["release"].format(
        id=parent.id, pr=7, sha7=HEAD[:7]
    )


def test_a_card_that_is_no_chain_plan_has_no_pull_requests_of_a_plan(tmp_path):
    cab = cabinet(tmp_path, chain=False)
    parent, _ = started(cab, (A, B))
    publish(cab, parent.id, HEAD)
    publish(cab, parent.id, "a" * 40, **WEB)
    assert prs_api.plan_pulls(cab.store, parent.id) == 0


def test_a_plain_plans_parent_publishes_as_any_card(tmp_path):
    cab = cabinet(tmp_path, chain=False)
    parent, _ = started(cab, (A, B))
    out = publish(cab, parent.id, HEAD)
    assert out["decision"]["state"] == "open" and "stage" not in out


# --- the end of the plan ----------------------------------------------------------------------------------------
H1, H2, H3 = "1" * 40, "2" * 40, "3" * 40


def pass_stage(cab, stage, head):
    _, seq = reviewed(cab, stage, head)
    return passing(cab, stage, seq)


def wakes(cab, card, agent="dev"):
    return [
        c["text"]
        for c in reversed(
            cab.store.commands(agent=agent, card=card.id, states=["queued"])
        )
    ]


def test_the_last_passed_stage_wakes_the_parents_agent_instead_of_review(cab):
    parent, kids = started(cab, (A, B))
    pass_stage(cab, kids["a"], H1)
    assert wakes(cab, parent) == []
    pass_stage(cab, kids["b"], H2)
    assert cab.store.card(parent.id).status == "In Progress"
    assert wakes(cab, parent) == [stages.FINAL.format(id=parent.id) + ": digipaddock#7"]
    note = cab.store.messages(parent.id)[-1]
    assert (
        note["author"] == "system"
        and kids["b"].id in note["body"]
        and "PR" in note["body"]
    )


def test_a_plain_plan_still_goes_to_review_by_itself(tmp_path):
    cab = cabinet(tmp_path, chain=False)
    parent, kids = started(cab, (A, B))
    close(cab, kids["a"])
    close(cab, kids["b"])
    assert cab.store.card(parent.id).status == "Review" and wakes(cab, parent) == []


def test_a_chain_plan_nobody_can_lead_to_its_end_goes_to_review_by_itself(cab):
    """Out of enforce nothing wakes the plan's agent: the owner sees the plan as he sees a plain one."""
    parent, kids = started(cab, (A, B))
    close(cab, kids["a"])
    pipeline(cab, mode="observe")
    close(cab, kids["b"])
    assert cab.store.card(parent.id).status == "Review" and wakes(cab, parent) == []


def test_the_parents_agent_hands_the_plan_in_after_its_merge(cab):
    parent, kids = started(cab, (A, B))
    pass_stage(cab, kids["a"], H1)
    pass_stage(cab, kids["b"], H2)
    publish(cab, parent.id, H2, state="merged")
    cab.store.edit_card(parent.id, None, {"status": "Review"}, actor="agent:dev")
    assert (
        stage_gate.latest(cab.store, parent.id) is None
    )  # the plan itself is no stage: the owner accepts it
    act(cab, parent.id, "accept")
    assert cab.store.card(parent.id).status == "Done"


def test_a_reopened_closed_stage_asks_boss_once_and_is_settled_by_its_done(cab):
    parent, kids = started(cab, (A, B))
    a = kids["a"]
    pass_stage(cab, a, H1)
    act(cab, a.id, "reopen")
    [problem] = problems.open_for(cab.store, parent.id)
    assert problem["kind"] == "stage_reopened" and problem["detail"]["child"] == a.id
    boss = [
        c["text"]
        for c in cab.store.commands(agent="boss", states=["queued"])
        if "reopened" in c["text"]
    ]
    assert boss == [
        problems.PROBLEM_WAKE["stage_reopened"].format(id=parent.id, agent="dev")
    ]
    cab.store.edit_card(a.id, None, {"status": "In Progress"}, actor="agent:dev")
    cab.store.edit_card(
        a.id, None, {"status": "To Do"}, actor="user:owner"
    )  # not a second reopening
    assert len(problems.open_for(cab.store, parent.id)) == 1
    pass_stage(cab, a, H2)
    assert problems.open_for(cab.store, parent.id) == []


def test_the_last_stage_wakes_the_agent_to_publish_every_pull_request(cab):
    parent, kids = started(cab, (A, B))
    pass_stage(cab, kids["a"], H1)
    pass_stage(cab, kids["b"], H2)
    (wake,) = wakes(cab, parent)
    assert "publish every pull request of the plan" in wake
    assert "merge them in the order the brief names" in wake
    assert "в каждом репозитории" in cab.store.messages(parent.id)[-1]["body"]


def test_the_final_wake_names_the_open_pull_requests_of_the_plan(cab):
    parent, kids = started(cab, (A, B))
    publish(cab, parent.id, H1, **WEB)
    publish(cab, parent.id, H1, number=12)
    publish(cab, parent.id, H1, number=4)
    publish(cab, parent.id, H1, repo="example/old", project="old", number=9)
    cab.store._conn.execute("UPDATE prs SET project='' WHERE repo='example/old'")  # a row of before projects
    publish(cab, parent.id, H1, number=30, state="closed")
    close(cab, kids["a"])
    close(cab, kids["b"])
    (wake,) = wakes(cab, parent)
    assert wake.startswith(stages.FINAL.format(id=parent.id))
    assert wake.endswith(": digipaddock#4, digipaddock#12, example/old#9, web#3")


def test_the_final_wake_without_open_pull_requests_keeps_its_text(cab):
    parent, kids = started(cab, (A, B))
    publish(cab, parent.id, H1, state="merged")
    close(cab, kids["a"])
    close(cab, kids["b"])
    assert wakes(cab, parent) == [stages.FINAL.format(id=parent.id)]


def test_a_stage_reopened_after_the_plans_last_one_wakes_the_parents_agent_again(cab):
    parent, kids = started(cab, (A, B))
    pass_stage(cab, kids["a"], H1)
    pass_stage(cab, kids["b"], H2)
    cab.store._conn.execute("UPDATE commands SET state='done' WHERE agent='dev'")
    act(cab, kids["b"].id, "reopen")
    pass_stage(cab, kids["b"], H3)
    assert wakes(cab, parent) == [stages.final_text(cab.store, parent.id)]


def test_a_reopened_stage_of_a_plain_plan_is_nobodys_problem(tmp_path):
    cab = cabinet(tmp_path, chain=False)
    parent, kids = started(cab, (A, C))
    close(cab, kids["a"])
    act(cab, kids["a"].id, "reopen")
    assert problems.open_for(cab.store, parent.id) == []


def test_a_turn_on_the_plan_does_not_settle_a_reopened_stage():
    assert "stage_reopened" in outcomes.HELD
