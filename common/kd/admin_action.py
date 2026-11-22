"""A critical action — one exact command on ordered servers, approved once.

The agent proposes the exact UTF-8 command (``kd-admin-propose-action``); the hard denylist is checked
already then. The owner sees the whole text on its own page and approves it: the service writes an
action grant valid for 15 minutes, bound to the card scope and the action hash. ``kd-admin-critical``
(the exec user) asks the service over a local socket to *consume* it before SSH; the service marks it
used under the store lock (journal first), so one approval runs at most once, and a retry after a
failure or a timeout needs a new decision — the remote command may have run.

Schema 2 of the action explains it to the owner: ``basis`` (what the agent
saw), ``effect`` and ``risk`` beside the intent and the rollback. The grant schema stays 1; a grant
from the cabinet names the person and the passkey signature (see ``admin_grant``).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Iterable, Optional, Tuple

from kd import admin_grant, admin_task, atomic_file, exec_policy, naming
from kd import admin_operations as ops
from kd.admin_grant import GrantError, fmt, parse_time
from kd.admin_task import ALIAS, TASK_ID, Card, canonical

SCHEMA_VERSION = 2  # of the action the agent proposes
GRANT_SCHEMA = 1
SCHEMA_HINT = "schema_version 2: add basis, effect and risk to the action"
ACTION_TTL = timedelta(minutes=15)
MAX_COMMAND = 8 * 1024


def production() -> str:
    """The production server's alias (host/host.json of the release tree); fails closed."""
    try:
        return naming.load_host()["prod_alias"]
    except naming.NamingError as exc:
        raise ops.ProposalError(f"production alias unknown: {exc}") from None


# system.reboot — the helper on the target writes the snapshot of routing, WireGuard, listeners and running
# services into /root/<card>/ and then reboots; the owner signs this exact command like any critical action
ACTION_VERBS = ("system.reboot",)
REBOOT_WORDS = ops.TYPED_HELPER + " reboot"


def reboot_command(task_id: object) -> str:
    if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
        raise ops.ProposalError("task_id must look like KD-123")
    return shlex.join(["python3", "-I", ops.TYPED_HELPER, "reboot", task_id])


PROPOSAL_ID = re.compile(r"[0-9a-f]{16}")
_KEYS = frozenset({"schema_version", "task_id", "intent", "basis", "servers", "command", "effect", "risk",
                   "rollback"})
_GRANT_KEYS = frozenset({
    "schema_version", "kind", "action_id", "proposal_id", "task_id", "assignee", "project", "card_scope_sha256",
    "action_sha256", "servers", "command", "approved_at", "expires_at", "approved_by", "state", "decision_id",
    "used_at",
})
_STATES = ("active", "consumed", "revoked")
Deny = Callable[[str, str], Optional[Tuple[str, str]]]  # (command, server) -> (rule, why) or None


@dataclass(frozen=True)
class Action:
    task_id: str
    intent: str
    servers: Tuple[str, ...]
    command: str
    rollback: str
    basis: str = ""
    effect: str = ""
    risk_level: str = ""
    risk_why: str = ""

    def as_dict(self) -> dict:
        return {"schema_version": SCHEMA_VERSION, "task_id": self.task_id, "intent": self.intent,
                "basis": self.basis, "servers": list(self.servers), "command": self.command, "effect": self.effect,
                "risk": {"level": self.risk_level, "why": self.risk_why}, "rollback": self.rollback}

    @property
    def sha256(self) -> str:
        return hashlib.sha256(canonical(self.as_dict())).hexdigest()

    @property
    def proposal_id(self) -> str:
        return self.sha256[:16]


def validate_action(data: object, inventory: Iterable[str], enabled: Iterable[str], *,
                    card_servers: Iterable[str], deny: Optional[Deny] = None) -> Action:
    if not isinstance(data, dict):
        raise ops.ProposalError("action must be a JSON object")
    if data.get("schema_version") == 1:
        raise ops.ProposalError(SCHEMA_HINT)
    if "verb" in data:  # a typed critical action — the stage forms its command
        if "command" in data or data["verb"] not in ACTION_VERBS:
            raise ops.ProposalError(f"verb must be one of {', '.join(ACTION_VERBS)}, without a command")
        data = dict(data, command=reboot_command(data.get("task_id")))
        del data["verb"]
    if set(data) != _KEYS:
        raise ops.ProposalError(f"action keys must be exactly {sorted(_KEYS)}")
    if data["schema_version"] != SCHEMA_VERSION:
        raise ops.ProposalError(f"unknown action schema_version {data['schema_version']!r}")
    task_id = data["task_id"]
    if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
        raise ops.ProposalError("task_id must look like KD-123")
    intent, rollback = ops._text(data, "intent"), ops._text(data, "rollback", ops.MAX_EXPLAIN)
    basis, effect = ops._text(data, "basis"), ops._text(data, "effect", ops.MAX_EXPLAIN)
    level, why = ops.risk(data["risk"])
    command = data["command"]
    if not isinstance(command, str) or not command.strip():
        raise ops.ProposalError("command must be a non-empty string")
    if len(command.encode("utf-8", "surrogatepass")) > MAX_COMMAND:
        raise ops.ProposalError("command is longer than 8 KiB")
    hidden = ops.hidden_char(command, "\t\n")  # tab and newline stay: shell scripts use them
    if hidden:
        raise ops.ProposalError(f"command holds an invisible or control character ({hidden})")
    servers = data["servers"]
    if not isinstance(servers, list) or not servers or len(servers) > ops.MAX_SERVERS:
        raise ops.ProposalError(f"servers must be a list of 1..{ops.MAX_SERVERS} aliases")
    inventory, enabled, labeled = set(inventory), set(enabled), set(card_servers)
    for server in servers:
        if not isinstance(server, str) or not ALIAS.fullmatch(server) or server not in inventory:
            raise ops.ProposalError(f"server {server!r} is not in the inventory")
        if server not in enabled:
            raise ops.ProposalError(f"server {server} is not enabled")
        if server not in labeled:
            raise ops.ProposalError(f"server {server} is not a server:{server} label of the card")
    if len(set(servers)) != len(servers):
        raise ops.ProposalError("a server is listed twice")
    prod = production()
    if prod in servers and servers[-1] != prod:
        raise ops.ProposalError(f"{prod} must be the last server")
    if REBOOT_WORDS in command:
        if command != reboot_command(task_id):
            raise ops.ProposalError("the helper's reboot runs only as the verb system.reboot of this card")
        try:
            home = ops.load_home()
        except ValueError as exc:
            raise ops.ProposalError(f"system.reboot needs the factory's home server: {exc}") from None
        if home in servers:
            raise ops.ProposalError(f"{home} is the factory's own host: system.reboot never reboots it")
    # rule 1 of the exec gate's policy, with the reason the target's gate gives (an action is critical by
    # its kind, so rule 2 has nothing to add)
    guarded = exec_policy.protect(command)
    if guarded is not None:
        raise ops.ProposalError(guarded)
    if deny is not None:
        for server in servers:
            verdict = deny(command, server)
            if verdict:
                rule, why = verdict
                raise ops.ProposalError(f"denied by rule {rule} on {server}: {why}; the owner does this by hand")
    return Action(task_id, intent, tuple(servers), command, rollback, basis, effect, level, why)


@dataclass(frozen=True)
class ActionGrant:
    action_id: str
    proposal_id: str
    task_id: str
    card_scope_sha256: str
    action_sha256: str
    servers: Tuple[str, ...]
    command: str
    approved_at: datetime
    expires_at: datetime
    state: str
    decision_id: str
    used_at: Optional[datetime] = None
    approved_by: str = "owner"
    signer: str = ""
    signature: str = ""

    def to_dict(self) -> dict:
        return admin_grant.signed({
            "schema_version": GRANT_SCHEMA, "kind": "action", "action_id": self.action_id,
            "proposal_id": self.proposal_id, "task_id": self.task_id, "assignee": admin_task.ASSIGNEE,
            "project": admin_task.PROJECT, "card_scope_sha256": self.card_scope_sha256,
            "action_sha256": self.action_sha256, "servers": list(self.servers), "command": self.command,
            "approved_at": fmt(self.approved_at), "expires_at": fmt(self.expires_at), "approved_by": self.approved_by,
            "state": self.state, "decision_id": self.decision_id,
            "used_at": fmt(self.used_at) if self.used_at else None,
        }, self.signer, self.signature)


def check_proposal_id(value: object) -> str:
    if not isinstance(value, str) or not PROPOSAL_ID.fullmatch(value):
        raise GrantError("action id must be 16 hex characters")
    return value


def parse_action_grant(data: bytes, task_id: str, proposal_id: str) -> ActionGrant:
    label = f"{task_id} action {proposal_id}"
    if len(data) > admin_grant.MAX_BYTES:
        raise GrantError(f"{label}: file is too large")
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise GrantError(f"{label}: file is not JSON") from exc
    if not isinstance(raw, dict) or not admin_grant.has_keys(raw, _GRANT_KEYS):
        raise GrantError(f"{label}: keys do not match schema v{GRANT_SCHEMA}")
    if raw["schema_version"] != GRANT_SCHEMA or raw["kind"] != "action":
        raise GrantError(f"{label}: unknown schema")
    if raw["task_id"] != task_id or raw["proposal_id"] != proposal_id:
        raise GrantError(f"{label}: file names another action")
    if raw["assignee"] != admin_task.ASSIGNEE or raw["project"] != admin_task.PROJECT:
        raise GrantError(f"{label}: not an action for @{admin_task.ASSIGNEE}")
    approved_by = admin_grant.check_approver(raw["approved_by"], label)
    signer, signature = admin_grant.check_signature(raw.get("signer", ""), raw.get("signature", ""), label)
    if raw["state"] not in _STATES:
        raise GrantError(f"{label}: unknown state {raw['state']!r}")
    for key in ("action_id", "decision_id"):
        if not isinstance(raw[key], str) or not admin_grant._UUID.fullmatch(raw[key]):
            raise GrantError(f"{label}: invalid {key}")
    if not isinstance(raw["card_scope_sha256"], str) or not admin_grant._SHA.fullmatch(raw["card_scope_sha256"]):
        raise GrantError(f"{label}: invalid card_scope_sha256")
    servers, command = raw["servers"], raw["command"]
    if (not isinstance(servers, list) or not servers or not all(isinstance(s, str) for s in servers)
            or not isinstance(command, str)):
        raise GrantError(f"{label}: invalid servers or command")
    if raw["action_sha256"] != binding_sha(task_id, servers, command):
        raise GrantError(f"{label}: action_sha256 does not match the command and servers")
    approved, expires = parse_time(raw["approved_at"], "approved_at"), parse_time(raw["expires_at"], "expires_at")
    if not timedelta(0) < expires - approved <= ACTION_TTL:
        raise GrantError(f"{label}: an action grant lives 15 minutes at most")
    used = parse_time(raw["used_at"], "used_at") if raw["used_at"] is not None else None
    if (raw["state"] == "active") != (used is None):
        raise GrantError(f"{label}: state and used_at disagree")
    return ActionGrant(raw["action_id"], proposal_id, task_id, raw["card_scope_sha256"], raw["action_sha256"],
                       tuple(servers), command, approved, expires, raw["state"], raw["decision_id"], used,
                       approved_by, signer, signature)


def binding_sha(task_id: str, servers: Iterable[str], command: str) -> str:
    """What the executor can recompute from the grant alone: task, ordered servers, exact command."""
    return hashlib.sha256(canonical({"command": command, "servers": list(servers), "task_id": task_id})).hexdigest()


class ActionStore:
    """Action grants beside the task grants: ``<root>/KD-n.action-<proposal id>.json``, the same journal and lock."""

    def __init__(self, root: str, owner_uid: Optional[int]):
        self.store = admin_grant.Store(root, owner_uid)

    def path(self, task_id: str, proposal_id: str) -> str:
        return os.path.join(self.store.root,
                            f"{admin_task.check_task_id(task_id)}.action-{check_proposal_id(proposal_id)}.json")

    def load(self, task_id: str, proposal_id: str) -> Optional[ActionGrant]:
        path = self.path(task_id, proposal_id)
        data = self.store.read(path, f"{task_id} action {proposal_id}")
        return None if data is None else parse_action_grant(data, task_id, proposal_id)

    def approve(self, card: Card, action: Action, *, now: datetime, via: str, expect_card_sha256: str,
                expect_action_sha256: str, approved_by: str = "owner", signer: str = "",
                signature: str = "") -> ActionGrant:
        admin_grant.check_approver(approved_by, card.task_id)
        admin_grant.check_signature(signer, signature, card.task_id)
        if action.task_id != card.task_id:
            raise GrantError(f"the action is for {action.task_id}, the card is {card.task_id}")
        admin_task.check_admin(card)
        if card.scope_sha256 != expect_card_sha256 or action.sha256 != expect_action_sha256:
            raise admin_grant.Conflict(f"{card.task_id}: the card or the action changed; reload the page")
        moment = admin_grant._second(now)
        grant = ActionGrant(str(uuid.uuid4()), action.proposal_id, card.task_id, card.scope_sha256,
                            binding_sha(card.task_id, action.servers, action.command), action.servers, action.command,
                            moment, moment + ACTION_TTL, "active", str(uuid.uuid4()), approved_by=approved_by,
                            signer=signer, signature=signature)
        return self._write("approve-action", grant, via, now)

    def consume(self, task_id: str, proposal_id: str, action_sha256: str, card: Card, now: datetime) -> ActionGrant:
        """Mark the action used before SSH; any mismatch leaves it untouched."""
        path = self.path(task_id, proposal_id)
        self.store._check_root()
        with atomic_file.locked(os.path.join(self.store.root, admin_grant.LOCK), 0o600):
            grant = self.load(task_id, proposal_id)
            if grant is None:
                raise GrantError(f"{task_id}: no owner's approval for action {proposal_id}")
            if grant.state != "active":
                raise GrantError(f"{task_id}: action {proposal_id} is {grant.state}; a new approval is needed")
            if grant.action_sha256 != action_sha256:
                raise GrantError(f"{task_id}: action {proposal_id} differs from the approved one")
            if now >= grant.expires_at:
                raise GrantError(f"{task_id}: the approval of action {proposal_id} expired at {fmt(grant.expires_at)}")
            if grant.approved_at - now > admin_grant.CLOCK_SKEW:
                raise GrantError(f"{task_id}: action approved in the future; check the clock")
            if card.task_id != task_id:
                raise GrantError(f"{task_id}: card {card.task_id} is another task")
            admin_task.check_admin(card)
            admin_task.check_active(card)
            if card.scope_sha256 != grant.card_scope_sha256:
                raise GrantError(f"{task_id}: the card's scope changed since the owner's approval")
            used = ActionGrant(**{**grant.__dict__, "state": "consumed", "used_at": admin_grant._second(now)})
            self.store.journal(self._record("consume-action", used, "exec", now))
            atomic_file.replace(path, _dump(used), 0o640)
        return used

    def revoke(self, task_id: str, proposal_id: str, *, now: datetime, via: str, by: str = "owner") -> ActionGrant:
        admin_grant.check_approver(by, task_id)
        self.store._check_root()
        with atomic_file.locked(os.path.join(self.store.root, admin_grant.LOCK), 0o600):
            grant = self.load(task_id, proposal_id)
            if grant is None:
                raise GrantError(f"{task_id}: no approval of action {proposal_id} to revoke")
            if grant.state != "active":
                return grant
            revoked = ActionGrant(**{**grant.__dict__, "state": "revoked", "used_at": admin_grant._second(now)})
            self.store.journal(self._record("revoke-action", revoked, via, now, by))
            atomic_file.replace(self.path(task_id, proposal_id), _dump(revoked), 0o640)
        return revoked

    def _write(self, decision: str, grant: ActionGrant, via: str, now: datetime) -> ActionGrant:
        self.store._check_root()
        with atomic_file.locked(os.path.join(self.store.root, admin_grant.LOCK), 0o600):
            self.store.journal(self._record(decision, grant, via, now))
            atomic_file.replace(self.path(grant.task_id, grant.proposal_id), _dump(grant), 0o640)
        return grant

    @staticmethod
    def _record(decision: str, grant: ActionGrant, via: str, now: datetime, by: Optional[str] = None) -> dict:
        return {"ts": fmt(now), "decision_id": grant.decision_id if decision == "approve-action" else str(uuid.uuid4()),
                "action": decision, "task_id": grant.task_id, "action_id": grant.action_id,
                "proposal_id": grant.proposal_id, "action_sha256": grant.action_sha256,
                "card_scope_sha256": grant.card_scope_sha256, "servers": list(grant.servers),
                "expires_at": fmt(grant.expires_at),
                "by": "exec" if decision == "consume-action" else by or grant.approved_by, "via": via}


def _dump(grant: ActionGrant) -> bytes:
    return json.dumps(grant.to_dict(), ensure_ascii=False, indent=1).encode("utf-8")
