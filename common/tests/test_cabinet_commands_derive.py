import pytest
from kd import board
from kd.cabinet import commands, store

NOW = "2026-10-17T10:00:00Z"
HOSTED = {"analyst", "boss"}


@pytest.fixture
def db(tmp_path):
    s = store.Store.open(str(tmp_path / "cabinet.db"), prefix="KD", clock=lambda: NOW)
    commands.install(s, lambda agent: agent in HOSTED)
    return s


def _card(db, **extra):
    values = {"title": "Work", "project": "digipaddock", "status": "Inbox", "created_by": "@owner", **extra}
    return db.create_card(values, actor="user:owner")


def _edit(db, card, changes, actor="user:owner"):
    return db.edit_card(card.id, db.card(card.id).revision, changes, actor=actor)


def test_to_do_assignment_of_hosted_agent_queues_one_command(db):
    card = _card(db, assignee="@analyst")
    assert db.commands(card=card.id) == []  # a new Inbox card is nobody's cue
    _edit(db, card, {"status": "To Do"})
    [command] = db.commands(card=card.id)
    assert command["agent"] == "analyst" and command["kind"] == "assigned"
    assert command["text"] == f"[board] {card.id} assigned to you" and command["thread"] == card.thread
    assert command["initiator"] == "user:owner" and command["key"].startswith("ev:")
    changed = [e for e in db.events_since(0) if e["kind"] == "card.changed"][-1]
    assert command["event"] == changed["seq"]


def test_not_hosted_agent_gets_none(db):
    card = _card(db, assignee="@dev")
    _edit(db, card, {"status": "To Do"})
    assert db.commands(card=card.id) == []


def test_brief_click_coalesces_to_one(db):
    card = _card(db)
    with db.transaction():
        _edit(db, card, {"assignee": "@analyst", "labels_add": ["spec:task-brief"]})
        db.wake_card(card.id, actor="user:owner", agent="@analyst")
    assert len(db.commands(card=card.id)) == 1


def test_mention_queues_message_command(db):
    card = _card(db, assignee="@dev")
    db.add_message(card.id, author="user:owner", kind="text", body="@analyst look", mentions=["@analyst"])
    [command] = db.commands(card=card.id)
    assert command["kind"] == "message" and command["text"] == board.MESSAGE_WAKE.format(id=card.id)


def test_agent_message_naming_nobody_queues_none(db):
    card = _card(db, assignee="@analyst")
    db.add_message(card.id, author="agent:analyst", kind="text", body="done")
    assert db.commands(card=card.id) == []


def test_answer_queues_answer_command(db):
    card = _card(db, assignee="@analyst")
    decision = "a" * 32
    with db.transaction():
        db._event(
            "user:owner",
            "decision.approved",
            card.id,
            {"type": "question", "requested_by": "agent:analyst", "decision": decision},
            card.thread,
        )
    [command] = db.commands(card=card.id)
    assert command["kind"] == "answer"
    assert command["text"] == board.ANSWER_WAKE.format(id=card.id, decision=decision)


def test_a_question_on_an_agents_decision_wakes_that_agent(db):
    # "Ask a question" on BOSS's proposal reached nobody — the decision stays open, its agent answers
    from kd.cabinet import decisions

    card = _card(db, assignee="@sysadmin")
    ledger = decisions.Decisions(db)
    opened = ledger.open("promote", card=card.id, payload={"reason": "r"}, requested_by="agent:boss", now=NOW)
    assert db.commands(card=card.id) == []
    ledger.ask(opened["id"], user="owner", text="Почему сейчас?", now=NOW)
    [command] = db.commands(card=card.id)
    assert command["agent"] == "boss" and command["kind"] == "message" and command["thread"] == card.thread
    assert command["text"] == board.ASKED_WAKE.format(id=card.id, type="promote", decision=opened["id"])
    assert "Почему" not in command["text"]  # the agent reads the person's words in the thread


def test_a_question_on_a_decision_kicks_no_watch():
    asked = {"kind": "asked", "id": "KD-1", "title": "", "status": "", "assignees": ["boss"], "old_status": None,
             "old_assignees": [], "by": "user:owner", "type": "promote", "decision": "a" * 32}
    assert board.coordinator_events([asked]) is False


def test_boss_not_woken_by_assignment(db):
    card = _card(db, assignee="@boss")
    _edit(db, card, {"status": "To Do"})
    assert db.commands(card=card.id) == []
    db.add_message(card.id, author="user:owner", kind="text", body="@boss", mentions=["@boss"])
    assert [c["kind"] for c in db.commands(card=card.id)] == ["message"]


def test_done_card_assignment_none(db):
    card = _card(db, status="Done")
    _edit(db, card, {"assignee": "@analyst"})
    assert db.commands(card=card.id) == []


def test_stale_reasons(db):
    hosted = lambda agent: agent in HOSTED  # noqa: E731
    card = _card(db, assignee="@analyst")
    _edit(db, card, {"status": "To Do"})
    [assigned] = db.commands(card=card.id)
    assert commands.stale_reason(db, assigned, hosted) is None
    assert commands.stale_reason(db, assigned, lambda agent: False) == "not_hosted"
    _edit(db, card, {"assignee": "@dev"})
    assert commands.stale_reason(db, assigned, hosted) == "reassigned"
    _edit(db, card, {"assignee": "@analyst", "status": "Done"})
    assert commands.stale_reason(db, assigned, hosted) == "card_done"
    answer = dict(assigned, kind="answer")
    assert commands.stale_reason(db, answer, hosted) is None  # an answer still reaches the agent
    gone = dict(assigned, card="KD-999")
    assert commands.stale_reason(db, gone, hosted) == "card_gone"


def test_signal_goes_to_agent_chat_without_card(db):
    command = commands.enqueue_signal(
        db, agent="analyst", text="[monitor] look", initiator="agent:sysadmin", key="sig-1"
    )
    assert command["card"] == "" and command["kind"] == "signal"
    assert db.thread_info(command["thread"])["kind"] == "agent_chat"
    card = _card(db)
    on_card = commands.enqueue_signal(
        db, agent="analyst", text="[boss] directive", card=card.id, initiator="agent:boss", key="sig-2"
    )
    assert on_card["thread"] == card.thread and on_card["card"] == card.id
    assert commands.stale_reason(db, command, lambda agent: True) is None


def test_derive_ignores_its_own_events(db):
    for kind in ("command.queued", "run.started", "session.changed"):
        assert commands.derive({"seq": 1, "kind": kind, "card": "KD-1", "actor": "user:owner", "payload": {}}) == []


@pytest.mark.parametrize(
    "module",
    ["kd.cabinet.server", "kd.cabinet.client", "kd.cabinet.api", "kd.cabinet.sessions_api", "kd.cabinet.commands",
     "kd.cabinet.dispatcher", "kd.cabinet.jobs", "kd.cabinet.usage", "kd.session_host.claude", "kd.board", "kd.wake"],
)
def test_every_module_imports_first_in_a_fresh_process(module):
    """No import cycle: whichever module a process loads first (kd-cabinet, kd-board, kd-task …) loads."""
    import subprocess
    import sys
    from pathlib import Path

    common = str(Path(__file__).resolve().parents[1])
    proc = subprocess.run([sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {common!r}); import {module}"],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr[-800:]


@pytest.mark.parametrize("before", ["Review", "Blocked"])
def test_a_person_returning_review_to_work_wakes_the_assignee_to_continue(db, before):
    card = _card(db, assignee="@analyst", status=before)
    _edit(db, card, {"status": "In Progress"})
    [command] = db.commands(card=card.id)
    assert command["agent"] == "analyst" and command["kind"] == "continue"
    assert command["text"] == board.CONTINUE_WAKE.format(id=card.id)


def test_blocked_back_to_work_wakes_the_assignee(db):
    card = _card(db, assignee="@analyst", status="Blocked")
    _edit(db, card, {"status": "In Progress", "assignee": "@boss"}, actor="user:owner")
    [command] = db.commands(card=card.id)  # the new assignee, even the coordinator: a person handed it over
    assert command["agent"] == "boss" and command["kind"] == "continue"


def test_an_agent_moving_its_own_card_wakes_nobody(db):
    card = _card(db, assignee="@analyst", status="Review")
    _edit(db, card, {"status": "In Progress"}, actor="agent:analyst")
    _edit(db, card, {"status": "Blocked"}, actor="user:owner")
    assert db.commands(card=card.id) == []


def test_a_ready_brief_wakes_the_coordinator_once(db):
    card = _card(db, assignee="@analyst", labels=["spec:task-brief"])
    _edit(db, card, {"assignee": "@dev", "labels_add": ["brief-ready"]}, actor="agent:analyst")
    [command] = db.commands(card=card.id)
    assert command["agent"] == "boss" and command["kind"] == "signal"
    assert command["text"] == board.BRIEF_WAKE.format(id=card.id) and command["thread"] == card.thread
    _edit(db, card, {"title": "Work, better"}, actor="agent:analyst")
    assert len(db.commands(card=card.id)) == 1


def test_a_brief_ready_label_already_there_wakes_nobody(db):
    card = _card(db, assignee="@dev", labels=["brief-ready"])
    _edit(db, card, {"title": "Work, again"}, actor="agent:analyst")
    other = _card(db, assignee="@dev", status="To Do")
    _edit(db, other, {"labels_add": ["brief-ready"]}, actor="agent:analyst")  # not in Inbox: not a brief to start
    assert db.commands(card=card.id) == [] and [c["kind"] for c in db.commands(card=other.id)] == []


def test_a_continue_for_a_reassigned_card_is_stale(db):
    hosted = lambda agent: agent in HOSTED  # noqa: E731
    card = _card(db, assignee="@analyst", status="Review")
    _edit(db, card, {"status": "In Progress"})
    [command] = db.commands(card=card.id)
    assert commands.stale_reason(db, command, hosted) is None
    _edit(db, card, {"assignee": "@dev"})
    assert commands.stale_reason(db, command, hosted) == "reassigned"


def test_a_ready_brief_does_not_kick_watch(db):
    brief = {"kind": "brief", "id": "KD-1", "title": "t", "status": "Inbox", "assignees": ["boss"],
             "old_status": "Inbox", "old_assignees": [], "by": "agent:analyst"}
    assert board.coordinator_events([brief]) is False
