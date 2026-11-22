import sqlite3

import pytest
from kd.cabinet import schema, store

NOW = "2026-10-17T10:00:00Z"


@pytest.fixture
def db(tmp_path):
    return store.Store.open(str(tmp_path / "cabinet.db"), prefix="KD", clock=lambda: NOW)


def _card(db, **extra):
    return db.create_card(
        {"title": "Work", "project": "digipaddock", "status": "Inbox", "created_by": "@owner", **extra},
        actor="user:owner",
    )


def _spec(db, card, **extra):
    return {
        "key": "k1",
        "agent": "analyst",
        "thread": card.thread,
        "card": card.id,
        "kind": "assigned",
        "text": f"[board] {card.id} assigned to you",
        "initiator": "user:owner",
        "event": 1,
        **extra,
    }


def _kinds(db, after=0):
    return [e["kind"] for e in db.events_since(after)]


def test_schema_v4_migrates_v3_base(tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(schema.DDL + schema.MIGRATIONS[2] + schema.MIGRATIONS[3])
    conn.execute("INSERT INTO meta VALUES ('schema_version', '3')")
    conn.execute("INSERT INTO meta VALUES ('next_card', '2')")
    conn.execute(
        "INSERT INTO cards (id, n, project, title, status, created_by, revision, created, updated)"
        " VALUES ('KD-1', 1, 'digipaddock', 'Old', 'To Do', '@owner', 3, 't', 't')"
    )
    conn.execute("INSERT INTO threads (kind, card) VALUES ('card', 'KD-1')")
    conn.execute(
        "INSERT INTO messages (thread, seq, author, kind, body, created) VALUES (1, 1, 'user:owner', 'text', 'hi', 't')"
    )
    conn.close()
    db = store.Store.open(path)
    assert db.schema_version() == schema.SCHEMA_VERSION
    assert db.card("KD-1").revision == 3 and db.messages("KD-1")[0]["body"] == "hi"
    tables = {r[0] for r in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"commands", "sessions"} <= tables


def test_nested_transaction_commits_once(db):
    card = _card(db)
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.add_message(card.id, author="user:owner", kind="text", body="inner")
            raise RuntimeError("outer fails after the inner write")
    assert db.messages(card.id) == []
    with db.transaction():
        with db.transaction():
            db.add_message(card.id, author="user:owner", kind="text", body="kept")
    assert [m["body"] for m in db.messages(card.id)] == ["kept"]


def test_inner_failure_caught_by_outer_rolls_back_only_inner(db):
    card = _card(db)
    with db.transaction():
        db.add_message(card.id, author="user:owner", kind="text", body="outer")
        with pytest.raises(store.Invalid):
            with db.transaction():
                db.add_message(card.id, author="user:owner", kind="text", body="inner")
                raise store.Invalid("inner refused")
    assert [m["body"] for m in db.messages(card.id)] == ["outer"]


def test_on_event_runs_inside_the_write(db):
    card = _card(db)

    def hook(s, event):
        if event["kind"] == "card.changed":
            s._enqueue_in_tx(_spec(s, card, event=event["seq"]))

    db.on_event = hook
    db.edit_card(card.id, card.revision, {"assignee": "@analyst"}, actor="user:owner")
    [command] = db.commands(card=card.id)
    assert command["state"] == "queued" and command["event"] == db.events_since(0)[-2]["seq"]
    assert _kinds(db)[-2:] == ["card.changed", "command.queued"]

    def broken(s, event):
        raise RuntimeError("hook fails")

    db.on_event = broken
    before = db.card(card.id)
    with pytest.raises(RuntimeError):
        db.edit_card(card.id, before.revision, {"assignee": "@dev"}, actor="user:owner")
    assert db.card(card.id).assignee == "@analyst" and len(db.commands(card=card.id)) == 1
    db.on_event = None
    assert db.edit_card(card.id, before.revision, {"assignee": "@dev"}, actor="user:owner").assignee == "@dev"


def test_a_repeated_key_with_the_same_content_is_the_old_command(db):
    card = _card(db)
    first = db.enqueue(_spec(db, card))
    db.command_state(first["id"], "delivered", session="s1")
    db.command_state(first["id"], "started")
    again = db.enqueue(_spec(db, card, initiator="root", event=9))
    assert again["id"] == first["id"] and len(db.commands(card=card.id)) == 1
    assert first["state"] == "queued" and first["next_try"] == NOW and first["attempts"] == 0


@pytest.mark.parametrize("change", ["text", "agent", "kind", "thread"])
def test_a_key_reused_with_other_content_is_a_conflict(db, change):
    card = _card(db)
    db.enqueue(_spec(db, card))
    other = {
        "text": {"text": "another text"},
        "agent": {"agent": "dev"},
        "kind": {"kind": "signal"},
        "thread": {"thread": db.agent_thread("analyst"), "card": ""},
    }[change]
    with pytest.raises(store.Conflict, match="in use with other content"):
        db.enqueue({**_spec(db, card), **other})
    assert len(db.commands()) == 1


def test_queued_commands_do_not_count_against_the_agents_write_limit(db):
    card = _card(db)
    command = db.enqueue(_spec(db, card, initiator="agent:dev"))
    db.command_state(command["id"], "delivered", session="s1", actor="agent:dev")
    session = db.session_for(card.thread, "analyst")
    db.session_update(session["id"], state="working", actor="agent:dev")
    assert db.count_events("agent:dev", "2026-10-14T00:00:00Z") == 0
    db.add_message(card.id, author="agent:dev", kind="progress", body="шаг")
    assert db.count_events("agent:dev", "2026-10-14T00:00:00Z") == 1


def test_enqueue_coalesces_same_text_while_not_started(db):
    card = _card(db)
    first = db.enqueue(_spec(db, card))
    assert db.enqueue(_spec(db, card, key="k2"))["id"] == first["id"]
    db.command_state(first["id"], "delivered", session="s1")
    assert db.enqueue(_spec(db, card, key="k3"))["id"] == first["id"]
    db.command_state(first["id"], "started")
    later = db.enqueue(_spec(db, card, key="k4"))
    assert later["id"] != first["id"] and len(db.commands(card=card.id)) == 2


def test_enqueue_rejects_unknown_kind_and_missing_key(db):
    card = _card(db)
    with pytest.raises(store.Invalid):
        db.enqueue(_spec(db, card, kind="reboot"))
    with pytest.raises(store.Invalid):
        db.enqueue(_spec(db, card, key=""))


def test_lease_takes_due_and_unleased_only(db):
    card = _card(db)
    a = db.enqueue(_spec(db, card))
    b = db.enqueue(_spec(db, card, key="k2", text="later"))
    db.command_state(b["id"], "queued", next_try="2026-10-17T11:00:00Z", error="host_unavailable")
    leased = db.lease_commands(NOW, "2026-10-17T10:01:00Z", 10)
    assert [c["id"] for c in leased] == [a["id"]] and leased[0]["attempts"] == 1
    assert db.lease_commands(NOW, "2026-10-17T10:01:00Z", 10) == []
    after_lease = db.lease_commands("2026-10-17T10:02:00Z", "2026-10-17T10:03:00Z", 10)
    assert [c["id"] for c in after_lease] == [a["id"]] and after_lease[0]["attempts"] == 2
    later = db.lease_commands("2026-10-17T11:00:00Z", "2026-10-17T11:01:00Z", 10)
    assert [c["id"] for c in later] == [a["id"], b["id"]]  # a's lease ran out, b is due


def test_command_moves_and_events(db):
    card = _card(db)
    c = db.enqueue(_spec(db, card))
    seq = db.last_seq()
    db.command_state(c["id"], "delivered", session="s1")
    db.command_state(c["id"], "started")
    done = db.command_state(c["id"], "done")
    assert done["state"] == "done" and done["session"] == "s1"
    events = db.events_since(seq)
    assert [e["kind"] for e in events] == ["command.delivered", "run.started", "run.finished"]
    assert all(e["card"] == card.id for e in events)
    assert events[0]["payload"] == {"command": c["id"], "agent": "analyst", "kind": "assigned", "session": "s1"}
    with pytest.raises(store.Invalid):
        db.command_state(c["id"], "started")
    f = db.enqueue(_spec(db, card, key="k2", text="other"))
    db.command_state(f["id"], "delivered", session="s1")
    db.command_state(f["id"], "started")
    failed = db.command_state(f["id"], "failed", error="exit 1")
    assert failed["error"] == "exit 1" and db.events_since(0)[-1]["kind"] == "run.failed"
    x = db.enqueue(_spec(db, card, key="k3", text="third"))
    db.command_state(x["id"], "cancelled", reason="card_done")
    assert db.events_since(0)[-1]["kind"] == "command.cancelled"
    assert db.events_since(0)[-1]["payload"]["reason"] == "card_done"


def test_requeue_after_delivery_and_stop_done(db):
    card = _card(db)
    c = db.enqueue(_spec(db, card))
    db.command_state(c["id"], "delivered", session="s1")
    back = db.command_state(c["id"], "queued", next_try=NOW)
    assert back["state"] == "queued" and back["lease_until"] == ""
    assert db.events_since(0)[-1]["kind"] == "command.queued"
    stop = db.enqueue(_spec(db, card, key="k2", kind="stop", text="stop"))
    db.command_state(stop["id"], "delivered", session="s1")
    assert db.command_state(stop["id"], "done")["state"] == "done"
    assert db.events_since(0)[-1]["kind"] == "command.done"
    other = db.enqueue(_spec(db, card, key="k3", text="third"))
    db.command_state(other["id"], "delivered", session="s1")
    with pytest.raises(store.Invalid):
        db.command_state(other["id"], "done")  # only a stop is done without a run


def test_agent_thread_once_and_messages(db):
    thread = db.agent_thread("analyst")
    assert db.agent_thread("analyst") == thread
    assert db.thread_info(thread) == {"id": thread, "kind": "agent_chat", "card": "", "agent": "analyst"}
    message = db.add_thread_message(thread, author="user:owner", kind="text", body="hello")
    assert message["seq"] == 1 and message["body"] == "hello"
    event = db.events_since(0)[-1]
    assert event["kind"] == "message.added" and event["card"] == ""
    assert event["payload"]["agent"] == "analyst"
    assert [m["body"] for m in db.thread_messages(thread)] == ["hello"]
    with pytest.raises(store.NotFound):
        db.thread_info(9999)


def test_card_thread_message_still_by_card(db):
    card = _card(db)
    message = db.add_thread_message(card.thread, author="agent:analyst", kind="text", body="x", session="s1")
    assert message["seq"] == 1
    assert db.events_since(0)[-1]["card"] == card.id
    assert db.thread_info(card.thread) == {"id": card.thread, "kind": "card", "card": card.id, "agent": ""}


def test_session_for_unique_per_thread_agent(db):
    card = _card(db)
    s = db.session_for(card.thread, "analyst")
    assert s["state"] == "new" and len(s["id"]) == 32
    assert db.session_for(card.thread, "analyst")["id"] == s["id"]
    assert db.session_for(card.thread, "dev")["id"] != s["id"]
    assert [x["id"] for x in db.sessions(thread=card.thread, agent="analyst")] == [s["id"]]


def test_session_update_event_only_on_state_or_error(db):
    card = _card(db)
    s = db.session_for(card.thread, "analyst")
    seq = db.last_seq()
    db.session_update(s["id"], state="starting", slot="analyst/1")
    db.session_update(s["id"], heartbeat="2026-10-17T10:00:05Z")
    db.session_update(s["id"], runtime_session_id="0b9c3f1e-1111-2222-3333-444455556666", usage={"in": 3})
    db.session_update(s["id"], state="failed", error="exit 1")
    events = db.events_since(seq)
    assert [e["kind"] for e in events] == ["session.changed", "session.changed"]
    assert events[1]["payload"] == {
        "session": s["id"],
        "agent": "analyst",
        "state": "failed",
        "was": "starting",
        "error": "exit 1",
    }
    assert events[1]["card"] == card.id
    row = db.session(s["id"])
    assert row["usage"] == {"in": 3} and row["slot"] == "analyst/1" and row["heartbeat"] == "2026-10-17T10:00:05Z"
    with pytest.raises(store.Invalid):
        db.session_update(s["id"], state="flying")
    with pytest.raises(store.Invalid):
        db.session_update(s["id"], agent="dev")


def test_command_basis_round_trips(db):
    """what a continuation was queued against comes back as it went in; the key stays the identity."""
    card = _card(db)
    first = db.enqueue(_spec(db, card, basis={"scope": "ab", "assignee": "@analyst"}))
    assert first["basis"] == {"scope": "ab", "assignee": "@analyst"}
    assert db.enqueue(_spec(db, card, basis={"scope": "cd"}))["id"] == first["id"]
    assert db.commands(card=card.id)[0]["basis"] == first["basis"]
    plain = db.enqueue(_spec(db, card, key="k2", text="other"))
    assert plain["basis"] == {}
