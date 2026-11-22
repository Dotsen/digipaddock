"""the WebSocket hub — events of visible projects, subscriptions, catching up, the inbox badge."""

import pytest
from starlette.websockets import WebSocketDisconnect
from web_env import WebEnv


@pytest.fixture
def w(tmp_path):
    w = WebEnv(tmp_path)
    w.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    w.users.create("anna", "Анна", roles=[("partner", ["web"])], actor="root")
    return w


def _url(w):
    # the test client joins a relative path to ws://testserver; the session cookie is the origin's
    return w.cfg.origin.replace("http", "ws", 1) + "/ws"


def _connect(w, client=None):
    return (client or w.client).websocket_connect(
        _url(w), headers={"Origin": w.cfg.origin}
    )


def _open(ws):
    """The hub's first two messages: the journal's current seq, then the inbox badge (returned)."""
    hello = ws.receive_json()
    assert set(hello) == {"hello"} and isinstance(hello["hello"]["seq"], int)
    return ws.receive_json()


def _next_event(ws):
    """The next ``event`` message, skipping badges."""
    while True:
        message = ws.receive_json()
        if "event" in message:
            return message["event"]


def test_a_subscribed_card_streams_its_events(w):
    w.login()
    card = w.client.post(
        "/api/cards", headers=w.headers(), json={"title": "Кеш", "project": "digipaddock"}
    ).json()
    with _connect(w) as ws:
        assert _open(ws) == {"badge": {"inbox": 0, "incidents": 0}}
        ws.send_json({"subscribe": {"card": card["id"]}})
        ws.receive_json()  # the subscription is confirmed
        w.client.post(
            f"/api/cards/{card['id']}/messages",
            headers=w.headers(),
            json={"body": "Привет"},
        )
        event = _next_event(ws)
        assert event["kind"] == "message.added" and event["card"] == card["id"]


def test_a_partner_gets_only_her_projects(w):
    secret = w.env.card(project="digipaddock", title="Тайна")
    mine = w.env.card(project="web", title="Своя")
    anna = w.other_client()
    w.login("anna", client=anna)
    w.login()
    with _connect(w, anna) as ws:
        _open(ws)
        ws.send_json({"subscribe": {"all": True}})
        ws.receive_json()
        for card in (secret, mine):
            w.client.post(
                f"/api/cards/{card.id}/messages",
                headers=w.headers(),
                json={"body": "x"},
            )
        assert _next_event(ws)["card"] == mine.id


def test_after_catches_up_on_missed_events(w):
    w.login()
    first = w.env.card(project="web", title="Раз")
    seq = w.env.store.last_seq()
    second = w.env.card(project="web", title="Два")
    with _connect(w) as ws:
        _open(ws)
        ws.send_json({"subscribe": {"all": True}})
        ws.receive_json()
        ws.send_json({"after": seq})
        event = _next_event(ws)
        assert event["kind"] == "card.created" and event["card"] == second.id
        assert event["seq"] > seq and first.id != second.id


def test_the_badge_follows_the_inbox(w):
    card = w.env.card(project="web", status="In Progress")
    w.login()
    with _connect(w) as ws:
        assert _open(ws) == {"badge": {"inbox": 0, "incidents": 0}}
        w.client.patch(
            f"/api/cards/{card.id}",
            headers=w.headers(),
            json={"revision": card.revision, "changes": {"status": "Review"}, "override_reason": "готово"},
        )
        assert _next_badge(ws) == {"inbox": 1, "incidents": 0}


def _incident(w, key, level="warn"):
    """An incident the monitor opens (as digi-sysadmin's ``monitor.push``): its number."""
    problem = {"key": key, "server": "app1", "check": "units", "subject": key.rsplit(":", 1)[-1], "level": level,
               "value": "failed", "opened_at": w.env.now}
    result = w.env.call("digi-sysadmin", {"op": "monitor.push", "items": [{"key": f"open-{key}", "kind": "open",
                                                                          "problem": problem}]})
    return result["results"][0]["incident"]


def _next_badge(ws):
    while True:
        message = ws.receive_json()
        if "badge" in message:
            return message["badge"]


def test_the_badge_counts_incidents_not_resolved_for_who_reads_the_servers(w):
    # the menu shows how many incidents are open (acknowledged ones too, resolved ones not)
    _incident(w, "app1:units:certbot.service")
    _incident(w, "app1:units:nginx.service")
    resolved = w.env.call("digi-sysadmin", {"op": "monitor.push", "items": [{
        "key": "resolve-nginx", "kind": "resolve", "problem_key": "app1:units:nginx.service", "at": w.env.now}]})
    assert "error" not in resolved["results"][0]
    card = w.env.card(project="web", status="In Progress")
    w.login()
    with _connect(w) as ws:
        assert _open(ws) == {"badge": {"inbox": 0, "incidents": 1}}
        _incident(w, "app1:units:cron.service", level="crit")  # an alarm: the inbox counts it too
        w.client.patch(  # any card or incident event recounts
            f"/api/cards/{card.id}",
            headers=w.headers(),
            json={"revision": card.revision, "changes": {"status": "Blocked"}, "override_reason": "ждём"},
        )
        assert _next_badge(ws) == {"inbox": 1, "incidents": 2}
    anna = w.other_client()
    w.login("anna", client=anna)
    with _connect(w, anna) as ws:
        assert _open(ws) == {"badge": {"inbox": 0}}  # a partner does not read the servers


def test_a_foreign_origin_or_no_session_is_refused(w):
    with pytest.raises(WebSocketDisconnect):
        with _connect(w) as ws:
            ws.receive_json()
    w.login()
    with pytest.raises(WebSocketDisconnect):
        with w.client.websocket_connect(
            _url(w), headers={"Origin": "https://evil.test"}
        ) as ws:
            ws.receive_json()


def test_a_closed_session_closes_the_socket(w):
    w.login()
    with _connect(w) as ws:
        _open(ws)
        ws.send_json({"subscribe": {"all": True}})
        ws.receive_json()
        w.users.close_all("owner")
        w.env.card(project="web")
        w.svc.hub.publish(w.env.store.events_since(w.env.store.last_seq() - 1))
        with pytest.raises(WebSocketDisconnect):
            while True:
                ws.receive_json()


def test_the_hub_first_tells_the_page_where_the_journal_is(w):
    # a page that has seen no event yet still knows what to ask after a reconnect
    w.env.card(project="web")
    w.login()
    with _connect(w) as ws:
        assert ws.receive_json() == {"hello": {"seq": w.env.store.last_seq()}}


def test_a_catch_up_too_long_to_replay_asks_the_page_to_reload(w, monkeypatch):
    from kd.cabinet.web import ws as hub

    monkeypatch.setattr(hub, "PAGE", 1)
    monkeypatch.setattr(hub, "AFTER_MAX", 2)
    w.login()
    seq = w.env.store.last_seq()
    for title in ("Раз", "Два", "Три", "Четыре"):
        w.env.card(project="web", title=title)
    with _connect(w) as ws:
        _open(ws)
        ws.send_json({"subscribe": {"all": True}})
        ws.receive_json()
        ws.send_json({"after": seq})
        messages = [ws.receive_json() for _ in range(3)]
        assert [("event" in m) for m in messages[:2]] == [True, True]
        assert messages[2] == {"reload": True}


def test_a_page_whose_queue_overflowed_is_asked_to_reload(monkeypatch):
    import asyncio

    from kd.cabinet.web import ws as hub

    class Socket:
        def __init__(self):
            self.sent = []

        async def send_json(self, message):
            self.sent.append(message)

    async def run():
        monkeypatch.setattr(hub, "QUEUE_MAX", 1)
        socket = Socket()
        conn = hub.Connection.__new__(hub.Connection)
        conn.loop, conn.queue, conn.overflow = (
            asyncio.get_running_loop(),
            asyncio.Queue(hub.QUEUE_MAX),
            False,
        )
        conn.put({"event": {"seq": 1}})
        conn.put({"event": {"seq": 2}})  # dropped: the queue is full
        conn.put(hub.CLOSE)  # dropped as well; the test ends the sender itself
        await asyncio.sleep(0)
        sender = asyncio.ensure_future(hub._send(conn, socket))
        await asyncio.sleep(0.01)
        sender.cancel()
        return socket.sent

    assert asyncio.run(run()) == [{"event": {"seq": 1}}, {"reload": True}]


def test_a_question_reaches_a_partner_only_on_her_projects(w):
    """The question's thread message is live, filtered by the card's project."""
    import asyncio

    secret = w.env.card(project="digipaddock", title="Тайна", assignee="@dev", status="In Progress")
    mine = w.env.card(project="web", title="Своя", assignee="@dev", status="In Progress")
    anna = w.other_client()
    w.login("anna", client=anna)
    executor = w.env.principal("digi")
    with _connect(w, anna) as ws:
        _open(ws)
        ws.send_json({"subscribe": {"all": True}})
        ws.receive_json()
        for card in (secret, mine):
            asyncio.run(
                w.svc.call(
                    executor,
                    {"op": "decision.open", "type": "question", "agent": "dev",
                     "card": card.id, "payload": {"text": "?"}},
                )
            )
        w.login()
        w.client.post(f"/api/cards/{mine.id}/messages", headers=w.headers(), json={"body": "конец"})
        got = []
        while not got or got[-1]["payload"].get("via") is not None or got[-1]["kind"] != "message.added":
            got.append(_next_event(ws))  # up to the owner's plain message, the last write
        assert [(e["kind"], e["card"], e["payload"].get("via")) for e in got] == [
            ("message.added", mine.id, "decision"),
            ("decision.opened", mine.id, None),
            ("message.added", mine.id, None),
        ]


def test_ws_agent_subscription_gets_chat_events_only_with_agents_chat(w):
    """the agent page subscribes to the agent — its chat and its runs outside cards, never a card's
    events; a person without agents.chat gets none of them."""
    from kd import session_config
    from kd.cabinet import api

    api.install_host(w.env.ctx, lambda: session_config.HostConfig({"analyst": 1}, 600))
    w.users.create("vera", "Вера", roles=[("viewer", "*")], actor="root")
    card = w.env.card(project="digipaddock", title="Карточка", assignee="@dev", status="In Progress")
    vera = w.other_client()
    w.login("vera", client=vera)
    w.login()
    with _connect(w) as ws, _connect(w, vera) as seen:
        for socket in (ws, seen):
            _open(socket)
            socket.send_json({"subscribe": {"agent": "analyst"}})
            assert socket.receive_json() == {"subscribed": {"all": False, "cards": [], "agents": ["analyst"]}}
        w.client.post(f"/api/cards/{card.id}/messages", headers=w.headers(), json={"body": "не сюда"})
        w.client.post("/api/agents/analyst/messages", headers=w.headers(), json={"body": "Привет"})
        first, second = _next_event(ws), _next_event(ws)
        assert {(first["kind"], first["card"]), (second["kind"], second["card"])} == {
            ("message.added", ""), ("command.queued", "")}
        seen.send_json({"subscribe": {"agent": "analyst"}})
        assert seen.receive_json()["subscribed"]["agents"] == ["analyst"]  # nothing came before it
        ws.send_json({"subscribe": {"card": card.id}})  # a card page: the agent's events stop
        reply = ws.receive_json()
        while "chats" in reply:  # the chat's counts may come in between
            reply = ws.receive_json()
        assert reply == {"subscribed": {"all": False, "cards": [card.id], "agents": []}}
