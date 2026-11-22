"""The monitor's servers and incidents P-n over HTTP.

Reading needs ``servers.read``, acting (acknowledge, mute, "Check now") ``incidents.ack``; the board API
answers ``refused`` (403) to everyone else — a partner too, on every route:

- ``GET /api/servers`` — the servers with their worst check, open incidents and last "Check now";
- ``GET /api/servers/{alias}`` — a server: checks, the week's status changes, incidents, cards, trusted lists;
- ``POST /api/servers/{alias}/check`` — "Check now" (a second press answers the active request);
- ``GET /api/incidents?status&server`` — incidents; ``GET /api/incidents/{P-n}`` — one, its timeline and cards;
- ``POST /api/incidents/{P-n}/ack``, ``/mute {for: 1h|1d|7d}``, ``/unmute``;
- ``POST /api/monitor-trust {kind, incident|server|cidr, label?}`` and ``/api/monitor-trust/remove {cidr}``:
  the owner's monitor trust — a decision he signs right after, or an address off the list without a signature;
  ``/api/monitor-trust/batch {incidents, kind?}`` — accepts (or revokes) of many P-n as one bundle.

``GET /api/alerts?before&limit`` is the incidents page's other list — the platform's own alerts, notices without a
card (a failed unit, a release, a factory operation): ``settings`` reads them, as in the feed.
"""

from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Request

from kd.cabinet import users
from kd.cabinet.web import deps
from kd.cabinet.web.cards import author_name, call
from kd.cabinet.web.errors import Failure

router = APIRouter()
MUTE = {"1h": timedelta(hours=1), "1d": timedelta(days=1), "7d": timedelta(days=7)}


@router.get("/api/incidents/query")
async def incident_query(request: Request, user: deps.User = Depends(deps.current_user),
                         status: str = "active", server: str = "", level: str = "", check: str = "",
                         q: str = "", cursor: str = "", limit: int = 50, assignee: str = "",
                         processing: str = "", since: str = "", until: str = "", group: str = ""):
    found = await call(request, user, {"op": "incident.query", "status": status, "server": server,
                                       "level": level, "check": check, "q": q, "cursor": cursor, "limit": limit,
                                       "assignee": assignee, "processing": processing, "since": since, "until": until,
                                       "group": group})
    found.pop("ok", None)
    return found


@router.post("/api/incidents/{pid}/handoff")
async def handoff(pid: str, request: Request, user: deps.User = Depends(deps.writer)):
    body = await deps.json_body(request)
    found = await call(request, user, {"op": "incident.handoff", "incident": pid,
                                       "project": body.get("project"), "comment": body.get("comment", "")})
    found.pop("ok", None)
    return found


@router.post("/api/incidents/{pid}/resume")
async def resume(pid: str, request: Request, user: deps.User = Depends(deps.writer)):
    return (await call(request, user, {"op": "incident.unmute", "incident": pid, "preserve_ack": True}))["incident"]


@router.post("/api/incidents/{pid}/link")
async def link(pid: str, request: Request, user: deps.User = Depends(deps.writer)):
    body = await deps.json_body(request)
    return await call(request, user, {"op": "incident.link", "incident": pid, "card": body.get("card"),
                                      "revision": body.get("revision"), "transfer": body.get("transfer", False)})


@router.post("/api/incidents/{pid}/retry")
async def retry(pid: str, request: Request, user: deps.User = Depends(deps.writer)):
    return await call(request, user, {"op": "incident.retry", "incident": pid})


@router.post("/api/incidents/{pid}/unack")
async def unack(pid: str, request: Request, user: deps.User = Depends(deps.writer)):
    return (await call(request, user, {"op": "incident.unack", "incident": pid}))["incident"]


def _named(service, found: dict) -> dict:
    for event in found.get("timeline", []):
        event["actor_name"] = author_name(service, event["actor"])
    return found


@router.get("/api/alerts")
async def alerts(request: Request, user: deps.User = Depends(deps.current_user), before: Optional[int] = None,
                 limit: int = 20):
    op = {"op": "notice.list", "limit": limit, **({"before": before} if before is not None else {})}
    found = await call(request, user, op)
    return {"items": found["items"], "next": found["next"]}


@router.get("/api/servers")
async def servers(request: Request, user: deps.User = Depends(deps.current_user)):
    return (await call(request, user, {"op": "server.list"}))["items"]


@router.get("/api/contours")
async def contours(request: Request, user: deps.User = Depends(deps.current_user)):
    """The dev.check contours: state, capacity, what runs there — the Servers page's table."""
    return (await call(request, user, {"op": "contour.list"}))["contours"]


@router.get("/api/servers/{alias}")
async def server(alias: str, request: Request, user: deps.User = Depends(deps.current_user)):
    found = await call(request, user, {"op": "server.get", "alias": alias})
    found.pop("ok", None)
    return found


@router.post("/api/servers/{alias}/check")
async def check_now(alias: str, request: Request, user: deps.User = Depends(deps.writer)):
    found = await call(request, user, {"op": "server.check.request", "alias": alias})
    return {"request": found["request"], "repeat": found["repeat"]}


@router.get("/api/incidents")
async def incident_list(
    request: Request,
    user: deps.User = Depends(deps.current_user),
    status: Optional[str] = None,
    server: Optional[str] = None,
):
    op = {"op": "incident.list", **({"status": status} if status else {}), **({"server": server} if server else {})}
    return (await call(request, user, op))["items"]


@router.get("/api/incidents/{pid}")
async def incident(pid: str, request: Request, user: deps.User = Depends(deps.current_user)):
    found = await call(request, user, {"op": "incident.get", "incident": pid})
    found.pop("ok", None)
    return _named(deps.svc(request), found)


@router.post("/api/incidents/{pid}/ack")
async def ack(pid: str, request: Request, user: deps.User = Depends(deps.writer)):
    return (await call(request, user, {"op": "incident.ack", "incident": pid}))["incident"]


@router.post("/api/incidents/{pid}/mute")
async def mute(pid: str, request: Request, user: deps.User = Depends(deps.writer)):
    body = await deps.json_body(request)
    span = MUTE.get(body.get("for"))
    if span is None:
        raise Failure(400, "invalid", "for is one of " + ", ".join(MUTE))
    until = (users.parse_time(deps.svc(request).now()) + span).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (await call(request, user, {"op": "incident.mute", "incident": pid, "until": until}))["incident"]


@router.post("/api/incidents/{pid}/unmute")
async def unmute(pid: str, request: Request, user: deps.User = Depends(deps.writer)):
    return (await call(request, user, {"op": "incident.unmute", "incident": pid}))["incident"]


TRUST_FIELDS = ("kind", "incident", "server", "cidr", "label", "text")


@router.post("/api/monitor-trust")
async def trust_propose(request: Request, user: deps.User = Depends(deps.writer)):
    """The buttons "Trust" on a P-n, "Capture a baseline" and "Add an address" on a server: the monitor_trust decision —
    a new one or the same open proposal; the page signs it right after through the decisions' challenge."""
    body = await deps.json_body(request)
    op = {"op": "monitor_trust.propose", **{k: body[k] for k in TRUST_FIELDS if k in body}}
    found = await call(request, user, op)
    return {"decision": found["decision"], "repeat": found["repeat"]}


@router.post("/api/monitor-trust/batch")
async def trust_propose_many(request: Request, user: deps.User = Depends(deps.writer)):
    """The "Accept into the baseline" action for many P-n at once: one bundle of accepts, signed once right after."""
    body = await deps.json_body(request)
    op = {"op": "monitor_trust.propose_many", "incidents": body.get("incidents")}
    if "kind" in body:  # "Access revoked" for many logins
        op["kind"] = body["kind"]
    found = await call(request, user, op)
    return {"decisions": found["decisions"], "refused": found["refused"]}


@router.post("/api/monitor-trust/remove")
async def trust_remove(request: Request, user: deps.User = Depends(deps.writer)):
    """The "Remove" button at a trusted address: no signature — the monitor takes it off on its next tick."""
    body = await deps.json_body(request)
    found = await call(request, user, {"op": "monitor_trust.remove", "cidr": body.get("cidr")})
    return {"removal": found["removal"], "repeat": found["repeat"]}
