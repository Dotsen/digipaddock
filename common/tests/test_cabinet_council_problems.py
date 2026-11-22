"""what goes wrong in a council — a failed critic, a council nobody could read for, a writer who keeps
silent, a job the host lost — as the pipeline's problems, the owner's notice and the sweep that plays a round out."""

import council_env as ce
import pytest
from test_cabinet_outcomes import enforce

from kd.cabinet import council_rounds, i18n, jobs, outcomes, problems

LATER = "2026-10-14T11:01:00Z"  # 61 minutes after the env's clock


@pytest.fixture
def env(tmp_path):
    return ce.make(tmp_path)


def _raised(env, card_id, kind=None):
    """Every problem of the card ever raised, open or settled: ``(kind, state, key)``."""
    found = env.store._conn.execute(
        "SELECT kind, state, key FROM problems WHERE card=? ORDER BY id", (card_id,)
    ).fetchall()
    return [tuple(r) for r in found if kind is None or r["kind"] == kind]


def _open(env, card_id):
    return [p["kind"] for p in problems.open_for(env.store, card_id)]


def _notices(env, card_id):
    return [
        e["payload"] for e in env.store.card_events(card_id) if e["kind"] == "notice"
    ]


def _needs_writer(env):
    card = ce.draft(env)
    ce.handin(env, card.id)
    ce.play(env, card.id, ce.block("needs_changes", [ce.finding()]), ce.block())
    return card.id, ce.council_of(env, card.id)["id"]


def test_a_failed_run_is_one_problem(env):
    card = ce.draft(env)
    ce.handin(env, card.id)
    first, second = ce.runs_of(env, card.id)

    ce.end(env, first["job"], state="no-result", reason="no final block")

    council_id = ce.council_of(env, card.id)["id"]
    (problem,) = problems.open_for(env.store, card.id)
    assert (problem["kind"], problem["key"], problem["agent"]) == (
        "council_run_failed",
        f"council-run:{council_id}:1:1",
        "analyst",
    )
    assert problem["detail"]["reason"] == "no final block"
    ce.end(
        env, second["job"], ce.block()
    )  # the round ends clean: nothing is left to look at
    assert _open(env, card.id) == []
    assert len(_raised(env, card.id, "council_run_failed")) == 1


def test_a_failed_council_tells_the_owner_and_boss(env):
    enforce(env)
    card = ce.draft(env)
    ce.handin(env, card.id)

    ce.play(env, card.id, None, None)

    assert _open(env, card.id) == ["council_failed"]
    (notice,) = [
        n for n in _notices(env, card.id) if n["key"] == "notice.council.failed"
    ]
    assert (notice["level"], notice["source"], notice["params"]) == (
        "warn",
        "pipeline",
        {"round": 1},
    )
    texts = [c["text"] for c in ce.wakes(env, "boss")]
    assert any("no critic of the council could start" in t for t in texts)
    assert any(t.endswith("propose the start") for t in texts)  # the brief goes on


def test_the_failed_councils_notice_reads_in_both_languages():
    for lang in ("ru", "en"):
        text = i18n.t(lang, "notice.council.failed", round=2)
        assert text != "notice.council.failed" and "2" in text and "{" not in text


def test_a_started_card_settles_the_failed_council(env):
    card = ce.draft(env)
    ce.handin(env, card.id)
    ce.play(env, card.id, None, None)

    env.store.edit_card(card.id, None, {"status": "To Do"}, actor="user:owner")

    assert _open(env, card.id) == []


def test_a_new_council_settles_the_failed_one(env):
    card = ce.draft(env)
    ce.handin(env, card.id)
    ce.play(env, card.id, None, None)

    ce.handin(env, card.id, description="Goal: a report, with a timeout.")

    assert ce.council_of(env, card.id)["state"] == "running"
    assert _open(env, card.id) == []


def test_a_silent_writer_is_a_stalled_problem_once(env):
    card_id, council_id = _needs_writer(env)

    assert outcomes.sweep(env.ctx)["councils"] == {"lost": 0, "stalled": 0}
    env.now = LATER
    assert outcomes.sweep(env.ctx)["councils"]["stalled"] == 1
    assert outcomes.sweep(env.ctx)["councils"]["stalled"] == 0

    (problem,) = problems.open_for(env.store, card_id)
    assert (problem["kind"], problem["key"], problem["agent"]) == (
        "council_stalled",
        f"council-stalled:{council_id}:1",
        "analyst",
    )


def test_an_answer_settles_the_stalled_problem(env):
    card_id, _ = _needs_writer(env)
    env.now = LATER
    outcomes.sweep(env.ctx)
    ce.started(env, card_id)

    ce.reply(env, card_id, {"r1.f1": "rejected"}, "r1.f1: by design")

    assert _open(env, card_id) == []
    assert (
        outcomes.sweep(env.ctx)["councils"]["stalled"] == 0
    )  # the clock started again


def test_the_next_round_settles_the_stalled_problem(env):
    card_id, _ = _needs_writer(env)
    env.now = LATER
    outcomes.sweep(env.ctx)

    ce.handin(env, card_id, description="Goal: a report, with a timeout.")

    assert ce.council_of(env, card_id)["round"] == 2
    assert "council_stalled" not in _open(env, card_id)


def test_the_councils_end_settles_what_its_rounds_left(env):
    card_id, council_id = _needs_writer(env)
    ce.started(env, card_id)
    ce.edit(env, card_id, description="Goal: a report, with a timeout.")
    ce.handoff(env, card_id)  # observe: handed in past the findings
    assert _open(env, card_id) == ["council_unanswered"]

    ce.play(env, card_id, ce.block(), ce.block())

    assert ce.council_of(env, card_id)["state"] == "clean"
    assert _open(env, card_id) == []


def test_sweep_plays_out_a_round_the_host_lost(env):
    card = ce.draft(env)
    ce.handin(env, card.id)
    gone, missed = ce.runs_of(env, card.id)
    env.store._conn.execute("DELETE FROM jobs WHERE id=?", (gone["job"],))
    env.store._conn.execute(  # ended at the host; its report never reached the council
        "UPDATE jobs SET state='failed', reason='timeout' WHERE id=?", (missed["job"],)
    )

    assert outcomes.sweep(env.ctx)["councils"]["lost"] == 2

    first, second = ce.runs_of(env, card.id)
    assert (first["state"], first["fail_reason"]) == ("failed", jobs.HOST_LOST)
    assert (second["state"], second["fail_reason"]) == ("failed", "timeout")
    found = ce.council_of(env, card.id)
    assert (found["state"], found["end_reason"]) == ("failed", "no_critic")
    assert len(ce.reviews(env, card.id)) == 2
    assert outcomes.sweep(env.ctx)["councils"]["lost"] == 0


def test_a_writer_without_the_card_is_not_stalled(env):
    card_id, _ = _needs_writer(env)
    env.store.edit_card(card_id, None, {"assignee": "@owner"}, actor="user:owner")
    env.now = LATER

    assert outcomes.sweep(env.ctx)["councils"]["stalled"] == 0


def test_sweep_leaves_a_round_that_reads(env):
    card = ce.draft(env)
    ce.handin(env, card.id)
    ce.start(env, ce.runs_of(env, card.id)[0]["job"])

    assert outcomes.sweep(env.ctx)["councils"] == {"lost": 0, "stalled": 0}
    assert [r["state"] for r in ce.runs_of(env, card.id)] == ["running", "queued"]


def test_a_broken_council_does_not_stop_the_sweep(env, monkeypatch, capsys):
    card_id, _ = _needs_writer(env)
    env.now = LATER

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(council_rounds.problems, "raise_problem", boom)

    assert outcomes.sweep(env.ctx)["councils"] == {"lost": 0, "stalled": 0}
    assert "council" in capsys.readouterr().err


# --- the card under its council, out of Inbox ------------------------------------------------------------------
def test_a_card_in_work_keeps_its_place_when_its_writer_is_woken(env):
    card = ce.draft(
        env, status="In Progress"
    )  # an aggregate's next brief version
    ce.handin(env, card.id)
    env.store.edit_card(
        card.id,
        None,
        {"comment": {"text": "a note", "author": "user:owner"}},
        actor="user:owner",
    )
    assert (
        ce.council_of(env, card.id)["state"] == "running"
    )  # a card in work is not «moved»

    ce.play(env, card.id, ce.block("needs_changes", [ce.finding()]), ce.block())

    record = env.store.card(card.id)
    assert (record.status, record.assignee) == ("In Progress", "@analyst")
    assert "spec:task-brief" in record.labels and "brief-ready" not in record.labels
    assert len(ce.wakes(env, "analyst")) == 1


def _stage(key, title):
    return {
        "key": key,
        "role": "dev",
        "spec": "implementer",
        "title": title,
        "description": "Do it.",
        "acceptance": ["It is done"],
        "depends": [],
    }


def _replanned(env):
    """An aggregate in work on its lead; the analyst re-plans its stages without holding it and the
    council's round over the new plan ends with a finding."""
    card = ce.draft(
        env,
        status="In Progress",
        assignee="@dev",
        labels=["spec:implementer", "size:L"],
        stages=[_stage("s1", "The parser")],
    )
    plan = [_stage("s1", "The parser"), _stage("s2", "The report")]
    changes = {"stages": plan, "labels_add": ["brief-ready"]}
    env.store.edit_card(card.id, None, changes, actor="agent:analyst")
    ce.play(env, card.id, ce.block("needs_changes", [ce.finding()]), ce.block())
    assert ce.council_of(env, card.id)["state"] == "needs_writer"
    return card.id


def test_a_replanned_aggregate_stays_with_its_holder(env):
    card_id = _replanned(env)

    record = env.store.card(card_id)
    assert (record.status, record.assignee) == ("In Progress", "@dev")
    assert {"spec:implementer", "size:L"} <= set(record.labels)
    assert "spec:task-brief" not in record.labels
    assert (
        "brief-ready" not in record.labels
    )  # the hand-in is undone: the plan is not the owner's to approve yet
    assert len(ce.wakes(env, "analyst")) == 1


def test_the_writer_of_a_replanned_aggregate_answers_without_holding_it(env):
    card_id = _replanned(env)
    ce.started(env, card_id)

    out = ce.reply(env, card_id, {"r1.f1": "rejected"}, note="r1.f1: the stage has it")

    assert out["unanswered"] == []


def test_the_silent_writer_of_a_replanned_aggregate_is_stalled(env):
    card_id = _replanned(env)
    env.now = LATER

    assert outcomes.sweep(env.ctx)["councils"]["stalled"] == 1
    (problem,) = problems.open_for(env.store, card_id)
    assert (problem["kind"], problem["agent"]) == ("council_stalled", "analyst")


def test_a_writer_who_asked_the_owner_is_not_silent(env):
    """The owner's move, not the writer's: the silence is counted from the owner's answer."""
    from kd.cabinet import decisions

    card_id, _ = _needs_writer(env)
    asked = decisions.Decisions(env.store).open(
        "question",
        card=card_id,
        payload={"text": "Which timeout?"},
        requested_by="agent:analyst",
        now=env.now,
    )
    env.now = LATER
    assert outcomes.sweep(env.ctx)["councils"]["stalled"] == 0

    decisions.Decisions(env.store).reply(
        asked["id"], user="owner", text="30 s", now=env.now
    )
    assert outcomes.sweep(env.ctx)["councils"]["stalled"] == 0  # just answered

    env.now = "2026-10-14T12:02:00Z"  # 61 minutes after the answer
    assert outcomes.sweep(env.ctx)["councils"]["stalled"] == 1
