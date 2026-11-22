"""Owner grants for admin cards — stored outside the board, checked fail closed."""
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from kd import admin_grant, admin_operations, admin_task
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # admin_samples
from admin_samples import CARD, CATALOG, ENABLED, INVENTORY, md_card, proposal as proposal_data  # noqa: E402

NOW = datetime(2026, 10, 9, 18, 0, 0, tzinfo=timezone.utc)


def make_card(tmp_path, text=CARD):
    return md_card(text.replace("  - 'server:node-e'\n", ""))


def make_proposal(**over):
    catalog = admin_operations.Catalog.from_dict(json.loads(json.dumps(CATALOG)))
    return admin_operations.validate_proposal(proposal_data(**over), catalog, INVENTORY, ENABLED,
                                              card_servers=("node-c",))


def store(tmp_path):
    root = tmp_path / "grants"
    root.mkdir(exist_ok=True)
    return admin_grant.Store(str(root), owner_uid=os.lstat(str(root)).st_uid)


def approve(tmp_path, now=NOW, ttl=None, card=None, prop=None):
    s = store(tmp_path)
    card = card or make_card(tmp_path)
    prop = prop or make_proposal()
    grant = s.approve(card, prop, now=now, ttl=admin_grant.DEFAULT_TTL if ttl is None else ttl, via="test",
                      expect_card_sha256=card.scope_sha256, expect_proposal_sha256=prop.sha256)
    return s, card, prop, grant


def test_approve_writes_grant_and_decision(tmp_path):
    s, card, prop, grant = approve(tmp_path)
    loaded = s.load("KD-123")
    assert loaded == grant
    assert loaded.state == "active" and loaded.approved_by == "owner"
    assert loaded.card_scope_sha256 == card.scope_sha256 and loaded.proposal_sha256 == prop.sha256
    assert loaded.expires_at - loaded.approved_at == timedelta(hours=72)
    assert [op for op in loaded.operations] == [op.as_dict() for op in prop.operations]
    lines = (tmp_path / "grants" / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[-1])
    assert entry["action"] == "approve" and entry["grant_id"] == grant.grant_id
    assert entry["decision_id"] == grant.decision_id and entry["via"] == "test"


def test_a_cabinet_grant_records_the_user_and_the_signature(tmp_path):
    s, card, prop = store(tmp_path), make_card(tmp_path), make_proposal()
    grant = s.approve(card, prop, now=NOW, ttl=admin_grant.DEFAULT_TTL, via="cabinet",
                      expect_card_sha256=card.scope_sha256, expect_proposal_sha256=prop.sha256,
                      approved_by="user:owner", signer="cred-1", signature='{"id": "cred-1"}')
    loaded = s.load("KD-123")
    assert loaded == grant and loaded.approved_by == "user:owner"
    assert (loaded.signer, loaded.signature) == ("cred-1", '{"id": "cred-1"}')
    entry = json.loads((tmp_path / "grants" / "decisions.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert entry["by"] == "user:owner" and entry["via"] == "cabinet"
    assert admin_grant.check_usable(loaded, card, NOW) is None
    for bad in ("user:", "user:Owner!", "boss", ""):
        with pytest.raises(admin_grant.GrantError, match="approved_by"):
            s.approve(card, prop, now=NOW, ttl=admin_grant.DEFAULT_TTL, via="cabinet",
                      expect_card_sha256=card.scope_sha256, expect_proposal_sha256=prop.sha256, approved_by=bad)


def test_an_owner_grant_has_no_signature_keys(tmp_path):
    approve(tmp_path)
    data = json.loads((tmp_path / "grants" / "KD-123.json").read_text(encoding="utf-8"))
    assert data["approved_by"] == "owner" and "signer" not in data and "signature" not in data


def test_usable_grant_passes(tmp_path):
    s, card, _, grant = approve(tmp_path)
    admin_grant.check_usable(s.load("KD-123"), card, NOW + timedelta(hours=1))


def test_notes_and_in_progress_keep_the_grant(tmp_path):
    s, _, _, _ = approve(tmp_path)
    text = CARD.replace("status: To Do", "status: In Progress").replace("[sysadmin] started", "[sysadmin] x")
    admin_grant.check_usable(s.load("KD-123"), make_card(tmp_path, text), NOW + timedelta(hours=1))


@pytest.mark.parametrize("old,new,why", [
    ("node-c: restart the app workers", "node-c: restart all", "scope"),
    ("Restart the app workers after", "Restart all after", "scope"),
    ("status: To Do", "status: Done", "status"),
    ("status: To Do", "status: Blocked", "status"),
    ("status: To Do", "status: Review", "status"),
    ("status: To Do", "status: Inbox", "status"),
    ("project: admin", "project: digipaddock", "project"),
    ("  - '@sysadmin'\n", "  - '@dev'\n", "assignee"),
])
def test_changed_card_stops_the_grant(tmp_path, old, new, why):
    s, _, _, _ = approve(tmp_path)
    with pytest.raises((admin_grant.GrantError, admin_task.CardError), match=why):
        admin_grant.check_usable(s.load("KD-123"), make_card(tmp_path, CARD.replace(old, new, 1)),
                                 NOW + timedelta(hours=1))


def test_expired_grant_is_refused(tmp_path):
    s, card, _, _ = approve(tmp_path)
    with pytest.raises(admin_grant.GrantError, match="expired"):
        admin_grant.check_usable(s.load("KD-123"), card, NOW + timedelta(hours=72, seconds=1))


def test_grant_from_the_future_is_refused(tmp_path):
    s, card, _, _ = approve(tmp_path, now=NOW + timedelta(hours=1))
    with pytest.raises(admin_grant.GrantError, match="future"):
        admin_grant.check_usable(s.load("KD-123"), card, NOW)


def test_revoke_stops_the_grant_and_is_journaled(tmp_path):
    s, card, _, grant = approve(tmp_path)
    s.revoke("KD-123", now=NOW + timedelta(minutes=5), via="test")
    with pytest.raises(admin_grant.GrantError, match="revoked"):
        admin_grant.check_usable(s.load("KD-123"), card, NOW + timedelta(minutes=6))
    entry = json.loads((tmp_path / "grants" / "decisions.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert entry["action"] == "revoke" and entry["grant_id"] == grant.grant_id


def test_reapprove_gets_a_new_grant_id_and_links_the_previous(tmp_path):
    s, card, prop, first = approve(tmp_path)
    second = s.approve(card, prop, now=NOW + timedelta(hours=1), ttl=admin_grant.DEFAULT_TTL, via="test",
                       expect_card_sha256=card.scope_sha256, expect_proposal_sha256=prop.sha256)
    assert second.grant_id != first.grant_id
    entry = json.loads((tmp_path / "grants" / "decisions.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert entry["previous_grant_id"] == first.grant_id


def test_changed_card_or_proposal_between_page_and_post_is_a_conflict(tmp_path):
    s = store(tmp_path)
    card, prop = make_card(tmp_path), make_proposal()
    with pytest.raises(admin_grant.Conflict):
        s.approve(card, prop, now=NOW, ttl=admin_grant.DEFAULT_TTL, via="test",
                  expect_card_sha256="0" * 64, expect_proposal_sha256=prop.sha256)
    with pytest.raises(admin_grant.Conflict):
        s.approve(card, prop, now=NOW, ttl=admin_grant.DEFAULT_TTL, via="test",
                  expect_card_sha256=card.scope_sha256, expect_proposal_sha256="0" * 64)
    assert s.load("KD-123") is None
    assert not (tmp_path / "grants" / "decisions.jsonl").exists()


def test_proposal_for_another_card_is_refused(tmp_path):
    s = store(tmp_path)
    card, prop = make_card(tmp_path), make_proposal(task_id="KD-124")
    with pytest.raises(admin_grant.GrantError, match="KD-124"):
        s.approve(card, prop, now=NOW, ttl=admin_grant.DEFAULT_TTL, via="test",
                  expect_card_sha256=card.scope_sha256, expect_proposal_sha256=prop.sha256)


@pytest.mark.parametrize("ttl", [timedelta(0), timedelta(days=7, seconds=1), timedelta(seconds=-1)])
def test_ttl_is_bounded(tmp_path, ttl):
    with pytest.raises(admin_grant.GrantError, match="ttl"):
        approve(tmp_path, ttl=ttl)


def test_seven_days_is_allowed(tmp_path):
    approve(tmp_path, ttl=admin_grant.MAX_TTL)


def test_journal_failure_writes_no_grant(tmp_path, monkeypatch):
    s = store(tmp_path)
    card, prop = make_card(tmp_path), make_proposal()

    def broken(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(admin_grant, "_append_journal", broken)
    with pytest.raises(admin_grant.GrantError, match="journal"):
        s.approve(card, prop, now=NOW, ttl=admin_grant.DEFAULT_TTL, via="test",
                  expect_card_sha256=card.scope_sha256, expect_proposal_sha256=prop.sha256)
    assert s.load("KD-123") is None


def tamper(tmp_path, **fields):
    s, card, _, grant = approve(tmp_path)
    path = tmp_path / "grants" / "KD-123.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(fields)
    path.write_text(json.dumps(data), encoding="utf-8")
    return s, card


@pytest.mark.parametrize("fields,why", [
    ({"schema_version": 2}, "schema"),
    ({"task_id": "KD-124"}, "task"),
    ({"state": "approved"}, "state"),
    ({"approved_by": "sysadmin"}, "approved_by"),
    ({"approved_by": "user:"}, "approved_by"),
    ({"signer": 5}, "signer"),
    ({"signature": "x" * 20000}, "signature"),
    ({"expires_at": "2026-10-20T18:00:00Z"}, "ttl"),
    ({"expires_at": "2026-10-09T17:00:00Z"}, "ttl"),
    ({"expires_at": "tomorrow"}, "time"),
    ({"expires_at": "2026-10-10T18:00:00+00:00"}, "time"),
    ({"grant_id": "x"}, "grant_id"),
    ({"card_scope_sha256": "abc"}, "sha256"),
    ({"operations": "all"}, "operations"),
    ({"extra": True}, "keys"),
])
def test_tampered_or_unknown_grant_is_refused(tmp_path, fields, why):
    s, _ = tamper(tmp_path, **fields)
    with pytest.raises(admin_grant.GrantError, match=why):
        s.load("KD-123")


def test_grant_with_missing_key_is_refused(tmp_path):
    s, _, _, _ = approve(tmp_path)
    path = tmp_path / "grants" / "KD-123.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["expires_at"]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(admin_grant.GrantError, match="keys"):
        s.load("KD-123")


def test_garbage_and_oversized_grants_are_refused(tmp_path):
    s, _, _, _ = approve(tmp_path)
    path = tmp_path / "grants" / "KD-123.json"
    path.write_bytes(b"{not json")
    with pytest.raises(admin_grant.GrantError):
        s.load("KD-123")
    path.write_bytes(b" " * (admin_grant.MAX_BYTES + 1))
    with pytest.raises(admin_grant.GrantError, match="large"):
        s.load("KD-123")


def test_grant_of_another_owner_is_refused(tmp_path):
    approve(tmp_path)
    other = admin_grant.Store(str(tmp_path / "grants"), owner_uid=os.lstat(str(tmp_path / "grants")).st_uid + 1)
    with pytest.raises(admin_grant.GrantError, match="owner"):
        other.load("KD-123")


@pytest.mark.skipif(os.name != "posix", reason="modes and symlinks")
def test_writable_or_symlinked_grant_is_refused(tmp_path):
    s, _, _, _ = approve(tmp_path)
    path = tmp_path / "grants" / "KD-123.json"
    os.chmod(path, 0o666)
    with pytest.raises(admin_grant.GrantError, match="mode"):
        s.load("KD-123")
    os.chmod(path, 0o640)
    real = tmp_path / "elsewhere.json"
    os.replace(path, real)
    os.symlink(real, path)
    with pytest.raises(admin_grant.GrantError):
        s.load("KD-123")


@pytest.mark.skipif(os.name != "posix", reason="modes and symlinks")
def test_store_directory_must_be_private(tmp_path):
    s, _, _, _ = approve(tmp_path)
    os.chmod(tmp_path / "grants", 0o777)
    with pytest.raises(admin_grant.GrantError, match="directory"):
        s.load("KD-123")


def test_missing_store_is_an_error_not_an_empty_store(tmp_path):
    s = admin_grant.Store(str(tmp_path / "nowhere"), owner_uid=0)
    with pytest.raises(admin_grant.GrantError, match="directory"):
        s.load("KD-123")


def test_no_grant_is_none(tmp_path):
    assert store(tmp_path).load("KD-123") is None


def test_operation_by_one_based_index(tmp_path):
    s, _, prop, _ = approve(tmp_path)
    grant = s.load("KD-123")
    assert admin_grant.operation_at(grant, 2) == prop.operations[1].as_dict()
    for bad in (0, 3, -1):
        with pytest.raises(admin_grant.GrantError, match="operation"):
            admin_grant.operation_at(grant, bad)



def test_shell_servers_is_gone():
    # a grant opens no free commands anywhere; every step is run by kd-admin-run from the grant
    assert not hasattr(admin_grant, "shell_servers")


# --- an approval by the test-zone rule ---------------------------------------------------------------------
def test_the_test_zone_rule_is_an_approver_only_where_it_is_asked_for():
    assert admin_grant.TEST_ZONE == "rule:test-env"
    assert admin_grant.check_approver(admin_grant.TEST_ZONE, "KD-1", rule=True) == "rule:test-env"
    with pytest.raises(admin_grant.GrantError, match="approved_by"):
        admin_grant.check_approver(admin_grant.TEST_ZONE, "KD-1")
    for other in ("rule:other", "rule:", "rule:test-env "):
        with pytest.raises(admin_grant.GrantError, match="approved_by"):
            admin_grant.check_approver(other, "KD-1", rule=True)


def test_a_rule_grant_is_written_and_read_as_a_server_grant(tmp_path):
    s, card, prop = store(tmp_path), make_card(tmp_path), make_proposal()
    grant = s.approve(card, prop, now=NOW, ttl=timedelta(hours=24), via="rule",
                      expect_card_sha256=card.scope_sha256, expect_proposal_sha256=prop.sha256,
                      approved_by=admin_grant.TEST_ZONE)
    loaded = s.load("KD-123")
    assert loaded == grant and loaded.approved_by == "rule:test-env" and loaded.signature == ""
    admin_grant.check_usable(loaded, card, NOW)
    entry = json.loads((tmp_path / "grants" / "decisions.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert entry["by"] == "rule:test-env" and entry["via"] == "rule"
    # the rule approves a plan, never a revocation
    with pytest.raises(admin_grant.GrantError, match="approved_by"):
        s.revoke("KD-123", now=NOW, via="test", by=admin_grant.TEST_ZONE)
