"""the whole path of the owner's signature — the cabinet's challenge, the passkey's assertion, the grant
the approval writes, the envelope an executor builds from that grant and the target's gate, which recomputes the
challenge from the envelope alone. Each side has its own tests; these hold the seam between them: a week of
``observe`` showed every signed step failing the gate, because the envelope named the grant's journal id where
the cabinet had digested its decision's id."""

import copy
import json
import sys
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

sys.path.insert(
    0, str(Path(__file__).resolve().parents[3] / "common" / "tests")
)  # web_env, softauth, admin_samples

from admin_samples import (
    CATALOG,
    ENABLED,
    INVENTORY,
    action as action_data,
    explain,
    proposal,
)  # noqa: E402
from softauth import SoftAuthenticator  # noqa: E402
from web_env import WebEnv  # noqa: E402

from gate import agent_exec_gate as gate  # noqa: E402
from kd import admin_action, admin_proposal, admin_signed, exec_envelope  # noqa: E402
from kd import admin_operations as ops  # noqa: E402
from kd.cabinet import client as cabinet_client  # noqa: E402

SCRIPT = "set -e\nnginx -t\nsystemctl reload nginx.service\n"
SCRIPT_CATALOG = {**copy.deepcopy(CATALOG), "verbs": [*CATALOG["verbs"], "script"]}


@pytest.fixture(autouse=True)
def policy_file(monkeypatch):
    """The gate loads the policy installed next to it; here — the repository's kd.exec_policy."""
    monkeypatch.setattr(
        gate,
        "POLICY_PATH",
        str(Path(gate.__file__).resolve().parents[3] / "common/kd/exec_policy.py"),
    )
    monkeypatch.setattr(gate, "_POLICY", {})


@pytest.fixture
def w(tmp_path):
    w = WebEnv(tmp_path)
    w.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    w.env.admin.catalog = lambda: ops.Catalog.from_dict(copy.deepcopy(SCRIPT_CATALOG))
    w.login()
    w.auth = SoftAuthenticator(w.cfg.rp_id, w.cfg.origin)
    options = w.client.post("/api/me/passkeys/options", headers=w.headers()).json()[
        "options"
    ]
    r = w.client.post(
        "/api/me/passkeys",
        headers=w.headers(),
        json={"credential": w.auth.register(options), "name": "Ключ подписи"},
    )
    assert r.status_code == 200, r.text
    return w


def crypto_verify(pem: str, data: bytes, signature: bytes) -> bool:
    try:
        serialization.load_pem_public_key(pem.encode("ascii")).verify(
            signature, data, ec.ECDSA(hashes.SHA256())
        )
    except InvalidSignature:
        return False
    return True


def registry(w):
    """The targets' registry of the owner's signing keys, as ``kd-cabinet user signers`` prints it."""
    keys = w.env.call("root", {"op": "signer.keys", "user": "owner"})["keys"]
    return {
        "schema_version": 1,
        "rp_id": w.cfg.rp_id,
        "origin": w.cfg.origin,
        "signers": [
            {
                "user": "owner",
                "credential_id": k["credential_id"],
                "public_key": k["public_key"],
                "name": k["name"],
            }
            for k in keys
        ],
    }


def verify(w, envelope: bytes, alias="node-c"):
    cfg = {
        "alias": alias,
        "rp_id": w.cfg.rp_id,
        "origin": w.cfg.origin,
        "mode": "enforce",
    }
    return gate.verify(
        json.loads(envelope),
        cfg,
        registry(w),
        w.env.epoch() + 60,
        verify_sig=crypto_verify,
    )


def sign(w, ids):
    """The owner signs the decisions on the page: one challenge, one assertion."""
    ch = w.client.post(
        "/api/decisions/challenge", headers=w.headers(), json={"ids": ids}
    )
    assert ch.status_code == 200, ch.text
    ch = ch.json()
    r = w.client.post(
        "/api/decisions/approve",
        headers=w.headers(),
        json={
            "challenge_id": ch["challenge_id"],
            "credential": w.auth.assert_(ch["options"]),
        },
    )
    assert r.status_code == 200, r.text


def card(w, task_id, status="Inbox"):
    w.env.put(
        task_id,
        project="admin",
        assignee="@sysadmin",
        status=status,
        servers=["node-c"],
    )
    return cabinet_client.admin_card(w.env.client("digi-cabinet"), task_id)


def plan_decision(w, task_id):
    """The sysadmin's plan with one script step on node-c, opened as the owner's decision."""
    board_card = card(w, task_id)
    data = proposal(
        task_id=task_id,
        operations=[
            explain(
                {"server": "node-c", "verb": "script", "script": SCRIPT},
                what="Перечитать конфигурацию nginx",
            )
        ],
    )
    plan = admin_proposal.submit(
        w.env.admin.paths.proposals,
        json.dumps(data).encode("utf-8"),
        agent="sysadmin",
        card=board_card,
        catalog=w.env.admin.catalog(),
        inventory=INVENTORY,
        enabled=ENABLED,
        now=w.env.when(),
    )
    payload = {
        "task_id": task_id,
        "proposal_sha256": plan.sha256,
        "card_scope_sha256": board_card.scope_sha256,
    }
    return w.env.call(
        "digi-sysadmin",
        {
            "op": "decision.open",
            "type": "admin_plan",
            "card": task_id,
            "payload": payload,
        },
    )["decision"]


def plan_envelope(w, task_id) -> bytes:
    """Step 1 of the card's approved plan as kd-admin-run builds it: the grant and the cabinet's signed copy."""
    grant = w.env.admin.store.load(task_id)
    stored = admin_signed.load_plan(
        w.env.admin.paths.grants, task_id, grant.proposal_sha256, None
    )
    plan = ops.validate_proposal(
        stored.data, w.env.admin.catalog(), INVENTORY, ENABLED, card_servers=["node-c"]
    )
    return exec_envelope.build_plan(grant, plan.as_dict(), 1, "node-c", SCRIPT)


def test_a_plan_step_the_owner_signed_passes_the_gate(w):
    d = plan_decision(w, "KD-194")
    sign(w, [d["id"]])
    envelope = plan_envelope(w, "KD-194")
    assert (
        json.loads(envelope)["decision"]["id"] == d["id"]
    )  # the cabinet's decision, not the grant's journal id
    assert verify(w, envelope) == {
        "kind": "plan",
        "task_id": "KD-194",
        "step": 1,
        "command": SCRIPT,
        "argv": None,
        "action_id": "",
        "once": "",
        "test_env": "",  # a signed step is never the test zone's
    }


def test_two_plans_signed_at_once_pass_the_gate_each(w):
    one, two = plan_decision(w, "KD-194"), plan_decision(w, "KD-195")
    sign(w, [one["id"], two["id"]])
    for task_id, decision in (("KD-194", one), ("KD-195", two)):
        envelope = plan_envelope(w, task_id)
        assert json.loads(envelope)["decision"]["id"] == decision["id"]
        assert verify(w, envelope)["task_id"] == task_id


def test_a_critical_command_the_owner_signed_passes_the_gate(w):
    board_card = card(w, "KD-196", status="To Do")
    submitted = admin_proposal.submit_action(
        w.env.admin.paths.proposals,
        json.dumps(action_data(task_id="KD-196")).encode("utf-8"),
        agent="sysadmin",
        card=board_card,
        inventory=INVENTORY,
        enabled=ENABLED,
        now=w.env.when(),
        deny=lambda command, alias: None,
    )
    payload = {
        "task_id": "KD-196",
        "proposal_id": submitted.proposal_id,
        "proposal_sha256": submitted.sha256,
        "card_scope_sha256": board_card.scope_sha256,
    }
    d = w.env.call(
        "digi-sysadmin",
        {
            "op": "decision.open",
            "type": "admin_critical",
            "card": "KD-196",
            "payload": payload,
        },
    )["decision"]
    sign(w, [d["id"]])
    grant = w.env.admin.actions.load("KD-196", submitted.proposal_id)
    stored = admin_signed.load_action(
        w.env.admin.paths.grants, "KD-196", grant.proposal_id, None
    )
    action = admin_action.validate_action(
        stored.data, INVENTORY, ENABLED, card_servers=["node-c"]
    )
    envelope = exec_envelope.build_action(
        grant, action.as_dict(), "node-c", grant.command
    )
    out = verify(w, envelope)
    assert (
        out["kind"] == "action"
        and out["command"] == grant.command
        and out["action_id"] == grant.action_id
    )
    assert out["once"] == gate.once_key(json.loads(envelope)["decision"]) != ""
