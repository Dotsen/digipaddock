"""agent-exec-gate — the exec key's forced command on a target. It verifies the owner's passkey signature
over the exact plan step (or critical command) before anything runs; observe logs and runs, enforce refuses.

The signature checks here use ``cryptography`` (``crypto_verify``), so the tests need no openssl; one test runs
the real ``openssl dgst -verify`` when it is on PATH.
"""

import base64
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cbor2
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from gate import agent_exec_gate as gate
from kd import admin_action, admin_grant, exec_envelope
from kd.admin_task import canonical

GATE_FILE = Path(gate.__file__)
PY = sys.executable
RP_ID = "cabinet.example.test"
ORIGIN = "https://cabinet.example.test"
ALIAS = "node-c"
GATE = {"alias": ALIAS, "rp_id": RP_ID, "origin": ORIGIN, "mode": "enforce"}
NOW_DT = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
NOW = NOW_DT.timestamp() + 60
EXP = "2026-11-07T10:05:00Z"
NONCE = "ab" * 16
D1, D2 = "d1" * 16, "d2" * 16  # the cabinet's decisions the signatures name
SCRIPT = "set -e\ncp /etc/nginx/nginx.conf /tmp/nginx.conf.bak\nnginx -t\n"
PROPOSAL = {
    "schema_version": 2, "task_id": "KD-123", "intent": "Проверить конфиг и перезапустить nginx",
    "basis": "ssh-ro: nginx отдаёт 502",
    "operations": [
        {"server": ALIAS, "verb": "script", "script": SCRIPT, "what": "Проверить конфиг",
         "effect": "Только чтение", "risk": {"level": "low", "why": "Проверка"}, "rollback": "Не нужен"},
        {"server": ALIAS, "verb": "service.restart", "unit": "nginx.service", "what": "Перезапустить nginx",
         "effect": "Сайт недоступен пару секунд", "risk": {"level": "low", "why": "Конфигурация не меняется"},
         "rollback": "Не нужен"},
        {"server": ALIAS, "verb": "package.refresh", "what": "Обновить индексы", "effect": "Нет",
         "risk": {"level": "low", "why": "Чтение"}, "rollback": "Не нужен"},
        {"server": "node-g", "verb": "service.start", "unit": "app.service", "what": "Запустить",
         "effect": "Служба поднимется", "risk": {"level": "low", "why": "Нет"}, "rollback": "Остановить"},
    ],
}
ACTION = {"schema_version": 2, "task_id": "KD-123", "intent": "Убрать пир", "basis": "wg show: пир не отвечает",
          "servers": [ALIAS, "node-g"], "command": "wg set wg0 peer 'abc=' remove", "effect": "Пир отключён",
          "risk": {"level": "high", "why": "Связь с пиром пропадёт"}, "rollback": "Вернуть пир"}


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def make_es256():
    key = ec.generate_private_key(ec.SECP256R1())
    numbers = key.public_key().public_numbers()
    cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")})
    return cose, key


def make_rs256():
    key = rsa.generate_private_key(65537, 2048)
    numbers = key.public_key().public_numbers()
    n = numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")
    e = numbers.e.to_bytes(3, "big")
    return cbor2.dumps({1: 3, 3: -257, -1: n, -2: e}), key


def sign_with(key, data: bytes) -> bytes:
    if isinstance(key, ec.EllipticCurvePrivateKey):
        return key.sign(data, ec.ECDSA(hashes.SHA256()))
    return key.sign(data, padding.PKCS1v15(), hashes.SHA256())


def crypto_verify(pem: str, data: bytes, signature: bytes) -> bool:
    public = serialization.load_pem_public_key(pem.encode("ascii"))
    try:
        if isinstance(public, ec.EllipticCurvePublicKey):
            public.verify(signature, data, ec.ECDSA(hashes.SHA256()))
        else:
            public.verify(signature, data, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        return False
    return True


class Signer:
    """The owner's signing passkey as the cabinet registers it and the target's registry lists it."""

    def __init__(self, make=make_es256, user="owner"):
        self.cose, self.key = make()
        self.credential_id = b64u(hashlib.sha256(self.cose).digest()[:16])
        self.user = user

    def registry(self, *others):
        return {"schema_version": 1, "rp_id": RP_ID, "origin": ORIGIN,
                "signers": [{"user": s.user, "credential_id": s.credential_id, "public_key": b64u(s.cose),
                             "name": "Chrome"} for s in (self, *others)]}

    def assertion(self, challenge: bytes, *, origin=ORIGIN, rp_id=RP_ID, flags=b"\x05", kind="webauthn.get"):
        client = json.dumps({"type": kind, "challenge": b64u(challenge), "origin": origin, "crossOrigin": False})
        client_bytes = client.encode("utf-8")
        auth = hashlib.sha256(rp_id.encode("utf-8")).digest() + flags + b"\0\0\0\x01"
        signature = sign_with(self.key, auth + hashlib.sha256(client_bytes).digest())
        return {"id": self.credential_id, "rawId": self.credential_id, "type": "public-key",
                "response": {"clientDataJSON": b64u(client_bytes), "authenticatorData": b64u(auth),
                             "signature": b64u(signature), "userHandle": "dXNlcg"}}


def plan_payload(proposal=PROPOSAL):
    return {"task_id": "KD-123", "proposal_sha256": hashlib.sha256(canonical(proposal)).hexdigest(),
            "card_scope_sha256": "c" * 64}


def decision_dict(revision=7, digests=None, user="owner", decision_id=D1):
    data = {"id": decision_id, "user": user, "expires": EXP, "nonce": NONCE, "revision": revision}
    if digests is not None:
        data["digests"] = digests
    return data


def plan_grant(signature: str, proposal=PROPOSAL, approved_by="user:owner", **over):
    payload = plan_payload(proposal)
    fields = dict(grant_id="g1", task_id="KD-123", assignee="@sysadmin", project="admin",
                  card_scope_sha256=payload["card_scope_sha256"], proposal_sha256=payload["proposal_sha256"],
                  operations=tuple(proposal["operations"]), approved_at=NOW_DT,
                  expires_at=NOW_DT + timedelta(hours=24), approved_by=approved_by, state="active",
                  decision_id="journal-1", signer="x", signature=signature)
    fields.update(over)
    return admin_grant.Grant(**fields)


def signed_plan(signer: Signer, step=1, *, revision=7, digests=None, proposal=PROPOSAL, assertion_over=None,
                command=None, **grant_over):
    """A plan envelope whose assertion the signer made over the cabinet's challenge for this decision."""
    decision = decision_dict(revision, digests)
    challenge = gate.challenge(decision, plan_payload(proposal))
    assertion = signer.assertion(challenge, **(assertion_over or {}))
    binding = {"expires": EXP, "nonce": NONCE, "revisions": {"KD-123": revision}}
    if digests is not None:
        binding["digests"] = digests
    grant = plan_grant(canonical({**assertion, "binding": binding, "decision": D1}).decode("utf-8"), proposal,
                       **grant_over)
    op = proposal["operations"][step - 1]
    if command is None:
        command = op["script"] if op["verb"] == "script" else {
            "service.restart": "systemctl restart " + op.get("unit", ""),
            "service.start": "systemctl start " + op.get("unit", ""),
            "package.refresh": "apt-get -q update",
        }[op["verb"]]
    return json.loads(exec_envelope.build_plan(grant, proposal, step, op["server"], command))


def signed_action(signer: Signer, *, server=ALIAS, command=None, revision=7, **grant_over):
    action_sha = hashlib.sha256(canonical(ACTION)).hexdigest()
    payload = {"task_id": "KD-123", "proposal_id": action_sha[:16], "proposal_sha256": action_sha,
               "card_scope_sha256": "c" * 64}
    decision = decision_dict(revision, decision_id=D2)
    assertion = signer.assertion(gate.challenge(decision, payload))
    binding = {"expires": EXP, "nonce": NONCE, "revisions": {"KD-123": revision}}
    fields = dict(action_id="0f4d9c7e-1111-4222-8333-444455556666", proposal_id=action_sha[:16], task_id="KD-123",
                  card_scope_sha256="c" * 64,
                  action_sha256=admin_action.binding_sha("KD-123", ACTION["servers"], ACTION["command"]),
                  servers=tuple(ACTION["servers"]), command=ACTION["command"], approved_at=NOW_DT,
                  expires_at=NOW_DT + timedelta(minutes=15), state="active", decision_id="journal-2",
                  approved_by="user:owner", signer="x",
                  signature=canonical({**assertion, "binding": binding, "decision": D2}).decode("utf-8"))
    fields.update(grant_over)
    grant = admin_action.ActionGrant(**fields)
    envelope = json.loads(exec_envelope.build_action(grant, ACTION, ALIAS, ACTION["command"]))
    envelope["server"] = server
    if command is not None:
        envelope["command"] = command
    return envelope


@pytest.fixture(autouse=True)
def apt_target(monkeypatch):
    monkeypatch.setattr(gate, "which", lambda name: "/usr/bin/apt-get" if name == "apt-get" else None)


POLICY_FILE = Path(gate.__file__).resolve().parents[3] / "common" / "kd" / "exec_policy.py"


@pytest.fixture(autouse=True)
def policy_file(monkeypatch):
    """the gate loads the policy installed next to it; here — the repository's kd.exec_policy."""
    monkeypatch.setattr(gate, "POLICY_PATH", str(POLICY_FILE))
    monkeypatch.setattr(gate, "_POLICY", {})


@pytest.fixture
def owner():
    return Signer()


def verify(envelope, signers, now=NOW, gate_cfg=GATE):
    return gate.verify(envelope, gate_cfg, signers, now, verify_sig=crypto_verify)


# --- keys and the challenge ------------------------------------------------------------------------------------

@pytest.mark.parametrize("make", [make_es256, make_rs256])
def test_cose_to_pem_matches_cryptography(make):
    cose, key = make()
    pem, alg = gate.cose_to_pem(cose)
    expected = key.public_key().public_bytes(serialization.Encoding.PEM,
                                             serialization.PublicFormat.SubjectPublicKeyInfo).decode("ascii")
    assert pem == expected and alg == ("ES256" if make is make_es256 else "RS256")
    data, signature = b"the signed bytes", sign_with(key, b"the signed bytes")
    assert crypto_verify(pem, data, signature) and not crypto_verify(pem, data + b"!", signature)


def test_other_cose_keys_and_bad_cbor_are_refused():
    cose, _ = make_es256()
    for broken in (cbor2.dumps({1: 1, 3: -8, -1: 6, -2: b"\x01" * 32}),  # OKP / EdDSA
                   cbor2.dumps({1: 2, 3: -7, -1: 2, -2: b"\x01" * 48, -3: b"\x01" * 48}),  # P-384
                   cbor2.dumps([1, 2, 3]), b"\xff\x00", cose[:-5], cose + b"\x00",
                   b"\x9f\x01\xff"):  # indefinite length
        with pytest.raises(gate.Deny):
            gate.cose_to_pem(broken)


def test_cbor_decode_covers_the_cose_majors():
    value = {1: 2, -2: b"\x00\x01", "s": "текст", 4: [1, -1, 24, 255, 256, 65536, 2 ** 32], 25: -25}
    assert gate.cbor_decode(cbor2.dumps(value)) == value
    for bad in (cbor2.dumps(1.5), cbor2.dumps(True), cbor2.dumps(None), cbor2.dumps({1: {2: 3}}) + b"\x01", b""):
        with pytest.raises(gate.Deny):
            gate.cbor_decode(bad)


def test_challenge_matches_cabinet_formula():
    from kd.cabinet import decisions as cab

    payload = plan_payload()
    row = {"id": D1, "payload_sha": cab.payload_sha(payload)}
    single = cab.Decisions.digest(row, user="owner", expires=EXP, card_revision=7)
    assert gate.challenge(decision_dict(), payload) == hashlib.sha256(single + bytes.fromhex(NONCE)).digest()
    other = "f" * 64
    digests = sorted([single.hex(), other])
    bound = cab.Decisions.bound(digests)
    assert gate.challenge(decision_dict(digests=digests), payload) == hashlib.sha256(bound + bytes.fromhex(NONCE)).digest()
    with pytest.raises(gate.Deny, match="digest"):  # the own digest must be in the batch
        gate.challenge(decision_dict(digests=[other, "e" * 64]), payload)
    with pytest.raises(gate.Deny):
        gate.challenge(decision_dict(digests=["x" * 64, single.hex()]), payload)
    with pytest.raises(gate.Deny):
        gate.challenge(decision_dict(digests=[single.hex()] + ["0" * 64] * 50), payload)
    with pytest.raises(gate.Deny, match="nonce"):
        gate.challenge({**decision_dict(), "nonce": "zz"}, payload)
    with pytest.raises(gate.Deny, match="ревизия"):
        gate.challenge({k: v for k, v in decision_dict().items() if k != "revision"}, payload)


# --- verify ------------------------------------------------------------------------------------------------------

def test_valid_plan_envelope_is_allowed(owner):
    out = verify(signed_plan(owner, 1), owner.registry())
    assert out == {"kind": "plan", "task_id": "KD-123", "step": 1, "command": SCRIPT, "argv": None, "action_id": "",
                   "once": "", "test_env": ""}
    typed = verify(signed_plan(owner, 2), owner.registry())
    assert typed["command"] == "systemctl restart nginx.service"
    assert typed["argv"] == ["systemctl", "restart", "nginx.service"]
    assert verify(signed_plan(owner, 3), owner.registry())["command"] == "apt-get -q update"


def test_valid_batch_envelope_is_allowed(owner):
    env = signed_plan(owner, 1, digests=None)
    payload_sha = hashlib.sha256(canonical(plan_payload())).hexdigest()
    own = hashlib.sha256(("%s|%s|7|owner|%s" % (D1, payload_sha, EXP)).encode()).hexdigest()
    batch = signed_plan(owner, 1, digests=sorted([own, "f" * 64]))
    assert batch["decision"]["digests"] == sorted([own, "f" * 64]) and env["decision"].get("digests") is None
    assert verify(batch, owner.registry())["kind"] == "plan"


def test_valid_action_envelope_is_allowed(owner):
    out = verify(signed_action(owner), owner.registry())
    assert out["kind"] == "action" and out["command"] == ACTION["command"]
    assert out["action_id"] == "0f4d9c7e-1111-4222-8333-444455556666"


def test_rs256_signer_is_allowed():
    owner = Signer(make_rs256)
    assert verify(signed_plan(owner, 1), owner.registry())["kind"] == "plan"


def test_envelope_for_another_server_is_refused(owner):
    env = signed_plan(owner, 1)
    with pytest.raises(gate.Deny, match="сервер"):
        verify(env, owner.registry(), gate_cfg={**GATE, "alias": "node-g"})
    # step 4 is node-g's: this server is not its server even when the envelope says so
    env = signed_plan(owner, 4)
    env["server"] = ALIAS
    with pytest.raises(gate.Deny, match="сервер"):
        verify(env, owner.registry())
    with pytest.raises(gate.Deny, match="сервер"):
        verify(signed_action(owner, server="node-l"), owner.registry(), gate_cfg={**GATE, "alias": "node-l"})


def test_assertion_of_another_proposal_is_refused(owner):
    env = signed_plan(owner, 1)
    other = json.loads(json.dumps(PROPOSAL))
    other["intent"] = "Другое"
    env["proposal"] = other
    with pytest.raises(gate.Deny, match="proposal"):  # the payload hash no longer matches the proposal
        verify(env, owner.registry())
    env = signed_plan(owner, 1)
    env["payload"]["card_scope_sha256"] = "d" * 64  # another card scope: the digest changes, the signature fails
    with pytest.raises(gate.Deny, match="подпись"):
        verify(env, owner.registry())
    env = signed_plan(owner, 1)
    env["decision"]["revision"] = 8
    with pytest.raises(gate.Deny, match="подпись"):
        verify(env, owner.registry())
    env = signed_plan(owner, 1)
    env["decision"]["user"] = "anna"
    with pytest.raises(gate.Deny):
        verify(env, owner.registry(Signer(user="anna")))


def test_command_not_the_step_text_is_refused(owner):
    env = signed_plan(owner, 1, command=SCRIPT + "rm -rf /\n")
    with pytest.raises(gate.Deny, match="команда"):
        verify(env, owner.registry())
    env = signed_plan(owner, 1, command=SCRIPT.replace("\n", "\r\n"))
    with pytest.raises(gate.Deny, match="команда"):
        verify(env, owner.registry())
    env = signed_plan(owner, 1)
    env["step"] = 2  # the script with the typed step's index
    with pytest.raises(gate.Deny, match="команда"):
        verify(env, owner.registry())


def test_typed_step_command_must_match_its_verb(owner, monkeypatch):
    with pytest.raises(gate.Deny, match="команда"):
        verify(signed_plan(owner, 2, command="systemctl restart sshd.service"), owner.registry())
    with pytest.raises(gate.Deny, match="команда"):
        verify(signed_plan(owner, 2, command="systemctl restart nginx.service; id"), owner.registry())
    monkeypatch.setattr(gate, "which", lambda name: "/usr/bin/dnf" if name == "dnf" else None)
    with pytest.raises(gate.Deny, match="команда"):
        verify(signed_plan(owner, 3, command="apt-get -q update"), owner.registry())
    assert verify(signed_plan(owner, 3, command="dnf -q makecache"), owner.registry())["kind"] == "plan"


@pytest.mark.parametrize("command", [
    "systemctl  restart 'nginx.service'",  # the same words, another text
    "bash -c 'systemctl restart nginx.service'",
    "systemctl restart nginx.service\n",
    "systemctl restart $'nginx.service'",
    "systemctl restart nginx.service #",
])
def test_typed_step_compares_the_whole_text_not_its_shlex_words(owner, command):
    # bash runs the text, so the gate compares the text the stage forms (shlex.join), not shlex.split
    with pytest.raises(gate.Deny, match="команда"):
        verify(signed_plan(owner, 2, command=command), owner.registry())
    assert gate.quote_argv(["systemctl", "restart", "a b.service"]) == "systemctl restart 'a b.service'"


def test_hex_and_ids_refuse_a_trailing_newline(owner, tmp_path):
    # re.match with ^…$ took "…\n"; fullmatch does not
    with pytest.raises(gate.Deny):
        gate._hex64("a" * 64 + "\n", "x")
    with pytest.raises(gate.Deny):
        gate.take_once(str(tmp_path), "abc\n")
    env = signed_plan(owner, 1)
    env["task_id"] = "KD-123\n"
    with pytest.raises(gate.Deny):
        verify(env, owner.registry())
    with pytest.raises(gate.Deny, match="nonce"):
        gate.challenge({**decision_dict(), "nonce": NONCE + "\n"}, plan_payload())


def test_deep_nesting_is_a_refusal_not_a_crash(host, owner):
    # RecursionError in json.loads or canonical ended the gate with exit 1 instead of 126
    deep = b'{"a":' + b"[" * 60000 + b"]" * 60000 + b"}"
    for mode in ("enforce", "observe"):
        code, out, err = host(mode, gate.RUN_WORD, deep)
        assert code == 126 and host.runs.calls == [] and "глубоко" in err
    nested = []  # type: list
    for _ in range(200000):
        nested = [nested]
    env = signed_plan(owner, 1)
    env["proposal"]["intent"] = nested
    with pytest.raises(gate.Deny, match="глубоко"):
        verify(env, owner.registry())
    assert gate._describe({"task_id": nested, "kind": "plan", "step": nested}) == "list plan step list ?"


def test_openssl_is_found_on_the_gates_own_path(monkeypatch):
    # an inherited PATH must not choose the openssl that verifies the owner's signature
    seen = {}
    monkeypatch.setattr(gate.shutil, "which", lambda name, path=None: seen.setdefault(name, path) and None)
    monkeypatch.setenv("PATH", "/tmp/evil")
    assert gate.openssl_path() == "/usr/bin/openssl" and seen["openssl"] == gate.SAFE_PATH


def test_package_install_argv_mirrors_the_stage(owner):
    proposal = json.loads(json.dumps(PROPOSAL))
    proposal["operations"][2] = {"server": ALIAS, "verb": "package.install",
                                 "packages": [{"name": "nginx", "version": "1.24.0-1"}, {"name": "curl", "version": "8.5.0-2"}],
                                 "what": "Поставить", "effect": "Пакеты", "risk": {"level": "medium", "why": "Новые пакеты"},
                                 "rollback": "Удалить"}
    command = ("env DEBIAN_FRONTEND=noninteractive apt-get -q -y -o Dpkg::Options::=--force-confdef -o "
               "Dpkg::Options::=--force-confold --no-remove install nginx=1.24.0-1 curl=8.5.0-2")
    assert verify(signed_plan(owner, 3, proposal=proposal, command=command), owner.registry())["command"] == command
    with pytest.raises(gate.Deny, match="команда"):
        verify(signed_plan(owner, 3, proposal=proposal, command=command.replace("--no-remove ", "")), owner.registry())


def test_action_command_or_server_outside_the_signed_action_is_refused(owner):
    with pytest.raises(gate.Deny, match="команда"):
        verify(signed_action(owner, command=ACTION["command"] + " --force"), owner.registry())
    env = signed_action(owner)
    env["proposal"]["servers"] = ["node-l"]
    with pytest.raises(gate.Deny):  # the proposal no longer hashes to the payload
        verify(env, owner.registry())


def test_future_approval_is_refused_with_clock_hint(owner):
    env = signed_plan(owner, 1)
    with pytest.raises(gate.Deny, match="проверь часы"):
        verify(env, owner.registry(), now=NOW_DT.timestamp() - gate.SKEW - 1)
    assert verify(env, owner.registry(), now=NOW_DT.timestamp() - gate.SKEW + 1)["kind"] == "plan"


def test_expired_grant_is_refused(owner):
    env = signed_plan(owner, 1)
    with pytest.raises(gate.Deny, match="истекло"):
        verify(env, owner.registry(), now=(NOW_DT + timedelta(hours=24)).timestamp())
    long = signed_plan(owner, 1, expires_at=NOW_DT + timedelta(days=8))
    with pytest.raises(gate.Deny, match="дольше"):
        verify(long, owner.registry())
    action = signed_action(owner, expires_at=NOW_DT + timedelta(minutes=16))
    with pytest.raises(gate.Deny, match="дольше"):
        verify(action, owner.registry())
    with pytest.raises(gate.Deny, match="истекло"):
        verify(signed_action(owner), owner.registry(), now=(NOW_DT + timedelta(minutes=15)).timestamp())


def test_grant_times_are_bound_by_the_signed_term(owner):
    # review C1: the grant's times are not signed — rewritten a year later, the envelope must still be refused:
    # the approval cannot postdate the signed term of the challenge, so the grant ends within the kind's longest
    # life of a moment the owner signed
    later = NOW_DT + timedelta(days=365)
    env = signed_plan(owner, 1)
    env["grant"]["approved_at"] = (later - timedelta(minutes=1)).strftime(gate.TIME_FORMAT)
    env["grant"]["expires_at"] = (later + timedelta(hours=1)).strftime(gate.TIME_FORMAT)
    with pytest.raises(gate.Deny, match="после срока его подписи"):
        verify(env, owner.registry(), now=later.timestamp())
    term = NOW_DT + timedelta(minutes=5)  # EXP: the signed term
    assert verify(signed_plan(owner, 1, approved_at=term), owner.registry())["kind"] == "plan"
    with pytest.raises(gate.Deny, match="после срока его подписи"):
        verify(signed_plan(owner, 1, approved_at=term + timedelta(seconds=1)), owner.registry())
    with pytest.raises(gate.Deny, match="после срока его подписи"):
        verify(signed_action(owner, approved_at=term + timedelta(seconds=1)), owner.registry())


def test_once_key_comes_from_the_signed_decision(owner, tmp_path):
    # review C2: the grant's action_id is not signed — a fresh id must not buy a second run on this server
    env = signed_action(owner)
    first = verify(env, owner.registry())
    assert first["once"] == hashlib.sha256((D2 + "|" + NONCE).encode("ascii")).hexdigest()
    gate.take_once(str(tmp_path), first["once"])
    env["grant"]["action_id"] = "fresh-id-2"
    second = verify(env, owner.registry())
    assert second["once"] == first["once"] and second["action_id"] == "fresh-id-2"
    with pytest.raises(gate.Deny, match="уже выполнено"):
        gate.take_once(str(tmp_path), second["once"])
    assert verify(signed_plan(owner, 1), owner.registry())["once"] == ""


def test_unknown_signer_or_other_user_is_refused(owner):
    env = signed_plan(owner, 1)
    stranger = Signer()
    with pytest.raises(gate.Deny, match="ключ подписи"):
        verify(env, stranger.registry())
    with pytest.raises(gate.Deny, match="ключ подписи"):
        verify(env, {**owner.registry(), "signers": [{**owner.registry()["signers"][0], "user": "anna"}]})
    with pytest.raises(gate.Deny, match="подпись не сходится"):  # the stranger's key under the owner's id
        verify(env, {**owner.registry(), "signers": [{**stranger.registry()["signers"][0], "user": "owner",
                                                      "credential_id": owner.credential_id}]})


def test_wrong_origin_or_rp_id_or_missing_uv_is_refused(owner):
    for over, why in (({"origin": "https://evil.example.test"}, "origin"),
                      ({"rp_id": "evil.example.test"}, "rp"),
                      ({"flags": b"\x01"}, "проверки пользователя"),
                      ({"flags": b"\x04"}, "присутствия"),
                      ({"kind": "webauthn.create"}, "webauthn.get")):
        env = signed_plan(owner, 1, assertion_over=over)
        with pytest.raises(gate.Deny, match=why):
            verify(env, owner.registry())


def test_unsigned_envelope_and_malformed_envelopes_are_refused(owner):
    env = signed_plan(owner, 1)
    env["assertion"] = None
    with pytest.raises(gate.Deny, match="подпись владельца passkey"):
        verify(env, owner.registry())
    for broken in ({**env, "schema_version": 2}, {**env, "kind": "shell"}, {k: v for k, v in env.items() if k != "grant"},
                   {**env, "extra": 1}, {**env, "step": "1"}, {**env, "step": 9}, {**env, "payload": {}},
                   {**env, "task_id": "KD-124"}, [], "x"):
        with pytest.raises(gate.Deny):
            verify(broken, owner.registry())


def test_oversized_envelope_is_refused(owner):
    env = signed_plan(owner, 1)
    env["proposal"]["intent"] = "x" * gate.MAX_ENVELOPE
    with pytest.raises(gate.Deny, match="слишком большой"):
        gate.parse_envelope(json.dumps(env).encode("utf-8"))
    assert gate.parse_envelope(json.dumps(signed_plan(owner, 1)).encode("utf-8"))["kind"] == "plan"
    with pytest.raises(gate.Deny):
        gate.parse_envelope(b"{not json")


# --- main --------------------------------------------------------------------------------------------------------

class Runs:
    def __init__(self, code=0):
        self.calls, self.code = [], code

    def __call__(self, command, as_root, timeout=gate.TIMEOUT):
        self.calls.append(command)
        return self.code


@pytest.fixture
def host(tmp_path, monkeypatch, owner):
    gate_json, signers_json, used = tmp_path / "gate.json", tmp_path / "signers.json", tmp_path / "used"
    signers_json.write_text(json.dumps(owner.registry()), encoding="utf-8")
    monkeypatch.setattr(gate, "GATE_JSON", str(gate_json))
    monkeypatch.setattr(gate, "SIGNERS_JSON", str(signers_json))
    monkeypatch.setattr(gate, "USED_DIR", str(used))
    # never the host's own list — on a host that has one (the CI contour) --check counted its entries
    monkeypatch.setattr(gate, "TEST_ENVS_JSON", str(tmp_path / "test-envs.json"))
    monkeypatch.setattr(gate, "SCRIPT_OFF", str(tmp_path / "no-script-off"))  # nor its script switch
    monkeypatch.setattr(gate, "openssl_version", lambda: "OpenSSL 3.0.13")
    logged = []
    monkeypatch.setattr(gate, "_log", lambda message, level=None: logged.append(message))
    runs = Runs()
    monkeypatch.setattr(gate, "run", runs)

    def main(mode, command, stdin=b"", now=NOW, **settings):
        gate_json.write_text(json.dumps({**GATE, "mode": mode, **settings}), encoding="utf-8")
        out, err = io.BytesIO(), io.BytesIO()
        code = gate.main([], environ={"SSH_ORIGINAL_COMMAND": command}, stdin=io.BytesIO(stdin), out=out, err=err,
                         now=lambda: now, verify_sig=crypto_verify)
        return code, out.getvalue().decode("utf-8"), err.getvalue().decode("utf-8")

    main.runs, main.logged, main.paths = runs, logged, (gate_json, signers_json, used)
    return main


def test_valid_envelope_runs_through_main(host, owner):
    raw = json.dumps(signed_plan(owner, 1)).encode("utf-8")
    code, out, err = host("enforce", gate.RUN_WORD, raw)
    assert (code, err) == (0, "") and host.runs.calls == [SCRIPT]
    assert any(m.startswith("allow KD-123 plan step 1") for m in host.logged)


def test_unsigned_grant_observe_runs_enforce_refuses(host, owner):
    env = signed_plan(owner, 1)
    env["assertion"] = None
    raw = json.dumps(env).encode("utf-8")
    code, out, err = host("observe", gate.RUN_WORD, raw)
    assert code == 0 and host.runs.calls == [SCRIPT]
    assert any(m.startswith("observe FAIL (") and "KD-123" in m for m in host.logged)
    code, out, err = host("enforce", gate.RUN_WORD, raw)
    assert code == 126 and host.runs.calls == [SCRIPT] and "agent-exec-gate: отказ" in err
    assert "подпись владельца passkey" in err


def test_legacy_command_observe_runs_enforce_refuses(host):
    code, out, err = host("observe", "uptime && df -h")
    assert code == 0 and host.runs.calls == ["uptime && df -h"] and any(m.startswith("legacy: ") for m in host.logged)
    code, out, err = host("enforce", "uptime && df -h")
    assert code == 126 and len(host.runs.calls) == 1 and "agent-exec-gate run" in err
    for mode in ("observe", "enforce"):
        code, out, err = host(mode, "")
        assert code == 126 and len(host.runs.calls) == 1


def test_snapshot_word_runs_the_window_script_in_both_modes(host):
    for mode in ("observe", "enforce"):
        code, out, err = host(mode, "agent-exec-gate snapshot 0123456789abcdef")
        assert (code, err) == (0, ""), err
        script = host.runs.calls[-1]
        assert "/usr/local/bin/agent-sec-snapshot" in script and "ss -tlnpu" in script and "wg show all allowed-ips" in script
        assert "@@kd-window-0123456789abcdef snapshot rc=%s" in script and "@@kd-window-0123456789abcdef wgpeers rc=%s" in script
    assert len(host.runs.calls) == 2
    for bad in ("agent-exec-gate snapshot", "agent-exec-gate snapshot ../x", "agent-exec-gate snapshot 0123456789abcdef; id"):
        code, out, err = host("enforce", bad)
        assert code == 126 and len(host.runs.calls) == 2


def test_action_runs_once(host, owner):
    raw = json.dumps(signed_action(owner)).encode("utf-8")
    code, out, err = host("enforce", gate.RUN_WORD, raw)
    assert (code, err) == (0, "") and host.runs.calls == [ACTION["command"]]
    once = hashlib.sha256((D2 + "|" + NONCE).encode("ascii")).hexdigest()  # the signed decision, not the action_id
    assert (host.paths[2] / once).exists() and not (host.paths[2] / "0f4d9c7e-1111-4222-8333-444455556666").exists()
    code, out, err = host("enforce", gate.RUN_WORD, raw)
    assert code == 126 and "уже выполнено" in err and len(host.runs.calls) == 1
    code, out, err = host("observe", gate.RUN_WORD, raw)  # observe enforces once-only too
    assert code == 126 and "уже выполнено" in err and len(host.runs.calls) == 1


def test_action_replay_with_a_new_action_id_is_refused_by_main(host, owner):
    # review C2, through main: enforce with a rewritten action_id, then observe with the signature stripped —
    # the once key is the signed decision's in both paths
    env = signed_action(owner)
    code, out, err = host("enforce", gate.RUN_WORD, json.dumps(env).encode("utf-8"))
    assert (code, err) == (0, "")
    env["grant"]["action_id"] = "fresh-id-2"
    code, out, err = host("enforce", gate.RUN_WORD, json.dumps(env).encode("utf-8"))
    assert code == 126 and "уже выполнено" in err and len(host.runs.calls) == 1
    env["assertion"] = None
    code, out, err = host("observe", gate.RUN_WORD, json.dumps(env).encode("utf-8"))
    assert code == 126 and "уже выполнено" in err and len(host.runs.calls) == 1
    assert any(m.startswith("observe FAIL (") for m in host.logged)


def test_unwritable_used_dir_refuses(host, owner, tmp_path):
    blocker = tmp_path / "used"
    blocker.write_text("not a directory")
    raw = json.dumps(signed_action(owner)).encode("utf-8")
    code, out, err = host("enforce", gate.RUN_WORD, raw)
    assert code == 126 and host.runs.calls == [] and "отказ" in err


# --- a signed plan's steps: once each, in order (KD-9483) ----------------------------------------------------------

def step_key(step, proposal=PROPOSAL):
    return gate.plan_key(D1, NONCE, plan_payload(proposal)["proposal_sha256"], step)


def steps_done(host, *steps):
    """The gate's journal says these steps of PROPOSAL succeeded on this server (a test of a later step)."""
    host.paths[2].mkdir(exist_ok=True)
    for step in steps:
        gate.finish_step(str(host.paths[2]), step_key(step), 0)


def test_the_plan_key_mirrors_the_stage():
    psha = plan_payload()["proposal_sha256"]
    assert gate.plan_key(D1, NONCE, psha, 2) == exec_envelope.plan_key(D1, NONCE, psha, 2) != step_key(1)
    assert re.fullmatch(r"[0-9a-f]{64}", step_key(1)) and step_key(1) != gate.once_key(decision_dict())
    for bad in (("", NONCE, psha, 1), (D1, "", psha, 1), (D1, NONCE, "x" * 64, 1), (D1, NONCE, psha, 0),
                (D1, NONCE, psha, True), (None, NONCE, psha, 1)):
        assert gate.plan_key(*bad) == "" == exec_envelope.plan_key(*bad)


@pytest.mark.parametrize("mode", ["enforce", "observe"])
def test_a_plan_step_that_succeeded_is_not_run_again(host, owner, mode):
    raw = json.dumps(signed_plan(owner, 1)).encode("utf-8")
    assert host(mode, gate.RUN_WORD, raw)[0] == 0
    assert (host.paths[2] / step_key(1)).read_text("utf-8").startswith("ok ")
    for again in ("enforce", "observe"):
        code, out, err = host(again, gate.RUN_WORD, raw)
        assert code == 126 and "шаг 1 этого плана уже выполнен" in err and host.runs.calls == [SCRIPT]
    stripped = signed_plan(owner, 1)
    stripped["assertion"] = None  # observe lets an unchecked signature through, never a second run
    code, out, err = host("observe", gate.RUN_WORD, json.dumps(stripped).encode("utf-8"))
    assert code == 126 and host.runs.calls == [SCRIPT]


def test_a_failed_plan_step_may_be_run_again(host, owner):
    raw = json.dumps(signed_plan(owner, 1)).encode("utf-8")
    host.runs.code = 1
    assert host("enforce", gate.RUN_WORD, raw)[0] == 1
    assert (host.paths[2] / step_key(1)).read_text("utf-8").startswith("rc 1 ")
    host.runs.code = 0
    assert host("enforce", gate.RUN_WORD, raw)[0] == 0  # the retry after a failure
    assert host("enforce", gate.RUN_WORD, raw)[0] == 126  # and no more once it succeeded
    assert host.runs.calls == [SCRIPT, SCRIPT]


def test_a_plan_step_waits_for_the_earlier_steps_of_its_server(host, owner):
    second = json.dumps(signed_plan(owner, 2)).encode("utf-8")
    code, out, err = host("enforce", gate.RUN_WORD, second)
    assert code == 126 and "шаг 1 этого плана" in err and "не выполнен" in err and host.runs.calls == []
    assert not (host.paths[2] / step_key(2)).exists()
    host.runs.code = 1
    host("enforce", gate.RUN_WORD, json.dumps(signed_plan(owner, 1)).encode("utf-8"))
    assert host("enforce", gate.RUN_WORD, second)[0] == 126  # a failed step 1 does not open step 2
    host.runs.code = 0
    host("enforce", gate.RUN_WORD, json.dumps(signed_plan(owner, 1)).encode("utf-8"))
    assert host("enforce", gate.RUN_WORD, second)[0] == 0
    third = json.dumps(signed_plan(owner, 3)).encode("utf-8")
    assert host("enforce", gate.RUN_WORD, third)[0] == 0


def test_another_servers_step_is_not_this_gates_to_wait_for(host, owner):
    # step 1 runs on node-g: its order is the executor's to check (agent-exec-gate step there)
    proposal = dict(PROPOSAL, operations=[PROPOSAL["operations"][3], PROPOSAL["operations"][0]])
    code, out, err = host("enforce", gate.RUN_WORD, json.dumps(signed_plan(owner, 2, proposal=proposal)).encode("utf-8"))
    assert (code, err) == (0, "") and host.runs.calls == [SCRIPT]


def test_the_step_word_says_whether_a_step_succeeded(host, owner):
    for mode in ("enforce", "observe"):
        assert host(mode, "agent-exec-gate step " + step_key(1)) == (0, "no\n", "")
    host("enforce", gate.RUN_WORD, json.dumps(signed_plan(owner, 1)).encode("utf-8"))
    for mode in ("enforce", "observe"):
        assert host(mode, "agent-exec-gate step " + step_key(1)) == (0, "ok\n", "")
    assert host("enforce", "agent-exec-gate step " + step_key(2))[1] == "no\n"
    for bad in ("agent-exec-gate step", "agent-exec-gate step ../x", "agent-exec-gate step " + step_key(1) + " x",
                "agent-exec-gate step " + step_key(1).upper()):
        code, out, err = host("observe", bad)
        assert code == 126 and out == ""
    assert host.runs.calls == [SCRIPT]


def test_a_test_zone_step_keeps_no_marks(host, envs_file):
    raw = json.dumps(unsigned(zone_proposal(zone_op("app.tool", Z_SPARK)))).encode("utf-8")
    assert host("enforce", gate.RUN_WORD, raw)[0] == 0
    assert host("enforce", gate.RUN_WORD, raw)[0] == 0  # no owner's signature to spend: the factory host's rule
    assert not host.paths[2].exists() or not any(host.paths[2].iterdir())


def test_a_plan_step_with_an_unreadable_journal_is_refused(host, owner, tmp_path):
    (tmp_path / "used").write_text("not a directory")
    code, out, err = host("enforce", gate.RUN_WORD, json.dumps(signed_plan(owner, 1)).encode("utf-8"))
    assert code == 126 and host.runs.calls == [] and "журнал" in err


@pytest.mark.skipif(os.name != "posix", reason="flock")
def test_a_plan_runs_one_step_at_a_time_on_a_server(host, owner, tmp_path):
    import fcntl

    psha = plan_payload()["proposal_sha256"]
    (tmp_path / "used").mkdir()
    with open(str(tmp_path / "used" / gate.plan_lock(D1, NONCE, psha)), "w") as held:  # another run holds the plan
        fcntl.flock(held, fcntl.LOCK_EX)
        code, out, err = host("enforce", gate.RUN_WORD, json.dumps(signed_plan(owner, 1)).encode("utf-8"))
    assert code == 126 and "уже выполняется" in err and host.runs.calls == []
    assert host("enforce", gate.RUN_WORD, json.dumps(signed_plan(owner, 1)).encode("utf-8"))[0] == 0


def test_stale_or_missing_gate_files_refuse(host, owner):
    raw = json.dumps(signed_plan(owner, 1)).encode("utf-8")
    host.paths[1].unlink()
    code, out, err = host("enforce", gate.RUN_WORD, raw)
    assert code == 126 and host.runs.calls == [] and "signers" in err
    host.paths[1].write_text("{broken", encoding="utf-8")
    code, out, err = host("observe", gate.RUN_WORD, raw)
    assert code == 0 and host.runs.calls == [SCRIPT]  # observe: logged, run anyway
    host.paths[0].write_text(json.dumps({**GATE, "mode": "maybe"}), encoding="utf-8")
    code = gate.main([], environ={"SSH_ORIGINAL_COMMAND": "uptime"}, stdin=io.BytesIO(b""), out=io.BytesIO(),
                     err=io.BytesIO(), now=lambda: NOW, verify_sig=crypto_verify)
    assert code == 126 and len(host.runs.calls) == 1


def test_oversized_stdin_is_refused(host, owner):
    env = signed_plan(owner, 1)
    env["proposal"]["intent"] = "x" * gate.MAX_ENVELOPE
    code, out, err = host("enforce", gate.RUN_WORD, json.dumps(env).encode("utf-8"))
    assert code == 126 and host.runs.calls == [] and "слишком большой" in err


def test_check_prints_mode_alias_signers(host, owner):
    host.paths[0].write_text(json.dumps(GATE), encoding="utf-8")
    out, err = io.BytesIO(), io.BytesIO()
    assert gate.main(["--check"], environ={}, stdin=io.BytesIO(b""), out=out, err=err) == 0
    text = out.getvalue().decode("utf-8")
    assert text == ("mode: enforce\nalias: node-c\nsigners: 1\nopenssl: OpenSSL 3.0.13\npolicy: ok (%s)\n"
                    "test-envs: 0\n") % POLICY_FILE
    out = io.BytesIO()
    assert gate.main(["--check-signers"], environ={}, stdin=io.BytesIO(b""), out=out, err=err) == 0
    assert out.getvalue().decode("utf-8") == "signers: 1 (owner: Chrome ES256)\n"
    host.paths[1].write_text(json.dumps({**owner.registry(), "signers": [
        {**owner.registry()["signers"][0], "public_key": "AAAA"}]}), encoding="utf-8")
    assert gate.main(["--check-signers"], environ={}, stdin=io.BytesIO(b""), out=io.BytesIO(), err=err) == 1
    host.paths[1].unlink()
    assert gate.main(["--check"], environ={}, stdin=io.BytesIO(b""), out=io.BytesIO(), err=err) == 1
    assert "signers" in err.getvalue().decode("utf-8")


def test_check_signers_reads_a_named_file_before_the_gate_is_configured(host, owner, tmp_path):
    """The installer checks a registry with the gate's own reader before it lands — on a target that has no
    gate.json yet, so the named file is the only thing read."""
    assert not host.paths[0].exists()
    candidate = tmp_path / "candidate.json"
    candidate.write_text(json.dumps(owner.registry()), encoding="utf-8")
    out, err = io.BytesIO(), io.BytesIO()
    assert gate.main(["--check-signers", str(candidate)], environ={}, stdin=io.BytesIO(b""), out=out, err=err) == 0
    assert out.getvalue().decode("utf-8") == "signers: 1 (owner: Chrome ES256)\n"
    candidate.write_text(json.dumps({"schema_version": 2, "signers": []}), encoding="utf-8")
    assert gate.main(["--check-signers", str(candidate)], environ={}, stdin=io.BytesIO(b""), out=io.BytesIO(),
                     err=err) == 1
    assert "повреждён" in err.getvalue().decode("utf-8")
    assert gate.main(["--check-signers", "a", "b"], environ={}, stdin=io.BytesIO(b""), out=io.BytesIO(),
                     err=io.BytesIO()) == 2


def test_take_once_names_only_safe_ids(tmp_path):
    used = tmp_path / "used"
    gate.take_once(str(used), "abc-123")
    assert (used / "abc-123").exists()
    with pytest.raises(gate.Deny, match="уже выполнено"):
        gate.take_once(str(used), "abc-123")
    for bad in ("../x", "a/b", "", "x" * 65):
        with pytest.raises(gate.Deny):
            gate.take_once(str(used), bad)


@pytest.mark.skipif(os.name != "posix", reason="process groups and bash")
def test_run_executes_bash_and_reports_the_exit_code(tmp_path):
    marker = tmp_path / "ran"
    assert gate.run("touch %s && exit 3" % marker, True) == 3 and marker.exists()
    started = time.monotonic()
    assert gate.run("sleep 30", True, timeout=0.5) == 124 and time.monotonic() - started < 10


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not on PATH")
@pytest.mark.parametrize("make", [make_es256, make_rs256])
def test_openssl_verify_round_trip(make):
    cose, key = make()
    pem, _ = gate.cose_to_pem(cose)
    data = b"authenticator data" + hashlib.sha256(b"client data").digest()
    signature = sign_with(key, data)
    assert gate.openssl_verify(pem, data, signature) is True
    assert gate.openssl_verify(pem, data + b"x", signature) is False
    assert gate.openssl_verify(pem, data, signature[:-1]) is False


# --- the policy ----------------------------------------------------------------------------------------

GUARD = "шлюз и доступ к серверу меняются только с машины владельца (deploy_target.sh)"


def plan_with(script, extra_ops=()):
    first = dict(PROPOSAL["operations"][0], script=script)
    return dict(PROPOSAL, operations=[first, *extra_ops])


@pytest.mark.parametrize("mode", ["observe", "enforce"])
@pytest.mark.parametrize("script", [
    "echo x > /etc/sudoers.d/x",
    "cp /tmp/k /root/.ssh/authorized_keys",
    "rm -f /usr/local/bin/agent-exec-gate",
    "useradd -m bob",
    "python3 -c 'import os'",
    "tar -xf /tmp/x.tar -C /",
])
def test_rule1_refuses_a_signed_plan_step_in_both_modes(host, owner, mode, script):
    raw = json.dumps(signed_plan(owner, 1, proposal=plan_with(script))).encode("utf-8")
    code, out, err = host(mode, gate.RUN_WORD, raw)
    assert code == 126 and host.runs.calls == [] and GUARD in err, err
    assert any(m.startswith("deny policy (") and "KD-123 plan step 1" in m for m in host.logged)


@pytest.mark.parametrize("mode", ["observe", "enforce"])
def test_rule1_holds_for_an_unsigned_envelope_in_observe_too(host, owner, mode):
    # the installer's self-check: an envelope with no signature at all, refused by the policy, not run in observe
    env = signed_plan(owner, 1, proposal=plan_with("echo x > /etc/sudoers.d/x"))
    env["assertion"] = None
    code, out, err = host(mode, gate.RUN_WORD, json.dumps(env).encode("utf-8"))
    assert code == 126 and host.runs.calls == [] and GUARD in err
    assert not any(m.startswith("observe FAIL") for m in host.logged)


@pytest.mark.parametrize("mode", ["observe", "enforce"])
def test_rule2_refuses_a_critical_plan_step_signed_as_a_plan(host, owner, mode):
    raw = json.dumps(signed_plan(owner, 1, proposal=plan_with("set -e\nreboot\n"))).encode("utf-8")
    code, out, err = host(mode, gate.RUN_WORD, raw)
    assert code == 126 and host.runs.calls == []
    assert "шаг 1 критичный (перезагрузка или остановка): владелец подписывает его отдельно" in err
    assert any(m.startswith("deny policy (шаг 1 критичный") for m in host.logged)


def test_rule2_reads_the_site_units_of_gate_json(host, owner):
    raw = json.dumps(signed_plan(owner, 1, proposal=plan_with("systemctl stop app-worker.service"))).encode("utf-8")
    code, out, err = host("enforce", gate.RUN_WORD, raw)  # no list: every unit's stop is critical
    assert code == 126 and "критичный" in err
    code, out, err = host("enforce", gate.RUN_WORD, raw, critical_units=["site-*"])
    assert (code, err) == (0, "") and host.runs.calls == ["systemctl stop app-worker.service"]
    code, out, err = host("enforce", gate.RUN_WORD, raw, critical_units=["app-*"])
    assert code == 126 and len(host.runs.calls) == 1
    code, out, err = host("enforce", gate.RUN_WORD, raw, critical_units="app-*")  # malformed: every unit
    assert code == 126 and len(host.runs.calls) == 1


def test_typed_steps_are_classified_too(host, owner):
    steps_done(host, 1)
    raw = json.dumps(signed_plan(owner, 2)).encode("utf-8")  # systemctl restart nginx.service: ordinary
    code, out, err = host("enforce", gate.RUN_WORD, raw)
    assert (code, err) == (0, "") and host.runs.calls == [["systemctl", "restart", "nginx.service"]]
    seen = []
    policy = gate.load_policy()
    original = policy.plan_step

    def spy(step, command, units):
        seen.append((step, command, units))
        return original(step, command, units)

    policy.plan_step = spy
    try:
        host("enforce", gate.RUN_WORD, raw, critical_units=["app-*"])
    finally:
        policy.plan_step = original
    assert seen == [(2, "systemctl restart nginx.service", ("app-*",))]


def test_rule1_refuses_a_signed_action_before_it_is_marked_run(host, owner):
    env = signed_action(owner, command="usermod -aG sudo bob")  # not the signed command either
    code, out, err = host("observe", gate.RUN_WORD, json.dumps(env).encode("utf-8"))
    assert code == 126 and host.runs.calls == [] and "команда usermod" in err
    assert not host.paths[2].exists() or not any(host.paths[2].iterdir())  # the once-mark is not spent


@pytest.mark.parametrize("mode", ["observe", "enforce"])
def test_rule1_refuses_a_legacy_command_in_both_modes(host, mode):
    code, out, err = host(mode, "echo 'bob ALL=(ALL) ALL' >> /etc/sudoers")
    assert code == 126 and host.runs.calls == [] and GUARD in err
    assert any(m.startswith("deny policy (") and "legacy" in m for m in host.logged)
    code, out, err = host(mode, "reboot")  # rule 2 does not read a legacy command (no kind to match)
    assert (code == 0) == (mode == "observe")


def test_a_policy_that_does_not_load_refuses_everything_but_the_snapshot(host, owner, monkeypatch, tmp_path):
    monkeypatch.setattr(gate, "POLICY_PATH", str(tmp_path / "missing"))
    raw = json.dumps(signed_plan(owner, 1)).encode("utf-8")
    for mode in ("observe", "enforce"):
        code, out, err = host(mode, gate.RUN_WORD, raw)
        assert code == 126 and "политика шлюза" in err
        code, out, err = host(mode, "uptime")
        assert code == 126
    assert host.runs.calls == []
    code, out, err = host("enforce", "agent-exec-gate snapshot 0123456789abcdef")
    assert code == 0 and len(host.runs.calls) == 1
    (tmp_path / "broken").write_text("def (", encoding="utf-8")
    monkeypatch.setattr(gate, "POLICY_PATH", str(tmp_path / "broken"))
    assert host("observe", "uptime")[0] == 126
    out = io.BytesIO()
    assert gate.main(["--check"], environ={}, stdin=io.BytesIO(b""), out=out, err=io.BytesIO()) == 1
    assert "policy: MISSING" in out.getvalue().decode("utf-8")


def test_the_policy_is_compiled_from_source_not_a_cached_pyc(monkeypatch, tmp_path):
    source = tmp_path / "agent-exec-policy"
    source.write_text(POLICY_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    module = gate.load_policy(str(source))
    assert module.protect("useradd x") and gate.load_policy(str(source)) is module
    assert not (tmp_path / "__pycache__").exists()


VERMIN = "import sys; from vermin.main import main; sys.argv[0] = 'vermin'; main()"


@pytest.mark.skipif(importlib.util.find_spec("vermin") is None, reason="vermin not installed (requirements-dev.txt)")
@pytest.mark.parametrize("source", [GATE_FILE, POLICY_FILE,  # the policy runs on the target too
                                    GATE_FILE.with_name("agent_typed_op.py")])  # and the typed helper
def test_exec_gate_source_is_python36_compatible(source):
    proc = subprocess.run([PY, "-c", VERMIN, "--no-tips", str(source)], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True)
    match = re.search(r"Minimum required versions:.*?3\.(\d+)", proc.stdout)
    assert match and int(match.group(1)) <= 6, proc.stdout + proc.stderr


@pytest.fixture(autouse=True)
def _accounts(monkeypatch):
    """The target's accounts as the gate looks them up (pwd): the application's, and a second name of uid 0."""
    monkeypatch.setattr(gate, "account_uid", {"www-data": 33, "toor": 0}.get)


# --- app steps carry their argv in the signed plan ------------------------------------------------------

APP_ARGV = [
    "runuser",
    "-u",
    "www-data",
    "--",
    "env",
    "-C",
    "/var/www/app.example.test",
    "./venv/bin/python",
    "tools/admin.py",
    "set-type",
    "--partner-id",
    "6",
]


def test_an_app_step_runs_exactly_the_argv_of_the_signed_plan():
    op = {"verb": "app.tool", "app": "app-staging", "argv": APP_ARGV}
    assert gate.expected_argv(op, "apt") == APP_ARGV
    gate.check_step_command(op, gate.quote_argv(APP_ARGV))
    with pytest.raises(gate.Deny, match="команда"):
        gate.check_step_command(op, gate.quote_argv(APP_ARGV + ["--force"]))
    flag = {
        "verb": "app.flag",
        "argv": APP_ARGV[:7]
        + [
            "python3",
            "-I",
            "/usr/local/libexec/agent-app-flag",
            "config.ini",
            "partner",
            "clients_enabled",
            "1",
        ],
    }
    gate.check_step_command(flag, gate.quote_argv(flag["argv"]))


@pytest.mark.parametrize(
    "argv",
    [
        None,
        [],
        "runuser -u www-data -- id",
        APP_ARGV + [6],
        APP_ARGV + [""],
        ["runuser", "-u", "root", "--", "id"],  # an app never runs as root
        ["id"],
        ["runuser", "-u", "www-data"],
        ["sudo", "-u", "www-data", "--", "id"],
        ["runuser", "-u", "www-data", "--"],  # no command behind the prefix
    ],
)
def test_an_app_step_without_a_proper_argv_is_refused(argv):
    with pytest.raises(gate.Deny, match="шаге приложения"):
        gate.expected_argv({"verb": "app.tool", "argv": argv}, "apt")


def test_service_reload_is_checked_like_restart():
    op = {"verb": "service.reload", "unit": "nginx.service"}
    assert gate.expected_argv(op, "apt") == ["systemctl", "reload", "nginx.service"]
    gate.check_step_command(op, "systemctl reload nginx.service")
    with pytest.raises(gate.Deny, match="команда"):
        gate.check_step_command(op, "systemctl restart nginx.service")
    with pytest.raises(gate.Deny, match="юнит"):
        gate.expected_argv({"verb": "service.reload"}, "apt")


def test_a_verb_the_gate_does_not_know_is_refused():
    with pytest.raises(gate.Deny, match="неизвестный глагол"):
        gate.expected_argv({"verb": "app.shell", "argv": APP_ARGV}, "apt")


FLAG_ARGV = APP_ARGV[:7] + ["python3", "-I", "/usr/local/libexec/agent-app-flag", "config.ini", "partner",
                            "clients_enabled", "1"]


def _swap(argv, index, word):
    return argv[:index] + [word] + argv[index + 1:]


@pytest.mark.parametrize("verb, argv", [
    # the final review: the signed words must have the shape of an app step — the application's account, its
    # directory through env -C, then the interpreter and the tool; nothing else passes as «a step of an application»
    ("app.tool", _swap(APP_ARGV, 2, "toor")),  # another name of uid 0
    ("app.tool", _swap(APP_ARGV, 2, "nobody-here")),  # no such account on this server
    ("app.tool", _swap(APP_ARGV, 2, "-g")),
    ("app.tool", APP_ARGV[:4] + ["id"]),
    ("app.tool", APP_ARGV[:4] + ["bash", "-c", "id", "x", "y"]),
    ("app.tool", _swap(APP_ARGV, 5, "-i")),
    ("app.tool", _swap(APP_ARGV, 6, "var/www/app.example.test")),
    ("app.tool", _swap(APP_ARGV, 6, "/var/www/../etc")),
    ("app.tool", _swap(APP_ARGV, 6, "/var/www/./app")),
    ("app.tool", _swap(APP_ARGV, 7, "-S")),
    ("app.tool", _swap(APP_ARGV, 7, "PATH=/tmp")),
    ("app.tool", _swap(APP_ARGV, 8, "-c")),
    ("app.tool", APP_ARGV[:8]),  # an interpreter with no tool
    ("app.flag", APP_ARGV),  # a tool's words signed as a switch
    ("app.flag", _swap(FLAG_ARGV, 9, "/tmp/agent-app-flag")),
    ("app.flag", _swap(FLAG_ARGV, 7, "./venv/bin/python")),
    ("app.flag", FLAG_ARGV + ["extra"]),
    ("app.flag", FLAG_ARGV[:13]),
    ("app.flag", _swap(FLAG_ARGV, 10, "../config.ini")),
    ("app.flag", _swap(FLAG_ARGV, 12, "webhook_token")),
    ("app.flag", _swap(FLAG_ARGV, 13, "2")),
])
def test_an_app_step_of_another_shape_is_refused(verb, argv):
    with pytest.raises(gate.Deny, match="шаге приложения"):
        gate.expected_argv({"verb": verb, "argv": argv}, "apt")


def test_the_shapes_the_stage_forms_pass():
    assert gate.expected_argv({"verb": "app.tool", "argv": APP_ARGV}, "apt") == APP_ARGV
    assert gate.expected_argv({"verb": "app.tool", "argv": APP_ARGV[:9]}, "apt") == APP_ARGV[:9]  # no arguments
    assert gate.expected_argv({"verb": "app.flag", "argv": FLAG_ARGV}, "apt") == FLAG_ARGV
    bare = _swap(APP_ARGV, 7, "python3")
    assert gate.expected_argv({"verb": "app.tool", "argv": bare}, "apt") == bare


# --- the shapes of a PHP application's steps — spark under php, a switch of an env file --------------------
ENV_ARGV = APP_ARGV[:7] + ["python3", "-I", "/usr/local/libexec/agent-app-flag", "--env", ".env", "CI_ENVIRONMENT",
                           "development"]
SPARK_ARGV = APP_ARGV[:7] + ["php", "spark", "migrate", "--force"]


def test_the_shapes_of_a_php_application_pass():
    assert gate.expected_argv({"verb": "app.flag", "argv": ENV_ARGV}, "apt") == ENV_ARGV
    dotted = _swap(_swap(ENV_ARGV, 11, "env.local"), 12, "app.forceGlobalSecureRequests")
    assert gate.expected_argv({"verb": "app.flag", "argv": dotted}, "apt") == dotted
    assert gate.expected_argv({"verb": "app.tool", "argv": SPARK_ARGV}, "apt") == SPARK_ARGV
    gate.check_step_command({"verb": "app.tool", "argv": SPARK_ARGV}, gate.quote_argv(SPARK_ARGV))


@pytest.mark.parametrize("argv", [
    _swap(ENV_ARGV, 11, "../.env"),
    _swap(ENV_ARGV, 11, ".."),
    _swap(ENV_ARGV, 11, "sub/.env"),
    _swap(ENV_ARGV, 12, "CI ENVIRONMENT"),
    _swap(ENV_ARGV, 12, "--help"),
    _swap(ENV_ARGV, 13, "a;b"),
    _swap(ENV_ARGV, 13, "https://x.example.test"),
    _swap(ENV_ARGV, 13, "x" * 41),
    _swap(ENV_ARGV, 10, "--ini"),
    _swap(ENV_ARGV, 9, "/tmp/agent-app-flag"),
    ENV_ARGV + ["extra"],
    ENV_ARGV[:13],
])
def test_an_env_switch_of_another_shape_is_refused(argv):
    with pytest.raises(gate.Deny, match="шаге приложения"):
        gate.expected_argv({"verb": "app.flag", "argv": argv}, "apt")


# --- the server's own list of test environments — a listed step runs without the owner's signature --------

ZONE_DIR = "/var/www/shop.example.test"
ZONE_PREFIX = ["runuser", "-u", "www-data", "--", "env", "-C"]
HELPER = ["python3", "-I", "/usr/local/libexec/agent-app-flag"]
ENVS = {
    "schema_version": 1,
    "envs": {
        "app-staging": {
            "user": "www-data",
            "dir": "/var/www/app.example.test",
            "python": "./venv/bin/python",
            "tools": ["tools/admin.py"],
            "flags": {"file": "config.ini", "format": "ini", "sections": ["partner"]},
            "reload": ["app.service"],
        },
        "shop-test": {
            "user": "www-data",
            "dir": ZONE_DIR,
            "php": "php",
            "spark": ["migrate", "migrate:status"],
            "flags": {"file": ".env", "format": "env", "keys": {"CI_ENVIRONMENT": ["development", "production"]}},
        },
    },
}
Z_SPARK = ZONE_PREFIX + [ZONE_DIR, "php", "spark", "migrate", "--force"]
Z_ENV = ZONE_PREFIX + [ZONE_DIR] + HELPER + ["--env", ".env", "CI_ENVIRONMENT", "development"]
Z_TOOL = APP_ARGV
Z_INI = APP_ARGV[:7] + HELPER + ["config.ini", "partner", "clients_enabled", "1"]


def zone_op(verb, argv=None, **over):
    op = {"server": ALIAS, "verb": verb, "what": "Шаг стенда", "effect": "Стенд", "rollback": "Не нужен",
          "risk": {"level": "low", "why": "Тестовый стенд"}}
    if argv is not None:
        op.update(app="shop-test", argv=list(argv))
    op.update(over)
    return op


def zone_proposal(*operations):
    return {"schema_version": 2, "task_id": "KD-123", "intent": "Миграции стенда", "basis": "просьба dev",
            "operations": list(operations)}


def unsigned(proposal, step=1, command=None, **grant_over):
    """The envelope kd-admin-run builds from an approval by the test-zone rule: no signature, no assertion."""
    op = proposal["operations"][step - 1]
    if command is None:
        command = op["script"] if op["verb"] == "script" else gate.quote_argv(gate.expected_argv(op, "apt"))
    grant = plan_grant("", proposal, approved_by="rule:test-env", signer="", **grant_over)
    return json.loads(exec_envelope.build_plan(grant, proposal, step, op["server"], command))


def envs_of(data=ENVS):
    return gate.validate_test_envs(data)


def zone_verify(envelope, envs=None, now=NOW):
    return gate.verify(envelope, GATE, {"schema_version": 1, "signers": []}, now, verify_sig=crypto_verify,
                       test_envs=envs_of() if envs is None else envs)


@pytest.mark.parametrize("op, name", [
    (zone_op("app.tool", Z_SPARK), "shop-test"),
    (zone_op("app.tool", Z_SPARK[:10]), "shop-test"),
    (zone_op("app.flag", Z_ENV), "shop-test"),
    (zone_op("app.tool", Z_TOOL), "app-staging"),
    (zone_op("app.flag", Z_INI), "app-staging"),
    (zone_op("service.reload", unit="app.service"), "app-staging"),
])
def test_a_step_that_fits_an_entry_word_for_word_is_of_that_environment(op, name):
    assert gate.test_env(op, envs_of()) == name


@pytest.mark.parametrize("op", [
    zone_op("app.tool", _swap(Z_SPARK, 9, "db:wipe")),  # a spark command the entry does not list
    zone_op("app.tool", _swap(Z_SPARK, 8, "artisan")),
    zone_op("app.tool", _swap(Z_SPARK, 7, "php8.1")),  # another interpreter
    zone_op("app.tool", Z_SPARK[:9]),  # php spark with no command
    zone_op("app.tool", _swap(Z_SPARK, 2, "deploy")),  # another account
    zone_op("app.tool", _swap(Z_SPARK, 6, ZONE_DIR + "/sub")),  # a directory with a suffix
    zone_op("app.tool", _swap(Z_SPARK, 6, ZONE_DIR + "2")),
    zone_op("app.tool", Z_SPARK + ["a;b"]),  # a word outside the argument pattern
    zone_op("app.tool", Z_SPARK + ["$(id)"]),
    zone_op("app.tool", _swap(Z_TOOL, 8, "tools/other.py")),  # a tool the entry does not list
    zone_op("app.tool", _swap(Z_TOOL, 7, "python3")),
    zone_op("app.tool", ZONE_PREFIX + [ZONE_DIR, "./venv/bin/python", "tools/admin.py"]),  # a tool of another entry
    zone_op("app.flag", _swap(Z_ENV, 13, "testing")),  # a value the entry does not list
    zone_op("app.flag", _swap(Z_ENV, 12, "encryption.key")),  # a key the entry does not list
    zone_op("app.flag", _swap(Z_ENV, 11, "other.env")),
    zone_op("app.flag", _swap(Z_INI, 11, "internal")),  # a section the entry does not list
    zone_op("app.flag", _swap(Z_INI, 10, "other.ini")),
    zone_op("app.flag", _swap(Z_INI, 13, "2")),
    zone_op("app.flag", Z_SPARK),  # a tool's words under the flag verb
    zone_op("app.tool", Z_ENV),  # the helper is no tool of an entry
    zone_op("service.reload", unit="nginx.service"),  # a unit no entry lists
    zone_op("service.restart", unit="app.service"),  # only a reload is of the zone
    zone_op("service.start", unit="app.service"),
    zone_op("script", script="php spark migrate"),
    zone_op("package.refresh"),
    zone_op("app.tool"),  # no argv at all
    zone_op("app.tool", ["runuser"]),
    {"verb": "app.tool", "argv": "runuser -u www-data"},
])
def test_a_step_outside_the_list_is_of_no_environment(op):
    assert gate.test_env(op, envs_of()) is None
    assert gate.test_env(zone_op("app.tool", Z_SPARK), {}) is None  # no list — no zone


def test_a_listed_step_verifies_without_a_signature_in_enforce():
    proposal = zone_proposal(zone_op("app.tool", Z_SPARK), zone_op("service.reload", unit="app.service"))
    checked = zone_verify(unsigned(proposal, 1))
    assert checked["test_env"] == "shop-test" and checked["command"] == "runuser -u www-data -- env -C " \
        + ZONE_DIR + " php spark migrate --force" and checked["step"] == 1
    assert zone_verify(unsigned(proposal, 2))["test_env"] == "app-staging"


def test_without_the_list_an_unsigned_step_is_refused_as_before():
    proposal = zone_proposal(zone_op("app.tool", Z_SPARK))
    for envs in ({}, None):
        with pytest.raises(gate.Deny, match="подпись владельца passkey"):
            gate.verify(unsigned(proposal), GATE, {"schema_version": 1, "signers": []}, NOW,
                        verify_sig=crypto_verify, test_envs=envs)
    with pytest.raises(gate.Deny, match="подпись владельца passkey"):
        gate.verify(unsigned(proposal), GATE, {"schema_version": 1, "signers": []}, NOW, verify_sig=crypto_verify)


@pytest.mark.parametrize("op", [
    zone_op("app.tool", _swap(Z_SPARK, 9, "db:wipe")),
    zone_op("app.flag", _swap(Z_ENV, 13, "testing")),
    zone_op("service.reload", unit="nginx.service"),
    zone_op("service.restart", unit="app.service"),
    zone_op("script", script="php spark migrate"),
])
def test_an_unsigned_step_outside_the_list_is_refused(op):
    with pytest.raises(gate.Deny, match="подпись владельца passkey"):
        zone_verify(unsigned(zone_proposal(op)))


def test_one_listed_step_does_not_carry_its_neighbour():
    proposal = zone_proposal(zone_op("app.tool", Z_SPARK), zone_op("service.restart", unit="app.service"))
    assert zone_verify(unsigned(proposal, 1))["test_env"] == "shop-test"
    with pytest.raises(gate.Deny, match="подпись владельца passkey"):
        zone_verify(unsigned(proposal, 2))


def test_a_doctored_unsigned_envelope_is_still_refused():
    proposal = zone_proposal(zone_op("app.tool", Z_SPARK))
    with pytest.raises(gate.Deny, match="команда"):  # another command than the step's
        zone_verify(unsigned(proposal, command=gate.quote_argv(Z_SPARK + ["--all"])))
    env = unsigned(proposal)
    env["proposal"]["operations"][0]["argv"] = _swap(Z_SPARK, 9, "migrate:status")  # the plan, not its hash
    with pytest.raises(gate.Deny, match="proposal"):
        zone_verify(env)
    other = unsigned(proposal)
    other["server"] = "node-g"
    with pytest.raises(gate.Deny, match="для сервера"):
        zone_verify(other)
    step = unsigned(proposal)
    step["step"] = 2
    with pytest.raises(gate.Deny, match="шага с таким номером"):
        zone_verify(step)
    root = zone_proposal(zone_op("app.tool", _swap(Z_SPARK, 2, "toor")))  # uid 0 under another name
    with pytest.raises(gate.Deny):
        zone_verify(unsigned(root, command=gate.quote_argv(_swap(Z_SPARK, 2, "toor"))),
                    envs=envs_of({"schema_version": 1, "envs": {"x": dict(ENVS["envs"]["shop-test"], user="toor")}}))


def test_an_unsigned_listed_step_has_no_term_the_gate_could_check():
    # the times of an unsigned grant are attested by nothing: the factory host keeps them (kd-admin-run)
    proposal = zone_proposal(zone_op("app.tool", Z_SPARK))
    old = unsigned(proposal, approved_at=NOW_DT - timedelta(days=30), expires_at=NOW_DT - timedelta(days=29))
    assert zone_verify(old)["test_env"] == "shop-test"


def test_a_signed_plan_of_a_test_environment_is_checked_as_a_signed_plan(owner):
    proposal = zone_proposal(zone_op("app.tool", Z_SPARK))
    command = gate.quote_argv(Z_SPARK)
    checked = gate.verify(signed_plan(owner, 1, proposal=proposal, command=command), GATE, owner.registry(), NOW,
                          verify_sig=crypto_verify, test_envs=envs_of())
    assert checked["test_env"] == "" and checked["command"] == command
    expired = signed_plan(owner, 1, proposal=proposal, command=command,
                          approved_at=NOW_DT - timedelta(days=3), expires_at=NOW_DT - timedelta(days=2))
    with pytest.raises(gate.Deny, match="истекло"):
        gate.verify(expired, GATE, owner.registry(), NOW, verify_sig=crypto_verify, test_envs=envs_of())


# --- the list file --------------------------------------------------------------------------------------------------
@pytest.fixture
def envs_file(tmp_path, monkeypatch):
    path = tmp_path / "test-envs.json"
    path.write_text(json.dumps(ENVS), encoding="utf-8")
    if os.name == "posix":
        path.chmod(0o644)
        monkeypatch.setattr(gate, "TEST_ENVS_OWNER", os.getuid())
    monkeypatch.setattr(gate, "TEST_ENVS_JSON", str(path))
    return path


def test_the_list_loads_from_roots_plain_file(envs_file, host):
    assert sorted(gate.load_test_envs()) == ["app-staging", "shop-test"]
    assert gate.load_test_envs(str(envs_file))["shop-test"]["spark"] == ["migrate", "migrate:status"]
    assert host.logged == []


def test_no_list_file_is_no_zone_and_no_warning(tmp_path, monkeypatch, host):
    monkeypatch.setattr(gate, "TEST_ENVS_JSON", str(tmp_path / "none.json"))
    assert gate.load_test_envs() == {} and host.logged == []


@pytest.mark.parametrize("content", [
    "{not json",
    "[]",
    json.dumps({"schema_version": 2, "envs": {}}),
    json.dumps({"schema_version": 1, "envs": []}),
    json.dumps({"schema_version": 1, "envs": {"x": {"user": "root", "dir": "/var/www/x", "php": "php",
                                                    "spark": ["migrate"]}}}),
    json.dumps({"schema_version": 1, "envs": {"x": {"user": "www-data", "dir": "/var/www/../etc", "php": "php",
                                                    "spark": ["migrate"]}}}),
    json.dumps({"schema_version": 1, "envs": {"x": {"user": "www-data", "dir": "/var/www/x", "php": "/bin/sh",
                                                    "spark": ["migrate"]}}}),
    json.dumps({"schema_version": 1, "envs": {"x": {"user": "www-data", "dir": "/var/www/x", "php": "php",
                                                    "spark": ["migrate"], "extra": 1}}}),
    json.dumps({"schema_version": 1, "envs": {"x": {"user": "www-data", "dir": "/var/www/x", "php": "php",
                                                    "spark": "migrate"}}}),
    json.dumps({"schema_version": 1, "envs": {"x": {"user": "www-data", "dir": "/var/www/x", "python": "python3",
                                                    "tools": ["../x.py"]}}}),
    json.dumps({"schema_version": 1, "envs": {"x": {"user": "www-data", "dir": "/var/www/x", "php": "php",
                                                    "spark": ["migrate"], "reload": ["nginx"]}}}),
    json.dumps({"schema_version": 1, "envs": {"x": {"user": "www-data", "dir": "/var/www/x", "php": "php",
                                                    "spark": ["migrate"],
                                                    "flags": {"file": ".env", "format": "env",
                                                              "keys": {"A": ["b c"]}}}}}),
    json.dumps({"schema_version": 1, "envs": {"Bad Name": ENVS["envs"]["shop-test"]}}),
    json.dumps({"schema_version": 1, "envs": {"x": {"user": "www-data", "dir": "/var/www/x"}}}),
])
def test_a_malformed_list_is_no_list_at_all(envs_file, host, content):
    envs_file.write_text(content, encoding="utf-8")
    assert gate.load_test_envs() == {}
    assert len(host.logged) == 1 and host.logged[0].startswith("test-envs ignored (")


@pytest.mark.skipif(os.name != "posix", reason="POSIX owners and modes")
def test_a_list_others_could_write_is_ignored(envs_file, host, monkeypatch):
    envs_file.chmod(0o666)
    assert gate.load_test_envs() == {} and "test-envs ignored" in host.logged[-1]
    envs_file.chmod(0o664)
    assert gate.load_test_envs() == {}
    envs_file.chmod(0o644)
    monkeypatch.setattr(gate, "TEST_ENVS_OWNER", os.getuid() + 1)  # not root's file
    assert gate.load_test_envs() == {}


def test_a_list_behind_a_link_is_ignored(envs_file, host, tmp_path, monkeypatch):
    link = tmp_path / "link.json"
    try:
        link.symlink_to(envs_file)
    except (OSError, NotImplementedError):
        pytest.skip("no symlinks here")
    monkeypatch.setattr(gate, "TEST_ENVS_JSON", str(link))
    assert gate.load_test_envs() == {} and "test-envs ignored" in host.logged[-1]


# --- through main ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["enforce", "observe"])
def test_main_runs_a_listed_step_unsigned_and_says_so(host, envs_file, mode):
    proposal = zone_proposal(zone_op("app.tool", Z_SPARK))
    code, out, err = host(mode, gate.RUN_WORD, json.dumps(unsigned(proposal)).encode("utf-8"))
    assert (code, err) == (0, "") and host.runs.calls == [Z_SPARK]  # its words, no shell
    assert host.logged == ["allow test-env shop-test KD-123 plan step 1 node-c"]


def test_main_without_the_list_refuses_in_enforce_and_notes_it_in_observe(host):
    raw = json.dumps(unsigned(zone_proposal(zone_op("app.tool", Z_SPARK)))).encode("utf-8")
    code, out, err = host("enforce", gate.RUN_WORD, raw)
    assert code == 126 and host.runs.calls == [] and "подпись владельца passkey" in err
    code, out, err = host("observe", gate.RUN_WORD, raw)
    assert code == 0 and host.runs.calls == [gate.quote_argv(Z_SPARK)]
    assert any(m.startswith("observe FAIL (") for m in host.logged)


def test_main_keeps_the_policy_first_for_a_listed_step(host, envs_file, monkeypatch):
    seen = []

    def refuse(gate_cfg, command, envelope=None):
        seen.append(command)
        raise gate.PolicyDeny("правило 1")

    monkeypatch.setattr(gate, "check_policy", refuse)
    raw = json.dumps(unsigned(zone_proposal(zone_op("app.tool", Z_SPARK)))).encode("utf-8")
    code, out, err = host("observe", gate.RUN_WORD, raw)
    assert code == 126 and host.runs.calls == [] and seen == [gate.quote_argv(Z_SPARK)]


def test_check_reports_the_installed_list(host, owner, envs_file):
    code, out, err = host_check(host)
    assert code == 0 and "test-envs: 2 (app-staging, shop-test)\n" in out
    envs_file.unlink()
    code, out, err = host_check(host)
    assert code == 0 and "test-envs: 0\n" in out


def host_check(host):
    gate_json = host.paths[0]
    gate_json.write_text(json.dumps({**GATE, "mode": "enforce"}), encoding="utf-8")
    out, err = io.BytesIO(), io.BytesIO()
    code = gate.main(["--check"], environ={}, stdin=io.BytesIO(b""), out=out, err=err)
    return code, out.getvalue().decode("utf-8"), err.getvalue().decode("utf-8")


# --- agent-exec-gate --test-envs <alias>: the list of one server out of the overlay's catalog -------------------------
ZONE_CATALOG = {
    "schema_version": 1,
    "verbs": ["service.reload", "app.tool", "app.flag"],
    "servers": {
        ALIAS: {"packages": "apt", "install": False, "units": ["app.service", "nginx.service"]},
        "node-g": {"packages": "dnf", "install": False, "units": []},
    },
    "apps": {
        "app-staging": {"server": ALIAS, "user": "www-data", "dir": "/var/www/app.example.test",
                        "python": "./venv/bin/python", "tools": ["tools/admin.py"],
                        "flags": {"file": "config.ini", "sections": ["partner"]},
                        "test": True, "reload": ["app.service"]},
        "shop-test": {"server": ALIAS, "user": "www-data", "dir": ZONE_DIR, "php": "php",
                      "spark": ["migrate:status", "migrate"],
                      "flags": {"file": ".env", "format": "env",
                                "keys": {"CI_ENVIRONMENT": ["production", "development"]}},
                      "test": True},
        "shop-live": {"server": ALIAS, "user": "www-data", "dir": "/var/www/live.example.test", "php": "php",
                      "spark": ["migrate"]},
        "far-test": {"server": "node-g", "user": "www-data", "dir": "/var/www/far.example.test", "php": "php",
                     "spark": ["migrate"], "test": True},
    },
}


def list_of(alias, catalog=ZONE_CATALOG):
    raw = catalog if isinstance(catalog, bytes) else json.dumps(catalog).encode("utf-8")
    out, err = io.BytesIO(), io.BytesIO()
    code = gate.main(["--test-envs", alias], environ={}, stdin=io.BytesIO(raw), out=out, err=err)
    return code, out.getvalue().decode("utf-8"), err.getvalue().decode("utf-8")


def test_the_list_of_a_server_is_its_test_entries_whole():
    code, out, err = list_of(ALIAS)
    assert (code, err) == (0, "") and json.loads(out) == ENVS
    assert gate.validate_test_envs(json.loads(out)) == ENVS["envs"]
    code, out, err = list_of("node-g")
    assert code == 0 and sorted(json.loads(out)["envs"]) == ["far-test"]
    code, out, err = list_of("node-e")
    assert code == 0 and json.loads(out) == {"schema_version": 1, "envs": {}}


def test_the_gates_list_is_the_catalogs_own():
    from kd import admin_operations as ops

    catalog = ops.Catalog.from_dict(ZONE_CATALOG)
    for alias in (ALIAS, "node-g"):
        assert json.loads(list_of(alias)[1]) == ops.test_envs(catalog, alias)


@pytest.mark.parametrize("catalog", [
    b"{not json",
    b"[]",
    {"schema_version": 1, "apps": []},
    {"schema_version": 1, "apps": {"x": {"server": ALIAS, "user": "root", "dir": "/var/www/x", "php": "php",
                                         "spark": ["migrate"], "test": True}}},
    {"schema_version": 1, "apps": {"x": {"server": ALIAS, "user": "www-data", "dir": "/var/www/x", "php": "php",
                                         "spark": ["migrate"], "test": "yes"}}},
    {"schema_version": 1, "apps": {"x": "entry"}},
])
def test_a_malformed_catalog_gives_no_list(catalog):
    code, out, err = list_of(ALIAS, catalog)
    assert code == 2 and out == "" and "agent-exec-gate" in err


def test_the_installer_asks_the_gate_about_a_candidate_list(tmp_path):
    path = tmp_path / "candidate.json"

    def ask():
        out, err = io.BytesIO(), io.BytesIO()
        code = gate.main(["--check-test-envs", str(path)], environ={}, stdin=io.BytesIO(b""), out=out, err=err)
        return code, out.getvalue().decode("utf-8"), err.getvalue().decode("utf-8")

    path.write_text(json.dumps(ENVS), encoding="utf-8")
    assert ask() == (0, "2\n", "")
    path.write_text(json.dumps({"schema_version": 1, "envs": {}}), encoding="utf-8")
    assert ask() == (0, "0\n", "")
    path.write_text(json.dumps({"schema_version": 1, "envs": {"x": {"user": "root", "dir": "/var/www/x"}}}),
                    encoding="utf-8")
    code, out, err = ask()
    assert code == 1 and out == "" and "повреждён" in err
    path.unlink()
    assert ask()[0] == 1


# --- a typed step runs as its argv, without a shell; package.upgrade ----------------------------------------
UPGRADE = {"server": ALIAS, "verb": "package.upgrade", "what": "Обновить систему", "effect": "Пакеты обновятся",
           "risk": {"level": "medium", "why": "Новые версии"}, "rollback": "Откат версий пакетов"}
APT_UPGRADE = ["systemd-run", "--unit=kd-package-upgrade", "--collect", "--setenv=NEEDRESTART_SUSPEND=1",
               "--setenv=DEBIAN_FRONTEND=noninteractive", "apt-get", "-q", "-y", "-o",
               "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold", "full-upgrade"]
DNF_UPGRADE = ["systemd-run", "--unit=kd-package-upgrade", "--collect", "dnf", "-q", "-y", "upgrade"]


def test_a_verified_typed_step_runs_its_argv_not_a_shell(host, owner):
    code, out, err = host("enforce", gate.RUN_WORD, json.dumps(signed_plan(owner, 1)).encode("utf-8"))
    assert (code, err) == (0, "") and host.runs.calls == [SCRIPT]  # a script step stays text for bash
    code, out, err = host("enforce", gate.RUN_WORD, json.dumps(signed_plan(owner, 2)).encode("utf-8"))
    assert (code, err) == (0, "") and host.runs.calls[-1] == ["systemctl", "restart", "nginx.service"]


def test_an_unverified_typed_step_in_observe_runs_as_before(host, owner):
    steps_done(host, 1)
    env = signed_plan(owner, 2)
    env["assertion"] = None
    code, out, err = host("observe", gate.RUN_WORD, json.dumps(env).encode("utf-8"))
    assert code == 0 and host.runs.calls == ["systemctl restart nginx.service"]


def test_package_upgrade_argv_mirrors_the_stage(owner, monkeypatch):
    from kd import admin_operations as ops

    proposal = dict(PROPOSAL, operations=[UPGRADE])
    catalog = ops.Catalog.from_dict({"schema_version": 1, "verbs": ["package.upgrade"], "servers": {
        ALIAS: {"packages": "apt", "install": False, "units": []},
        "node-g": {"packages": "dnf", "install": False, "units": []}}})
    for tool, argv, server in (("apt-get", APT_UPGRADE, ALIAS), ("dnf", DNF_UPGRADE, "node-g")):
        monkeypatch.setattr(gate, "which", lambda name, tool=tool: "/usr/bin/" + name if name == tool else None)
        op = ops.validate_operation(dict(UPGRADE, server=server), catalog, {ALIAS, "node-g"}, {ALIAS, "node-g"})
        assert ops.remote_argv(op, catalog) == argv
        command = ops.remote_command(op, catalog)
        checked = verify(signed_plan(owner, 1, proposal=proposal, command=command), owner.registry())
        assert checked["argv"] == argv and checked["command"] == command
    with pytest.raises(gate.Deny, match="команда"):
        verify(signed_plan(owner, 1, proposal=proposal, command=gate.quote_argv(APT_UPGRADE[3:])), owner.registry())


def test_a_script_step_has_no_argv(owner):
    assert verify(signed_plan(owner, 1), owner.registry())["argv"] is None


@pytest.mark.skipif(os.name != "posix", reason="exec of an argv")
def test_run_executes_an_argv_without_a_shell(tmp_path):
    marker = tmp_path / "a b; touch x"
    assert gate.run(["touch", str(marker)], True) == 0 and marker.exists()
    assert not (tmp_path / "x").exists()
    assert gate.run(["false"], True) == 1
    assert gate.run(["sleep", "30"], True, timeout=0.5) == 124
    assert gate.run(["no-such-command-kd9345"], True) == 127


# --- cron.line, file.drop, gate.script and the script switch ------------------------------------------------
TYPED = ["python3", "-I", "/usr/local/libexec/agent-typed-op"]


def explained(op):
    return dict(op, what="Шаг", effect="Эффект", risk={"level": "low", "why": "Почему"}, rollback="Откат")


def typed_plan(*ops):
    return dict(PROPOSAL, operations=[explained(dict(op, server=ALIAS)) for op in ops])


CRON = {"verb": "cron.line", "mark": "KD-7", "action": "set", "line": "0 3 * * * /usr/sbin/logrotate /etc/x.conf"}
DROP = {"verb": "file.drop", "dir": "/etc/logrotate.d", "name": "app", "mark": "KD-7", "action": "set",
        "text": "/var/log/app.log {\n daily\n}\n"}


@pytest.mark.parametrize("op,words", [
    (CRON, ["cron", "KD-7", "set", CRON["line"]]),
    ({"verb": "cron.line", "mark": "KD-7", "action": "remove"}, ["cron", "KD-7", "remove"]),
    (DROP, ["file", "/etc/logrotate.d", "app", "KD-7", "set", DROP["text"]]),
    ({"verb": "file.drop", "dir": "/etc/sysctl.d", "name": "x.conf", "mark": "KD-7", "action": "remove"},
     ["file", "/etc/sysctl.d", "x.conf", "KD-7", "remove"]),
    ({"verb": "gate.script", "value": "off"}, ["script", "off"]),
])
def test_typed_helper_steps_mirror_the_stage(owner, op, words):
    from kd import admin_operations as ops

    proposal = typed_plan(op)
    catalog = ops.Catalog.from_dict({"schema_version": 1, "verbs": list(ops.TYPED_VERBS), "servers": {
        ALIAS: {"packages": "apt", "install": False, "units": []}}})
    stage = ops.validate_operation(proposal["operations"][0], catalog, {ALIAS}, {ALIAS})
    assert ops.remote_argv(stage, catalog) == TYPED + words
    command = gate.quote_argv(TYPED + words)
    assert verify(signed_plan(owner, 1, proposal=proposal, command=command), owner.registry())["argv"] == TYPED + words


@pytest.mark.parametrize("op", [
    dict(CRON, line="0 4 * * * systemctl reboot"),
    dict(CRON, line="* * * * * echo x >> /etc/sudoers"),
    dict(CRON, line="*/5 * * * * sh%iptables -F%reboot"),  # cron feeds what follows % to sh on stdin
    dict(CRON, line="a b c"),
    dict(CRON, action="remove"),  # a line with remove
    dict(CRON, mark="7"),
    dict(DROP, text="/var/log/a {\n postrotate\n id\n endscript\n}\n"),
    dict(DROP, text="/etc/shadow {\n daily\n}\n"),  # a stanza outside /var/log/
    dict(DROP, text="/var/log/a.log {\n create 0666 root root\n}\n"),
    dict(DROP, dir="/etc/sysctl.d", name="60-kd.conf", text="net.ipv4.ip_forward = 1\n"),  # a critical action's
    dict(DROP, dir="/etc/sysctl.d", name="60-kd.conf", text="kernel.yama.ptrace_scope = 0\n"),  # a critical action's
    dict(DROP, dir="/etc/cron.d"),
    dict(DROP, name="../x"),
    {"verb": "gate.script", "value": "maybe"},
])
def test_the_gate_reads_typed_content_itself(owner, op):
    # a stage that formed the words of a bad step still gets a refusal: the gate checks the content with its policy
    proposal = typed_plan(op)
    words = [w for w in (op.get("mark"), op.get("action"), op.get("line")) if w] if op["verb"] == "cron.line" else (
        [op["dir"], op["name"], op["mark"], op["action"], op["text"]] if op["verb"] == "file.drop"
        else [op["value"]])
    first = {"cron.line": "cron", "file.drop": "file", "gate.script": "script"}[op["verb"]]
    command = gate.quote_argv(TYPED + [first] + words)
    with pytest.raises(gate.Deny):
        verify(signed_plan(owner, 1, proposal=proposal, command=command), owner.registry())


def test_the_script_switch_refuses_text_steps_in_both_modes(host, owner, tmp_path, monkeypatch):
    flag = tmp_path / "script-off"
    monkeypatch.setattr(gate, "SCRIPT_OFF", str(flag))
    script = json.dumps(signed_plan(owner, 1)).encode("utf-8")
    typed = json.dumps(signed_plan(owner, 2)).encode("utf-8")
    assert host("enforce", gate.RUN_WORD, script)[0] == 0
    flag.write_text("")
    for mode in ("enforce", "observe"):
        code, out, err = host(mode, gate.RUN_WORD, script)
        assert code == 126 and "скрипт" in err and "выключ" in err
        code, out, err = host(mode, "uptime && df -h")  # a legacy command is text for bash too
        assert code == 126
    unsigned = signed_plan(owner, 2)
    unsigned["assertion"] = None  # observe would run it as text: refused while scripts are off
    code, out, err = host("observe", gate.RUN_WORD, json.dumps(unsigned).encode("utf-8"))
    assert code == 126
    code, out, err = host("enforce", gate.RUN_WORD, typed)
    assert code == 0 and host.runs.calls[-1] == ["systemctl", "restart", "nginx.service"]
    code, out, err = host("enforce", gate.RUN_WORD, json.dumps(signed_action(owner)).encode("utf-8"))
    assert code == 0  # a critical action stays: the owner signs that exact command
    code, out, err = host("enforce", "agent-exec-gate snapshot 0123456789abcdef")
    assert code == 0


# --- the directories a test environment opens for reading ---------------------------------------------------

def _reads(tmp_path, data):
    path = tmp_path / "list.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    out, err = io.BytesIO(), io.BytesIO()
    code = gate.main(["--test-env-reads", str(path)], environ={}, stdin=io.BytesIO(b""), out=out, err=err)
    return code, out.getvalue().decode("utf-8"), err.getvalue().decode("utf-8")


def test_the_installer_gets_the_read_directories_of_a_list(tmp_path):
    data = json.loads(json.dumps(ENVS))
    data["envs"]["app-staging"]["read"] = ["logs/api_journal", "logs"]
    data["envs"]["shop-test"]["read"] = ["writable/logs"]
    dirs = {name: env["dir"] for name, env in data["envs"].items()}
    assert _reads(tmp_path, data) == (0, "".join(sorted(
        "%s\n" % p for p in (dirs["app-staging"] + "/logs/api_journal", dirs["app-staging"] + "/logs",
                              ZONE_DIR + "/writable/logs"))), "")
    assert _reads(tmp_path, ENVS) == (0, "", "")


@pytest.mark.parametrize("read", [["../etc"], ["/etc"], ["logs/../.."], "logs", [], ["a b"], ["$(id)"],
                                  [".hidden"], ["x%d" % i for i in range(11)]])
def test_a_malformed_read_list_is_refused_whole(tmp_path, read):
    data = json.loads(json.dumps(ENVS))
    data["envs"]["shop-test"]["read"] = read
    code, out, err = _reads(tmp_path, data)
    assert code == 1 and out == "" and "повреждён" in err
    with pytest.raises(gate.Deny):
        gate.validate_test_envs(data)


def test_the_list_of_a_server_carries_its_reads():
    catalog = json.loads(json.dumps(ZONE_CATALOG))
    catalog["apps"]["shop-test"]["read"] = ["writable/logs", "logs"]
    code, out, _ = list_of(ALIAS, catalog)
    assert code == 0 and json.loads(out)["envs"]["shop-test"]["read"] == ["logs", "writable/logs"]


def test_the_helpers_reboot_in_a_plan_step_is_critical(host, owner):
    # system.reboot is a critical action; a script step calling the helper's reboot is refused in both
    # modes
    script = "python3 -I /usr/local/libexec/agent-typed-op reboot KD-123\n"
    raw = json.dumps(signed_plan(owner, 1, proposal=plan_with(script))).encode("utf-8")
    for mode in ("enforce", "observe"):
        code, out, err = host(mode, gate.RUN_WORD, raw)
        assert code == 126 and "критичный" in err and host.runs.calls == []
