"""a person's message with files — in a card's thread and in an agent's chat; reading and deleting a file."""

import pytest

from kd import session_config
from kd.cabinet import api, users
from cabinet_env import CabinetEnv

HOSTED = session_config.HostConfig({"analyst": 1}, 600)


def person(user):
    return api.Principal("user", -1, user)


@pytest.fixture
def env(tmp_path):
    e = CabinetEnv(tmp_path)
    u = users.Users(e.store)
    u.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    u.create("anna", "Анна", roles=[("partner", ["web", "digipaddock"])], actor="user:owner")
    u.create("boris", "Борис", roles=[("partner", ["web", "digipaddock"])], actor="user:owner")
    u.create("vera", "Вера", roles=[("viewer", "*")], actor="user:owner")
    api.install_host(e.ctx, lambda: HOSTED)
    return e


def one(n, size=10, mime="image/png", name=None):
    return {"id": f"{n:032x}", "name": name or f"shot{n}.png", "mime": mime, "size": size, "sha256": "b" * 64}


def comment(env, user, card, text="", files=(), **extra):
    request = {"op": "user.card.comment", "card": card, "text": text, "files": list(files), **extra}
    return api.dispatch(env.ctx, person(user), request)["message"]


def chat(env, user, text="", files=(), agent="analyst"):
    request = {"op": "user.agent.message", "agent": agent, "text": text, "files": list(files)}
    return api.dispatch(env.ctx, person(user), request)["message"]


def last_event(env, kind="message.added"):
    return [e for e in env.store.events_since(0) if e["kind"] == kind][-1]


def test_text_and_files_are_one_message_with_attachment_rows(env):
    card = env.card(project="web", assignee="@dev", status="In Progress")
    message = comment(env, "anna", card.id, "Глянь скриншот", [one(1), one(2, mime="text/plain", name="a.log")])
    assert message["kind"] == "text" and message["body"] == "Глянь скриншот"
    assert message["payload"]["files"] == [
        {"id": f"{1:032x}", "name": "shot1.png", "mime": "image/png", "size": 10},
        {"id": f"{2:032x}", "name": "a.log", "mime": "text/plain", "size": 10},
    ]
    assert [env.store.attachment(f"{n:032x}")["seq"] for n in (1, 2)] == [message["seq"]] * 2
    assert env.store.files_size("user:anna") == 20
    # text with files wakes whom the same text would (_addressees): the card's agent
    assert last_event(env)["payload"]["mentions"] == ["@dev"]


def test_files_without_text_wake_nobody(env):
    card = env.card(project="web", assignee="@dev", status="In Progress")
    message = comment(env, "anna", card.id, "  ", [one(1)])
    assert message["kind"] == "file" and message["body"] == "shot1.png"
    assert "mentions" not in last_event(env)["payload"]
    named = comment(env, "owner", card.id, "@analyst посмотри", [one(2)])
    assert named["kind"] == "text" and last_event(env)["payload"]["mentions"] == ["@analyst"]
    # a partner names only the project's agent and the card's (review 04.11.2026): the text goes to the card's
    comment(env, "anna", card.id, "@analyst посмотри", [one(3)])
    assert last_event(env)["payload"]["mentions"] == ["@dev"]


def test_a_message_needs_text_or_a_file(env):
    card = env.card(project="web")
    with pytest.raises(api.ApiError) as exc:
        comment(env, "anna", card.id, " ", [])
    assert exc.value.kind == "invalid"


def test_file_and_message_limits(env):
    card = env.card(project="web")
    comment(env, "anna", card.id, "", [one(1, size=api.FILE_MAX)])  # 20 MB is taken
    for files in (
        [one(2, size=api.FILE_MAX + 1)],
        [one(n, size=api.FILE_MAX) for n in range(3, 8)] + [one(9, size=1)],  # 100 MB and a byte
        [one(n, size=1) for n in range(10, 11 + api.FILES_PER_MESSAGE)],
        [one(30), one(30)],  # the same id twice
        [{**one(31), "id": "../x"}],
        [{**one(32), "name": "a/b"}],
        [{**one(33), "sha256": "x"}],
    ):
        with pytest.raises(api.ApiError) as exc:
            comment(env, "anna", card.id, "x", files)
        assert exc.value.kind == "invalid", files
    assert api.MESSAGE_FILES_MAX == 100 * 1024 * 1024 and api.FILE_MAX == 20 * 1024 * 1024
    comment(env, "anna", card.id, "", [one(n, size=api.FILE_MAX) for n in range(40, 45)])  # 100 MB is taken


def test_quotas_count_the_whole_message(env, monkeypatch):
    card = env.card(project="web")
    monkeypatch.setattr(api, "FILES_USER_MAX", 25)
    with pytest.raises(api.ApiError) as exc:
        comment(env, "anna", card.id, "", [one(1), one(2), one(3)])
    assert (exc.value.kind, exc.value.reason) == ("refused", "files_quota")
    assert env.store.files_size() == 0 and len(env.store.messages(card.id)) == 0
    comment(env, "anna", card.id, "", [one(1), one(2)])
    monkeypatch.setattr(api, "FILES_USER_MAX", 10**9)
    monkeypatch.setattr(api, "FILES_TOTAL_MAX", 25)
    with pytest.raises(api.ApiError) as exc:
        comment(env, "boris", card.id, "", [one(4)])
    assert (exc.value.kind, exc.value.reason) == ("refused", "files_full") and "owner" in str(exc.value)


def test_files_in_the_agent_chat(env):
    message = chat(env, "owner", "Что на картинке?", [one(1)])
    assert message["kind"] == "text" and message["payload"]["files"][0]["id"] == f"{1:032x}"
    [command] = env.store.commands(agent="analyst")
    assert command["kind"] == "chat"
    quiet = chat(env, "owner", "", [one(2)])
    assert quiet["kind"] == "file" and len(env.store.commands(agent="analyst")) == 1  # a file alone wakes nobody
    found = api.dispatch(env.ctx, person("owner"), {"op": "file.get", "id": f"{2:032x}"})["file"]
    assert found["card"] == "" and found["agent"] == "analyst" and found["name"] == "shot2.png"
    with pytest.raises(api.ApiError) as exc:
        api.dispatch(env.ctx, person("vera"), {"op": "file.get", "id": f"{2:032x}"})
    assert exc.value.kind == "not_found"  # no agents.chat: the chat's files are not there for her


def test_old_file_messages_keep_reading(env):
    card = env.card(project="web")
    message = env.store.add_message(
        card.id, author="user:anna", kind="file", body="old.txt",
        payload={"id": "c" * 32, "name": "old.txt", "mime": "text/plain", "size": 3},
    )
    env.store._conn.execute(
        "INSERT INTO attachments (id, thread, seq, name, mime, size, sha256, created) VALUES (?,?,?,?,?,?,?,?)",
        ("c" * 32, card.thread, message["seq"], "old.txt", "text/plain", 3, "b" * 64, "2026-10-15T00:00:00Z"),
    )
    env.store._conn.commit()
    found = api.dispatch(env.ctx, person("anna"), {"op": "file.get", "id": "c" * 32})["file"]
    assert found["card"] == card.id and found["author"] == "user:anna"
    out = api.dispatch(env.ctx, person("anna"), {"op": "user.file.delete", "id": "c" * 32})["message"]
    assert out["payload"]["files"] == [{"id": "c" * 32, "name": "old.txt", "deleted": True}]


def test_the_author_or_the_owner_deletes_a_file(env):
    card = env.card(project="web")
    message = comment(env, "anna", card.id, "два файла", [one(1), one(2)])
    with pytest.raises(api.ApiError) as exc:
        api.dispatch(env.ctx, person("boris"), {"op": "user.file.delete", "id": f"{1:032x}"})
    assert exc.value.kind == "refused"
    out = api.dispatch(env.ctx, person("anna"), {"op": "user.file.delete", "id": f"{1:032x}"})
    assert out["message"]["seq"] == message["seq"] and out["message"]["body"] == "два файла"
    assert out["message"]["payload"]["files"][0] == {"id": f"{1:032x}", "name": "shot1.png", "deleted": True}
    assert out["message"]["payload"]["files"][1]["size"] == 10
    assert env.store.files_size("user:anna") == 10
    event = last_event(env, "message.changed")
    assert event["card"] == card.id and event["payload"]["seq"] == message["seq"]
    api.dispatch(env.ctx, person("owner"), {"op": "user.file.delete", "id": f"{2:032x}"})
    assert env.store.files_size() == 0
    for user in ("anna", "owner"):  # gone is gone
        with pytest.raises(api.ApiError) as exc:
            api.dispatch(env.ctx, person(user), {"op": "user.file.delete", "id": f"{2:032x}"})
        assert exc.value.kind == "not_found"
        with pytest.raises(api.ApiError) as exc:
            api.dispatch(env.ctx, person(user), {"op": "file.get", "id": f"{2:032x}"})
        assert exc.value.kind == "not_found"


def test_a_file_out_of_sight_is_not_found_for_delete(env):
    card = env.card(project="web")
    comment(env, "owner", card.id, "", [one(1)])
    # anna sees web and digipaddock; a card of admin is out of her sight
    secret = env.card(project="admin")
    comment(env, "owner", secret.id, "", [one(2)])
    with pytest.raises(api.ApiError) as exc:
        api.dispatch(env.ctx, person("anna"), {"op": "user.file.delete", "id": f"{2:032x}"})
    assert exc.value.kind == "not_found"
