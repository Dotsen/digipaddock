"""Passkeys (WebAuthn) over py_webauthn: registration, discoverable login and challenges bound to decisions.

Every ceremony requires user verification (Face ID, a fingerprint, a PIN); the origin and rp id are
the cabinet's own (``config.Config``); the signature counter must grow when the authenticator keeps
one. Any failure is a ``PasskeyError`` — callers answer 401/403 and never say which check failed.
Imports of ``webauthn`` stay inside the functions: only the service's venv has it.
"""

import base64
from dataclasses import dataclass
import hashlib
import json
import os
from typing import Iterable, List, Optional, Tuple

RP_NAME = "digipaddock"
ALGORITHMS = (-7, -257)  # ES256, RS256
TIMEOUT_MS = 120000


class PasskeyError(Exception):
    pass


@dataclass(frozen=True)
class Registered:
    credential_id: bytes
    public_key: bytes
    sign_count: int
    transports: List[str]


def user_handle(user_id: str) -> bytes:
    """The opaque WebAuthn user handle of a cabinet user (not the user id itself). The salt is the board's first
    name and stays: the browser keeps the handle with every registered key."""
    return hashlib.sha256(b"kh-cabinet-user|" + user_id.encode("utf-8")).digest()[:16]


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _parsed(credential_json) -> dict:
    try:
        data = (
            json.loads(credential_json)
            if isinstance(credential_json, str)
            else credential_json
        )
    except ValueError as exc:
        raise PasskeyError("the credential is not JSON") from exc
    if not isinstance(data, dict):
        raise PasskeyError("the credential is not an object")
    return data


def credential_id_of(credential_json) -> bytes:
    data = _parsed(credential_json)
    raw = data.get("rawId") or data.get("id")
    if not isinstance(raw, str) or not raw:
        raise PasskeyError("the credential has no id")
    try:
        return _b64url_decode(raw)
    except (ValueError, TypeError) as exc:
        raise PasskeyError("the credential id is not base64url") from exc


def registration_options(
    cfg, *, user_id: str, user_name: str, exclude: Iterable[bytes]
) -> Tuple[str, bytes]:
    from webauthn import generate_registration_options, options_to_json
    from webauthn.helpers.cose import COSEAlgorithmIdentifier
    from webauthn.helpers.structs import (
        AuthenticatorSelectionCriteria,
        PublicKeyCredentialDescriptor,
        ResidentKeyRequirement,
        UserVerificationRequirement,
    )

    options = generate_registration_options(
        rp_id=cfg.rp_id,
        rp_name=RP_NAME,
        user_id=user_handle(user_id),
        user_name=user_id,
        user_display_name=user_name,
        timeout=TIMEOUT_MS,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=c) for c in exclude],
        supported_pub_key_algs=[COSEAlgorithmIdentifier(a) for a in ALGORITHMS],
    )
    return options_to_json(options), options.challenge


def verify_registration(cfg, credential_json, challenge: bytes) -> Registered:
    from webauthn import verify_registration_response
    from webauthn.helpers.cose import COSEAlgorithmIdentifier
    from webauthn.helpers.exceptions import WebAuthnException

    data = _parsed(credential_json)
    try:
        verified = verify_registration_response(
            credential=data,
            expected_challenge=challenge,
            expected_rp_id=cfg.rp_id,
            expected_origin=cfg.origin,
            require_user_verification=True,
            supported_pub_key_algs=[COSEAlgorithmIdentifier(a) for a in ALGORITHMS],
        )
    except (WebAuthnException, KeyError, TypeError, ValueError) as exc:
        raise PasskeyError("the passkey was not accepted") from exc
    transports = (data.get("response") or {}).get("transports") or []
    return Registered(
        credential_id=verified.credential_id,
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        transports=[t for t in transports if isinstance(t, str)][:8],
    )


def authentication_options(
    cfg, *, challenge: Optional[bytes] = None, allow: Iterable[bytes] = ()
) -> Tuple[str, bytes]:
    """Options for ``navigator.credentials.get``; no ``allow`` = discoverable (the browser offers the passkeys)."""
    from webauthn import generate_authentication_options, options_to_json
    from webauthn.helpers.structs import (
        PublicKeyCredentialDescriptor,
        UserVerificationRequirement,
    )

    challenge = challenge if challenge is not None else os.urandom(32)
    options = generate_authentication_options(
        rp_id=cfg.rp_id,
        challenge=challenge,
        timeout=TIMEOUT_MS,
        allow_credentials=[PublicKeyCredentialDescriptor(id=c) for c in allow],
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    return options_to_json(options), challenge


def verify_authentication(
    cfg, credential_json, *, challenge: bytes, public_key: bytes, sign_count: int
) -> int:
    """The new signature counter after a valid assertion; PasskeyError otherwise."""
    from webauthn import verify_authentication_response
    from webauthn.helpers.exceptions import WebAuthnException

    data = _parsed(credential_json)
    try:
        verified = verify_authentication_response(
            credential=data,
            expected_challenge=challenge,
            expected_rp_id=cfg.rp_id,
            expected_origin=cfg.origin,
            credential_public_key=public_key,
            credential_current_sign_count=sign_count,
            require_user_verification=True,
        )
    except (WebAuthnException, KeyError, TypeError, ValueError) as exc:
        raise PasskeyError("the passkey signature was not accepted") from exc
    return verified.new_sign_count
