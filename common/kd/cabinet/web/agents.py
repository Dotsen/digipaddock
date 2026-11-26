"""Agents and token spend over HTTP: who works on what, its state from facts, today's spend.

Agents come from the host's registry (``agents.json``), their specializations from the repository's
registry. An agent's state and every number about it (``kd.cabinet.team``) come from its
sessions, commands, questions and cards on the threads the person may read — the cards they see, its chat with
``agents.chat``, its «Monitor» with ``servers.read`` too. Spend is ``kd-usage``'s ledger (``usage.jsonl``): today's rows per agent and the ledger itself,
both only with ``settings``. An agent on the session host has ``host`` — its sessions at work and
waiting for a slot (of those threads) and its slots — and its sessions on those threads.
"""

import json
from datetime import date, timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request

from kd import agents as registry
from kd.token_pricing import with_price
from kd.cabinet import api, team, users
from kd.cabinet.web import deps
from kd.cabinet.web.cards import call, page_op, thread_reply, with_names
from kd.cabinet.web.errors import Failure

router = APIRouter()
DAYS_MAX = 90
PROGRESS_SHOWN = 10  # the agent page's latest progress lines


def _usage(service) -> List[dict]:
    """The ledger's rows, each priced: a cabinet model's by its table, the rest by the reference list."""
    table = {m["id"]: m for m in service.ctx.store.llm_models()}
    rows = []
    try:
        with open(service.usage_path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and isinstance(row.get("day"), str):
                    rows.append(with_price(row, table))
    except OSError:
        return []
    return rows


def _today(service) -> str:
    return (
        users.parse_time(service.now())
        .astimezone(ZoneInfo(service.cfg.timezone))
        .date()
        .isoformat()
    )


def _spend(rows, name, day) -> Optional[dict]:
    # the Hindsight server's category is spend, not an agent: it is in /api/usage only
    mine = [r for r in rows if r.get("agent") == name and r["day"] == day and r.get("category") != "hindsight"]
    if not mine:
        return None
    cost = round(sum(float(r.get("cost_usd") or 0) for r in mine), 4)
    return {"cost": cost, "calls": sum(int(r.get("requests") or 0) for r in mine)}




async def _agents(request: Request, user: deps.User) -> List[dict]:
    service = deps.svc(request)
    path = service.ctx.registry_path
    try:
        specs = service.ctx.specs()
    except api.ApiError:
        specs = {}
    cards = (await call(request, user, {"op": "card.list"}))["cards"]
    # spend is money: only a person with settings sees it, as with the ledger itself
    money = service.users.can(user.id, "settings")
    rows, day = (_usage(service) if money else []), _today(service)
    spaces = api.agent_spaces(service.ctx, deps.principal(user))
    config = service.ctx.host_config()
    out = []
    for name in registry.names(path):
        handle = f"@{name}"
        hosted = config.hosted(name)
        found = team.facts(service.ctx.store, name, cards=cards, spaces=spaces, hosted=hosted, now=service.now())
        counts = found.pop("cards")
        out.append(
            {
                "name": name,
                "state": found.pop("state"),
                "facts": found,
                "cards": counts,
                "specs": sorted(specs.get(handle, ())),
                "usage_today": _spend(rows, name, day),
                # the slots it has and, of the threads this person reads, how many work or wait for one
                "host": {"working": found["working"], "queued": found["queued"], "slots": config.slots(name)}
                if hosted
                else None,
                "_cards": [c for c in cards if c["assignee"] == handle],
                "_readable": {c["id"] for c in cards},
            }
        )
    return out


@router.get("/api/agents")
async def agents(request: Request, user: deps.User = Depends(deps.current_user)):
    return [
        {k: v for k, v in a.items() if not k.startswith("_")}
        for a in await _agents(request, user)
    ]


@router.get("/api/agents/{name}")
async def agent(
    name: str, request: Request, user: deps.User = Depends(deps.current_user)
):
    """One agent: the counts move to ``counts``, ``cards`` lists the cards the person may read, ``progress``
    the agent's latest progress lines on those cards."""
    for found in await _agents(request, user):
        if found["name"] == name:
            rest = {
                k: v for k, v in found.items() if k != "cards" and not k.startswith("_")
            }
            lines = deps.svc(request).ctx.store.recent_messages_of(
                f"agent:{name}", "progress"
            )
            progress = [m for m in lines if m["card"] in found["_readable"]]
            sessions = []
            if found["host"] is not None:
                try:
                    op = {"op": "session.list", "agent": name}
                    sessions = (await call(request, user, op))["sessions"]
                except api.ApiError as exc:
                    if exc.kind != "refused":
                        raise
            # its "Monitor" thread, once the monitor (or a person) opened it, for who may read it (the servers too)
            service = deps.svc(request)
            monitor = (api.reads_agent_thread(service.ctx, deps.principal(user), "monitor")
                       and (name, "monitor") in service.ctx.store.agent_spaces())
            return {
                **rest,
                "counts": found["cards"],
                "cards": found["_cards"],
                "progress": progress[:PROGRESS_SHOWN],
                "sessions": sessions,
                "monitor": monitor,
            }
    raise Failure(404, "not_found", "no such agent")


async def _messages(request, user, name, space, after, before, limit):
    op = {"op": "thread.messages", "agent": name, "space": space, "after": max(0, after)}
    return thread_reply(request, await call(request, user, page_op(op, before, limit)))


@router.get("/api/agents/{name}/messages")
async def chat(
    name: str,
    request: Request,
    user: deps.User = Depends(deps.current_user),
    after: int = 0,
    before: Optional[int] = None,
    limit: Optional[int] = None,
):
    """The agent's chat as ``{messages, watermark, more}``, as a card's thread."""
    return await _messages(request, user, name, "chat", after, before, limit)


@router.get("/api/agents/{name}/monitor/messages")
async def monitor(
    name: str,
    request: Request,
    user: deps.User = Depends(deps.current_user),
    after: int = 0,
    before: Optional[int] = None,
    limit: Optional[int] = None,
):
    """The agent's "Monitor" thread, as its chat."""
    return await _messages(request, user, name, "monitor", after, before, limit)


async def _post(request, user, name, space):
    body = await deps.json_body(request)
    op = {
        "op": "user.agent.message",
        "agent": name,
        "space": space,
        "text": body.get("body"),
        "client_key": body.get("client_key", ""),
    }
    message = (await call(request, user, op))["message"]
    return with_names(deps.svc(request), [message])[0]


@router.post("/api/agents/{name}/messages")
async def chat_post(name: str, request: Request, user: deps.User = Depends(deps.writer)):
    return await _post(request, user, name, "chat")


@router.post("/api/agents/{name}/monitor/messages")
async def monitor_post(name: str, request: Request, user: deps.User = Depends(deps.writer)):
    """A person's message in "Monitor" wakes the thread's own session."""
    return await _post(request, user, name, "monitor")


@router.get("/api/chats")
async def chats(request: Request, user: deps.User = Depends(deps.current_user)):
    """The person's agent chats: ``{agent: {read, unread, first, last}}``."""
    return (await call(request, user, {"op": "user.chat.unread"}))["chats"]


async def _read(request, user, name, space):
    body = await deps.json_body(request)
    op = {"op": "user.chat.read", "agent": name, "space": space, "seq": body.get("seq")}
    return (await call(request, user, op))["chat"]


@router.post("/api/agents/{name}/read")
async def chat_read(name: str, request: Request, user: deps.User = Depends(deps.writer)):
    """``{seq}`` — the page showed the chat up to this message: the person's cursor moves up."""
    return await _read(request, user, name, "chat")


@router.post("/api/agents/{name}/monitor/read")
async def monitor_read(name: str, request: Request, user: deps.User = Depends(deps.writer)):
    """The same for "Monitor"."""
    return await _read(request, user, name, "monitor")


async def _presence(request, user, name, space):
    body = await deps.json_body(request)
    op = {"op": "user.chat.presence", "agent": name, "space": space, "device": body.get("device"),
          "on": body.get("on")}
    return await call(request, user, op)


@router.post("/api/agents/{name}/presence")
async def chat_presence(name: str, request: Request, user: deps.User = Depends(deps.writer)):
    """``{device, on}`` — this page shows the agent's chat now (or stopped): no push for what it shows."""
    return await _presence(request, user, name, "chat")


@router.post("/api/agents/{name}/monitor/presence")
async def monitor_presence(name: str, request: Request, user: deps.User = Depends(deps.writer)):
    """The same for "Monitor"."""
    return await _presence(request, user, name, "monitor")


@router.get("/api/usage")
async def usage(
    request: Request, user: deps.User = Depends(deps.current_user), days: int = 7
):
    service = deps.svc(request)
    if not service.users.can(user.id, "settings"):
        raise Failure(403, "refused", "no permission settings")
    if not 0 < days <= DAYS_MAX:
        raise Failure(400, "invalid", f"days is 1 to {DAYS_MAX}")
    today = _today(service)
    first = (date.fromisoformat(today) - timedelta(days=days - 1)).isoformat()
    return [r for r in _usage(service) if first <= r["day"] <= today]
