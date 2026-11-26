"""read cursors of agent chats — the replies a person has not been shown, per person and chat."""

import sqlite3

import pytest
from web_env import WebEnv

from kd.cabinet import api, schema, store

TURN_A, TURN_B = "a" * 32, "b" * 32


@pytest.fixture
def w(tmp_path):
    w = WebEnv(tmp_path)
    w.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    w.users.set_role("talker", ["cards.read", "agents.chat"], actor="root")  # no default role but the owner's has it
    w.users.create("petr", "Пётр", roles=[("talker", "*")], actor="root")
    w.users.create("vera", "Вера", roles=[("viewer", "*")], actor="root")
    return w


def _say(w, agent="boss", body="Ответ", kind="text", command=None, author=None):
    thread = w.env.store.agent_thread(agent)
    payload = {"command": command} if command else {}
    message = w.env.store.add_thread_message(
        thread, author=author or f"agent:{agent}", kind=kind, body=body, payload=payload,
        via="session" if command else None,
    )
    return message["seq"]


def _as(user, request, w):
    return api.dispatch(w.env.ctx, api.Principal("user", -1, user), request)


def _unread(w, user="owner"):
    return _as(user, {"op": "user.chat.unread"}, w)["chats"]


def _read(w, seq, user="owner", agent="boss"):
    return _as(user, {"op": "user.chat.read", "agent": agent, "seq": seq}, w)


def test_only_agent_texts_count_and_one_turn_is_one_reply(w):
    w.env.store.agent_thread("boss")
    _say(w, author="user:owner", body="Вопрос")  # their own message
    first = _say(w, body="Смотрю", command=TURN_A)
    _say(w, kind="tool_call", body="kd-task list", command=TURN_A)
    _say(w, kind="tool_result", body="", command=TURN_A)
    _say(w, body="Готово: KD-9032", command=TURN_A)  # the same turn: still one reply
    _say(w, body="Отдельный пост")  # kd-task chat --post: a reply of its own
    _say(w, author="system", kind="status", body="the session failed")
    state = _unread(w)["boss"]
    assert state["unread"] == 2 and state["first"] == first and state["read"] == 0
    _say(w, body="Новый ход", command=TURN_B)
    assert _unread(w)["boss"]["unread"] == 3


def test_read_moves_up_only_within_the_chat_and_once(w):
    one = _say(w, body="Первый")
    two = _say(w, body="Второй")
    before = w.env.store.last_seq()
    state = _read(w, one)["chat"]
    assert state == {"read": one, "unread": 1, "first": two, "last": two}
    events = w.env.store.events_since(before)
    assert [(e["kind"], e["actor"], e["payload"]) for e in events] == [
        ("chat.read", "user:owner", {"agent": "boss", "seq": one, "user": "owner"})]
    after = w.env.store.last_seq()
    assert _read(w, one)["chat"]["read"] == one  # a repeat: no event
    assert _read(w, 0)["chat"]["read"] == one  # never back
    assert w.env.store.last_seq() == after
    assert _read(w, 10**6)["chat"] == {"read": two, "unread": 0, "first": None, "last": two}  # not past the end


def test_cursors_are_per_person(w):
    seq = _say(w)
    _read(w, seq, user="owner")
    assert _unread(w, "owner")["boss"]["unread"] == 0
    assert _unread(w, "petr")["boss"]["unread"] == 1


def test_a_viewer_has_no_chats_and_may_not_read(w):
    _say(w)
    assert _unread(w, "vera") == {}
    with pytest.raises(api.ApiError) as exc:
        _read(w, 1, user="vera")
    assert exc.value.kind == "refused"
    with pytest.raises(api.ApiError) as exc:
        _read(w, 1, agent="ghost")
    assert exc.value.kind == "not_found"
    with pytest.raises(api.ApiError):
        _read(w, -1)
    with pytest.raises(api.ApiError):
        _read(w, True)


def test_a_new_person_starts_at_the_end_of_every_chat(w):
    _say(w)
    _say(w, agent="analyst")
    w.users.create("nina", "Нина", roles=[("talker", "*")], actor="root")
    chats = _unread(w, "nina")
    assert chats["boss"]["unread"] == 0 and chats["analyst"]["unread"] == 0
    _say(w)
    assert _unread(w, "nina")["boss"]["unread"] == 1


def test_v9_base_migrates_to_v10_with_chats_read_to_their_end(tmp_path):
    """AC18: what was in the chats before the feature is not suddenly new."""
    path = str(tmp_path / "v9.db")
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(schema.DDL + "".join(schema.MIGRATIONS[v] for v in range(2, 10)))
    conn.execute("INSERT INTO meta VALUES ('schema_version', '9')")
    conn.execute("INSERT INTO meta VALUES ('next_card', '1')")
    conn.execute("INSERT INTO users (id, name, created) VALUES ('owner', 'O', 't')")
    conn.execute("INSERT INTO threads (id, kind, card, agent) VALUES (5, 'agent_chat', NULL, 'boss')")
    conn.execute("INSERT INTO threads (id, kind, card, agent) VALUES (6, 'agent_chat', NULL, 'dev')")
    conn.execute(
        "INSERT INTO messages (thread, seq, author, kind, body, created) VALUES"
        " (5, 1, 'agent:boss', 'text', 'старое', 't'), (5, 2, 'agent:boss', 'text', 'старое', 't')"
    )
    conn.close()
    db = store.Store.open(path)
    assert db.schema_version() == schema.SCHEMA_VERSION
    assert db.chat_state("owner", 5) == {"read": 2, "unread": 0, "first": None, "last": 2}
    assert db.chat_state("owner", 6)["read"] == 0


def test_http_chats_and_read(w):
    seq = _say(w)
    w.login()
    assert w.client.get("/api/chats").json()["boss"]["unread"] == 1
    assert w.client.post("/api/agents/boss/read", json={"seq": seq}).status_code == 403  # no CSRF token
    r = w.client.post("/api/agents/boss/read", headers=w.headers(), json={"seq": seq})
    assert r.status_code == 200 and r.json()["unread"] == 0
    assert w.client.get("/api/chats").json()["boss"]["read"] == seq
    r = w.client.post("/api/agents/boss/read", headers=w.headers(), json={"seq": "x"})
    assert r.status_code == 400


def test_http_presence_and_sign_out_drops_it(w):
    """A chat page holds its presence; signing out lets pushes come again."""
    w.login()
    body = {"device": "tab-00001", "on": True}
    assert w.client.post("/api/agents/boss/presence", json=body).status_code == 403  # no CSRF token
    r = w.client.post("/api/agents/boss/presence", headers=w.headers(), json=body)
    assert r.status_code == 200 and r.json()["until"]
    thread = w.env.store.agent_thread("boss")
    assert w.env.store.chat_present("owner", thread, w.env.now)
    w.client.post("/api/auth/logout", headers=w.headers())
    assert not w.env.store.chat_present("owner", thread, w.env.now)


def _ws(w, client=None):
    url = w.cfg.origin.replace("http", "ws", 1) + "/ws"
    return (client or w.client).websocket_connect(url, headers={"Origin": w.cfg.origin})


def _next(ws, key):
    while True:
        message = ws.receive_json()
        if key in message:
            return message[key]


def test_ws_sends_chats_on_connect_on_a_reply_and_on_a_read_in_another_tab(w):
    seq = _say(w)
    w.login()
    with _ws(w) as ws:
        assert _next(ws, "chats")["boss"]["unread"] == 1
        two = _say_over_api(w)
        assert _next(ws, "chats")["boss"] == {"read": 0, "unread": 2, "first": seq, "last": two}
        w.client.post("/api/agents/boss/read", headers=w.headers(), json={"seq": two})  # «another tab» reads
        assert _next(ws, "chats")["boss"]["unread"] == 0


def test_ws_never_streams_chat_read_as_an_event():
    """The cursor reaches pages as {"chats"}; a feed or board page subscribed to all is not woken by every read."""
    from kd.cabinet.web import ws

    conn = ws.Connection.__new__(ws.Connection)
    conn.all, conn.cards, conn.agents = True, set(), {"boss"}
    assert not conn.wants({"kind": "chat.read", "card": "", "payload": {"user": "owner", "agent": "boss", "seq": 3}})
    assert conn.wants({"kind": "message.added", "card": "", "payload": {"agent": "boss", "seq": 3}})


def _say_over_api(w):
    """A reply the hub publishes, as after any write of the service."""
    service = w.client.app.state.svc
    before = service.ctx.store.last_seq()
    seq = _say(w, body="Ещё ответ")
    service.hub.publish(service.ctx.store.events_since(before))
    return seq
