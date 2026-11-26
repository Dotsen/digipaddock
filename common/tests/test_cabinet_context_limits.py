"""A role's session context limit: one value per agent role in the cabinet (``context.*``, Settings -> Models), the
journal of its changes, and the value in force on each session's delivery and each worker job's definition."""

import asyncio

import pytest
from cabinet_env import CabinetEnv
from web_env import WebEnv

from kd import context_limit, session_config
from kd.cabinet import (
    api,
    config,
    context_limits,
    decisions,
    dispatcher,
    service,
    users,
)


def person(user):
    return api.Principal("user", -1, user)


@pytest.fixture
def env(tmp_path):
    cab = CabinetEnv(tmp_path)
    people = users.Users(cab.store)
    people.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    people.create(
        "anna", "Анна", roles=[("partner", ["web", "digipaddock"])], actor="user:owner"
    )
    return cab


def call(env, user, request):
    return api.dispatch(env.ctx, person(user), request)


def refused(env, user, request):
    with pytest.raises(api.ApiError) as exc:
        call(env, user, request)
    return exc.value


def limits(env, user="owner"):
    return {
        row["agent"]: row for row in call(env, user, {"op": "context.list"})["limits"]
    }


def changes(env):
    return [
        e
        for e in env.store.events_since(0, 1000)
        if e["kind"] == "context_limit.changed"
    ]


# --- the store and the API -----------------------------------------------------------------------------------
def test_every_role_of_the_registry_starts_on_the_default(env):
    listed = call(env, "owner", {"op": "context.list"})["limits"]
    assert [row["agent"] for row in listed] == ["analyst", "boss", "dev", "sysadmin"]
    assert all(
        row == {"agent": row["agent"], "limit": 400000, "set": False} for row in listed
    )
    assert context_limit.DEFAULT == 400000
    # a role registered later is listed on the default too: the list is the registry's, not the store's
    env.registry.write_text(
        '{"dev": {"user": "digi-dev"}, "writer": {"user": "digi-writer"}}',
        encoding="utf-8",
    )
    assert limits(env)["writer"] == {"agent": "writer", "limit": 400000, "set": False}


def test_the_owner_sets_a_limit_or_no_limit_per_role(env):
    row = call(env, "owner", {"op": "context.set", "agent": "dev", "limit": 250000})[
        "limit"
    ]
    assert row == {"agent": "dev", "limit": 250000, "set": True}
    call(env, "owner", {"op": "context.set", "agent": "sysadmin", "limit": "none"})
    found = limits(env)
    assert found["dev"]["limit"] == 250000 and found["sysadmin"] == {
        "agent": "sysadmin",
        "limit": "none",
        "set": True,
    }
    assert found["analyst"]["limit"] == 400000  # roles are independent
    assert context_limits.effective(env.store, "dev") == 250000
    assert context_limits.effective(env.store, "sysadmin") == 0  # no limit
    assert context_limits.effective(env.store, "analyst") == 400000
    # the bounds themselves are allowed
    for value in (100000, 1000000):
        assert (
            call(env, "owner", {"op": "context.set", "agent": "boss", "limit": value})[
                "limit"
            ]["limit"]
            == value
        )


def test_each_change_is_journaled_with_the_old_and_the_new_value(env):
    call(env, "owner", {"op": "context.set", "agent": "dev", "limit": 250000})
    call(env, "owner", {"op": "context.set", "agent": "dev", "limit": "none"})
    call(
        env, "owner", {"op": "context.set", "agent": "dev", "limit": "none"}
    )  # the same: nothing to journal
    found = changes(env)
    assert [(e["actor"], e["card"], e["payload"]) for e in found] == [
        ("user:owner", "", {"agent": "dev", "limit": {"from": 400000, "to": 250000}}),
        ("user:owner", "", {"agent": "dev", "limit": {"from": 250000, "to": "none"}}),
    ]


@pytest.mark.parametrize(
    "bad", [99999, 1000001, 0, -1, "abc", "", None, 400000.5, "400000", True, [400000]]
)
def test_an_invalid_limit_is_refused_and_changes_nothing(env, bad):
    error = refused(env, "owner", {"op": "context.set", "agent": "dev", "limit": bad})
    assert error.kind == "invalid" and error.reason == "context_limit_range"
    assert (
        limits(env)["dev"] == {"agent": "dev", "limit": 400000, "set": False}
        and changes(env) == []
    )


def test_an_unknown_role_or_a_missing_field_is_refused(env):
    assert (
        refused(
            env, "owner", {"op": "context.set", "agent": "nobody", "limit": 200000}
        ).kind
        == "not_found"
    )
    assert (
        refused(
            env, "owner", {"op": "context.set", "agent": "Dev!", "limit": 200000}
        ).kind
        == "invalid"
    )
    assert (
        refused(env, "owner", {"op": "context.set", "agent": "dev"}).kind == "invalid"
    )
    assert (
        refused(
            env, "owner", {"op": "context.set", "agent": "dev", "limit": 200000, "x": 1}
        ).kind
        == "invalid"
    )


def test_only_settings_reads_or_changes_the_limits(env):
    assert refused(env, "anna", {"op": "context.list"}).kind == "refused"
    assert (
        refused(
            env, "anna", {"op": "context.set", "agent": "dev", "limit": 200000}
        ).kind
        == "refused"
    )
    for request in (
        {"op": "context.list"},
        {"op": "context.set", "agent": "dev", "limit": 200000},
    ):
        with pytest.raises(
            api.ApiError
        ) as exc:  # an agent never reads or sets its own window
            env.call("digi-dev", request)
        assert exc.value.kind == "refused"
    assert env.call("root", {"op": "context.list"})[
        "limits"
    ]  # root and the cabinet's CLI do, as with models
    assert changes(env) == []


# --- the delivery and the job ----------------------------------------------------------------------------------
class Host:
    """A session host that records requests and answers from a script."""

    def __init__(self, *answers):
        self.calls, self.answers = [], list(answers)

    async def call(self, request, timeout=10.0):
        self.calls.append(request)
        answer = (
            self.answers.pop(0) if self.answers else {"ok": True, "state": "starting"}
        )
        if isinstance(answer, Exception):
            raise answer
        return answer


class Quiet:
    def catch_up(self, decisions):
        pass


@pytest.fixture
def svc(env, tmp_path):
    env.ctx.llm_env_dir = str(tmp_path / "llm")
    api.install_host(
        env.ctx,
        lambda: session_config.HostConfig({"analyst": 1, "dev": 1, "sysadmin": 1}, 600),
    )
    return service.Service(
        env.ctx,
        config.load("/none", "/none", dev=True),
        users=env.users,
        decisions=decisions.Decisions(env.store),
        notifier=Quiet(),
        now=lambda: env.now,
    )


def _tick(env, svc, host):
    return asyncio.run(dispatcher.tick(svc, host=host, now=env.when()))


def _wake(env, agent):
    card = env.card(assignee=f"@{agent}", status="Inbox")
    env.store.edit_card(card.id, None, {"status": "To Do"}, actor="user:owner")
    return card


def test_a_delivery_carries_its_roles_limit_in_force(env, svc):
    _wake(env, "analyst")
    host = Host()
    assert _tick(env, svc, host)["delivered"] == 1
    assert (
        host.calls[0]["context_limit"] == 400000
    )  # the default: the role was never set


def test_a_changed_limit_reaches_the_next_delivery_and_no_limit_sends_nothing(env, svc):
    call(env, "owner", {"op": "context.set", "agent": "analyst", "limit": 150000})
    _wake(env, "analyst")
    host = Host()
    _tick(env, svc, host)
    assert host.calls[0]["context_limit"] == 150000
    call(env, "owner", {"op": "context.set", "agent": "analyst", "limit": "none"})
    chat = env.store.agent_thread("analyst")
    env.store.enqueue(
        {
            "key": "test:chat",
            "agent": "analyst",
            "thread": chat,
            "card": "",
            "kind": "chat",
            "text": "[chat] hi",
        }
    )
    _tick(env, svc, host)
    assert host.calls[1]["thread"] == chat and "context_limit" not in host.calls[1]


def test_the_roles_are_independent(env, svc):
    call(env, "owner", {"op": "context.set", "agent": "dev", "limit": 120000})
    _wake(env, "sysadmin")
    _wake(env, "dev")
    host = Host()
    assert _tick(env, svc, host)["delivered"] == 2
    assert {c["agent"]: c["context_limit"] for c in host.calls} == {
        "sysadmin": 400000,
        "dev": 120000,
    }


def test_a_host_that_does_not_know_the_field_gets_the_delivery_without_it(env, svc):
    """A host not restarted after the release refuses the unknown field: the session starts as before, on Claude
    Code's own window, rather than not at all."""
    _wake(env, "analyst")
    host = Host(
        dispatcher.HostRefused("invalid", "unknown field for deliver"),
        {"ok": True, "state": "starting"},
    )
    assert _tick(env, svc, host)["delivered"] == 1
    assert [c.get("context_limit") for c in host.calls] == [400000, None]


def test_a_host_without_the_field_still_runs_a_cards_model(env, svc):
    """the limit goes first: a host that knows the model but not the limit runs the card on its model; one that
    knows neither fails it ``host_outdated`` as before."""
    env.store.update_project("digipaddock", actor="user:owner", effort="low")
    _wake(env, "analyst")
    unknown = dispatcher.HostRefused("invalid", "unknown field for deliver")
    host = Host(unknown, {"ok": True, "state": "starting"})
    assert _tick(env, svc, host)["delivered"] == 1
    assert [(c.get("context_limit"), c.get("effort")) for c in host.calls] == [
        (400000, "low"),
        (None, "low"),
    ]
    _wake(env, "analyst")
    host = Host(unknown, unknown)
    assert _tick(env, svc, host)["failed"] == 1 and len(host.calls) == 2


def _job(env, agent="dev"):
    card = env.card()
    job = {
        "id": "1" * 32,
        "card": card.id,
        "agent": agent,
        "type": "developer",
        "repo": "digipaddock",
        "spec": "implementer",
        "skills": [],
        "prompt": {},
        "limits": {"write": "repo", "capabilities": [], "network": "none"},
        "brief": "b",
        "caller": "digi-boss",
        "requester": "boss",
        "writer": True,
        "timeout": 3600,
        "model": "sonnet",
    }
    env.call(
        "digi",
        {
            "op": "job.submit",
            "job": job,
            "limits": {"jobs_per_day": 9, "attempts_per_card_spec": 9},
        },
    )
    return job


def test_a_worker_job_takes_its_roles_limit_when_it_is_sent(env, svc):
    job = _job(env)
    call(
        env, "owner", {"op": "context.set", "agent": "dev", "limit": 300000}
    )  # after the submit: the value sent
    call(
        env, "owner", {"op": "context.set", "agent": "analyst", "limit": 100000}
    )  # another role's: not this job's
    host = Host({"ok": True, "state": "queued", "position": 1})
    _tick(env, svc, host)
    assert [c for c in host.calls if c["op"] == "job"] == [
        {
            "op": "job",
            "job": job["id"],
            "agent": "dev",
            "definition": {**job, "effort": "", "context_limit": 300000},
        }
    ]
    # the stored definition is kd-worker's with the card's model and effort; the limit is the send's
    stored = env.store._conn.execute(
        "SELECT data FROM jobs WHERE id=?", (job["id"],)
    ).fetchone()[0]
    assert "context_limit" not in stored


def test_a_worker_job_of_a_role_without_limit_carries_none(env, svc):
    call(env, "owner", {"op": "context.set", "agent": "dev", "limit": "none"})
    _job(env)
    host = Host({"ok": True, "state": "queued", "position": 1})
    _tick(env, svc, host)
    [sent] = [c for c in host.calls if c["op"] == "job"]
    assert "context_limit" not in sent["definition"]


# --- over HTTP -------------------------------------------------------------------------------------------------
@pytest.fixture
def w(tmp_path):
    w = WebEnv(tmp_path)
    w.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    w.users.create(
        "anna", "Анна", roles=[("partner", ["web", "digipaddock"])], actor="root"
    )
    return w


def test_the_settings_page_reads_and_saves_a_roles_limit(w):
    w.login()
    listed = w.client.get("/api/context-limits").json()
    assert [r["agent"] for r in listed] == [
        "analyst",
        "boss",
        "dev",
        "sysadmin",
    ] and listed[0]["limit"] == 400000
    r = w.client.put(
        "/api/context-limits/dev", headers=w.headers(), json={"limit": 200000}
    )
    assert r.status_code == 200 and r.json() == {
        "agent": "dev",
        "limit": 200000,
        "set": True,
    }
    r = w.client.put(
        "/api/context-limits/dev", headers=w.headers(), json={"limit": "none"}
    )
    assert r.status_code == 200 and r.json()["limit"] == "none"
    r = w.client.put(
        "/api/context-limits/dev", headers=w.headers(), json={"limit": 50000}
    )
    assert r.status_code == 400 and r.json()["reason"] == "context_limit_range"
    assert (
        w.client.put(
            "/api/context-limits/dev", headers=w.headers(), json={"limit": 2e5, "x": 1}
        ).status_code
        == 400
    )
    assert (
        w.client.put(
            "/api/context-limits/nobody", headers=w.headers(), json={"limit": 200000}
        ).status_code
        == 404
    )


def test_a_partner_neither_reads_nor_changes_the_limits(w):
    anna = w.other_client()
    me = w.login("anna", client=anna)
    assert anna.get("/api/context-limits").status_code == 403
    assert (
        anna.put(
            "/api/context-limits/dev",
            headers=w.headers(me["csrf"]),
            json={"limit": 200000},
        ).status_code
        == 403
    )
