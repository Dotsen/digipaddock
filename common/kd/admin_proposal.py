"""The agent's proposals, untrusted data kept apart from the grants.

``kd-admin-propose`` (run as the agent) validates a proposal against the card, the catalog and
the enabled inventory, then replaces ``<root>/KD-n.json`` atomically. The file only records what
the agent asked for and who asked: the approval service validates it again against the current
card and catalog before showing it, and only the owner's POST turns it into a grant.
"""

import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

from kd import admin_operations as ops
from kd import admin_task, atomic_file
from kd.admin_grant import fmt, parse_time, reader

ROOT = "/var/lib/digipaddock/admin-proposals"
SCHEMA_VERSION = 1
MAX_BYTES = 64 * 1024
OPEN = ("Inbox",) + admin_task.ACTIVE  # a card the owner may still approve
_KEYS = frozenset({"schema_version", "task_id", "proposed_by", "proposed_at", "proposal"})
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


class ProposalStoreError(ValueError):
    """The stored proposal cannot be trusted to be what the agent submitted."""


@dataclass(frozen=True)
class Stored:
    task_id: str
    proposed_by: str
    proposed_at: datetime
    data: dict  # the proposal as submitted; validate it again before use


def loads(data: bytes, what: str) -> object:
    """Strict JSON: bounded UTF-8, no duplicate keys, no NaN or Infinity."""
    if len(data) > MAX_BYTES:
        raise ops.ProposalError(f"{what} is larger than 64 KiB")

    def unique(pairs):
        keys = [key for key, _ in pairs]
        if len(keys) != len(set(keys)):
            raise ops.ProposalError(f"{what}: duplicate key")
        return dict(pairs)

    def constant(name):
        raise ops.ProposalError(f"{what}: {name} is not JSON")

    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=unique, parse_constant=constant)
    except ops.ProposalError:
        raise
    except (UnicodeDecodeError, ValueError) as exc:
        raise ops.ProposalError(f"{what} is not JSON") from exc


def submit(root: str, raw: bytes, *, agent: str, card: admin_task.Card, catalog: ops.Catalog,
           inventory: Iterable[str], enabled: Iterable[str], now: datetime, home: Optional[str] = None,
           deny=None, critical=None) -> ops.Proposal:
    """Validate ``raw`` for ``card`` and store it as the card's current proposal; ``deny`` and ``critical`` read its
    script steps (see ``ops.validate_proposal``)."""
    data = loads(raw, "proposal")
    if not isinstance(data, dict):
        raise ops.ProposalError("proposal must be a JSON object")
    admin_task.check_admin(card)
    if agent != admin_task.ASSIGNEE:
        raise ops.ProposalError(f"only the card assignee @{admin_task.ASSIGNEE} may propose, not {agent}")
    if card.status not in OPEN:
        raise ops.ProposalError(f"{card.task_id}: status {card.status} takes no proposal")
    proposal = ops.validate_proposal(data, catalog, inventory, enabled, card_servers=card.servers, home=home,
                                     deny=deny, critical=critical)
    if proposal.task_id != card.task_id:
        raise ops.ProposalError(f"the proposal is for {proposal.task_id}, the card is {card.task_id}")
    _check_root(root, None)
    body = dump(card.task_id, agent, now, proposal.as_dict())
    # the plan is also kept under its hash — a later proposal replaces the card's current one, but the
    # steps of a live grant still need the text the owner signed (kd-admin-run builds the envelope from it)
    signed = plan_path(root, card.task_id, proposal.sha256)
    with atomic_file.locked(signed, 0o640):
        atomic_file.replace(signed, body, 0o640)
    path = _path(root, card.task_id)
    with atomic_file.locked(path, 0o640):
        atomic_file.replace(path, body, 0o640)
    return proposal


def plan_path(root: str, task_id: str, sha256: str) -> str:
    if not isinstance(sha256, str) or len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        raise ProposalStoreError(f"{task_id}: a plan hash must be 64 hex digits")
    return os.path.join(root, f"{admin_task.check_task_id(task_id)}.plan-{sha256}.json")


def load_plan(root: str, task_id: str, sha256: str, owner_uid: Optional[int] = None) -> Optional[Stored]:
    """The card's plan as stored under ``sha256`` — the hash a grant names; a plan stored before the change is only
    the card's current one, so that is the fallback. None when neither is there; the caller compares the hash."""
    stored = _load(root, plan_path(root, task_id, sha256), task_id, owner_uid)
    return stored if stored is not None else load(root, task_id, owner_uid)


def submit_action(root: str, raw: bytes, *, agent: str, card: admin_task.Card, inventory: Iterable[str],
                  enabled: Iterable[str], now: datetime, deny) -> "admin_action.Action":
    """Validate a critical action (the denylist included) and store it beside the card's plan."""
    from kd import admin_action

    data = loads(raw, "action")
    admin_task.check_admin(card)
    if agent != admin_task.ASSIGNEE:
        raise ops.ProposalError(f"only the card assignee @{admin_task.ASSIGNEE} may propose, not {agent}")
    if card.status not in OPEN:
        raise ops.ProposalError(f"{card.task_id}: status {card.status} takes no proposal")
    action = admin_action.validate_action(data, inventory, enabled, card_servers=card.servers, deny=deny)
    if action.task_id != card.task_id:
        raise ops.ProposalError(f"the action is for {action.task_id}, the card is {card.task_id}")
    _check_root(root, None)
    path = action_path(root, card.task_id, action.proposal_id)
    with atomic_file.locked(path, 0o640):
        atomic_file.replace(path, dump(card.task_id, agent, now, action.as_dict()), 0o640)
    return action


def dump(task_id: str, proposed_by: str, proposed_at: datetime, data: dict) -> bytes:
    """The stored form of a plan or an action: what ``parse`` reads back (the grant store's signed copy too)."""
    envelope = {"schema_version": SCHEMA_VERSION, "task_id": task_id, "proposed_by": proposed_by,
                "proposed_at": fmt(proposed_at), "proposal": data}
    return json.dumps(envelope, ensure_ascii=False, indent=1).encode("utf-8")


def action_path(root: str, task_id: str, proposal_id: str) -> str:
    from kd import admin_action

    try:
        admin_action.check_proposal_id(proposal_id)
    except ValueError as exc:
        raise ProposalStoreError(str(exc)) from exc
    return os.path.join(root, f"{admin_task.check_task_id(task_id)}.action-{proposal_id}.json")


def action_ids(root: str, task_id: str) -> list:
    """Proposal ids of the card's stored critical actions, sorted."""
    prefix = admin_task.check_task_id(task_id) + ".action-"
    try:
        names = os.listdir(root)
    except OSError as exc:
        raise ProposalStoreError(f"proposal directory {root} cannot be listed") from exc
    found = [name[len(prefix):-5] for name in names if name.startswith(prefix) and name.endswith(".json")]
    return sorted(pid for pid in found if len(pid) == 16 and all(c in "0123456789abcdef" for c in pid))


def load_action(root: str, task_id: str, proposal_id: str, owner_uid: Optional[int] = None) -> Optional[Stored]:
    """A stored critical action of the card, None when there is none."""
    return _load(root, action_path(root, task_id, proposal_id), task_id, owner_uid)


def load(root: str, task_id: str, owner_uid: Optional[int] = None) -> Optional[Stored]:
    """The card's stored proposal, None when there is none; ``owner_uid`` is the agent's uid."""
    return _load(root, _path(root, task_id), task_id, owner_uid)


def _load(root: str, path: str, task_id: str, owner_uid: Optional[int]) -> Optional[Stored]:
    _check_root(root, owner_uid)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except PermissionError as exc:
        raise ProposalStoreError(f"{task_id}: the proposal store {root} is not readable by {reader()}") from exc
    except OSError as exc:
        raise ProposalStoreError(f"{task_id}: proposal cannot be read") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ProposalStoreError(f"{task_id}: proposal is not a regular file")
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW | getattr(os, "O_NONBLOCK", 0))  # a FIFO must not block
    except PermissionError as exc:
        raise ProposalStoreError(f"{task_id}: {path} is not readable by {reader()}") from exc
    except OSError as exc:
        raise ProposalStoreError(f"{task_id}: proposal cannot be opened") from exc
    with os.fdopen(fd, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise ProposalStoreError(f"{task_id}: proposal is not a regular file")
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ProposalStoreError(f"{task_id}: proposal changed while it was opened")
        if owner_uid is not None and opened.st_uid != owner_uid:
            raise ProposalStoreError(f"{task_id}: proposal has the wrong owner")
        raw = handle.read(MAX_BYTES + 1)
    return parse(raw, task_id)


def parse(raw: bytes, task_id: str) -> Stored:
    """A stored plan or action of ``task_id`` from its bytes; ProposalStoreError when it is not one."""
    try:
        data = loads(raw, "stored proposal")
    except ops.ProposalError as exc:
        raise ProposalStoreError(f"{task_id}: {exc}") from exc
    if not isinstance(data, dict) or set(data) != _KEYS:
        raise ProposalStoreError(f"{task_id}: stored proposal keys do not match schema v{SCHEMA_VERSION}")
    if data["schema_version"] != SCHEMA_VERSION:
        raise ProposalStoreError(f"{task_id}: unknown stored proposal schema_version")
    if data["task_id"] != task_id:
        raise ProposalStoreError(f"{task_id}: stored proposal names task {data['task_id']!r}")
    if data["proposed_by"] != admin_task.ASSIGNEE:
        raise ProposalStoreError(f"{task_id}: proposed_by must be {admin_task.ASSIGNEE}")
    try:
        proposed_at = parse_time(data["proposed_at"], "proposed_at")
    except ValueError as exc:
        raise ProposalStoreError(f"{task_id}: invalid proposed_at") from exc
    if not isinstance(data["proposal"], dict):
        raise ProposalStoreError(f"{task_id}: proposal must be an object")
    return Stored(task_id, data["proposed_by"], proposed_at, data["proposal"])


def _path(root: str, task_id: str) -> str:
    return os.path.join(root, admin_task.check_task_id(task_id) + ".json")


def _check_root(root: str, owner_uid: Optional[int]) -> None:
    try:
        info = os.lstat(root)
    except OSError as exc:
        raise ProposalStoreError(f"proposal directory {root} is missing") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise ProposalStoreError(f"proposal directory {root} is not a directory")
    if owner_uid is not None and info.st_uid != owner_uid:
        raise ProposalStoreError(f"proposal directory {root} has the wrong owner")
    if os.name == "posix" and info.st_mode & 0o022:
        raise ProposalStoreError(f"proposal directory {root} is writable by others")
