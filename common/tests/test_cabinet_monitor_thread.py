"""An agent's second thread with people, "Monitor" — the monitor's wakes go there, people may write there, its
own session answers there; its unread replies and pushes are counted apart from the chat."""

import pytest

from kd import session_config
from kd.cabinet import api, chats, dispatcher, notify, schema, sessions_api, users
from cabinet_env import CabinetEnv

HOSTED = session_config.HostConfig({"analyst": 1}, 600)


def person(user):
    return api.Principal("user", -1, user)


@pytest.fixture
def env(tmp_path):
    e = CabinetEnv(tmp_path)
    u = users.Users(e.store)
    u.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    u.create("vera", "Вера", roles=[("viewer", "*")], actor="user:owner")
    api.install_host(e.ctx, lambda: HOSTED)
    return e


def _enqueue(env, key, **extra):
    request = {"op": "command.enqueue", "agent": "analyst", "text": "[монитор] P-7 открыта", "key": key, **extra}
    return env.call("digi-analyst", request)["command"]


def test_schema_keeps_one_monitor_thread_per_agent(env):
    assert schema.SCHEMA_VERSION >= 24 and "agent_monitor" in schema.AGENT_THREAD_KINDS
    first = env.store.agent_thread("analyst", "monitor")
    assert env.store.agent_thread("analyst", "monitor") == first != env.store.agent_thread("analyst")
    assert env.store.agent_space(first) == "monitor" and env.store.agent_space(env.store.agent_thread("analyst")) == "chat"
    with pytest.raises(Exception):
        env.store._conn.execute("INSERT INTO threads (kind, card, agent) VALUES ('agent_monitor', NULL, 'analyst')")
    with pytest.raises(KeyError):
        env.store.agent_thread("analyst", "nowhere")


def test_monitor_wake_lands_in_monitor_and_other_signals_in_the_chat(env):
    """The monitor's command is in "Monitor"; a signal without a card from another source stays in the chat."""
    monitor = _enqueue(env, "m1", space="monitor")
    plain = _enqueue(env, "s1")
    assert monitor["thread"] == env.store.agent_thread("analyst", "monitor") and monitor["kind"] == "signal"
    assert plain["thread"] == env.store.agent_thread("analyst") != monitor["thread"]
    card = env.card(assignee="@analyst")
    for bad in ({"space": "nowhere"}, {"space": ["monitor"]}, {"space": "monitor", "card": card.id}):
        with pytest.raises(api.ApiError) as exc:
            _enqueue(env, "b1", **bad)
        assert exc.value.kind == "invalid"


def test_a_person_writes_to_monitor_and_wakes_its_session(env):
    out = api.dispatch(env.ctx, person("owner"),
                       {"op": "user.agent.message", "agent": "analyst", "space": "monitor", "text": "что с P-7?"})
    thread = env.store.agent_thread("analyst", "monitor")
    [command] = env.store.commands(agent="analyst")
    assert command["thread"] == thread and command["kind"] == "chat" and command["text"] == sessions_api.MONITOR_WAKE
    assert "P-7" not in command["text"] and out["message"]["body"] == "что с P-7?"
    page = api.dispatch(env.ctx, person("owner"), {"op": "thread.messages", "agent": "analyst", "space": "monitor"})
    assert [m["body"] for m in page["messages"]] == ["что с P-7?"] and page["thread"] == thread
    chat = api.dispatch(env.ctx, person("owner"), {"op": "thread.messages", "agent": "analyst"})
    assert chat["messages"] == []
    with pytest.raises(api.ApiError):
        api.dispatch(env.ctx, person("vera"),
                     {"op": "user.agent.message", "agent": "analyst", "space": "monitor", "text": "x"})
    with pytest.raises(api.ApiError) as exc:
        api.dispatch(env.ctx, person("owner"), {"op": "thread.messages", "agent": "analyst", "space": {"x": 1}})
    assert exc.value.kind == "invalid"


def test_message_events_name_the_monitor_space(env):
    before = env.store.last_seq()
    api.dispatch(env.ctx, person("owner"), {"op": "user.agent.message", "agent": "analyst", "space": "monitor",
                                            "text": "hi"})
    api.dispatch(env.ctx, person("owner"), {"op": "user.agent.message", "agent": "analyst", "text": "hi"})
    added = [e for e in env.store.events_since(before) if e["kind"] == "message.added"]
    assert [(e["payload"]["agent"], e["payload"].get("space")) for e in added] == [("analyst", "monitor"),
                                                                                   ("analyst", None)]
    assert all(sessions_api.chat_event(e) for e in added)


def _session_in(env, space):
    command = _enqueue(env, f"k-{space}", space=space)
    return env.store.session_for(command["thread"], "analyst")


def test_kd_task_chat_of_a_monitor_session_reads_and_answers_monitor(env):
    """kd-task chat in the "Monitor" session reads that thread; in any other — the chat."""
    monitor = _session_in(env, "monitor")
    chat = _session_in(env, "chat")
    env.store.add_thread_message(env.store.agent_thread("analyst", "monitor"), author="user:owner", kind="text",
                                 body="в мониторе")
    env.store.add_thread_message(env.store.agent_thread("analyst"), author="user:owner", kind="text", body="в чате")

    def read(session=None):
        request = {"op": "agent.chat.read", "agent": "analyst", **({"session": session} if session else {})}
        return [m["body"] for m in env.call("digi", request)["messages"]]

    assert read(monitor["id"][:12]) == ["в мониторе"]
    assert read(chat["id"][:12]) == read() == read("0" * 12) == read("bad") == ["в чате"]
    seq = env.store.thread_messages(env.store.agent_thread("analyst", "monitor"), 0)[-1]["seq"]
    whole = env.call("digi", {"op": "agent.chat.message", "agent": "analyst", "seq": seq,
                             "session": monitor["id"][:12]})
    assert "в мониторе" in whole["text"]


def _post(env, session=None, **extra):
    request = {"op": "agent.chat.post", "agent": "analyst", "text": "ответ",
               **({"session": session["id"][:12]} if session else {}), **extra}
    return env.call("digi", request)["message"]


def test_a_session_never_answers_twice_in_its_own_thread(env):
    """a session's turn text is posted to its own thread already — chat.post there would be a second
    message for one answer; the refusal says where the answer goes."""
    chat, monitor = _session_in(env, "chat"), _session_in(env, "monitor")
    threads = [env.store.agent_thread("analyst"), env.store.agent_thread("analyst", "monitor")]
    before = [len(env.store.thread_messages(t, 0)) for t in threads]
    for session, extra in ((chat, {}), (chat, {"space": "chat"}), (monitor, {})):
        with pytest.raises(api.ApiError) as exc:
            _post(env, session, **extra)
        assert exc.value.kind == "refused" and "your turn's text" in str(exc.value)
    assert [len(env.store.thread_messages(t, 0)) for t in threads] == before
    assert all("--post" not in wake and "turn's text" in wake for wake in sessions_api.WAKES.values())


def test_posts_into_the_chat_from_elsewhere_still_go(env):
    """From "Monitor" with --in-chat, from a card's session, or with no session: the chat gets it."""
    monitor = _session_in(env, "monitor")
    card = env.card(assignee="@analyst")
    on_card = env.store.session_for(_enqueue(env, "k-card", card=card.id)["thread"], "analyst")
    chat = env.store.agent_thread("analyst")
    for session, extra in ((monitor, {"space": "chat"}), (on_card, {}), (None, {})):
        message = _post(env, session, **extra)
        assert env.store.thread_messages(chat, 0)[-1]["seq"] == message["seq"]


def test_unread_and_read_are_per_thread(env):
    chat = env.store.agent_thread("analyst")
    monitor = env.store.agent_thread("analyst", "monitor")
    env.store.add_thread_message(monitor, author="agent:analyst", kind="text", body="P-7: диск")
    env.store.add_thread_message(chat, author="agent:analyst", kind="text", body="привет")
    env.store.add_thread_message(monitor, author="agent:analyst", kind="text", body="P-8: память")
    found = api.dispatch(env.ctx, person("owner"), {"op": "user.chat.unread"})["chats"]
    assert found["analyst"]["unread"] == 1 and found[chats.key("analyst", "monitor")]["unread"] == 2
    assert chats.key("analyst", "monitor") == "analyst/monitor"
    read = api.dispatch(env.ctx, person("owner"),
                        {"op": "user.chat.read", "agent": "analyst", "space": "monitor", "seq": 2})["chat"]
    assert read["read"] == 2 and read["unread"] == 0
    assert api.dispatch(env.ctx, person("owner"), {"op": "user.chat.unread"})["chats"]["analyst"]["unread"] == 1
    events = [e for e in env.store.events_since(0) if e["kind"] == "chat.read"]
    assert events[-1]["payload"]["space"] == "monitor"
    out = api.dispatch(env.ctx, person("owner"), {"op": "user.chat.presence", "agent": "analyst", "space": "monitor",
                                                  "device": "device-01", "on": True})
    assert out["until"]


def test_a_new_person_starts_at_the_end_of_monitor_too(env):
    monitor = env.store.agent_thread("analyst", "monitor")
    env.store.add_thread_message(monitor, author="agent:analyst", kind="text", body="старое")
    users.Users(env.store).create("ivan", "Иван", roles=[("owner", "*")], actor="root")
    found = api.dispatch(env.ctx, person("ivan"), {"op": "user.chat.unread"})["chats"]
    assert found["analyst/monitor"]["unread"] == 0


def test_monitor_reply_push_opens_the_monitor_tab(env):
    """A reply in "Monitor" is pushed like a chat reply, its own tag and a link to the tab."""
    monitor = env.store.agent_thread("analyst", "monitor")
    chat = env.store.agent_thread("analyst")
    notifier = notify.Notifier(env.store, env.users, None)
    notifier.catch_up()
    env.store.add_thread_message(monitor, author="agent:analyst", kind="text", body="P-7: диск 95%")
    env.store.add_thread_message(chat, author="agent:analyst", kind="text", body="привет")
    notifier.catch_up()
    rows = env.store._conn.execute("SELECT tag, url, title, thread FROM notifications ORDER BY id").fetchall()
    mine = {r["thread"]: r for r in rows}
    assert mine[monitor]["url"] == "/agents/analyst?tab=monitor" and mine[monitor]["tag"].startswith("monitor:analyst")
    assert "Монитор" in mine[monitor]["title"]
    assert mine[chat]["url"] == "/agents/analyst" and mine[chat]["tag"].startswith("chat:analyst")


def test_the_coordinators_monitor_is_a_work_log(env):
    """The coordinator's "Monitor" holds its attention ticks — no unread count and no push; its chat keeps
    both, and another agent's "Monitor" is unchanged."""
    log = env.store.agent_thread("boss", "monitor")
    chat = env.store.agent_thread("boss")
    notifier = notify.Notifier(env.store, env.users, None)
    notifier.catch_up()
    env.store.add_thread_message(log, author="agent:boss", kind="text", body="Обработано.")
    env.store.add_thread_message(chat, author="agent:boss", kind="text", body="KD-9 ждёт вашего решения")
    notifier.catch_up()
    pushed = [r["thread"] for r in env.store._conn.execute("SELECT thread FROM notifications").fetchall()]
    assert pushed == [chat]
    found = api.dispatch(env.ctx, person("owner"), {"op": "user.chat.unread"})["chats"]
    assert "boss/monitor" not in found and found["boss"]["unread"] == 1
    assert ("boss", "monitor") in env.store.agent_spaces()  # the page still shows the tab


def test_dispatcher_names_the_space_and_an_old_host_gets_it_without(env):
    from datetime import datetime

    monitor = _enqueue(env, "d1", space="monitor")
    plain = _enqueue(env, "d2")
    svc = type("Svc", (), {"ctx": env.ctx})()
    now = datetime.fromisoformat(monitor["created"].replace("Z", "+00:00"))  # the store's clock, not the wall's
    request = dispatcher._prepare(svc, monitor, now)
    assert request["space"] == "monitor" and "space" not in dispatcher._prepare(svc, plain, now)
    old = dispatcher.HostRefused("invalid", "unknown field for deliver")
    assert "space" not in dispatcher._ordinary(request, old) and dispatcher._ordinary(request, ValueError()) is None
