"""Read cursors of agent chats: what of an agent's replies each person has been shown.

``user.chat.unread`` → ``{chats: {agent: {read, unread, first, last}}}`` — every agent chat the person may read
(``agents.chat``): their cursor, the replies past it (``Store.chat_state``: one turn's texts are one reply), the
first unread message and the chat's last seq. ``user.chat.read {agent, seq}`` moves the person's own cursor up to
the message their page showed (never back, never past the chat's end) and returns the chat's state. A page reports
only what it showed in a visible, focused window; a snapshot, a push or a replay reads nothing. An agent's
"Monitor" is a second such thread — ``space: "monitor"`` in a request, ``<agent>/monitor`` in ``chats``.
"""

import re

from kd import agents
from kd.cabinet import api, sessions_api, store, users

DEVICE = re.compile(r"[A-Za-z0-9-]{8,64}")
PRESENCE_LEASE = 25  # seconds a chat page's presence holds without renewal


def _readable(ctx, principal) -> bool:
    return api.allowed(ctx, principal, "agents.chat")


def _names(ctx) -> set:
    try:
        return set(agents.names(ctx.registry_path))
    except ValueError as exc:
        raise api.ApiError("unavailable", str(exc))


def key(agent: str, space: str = "chat") -> str:
    """A thread's key in ``chats``: the agent for its chat, ``<agent>/<space>`` for another ("Monitor")."""
    return agent if space == "chat" else f"{agent}/{space}"


def unread(ctx, principal) -> dict:
    """agent (or ``<agent>/monitor``) → that thread's state for the person; nothing without ``agents.chat``. A work
    log (``Store.WORK_LOGS``) counts nothing."""
    if not _readable(ctx, principal):
        return {}
    names = _names(ctx)
    return {
        key(agent, space): ctx.store.chat_state(principal.user, thread)
        for (agent, space), thread in ctx.store.agent_spaces().items()
        if agent in names and (agent, space) not in store.WORK_LOGS
    }


def _unread(ctx, principal, request) -> dict:
    return {"chats": unread(ctx, principal)}


def _read(ctx, principal, request) -> dict:
    api._require(ctx, principal, "agents.chat")
    agent = request.get("agent")
    if not isinstance(agent, str) or not agents.NAME_RE.fullmatch(agent) or agent not in _names(ctx):
        raise api.ApiError("not_found", "no such agent")
    seq = request.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise api.ApiError("invalid", "seq is a message number")
    thread = ctx.store.agent_spaces().get((agent, sessions_api.space_of(request)))
    if thread is None:
        return {"chat": {"read": 0, "unread": 0, "first": None, "last": 0}}
    return {"chat": ctx.store.chat_read(principal.user, thread, seq)}


def _presence(ctx, principal, request) -> dict:
    """``user.chat.presence {agent, device, on}``: a visible, focused chat page says it shows the chat (renewed every
    10 s by the page, a lease of ``PRESENCE_LEASE`` s) or that it stopped; a reply then needs no push to this
    person. Another person, another tab of the app or a hidden page suppresses nothing."""
    api._require(ctx, principal, "agents.chat")
    agent = request.get("agent")
    if not isinstance(agent, str) or not agents.NAME_RE.fullmatch(agent) or agent not in _names(ctx):
        raise api.ApiError("not_found", "no such agent")
    device = request.get("device")
    if not isinstance(device, str) or not DEVICE.fullmatch(device):
        raise api.ApiError("invalid", "device is 8 to 64 letters, digits or dashes")
    on = request.get("on")
    if not isinstance(on, bool):
        raise api.ApiError("invalid", "on is true or false")
    thread = ctx.store.agent_thread(agent, sessions_api.space_of(request))
    until = users.shift(ctx.now(), seconds=PRESENCE_LEASE) if on else None
    ctx.store.chat_presence(principal.user, thread, device, until)
    return {"until": until}


OPS = {
    "user.chat.unread": (frozenset({"user"}), _unread),
    "user.chat.read": (frozenset({"user"}), _read),
    "user.chat.presence": (frozenset({"user"}), _presence),
}
api.OPS.update(OPS)  # the board API serves them (api imports this module at its end)
