"""The overview over HTTP: ``GET /api/dashboard`` and the person's «seen» mark.

``GET /api/dashboard?project&period`` is ``overview.get`` with the actors named for the page; ``POST
/api/dashboard/seen {seq}`` moves the person's own mark (only up): changes past it are «new».
"""

from typing import Optional

from fastapi import APIRouter, Depends, Request

from kd.cabinet.web import deps
from kd.cabinet.web.cards import author_name, call

router = APIRouter()


@router.get("/api/dashboard")
async def dashboard(
    request: Request,
    user: deps.User = Depends(deps.current_user),
    project: Optional[str] = None,
    period: Optional[str] = None,
):
    op = {"op": "overview.get", **({"project": project} if project else {}), **({"period": period} if period else {})}
    found = await call(request, user, op)
    service = deps.svc(request)
    for change in found["changes"]:
        change["actor_name"] = author_name(service, change["actor"])
    return found


@router.post("/api/dashboard/seen")
async def seen(request: Request, user: deps.User = Depends(deps.writer)):
    body = await deps.json_body(request)
    op = {"op": "user.mark", "name": "overview_seen", "value": body.get("seq")}
    return (await call(request, user, op))["marks"]
