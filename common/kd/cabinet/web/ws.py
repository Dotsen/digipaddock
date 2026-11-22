"""The cabinet's WebSocket: journal events to the open pages, as each person may see them.

``/ws`` takes the session cookie and the cabinet's own Origin. The hub first sends
``{"hello": {"seq": n}}`` (the journal's last seq, what the page asks ``after`` on a reconnect even when
it saw no event). The page sends ``{"subscribe": {"card": "KD-n"}}`` (one card's page),
``{"subscribe": {"all": true}}`` (the feed, the board), ``{"subscribe": {"agent": "<name>"}}`` (an agent's page:
its chat and its runs outside cards; a card or an agent replaces the other) and ``{"after": seq}``
(what it missed while away);
the hub sends ``{"event": {...}}`` for the subscribed events of visible projects (a project's own events —
its name or route — to every page),
``{"badge": {"inbox": n, "incidents": m}}`` whenever the person's inbox count or the count of incidents not resolved
changes (``incidents`` only for who reads the servers),``{"chats": {agent: {read, unread, first,
last}}}`` whenever their agent chats' cursors or unread replies change (on connect too) and ``{"reload": true}`` when it
cannot replay what the page missed (a catch-up past ``AFTER_MAX``, a full queue). ``Hub.publish`` runs after every write (``Service._after``); a connection whose session is gone
is closed on the next publish.
"""

import asyncio
from typing import Optional, Set

from fastapi import APIRouter, WebSocket
from kd.cabinet import api, chats, incidents, sessions_api
from kd.cabinet.web import deps

router = APIRouter()
QUEUE_MAX = 1000  # a page that falls this far behind is asked to reload
AFTER_MAX = 2000  # events one catch-up reads at most
PAGE = 500
INBOX = ("decision.", "card.", "incident.")  # event kinds that may change an inbox (an incident's alarm)
CLOSE_POLICY = 4401  # the session is gone
CLOSE = object()


class Connection:
    def __init__(self, websocket, user: deps.User, loop):
        self.websocket, self.user, self.loop = websocket, user, loop
        self.principal = deps.principal(user)
        self.cards: Set[str] = set()
        self.agents: Set[str] = set()
        self.all = False
        self.badge: Optional[dict] = None
        self.chats: dict = {}  # no chat yet: nothing to send
        self.queue: asyncio.Queue = asyncio.Queue(QUEUE_MAX)
        self.overflow = False  # a message was dropped: the page must reload

    def wants(self, event: dict) -> bool:
        if event.get("kind") == "chat.read":
            return False  # a reader's cursor reaches the pages as {"chats"}, not as an event
        if self.all or event.get("card") in self.cards or event.get("kind", "").startswith("project."):
            return True  # a project's name or route: every page shows them
        return sessions_api.chat_event(event) and event["payload"]["agent"] in self.agents

    def put(self, message):
        """Queue a message from any thread (the writer may run on another loop in tests)."""

        def put():
            try:
                self.queue.put_nowait(message)
            except asyncio.QueueFull:
                self.overflow = True  # the sender asks the page to reload

        self.loop.call_soon_threadsafe(put)


class Hub:
    def __init__(self, service):
        self.service = service
        self.connections: Set[Connection] = set()

    def connect(self, websocket, user: deps.User) -> Connection:
        conn = Connection(websocket, user, asyncio.get_running_loop())
        self.connections.add(conn)
        conn.put({"hello": {"seq": self.service.ctx.store.last_seq()}})
        self._badge(conn)
        self._chats(conn)
        return conn

    def disconnect(self, conn: Connection):
        self.connections.discard(conn)

    def publish(self, events):
        """New journal events: each connection gets the visible, subscribed ones (and a new badge)."""
        inbox = any(e["kind"].startswith(INBOX) for e in events)
        chat = any(_chat_change(e) for e in events)
        ctx = self.service.ctx
        for conn in list(self.connections):
            if not self.service.users.session_alive(
                conn.user.session_id, self.service.now()
            ):
                conn.put(CLOSE)
                continue
            for event in api.event_filter(ctx, conn.principal)(events):
                if conn.wants(event):
                    conn.put({"event": event})
            if inbox:
                self._badge(conn)
            if chat:
                self._chats(conn)

    def _badge(self, conn: Connection):
        ctx = self.service.ctx
        badge = {"inbox": api.inbox_count(ctx, conn.principal)}
        active = incidents.active_count(ctx, conn.principal)
        if active is not None:
            badge["incidents"] = active  # the menu's count of incidents not resolved
        if badge != conn.badge:
            conn.badge = badge
            conn.put({"badge": badge})

    def _chats(self, conn: Connection):
        """The person's agent chats — their cursor and unread replies — whenever they change."""
        try:
            found = chats.unread(self.service.ctx, conn.principal)
        except api.ApiError:
            return
        if found != conn.chats:
            conn.chats = found
            conn.put({"chats": found})

    async def catch_up(self, conn: Connection, after: int):
        """The subscribed events after ``after`` the person may see (read under the service's lock); past
        ``AFTER_MAX`` the page is asked to reload instead."""
        cursor, read = after, 0
        while True:
            page = await self.service.call(
                conn.principal, {"op": "events.since", "after": cursor, "limit": PAGE}
            )
            for event in page["events"]:
                if conn.wants(event):
                    conn.put({"event": event})
            if page["last"] == cursor:
                return
            cursor, read = page["last"], read + PAGE
            if read >= AFTER_MAX:
                conn.put({"reload": True})
                return


def _chat_change(event) -> bool:
    """A new message in an agent chat or a moved read cursor: the chats' counts may change."""
    if event["kind"] == "chat.read":
        return True
    return event["kind"] == "message.added" and sessions_api.chat_event(event)


async def _receive(hub: Hub, conn: Connection, websocket: WebSocket):
    while True:
        message = await websocket.receive_json()
        if not isinstance(message, dict):
            continue
        subscribe = message.get("subscribe")
        if isinstance(subscribe, dict):
            if "all" in subscribe:
                conn.all = bool(subscribe["all"])
            if "card" in subscribe or "agent" in subscribe:
                card, agent = subscribe.get("card"), subscribe.get("agent")
                conn.cards = {card} if isinstance(card, str) and card else set()
                conn.agents = {agent} if isinstance(agent, str) and agent else set()
            conn.put({"subscribed": {"all": conn.all, "cards": sorted(conn.cards), "agents": sorted(conn.agents)}})
        after = message.get("after")
        if isinstance(after, int) and not isinstance(after, bool) and after >= 0:
            await hub.catch_up(conn, after)


async def _send(conn: Connection, websocket: WebSocket):
    while True:
        message = await conn.queue.get()
        if message is CLOSE:
            await websocket.close(code=CLOSE_POLICY)
            return
        await websocket.send_json(message)
        if conn.overflow:
            conn.overflow = False
            await websocket.send_json({"reload": True})


@router.websocket("/ws")
async def socket(websocket: WebSocket):
    service = websocket.app.state.svc
    hub = service.hub
    if hub is None or websocket.headers.get("origin", "") != service.cfg.origin:
        await websocket.close(code=1008)
        return
    user = await deps.optional_user(websocket)
    if user is None:
        await websocket.close(code=CLOSE_POLICY)
        return
    await websocket.accept()
    conn = hub.connect(websocket, user)
    tasks = [
        asyncio.ensure_future(_receive(hub, conn, websocket)),
        asyncio.ensure_future(_send(conn, websocket)),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        hub.disconnect(conn)
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — the socket is gone either way
                pass
