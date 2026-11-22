"""sessions, commands and the agent chat over HTTP — each behind the person's projects and permissions."""

import pytest
from web_env import WebEnv

from kd import session_config
from kd.cabinet import api

HOSTED = session_config.HostConfig({"analyst": 2}, 600)


@pytest.fixture
def w(tmp_path):
    w = WebEnv(tmp_path)
    api.install_host(w.env.ctx, lambda: HOSTED)
    w.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    w.users.create("anna", "Анна", roles=[("partner", ["web"])], actor="root")
    w.users.create("petr", "Пётр", roles=[("partner", ["digipaddock"])], actor="root")
    w.users.create("vera", "Вера", roles=[("viewer", "*")], actor="root")
    return w


def _as(w, user):
    browser = w.other_client()
    me = w.login(user, client=browser)
    return browser, w.headers(me["csrf"])


def _assigned(w, title="Card"):
    """A To Do card of the analyst, its command delivered to the card's session."""
    card = w.env.card(assignee="@analyst", status="Inbox", title=title)
    w.env.store.edit_card(card.id, None, {"status": "To Do"}, actor="user:owner")
    [command] = w.env.store.commands(card=card.id)
    session = w.env.store.session_for(command["thread"], command["agent"])
    w.env.store.command_state(command["id"], "delivered", session=session["id"])
    return w.env.store.card(card.id), command, session


def test_card_sessions_and_commands_visible_by_project(w):
    card, command, session = _assigned(w)
    w.login()
    commands = w.client.get(f"/api/cards/{card.id}/commands").json()
    assert [(c["id"], c["state"]) for c in commands] == [(command["id"], "delivered")]
    assert [s["id"] for s in w.client.get(f"/api/cards/{card.id}/sessions").json()] == [session["id"]]
    anna, _ = _as(w, "anna")
    assert anna.get(f"/api/cards/{card.id}/commands").status_code == 404
    assert anna.get(f"/api/cards/{card.id}/sessions").status_code == 404


def _job(card, n=1):
    """A worker job of @dev on the card, as kd-worker submits it."""
    return {
        "id": f"{n:032x}", "card": card, "agent": "dev", "type": "developer", "repo": "digipaddock", "spec": "implementer",
        "skills": [], "prompt": {"spec": {"name": "implementer", "version": 1, "sha256": "0" * 64}, "skills": []},
        "limits": {"write": "repo", "capabilities": [], "network": "none"}, "brief": "Сделай по ТЗ.",
        "caller": "digi-boss", "requester": "boss", "writer": True, "timeout": 3600, "model": "sonnet",
    }


def test_card_jobs_visible_by_project(w):
    card = w.env.card(assignee="@dev", project="digipaddock")
    limits = {"jobs_per_day": 10, "attempts_per_card_spec": 3}
    w.env.call("digi", {"op": "job.submit", "job": _job(card.id), "limits": limits})
    w.env.call("root", {"op": "host.report", "items": [
        {"type": "job", "job": f"{1:032x}", "state": "starting", "slot": "dev/worker1"}]})
    w.login()
    (job,) = w.client.get(f"/api/cards/{card.id}/jobs").json()
    assert (job["id"], job["spec"], job["state"], job["slot"]) == (f"{1:032x}", "implementer", "running", "dev/worker1")
    assert "brief" not in job and "caller" not in job  # the view, never the definition
    anna, _ = _as(w, "anna")
    assert anna.get(f"/api/cards/{card.id}/jobs").status_code == 404
    petr, _ = _as(w, "petr")
    assert [j["id"] for j in petr.get(f"/api/cards/{card.id}/jobs").json()] == [f"{1:032x}"]


def test_continue_and_stop_need_agents_control(w):
    card, _, session = _assigned(w)
    w.env.store.session_update(session["id"], state="working")
    petr, headers = _as(w, "petr")  # sees the card, may not steer the agent
    assert petr.get(f"/api/cards/{card.id}/sessions").status_code == 200
    r = petr.post(f"/api/sessions/{session['id']}/stop", headers=headers)
    assert r.status_code == 403 and r.json()["error"] == "refused"
    w.login()
    assert w.client.post(f"/api/sessions/{session['id']}/stop").status_code == 403  # no CSRF token
    r = w.client.post(f"/api/sessions/{session['id']}/stop", headers=w.headers())
    assert r.status_code == 200 and r.json()["kind"] == "stop"
    r = w.client.post(f"/api/sessions/{session['id']}/continue", headers=w.headers())
    assert r.status_code == 403  # a working session has nothing to continue
    w.env.store.session_update(session["id"], state="failed")
    r = w.client.post(f"/api/sessions/{session['id']}/continue", headers=w.headers())
    assert r.status_code == 200 and r.json()["kind"] == "continue" and r.json()["card"] == card.id
    assert w.client.post("/api/sessions/" + "0" * 32 + "/continue", headers=w.headers()).status_code == 404


def test_turns_and_output_over_http_need_agents_control(w):
    """a session's turns and a tool's output — for whoever controls the agent."""
    card, command, session = _assigned(w)
    w.env.call("root", {"op": "host.report", "items": [
        {"type": "turn", "session": session["id"], "command": command["id"], "phase": "started"},
        {"type": "message", "session": session["id"], "command": command["id"], "kind": "tool_result", "body": "",
         "payload": {"id": "toolu_1", "ok": True, "size": 5}, "output": {"text": "вывод", "size": 5,
                                                                         "truncated": False}}]})
    seq = w.env.store.messages(card.id)[-1]["seq"]
    w.login()
    [turn] = w.client.get(f"/api/sessions/{session['id']}/turns").json()
    assert turn["command"] == command["id"] and [s["seq"] for s in turn["steps"]] == [seq]
    r = w.client.get(f"/api/sessions/{session['id']}/output/{seq}")
    assert r.status_code == 200 and r.json() == {"text": "вывод", "size": 5, "truncated": False}
    assert w.client.get(f"/api/sessions/{session['id']}/output/{seq + 1}").status_code == 404
    petr, _ = _as(w, "petr")  # sees the card, may not steer the agent
    assert petr.get(f"/api/sessions/{session['id']}/turns").status_code == 403
    assert petr.get(f"/api/sessions/{session['id']}/output/{seq}").status_code == 403
    anna, _ = _as(w, "anna")  # does not see the card
    assert anna.get(f"/api/sessions/{session['id']}/turns").status_code == 404


def test_agent_chat_get_post_need_agents_chat(w):
    w.login()
    r = w.client.post("/api/agents/analyst/messages", headers=w.headers(),
                      json={"body": "Как дела?", "client_key": "chat-0001"})
    assert r.status_code == 200
    message = r.json()
    assert message["body"] == "Как дела?" and message["author_name"] == "Владелец"
    [command] = w.env.store.commands(agent="analyst")
    assert command["kind"] == "chat" and command["card"] == ""
    found = w.client.get("/api/agents/analyst/messages").json()
    assert [m["seq"] for m in found["messages"]] == [message["seq"]] and isinstance(found["watermark"], int)
    assert w.client.get("/api/agents/analyst/messages", params={"after": message["seq"]}).json()["messages"] == []
    page = w.client.get("/api/agents/analyst/messages", params={"limit": 1}).json()
    assert [m["seq"] for m in page["messages"]] == [message["seq"]] and "more" in page
    vera, headers = _as(w, "vera")  # a viewer: no agents.chat
    assert vera.get("/api/agents/analyst/messages").status_code == 403
    assert vera.post("/api/agents/analyst/messages", headers=headers, json={"body": "x"}).status_code == 403
    r = w.client.post("/api/agents/dev/messages", headers=w.headers(), json={"body": "x"})
    assert r.status_code == 403 and "session host" in r.json()["message"]  # dev is still in its digipaddock panel
    assert w.client.get("/api/agents/ghost/messages").status_code == 404


def test_the_monitor_thread_over_http_is_apart_from_the_chat(w):
    """The "Monitor" thread has its own messages, read cursor, presence and files; the agent page says it exists."""
    w.login()
    assert w.client.get("/api/agents/analyst").json()["monitor"] is False
    r = w.client.post("/api/agents/analyst/monitor/messages", headers=w.headers(),
                      json={"body": "что с P-7?", "client_key": "mon-00001"})
    assert r.status_code == 200
    seq = r.json()["seq"]
    monitor = w.env.store.agent_thread("analyst", "monitor")
    [command] = w.env.store.commands(agent="analyst")
    assert command["thread"] == monitor and command["kind"] == "chat"
    assert [m["body"] for m in w.client.get("/api/agents/analyst/monitor/messages").json()["messages"]] == ["что с P-7?"]
    assert w.client.get("/api/agents/analyst/messages").json()["messages"] == []
    assert w.client.get("/api/agents/analyst").json()["monitor"] is True
    w.env.store.add_thread_message(monitor, author="agent:analyst", kind="text", body="смотрю")
    assert w.client.get("/api/chats").json()["analyst/monitor"]["unread"] == 1
    r = w.client.post("/api/agents/analyst/monitor/read", headers=w.headers(), json={"seq": seq + 1})
    assert r.status_code == 200 and r.json()["unread"] == 0
    r = w.client.post("/api/agents/analyst/monitor/presence", headers=w.headers(), json={"device": "tab-00001", "on": True})
    assert r.status_code == 200 and w.env.store.chat_present("owner", monitor, w.env.now)
    r = w.client.post("/api/agents/analyst/monitor/files", headers=w.headers(), data={"text": "лог"},
                      files={"file": ("a.txt", b"x", "text/plain")})
    assert r.status_code == 200 and w.env.store.thread_messages(monitor, seq + 1)[-1]["author"] == "user:owner", r.text
    vera, headers = _as(w, "vera")  # a viewer: no agents.chat
    assert vera.get("/api/agents/analyst/monitor/messages").status_code == 403
    assert vera.get("/api/agents/analyst").json()["monitor"] is False


def test_agents_list_has_slot_counts(w):
    _, _, working = _assigned(w, "One")
    _, _, waiting = _assigned(w, "Two")
    w.env.store.session_update(working["id"], state="working")
    w.env.store.session_update(waiting["id"], state="queued")
    w.login()
    found = {a["name"]: a for a in w.client.get("/api/agents").json()}
    assert found["analyst"]["host"] == {"working": 1, "queued": 1, "slots": 2}
    assert found["dev"]["host"] is None  # not on the session host
    one = w.client.get("/api/agents/analyst").json()
    assert one["host"] == {"working": 1, "queued": 1, "slots": 2}
    assert {s["id"] for s in one["sessions"]} == {working["id"], waiting["id"]}
    anna, _ = _as(w, "anna")  # her projects hold none of these cards: not even their counts (AC22)
    seen = anna.get("/api/agents/analyst").json()
    assert seen["sessions"] == [] and seen["host"] == {"working": 0, "queued": 0, "slots": 2}
    vera, _ = _as(w, "vera")  # she reads the cards: their counts; the list needs agents.chat
    seen = vera.get("/api/agents/analyst").json()
    assert seen["host"]["working"] == 1 and seen["sessions"] == []
