"""The owner's grant lets BOSS create and start a bounded number of subtasks of one goal.

the board is the cabinet, reached as the base user (the executor) the way kd-boss-board does.
"""

import json
import os
import sys
import threading
from pathlib import Path

import pytest

try:
    import fcntl
except ImportError:  # Windows: the hold is a no-op there
    fcntl = None

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cabinet_env import CabinetEnv  # noqa: E402

from kd import boss_exec, delegation  # noqa: E402
from kd.cabinet import client  # noqa: E402

NOW = 1_800_000_000.0
GID = "a" * 32
DAY = 86400
GOAL = "KD-1"


class Failing:
    """The executor's client; ``fail`` names one call (``boss.create-child`` or ``boss.promote``) that fails once."""

    def __init__(self, inner):
        self.inner, self.fail, self.calls = inner, None, []

    def call(self, request):
        self.calls.append(request["op"])
        if request["op"] == self.fail:
            self.fail = None
            raise client.CabinetError("unavailable", "board failed")
        return self.inner.call(request)


class Board:
    """A cabinet with its projects and a goal card in Inbox, the grants directory and the children journal."""

    def __init__(self, tmp_path):
        self.cab = CabinetEnv(tmp_path / "cab")
        self.client = Failing(client.LocalClient(self.cab.ctx, self.cab.principal("digi")))
        self.audit = tmp_path / "boss.jsonl"
        self.grants = tmp_path / "grants"
        self.grants.mkdir()
        self.journal = tmp_path / "delegation.jsonl"
        assert self.cab.card(title="Большая цель", status="Inbox", assignee="").id == GOAL

    def record(self, card_id):
        return self.cab.store.card(card_id)

    def status(self, card_id):
        return self.record(card_id).status

    def set_status(self, card_id, status):
        card = self.record(card_id)
        self.cab.store.edit_card(card_id, card.revision, {"status": status}, actor="user:owner")

    def creates(self):
        return [op for op in self.client.calls if op == "boss.create-child"]

    def grant(self, gid=GID, goal=GOAL, projects=None, max_children=5, max_active=3, days=7, state="active"):
        grant = delegation.Grant(
            id=gid, goal=goal, projects=projects or {"digipaddock": "@dev", "admin": "@sysadmin"},
            max_children=max_children, max_active=max_active, created=NOW - 60, expires=NOW - 60 + days * DAY,
            state=state, approved_by="owner",
        )
        delegation.write_grant(str(self.grants), grant)
        return grant

    def child(self, title="Сделать часть A", project="digipaddock", now=NOW, goal=GOAL, owner_uid=None):
        return delegation.create_child(
            goal=goal, title=title, project=project, reason="часть цели", now=now,
            grants_root=str(self.grants), children_path=str(self.journal), client=self.client,
            registry_path=str(self.cab.registry),
            audit_path=str(self.audit), caller="digi-boss", owner_uid=owner_uid,
        )

    def phases(self):
        return [json.loads(x)["phase"] for x in self.journal.read_text("utf-8").splitlines()]

    def audit_ops(self):
        return [
            (r["op"], r.get("grant"))
            for r in map(json.loads, self.audit.read_text("utf-8").splitlines())
            if r["phase"] == "intent"
        ]


@pytest.fixture
def board(tmp_path):
    return Board(tmp_path)


def refused(board, code=boss_exec.EXIT_REFUSED, **kw):
    with pytest.raises(boss_exec.Failure) as info:
        board.child(**kw)
    assert info.value.code == code
    return str(info.value)


def test_a_child_is_created_in_inbox_and_started_by_the_grant(board):
    board.grant()
    assert board.child() == ("KD-2", "started")
    card = board.record("KD-2")
    assert (card.status, card.assignee, card.project, card.created_by) == ("To Do", "@dev", "digipaddock", "@boss")
    assert "Подзадача цели KD-1 (разрешение владельца aaaaaaaa)" in card.description
    assert board.phases() == ["intent", "created", "started"]
    assert board.audit_ops() == [("create-child", GID), ("promote", GID)]
    # the cabinet keeps the owner's grant on the promote event
    (start,) = [e for e in board.cab.store.events_since(0) if e["card"] == "KD-2" and e["kind"] == "card.changed"]
    assert start["payload"]["grant"] == GID and start["actor"] == "agent:boss"
    assert board.status(GOAL) == "Inbox"  # the goal itself is never moved by the grant


def test_the_same_title_again_creates_nothing(board):
    board.grant()
    board.child()
    assert board.child() == ("KD-2", "already")
    assert len(board.creates()) == 1


def test_a_retry_after_a_failed_promote_starts_the_inbox_card(board):
    board.grant()
    board.client.fail = "boss.promote"
    refused(board, code=boss_exec.EXIT_ERROR)
    assert board.status("KD-2") == "Inbox"
    assert board.child() == ("KD-2", "started")
    assert len(board.creates()) == 1
    assert board.status("KD-2") == "To Do"


def test_an_intent_without_a_card_is_uncertain_and_keeps_its_quota(board):
    board.grant(max_children=1)
    board.client.fail = "boss.create-child"
    refused(board, code=boss_exec.EXIT_ERROR)
    assert "uncertain" in refused(board, code=boss_exec.EXIT_ERROR)
    assert "subtasks" in refused(board, title="Другая часть")
    assert len(board.creates()) == 1


def test_the_quota_of_children_counts_done_ones_too(board):
    board.grant(max_children=2, max_active=2)
    board.child(title="Часть 1")
    board.child(title="Часть 2")
    board.set_status("KD-2", "Done")
    assert "2 subtasks" in refused(board, title="Часть 3")


def test_active_children_are_limited_until_one_is_done(board):
    board.grant(max_children=5, max_active=1)
    board.child(title="Часть 1")
    assert "active" in refused(board, title="Часть 2")
    board.set_status("KD-2", "Done")
    assert board.child(title="Часть 2") == ("KD-3", "started")


@pytest.mark.parametrize("project", ["admin", "web", "nope"])
def test_a_project_outside_the_grant_is_refused(board, project):
    board.grant(projects={"digipaddock": "@dev"})
    assert "not in the owner's grant" in refused(board, project=project)
    assert not board.journal.exists()


def test_a_route_changed_since_the_grant_is_refused(board):
    board.grant()
    # the owner rerouted the project in the cabinet; the grant's snapshot is stale — no restart needed
    board.cab.call("root", {"op": "project.update", "id": "digipaddock", "route": "@sysadmin"})
    assert "route_mismatch" in refused(board)
    assert board.creates() == []


def test_a_deleted_project_leaves_the_grant(board):
    board.grant()
    board.cab.ctx.promote = type("Promote", (), {"grants": str(board.grants)})()
    board.cab.call("root", {"op": "project.delete", "id": "admin"})
    assert delegation.load_grant(str(board.grants), GID).projects == {"digipaddock": "@dev"}
    assert "not in the owner's grant" in refused(board, project="admin")


@pytest.mark.parametrize("case", ["none", "revoked", "expired", "other goal", "foreign owner"])
def test_without_an_active_grant_of_this_goal_nothing_is_created(board, case):
    kw = {}
    if case == "revoked":
        board.grant()
        delegation.revoke(str(board.grants), GID, "owner", NOW - 1)
    elif case == "expired":
        board.grant(days=1)
        kw["now"] = NOW + 2 * DAY
    elif case == "other goal":
        board.grant(goal="KD-8")
    elif case == "foreign owner":
        board.grant()
        kw["owner_uid"] = os.stat(board.grants / f"{GID}.json").st_uid + 1
    assert "no active owner grant" in refused(board, **kw)
    assert board.creates() == []


def test_an_invalid_title_is_refused_before_any_record(board):
    board.grant()
    refused(board, title="-x")
    assert not board.journal.exists()


def test_a_rate_limited_create_writes_no_intent(board):
    board.grant()
    board.audit.write_text(
        "".join(json.dumps({"phase": "intent", "at": NOW - 60, "op": "create-inbox"}) + "\n" for _ in range(20)),
        encoding="utf-8",
    )
    refused(board, code=boss_exec.EXIT_RATE)
    assert not board.journal.exists()  # final review: the limit is checked before the intent is written


def test_grant_of_child_is_read_from_the_journal_only(board):
    board.grant()
    board.child()
    other = board.cab.card(title="Чужая", description="[boss] Подзадача цели KD-1 (разрешение владельца aaaaaaaa)")
    assert delegation.grant_of_child(str(board.journal), "KD-2") == GID
    assert delegation.grant_of_child(str(board.journal), other.id) is None
    assert delegation.grant_of_child(str(board.journal), GOAL) is None
    assert [(c["card"], c["state"]) for c in delegation.children(str(board.journal), GID)] == [("KD-2", "started")]


def test_revoke_is_final_and_keeps_the_first_revoker(board):
    board.grant()
    first = delegation.revoke(str(board.grants), GID, "owner", NOW)
    again = delegation.revoke(str(board.grants), GID, "someone", NOW + 5)
    assert (first.state, again.revoked_by, again.revoked) == ("revoked", "owner", NOW)
    assert not again.active(NOW)


def test_active_grant_for_prefers_the_newest(board):
    board.grant(gid="b" * 32)
    newer = delegation.Grant(**{**delegation.load_grant(str(board.grants), "b" * 32).__dict__, "id": "c" * 32,
                                "created": NOW})
    delegation.write_grant(str(board.grants), newer)
    assert delegation.active_grant_for(str(board.grants), GOAL, NOW).id == "c" * 32
    assert delegation.active_grant_for(str(board.grants), "KD-9", NOW) is None


@pytest.mark.parametrize("raw", ['{"id": 1}', "not json", ""])
def test_an_unreadable_grant_is_an_error(board, raw):
    (board.grants / f"{GID}.json").write_text(raw, encoding="utf-8")
    with pytest.raises(delegation.GrantError):
        delegation.load_grant(str(board.grants), GID)


def test_the_repo_limits_are_the_spec_defaults():
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "coordination")
    assert delegation.load_limits(os.path.join(root, "delegation.json")) == delegation.Limits(5, 3, 7)


@pytest.mark.parametrize(
    "raw", [{"maxChildren": 0, "maxActive": 1, "days": 7}, {"maxChildren": 2, "maxActive": 3, "days": 7},
            {"maxChildren": 5, "maxActive": 3}, {"maxChildren": True, "maxActive": 1, "days": 7}]
)
def test_invalid_limits_are_refused(tmp_path, raw):
    path = tmp_path / "delegation.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        delegation.load_limits(str(path))


def test_a_retry_refuses_a_child_moved_outside_the_grant(board):
    board.grant(projects={"digipaddock": "@dev"})
    board.client.fail = "boss.promote"
    refused(board, code=boss_exec.EXIT_ERROR)
    card = board.record("KD-2")
    board.cab.store.edit_card("KD-2", card.revision, {"project": "admin", "assignee": "@sysadmin"}, actor="user:owner")
    assert "outside the grant" in refused(board)
    assert board.status("KD-2") == "Inbox"


def test_a_large_journal_is_compacted_without_losing_children(board, monkeypatch):
    board.grant()
    board.child(title="Часть 1")
    board.child(title="Часть 2")
    monkeypatch.setattr(delegation, "COMPACT_AT", 10)
    board.child(title="Часть 3")
    lines = board.journal.read_text("utf-8").splitlines()
    assert len(lines) == 3  # one line per child
    assert [(c["title"], c["card"], c["state"]) for c in delegation.children(str(board.journal), GID)] == [
        ("Часть 1", "KD-2", "started"), ("Часть 2", "KD-3", "started"), ("Часть 3", "KD-4", "started"),
    ]
    assert delegation.grant_of_child(str(board.journal), "KD-3") == GID
    assert board.child(title="Часть 2") == ("KD-3", "already")


def test_a_revoke_between_the_check_and_the_hold_is_seen(board, monkeypatch):
    # The grant is read again under its hold, so a revoke just before it stops the create
    stale = board.grant()
    delegation.revoke(str(board.grants), GID, "owner", NOW)
    monkeypatch.setattr(delegation, "active_grant_for", lambda *args, **kwargs: stale)
    message = refused(board)
    assert "revoked or expired" in message and board.creates() == [] and not board.journal.exists()


@pytest.mark.skipif(fcntl is None, reason="flock")
def test_a_revoke_waits_for_a_running_create(board):
    board.grant()
    done = threading.Event()

    def owner_revokes():
        delegation.revoke(str(board.grants), GID, "owner", NOW)
        done.set()

    with delegation.held(str(board.grants), GID, shared=True):  # a create-child between its check and its start
        worker = threading.Thread(target=owner_revokes)
        worker.start()
        assert not done.wait(0.3)
    worker.join(5)
    assert done.is_set() and delegation.load_grant(str(board.grants), GID).state == "revoked"


def test_a_host_without_the_approval_user_trusts_no_grant():
    def missing(name):
        raise KeyError(name)

    assert delegation.owner_uid(getpwnam=missing) == -1


def test_the_grants_belong_to_the_cabinets_service_user():
    from kd import naming

    asked = []

    def lookup(name):
        asked.append(name)
        return type("pw", (), {"pw_uid": 991})()

    assert delegation.owner_uid(getpwnam=lookup) == 991 and asked == [naming.service_user("cabinet")]
