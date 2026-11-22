"""The monitor's settings over HTTP: read with ``servers.read``, saved with ``settings``.

``PUT /api/monitor-settings {revision, changes, stepup?}`` — the cabinet classifies the changes itself: a save with any
weakening answers 403 ``stepup_required`` until it carries a step-up of purpose ``monitor.settings`` (one signature for
the whole save), and nothing changes before that.

``GET|POST /api/monitor-test`` — the owner's test critical alert: its state, and the button (``settings``).
"""

from fastapi import APIRouter, Depends, Request

from kd.cabinet import api
from kd.cabinet.web import deps, stepup
from kd.cabinet.web.errors import Failure

router = APIRouter()
PURPOSE = "monitor.settings"


async def call(request: Request, user: deps.User, op: dict) -> dict:
    return await deps.svc(request).call(deps.principal(user), op)


@router.get("/api/monitor-settings")
async def read(request: Request, user: deps.User = Depends(deps.current_user)):
    return await call(request, user, {"op": "monitor.settings.get"})


@router.put("/api/monitor-settings")
async def save(request: Request, user: deps.User = Depends(deps.writer)):
    body = await deps.json_body(request)
    if not deps.svc(request).users.can(user.id, "settings"):  # before any step-up is spent
        raise Failure(403, "refused", "settings is needed")
    op = {"op": "monitor.settings.save", "revision": body.get("revision"), "changes": body.get("changes"),
          "signed": False}
    # unsigned first: a bad value or a stale revision answers before the step-up is spent, a tightening needs none
    try:
        return await call(request, user, op)
    except api.ApiError as exc:
        if exc.kind != "stepup_required" or body.get("stepup") is None:
            raise
    await stepup.require(request, user, body, PURPOSE)
    return await call(request, user, {**op, "signed": True})


@router.get("/api/monitor-test")
async def test_read(request: Request, user: deps.User = Depends(deps.current_user)):
    """The last test critical alert, with ``servers.read``."""
    return {"test": (await call(request, user, {"op": "monitor.test.get"}))["test"]}


@router.post("/api/monitor-test")
async def test_run(request: Request, user: deps.User = Depends(deps.writer)):
    """The "Test critical alert" action (``settings``): a new test, or the one waiting or running (``repeat``)."""
    found = await call(request, user, {"op": "monitor.test.request"})
    return {"test": found["test"], "repeat": found["repeat"]}
