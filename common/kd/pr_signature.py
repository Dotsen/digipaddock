"""The value a person signs to approve a pull request.

One formula for both sides: the cabinet's web asks the passkey to sign it, and kd-review recomputes it from
the decision it is shown before it posts an Approve to Forgejo. The digest is the cabinet's own
(``Decisions.digest``: decision id, payload hash, card revision, person, term); a PR approval adds the nonce
of the challenge and the mode the person chose (approve, merge, release), so a signature for «approve» does
not pass as «merge and release». Standard library only: kd-review imports it without the cabinet's store.
"""

import hashlib
import json
import re

MODES = ("approve", "merge", "release")
_NONCE = re.compile(r"[0-9a-f]{32}")


def canonical(value) -> bytes:
    """The cabinet's canonical JSON (``decisions.canonical``)."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def payload_sha(payload) -> str:
    return hashlib.sha256(canonical(payload)).hexdigest()


def digest(decision_id: str, payload, card_revision: int, user: str, expires: str) -> bytes:
    text = f"{decision_id}|{payload_sha(payload)}|{card_revision}|{user}|{expires}"
    return hashlib.sha256(text.encode("utf-8")).digest()


def challenge(decision_id: str, payload, card_revision: int, user: str, expires: str, nonce_hex: str,
              mode: str) -> bytes:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    if not isinstance(nonce_hex, str) or not _NONCE.fullmatch(nonce_hex):
        raise ValueError("the nonce must be 32 hex characters")
    bound = digest(decision_id, payload, card_revision, user, expires)
    return hashlib.sha256(bound + bytes.fromhex(nonce_hex) + b"|pr_approve|" + mode.encode("ascii")).digest()
