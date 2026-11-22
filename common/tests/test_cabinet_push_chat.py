"""A push for each agent reply outside an open chat, per device, never the old queue."""

import sqlite3

import pytest
from cabinet_env import CabinetEnv

from kd.cabinet import api, notify, push_worker, schema, store

TURN_A, TURN_B = "a" * 32, "b" * 32
T0 = "2026-10-14T10:00:00Z"


def _user(env, user):
    return api.Principal("user", -1, user)


def _subscribe(env, user, name):
    api.dispatch(env.ctx, _user(env, user), {
        "op": "push.subscribe", "endpoint": f"https://fcm.googleapis.com/fcm/send/{name}",
        "keys": {"p256dh": "k" * 20, "auth": "a" * 8}, "device": name})


@pytest.fixture
def env(tmp_path):
    env = CabinetEnv(tmp_path)
    for user in ("owner", "bob"):
        env.users.create(user, user, roles=[("owner", "*")], actor="root", language="ru", timezone="UTC")
    _subscribe(env, "owner", "phone")
    env.notifier = notify.Notifier(env.store, env.users, None)
    env.notifier.catch_up()  # the notifier starts at the journal's end
    return env


def _reply(env, body="Ответ", command=None):
    thread = env.store.agent_thread("boss")
    message = env.store.add_thread_message(
        thread, author="agent:boss", kind="text", body=body, payload={"command": command} if command else {},
        via="session" if command else None)
    env.notifier.catch_up()
    return thread, message["seq"]


def _take(env, at):
    env.now = at
    return env.call("digi-cabinet", {"op": "push.take", "limit": 50})["items"]


def _send(env, at, answers=None):
    """One pass of the worker at ``at``: the endpoints it pushed to."""
    env.now = at
    seen = []

    def send(subscription, payload, *_):
        seen.append((subscription["endpoint"].rsplit("/", 1)[-1], payload))
        return (answers or {}).get(subscription["endpoint"].rsplit("/", 1)[-1], 201)

    push_worker.run(env.client("digi-cabinet"), send=send, once=True)
    return seen


def test_two_replies_are_two_pushes_one_turn_is_one(env):
    _reply(env, "Смотрю", TURN_A)
    _reply(env, "Готово: KD-9032", TURN_A)  # the same turn: one reply, its newest text
    _reply(env, "Отдельный ответ")
    assert _take(env, "2026-10-14T10:00:01Z") == []  # the 2 s window
    items = _take(env, "2026-10-14T10:00:03Z")
    assert len(items) == 2 and len({i["tag"] for i in items}) == 2
    first = next(i for i in items if i["tag"].endswith(TURN_A))
    assert first["body"] == "Готово: KD-9032" and first["url"] == "/agents/boss"
    assert {i["badge"] for i in items} == {0}  # the installed app's badge is the inbox, not the chats
    assert _send(env, "2026-10-14T10:00:03Z") and _take(env, "2026-10-14T10:00:10Z") == []
    _reply(env, "Ещё в том же ходе", TURN_A)  # after it went: no second push of the same reply
    assert _take(env, "2026-10-14T10:00:20Z") == []


def test_the_badge_counts_the_persons_inbox(env):
    env.card(status="Review")
    env.card(status="Review")
    _reply(env)
    items = _take(env, "2026-10-14T10:00:03Z")
    assert [i["badge"] for i in items] == [2] and "user" not in items[0]  # two cards in Review, one push


def test_a_reply_read_before_it_is_due_is_not_pushed(env):
    thread, seq = _reply(env)
    env.store.chat_read("owner", thread, seq)
    assert _take(env, "2026-10-14T10:00:03Z") == []
    [row] = env.store.notifications("owner")
    assert row["result"] == "read" and row["sent"]


def test_an_open_chat_suppresses_until_its_lease_ends_and_only_for_its_person(env):
    _subscribe(env, "bob", "bobs")
    api.dispatch(env.ctx, _user(env, "owner"), {
        "op": "user.chat.presence", "agent": "boss", "device": "tab-00001", "on": True})
    _reply(env)
    items = _take(env, "2026-10-14T10:00:03Z")
    assert [i["endpoint"].rsplit("/", 1)[-1] for i in items] == ["bobs"]  # bob's push is not suppressed
    assert env.store.notifications("owner")[0]["result"] == "present"
    env.now = T0
    _reply(env, "Второй", TURN_B)  # the page closed without a word: its lease runs out after 25 s
    second = [i for i in _take(env, "2026-10-14T10:00:26Z") if i["tag"].endswith(TURN_B)]
    assert sorted(i["endpoint"].rsplit("/", 1)[-1] for i in second) == ["bobs", "phone"]


def test_presence_off_and_sign_out_end_it(env):
    on = {"op": "user.chat.presence", "agent": "boss", "device": "tab-00001", "on": True}
    api.dispatch(env.ctx, _user(env, "owner"), on)
    api.dispatch(env.ctx, _user(env, "owner"), {**on, "on": False})
    thread = env.store.agent_thread("boss")
    assert not env.store.chat_present("owner", thread, T0)
    api.dispatch(env.ctx, _user(env, "owner"), on)
    env.store.chat_presence_clear("owner")
    assert not env.store.chat_present("owner", thread, T0)
    for bad in ({**on, "device": "x"}, {**on, "on": "yes"}, {**on, "agent": "ghost"}):
        with pytest.raises(api.ApiError):
            api.dispatch(env.ctx, _user(env, "owner"), bad)


def test_a_new_device_does_not_get_the_old_queue(env):
    env.store._conn.execute("DELETE FROM push_subscriptions")
    _reply(env, "Старое")
    env.now = "2026-10-14T11:00:00Z"
    _subscribe(env, "owner", "laptop")  # the first device, an hour later
    assert _take(env, "2026-10-14T11:00:01Z") == []
    env.now = "2026-10-14T11:00:01Z"
    _reply(env, "Новое")
    assert [i["body"] for i in _take(env, "2026-10-14T11:00:05Z")] == ["Новое"]


def test_each_device_is_delivered_on_its_own(env):
    _subscribe(env, "owner", "laptop")
    _reply(env)
    seen = _send(env, "2026-10-14T10:00:03Z", {"laptop": 503})
    assert sorted(name for name, _ in seen) == ["laptop", "phone"]
    [row] = env.store.notifications("owner")
    assert row["sent"] == ""  # the laptop still waits for it
    assert [n for n, _ in _send(env, "2026-10-14T10:06:00Z")] == ["laptop"]  # only the one that failed, again
    [row] = env.store.notifications("owner")
    assert row["sent"]
    assert _send(env, "2026-10-14T10:20:00Z") == []


def test_a_gone_device_is_dropped_alone(env):
    _subscribe(env, "owner", "laptop")
    _reply(env)
    _send(env, "2026-10-14T10:00:03Z", {"laptop": 410})
    assert [s["device"] for s in env.store.subscriptions("owner")] == ["phone"]
    assert env.store.notifications("owner")[0]["sent"]


def test_the_settings_see_the_last_attempt_of_this_device(env):
    endpoint = "https://fcm.googleapis.com/fcm/send/phone"
    assert api.dispatch(env.ctx, _user(env, "owner"), {"op": "push.mine", "endpoint": endpoint})["last"] is None
    _reply(env)
    _send(env, "2026-10-14T10:00:03Z", {"phone": 429})
    last = api.dispatch(env.ctx, _user(env, "owner"), {"op": "push.mine", "endpoint": endpoint})["last"]
    assert last == {"result": "429", "at": "2026-10-14T10:00:03Z", "sent": False}


def test_the_same_turn_names_its_own_row(env):
    thread = env.store.agent_thread("boss")
    ids = []
    for body in ("Смотрю", "Готово"):
        env.store.add_thread_message(thread, author="agent:boss", kind="text", body=body,
                                     payload={"command": TURN_A}, via="session")
        ids.append(env.notifier.on_event(env.store.events_since(0)[-1], None))
    assert len(ids[0]) == 2 and ids[0] == ids[1]  # owner and bob: their own rows, refreshed — not another row
    assert {env.store.notification(i)["body"] for i in ids[1]} == {"Готово"}


def test_purge_drops_orphan_deliveries_and_lapsed_presence(env):
    _subscribe(env, "owner", "laptop")
    _reply(env)
    _send(env, "2026-10-14T10:00:03Z", {"laptop": 503})
    api.dispatch(env.ctx, _user(env, "owner"), {"op": "push.unsubscribe",
                                               "endpoint": "https://fcm.googleapis.com/fcm/send/laptop"})
    deliveries = lambda: env.store._conn.execute("SELECT COUNT(*) FROM push_deliveries").fetchone()[0]  # noqa: E731
    assert deliveries() == 1  # the laptop's record went with it
    api.dispatch(env.ctx, _user(env, "owner"), {
        "op": "user.chat.presence", "agent": "boss", "device": "tab-00001", "on": True})
    env.store._conn.execute("DELETE FROM notifications")
    env.notifier._purge(env.when().replace(year=2027))
    assert deliveries() == 0
    assert env.store._conn.execute("SELECT COUNT(*) FROM chat_presence").fetchone()[0] == 0


def test_an_event_the_notifier_fails_on_is_skipped_not_retried_forever(env, monkeypatch, capsys):
    # a failing event left the cursor on itself — every later catch-up failed on it again, and no real
    # event reached a phone while the settings' test (which bypasses the journal) still did
    handle = env.notifier._handle
    poison = []

    def flaky(event, decisions):
        if not poison:
            poison.append(event["seq"])
            env.store._conn.execute("INSERT INTO notifications (user, event, kind, tag, title, body, url, due) "
                "VALUES ('owner', 0, 'x', 'x', 't', 'b', '/', '2026-10-14T10:00:00Z')")
            raise RuntimeError("boom")
        return handle(event, decisions)

    monkeypatch.setattr(env.notifier, "_handle", flaky)
    _reply(env, "Первый")  # the notifier fails on this one
    assert env.store.get_state(notify.CURSOR) >= poison[0]
    assert [n["tag"] for n in env.store.notifications("owner")] == []  # its half-written rows rolled back
    err = capsys.readouterr().err
    assert f"seq {poison[0]}" in err and "RuntimeError: boom" in err
    _reply(env, "Второй")
    (row,) = env.store.notifications("owner")
    assert row["body"] == "Второй"


def test_a_database_error_is_not_skipped_the_next_catch_up_tries_again(env, monkeypatch):
    handle, calls = env.notifier._handle, []

    def locked(event, decisions):
        calls.append(event["seq"])
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return handle(event, decisions)

    monkeypatch.setattr(env.notifier, "_handle", locked)
    before = env.store.get_state(notify.CURSOR)
    with pytest.raises(sqlite3.OperationalError):
        _reply(env, "Первый")
    assert env.store.get_state(notify.CURSOR) == before  # not moved past an event never handled
    env.notifier.catch_up()
    (row,) = env.store.notifications("owner")
    assert row["body"] == "Первый" and calls[0] == calls[1]


def test_a_notifier_failing_on_every_event_stops_instead_of_skipping_the_journal(env, monkeypatch, capsys):
    def broken(event, decisions):
        raise ValueError("no such time zone")

    monkeypatch.setattr(env.notifier, "_handle", broken)
    before = env.store.get_state(notify.CURSOR)
    thread = env.store.agent_thread("boss")
    for n in range(notify.SKIP_LIMIT + 2):
        env.store.add_thread_message(thread, author="agent:boss", kind="text", body=f"#{n}", payload={})
    with pytest.raises(ValueError):
        env.notifier.catch_up()
    skipped = capsys.readouterr().err.count(") skipped")
    assert skipped == notify.SKIP_LIMIT
    assert env.store.get_state(notify.CURSOR) < env.store.last_seq() and env.store.get_state(notify.CURSOR) > before
    monkeypatch.undo()  # fixed: what was not skipped is still pushed
    env.notifier.catch_up()
    assert [r["body"] for r in env.store.notifications("owner")] == [f"#{notify.SKIP_LIMIT}", f"#{notify.SKIP_LIMIT + 1}"]


def test_v13_base_migrates_to_v14_giving_up_exhausted_rows(tmp_path):
    path = str(tmp_path / "v13.db")
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(schema.DDL + "".join(schema.MIGRATIONS[v] for v in range(2, 14)))
    conn.execute("INSERT INTO meta VALUES ('schema_version', '13')")
    conn.execute("INSERT INTO meta VALUES ('next_card', '1')")
    conn.execute(
        "INSERT INTO notifications (user, kind, title, body, due, attempts) VALUES"
        " ('o', 'x', 't', 'b', '2026-10-01T00:00:00Z', 5), ('o', 'x', 't', 'b', '2026-10-01T00:00:00Z', 1)")
    conn.close()
    db = store.Store.open(path)
    assert db.schema_version() == schema.SCHEMA_VERSION
    rows = db.notifications("o")
    assert rows[0]["sent"] and rows[1]["sent"] == "" and rows[1]["reply"] == ""
