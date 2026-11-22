"""The cabinet's host settings: public ones in cabinet.json, secrets in cabinet.env (owner cabinet spec section 9).

``cabinet.json`` (0644) holds what any local tool may read — the origin the owner opens, the host's
default language and time zone; the agents' CLIs build card and decision links from it with
``public()`` (stdlib only: they run on the system python, without the cabinet's venv).
``cabinet.env`` (root and the cabinet user, 0640) holds the session secret and the VAPID public key; the
VAPID private key lives in ``push.env`` (root and the push user, 0640), read only by the push sender — the one
cabinet process with network, which has no other secret and no owner authority on the socket (global review
15.10.2026). ``dev=True`` needs no file: a local run gets a fresh secret and key pair and an http://localhost origin.
"""

import base64
from dataclasses import dataclass
import json
import os
import re
from urllib.parse import urlsplit

from kd import naming

JSON = "/etc/digipaddock/cabinet.json"
ENV = "/etc/digipaddock/cabinet.env"
PUSH_ENV = "/etc/digipaddock/push.env"
LANGUAGES = ("ru", "en")
DEV_ORIGIN = "http://localhost:8765"
SECRET_MIN = 32
FILES = "/var/lib/digipaddock/cabinet/files"
_HEX = re.compile(r"[0-9a-fA-F]+")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    origin: str
    rp_id: str
    language: str
    timezone: str
    secret: bytes
    vapid_private: str
    vapid_public: str
    vapid_subject: str
    dev: bool = False
    files_dir: str = FILES
    transcribe: bool = False  # people's audio to text: cabinet.json "transcribe", on unless false

    @property
    def secure(self) -> bool:
        return self.origin.startswith("https://")


def card_url(origin: str, card_id: str) -> str:
    return f"{origin}/cards/{card_id}"


def decision_url(origin: str, decision_id: str) -> str:
    return f"{origin}/decisions/{decision_id}"


def card_link(card_id: str, fallback: str, json_path=JSON) -> str:
    """The card's page in the cabinet; ``fallback + card_id`` on a host without cabinet.json."""
    origin = origin_of(json_path)
    return card_url(origin, card_id) if origin else f"{fallback}{card_id}"


def public(json_path=JSON) -> dict:
    """The public settings as stored; ConfigError when the file is missing or not an object."""
    try:
        with open(json_path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ConfigError(
            f"{json_path}: unreadable ({exc.__class__.__name__})"
        ) from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{json_path}: not an object")
    return data


def origin_of(json_path=JSON):
    """The cabinet's valid origin, or None when the host has no usable cabinet.json (links fall back)."""
    try:
        origin = public(json_path).get("origin")
        check_origin(origin)
    except ConfigError:
        return None
    return origin


def check_origin(origin) -> str:
    """``https://host[:port]`` or ``http://localhost[:port]``, nothing after the authority; returns the rp id."""
    if not isinstance(origin, str):
        raise ConfigError("origin must be text")
    parts = urlsplit(origin)
    host = parts.hostname or ""
    if (
        parts.scheme not in ("https", "http")
        or not host
        or parts.path not in ("",)
        or parts.query
        or parts.fragment
        or parts.username
        or parts.password
    ):
        raise ConfigError(f"origin {origin!r} must be https://host[:port]")
    if parts.scheme == "http" and host != "localhost":
        raise ConfigError("plain http is for http://localhost only")
    try:
        parts.port
    except ValueError as exc:
        raise ConfigError(f"origin {origin!r}: bad port") from exc
    return host


def check_timezone(name) -> str:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    if not isinstance(name, str) or not name:
        raise ConfigError("timezone must be an IANA name")
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"unknown timezone {name!r}") from exc
    return name


def _env(path) -> dict:
    """``KEY=VALUE`` lines; no quotes, no export, comments and blank lines skipped."""
    values = {}
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError as exc:
        raise ConfigError(f"{path}: unreadable ({exc.__class__.__name__})") from exc
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_vapid():
    """A fresh VAPID pair: (private key as raw base64url, public key as the uncompressed point, base64url)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_numbers().private_value.to_bytes(32, "big")
    public_point = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return _b64url(private), _b64url(public_point)


def owner_language() -> str:
    """The deployment's owner language (host.json ``owner_language``): the cabinet's default for a person who chose
    none, the notices' language. A host.json that cannot say it fails closed, as for every host value."""
    return naming.load_host()["owner_language"]


def load(
    json_path=JSON, env_path=ENV, *, dev=False, origin=None, files_dir=None, language=None
) -> Config:
    if dev:
        origin = origin or DEV_ORIGIN
        private, public_key = generate_vapid()
        return Config(
            origin=origin,
            rp_id=check_origin(origin),
            language=language or owner_language(),
            timezone="UTC",
            secret=os.urandom(32),
            vapid_private=private,
            vapid_public=public_key,
            vapid_subject="mailto:dev@localhost",
            dev=True,
            files_dir=files_dir or FILES,
        )
    settings = public(json_path)
    origin = origin or settings.get("origin")
    rp_id = check_origin(origin)
    language = settings.get("language") or owner_language()
    if language not in LANGUAGES:
        raise ConfigError(f"language must be one of {', '.join(LANGUAGES)}")
    timezone = check_timezone(settings.get("timezone", "UTC"))
    transcribe = settings.get("transcribe", True)
    if not isinstance(transcribe, bool):
        raise ConfigError("transcribe must be true or false")
    env = _env(env_path)
    secret = env.get("CABINET_SECRET", "")
    if not _HEX.fullmatch(secret) or len(secret) % 2 or len(secret) < 2 * SECRET_MIN:
        raise ConfigError(f"CABINET_SECRET must be hex of at least {SECRET_MIN} bytes")
    vapid = [env.get(k, "") for k in ("VAPID_PUBLIC_KEY", "VAPID_SUBJECT")]
    if not all(vapid):
        raise ConfigError("VAPID_PUBLIC_KEY and VAPID_SUBJECT are required")
    return Config(
        origin=origin,
        rp_id=rp_id,
        language=language,
        timezone=timezone,
        secret=bytes.fromhex(secret),
        vapid_private=env.get("VAPID_PRIVATE_KEY", ""),  # a pre-split cabinet.env; the service never uses it
        vapid_public=vapid[0],
        vapid_subject=vapid[1],
        files_dir=files_dir or FILES,
        transcribe=transcribe,
    )


@dataclass(frozen=True)
class PushConfig:
    vapid_private: str
    vapid_subject: str


def load_push(env_path=PUSH_ENV) -> PushConfig:
    """The push sender's secret: the VAPID private key and subject of push.env, nothing else."""
    env = _env(env_path)
    private, subject = env.get("VAPID_PRIVATE_KEY", ""), env.get("VAPID_SUBJECT", "")
    if not private or not subject:
        raise ConfigError(f"{env_path}: VAPID_PRIVATE_KEY and VAPID_SUBJECT are required")
    return PushConfig(vapid_private=private, vapid_subject=subject)
