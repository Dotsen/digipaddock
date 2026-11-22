"""the rounds of a code council — a pull request's published head is the basis, the critics read it in
the developer's worker pool, a head the host's copy has not got yet is waited for, the pull request's end ends the
council."""

import json

import council_env as ce
import pytest
from test_cabinet_outcomes import begin, enforce, question
from test_cabinet_outcomes import end as end_turn

from kd import council, worker_slots
from kd.cabinet import api, council_rounds, councils, jobs, outcomes, problems, waits

A, B, C, D = "a" * 40, "b" * 40, "c" * 40, "d" * 40
MISSING = "head_missing: the copy has no refs/pull/7/head"


@pytest.fixture
def env(tmp_path):
    return ce.make(tmp_path)


def _council(env, card_id):
    return ce.council_of(env, card_id, "code")


def _runs(env, card_id, round_no=None):
    return ce.runs_of(env, card_id, round_no, kind="code")


def _problems(env, card_id):
    return sorted(
        r[0]
        for r in env.store._conn.execute(
            "SELECT kind FROM problems WHERE card=? AND state='open'", (card_id,)
        )
    )


def _later(env, minutes=0, seconds=0):
    stamp = env.when().timestamp() + minutes * 60 + seconds
    from datetime import datetime, timezone

    env.now = datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- a published head gathers the council --------------------------------------------------------------------
def test_a_published_head_gathers_the_code_council(env):
    card = ce.coding(env)

    found = ce.head(env, ce.pull(card.id))

    assert (found["kind"], found["state"], found["round"], found["rounds_max"]) == (
        "code",
        "running",
        1,
        3,
    )
    basis = found["basis"]
    assert {
        key: basis[key] for key in ("repo", "project", "number", "head", "target")
    } == {
        "repo": "acme/web",
        "project": "web",
        "number": 7,
        "head": A,
        "target": "main",
    }
    assert [
        (e["round"], e["head"], e["target"], e["state"]) for e in basis["rounds"]
    ] == [(1, A, "main", "running")]
    runs = _runs(env, card.id)
    assert [(r["n"], r["model"], r["state"]) for r in runs] == [
        (1, "claude:opus", "queued"),
        (2, "or-gpt5", "queued"),
    ]
    job = jobs.row(env.store, runs[0]["job"])
    definition = job["data"]["definition"]
    assert (
        job["agent"],
        definition["type"],
        definition["repo"],
        definition["spec"],
    ) == ("dev", "developer", "web", "code-critic")
    assert definition["pull"] == {"number": 7, "head": A, "target": "main"}
    assert definition["limits"]["write"] == "none" and definition["caller"] == "cabinet"
    assert set(job["data"]["inputs"]) == {"card.md"}
    assert (
        f"card {card.id}: pull request web#7, head {A[:12]} into main"
        in definition["brief"]
    )


def test_a_pull_below_min_lines_gathers_nobody(env):
    card = ce.coding(env)

    assert (
        ce.head(env, ce.pull(card.id, lines=99)) is None
    )  # its 400 lines of tests do not count

    assert _council(env, card.id) is None
    assert ce.head(env, ce.pull(card.id, lines=100))["state"] == "running"


def test_min_lines_zero_gathers_for_any_pull(env):
    card = ce.coding(
        env, {"code": {"critics": ["claude:opus"], "rounds": 3, "min_lines": 0}}
    )

    assert ce.head(env, ce.pull(card.id, lines=0))["state"] == "running"


def test_a_card_without_code_critics_gathers_nobody(env):
    card = ce.coding(env, ce.TWO)  # a council of the brief only

    assert ce.head(env, ce.pull(card.id)) is None


def test_the_same_head_again_is_the_same_round(env):
    card = ce.coding(env)
    first = ce.head(env, ce.pull(card.id))

    again = ce.head(env, ce.pull(card.id, title="another title"))

    assert (again["id"], again["round"], again["state"]) == (first["id"], 1, "running")
    assert len(_runs(env, card.id)) == 2
    assert len(councils.of_card(env.store, card.id, "code")) == 1


# --- a new head ------------------------------------------------------------------------------------------------
def test_a_new_head_while_critics_read_supersedes_the_round(env):
    card = ce.coding(env)
    ce.head(env, ce.pull(card.id))
    reading = [r["job"] for r in _runs(env, card.id, 1)]

    found = ce.head(env, ce.pull(card.id, head=B))

    assert (found["state"], found["round"], found["basis"]["head"]) == ("running", 2, B)
    assert [e["state"] for e in found["basis"]["rounds"]] == ["superseded", "running"]
    assert [r["state"] for r in _runs(env, card.id, 1)] == ["cancelled"] * 2
    assert all(jobs.row(env.store, job)["state"] == "cancelled" for job in reading)
    assert all(jobs.row(env.store, job)["reason"] == "superseded" for job in reading)
    second = jobs.row(env.store, _runs(env, card.id, 2)[0]["job"])
    assert second["data"]["definition"]["pull"]["head"] == B


def test_a_new_head_at_needs_writer_is_the_next_round(env):
    card = ce.coding(env)
    ce.head(env, ce.pull(card.id))
    ce.play(
        env,
        card.id,
        ce.block("with_fixes", [ce.finding()]),
        ce.block("ready"),
        kind="code",
    )
    assert _council(env, card.id)["state"] == "needs_writer"

    found = ce.head(env, ce.pull(card.id, head=B))

    assert (found["state"], found["round"]) == ("running", 2)
    job = jobs.row(env.store, _runs(env, card.id, 2)[0]["job"])
    assert "r1.f1 major unanswered" in job["data"]["definition"]["brief"]
    assert set(job["data"]["inputs"]) == {"card.md", "council.md"}
    assert (
        f"round 1 · code head {A[:12]} · ended" in job["data"]["inputs"]["council.md"]
    )


def test_a_new_head_past_the_last_round_limits_the_council(env):
    card = ce.coding(
        env, {"code": {"critics": ["claude:opus"], "rounds": 1, "min_lines": 0}}
    )
    ce.head(env, ce.pull(card.id))
    ce.play(env, card.id, ce.block("not_ready", [ce.finding("critical")]), kind="code")

    found = ce.head(env, ce.pull(card.id, head=B))

    assert (found["state"], found["end_reason"], found["round"]) == (
        "limited",
        "rounds",
        1,
    )
    assert _runs(env, card.id, 2) == []


def test_a_new_head_after_the_end_gathers_a_new_council(env):
    card = ce.coding(env)
    first = ce.head(env, ce.pull(card.id))
    ce.play(env, card.id, ce.block("ready"), ce.block("ready"), kind="code")
    assert _council(env, card.id)["state"] == "clean"

    same = ce.head(
        env, ce.pull(card.id)
    )  # the head it read, published again: nobody reads it twice
    assert (same["id"], same["state"]) == (first["id"], "clean")
    new = ce.head(env, ce.pull(card.id, head=B))

    assert new["id"] != first["id"] and (
        new["state"],
        new["round"],
        new["basis"]["head"],
    ) == ("running", 1, B)


def test_another_target_on_the_same_head_is_a_new_round(env):
    card = ce.coding(env)
    ce.head(env, ce.pull(card.id))

    found = ce.head(env, ce.pull(card.id, target="release"))

    assert (found["round"], found["basis"]["target"]) == (2, "release")


def test_two_pulls_of_a_card_have_a_council_each(env):
    card = ce.coding(env)
    first = ce.head(env, ce.pull(card.id))

    second = ce.head(env, ce.pull(card.id, number=8, head=B))

    assert second["id"] != first["id"] and second["round"] == 1
    assert councils.row(env.store, first["id"])["state"] == "running"
    assert (
        councils.open_for_pull(env.store, card.id, "acme/web", 7)["id"] == first["id"]
    )
    assert (
        councils.open_for_pull(env.store, card.id, "acme/web", 8)["id"] == second["id"]
    )
    assert councils.open_for_pull(env.store, card.id, "acme/other", 7) is None


def test_for_head_names_the_council_that_read_the_head(env):
    card = ce.coding(env)
    first = ce.head(env, ce.pull(card.id))
    ce.head(env, ce.pull(card.id, head=B))

    assert councils.for_head(env.store, card.id, "acme/web", 7, B)["id"] == first["id"]
    assert (
        councils.for_head(env.store, card.id, "acme/web", 7, A) is None
    )  # superseded: nobody read it to the end
    assert councils.for_head(env.store, card.id, "acme/web", 7, C) is None
    assert councils.for_head(env.store, card.id, "acme/web", 8, B) is None


# --- the head the copy has not got yet --------------------------------------------------------------------------
def test_a_missing_head_waits_and_is_tried_again(env):
    card = ce.coding(env)
    ce.head(env, ce.pull(card.id))
    waiting, other = _runs(env, card.id)

    ce.end(env, waiting["job"], state="failed", reason=MISSING)

    run = _runs(env, card.id)[0]
    assert (run["state"], run["job"], run["fail_reason"]) == (
        "queued",
        waiting["job"],
        "",
    )
    assert _council(env, card.id)["state"] == "running"
    assert ce.reviews(env, card.id) == [] and _problems(env, card.id) == []
    _later(env, seconds=119)
    assert outcomes.sweep(env.ctx)["councils"] == {"lost": 0, "stalled": 0}
    assert _runs(env, card.id)[0]["job"] == waiting["job"]  # not yet

    _later(env, seconds=1)
    assert outcomes.sweep(env.ctx)["councils"] == {"lost": 0, "stalled": 0}

    again = _runs(env, card.id)[0]
    assert again["state"] == "queued" and again["job"] not in ("", waiting["job"])
    job = jobs.row(env.store, again["job"])
    assert (job["state"], job["data"]["definition"]["pull"]["head"]) == ("queued", A)
    assert job["data"]["definition"]["brief"].splitlines()[0].endswith("critic 1 of 2")
    ce.end(env, again["job"], ce.block("ready"))
    ce.end(env, other["job"], ce.block("ready"))
    assert _council(env, card.id)["state"] == "clean"


def test_a_head_missing_past_the_wait_fails_the_run(env):
    card = ce.coding(env)
    ce.head(env, ce.pull(card.id))
    late, waiting = _runs(env, card.id)
    ce.end(
        env, waiting["job"], state="failed", reason=MISSING
    )  # waits: the round has just begun
    _later(env, minutes=20)

    ce.end(
        env, late["job"], state="failed", reason=MISSING
    )  # twenty minutes on: no more waiting

    first, second = _runs(env, card.id)
    assert (first["state"], first["fail_reason"]) == ("failed", MISSING)
    assert second["state"] == "queued" and _council(env, card.id)["state"] == "running"
    assert (
        outcomes.sweep(env.ctx)["councils"]["lost"] == 1
    )  # the one that waited: its time is over too
    first, second = _runs(env, card.id)
    assert (second["state"], second["fail_reason"]) == ("failed", MISSING)
    found = _council(env, card.id)
    assert (found["state"], found["end_reason"]) == ("failed", "no_critic")
    assert "council_run_failed" in _problems(
        env, card.id
    ) or "council_failed" in _problems(env, card.id)


def test_a_retry_that_cannot_start_fails_the_run(env, monkeypatch):
    card = ce.coding(env)
    ce.head(env, ce.pull(card.id))
    waiting, _ = _runs(env, card.id)
    ce.end(env, waiting["job"], state="failed", reason=MISSING)
    real = council.limits()
    monkeypatch.setattr(
        council, "limits", lambda path=None: {**real, "jobs_per_day": 1}
    )  # the other critic's job is the day's one; the one that met no head does not count
    _later(env, seconds=120)

    outcomes.sweep(env.ctx)

    run = _runs(env, card.id)[0]
    assert (run["state"], run["fail_reason"]) == ("failed", "rate")
    assert len(ce.reviews(env, card.id)) == 1 and "council_run_failed" in _problems(
        env, card.id
    )


def test_a_head_missing_never_waits_for_a_brief(env):
    card = ce.draft(env)
    ce.handin(env, card.id)
    first = ce.runs_of(env, card.id)[0]

    ce.end(env, first["job"], state="failed", reason=MISSING)

    assert ce.runs_of(env, card.id)[0]["state"] == "failed"


# --- what ends it ----------------------------------------------------------------------------------------------
def test_a_merged_pull_cancels_its_council(env):
    card = ce.coding(env)
    first = ce.head(env, ce.pull(card.id))
    other = ce.head(env, ce.pull(card.id, number=8, head=B))
    reading = [r["job"] for r in councils.runs(env.store, first["id"])]

    council_rounds.on_pull_ended(env.ctx, card.id, "acme/web", 7)

    found = councils.row(env.store, first["id"])
    assert (found["state"], found["end_reason"]) == ("cancelled", "pr_closed")
    assert all(jobs.row(env.store, job)["state"] == "cancelled" for job in reading)
    assert councils.row(env.store, other["id"])["state"] == "running"
    assert ce.wakes(env, "dev") == []


def test_a_closed_card_cancels_the_code_council(env):
    card = ce.coding(env)
    found = ce.head(env, ce.pull(card.id))

    env.store.edit_card(card.id, None, {"status": "Done"}, actor="user:owner")

    found = councils.row(env.store, found["id"])
    assert (found["state"], found["end_reason"]) == ("cancelled", "card_closed")


def test_no_developer_slot_fails_the_council(env, tmp_path, monkeypatch):
    card = ce.coding(env)
    pools = tmp_path / "worker-slots.json"
    pools.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "requester": "boss",
                "model": "sonnet",
                "agents": {"analyst": ["worker1"]},
                "limits": {
                    "jobs_per_day": 12,
                    "attempts_per_card_spec": 3,
                    "timeout_minutes": 60,
                },
            }
        ),
        encoding="utf-8",
    )
    real = worker_slots.load_config
    monkeypatch.setattr(
        council_rounds.worker_slots, "load_config", lambda path: real(pools)
    )

    found = ce.head(env, ce.pull(card.id))

    assert [r["fail_reason"] for r in _runs(env, card.id)] == ["no_slots"] * 2
    assert (found["state"], found["end_reason"]) == ("failed", "no_critic")


def test_the_critics_block_of_another_shape_fails_the_run(env):
    card = ce.coding(env)
    ce.head(env, ce.pull(card.id))

    ce.play(
        env, card.id, ce.block("with fixes"), ce.block("needs_changes"), kind="code"
    )

    first, second = _runs(env, card.id)
    assert first["state"] == second["state"] == "failed"
    assert (
        first["fail_reason"]
        == "bad_result: verdict is one of ready, with_fixes, not_ready"
    )
    assert _council(env, card.id)["state"] == "failed"


# --- the writer ---------------------------------------------------------------------------------------------------
def test_the_round_end_wakes_the_holder_and_leaves_the_card(env):
    card = ce.coding(env)
    ce.head(env, ce.pull(card.id))

    ce.play(
        env,
        card.id,
        ce.block("with_fixes", [ce.finding()]),
        ce.block("ready"),
        kind="code",
    )

    found = _council(env, card.id)
    assert (
        found["state"] == "needs_writer" and councils.writer(env.store, found) == "dev"
    )
    record = env.store.card(card.id)
    assert (record.assignee, record.status) == (
        "@dev",
        "In Progress",
    )  # nothing is handed over
    assert "spec:implementer" in record.labels
    [wake] = ce.wakes(env, "dev")
    assert wake["text"] == (
        f"[council] {card.id}: round 1 of the code council ended — read kd-work council {card.id}"
        " and answer each finding"
    )


def test_a_card_the_owner_holds_has_no_writer_to_wake(env):
    card = ce.coding(env)
    ce.head(env, ce.pull(card.id))
    env.store.edit_card(card.id, None, {"assignee": "@owner"}, actor="agent:dev")

    ce.play(
        env,
        card.id,
        ce.block("with_fixes", [ce.finding()]),
        ce.block("ready"),
        kind="code",
    )

    found = _council(env, card.id)
    assert found["state"] == "needs_writer" and councils.writer(env.store, found) == ""
    assert [w for w in ce.wakes(env, "dev") if w["text"].startswith("[council]")] == []


def test_the_coordinator_is_never_a_councils_writer(env):
    card = ce.coding(env)
    ce.head(env, ce.pull(card.id))
    env.store.edit_card(card.id, None, {"assignee": "@boss"}, actor="agent:dev")

    ce.play(
        env,
        card.id,
        ce.block("with_fixes", [ce.finding()]),
        ce.block("ready"),
        kind="code",
    )

    found = _council(env, card.id)
    assert found["state"] == "needs_writer" and councils.writer(env.store, found) == ""
    assert [w for w in ce.wakes(env, "boss") if w["text"].startswith("[council]")] == []


@pytest.mark.parametrize("holder", ["owner", "boss"])
def test_a_council_whose_card_no_writer_holds_is_stalled_on_the_holder(env, holder):
    # the pull request's decision is held back: a council nobody can answer must not wait in silence
    card = ce.coding(env)
    ce.head(env, ce.pull(card.id))
    env.store.edit_card(card.id, None, {"assignee": "@" + holder}, actor="agent:dev")
    ce.play(
        env,
        card.id,
        ce.block("with_fixes", [ce.finding()]),
        ce.block("ready"),
        kind="code",
    )
    _later(env, minutes=59)
    assert outcomes.sweep(env.ctx)["councils"] == {"lost": 0, "stalled": 0}

    _later(env, minutes=2)

    assert outcomes.sweep(env.ctx)["councils"] == {"lost": 0, "stalled": 1}
    [problem] = [
        p
        for p in problems.open_for(env.store, card.id)
        if p["kind"] == "council_stalled"
    ]
    assert problem["agent"] == holder


# --- the pull request's decision waits for its council ------------------------------------------------
def _owner():
    from kd.cabinet import api

    return api.Principal("user", -1, "owner")


def test_a_pull_under_its_council_opens_no_decision(env):
    card = ce.coding(env)

    out = ce.publish(env, ce.pull(card.id))

    found = _council(env, card.id)
    assert out["decision"] is None
    assert out["council"] == {
        "id": found["id"],
        "round": 1,
        "rounds": 3,
        "state": "running",
    }
    assert out["pr"]["head"] == A and ce.pr_decisions(env, card.id) == []


def test_the_decision_opens_when_the_council_ends_clean(env):
    card = ce.coding(env)
    ce.publish(env, ce.pull(card.id, effect="deploys the site", checks=["unit"]))

    ce.play(env, card.id, ce.block("ready"), ce.block("ready"), kind="code")

    [decision] = ce.pr_decisions(env, card.id)
    assert decision["requested_by"] == "agent:dev"
    payload = decision["payload"]
    assert (payload["repo"], payload["number"], payload["head"], payload["target"]) == (
        "acme/web",
        7,
        A,
        "main",
    )
    assert (payload["effect"], payload["checks"]) == (
        "deploys the site",
        ["unit"],
    )  # the passport of the publish


@pytest.mark.parametrize(
    "state, reason", [("settled", "rejected"), ("limited", "unanswered")]
)
def test_the_decision_opens_when_the_council_ends_with_findings(env, state, reason):
    card = ce.coding(env)
    ce.publish(env, ce.pull(card.id))
    ce.play(
        env,
        card.id,
        ce.block("with_fixes", [ce.finding()]),
        ce.block("ready"),
        kind="code",
    )
    assert ce.pr_decisions(env, card.id) == []  # the writer's move: still held

    council_rounds._end(
        env.ctx, _council(env, card.id), env.store.card(card.id), state, reason
    )

    assert [d["payload"]["head"] for d in ce.pr_decisions(env, card.id)] == [A]


def test_the_decision_opens_when_no_critic_could_read(env):
    card = ce.coding(env)
    ce.publish(env, ce.pull(card.id))

    ce.play(env, card.id, None, None, kind="code")

    assert _council(env, card.id)["state"] == "failed"
    assert [d["payload"]["head"] for d in ce.pr_decisions(env, card.id)] == [A]


def test_a_council_that_could_not_start_opens_the_decision_at_once(env, monkeypatch):
    card = ce.coding(env)
    monkeypatch.setattr(
        council_rounds,
        "_plan",
        lambda ctx, kind: (_ for _ in ()).throw(council_rounds._NoRun("no_slots")),
    )

    out = ce.publish(env, ce.pull(card.id))

    assert (
        out["council"]["state"] == "failed" and out["decision"]["payload"]["head"] == A
    )
    assert len(ce.pr_decisions(env, card.id)) == 1


def test_a_new_head_past_the_last_round_opens_the_decision_on_it(env):
    card = ce.coding(
        env, {"code": {"critics": ["claude:opus"], "rounds": 1, "min_lines": 0}}
    )
    ce.publish(env, ce.pull(card.id))
    ce.play(env, card.id, ce.block("not_ready", [ce.finding("critical")]), kind="code")

    out = ce.publish(env, ce.pull(card.id, head=B))

    assert (
        out["council"]["state"] == "limited" and out["decision"]["payload"]["head"] == B
    )
    assert [d["payload"]["head"] for d in ce.pr_decisions(env, card.id)] == [B]


def test_cancelling_the_council_opens_the_decision(env):
    from kd.cabinet import api

    card = ce.coding(env)
    ce.publish(env, ce.pull(card.id))
    found = _council(env, card.id)

    api.dispatch(env.ctx, _owner(), {"op": "council.cancel", "council": found["id"]})

    assert _council(env, card.id)["end_reason"] == "owner"
    assert [d["payload"]["head"] for d in ce.pr_decisions(env, card.id)] == [A]


def test_taking_the_critics_off_the_card_opens_the_decision(env):
    card = ce.coding(env)
    ce.publish(env, ce.pull(card.id))

    env.store.edit_card(
        card.id, None, {"council": {"code": {"critics": []}}}, actor="user:owner"
    )

    assert (_council(env, card.id)["state"], _council(env, card.id)["end_reason"]) == (
        "cancelled",
        "removed",
    )
    assert [d["payload"]["head"] for d in ce.pr_decisions(env, card.id)] == [A]


def test_the_same_head_published_again_keeps_the_hold(env):
    card = ce.coding(env)
    first = ce.publish(env, ce.pull(card.id))

    again = ce.publish(env, ce.pull(card.id))

    assert again["decision"] is None and again["council"] == first["council"]
    assert ce.pr_decisions(env, card.id) == [] and len(_runs(env, card.id)) == 2


def test_the_same_head_published_after_the_council_keeps_its_decision(env):
    card = ce.coding(env)
    ce.publish(env, ce.pull(card.id))
    ce.play(env, card.id, ce.block("ready"), ce.block("ready"), kind="code")
    [decision] = ce.pr_decisions(env, card.id)

    again = ce.publish(env, ce.pull(card.id))

    assert (
        again["decision"]["id"] == decision["id"]
        and again["council"]["state"] == "clean"
    )
    assert (
        len(ce.pr_decisions(env, card.id)) == 1
        and len(councils.of_card(env.store, card.id, "code")) == 1
    )


def test_a_new_head_expires_the_open_decision_and_holds_again(env):
    card = ce.coding(env)
    ce.publish(env, ce.pull(card.id))
    ce.play(env, card.id, ce.block("ready"), ce.block("ready"), kind="code")
    [first] = ce.pr_decisions(env, card.id)

    out = ce.publish(env, ce.pull(card.id, head=B))

    assert out["decision"] is None and out["council"]["state"] == "running"
    assert ce.pr_decisions(env, card.id) == []
    assert [d["id"] for d in ce.pr_decisions(env, card.id, "expired")] == [first["id"]]


def test_a_pull_below_the_threshold_opens_the_decision_at_once(env):
    card = ce.coding(env)

    out = ce.publish(env, ce.pull(card.id, lines=10))

    assert out["council"] is None and out["decision"]["payload"]["head"] == A
    assert len(ce.pr_decisions(env, card.id)) == 1


def test_a_decision_already_open_on_the_head_gathers_nobody(env):
    card = ce.coding(env, council=None)
    first = ce.publish(env, ce.pull(card.id))["decision"]
    env.store.edit_card(
        card.id, None, {"council": ce.CODE}, actor="user:owner"
    )  # the critics come later

    again = ce.publish(env, ce.pull(card.id))

    assert again["decision"]["id"] == first["id"] and again["council"] is None
    assert _council(env, card.id) is None
    assert (
        ce.publish(env, ce.pull(card.id, head=B))["council"]["state"] == "running"
    )  # from the next head on


def test_a_merged_pull_opens_nothing(env):
    card = ce.coding(env)
    ce.publish(env, ce.pull(card.id))
    reading = _runs(env, card.id)

    env.call(
        "digi-forge",
        {"op": "pr.state", "repo": "acme/web", "number": 7, "state": "merged"},
    )

    found = _council(env, card.id)
    assert (found["state"], found["end_reason"]) == ("cancelled", "pr_closed")
    assert ce.pr_decisions(env, card.id) == []
    ce.report(
        env,
        {
            "type": "job",
            "job": reading[0]["job"],
            "state": "cancelled",
            "usage": ce.USED,
        },
    )
    assert ce.pr_decisions(env, card.id) == []


def test_a_pull_published_as_closed_ends_its_council(env):
    card = ce.coding(env)
    ce.publish(env, ce.pull(card.id))

    out = ce.publish(env, ce.pull(card.id, state="closed"))

    assert out["decision"] is None and out["council"] is None
    assert (
        _council(env, card.id)["end_reason"] == "pr_closed"
        and ce.pr_decisions(env, card.id) == []
    )


def test_a_pull_moved_to_another_card_ends_the_old_cards_council(env):
    card, other = ce.coding(env), ce.coding(env, council=None)
    ce.publish(env, ce.pull(card.id))

    out = ce.publish(env, ce.pull(other.id))

    assert _council(env, card.id)["end_reason"] == "pr_closed"
    assert out["decision"]["card"] == other.id and ce.pr_decisions(env, card.id) == []


def test_the_decision_is_not_opened_for_a_head_that_moved(env):
    card = ce.coding(env)
    ce.publish(env, ce.pull(card.id))
    env.store.pr_put(
        {**ce.pull(card.id, head=B)}, actor="forge"
    )  # the board holds another head by now

    ce.play(env, card.id, ce.block("ready"), ce.block("ready"), kind="code")

    assert (
        _council(env, card.id)["state"] == "clean"
        and ce.pr_decisions(env, card.id) == []
    )


def test_a_closed_card_opens_no_decision(env):
    card = ce.coding(env)
    ce.publish(env, ce.pull(card.id))

    env.store.edit_card(card.id, None, {"status": "Done"}, actor="user:owner")

    assert (
        _council(env, card.id)["end_reason"] == "card_closed"
        and ce.pr_decisions(env, card.id) == []
    )


def test_a_decision_that_cannot_open_does_not_undo_the_councils_end(env, monkeypatch):
    from kd.cabinet import prs_api

    card = ce.coding(env)
    ce.publish(env, ce.pull(card.id))
    monkeypatch.setattr(prs_api, "council_ended", lambda ctx, found: 1 / 0)

    ce.play(env, card.id, ce.block("ready"), ce.block("ready"), kind="code")

    assert (
        _council(env, card.id)["state"] == "clean"
        and ce.pr_decisions(env, card.id) == []
    )
    [notice] = [
        e["payload"]
        for e in env.store.card_events(card.id)
        if e["kind"] == "notice"
        and e["payload"]["key"] == "notice.council.decision_failed"
    ]  # the owner is told: nothing else would say a pull request waits without its decision
    assert (notice["level"], notice["params"]) == ("warn", {"number": 7})
    monkeypatch.undo()
    assert (
        ce.publish(env, ce.pull(card.id))["decision"]["payload"]["head"] == A
    )  # the next publish opens it


def test_publish_answers_with_how_the_council_ended(env):
    card = ce.coding(env)
    ce.publish(env, ce.pull(card.id))
    ce.play(env, card.id, ce.block("ready"), ce.block("ready"), kind="code")

    out = ce.publish(env, ce.pull(card.id))

    assert out["council"] == {
        "id": _council(env, card.id)["id"],
        "round": 1,
        "rounds": 3,
        "state": "clean",
    }


# --- the developer's move -----------------------------------------------------------------------------
REJECTED = "r1.f1: the gateway already limits it"


def _needs_writer(env):
    """A pull request whose council's first round left one major finding: the developer's move."""
    card = ce.coding(env)
    ce.publish(env, ce.pull(card.id))
    found = ce.play(
        env,
        card.id,
        ce.block("with_fixes", [ce.finding()]),
        ce.block("ready"),
        kind="code",
    )
    assert found["state"] == "needs_writer"
    return card.id, found["id"]


def _turn(env, card_id):
    return begin(env, env.store.card(card_id), agent="dev", kind="signal")


def _finish(env, card_id, outcome="wait_dependency", **more):
    request = {
        "op": "turn.finish",
        "agent": "dev",
        "card": card_id,
        "outcome": outcome,
        "note": "what is next",
    }
    return env.call("digi", {**request, **more})


def _refused(call):
    with pytest.raises(api.ApiError) as err:
        call()
    assert err.value.kind == "refused", str(err.value)
    return str(err.value)


def _queued(env, card_id):
    found = env.store.commands(card=card_id, states=["queued"])
    return [(c["kind"], c["key"]) for c in found if c["agent"] == "dev"]


def test_a_writer_waiting_for_the_council_gets_one_continuation_and_no_signal(env):
    enforce(env)
    card = ce.coding(env)
    council_id = ce.publish(env, ce.pull(card.id))["council"]["id"]
    command, session = _turn(env, card.id)
    out = _finish(env, card.id, councils=[council_id])
    end_turn(env, command, session)
    assert [(w["kind"], w["ref"]) for w in out["waits"]] == [("council", council_id)]

    ce.play(
        env,
        card.id,
        ce.block("with_fixes", [ce.finding()]),
        ce.block("ready"),
        kind="code",
    )

    assert [w["state"] for w in waits.of_command(env.store, command)] == ["resolved"]
    assert _queued(env, card.id) == [
        ("continue", f"wait:{command}")
    ]  # the wait's end is the wake: no signal


def test_a_round_that_ends_in_the_writers_turn_wakes_it_once(env):
    enforce(env)
    card = ce.coding(env)
    council_id = ce.publish(env, ce.pull(card.id))["council"]["id"]
    command, session = _turn(env, card.id)
    _finish(env, card.id, councils=[council_id])

    ce.play(
        env,
        card.id,
        ce.block("with_fixes", [ce.finding()]),
        ce.block("ready"),
        kind="code",
    )
    assert (
        _queued(env, card.id) == []
    )  # the turn still runs: its wait is not counted yet

    end_turn(env, command, session)
    assert _queued(env, card.id) == [("continue", f"wait:{command}")]


def test_a_waiting_writer_is_signalled_where_continuations_are_only_observed(env):
    card = ce.coding(env)
    council_id = ce.publish(env, ce.pull(card.id))["council"]["id"]
    command, session = _turn(env, card.id)
    _finish(env, card.id, councils=[council_id])
    end_turn(env, command, session)

    ce.play(
        env,
        card.id,
        ce.block("with_fixes", [ce.finding()]),
        ce.block("ready"),
        kind="code",
    )

    assert _queued(env, card.id) == [("signal", f"council:{council_id}:round:1:writer")]


@pytest.mark.parametrize(
    "outcome, waited",
    [("wait_user", "decision"), ("review", ""), ("wait_dependency", "council")],
)
def test_all_rejected_and_a_wait_user_finish_settles_the_code_council(
    env, outcome, waited
):
    enforce(env)
    card_id, council_id = _needs_writer(env)
    _turn(env, card_id)
    ce.reply(env, card_id, {"r1.f1": "rejected"}, REJECTED, agent="dev")
    assert ce.pr_decisions(env, card_id) == []

    out = _finish(
        env,
        card_id,
        outcome,
        **({"councils": [council_id]} if waited == "council" else {}),
    )

    found = councils.row(env.store, council_id)
    assert (found["state"], found["end_reason"], found["round"]) == (
        "settled",
        "rejected",
        1,
    )
    [decision] = ce.pr_decisions(
        env, card_id
    )  # the rejections are the owner's to weigh
    ref = {"decision": decision["id"], "council": council_id}.get(waited)
    assert [(w["kind"], w["ref"]) for w in out["waits"]] == (
        [(waited, ref)] if waited else []
    )


def test_a_later_answer_settles_as_a_rejection_does(env):
    enforce(env)
    card_id, council_id = _needs_writer(env)
    other = ce.coding(env, council=None)
    _turn(env, card_id)
    ce.reply(
        env,
        card_id,
        {"r1.f1": ("later", other.id)},
        "r1.f1: a card of its own",
        agent="dev",
    )

    _finish(env, card_id, "wait_user")

    assert councils.row(env.store, council_id)["state"] == "settled"


def test_a_fixed_answer_without_a_new_head_keeps_the_council_open(env):
    enforce(env)
    card_id, council_id = _needs_writer(env)
    _turn(env, card_id)
    ce.reply(env, card_id, {"r1.f1": "fixed"}, agent="dev")

    why = _refused(
        lambda: _finish(env, card_id, "wait_user")
    )  # nothing of the owner's to wait for yet
    assert f"council {council_id[:8]}" in why and "kd-forge publish" in why

    asked = question(
        env, env.store.card(card_id)
    )  # the developer may have asked the owner about the fix
    out = _finish(env, card_id, "wait_user")
    assert [(w["kind"], w["ref"]) for w in out["waits"]] == [("decision", asked)]
    assert councils.row(env.store, council_id)["state"] == "needs_writer"
    assert ce.pr_decisions(env, card_id) == []
    assert ce.publish(env, ce.pull(card_id, head=B))["council"] == {
        "id": council_id,
        "round": 2,
        "rounds": 3,
        "state": "running",
    }  # the fix is the next round


def test_a_minor_finding_answered_fixed_without_a_new_head_is_not_handed_in(env):
    # the writer answers minor findings too — «fixed» on one, with the head the council read, is neither
    # handed in nor settled
    enforce(env)
    card = ce.coding(env)
    ce.publish(env, ce.pull(card.id))
    council_id = ce.play(
        env,
        card.id,
        ce.block("with_fixes", [ce.finding(), ce.finding("minor", 2)]),
        ce.block("ready"),
        kind="code",
    )["id"]
    _turn(env, card.id)
    ce.reply(
        env, card.id, {"r1.f1": "rejected", "r1.f2": "fixed"}, REJECTED, agent="dev"
    )

    why = _refused(lambda: _finish(env, card.id, "review"))
    assert (
        "r1.f2 answered fixed, but the pull request is on the head the council read"
        in why
    )
    why = _refused(lambda: _finish(env, card.id, "wait_user"))
    assert f"council {council_id[:8]}" in why and "kd-forge publish" in why
    assert councils.row(env.store, council_id)["state"] == "needs_writer"

    ce.reply(
        env, card.id, {"r1.f2": "rejected"}, "r1.f2: a matter of taste", agent="dev"
    )
    _finish(env, card.id, "wait_user")  # answered again in the same turn: settled
    assert councils.row(env.store, council_id)["state"] == "settled"


def test_a_writer_with_an_open_question_does_not_settle(env):
    from kd.cabinet import decisions

    enforce(env)
    card_id, council_id = _needs_writer(env)
    command, session = _turn(env, card_id)
    ce.reply(env, card_id, {"r1.f1": "rejected"}, REJECTED, agent="dev")
    asked = question(env, env.store.card(card_id))

    out = _finish(env, card_id, "wait_user")
    end_turn(env, command, session)

    assert [(w["kind"], w["ref"]) for w in out["waits"]] == [("decision", asked)]
    assert councils.row(env.store, council_id)["state"] == "needs_writer"
    assert ce.pr_decisions(env, card_id) == []
    decisions.Decisions(env.store).reply(
        asked, user="owner", text="as you see fit", now=env.now
    )
    _turn(env, card_id)
    _finish(env, card_id, "wait_user")  # the owner answered: the rejection stands
    assert councils.row(env.store, council_id)["state"] == "settled"
    assert len(ce.pr_decisions(env, card_id)) == 1


def test_unanswered_findings_do_not_settle_on_a_wait_user_finish(env):
    enforce(env)
    card_id, council_id = _needs_writer(env)
    _turn(env, card_id)
    asked = question(
        env, env.store.card(card_id)
    )  # the developer asks the owner about the finding first

    out = _finish(env, card_id, "wait_user")

    assert [(w["kind"], w["ref"]) for w in out["waits"]] == [("decision", asked)]
    assert councils.row(env.store, council_id)["state"] == "needs_writer"


@pytest.mark.parametrize(
    "how", [{"outcome": "review"}, {"outcome": "wait_dependency", "councils": True}]
)
def test_finish_review_past_the_findings_is_refused(env, how):
    enforce(env)
    card_id, council_id = _needs_writer(env)
    _turn(env, card_id)
    more = {"councils": [council_id]} if how.get("councils") else {}

    why = _refused(lambda: _finish(env, card_id, how["outcome"], **more))
    assert f"{card_id} has unanswered council findings: r1.f1" in why

    ce.reply(env, card_id, {"r1.f1": "fixed"}, agent="dev")
    why = _refused(lambda: _finish(env, card_id, how["outcome"], **more))
    assert (
        "r1.f1 answered fixed, but the pull request is on the head the council read"
        in why
    )
    assert "kd-forge publish" in why
    assert councils.row(env.store, council_id)["state"] == "needs_writer"
    assert env.store.card(card_id).status == "In Progress"


def test_finish_review_past_the_findings_is_a_problem_in_observe(env):
    card_id, council_id = _needs_writer(env)
    _turn(env, card_id)

    _finish(env, card_id, "review")

    [problem] = [
        p
        for p in problems.open_for(env.store, card_id)
        if p["kind"] == "council_unanswered"
    ]
    assert (problem["agent"], problem["detail"]["findings"]) == ("dev", ["r1.f1"])
    assert (
        councils.row(env.store, council_id)["state"] == "needs_writer"
    )  # the findings stay the developer's


def test_wait_user_under_a_reading_council_names_the_council(env):
    enforce(env)
    card = ce.coding(env)
    council_id = ce.publish(env, ce.pull(card.id))["council"]["id"]
    _turn(env, card.id)

    why = _refused(lambda: _finish(env, card.id, "wait_user"))

    assert f"--outcome wait_dependency --council {council_id}" in why


def test_each_pull_of_a_card_is_gated_by_its_own_council(env):
    enforce(env)
    card_id, council_id = _needs_writer(env)
    ce.publish(
        env,
        ce.pull(
            card_id, number=8, head=C, url="https://forge.example.test/acme/web/pulls/8"
        ),
    )
    newer = ce.play(
        env,
        card_id,
        ce.block("with_fixes", [ce.finding()]),
        ce.block("ready"),
        kind="code",
    )
    assert newer["state"] == "needs_writer" and newer["id"] != council_id
    _turn(env, card_id)
    ce.reply(
        env, card_id, {"r1.f1": "rejected"}, REJECTED, agent="dev"
    )  # the newer council's finding

    why = _refused(
        lambda: _finish(env, card_id, "review")
    )  # the older one's is still owed

    assert "unanswered council findings: r1.f1" in why
    assert [
        councils.row(env.store, one)["state"] for one in (council_id, newer["id"])
    ] == ["needs_writer"] * 2
    _finish(
        env, card_id, "wait_user"
    )  # the answered council is settled, its decision the owner's
    assert [
        councils.row(env.store, one)["state"] for one in (council_id, newer["id"])
    ] == [
        "needs_writer",
        "settled",
    ]
    assert [d["payload"]["number"] for d in ce.pr_decisions(env, card_id)] == [8]


def test_a_silent_developer_is_stalled(env):
    card_id, council_id = _needs_writer(env)
    _later(env, minutes=61)

    assert outcomes.sweep(env.ctx)["councils"] == {"lost": 0, "stalled": 1}
    [problem] = [
        p
        for p in problems.open_for(env.store, card_id)
        if p["kind"] == "council_stalled"
    ]
    assert (problem["agent"], problem["detail"]) == (
        "dev",
        {"council": council_id, "round": 1},
    )


# --- what the whole branch's review found ---------------------------------------------------------------
def test_a_head_past_the_last_round_says_limited_on_its_decision(env):
    card = ce.coding(
        env, {"code": {"critics": ["claude:opus"], "rounds": 1, "min_lines": 0}}
    )
    ce.publish(env, ce.pull(card.id))
    ce.play(env, card.id, ce.block("not_ready", [ce.finding("critical")]), kind="code")

    out = ce.publish(env, ce.pull(card.id, head=B))

    found = _council(env, card.id)
    assert (found["state"], found["basis"]["head"]) == (
        "limited",
        B,
    )  # the head its end handed to the owner
    assert [e["head"] for e in found["basis"]["rounds"]] == [A]  # what its round read
    said = councils.pull_review(env.store, card.id, out["decision"]["payload"])
    assert (said["state"], said["council"]) == ("limited", found["id"])
    again = ce.publish(env, ce.pull(card.id, head=B))  # the same head again
    assert again["decision"]["id"] == out["decision"]["id"]
    assert [d["payload"]["head"] for d in ce.pr_decisions(env, card.id)] == [B]
    assert len(councils.of_card(env.store, card.id, "code")) == 1


def test_head_missing_retries_do_not_spend_the_councils_day_limit(env, monkeypatch):
    card = ce.coding(env)
    ce.head(env, ce.pull(card.id))
    real = council.limits()
    monkeypatch.setattr(
        council, "limits", lambda path=None: {**real, "jobs_per_day": 2}
    )
    for run in _runs(env, card.id):
        ce.end(env, run["job"], state="failed", reason=MISSING)
    _later(env, seconds=120)

    outcomes.sweep(env.ctx)

    again = _runs(env, card.id)
    assert [r["state"] for r in again] == ["queued", "queued"]
    assert [jobs.row(env.store, r["job"])["state"] for r in again] == [
        "queued",
        "queued",
    ]


def test_the_same_head_published_again_holds_its_newest_passport(env):
    card = ce.coding(env)
    ce.publish(env, ce.pull(card.id))
    ce.publish(
        env, ce.pull(card.id, checks=["static"], effect="deploys the site")
    )  # the checks went green after the first publish

    ce.play(env, card.id, ce.block("ready"), ce.block("ready"), kind="code")

    [decision] = ce.pr_decisions(env, card.id)
    assert (decision["payload"]["checks"], decision["payload"]["effect"]) == (
        ["static"],
        "deploys the site",
    )
    assert len(_runs(env, card.id)) == 2  # the same round
