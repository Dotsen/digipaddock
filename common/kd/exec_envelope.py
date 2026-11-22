"""The exec envelope: what the factory host sends to a target's exec gate with every step of an approved
plan or with a critical command, and what the gate verifies before anything runs.

The owner signs a server plan or a critical command in the cabinet with a passkey. The grant keeps the browser's
assertion, the id of the cabinet's decision it approved and the *binding* the challenge was built from: the term,
the nonce, the cards' revisions the page showed and, for a batch signed at once, the hex digest of every decision in
it. The decision's id is the cabinet's: the grant's own ``decision_id`` names its journal record, and a
digest over it is not the one the owner signed. An envelope carries the decision's payload
(what the cabinet digested), the signed proposal, those binding values, the assertion and the exact command of one
step. The target recomputes the decision digest from the payload, the challenge from the digests and the nonce,
checks the assertion with the owner's public key from its own registry (``kd-cabinet user signers``) and runs the
command only when it is the step's text — a host that holds the exec key but not the owner's signature can send
nothing a target runs.

``enabled`` is the host-wide switch: the presence file the installer writes once every target carries the gate.
Nothing here does I/O besides that; the executors (``kd-admin-run``, ``kd-admin-critical``) build the envelope and
``ssh-exec`` feeds it to ``GATE_RUN`` on the target's stdin.
"""

import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple

from kd import admin_grant
from kd.admin_action import binding_sha
from kd.admin_task import canonical

SCHEMA_VERSION = 1
MAX_BYTES = 128 * 1024
GATE_RUN = "agent-exec-gate run"
SNAPSHOT_RUN = "agent-exec-gate snapshot"  # + " <16-hex nonce>": the plan window's read-only snapshot by the gate
FLAG = "/etc/digipaddock/exec-envelopes"
_NONCE = re.compile(r"[0-9a-f]{32}")
_DECISION = re.compile(r"[0-9a-f]{32}")  # the cabinet's decision id (kd.cabinet.api.DECISION_ID)
_HEX = re.compile(r"[0-9a-f]{64}")
_RESPONSE = ("clientDataJSON", "authenticatorData", "signature")


def enabled(path: str = FLAG) -> bool:
    """Envelopes are sent from this host: the installer's presence file exists."""
    return os.path.exists(path)


class EnvelopeError(ValueError):
    """No envelope: the step does not run (the stored proposal is not the signed one, the step is out of range,
    the envelope would not fit the gate's limit)."""


@dataclass(frozen=True)
class Decision:
    """What the grant knows of the owner's decision, as the target recomputes it: ``id`` is the cabinet's decision
    the signature names, "" when it names none; ``assertion`` is None for a grant the target cannot verify (TOTP,
    unsigned, written before the signature named its decision or the binding carried revisions)."""

    id: str
    user: str
    expires: str
    nonce: str
    revisions: dict
    digests: Tuple[str, ...]
    assertion: Optional[dict]


def _user_of(approved_by: str) -> str:
    return approved_by[len("user:"):] if approved_by.startswith("user:") else approved_by


def _assertion_of(data: Mapping) -> Optional[dict]:
    response = data.get("response")
    if data.get("type") != "public-key" or not isinstance(data.get("id"), str) or not isinstance(response, Mapping):
        return None
    if not all(isinstance(response.get(key), str) for key in _RESPONSE):
        return None
    raw_id = data.get("rawId") if isinstance(data.get("rawId"), str) else data["id"]
    return {"id": data["id"], "rawId": raw_id, "type": "public-key",
            "response": {key: response[key] for key in _RESPONSE}}


def decision_of(grant) -> Decision:
    """The decision of a ``Grant`` or ``ActionGrant`` from its stored signature; total — a signature the target
    could not verify gives ``assertion=None``, never an exception."""
    user = _user_of(grant.approved_by)
    try:
        data = json.loads(grant.signature) if grant.signature else {}
    except ValueError:
        data = {}
    if not isinstance(data, Mapping):
        data = {}
    binding = data.get("binding")
    if not isinstance(binding, Mapping):
        binding = {}
    expires = binding.get("expires") if isinstance(binding.get("expires"), str) else ""
    nonce = binding.get("nonce") if isinstance(binding.get("nonce"), str) and _NONCE.fullmatch(binding["nonce"]) else ""
    revisions = dict(binding["revisions"]) if isinstance(binding.get("revisions"), Mapping) else None
    raw_digests = binding.get("digests")
    digests = (tuple(raw_digests)
               if isinstance(raw_digests, list) and all(isinstance(d, str) and _HEX.fullmatch(d) for d in raw_digests)
               else ())
    named = data.get("decision")
    decision_id = named if isinstance(named, str) and _DECISION.fullmatch(named) else ""
    assertion = _assertion_of(data) if decision_id and revisions is not None and nonce and expires else None
    return Decision(decision_id, user, expires, nonce, revisions or {}, digests, assertion)


def _envelope(kind: str, grant, payload: dict, proposal: Mapping, server: str, command: str, **extra: Any) -> bytes:
    decision = decision_of(grant)
    about = {"id": decision.id, "user": decision.user, "expires": decision.expires, "nonce": decision.nonce}
    if grant.task_id in decision.revisions:
        about["revision"] = decision.revisions[grant.task_id]
    if len(decision.digests) > 1:
        about["digests"] = list(decision.digests)
    grant_part = {"id": extra.pop("grant_id"), "approved_at": admin_grant.fmt(grant.approved_at),
                  "expires_at": admin_grant.fmt(grant.expires_at)}
    grant_part.update(extra.pop("grant_extra", {}))
    envelope = {"schema_version": SCHEMA_VERSION, "kind": kind, "task_id": grant.task_id, "server": server,
                "command": command, "payload": payload, "proposal": dict(proposal), "grant": grant_part,
                "decision": about, "assertion": decision.assertion}
    envelope.update(extra)
    raw = canonical(envelope)
    if len(raw) > MAX_BYTES:
        raise EnvelopeError(f"{grant.task_id}: the envelope is {len(raw)} bytes, the gate takes {MAX_BYTES} at most")
    return raw


def build_plan(grant, proposal: Mapping, step: int, server: str, command: str) -> bytes:
    """The envelope of step ``step`` (1-based) of the plan the owner signed as ``grant``; ``proposal`` is the stored
    plan's ``as_dict()`` and must hash to the grant's ``proposal_sha256``."""
    if hashlib.sha256(canonical(proposal)).hexdigest() != grant.proposal_sha256:
        raise EnvelopeError(f"{grant.task_id}: the stored proposal is not the one the owner signed")
    operations = proposal.get("operations") if isinstance(proposal, Mapping) else None
    count = len(operations) if isinstance(operations, list) else 0
    if isinstance(step, bool) or not isinstance(step, int) or not 1 <= step <= count:
        raise EnvelopeError(f"{grant.task_id}: step {step!r} is out of range 1..{count}")
    payload = {"task_id": grant.task_id, "proposal_sha256": grant.proposal_sha256,
               "card_scope_sha256": grant.card_scope_sha256}
    return _envelope("plan", grant, payload, proposal, server, command, step=step, grant_id=grant.grant_id)


def build_action(grant, action: Mapping, server: str, command: str) -> bytes:
    """The envelope of the critical command the owner signed as ``grant`` (an ``ActionGrant``); ``action`` is the
    stored action's ``as_dict()``. The grant binds the task, the servers and the exact command (``binding_sha``),
    the decision payload named the action by its hash."""
    action_sha = hashlib.sha256(canonical(action)).hexdigest()
    servers = action.get("servers") if isinstance(action.get("servers"), list) else []
    if (action_sha[:16] != grant.proposal_id
            or binding_sha(grant.task_id, servers, action.get("command")) != grant.action_sha256):
        raise EnvelopeError(f"{grant.task_id}: the stored action is not the one the owner signed")
    if command != action.get("command"):
        raise EnvelopeError(f"{grant.task_id}: the command is not the signed action's command")
    if server not in servers:
        raise EnvelopeError(f"{grant.task_id}: the signed action names no server {server}")
    payload = {"task_id": grant.task_id, "proposal_id": grant.proposal_id, "proposal_sha256": action_sha,
               "card_scope_sha256": grant.card_scope_sha256}
    return _envelope("action", grant, payload, action, server, command, grant_id=grant.action_id,
                     grant_extra={"action_id": grant.action_id})
