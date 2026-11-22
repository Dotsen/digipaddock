"""Provider keys signed by an owner. Only the cabinet connects to this root service.

Root enrolls individual public keys independently of the cabinet database. Challenges and key digests
live only in memory, expire in five minutes, and disappear on restart. No request, assertion, digest or
secret is logged. The only filesystem effects are finite catalog paths, through kd-secret's writer.
"""

import base64
import hashlib
import io
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

from kd import claude_token, host_secrets, llm_models, model_credentials
from kd.cabinet import config, passkeys

SOCKET = "/run/digipaddock-model-keys/keys.sock"
SIGNERS = "/etc/digipaddock/model-key-signers"
LINE_MAX = 32768
KEY_MAX = 4096
TTL = 300
USER = re.compile(r"[a-z][a-z0-9_-]{0,31}")
DIGEST = re.compile(r"[0-9a-f]{64}")


class Refused(Exception):
    """Only fixed, secret-free messages may cross the socket or reach the journal."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def unb64(value: str) -> bytes:
    return base64.b64decode(
        value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
    )


def key_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def check_key(value) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= KEY_MAX:
        raise Refused("invalid", "key must contain 1 to 4096 characters")
    if not re.fullmatch(r"[A-Za-z0-9_./+=-]+", value):
        raise Refused("invalid", "key must be one bare token, without spaces or quotes")
    return value


def registered(user: str, directory=SIGNERS) -> list:
    """A root-enrolled owner and their signing keys; absence never falls back to the database."""
    try:
        data = json.loads(
            (Path(directory) / f"{user}.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        raise Refused(
            "setup_required", "root must enroll an owner signing key with kd-model-keys"
        ) from None
    if (
        not isinstance(data, dict)
        or data.get("user") != user
        or data.get("owner") is not True
    ):
        raise Refused("refused", "not an enrolled owner")
    keys = data.get("keys")
    if not isinstance(keys, list) or not keys:
        raise Refused("refused", "no enrolled signing key")
    return keys


class KeyService:
    def __init__(
        self,
        cfg,
        *,
        signers_dir=SIGNERS,
        clock=time.monotonic,
        write=None,
        remove=None,
        audit=None,
        status=None,
    ):
        self.cfg, self.signers_dir, self.clock = cfg, signers_dir, clock
        self.write, self.remove, self.audit = (
            write or write_key,
            remove or remove_key,
            audit or audit_key,
        )
        self.pending = {}
        self.status = status or model_credentials.subscription_status

    def handle(self, request: dict) -> dict:
        if not isinstance(request, dict):
            raise Refused("invalid", "request must be an object")
        op = request.get("op")
        if op == "status" and set(request) <= {"op", "user", "provider"}:
            return {"subscription": self.status()}
        allowed = (
            {"op", "user", "provider", "action", "digest"}
            if op == "challenge"
            else {
                "op",
                "user",
                "provider",
                "action",
                "challenge_id",
                "credential",
                "key",
            }
        )
        if op not in ("challenge", "apply") or set(request) - allowed:
            raise Refused("invalid", "unknown operation or field")
        user, provider, action = (
            request.get(k) for k in ("user", "provider", "action")
        )
        if (
            not isinstance(user, str)
            or not USER.fullmatch(user)
            or not isinstance(provider, str)
            or not model_credentials.known(provider)
            or action not in ("put", "delete")
        ):
            raise Refused("invalid", "invalid owner, provider or action")
        if provider == model_credentials.SUBSCRIPTION and action != "put":
            raise Refused("refused", "the Claude subscription token can only be replaced")
        keys = registered(
            user, self.signers_dir
        )  # reread on apply: root revocation is immediate
        now = self.clock()
        self.pending = {k: v for k, v in self.pending.items() if v["expires"] > now}
        if op == "challenge":
            digest = request.get("digest", "")
            if (
                action == "put"
                and (not isinstance(digest, str) or not DIGEST.fullmatch(digest))
            ) or (action == "delete" and digest != ""):
                raise Refused("invalid", "invalid key digest")
            # Bound memory even if a compromised cabinet floods the socket. One outstanding action per owner.
            self.pending = {k: v for k, v in self.pending.items() if v["user"] != user}
            ident = secrets.token_hex(16)
            value = hashlib.sha256(
                json.dumps(
                    ["model-key-v1", ident, user, provider, action, digest]
                ).encode()
            ).digest()
            options, _ = passkeys.authentication_options(
                self.cfg,
                challenge=value,
                allow=[unb64(k["credential_id"]) for k in keys],
            )
            self.pending[ident] = {
                "user": user,
                "provider": provider,
                "action": action,
                "digest": digest,
                "challenge": value,
                "expires": now + TTL,
            }
            return {"challenge_id": ident, "options": json.loads(options)}
        ident = request.get("challenge_id")
        if not isinstance(ident, str):
            raise Refused("stepup_required", "a passkey signature is required")
        held = self.pending.pop(
            ident, None
        )  # consume even a failed attempt, before every effect
        if held is None or any(
            held[k] != request[k] for k in ("user", "provider", "action")
        ):
            raise Refused(
                "stepup_required", "signature expired, used or for another change"
            )
        value = check_key(request.get("key")) if action == "put" else ""
        if provider == model_credentials.SUBSCRIPTION and claude_token.problem_text(claude_token.KEY + "=" + value):
            raise Refused("invalid", "invalid Claude subscription token format")
        if (action == "delete" and "key" in request) or (
            action == "put" and key_digest(value) != held["digest"]
        ):
            raise Refused("stepup_required", "signature is for another key")
        try:
            credential = request.get("credential")
            ident = b64(passkeys.credential_id_of(credential))
            key = next((k for k in keys if k["credential_id"] == ident), None)
            if key is None:
                raise passkeys.PasskeyError("not enrolled")
            passkeys.verify_authentication(
                self.cfg,
                credential,
                challenge=held["challenge"],
                public_key=unb64(key["public_key"]),
                sign_count=0,
            )
        except (ValueError, TypeError, passkeys.PasskeyError):
            raise Refused(
                "stepup_required", "the owner signature was not accepted"
            ) from None
        written = {}
        if action == "put":
            written = self.write(provider, value) or {}
        else:
            self.remove(provider)
        print(
            json.dumps(
                {
                    "event": "model-key.changed",
                    "user": user,
                    "provider": provider,
                    "action": action,
                }
            ),
            file=sys.stderr,
        )
        # The effect stands if the cabinet's audit socket is unavailable. Tell the caller explicitly.
        try:
            self.audit(user, provider, action)
        except Exception:  # noqa: BLE001 — no request or exception text may expose a key
            print("model-key: changed; feed audit unavailable", file=sys.stderr)
            return {"key": action == "put", "audit": False, **written}
        return {"key": action == "put", "audit": True, **written}


def write_key(provider: str, value: str) -> dict:
    wrote = False

    def run(argv, raw):
        nonlocal wrote
        try:
            code = subprocess.run(
                argv,
                input=raw,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
            ).returncode
        except (OSError, subprocess.TimeoutExpired):
            return 1
        if argv[0] == "bash" and code == 0:
            wrote = True
        return code

    result = host_secrets.main(
        ["put", model_credentials.secret_name(provider)],
        stdin=io.BytesIO((model_credentials.variable(provider) + "=" + value + "\n").encode("utf-8")),
        run=run,
        out=io.StringIO(),
        err=io.StringIO(),
    )
    if result and not wrote:
        raise Refused(
            "unavailable", "key was not written; check the provider key directory"
        )
    return {"restart": result == 0} if provider == model_credentials.SUBSCRIPTION else {}


def remove_key(provider: str) -> None:
    if provider == model_credentials.SUBSCRIPTION:
        raise Refused("refused", "the Claude subscription token can only be replaced")
    # Finite catalog path; unlink removes a symlink itself, never its target. Its parent is root-owned.
    Path(llm_models.env_file(provider)).unlink(missing_ok=True)


def audit_key(user: str, provider: str, action: str) -> None:
    from kd.cabinet.client import Client

    Client().call(
        {"op": "llm.key.changed", "user": user, "provider": provider, "action": action}
    )


def answer(service: KeyService, raw: bytes, allowed=True) -> bytes:
    try:
        if not allowed:
            raise Refused("refused", "only the cabinet may connect")
        if len(raw) > LINE_MAX:
            raise Refused("invalid", "request too large")
        result = {"ok": True, **service.handle(json.loads(raw))}
    except Refused as exc:
        result = {"ok": False, "error": exc.kind, "message": str(exc)}
    except Exception:  # noqa: BLE001 — protocol errors never log raw input or exception text
        result = {
            "ok": False,
            "error": "unavailable",
            "message": "provider key service failed",
        }
    print("model-key: " + ("ok" if result["ok"] else result["error"]), file=sys.stderr)
    return json.dumps(result).encode("utf-8") + b"\n"


def serve_one(service: KeyService, conn: socket.socket, allowed_uid: int) -> None:
    from kd.cap_serve import peer

    with conn:
        conn.settimeout(30)
        try:
            _, uid, _ = peer(conn)
            if uid != allowed_uid:
                conn.sendall(answer(service, b"", False))
                return
            with conn.makefile("rb") as stream:
                raw = stream.readline(LINE_MAX + 1)
            conn.sendall(answer(service, raw))
        except OSError:
            pass


def serve(service: KeyService, sock: socket.socket, allowed_uid: int) -> None:
    while True:
        conn, _ = sock.accept()
        serve_one(service, conn, allowed_uid)


def enroll(user: str, prefix: str, cabinet, directory=SIGNERS) -> dict:
    """Root explicitly chooses one current owner's signing credential. Never auto-enroll at runtime."""
    people = cabinet.call({"op": "user.list"})["users"]
    person = next((p for p in people if p["id"] == user), None)
    if (
        not USER.fullmatch(user)
        or person is None
        or person["status"] != "active"
        or not any(r[0] == "owner" and r[1] == "*" for r in person["roles"])
    ):
        raise Refused("refused", "only an active owner may be enrolled")
    keys = cabinet.call({"op": "signer.keys", "user": user})["keys"]
    if not prefix:
        return {
            "keys": [
                {k: v for k, v in key.items() if k != "public_key"} for key in keys
            ]
        }
    if len(prefix) < 8:
        raise Refused(
            "invalid", "credential prefix must contain at least eight characters"
        )
    chosen = [k for k in keys if k["credential_id"].startswith(prefix)]
    if len(chosen) != 1:
        raise Refused(
            "refused",
            "choose exactly one credential by its id prefix (at least 8 characters)",
        )
    try:
        entries = registered(user, directory)
    except Refused as exc:
        if exc.kind != "setup_required":
            raise
        entries = []
    entries = [
        k for k in entries if k["credential_id"] != chosen[0]["credential_id"]
    ] + chosen
    path = Path(directory) / f"{user}.json"
    # Use a root-owned directory created by the installer; no cabinet-supplied paths.
    temporary = path.with_name("." + path.name + "." + secrets.token_hex(8))
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            os.chmod(temporary, 0o600)
            json.dump({"user": user, "owner": True, "keys": entries}, handle)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"enrolled": user, "credential_id": chosen[0]["credential_id"]}


def main(argv=None) -> int:
    from kd import naming
    from kd.cabinet.client import Client

    argv = sys.argv[1:] if argv is None else argv
    if os.geteuid() != 0:
        print("root is required", file=sys.stderr)
        return 1
    try:
        if argv == ["serve"]:
            import pwd

            if (
                os.environ.get("LISTEN_PID") != str(os.getpid())
                or os.environ.get("LISTEN_FDS") != "1"
            ):
                raise Refused("unavailable", "start through the systemd socket")
            cfg = config.load()
            serve(
                KeyService(cfg),
                socket.socket(fileno=3),
                pwd.getpwnam(naming.service_user("cabinet")).pw_uid,
            )
        elif len(argv) in (2, 3) and argv[0] == "enroll":
            print(
                json.dumps(enroll(argv[1], argv[2] if len(argv) == 3 else "", Client()))
            )
        elif len(argv) == 2 and argv[0] == "revoke" and USER.fullmatch(argv[1]):
            (Path(SIGNERS) / f"{argv[1]}.json").unlink(missing_ok=True)
        else:
            print(
                "usage: kd-model-keys serve | enroll <owner> [credential-prefix] | revoke <owner>",
                file=sys.stderr,
            )
            return 2
    except Refused as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0
