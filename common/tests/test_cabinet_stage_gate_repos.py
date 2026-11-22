"""A chain plan across several repositories: a handed-in stage is read in every repository it changed, and the
coordinator decides once, on all of them."""

import sqlite3

import council_env as ce
import pytest
from chain_env import (
    READY,
    WEB,
    A,
    B,
    boss_signals,
    cabinet,
    call,
    close,
    done_work,
    handin,
    later,
    latest,
    passing,
    publish,
    return_to_work,
    reviewed,
    started,
)

from kd.cabinet import api, jobs, schema, snapshot, stage_gate, store

H1, H2, W1, W2 = "1" * 40, "2" * 40, "a" * 40, "b" * 40
MISSING = jobs.HEAD_MISSING + ": the copy has no refs/pull/3/head"
FIX = {
    "severity": "Important",
    "where": "src/app.py:12",
    "what": "the empty list is not handled",
}
FIXES = {"verdict": "with fixes", "findings": [FIX]}
BAD = {"verdict": "not ready", "findings": [{**FIX, "severity": "Critical"}]}


@pytest.fixture
def cab(tmp_path):
    return cabinet(tmp_path)


@pytest.fixture
def plan(cab):
    """A chain plan a → b: ``(parent, a, b)``."""
    parent, kids = started(cab, (A, B))
    return parent, kids["a"], kids["b"]


def both(cab, parent, stage, main=H1, web=W1):
    """The stage published into the plan's pull requests of two repositories and was handed in."""
    publish(cab, parent.id, web, **WEB)
    return handin(cab, stage, main)


def part(row, project):
    return next(p for p in row["parts"] if p["project"] == project)


def read(cab, row, main=READY, web=READY):
    """Both reviewers of the hand-in ended with these blocks."""
    ce.end(cab, part(row, "web")["job"], web)
    ce.end(cab, part(row, "digipaddock")["job"], main)
    return stage_gate.latest(cab.store, row["card"])


def passed(cab, stage):
    """The stage's hand-in passed and its card closed, as the coordinator's pass leaves them."""
    cab.store._conn.execute(
        "UPDATE stage_reviews SET gate='passed' WHERE id=?", (latest(cab, stage)["id"],)
    )
    close(cab, stage)


def refused(cab, op, stage, text, **fields):
    before = cab.store.card(stage.id).status
    with pytest.raises(api.ApiError) as exc:
        call(cab, op, stage, **fields)
    assert exc.value.kind == "refused" and text in str(exc.value), str(exc.value)
    assert cab.store.card(stage.id).status == before


def test_a_stage_in_two_repositories_gets_a_reviewer_on_each(cab, plan):
    parent, a, _ = plan
    row = both(cab, parent, a)
    assert (row["state"], row["gate"]) == ("reviewing", "")
    assert {(p["project"], p["head"], p["base"], p["state"]) for p in row["parts"]} == {
        ("digipaddock", H1, "", "reviewing"),
        ("web", W1, "", "reviewing"),
    }
    found = {
        p["project"]: jobs.row(cab.store, p["job"])["data"]["definition"]
        for p in row["parts"]
    }
    assert found["web"]["repo"] == "web"
    assert found["web"]["pull"] == {"number": 3, "head": W1, "target": "main"}
    assert found["digipaddock"]["pull"] == {"number": 7, "head": H1, "target": "main"}
    assert (
        boss_signals(cab) == []
    )  # the coordinator wakes on the verdict of all of them


def test_a_pull_request_the_stage_did_not_touch_is_not_read_again(cab, plan):
    parent, a, b = plan
    read(cab, both(cab, parent, a))
    passed(cab, a)
    second = handin(cab, b, H2)  # the second stage changed one repository only
    web, main = part(second, "web"), part(second, "digipaddock")
    assert (web["state"], web["job"], web["base"], web["head"]) == (
        "skipped",
        "",
        W1,
        W1,
    )
    assert (main["state"], main["base"], main["head"]) == ("reviewing", H1, H2)
    ce.end(cab, main["job"], READY)
    woken = [text for text in boss_signals(cab) if b.id in text]
    assert (latest(cab, b)["state"], len(woken)) == ("ready", 1)


@pytest.mark.parametrize(
    "main, web, state, verdict",
    [
        (READY, READY, "ready", "ready"),
        (READY, FIXES, "fixes", "with fixes"),
        (FIXES, BAD, "fixes", "not ready"),
    ],
)
def test_the_coordinator_is_woken_once_with_the_worst_verdict(
    cab, plan, main, web, state, verdict
):
    parent, a, _ = plan
    row = both(cab, parent, a)
    ce.end(cab, part(row, "web")["job"], web)
    assert latest(cab, a)["state"] == "reviewing" and boss_signals(cab) == []
    ce.end(cab, part(row, "digipaddock")["job"], main)
    after = latest(cab, a)
    assert (after["state"], after["verdict"], after["gate"]) == (state, verdict, "")
    assert boss_signals(cab) == [
        f"[stage] {a.id}: stage a of {parent.id} is handed in, review {verdict} — read kd-task view {a.id}"
    ]


def test_findings_name_the_pull_request_they_are_about(cab, plan):
    parent, a, _ = plan
    after = read(cab, both(cab, parent, a), web=FIXES)
    web = part(after, "web")
    body = next(
        m["body"] for m in cab.store.messages(a.id) if m["seq"] == web["message"]
    )
    assert "web#3" in body and "src/app.py:12" in body
    assert (after["message"], after["job"]) == (
        web["message"],
        web["job"],
    )  # what a return names
    assert after["findings"] == [{**FIX, "where": "web: src/app.py:12"}]
    assert (
        part(after, "digipaddock")["message"] == 0
    )  # a ready review with nothing to say


@pytest.mark.parametrize("how", ["failed", "blocked"])
def test_one_reviewer_that_fails_holds_the_stage_and_cancels_the_other(cab, plan, how):
    parent, a, _ = plan
    row = both(cab, parent, a)
    web, main = part(row, "web"), part(row, "digipaddock")
    if how == "failed":
        ce.end(cab, web["job"], state="failed", reason="the session ended")
    else:
        unread = {**FIX, "what": "no way to run git"}
        ce.end(
            cab,
            web["job"],
            {"status": "blocked", "verdict": "not ready", "findings": [unread]},
        )
    after = latest(cab, a)
    assert (after["state"], after["gate"], after["verdict"]) == ("failed", "held", "")
    assert jobs.row(cab.store, main["job"])["state"] in ("cancelled", "cancelling")
    # the other reviewer's block, should it still come, is nobody's verdict
    stale = {**jobs.row(cab.store, main["job"]), "state": "done"}
    stale["data"] = {**stale["data"], "result": READY}
    stage_gate.on_job_end(cab.ctx, stale)
    assert latest(cab, a)["state"] == "failed" and boss_signals(cab) == []


def test_a_missing_head_in_one_repository_is_ordered_again_alone(cab, plan):
    parent, a, _ = plan
    row = both(cab, parent, a)
    web, main = part(row, "web"), part(row, "digipaddock")
    ce.end(cab, web["job"], state="failed", reason=MISSING)
    waiting = latest(cab, a)
    assert (waiting["state"], waiting["gate"]) == ("reviewing", "")
    assert (part(waiting, "web")["job"], part(waiting, "digipaddock")["job"]) == (
        "",
        main["job"],
    )
    later(cab, seconds=stage_gate.RETRY_S)
    assert stage_gate.sweep(cab.ctx) == 1
    again = latest(cab, a)
    assert part(again, "web")["job"] not in ("", web["job"])
    assert part(again, "digipaddock")["job"] == main["job"]  # still the same reviewer
    assert jobs.row(cab.store, main["job"])["state"] not in jobs.TERMINAL


def test_a_pass_needs_every_pull_request_on_its_reviewed_head(cab, plan):
    parent, a, b = plan
    seq = done_work(cab, a)
    read(cab, both(cab, parent, a))
    publish(cab, parent.id, W2, **WEB)  # one repository moved after its review
    refused(
        cab,
        "boss.stage-pass",
        a,
        "web#3: the pull request moved past the reviewed head",
        reason="x",
        source={"card": a.id, "seq": seq},
    )
    publish(cab, parent.id, W1, **WEB)
    passing(cab, a, seq)
    assert cab.store.card(a.id).status == "Done"
    assert cab.store.card(b.id).status == "To Do"


def test_a_pull_request_opened_after_the_hand_in_refuses_the_pass(cab, plan):
    parent, a, _ = plan
    _, seq = reviewed(cab, a, H1)
    publish(cab, parent.id, W1, **WEB)
    refused(
        cab,
        "boss.stage-pass",
        a,
        "web#3: the pull request moved past the reviewed head (none)",
        reason="x",
        source={"card": a.id, "seq": seq},
    )


def test_a_return_names_any_review_with_fixes(cab, plan):
    parent, a, _ = plan
    done_work(cab, a)
    after = read(cab, both(cab, parent, a), web=FIXES)
    web, main = part(after, "web"), part(after, "digipaddock")
    refused(cab, "boss.stage-return", a, "is not a review with fixes", job=main["job"])
    call(cab, "boss.stage-return", a, job=web["job"])
    assert cab.store.card(a.id).status == "In Progress"
    [wake] = cab.store.commands(agent="dev", card=a.id, states=["queued"])
    assert f"the findings are kd-task message {a.id} {web['message']}" in wake["text"]


def test_a_return_tells_of_the_findings_of_every_repository(cab, plan):
    parent, a, _ = plan
    done_work(cab, a)
    after = read(cab, both(cab, parent, a), main=BAD, web=FIXES)
    call(cab, "boss.stage-return", a, job=after["job"])
    [wake] = cab.store.commands(agent="dev", card=a.id, states=["queued"])
    seqs = sorted(p["message"] for p in after["parts"])
    assert (
        f"the findings are kd-task message {a.id} {seqs[0]} and kd-task message {a.id} {seqs[1]}"
        in wake["text"]
    )
    assert "pull request of each repository you changed" in wake["text"]
    assert "(not ready)" in wake["text"]


def test_a_pull_request_the_passed_hand_in_did_not_have_is_read_from_its_start(
    cab, plan
):
    """Returned with fixes in web, the stage closed that pull request and was passed on the other one alone: the
    head nobody passed is no base when the pull request is open again."""
    parent, a, b = plan
    read(cab, both(cab, parent, a), web=FIXES)
    return_to_work(cab, a)
    publish(cab, parent.id, W1, state="closed", **WEB)
    ce.end(cab, handin(cab, a, H2)["job"], READY)
    passed(cab, a)
    publish(cab, parent.id, W1, **WEB)
    web = part(handin(cab, b, H2), "web")
    assert (web["base"], web["state"]) == ("", "reviewing")


def test_the_second_stage_is_read_from_each_repositorys_passed_head(cab, plan):
    parent, a, b = plan
    read(cab, both(cab, parent, a))
    passed(cab, a)
    second = both(cab, parent, b, main=H2, web=W2)
    assert {(p["project"], p["base"], p["head"]) for p in second["parts"]} == {
        ("digipaddock", H1, H2),
        ("web", W1, W2),
    }
    brief = jobs.row(cab.store, part(second, "web")["job"])["data"]["definition"][
        "brief"
    ]
    assert f"base: {W1}" in brief and "pull request web#3" in brief


def briefs(cab, row):
    return {
        p["project"]: jobs.row(cab.store, p["job"])["data"]["definition"]["brief"]
        for p in row["parts"]
        if p["job"]
    }


def test_the_reviewers_task_names_the_stages_other_pull_requests(cab, plan):
    parent, a, _ = plan
    found = briefs(cab, both(cab, parent, a))
    assert "the stage also changed: digipaddock#7" in found["web"]
    assert "the stage also changed: web#3" in found["digipaddock"]


def test_a_reviewer_is_not_told_of_a_pull_request_the_stage_left_alone(cab, plan):
    parent, a, b = plan
    read(cab, both(cab, parent, a))
    passed(cab, a)
    found = briefs(cab, handin(cab, b, H2))
    assert list(found) == ["digipaddock"] and "also changed" not in found["digipaddock"]


def test_the_snapshot_says_a_branch_and_a_pull_request_per_repository(cab, plan):
    parent, a, _ = plan
    both(cab, parent, a)
    head = snapshot.render(snapshot.build(cab.ctx, cab.store.card(a.id))).splitlines()[
        0
    ]
    assert (
        f"· parent {parent.id} (chain: a branch agent/{parent.id}-… and a pull request"
        f" of {parent.id} in each repository)" in head
    )


def test_hand_ins_of_schema_44_keep_their_heads(tmp_path):
    """A base with hand-ins from before the parts: each becomes a hand-in of one part, so the next stage is still
    read from the head the last closed one was passed on."""
    path = str(tmp_path / "v44.db")
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(schema.DDL + "".join(schema.MIGRATIONS[v] for v in range(2, 45)))
    conn.execute("INSERT INTO meta VALUES ('schema_version', '44')")
    conn.execute(
        "INSERT INTO prs (repo, number, card, project, head, state, updated) VALUES (?,?,?,?,?,?,?)",
        (
            "example/digipaddock",
            7,
            "KD-1",
            "digipaddock",
            H1,
            "open",
            "2026-01-01T00:00:00Z",
        ),
    )
    conn.execute(
        "INSERT INTO stage_reviews (card, parent, repo, number, head, base, target, job, state, verdict,"
        " findings, gate, message, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("KD-2", "KD-1", "example/digipaddock", 7, H1, "", "main", "j1", "fixes", "with fixes",
         '[{"severity": "Minor", "where": "a.py:1", "what": "x"}]', "returned", 5,
         "2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z"),
    )  # fmt: skip
    conn.execute(
        "INSERT INTO stage_reviews (card, parent, state, gate, created, updated) VALUES (?,?,?,?,?,?)",
        (
            "KD-2",
            "KD-1",
            "skipped",
            "passed",
            "2026-01-01T00:02:00Z",
            "2026-01-01T00:02:00Z",
        ),
    )  # nothing was published: a hand-in with no pull request has no part
    conn.execute(
        "INSERT INTO stage_reviews (card, parent, repo, number, head, target, state, reason, created, updated)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("KD-3", "KD-1", "example/digipaddock", 7, H2, "main", "reviewing", MISSING,
         "2026-01-01T00:03:00Z", "2026-01-01T00:04:00Z"),
    )  # fmt: skip
    conn.close()
    db = store.Store.open(path)
    assert db.schema_version() == schema.SCHEMA_VERSION >= 45
    first, second = (stage_gate._get(db, n) for n in (1, 2))
    [waiting] = stage_gate._get(db, 3)[
        "parts"
    ]  # a reviewer that waits for its head keeps why
    assert (waiting["state"], waiting["job"], waiting["reason"]) == (
        "reviewing",
        "",
        MISSING,
    )
    assert [
        (p["project"], p["head"], p["job"], p["state"], p["message"])
        for p in first["parts"]
    ] == [("digipaddock", H1, "j1", "fixes", 5)]
    assert (first["head"], first["job"], first["findings"][0]["where"]) == (
        H1,
        "j1",
        "a.py:1",
    )
    assert second["parts"] == [] and second["head"] == ""
