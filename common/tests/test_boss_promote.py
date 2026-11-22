"""BOSS only proposes a promote; the owner's POST on the page runs the one Inbox -> To Do edit.
A ``delegate`` proposal becomes the owner's grant to split the goal; the goal itself is not moved.
the page writes through the cabinet as the approve user (``card.promote``, the goal's ``boss.comment``)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cabinet_env import CabinetEnv  # noqa: E402

from kd import boss_board, boss_exec, boss_promote, delegation  # noqa: E402
from kd.cabinet import client  # noqa: E402

NOW = 1_800_000_000.0
CARD = "KD-1"


class Racing:
    """The approve user's client; ``before_write`` runs once just before the first board write."""

    def __init__(self, inner):
        self.inner, self.before_write = inner, None

    def call(self, request):
        if self.before_write and request["op"] != "card.get":
            hook, self.before_write = self.before_write, None
            hook()
        return self.inner.call(request)


class Page:
    """The cabinet with one Inbox card, the routing and the page's stores, reached as the approve user."""

    def __init__(self, tmp_path):
        self.cab = CabinetEnv(tmp_path / "cab")
        self.client = Racing(client.LocalClient(self.cab.ctx, self.cab.principal("digi-cabinet")))
        self.audit = tmp_path / "boss.jsonl"
        self.switch = tmp_path / "base.json"
        self.switch.write_text("{}", encoding="utf-8")
        self.proposals = tmp_path / "proposals"
        self.decisions = tmp_path / "decisions"
        self.grants = tmp_path / "grants"
        self.children = tmp_path / "delegation.jsonl"
        for path in (self.proposals, self.decisions, self.grants):
            path.mkdir()

    def card(self, **over):
        values = dict(title="Починить выкатку", status="Inbox", assignee="@dev", project="digipaddock")
        values.update(over)
        record = self.cab.card(**values)
        assert record.id == CARD
        return record

    def record(self):
        return self.cab.store.card(CARD)

    def touch(self, **changes):
        """Someone else edits the card (a new revision)."""
        record = self.record()
        self.cab.store.edit_card(CARD, record.revision, changes or {"description": f"правка {record.revision}"},
                                 actor="user:other")

    def writes(self):
        """The page's board writes (not the fixtures' ones)."""
        return [e for e in self.cab.store.events_since(0) if e["kind"] == "card.changed" and e["actor"] != "user:other"]

    def settings(self):
        return boss_promote.Settings(
            proposals=str(self.proposals), decisions=str(self.decisions), client=self.client,
            registry_path=str(self.cab.registry), audit_path=str(self.audit),
            channel_switch=str(self.switch), grants=str(self.grants), children=str(self.children),
        )

    def _read(self):
        return (boss_exec.load_card(self.client, CARD),
                boss_exec.load_routing(self.client, str(self.cab.registry)))

    def propose(self, reason="готова к работе", now=NOW):
        card, routing = self._read()
        return boss_promote.propose(card, routing, card.revision, reason, now=now, root=str(self.proposals),
                                    decisions=str(self.decisions))

    def propose_delegate(self, reason="большая цель", now=NOW, limits=delegation.Limits(5, 3, 7)):
        card, routing = self._read()
        return boss_promote.propose_delegate(card, routing, card.revision, reason, limits, now=now,
                                             root=str(self.proposals), decisions=str(self.decisions))

    def decide(self, proposal, approve=True, *, sha=None, now=NOW + 60, caller="owner"):
        s = self.settings()
        return boss_promote.decide(
            proposal.id, sha or proposal.sha256, approve, client=s.client,
            registry_path=s.registry_path, audit_path=s.audit_path, caller=caller, now=now,
            proposals=s.proposals, decisions=s.decisions, channel_switch=s.channel_switch, grants=s.grants,
        )

    def decision(self, proposal):
        path = self.decisions / f"{proposal.id}.json"
        return json.loads(path.read_text("utf-8")) if path.exists() else None

    def audit_records(self):
        return [json.loads(x) for x in self.audit.read_text("utf-8").splitlines()] if self.audit.exists() else []


@pytest.fixture
def fake(tmp_path):
    return Page(tmp_path)


def test_a_repeated_proposal_of_the_same_revision_reuses_the_live_one(fake):
    # BOSS repeating itself gives the owner one link, not a file per call
    fake.card()
    first = fake.propose()
    assert fake.propose(now=NOW + 60) == first
    assert sorted(p.name for p in fake.proposals.iterdir()) == [f"{first.id}.json"]
    later = fake.propose(now=first.expires + 1)
    assert later.id != first.id and len(list(fake.proposals.iterdir())) == 2


def test_a_new_reason_gets_a_new_proposal(fake):
    # The owner sees the reason BOSS gives now, not the one of an hour ago
    fake.card()
    first = fake.propose()
    second = fake.propose(reason="владелец просил предложить снова", now=NOW + 60)
    assert second.id != first.id and second.reason == "владелец просил предложить снова"


@pytest.mark.parametrize("approve", [False, True])
def test_a_decided_proposal_is_not_reused(fake, approve):
    # Not reused after a decision: a rejected proposal proposed again gets a fresh link
    fake.card()
    first = fake.propose()
    fake.decide(first, approve=False)
    second = fake.propose(now=NOW + 120)
    assert second.id != first.id
    assert fake.decide(second, approve=approve, now=NOW + 180) == ("approved" if approve else "rejected")


def test_an_unreadable_decision_store_is_not_taken_for_an_open_proposal(fake, monkeypatch):
    # the proposer only traverses the decision directory; if it cannot tell, it makes a new proposal
    fake.card()
    first = fake.propose()
    real = boss_promote.os.lstat

    def lstat(path, *a, **kw):
        if str(path).startswith(str(fake.decisions)):
            raise PermissionError(13, "denied")
        return real(path, *a, **kw)

    monkeypatch.setattr(boss_promote.os, "lstat", lstat)
    assert fake.propose(now=NOW + 60).id != first.id


def test_proposing_removes_proposals_expired_long_ago(fake):
    fake.card()
    old = fake.propose(now=NOW - boss_promote.KEEP - boss_promote.TTL - 1)
    recent = fake.propose(now=NOW - boss_promote.TTL - 60)
    (fake.proposals / "notes.txt").write_text("x", encoding="utf-8")
    new = fake.propose()
    names = sorted(p.name for p in fake.proposals.iterdir())
    assert names == sorted([f"{recent.id}.json", f"{new.id}.json", "notes.txt"])
    assert old.id not in "".join(names)


def test_a_proposal_lives_as_long_as_its_decision_in_the_cabinet():
    # the owner decides in the cabinet (push, reminders at 4 h and 24 h); a proposal that died after an hour
    # left an open decision whose approval was refused ("the proposal's term has expired")
    from kd.cabinet import decisions

    assert boss_promote.TTL == decisions.TTL_SECONDS["promote"] == decisions.TTL_SECONDS["delegate"]


def test_propose_stores_the_proposal_and_changes_nothing(fake):
    card = fake.card()
    p = fake.propose()
    assert fake.writes() == [] and fake.record().revision == card.revision and fake.audit_records() == []
    assert (p.card, p.revision, p.title, p.project, p.assignee, p.reason) == (
        CARD, card.revision, "Починить выкатку", "digipaddock", "@dev", "готова к работе")
    assert p.created == NOW and p.expires == NOW + boss_promote.TTL == NOW + 72 * 3600
    assert boss_promote.load(p.id, str(fake.proposals)) == p


@pytest.mark.parametrize(
    "over, message",
    [
        ({"status": "To Do"}, "not in Inbox"),
        ({"assignee": ""}, "exactly one known assignee"),
        ({"project": "nowhere"}, "not in routes"),
    ],
)
def test_propose_refuses_a_card_that_is_not_ready(fake, over, message):
    fake.card(**over)
    with pytest.raises(boss_board.Refused, match=message):
        fake.propose()
    assert list(fake.proposals.iterdir()) == []


@pytest.mark.parametrize("revision", ["a" * 64, True, 0, "3"])
def test_a_proposal_of_a_non_cabinet_revision_is_not_readable(fake, revision):
    # a stored proposal names the cabinet's integer revision; an old Backlog hash is not a proposal
    fake.card()
    p = fake.propose()
    path = fake.proposals / f"{p.id}.json"
    raw = json.loads(path.read_text("utf-8"))
    raw["revision"] = revision
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(boss_promote.Missing):
        boss_promote.load(p.id, str(fake.proposals))


def test_owner_approval_runs_one_write_and_records_the_decision(fake):
    card = fake.card()
    p = fake.propose()
    assert fake.decide(p) == "approved"
    (write,) = fake.writes()
    after = fake.record()
    assert (write["actor"], after.status, after.revision) == ("user:owner", "To Do", card.revision + 1)
    intent, result = fake.audit_records()
    assert (intent["phase"], intent["op"], intent["proposal"], intent["caller"], intent["revision"]) == (
        "intent", "promote", p.id, "owner", card.revision)
    assert (result["phase"], result["status"], result["proposal"], result["newRevision"]) == (
        "result", 0, p.id, card.revision + 1)
    decision = fake.decision(p)
    assert decision["decision"] == "approved" and decision["card"] == CARD
    assert decision["newRevision"] == after.revision


def test_the_person_who_approved_is_the_author_of_the_promote(fake):
    # a partner with decide.promote approves in the cabinet — the card names her, not the owner
    card = fake.card()
    p = fake.propose()
    assert fake.decide(p, caller="user:anna") == "approved"
    (write,) = fake.writes()
    assert write["actor"] == "user:anna" and fake.record().revision == card.revision + 1
    last = fake.cab.store.messages(CARD)[-1]
    assert last["author"] == "user:anna"
    # the reason is BOSS's, not hers — the comment says whose proposal she approved
    assert last["body"].startswith(f"[anna] To Do: одобрено предложение @boss, исполнитель {card.assignee}. "
                                   f"Основание @boss: {p.reason}")


def test_a_second_post_changes_nothing(fake):
    fake.card()
    p = fake.propose()
    assert fake.decide(p) == "approved"
    assert fake.decide(p) == "already-decided" and fake.decide(p, approve=False) == "already-decided"
    assert len(fake.writes()) == 1 and fake.decision(p)["decision"] == "approved"


def test_rejection_changes_nothing_and_is_final(fake):
    fake.card()
    p = fake.propose()
    assert fake.decide(p, approve=False) == "rejected"
    assert fake.writes() == [] and fake.record().status == "Inbox"
    assert fake.decision(p)["decision"] == "rejected"
    assert fake.decide(p) == "already-decided" and fake.writes() == []


@pytest.mark.parametrize("case", ["expired", "other hash", "card changed"])
def test_a_stale_proposal_is_refused_without_spending_the_decision(fake, case):
    fake.card()
    p = fake.propose()
    kwargs = {}
    if case == "expired":
        kwargs["now"] = p.expires
    elif case == "other hash":
        kwargs["sha"] = "0" * 64
    else:
        fake.touch()
    with pytest.raises(boss_promote.Conflict) as refused:
        fake.decide(p, **kwargs)
    assert refused.value.reason == {"expired": boss_promote.EXPIRED, "other hash": boss_promote.PROPOSAL_CHANGED,
                                    "card changed": boss_promote.CARD_CHANGED}[case]
    assert fake.writes() == [] and fake.decision(p) is None and fake.audit_records() == []
    if case == "other hash":
        assert fake.decide(p) == "approved"  # the owner refreshes the page and decides again


def test_a_switched_off_boss_channel_refuses_the_change_until_it_is_back(fake):
    # a release reinstalling BOSS switches it off for minutes — a retry, not a stale proposal
    fake.card()
    p = fake.propose()
    fake.switch.unlink()
    with pytest.raises(boss_promote.Unavailable, match="канал BOSS выключен") as refused:
        fake.decide(p)
    assert refused.value.reason == boss_promote.BOSS_OFF
    assert fake.writes() == [] and fake.decision(p) is None
    fake.switch.write_text("{}", encoding="utf-8")
    assert fake.decide(p) == "approved"


def test_a_failed_decision_record_after_the_write_is_uncertain_and_never_repeats(fake, monkeypatch):
    fake.card()
    p = fake.propose()
    real = boss_promote.atomic_file.replace

    def broken(path, data, mode):
        if path.endswith(f"{p.id}.json"):
            raise OSError("disk full")
        return real(path, data, mode)

    monkeypatch.setattr(boss_promote.atomic_file, "replace", broken)
    with pytest.raises(boss_promote.Uncertain, match="boss.jsonl"):
        fake.decide(p)
    monkeypatch.setattr(boss_promote.atomic_file, "replace", real)
    with pytest.raises(boss_promote.Conflict):
        fake.decide(p)  # the card has a new revision: no second write
    assert len(fake.writes()) == 1


def test_a_board_conflict_during_the_write_keeps_the_decision_open(fake):
    fake.card()
    p = fake.propose()
    fake.client.before_write = fake.touch  # someone edits after the page's checks
    with pytest.raises(boss_promote.Conflict):
        fake.decide(p)
    assert fake.decision(p) is None and fake.record().status == "Inbox"


def test_the_approve_user_promotes_only_through_card_promote(fake):
    # the cabinet keeps BOSS's own promote for the executor with a grant; the page has no boss.* write but its comment
    card = fake.card()
    for op in ("boss.promote", "boss.assign"):
        with pytest.raises(client.CabinetError) as caught:
            fake.client.call({"op": op, "agent": "boss", "task": CARD, "revision": card.revision, "reason": "x",
                              "assignee": "@sysadmin", "grant": "a" * 32})
        assert caught.value.kind == "refused"
    assert fake.writes() == []


@pytest.mark.parametrize("pid", ["../x", "A" * 32, "a" * 31, ""])
def test_unknown_or_malformed_ids_are_missing(fake, pid):
    with pytest.raises(boss_promote.Missing):
        boss_promote.load(pid, str(fake.proposals))


# --- the owner's grant to split a goal ---------------------------------------------------------------


def test_propose_delegate_stores_the_scope_and_changes_nothing(fake):
    card = fake.card(assignee="")
    p = fake.propose_delegate()
    assert (p.kind, p.card, p.revision, p.reason) == ("delegate", CARD, card.revision, "большая цель")
    assert json.loads(p.scope) == {
        "maxChildren": 5, "maxActive": 3, "days": 7, "projects": {"digipaddock": "@dev", "admin": "@sysadmin"},
    }  # projects routed to the owner are not delegable
    assert fake.writes() == [] and boss_promote.load(p.id, str(fake.proposals)) == p
    assert fake.propose_delegate(now=NOW + 60) == p  # a repeat reuses the live one


def test_promote_and_delegate_of_the_same_card_are_different_proposals(fake):
    fake.card()
    assert fake.propose(reason="x").id != fake.propose_delegate(reason="x").id


def test_propose_delegate_refuses_a_done_goal(fake):
    fake.card(status="Done")
    with pytest.raises(boss_board.Refused, match="Done"):
        fake.propose_delegate()


def test_approving_a_delegation_writes_the_grant_and_comments_on_the_goal(fake):
    fake.card(assignee="")
    p = fake.propose_delegate()
    assert fake.decide(p, now=NOW + 60) == "approved"
    grant = delegation.load_grant(str(fake.grants), p.id)
    assert (grant.goal, grant.projects, grant.max_children, grant.max_active, grant.state, grant.approved_by) == (
        CARD, {"digipaddock": "@dev", "admin": "@sysadmin"}, 5, 3, "active", "owner",
    )
    assert grant.expires == NOW + 60 + 7 * 86400
    (write,) = fake.writes()
    assert write["actor"] == "agent:boss" and "status" not in write["payload"]["fields"]  # the goal is not moved
    assert fake.record().status == "Inbox"
    assert any(m["body"].startswith("[boss] владелец разрешил разбить цель") for m in fake.cab.store.messages(CARD))
    assert fake.decision(p)["decision"] == "approved" and fake.decision(p)["grant"] == p.id
    assert fake.decide(p) == "already-decided"


def test_a_failed_goal_comment_keeps_the_grant(fake):
    fake.card()
    p = fake.propose_delegate()
    fake.client.before_write = fake.touch  # the goal changes between the page's checks and its comment
    assert fake.decide(p) == "approved"
    assert delegation.load_grant(str(fake.grants), p.id).state == "active"
    assert fake.writes() == []


def test_a_second_active_grant_of_the_same_goal_is_refused(fake):
    fake.card()
    first = fake.propose_delegate(reason="первая")
    assert fake.decide(first) == "approved"
    second = fake.propose_delegate(reason="вторая", now=NOW + 120)  # on the revision after the goal comment
    with pytest.raises(boss_promote.Conflict, match="уже"):
        fake.decide(second, now=NOW + 180)


@pytest.mark.parametrize("change", [
    {"op": "project.update", "id": "admin", "route": "@dev"},
    {"op": "project.delete", "id": "admin"},
])
def test_a_delegation_over_a_route_changed_since_is_refused_as_route_mismatch(fake, change):
    """the proposal's scope is a snapshot of the routes; the owner rerouted or deleted a project since."""
    fake.card(assignee="")
    p = fake.propose_delegate()
    fake.cab.call("root", change)
    with pytest.raises(boss_promote.Conflict, match="route_mismatch: маршрут проекта admin изменился"):
        fake.decide(p, now=NOW + 60)
    assert list(fake.grants.iterdir()) == [] and fake.decision(p) is None


def test_rejecting_a_delegation_writes_no_grant(fake):
    fake.card()
    p = fake.propose_delegate()
    assert fake.decide(p, approve=False) == "rejected"
    assert list(fake.grants.iterdir()) == [] and fake.writes() == []


def test_revoke_delegation_ends_the_grant(fake):
    fake.card()
    p = fake.propose_delegate()
    fake.decide(p)
    grant = boss_promote.revoke_delegation(p.id, grants=str(fake.grants), caller="owner", now=NOW + 100)
    assert (grant.state, grant.revoked_by) == ("revoked", "owner")
    with pytest.raises(boss_promote.Missing):
        boss_promote.revoke_delegation("b" * 32, grants=str(fake.grants), caller="owner", now=NOW + 100)


def test_a_proposal_without_kind_is_refused(fake):
    fake.card()
    p = fake.propose()
    path = fake.proposals / f"{p.id}.json"
    raw = json.loads(path.read_text("utf-8"))
    del raw["kind"], raw["scope"]
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(boss_promote.Missing):
        boss_promote.load(p.id, str(fake.proposals))
