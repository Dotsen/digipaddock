"""Who is asking (the session cookie), whether a write is the page's own (CSRF + Origin), request bodies."""

import hashlib
import hmac
from dataclasses import dataclass
from typing import Optional

from fastapi import Request

from kd.cabinet import api
from kd.cabinet.web.errors import Failure

COOKIE = "kd_session"
COOKIE_MAX_AGE = 30 * 24 * 3600
CSRF_HEADER = "X-CSRF-Token"


@dataclass(frozen=True)
class User:
    id: str
    name: str
    language: str
    timezone: str
    session_id: str
    token: str


def svc(request: Request):
    return request.app.state.svc


def client_ip(request: Request) -> str:
    """nginx overwrites X-Forwarded-For with the peer's address; without nginx (dev) the peer itself."""
    forwarded = request.headers.get("x-forwarded-for", "")
    first = forwarded.split(",")[0].strip()
    return first or (request.client.host if request.client else "")


def device(request: Request, given=None) -> str:
    if isinstance(given, str) and given.strip():
        return given.strip()
    return request.headers.get("user-agent", "")[:120]


def csrf_token(secret: bytes, session_id: str) -> str:
    return hmac.new(
        secret, f"csrf|{session_id}".encode("utf-8"), hashlib.sha256
    ).hexdigest()


async def json_body(request: Request) -> dict:
    """The request's JSON object, or 400."""
    try:
        data = await request.json()
    except ValueError:
        raise Failure(400, "invalid", "the body is not JSON")
    if not isinstance(data, dict):
        raise Failure(400, "invalid", "the body must be a JSON object")
    return data


async def optional_user(request: Request) -> Optional[User]:
    service = svc(request)
    token = request.cookies.get(COOKIE, "")
    if not token:
        return None
    session = await service.locked(lambda: service.users.session(token, service.now()))
    if session is None:
        return None
    person = service.users.get(session["user"])
    if person is None or person["status"] != "active":
        return None
    return User(
        id=person["id"],
        name=person["name"],
        language=person["language"] or service.cfg.language,
        timezone=person["timezone"] or service.cfg.timezone,
        session_id=session["id"],
        token=token,
    )


async def current_user(request: Request) -> User:
    user = await optional_user(request)
    if user is None:
        raise Failure(401, "unauthorized", "sign in")
    return user


def check_origin(request: Request):
    """A POST from the cabinet's own page (its exact Origin), or 403 — before a session exists too: a cross-site
    form must not sign the browser into an account it holds a code for (global review 15.10.2026)."""
    if request.headers.get("origin", "") != svc(request).cfg.origin:
        raise Failure(
            403, "refused", "the request did not come from the cabinet's page"
        )


def check_csrf(request: Request, user: User):
    """A write from the cabinet's own page: its Origin and the session's CSRF token, or 403."""
    check_origin(request)
    service = svc(request)
    expected = csrf_token(service.cfg.secret, user.session_id)
    if not hmac.compare_digest(request.headers.get(CSRF_HEADER, ""), expected):
        raise Failure(
            403, "refused", "the request did not come from the cabinet's page"
        )


async def writer(request: Request) -> User:
    """The person behind a write (POST, PUT, PATCH, DELETE): signed in, from the cabinet's own page."""
    user = await current_user(request)
    check_csrf(request, user)
    return user


def principal(user: User) -> api.Principal:
    return api.Principal("user", -1, user.id)
