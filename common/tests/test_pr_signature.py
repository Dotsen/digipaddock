"""the one formula of the value the owner signs to approve a pull request (the cabinet's web and kd-review)."""

import hashlib

import pytest

from kd import pr_signature
from kd.cabinet import decisions
from cabinet_env import CabinetEnv

PAYLOAD = {"kind": "pr_approve", "repo": "owner/repo", "project": "digipaddock", "number": 12, "head": "a" * 40,
           "url": "https://forge.example/owner/repo/pulls/12", "title": "KD-5: x", "release": True}
NONCE = "00" * 16


def test_digest_is_the_cabinets(tmp_path):
    env = CabinetEnv(tmp_path)
    card = env.card(assignee="@dev", status="Review")
    ledger = decisions.Decisions(env.store)
    opened = ledger.open("pr_approve", card=card.id, payload=PAYLOAD, requested_by="agent:dev", now=env.now)
    ours = pr_signature.digest(opened["id"], opened["payload"], 3, "owner", "2026-10-19T10:05:00Z")
    assert ours == ledger.digest(opened, user="owner", expires="2026-10-19T10:05:00Z", card_revision=3)
    assert pr_signature.payload_sha(PAYLOAD) == opened["payload_sha"]


def test_mode_and_nonce_change_the_challenge():
    args = ("d" * 32, PAYLOAD, 3, "owner", "2026-10-19T10:05:00Z")
    values = {pr_signature.challenge(*args, NONCE, mode) for mode in pr_signature.MODES}
    assert len(values) == 3
    assert pr_signature.challenge(*args, "11" * 16, "merge") != pr_signature.challenge(*args, NONCE, "merge")
    expected = hashlib.sha256(pr_signature.digest(*args) + bytes(16) + b"|pr_approve|merge").digest()
    assert pr_signature.challenge(*args, NONCE, "merge") == expected
    # the payload itself is bound: another head is another value
    other = dict(PAYLOAD, head="b" * 40)
    assert pr_signature.challenge("d" * 32, other, 3, "owner", "2026-10-19T10:05:00Z", NONCE, "merge") != expected


@pytest.mark.parametrize("nonce, mode", [(NONCE, "deploy"), (NONCE, ""), ("zz" * 16, "merge"), ("00", "merge"),
                                         (None, "merge"), (NONCE, None)])
def test_bad_mode_or_nonce_is_refused(nonce, mode):
    with pytest.raises(ValueError):
        pr_signature.challenge("d" * 32, PAYLOAD, 3, "owner", "2026-10-19T10:05:00Z", nonce, mode)
