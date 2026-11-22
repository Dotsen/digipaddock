"""Owner grants for admin cards: the only source of authority for ordinary operations.

A grant is written by the approval service after an authenticated owner POST, or by the root-only
owner CLI; never by the agent, never from the board. It lives in a directory that only the service
user can write (``/var/lib/digipaddock/admin-grants``); the executor reads it. Every decision is
first appended to ``decisions.jsonl`` (fsync), then the grant file is replaced atomically, both under
one store lock: a failed journal write means no grant.

Reading is fail closed: a missing or foreign-owned directory, a symlink, a group- or world-writable
file, an unknown schema, a malformed field, a time from the future or a lifetime over seven days is
an error, not an empty answer. ``check_usable`` ties the grant to the current card: active state,
not expired, same scope hash, admin card of the sysadmin in To Do or In Progress.

A decision taken in the owner's cabinet names the person, ``approved_by: user:<id>``, and
carries the passkey that signed it: ``signer`` (the credential id) and ``signature`` (the WebAuthn
assertion). Both keys are optional on read, so grants written before the cabinet stay valid.
"""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import FrozenSet, Optional, Tuple

from kd import admin_task, atomic_file
from kd.admin_operations import Proposal
from kd.admin_task import Card, canonical, check_task_id

SCHEMA_VERSION = 1
DEFAULT_TTL = timedelta(hours=72)
MAX_TTL = timedelta(days=7)
CLOCK_SKEW = timedelta(minutes=5)
MAX_BYTES = 64 * 1024
JOURNAL = "decisions.jsonl"
LOCK = "store"  # atomic_file.locked takes <root>/store.lock for every decision
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_SHA = re.compile(r"[0-9a-f]{64}")
APPROVER = re.compile(r"owner|user:[a-z][a-z0-9_-]{0,31}")
# the approver of a plan whose every step belongs to a test environment of the catalog — the cabinet's
# rule, no person and no signature. Only a server plan's grant may carry it; what such a grant may run is checked
# again at every step (kd-admin-run) and by the target's own list (agent-exec-gate).
TEST_ZONE = "rule:test-env"
MAX_SIGNER = 512
MAX_SIGNATURE = 16 * 1024
SIGNED = frozenset({"signer", "signature"})  # optional: only decisions signed in the cabinet carry them
_KEYS = frozenset({
    "schema_version", "grant_id", "task_id", "assignee", "project", "card_scope_sha256", "proposal_sha256",
    "operations", "approved_at", "expires_at", "approved_by", "state", "decision_id", "revoked_at",
})
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
# one grant format, a policy per role, from the grant's (assignee, project) — never a key of its own, so an
# older grant reads as it always did: the sysadmin's server operations (the default everywhere) or the factory's
SERVER = "server"
FACTORY = "factory"


@dataclass(frozen=True)
class Factory:
    """Who the factory policy applies to (``kd.factory_ops.Catalog.scope``): its projects and requesters."""

    projects: FrozenSet[str]
    requesters: FrozenSet[str]


class GrantError(ValueError):
    """No usable grant: the run or the decision stops here, before any SSH."""


class Conflict(GrantError):
    """The card or the proposal changed since the owner's page was rendered (HTTP 409)."""


@dataclass(frozen=True)
class Grant:
    grant_id: str
    task_id: str
    assignee: str
    project: str
    card_scope_sha256: str
    proposal_sha256: str
    operations: Tuple[dict, ...]
    approved_at: datetime
    expires_at: datetime
    approved_by: str
    state: str
    decision_id: str
    revoked_at: Optional[datetime] = None
    signer: str = ""
    signature: str = ""

    def to_dict(self) -> dict:
        data = {
            "schema_version": SCHEMA_VERSION, "grant_id": self.grant_id, "task_id": self.task_id,
            "assignee": self.assignee, "project": self.project, "card_scope_sha256": self.card_scope_sha256,
            "proposal_sha256": self.proposal_sha256, "operations": list(self.operations),
            "approved_at": fmt(self.approved_at), "expires_at": fmt(self.expires_at),
            "approved_by": self.approved_by, "state": self.state, "decision_id": self.decision_id,
            "revoked_at": fmt(self.revoked_at) if self.revoked_at else None,
        }
        return signed(data, self.signer, self.signature)


def signed(data: dict, signer: str, signature: str) -> dict:
    """``data`` with the signing keys when the decision was signed; untouched otherwise."""
    if signer:
        data["signer"] = signer
    if signature:
        data["signature"] = signature
    return data


def check_approver(value: object, label: str, rule: bool = False) -> str:
    """A person — ``owner`` or ``user:<id>``; ``rule`` also takes the test-zone rule (a server plan's grant)."""
    if rule and value == TEST_ZONE:
        return TEST_ZONE
    if not isinstance(value, str) or not APPROVER.fullmatch(value):
        raise GrantError(f"{label}: approved_by must be owner or user:<id>")
    return value


def check_signature(signer: object, signature: object, label: str) -> Tuple[str, str]:
    if not isinstance(signer, str) or len(signer) > MAX_SIGNER:
        raise GrantError(f"{label}: signer must be text up to {MAX_SIGNER} characters")
    if not isinstance(signature, str) or len(signature) > MAX_SIGNATURE:
        raise GrantError(f"{label}: signature must be text up to {MAX_SIGNATURE} characters")
    return signer, signature


def has_keys(raw: dict, required: FrozenSet[str]) -> bool:
    """Exactly the required keys plus, optionally, the signing ones."""
    return required <= set(raw) <= required | SIGNED


def fmt(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(TIME_FORMAT)


def parse_time(value: object, name: str) -> datetime:
    if not isinstance(value, str) or not _TIME.fullmatch(value):
        raise GrantError(f"{name}: time must look like 2026-10-09T18:00:00Z")
    try:
        return datetime.strptime(value, TIME_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise GrantError(f"{name}: invalid time {value!r}") from exc


def reader() -> str:
    """The user this process reads as, for a refusal that names a missing read right."""
    uid = os.geteuid() if hasattr(os, "geteuid") else -1
    try:
        import pwd

        return pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError):
        return f"uid {uid}"


def policy_of(assignee: str, project: str, factory: Optional[Factory] = None) -> Optional[str]:
    """The policy of a grant for @``assignee`` in ``project``: the sysadmin's admin card is ``server``, the factory's
    requester in one of its projects ``factory``; None for any other pair."""
    if (assignee, project) == (admin_task.ASSIGNEE, admin_task.PROJECT):
        return SERVER
    if factory is not None and assignee in factory.requesters and project in factory.projects:
        return FACTORY
    return None


def check_policy(task_id: str, assignee: str, project: str, policies: Tuple[str, ...],
                 factory: Optional[Factory] = None) -> str:
    """The pair's policy when it is one of ``policies``; GrantError otherwise (the default: the sysadmin's only)."""
    policy = policy_of(assignee, project, factory)
    if policy in policies:
        return policy
    if policies == (SERVER,):
        raise GrantError(f"{task_id}: grant is not for @{admin_task.ASSIGNEE} in {admin_task.PROJECT}")
    raise GrantError(f"{task_id}: a grant for @{assignee} in {project} is not a {' or '.join(policies)} grant")


def parse_grant(data: bytes, task_id: str, policies: Tuple[str, ...] = (SERVER,),
                factory: Optional[Factory] = None) -> Grant:
    if len(data) > MAX_BYTES:
        raise GrantError(f"{task_id}: grant file is too large")
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise GrantError(f"{task_id}: grant file is not JSON") from exc
    if not isinstance(raw, dict) or not has_keys(raw, _KEYS):
        raise GrantError(f"{task_id}: grant keys do not match schema v{SCHEMA_VERSION}")
    if raw["schema_version"] != SCHEMA_VERSION:
        raise GrantError(f"{task_id}: unknown grant schema_version {raw['schema_version']!r}")
    if raw["task_id"] != task_id:
        raise GrantError(f"{task_id}: grant file names task {raw['task_id']!r}")
    for key in ("grant_id", "decision_id"):
        if not isinstance(raw[key], str) or not _UUID.fullmatch(raw[key]):
            raise GrantError(f"{task_id}: invalid {key}")
    for key in ("card_scope_sha256", "proposal_sha256"):
        if not isinstance(raw[key], str) or not _SHA.fullmatch(raw[key]):
            raise GrantError(f"{task_id}: invalid {key} (sha256 hex)")
    signer, signature = check_signature(raw.get("signer", ""), raw.get("signature", ""), task_id)
    if raw["state"] not in ("active", "revoked"):
        raise GrantError(f"{task_id}: unknown state {raw['state']!r}")
    if not isinstance(raw["assignee"], str) or not isinstance(raw["project"], str):
        raise GrantError(f"{task_id}: grant is not for @{admin_task.ASSIGNEE} in {admin_task.PROJECT}")
    policy = check_policy(task_id, raw["assignee"], raw["project"], policies, factory)
    check_approver(raw["approved_by"], task_id, rule=policy == SERVER)
    operations = raw["operations"]
    if not isinstance(operations, list) or not operations or not all(isinstance(op, dict) for op in operations):
        raise GrantError(f"{task_id}: operations must be a non-empty list of objects")
    approved, expires = parse_time(raw["approved_at"], "approved_at"), parse_time(raw["expires_at"], "expires_at")
    if not timedelta(0) < expires - approved <= MAX_TTL:
        raise GrantError(f"{task_id}: grant ttl must be within {MAX_TTL.days} days")
    revoked = None
    if raw["revoked_at"] is not None:
        revoked = parse_time(raw["revoked_at"], "revoked_at")
    if (raw["state"] == "revoked") != (revoked is not None):
        raise GrantError(f"{task_id}: state and revoked_at disagree")
    return Grant(raw["grant_id"], task_id, raw["assignee"], raw["project"], raw["card_scope_sha256"],
                 raw["proposal_sha256"], tuple(operations), approved, expires, raw["approved_by"], raw["state"],
                 raw["decision_id"], revoked, signer, signature)


def check_usable(grant: Grant, card: Card, now: datetime, policy: str = SERVER,
                 factory: Optional[Factory] = None) -> None:
    """The grant may drive a run now for this card under ``policy``; GrantError or CardError otherwise."""
    check_policy(grant.task_id, grant.assignee, grant.project, (policy,), factory)
    if grant.state != "active":
        raise GrantError(f"{grant.task_id}: grant {grant.grant_id} is revoked")
    if now >= grant.expires_at:
        raise GrantError(f"{grant.task_id}: grant expired at {fmt(grant.expires_at)}")
    if grant.approved_at - now > CLOCK_SKEW:
        raise GrantError(f"{grant.task_id}: grant approved in the future ({fmt(grant.approved_at)}); check the clock")
    if card.task_id != grant.task_id:
        raise GrantError(f"{grant.task_id}: card {card.task_id} is another task")
    admin_task.check_admin(card, project=grant.project, assignee=grant.assignee)
    admin_task.check_active(card)
    if card.scope_sha256 != grant.card_scope_sha256:
        raise GrantError(f"{grant.task_id}: the card's scope changed since the owner's approval; a new approval is needed")


def operation_at(grant: Grant, index: int) -> dict:
    """The 1-based ``index``-th approved operation."""
    if not isinstance(index, int) or isinstance(index, bool) or not 1 <= index <= len(grant.operations):
        raise GrantError(f"{grant.task_id}: no operation {index!r} (1..{len(grant.operations)})")
    return dict(grant.operations[index - 1])




def _append_journal(path: str, record: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | _NOFOLLOW, 0o640)
    with os.fdopen(fd, "ab") as handle:
        handle.write(canonical(record) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())


class Store:
    """Grants under ``root``; ``owner_uid`` is the service user every file and the directory must belong to."""

    def __init__(self, root: str, owner_uid: Optional[int]):
        self.root = root
        self.owner_uid = owner_uid

    def path(self, task_id: str) -> str:
        return os.path.join(self.root, check_task_id(task_id) + ".json")

    def _check_root(self) -> None:
        try:
            info = os.lstat(self.root)
        except OSError as exc:
            raise GrantError(f"grant directory {self.root} is missing") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise GrantError(f"grant directory {self.root} is not a directory")
        if self.owner_uid is not None and info.st_uid != self.owner_uid:
            raise GrantError(f"grant directory {self.root} has the wrong owner")
        if os.name == "posix" and info.st_mode & 0o022:
            raise GrantError(f"grant directory {self.root} is writable by others")

    def load(self, task_id: str, policies: Tuple[str, ...] = (SERVER,),
             factory: Optional[Factory] = None) -> Optional[Grant]:
        """The stored grant, None when there is none; GrantError when the store cannot be trusted or the grant is of
        another policy than ``policies``."""
        data = self.read(self.path(task_id), task_id)
        return None if data is None else parse_grant(data, task_id, policies, factory)

    def read(self, path: str, label: str) -> Optional[bytes]:
        """Bytes of one store file: a regular file of the service user, not writable by others; None if missing."""
        self._check_root()
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return None
        except PermissionError as exc:
            raise GrantError(f"{label}: the grant store {self.root} is not readable by {reader()}") from exc
        except OSError as exc:
            raise GrantError(f"{label}: grant file cannot be read") from exc
        if not stat.S_ISREG(info.st_mode):
            raise GrantError(f"{label}: grant file is not a regular file")
        try:
            fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
        except PermissionError as exc:
            raise GrantError(f"{label}: {path} is not readable by {reader()}") from exc
        except OSError as exc:
            raise GrantError(f"{label}: grant file cannot be opened") from exc
        with os.fdopen(fd, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise GrantError(f"{label}: grant file changed while it was opened")
            if self.owner_uid is not None and opened.st_uid != self.owner_uid:
                raise GrantError(f"{label}: grant file has the wrong owner")
            if os.name == "posix" and opened.st_mode & 0o022:
                raise GrantError(f"{label}: grant file mode {stat.S_IMODE(opened.st_mode):o} is writable by others")
            return handle.read(MAX_BYTES + 1)

    def approve(self, card: Card, proposal: Proposal, *, now: datetime, ttl: timedelta, via: str,
                expect_card_sha256: str, expect_proposal_sha256: str, approved_by: str = "owner", signer: str = "",
                signature: str = "", policy: str = SERVER, factory: Optional[Factory] = None) -> Grant:
        """Record the approval of exactly the card scope and proposal the page showed, by ``approved_by``; the card's
        single assignee and project must make ``policy``'s pair."""
        check_approver(approved_by, card.task_id, rule=policy == SERVER)
        check_signature(signer, signature, card.task_id)
        if not timedelta(0) < ttl <= MAX_TTL:
            raise GrantError(f"ttl must be within {MAX_TTL.days} days")
        if proposal.task_id != card.task_id:
            raise GrantError(f"the proposal is for {proposal.task_id}, the card is {card.task_id}")
        assignee = card.assignees[0] if len(card.assignees) == 1 else ""
        check_policy(card.task_id, assignee, card.project, (policy,), factory)
        admin_task.check_admin(card, project=card.project, assignee=assignee)
        if card.scope_sha256 != expect_card_sha256 or proposal.sha256 != expect_proposal_sha256:
            raise Conflict(f"{card.task_id}: the card or the proposal changed; reload the page")
        self._check_root()
        with atomic_file.locked(os.path.join(self.root, LOCK), 0o600):
            previous = self._previous(card.task_id, factory)
            grant = Grant(str(uuid.uuid4()), card.task_id, assignee, card.project,
                          card.scope_sha256, proposal.sha256, tuple(op.as_dict() for op in proposal.operations),
                          _second(now), _second(now) + ttl, approved_by, "active", str(uuid.uuid4()),
                          signer=signer, signature=signature)
            self._decide("approve", grant, previous, via, now)
            atomic_file.replace(self.path(card.task_id), json.dumps(grant.to_dict(), ensure_ascii=False,
                                                                    indent=1).encode("utf-8"), 0o640)
        return grant

    def _previous(self, task_id: str, factory: Optional[Factory]) -> Optional[Grant]:
        """The grant a new approval replaces, of either policy (a card moved between them keeps one file)."""
        return self.load(task_id, (SERVER, FACTORY), factory or Factory(frozenset(), frozenset()))

    def revoke(self, task_id: str, *, now: datetime, via: str, by: str = "owner", policies: Tuple[str, ...] = (SERVER,),
               factory: Optional[Factory] = None) -> Grant:
        check_approver(by, task_id)
        self._check_root()
        with atomic_file.locked(os.path.join(self.root, LOCK), 0o600):
            current = self.load(task_id, policies, factory)
            if current is None:
                raise GrantError(f"{task_id}: no grant to revoke")
            if current.state == "revoked":
                return current
            revoked = Grant(**{**current.__dict__, "state": "revoked", "revoked_at": _second(now)})
            self._decide("revoke", revoked, current, via, now, by)
            atomic_file.replace(self.path(task_id), json.dumps(revoked.to_dict(), ensure_ascii=False,
                                                               indent=1).encode("utf-8"), 0o640)
        return revoked

    def journal(self, record: dict) -> None:
        """Append one decision (fsync) before its file changes; a failure means no decision."""
        try:
            _append_journal(os.path.join(self.root, JOURNAL), record)
        except OSError as exc:
            raise GrantError(f"decision journal cannot be written ({exc.__class__.__name__}); nothing was granted") from exc

    def _decide(self, action: str, grant: Grant, previous: Optional[Grant], via: str, now: datetime,
                by: Optional[str] = None) -> None:
        record = {
            "ts": fmt(now), "decision_id": grant.decision_id if action == "approve" else str(uuid.uuid4()),
            "action": action, "task_id": grant.task_id, "grant_id": grant.grant_id,
            "previous_grant_id": previous.grant_id if previous else None,
            "card_scope_sha256": grant.card_scope_sha256, "proposal_sha256": grant.proposal_sha256,
            "expires_at": fmt(grant.expires_at), "by": by or grant.approved_by, "via": via,
        }
        self.journal(record)


def _second(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(microsecond=0)
