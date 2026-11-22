"""BOSS looks before it creates; the cabinet checks the look again inside the create."""

import pytest
from cabinet_env import CabinetEnv

from kd.cabinet import api


@pytest.fixture
def env(tmp_path):
    return CabinetEnv(tmp_path)


def _look(env, intent, project="digipaddock"):
    return env.call("digi", {"op": "boss.prepare-create", "agent": "boss", "intent": intent, "project": project})


def _create(env, look, title, **over):
    request = {"op": "boss.create-inbox", "agent": "boss", "reason": "по просьбе владельца", "title": title,
               "project": "digipaddock", "token": look["token"], "reviewed": [c["id"] for c in look["candidates"]],
               "difference": "другое", **over}
    return env.call("digi", request)


def test_candidates_share_words_any_status_and_the_basis_stays_out_of_the_card(env):
    voice = env.card(title="Голосовые сообщения", status="Done")
    env.card(title="Монитор серверов")
    look = _look(env, "голосовых сообщений в чате нет — сломалось?")
    assert [c["id"] for c in look["candidates"]] == [voice.id]  # a Done card: maybe a regression of it
    made = _create(env, look, "Голосовые сломались в чате", difference="регрессия KD-готовой функции")["card"]
    # the duplicate check goes to the audit journal, the owner reads only the reason
    assert made["status"] == "Inbox" and voice.id not in made["description"]


def test_no_token_or_unread_candidates_refuse(env):
    voice = env.card(title="Голосовые")
    look = _look(env, "голосовые")
    with pytest.raises(api.ApiError) as exc:
        _create(env, look, "Голос", reviewed=[])
    assert voice.id in str(exc.value)
    with pytest.raises(api.ApiError):
        _create(env, look, "Голос", difference="")
    with pytest.raises(api.ApiError):
        _create(env, {"token": "0" * 32, "candidates": []}, "Голос")


def test_a_changed_candidate_needs_a_new_look_a_note_does_not(env):
    voice = env.card(title="Голосовые")
    look = _look(env, "голосовые")
    env.store.edit_card(voice.id, None, {"notes_append": "заметка"}, actor="agent:dev")  # not what makes it similar
    env.store.edit_card(voice.id, None, {"description": "теперь и про файлы"}, actor="user:owner")
    with pytest.raises(api.ApiError) as exc:
        _create(env, look, "Вложения")
    assert exc.value.kind == "conflict"
    look = _look(env, "голосовые")
    env.store.edit_card(voice.id, None, {"notes_append": "ещё заметка"}, actor="agent:dev")
    assert _create(env, look, "Вложения")["card"]["title"] == "Вложения"


def test_a_repeat_or_a_parallel_create_of_the_same_intent_makes_one_card(env):
    first, second = _look(env, "отчёт по расходам"), _look(env, "отчёт по расходам")
    made = _create(env, first, "Отчёт по расходам")["card"]
    assert _create(env, first, "Отчёт по расходам")["card"]["id"] == made["id"]
    twin = _create(env, second, "Отчёт о расходах")
    assert twin["card"]["id"] == made["id"] and twin["existing"]


def test_a_card_of_the_same_words_long_ago_or_closed_since_is_no_twin(env):
    old = _create(env, _look(env, "отчёт по расходам"), "Отчёт по расходам")["card"]
    env.store.edit_card(old["id"], None, {"status": "Done"}, actor="user:owner")
    env.now = "2026-10-20T10:00:00Z"  # days later: the same words are a new need
    made = _create(env, _look(env, "отчёт по расходам"), "Отчёт по расходам за октябрь",
                   difference="прошлый отчёт сдан")
    assert made["card"]["id"] != old["id"] and not made.get("existing")


def test_an_open_card_of_the_same_title_is_returned_whatever_the_intent(env):
    open_ = env.card(title="Расходы", status="Inbox")
    found = _create(env, _look(env, "нужна сводка трат за месяц"), "Расходы")
    assert found["card"]["id"] == open_.id and found["existing"]


def test_request_brief_drops_a_ready_brief_and_another_spec_and_refuses_outside_inbox(env):
    card = env.card(status="Inbox", assignee="@dev", labels=["brief-ready", "spec:implementer"])
    request = {"op": "boss.request-brief", "agent": "boss", "task": card.id, "comment": "переписать: офлайн"}
    after = env.call("digi", {**request, "revision": card.revision})["card"]
    assert after["assignee"] == "@analyst" and after["labels"] == ["spec:task-brief"]
    with pytest.raises(api.ApiError) as exc:
        env.call("digi", {**request, "revision": card.revision})
    assert exc.value.kind == "conflict"
    started = env.card(status="To Do", assignee="@dev")
    with pytest.raises(api.ApiError) as exc:
        env.call("digi", {**request, "task": started.id, "revision": started.revision})
    assert exc.value.kind == "refused"


def test_an_open_card_of_the_same_title_is_returned(env):
    open_ = env.card(title="Отчёт по расходам", status="Inbox")
    look = _look(env, "отчёт по расходам")
    found = _create(env, look, "Отчёт  по РАСХОДАМ")
    assert found["card"]["id"] == open_.id and found["existing"]


def test_an_expired_or_foreign_project_token_refuses(env):
    look = _look(env, "что-то новое")
    with pytest.raises(api.ApiError):
        _create(env, look, "Что-то", project="admin")
    env.now = "2026-10-14T11:00:00Z"
    with pytest.raises(api.ApiError) as exc:
        _create(env, look, "Что-то")
    assert "expired" in str(exc.value)


def test_v16_base_migrates_to_v17_with_create_tokens(tmp_path):
    import sqlite3

    from kd.cabinet import schema, store

    path = str(tmp_path / "v16.db")
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(schema.DDL + "".join(schema.MIGRATIONS[v] for v in range(2, 17)))
    conn.execute("INSERT INTO meta VALUES ('schema_version', '16')")
    conn.execute("INSERT INTO meta VALUES ('next_card', '1')")
    conn.close()
    db = store.Store.open(path)
    assert db.schema_version() == schema.SCHEMA_VERSION
    assert {r[1] for r in db._conn.execute("PRAGMA table_info(create_tokens)")} >= {"token", "intent", "card"}


def test_only_boss_looks(env):
    with pytest.raises(api.ApiError):
        env.call("digi", {"op": "boss.prepare-create", "agent": "dev", "intent": "x", "project": "digipaddock"})
