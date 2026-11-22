"""the exec envelope — what the factory host sends to a target's exec gate with every step, and what the
gate recomputes the owner's signed value from (the decision payload, the signed proposal, the binding, the
assertion)."""

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from kd import admin_action, admin_grant, admin_task, exec_envelope
from kd import admin_operations as ops
from admin_samples import CATALOG, ENABLED, INVENTORY, action, proposal

NOW = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
EXP = "2026-11-07T10:05:00Z"
NONCE = "ab" * 16
D1, D2 = "d1" * 16, "d2" * 16  # the cabinet's decisions the signatures name
ASSERTION = {
    "id": "Y3JlZC0x", "rawId": "Y3JlZC0x", "type": "public-key", "authenticatorAttachment": "platform",
    "clientExtensionResults": {},
    "response": {"clientDataJSON": "e30", "authenticatorData": "AAAA", "signature": "c2ln", "userHandle": "dXNlcg"},
}
BINDING = {"expires": EXP, "nonce": NONCE, "revisions": {"KD-123": 7}}


def signed(binding=BINDING, decision=D1, **over):
    """The signature the cabinet keeps in the grant: the browser's assertion, the binding and the id of the
    decision it approved, canonical JSON."""
    data = {**ASSERTION, "binding": binding, "decision": decision}
    data.update(over)
    return admin_task.canonical(data).decode("utf-8")


def plan_dict(**over):
    catalog = ops.Catalog.from_dict({**CATALOG, "verbs": [*CATALOG["verbs"], "script"]})
    return ops.validate_proposal(proposal(**over), catalog, INVENTORY, ENABLED, card_servers=["node-c"]).as_dict()


def make_grant(signature=signed(), data=None, **over):
    data = plan_dict() if data is None else data
    fields = dict(grant_id="g1", task_id="KD-123", assignee="@sysadmin", project="admin",
                  card_scope_sha256="c" * 64, proposal_sha256=hashlib.sha256(admin_task.canonical(data)).hexdigest(),
                  operations=tuple(data["operations"]), approved_at=NOW, expires_at=NOW + timedelta(hours=24),
                  approved_by="user:owner", state="active", decision_id="journal-1", signer="Y3JlZC0x" if signature else "",
                  signature=signature)
    fields.update(over)
    return admin_grant.Grant(**fields)


def make_action_grant(signature=signed(decision=D2), **over):
    act = admin_action.validate_action(action(), INVENTORY, ENABLED, card_servers=["node-c"])
    fields = dict(action_id="a1", proposal_id=act.proposal_id, task_id="KD-123", card_scope_sha256="c" * 64,
                  action_sha256=admin_action.binding_sha("KD-123", act.servers, act.command), servers=act.servers,
                  command=act.command, approved_at=NOW, expires_at=NOW + timedelta(minutes=15), state="active",
                  decision_id="journal-2", approved_by="user:owner", signer="Y3JlZC0x" if signature else "",
                  signature=signature)
    fields.update(over)
    return act.as_dict(), admin_action.ActionGrant(**fields)


def test_decision_of_signed_grant():
    d = exec_envelope.decision_of(make_grant())
    assert (d.id, d.user, d.expires, d.nonce, d.revisions["KD-123"], d.digests) == (D1, "owner", EXP, NONCE, 7, ())
    assert set(d.assertion) == {"id", "rawId", "type", "response"} and d.assertion["type"] == "public-key"
    assert set(d.assertion["response"]) == {"clientDataJSON", "authenticatorData", "signature"}
    assert d.assertion["response"]["signature"] == "c2ln"


def test_decision_of_a_batch_carries_its_digests_and_a_plain_approver():
    batch = {**BINDING, "digests": ["0" * 64, "1" * 64]}
    d = exec_envelope.decision_of(make_grant(signature=signed(batch), approved_by="owner"))
    assert d.digests == ("0" * 64, "1" * 64) and d.user == "owner"


def test_decision_of_totp_or_unsigned_grant_has_no_assertion():
    totp = admin_task.canonical({"id": "totp", "type": "totp", "user": "owner", "binding": BINDING}).decode()
    d = exec_envelope.decision_of(make_grant(signature=totp, signer="totp"))
    assert d.assertion is None and d.expires == EXP and d.revisions == {"KD-123": 7}
    d = exec_envelope.decision_of(make_grant(signature=""))
    assert d.assertion is None and d.revisions == {} and d.nonce == "" and d.id == ""
    # a grant written before the binding carried revisions: the target could not recompute the digest — unsigned
    d = exec_envelope.decision_of(make_grant(signature=signed({"expires": EXP, "nonce": NONCE})))
    assert d.assertion is None and d.revisions == {} and d.nonce == NONCE
    # an assertion without one of the three response parts, or a nonce that is not 32 hex, signs nothing either
    broken = {**ASSERTION, "response": {k: v for k, v in ASSERTION["response"].items() if k != "signature"}}
    d = exec_envelope.decision_of(make_grant(signature=admin_task.canonical({**broken, "binding": BINDING}).decode()))
    assert d.assertion is None
    d = exec_envelope.decision_of(make_grant(signature=signed({**BINDING, "nonce": "zz"})))
    assert d.assertion is None and d.nonce == ""
    d = exec_envelope.decision_of(make_grant(signature="{not json"))
    assert d.assertion is None and d.revisions == {}


def test_decision_id_is_the_one_the_signature_names_never_the_grants_own():
    """the grant's ``decision_id`` is its journal record; the cabinet digested its own decision's id, which
    the signature names. A signature that names none (a grant approved before that) is one the target cannot
    verify."""
    d = exec_envelope.decision_of(make_grant(decision_id="0f4d9c7e-1111-4222-8333-444455556666"))
    assert d.id == D1 and d.assertion is not None
    for unnamed in ({}, {"decision": "0f4d9c7e-1111-4222-8333-444455556666"}, {"decision": 7}, {"decision": D1 + "0"}):
        data = {**ASSERTION, "binding": BINDING, **unnamed}
        d = exec_envelope.decision_of(make_grant(signature=admin_task.canonical(data).decode("utf-8")))
        assert d.id == "" and d.assertion is None and d.nonce == NONCE and d.revisions == {"KD-123": 7}


def test_build_plan_envelope_is_canonical_and_bound():
    grant, data = make_grant(), plan_dict()
    raw = exec_envelope.build_plan(grant, data, 2, "node-c", "systemctl restart nginx.service")
    assert raw == admin_task.canonical(json.loads(raw))  # canonical JSON: the gate hashes nothing of it, but one form
    env = json.loads(raw)
    assert set(env) == {"schema_version", "kind", "task_id", "server", "step", "command", "payload", "proposal",
                        "grant", "decision", "assertion"}
    assert env["schema_version"] == 1 and env["kind"] == "plan" and env["task_id"] == "KD-123"
    assert env["server"] == "node-c" and env["step"] == 2 and env["command"] == "systemctl restart nginx.service"
    assert env["payload"] == {"task_id": "KD-123", "proposal_sha256": grant.proposal_sha256,
                              "card_scope_sha256": grant.card_scope_sha256}
    assert env["proposal"] == data
    assert env["grant"] == {"id": "g1", "approved_at": admin_grant.fmt(NOW),
                            "expires_at": admin_grant.fmt(grant.expires_at)}
    assert env["decision"] == {"id": D1, "user": "owner", "expires": EXP, "nonce": NONCE, "revision": 7}
    assert env["assertion"] == {"id": "Y3JlZC0x", "rawId": "Y3JlZC0x", "type": "public-key",
                                "response": {"clientDataJSON": "e30", "authenticatorData": "AAAA", "signature": "c2ln"}}
    # the payload hashes to what the cabinet digested: the gate recomputes the decision digest from it
    from kd.cabinet import decisions

    assert decisions.payload_sha(env["payload"]) == hashlib.sha256(admin_task.canonical(env["payload"])).hexdigest()


def test_build_plan_carries_batch_digests_and_no_revision_when_unknown():
    batch = {**BINDING, "digests": ["0" * 64, "1" * 64]}
    env = json.loads(exec_envelope.build_plan(make_grant(signature=signed(batch)), plan_dict(), 1, "node-c", "x"))
    assert env["decision"]["digests"] == ["0" * 64, "1" * 64] and env["decision"]["revision"] == 7
    other = {**BINDING, "revisions": {"KD-999": 3}}
    env = json.loads(exec_envelope.build_plan(make_grant(signature=signed(other)), plan_dict(), 1, "node-c", "x"))
    assert "revision" not in env["decision"] and "digests" not in env["decision"]


def test_build_plan_of_an_unsigned_grant_has_a_null_assertion():
    env = json.loads(exec_envelope.build_plan(make_grant(signature=""), plan_dict(), 1, "node-c", "x"))
    assert env["assertion"] is None and env["decision"] == {"id": "", "user": "owner", "expires": "", "nonce": ""}


def test_build_plan_refuses_a_proposal_that_is_not_the_signed_one():
    grant = make_grant()
    other = plan_dict(intent="Другое намерение")
    with pytest.raises(exec_envelope.EnvelopeError, match="not the one the owner signed"):
        exec_envelope.build_plan(grant, other, 1, "node-c", "x")


def test_build_plan_refuses_a_step_out_of_range():
    grant, data = make_grant(), plan_dict()
    for step in (0, 3, -1, "1", True):
        with pytest.raises(exec_envelope.EnvelopeError, match="step"):
            exec_envelope.build_plan(grant, data, step, "node-c", "x")


def test_build_action_envelope_carries_action_id_and_once_binding():
    act, grant = make_action_grant()
    raw = exec_envelope.build_action(grant, act, "node-c", act["command"])
    env = json.loads(raw)
    assert env["kind"] == "action" and "step" not in env and env["server"] == "node-c"
    assert env["command"] == "wg set wg0 peer 'abc=' remove" and env["proposal"] == act
    action_sha = hashlib.sha256(admin_task.canonical(act)).hexdigest()
    assert env["payload"] == {"task_id": "KD-123", "proposal_id": action_sha[:16], "proposal_sha256": action_sha,
                              "card_scope_sha256": "c" * 64}
    assert env["grant"] == {"id": "a1", "action_id": "a1", "approved_at": admin_grant.fmt(NOW),
                            "expires_at": admin_grant.fmt(NOW + timedelta(minutes=15))}
    assert env["decision"]["id"] == D2 and env["assertion"]["type"] == "public-key"


def test_build_action_refuses_another_action_server_or_command():
    act, grant = make_action_grant()
    with pytest.raises(exec_envelope.EnvelopeError, match="not the one the owner signed"):
        exec_envelope.build_action(grant, {**act, "intent": "другое"}, "node-c", act["command"])
    with pytest.raises(exec_envelope.EnvelopeError, match="command"):
        exec_envelope.build_action(grant, act, "node-c", act["command"] + " --force")
    with pytest.raises(exec_envelope.EnvelopeError, match="server"):
        exec_envelope.build_action(grant, act, "node-g", act["command"])
    _, other = make_action_grant(action_sha256="0" * 64)  # a grant whose binding is not this action's
    with pytest.raises(exec_envelope.EnvelopeError, match="not the one the owner signed"):
        exec_envelope.build_action(other, act, "node-c", act["command"])


def test_envelope_size_limit():
    # nine readable scripts just under the step limit (16 KiB): together they pass what the gate reads (128 KiB)
    line = "echo 'line of a long but readable script'\n"
    big = plan_dict(operations=[{"server": "node-c", "verb": "script", "script": line * (15 * 1024 // len(line)),
                                 "what": "много", "effect": "много", "risk": {"level": "low", "why": "тест"},
                                 "rollback": "нет"}] * 9)
    grant = make_grant(data=big)
    with pytest.raises(exec_envelope.EnvelopeError, match="bytes"):
        exec_envelope.build_plan(grant, big, 1, "node-c", big["operations"][0]["script"])
    assert exec_envelope.MAX_BYTES == 128 * 1024


def test_enabled_reads_the_presence_file(tmp_path):
    flag = tmp_path / "exec-envelopes"
    assert exec_envelope.enabled(str(flag)) is False
    flag.write_text("")
    assert exec_envelope.enabled(str(flag)) is True
    assert exec_envelope.FLAG == "/etc/digipaddock/exec-envelopes"
    assert exec_envelope.GATE_RUN == "agent-exec-gate run" and exec_envelope.SCHEMA_VERSION == 1
