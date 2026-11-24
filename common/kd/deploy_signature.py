"""The value the owner signs to start a prod deploy from the cabinet (``deploy_prod``, KD-9443).

One formula for both sides, like ``pr_signature``: the cabinet's web asks the passkey to sign it, and kd-review
recomputes it from the launch it is shown before it calls ``workflow_dispatch``. It binds the launch's id, the
payload's hash (project, repository, environment, workflow, branch, the branch head the owner saw, ``skip_tests``),
the person, the term and a fresh nonce — a signature for one commit, project or test choice does not start another.
This is the one signature of the cabinet given without user verification (the owner's choice of 24.11.2026: the
saved passkey alone, no fingerprint or PIN), so its term is short (``TERM_SECONDS``) and kd-review spends its nonce.
Standard library only: kd-review imports it without the cabinet's store.
"""

import hashlib
import re

from kd import pr_signature

TYPE = "deploy_prod"
TERM_SECONDS = (
    600  # ten minutes from the challenge: the brief's term of a deploy_prod signature
)
CONFIRM = "deploy"  # the word the owner types; a workflow's confirm input gets it
FIELDS = ("kind", "project", "repo", "env", "workflow", "branch", "sha", "skip_tests")
_ID = re.compile(r"[0-9a-f]{32}")
_SHA = re.compile(r"[0-9a-f]{40}")


def valid_payload(payload) -> bool:
    return (
        isinstance(payload, dict)
        and set(payload) == set(FIELDS)
        and payload["kind"] == TYPE
        and all(isinstance(payload[k], str) and payload[k] for k in FIELDS[1:7])
        and bool(_SHA.fullmatch(payload["sha"]))
        and isinstance(payload["skip_tests"], bool)
    )


def challenge(
    launch_id: str, payload, user: str, expires: str, nonce_hex: str
) -> bytes:
    if not isinstance(launch_id, str) or not _ID.fullmatch(launch_id):
        raise ValueError("the launch id must be 32 hex characters")
    if not isinstance(nonce_hex, str) or not _ID.fullmatch(nonce_hex):
        raise ValueError("the nonce must be 32 hex characters")
    if not valid_payload(payload):
        raise ValueError(f"the payload must be {{{', '.join(FIELDS)}}} of a {TYPE}")
    text = f"{TYPE}|{launch_id}|{pr_signature.payload_sha(payload)}|{user}|{expires}"
    return hashlib.sha256(
        text.encode("utf-8") + b"|" + bytes.fromhex(nonce_hex)
    ).digest()
