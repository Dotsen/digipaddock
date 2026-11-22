"""People of the cabinet: accounts, roles over projects, login codes, password and TOTP, web sessions.

Works on the store's connection; every write is a store transaction with its event, and no event
carries a code, a token, a password or a TOTP secret. Codes and session tokens are kept as their
sha256 only. The service runs these methods under its one lock, like every other write.

Time is the store's text form ``%Y-%m-%dT%H:%M:%SZ``; TOTP codes are checked against the wall
clock (an authenticator app knows no other), the ``now`` arguments stamp the change.
"""

import base64
from datetime import datetime, timedelta, timezone
import functools
import hashlib
import json
import re
import secrets
from typing import Dict, Iterable, List, Optional, Set, Tuple, Union

from kd.cabinet import perms


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(bytes(raw)).rstrip(b"=").decode("ascii")

USER_ID = re.compile(r"[a-z][a-z0-9_-]{0,31}")
ROLE_ID = re.compile(r"[a-z][a-z0-9_-]{0,31}")
STATUSES = ("active", "blocked")
LANGUAGES = ("", "ru", "en")
NAME_MAX = 64
SIGNING_PREFIX_MIN = 8  # characters of a passkey's base64url id that name it for set_signing (as kd.model_keys)
DEVICE_MAX = 120
CODE_TTL = 900
SESSION_DAYS = 30
SLIDE = timedelta(hours=1)
FMT = "%Y-%m-%dT%H:%M:%SZ"
PUBLIC = ("id", "name", "status", "language", "timezone", "created", "last_login")
Projects = Union[str, List[str]]


def parse_time(text: str) -> datetime:
    return datetime.strptime(text, FMT).replace(tzinfo=timezone.utc)


def format_time(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(FMT)


def shift(now: str, **delta) -> str:
    return format_time(parse_time(now) + timedelta(**delta))


@functools.lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """An argon2 hash of no password anybody has, with the hasher's own parameters: verified against for nobody."""
    from argon2 import PasswordHasher

    return PasswordHasher().hash(secrets.token_urlsafe(32))


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _fernet(secret_key: bytes):
    from cryptography.fernet import Fernet

    return Fernet(
        base64.urlsafe_b64encode(hashlib.sha256(secret_key + b"totp").digest())
    )


class Users:
    def __init__(self, store):
        self.store = store
        self._conn = store._conn

    # --- reads ---------------------------------------------------------------------------------
    def _row(self, user_id):
        return self._conn.execute(
            "SELECT * FROM users WHERE id=?", (user_id,)
        ).fetchone()

    def get(self, user_id) -> Optional[dict]:
        row = self._row(user_id)
        return None if row is None else {k: row[k] for k in PUBLIC}

    def list(self) -> List[dict]:
        rows = self._conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        return [{k: r[k] for k in PUBLIC} for r in rows]

    def roles(self) -> Dict[str, List[str]]:
        return {
            r["id"]: json.loads(r["perms"])
            for r in self._conn.execute("SELECT id, perms FROM roles ORDER BY id")
        }

    def assignments(self, user_id) -> List[Tuple[str, Projects]]:
        return [
            (r["role"], json.loads(r["projects"]))
            for r in self._conn.execute(
                "SELECT role, projects FROM user_roles WHERE user=? ORDER BY role",
                (user_id,),
            )
        ]

    def permissions(self, user_id) -> Dict[str, Set[str]]:
        """Project -> permissions; the key ``"*"`` holds what applies to every project."""
        row = self._row(user_id)
        if row is None or row["status"] != "active":
            return {}
        roles, out = self.roles(), {}
        for role, projects in self.assignments(user_id):
            granted = set(roles.get(role, ()))
            for project in ["*"] if projects == "*" else projects:
                out.setdefault(project, set()).update(granted)
        return out

    def can(self, user_id, perm: str, project: Optional[str] = None) -> bool:
        """``perm`` on ``project``; without a project only a grant over every project counts."""
        perms.check(perm)
        table = self.permissions(user_id)
        scopes = [table.get("*", set())]
        if project is not None:
            scopes.append(table.get(project, set()))
        return any(perms.allows(s, perm) for s in scopes)

    def visible_projects(self, user_id, all_projects: Iterable[str]) -> Set[str]:
        return {p for p in all_projects if self.can(user_id, "cards.read", p)}

    def has_second_factor(self, user_id) -> dict:
        row = self._row(user_id)
        if row is None:
            return {"passkey": False, "totp": False, "password": False}
        passkey = self._conn.execute(
            "SELECT 1 FROM credentials WHERE user=? LIMIT 1", (user_id,)
        ).fetchone()
        return {
            "passkey": passkey is not None,
            "totp": bool(row["totp_enabled"]),
            "password": bool(row["password_hash"]),
        }

    # --- accounts ------------------------------------------------------------------------------
    def _check_roles(self, roles) -> List[Tuple[str, Projects]]:
        known, out = self.roles(), []
        for item in roles:
            try:
                role, projects = item
            except (TypeError, ValueError) as exc:
                raise ValueError("a role is (role, projects)") from exc
            if role not in known:
                raise ValueError(f"unknown role {role!r}")
            if projects != "*" and not (
                isinstance(projects, (list, tuple))
                and projects
                and all(isinstance(p, str) and p for p in projects)
            ):
                raise ValueError("projects are '*' or a list of project ids")
            out.append((role, "*" if projects == "*" else sorted(set(projects))))
        if not out:
            raise ValueError("a user needs a role")
        return out

    @staticmethod
    def _check_profile(name=None, language=None, tz=None):
        if name is not None and (
            not isinstance(name, str) or not name.strip() or len(name) > NAME_MAX
        ):
            raise ValueError("a name is 1 to 64 characters")
        if language is not None and language not in LANGUAGES:
            raise ValueError("language must be ru or en")
        if tz:
            from kd.cabinet import config

            try:
                config.check_timezone(tz)
            except config.ConfigError as exc:
                raise ValueError(str(exc)) from exc

    def create(
        self,
        user_id,
        name,
        *,
        roles,
        language=None,
        timezone=None,
        actor,
    ) -> dict:
        if not isinstance(user_id, str) or not USER_ID.fullmatch(user_id):
            raise ValueError("a user id is lowercase latin letters, digits, - and _")
        self._check_profile(name, language, timezone)
        assigned = self._check_roles(roles)
        with self.store._tx():
            if self._row(user_id) is not None:
                raise ValueError(f"user {user_id} exists")
            self._conn.execute(
                "INSERT INTO users (id, name, language, timezone, created) VALUES (?,?,?,?,?)",
                (
                    user_id,
                    name.strip(),
                    language or "",
                    timezone or "",
                    self.store._clock(),
                ),
            )
            self._write_roles(user_id, assigned)
            self.store.chat_reads_start(user_id)  # the chats' past is not new to them
            self.store._event(
                actor,
                "user.created",
                "",
                {"user": user_id, "roles": [[r, p] for r, p in assigned]},
            )
        return self.get(user_id)

    def _write_roles(self, user_id, assigned):
        self._conn.execute("DELETE FROM user_roles WHERE user=?", (user_id,))
        self._conn.executemany(
            "INSERT INTO user_roles (user, role, projects) VALUES (?,?,?)",
            [(user_id, r, json.dumps(p)) for r, p in assigned],
        )

    def _require(self, user_id):
        if self._row(user_id) is None:
            raise ValueError(f"no user {user_id}")

    def assign(self, user_id, roles, actor):
        assigned = self._check_roles(roles)
        with self.store._tx():
            self._require(user_id)
            self._write_roles(user_id, assigned)
            self.store._event(
                actor,
                "user.role_set",
                "",
                {"user": user_id, "roles": [[r, p] for r, p in assigned]},
            )

    def set_role(self, role_id, role_perms, actor):
        if not isinstance(role_id, str) or not ROLE_ID.fullmatch(role_id):
            raise ValueError("a role id is lowercase latin letters, digits, - and _")
        checked = sorted({perms.check(p) for p in role_perms})
        with self.store._tx():
            self._conn.execute(
                "INSERT INTO roles (id, perms) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET perms=excluded.perms",
                (role_id, json.dumps(checked)),
            )
            self.store._event(
                actor, "user.role_set", "", {"role": role_id, "perms": checked}
            )

    def set_status(self, user_id, status, actor):
        if status not in STATUSES:
            raise ValueError("status is active or blocked")
        with self.store._tx():
            self._require(user_id)
            self._conn.execute(
                "UPDATE users SET status=? WHERE id=?", (status, user_id)
            )
            if status == "blocked":
                self._conn.execute("DELETE FROM web_sessions WHERE user=?", (user_id,))
                self._conn.execute("DELETE FROM push_subscriptions WHERE user=?", (user_id,))
            self.store._event(
                actor, "user.updated", "", {"user": user_id, "status": status}
            )

    def update(self, user_id, *, name=None, language=None, timezone=None, actor):
        self._check_profile(name, language, timezone)
        changes = {
            k: v
            for k, v in (
                ("name", name.strip() if isinstance(name, str) else None),
                ("language", language),
                ("timezone", timezone),
            )
            if v is not None
        }
        if not changes:
            return self.get(user_id)
        with self.store._tx():
            self._require(user_id)
            sets = ", ".join(f"{k}=?" for k in changes)
            self._conn.execute(
                f"UPDATE users SET {sets} WHERE id=?", (*changes.values(), user_id)
            )
            self.store._event(
                actor, "user.updated", "", {"user": user_id, "fields": sorted(changes)}
            )
        return self.get(user_id)

    # --- notification preferences ----------------------------------------------
    def prefs(self, user_id) -> Dict[str, str]:
        """What the person set: ``kind -> now|batch|feed`` and ``quiet``; the defaults are ``notify``'s."""
        return {
            r["kind"]: r["mode"]
            for r in self._conn.execute(
                "SELECT kind, mode FROM notification_prefs WHERE user=?", (user_id,)
            )
        }

    def set_prefs(self, user_id, prefs, actor=None) -> Dict[str, str]:
        """Merge ``prefs`` (checked by ``notify.check_prefs``) into the person's own; the whole stored set."""
        from kd.cabinet import notify

        checked = notify.check_prefs(prefs)
        with self.store._tx():
            self._require(user_id)
            self._conn.executemany(
                "INSERT INTO notification_prefs VALUES (?,?,?) ON CONFLICT(user, kind) DO UPDATE SET mode=excluded.mode",
                [(user_id, k, v) for k, v in checked.items()],
            )
            self.store._event(
                actor or f"user:{user_id}",
                "user.prefs",
                "",
                {"user": user_id, "kinds": sorted(checked)},
            )
        return self.prefs(user_id)

    # --- one-time login codes ------------------------------------------------------------------
    def issue_code(self, user_id, now, ttl_seconds=CODE_TTL, *, actor="system") -> str:
        """A fresh code; earlier unused codes of the user stop working. The event says who gave it to whom and
        until when (the feed shows it), never the code."""
        code = secrets.token_urlsafe(9)
        expires = shift(now, seconds=ttl_seconds)
        with self.store._tx():
            self._require(user_id)
            self._conn.execute(
                "DELETE FROM login_codes WHERE user=? AND used=''", (user_id,)
            )
            self._conn.execute(
                "INSERT INTO login_codes (code_hash, user, expires) VALUES (?,?,?)",
                (digest(code), user_id, expires),
            )
            self.store._event(
                actor,
                "user.code_issued",
                "",
                {"user": user_id, "expires": expires},
            )
        return code

    def redeem_code(self, user_id, code, now) -> bool:
        if not isinstance(code, str) or not code:
            return False
        with self.store._tx():
            row = self._conn.execute(
                "SELECT * FROM login_codes WHERE code_hash=?", (digest(code),)
            ).fetchone()
            if (
                row is None
                or row["user"] != user_id
                or row["used"]
                or row["expires"] <= now
            ):
                return False
            self._conn.execute(
                "UPDATE login_codes SET used=? WHERE code_hash=?",
                (now, row["code_hash"]),
            )
        return True

    # --- password and TOTP ---------------------------------------------------------------------
    def set_password(self, user_id, password):
        from argon2 import PasswordHasher

        if not isinstance(password, str) or len(password) < 10:
            raise ValueError("a password is at least 10 characters")
        hashed = PasswordHasher().hash(password)
        with self.store._tx():
            self._require(user_id)
            self._conn.execute(
                "UPDATE users SET password_hash=? WHERE id=?", (hashed, user_id)
            )
            self.store._event(
                f"user:{user_id}",
                "user.updated",
                "",
                {"user": user_id, "fields": ["password"]},
            )

    def verify_password(self, user_id, password) -> bool:
        from argon2 import PasswordHasher
        from argon2.exceptions import Argon2Error, InvalidHashError

        row = self._row(user_id)
        if row is None or not row["password_hash"] or not isinstance(password, str):
            # the same argon2 work as for a person with a password: the time never says who has one (review 04.11.2026)
            try:
                PasswordHasher().verify(_dummy_hash(), password if isinstance(password, str) else "")
            except (Argon2Error, InvalidHashError):
                pass
            return False
        try:
            return PasswordHasher().verify(row["password_hash"], password)
        except (Argon2Error, InvalidHashError):
            return False

    def totp_begin(self, user_id, secret_key: bytes) -> str:
        """A new secret (base32, for the QR code); it works only after ``totp_confirm``."""
        import pyotp

        b32 = pyotp.random_base32()
        token = _fernet(secret_key).encrypt(b32.encode("ascii")).decode("ascii")
        with self.store._tx():
            self._require(user_id)
            self._conn.execute(
                "UPDATE users SET totp_secret=?, totp_enabled=0 WHERE id=?",
                (token, user_id),
            )
        return b32

    def _totp_step(self, row, code, secret_key) -> Optional[int]:
        """The time step (RFC 6238 counter) the code belongs to, within one step of the wall clock; None otherwise."""
        import hmac

        import pyotp
        from cryptography.fernet import InvalidToken

        if not row["totp_secret"] or not isinstance(code, str):
            return None
        try:
            b32 = _fernet(secret_key).decrypt(row["totp_secret"].encode("ascii"))
        except InvalidToken:
            return None
        code = code.strip().replace(" ", "")
        if not code.isdigit():
            return None
        totp = pyotp.TOTP(b32.decode("ascii"))
        current = totp.timecode(datetime.now(timezone.utc))
        for step in (current - 1, current, current + 1):
            if hmac.compare_digest(totp.generate_otp(step), code):
                return step
        return None

    def _totp_take(self, user_id, step) -> bool:
        """The step becomes the person's last accepted one — once: a code seen before is refused (RFC 6238 §5.2),
        however long its window still runs (global review 15.10.2026). Runs inside the caller's transaction."""
        taken = self._conn.execute(
            "UPDATE users SET totp_last_step=? WHERE id=? AND totp_last_step<?",
            (step, user_id, step),
        )
        return taken.rowcount == 1

    def totp_confirm(self, user_id, code, secret_key: bytes, now) -> bool:
        row = self._row(user_id)
        step = None if row is None else self._totp_step(row, code, secret_key)
        if step is None:
            return False
        with self.store._tx():
            if not self._totp_take(user_id, step):
                return False
            self._conn.execute("UPDATE users SET totp_enabled=1 WHERE id=?", (user_id,))
            self.store._event(
                f"user:{user_id}",
                "user.updated",
                "",
                {"user": user_id, "fields": ["totp"]},
            )
        return True

    def totp_verify(self, user_id, code, secret_key: bytes, now) -> bool:
        """A confirmed person's fresh code; a write (the step taken), so the service runs it under its lock."""
        row = self._row(user_id)
        if row is None or not row["totp_enabled"]:
            return False
        step = self._totp_step(row, code, secret_key)
        if step is None:
            return False
        with self.store._tx():
            return self._totp_take(user_id, step)

    # --- passkeys -------------------------------------------------------------------------------
    def add_credential(self, user_id, registered, name, now) -> dict:
        """A verified passkey (``passkeys.Registered``) of the person; the event names it, never its key."""
        name = str(name or "").strip()[:NAME_MAX] or "passkey"
        with self.store._tx():
            self._require(user_id)
            cursor = self._conn.execute(
                "INSERT INTO credentials (user, credential_id, public_key, sign_count, transports, name, created) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    user_id,
                    registered.credential_id,
                    registered.public_key,
                    registered.sign_count,
                    json.dumps(list(registered.transports)),
                    name,
                    now,
                ),
            )
            self.store._event(
                f"user:{user_id}",
                "user.passkey_added",
                "",
                {"user": user_id, "credential": cursor.lastrowid, "name": name},
            )
        return next(c for c in self.credentials(user_id) if c["id"] == cursor.lastrowid)

    def credentials(self, user_id) -> List[dict]:
        """The person's passkeys as the settings page shows them (no key material)."""
        return [
            {**dict(r), "signing_enabled": bool(r["signing_enabled"])}
            for r in self._conn.execute(
                "SELECT id, name, signing_enabled, created, last_used FROM credentials WHERE user=? ORDER BY id",
                (user_id,),
            )
        ]

    def credential(self, credential_id: bytes) -> Optional[dict]:
        """One passkey by its WebAuthn id, with its public key and counter, or None."""
        row = self._conn.execute(
            "SELECT * FROM credentials WHERE credential_id=?", (credential_id,)
        ).fetchone()
        return None if row is None else dict(row)

    def signing_keys(self, user_id) -> List[dict]:
        """The person's passkeys that may sign, with their public keys (root gives them to kd-review)."""
        return [
            dict(r)
            for r in self._conn.execute(
                "SELECT credential_id, public_key, name, created FROM credentials WHERE user=? AND signing_enabled=1"
                " ORDER BY id",
                (user_id,),
            )
        ]

    def credential_ids(self, user_id, *, signing=False) -> List[bytes]:
        query = "SELECT credential_id FROM credentials WHERE user=?" + (
            " AND signing_enabled=1" if signing else ""
        )
        return [bytes(r[0]) for r in self._conn.execute(query, (user_id,))]

    def set_signing(self, user_id, prefix: str, enabled: bool) -> dict:
        """one of the person's passkeys becomes a signing key (the targets' exec gate verifies its
        assertions) or stops being one — root names it by a prefix of its base64url id, which must match exactly
        one key, else ValueError. The event names the key, never its material; the row comes back with the
        base64url id, not the key."""
        if not isinstance(prefix, str) or not prefix.strip():
            raise ValueError("name the key by a prefix of its id")
        prefix = prefix.strip()
        if len(prefix) < SIGNING_PREFIX_MIN:  # a one-letter prefix would name a key by luck
            raise ValueError(f"a key prefix needs at least {SIGNING_PREFIX_MIN} characters of its id")
        rows = self._conn.execute(
            "SELECT id, credential_id, name FROM credentials WHERE user=? ORDER BY id", (user_id,)
        ).fetchall()
        found = [r for r in rows if _b64url(r["credential_id"]).startswith(prefix)]
        if len(found) != 1:
            raise ValueError(f"{len(found)} keys of {user_id} start with {prefix!r}; name exactly one")
        row = found[0]
        with self.store._tx():
            self._conn.execute(
                "UPDATE credentials SET signing_enabled=? WHERE id=?", (int(bool(enabled)), row["id"])
            )
            self.store._event(
                f"user:{user_id}",
                "user.passkey_signing",
                "",
                {"user": user_id, "credential": row["id"], "enabled": bool(enabled)},
            )
        return {"id": row["id"], "name": row["name"], "signing_enabled": bool(enabled),
                "credential_id": _b64url(row["credential_id"])}

    def remove_credential(self, user_id, row_id) -> bool:
        """Remove one of the person's passkeys; False when it is not theirs."""
        with self.store._tx():
            gone = self._conn.execute(
                "DELETE FROM credentials WHERE id=? AND user=?", (row_id, user_id)
            ).rowcount
            if gone:
                self.store._event(
                    f"user:{user_id}",
                    "user.passkey_removed",
                    "",
                    {"user": user_id, "credential": row_id},
                )
        return bool(gone)

    def credential_used(self, row_id, sign_count, now):
        with self.store._tx():
            self._conn.execute(
                "UPDATE credentials SET sign_count=?, last_used=? WHERE id=?",
                (sign_count, now, row_id),
            )

    def signs(self, user_id) -> bool:
        """The person has a signing factor (a passkey or a confirmed TOTP): changing factors then needs one."""
        second = self.has_second_factor(user_id)
        return second["passkey"] or second["totp"]

    # --- web sessions --------------------------------------------------------------------------
    def open_session(self, user_id, device, now, ttl_days=SESSION_DAYS) -> str:
        """A new session; the token goes to the cookie, the base keeps its sha256 (the session id)."""
        token = secrets.token_urlsafe(32)
        device = str(device or "")[:DEVICE_MAX]
        with self.store._tx():
            self._require(user_id)
            self._conn.execute(
                "INSERT INTO web_sessions (id, user, device, created, last_seen, expires) VALUES (?,?,?,?,?,?)",
                (digest(token), user_id, device, now, now, shift(now, days=ttl_days)),
            )
            self._conn.execute(
                "UPDATE users SET last_login=? WHERE id=?", (now, user_id)
            )
            self.store._event(
                f"user:{user_id}",
                "user.session_opened",
                "",
                {"user": user_id, "device": device},
            )
        return token

    def session(self, token, now, ttl_days=SESSION_DAYS) -> Optional[dict]:
        """The live session of a token (sliding: last use + 30 days), or None."""
        if not isinstance(token, str) or not token:
            return None
        row = self._conn.execute(
            "SELECT s.*, u.status FROM web_sessions s JOIN users u ON u.id = s.user WHERE s.id=?",
            (digest(token),),
        ).fetchone()
        if row is None or row["status"] != "active":
            return None
        if row["expires"] <= now:
            self._conn.execute("DELETE FROM web_sessions WHERE id=?", (row["id"],))
            return None
        session = {
            k: row[k]
            for k in ("id", "user", "device", "created", "last_seen", "expires")
        }
        if parse_time(now) - parse_time(row["last_seen"]) >= SLIDE:
            session["last_seen"], session["expires"] = now, shift(now, days=ttl_days)
            self._conn.execute(
                "UPDATE web_sessions SET last_seen=?, expires=? WHERE id=?",
                (now, session["expires"], row["id"]),
            )
        return session

    def session_alive(self, session_id, now) -> bool:
        """The session still stands (not closed, not expired, its person active); nothing is written."""
        row = self._conn.execute(
            "SELECT s.expires, u.status FROM web_sessions s JOIN users u ON u.id = s.user WHERE s.id=?",
            (session_id,),
        ).fetchone()
        return row is not None and row["status"] == "active" and row["expires"] > now

    def sessions(self, user_id) -> List[dict]:
        return [
            dict(r)
            for r in self._conn.execute(
                "SELECT id, device, created, last_seen, expires FROM web_sessions WHERE user=? ORDER BY last_seen DESC",
                (user_id,),
            )
        ]

    def close_session(self, token):
        self.close_session_id(digest(token) if isinstance(token, str) else "")

    def close_session_id(self, session_id, user_id=None) -> bool:
        with self.store._tx():
            row = self._conn.execute(
                "SELECT user FROM web_sessions WHERE id=?", (session_id,)
            ).fetchone()
            if row is None or (user_id is not None and row["user"] != user_id):
                return False
            self._conn.execute("DELETE FROM web_sessions WHERE id=?", (session_id,))
            self.store._event(
                f"user:{row['user']}",
                "user.session_closed",
                "",
                {"user": row["user"]},
            )
        return True

    def close_all(self, user_id, actor=None):
        """Every session of the person, and every push subscription: a device signed out everywhere hears nothing."""
        with self.store._tx():
            self._conn.execute("DELETE FROM web_sessions WHERE user=?", (user_id,))
            self._conn.execute("DELETE FROM push_subscriptions WHERE user=?", (user_id,))
            self.store._event(
                actor or f"user:{user_id}",
                "user.session_closed",
                "",
                {"user": user_id, "all": True},
            )

    # --- login attempts ------------------------------------------------------------------------
    def failed_attempt(self, key, now, window=900) -> int:
        with self.store._tx():
            self._conn.execute(
                "DELETE FROM login_attempts WHERE at<=?", (shift(now, seconds=-window),)
            )
            self._conn.execute(
                "INSERT INTO login_attempts (key, at) VALUES (?,?)", (key, now)
            )
            return self._count(key, now, window)

    def _count(self, key, now, window) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM login_attempts WHERE key=? AND at>?",
            (key, shift(now, seconds=-window)),
        ).fetchone()[0]

    def too_many(self, key, now, limit=5, window=900) -> bool:
        return self._count(key, now, window) >= limit

    def clear_attempts(self, key):
        self._conn.execute("DELETE FROM login_attempts WHERE key=?", (key,))
