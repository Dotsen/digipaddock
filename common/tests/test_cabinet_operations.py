"""A factory operation in the cabinet — one row per logical request, checked against
the card's factory grant, idempotent by key, the same work of the release and the lead is one operation, a newer
release supersedes a pending one, the host's report moves it and a failure is a critical notice."""

import sys
from pathlib import Path

import pytest

from kd.cabinet import api, operations

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_cabinet_factory_plan import STEP, approve, env, open_plan  # noqa: E402,F401

REV_A, REV_B = "a" * 40, "b" * 40


@pytest.fixture
def granted(env):
    env.put("KD-50", project="digipaddock", assignee="@dev", status="In Progress")
    approve(env, open_plan(env, steps=({**STEP, "agents": ["boss", "dev"]},)))
    env.ctx.deployed_revision = lambda: REV_A
    return env


def lead(env, key="lead:1", agents=("dev",), card="KD-50", **extra):
    return env.call("digi", {"op": "operation.request", "agent": "dev", "card": card, "kind": "agent.reinstall",
                            "agents": list(agents), "key": key, **extra})["operation"]


def release(env, key=None, agents=("dev",), card="KD-50"):
    request = {"op": "operation.request", "requester": "release", "kind": "agent.reinstall", "agents": list(agents),
               "key": key or f"release:{env.ctx.deployed_revision()}"}
    if card:
        request["card"] = card
    return env.call("root", request)["operation"]


def events(env, kind):
    return [e for e in env.store.events_since(0) if e["kind"] == kind]


def test_request_without_grant_refused(granted):
    granted.put("KD-60", project="digipaddock", assignee="@dev", status="In Progress")
    with pytest.raises(api.ApiError) as exc:
        lead(granted, card="KD-60")
    assert exc.value.kind == "refused" and "approved" in str(exc.value)


def test_request_outside_grant_refused(granted):
    with pytest.raises(api.ApiError) as exc:
        lead(granted, agents=("dev", "sysadmin"))
    assert "sysadmin" in str(exc.value)
    with pytest.raises(api.ApiError):
        lead(granted, agents=("ghost",))


def test_request_of_other_agent_refused(granted):
    with pytest.raises(api.ApiError) as exc:
        granted.call("digi", {"op": "operation.request", "agent": "analyst", "card": "KD-50",
                             "kind": "agent.reinstall", "agents": ["dev"], "key": "x:1"})
    assert exc.value.kind == "refused"


def test_request_wrong_revision_refused(granted):
    with pytest.raises(api.ApiError) as exc:
        lead(granted, revision=REV_B)
    assert "revision_moved" in str(exc.value)
    with pytest.raises(api.ApiError):
        lead(granted, kind_override=True)  # an unknown field is refused, not ignored


def test_request_idempotent_by_key(granted):
    first = lead(granted)
    again = lead(granted)
    assert first["id"] == again["id"] and first["state"] == "pending" and first["revision"] == REV_A
    assert len(events(granted, "operation.queued")) == 1


def test_same_key_other_payload_conflict(granted):
    lead(granted)
    with pytest.raises(api.ApiError) as exc:
        lead(granted, agents=("boss",))
    assert exc.value.kind == "conflict"


def test_release_and_lead_same_work_one_operation(granted):
    by_release = release(granted, agents=("boss", "dev"))
    by_lead = lead(granted, agents=("dev",))
    assert by_lead["id"] == by_release["id"]
    assert len(events(granted, "operation.queued")) == 1


def test_newer_release_supersedes_pending(granted):
    old = release(granted)
    granted.ctx.deployed_revision = lambda: REV_B
    new = release(granted, agents=("boss", "dev"))
    assert new["id"] != old["id"] and new["revision"] == REV_B
    stale = operations.row(granted.store, old["id"])
    assert stale["state"] == "superseded" and stale["superseded_by"] == new["id"]
    assert [e["payload"]["operation"] for e in events(granted, "operation.superseded")] == [old["id"]]


def test_root_request_needs_no_grant(granted):
    op = granted.call("root", {"op": "operation.request", "kind": "agent.reinstall", "agents": ["analyst"],
                               "key": "root:probe"})["operation"]
    assert op["requested_by"] == "root" and op["card"] == "" and op["grant_id"] == ""


def test_release_without_grant_is_uncovered(granted):
    granted.put("KD-61", project="digipaddock", assignee="@dev", status="In Progress")
    with pytest.raises(api.ApiError) as exc:
        release(granted, card="KD-61")
    assert exc.value.kind == "refused"


def report(env, op, state, **extra):
    env.call("root", {"op": "host.report", "items": [
        {"type": "operation", "operation": op["id"], "state": state, **extra}]})


def test_report_moves_state_and_emits_event(granted):
    from kd.cabinet import sessions_api  # noqa: F401 — host.report

    op = lead(granted)
    report(granted, op, "running", phase="draining")
    assert operations.row(granted.store, op["id"])["state"] == "running"
    report(granted, op, "succeeded", agents={"dev": {"state": "ok", "stage": "check", "error": ""}})
    report(granted, op, "succeeded", agents={"dev": {"state": "ok", "stage": "check", "error": ""}})
    done = operations.row(granted.store, op["id"])
    assert done["state"] == "succeeded" and done["result"]["agents"]["dev"]["state"] == "ok"
    assert [len(events(granted, k)) for k in ("operation.running", "operation.succeeded")] == [1, 1]
    report(granted, op, "failed", reason="late")  # a final state never moves
    assert operations.row(granted.store, op["id"])["state"] == "succeeded"


def test_revision_moved_under_a_newer_release_is_superseded(granted):
    """Review I2: the first release's operation drains when the second release lands; its runner finds the tree moved
    — that is the newer operation's work, not a failure the owner must hear of."""
    old = release(granted)
    report(granted, old, "running", phase="draining")  # delivered: a newer one does not supersede it at request
    granted.ctx.deployed_revision = lambda: REV_B
    new = release(granted, agents=("boss", "dev"))
    assert operations.row(granted.store, old["id"])["state"] == "running"
    report(granted, old, "failed", phase="installing", reason="revision_moved",
           agents={"dev": {"state": "skipped", "stage": "", "error": "revision_moved"}})
    stale = operations.row(granted.store, old["id"])
    assert stale["state"] == "superseded" and stale["superseded_by"] == new["id"]
    assert not [m for m in granted.store.messages("KD-50") if m["kind"] == "notice"
                and m["payload"].get("key") == "notice.factory.failed"]


def test_revision_moved_without_a_newer_operation_fails(granted):
    op = release(granted)
    report(granted, op, "failed", phase="installing", reason="revision_moved")
    assert operations.row(granted.store, op["id"])["state"] == "failed"


def test_failure_notice_says_whether_the_role_stays_paused(granted):
    """Review minor 2: only a failed install keeps a role paused; a drain timeout lifts it."""
    op = lead(granted)
    report(granted, op, "failed", phase="draining", reason="drain_timeout")
    [notice] = [m for m in granted.store.messages("KD-50") if m["kind"] == "notice"]
    assert notice["payload"]["key"] == "notice.factory.failed_resumed"


def test_failed_operation_posts_critical_notice(granted):
    op = lead(granted)
    report(granted, op, "failed", phase="installing", reason="check_failed",
           agents={"dev": {"state": "failed", "stage": "check", "error": "policy mismatch"}})
    notices = [m for m in granted.store.messages("KD-50") if m["kind"] == "notice"]
    assert notices and notices[-1]["payload"]["level"] == "critical"
    assert notices[-1]["payload"]["key"] == "notice.factory.failed"
    assert "dev" in notices[-1]["payload"]["params"]["agents"]
    assert operations.row(granted.store, op["id"])["error"] == "check_failed"


def test_revoked_grant_cancels_pending(granted):
    op = lead(granted)
    [d] = [d for d in granted.call("digi-cabinet", {"op": "decision.list", "card": "KD-50"})["decisions"]
           if d["type"] == "admin_plan"]
    granted.call("digi-cabinet", {"op": "decision.revoke", "id": d["id"], "user": "owner"})
    row = operations.row(granted.store, op["id"])
    assert row["state"] == "failed" and row["error"] == "grant_revoked"


def test_lost_result_tells_the_owner_to_verify(granted):
    """``reconcile`` is read — an operation the host lost says so in its notice: check the roles."""
    op = lead(granted)
    report(granted, op, "running", phase="installing")
    operations.answered(granted.ctx, op["id"], {"state": "unknown"})
    found = operations.row(granted.store, op["id"])
    assert (found["state"], found["error"], found["reconcile"]) == ("failed", "unknown_result", True)
    [notice] = [m for m in granted.store.messages("KD-50") if m["kind"] == "notice"]
    assert notice["payload"]["key"] == "notice.factory.unknown" and notice["payload"]["level"] == "critical"
    assert notice["payload"]["params"]["agents"] == "dev"


# --- who may run an operation ---------------------------------------------------------------------------------------------
def authorize(env, op, agents=("dev",), who="root"):
    answer = env.call(who, {"op": "operation.authorize", "operation": op["id"], "agents": list(agents)})
    return {k: answer[k] for k in ("allowed", "reason")}


def test_authorize_answers_from_the_grant_and_the_tree(granted):
    """R2: the host asks right before a batch starts — the grant must still cover it, the tree be its revision."""
    op = lead(granted)
    operations.delivered(granted.ctx, op["id"], {"state": "running"})
    assert authorize(granted, op) == {"allowed": True, "reason": ""}
    granted.ctx.deployed_revision = lambda: REV_B
    assert authorize(granted, op) == {"allowed": False, "reason": "revision_moved"}
    granted.ctx.deployed_revision = lambda: REV_A
    [d] = [d for d in granted.call("digi-cabinet", {"op": "decision.list", "card": "KD-50"})["decisions"]
           if d["type"] == "admin_plan"]
    granted.call("digi-cabinet", {"op": "decision.revoke", "id": d["id"], "user": "owner"})
    assert authorize(granted, op) == {"allowed": False, "reason": "grant_revoked"}
    with pytest.raises(api.ApiError):
        authorize(granted, op, agents=("sysadmin",))  # a role the operation does not name
    with pytest.raises(api.ApiError):
        authorize(granted, op, who="digi")
    assert authorize(granted, {"id": "f" * 32}) == {"allowed": False, "reason": "unknown_operation"}
    root = granted.call("root", {"op": "operation.request", "kind": "agent.reinstall", "agents": ["analyst"],
                                 "key": "root:probe"})["operation"]
    assert authorize(granted, root, agents=("analyst",))["allowed"] is True
    report(granted, root, "failed", phase="draining", reason="drain_timeout")
    assert authorize(granted, root, agents=("analyst",)) == {"allowed": False, "reason": "operation_failed"}


def test_a_failed_host_check_keeps_the_roles_the_host_names(granted):
    """R3: the host's result names the roles it keeps; without the list the cabinet assumes all of them."""
    op = lead(granted, agents=("boss", "dev"))
    report(granted, op, "failed", phase="installing", reason="host_check_failed", kept=["dev"],
           agents={"boss": {"state": "ok", "stage": "verify", "error": ""},
                   "dev": {"state": "ok", "stage": "verify", "error": ""}})
    [notice] = [m for m in granted.store.messages("KD-50") if m["kind"] == "notice"]
    assert notice["payload"]["key"] == "notice.factory.failed" and notice["payload"]["params"]["failed"] == "dev"
    assert operations._kept("host_check_failed", {"dev": {"state": "ok"}}, ["boss", "dev"]) == ["boss", "dev"]


def test_running_reports_record_the_progress(granted):
    """R9/R10: each batch's roles and the drain's deadline reach the operation while it runs."""
    op = lead(granted, agents=("boss", "dev"))
    report(granted, op, "running", phase="draining", agents={}, until=1_800_000_000)
    report(granted, op, "running", phase="draining", until=1_800_000_000,
           agents={"boss": {"state": "ok", "stage": "verify", "error": ""}})
    row = operations.row(granted.store, op["id"])
    assert row["result"]["agents"] == {"boss": {"state": "ok", "stage": "verify", "error": ""}}
    assert row["result"]["until"] == "2027-01-15T08:00:00Z"
    assert len(events(granted, "operation.running")) == 1


def test_a_pair_release_key_is_a_key(granted):
    """the release of code + overlay keys its operations ``release:<rev>+<deploy>``."""
    rev = granted.ctx.deployed_revision()
    found = release(granted, key=f"release:{rev}+1234567")
    assert found["key"] == f"release:{rev}+1234567"
