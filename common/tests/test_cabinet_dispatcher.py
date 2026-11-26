"""the dispatcher leases queued commands, re-checks them and delivers them to the session host."""

import asyncio
from datetime import timedelta

import pytest
from cabinet_env import CabinetEnv

from kd import session_config
from kd.cabinet import api, config, decisions, dispatcher, llm_api, service

HOSTED = session_config.HostConfig({"analyst": 1}, 600)


class Host:
    """A session host that records requests and answers from a script."""

    def __init__(self, *answers):
        self.calls, self.answers = [], list(answers)

    async def call(self, request, timeout=10.0):
        self.calls.append(request)
        answer = self.answers.pop(0) if self.answers else {"ok": True, "state": "starting"}
        if isinstance(answer, Exception):
            raise answer
        return answer


class Quiet:
    def catch_up(self, decisions):
        pass


@pytest.fixture
def env(tmp_path):
    e = CabinetEnv(tmp_path)
    e.config = HOSTED
    e.llm_dir = tmp_path / "llm"
    e.ctx.llm_env_dir = str(e.llm_dir)  # the host's key files: none unless a test writes one
    api.install_host(e.ctx, lambda: e.config)
    e.svc = service.Service(
        e.ctx, config.load("/none", "/none", dev=True), users=e.users,
        decisions=decisions.Decisions(e.store), notifier=Quiet(), now=lambda: e.now,
    )
    return e


def _assigned(env):
    card = env.card(assignee="@analyst", status="Inbox")
    env.store.edit_card(card.id, None, {"status": "To Do"}, actor="user:owner")
    [command] = env.store.commands(card=card.id)
    return env.store.card(card.id), command


def _tick(env, host, now=None):
    return asyncio.run(dispatcher.tick(env.svc, host=host, now=now or env.when()))


# a host older than a field refuses it, and every delivery names its role's context limit, the newest field:
# such a host refuses that one first (test_cabinet_context_limits), then the field the test is about
OLDER = (dispatcher.HostRefused("invalid", "unknown field for deliver"),) * 2


def test_delivers_queued_command_with_resume_id(env):
    card, command = _assigned(env)
    session = env.store.session_for(card.thread, "analyst")
    env.store.session_update(session["id"], state="exited", runtime_session_id="0b9c3f1e-1111-2222-3333-444455556666")
    host = Host()
    assert _tick(env, host) == {"delivered": 1, "retried": 0, "cancelled": 0, "failed": 0}
    [request] = host.calls
    assert request == {
        "op": "deliver", "command": command["id"], "session": session["id"], "agent": "analyst",
        "thread": card.thread, "card": card.id, "text": command["text"],
        "resume": "0b9c3f1e-1111-2222-3333-444455556666",
        "context_limit": 400000,  # the role's session context limit, the default here (test_cabinet_context_limits)
    }
    after = env.store.command(command["id"])
    assert after["state"] == "delivered" and after["session"] == session["id"]


def test_queued_ack_moves_the_session_to_queued(env):
    card, command = _assigned(env)
    _tick(env, Host({"ok": True, "state": "queued", "position": 2}))
    [session] = env.store.sessions(thread=card.thread)
    assert session["state"] == "queued"
    # its place in the agent's queue and since when it waits — the cabinet shows both
    assert session["queue_position"] == 2 and session["queued_since"]


def test_stale_command_cancelled_not_delivered(env):
    card, command = _assigned(env)
    env.store.edit_card(card.id, None, {"status": "Done"}, actor="user:owner")
    host = Host()
    assert _tick(env, host)["cancelled"] == 1 and host.calls == []
    after = env.store.command(command["id"])
    assert after["state"] == "cancelled" and after["reason"] == "card_done"


def test_a_delivered_command_whose_card_moved_on_is_cancelled_at_the_host(env):
    """Remediation §4.2: the conditions are re-checked before the run — a command waiting at the host for a slot
    or behind a turn is taken back when its card closes."""
    card, command = _assigned(env)
    _tick(env, Host({"ok": True, "state": "queued", "position": 1}))
    session = env.store.command(command["id"])["session"]
    env.store.edit_card(card.id, None, {"status": "Done"}, actor="user:owner")
    host = Host({"ok": True, "cancelled": True})
    assert _tick(env, host)["cancelled"] == 1
    assert host.calls == [{"op": "cancel", "session": session, "command": command["id"]}]
    after = env.store.command(command["id"])
    assert after["state"] == "cancelled" and after["reason"] == "card_done"
    assert _tick(env, Host()) == {"delivered": 0, "retried": 0, "cancelled": 0, "failed": 0}


def test_a_delivered_command_the_host_already_runs_or_cannot_hear_stays(env):
    card, command = _assigned(env)
    _tick(env, Host({"ok": True, "state": "running", "position": 1}))
    env.store.edit_card(card.id, None, {"status": "Done"}, actor="user:owner")
    for answer in ({"ok": True, "cancelled": False}, dispatcher.HostUnavailable("no socket"),
                   dispatcher.HostRefused("invalid", "unknown operation")):
        host = Host(answer)
        assert _tick(env, host)["cancelled"] == 0 and host.calls[0]["op"] == "cancel"
        assert env.store.command(command["id"])["state"] == "delivered"
    _, other = _assigned(env)
    _tick(env, Host())
    host = Host({"ok": True, "cancelled": False})
    _tick(env, host)
    assert [c["command"] for c in host.calls] == [command["id"]]  # a delivered command whose card holds: not asked


def test_unavailable_host_backs_off_then_delivers(env):
    card, command = _assigned(env)
    host = Host(dispatcher.HostUnavailable("no socket"))
    assert _tick(env, host)["retried"] == 1
    after = env.store.command(command["id"])
    assert after["state"] == "queued" and after["error"] == "host_unavailable"
    assert after["next_try"] == "2026-10-14T10:00:02Z"  # the first backoff step
    assert _tick(env, host, env.when() + timedelta(seconds=1))["delivered"] == 0  # not due yet
    assert _tick(env, host, env.when() + timedelta(seconds=3))["delivered"] == 1
    assert env.store.command(command["id"])["state"] == "delivered"


def test_old_command_fails_terminally_with_reason(env):
    card, command = _assigned(env)
    host = Host()
    out = _tick(env, host, env.when() + dispatcher.MAX_AGE + timedelta(seconds=1))
    assert out["failed"] == 1 and host.calls == []
    after = env.store.command(command["id"])
    assert after["state"] == "failed" and after["reason"] == "delivery_expired"


def test_refusal_fails_with_host_refused(env):
    card, command = _assigned(env)
    _tick(env, Host(dispatcher.HostRefused("refused", "analyst is not hosted")))
    after = env.store.command(command["id"])
    assert after["state"] == "failed" and after["reason"] == "host_refused"
    assert "not hosted" in after["error"]


def test_lease_prevents_double_delivery_in_overlapping_ticks(env):
    card, command = _assigned(env)
    gate = asyncio.Event()

    class Slow(Host):
        async def call(self, request, timeout=10.0):
            self.calls.append(request)
            await gate.wait()
            return {"ok": True, "state": "starting"}

    host = Slow()

    async def both():
        first = asyncio.create_task(dispatcher.tick(env.svc, host=host, now=env.when()))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        second = await dispatcher.tick(env.svc, host=host, now=env.when())
        gate.set()
        return await first, second

    first, second = asyncio.run(both())
    assert len(host.calls) == 1 and first["delivered"] == 1 and second["delivered"] == 0


def test_turn_reported_before_the_ack_is_kept(env):
    card, command = _assigned(env)
    session = env.store.session_for(card.thread, "analyst")

    class Racing(Host):
        async def call(self, request, timeout=10.0):
            self.calls.append(request)
            env.call("root", {"op": "host.report", "items": [
                {"type": "turn", "session": session["id"], "command": command["id"], "phase": "started"}]})
            return {"ok": True, "state": "running"}

    assert _tick(env, Racing())["delivered"] == 1
    assert env.store.command(command["id"])["state"] == "started"


def test_kick_wakes_run_before_interval(env):
    host = Host()

    async def scenario():
        task = asyncio.create_task(dispatcher.run(env.svc, host=host, interval=30, clock=env.when))
        await asyncio.sleep(0.05)
        card = env.card(assignee="@analyst", status="Inbox")
        await env.svc.locked(lambda: env.store.edit_card(card.id, None, {"status": "To Do"}, actor="user:owner"))
        for _ in range(50):
            if host.calls:
                break
            await asyncio.sleep(0.02)
        task.cancel()
        return len(host.calls)

    assert asyncio.run(scenario()) == 1


def test_stop_command_sends_stop(env):
    card, command = _assigned(env)
    _tick(env, Host())
    session = env.store.session_for(card.thread, "analyst")
    env.store.session_update(session["id"], state="working")
    env.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    owner = api.Principal("user", -1, "owner")
    stop = api.dispatch(env.ctx, owner, {"op": "user.session.stop", "session": session["id"]})
    host = Host({"ok": True})
    _tick(env, host)
    assert host.calls == [{"op": "stop", "session": session["id"], "command": stop["command"]["id"]}]
    assert env.store.command(stop["command"]["id"])["state"] == "done"


# --- worker jobs --------------------------------------------------------------------
def _job(env, n=1, card=None):
    card = card or env.card()
    job = {
        "id": f"{n:032x}", "card": card.id, "agent": "dev", "type": "developer", "repo": "digipaddock",
        "spec": "implementer", "skills": [], "prompt": {}, "limits": {"write": "repo", "capabilities": [],
                                                                      "network": "none"},
        "brief": "b", "caller": "digi-boss", "requester": "boss", "writer": True, "timeout": 3600, "model": "sonnet",
    }
    env.call("digi", {"op": "job.submit", "job": job, "limits": {"jobs_per_day": 9, "attempts_per_card_spec": 9}})
    return card, job


def _state(env, job):
    return env.call("root", {"op": "job.view", "job": job["id"], "caller": "root"})["job"]


def test_queued_job_is_delivered_to_the_host(env):
    card, job = _job(env)
    host = Host({"ok": True, "state": "queued", "position": 1})
    assert _tick(env, host) == {"delivered": 1, "retried": 0, "cancelled": 0, "failed": 0}
    # the stored definition carries the card's effort (none here) beside what kd-worker sent; the send adds the
    # role's session context limit (the default here)
    assert host.calls == [{"op": "job", "job": job["id"], "agent": "dev",
                           "definition": {**job, "effort": "", "context_limit": 400000}}]
    assert _state(env, job)["state"] == "delivered"
    assert _tick(env, Host())["delivered"] == 0  # delivered once


def test_cancelling_job_is_sent_job_cancel(env):
    jobs = [_job(env, n)[1] for n in (1, 2, 3)]
    _tick(env, Host(*[{"ok": True, "state": "starting"}] * 3))
    for job in jobs:
        out = env.call("digi", {"op": "job.cancel", "job": job["id"], "caller": "digi-boss", "reason": "стоп"})
        assert out["state"] == "cancelling"
    host = Host({"ok": True, "cancelled": "queued"}, {"ok": True, "cancelled": "running"},
                {"ok": True, "cancelled": "unknown"})
    assert _tick(env, host)["cancelled"] == 2
    assert host.calls == [{"op": "job_cancel", "job": j["id"]} for j in jobs] + [{"op": "status"}]  # + the sweep
    assert [_state(env, j)["state"] for j in jobs] == ["cancelled", "cancelling", "cancelled"]
    assert _state(env, jobs[2])["reason"] == "стоп"  # the requester's reason stays: the host never had it
    host = Host({"ok": True, "cancelled": "running"})
    _tick(env, host)
    assert host.calls == [{"op": "job_cancel", "job": jobs[1]["id"]}]  # asked again until the host reports


def test_a_job_whose_card_closed_is_cancelled(env):
    card, job = _job(env)
    env.store.edit_card(card.id, None, {"status": "Done"}, actor="user:owner")
    host = Host()
    assert _tick(env, host)["cancelled"] == 1 and host.calls == []
    assert (_state(env, job)["state"], _state(env, job)["reason"]) == ("cancelled", "card_done")


def test_host_away_retries_a_job_with_backoff(env):
    card, job = _job(env)
    assert _tick(env, Host(dispatcher.HostUnavailable("no socket")))["retried"] == 1
    assert _state(env, job)["state"] == "queued"
    assert _tick(env, Host(), env.when() + timedelta(seconds=1))["delivered"] == 0
    assert _tick(env, Host(), env.when() + timedelta(seconds=3))["delivered"] == 1
    _, refused = _job(env, 2)
    _tick(env, Host(GONE, dispatcher.HostRefused("refused", "no worker slots for dev")))  # the sweep asks first
    assert (_state(env, refused)["state"], _state(env, refused)["reason"]) == ("failed", "host_refused")
    assert [c["text"].split(" ")[3] for c in env.store.commands(limit=50) if c["kind"] == "signal"] == ["failed"]


def test_a_queued_job_cancelled_while_it_is_being_sent_goes_through_the_host(env):
    """The lease is the dispatcher's: a job it holds may be at the host already — the cancel asks the host."""
    card, job = _job(env)
    env.store._conn.execute("UPDATE jobs SET lease_until='2026-10-14T10:01:00Z' WHERE id=?", (job["id"],))
    out = env.call("digi", {"op": "job.cancel", "job": job["id"], "caller": "digi-boss", "reason": "стоп"})
    assert out == {"ok": True, "state": "cancelling", "previous": "queued"}


def test_a_job_waiting_past_a_day_fails(env):
    card, job = _job(env)
    host = Host()
    assert _tick(env, host, env.when() + dispatcher.MAX_AGE + timedelta(seconds=1))["failed"] == 1
    assert host.calls == [] and _state(env, job)["reason"] == "delivery_expired"


# --- final review: jobs the host lost, and jobs whose card moved on before they started -------------------------
GONE = {"ok": True, "sessions": [], "slots": {}, "jobs": [], "worker_slots": {}, "reporting": []}


def _live(*, jobs=(), reporting=()):
    return {**GONE, "jobs": [{"job": j, "agent": "dev", "state": "running", "slot": "dev/worker1"} for j in jobs],
            "reporting": list(reporting)}


def _running(env, job):
    _tick(env, Host({"ok": True, "state": "starting"}))
    env.call("root", {"op": "host.report", "items": [
        {"type": "job", "job": job["id"], "state": "starting", "slot": "dev/worker1"}]})


def test_a_job_the_host_no_longer_has_fails_after_a_minute(env):
    """An end report the cabinet refused (or the host dropped) must not hold the job and its card's writer."""
    card, job = _job(env)
    _running(env, job)
    t0 = env.when()
    for seconds in (0, dispatcher.SWEEP_S):
        _tick(env, Host(GONE), t0 + timedelta(seconds=seconds))
        assert _state(env, job)["state"] == "running"
    assert _tick(env, Host(GONE), t0 + timedelta(seconds=dispatcher.LOST_S))["failed"] == 1
    after = _state(env, job)
    assert (after["state"], after["reason"]) == ("failed", "host_lost_it")
    assert [c["text"].split(" ")[3] for c in env.store.commands(limit=50) if c["kind"] == "signal"] == ["failed"]
    _job(env, 2, card=env.store.card(card.id))  # its writer is free again


def test_a_job_the_host_runs_or_still_reports_is_left_alone(env):
    card, job = _job(env)
    _running(env, job)
    t0 = env.when()
    answers = (GONE, _live(jobs=[job["id"]]), GONE, _live(reporting=[job["id"]]), GONE)
    for n, answer in enumerate(answers):  # never missing for a whole minute: each sighting starts it over
        _tick(env, Host(answer), t0 + timedelta(seconds=n * dispatcher.SWEEP_S))
    assert _state(env, job)["state"] == "running"


def test_a_host_away_from_the_sweep_ends_nothing(env):
    card, job = _job(env)
    _running(env, job)
    t0 = env.when()
    _tick(env, Host(GONE), t0)
    _tick(env, Host(dispatcher.HostUnavailable("no socket")), t0 + timedelta(seconds=dispatcher.LOST_S))
    assert _state(env, job)["state"] == "running"


def test_a_cancelling_job_whose_end_never_comes_is_cancelled(env):
    card, job = _job(env)
    _running(env, job)
    env.call("digi", {"op": "job.cancel", "job": job["id"], "caller": "digi-boss", "reason": "стоп"})
    t0 = env.when()
    for seconds in (0, dispatcher.SWEEP_S, dispatcher.LOST_S):
        _tick(env, Host({"ok": True, "cancelled": "ended"}, GONE), t0 + timedelta(seconds=seconds))
    after = _state(env, job)
    assert (after["state"], after["reason"]) == ("cancelled", "стоп")


@pytest.mark.parametrize("change, reason", [
    ({"status": "Review"}, "card_not_approved"),
    ({"assignee": "@sysadmin"}, "reassigned"),
    ({"status": "Done"}, "card_done"),
])
def test_a_delivered_job_whose_card_moved_on_is_taken_back(env, change, reason):
    """Remediation §4.2 for jobs: kd-worker's admission holds until the job starts, as for commands."""
    card, job = _job(env)
    _tick(env, Host({"ok": True, "state": "queued", "position": 3}))
    env.store.edit_card(card.id, None, change, actor="user:owner")
    host = Host({"ok": True, "cancelled": "queued"}, GONE)
    assert _tick(env, host)["cancelled"] == 1
    assert host.calls[0] == {"op": "job_cancel", "job": job["id"]}
    after = _state(env, job)
    assert (after["state"], after["reason"]) == ("cancelled", reason)


def test_a_queued_job_whose_card_was_reassigned_is_cancelled_here(env):
    card, job = _job(env)
    env.store.edit_card(card.id, None, {"assignee": "@sysadmin"}, actor="user:owner")
    host = Host()
    assert _tick(env, host)["cancelled"] == 1 and host.calls == []
    assert _state(env, job)["reason"] == "reassigned"


def test_a_running_job_is_not_taken_back_when_its_card_moves_on(env):
    card, job = _job(env)
    _running(env, job)
    env.store.edit_card(card.id, None, {"status": "Review"}, actor="user:owner")
    host = Host(_live(jobs=[job["id"]]))
    _tick(env, host)
    assert _state(env, job)["state"] == "running" and host.calls == [{"op": "status"}]


# --- a continuation is checked against its basis before delivery ---------------------------------------
def _continuation(env, **card_extra):
    """An In Progress card of the analyst, its wake on creation done, and one queued continuation with its basis."""
    from kd.cabinet import continuations

    card = env.card(assignee="@analyst", status="In Progress", **card_extra)
    for command in env.store.commands(card=card.id, states=["queued"]):
        env.store.command_state(command["id"], "cancelled", reason="test")
    record = env.store.card(card.id)
    command = env.store.enqueue({"key": f"cont:{card.id}", "agent": "analyst", "thread": record.thread,
                                 "card": record.id, "kind": "continue", "initiator": "system",
                                 "text": continuations.CONTINUE.format(id=record.id),
                                 "basis": continuations.basis_of(env.store, record)})
    return record, command


def _cancelled_with(env, command, reason):
    host = Host()
    assert _tick(env, host)["cancelled"] == 1 and host.calls == []
    after = env.store.command(command["id"])
    assert (after["state"], after["reason"]) == ("cancelled", reason)


def test_scope_change_cancels_the_continuation(env):
    card, command = _continuation(env)
    env.store.edit_card(card.id, None, {"description": "a new scope"}, actor="user:owner")
    _cancelled_with(env, command, "scope_changed")


def test_review_by_the_owner_cancels_the_continuation(env):
    card, command = _continuation(env)
    env.store.edit_card(card.id, None, {"status": "Review"}, actor="user:owner")
    _cancelled_with(env, command, "status_changed")


def test_a_new_review_round_cancels_the_continuation(env):
    card = env.card(assignee="@analyst", status="In Progress")
    env.store.edit_card(card.id, None, {"status": "Review"}, actor="user:owner")
    env.store.edit_card(card.id, None, {"status": "In Progress"}, actor="user:owner")
    from kd.cabinet import continuations

    for queued in env.store.commands(card=card.id, states=["queued"]):
        env.store.command_state(queued["id"], "cancelled", reason="test")
    record = env.store.card(card.id)
    basis = continuations.basis_of(env.store, record)
    basis["iteration"] = 0  # queued before the round that has come since
    command = env.store.enqueue({"key": "cont:r", "agent": "analyst", "thread": record.thread, "card": record.id,
                                 "kind": "continue", "text": "[board] continue", "basis": basis})
    _cancelled_with(env, command, "iteration_changed")


def test_reassignment_cancels_the_continuation(env):
    card, command = _continuation(env)
    env.store.edit_card(card.id, None, {"assignee": "@dev"}, actor="user:owner")
    _cancelled_with(env, command, "reassigned")


def _grant(env, grant_id, **over):
    from kd import delegation

    fields = dict(id=grant_id, goal="KD-1", projects={"digipaddock": "@analyst"}, max_children=5, max_active=3,
                  created=env.epoch() - 60, expires=env.epoch() + 3600, state="active", approved_by="owner")
    fields.update(over)
    delegation.write_grant(env.promote.grants, delegation.Grant(**fields))


def _granted(env, grant_id):
    card, command = _continuation(env)
    env.store._conn.execute("UPDATE commands SET basis=? WHERE id=?",
                            ('{"assignee": "@analyst", "grant": "%s", "iteration": 0, "scope": "%s", '
                             '"status": "In Progress"}' % (grant_id, command["basis"]["scope"]), command["id"]))
    return card, env.store.command(command["id"])


def test_a_revoked_grant_cancels_the_continuation(env):
    grant = "a" * 32
    _grant(env, grant, state="revoked", revoked=env.epoch(), revoked_by="owner")
    card, command = _granted(env, grant)
    _cancelled_with(env, command, "grant_revoked")


def test_an_expired_grant_cancels_the_continuation(env):
    grant = "b" * 32
    _grant(env, grant, expires=env.epoch() - 1)
    card, command = _granted(env, grant)
    _cancelled_with(env, command, "grant_expired")


def test_note_edit_keeps_the_continuation(env):
    card, command = _continuation(env)
    env.store.edit_card(card.id, None, {"ac_add": ["Критерий"]}, actor="user:owner")
    from kd.cabinet import continuations

    record = env.store.card(card.id)
    env.store.command_state(command["id"], "cancelled", reason="test")
    command = env.store.enqueue({"key": "cont:again", "agent": "analyst", "thread": record.thread,
                                 "card": record.id, "kind": "continue", "text": "[board] continue again",
                                 "basis": continuations.basis_of(env.store, record)})
    env.store.edit_card(card.id, None, {"notes_append": "заметка", "ac_check": [1]}, actor="agent:analyst")
    host = Host()
    assert _tick(env, host)["delivered"] == 1 and host.calls[0]["command"] == command["id"]


# --- a card's delivery names its project's memory bank -------------------------------------------------
def _bound(env, bank="coding-agent::shop"):
    card, command = _assigned(env)
    env.store.update_project(card.project, actor="user:owner", memory_bank=bank)
    return card, command


def test_a_card_of_a_bound_project_is_delivered_with_its_bank(env):
    _bound(env)
    host = Host()
    _tick(env, host)
    assert host.calls[0]["memory"] == "coding-agent::shop"


def test_an_unbound_project_and_the_chat_carry_no_bank(env):
    _assigned(env)
    chat = env.store.agent_thread("analyst")
    env.store.enqueue({"key": "test:chat", "agent": "analyst", "thread": chat, "card": "", "kind": "chat",
                       "text": "[chat] hi"})
    host = Host()
    _tick(env, host)
    assert len(host.calls) == 2 and not [c for c in host.calls if "memory" in c]


def test_a_chat_carries_no_bank_even_when_projects_have_one(env):
    _bound(env)
    host = Host()
    _tick(env, host)  # the card's command
    chat = env.store.agent_thread("analyst")
    env.store.enqueue({"key": "test:chat", "agent": "analyst", "thread": chat, "card": "", "kind": "chat",
                       "text": "[chat] hi"})
    _tick(env, host)
    assert "memory" in host.calls[0] and "memory" not in host.calls[1]


def test_a_host_without_memory_gets_the_delivery_without_the_bank(env):
    """A host not restarted after the release refuses the unknown field: the session starts on the role bank."""
    _, command = _bound(env)
    host = Host(*OLDER, {"ok": True, "state": "queued"})
    assert _tick(env, host)["delivered"] == 1
    assert [c.get("memory") for c in host.calls] == ["coding-agent::shop", "coding-agent::shop", None]
    assert env.store.command(command["id"])["state"] == "delivered"


# --- the wake of an approved critical command is urgent ---------------------------------------------------
def _urgent(env, status="In Progress"):
    card = env.card(assignee="@analyst", status=status)
    command = env.store.enqueue({"key": "ev:urgent", "agent": "analyst", "thread": card.thread, "card": card.id,
                                 "kind": "urgent", "text": "[board] approved critical command"})
    return card, command


def test_an_urgent_command_is_delivered_as_urgent(env):
    card, command = _urgent(env)
    host = Host()
    assert _tick(env, host)["delivered"] == 1
    [request] = host.calls
    assert request["urgent"] is True and request["command"] == command["id"]
    assert env.store.command(command["id"])["state"] == "delivered"


def test_an_ordinary_command_carries_no_urgent_field(env):
    _assigned(env)
    host = Host()
    _tick(env, host)
    assert "urgent" not in host.calls[0]


def test_a_host_without_urgent_gets_the_command_as_an_ordinary_one(env):
    """A host not restarted after the release refuses the unknown field: the command still reaches it, in the
    agent's queue as before."""
    card, command = _urgent(env)
    host = Host(*OLDER, {"ok": True, "state": "queued"})
    assert _tick(env, host)["delivered"] == 1
    assert [c.get("urgent") for c in host.calls] == [True, True, None]
    assert env.store.command(command["id"])["state"] == "delivered"


def test_a_host_without_memory_keeps_an_urgent_command_urgent(env):
    """a host that knows ``urgent`` but not ``memory`` refuses the bank alone — the approved critical
    command still starts at once, on the role bank."""
    card, command = _urgent(env)
    env.store.update_project(card.project, actor="user:owner", memory_bank="coding-agent::shop")
    host = Host(*OLDER, {"ok": True, "state": "starting"})
    assert _tick(env, host)["delivered"] == 1
    assert [(c.get("urgent"), c.get("memory")) for c in host.calls] == [
        (True, "coding-agent::shop"), (True, "coding-agent::shop"), (True, None)]


def test_a_host_before_urgent_still_gets_a_bound_urgent_command(env):
    card, command = _urgent(env)
    env.store.update_project(card.project, actor="user:owner", memory_bank="coding-agent::shop")
    refused = dispatcher.HostRefused("invalid", "unknown field for deliver")
    host = Host(*OLDER, refused, {"ok": True, "state": "queued"})
    assert _tick(env, host)["delivered"] == 1
    assert [(c.get("urgent"), c.get("memory")) for c in host.calls] == [
        (True, "coding-agent::shop"), (True, "coding-agent::shop"), (True, None), (None, None)]
    assert env.store.command(command["id"])["state"] == "delivered"


def test_an_urgent_command_refused_for_another_reason_fails(env):
    card, command = _urgent(env)
    host = Host(dispatcher.HostRefused("refused", "analyst is not on this session host"))
    assert _tick(env, host)["failed"] == 1 and len(host.calls) == 1
    assert env.store.command(command["id"])["state"] == "failed"


@pytest.mark.parametrize("change, reason", [({"status": "Done"}, "card_done"),
                                            ({"assignee": "@dev"}, "reassigned")])
def test_an_urgent_command_whose_card_moved_on_is_cancelled(env, change, reason):
    card, command = _urgent(env)
    env.store.edit_card(card.id, None, change, actor="user:owner")
    host = Host()
    assert _tick(env, host)["cancelled"] == 1 and host.calls == []
    assert env.store.command(command["id"])["reason"] == reason


# --- a card's delivery names its model and effort --------------------------------------------------------
MODEL = {"id": "or-x", "provider": "openrouter", "base_url": "", "upstream": "anthropic/claude-sonnet-4.5",
         "title": "Sonnet via OpenRouter", "price_in": 3, "price_out": 15, "price_cache_read": 0.3,
         "price_cache_write": 3.75, "context": 200000}
SPEC = {"id": "or-x", "provider": "openrouter", "upstream": "anthropic/claude-sonnet-4.5", "base_url": ""}


def _modelled(env, key=True, **over):
    """A To Do card of the analyst on the cabinet model ``or-x`` (its key on the host unless ``key`` is False)."""
    env.store.create_llm_model(MODEL, actor="user:owner")
    if key:
        env.llm_dir.mkdir(exist_ok=True)
        (env.llm_dir / "openrouter.env").write_text("ANTHROPIC_AUTH_TOKEN=sk-or-test\n", encoding="utf-8")
    card = env.card(assignee="@analyst", status="Inbox")
    env.store.edit_card(card.id, None, {"model": "or-x", **over}, actor="user:owner")
    env.store.edit_card(card.id, None, {"status": "To Do"}, actor="user:owner")
    [command] = env.store.commands(card=card.id)
    return env.store.card(card.id), command


def _last_line(env, card):
    return env.store.messages(card.id)[-1]


def test_a_card_on_a_model_is_delivered_with_its_spec(env):
    _modelled(env)
    host = Host()
    assert _tick(env, host)["delivered"] == 1
    assert host.calls[0]["model"] == SPEC and "effort" not in host.calls[0]


def test_the_projects_effort_reaches_the_delivery_without_a_model(env):
    card, _ = _assigned(env)
    env.store.update_project(card.project, actor="user:owner", effort="low")
    host = Host()
    _tick(env, host)
    assert host.calls[0]["effort"] == "low" and "model" not in host.calls[0]


def test_the_cards_effort_beats_the_projects(env):
    card, _ = _modelled(env, effort="high")
    env.store.update_project(card.project, actor="user:owner", effort="low")
    host = Host()
    _tick(env, host)
    assert host.calls[0]["effort"] == "high"


def test_a_chat_carries_no_model_even_when_the_project_has_one(env):
    _modelled(env)
    env.store.update_project("digipaddock", actor="user:owner", model="or-x", effort="low")
    chat = env.store.agent_thread("analyst")
    env.store.enqueue({"key": "test:chat", "agent": "analyst", "thread": chat, "card": "", "kind": "chat",
                       "text": "[chat] hi"})
    host = Host()
    _tick(env, host)
    [chat_call] = [c for c in host.calls if c["thread"] == chat]
    assert "model" not in chat_call and "effort" not in chat_call


def test_a_disabled_model_fails_the_command_before_the_host(env):
    card, command = _modelled(env)
    env.store.update_llm_model("or-x", {"enabled": 0}, actor="user:owner")
    host = Host()
    assert _tick(env, host)["failed"] == 1 and host.calls == []
    after = env.store.command(command["id"])
    assert after["state"] == "failed" and after["reason"] == "model_disabled"
    line = _last_line(env, card)
    assert line["kind"] == "status" and line["payload"]["key"] == "sys.session.model_disabled"
    assert line["payload"]["params"] == {"model": "or-x"}


def test_a_model_without_a_key_fails_the_command_before_the_host(env):
    card, command = _modelled(env, key=False)
    host = Host()
    assert _tick(env, host)["failed"] == 1 and host.calls == []
    assert env.store.command(command["id"])["reason"] == "no_key"
    line = _last_line(env, card)
    assert line["payload"]["key"] == "sys.session.no_key"
    assert line["payload"]["params"]["provider"] == "openrouter"
    assert line["payload"]["params"]["command"].endswith("-secret")


def test_a_host_without_model_fails_the_command_as_outdated(env):
    """A host not restarted after the release refuses the field: the card is not run on the subscription behind the
    owner's back — the command fails, with no delivery without the model (only the role's context limit, newer
    still, is dropped first)."""
    card, command = _modelled(env)
    host = Host(*OLDER, {"ok": True, "state": "starting"})
    assert _tick(env, host)["failed"] == 1 and len(host.calls) == 2
    assert all(c["model"] for c in host.calls)
    after = env.store.command(command["id"])
    assert after["state"] == "failed" and after["reason"] == "host_outdated"
    assert _last_line(env, card)["payload"]["key"] == "sys.session.host_outdated"


def test_a_host_without_effort_fails_the_command_as_outdated_too(env):
    card, _ = _assigned(env)
    env.store.update_project(card.project, actor="user:owner", memory_bank="coding-agent::shop", effort="low")
    host = Host(*OLDER, {"ok": True, "state": "starting"})
    assert _tick(env, host)["failed"] == 1 and len(host.calls) == 2
    assert _last_line(env, card)["payload"]["key"] == "sys.session.host_outdated"


def test_a_host_refusing_for_no_key_fails_the_command_with_no_key(env):
    card, command = _modelled(env)
    host = Host(dispatcher.HostRefused("refused", "no_key: openrouter"))
    assert _tick(env, host)["failed"] == 1
    after = env.store.command(command["id"])
    assert after["reason"] == "no_key" and after["error"] == "refused: no_key: openrouter"
    line = _last_line(env, card)
    assert line["payload"]["key"] == "sys.session.no_key" and line["payload"]["params"]["provider"] == "openrouter"


# --- a model's probe goes through the host ------------------------------------------------------------
def _probing(env, key=True):
    env.store.create_llm_model(MODEL, actor="user:owner")
    if key:
        env.llm_dir.mkdir(exist_ok=True)
        (env.llm_dir / "openrouter.env").write_text("ANTHROPIC_AUTH_TOKEN=sk-or-test\n", encoding="utf-8")
    env.store.update_llm_model("or-x", {"probe": {"state": "requested", "at": env.now}}, actor="user:owner")


def _probe_calls(host):
    return [c for c in host.calls if c.get("op") == "probe"]


def test_a_requested_probe_goes_to_the_host_once_and_its_report_ends_it(env):
    _probing(env)
    host = Host()
    _tick(env, host)
    [call] = _probe_calls(host)
    assert call["model"] == SPEC and dispatcher.HEX32.fullmatch(call["probe"])
    probe = env.store.llm_model("or-x")["probe"]
    assert probe == {"state": "running", "at": env.now, "id": call["probe"]}
    _tick(env, host)
    assert len(_probe_calls(host)) == 1  # a running probe is not asked again
    env.now = "2026-10-14T10:00:30Z"
    env.call("root", {"op": "host.report", "items": [
        {"type": "probe", "probe": call["probe"], "ok": True, "model": "anthropic/claude-sonnet-4.5", "ms": 1200,
         "detail": "OK"}]})
    assert env.store.llm_model("or-x")["probe"] == {"at": env.now, "ok": True, "model": "anthropic/claude-sonnet-4.5",
                                                   "ms": 1200, "detail": "OK"}
    _tick(env, host)
    assert len(_probe_calls(host)) == 1


def test_a_busy_or_absent_host_is_asked_again_next_tick(env):
    _probing(env)
    for answer in (dispatcher.HostRefused("refused", "busy"), dispatcher.HostUnavailable("no socket")):
        host = Host(answer)
        _tick(env, host)
        assert len(_probe_calls(host)) == 1
        assert env.store.llm_model("or-x")["probe"]["state"] == "requested"
    host = Host()
    _tick(env, host)
    assert env.store.llm_model("or-x")["probe"]["state"] == "running"


def test_a_probe_of_a_kind_without_its_key_fails_without_the_host(env):
    _probing(env, key=False)
    host = Host()
    _tick(env, host)
    probe = env.store.llm_model("or-x")["probe"]
    assert _probe_calls(host) == [] and probe["ok"] is False and "no key" in probe["detail"]
    assert probe["at"] == env.now and "state" not in probe


def test_a_probe_the_host_refuses_fails_with_its_reason(env):
    _probing(env)
    _tick(env, Host(dispatcher.HostRefused("refused", "no_key: openrouter")))
    probe = env.store.llm_model("or-x")["probe"]
    assert probe["ok"] is False and "no key" in probe["detail"]
    _probing_again = env.store.update_llm_model("or-x", {"probe": {"state": "requested", "at": env.now}},
                                                actor="user:owner")
    _tick(env, Host(dispatcher.HostRefused("invalid", "unknown operation")))
    probe = env.store.llm_model("or-x")["probe"]
    assert probe["ok"] is False and "unknown operation" in probe["detail"]


def test_a_running_probe_without_a_result_fails_after_the_timeout(env):
    _probing(env)
    host = Host()
    _tick(env, host)
    late = env.when() + timedelta(seconds=dispatcher.PROBE_TIMEOUT - 1)
    _tick(env, host, late)
    assert env.store.llm_model("or-x")["probe"]["state"] == "running"
    _tick(env, host, late + timedelta(seconds=2))
    probe = env.store.llm_model("or-x")["probe"]
    assert probe["ok"] is False and "no result" in probe["detail"] and len(_probe_calls(host)) == 1


def test_another_refusal_of_a_modelled_command_is_host_refused(env):
    card, command = _modelled(env)
    _tick(env, Host(dispatcher.HostRefused("refused", "analyst is not hosted")))
    after = env.store.command(command["id"])
    assert after["reason"] == "host_refused"
    assert not [m for m in env.store.messages(card.id) if m["kind"] == "status"]


# --- a card on a model of the Claude subscription ---------------------------------------------------------
SONNET = {"id": "claude:sonnet", "provider": "subscription", "upstream": "sonnet", "base_url": ""}


def _on_subscription(env, ref="claude:sonnet"):
    card = env.card(assignee="@analyst", status="Inbox")
    env.store.edit_card(card.id, None, {"model": ref}, actor="user:owner")
    env.store.edit_card(card.id, None, {"status": "To Do"}, actor="user:owner")
    [command] = env.store.commands(card=card.id)
    return env.store.card(card.id), command


def test_a_card_on_a_subscription_model_is_delivered_with_its_spec(env):
    """No key file of any kind is on the host: a subscription model needs none."""
    _, command = _on_subscription(env)
    host = Host()
    assert _tick(env, host)["delivered"] == 1
    assert host.calls[0]["model"] == SONNET
    assert env.store.command(command["id"])["state"] == "delivered"


def test_an_older_host_refusing_the_subscription_spec_is_host_outdated(env):
    """A host of the release before knows no kind ``subscription``: the command fails as outdated and is never sent
    again without the model — the card does not run on the agent's own model behind the owner's back."""
    card, command = _on_subscription(env)
    refusal = dispatcher.HostRefused("invalid", "model: unknown provider 'subscription'; the catalog has openrouter")
    host = Host(refusal, {"ok": True, "state": "starting"})
    assert _tick(env, host)["failed"] == 1 and len(host.calls) == 1
    after = env.store.command(command["id"])
    assert after["state"] == "failed" and after["reason"] == "host_outdated"
    assert _last_line(env, card)["payload"]["key"] == "sys.session.host_outdated"


def test_a_subscription_model_probe_goes_to_the_host(env):
    env.call("root", {"op": "llm.probe.request", "id": "claude:opus"})
    host = Host()
    _tick(env, host)
    [call] = _probe_calls(host)
    assert call["model"] == {"id": "claude:opus", "provider": "subscription", "upstream": "opus", "base_url": ""}
    probe = llm_api.row(env.store, "claude:opus")["probe"]
    assert probe == {"state": "running", "at": env.now, "id": call["probe"]}
    _tick(env, host)
    assert len(_probe_calls(host)) == 1
    env.call("root", {"op": "host.report", "items": [
        {"type": "probe", "probe": call["probe"], "ok": True, "model": "claude-opus-5-5", "ms": 900, "detail": "OK"}]})
    assert llm_api.row(env.store, "claude:opus")["probe"]["ok"] is True


def test_a_card_on_an_alias_gone_from_the_catalog_is_delivered_as_the_agents_own(env):
    """The reference of an alias a later catalog dropped names no model — the
    command goes to the host without one, as a card's without a model does."""
    card, command = _on_subscription(env)
    env.store._conn.execute("UPDATE cards SET model='claude:old' WHERE id=?", (card.id,))
    host = Host()
    assert _tick(env, host)["delivered"] == 1
    assert host.calls[0].get("model") is None
    assert env.store.command(command["id"])["state"] == "delivered"
