import pytest

from cabinet_env import CabinetEnv
from kd.cabinet.client import CabinetError


def test_reports_are_version_bound_and_do_not_modify_target(tmp_path):
    env = CabinetEnv(tmp_path)
    target = env.card(
        description="Goal; missing acceptance and contradictory dependency"
    )
    review = env.card(
        assignee="@analyst", status="In Progress", labels=["spec:brief-review"]
    )
    client = env.client("digi")

    def call(action, **kw):
        return client.call(
            dict(
                op="brief-review", agent="analyst", card=review.id, action=action, **kw
            )
        )

    with pytest.raises(CabinetError, match="separate"):
        call("prepare", target=review.id, commit="a" * 40)
    packet = call("prepare", target=target.id, commit="a" * 40)["packet"]
    assert packet["scope"]["description"] == target.description
    finding = dict(
        severity="major",
        location="AC",
        evidence="No acceptance criteria",
        scenario="Implementation cannot be objectively accepted",
        recommendation="Add an observable criterion",
    )
    report = dict(
        verdict="needs_changes",
        basis=packet["basis"],
        findings=[finding],
        missing_inputs=[],
    )
    assert call("record", report=report)["rounds"][0]["current"]
    assert env.store.card(target.id).revision == target.revision
    env.put(target.id, notes="unrelated note")
    assert call("status")["rounds"][0]["current"]
    env.put(target.id, description="Corrected goal with feasible scope")
    assert not call("status")["rounds"][0]["current"]
    with pytest.raises(CabinetError, match="changed"):
        call("record", report=report)
    fixed = call("prepare", target=target.id, commit="b" * 40)["packet"]
    ready = dict(verdict="ready", basis=fixed["basis"], findings=[], missing_inputs=[])
    rounds = call("record", report=ready)["rounds"]
    assert [r["current"] for r in rounds] == [False, True]
    with pytest.raises(CabinetError, match="inconsistent"):
        call("record", report=dict(ready, missing_inputs=["owner goal"]))
    assert (
        call(
            "record",
            report=dict(ready, verdict="blocked", missing_inputs=["owner goal"]),
        )["rounds"][-1]["report"]["verdict"]
        == "blocked"
    )


def test_author_and_unclaimed_review_cannot_record_a_verdict(tmp_path):
    env = CabinetEnv(tmp_path)
    target = env.card()
    with pytest.raises(CabinetError, match="claimed analyst"):
        env.client("digi").call(
            dict(op="brief-review", agent="dev", card=target.id, action="status")
        )


def test_dependency_changes_and_new_commit_invalidate_prior_review(tmp_path):
    env = CabinetEnv(tmp_path)
    dependency = env.card()
    target = env.card()
    review = env.card(
        assignee="@analyst", status="In Progress", labels=["spec:brief-review"]
    )
    client = env.client("digi")

    def call(action, **fields):
        return client.call(
            dict(
                op="brief-review",
                agent="analyst",
                card=review.id,
                action=action,
                **fields,
            )
        )

    packet = call("prepare", target=target.id, commit="a" * 40)["packet"]
    report = dict(
        verdict="ready", basis=packet["basis"], findings=[], missing_inputs=[]
    )
    assert call("record", report=report)["rounds"][0]["current"]
    env.put(target.id, dependencies=[dependency.id])
    assert not call("status")["rounds"][0]["current"]
    with pytest.raises(CabinetError, match="scope changed"):
        call("record", report=report)

    packet = call("prepare", target=target.id, commit="a" * 40)["packet"]
    assert packet["scope"]["dependencies"] == [dependency.id]
    report = dict(report, basis=packet["basis"])
    assert call("record", report=report)["rounds"][-1]["current"]
    call("prepare", target=target.id, commit="b" * 40)
    assert not any(row["current"] for row in call("status")["rounds"])
    with pytest.raises(CabinetError, match="basis differs"):
        call("record", report=report)


@pytest.mark.parametrize(
    "changes",
    [
        {"assignee": "@analyst"},
        {"labels": ["spec:tester"]},
        {"labels": ["skill:frontend"]},
    ],
)
def test_changed_executor_or_instruction_selection_invalidates_review(
    tmp_path, changes
):
    env = CabinetEnv(tmp_path)
    target = env.card(assignee="@dev")
    review = env.card(
        assignee="@analyst", status="In Progress", labels=["spec:brief-review"]
    )
    client = env.client("digi")

    def call(action, **fields):
        return client.call(
            dict(
                op="brief-review",
                agent="analyst",
                card=review.id,
                action=action,
                **fields,
            )
        )

    packet = call("prepare", target=target.id, commit="a" * 40)["packet"]
    report = dict(
        verdict="ready", basis=packet["basis"], findings=[], missing_inputs=[]
    )
    call("record", report=report)
    env.put(target.id, labels=["ordinary-triage-label"])
    assert call("status")["rounds"][0]["current"]
    env.put(target.id, **changes)
    assert not call("status")["rounds"][0]["current"]
    with pytest.raises(CabinetError, match="scope changed"):
        call("record", report=report)
    fresh = call("prepare", target=target.id, commit="a" * 40)["packet"]
    assert fresh["scope"]["selection"]["assignee"] == env.store.card(target.id).assignee


def _review(env):
    review = env.card(assignee="@analyst", status="In Progress", labels=["spec:brief-review"])
    client = env.client("digi")

    def call(action, **kw):
        return client.call(dict(op="brief-review", agent="analyst", card=review.id, action=action, **kw))

    return call


def _finding(severity):
    return dict(severity=severity, location="AC 2", evidence="wording", scenario="reader pauses",
                recommendation="rephrase")


def test_ready_may_carry_minor_findings_only(tmp_path):
    """Review M1: style remarks do not turn a sound brief into needs_changes."""
    env = CabinetEnv(tmp_path)
    call = _review(env)
    basis = call("prepare", target=env.card(description="Goal").id, commit="a" * 40)["packet"]["basis"]
    ready = dict(verdict="ready", basis=basis, findings=[_finding("minor")], missing_inputs=[])
    assert call("record", report=ready)["rounds"][-1]["report"]["verdict"] == "ready"
    with pytest.raises(CabinetError, match="inconsistent"):
        call("record", report=dict(ready, findings=[_finding("major")]))
    with pytest.raises(CabinetError, match="inconsistent"):
        call("record", report=dict(ready, verdict="needs_changes", findings=[_finding("minor")]))


def test_a_review_of_the_analysts_own_brief_says_it_is_not_independent(tmp_path):
    """Review I4: one analyst writes and reviews — the packet names the brief's author and the round says so."""
    env = CabinetEnv(tmp_path)
    own = env.briefed()
    call = _review(env)
    packet = call("prepare", target=own.id, commit="a" * 40)["packet"]
    assert packet["author"] == "agent:analyst" and packet["same_role"] is True
    report = dict(verdict="ready", basis=packet["basis"], findings=[], missing_inputs=[])
    assert call("record", report=report)["rounds"][-1]["same_role"] is True
    other = _review(env)
    person = other("prepare", target=env.card(description="Goal").id, commit="a" * 40)["packet"]
    assert person["author"] is None and person["same_role"] is False
