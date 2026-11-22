"""a council's round over a brief — the critics' jobs, their reviews, the round's end, the writer woken."""

import json

import council_env as ce
import pytest

from kd import board, worker_inputs, worker_slots
from kd.cabinet import council_inputs, council_rounds, councils, jobs


@pytest.fixture
def env(tmp_path):
    return ce.make(tmp_path)


def _events(env, card_id, prefix="council."):
    return [
        (e["kind"], e["payload"])
        for e in env.store.card_events(card_id)
        if e["kind"].startswith(prefix)
    ]


def _brief_wakes(env, card_id):
    text = board.BRIEF_WAKE.format(id=card_id)
    return [c for c in ce.wakes(env, "boss") if c["text"] == text]


def test_a_handin_starts_round_one(env):
    card = ce.draft(env)

    ce.handin(env, card.id)

    found = ce.council_of(env, card.id)
    assert (found["kind"], found["state"], found["round"], found["rounds_max"]) == (
        "brief",
        "running",
        1,
        3,
    )
    assert found["critics"] == ["claude:opus", "or-gpt5"]
    assert found["basis"]["version"] == 1 and len(found["basis"]["scope_sha"]) == 64
    assert [r["state"] for r in found["basis"]["rounds"]] == ["running"]
    runs = ce.runs_of(env, card.id)
    assert [(r["round"], r["n"], r["model"], r["state"]) for r in runs] == [
        (1, 1, "claude:opus", "queued"),
        (1, 2, "or-gpt5", "queued"),
    ]
    for run in runs:
        job = jobs.row(env.store, run["job"])
        definition = job["data"]["definition"]
        assert (job["agent"], job["spec"], job["caller"], job["requester"]) == (
            "analyst",
            "brief-critic",
            "cabinet",
            "",
        )
        assert definition["model"] == run["model"] and definition["type"] == "analyst"
        assert definition["limits"]["write"] == "none" and not definition["writer"]
        assert definition["timeout"] == 3600
        assert f"council {found['id']}" in definition["brief"]
        assert "round 1 of 3" in definition["brief"]
    assert _events(env, card.id)[0] == (
        "council.started",
        {"council": found["id"], "kind": "brief", "round": 1},
    )


def test_no_council_without_critics_or_for_a_persons_version(env):
    bare = ce.draft(env, council=None)
    ce.handin(env, bare.id)
    mine = ce.draft(env)
    env.store.edit_card(
        mine.id,
        None,
        {"description": "By hand.", "labels_add": ["brief-ready"]},
        actor="user:owner",
    )

    assert ce.council_of(env, bare.id) is None
    assert ce.council_of(env, mine.id) is None


def test_a_clean_round_ends_the_council(env):
    card = ce.draft(env)
    ce.handin(env, card.id)
    for command in _brief_wakes(
        env, card.id
    ):  # the dispatcher drops it while the council reads
        env.store.command_state(command["id"], "cancelled", reason="in_council")

    found = ce.play(env, card.id, ce.block(), ce.block(findings=[ce.finding("minor")]))

    assert (found["state"], found["end_reason"]) == ("clean", "")
    assert found["ended"] and found["basis"]["rounds"][0]["state"] == "ended"
    posted = ce.reviews(env, card.id)
    assert [m["author"] for m in posted] == ["system:council"] * 2
    assert [m["payload"]["model"] for m in posted] == ["claude:opus", "or-gpt5"]
    assert posted[1]["payload"]["findings"][0]["id"] == "r1.f1"
    assert posted[0]["body"].startswith("brief council round 1 · claude:opus · ready")
    record = env.store.card(card.id)
    assert record.assignee == "@dev" and "brief-ready" in record.labels
    assert len(_brief_wakes(env, card.id)) == 1
    assert ce.wakes(env, "analyst") == []
    assert _events(env, card.id)[-1] == (
        "council.ended",
        {"council": found["id"], "state": "clean", "reason": ""},
    )


def test_a_finding_wakes_the_writer(env):
    card = ce.draft(env)
    ce.handin(env, card.id)

    found = ce.play(
        env,
        card.id,
        ce.block("needs_changes", [ce.finding("major"), ce.finding("minor", 2)]),
        ce.block(),
    )

    assert (found["state"], found["round"], found["ended"]) == ("needs_writer", 1, "")
    record = env.store.card(card.id)
    assert (record.assignee, record.status) == ("@analyst", "Inbox")
    assert "spec:task-brief" in record.labels and "brief-ready" not in record.labels
    assert "spec:implementer" not in record.labels
    (wake,) = ce.wakes(env, "analyst")
    assert wake["text"] == (
        f"[council] {card.id}: round 1 of the brief council ended — read kd-work council {card.id}"
        " and answer each finding"
    )
    first = ce.runs_of(env, card.id)[0]
    assert [(f["id"], f["severity"]) for f in first["findings"]] == [
        ("r1.f1", "major"),
        ("r1.f2", "minor"),
    ]
    assert first["verdict"] == "needs_changes" and first["usage"]["requests"] == 4


def test_findings_are_numbered_across_the_round(env):
    card = ce.draft(env)
    ce.handin(env, card.id)

    ce.play(
        env,
        card.id,
        ce.block("needs_changes", [ce.finding(), ce.finding()]),
        ce.block("needs_changes", [ce.finding("critical")]),
    )

    ids = [f["id"] for run in ce.runs_of(env, card.id) for f in run["findings"]]
    assert ids == ["r1.f1", "r1.f2", "r1.f3"]


def test_a_failed_critic_does_not_block_the_round(env):
    card = ce.draft(env)
    ce.handin(env, card.id)

    found = ce.play(env, card.id, None, ce.block())

    assert found["state"] == "clean"
    assert ce.councils.facts(env.store, card.id, "brief")["done"] == 2
    failed, done = ce.runs_of(env, card.id)
    assert (failed["state"], done["state"]) == ("failed", "done")
    assert failed["fail_reason"] == "no final block"
    assert ce.reviews(env, card.id)[0]["payload"]["state"] == "failed"


def test_no_critic_at_all_fails_the_council(env):
    card = ce.draft(env)
    ce.handin(env, card.id)

    found = ce.play(env, card.id, None, None)

    assert (found["state"], found["end_reason"]) == ("failed", "no_critic")
    record = env.store.card(card.id)
    assert (
        record.assignee == "@dev" and "brief-ready" in record.labels
    )  # the brief goes on without the council


@pytest.mark.parametrize(
    "bad",
    [
        {"status": "done", "verdict": "ready", "findings": ["the brief is vague"]},
        {"status": "done", "verdict": "fine", "findings": []},
        {"status": "done", "verdict": "ready"},
        {
            "status": "done",
            "verdict": "needs_changes",
            "findings": [{"severity": "major", "location": "x"}],
        },
    ],
)
def test_a_bad_block_fails_the_run(env, bad):
    card = ce.draft(env)
    ce.handin(env, card.id)

    found = ce.play(env, card.id, bad, ce.block())  # the host's report does not raise

    assert found["state"] == "clean"
    run = ce.runs_of(env, card.id)[0]
    assert run["state"] == "failed" and run["fail_reason"].startswith("bad_result: ")
    assert run["findings"] == []


def test_a_disabled_critic_fails_its_run_at_once(env):
    card = ce.draft(env)
    env.store.update_llm_model("or-gpt5", {"enabled": False}, actor="user:owner")

    ce.handin(env, card.id)

    opus, gpt = ce.runs_of(env, card.id)
    assert (opus["state"], gpt["state"]) == ("queued", "failed")
    assert (gpt["job"], gpt["fail_reason"]) == ("", "model_disabled")
    assert ce.council_of(env, card.id)["state"] == "running"
    assert ce.play(env, card.id, ce.block())["state"] == "clean"


def test_critics_that_cannot_start_fail_the_council_at_the_handin(env):
    card = ce.draft(env, council={"brief": {"critics": ["or-gpt5"], "rounds": 2}})
    (env.llm_dir / "openrouter.env").unlink()

    ce.handin(env, card.id)

    found = ce.council_of(env, card.id)
    assert (found["state"], found["end_reason"]) == ("failed", "no_critic")
    assert ce.runs_of(env, card.id)[0]["fail_reason"] == "no_key"


def test_an_agent_without_a_worker_pool_fails_its_runs(env, monkeypatch, tmp_path):
    card = ce.draft(env)
    pools = tmp_path / "worker-slots.json"
    pools.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "requester": "boss",
                "model": "sonnet",
                "agents": {"dev": ["worker1"]},
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

    ce.handin(env, card.id)

    assert [r["fail_reason"] for r in ce.runs_of(env, card.id)] == ["no_slots"] * 2
    assert ce.council_of(env, card.id)["state"] == "failed"


def _to_round_two(env, card_id):
    ce.play(env, card_id, ce.block("needs_changes", [ce.finding()]), ce.block())
    return ce.handin(env, card_id, description="Goal: a report, with a timeout.")


def test_a_new_version_at_needs_writer_is_the_next_round(env):
    card = ce.draft(env)
    ce.handin(env, card.id)

    _to_round_two(env, card.id)

    found = ce.council_of(env, card.id)
    assert (found["state"], found["round"]) == ("running", 2)
    assert found["basis"]["version"] == 2
    assert [
        (r["round"], r["version"], r["state"]) for r in found["basis"]["rounds"]
    ] == [
        (1, 1, "ended"),
        (2, 2, "running"),
    ]
    second = ce.runs_of(env, card.id, 2)
    assert [r["state"] for r in second] == ["queued", "queued"]
    brief = jobs.row(env.store, second[0]["job"])["data"]["definition"]["brief"]
    assert "round 2 of 3" in brief and "r1.f1 major unanswered" in brief
    assert ce.play(env, card.id, ce.block(), ce.block())["state"] == "clean"
    assert [f["id"] for f in ce.runs_of(env, card.id, 1)[0]["findings"]] == ["r1.f1"]


def test_the_last_round_is_limited(env):
    card = ce.draft(env, council={"brief": {"critics": ["claude:opus"], "rounds": 1}})
    ce.handin(env, card.id)
    ce.play(env, card.id, ce.block("needs_changes", [ce.finding()]))

    ce.handin(env, card.id, description="Goal: a report, with a timeout.")

    found = ce.council_of(env, card.id)
    assert (found["state"], found["end_reason"], found["round"]) == (
        "limited",
        "rounds",
        1,
    )
    assert ce.runs_of(env, card.id, 2) == []
    assert len(_brief_wakes(env, card.id)) == 1


def test_a_new_version_while_running_supersedes_the_round(env):
    card = ce.draft(env)
    ce.handin(env, card.id)
    first = ce.runs_of(env, card.id)
    ce.start(env, first[0]["job"])
    env.store.edit_card(  # the owner sent it back; the analyst hands in another text while the critics read
        card.id,
        None,
        {"assignee": "@analyst", "labels_remove": ["brief-ready"]},
        actor="user:owner",
    )

    ce.handin(env, card.id, description="Goal: another report.")

    found = ce.council_of(env, card.id)
    assert (found["state"], found["round"]) == ("running", 2)
    assert [r["state"] for r in found["basis"]["rounds"]] == ["superseded", "running"]
    assert [r["state"] for r in ce.runs_of(env, card.id, 1)] == [
        "cancelled",
        "cancelled",
    ]
    states = [jobs.row(env.store, r["job"])["state"] for r in first]
    assert states == ["cancelling", "cancelled"]
    assert len(ce.runs_of(env, card.id, 2)) == 2


def test_a_late_report_changes_nothing(env):
    card = ce.draft(env)
    ce.handin(env, card.id)
    late = ce.runs_of(env, card.id)[0]["job"]
    ce.start(env, late)
    env.store.edit_card(
        card.id,
        None,
        {"assignee": "@analyst", "labels_remove": ["brief-ready"]},
        actor="user:owner",
    )
    ce.handin(env, card.id, description="Goal: another report.")

    ce.end(env, late, ce.block("needs_changes", [ce.finding("critical")]))

    found = ce.council_of(env, card.id)
    assert (found["state"], found["round"]) == ("running", 2)
    assert ce.runs_of(env, card.id, 1)[0]["findings"] == []
    assert [r["state"] for r in ce.runs_of(env, card.id, 2)] == ["queued", "queued"]
    assert ce.reviews(env, card.id) == []
    assert (
        jobs.row(env.store, late)["state"] == "done"
    )  # the job's own record is the host's


def test_two_critics_on_one_model_read_twice(env):
    card = ce.draft(
        env, council={"brief": {"critics": ["claude:opus", "claude:opus"], "rounds": 2}}
    )

    ce.handin(env, card.id)

    runs = ce.runs_of(env, card.id)
    assert [r["model"] for r in runs] == ["claude:opus", "claude:opus"]
    assert len({r["job"] for r in runs}) == 2


def test_a_started_job_is_a_running_run(env):
    card = ce.draft(env)
    ce.handin(env, card.id)
    first, second = ce.runs_of(env, card.id)

    ce.start(env, first["job"])
    ce.end(env, second["job"], ce.block("needs_changes", [ce.finding()]))

    assert ce.runs_of(env, card.id)[0]["state"] == "running"
    facts = ce.councils.facts(env.store, card.id, "brief")
    assert {k: facts[k] for k in ("state", "round", "rounds", "done", "total")} == {
        "state": "running",
        "round": 1,
        "rounds": 3,
        "done": 1,
        "total": 2,
    }
    assert (facts["open_findings"], facts["version"]) == (1, 1)


def test_a_council_error_never_breaks_the_handoff(env, monkeypatch, capsys):
    card = ce.draft(env)

    def boom(*args, **kwargs):
        raise RuntimeError("the council is broken")

    monkeypatch.setattr(council_rounds, "_start_round", boom)

    record = ce.handin(env, card.id)

    assert record.assignee == "@dev" and "brief-ready" in record.labels
    assert ce.council_of(env, card.id) is None
    assert "the council is broken" in capsys.readouterr().err


def test_cancel_takes_the_jobs_back(env):
    card = ce.draft(env)
    ce.handin(env, card.id)
    found = ce.council_of(env, card.id)

    council_rounds.cancel(env.ctx, found, "removed")

    after = ce.council_of(env, card.id)
    assert (after["state"], after["end_reason"]) == ("cancelled", "removed")
    assert [r["state"] for r in ce.runs_of(env, card.id)] == ["cancelled", "cancelled"]
    assert all(
        jobs.row(env.store, r["job"])["state"] == "cancelled"
        for r in ce.runs_of(env, card.id)
    )


# --- what a critic reads: a worker has no board, so the card travels with its job -----------------------------
def _job_of(env, run):
    return jobs.row(env.store, run["job"])


def _say(env, card_id, body, author="user:owner"):
    return env.store.add_message(card_id, author=author, kind="text", body=body)


def test_a_critic_is_given_the_brief_the_thread_and_the_named_cards(env):
    source = env.card(
        title="The numbers' source", description="Where the numbers come from."
    )
    card = ce.draft(env)
    _say(env, card.id, "I want the report every morning.")

    ce.handin(
        env,
        card.id,
        description=f"Goal: a report built on {source.id}.",
        ac=("There is a report", "It names its source"),
    )

    found = ce.council_of(env, card.id)
    for run in ce.runs_of(env, card.id):
        job = _job_of(env, run)
        files = job["data"]["inputs"]
        assert sorted(files) == [
            "brief.md",
            "cards.md",
            "thread.md",
        ]  # no record before the second round
        assert worker_inputs.check(files) == files
        brief = files["brief.md"]
        assert brief.startswith(f"# {card.id} · Inbox · @dev · ")
        assert f"Goal: a report built on {source.id}." in brief
        assert "- [ ] #1 There is a report\n- [ ] #2 It names its source" in brief
        assert (
            "I want the report every morning." in files["thread.md"]
            and "@owner" in files["thread.md"]
        )
        assert (
            f"## {source.id} · " in files["cards.md"]
            and "Where the numbers come from." in files["cards.md"]
        )
        task = job["data"]["definition"]["brief"]
        assert f"council {found['id']}" in task and "inputs/brief.md" in task
        assert (
            "inputs/thread.md" in task
            and "inputs/cards.md" in task
            and "council.md" not in task
        )
        assert "kd-task" not in task and "kd-work" not in task  # a worker has neither
        for text in files.values():
            assert council_inputs.DATA in text


def test_a_brief_alone_is_given_alone(env):
    card = ce.draft(env)

    ce.handin(env, card.id)

    files = _job_of(env, ce.runs_of(env, card.id)[0])["data"]["inputs"]
    assert sorted(files) == ["brief.md"]


def test_the_brief_carries_its_operations_and_stages(env):
    card = ce.draft(env)
    stages = [
        {
            "key": "s1",
            "role": "dev",
            "spec": "implementer",
            "title": "The parser",
            "description": "Parse it.",
            "acceptance": ["It parses"],
            "depends": [],
        },
    ]

    ce.handin(env, card.id, stages=stages)

    brief = _job_of(env, ce.runs_of(env, card.id)[0])["data"]["inputs"]["brief.md"]
    assert (
        "Этапы:\n- s1 · dev · implementer · The parser" in brief
        and "  - It parses" in brief
    )


def test_a_thorough_critics_review_in_russian_is_kept(env):
    """Thirty findings in Russian are some 40 KiB of JSON: the review of a critic that did its work."""
    card = ce.draft(env)
    ce.handin(env, card.id)

    found = ce.play(env, card.id, ce.thorough(), ce.block())

    run = ce.runs_of(env, card.id, 1)[0]
    assert (run["state"], len(run["findings"])) == ("done", 30)
    assert found["state"] == "needs_writer"


def test_the_second_round_is_given_the_record_of_the_first(env):
    card = ce.draft(env)
    ce.handin(env, card.id)
    ce.play(env, card.id, ce.block("needs_changes", [ce.finding()]), ce.block())
    ce.started(env, card.id)
    ce.reply(
        env,
        card.id,
        {"r1.f1": "rejected"},
        note="r1.f1: the pool already has a timeout",
    )

    ce.handin(env, card.id, description="Goal: a report, with a timeout.")

    job = _job_of(env, ce.runs_of(env, card.id, 2)[0])
    record = job["data"]["inputs"]["council.md"]
    assert "\nround 1 · brief v1" in record and "\nround 2 ·" not in record
    assert (
        "r1.f1 major" in record
        and "answer: rejected — the pool already has a timeout" in record
    )
    assert (
        council_inputs.DATA in record and "you owe" not in record
    )  # the writer's notice is not the critic's
    task = job["data"]["definition"]["brief"]
    assert (
        "inputs/council.md" in task
        and "r1.f1 major rejected" in task
        and "kd-work" not in task
    )
    assert "Goal: a report, with a timeout." in job["data"]["inputs"]["brief.md"]


# --- the record of the rounds before — the latest first and as full as fits, an older one gives way ----
LONG = "the brief names no timeout, and " + "the pool waits " * 40
MINOR_FOLDED = " · its minor findings are one line each: the rest did not fit"
ALL_FOLDED = " · each finding is one line, the questions are not here: the rest did not fit"


def _third_round(env):
    """A council in its third round: the first left a rejected major finding and a minor one, the second a major
    one the writer fixed. The council's row."""
    card = ce.draft(env)
    ce.handin(env, card.id)
    ce.play(
        env,
        card.id,
        ce.block(
            "needs_changes",
            [
                dict(ce.finding(), evidence=LONG),
                dict(ce.finding("minor", 2), evidence=LONG),
            ],
        ),
        ce.block(),
    )
    ce.started(env, card.id)
    ce.reply(
        env,
        card.id,
        {"r1.f1": "rejected"},
        note="r1.f1: the pool already has a timeout",
    )
    ce.handin(env, card.id, description="Goal: a report, with a timeout.")
    ce.play(
        env,
        card.id,
        ce.block("needs_changes", [dict(ce.finding("major", 3), evidence=LONG)]),
        ce.block(),
    )
    env.now = "2026-10-14T11:00:00Z"  # the writer's next turn is another command
    ce.started(env, card.id)
    ce.reply(env, card.id, {"r2.f1": "fixed"})
    ce.handin(env, card.id, description="Goal: a report, with a timeout of 30 s.")
    found = ce.council_of(env, card.id)
    assert (found["round"], found["state"]) == (3, "running")
    return found


def _stubbed(monkeypatch, rounds, findings=4):
    """A council past ``rounds`` rounds without a store: each had one critic's run of ``findings`` findings, the
    first major and the rest minor, none answered. The council's row."""

    def run(round_no):
        return {
            "round": round_no,
            "n": 1,
            "model": "claude:opus",
            "state": "done",
            "verdict": "needs_changes",
            "fail_reason": "",
            "questions": [],
            "findings": [
                dict(
                    ce.finding("major" if n == 1 else "minor", n),
                    id=f"r{round_no}.f{n}",
                    evidence=LONG,
                )
                for n in range(1, findings + 1)
            ],
        }

    runs = [run(n) for n in range(1, rounds + 1)]
    monkeypatch.setattr(
        councils,
        "runs",
        lambda db, council_id, round_no=None: [
            r for r in runs if round_no in (None, r["round"])
        ],
    )
    return {
        "id": "c" * 32,
        "card": "KD-1",
        "kind": "brief",
        "round": rounds + 1,
        "basis": {
            "rounds": [
                {"round": n, "version": n, "state": "ended"}
                for n in range(1, rounds + 1)
            ]
        },
    }


def _record(db, monkeypatch, found, budget, cut=False):
    monkeypatch.setitem(council_inputs.BUDGET, council_inputs.COUNCIL, budget)
    record = council_inputs.record_text(db, found)
    assert worker_inputs.size(record) <= budget
    # a round gives way whole: the record is cut only when its latest round alone is over its part
    assert (council_inputs.CUT in record) == cut
    return record


def _block(record, n):
    """The text of round ``n`` in a record, from its line to the next round's."""
    rest = record[record.index(f"\nround {n} ·") :]
    return rest[: rest.index("\nround ", 1)] if "\nround " in rest[1:] else rest


def test_the_record_gives_the_latest_round_first(env, monkeypatch):
    found = _third_round(env)

    record = _record(env.store, monkeypatch, found, 48 * 1024)

    assert record.index("\nround 2 · brief v2") < record.index("\nround 1 · brief v1")
    assert "the latest first" in record.splitlines()[0]
    assert record.count("  evidence: ") == 3  # with room, every round is whole
    assert "answer: fixed" in record
    assert "answer: rejected — the pool already has a timeout" in record


def test_an_older_round_gives_way_step_by_step_and_the_latest_stays_whole(
    env, monkeypatch
):
    found = _third_round(env)
    whole = _record(env.store, monkeypatch, found, 48 * 1024)
    latest = _block(whole, 2)
    forms = {}  # the oldest round's line → its text, in the order the shrinking part brings them
    for budget in range(worker_inputs.size(whole), 0, -20):
        monkeypatch.setitem(council_inputs.BUDGET, council_inputs.COUNCIL, budget)
        record = council_inputs.record_text(env.store, found)
        assert worker_inputs.size(record) <= budget
        if _block(record, 2) != latest:
            break  # below this the latest round itself gives way
        assert council_inputs.CUT not in record
        oldest = _block(record, 1)
        forms.setdefault(oldest.splitlines()[1], oldest)

    head = "round 1 · brief v1 · ended"
    lines = list(forms)
    assert lines[:3] == [head, head + MINOR_FOLDED, head + ALL_FOLDED]
    assert len(lines) == 4 and lines[3].startswith(head + " · not listed, it did not fit: ")

    minor_folded, all_folded, counted = (forms[line] for line in lines[1:])
    # 1. the minor finding is a line, the major one whole
    assert "  evidence: " in minor_folded and "\nr1.f1 major AC 1\n" in minor_folded
    (minor,) = [ln for ln in minor_folded.splitlines() if ln.startswith("r1.f2 ")]
    assert minor.startswith("r1.f2 minor AC 2 · a request hangs and the owner waits forever")
    assert minor.endswith("· answer: none")
    # 2. every finding is a line: what it is about and what the writer answered, with the reason
    assert "  evidence: " not in all_folded
    (major,) = [ln for ln in all_folded.splitlines() if ln.startswith("r1.f1 ")]
    assert major.startswith("r1.f1 major AC 1 · a request hangs")
    assert major.endswith("· answer: rejected — the pool already has a timeout")
    assert "#1 claude:opus done needs_changes · critical 0 major 1 minor 1" in all_folded
    # 3. only counted
    assert "r1.f1" not in counted
    assert "findings 2 (critical 0, major 1, minor 1)" in counted
    assert "fixed 0, rejected 1, later 0, without an answer 1" in counted


def test_no_older_round_is_only_counted_while_another_is_given_in_full(monkeypatch):
    # three rounds before the fourth: a rejected finding a critic never sees is raised again, so each older round
    # keeps a line per finding before any of them grows richer
    found = _stubbed(monkeypatch, rounds=3)
    whole = _record(None, monkeypatch, found, 48 * 1024)
    assert whole.count("  evidence: ") == 12
    latest, seen = _block(whole, 3), set()
    for budget in range(worker_inputs.size(whole), 0, -40):
        monkeypatch.setitem(council_inputs.BUDGET, council_inputs.COUNCIL, budget)
        record = council_inputs.record_text(None, found)
        if _block(record, 3) != latest:
            break
        counted = [n for n in (1, 2) if "not listed" in _block(record, n)]
        full = [n for n in (1, 2) if "  evidence: " in _block(record, n)]
        assert not (counted and full), (budget, counted, full)
        assert counted in ([], [1], [1, 2])  # the older of the two gives way first
        seen.add((tuple(counted), tuple(full)))
    assert ((), ()) in seen and ((1, 2), ()) in seen  # the scan went through both ends


def test_a_latest_round_over_the_records_part_keeps_its_first_findings_whole_lines(
    monkeypatch,
):
    # one earlier round too large even a line per finding: its beginning, cut between findings — never a count
    # alone, which would hide every answer the critic is to check
    found = _stubbed(monkeypatch, rounds=2, findings=12)
    whole = _record(None, monkeypatch, found, 48 * 1024)
    head = worker_inputs.size(whole[: whole.index("\nround 2 ·")])

    record = _record(None, monkeypatch, found, head + 900, cut=True)

    assert "not listed" not in record and "\nround 1 ·" not in record
    lines = record.splitlines()
    assert lines[-1] == council_inputs.CUT
    assert lines[4] == "round 2 · brief v2 · ended" + ALL_FOLDED  # after the head, the data line and a blank
    listed = [line for line in lines if line.startswith("r2.f")]
    assert 2 <= len(listed) < 12 and listed[0].startswith("r2.f1 major AC 1 · ")
    assert all(line.endswith("· answer: none") for line in listed)  # none cut inside


def test_a_folded_finding_says_its_scenario_in_brief(env, monkeypatch):
    found = _third_round(env)
    run = councils.runs(env.store, found["id"], 1)[0]
    long = dict(run["findings"][0], scenario="a request hangs.\n" * 60)

    line = council_inputs.folded(long)

    assert "\n" not in line and len(line) < 400 and "…" in line
    assert line.startswith("r1.f1 major AC 1 · a request hangs. a request hangs.")
    assert line.endswith("· answer: rejected — the pool already has a timeout")


def test_a_long_thread_keeps_its_newest_messages(env, monkeypatch):
    monkeypatch.setitem(council_inputs.BUDGET, council_inputs.THREAD, 600)
    card = ce.draft(env)
    for n in range(1, 9):
        _say(env, card.id, f"remark {n} " + "x" * 100)

    ce.handin(env, card.id)

    thread = _job_of(env, ce.runs_of(env, card.id)[0])["data"]["inputs"]["thread.md"]
    assert "remark 8 " in thread and "remark 1 " not in thread
    assert "older messages are not here" in thread
    assert (
        worker_inputs.size(thread) <= 600 + 200
    )  # the head lines ride over the messages' part


def test_one_long_message_is_cut_not_dropped(env, monkeypatch):
    monkeypatch.setattr(council_inputs, "MESSAGE_CUT", 300)
    card = ce.draft(env)
    _say(env, card.id, "the decision: weekly. " + "y" * 2000)

    ce.handin(env, card.id)

    thread = _job_of(env, ce.runs_of(env, card.id)[0])["data"]["inputs"]["thread.md"]
    assert (
        "the decision: weekly." in thread
        and council_inputs.CUT in thread
        and "y" * 400 not in thread
    )


def test_a_brief_over_the_critics_part_fails_the_round_and_says_so(env, monkeypatch):
    monkeypatch.setitem(council_inputs.BUDGET, council_inputs.BRIEF, 200)
    card = ce.draft(env)

    ce.handin(env, card.id, description="Goal: a report. " + "z" * 400)

    found = ce.council_of(env, card.id)
    runs = ce.runs_of(env, card.id)
    assert (found["state"], found["end_reason"]) == ("failed", "no_critic")
    assert [r["state"] for r in runs] == ["failed", "failed"]
    assert all(r["fail_reason"].startswith("config: the brief is over") for r in runs)
    assert all(not r["job"] for r in runs)


def test_only_the_first_named_cards_are_given(env, monkeypatch):
    monkeypatch.setattr(council_inputs, "CARDS_MAX", 2)
    named = [
        env.card(title=f"Part {n}", description=f"Part {n} text.") for n in range(1, 4)
    ]
    card = ce.draft(env)

    ce.handin(
        env,
        card.id,
        description="Goal: "
        + ", ".join(c.id for c in named)
        + f" and {card.id} itself.",
    )

    cards = _job_of(env, ce.runs_of(env, card.id)[0])["data"]["inputs"]["cards.md"]
    assert (
        "Part 1 text." in cards
        and "Part 2 text." in cards
        and "Part 3 text." not in cards
    )
    assert f"## {card.id} · " not in cards  # the card itself is the brief
    assert f"not here: {named[2].id}" in cards
