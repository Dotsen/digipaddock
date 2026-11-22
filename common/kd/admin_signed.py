"""the text the owner signed, kept beside its grant for the executor.

``kd-admin-run`` and ``kd-admin-critical`` run as the exec user, which reads the grant store
(``admin-grants``, <cabinet>:<exec> 2750) and must never read the agent's proposals (``admin-proposals``,
<agent>:<cabinet> 2750). So the cabinet, when the owner approves, first writes the exact plan or action he
signed into the grant store — ``KD-n.signed-plan-<sha256>.json`` and ``KD-n.signed-action-<id>.json``, in
the proposals' stored form — and only then the grant: a grant without its copy refuses, it never runs. The
executor reads the copy as it reads a grant (no links, the cabinet's file, not writable by others) and checks
it against the grant's hash. ``backfill`` gives the live grants approved before this change their copies,
from the proposals the cabinet can read, when the hash matches.
"""

import hashlib
import json
import os
import re
from datetime import datetime
from typing import List, Optional

from kd import admin_action, admin_grant, admin_proposal, admin_task, atomic_file, ids

MODE = 0o640
_GRANT = re.compile(rf"({ids.CARD.pattern})\.json")
_ACTION_GRANT = re.compile(rf"({ids.CARD.pattern})\.action-([0-9a-f]{{16}})\.json")


class SignedError(ValueError):
    """The signed copy is missing or is not the text the grant names: nothing runs."""


def plan_path(root: str, task_id: str, sha256: str) -> str:
    if not isinstance(sha256, str) or not admin_grant._SHA.fullmatch(sha256):
        raise SignedError(f"{task_id}: a plan hash must be 64 hex digits")
    return os.path.join(
        root, f"{admin_task.check_task_id(task_id)}.signed-plan-{sha256}.json"
    )


def action_path(root: str, task_id: str, proposal_id: str) -> str:
    try:
        admin_action.check_proposal_id(proposal_id)
    except admin_grant.GrantError as exc:
        raise SignedError(str(exc)) from exc
    return os.path.join(
        root, f"{admin_task.check_task_id(task_id)}.signed-action-{proposal_id}.json"
    )


def digest(data: dict) -> str:
    """The hash a plan's grant names, and the action id is the head of (``Proposal.sha256``, ``Action.sha256``)."""
    return hashlib.sha256(admin_task.canonical(data)).hexdigest()


def write_plan(
    root: str, stored: admin_proposal.Stored, plan: dict, sha256: str
) -> str:
    """Keep ``plan`` — the validated dict whose hash is ``sha256`` — in the grant store; before the grant."""
    if digest(plan) != sha256:
        raise SignedError(f"{stored.task_id}: the plan to keep is not the one approved")
    return _write(root, plan_path(root, stored.task_id, sha256), stored, plan)


def write_action(
    root: str, stored: admin_proposal.Stored, action: dict, proposal_id: str
) -> str:
    if digest(action)[:16] != proposal_id:
        raise SignedError(
            f"{stored.task_id}: the action to keep is not action {proposal_id}"
        )
    return _write(root, action_path(root, stored.task_id, proposal_id), stored, action)


def _write(root: str, path: str, stored: admin_proposal.Stored, data: dict) -> str:
    admin_grant.Store(root, None)._check_root()
    body = admin_proposal.dump(
        stored.task_id, stored.proposed_by, stored.proposed_at, data
    )
    with atomic_file.locked(os.path.join(root, admin_grant.LOCK), 0o600):
        atomic_file.replace(path, body, MODE)
    return path


def load_plan(
    root: str, task_id: str, sha256: str, owner_uid: Optional[int]
) -> admin_proposal.Stored:
    """The signed plan of the grant naming ``sha256``; SignedError (or GrantError) when there is none to trust."""
    stored = _read(root, plan_path(root, task_id, sha256), task_id, owner_uid, "plan")
    if digest(stored.data) != sha256:
        raise SignedError(
            f"{task_id}: the kept plan does not match the grant's hash; a new approval is needed"
        )
    return stored


def load_action(
    root: str, task_id: str, proposal_id: str, owner_uid: Optional[int]
) -> admin_proposal.Stored:
    stored = _read(
        root,
        action_path(root, task_id, proposal_id),
        task_id,
        owner_uid,
        f"action {proposal_id}",
    )
    if digest(stored.data)[:16] != proposal_id:
        raise SignedError(
            f"{task_id}: the kept action is not action {proposal_id}; a new approval is needed"
        )
    return stored


def _read(
    root: str, path: str, task_id: str, owner_uid: Optional[int], what: str
) -> admin_proposal.Stored:
    raw = admin_grant.Store(root, owner_uid).read(path, f"{task_id} signed {what}")
    if raw is None:
        raise SignedError(
            f"{task_id}: the signed {what} was not kept at the owner's approval; a new approval is needed"
        )
    try:
        return admin_proposal.parse(raw, task_id)
    except admin_proposal.ProposalStoreError as exc:
        raise SignedError(f"{task_id}: the kept {what} is unreadable ({exc})") from exc


def backfill(
    grants: str,
    proposals: str,
    *,
    grant_uid: Optional[int],
    agent_uid: Optional[int],
    now: datetime,
) -> List[str]:
    """Copies for the live grants approved before the change, from the agent's proposals when the hash matches; one
    line per grant that had none. A grant left without a copy refuses its steps (a new approval is needed)."""
    store = admin_grant.Store(grants, grant_uid)
    actions = admin_action.ActionStore(grants, grant_uid)
    store._check_root()
    lines = []
    for name in sorted(os.listdir(grants)):
        plan, action = _GRANT.fullmatch(name), _ACTION_GRANT.fullmatch(name)
        try:
            if plan:
                lines += _backfill_plan(store, proposals, plan.group(1), agent_uid, now)
            elif action:
                lines += _backfill_action(
                    actions, proposals, action.group(1), action.group(2), agent_uid, now
                )
        except (
            ValueError,
            OSError,
        ) as exc:  # store, proposal, card id or copy errors: this grant only
            lines.append(f"{name}: no copy ({exc})")
    return lines


def _backfill_plan(store, proposals, task_id, agent_uid, now) -> List[str]:
    raw = store.read(store.path(task_id), task_id)
    if raw is None or _policy(raw) != admin_grant.SERVER:
        return []  # a factory grant: no steps of kd-admin-run
    grant = admin_grant.parse_grant(raw, task_id)
    if grant is None or grant.state != "active" or now >= grant.expires_at:
        return []
    path = plan_path(store.root, task_id, grant.proposal_sha256)
    if os.path.lexists(path):
        return []
    stored = admin_proposal.load_plan(
        proposals, task_id, grant.proposal_sha256, agent_uid
    )
    if stored is None or digest(stored.data) != grant.proposal_sha256:
        return [
            f"{task_id}: no plan with the grant's hash; its steps need a new approval"
        ]
    write_plan(store.root, stored, stored.data, grant.proposal_sha256)
    return [f"{task_id}: signed plan kept"]


def _backfill_action(
    actions, proposals, task_id, proposal_id, agent_uid, now
) -> List[str]:
    grant = actions.load(task_id, proposal_id)
    if grant is None or grant.state != "active" or now >= grant.expires_at:
        return []
    root = actions.store.root
    if os.path.lexists(action_path(root, task_id, proposal_id)):
        return []
    stored = admin_proposal.load_action(proposals, task_id, proposal_id, agent_uid)
    if (
        stored is None
        or digest(stored.data)[:16] != proposal_id
        or not _binds(stored.data, grant)
    ):
        return [
            f"{task_id}: no action {proposal_id} matching the approval; it needs a new approval"
        ]
    write_action(root, stored, stored.data, proposal_id)
    return [f"{task_id}: signed action {proposal_id} kept"]


def _policy(raw: bytes) -> Optional[str]:
    """The policy of a stored grant from its own (assignee, project); a broken file reads as the sysadmin's, so
    ``parse_grant`` says what is wrong with it."""
    try:
        data = json.loads(raw.decode("utf-8"))
        return admin_grant.policy_of(data["assignee"], data["project"])
    except (UnicodeDecodeError, ValueError, TypeError, KeyError):
        return admin_grant.SERVER


def _binds(data: dict, grant: admin_action.ActionGrant) -> bool:
    servers, command = data.get("servers"), data.get("command")
    if not isinstance(servers, list) or not isinstance(command, str):
        return False
    return (
        admin_action.binding_sha(grant.task_id, servers, command) == grant.action_sha256
    )
