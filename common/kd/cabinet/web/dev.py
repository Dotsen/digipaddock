"""The dev cabinet's executor route: e2e plays an agent's board write through it.

``POST /api/dev/executor`` runs one socket operation as the board executor (the base user), as
``kd-task`` / ``kd-work`` reach the local socket on a host. ``create_app`` mounts it only in the dev
cabinet (``kd-cabinet serve --dev``); the production app has no such route. It still takes a signed-in
person from the cabinet's own page (session and CSRF), so another site cannot drive it.
"""

from fastapi import APIRouter, Depends, Request

from kd import naming
from kd.cabinet import api
from kd.cabinet.web import deps

router = APIRouter()


@router.post("/api/dev/executor")
async def executor(request: Request, user: deps.User = Depends(deps.writer)):
    body = await deps.json_body(request)
    principal = api.Principal("executor", -1, naming.base_user())
    return await deps.svc(request).call(principal, body)
