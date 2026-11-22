"""A critical action is one exact command, approved once by the owner and consumed once."""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kd import admin_action, admin_grant, admin_task, exec_policy
from kd import admin_operations as ops

sys.path.insert(0, str(Path(__file__).resolve().parent))  # admin_samples
from admin_samples import CARD, ENABLED, INVENTORY, md_card  # noqa: E402

NOW = datetime(2026, 10, 9, 17, 0, tzinfo=timezone.utc)
COMMAND = "wg set wg0 peer 'abc=' remove"


def action(**over):
    data = {"schema_version": 2, "task_id": "KD-123", "intent": "Убрать пир", "basis": "wg show: пир молчит",
            "servers": ["node-c"], "command": COMMAND, "effect": "Пир отключён",
            "risk": {"level": "high", "why": "Связь с пиром пропадёт"}, "rollback": "Вернуть пир из резервной копии"}
    data.update(over)
    return data


def validate(data, servers=("node-c", "node-e"), deny=None):
    return admin_action.validate_action(data, INVENTORY, ENABLED, card_servers=servers, deny=deny)


def card(text=CARD):
    return md_card(text)


def test_valid_action_keeps_the_exact_command():
    a = validate(action())
    assert a.command == COMMAND and a.servers == ("node-c",)
    assert len(a.sha256) == 64 and a.proposal_id == a.sha256[:16]


def test_hash_covers_command_servers_and_task():
    base = validate(action()).sha256
    assert validate(action(command=COMMAND + " ")).sha256 != base
    assert validate(action(servers=["node-c", "node-e"])).sha256 != base
    assert validate(action(servers=["node-e", "node-c"])).sha256 != validate(action(servers=["node-c", "node-e"])).sha256
    assert validate(action(intent="другое")).sha256 != base
    assert validate(action(basis="другое")).sha256 != base
    assert validate(action(effect="другое")).sha256 != base
    assert validate(action(risk={"level": "low", "why": "x"})).sha256 != base


@pytest.mark.parametrize("over,why", [
    ({"schema_version": 3}, "schema"),
    ({"schema_version": 1}, "schema_version 2: add basis, effect and risk"),
    ({"basis": ""}, "basis"),
    ({"effect": " "}, "effect"),
    ({"risk": {"level": "urgent", "why": "x"}}, "risk"),
    ({"risk": "high"}, "risk"),
    ({"task_id": "KD-0"}, "task"),
    ({"command": ""}, "command"),
    ({"command": "x" * (8 * 1024 + 1)}, "8 KiB"),
    ({"command": "ls\x00"}, "command"),
    ({"command": "ls\rrm"}, "command"),
    ({"command": ["ls"]}, "command"),
    ({"servers": []}, "servers"),
    ({"servers": "node-c"}, "servers"),
    ({"servers": ["node-c", "node-c"]}, "twice"),
    ({"servers": ["node-l"]}, "enabled"),
    ({"servers": ["unknown"]}, "inventory"),
    ({"servers": ["all"]}, "inventory"),
    ({"servers": ["example-prod", "node-c"]}, "last"),
    ({"servers": ["node-g"]}, "card"),
    ({"extra": 1}, "keys"),
    ({"intent": ""}, "intent"),
])
def test_invalid_actions_are_refused(over, why):
    with pytest.raises(ops.ProposalError, match=why):
        validate(action(**over), servers=("node-c", "node-e", "node-l", "unknown", "all", "example-prod"))


def test_production_last_is_accepted():
    a = validate(action(servers=["node-c", "example-prod"]), servers=("node-c", "example-prod"))
    assert a.servers == ("node-c", "example-prod")


def test_denylisted_command_cannot_be_proposed():
    deny = lambda command, alias: ("users", "users are owner-only") if "usermod" in command else None  # noqa: E731
    with pytest.raises(ops.ProposalError, match="denied by rule users"):
        validate(action(command="systemctl restart x; echo usermod"), deny=deny)
    validate(action(), deny=deny)


@pytest.mark.parametrize("command", [
    "useradd bob", "echo x > /etc/sudoers.d/x", "systemctl reload ssh; cat /root/.ssh/authorized_keys",
    "python3 -c 'import os'",
])
def test_rule1_of_the_gate_policy_refuses_an_action_with_the_gates_reason(command):
    # the gate's own reason, before the denylist (which would name another rule)
    deny = lambda command, alias: ("other", "the denylist's reason")  # noqa: E731
    with pytest.raises(ops.ProposalError) as caught:
        validate(action(command=command), deny=deny)
    assert str(caught.value) == exec_policy.protect(command)
    assert str(caught.value).endswith("шлюз и доступ к серверу меняются только с машины владельца (deploy_target.sh)")


# --- the one-time action grant -----------------------------------------------------------------


def store(tmp_path):
    root = tmp_path / "grants"
    root.mkdir(exist_ok=True)
    return admin_action.ActionStore(str(root), None)


def approve(s, a=None, c=None, now=NOW):
    a = a or validate(action())
    c = c or card()
    return s.approve(c, a, now=now, via="test", expect_card_sha256=c.scope_sha256, expect_action_sha256=a.sha256)


def test_approve_records_the_exact_action_for_fifteen_minutes(tmp_path):
    s = store(tmp_path)
    g = approve(s)
    assert g.command == COMMAND and g.servers == ("node-c",) and g.state == "active"
    assert g.expires_at == NOW + admin_action.ACTION_TTL
    assert s.load("KD-123", g.proposal_id).action_id == g.action_id
    journal = (tmp_path / "grants" / admin_grant.JOURNAL).read_text("utf-8").splitlines()
    assert json.loads(journal[-1])["action"] == "approve-action"


def test_an_action_approved_in_the_cabinet_records_the_user_and_the_signature(tmp_path):
    s = store(tmp_path)
    a, c = validate(action()), card()
    g = s.approve(c, a, now=NOW, via="cabinet", expect_card_sha256=c.scope_sha256, expect_action_sha256=a.sha256,
                  approved_by="user:owner", signer="cred-1", signature='{"id": "cred-1"}')
    loaded = s.load("KD-123", g.proposal_id)
    assert (loaded.approved_by, loaded.signer, loaded.signature) == ("user:owner", "cred-1", '{"id": "cred-1"}')
    journal = (tmp_path / "grants" / admin_grant.JOURNAL).read_text("utf-8").splitlines()
    assert json.loads(journal[-1])["by"] == "user:owner"
    (tmp_path / "plain").mkdir()
    plain = approve(store(tmp_path / "plain"))
    data = json.loads((tmp_path / "plain" / "grants" / f"KD-123.action-{plain.proposal_id}.json").read_text("utf-8"))
    assert data["approved_by"] == "owner" and "signer" not in data and "signature" not in data
    with pytest.raises(admin_grant.GrantError, match="approved_by"):
        s.approve(c, a, now=NOW, via="cabinet", expect_card_sha256=c.scope_sha256, expect_action_sha256=a.sha256,
                  approved_by="user:Bad Name")


def test_consume_once(tmp_path):
    s = store(tmp_path)
    g = approve(s)
    used = s.consume("KD-123", g.proposal_id, g.action_sha256, card(), NOW + timedelta(minutes=1))
    assert used.state == "consumed" and used.action_id == g.action_id
    with pytest.raises(admin_grant.GrantError, match="consumed"):
        s.consume("KD-123", g.proposal_id, g.action_sha256, card(), NOW + timedelta(minutes=2))


@pytest.mark.parametrize("change", ["task", "sha", "late", "card"])
def test_consume_refuses_anything_else(tmp_path, change):
    s = store(tmp_path)
    g = approve(s)
    task, sha, when, c = "KD-123", g.action_sha256, NOW + timedelta(minutes=1), card()
    if change == "task":
        task = "KD-124"
    elif change == "sha":
        sha = "0" * 64
    elif change == "late":
        when = NOW + admin_action.ACTION_TTL
    else:
        c = card(CARD.replace("Restart the app", "Restart and wipe the app"))
    with pytest.raises((admin_grant.GrantError, admin_task.CardError)):
        s.consume(task, g.proposal_id, sha, c, when)
    if change != "task":
        assert s.load("KD-123", g.proposal_id).state == "active"


def test_consume_refuses_a_closed_card(tmp_path):
    s = store(tmp_path)
    g = approve(s)
    with pytest.raises(admin_task.CardError, match="status"):
        s.consume("KD-123", g.proposal_id, g.action_sha256, card(CARD.replace("status: To Do", "status: Done")), NOW)


def test_revoked_action_cannot_be_consumed(tmp_path):
    s = store(tmp_path)
    g = approve(s)
    s.revoke("KD-123", g.proposal_id, now=NOW, via="test")
    with pytest.raises(admin_grant.GrantError, match="revoked"):
        s.consume("KD-123", g.proposal_id, g.action_sha256, card(), NOW)


def test_new_approval_after_use_is_a_new_action_id(tmp_path):
    s = store(tmp_path)
    g = approve(s)
    s.consume("KD-123", g.proposal_id, g.action_sha256, card(), NOW)
    again = approve(s, now=NOW + timedelta(minutes=5))
    assert again.action_id != g.action_id and again.state == "active"


def test_unwritable_journal_consumes_nothing(tmp_path):
    s = store(tmp_path)
    g = approve(s)
    journal = tmp_path / "grants" / admin_grant.JOURNAL
    journal.unlink()
    journal.mkdir()
    with pytest.raises(admin_grant.GrantError):
        s.consume("KD-123", g.proposal_id, g.action_sha256, card(), NOW)
    assert s.load("KD-123", g.proposal_id).state == "active"


def test_changed_hash_at_approval_is_a_conflict(tmp_path):
    s = store(tmp_path)
    a, c = validate(action()), card()
    with pytest.raises(admin_grant.Conflict):
        s.approve(c, a, now=NOW, via="test", expect_card_sha256=c.scope_sha256, expect_action_sha256="0" * 64)


@pytest.mark.parametrize("mutate,why", [
    (lambda d: d.update(command="reboot"), "sha256"),
    (lambda d: d.update(servers=["node-e"]), "sha256"),
    (lambda d: d.update(state="used"), "state"),
    (lambda d: d.update(expires_at="2026-10-10T17:00:00Z"), "15"),
    (lambda d: d.update(extra=1), "keys"),
])
def test_tampered_action_grant_is_refused(tmp_path, mutate, why):
    s = store(tmp_path)
    g = approve(s)
    path = Path(s.path("KD-123", g.proposal_id))
    data = json.loads(path.read_text("utf-8"))
    mutate(data)
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(admin_grant.GrantError, match=why):
        s.load("KD-123", g.proposal_id)


@pytest.mark.parametrize("pid", ["../x", "ABC", "0" * 15, "0" * 17, ""])
def test_bad_proposal_ids_are_refused(tmp_path, pid):
    with pytest.raises(admin_grant.GrantError, match="action id"):
        store(tmp_path).load("KD-123", pid)


@pytest.mark.skipif(os.name != "posix", reason="owners")
def test_foreign_owner_is_refused(tmp_path):
    s = store(tmp_path)
    g = approve(s)
    with pytest.raises(admin_grant.GrantError, match="owner"):
        admin_action.ActionStore(str(tmp_path / "grants"), os.getuid() + 1).load("KD-123", g.proposal_id)


@pytest.mark.parametrize("char", ["\u202e", "\u2066", "\u200b", "\u200d", "\ufeff", "\u2028", "\u2029", "\x85",
                                  "\ue000", "\U000e0041", "\u0378"],
                         ids=["rlo", "lri", "zwsp", "zwj", "bom", "ls", "ps", "nel", "private", "tag", "unassigned"])
def test_invisible_or_reordering_characters_are_refused(char):
    with pytest.raises(ops.ProposalError, match="invisible"):
        validate(action(command=f"systemctl status nginx {char}; true"))
    for key in ("intent", "rollback"):
        with pytest.raises(ops.ProposalError, match="invisible"):
            validate(action(**{key: f"Убрать {char}пир"}))


def test_cyrillic_tab_and_newline_stay_allowed():
    valid = validate(action(command="echo 'привет'\tok\ntrue", intent="Проверить\nстроки"))
    assert "привет" in valid.command and "\t" in valid.command
