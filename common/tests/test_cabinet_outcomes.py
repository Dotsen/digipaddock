"""a turn's outcome — kd-work finish records it for the running turn on the agent's own card, and the
cabinet applies it when the turn ends."""

import pytest
from cabinet_env import CabinetEnv

from kd import pipeline_config, session_config
from kd.cabinet import api, decisions, problems

DEV = "digi"  # the executor runs as the base user; the agent comes from the channel


@pytest.fixture
def cab(tmp_path):
    env = CabinetEnv(tmp_path)
    env.ctx.host_config = lambda: session_config.HostConfig({"boss": 1, "dev": 2, "analyst": 1})
    return env


def card(cab, assignee="@dev", status="In Progress", **extra):
    return cab.store.create_card({"title": "Work", "project": "digipaddock", "status": status, "assignee": assignee,
                                  "created_by": "@owner", **extra}, actor="user:owner")


def started(cab, record, agent="dev", kind="assigned", thread=None, to="started"):
    """A command of ``agent`` the host delivered and started, in the card's thread (or ``thread``)."""
    thread = thread or record.thread
    session = cab.store.session_for(thread, agent)["id"]
    command = cab.store.enqueue({"key": f"k:{thread}:{agent}:{kind}:{to}", "agent": agent, "thread": thread,
                                 "card": record.id if thread == record.thread else "", "kind": kind,
                                 "text": f"[board] {record.id} {kind} {to}"})["id"]
    path = {"queued": [], "started": ["delivered", "started"], "done": ["delivered", "started", "done"]}[to]
    for state in path:
        cab.store.command_state(command, state, session=session)
    return command


def finish(cab, record, outcome="continue", note="next: write the tests", agent="dev", **extra):
    return cab.call(DEV, {"op": "turn.finish", "agent": agent, "card": record.id, "outcome": outcome, "note": note,
                          **extra})


def refused(cab, record, *, kind="refused", **kw):
    with pytest.raises(api.ApiError) as exc:
        finish(cab, record, **kw)
    assert exc.value.kind == kind, exc.value
    return str(exc.value)


def question(cab, record, agent="dev", text="?"):
    return decisions.Decisions(cab.store).open("question", card=record.id, payload={"text": text},
                                               requested_by=f"agent:{agent}", now=cab.now)["id"]


def outcome_rows(cab, record):
    return [dict(r) for r in cab.store._conn.execute("SELECT * FROM turn_outcomes WHERE card=?", (record.id,))]


def wait_rows(cab, record):
    return [dict(r) for r in cab.store._conn.execute(
        "SELECT * FROM waits WHERE card=? ORDER BY kind, ref", (record.id,))]


def test_finish_records_a_pending_outcome(cab):
    record = card(cab)
    command = started(cab, record)
    seq = cab.store.last_seq()
    result = finish(cab, record)
    assert result["outcome"]["state"] == "pending" and result["outcome"]["command"] == command
    assert result["outcome"]["basis"]["assignee"] == "@dev" and result["waits"] == []
    assert [e["kind"] for e in cab.store.events_since(seq)] == ["turn.finish"]
    assert cab.store.card(record.id).status == "In Progress"


@pytest.mark.parametrize("to", ["queued", "done"])
def test_finish_without_a_running_turn_is_refused(cab, to):
    record = card(cab)
    started(cab, record, to=to)
    assert "no turn of yours is running" in refused(cab, record)
    assert outcome_rows(cab, record) == []


def test_finish_from_another_thread_is_refused(cab):
    record = card(cab)
    started(cab, record, kind="chat", thread=cab.store.agent_thread("dev"))
    assert "no turn of yours is running" in refused(cab, record)


def test_finish_by_a_non_assignee_is_refused(cab):
    record = card(cab)
    started(cab, record, agent="boss", kind="signal")
    assert "finish is for the card's assignee" in refused(cab, record, agent="boss")


def test_wait_user_takes_the_agents_open_decisions(cab):
    record, other = card(cab), card(cab)
    started(cab, record)
    asked = {question(cab, record), question(cab, record, text="??")}
    pr = decisions.Decisions(cab.store).open("pr_approve", card=record.id, payload={"head": "a" * 40},
                                             requested_by="agent:dev", now=cab.now)["id"]
    question(cab, record, agent="analyst")  # another agent's question is not dev's wait
    result = finish(cab, record, outcome="wait_user", note="waiting for the owner's answers")
    assert {w["ref"] for w in result["waits"]} == asked | {pr}
    assert all(w["kind"] == "decision" and w["blocking"] == 1 and w["state"] == "pending" for w in result["waits"])
    foreign = question(cab, other)
    assert "is not on" in refused(cab, record, kind="invalid", outcome="wait_user", decisions=[foreign])


def test_wait_user_without_a_decision_is_refused(cab):
    record = card(cab)
    started(cab, record)
    assert "no open decision of yours" in refused(cab, record, outcome="wait_user")


def test_wait_dependency_needs_a_structured_dependency(cab):
    dep, unrelated = card(cab), card(cab)
    record = card(cab)
    started(cab, record)
    assert "has no open structured dependency" in refused(cab, record, outcome="wait_dependency")
    cab.store.edit_card(record.id, None, {"dependencies": [dep.id]}, actor="user:owner")
    assert "is not a dependency" in refused(cab, record, outcome="wait_dependency", cards=[unrelated.id])
    result = finish(cab, record, outcome="wait_dependency", note="waiting for the dependency")
    assert [(w["kind"], w["ref"], w["blocking"]) for w in result["waits"]] == [("card", dep.id, 1)]


def test_continue_keeps_open_questions_non_blocking(cab):
    record = card(cab)
    started(cab, record)
    asked = question(cab, record)
    result = finish(cab, record)
    assert [(w["ref"], w["blocking"]) for w in result["waits"]] == [(asked, 0)]


def test_refinish_replaces_and_same_is_unchanged(cab):
    record = card(cab)
    started(cab, record)
    question(cab, record)
    finish(cab, record)
    finish(cab, record, outcome="wait_user", note="waiting")
    assert [(o["outcome"], o["state"]) for o in outcome_rows(cab, record)] == [("wait_user", "pending")]
    assert [w["blocking"] for w in wait_rows(cab, record)] == [1]
    seq = cab.store.last_seq()
    assert finish(cab, record, outcome="wait_user", note="waiting").get("unchanged") is True
    assert cab.store.events_since(seq) == []


@pytest.mark.parametrize("note", ["", " ", "x" * 301, "one\ntwo"])
def test_note_is_one_short_line(cab, note):
    record = card(cab)
    started(cab, record)
    refused(cab, record, kind="invalid", note=note)


def test_outcome_is_one_of_the_known(cab):
    record = card(cab)
    started(cab, record)
    assert "package C" in refused(cab, record, kind="invalid", outcome="wait_handoff")
    refused(cab, record, kind="invalid", outcome="later")


def test_the_coordinator_records_no_review_or_done(cab):
    """BOSS may not set Review or Done (its authority): its outcome on its own card cannot either."""
    record = card(cab, assignee="@boss")
    started(cab, record, agent="boss")
    for outcome in ("review", "done"):
        assert "the owner's" in refused(cab, record, outcome=outcome, agent="boss")
    assert finish(cab, record, outcome="continue", agent="boss")["outcome"]["outcome"] == "continue"


def test_review_and_done_need_their_status(cab):
    record = card(cab, status="To Do")
    started(cab, record)
    assert "In Progress" in refused(cab, record, outcome="review")
    assert "In Progress" in refused(cab, record, outcome="done")


def test_owner_cards_and_cards_out_of_work_owe_nothing(cab):
    closed = card(cab, status="Done")
    started(cab, closed)
    assert "finish is for the card's assignee" in refused(cab, closed)
    review = card(cab, status="Review")
    started(cab, review)
    assert finish(cab, review, outcome="done", note="merged and released")["outcome"]["outcome"] == "done"
    assert "is in Review" in refused(cab, review, outcome="review")
    brief = card(cab, assignee="@analyst", status="Inbox", labels=["spec:task-brief"])
    started(cab, brief, agent="analyst")
    assert finish(cab, brief, agent="analyst")["outcome"]["state"] == "pending"


# --- the outcome applies when the turn ends; a turn without one is a problem ---------------------
HOSTED = session_config.HostConfig({"boss": 1, "dev": 2, "analyst": 1})


def enforce(cab):
    cab.ctx.pipeline = lambda: pipeline_config.Config(mode="enforce")


def report(cab, *items):
    return cab.call("root", {"op": "host.report", "items": list(items)})


def delivered(cab, record, agent="dev", kind="assigned", key=None, thread=None):
    thread = thread or record.thread
    session = cab.store.session_for(thread, agent)["id"]
    command = cab.store.enqueue({"key": key or f"d:{thread}:{agent}:{kind}:{cab.store.last_seq()}", "agent": agent,
                                 "thread": thread, "card": record.id if thread == record.thread else "",
                                 "kind": kind, "text": f"[board] {record.id} {kind} {cab.store.last_seq()}"})["id"]
    cab.store.command_state(command, "delivered", session=session)
    return command, session


def begin(cab, record, **kw):
    command, session = delivered(cab, record, **kw)
    report(cab, {"type": "turn", "session": session, "command": command, "phase": "started"})
    return command, session


def end(cab, command, session, ok=True):
    item = {"type": "turn", "session": session, "command": command, "phase": "ended", "ok": ok}
    if not ok:
        item["error"] = "boom"
    report(cab, item)


def outcome_of(cab, command):
    return dict(cab.store._conn.execute("SELECT * FROM turn_outcomes WHERE command=?", (command,)).fetchone())


def queued(cab, record, kind=None):
    return [c for c in cab.store.commands(card=record.id, states=["queued"]) if kind is None or c["kind"] == kind]


def test_continue_at_turn_end_queues_one_continuation(cab):
    enforce(cab)
    record = card(cab)
    command, session = begin(cab, record)
    finish(cab, record)
    end(cab, command, session)
    [cont] = queued(cab, record, "continue")
    assert cont["key"] == f"cont:{command}" and cont["basis"]["assignee"] == "@dev"
    assert outcome_of(cab, command)["state"] == "final"
    end(cab, command, session)  # the same report again
    assert len(queued(cab, record, "continue")) == 1


def test_observe_records_and_queues_nothing(cab):
    record = card(cab)
    command, session = begin(cab, record)
    finish(cab, record)
    end(cab, command, session)
    assert outcome_of(cab, command)["state"] == "final" and queued(cab, record) == []
    [row] = cab.store._conn.execute("SELECT state FROM continuations WHERE card=?", (record.id,)).fetchall()
    assert row[0] == "observed"


def test_failed_turn_voids_the_outcome(cab):
    enforce(cab)
    record = card(cab)
    command, session = begin(cab, record)
    question(cab, record)
    finish(cab, record)
    end(cab, command, session, ok=False)
    assert outcome_of(cab, command)["state"] == "void" and queued(cab, record) == []
    assert {w["state"] for w in wait_rows(cab, record)} == {"cancelled"}


def test_host_restart_voids_a_started_turns_outcome(cab):
    enforce(cab)
    record = card(cab)
    command, _ = begin(cab, record)
    finish(cab, record)
    report(cab, {"type": "host", "phase": "started", "inflight": [command]})
    row = outcome_of(cab, command)
    assert (row["state"], row["reason"]) == ("void", "host_restart") and queued(cab, record, "continue") == []


@pytest.mark.parametrize("stop_state", ["queued", "done"])
def test_a_continue_the_owners_stop_beat_is_void_not_applied(cab, stop_state):
    """Review M5 (spec K): the continuation is stopped, so the outcome is not shown as applied, and the card waits
    for the owner's "Continue", not for a route."""
    enforce(cab)
    record = card(cab)
    command, session = begin(cab, record)
    finish(cab, record)
    stop = cab.store.enqueue({"key": "stop1", "agent": "dev", "thread": record.thread, "card": record.id,
                              "kind": "stop", "text": "[session] stop", "initiator": "user:owner"})["id"]
    if stop_state == "done":
        for state in ("delivered", "started", "done"):
            cab.store.command_state(stop, state)
    end(cab, command, session)
    row = outcome_of(cab, command)
    assert (row["state"], row["reason"]) == ("void", "stopped") and queued(cab, record, "continue") == []
    assert [p for p in problems.open_for(cab.store, record.id) if p["kind"] == "missing_next_step"] == []


def test_reassigned_during_the_turn_supersedes(cab):
    enforce(cab)
    record = card(cab)
    command, session = begin(cab, record)
    finish(cab, record)
    cab.store.edit_card(record.id, None, {"assignee": "@sysadmin"}, actor="user:owner")
    end(cab, command, session)
    row = outcome_of(cab, command)
    assert (row["state"], row["reason"]) == ("superseded", "reassigned") and queued(cab, record, "continue") == []


def test_review_outcome_moves_the_card(cab):
    record = card(cab)
    command, session = begin(cab, record)
    finish(cab, record, outcome="review", note="ready for review")
    seq = cab.store.last_seq()
    end(cab, command, session)
    assert cab.store.card(record.id).status == "Review"
    moved = [e for e in cab.store.events_since(seq) if e["kind"] == "card.changed"]
    assert moved and moved[0]["actor"] == "agent:dev"


def test_turn_without_outcome_raises_one_problem(cab):
    enforce(cab)
    record = card(cab)
    command, session = begin(cab, record)
    end(cab, command, session)
    [problem] = problems.open_for(cab.store, record.id)
    assert (problem["kind"], problem["key"]) == ("missing_next_step", f"missing:{command}")
    boss = [c for c in queued(cab, record) if c["agent"] == "boss"]
    assert [c["key"] for c in boss] == [f"problem:{problem['id']}"]
    end(cab, command, session)  # the same report again: still one
    assert len(problems.open_for(cab.store, record.id)) == 1
    second, session2 = begin(cab, record)
    end(cab, second, session2)
    assert [p["key"] for p in problems.open_for(cab.store, record.id)] == [f"missing:{second}"]


def test_open_question_is_a_next_step(cab):
    record = card(cab)
    command, session = begin(cab, record)
    question(cab, record)
    end(cab, command, session)
    assert problems.open_for(cab.store, record.id) == []


def test_queued_command_is_a_next_step(cab):
    record = card(cab)
    command, session = begin(cab, record)
    cab.store.enqueue({"key": "msg1", "agent": "dev", "thread": record.thread, "card": record.id, "kind": "message",
                       "text": f"[board] {record.id}: a message for you"})
    end(cab, command, session)
    assert problems.open_for(cab.store, record.id) == []


def test_chat_and_other_agents_turns_owe_nothing(cab):
    record = card(cab)
    chat = cab.store.agent_thread("dev")
    command, session = begin(cab, record, kind="chat", thread=chat)
    end(cab, command, session)
    command, session = begin(cab, record, agent="boss", kind="signal")
    end(cab, command, session)
    assert problems.open_for(cab.store, record.id) == []


def test_brief_work_in_inbox_owes_an_outcome(cab):
    record = card(cab, assignee="@analyst", status="Inbox", labels=["spec:task-brief"])
    command, session = begin(cab, record, agent="analyst")
    end(cab, command, session)
    assert [p["kind"] for p in problems.open_for(cab.store, record.id)] == ["missing_next_step"]
    other = card(cab, assignee="@analyst", status="Inbox", labels=["spec:task-brief"])
    command, session = begin(cab, other, agent="analyst")
    cab.store.edit_card(other.id, None, {"assignee": "@dev", "labels_remove": ["spec:task-brief"]}, actor="agent:analyst")
    end(cab, command, session)
    assert problems.open_for(cab.store, other.id) == []


def test_next_turn_resolves_the_problem(cab):
    record = card(cab)
    command, session = begin(cab, record)
    end(cab, command, session)
    [problem] = problems.open_for(cab.store, record.id)
    begin(cab, record)
    assert problems.open_for(cab.store, record.id) == []
    state = cab.store._conn.execute("SELECT state FROM problems WHERE id=?", (problem["id"],)).fetchone()[0]
    assert state == "resolved"


def test_a_final_outcome_resolves_the_problem(cab):
    enforce(cab)
    record = card(cab)
    command, session = begin(cab, record)
    end(cab, command, session)
    assert problems.open_for(cab.store, record.id)
    # the next turn resolves it as it starts; its outcome applies cleanly and raises nothing new
    command, session = begin(cab, record)
    finish(cab, record, outcome="review", note="done, to review")
    end(cab, command, session)
    assert problems.open_for(cab.store, record.id) == []


def test_review_from_the_wrong_status_is_an_outcome_refused(cab):
    record = card(cab)
    command, session = begin(cab, record)
    finish(cab, record, outcome="review", note="ready")
    cab.store.edit_card(record.id, None, {"status": "Blocked"}, actor="user:owner")
    end(cab, command, session)
    row = outcome_of(cab, command)
    assert row["state"] == "final" and row["reason"]
    assert [p["kind"] for p in problems.open_for(cab.store, record.id)] == ["outcome_refused"]


def test_continuation_does_not_jump_the_queue(cab):
    enforce(cab)
    first, record = card(cab), card(cab)
    waiting = cab.store.enqueue({"key": "b1", "agent": "dev", "thread": first.thread, "card": first.id,
                                 "kind": "assigned", "text": f"[board] {first.id} assigned to you"})
    command, session = begin(cab, record)
    finish(cab, record)
    end(cab, command, session)
    leased = cab.store.lease_commands("2099-01-01T00:00:00Z", "2099-01-01T00:01:00Z", 10)
    assert [c["id"] for c in leased][0] == waiting["id"]


# --- final review findings --------------------------------------------------------------------------------------
def test_superseded_outcome_with_nobody_on_the_card_is_a_problem(cab):
    enforce(cab)
    record = card(cab)
    command, session = begin(cab, record)
    finish(cab, record)
    cab.store.edit_card(record.id, None, {"description": "a new scope"}, actor="user:owner")
    end(cab, command, session)
    row = outcome_of(cab, command)
    assert (row["state"], row["reason"]) == ("superseded", "scope_changed")
    assert [p["key"] for p in problems.open_for(cab.store, record.id)] == [f"missing:{command}"]


@pytest.mark.parametrize("outcome,status", [("review", "Review"), ("done", "Done")])
def test_an_outcome_whose_status_is_in_place_already_applies(cab, outcome, status):
    record = card(cab)
    command, session = begin(cab, record)
    finish(cab, record, outcome=outcome, note="ready")
    cab.store.edit_card(record.id, None, {"status": status}, actor="user:owner")
    end(cab, command, session)
    row = outcome_of(cab, command)
    assert (row["state"], row["reason"]) == ("final", "")
    assert problems.open_for(cab.store, record.id) == []


def test_in_review_only_done_is_recorded(cab):
    record = card(cab, status="Review")
    started(cab, record)
    for outcome in ("continue", "review"):
        assert "is in Review" in refused(cab, record, outcome=outcome)
    assert finish(cab, record, outcome="done", note="merged")["outcome"]["outcome"] == "done"


def test_finish_names_the_callers_own_session(cab):
    first, second = card(cab), card(cab)
    started(cab, first)
    started(cab, second)
    own = cab.store.session_for(second.thread, "dev")["id"][:12]
    other = cab.store.session_for(first.thread, "dev")["id"][:12]
    assert "no turn of yours is running" in refused(cab, second, session=other)
    assert outcome_rows(cab, second) == []
    assert "session" in refused(cab, second, kind="invalid", session="not a session")
    assert finish(cab, second, session=own)["outcome"]["state"] == "pending"


def test_a_failing_finalize_keeps_the_turns_end(cab, monkeypatch):
    from kd.cabinet import outcomes

    record = card(cab)
    command, session = begin(cab, record)

    def boom(ctx, command_row, ok):
        problems.raise_problem(ctx, key="half-written", record=record, kind="missing_next_step", agent="dev")
        raise RuntimeError("boom")

    monkeypatch.setattr(outcomes, "finalize", boom)
    end(cab, command, session)
    assert cab.store.command(command)["state"] == "done"
    assert problems.open_for(cab.store, record.id) == []  # what it wrote before failing is rolled back
