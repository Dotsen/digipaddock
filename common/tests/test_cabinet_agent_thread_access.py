"""An agent's own threads with people — its chat and its «Monitor», outside cards — are not a partner's (KD-9476).

A partner talks to agents in their cards' threads only. An agent's chat needs ``agents.chat``, which no default role but
the owner's holds; «Monitor» (the monitor's reports on the servers) also needs ``servers.read``. Every way in is
checked: reading, writing, files, live events, unread counts, pushes, the lists of the agent's commands and
sessions and the facts counted from those threads."""

import pytest
from web_env import WebEnv

from kd import session_config
from kd.cabinet import api, notify, perms, store, team

HOSTED = session_config.HostConfig({"analyst": 1, "sysadmin": 1}, 600)
SPACES = ("chat", "monitor")
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


@pytest.fixture
def w(tmp_path):
    w = WebEnv(tmp_path)
    api.install_host(w.env.ctx, lambda: HOSTED)
    w.users.set_role(
        "talker", ["cards.read", "agents.chat"], actor="root"
    )  # the chat, not the servers
    w.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    w.users.create(
        "anna", "Анна", roles=[("partner", "*")], actor="root"
    )  # a partner of every project
    w.users.create(
        "pavel", "Павел", roles=[("partner", ["admin"])], actor="root"
    )  # a partner of one
    w.users.create("chen", "Чэнь", roles=[("talker", "*")], actor="root")
    w.cfg = w.svc.cfg = type(w.cfg)(
        **{**w.cfg.__dict__, "files_dir": str(tmp_path / "files")}
    )
    return w


def person(user):
    return api.Principal("user", -1, user)


def call(w, user, request):
    return api.dispatch(w.env.ctx, person(user), request)


def refused(w, user, request, kind="refused"):
    with pytest.raises(api.ApiError) as exc:
        call(w, user, request)
    assert exc.value.kind == kind, (user, request, exc.value)


def _said(w, space, body="отчёт"):
    """The owner's message and the agent's reply in the sysadmin's ``space``; the thread."""
    call(
        w,
        "owner",
        {
            "op": "user.agent.message",
            "agent": "sysadmin",
            "space": space,
            "text": f"что с {space}?",
        },
    )
    thread = w.env.store.agent_thread("sysadmin", space)
    w.env.store.add_thread_message(
        thread, author="agent:sysadmin", kind="text", body=body
    )
    return thread


def _as(w, user):
    browser = w.other_client()
    me = w.login(user, client=browser)
    return browser, w.headers(me["csrf"])


def test_no_default_role_but_the_owners_holds_agents_chat(w):
    assert (
        "agents.chat" not in perms.DEFAULT_ROLES["partner"]
        and "agents.chat" not in perms.DEFAULT_ROLES["viewer"]
    )
    assert "agents.chat" not in w.users.roles()["partner"]
    assert perms.AGENT_THREAD_PERMS == {
        "chat": ("agents.chat",),
        "monitor": ("agents.chat", "servers.read"),
    }


@pytest.mark.parametrize("user", ["anna", "pavel"])
@pytest.mark.parametrize("space", SPACES)
def test_a_partner_neither_reads_nor_writes_an_agents_thread(w, user, space):
    _said(w, space)
    for request in (
        {"op": "thread.messages", "agent": "sysadmin", "space": space},
        {
            "op": "user.agent.message",
            "agent": "sysadmin",
            "space": space,
            "text": "покажи серверы",
        },
        {"op": "user.agent.check", "agent": "sysadmin", "space": space},
        {"op": "user.chat.read", "agent": "sysadmin", "space": space, "seq": 1},
        {
            "op": "user.chat.presence",
            "agent": "sysadmin",
            "space": space,
            "device": "tab-00001",
            "on": True,
        },
        {"op": "command.list", "agent": "sysadmin"},
        {"op": "session.list", "agent": "sysadmin"},
    ):
        refused(w, user, request)
    assert call(w, user, {"op": "user.chat.unread"})["chats"] == {}
    assert [
        m["author"]
        for m in w.env.store.thread_messages(
            w.env.store.agent_thread("sysadmin", space)
        )
    ] == ["user:owner", "agent:sysadmin"]  # nothing of hers got in


def test_the_chat_without_servers_read_opens_but_monitor_does_not(w):
    chat, monitor = _said(w, "chat"), _said(w, "monitor", body="диск на node-a 97%")
    page = call(w, "chen", {"op": "thread.messages", "agent": "sysadmin"})
    assert page["thread"] == chat and len(page["messages"]) == 2
    call(w, "chen", {"op": "user.agent.message", "agent": "sysadmin", "text": "привет"})
    for request in (
        {"op": "thread.messages", "agent": "sysadmin", "space": "monitor"},
        {
            "op": "user.agent.message",
            "agent": "sysadmin",
            "space": "monitor",
            "text": "что на серверах?",
        },
        {"op": "user.agent.check", "agent": "sysadmin", "space": "monitor"},
        {"op": "user.chat.read", "agent": "sysadmin", "space": "monitor", "seq": 1},
        {
            "op": "user.chat.presence",
            "agent": "sysadmin",
            "space": "monitor",
            "device": "tab-00001",
            "on": True,
        },
    ):
        refused(w, "chen", request)
    assert set(call(w, "chen", {"op": "user.chat.unread"})["chats"]) == {"sysadmin"}
    assert set(call(w, "owner", {"op": "user.chat.unread"})["chats"]) == {
        "sysadmin",
        "sysadmin/monitor",
    }
    # the agent's commands and sessions outside cards: the chat's, never «Monitor»'s
    threads = {
        c["thread"]
        for c in call(w, "chen", {"op": "command.list", "agent": "sysadmin"})[
            "commands"
        ]
    }
    assert threads == {chat}
    assert {
        c["thread"]
        for c in call(w, "owner", {"op": "command.list", "agent": "sysadmin"})[
            "commands"
        ]
    } == {chat, monitor}
    w.env.store.session_for(monitor, "sysadmin")
    w.env.store.session_for(chat, "sysadmin")
    sessions = call(w, "chen", {"op": "session.list", "agent": "sysadmin"})["sessions"]
    assert {s["thread"] for s in sessions} == {chat}


def test_the_owner_and_the_agent_itself_read_both_threads_as_before(w):
    chat, monitor = _said(w, "chat"), _said(w, "monitor")
    for space, thread in (("chat", chat), ("monitor", monitor)):
        assert (
            call(
                w,
                "owner",
                {"op": "thread.messages", "agent": "sysadmin", "space": space},
            )["thread"]
            == thread
        )
        mine = w.env.call(
            "digi-sysadmin",
            {"op": "thread.messages", "agent": "sysadmin", "space": space},
        )
        assert [m["author"] for m in mine["messages"]][-1] == "agent:sysadmin"
    with pytest.raises(api.ApiError):
        w.env.call(
            "digi-analyst",
            {"op": "thread.messages", "agent": "sysadmin", "space": "monitor"},
        )


def test_a_partner_talks_to_her_projects_agent_in_the_cards_thread_as_before(w):
    card = w.env.card(
        project="admin", assignee="@sysadmin", status="In Progress", title="Сертификат"
    )
    message = call(
        w,
        "pavel",
        {
            "op": "user.card.comment",
            "card": card.id,
            "text": "@sysadmin проверь, пожалуйста",
        },
    )
    assert message["message"]["author"] == "user:pavel"
    [command] = w.env.store.commands(card=card.id)
    assert command["agent"] == "sysadmin" and command["thread"] == card.thread
    page = call(w, "pavel", {"op": "thread.messages", "card": card.id})
    assert [m["body"] for m in page["messages"]][-1] == "@sysadmin проверь, пожалуйста"
    assert [
        c["id"]
        for c in call(w, "pavel", {"op": "command.list", "card": card.id})["commands"]
    ] == [command["id"]]
    events = api.event_filter(w.env.ctx, person("pavel"))(w.env.store.events_since(0))
    assert {"message.added", "command.queued"} <= {
        e["kind"] for e in events if e["card"] == card.id
    }


def test_agent_thread_events_reach_only_who_may_read_the_thread(w):
    chat, monitor = _said(w, "chat"), _said(w, "monitor")
    session = w.env.store.session_for(monitor, "sysadmin")
    w.env.store.session_update(session["id"], state="working")
    events = w.env.store.events_since(0)

    def threads(user):
        return {
            e["thread"]
            for e in api.event_filter(w.env.ctx, person(user))(events)
            if e["thread"] in (chat, monitor)
        }

    kinds = {e["kind"] for e in events if e["thread"] == monitor}
    assert {"message.added", "command.queued", "session.changed"} <= kinds
    assert threads("owner") == {chat, monitor}
    assert threads("chen") == {chat}
    assert threads("anna") == threads("pavel") == set()


def test_a_monitor_file_is_no_file_without_servers_read(w):
    for space in SPACES:
        call(
            w,
            "owner",
            {
                "op": "user.agent.message",
                "agent": "sysadmin",
                "space": space,
                "text": "лог",
                "files": [
                    {
                        "id": f"{len(space):032x}",
                        "name": "a.log",
                        "mime": "text/plain",
                        "size": 3,
                        "sha256": "b" * 64,
                    }
                ],
            },
        )
    chat_file, monitor_file = f"{len('chat'):032x}", f"{len('monitor'):032x}"
    assert (
        call(w, "owner", {"op": "file.get", "id": monitor_file})["file"]["agent"]
        == "sysadmin"
    )
    assert (
        call(w, "chen", {"op": "file.get", "id": chat_file})["file"]["agent"]
        == "sysadmin"
    )
    refused(w, "chen", {"op": "file.get", "id": monitor_file}, "not_found")
    for user in ("anna", "pavel"):
        for file_id in (chat_file, monitor_file):
            refused(w, user, {"op": "file.get", "id": file_id}, "not_found")


def test_the_team_facts_count_monitor_only_with_servers_read(w):
    monitor = w.env.store.agent_thread("sysadmin", "monitor")
    session = w.env.store.session_for(monitor, "sysadmin")
    w.env.store.session_update(session["id"], state="working", heartbeat=w.env.now)
    w.env.store.add_thread_message(
        monitor, author="agent:sysadmin", kind="text", body="диск 97%"
    )

    def facts(user):
        spaces = api.agent_spaces(w.env.ctx, person(user))
        return team.facts(
            w.env.store, "sysadmin", cards=[], spaces=spaces, hosted=True, now=w.env.now
        )

    assert api.agent_spaces(w.env.ctx, person("owner")) == ("chat", "monitor")
    assert api.agent_spaces(w.env.ctx, person("chen")) == ("chat",)
    assert api.agent_spaces(w.env.ctx, person("anna")) == ()
    assert (
        facts("owner")["working"] == 1
        and facts("owner")["last_action"]["text"] == "диск 97%"
    )
    for user in ("chen", "anna"):
        assert facts(user)["working"] == 0 and facts(user)["last_action"] is None
    team_of = {
        user: {
            t["name"]: t["working"]
            for t in call(w, user, {"op": "overview.get"})["team"]
        }
        for user in ("owner", "chen")
    }
    assert (team_of["owner"]["sysadmin"], team_of["chen"]["sysadmin"]) == (1, 0)


def test_a_monitor_reply_and_its_failed_session_push_only_who_reads_the_servers(w):
    n = notify.Notifier(w.env.store, w.users, None)
    chat = w.env.store.agent_thread("sysadmin")
    w.env.store.add_thread_message(
        chat, author="agent:sysadmin", kind="text", body="в чате"
    )
    rows = [
        w.env.store.notification(i)
        for i in n.on_event(w.env.store.events_since(0)[-1], None)
    ]
    assert {r["user"] for r in rows} == {"owner", "chen"}
    monitor = w.env.store.agent_thread("sysadmin", "monitor")
    w.env.store.add_thread_message(
        monitor, author="agent:sysadmin", kind="text", body="диск 97%"
    )
    rows = [
        w.env.store.notification(i)
        for i in n.on_event(w.env.store.events_since(0)[-1], None)
    ]
    assert {r["user"] for r in rows} == {"owner"}
    session = w.env.store.session_for(monitor, "sysadmin")
    w.env.store.session_update(session["id"], state="failed", error="exit 1")
    rows = [
        w.env.store.notification(i)
        for i in n.on_event(w.env.store.events_since(0)[-1], None)
    ]
    assert [(r["user"], r["kind"]) for r in rows] == [("owner", "session.failed")]


def test_over_http_a_partner_gets_403_and_no_monitor_tab(w):
    _said(w, "chat"), _said(w, "monitor")
    anna, headers = _as(w, "anna")
    for space in ("", "/monitor"):
        assert anna.get(f"/api/agents/sysadmin{space}/messages").status_code == 403
        r = anna.post(
            f"/api/agents/sysadmin{space}/messages", headers=headers, json={"body": "x"}
        )
        assert r.status_code == 403
        r = anna.post(
            f"/api/agents/sysadmin{space}/files",
            headers=headers,
            data={"text": "x"},
            files=[("file", ("a.png", PNG, "image/png"))],
        )
        assert r.status_code == 403
        assert (
            anna.post(
                f"/api/agents/sysadmin{space}/read", headers=headers, json={"seq": 1}
            ).status_code
            == 403
        )
    assert anna.get("/api/chats").json() == {}
    assert anna.get("/api/agents/sysadmin").json()["monitor"] is False
    assert "agents.chat" not in anna.get("/api/me").json()["permissions"]["*"]
    chen, headers = _as(w, "chen")
    assert chen.get("/api/agents/sysadmin/messages").status_code == 200
    assert chen.get("/api/agents/sysadmin/monitor/messages").status_code == 403
    r = chen.post(
        "/api/agents/sysadmin/monitor/files",
        headers=headers,
        data={"text": "x"},
        files=[("file", ("a.png", PNG, "image/png"))],
    )
    assert r.status_code == 403
    assert chen.get("/api/agents/sysadmin").json()["monitor"] is False
    w.login()
    assert w.client.get("/api/agents/sysadmin").json()["monitor"] is True
    assert w.client.get("/api/agents/sysadmin/monitor/messages").status_code == 200


def test_a_partners_websocket_carries_no_agent_thread_event(w):
    origin = w.cfg.origin.replace("http", "ws", 1) + "/ws"
    anna, _ = _as(w, "anna")
    chen, _ = _as(w, "chen")
    w.login()

    def received(socket, card):
        """Up to the card's message (the last write): ``(kind, card, thread)`` of each event, the chats' keys."""
        got, keys = [], set()
        while True:
            message = socket.receive_json()
            keys |= set(message.get("chats", {}))
            event = message.get("event")
            if event:
                got.append((event["kind"], event["card"], event.get("thread")))
                if event["card"] == card and event["kind"] == "message.added":
                    return got, keys

    with (
        anna.websocket_connect(origin, headers={"Origin": w.cfg.origin}) as a_ws,
        chen.websocket_connect(origin, headers={"Origin": w.cfg.origin}) as c_ws,
    ):
        for socket in (a_ws, c_ws):
            socket.receive_json()  # hello
            socket.send_json(
                {"subscribe": {"all": True, "agent": "sysadmin"}}
            )  # every event she may see
            while "subscribed" not in socket.receive_json():
                pass
        for space in SPACES:
            w.client.post(
                f"/api/agents/sysadmin{'' if space == 'chat' else '/monitor'}/messages",
                headers=w.headers(),
                json={"body": "что там?"},
            )
        monitor = w.env.store.agent_thread("sysadmin", "monitor")
        w.env.store.add_thread_message(
            monitor, author="agent:sysadmin", kind="text", body="диск 97%"
        )
        last = w.env.store.last_seq()
        card = w.env.card(
            project="web", title="Метка"
        )  # the last write: a card event both of them see
        w.env.store.add_message(card.id, author="user:owner", kind="text", body="конец")
        w.svc._after(last - 1)
        anna_events, anna_chats = received(a_ws, card.id)
        chen_events, chen_chats = received(c_ws, card.id)
    chat = w.env.store.agent_thread("sysadmin")
    assert [e for e in anna_events if not e[1]] == [] and anna_chats == set()
    assert {e[2] for e in chen_events if not e[1]} == {
        chat
    }  # her chat's events, none of «Monitor»'s
    assert chen_chats == {"sysadmin"}


def test_an_upgrade_takes_agents_chat_from_the_stored_partner_role(tmp_path):
    """A base from before KD-9476 keeps its roles (``_default_roles`` adds only missing ones): the migration takes
    agents.chat from the stored partner role and leaves every other role and permission as it was."""
    from cabinet_env import CabinetEnv
    from kd.cabinet import schema

    env = CabinetEnv(tmp_path)
    conn = env.store._conn
    old = [
        "cards.read",
        "cards.create",
        "cards.comment",
        "cards.edit",
        "decide.promote",
        "decide.question",
        "agents.chat",
    ]
    conn.execute(
        "UPDATE roles SET perms=? WHERE id='partner'", (store.json.dumps(old),)
    )
    conn.execute(
        "INSERT INTO roles (id, perms) VALUES ('talker', '[\"agents.chat\", \"cards.read\"]')"
    )
    conn.execute(
        "UPDATE meta SET value=? WHERE key='schema_version'",
        (str(schema.SCHEMA_VERSION - 1),),
    )
    env.users.create("anna", "Анна", roles=[("partner", "*")], actor="root")
    assert env.users.can("anna", "agents.chat")
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    env.store.close()

    reopened = store.Store.open(path)
    try:
        from kd.cabinet import users

        people = users.Users(reopened)
        roles = people.roles()
        assert roles["partner"] == old[:-1]
        assert roles["talker"] == ["agents.chat", "cards.read"] and roles["owner"] == [
            "*"
        ]
        assert not people.can("anna", "agents.chat") and people.can(
            "anna", "cards.edit"
        )
        assert reopened.schema_version() == schema.SCHEMA_VERSION
    finally:
        reopened.close()
