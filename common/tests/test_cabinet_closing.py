"""A card closed without the work — Cancelled with its outcome, never a false Done."""

import sqlite3

import pytest
from cabinet_env import CabinetEnv

from kd import attention_rules
from kd.cabinet import api, flow, schema, store, waits


@pytest.fixture
def env(tmp_path):
    env = CabinetEnv(tmp_path)
    env.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    env.users.create("vera", "Вера", roles=[("viewer", "*")], actor="root")
    return env


def _dup(env, title="Голосовые ещё раз", **over):
    return env.card(status="Inbox", assignee="", title=title, **{"created_by": "@boss", **over})


def _close_dup(env, dup, target, **over):
    request = {"op": "boss.close-duplicate", "agent": "boss", "task": dup.id, "revision": dup.revision,
               "canonical": target.id, "canonical_revision": target.revision,
               "reason": "та же потребность", "wishes": "выбор движка распознавания", **over}
    return env.call("digi", request)


def test_a_duplicate_closes_into_cancelled_and_its_wishes_reach_the_canonical_card(env):
    canonical = env.card(title="Голосовые сообщения", status="Inbox")
    dup = _dup(env)
    found = _close_dup(env, dup, canonical)["card"]
    after = env.store.card(dup.id)
    assert (after.status, after.outcome, after.canonical) == ("Cancelled", "duplicate", canonical.id)
    assert found["status"] == "Cancelled" and after.terminal
    note = env.store.messages(canonical.id)[-1]
    assert note["author"] == "agent:boss" and dup.id in note["body"] and "выбор движка" in note["body"]
    assert env.store.card(canonical.id).description == canonical.description  # its brief is not rewritten
    assert "дубль" in env.store.messages(dup.id)[-1]["body"]


@pytest.mark.parametrize("why", ["self", "cancelled_canonical", "stale", "stale_canonical", "not_inbox"])
def test_a_duplicate_close_refuses_what_is_not_a_safe_inbox_duplicate(env, why):
    canonical = env.card(title="Канон", status="Inbox")
    dup = _dup(env)
    over = {}
    if why == "self":
        over = {"canonical": dup.id, "canonical_revision": dup.revision}
    elif why == "cancelled_canonical":
        other = _dup(env, title="Другой")
        _close_dup(env, other, canonical)
        over = {"canonical": other.id, "canonical_revision": env.store.card(other.id).revision}
    elif why == "stale":
        over = {"revision": dup.revision - 1}
    elif why == "stale_canonical":
        over = {"canonical_revision": canonical.revision + 5}
    elif why == "not_inbox":
        env.store.edit_card(dup.id, None, {"status": "To Do"}, actor="user:owner")
        over = {"revision": env.store.card(dup.id).revision}
    with pytest.raises(api.ApiError) as exc:
        _close_dup(env, dup, canonical, **over)
    assert exc.value.kind in ("refused", "conflict")
    assert env.store.card(dup.id).status != "Cancelled"


def test_work_begun_or_an_open_pr_or_decision_refuses_the_close(env):
    canonical = env.card(title="Канон", status="Inbox")
    dup = _dup(env)
    thread = dup.thread
    command = env.store.enqueue({"key": "k1", "agent": "analyst", "thread": thread, "card": dup.id,
                                 "kind": "message", "text": "x", "initiator": "user:owner"})
    env.store.command_state(command["id"], "delivered")
    with pytest.raises(api.ApiError) as exc:
        _close_dup(env, dup, canonical)
    assert "work has begun" in str(exc.value)


def test_queued_commands_are_cancelled_with_the_close(env):
    canonical = env.card(title="Канон", status="Inbox")
    dup = _dup(env)
    command = env.store.enqueue({"key": "k2", "agent": "analyst", "thread": dup.thread, "card": dup.id,
                                 "kind": "message", "text": "x", "initiator": "user:owner"})
    _close_dup(env, dup, env.store.card(canonical.id))
    assert env.store.command(command["id"])["state"] == "cancelled"


def test_cancelled_needs_an_outcome_and_a_duplicate_its_canonical(env):
    card = env.card(status="Inbox")
    for changes in ({"status": "Cancelled"}, {"status": "Cancelled", "outcome": "duplicate"},
                    {"status": "Cancelled", "outcome": "cancelled", "canonical": "KD-99"},
                    {"outcome": "cancelled"}):
        with pytest.raises(store.Invalid):
            env.store.edit_card(card.id, None, changes, actor="user:owner")
    with pytest.raises(store.Invalid):
        env.store.create_card({"title": "x", "project": "digipaddock", "status": "Cancelled", "created_by": "@owner"},
                              actor="user:owner")


def test_cancelled_satisfies_no_dependency(env):
    canonical = env.card(title="Канон", status="Inbox")
    dup = _dup(env)
    waiting = env.card(status="Blocked", assignee="@dev")
    env.store.edit_card(waiting.id, None, {"dependencies": [dup.id]}, actor="user:owner")
    _close_dup(env, dup, canonical)
    assert flow.open_dependencies(env.ctx, env.store.card(waiting.id)) == [dup.id]
    # a recorded wait for it needs routing (BOSS moves the dependency to the canonical card), never "resolved"
    assert waits.source_state(env.ctx, {"kind": "card", "ref": dup.id}) == "needs_routing"
    board = {dup.id: {"status": "Cancelled"}, waiting.id: {"status": "Blocked", "dependencies": [dup.id]}}
    diagnostics = []
    ctx = type("Ctx", (), {"cards": board, "note": lambda self, *a: diagnostics.append(a),
                           "emit": lambda self, *a: diagnostics.append(("emit",) + a)})()
    attention_rules._dependency_done(ctx, waiting.id, board[waiting.id])
    assert diagnostics == [("dependency_cancelled", waiting.id, "dependency_done")]


def test_the_owners_word_on_the_card_cancels_it_and_a_retelling_does_not(env):
    card = env.card(status="Inbox", title="Не нужна")
    seq = env.store.add_message(card.id, author="user:owner", kind="text", body="не нужно, закрой")["seq"]
    told = env.store.add_message(card.id, author="agent:boss", kind="text", body="владелец сказал закрыть")["seq"]
    request = {"op": "boss.close-cancelled", "agent": "boss", "task": card.id, "reason": "отменена"}
    with pytest.raises(api.ApiError):
        env.call("digi", {**request, "revision": card.revision, "source": {"card": card.id, "seq": told}})
    with pytest.raises(api.ApiError):  # a viewer's word is no authority
        vseq = env.store.add_message(card.id, author="user:vera", kind="text", body="закройте")["seq"]
        env.call("digi", {**request, "revision": card.revision, "source": {"card": card.id, "seq": vseq}})
    found = env.call("digi", {**request, "revision": env.store.card(card.id).revision,
                             "source": {"card": card.id, "seq": seq}})["card"]
    assert (found["status"], found["outcome"]) == ("Cancelled", "cancelled")


def test_a_chat_word_must_name_the_card(env):
    card = env.card(status="Inbox", title="Не нужна")
    chat = env.store.agent_thread("boss")
    other = env.store.add_thread_message(chat, author="user:owner", kind="text", body="закрой лишнее")["seq"]
    named = env.store.add_thread_message(chat, author="user:owner", kind="text", body=f"{card.id} не нужна")["seq"]
    request = {"op": "boss.close-cancelled", "agent": "boss", "task": card.id, "revision": card.revision,
               "reason": "по слову владельца"}
    with pytest.raises(api.ApiError):
        env.call("digi", {**request, "source": {"chat": other}})
    assert env.call("digi", {**request, "source": {"chat": named}})["card"]["status"] == "Cancelled"


def test_a_chat_word_about_another_card_with_a_longer_number_is_no_word(env):
    card = env.card(status="Inbox", title="Не нужна")
    chat = env.store.agent_thread("boss")
    longer = env.store.add_thread_message(chat, author="user:owner", kind="text", body=f"{card.id}0 не нужна")["seq"]
    with pytest.raises(api.ApiError):
        env.call("digi", {"op": "boss.close-cancelled", "agent": "boss", "task": card.id, "revision": card.revision,
                         "reason": "x", "source": {"chat": longer}})


def test_the_cabinets_note_of_an_owners_action_is_no_word(env):
    card = env.card(status="Inbox", title="Карточка")
    env.store.edit_card(card.id, None, {"priority": "high", "comment": {"text": "[owner] вручную", "author": "@owner"}},
                        actor="user:owner")
    seq = env.store.messages(card.id)[-1]["seq"]
    with pytest.raises(api.ApiError) as exc:
        env.call("digi", {"op": "boss.close-cancelled", "agent": "boss", "task": card.id,
                         "revision": env.store.card(card.id).revision, "reason": "x",
                         "source": {"card": card.id, "seq": seq}})
    assert "note of an action" in str(exc.value)


def test_a_command_on_its_way_to_the_host_refuses_the_close(env):
    canonical = env.card(title="Канон", status="Inbox")
    dup = _dup(env)
    command = env.store.enqueue({"key": "k3", "agent": "analyst", "thread": dup.thread, "card": dup.id,
                                 "kind": "message", "text": "x", "initiator": "user:owner"})
    env.store._conn.execute("UPDATE commands SET lease_until='2999-01-01T00:00:00Z' WHERE id=?", (command["id"],))
    with pytest.raises(api.ApiError) as exc:
        _close_dup(env, dup, canonical)
    assert "being delivered" in str(exc.value)


def test_own_mistake_only_on_bosss_own_untouched_card(env):
    own = _dup(env, title="Ошибочная")
    theirs = env.card(status="Inbox", title="Чужая", created_by="@owner")
    request = {"op": "boss.close-cancelled", "agent": "boss", "reason": "создал по ошибке", "source": "own"}
    with pytest.raises(api.ApiError):
        env.call("digi", {**request, "task": theirs.id, "revision": theirs.revision})
    env.store.add_message(own.id, author="user:owner", kind="text", body="а мне нужна")
    with pytest.raises(api.ApiError):
        env.call("digi", {**request, "task": own.id, "revision": env.store.card(own.id).revision})
    fresh = _dup(env, title="Ещё одна ошибочная")
    assert env.call("digi", {**request, "task": fresh.id, "revision": fresh.revision})["card"]["status"] == "Cancelled"


def test_only_boss_closes(env):
    card = env.card(status="Inbox")
    with pytest.raises(api.ApiError) as exc:
        env.call("digi", {"op": "boss.close-cancelled", "agent": "dev", "task": card.id, "revision": card.revision,
                         "reason": "x", "source": "own"})
    assert exc.value.kind == "refused"


def test_the_owner_restores_a_closed_card_to_inbox_clearing_its_outcome(env):
    canonical = env.card(title="Канон", status="Inbox")
    dup = _dup(env)
    _close_dup(env, dup, canonical)
    closed = env.store.card(dup.id)
    owner = api.Principal("user", -1, "owner")
    assert [a["id"] for a in flow.actions(env.ctx, owner, closed)] == ["restore"]
    found = api.dispatch(env.ctx, owner, {"op": "user.card.act", "card": dup.id, "action": "restore",
                                         "revision": closed.revision})
    after = env.store.card(dup.id)
    assert (after.status, after.outcome, after.canonical) == ("Inbox", "", "")
    assert found


def test_v14_base_migrates_to_v15(tmp_path):
    path = str(tmp_path / "v14.db")
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(schema.DDL + "".join(schema.MIGRATIONS[v] for v in range(2, 15)))
    conn.execute("INSERT INTO meta VALUES ('schema_version', '14')")
    conn.execute("INSERT INTO meta VALUES ('next_card', '2')")
    conn.execute("INSERT INTO cards (id, n, project, title, status, created_by, revision, created, updated)"
                 " VALUES ('KD-1', 1, 'digipaddock', 'Old', 'Done', '@owner', 3, 't', 't')")
    conn.execute("INSERT INTO threads (kind, card) VALUES ('card', 'KD-1')")
    conn.close()
    db = store.Store.open(path)
    assert db.schema_version() == schema.SCHEMA_VERSION
    old = db.card("KD-1")
    assert (old.status, old.outcome, old.canonical) == ("Done", "", "")  # an old Done keeps its meaning
