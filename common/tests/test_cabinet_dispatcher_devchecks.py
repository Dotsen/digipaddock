"""The dispatcher keeps a lane per contour — an exclusive one takes one check at a
time, another up to its ``parallel``; it polls the checks the host has, sends their cancels and backs off an absent
host. The factory's one operation at a time holds no check."""

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kd.cabinet import config, decisions, devchecks, dispatcher, operations, service
from kd.cabinet.dispatcher import HostUnavailable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_cabinet_devchecks import HEAD, ask, cab  # noqa: E402,F401
from test_cabinet_factory_plan import env  # noqa: E402,F401

T0 = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
CONTOURS = {"app-ci": {"host": "local", "components": ["digipaddock", "digipaddock-deploy"], "parallel": 2},
            "solo": {"host": "local", "components": ["admin"], "exclusive": True}}


class Host:
    """The session host: ``devcheck`` starts or reports, ``devcheck_cancel`` stops; ``answers`` by op override."""

    def __init__(self, **answers):
        self.calls, self.answers = [], {k: list(v) for k, v in answers.items()}

    async def call(self, request, timeout=10.0):
        self.calls.append(request)
        queue = self.answers.get(request["op"]) or []
        answer = queue.pop(0) if queue else {"ok": True, "state": "running"}
        if isinstance(answer, Exception):
            raise answer
        return answer

    def of(self, op, **match):
        return [c for c in self.calls if c["op"] == op and all(c.get(k) == v for k, v in match.items())]


class Quiet:
    def catch_up(self, decisions):
        pass


@pytest.fixture
def svc(cab, tmp_path):
    path = tmp_path / "contours-2.json"
    path.write_text(json.dumps(CONTOURS), encoding="utf-8")
    cab.ctx.contours_path = str(path)
    cab.svc = service.Service(cab.ctx, config.load("/none", "/none", dev=True), users=cab.users,
                              decisions=decisions.Decisions(cab.store), notifier=Quiet(), now=lambda: cab.now)
    return cab


def tick(env, host, now=T0):
    return asyncio.run(dispatcher.tick(env.svc, host=host, now=now))


def heads(n):
    return [f"{i:x}" * 40 for i in range(1, n + 1)]


def checks(env, n, **over):
    return [ask(env, head=h, pr=100 + i, **over)["operation"]["id"] for i, h in enumerate(heads(n))]


def state(env, op_id):
    found = operations.row(env.store, op_id)
    return found["state"], found["phase"], found["error"]


def admin_card(env):
    env.put("KD-60", project="admin", assignee="@dev", status="In Progress")
    for command in env.store.commands(card="KD-60", states=["queued"]):
        for st in ("delivered", "started", "done"):
            env.store.command_state(command["id"], st)


def test_parallel_contour_takes_two_and_queues_the_third(svc):
    first, second, third = checks(svc, 3)
    host = Host()
    tick(svc, host)
    sent = [c["check"] for c in host.of("devcheck", start=True)]
    assert sent == [first, second]
    assert host.of("devcheck")[0]["timeout"] == 1200 + devchecks.GRACE_S
    assert state(svc, first)[1] == "delivered" and state(svc, third) == ("pending", "", "")
    tick(svc, host, T0 + timedelta(seconds=1))
    assert len(host.of("devcheck", start=True)) == 2  # still full; polled only every SWEEP_S
    assert host.of("devcheck", start=False) == []


def test_exclusive_contour_takes_one(svc):
    admin_card(svc)
    first, second = [ask(svc, card="KD-60", project="admin", contour="solo", head=h, pr=200 + i)["operation"]["id"]
                     for i, h in enumerate(heads(2))]
    host = Host()
    tick(svc, host)
    assert [c["check"] for c in host.of("devcheck", start=True)] == [first]
    assert state(svc, second) == ("pending", "", "")


def test_two_contours_do_not_block_each_other(svc):
    admin_card(svc)
    solo = ask(svc, card="KD-60", project="admin", contour="solo", head=heads(1)[0], pr=300)["operation"]["id"]
    ci = checks(svc, 1)[0]
    host = Host()
    tick(svc, host)
    assert sorted(c["check"] for c in host.of("devcheck", start=True)) == sorted([solo, ci])


def test_a_factory_operation_in_flight_does_not_hold_a_check(svc):
    factory = svc.call("root", {"op": "operation.request", "kind": "agent.reinstall", "agents": ["dev"],
                                "key": "root:1", "card": "KD-50"})["operation"]["id"]
    check = checks(svc, 1)[0]
    host = Host(operation=[{"ok": True, "state": "running", "report": {
        "type": "operation", "operation": factory, "state": "running", "phase": "draining"}}])
    tick(svc, host)
    assert state(svc, factory)[0] == "running"
    assert [c["check"] for c in host.of("devcheck", start=True)] == [check]


def test_poll_reports_and_unknown_ends_host_lost(svc):
    first, second = checks(svc, 2)
    host = Host()
    tick(svc, host)
    host.answers["devcheck"] = [
        {"ok": True, "state": "running", "report": {"type": "devcheck", "check": first, "state": "running"}},
        {"ok": True, "state": "unknown"}]
    tick(svc, host, T0 + timedelta(seconds=dispatcher.SWEEP_S))
    polls = host.of("devcheck", start=False)
    assert sorted(c["check"] for c in polls) == sorted([first, second])
    assert state(svc, first)[0] == "running"
    assert state(svc, second) == ("failed", "", "host_lost")


def test_cancel_is_sent_until_the_host_answers(svc):
    op = checks(svc, 1)[0]
    host = Host()
    tick(svc, host)
    svc.call("root", {"op": "devcheck.cancel", "id": op})
    host.answers["devcheck_cancel"] = [HostUnavailable("down"), {"ok": True, "state": "stopping"}]
    tick(svc, host, T0 + timedelta(seconds=1))
    tick(svc, host, T0 + timedelta(seconds=2))
    assert len(host.of("devcheck_cancel", check=op)) == 2 and state(svc, op)[0] == "pending"
    host.answers["devcheck_cancel"] = [{"ok": True, "state": "running", "report": {
        "type": "devcheck", "check": op, "state": "failed", "class": "cancelled"}}]
    tick(svc, host, T0 + timedelta(seconds=3))
    assert state(svc, op) == ("failed", "", "cancelled")
    tick(svc, host, T0 + timedelta(seconds=4))
    assert len(host.of("devcheck_cancel", check=op)) == 3


def test_a_cancel_the_host_does_not_hold_ends_cancelled(svc):
    op = checks(svc, 1)[0]
    tick(svc, Host())
    svc.call("root", {"op": "devcheck.cancel", "id": op})
    tick(svc, Host(devcheck_cancel=[{"ok": True, "state": "unknown"}]), T0 + timedelta(seconds=1))
    assert state(svc, op) == ("failed", "", "cancelled")


def test_unavailable_host_backs_off(svc):
    op = checks(svc, 1)[0]
    host = Host(devcheck=[HostUnavailable("down")])
    tick(svc, host)
    found = operations.row(svc.store, op)
    assert found["phase"] == "" and found["next_try"] > "2026-10-28T12:00:00Z"
    tick(svc, host, T0 + timedelta(seconds=1))
    assert len(host.of("devcheck")) == 1  # waits for its next try
    tick(svc, host, T0 + timedelta(seconds=dispatcher.BACKOFF[0]))
    assert state(svc, op)[1] == "delivered"


def test_busy_host_backs_off_and_refusal_ends_it(svc):
    first, second = checks(svc, 2)
    host = Host(devcheck=[{"ok": True, "state": "busy"}, dispatcher.HostRefused("invalid", "bad params")])
    tick(svc, host)
    assert state(svc, first)[:2] == ("pending", "")
    tick(svc, host, T0 + timedelta(seconds=dispatcher.BACKOFF[0]))
    assert state(svc, first)[2] == "environment_not_ready"


def test_a_check_of_a_contour_gone_ends(svc, tmp_path):
    op = checks(svc, 1)[0]
    path = tmp_path / "contours-3.json"
    path.write_text(json.dumps({"solo": CONTOURS["solo"]}), encoding="utf-8")
    svc.ctx.contours_path = str(path)
    host = Host()
    tick(svc, host)
    assert host.of("devcheck") == [] and state(svc, op)[2] == "environment_not_ready"
