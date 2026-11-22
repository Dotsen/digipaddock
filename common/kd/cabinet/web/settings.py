"""Settings over HTTP: the person's own (profile, passkeys, sessions, notifications, push
devices) and the owner's (people and roles).

Removing a passkey, managing people, changing roles and closing another person's sessions need a step-up
(``stepup``): a stolen session alone changes none of them. The last passkey of a person who has no
password with TOTP to fall back on stays (409): without it they could not sign in or sign decisions.
"""

from fastapi import APIRouter, Depends, Request

from kd.cabinet import perms
from kd.cabinet.web import deps, stepup
from kd.cabinet.web.cards import call
from kd.cabinet.web.errors import Failure

router = APIRouter()


# --- the person's own ------------------------------------------------------------------------------
@router.patch("/api/me")
async def profile(request: Request, user: deps.User = Depends(deps.writer)):
    from kd.cabinet.web import auth

    body = await deps.json_body(request)
    op = {"op": "user.update", "id": user.id}
    op.update({k: body[k] for k in ("name", "language", "timezone") if k in body})
    await call(request, user, op)
    return auth.me(deps.svc(request), user.id, user.session_id)


@router.get("/api/me/passkeys")
async def passkeys_list(request: Request, user: deps.User = Depends(deps.current_user)):
    return deps.svc(request).users.credentials(user.id)


@router.delete("/api/me/passkeys/{row_id}")
async def passkey_remove(
    row_id: int, request: Request, user: deps.User = Depends(deps.writer)
):
    service = deps.svc(request)
    body = await deps.json_body(request) if await request.body() else {}
    await stepup.require(request, user, body, "passkey.remove")
    keys = service.users.credentials(user.id)
    if row_id not in {k["id"] for k in keys}:
        raise Failure(404, "not_found", "no such passkey")
    second = service.users.has_second_factor(user.id)
    if len(keys) == 1 and not (second["password"] and second["totp"]):
        raise Failure(
            409, "conflict", "the last passkey stays until a password with TOTP is set"
        )
    await service.locked(lambda: service.users.remove_credential(user.id, row_id))
    return service.users.credentials(user.id)


@router.get("/api/me/sessions")
async def sessions(request: Request, user: deps.User = Depends(deps.current_user)):
    found = deps.svc(request).users.sessions(user.id)
    return [{**s, "current": s["id"] == user.session_id} for s in found]


@router.delete("/api/me/sessions/{session_id}")
async def session_close(
    session_id: str, request: Request, user: deps.User = Depends(deps.writer)
):
    service = deps.svc(request)
    closed = await service.locked(
        lambda: service.users.close_session_id(session_id, user_id=user.id)
    )
    if not closed:
        raise Failure(404, "not_found", "no such session")
    return {}


@router.post("/api/me/code")
async def own_code(request: Request, user: deps.User = Depends(deps.writer)):
    """A sign-in code for the person's new device, the same as ``kd-cabinet user code``: 15 minutes,
    a new one cancels the unused one. It is a new sign-in, so a passkey or TOTP confirms it."""
    body = await deps.json_body(request) if await request.body() else {}
    await stepup.require(request, user, body, "code.self")
    return await call(request, user, {"op": "user.code", "id": user.id})


@router.get("/api/me/notifications")
async def notifications(request: Request, user: deps.User = Depends(deps.current_user)):
    prefs = (await call(request, user, {"op": "prefs.get"}))["prefs"]
    return _shape(prefs)


def _shape(prefs: dict) -> dict:
    return {
        "kinds": {k: v for k, v in prefs.items() if k != "quiet"},
        "quiet": prefs.get("quiet", ""),
    }


@router.put("/api/me/notifications")
async def notifications_set(request: Request, user: deps.User = Depends(deps.writer)):
    body = await deps.json_body(request)
    kinds = body.get("kinds", {})
    if not isinstance(kinds, dict):
        raise Failure(400, "invalid", "kinds must map a kind to now, batch or feed")
    prefs = dict(kinds)
    if "quiet" in body:
        prefs["quiet"] = body["quiet"]
    stored = (await call(request, user, {"op": "prefs.set", "prefs": prefs}))["prefs"]
    return _shape(stored)


@router.post("/api/push/subscribe")
async def push_subscribe(request: Request, user: deps.User = Depends(deps.writer)):
    body = await deps.json_body(request)
    subscription = body.get("subscription")
    if not isinstance(subscription, dict):
        raise Failure(400, "invalid", "subscription is the browser's PushSubscription")
    op = {
        "op": "push.subscribe",
        "endpoint": subscription.get("endpoint"),
        "keys": subscription.get("keys"),
        "device": deps.device(request, body.get("device")),
    }
    return (await call(request, user, op))["subscriptions"]


@router.post("/api/push/unsubscribe")
async def push_unsubscribe(request: Request, user: deps.User = Depends(deps.writer)):
    body = await deps.json_body(request)
    op = {"op": "push.unsubscribe", "endpoint": body.get("endpoint")}
    return (await call(request, user, op))["subscriptions"]


@router.post("/api/push/state")
async def push_state(request: Request, user: deps.User = Depends(deps.writer)):
    """``{"mine": bool, "last": {result, at, sent} | null}`` for the browser's subscription endpoint (the endpoint is a
    secret: never listed, only asked); ``last`` — this device's last delivery as the gateway answered."""
    body = await deps.json_body(request)
    found = await call(request, user, {"op": "push.mine", "endpoint": body.get("endpoint")})
    return {"mine": found["mine"], "last": found["last"]}


@router.post("/api/push/test")
async def push_test(request: Request, incident: str = "", user: deps.User = Depends(deps.writer)):
    service = deps.svc(request)
    if service.notifier is None:
        raise Failure(503, "unavailable", "notifications are not served here")
    found = await call(request, user, {"op":"incident.get", "incident":incident}) if incident else None
    row = await service.locked(lambda: service.notifier.test(user.id, service.now(), incident=found["incident"] if found else None))
    return {"notification": row}


# --- people and roles ------------------------------------------------------------------------------
def _roles(value) -> list:
    """``[{role, projects}]`` -> ``[[role, projects]]`` as the board API takes them."""
    if not isinstance(value, list) or not all(
        isinstance(r, dict) and "role" in r for r in value
    ):
        raise Failure(400, "invalid", "roles are a list of {role, projects}")
    return [[r["role"], r.get("projects", "*")] for r in value]


@router.get("/api/users")
async def people(request: Request, user: deps.User = Depends(deps.current_user)):
    return await call(request, user, {"op": "user.list"})


@router.post("/api/users")
async def person_add(request: Request, user: deps.User = Depends(deps.writer)):
    body = await deps.json_body(request)
    _manager(request, user)
    await stepup.require(request, user, body, "users.manage")
    op = {
        "op": "user.create",
        "id": body.get("id"),
        "name": body.get("name"),
        "roles": _roles(body.get("roles")),
    }
    op.update({k: body[k] for k in ("language", "timezone") if k in body})
    return await call(request, user, op)


@router.patch("/api/users/{user_id}")
async def person_change(
    user_id: str, request: Request, user: deps.User = Depends(deps.writer)
):
    body = await deps.json_body(request)
    _manager(request, user)
    await stepup.require(request, user, body, "users.manage")
    op = {"op": "user.update", "id": user_id}
    op.update(
        {k: body[k] for k in ("name", "status", "language", "timezone") if k in body}
    )
    if "roles" in body:
        op["roles"] = _roles(body["roles"])
    return (await call(request, user, op))["user"]


@router.post("/api/users/{user_id}/code")
async def person_code(
    user_id: str, request: Request, user: deps.User = Depends(deps.writer)
):
    body = await deps.json_body(request)
    _manager(request, user)
    await stepup.require(request, user, body, "users.manage")
    return await call(request, user, {"op": "user.code", "id": user_id})


@router.post("/api/users/{user_id}/logout-all")
async def person_logout(
    user_id: str, request: Request, user: deps.User = Depends(deps.writer)
):
    service = deps.svc(request)
    body = await deps.json_body(request)
    _manager(request, user)
    await stepup.require(request, user, body, "sessions.close")
    if service.users.get(user_id) is None:
        raise Failure(404, "not_found", "no such user")
    await service.locked(
        lambda: service.users.close_all(user_id, actor=f"user:{user.id}")
    )
    return {}


def _manager(request: Request, user: deps.User):
    """Who may not manage people hears so before any step-up is spent."""
    if not deps.svc(request).users.can(user.id, "users.manage"):
        raise Failure(403, "refused", "users.manage is needed")


@router.get("/api/roles")
async def roles(request: Request, user: deps.User = Depends(deps.current_user)):
    service = deps.svc(request)
    if not (
        service.users.can(user.id, "settings")
        or service.users.can(user.id, "users.manage")
    ):
        raise Failure(403, "refused", "settings is needed")
    return {"roles": service.users.roles(), "permissions": list(perms.PERMISSIONS)}


@router.put("/api/roles/{role_id}")
async def role_set(
    role_id: str, request: Request, user: deps.User = Depends(deps.writer)
):
    service = deps.svc(request)
    body = await deps.json_body(request)
    if not service.users.can(user.id, "users.manage"):
        raise Failure(403, "refused", "users.manage is needed")
    await stepup.require(request, user, body, "roles.set")
    return await call(
        request, user, {"op": "role.set", "id": role_id, "perms": body.get("perms")}
    )
