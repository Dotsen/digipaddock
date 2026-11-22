"""The gate between the stages of a chain plan: a handed-in stage gets one reviewer session on the head of the
plan's pull request, and its verdict is what the coordinator is woken with."""

import council_env as ce
import pytest
from chain_env import (
    HOSTED,
    OPS,
    A,
    B,
    boss_signals,
    cabinet,
    close,
    READY,
    call,
    done_work,
    handin,
    latest,
    passing,
    reviewed,
    later,
    notices_of,
    owner,
    pipeline,
    publish,
    return_to_work,
    started,
)

from kd import council, session_config
from kd.cabinet import api, flow, jobs, notify, outcomes, snapshot, stage_gate, stages

H1, H2 = "1" * 40, "2" * 40
MISSING = jobs.HEAD_MISSING + ": the copy has no refs/pull/7/head"
FINDING = {
    "severity": "Important",
    "where": "src/app.py:12",
    "what": "the empty list is not handled",
}


@pytest.fixture
def cab(tmp_path):
    return cabinet(tmp_path)


@pytest.fixture
def plan(cab):
    """A chain plan a → b: ``(parent, a, b)``."""
    parent, kids = started(cab, (A, B))
    return parent, kids["a"], kids["b"]


def passed(cab, stage):
    """The stage's hand-in passed and its card closed, as the coordinator's pass leaves them."""
    cab.store._conn.execute(
        "UPDATE stage_reviews SET gate='passed' WHERE id=?", (latest(cab, stage)["id"],)
    )
    close(cab, stage)


def test_a_handed_in_stage_orders_one_reviewer_on_the_plans_pull_head(cab, plan):
    parent, a, _ = plan
    row = handin(cab, a, H1)
    job = jobs.row(cab.store, row["job"])
    definition = job["data"]["definition"]
    assert (row["state"], row["gate"], row["base"], row["head"], row["parent"]) == (
        "reviewing",
        "",
        "",
        H1,
        parent.id,
    )
    assert (job["spec"], job["caller"], job["card"], job["agent"]) == (
        "reviewer",
        jobs.CABINET,
        a.id,
        "dev",
    )
    assert definition["pull"] == {"number": 7, "head": H1, "target": "main"}
    assert definition["limits"]["write"] == "none" and definition["requester"] == ""
    assert set(job["data"]["inputs"]) == {"card.md"}
    assert f"stage a of plan {parent.id}" in definition["brief"]
    assert "base: branch pr-base" in definition["brief"]
    assert boss_signals(cab) == []  # the coordinator wakes on the verdict, not before


def test_the_second_stage_is_reviewed_from_the_head_the_first_passed_on(cab, plan):
    _, a, b = plan
    row = handin(cab, a, H1)
    ce.end(cab, row["job"], {"verdict": "ready", "findings": []})
    passed(cab, a)
    second = handin(cab, b, H2)
    assert (second["base"], second["head"]) == (H1, H2)
    brief = jobs.row(cab.store, second["job"])["data"]["definition"]["brief"]
    assert f"base: {H1}" in brief


def test_a_ready_verdict_wakes_boss_with_it(cab, plan):
    parent, a, _ = plan
    row = handin(cab, a, H1)
    ce.end(cab, row["job"], {"verdict": "ready", "findings": []})
    after = latest(cab, a)
    assert (after["state"], after["verdict"], after["gate"]) == ("ready", "ready", "")
    assert boss_signals(cab) == [
        f"[stage] {a.id}: stage a of {parent.id} is handed in, review ready — read kd-task view {a.id}"
    ]


@pytest.mark.parametrize("verdict", ["with fixes", "not ready"])
def test_findings_are_posted_to_the_stages_thread_as_data(cab, plan, verdict):
    parent, a, _ = plan
    row = handin(cab, a, H1)
    ce.end(cab, row["job"], {"verdict": verdict, "findings": [FINDING]})
    after = latest(cab, a)
    assert (after["state"], after["verdict"], after["findings"]) == (
        "fixes",
        verdict,
        [FINDING],
    )
    message = cab.store.messages(a.id)[-1]
    assert (message["author"], message["kind"], message["seq"]) == (
        "system",
        "text",
        after["message"],
    )
    assert "Important" in message["body"] and "src/app.py:12" in message["body"]
    assert "the empty list is not handled" in message["body"]
    assert boss_signals(cab) == [
        f"[stage] {a.id}: stage a of {parent.id} is handed in, review {verdict} — read kd-task view {a.id}"
    ]


def test_a_stage_without_new_commits_skips_the_review(cab, plan):
    parent, a, _ = plan
    row = handin(cab, a)  # nothing published
    assert (row["state"], row["job"], row["head"]) == ("skipped", "", "")
    assert boss_signals(cab) == [
        f"[stage] {a.id}: stage a of {parent.id} is handed in, review none — read kd-task view {a.id}"
    ]


def test_a_stage_that_published_nothing_new_since_the_last_pass_skips_the_review(
    cab, plan
):
    _, a, b = plan
    row = handin(cab, a, H1)
    ce.end(cab, row["job"], {"verdict": "ready", "findings": []})
    passed(cab, a)
    second = handin(cab, b)
    assert (second["state"], second["job"], second["head"], second["base"]) == (
        "skipped",
        "",
        H1,
        H1,
    )


def test_a_missing_head_is_ordered_again_by_the_sweep(cab, plan):
    _, a, _ = plan
    row = handin(cab, a, H1)
    ce.end(cab, row["job"], state="failed", reason=MISSING)
    waiting = latest(cab, a)
    assert (waiting["state"], waiting["job"], waiting["gate"]) == ("reviewing", "", "")
    later(cab, seconds=119)
    assert stage_gate.sweep(cab.ctx) == 0  # not yet
    later(cab, seconds=1)
    assert stage_gate.sweep(cab.ctx) == 1
    again = latest(cab, a)
    assert again["job"] not in ("", row["job"])
    assert jobs.row(cab.store, again["job"])["data"]["definition"]["pull"]["head"] == H1
    assert boss_signals(cab) == []


def test_a_head_missing_past_the_wait_goes_to_the_owner(cab, plan):
    _, a, _ = plan
    row = handin(cab, a, H1)
    ce.end(cab, row["job"], state="failed", reason=MISSING)
    later(cab, minutes=int(council.limits()["head_wait_minutes"]) + 1)
    assert stage_gate.sweep(cab.ctx) == 1
    after = latest(cab, a)
    assert (after["state"], after["gate"]) == ("failed", "held")
    assert jobs.HEAD_MISSING in after["reason"]
    [notice] = notices_of(cab, a.id)
    assert notice["key"] == "notice.pipeline.stage_review_failed"
    assert jobs.HEAD_MISSING in notice["params"]["reason"]
    assert boss_signals(cab) == []


def test_a_new_hand_in_takes_the_older_one_back(cab, plan):
    _, a, _ = plan
    old = handin(cab, a, H1)
    return_to_work(cab, a)
    assert jobs.row(cab.store, old["job"])["state"] in ("cancelled", "cancelling")
    new = handin(cab, a, H2)
    assert new["id"] != old["id"] and new["head"] == H2
    # the older reviewer's block, should it still come, is nobody's verdict
    stale = {**jobs.row(cab.store, old["job"]), "state": "done"}
    stale["data"] = {**stale["data"], "result": {"verdict": "ready", "findings": []}}
    stage_gate.on_job_end(cab.ctx, stale)
    assert latest(cab, a)["id"] == new["id"] and latest(cab, a)["state"] == "reviewing"
    assert boss_signals(cab) == []


@pytest.mark.parametrize(
    "why", ["rate", "boss_absent", "observe", "bad_block", "failed", "blocked"]
)
def test_a_review_that_cannot_decide_shows_the_stage_to_the_owner(
    cab, plan, why, monkeypatch
):
    _, a, _ = plan
    if why == "rate":
        limits = council.limits()
        monkeypatch.setattr(council, "limits", lambda: {**limits, "jobs_per_day": 0})
    if why == "boss_absent":
        without = {name: n for name, n in HOSTED.agents.items() if name != "boss"}
        api.install_host(cab.ctx, lambda: session_config.HostConfig(without, 600))
    row = handin(cab, a, H1)
    if why == "observe":
        pipeline(cab, mode="observe")
    if why == "bad_block":
        ce.end(cab, row["job"], {"verdict": "perhaps", "findings": []})
    elif why == "failed":
        ce.end(cab, row["job"], state="failed", reason="the session ended")
    elif (
        why == "blocked"
    ):  # the reviewer could not read the change: its verdict is a placeholder
        unread = {
            "severity": "Important",
            "where": "repo/",
            "what": "no way to run git",
        }
        ce.end(
            cab,
            row["job"],
            {"status": "blocked", "verdict": "not ready", "findings": [unread]},
        )
    elif why != "rate":
        ce.end(cab, row["job"], {"verdict": "ready", "findings": []})
    after = latest(cab, a)
    assert after["gate"] == "held" and after["reason"]
    if why == "blocked":  # no verdict to return the stage on, and the owner reads why
        assert (after["state"], after["verdict"]) == ("failed", "")
        assert "no way to run git" in after["reason"]
    absent = why in ("boss_absent", "observe")
    assert [n["key"] for n in notices_of(cab, a.id)] == [
        "notice.pipeline.stage_boss_absent"
        if absent
        else "notice.pipeline.stage_review_failed"
    ]
    assert boss_signals(cab) == []
    assert (
        cab.store.card(a.id).status == "Review"
    )  # the owner's accept or return is next


def test_a_stage_of_a_plain_plan_orders_nothing(tmp_path):
    cab = cabinet(tmp_path, chain=False)
    _, kids = started(cab, (A, B))
    assert handin(cab, kids["a"], H1) is None
    assert boss_signals(cab) == [] and notices_of(cab, kids["a"].id) == []


def test_a_card_outside_a_plan_orders_nothing(cab):
    card = cab.card(status="In Progress")
    cab.store.edit_card(card.id, None, {"status": "Review"}, actor="agent:dev")
    assert stage_gate.latest(cab.store, card.id) is None


# --- the coordinator's decision ------------------------------------------------------------------------------


def refused(cab, op, stage, text, **fields):
    before = cab.store.card(stage.id).status
    with pytest.raises(api.ApiError) as exc:
        call(cab, op, stage, **fields)
    assert exc.value.kind == "refused" and text in str(exc.value), str(exc.value)
    assert cab.store.card(stage.id).status == before
    return exc.value


def test_boss_passes_a_reviewed_stage_and_the_next_one_starts(cab, plan):
    parent, a, b = plan
    _, seq = reviewed(cab, a, H1)
    out = passing(cab, a, seq)
    assert out["card"]["status"] == "Done" and out["changed"] is True
    assert latest(cab, a)["gate"] == "passed"
    assert (
        cab.store.card(b.id).status == "To Do"
        and cab.store.card(parent.id).status == "In Progress"
    )
    note = cab.store.messages(a.id)[-1]
    assert (
        note["author"] == "agent:boss"
        and "критерии закрыты" in note["body"]
        and f"#{seq}" in note["body"]
    )


def test_a_stage_that_changed_no_code_is_passed_without_a_review(cab, plan):
    _, a, _ = plan
    seq = done_work(cab, a)
    assert handin(cab, a)["state"] == "skipped"
    assert passing(cab, a, seq)["card"]["status"] == "Done"


@pytest.mark.parametrize("state", ["reviewing", "fixes", "failed"])
def test_no_pass_without_a_ready_review(cab, plan, state):
    _, a, _ = plan
    seq = done_work(cab, a)
    row = handin(cab, a, H1)
    if state == "fixes":
        ce.end(cab, row["job"], {"verdict": "with fixes", "findings": [FINDING]})
    if state == "failed":
        ce.end(cab, row["job"], state="failed", reason="the session ended")
    refused(
        cab,
        "boss.stage-pass",
        a,
        "review",
        reason="x",
        source={"card": a.id, "seq": seq},
    )
    assert latest(cab, a)["gate"] in ("", "held")


def test_no_pass_on_a_review_of_an_older_head(cab, plan):
    parent, a, _ = plan
    _, seq = reviewed(cab, a, H1)
    publish(cab, parent.id, H2)
    refused(
        cab,
        "boss.stage-pass",
        a,
        "the pull request moved",
        reason="x",
        source={"card": a.id, "seq": seq},
    )


def test_no_pass_twice_or_after_a_hold(cab, plan):
    _, a, _ = plan
    _, seq = reviewed(cab, a, H1)
    call(cab, "boss.stage-hold", a, reason="расходится с ТЗ")
    refused(
        cab, "boss.stage-pass", a, "held", reason="x", source={"card": a.id, "seq": seq}
    )


def test_no_pass_while_an_earlier_stage_is_reopened_and_a_pass_once_it_is_done_again(
    cab, plan
):
    _, a, b = plan
    _, first = reviewed(cab, a, H1)
    passing(cab, a, first)
    _, seq = reviewed(cab, b, H2)
    return_to_work(cab, a)  # the owner reopened the first stage
    refused(
        cab,
        "boss.stage-pass",
        b,
        f"an earlier stage {a.id} of the plan is In Progress — stage-hold it",
        reason="x",
        source={"card": b.id, "seq": seq},
    )
    assert cab.store.card(b.id).status == "Review" and latest(cab, b)["gate"] == ""
    close(cab, a)
    assert passing(cab, b, seq)["card"]["status"] == "Done"
    assert latest(cab, b)["gate"] == "passed"


def test_a_decided_hand_in_says_so_before_an_earlier_stage_is_judged(cab, plan):
    _, a, b = plan
    _, first = reviewed(cab, a, H1)
    passing(cab, a, first)
    _, seq = reviewed(cab, b, H2)
    call(cab, "boss.stage-hold", b, reason="ждёт владельца")
    return_to_work(cab, a)  # the owner reopened the first stage afterwards
    refused(
        cab,
        "boss.stage-pass",
        b,
        "this hand-in is already held",
        reason="x",
        source={"card": b.id, "seq": seq},
    )


def test_no_pass_with_an_unchecked_criterion(cab, plan):
    _, a, _ = plan
    _, seq = reviewed(cab, a, H1, tick=False)
    refused(
        cab,
        "boss.stage-pass",
        a,
        "acceptance criteria 1",
        reason="x",
        source={"card": a.id, "seq": seq},
    )


def test_no_pass_while_the_stages_agent_has_a_turn(cab, plan):
    _, a, _ = plan
    _, seq = reviewed(cab, a, H1)
    cab.store.wake_card(a.id, actor="user:owner", agent="@dev")
    refused(
        cab,
        "boss.stage-pass",
        a,
        "works on it",
        reason="x",
        source={"card": a.id, "seq": seq},
    )


def test_the_source_of_a_pass_is_a_report_on_the_stage_itself(cab, plan):
    _, a, b = plan
    row, seq = reviewed(cab, a, H1, {"verdict": "ready", "findings": []})
    elsewhere = cab.store.add_message(
        b.id, author="agent:dev", kind="text", body="готово"
    )["seq"]
    refused(
        cab,
        "boss.stage-pass",
        a,
        "itself",
        reason="x",
        source={"card": b.id, "seq": elsewhere},
    )
    notice = cab.store.add_message(
        a.id, author="system", kind="text", body="[system] …"
    )["seq"]
    refused(
        cab,
        "boss.stage-pass",
        a,
        "not a report",
        reason="x",
        source={"card": a.id, "seq": notice},
    )


def test_a_pass_is_refused_on_a_card_that_is_no_chain_stage(cab):
    card = cab.card(status="Review")
    seq = cab.store.add_message(
        card.id, author="agent:dev", kind="text", body="готово"
    )["seq"]
    refused(
        cab,
        "boss.stage-pass",
        card,
        "no stage of a chain plan",
        reason="x",
        source={"card": card.id, "seq": seq},
    )
    refused(cab, "boss.stage-hold", card, "no stage of a chain plan", reason="x")


def test_a_pass_on_an_older_revision_is_a_conflict(cab, plan):
    _, a, _ = plan
    _, seq = reviewed(cab, a, H1)
    with pytest.raises(api.ApiError) as exc:
        call(
            cab,
            "boss.stage-pass",
            a,
            revision=1,
            reason="x",
            source={"card": a.id, "seq": seq},
        )
    assert exc.value.kind == "conflict"


def test_boss_returns_a_stage_on_the_reviewers_verdict_and_wakes_its_agent(cab, plan):
    _, a, _ = plan
    row, _ = reviewed(cab, a, H1, {"verdict": "with fixes", "findings": [FINDING]})
    out = call(cab, "boss.stage-return", a, job=row["job"])
    assert out["card"]["status"] == "In Progress"
    assert latest(cab, a)["gate"] == "returned"
    wake = cab.store.commands(agent="dev", card=a.id, states=["queued"])
    assert len(wake) == 1 and a.id in wake[0]["text"]
    assert (
        f"the findings are kd-task message {a.id} {row['message']}" in wake[0]["text"]
    )  # the command that reads them
    note = [m for m in cab.store.messages(a.id) if m["author"] == "agent:boss"][-1]
    assert "with fixes" in note["body"] and f"#{row['message']}" in note["body"]


def test_the_third_return_is_refused(cab, plan):
    _, a, _ = plan
    for head in (H1, H2):
        row, _ = reviewed(cab, a, head, {"verdict": "not ready", "findings": [FINDING]})
        call(cab, "boss.stage-return", a, job=row["job"])
    row, _ = reviewed(cab, a, "3" * 40, {"verdict": "not ready", "findings": [FINDING]})
    refused(cab, "boss.stage-return", a, "returned twice", job=row["job"])
    call(cab, "boss.stage-hold", a, reason="две доработки не помогли")
    assert latest(cab, a)["gate"] == "held"


def test_no_return_on_a_ready_verdict_or_another_job(cab, plan):
    _, a, _ = plan
    row, _ = reviewed(cab, a, H1)
    refused(cab, "boss.stage-return", a, "with fixes", job=row["job"])
    return_to_work(cab, a)
    row, _ = reviewed(cab, a, H2, {"verdict": "with fixes", "findings": [FINDING]})
    refused(cab, "boss.stage-return", a, "not the review", job="f" * 32)


def test_a_hold_shows_the_stage_to_the_owner_with_the_reason(cab, plan):
    _, a, _ = plan
    reviewed(cab, a, H1)
    out = call(cab, "boss.stage-hold", a, reason="расходится с ТЗ в формате отчёта")
    after = latest(cab, a)
    assert out["card"]["status"] == "Review"
    assert (after["gate"], after["reason"]) == (
        "held",
        "расходится с ТЗ в формате отчёта",
    )
    assert notices_of(cab, a.id)[-1] == {
        "key": "notice.pipeline.stage_held",
        "params": {"reason": "расходится с ТЗ в формате отчёта"},
        "level": "warn",
        "source": "pipeline",
    }
    refused(cab, "boss.stage-hold", a, "held", reason="ещё раз")


def test_only_the_coordinator_decides_on_a_stage(cab, plan):
    _, a, _ = plan
    _, seq = reviewed(cab, a, H1)
    with pytest.raises(api.ApiError) as exc:
        cab.call(
            "digi",
            {
                "op": "boss.stage-pass",
                "agent": "dev",
                "task": a.id,
                "revision": cab.store.card(a.id).revision,
                "reason": "x",
                "source": {"card": a.id, "seq": seq},
            },
        )
    assert exc.value.kind == "refused" and "may not do this" in str(exc.value)
    assert cab.store.card(a.id).status == "Review"


def test_a_verdict_on_a_stage_boss_already_held_wakes_nobody(cab, plan):
    _, a, _ = plan
    done_work(cab, a)
    row = handin(cab, a, H1)
    call(cab, "boss.stage-hold", a, reason="не то, что в ТЗ")
    ce.end(cab, row["job"], READY)
    after = latest(cab, a)
    assert (after["state"], after["gate"]) == ("ready", "held")
    assert boss_signals(cab) == []


# --- what the owner sees -------------------------------------------------------------------------------------
def review_items(cab):
    found = api.dispatch(cab.ctx, owner(), {"op": "overview.get"})
    return [r["card"] for r in found["needs"] if r["kind"] == "review"]


def pushes(cab, tmp_path, kind):
    """The owner's notifications of ``kind`` for everything the journal holds."""
    n = notify.Notifier(
        cab.store, cab.users, None, usage_path=str(tmp_path / "usage.jsonl")
    )
    ids = [i for event in cab.store.events_since(0) for i in n.on_event(event, None)]
    rows = [cab.store.notification(i) for i in ids]
    return [r["tag"] for r in rows if r["kind"] == kind and r["user"] == "owner"]


def test_a_handed_in_chain_stage_is_not_the_owners_review_until_held(
    cab, plan, tmp_path
):
    _, a, _ = plan
    before = api.inbox_count(cab.ctx, owner())
    done_work(cab, a)
    handin(cab, a, H1)
    assert stage_gate.hidden_from_owner(cab.store, cab.store.card(a.id))
    assert a.id not in review_items(cab) and api.inbox_count(cab.ctx, owner()) == before
    call(cab, "boss.stage-hold", a, reason="расходится с ТЗ")
    assert not stage_gate.hidden_from_owner(cab.store, cab.store.card(a.id))
    assert a.id in review_items(cab) and api.inbox_count(cab.ctx, owner()) == before + 1
    assert (
        pushes(cab, tmp_path, "card.review") == []
    )  # the hold's own notice is what tells him
    assert pushes(cab, tmp_path, "notice.warn") == []  # handled once
    assert [r["kind"] for r in cab.store.notifications("owner")].count(
        "notice.warn"
    ) == 1


def test_a_plain_cards_review_reaches_the_owner_as_before(tmp_path):
    cab = cabinet(tmp_path, chain=False)
    _, kids = started(cab, (A, B))
    a = kids["a"]
    handin(cab, a)
    assert not stage_gate.hidden_from_owner(cab.store, cab.store.card(a.id))
    assert a.id in review_items(cab)
    assert pushes(cab, tmp_path, "card.review") == [a.id]


def test_boss_silent_for_thirty_minutes_shows_the_stage_to_the_owner(cab, plan):
    _, a, _ = plan
    reviewed(cab, a, H1)
    later(cab, minutes=stage_gate.BOSS_WAIT_MINUTES - 1)
    assert stage_gate.sweep(cab.ctx) == 0
    later(cab, minutes=2)
    assert stage_gate.sweep(cab.ctx) == 1
    after = latest(cab, a)
    assert (after["gate"], after["reason"]) == ("held", "boss_silent")
    assert notices_of(cab, a.id) == [
        {
            "key": "notice.pipeline.stage_boss_silent",
            "params": {"reason": ""},
            "level": "warn",
            "source": "pipeline",
        }
    ]
    assert stage_gate.sweep(cab.ctx) == 0
    assert a.id in review_items(cab)


def test_a_review_that_still_runs_is_not_boss_silence(cab, plan):
    _, a, _ = plan
    done_work(cab, a)
    handin(cab, a, H1)
    later(cab, minutes=stage_gate.BOSS_WAIT_MINUTES + 1)
    assert stage_gate.sweep(cab.ctx) == 0 and latest(cab, a)["gate"] == ""


def test_a_stage_the_owner_took_back_is_not_held_for_silence(cab, plan):
    _, a, _ = plan
    reviewed(cab, a, H1)
    return_to_work(cab, a)
    later(cab, minutes=stage_gate.BOSS_WAIT_MINUTES + 1)
    assert stage_gate.sweep(cab.ctx) == 0 and latest(cab, a)["gate"] == ""
    assert notices_of(cab, a.id) == []


def test_the_next_stage_is_reviewed_from_the_head_of_a_stage_the_owner_accepted(
    cab, plan
):
    """A held stage the owner accepted himself is as closed as one the coordinator passed."""
    _, a, b = plan
    reviewed(cab, a, H1, {"verdict": "with fixes", "findings": [FINDING]})
    call(cab, "boss.stage-hold", a, reason="спорное замечание")
    close(cab, a)
    assert handin(cab, b, H2)["base"] == H1


def test_the_plans_facts_name_the_chain_and_each_stages_gate(cab, plan):
    parent, a, b = plan
    facts = stages.facts(cab.store, parent.id)
    assert facts["chain"] is True and [i["gate"] for i in facts["items"]] == ["", ""]
    done_work(cab, a)
    row = handin(cab, a, H1)
    assert stages.facts(cab.store, parent.id)["items"][0]["gate"] == "reviewing"
    ce.end(cab, row["job"], {"verdict": "with fixes", "findings": [FINDING]})
    assert stages.facts(cab.store, parent.id)["items"][0]["gate"] == "fixes"
    call(cab, "boss.stage-return", a, job=row["job"])
    assert stages.facts(cab.store, parent.id)["items"][0]["gate"] == "returned"
    _, seq = reviewed(cab, a, H2)
    assert stages.facts(cab.store, parent.id)["items"][0]["gate"] == "ready"
    passing(cab, a, seq)
    assert [i["gate"] for i in stages.facts(cab.store, parent.id)["items"]] == [
        "passed",
        "",
    ]


def test_the_facts_of_a_plain_plan_carry_no_gate(tmp_path):
    cab = cabinet(tmp_path, chain=False)
    parent, kids = started(cab, (A, B))
    handin(cab, kids["a"])
    facts = stages.facts(cab.store, parent.id)
    assert facts["chain"] is False and [i["gate"] for i in facts["items"]] == ["", ""]


def text_of(cab, card):
    return snapshot.render(snapshot.build(cab.ctx, cab.store.card(card.id)))


def test_the_snapshot_names_the_chain_and_each_stages_gate(cab, plan):
    parent, a, b = plan
    done_work(cab, a)
    row = handin(cab, a, H1)
    assert (
        f"stages: chain · 0 of 2 done — a {a.id} Review @dev (reviewing), b {b.id} Blocked @dev"
        in text_of(cab, parent)
    )
    stage = text_of(cab, a)
    assert (
        f"· parent {parent.id} (chain: a branch agent/{parent.id}-… and a pull request"
        f" of {parent.id} in each repository)" in stage.splitlines()[0]
    )
    assert "next (derived): stage_review" in stage
    ce.end(cab, row["job"], READY)
    found = snapshot.next_step(snapshot.facts(cab.ctx, cab.store.card(a.id)))
    assert (found["what"], found["who"]) == (
        "run_queued",
        "@boss",
    )  # its wake with the verdict
    cab.store._conn.execute("UPDATE commands SET state='done' WHERE agent='boss'")
    found = snapshot.next_step(snapshot.facts(cab.ctx, cab.store.card(a.id)))
    assert (found["what"], found["who"]) == (
        "stage_gate",
        "@boss",
    ) and f"review ready (job {row['job']})" in found["text"]  # the job a return names
    call(cab, "boss.stage-hold", a, reason="расходится с ТЗ")
    found = snapshot.next_step(snapshot.facts(cab.ctx, cab.store.card(a.id)))
    assert (found["what"], found["who"]) == (
        "review",
        "@owner",
    ) and "расходится с ТЗ" in found["text"]


def test_the_snapshot_of_a_plain_stage_is_as_before(tmp_path):
    cab = cabinet(tmp_path, chain=False)
    parent, kids = started(cab, (A, B))
    done_work(cab, kids["a"])
    handin(cab, kids["a"])
    stage = text_of(cab, kids["a"])
    assert "chain" not in stage.splitlines()[0] and "next (derived): review" in stage
    assert (
        f"stages: 0 of 2 done — a {kids['a'].id} Review @dev, b {kids['b'].id} To Do @dev"
        in text_of(cab, parent)
    )


def test_the_inbox_page_lists_a_chain_stage_only_when_held(cab, plan, tmp_path):
    from web_env import WebEnv

    _, a, _ = plan
    done_work(cab, a)
    handin(cab, a, H1)
    w = WebEnv(tmp_path / "web", env=cab)
    w.login()
    assert a.id not in [c["id"] for c in w.client.get("/api/inbox").json()["review"]]
    call(cab, "boss.stage-hold", a, reason="расходится с ТЗ")
    assert a.id in [c["id"] for c in w.client.get("/api/inbox").json()["review"]]


def test_every_hold_notice_is_worded_in_both_languages():
    from kd.cabinet import i18n

    keys = [stage_gate.NOTICE, *stage_gate.SYSTEM_NOTICES.values()]
    assert set(stage_gate.SYSTEM_NOTICES) == {
        "boss_absent",
        "boss_silent",
        "review_failed",
    }
    for language in ("ru", "en"):
        assert all(key in i18n.load()[language] for key in keys)


def test_the_plans_facts_say_why_a_stage_is_held(cab, plan):
    parent, a, _ = plan
    reviewed(cab, a, H1)
    assert stages.facts(cab.store, parent.id)["items"][0]["reason"] == ""
    call(cab, "boss.stage-hold", a, reason="расходится с ТЗ")
    item = stages.facts(cab.store, parent.id)["items"][0]
    assert (item["gate"], item["reason"]) == ("held", "расходится с ТЗ")


def test_the_start_of_a_plan_says_it_will_run_as_a_chain(cab):
    from chain_env import planned

    def start_of(card):
        found = flow.actions(cab.ctx, owner(), cab.store.card(card.id))
        return next(a for a in found if a["id"] == "start")

    assert start_of(planned(cab, (A, B)))["chain"] is True
    assert "chain" not in start_of(planned(cab, (A, OPS)))
    plain = cab.briefed()
    assert "chain" not in start_of(plain)


def test_a_pass_sends_the_owner_no_done_push(cab, plan, tmp_path):
    _, a, _ = plan
    _, seq = reviewed(cab, a, H1)
    passing(cab, a, seq)
    assert cab.store.card(a.id).status == "Done"
    # a passed stage is not work the owner accepted: nothing tells him «done»
    assert pushes(cab, tmp_path, "card.done") == []
    assert cab.store.notifications("owner") == []


def test_a_review_whose_end_never_reached_the_gate_is_taken_by_the_sweep(
    cab, plan, monkeypatch
):
    _, a, _ = plan
    done_work(cab, a)
    row = handin(cab, a, H1)

    def boom(*args, **kwargs):
        raise RuntimeError("the gate failed on the job's end")

    with monkeypatch.context() as broken:
        broken.setattr(stage_gate, "_tell", boom)
        ce.end(cab, row["job"], READY)
    stuck = latest(cab, a)
    assert (stuck["state"], stuck["job"]) == (
        "reviewing",
        row["job"],
    )  # hidden, nobody woken
    assert boss_signals(cab) == []
    assert stage_gate.sweep(cab.ctx) == 1
    assert (latest(cab, a)["state"], latest(cab, a)["verdict"]) == ("ready", "ready")
    assert len(boss_signals(cab)) == 1
    assert stage_gate.sweep(cab.ctx) == 0


def test_a_review_the_host_lost_goes_to_the_owner(cab, plan):
    _, a, _ = plan
    done_work(cab, a)
    row = handin(cab, a, H1)
    assert stage_gate.sweep(cab.ctx) == 0  # a job that still runs is left alone
    cab.store._conn.execute("DELETE FROM jobs WHERE id=?", (row["job"],))
    assert stage_gate.sweep(cab.ctx) == 1
    after = latest(cab, a)
    assert (after["state"], after["gate"]) == ("failed", "held")
    assert jobs.HOST_LOST in after["reason"]
    assert a.id in review_items(cab)


def test_the_stages_agent_cannot_close_it_past_the_gate(cab, plan):
    _, a, _ = plan
    cab.store._conn.execute("UPDATE projects SET done_by='dev' WHERE id='digipaddock'")
    done_work(cab, a)
    why = outcomes.done_refusal(cab.ctx, cab.store.card(a.id), "dev")
    assert why and "a stage of a chain plan" in why and "--outcome review" in why


def test_the_agent_of_a_plain_stage_closes_it_as_before(tmp_path):
    cab = cabinet(tmp_path, chain=False)
    _, kids = started(cab, (A, B))
    cab.store._conn.execute("UPDATE projects SET done_by='dev' WHERE id='digipaddock'")
    assert outcomes.done_refusal(cab.ctx, cab.store.card(kids["a"].id), "dev") is None
