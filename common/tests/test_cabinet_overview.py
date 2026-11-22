"""The overview — what needs the person, work, queue and failures, changes, the team; rights first."""

import sys
from pathlib import Path

import pytest

from kd.cabinet import api, decisions, schema, store

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cabinet_env import CabinetEnv  # noqa: E402

NOW = "2026-10-14T10:00:00Z"


@pytest.fixture
def env(tmp_path):
    env = CabinetEnv(tmp_path)
    env.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    env.users.create("anna", "Анна", roles=[("partner", ["web"])], actor="root")
    return env


def person(user):
    return api.Principal("user", -1, user)


def overview(env, user="owner", **request):
    return api.dispatch(env.ctx, person(user), {"op": "overview.get", **request})


def command(env, card, agent="dev", n=1, state="queued", **extra):
    found = env.store.enqueue(
        {"key": f"o:{card.id}:{n}", "agent": agent, "thread": card.thread, "kind": "assigned", "text": f"go {n}"}
    )
    if state != "queued":
        session = env.store.session_for(card.thread, agent)
        env.store.command_state(found["id"], "delivered", session=session["id"])
        if state in ("started", "failed"):
            env.store.command_state(found["id"], "started", session=session["id"])
        if state == "failed":
            env.store.command_state(found["id"], "failed", session=session["id"], **extra)
    return env.store.command(found["id"])


def seed(env, project="digipaddock"):
    """One card of each kind the overview sorts, in ``project``."""
    asks = env.card(project=project, status="In Progress", title="Asks")
    decisions.Decisions(env.store).open(
        "question", card=asks.id, payload={"text": "?"}, requested_by="agent:dev", now=env.when()
    )
    review = env.card(project=project, status="In Progress", title="Review me")
    env.store.edit_card(review.id, None, {"status": "Review"}, actor="agent:dev")
    working = env.card(project=project, status="In Progress", title="Working")
    command(env, working, state="started")
    env.store.session_update(env.store.session_for(working.thread, "dev")["id"], state="working", heartbeat=NOW)
    env.store.add_message(working.id, author="agent:dev", kind="text", body="Собрал данные")
    env.store.add_message(working.id, author="agent:dev", kind="progress", body="шаг 3")
    brief = env.card(project=project, status="Inbox", assignee="@analyst", labels=["spec:task-brief"], title="Brief")
    command(env, brief, agent="analyst", state="started")
    env.store.session_update(env.store.session_for(brief.thread, "analyst")["id"], state="working", heartbeat=NOW)
    failed = env.card(project=project, status="In Progress", title="Failed")
    command(env, failed, state="failed", error="exit 1", reason="exit")
    late = env.card(project=project, status="To Do", title="Late")
    command(env, late, state="delivered")
    dep = env.card(project=project, status="To Do", title="Dep")
    blocked = env.card(project=project, status="Blocked", title="Blocked", dependencies=[dep.id])
    return {"asks": asks, "review": review, "working": working, "brief": brief, "failed": failed, "late": late,
            "blocked": blocked, "dep": dep}


def ids(rows):
    return [r["card"] for r in rows]


def test_every_section_of_the_overview(env):
    cards = seed(env)
    env.now = "2026-10-14T10:05:00Z"  # the late start is 5 minutes old; the working session beats on
    env.store.session_update(env.store.session_for(cards["working"].thread, "dev")["id"], heartbeat=env.now)
    found = overview(env)
    needs = {(r["kind"], r["card"]) for r in found["needs"]}
    assert ("question", cards["asks"].id) in needs and ("review", cards["review"].id) in needs
    question = next(r for r in found["needs"] if r["kind"] == "question")
    assert question["waiting"] == "@dev" and question["link"] == f"/cards/{cards['asks'].id}?decision={question['decision']}"
    review = next(r for r in found["needs"] if r["kind"] == "review")
    assert review["link"] == f"/cards/{cards['review'].id}?tab=result" and review["since"] == NOW
    work = {r["card"]: r for r in found["work"]}
    assert work[cards["working"].id]["run"] == "working"
    assert work[cards["working"].id]["last_step"] == {"at": NOW, "text": "Собрал данные"}
    assert work[cards["brief"].id]["stage"] == "in_progress"  # the analyst's brief in Inbox is work
    assert work[cards["late"].id]["run"] == "delayed"
    problems = {(r["kind"], r["reason"], r["card"]) for r in found["problems"]}
    assert ("error", "exit", cards["failed"].id) in problems
    assert ("waiting", "delayed", cards["late"].id) in problems
    assert ("waiting", "deps", cards["blocked"].id) in problems
    dev = next(a for a in found["team"] if a["name"] == "dev")
    assert dev["working"] == 1 and dev["waiting_you"] == 1
    assert found["counts"]["needs"] == len(found["needs"]) and found["counts"]["problems"] == len(found["problems"])
    assert found["watermark"] == env.store.last_seq()


def test_changes_are_results_not_noise(env):
    cards = seed(env)
    kinds = {(c["kind"], c["card"]) for c in overview(env)["changes"]}
    assert ("review", cards["review"].id) in kinds and ("created", cards["asks"].id) in kinds
    assert all(c["kind"] in ("created", "review", "done", "blocked", "handoff", "brief") for c in overview(env)["changes"])


def test_new_is_what_came_after_the_persons_last_look(env):
    seed(env)
    first = overview(env)
    assert first["seen"] == 0 and all(c["new"] for c in first["changes"])
    api.dispatch(env.ctx, person("owner"), {"op": "user.mark", "name": "overview_seen", "value": first["watermark"]})
    card = env.card(title="After")
    again = overview(env)
    assert [c["card"] for c in again["changes"] if c["new"]] == [card.id]
    assert again["counts"]["changes_new"] == 1
    # the mark only grows and closes no question
    api.dispatch(env.ctx, person("owner"), {"op": "user.mark", "name": "overview_seen", "value": 1})
    assert overview(env)["seen"] == first["watermark"]
    assert any(r["kind"] == "question" for r in overview(env)["needs"])


def test_overview_for_a_partner_has_nothing_of_other_projects(env):
    seed(env, "digipaddock")
    mine = env.card(project="web", status="In Progress", title="Ops")
    decisions.Decisions(env.store).open(
        "question", card=mine.id, payload={"text": "?"}, requested_by="agent:dev", now=env.when()
    )
    review = env.card(project="web", status="Review", title="Ops review")  # she may not accept it: not hers to do
    found = overview(env, "anna")
    rows = found["needs"] + found["work"] + found["problems"]
    assert {r["card"] for r in rows} == {mine.id}
    assert {r["card"] for r in found["changes"]} == {mine.id, review.id}
    dev = next(a for a in found["team"] if a["name"] == "dev")
    assert (dev["working"], dev["waiting_you"], dev["queued"], dev["failed"]) == (0, 1, 0, 0)
    assert found["counts"] == {"needs": 1, "work": 1, "problems": 0, "changes_new": len(found["changes"])}


def test_project_and_period_narrow_it(env):
    seed(env, "digipaddock")
    web = env.card(project="web", status="Review", title="Web")
    found = overview(env, project="web")
    assert ids(found["needs"]) == [web.id] and found["work"] == []
    env.now = "2026-10-20T10:00:00Z"
    assert overview(env, period="1d")["changes"] == []
    assert overview(env, period="7d")["changes"]
    with pytest.raises(api.ApiError) as exc:
        overview(env, period="2d")
    assert exc.value.kind == "invalid"


def test_v5_base_migrates_to_v6(tmp_path):
    import sqlite3

    path = str(tmp_path / "v5.db")
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(schema.DDL + schema.DDL_V2 + schema.DDL_V3 + schema.DDL_V4 + schema.DDL_V5)
    conn.execute("INSERT INTO meta VALUES ('schema_version', '5')")
    conn.execute("INSERT INTO meta VALUES ('next_card', '1')")
    conn.close()
    db = store.Store.open(path)
    assert db.schema_version() == schema.SCHEMA_VERSION  # v6 and every later step
    db.mark("owner", "overview_seen", 5)
    assert db.marks("owner") == {"overview_seen": 5}
    assert store.Store.open(path).marks("owner") == {"overview_seen": 5}


def test_a_run_whose_session_stopped_beating_is_not_working(env):
    card = env.card(status="In Progress", title="Silent")
    command(env, card, state="started")
    env.store.session_update(env.store.session_for(card.thread, "dev")["id"], state="working", heartbeat=NOW)
    env.now = "2026-10-14T10:05:00Z"
    (row,) = [r for r in overview(env)["work"] if r["card"] == card.id]
    assert row["run"] == "stale"


def test_a_handoff_with_a_status_change_in_the_same_write_is_a_handoff(env):
    card = env.card(status="To Do")
    env.store.edit_card(card.id, None, {"status": "In Progress", "assignee": "@analyst"}, actor="agent:dev")
    changes = [c for c in overview(env)["changes"] if c["card"] == card.id]
    assert ("handoff", "@analyst") in {(c["kind"], c.get("to")) for c in changes}


def test_a_mark_past_the_journal_is_refused(env):
    env.card()
    with pytest.raises(api.ApiError) as exc:
        api.dispatch(env.ctx, person("owner"), {"op": "user.mark", "name": "overview_seen", "value": 2**63})
    assert exc.value.kind == "invalid"
    assert overview(env)["seen"] == 0


def test_pipeline_problems_in_overview(env):
    from kd.cabinet import problems

    card = env.card(status="In Progress", title="Stuck")
    problems.raise_problem(env.ctx, key="m:1", record=env.store.card(card.id), kind="missing_next_step", agent="dev",
                           command="c1")
    found = overview(env)
    [row] = [r for r in found["problems"] if r["kind"] == "stuck"]
    assert (row["card"], row["reason"], row["agent"], row["since"]) == (card.id, "missing_next_step", "@dev", NOW)
    assert row["title"] == "Stuck" and found["counts"]["problems"] == len(found["problems"])
    assert [r for r in overview(env, "anna")["problems"] if r["kind"] == "stuck"] == []
    problems.resolve(env.store, card.id, ["missing_next_step"], by="test")
    assert [r for r in overview(env)["problems"] if r["kind"] == "stuck"] == []
