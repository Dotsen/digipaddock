"""Step-up for sensitive changes: a passkey assertion or a TOTP code over a fresh challenge.

``POST /api/stepup/challenge {purpose}`` returns ``{challenge_id, options, totp}``; the change then
carries ``stepup: {challenge_id, credential | totp}``. The challenge is
``sha256("stepup|" + session id + "|" + purpose + "|" + nonce)``: it is good for one change of one
kind, in one session, once, for five minutes. A stolen session alone adds no key and no TOTP.
"""

import hashlib
import json
import secrets

from fastapi import APIRouter, Depends, Request

from kd.cabinet import passkeys
from kd.cabinet.web import deps
from kd.cabinet.web.errors import Failure

PURPOSES = (
    "passkey.add",
    "passkey.remove",
    "password.set",
    "totp.setup",
    "users.manage",
    "roles.set",
    "sessions.close",
    "projects.delete",  # a project's cards move with it
    "monitor.settings",  # a save of the monitor's settings that weakens it
    "code.self",  # a sign-in code for one's own new device is a new sign-in
)
# a person's first signing factor is set by the session alone (onboarding after a root code)
FIRST_FACTOR = ("passkey.add", "password.set", "totp.setup")
router = APIRouter()


def _purpose(value) -> str:
    if value not in PURPOSES:
        raise Failure(400, "invalid", f"purpose must be one of {', '.join(PURPOSES)}")
    return value


@router.post("/api/stepup/challenge")
async def challenge(request: Request, user: deps.User = Depends(deps.writer)):
    service = deps.svc(request)
    purpose = _purpose((await deps.json_body(request)).get("purpose"))
    if not service.users.signs(user.id):
        raise Failure(403, "setup_required", "add a passkey or TOTP first")
    nonce = secrets.token_hex(16)
    value = hashlib.sha256(
        f"stepup|{user.session_id}|{purpose}|{nonce}".encode("utf-8")
    ).digest()
    challenge_id = secrets.token_urlsafe(16)
    service.challenges.put(
        "stepup",
        challenge_id,
        {
            "user": user.id,
            "session": user.session_id,
            "purpose": purpose,
            "challenge": value,
        },
    )
    allow = service.users.credential_ids(user.id)
    options = None
    if allow:
        options_json, _ = passkeys.authentication_options(
            service.cfg, challenge=value, allow=allow
        )
        options = json.loads(options_json)
    totp = service.users.has_second_factor(user.id)["totp"]
    return {"challenge_id": challenge_id, "options": options, "totp": totp}


async def require(request: Request, user: deps.User, body: dict, purpose: str):
    """Pass when ``body["stepup"]`` proves this purpose; 403 ``stepup_required`` otherwise. A person with no
    signing factor yet sets the first one by the session alone, and nothing else: 403 ``setup_required``."""
    service = deps.svc(request)
    if not service.users.signs(user.id):
        if purpose in FIRST_FACTOR:
            return
        raise Failure(403, "setup_required", "add a passkey or TOTP first")
    proof = body.get("stepup")
    if not isinstance(proof, dict) or not isinstance(proof.get("challenge_id"), str):
        raise Failure(403, "stepup_required", "confirm with a passkey or a TOTP code")
    held = service.challenges.pop("stepup", proof["challenge_id"])
    if held is None or (held["user"], held["session"], held["purpose"]) != (
        user.id,
        user.session_id,
        purpose,
    ):
        raise Failure(403, "stepup_required", "the confirmation is not for this change")
    if "credential" in proof:
        await verify_passkey(request, user, proof["credential"], held["challenge"])
        return
    if isinstance(proof.get("totp"), str):
        await totp(request, user, proof["totp"])
        return
    raise Failure(403, "stepup_required", "the confirmation was not accepted")


async def totp(request: Request, user: deps.User, code: str):
    """A TOTP code of the person; five wrong ones in 15 minutes lock TOTP for the rest of the window (429)."""
    service = deps.svc(request)
    key, now = f"totp|{user.id}", service.now()
    if service.users.too_many(key, now):
        raise Failure(
            429, "rate_limited", "too many wrong codes; use a passkey or wait"
        )
    if await service.locked(
        lambda: service.users.totp_verify(user.id, code, service.cfg.secret, now)
    ):
        return
    await service.locked(lambda: service.users.failed_attempt(key, now))
    raise Failure(403, "stepup_required", "the code was not accepted")


async def verify_passkey(
    request: Request, user: deps.User, credential, challenge: bytes, *, signing=False, user_verification=True
) -> dict:
    """A valid assertion by one of the person's own passkeys over ``challenge``; the key's row. 403 otherwise.
    ``user_verification=False`` — the prod deploy's signature only (``web/deploys.py``)."""
    service = deps.svc(request)
    try:
        row = service.users.credential(passkeys.credential_id_of(credential))
        if (
            row is None
            or row["user"] != user.id
            or (signing and not row["signing_enabled"])
        ):
            raise passkeys.PasskeyError("not this person's passkey")
        count = passkeys.verify_authentication(
            service.cfg,
            credential,
            challenge=challenge,
            public_key=row["public_key"],
            sign_count=row["sign_count"],
            user_verification=user_verification,
        )
    except passkeys.PasskeyError:
        raise Failure(403, "stepup_required", "the passkey signature was not accepted")
    await service.locked(
        lambda: service.users.credential_used(row["id"], count, service.now())
    )
    return row
