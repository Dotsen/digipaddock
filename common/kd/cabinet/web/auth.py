"""Signing in and the person's own factors.

Sign-in: a one-time code (issued by root, ``kd-cabinet user code``, by who manages people, or by the person
for a new device behind a step-up — ``settings``), a password together with a TOTP
code (a password alone never signs in), or a passkey (discoverable: the browser offers the person's passkeys). Five failures per person and
address in 15 minutes answer 429. A session is an HttpOnly, SameSite=Strict cookie; writes carry the
CSRF token from ``/api/me``. The first signing factor (passkey or TOTP) is set by the session alone;
once there is one, adding or changing factors needs a step-up (``stepup``).
"""

import json
import secrets

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from kd import hindsight_status, ids, naming
from kd.cabinet import passkeys, voice
from kd.cabinet.web import deps, stepup
from kd.cabinet.web.errors import Failure

router = APIRouter()
NAME_MAX = 64


def me(service, user_id: str, session_id: str) -> dict:
    person = service.users.get(user_id)
    table = service.users.permissions(user_id)
    second = service.users.has_second_factor(user_id)
    return {
        "id": person["id"],
        "name": person["name"],
        "language": person["language"] or service.cfg.language,
        "timezone": person["timezone"] or service.cfg.timezone,
        "permissions": {k: sorted(v) for k, v in sorted(table.items())},
        "projects": sorted(
            service.users.visible_projects(user_id, service.ctx.routing().projects)
        ),
        "csrf": deps.csrf_token(service.cfg.secret, session_id),
        "push_public_key": service.cfg.vapid_public,
        "voice": voice.on(service.ctx),  # people's audio is transcribed: a question takes a recording
        "memory": hindsight_status.installed(),  # the host runs Hindsight: its page and project banks
        "cards": ids.PREFIX,  # the host's card prefix
        "card_aliases": list(ids.ALIASES),  # its earlier ones: an old link's card opens under the current ID
        "commands": naming.COMMANDS,  # and its command prefix: the catalogs' kd-<word> are named by it
        "second_factor": second,
        "must_setup": not any(second.values()),
    }


async def _signed_in(request: Request, user_id: str, given_device) -> JSONResponse:
    service = deps.svc(request)
    token = await service.locked(
        lambda: service.users.open_session(
            user_id, deps.device(request, given_device), service.now()
        )
    )
    session = service.users.session(token, service.now())
    response = JSONResponse(me(service, user_id, session["id"]))
    response.set_cookie(
        deps.COOKIE,
        token,
        max_age=deps.COOKIE_MAX_AGE,
        path="/",
        httponly=True,
        samesite="strict",
        secure=service.cfg.secure,
    )
    return response


def _text(body: dict, key: str, required=True) -> str:
    value = body.get(key)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or not value or len(value) > 512:
        raise Failure(400, "invalid", f"{key} is required")
    return value


async def _limited(request: Request, key: str):
    service = deps.svc(request)
    if service.users.too_many(key, service.now()):
        raise Failure(429, "rate_limited", "too many attempts; wait 15 minutes")


async def _failed(request: Request, key: str, message: str):
    service = deps.svc(request)
    await service.locked(lambda: service.users.failed_attempt(key, service.now()))
    raise Failure(401, "unauthorized", message)


@router.post("/api/auth/code")
async def code_login(request: Request):
    deps.check_origin(request)
    service = deps.svc(request)
    body = await deps.json_body(request)
    user, code = _text(body, "user"), _text(body, "code")
    key = f"{user}|{deps.client_ip(request)}"
    await _limited(request, key)
    if not await service.locked(
        lambda: service.users.redeem_code(user, code, service.now())
    ):
        await _failed(request, key, "wrong code")
    await service.locked(lambda: service.users.clear_attempts(key))
    return await _signed_in(request, user, body.get("device"))


@router.post("/api/auth/password")
async def password_login(request: Request):
    deps.check_origin(request)
    service = deps.svc(request)
    body = await deps.json_body(request)
    user, password = _text(body, "user"), _text(body, "password")
    key = f"{user}|{deps.client_ip(request)}"
    await _limited(request, key)
    # the second factor's misses count for the account from every address (step-up's counter): a known password and
    # many addresses no longer try a million codes (review 04.11.2026). Its lock is read only after the password:
    # a 429 before it would say that the password is right
    totp_key = f"totp|{user}"
    person = service.users.get(user)
    # argon2 runs for every attempt, a person's or nobody's: the time never says who exists or has a password
    password_ok = service.users.verify_password(user, password)
    # a password is one factor: it signs in only with a confirmed TOTP, and any miss is the same 401
    ok = (
        person is not None
        and person["status"] == "active"
        and password_ok
        and service.users.has_second_factor(user)["totp"]
    )
    if ok and service.users.too_many(totp_key, service.now()):
        ok = False  # locked: the same 401 as a wrong password, and the code is not tried
    elif ok:  # the code is taken once: a write, under the lock
        code = _text(body, "totp", required=False)
        ok = await service.locked(
            lambda: service.users.totp_verify(user, code, service.cfg.secret, service.now())
        )
        if not ok:  # the password was right: this miss is the second factor's
            await service.locked(lambda: service.users.failed_attempt(totp_key, service.now()))
    if not ok:
        await _failed(request, key, "wrong password or code")
    await service.locked(lambda: (service.users.clear_attempts(key), service.users.clear_attempts(totp_key)))
    return await _signed_in(request, user, body.get("device"))


@router.get("/api/auth/passkey/options")
async def passkey_options(request: Request):
    service = deps.svc(request)
    options_json, value = passkeys.authentication_options(service.cfg)
    login_id = secrets.token_urlsafe(16)
    service.login_challenges.put("login", login_id, value)
    return {"options": json.loads(options_json), "login_id": login_id}


@router.post("/api/auth/passkey")
async def passkey_login(request: Request):
    deps.check_origin(request)
    service = deps.svc(request)
    body = await deps.json_body(request)
    key = f"passkey|{deps.client_ip(request)}"
    await _limited(request, key)
    value = service.login_challenges.pop("login", str(body.get("login_id", "")))
    try:
        if value is None:
            raise passkeys.PasskeyError("no such login")
        row = service.users.credential(
            passkeys.credential_id_of(body.get("credential"))
        )
        person = row and service.users.get(row["user"])
        if not person or person["status"] != "active":
            raise passkeys.PasskeyError("unknown passkey")
        count = passkeys.verify_authentication(
            service.cfg,
            body.get("credential"),
            challenge=value,
            public_key=row["public_key"],
            sign_count=row["sign_count"],
        )
    except passkeys.PasskeyError:
        await _failed(request, key, "the passkey was not accepted")
    await service.locked(
        lambda: service.users.credential_used(row["id"], count, service.now())
    )
    return await _signed_in(request, row["user"], body.get("device"))


@router.post("/api/auth/logout")
async def logout(request: Request, user: deps.User = Depends(deps.writer)):
    service = deps.svc(request)
    await service.locked(lambda: service.users.close_session(user.token))
    await service.locked(lambda: service.ctx.store.chat_presence_clear(user.id))  # pushes come again
    response = JSONResponse({})
    response.delete_cookie(deps.COOKIE, path="/")
    return response


@router.post("/api/auth/logout-all")
async def logout_all(request: Request, user: deps.User = Depends(deps.writer)):
    service = deps.svc(request)
    await service.locked(lambda: service.users.close_all(user.id))
    await service.locked(lambda: service.ctx.store.chat_presence_clear(user.id))
    response = JSONResponse({})
    response.delete_cookie(deps.COOKIE, path="/")
    return response


@router.get("/api/me")
async def get_me(request: Request, user: deps.User = Depends(deps.current_user)):
    return me(deps.svc(request), user.id, user.session_id)


# --- the person's own factors ---------------------------------------------------------------------
@router.post("/api/me/passkeys/options")
async def passkey_add_options(request: Request, user: deps.User = Depends(deps.writer)):
    service = deps.svc(request)
    options_json, value = passkeys.registration_options(
        service.cfg,
        user_id=user.id,
        user_name=user.name,
        exclude=service.users.credential_ids(user.id),
    )
    service.challenges.put("register", user.session_id, value)
    return {"options": json.loads(options_json)}


@router.post("/api/me/passkeys")
async def passkey_add(request: Request, user: deps.User = Depends(deps.writer)):
    service = deps.svc(request)
    body = await deps.json_body(request)
    await stepup.require(request, user, body, "passkey.add")
    value = service.challenges.pop("register", user.session_id)
    try:
        if value is None:
            raise passkeys.PasskeyError("no registration started")
        registered = passkeys.verify_registration(
            service.cfg, body.get("credential"), value
        )
    except passkeys.PasskeyError as exc:
        raise Failure(400, "invalid", str(exc))
    if service.users.credential(registered.credential_id) is not None:
        raise Failure(409, "conflict", "this passkey is registered already")
    name = str(body.get("name") or "")[:NAME_MAX]
    await service.locked(
        lambda: service.users.add_credential(user.id, registered, name, service.now())
    )
    return {"passkeys": service.users.credentials(user.id)}


@router.post("/api/me/password")
async def password_set(request: Request, user: deps.User = Depends(deps.writer)):
    service = deps.svc(request)
    body = await deps.json_body(request)
    await stepup.require(request, user, body, "password.set")
    try:
        await service.locked(
            lambda: service.users.set_password(user.id, body.get("password"))
        )
    except ValueError as exc:
        raise Failure(400, "invalid", str(exc))
    return {"second_factor": service.users.has_second_factor(user.id)}


@router.post("/api/me/totp/setup")
async def totp_setup(request: Request, user: deps.User = Depends(deps.writer)):
    service = deps.svc(request)
    body = await deps.json_body(request) if await request.body() else {}
    await stepup.require(request, user, body, "totp.setup")
    secret = await service.locked(
        lambda: service.users.totp_begin(user.id, service.cfg.secret)
    )
    issuer = passkeys.RP_NAME
    uri = f"otpauth://totp/{issuer}:{user.id}?secret={secret}&issuer={issuer}"
    return {"secret": secret, "uri": uri}


@router.post("/api/me/totp/confirm")
async def totp_confirm(request: Request, user: deps.User = Depends(deps.writer)):
    service = deps.svc(request)
    body = await deps.json_body(request)
    ok = await service.locked(
        lambda: service.users.totp_confirm(
            user.id, _text(body, "code"), service.cfg.secret, service.now()
        )
    )
    if not ok:
        raise Failure(400, "invalid", "the code does not match")
    return {"second_factor": service.users.has_second_factor(user.id)}
