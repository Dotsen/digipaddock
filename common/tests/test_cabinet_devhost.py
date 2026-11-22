"""the dev cabinet's scripted session host runs a turn through the real dispatcher and host.report."""

import asyncio

import pytest
from web_env import WebEnv

from kd import session_config
from kd.cabinet import api, devhost, dispatcher


@pytest.fixture
def w(tmp_path):
    w = WebEnv(tmp_path)
    api.install_host(w.env.ctx, lambda: session_config.HostConfig({"analyst": 1}, 600))
    return w


def _run(w, host):
    async def go():
        await dispatcher.tick(w.svc, host=host, now=w.env.when())
        while host.tasks:
            await asyncio.gather(*list(host.tasks))

    asyncio.run(go())


def _assigned(w):
    card = w.env.card(assignee="@analyst", status="Inbox")
    w.env.store.edit_card(card.id, None, {"status": "To Do"}, actor="user:owner")
    return w.env.store.card(card.id)


def test_devhost_runs_a_turn_through_the_dispatcher(w):
    card = _assigned(w)
    host = devhost.DevHost(w.svc, step=0)
    _run(w, host)
    [command] = w.env.store.commands(card=card.id)
    [session] = w.env.store.sessions(thread=card.thread)
    assert command["state"] == "done" and session["state"] == "idle" and session["slot"] == "analyst/1"
    last = w.env.store.messages(card.id)[-1]
    assert last["author"] == "agent:analyst" and last["body"] == f"Got it: [board] {card.id} assigned to you"
    [turn] = w.env.call("root", {"op": "session.turns", "session": session["id"]})["turns"]
    assert turn["command"] == command["id"] and [s["kind"] for s in turn["steps"]] == ["tool_call", "tool_result", "text"]
    seq = turn["steps"][1]["seq"]
    assert w.env.call("root", {"op": "session.output", "session": session["id"], "seq": seq})["output"]["text"] == (
        "brief.md\nnotes.md")
    _run(w, host)  # nothing new: no second turn
    assert sum(m["author"] == "agent:analyst" and m["kind"] == "text" for m in w.env.store.messages(card.id)) == 1


def test_a_fail_marker_fails_once_and_continue_runs_again(w):
    card = _assigned(w)
    host = devhost.DevHost(w.svc, step=0)
    _run(w, host)
    w.env.store.add_message(card.id, author="user:owner", kind="text", body="@analyst [fail] проверь", mentions=["@analyst"])
    _run(w, host)
    [session] = w.env.store.sessions(thread=card.thread)
    newest = w.env.store.commands(card=card.id)[0]
    assert session["state"] == "failed" and newest["state"] == "failed" and newest["kind"] == "message"
    owner = api.Principal("user", -1, "owner")
    w.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    api.dispatch(w.env.ctx, owner, {"op": "user.session.continue", "session": session["id"]})
    _run(w, host)
    assert w.env.store.session(session["id"])["state"] == "idle"
    assert w.env.store.commands(card=card.id)[0]["state"] == "done"


def test_stop_ends_the_session(w):
    card = _assigned(w)
    host = devhost.DevHost(w.svc, step=0)
    _run(w, host)
    [session] = w.env.store.sessions(thread=card.thread)
    w.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    api.dispatch(w.env.ctx, api.Principal("user", -1, "owner"), {"op": "user.session.stop", "session": session["id"]})
    _run(w, host)
    assert w.env.store.session(session["id"])["state"] == "stopped"


# --- the dev host plays a council's critics, so a council in the dev cabinet ends as a real one does ----
import council_env as ce  # noqa: E402

from kd.cabinet import councils, jobs  # noqa: E402

PAIR = {"brief": {"critics": ["claude:opus", "claude:sonnet"], "rounds": 2}}


@pytest.fixture
def c(tmp_path):
    return WebEnv(tmp_path, env=ce.make(tmp_path))


def _handed_in(c):
    card = ce.draft(c.env, PAIR)
    ce.handin(c.env, card.id)
    found = ce.council_of(c.env, card.id)
    return card, found, councils.runs(c.env.store, found["id"], 1)


def _request(c, run):
    job = jobs.row(c.env.store, run["job"])
    return {"op": "job", "job": job["id"], "agent": job["agent"], "definition": job["data"]["definition"]}


def test_devhost_plays_a_councils_critics(c):
    card, found, _ = _handed_in(c)

    _run(c, devhost.DevHost(c.svc, step=0))

    runs = councils.runs(c.env.store, found["id"], 1)
    assert [(r["state"], r["verdict"]) for r in runs] == [("done", "ready"), ("done", "ready")]
    assert jobs.row(c.env.store, runs[0]["job"])["slot"] == "analyst/worker1"
    assert councils.row(c.env.store, found["id"])["state"] == "clean"


def test_a_job_delivered_twice_is_played_once(c):
    _, _, runs = _handed_in(c)
    host, request = devhost.DevHost(c.svc, step=0), _request(c, runs[0])

    async def go():
        assert (await host.call(request))["ok"] and (await host.call(request))["ok"]
        assert len(host.tasks) == 1
        await asyncio.gather(*list(host.tasks))

    asyncio.run(go())

    assert jobs.row(c.env.store, runs[0]["job"])["state"] == "done"


def test_devhost_refuses_a_job_that_is_no_critic(c):
    _, _, runs = _handed_in(c)
    request = _request(c, runs[0])
    request["definition"] = {**request["definition"], "spec": "implementer"}
    host = devhost.DevHost(c.svc, step=0)

    with pytest.raises(dispatcher.HostRefused):
        asyncio.run(host.call(request))

    assert not host.tasks


def test_a_job_cancelled_before_it_started_is_never_reported(c):
    _, _, runs = _handed_in(c)
    host, request = devhost.DevHost(c.svc, step=0), _request(c, runs[0])

    async def go():
        await host.call(request)
        answer = await host.call({"op": "job_cancel", "job": request["job"]})
        await asyncio.gather(*list(host.tasks))
        return answer

    answer = asyncio.run(go())

    assert answer == {"ok": True, "cancelled": "queued"}
    job = jobs.row(c.env.store, request["job"])
    assert job["state"] == "queued" and not job["started"]  # the host said nothing of it


def test_a_job_cancelled_while_it_runs_ends_cancelled(c):
    _, found, runs = _handed_in(c)
    host, request = devhost.DevHost(c.svc, step=0.4), _request(c, runs[0])

    async def go():
        await host.call(request)
        await asyncio.sleep(0.6)  # started, not ended yet
        answer = await host.call({"op": "job_cancel", "job": request["job"]})
        await asyncio.gather(*list(host.tasks))
        return answer

    answer = asyncio.run(go())

    assert answer == {"ok": True, "cancelled": "running"}
    assert jobs.row(c.env.store, request["job"])["state"] == "cancelled"
    assert councils.runs(c.env.store, found["id"], 1)[0]["verdict"] == ""  # no block of a cancelled read


def test_job_cancel_of_a_job_the_host_never_had_says_unknown(c):
    host = devhost.DevHost(c.svc, step=0)

    assert asyncio.run(host.call({"op": "job_cancel", "job": "0" * 32})) == {"ok": True, "cancelled": "unknown"}


# --- a played run on a subscription model reports the model that served it -------------------------------
from kd.cabinet import usage  # noqa: E402


def test_a_played_critic_on_a_subscription_model_reports_what_served_it(c):
    card, found, _ = _handed_in(c)

    _run(c, devhost.DevHost(c.svc, step=0))

    seen = councils.view(c.env.store, councils.row(c.env.store, found["id"]))
    assert [run["served"] for run in seen["rounds_list"][0]["runs"]] == [["claude-opus-5-5"], ["claude-sonnet-5-5"]]
    assert usage.version(c.env.store, "claude:opus") == "claude-opus-5-5"
    assert usage.version(c.env.store, "claude:sonnet") == "claude-sonnet-5-5"


def test_a_played_turn_on_a_subscription_model_reports_what_served_it(w):
    card = _assigned(w)
    plain = w.env.card(assignee="@analyst", status="Inbox")
    w.env.store.edit_card(plain.id, None, {"status": "To Do"}, actor="user:owner")
    w.env.store.edit_card(card.id, None, {"model": "claude:sonnet"}, actor="user:owner")
    host = devhost.DevHost(w.svc, step=0)
    _run(w, host)
    [session] = w.env.store.sessions(thread=card.thread)
    assert session["model"] == "claude:sonnet"
    [turn] = w.env.call("root", {"op": "session.turns", "session": session["id"]})["turns"]
    assert turn["served"] == ["claude-sonnet-5-5"] and turn["cost"] > 0
    assert usage.version(w.env.store, "claude:sonnet") == "claude-sonnet-5-5"
    # a turn on the agent's own model reports as before: nothing
    [other] = w.env.store.sessions(thread=w.env.store.card(plain.id).thread)
    [quiet] = w.env.call("root", {"op": "session.turns", "session": other["id"]})["turns"]
    assert (quiet["served"], quiet["cost"], other["model"]) == ([], 0, "")
