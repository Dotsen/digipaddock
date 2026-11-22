"""The approval flow shared by the web page and the owner CLI (the board is the cabinet)."""
import copy
import json
import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kd import admin_approval, admin_grant, admin_proposal, admin_signed
from kd import admin_operations as ops
from kd.cabinet import client as cabinet_client

sys.path.insert(0, str(Path(__file__).resolve().parent))  # admin_samples, admin_board
from admin_board import AdminBoard, AdminEnv  # noqa: E402
from admin_samples import CARD, CATALOG, ENABLED, INVENTORY, proposal  # noqa: E402

NOW = datetime(2026, 10, 9, 17, 0, tzinfo=timezone.utc)
ONE_SERVER = CARD.replace("  - 'server:node-e'\n", "")


class FailingPromote:
    """The approval user's client whose ``card.promote`` fails with one kind of cabinet error."""

    def __init__(self, board, kind):
        self.board, self.kind = board, kind

    def call(self, request):
        if request["op"] == "card.promote":
            raise cabinet_client.CabinetError(self.kind, "the board said no")
        return self.board.call(request)


@pytest.fixture
def env(tmp_path):
    (tmp_path / "proposals").mkdir()
    (tmp_path / "grants").mkdir()
    return AdminEnv(tmp_path, ONE_SERVER, user="digi-cabinet")


def write_card(env, text):
    env.board.put(text)


def propose(env, **over):
    return admin_proposal.submit(str(env / "proposals"), json.dumps(proposal(**over)).encode("utf-8"),
                                 agent="sysadmin", card=env.board.card(),
                                 catalog=ops.Catalog.from_dict(copy.deepcopy(CATALOG)),
                                 inventory=INVENTORY, enabled=ENABLED, now=NOW)


def service(env, client=None):
    paths = admin_approval.Paths(proposals=str(env / "proposals"), grants=str(env / "grants"))
    return admin_approval.Service(paths, catalog=lambda: ops.Catalog.from_dict(copy.deepcopy(CATALOG)),
                                  inventory=lambda: INVENTORY, enabled=lambda: ENABLED,
                                  client=client or env.board)


def approve(svc, view, **over):
    args = dict(card_sha256=view.card.scope_sha256, proposal_sha256=view.proposal.sha256,
                ttl=admin_grant.DEFAULT_TTL, via="web", now=NOW)
    args.update(over)
    return svc.approve("KD-123", **args)


def test_view_shows_card_proposal_and_no_grant(env):
    write_card(env, ONE_SERVER.replace("status: To Do", "status: Inbox"))
    p = propose(env)
    view = service(env).view("KD-123", NOW)
    assert view.card.title == "node-c: restart the app workers"
    assert view.proposal.sha256 == p.sha256
    assert view.stored.proposed_by == "sysadmin"
    assert view.grant is None and view.problem is None
    assert view.can_approve


def test_view_without_proposal_cannot_approve(env):
    view = service(env).view("KD-123", NOW)
    assert view.proposal is None and not view.can_approve
    assert "предложения нет" in view.problem


def test_view_reports_a_proposal_that_no_longer_validates(env):
    propose(env)
    write_card(env, ONE_SERVER.replace("'server:node-c'", "'server:node-e'"))
    view = service(env).view("KD-123", NOW)
    assert view.proposal is None and not view.can_approve
    assert "server" in view.problem


def test_approve_active_card_writes_a_grant_without_touching_the_card(env):
    propose(env)
    svc = service(env)
    grant = approve(svc, svc.view("KD-123", NOW))
    assert env.board.promotes() == []
    assert grant.state == "active" and grant.expires_at == NOW + admin_grant.DEFAULT_TTL
    stored = svc.view("KD-123", NOW)
    assert stored.grant.grant_id == grant.grant_id and stored.usable


def test_approve_inbox_card_moves_it_to_to_do_first(env):
    write_card(env, ONE_SERVER.replace("status: To Do", "status: Inbox"))
    propose(env)
    svc = service(env)
    view = svc.view("KD-123", NOW)
    assert view.card.status == "Inbox"
    approve(svc, view)
    (promote,) = env.board.promotes()  # the owner's card.promote, revision-checked by the cabinet
    assert promote["payload"]["after"]["status"] == "To Do"
    after = svc.view("KD-123", NOW)
    assert after.card.status == "To Do" and after.usable


@pytest.mark.parametrize("outcome,error", [
    ("conflict", admin_approval.Conflict), ("unavailable", admin_approval.Unavailable),
    ("refused", admin_approval.Refused),
])
def test_failed_status_change_creates_no_grant(env, outcome, error):
    write_card(env, ONE_SERVER.replace("status: To Do", "status: Inbox"))
    propose(env)
    svc = service(env, FailingPromote(env.board, outcome))
    with pytest.raises(error):
        approve(svc, svc.view("KD-123", NOW))
    assert svc.view("KD-123", NOW).grant is None


def test_replaced_proposal_is_a_conflict(env):
    propose(env)
    svc = service(env)
    view = svc.view("KD-123", NOW)
    propose(env, intent="Другое")
    with pytest.raises(admin_approval.Conflict):
        approve(svc, view)
    assert svc.view("KD-123", NOW).grant is None


def test_changed_card_scope_is_a_conflict(env):
    propose(env)
    svc = service(env)
    view = svc.view("KD-123", NOW)
    write_card(env, ONE_SERVER.replace("Restart the app", "Restart and upgrade the app"))
    with pytest.raises(admin_approval.Conflict):
        approve(svc, view)
    assert svc.view("KD-123", NOW).grant is None


@pytest.mark.parametrize("status", ["Blocked", "Review", "Done"])
def test_closed_card_is_refused(env, status):
    propose(env)
    svc = service(env)
    view = svc.view("KD-123", NOW)
    write_card(env, ONE_SERVER.replace("status: To Do", f"status: {status}"))
    with pytest.raises(admin_approval.Refused, match="status"):
        approve(svc, view)


def test_broken_grant_store_creates_no_grant(env):
    propose(env)
    svc = service(env)
    view = svc.view("KD-123", NOW)
    (env / "grants").rmdir()
    with pytest.raises(admin_approval.Unavailable):
        approve(svc, view)


def test_unwritable_decision_journal_creates_no_grant(env):
    propose(env)
    svc = service(env)
    view = svc.view("KD-123", NOW)
    (env / "grants" / admin_grant.JOURNAL).mkdir()
    with pytest.raises(admin_approval.Unavailable):
        approve(svc, view)
    assert not (env / "grants" / "KD-123.json").exists()


def test_inbox_moved_but_grant_failed_shows_unfinished_approval(env):
    write_card(env, ONE_SERVER.replace("status: To Do", "status: Inbox"))
    propose(env)
    svc = service(env)
    view = svc.view("KD-123", NOW)
    (env / "grants" / admin_grant.JOURNAL).mkdir()
    with pytest.raises(admin_approval.Unavailable):
        approve(svc, view)
    after = svc.view("KD-123", NOW)
    assert after.card.status == "To Do" and after.grant is None
    assert "завершить согласование" in after.problem


@pytest.mark.parametrize("ttl", [timedelta(0), timedelta(days=8)])
def test_ttl_out_of_bounds_is_refused(env, ttl):
    propose(env)
    svc = service(env)
    with pytest.raises(admin_approval.Refused, match="ttl"):
        approve(svc, svc.view("KD-123", NOW), ttl=ttl)


def test_revoke_ends_the_grant(env):
    propose(env)
    svc = service(env)
    grant = approve(svc, svc.view("KD-123", NOW))
    revoked = svc.revoke("KD-123", grant_id=grant.grant_id, via="web", now=NOW)
    assert revoked.state == "revoked"
    view = svc.view("KD-123", NOW)
    assert not view.usable and view.grant.state == "revoked"


def test_revoke_of_another_grant_is_a_conflict(env):
    propose(env)
    svc = service(env)
    approve(svc, svc.view("KD-123", NOW))
    with pytest.raises(admin_approval.Conflict):
        svc.revoke("KD-123", grant_id="00000000-0000-4000-8000-000000000000", via="web", now=NOW)


def test_revoke_without_grant_is_refused(env):
    with pytest.raises(admin_approval.Refused):
        service(env).revoke("KD-123", grant_id=None, via="cli", now=NOW)


def test_expired_grant_is_not_usable(env):
    propose(env)
    svc = service(env)
    approve(svc, svc.view("KD-123", NOW))
    later = svc.view("KD-123", NOW + admin_grant.DEFAULT_TTL + timedelta(seconds=1))
    assert not later.usable and "expired" in later.grant_problem


def test_missing_card_is_reported_not_raised(env):
    propose(env)
    other = AdminBoard(env / "other", ONE_SERVER.replace("KD-123", "KD-124"), user="digi-cabinet")
    view = service(env, other).view("KD-123", NOW)
    assert view.card is None and "not found" in view.problem and not view.can_approve


# --- critical actions (stage E) ---------------------------------------------------------------------

ACTION = {"schema_version": 2, "task_id": "KD-123", "intent": "Убрать пир", "basis": "wg show: пир молчит",
          "servers": ["node-c"], "command": "wg set wg0 peer 'abc=' remove", "effect": "Пир отключён",
          "risk": {"level": "high", "why": "Связь с пиром пропадёт"}, "rollback": "Вернуть пир"}


def no_deny(command, alias):
    return ("users", "owner-only") if "useradd" in command else None


def propose_action(env, data=ACTION):
    return admin_proposal.submit_action(str(env / "proposals"), json.dumps(data).encode("utf-8"), agent="sysadmin",
                                        card=env.board.card(), inventory=INVENTORY, enabled=ENABLED, now=NOW,
                                        deny=no_deny)


def action_service(env, deny=no_deny, client=None):
    svc = service(env, client)
    svc.deny = deny
    return svc


def approve_action(svc, view, now=NOW):
    return svc.approve_action("KD-123", view.proposal_id, card_sha256=view.card.scope_sha256,
                              action_sha256=view.action.sha256, via="web", now=now)


def test_action_view_and_one_time_approval(env):
    a = propose_action(env)
    svc = action_service(env)
    assert svc.action_ids("KD-123") == [a.proposal_id]
    view = svc.view_action("KD-123", a.proposal_id, NOW)
    assert view.action.command == ACTION["command"] and view.can_approve
    grant = approve_action(svc, view)
    after = svc.view_action("KD-123", a.proposal_id, NOW)
    assert after.live(NOW) and not after.can_approve
    used = svc.consume("KD-123", a.proposal_id, grant.action_sha256, NOW)
    assert used.state == "consumed"
    assert svc.view_action("KD-123", a.proposal_id, NOW).can_approve  # a new decision is possible again


def test_action_needs_the_denylist(env):
    a = propose_action(env)
    svc = action_service(env, deny=None)
    view = svc.view_action("KD-123", a.proposal_id, NOW)
    assert not view.can_approve and "denylist" in view.problem


def test_action_denied_now_cannot_be_approved(env):
    a = propose_action(env)
    svc = action_service(env, deny=lambda command, alias: ("wg", "no"))
    view = svc.view_action("KD-123", a.proposal_id, NOW)
    assert not view.can_approve and "rule" in view.problem


def test_changed_card_blocks_action_approval_and_consume(env):
    a = propose_action(env)
    svc = action_service(env)
    view = svc.view_action("KD-123", a.proposal_id, NOW)
    grant = approve_action(svc, view)
    write_card(env, ONE_SERVER.replace("Restart the app", "Restart and wipe the app"))
    with pytest.raises(admin_grant.GrantError, match="scope"):
        svc.consume("KD-123", a.proposal_id, grant.action_sha256, NOW)
    with pytest.raises(admin_approval.Conflict):
        approve_action(svc, view)


def test_inbox_card_moves_to_to_do_on_action_approval(env):
    write_card(env, ONE_SERVER.replace("status: To Do", "status: Inbox"))
    a = propose_action(env)
    svc = action_service(env)
    approve_action(svc, svc.view_action("KD-123", a.proposal_id, NOW))
    assert env.board.promotes() and svc.view_action("KD-123", a.proposal_id, NOW).card.status == "To Do"


def test_revoke_action(env):
    a = propose_action(env)
    svc = action_service(env)
    grant = approve_action(svc, svc.view_action("KD-123", a.proposal_id, NOW))
    assert svc.revoke_action("KD-123", a.proposal_id, action_id=grant.action_id, via="web", now=NOW).state == "revoked"
    with pytest.raises(admin_grant.GrantError, match="revoked"):
        svc.consume("KD-123", a.proposal_id, grant.action_sha256, NOW)


def test_renamed_action_file_is_refused(env):
    a = propose_action(env)
    other = "0" * 16
    src = env / "proposals" / f"KD-123.action-{a.proposal_id}.json"
    src.rename(env / "proposals" / f"KD-123.action-{other}.json")
    view = action_service(env).view_action("KD-123", other, NOW)
    assert view.action is None and not view.can_approve and "id" in view.problem


# --- the signed text kept in the grant store for the exec user -------------------------------------------

def test_approve_keeps_the_signed_plan_beside_the_grant(env):
    propose(env)
    svc = service(env)
    grant = approve(svc, svc.view("KD-123", NOW))
    path = Path(admin_signed.plan_path(str(env / "grants"), "KD-123", grant.proposal_sha256))
    info = path.lstat()
    assert stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o640 and info.st_uid == os.getuid()
    kept = admin_signed.load_plan(str(env / "grants"), "KD-123", grant.proposal_sha256, os.getuid())
    assert admin_signed.digest(kept.data) == grant.proposal_sha256 and kept.proposed_by == "sysadmin"


def test_no_signed_copy_means_no_grant(env, monkeypatch):
    propose(env)
    svc = service(env)
    view = svc.view("KD-123", NOW)

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(admin_signed, "write_plan", fail)
    with pytest.raises(admin_approval.Unavailable, match="disk full"):
        approve(svc, view)
    assert svc.view("KD-123", NOW).grant is None


def test_approve_action_keeps_the_signed_action(env):
    a = propose_action(env)
    svc = action_service(env)
    approve_action(svc, svc.view_action("KD-123", a.proposal_id, NOW))
    kept = admin_signed.load_action(str(env / "grants"), "KD-123", a.proposal_id, os.getuid())
    assert kept.data["command"] == ACTION["command"]


def old_grant(env, svc):
    """A grant as the cabinet wrote it before the change: no signed copy beside it."""
    view = svc.view("KD-123", NOW)
    return svc.store.approve(view.card, view.proposal, now=NOW, ttl=admin_grant.DEFAULT_TTL, via="web",
                             expect_card_sha256=view.card.scope_sha256, expect_proposal_sha256=view.proposal.sha256)


def test_backfill_keeps_the_plan_of_a_live_grant(env):
    propose(env)
    svc = service(env)
    grant = old_grant(env, svc)
    assert svc.backfill(NOW) == ["KD-123: signed plan kept"]
    kept = admin_signed.load_plan(str(env / "grants"), "KD-123", grant.proposal_sha256, None)
    assert admin_signed.digest(kept.data) == grant.proposal_sha256
    assert svc.backfill(NOW) == []  # once


def test_backfill_without_a_matching_plan_leaves_the_grant_to_a_new_approval(env):
    propose(env)
    svc = service(env)
    grant = old_grant(env, svc)
    propose(env, intent="Другое намерение")  # the card's current plan is another one …
    os.unlink(admin_proposal.plan_path(str(env / "proposals"), "KD-123", grant.proposal_sha256))  # … and no hash copy
    assert svc.backfill(NOW) == ["KD-123: no plan with the grant's hash; its steps need a new approval"]
    with pytest.raises(admin_signed.SignedError, match="new approval"):
        admin_signed.load_plan(str(env / "grants"), "KD-123", grant.proposal_sha256, None)


def test_backfill_skips_expired_and_revoked_grants(env):
    propose(env)
    svc = service(env)
    grant = old_grant(env, svc)
    assert svc.backfill(grant.expires_at) == []
    svc.store.revoke("KD-123", now=NOW, via="web")
    assert svc.backfill(NOW) == []


def test_backfill_keeps_a_live_action(env):
    a = propose_action(env)
    svc = action_service(env)
    view = svc.view_action("KD-123", a.proposal_id, NOW)
    svc.actions.approve(view.card, view.action, now=NOW, via="web", expect_card_sha256=view.card.scope_sha256,
                        expect_action_sha256=view.action.sha256)
    assert svc.backfill(NOW) == [f"KD-123: signed action {a.proposal_id} kept"]
    assert admin_signed.load_action(str(env / "grants"), "KD-123", a.proposal_id, None).data["command"] == ACTION["command"]


def test_backfill_survives_stray_files_and_skips_factory_grants(env):
    propose(env)
    svc = service(env)
    old_grant(env, svc)
    grants = env / "grants"
    (grants / "KD-0.json").write_text("{}", encoding="utf-8")  # not a card id: ignored
    (grants / "KD-5.json").write_text("{}", encoding="utf-8")  # a broken grant: said, not fatal
    factory = json.loads((grants / "KD-123.json").read_text("utf-8"))
    (grants / "KD-7.json").write_text(json.dumps(dict(factory, task_id="KD-7", assignee="dev", project="digipaddock")),
                                      encoding="utf-8")
    lines = svc.backfill(NOW)
    assert lines[0] == "KD-123: signed plan kept" and len(lines) == 2
    assert lines[1].startswith("KD-5.json: no copy (KD-5: grant keys")


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root reads any file")
def test_an_unreadable_proposal_store_names_the_missing_right(env):
    propose(env)
    (env / "proposals").chmod(0o600)  # no search right: the file inside cannot be looked up
    try:
        with pytest.raises(admin_proposal.ProposalStoreError) as caught:
            admin_proposal.load(str(env / "proposals"), "KD-123")
    finally:
        (env / "proposals").chmod(0o750)
    assert f"is not readable by {admin_grant.reader()}" in str(caught.value)
    assert "cannot be read" not in str(caught.value)


# --- the test zone — a plan of test environments is approved by rule ----------------------------------------
def zone_service(env, catalog=None, client=None):
    from admin_samples import ZONE_CATALOG

    paths = admin_approval.Paths(proposals=str(env / "proposals"), grants=str(env / "grants"))
    return admin_approval.Service(paths, catalog=lambda: ops.Catalog.from_dict(copy.deepcopy(catalog or ZONE_CATALOG)),
                                  inventory=lambda: INVENTORY, enabled=lambda: ENABLED, client=client or env.board)


def zone_propose(env, data=None):
    from admin_samples import ZONE_CATALOG, zone_proposal

    return admin_proposal.submit(str(env / "proposals"), json.dumps(data or zone_proposal()).encode("utf-8"),
                                 agent="sysadmin", card=env.board.card(),
                                 catalog=ops.Catalog.from_dict(copy.deepcopy(ZONE_CATALOG)),
                                 inventory=INVENTORY, enabled=ENABLED, now=NOW)


def by_rule(svc, plan, env, **over):
    args = dict(card_sha256=env.board.card().scope_sha256, proposal_sha256=plan.sha256, now=NOW)
    args.update(over)
    return svc.approve_test_zone("KD-123", **args)


def test_a_plan_of_test_environments_is_approved_by_rule_for_a_day(env):
    plan = zone_propose(env)
    svc = zone_service(env)
    zone = by_rule(svc, plan, env)
    assert zone.fresh and zone.envs == ("shop-test",) and zone.steps == 2
    grant = zone.grant
    assert grant.approved_by == admin_grant.TEST_ZONE and grant.signature == "" and grant.signer == ""
    assert grant.expires_at - grant.approved_at == timedelta(hours=24) == admin_approval.TEST_ZONE_TTL
    assert svc.view("KD-123", NOW).usable
    entry = json.loads((env / "grants" / "decisions.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert (entry["via"], entry["by"]) == ("rule", "rule:test-env")
    # the signed-plan copy the executor reads is written for a rule grant too
    assert admin_signed.load_plan(str(env / "grants"), "KD-123", plan.sha256, None).data == plan.as_dict()


def test_the_rule_moves_an_inbox_card_to_to_do_quietly(env):
    write_card(env, ONE_SERVER.replace("status: To Do", "status: Inbox"))
    plan = zone_propose(env)
    by_rule(zone_service(env), plan, env)
    assert env.board.promotes() == []  # not the owner's start: the cabinet records it as its own
    (promote,) = [e for e in env.board.cab.store.events_since(0)
                  if e["kind"] == "card.changed" and e["payload"].get("after", {}).get("status") == "To Do"]
    assert promote["actor"] == "system" and promote["payload"].get("wake") is False


def test_an_inbox_card_with_a_brief_is_not_the_rules_to_start(env):
    """The start of a card with a brief approves that brief in a person's name — the rule leaves
    such a card's plan to the owner."""
    write_card(env, ONE_SERVER.replace("status: To Do", "status: Inbox"))
    plan = zone_propose(env)
    env.board.cab.store.edit_card("KD-123", None, {"labels_add": ["brief-ready"]}, actor="agent:sysadmin")
    svc = zone_service(env)
    assert by_rule(svc, plan, env) is None
    assert env.board.card().status == "Inbox" and svc.view("KD-123", NOW).grant is None


def test_a_plan_with_any_other_step_is_not_the_rules(env):
    from admin_samples import ZONE_STEPS, explain, zone_proposal

    write_card(env, ONE_SERVER.replace("status: To Do", "status: Inbox"))
    mixed = zone_proposal(operations=ZONE_STEPS + [explain({"server": "node-c", "verb": "service.restart",
                                                            "unit": "nginx.service"})])
    plan = zone_propose(env, mixed)
    svc = zone_service(env)
    assert by_rule(svc, plan, env) is None
    assert svc.view("KD-123", NOW).grant is None and env.board.promotes() == []
    assert env.board.card().status == "Inbox"


def test_a_plan_is_not_the_rules_once_the_catalog_drops_the_mark(env):
    from admin_samples import ZONE_CATALOG

    plan = zone_propose(env)
    unmarked = copy.deepcopy(ZONE_CATALOG)
    del unmarked["apps"]["shop-test"]["test"], unmarked["apps"]["shop-test"]["reload"]
    assert by_rule(zone_service(env, catalog=unmarked), plan, env) is None


def test_asking_the_rule_again_answers_the_live_approval(env):
    plan = zone_propose(env)
    svc = zone_service(env)
    first = by_rule(svc, plan, env)
    again = by_rule(svc, plan, env, now=NOW + timedelta(hours=1))
    assert again.grant.grant_id == first.grant.grant_id and not again.fresh
    late = by_rule(svc, plan, env, now=NOW + timedelta(hours=25))  # the day is over: a new approval
    assert late.fresh and late.grant.grant_id != first.grant.grant_id


def test_the_rule_checks_what_the_agent_was_shown(env):
    plan = zone_propose(env)
    svc = zone_service(env)
    with pytest.raises(admin_approval.Conflict):
        by_rule(svc, plan, env, proposal_sha256="0" * 64)
    with pytest.raises(admin_approval.Conflict):
        by_rule(svc, plan, env, card_sha256="0" * 64)
    assert svc.view("KD-123", NOW).grant is None


@pytest.mark.parametrize("status", ["Done", "Review", "Blocked"])
def test_the_rule_approves_nothing_on_a_card_out_of_work(env, status):
    plan = zone_propose(env)
    write_card(env, ONE_SERVER.replace("status: To Do", f"status: {status}"))
    svc = zone_service(env)
    with pytest.raises(admin_approval.Refused):
        by_rule(svc, plan, env)
    assert svc.view("KD-123", NOW).grant is None


def test_a_revoked_approval_sends_the_cards_next_plan_to_the_owner(env):
    """The owner took the card's approval back — the rule does not hand it out again, for the same
    plan or a reworded one: the card's next plan is his to decide."""
    from admin_samples import ZONE_STEPS, zone_proposal

    plan = zone_propose(env)
    svc = zone_service(env)
    by_rule(svc, plan, env)
    svc.revoke("KD-123", grant_id=None, via="cli", now=NOW + timedelta(minutes=5))
    later = NOW + timedelta(minutes=10)
    assert by_rule(svc, plan, env, now=later) is None
    reworded = zone_propose(env, zone_proposal(operations=[{**ZONE_STEPS[0], "what": "То же, другими словами"}]))
    assert reworded.sha256 != plan.sha256 and by_rule(svc, reworded, env, now=later) is None
    assert svc.view("KD-123", later).grant.state == "revoked"
