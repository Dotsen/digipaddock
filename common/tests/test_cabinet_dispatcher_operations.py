"""The dispatcher hands the session host one factory operation at a time, polls the
one in flight every SWEEP_S, applies the host's answer as its report, re-checks the grant right before sending and
retries an absent host with the backoff."""

import asyncio
from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path

import pytest

from kd import admin_grant, factory_ops
from kd.cabinet import config, decisions, dispatcher, operations, service

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_cabinet_factory_plan import env  # noqa: E402,F401
from test_cabinet_operations import REV_A, granted, lead  # noqa: E402,F401


class Host:
    def __init__(self, *answers):
        self.calls, self.answers = [], list(answers)

    async def call(self, request, timeout=10.0):
        self.calls.append(request)
        answer = self.answers.pop(0) if self.answers else {"ok": True, "state": "starting"}
        if isinstance(answer, Exception):
            raise answer
        return answer

    def operations(self):
        return [c for c in self.calls if c["op"] == "operation"]


class Quiet:
    def catch_up(self, decisions):
        pass


@pytest.fixture
def svc(granted):
    granted.svc = service.Service(granted.ctx, config.load("/none", "/none", dev=True), users=granted.users,
                                  decisions=decisions.Decisions(granted.store), notifier=Quiet(),
                                  now=lambda: granted.now)
    return granted


def root_op(env, agents, key):
    return env.call("root", {"op": "operation.request", "kind": "agent.reinstall", "agents": list(agents),
                             "key": key, "card": "KD-50"})["operation"]["id"]


def tick(env, host, now):
    return asyncio.run(dispatcher.tick(env.svc, host=host, now=now))


def running(op_id, phase="draining"):
    return {"ok": True, "state": "running",
            "report": {"type": "operation", "operation": op_id, "state": "running", "phase": phase}}


def state(env, op_id):
    found = operations.row(env.store, op_id)
    return found["state"], found["phase"], found["error"]


def test_dispatch_one_operation_at_a_time(svc):
    first, second = root_op(svc, ["dev"], "root:1"), root_op(svc, ["analyst"], "root:2")
    host = Host(running(first))
    t0 = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    tick(svc, host, t0)
    assert host.operations() == [{"op": "operation", "operation": first, "kind": "agent.reinstall",
                                  "agents": ["dev"], "revision": REV_A, "timeout": dispatcher.OPERATION_TIMEOUT,
                                  "start": True}]
    assert state(svc, first)[:2] == ("running", "draining") and state(svc, second)[0] == "pending"
    tick(svc, host, t0 + timedelta(seconds=1))  # in flight, polled every SWEEP_S only; the second waits
    assert len(host.operations()) == 1
    host.answers.append(running(first, "installing"))
    tick(svc, host, t0 + timedelta(seconds=dispatcher.SWEEP_S))
    assert host.operations()[-1]["operation"] == first and host.operations()[-1]["start"] is False
    assert state(svc, first)[:2] == ("running", "installing") and state(svc, second)[0] == "pending"


def test_running_operation_result_from_host_answer(svc):
    first, second = root_op(svc, ["dev"], "root:1"), root_op(svc, ["analyst"], "root:2")
    t0 = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    done = {"ok": True, "state": "succeeded", "report": {
        "type": "operation", "operation": first, "state": "succeeded", "phase": "installing",
        "agents": {"dev": {"state": "ok", "stage": "verify", "error": ""}}, "reason": ""}}
    host = Host(running(first), done, running(second))
    tick(svc, host, t0)
    tick(svc, host, t0 + timedelta(seconds=dispatcher.SWEEP_S))
    assert state(svc, first)[0] == "succeeded"
    tick(svc, host, t0 + timedelta(seconds=dispatcher.SWEEP_S + 1))
    assert host.operations()[-1]["operation"] == second and state(svc, second)[0] == "running"


def test_host_lost_the_operation_fails_it_for_reconcile(svc):
    first = root_op(svc, ["dev"], "root:1")
    t0 = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    host = Host(running(first), {"ok": True, "state": "unknown"})
    tick(svc, host, t0)
    tick(svc, host, t0 + timedelta(seconds=dispatcher.SWEEP_S))
    found = operations.row(svc.store, first)
    assert (found["state"], found["error"], found["reconcile"]) == ("failed", "unknown_result", True)


def test_revoked_grant_before_dispatch_fails(svc):
    op = lead(svc)["id"]
    scope = factory_ops.load_catalog().scope
    svc.admin.store.revoke("KD-50", now=datetime.now(timezone.utc), via="test", by="owner",
                           policies=(admin_grant.FACTORY,), factory=scope)  # past the cabinet's hook
    host = Host()
    tick(svc, host, datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc))
    assert host.operations() == []
    assert state(svc, op)[0] == "failed" and state(svc, op)[2] in ("grant_revoked", "grant_unusable")


def test_host_unavailable_retries(svc):
    op = root_op(svc, ["dev"], "root:1")
    t0 = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    host = Host(dispatcher.HostUnavailable("down"), {"ok": True, "state": "busy", "operation": "c" * 32})
    tick(svc, host, t0)
    found = operations.row(svc.store, op)
    assert (found["state"], found["phase"], found["attempts"]) == ("pending", "", 1)
    tick(svc, host, t0 + timedelta(seconds=1))  # before its next try: nothing sent
    assert len(host.operations()) == 1
    tick(svc, host, t0 + timedelta(seconds=dispatcher.BACKOFF[0]))  # the host holds another one: later again
    found = operations.row(svc.store, op)
    assert (found["state"], found["phase"], found["attempts"]) == ("pending", "", 2)


def test_host_refusal_fails_the_operation(svc):
    op = root_op(svc, ["dev"], "root:1")
    host = Host(dispatcher.HostRefused("refused", "not on this session host: dev"))
    tick(svc, host, datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc))
    assert state(svc, op)[0] == "failed" and state(svc, op)[2].startswith("host_refused")


def test_revoked_grant_cancels_the_operation_while_it_drains(svc):
    """the grant revoked after the host took the operation — the next poll asks the host to cancel it
    (nothing is installed while the roles drain); the host's answer ends it with the grant's reason."""
    op = lead(svc)["id"]
    t0 = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    cancelled = {"ok": True, "state": "failed", "report": {
        "type": "operation", "operation": op, "state": "failed", "phase": "draining", "agents": {},
        "reason": "grant_revoked"}}
    host = Host(running(op), cancelled)
    tick(svc, host, t0)
    scope = factory_ops.load_catalog().scope
    svc.admin.store.revoke("KD-50", now=datetime.now(timezone.utc), via="test", by="owner",
                           policies=(admin_grant.FACTORY,), factory=scope)
    tick(svc, host, t0 + timedelta(seconds=dispatcher.SWEEP_S))
    assert host.calls[-1] == {"op": "operation_cancel", "operation": op, "reason": "grant_revoked"}
    assert state(svc, op) == ("failed", "draining", "grant_revoked")


def test_installing_operation_is_never_cancelled(svc):
    op = lead(svc)["id"]
    t0 = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    host = Host(running(op, "installing"), running(op, "installing"))
    tick(svc, host, t0)
    scope = factory_ops.load_catalog().scope
    svc.admin.store.revoke("KD-50", now=datetime.now(timezone.utc), via="test", by="owner",
                           policies=(admin_grant.FACTORY,), factory=scope)
    tick(svc, host, t0 + timedelta(seconds=dispatcher.SWEEP_S))
    assert host.calls[-1]["op"] == "operation" and state(svc, op)[:2] == ("running", "installing")
