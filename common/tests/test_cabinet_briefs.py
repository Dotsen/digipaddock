"""The brief as a versioned document (remediation spec R17): its versions and state come from facts — a
ready handoff records vN, a change of its scope makes it stale, an approved version never changes."""

import pytest

from kd.cabinet import briefs, commands, decisions, store

NOW = "2026-10-17T10:00:00Z"


@pytest.fixture
def db(tmp_path):
    s = store.Store.open(str(tmp_path / "cabinet.db"), prefix="KD", clock=lambda: NOW)
    commands.install(s, lambda agent: agent == "analyst")
    return s


def _draft(db, **extra):
    """A card the analyst is writing the brief of, in Inbox."""
    values = {"title": "Отчёт", "project": "digipaddock", "status": "Inbox", "assignee": "@analyst",
              "labels": ["spec:task-brief"], "created_by": "@owner", **extra}
    return db.create_card(values, actor="user:owner")


def _edit(db, card, changes, actor="agent:analyst"):
    return db.edit_card(card.id, db.card(card.id).revision, changes, actor=actor)


def _handoff(db, card, description="Цель: отчёт.", ac=("Есть отчёт", "Есть тест")):
    """As kd-task handoff writes it: the brief, the next assignee and spec, and brief-ready, in one write."""
    current = db.card(card.id)
    return _edit(db, card, {
        "description": description,
        "acceptance": [{"text": t, "checked": False} for t in ac],
        "assignee": "@dev",
        "labels_remove": ["spec:task-brief"],
        "labels_add": ["spec:implementer", "brief-ready"],
    } if current.assignee == "@analyst" else {"description": description, "labels_add": ["brief-ready"]})


def _kinds(db, start):
    return [e["kind"] for e in db.events_since(start)]


def test_a_handoff_with_brief_ready_records_version_1(db):
    card = _draft(db)
    start = db.last_seq()
    after = _handoff(db, card)
    [v1] = briefs.versions(db, card.id)
    assert (v1["version"], v1["origin"], v1["author"]) == (1, "agent", "agent:analyst")
    assert v1["description"] == "Цель: отчёт." and v1["acceptance"] == ["Есть отчёт", "Есть тест"]
    assert v1["scope_sha"] == briefs.scope_sha(briefs.scope(after)) and v1["approved_by"] == ""
    assert "brief.version" in _kinds(db, start)
    event = [e for e in db.events_since(start) if e["kind"] == "brief.version"][0]
    assert event["payload"] == {"version": 1, "origin": "agent"} and event["card"] == card.id
    assert briefs.state(db, after) == {
        "state": "ready", "version": 1, "approved": False, "legacy": False, "questions": 0, "error": "",
    }


def test_the_same_content_handed_again_adds_no_version(db):
    card = _draft(db)
    _handoff(db, card)
    _edit(db, card, {"labels_remove": ["brief-ready"]}, actor="user:owner")
    _edit(db, card, {"labels_add": ["brief-ready"]})
    assert [v["version"] for v in briefs.versions(db, card.id)] == [1]
    assert briefs.add_version(db, db.card(card.id), origin="person", author="user:owner") is None


def test_a_scope_edit_after_ready_is_stale_and_drops_the_label(db):
    card = _draft(db)
    _handoff(db, card)
    after = _edit(db, card, {"description": "Цель: отчёт и график."}, actor="user:owner")
    assert "brief-ready" not in after.labels
    assert briefs.state(db, after)["state"] == "stale"
    [v1] = briefs.versions(db, card.id)
    assert v1["description"] == "Цель: отчёт."
    changed = [e for e in db.events_since(0) if e["kind"] == "card.changed"][-1]
    assert "brief-ready" not in changed["payload"]["after"]["labels"]
    again = _edit(db, card, {"ac_add": ["Ещё критерий"]}, actor="user:owner")
    assert briefs.state(db, again)["state"] == "stale"


@pytest.mark.parametrize("changes", [
    {"ac_check": [1]}, {"notes_append": "заметка"}, {"comment": {"text": "ок", "author": "@owner"}},
    {"status": "To Do"}, {"title": "Отчёт за неделю"}, {"ac_uncheck": [1]},
], ids=["ac-tick", "notes", "comment", "status", "title", "ac-untick"])
def test_ticking_ac_notes_comments_status_and_title_keep_it_ready(db, changes):
    card = _draft(db)
    _handoff(db, card)
    if "ac_uncheck" in changes:
        _edit(db, card, {"ac_check": [1]}, actor="user:owner")
    after = _edit(db, card, changes, actor="user:owner")
    assert "brief-ready" in after.labels
    seen = briefs.state(db, after)
    assert (seen["state"], seen["version"], seen["legacy"]) == ("ready", 1, False)


def test_states_while_the_analyst_prepares(db):
    card = _draft(db)
    seen = briefs.state(db, card)  # nobody told the analyst: nothing is being prepared, a retry is the step
    assert (seen["state"], seen["error"]) == ("failed", "brief_not_started")
    command = db.enqueue({"key": "k1", "agent": "analyst", "thread": card.thread, "card": card.id,
                          "kind": "assigned", "text": f"[board] {card.id} assigned to you", "initiator": "user:owner"})
    assert briefs.state(db, db.card(card.id))["state"] == "queued"
    db.command_state(command["id"], "delivered")
    assert briefs.state(db, db.card(card.id))["state"] == "queued"
    db.command_state(command["id"], "started")
    assert briefs.state(db, db.card(card.id))["state"] == "in_progress"
    book = decisions.Decisions(db)
    for text in ("Как?", "Где?"):
        book.open("question", card=card.id, payload={"text": text}, requested_by="agent:analyst", now=NOW)
    seen = briefs.state(db, db.card(card.id))
    assert (seen["state"], seen["questions"]) == ("waiting_user", 2)
    for d in book.list(state="open", card=card.id):
        book.reply(d["id"], user="owner", text="так", now=NOW)
    newest = db.commands(thread=card.thread, agent="analyst", limit=1)[0]
    assert newest["kind"] == "answer"  # the answers wake the analyst: its newest command is queued again
    assert briefs.state(db, db.card(card.id))["state"] == "queued"
    db.command_state(newest["id"], "failed", error="spawn_failed")
    seen = briefs.state(db, db.card(card.id))
    assert (seen["state"], seen["error"]) == ("failed", "spawn_failed")


def test_a_finished_turn_without_a_brief_is_not_in_progress(db):
    card = _draft(db)
    command = db.enqueue({"key": "k1", "agent": "analyst", "thread": card.thread, "card": card.id,
                          "kind": "assigned", "text": f"[board] {card.id} assigned to you", "initiator": "user:owner"})
    db.command_state(command["id"], "delivered")
    db.command_state(command["id"], "started")
    session = db.session_for(card.thread, "analyst")
    db.session_update(session["id"], state="working")
    assert briefs.state(db, db.card(card.id))["state"] == "in_progress"
    db.command_state(command["id"], "done")
    assert briefs.state(db, db.card(card.id))["state"] == "in_progress"  # the session still works
    db.session_update(session["id"], state="idle")
    seen = briefs.state(db, db.card(card.id))  # the turn ended with neither a handoff nor a question
    assert (seen["state"], seen["error"]) == ("failed", "brief_ended")


def test_a_handoff_counts_once_toward_the_agents_write_limit(db):
    card = _draft(db)
    _handoff(db, card)
    assert db.count_events("agent:analyst", NOW) == 1  # the card's change; its brief version is not a write


def test_a_failed_session_is_a_failed_preparation(db):
    card = _draft(db)
    session = db.session_for(card.thread, "analyst")
    db.session_update(session["id"], state="failed", error="the turn crashed")
    seen = briefs.state(db, db.card(card.id))
    assert (seen["state"], seen["error"]) == ("failed", "the turn crashed")


def test_a_legacy_label_is_ready_but_not_approved(db):
    card = db.create_card({"title": "Старое", "project": "digipaddock", "status": "Inbox", "assignee": "@dev",
                           "labels": ["brief-ready"], "description": "ТЗ", "created_by": "@owner"}, actor="user:owner")
    assert briefs.versions(db, card.id) == []
    assert briefs.state(db, card) == {
        "state": "ready", "version": 0, "approved": False, "legacy": True, "questions": 0, "error": "",
    }
    plain = db.create_card({"title": "Нет ТЗ", "project": "digipaddock", "created_by": "@owner"}, actor="user:owner")
    assert briefs.state(db, plain)["state"] == "not_requested"


def test_approve_marks_the_latest_once(db):
    card = _draft(db)
    _handoff(db, card)
    start = db.last_seq()
    v1 = briefs.approve(db, db.card(card.id), by="user:owner")
    assert (v1["version"], v1["origin"], v1["approved_by"], v1["approved_at"]) == (1, "agent", "user:owner", NOW)
    assert _kinds(db, start).count("brief.approved") == 1
    again = briefs.approve(db, db.card(card.id), by="user:anna")
    assert again["approved_by"] == "user:owner" and _kinds(db, start).count("brief.approved") == 1
    assert briefs.state(db, db.card(card.id))["approved"] is True


def test_approve_a_legacy_card_records_an_approved_version(db):
    card = db.create_card({"title": "Старое", "project": "digipaddock", "status": "Inbox", "assignee": "@dev",
                           "labels": ["brief-ready"], "description": "ТЗ", "created_by": "@owner"}, actor="user:owner")
    v1 = briefs.approve(db, card, by="user:owner")
    assert (v1["version"], v1["origin"], v1["approved_by"], v1["description"]) == (1, "legacy", "user:owner", "ТЗ")
    plain = db.create_card({"title": "Нет ТЗ", "project": "digipaddock", "created_by": "@owner"}, actor="user:owner")
    assert briefs.approve(db, plain, by="user:owner") is None


def test_approve_a_stale_brief_raises(db):
    card = _draft(db)
    _handoff(db, card)
    _edit(db, card, {"description": "Другое"}, actor="user:owner")
    with pytest.raises(briefs.Stale):
        briefs.approve(db, db.card(card.id), by="user:owner")
    assert briefs.versions(db, card.id)[0]["approved_by"] == ""


def test_an_approved_version_never_changes(db):
    card = _draft(db)
    _handoff(db, card)
    briefs.approve(db, db.card(card.id), by="user:owner")
    after = _edit(db, card, {"description": "Цель: другое."}, actor="user:owner")
    assert briefs.state(db, after)["state"] == "stale"
    [v1] = briefs.versions(db, card.id)
    assert (v1["origin"], v1["description"], v1["approved_by"]) == ("agent", "Цель: отчёт.", "user:owner")


def test_diff_marks_added_removed_and_skips_unchanged_runs():
    old = {"description": "\n".join(f"строка {i}" for i in range(1, 9)), "acceptance": ["А", "Б"]}
    new = {"description": old["description"].replace("строка 5", "строка пять"), "acceptance": ["А", "В"]}
    assert briefs.render(old).split("\n")[-3:] == ["", "- А", "- Б"]
    assert briefs.diff(old, new) == [
        {"op": "…", "text": "2"},
        {"op": "=", "text": "строка 3"},
        {"op": "=", "text": "строка 4"},
        {"op": "-", "text": "строка 5"},
        {"op": "+", "text": "строка пять"},
        {"op": "=", "text": "строка 6"},
        {"op": "=", "text": "строка 7"},
        {"op": "=", "text": "строка 8"},
        {"op": "=", "text": ""},
        {"op": "=", "text": "- А"},
        {"op": "-", "text": "- Б"},
        {"op": "+", "text": "- В"},
    ]
    assert briefs.diff(old, old) == [{"op": "…", "text": "11"}]


def test_brief_events_queue_no_commands(db):
    card = _draft(db, assignee="@dev", labels=[])
    _edit(db, card, {"description": "ТЗ", "labels_add": ["brief-ready"]}, actor="user:owner")
    briefs.approve(db, db.card(card.id), by="user:owner")
    kinds = {e["kind"] for e in db.events_since(0)}
    assert {"brief.version", "brief.approved"} <= kinds
    assert db.commands(card=card.id) == []
