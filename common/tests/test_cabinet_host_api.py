import pytest

from kd import session_config
from kd.cabinet import api, sessions_api, users
from cabinet_env import CabinetEnv

HOSTED = session_config.HostConfig({"analyst": 1}, 600)


def person(user):
    return api.Principal("user", -1, user)


@pytest.fixture
def env(tmp_path):
    e = CabinetEnv(tmp_path)
    u = users.Users(e.store)
    u.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    u.create("anna", "Анна", roles=[("partner", "*")], actor="user:owner")
    u.create("vera", "Вера", roles=[("viewer", "*")], actor="user:owner")
    api.install_host(e.ctx, lambda: HOSTED)
    return e


def _assigned(env):
    """A To Do card of the analyst and its queued command."""
    card = env.card(assignee="@analyst", status="Inbox")
    env.store.edit_card(card.id, None, {"status": "To Do"}, actor="user:owner")
    [command] = env.store.commands(card=card.id)
    return env.store.card(card.id), command


def _deliver(env, command):
    session = env.store.session_for(command["thread"], command["agent"])
    env.store.command_state(command["id"], "delivered", session=session["id"])
    return session


def _report(env, *items):
    return env.call("root", {"op": "host.report", "items": list(items)})


def _last_message(env, card):
    return env.store.messages(card.id)[-1]


def test_host_report_only_root(env):
    for user in ("digi", "digi-cabinet", "digi-analyst"):
        with pytest.raises(api.ApiError) as exc:
            env.call(user, {"op": "host.report", "items": []})
        assert exc.value.kind == "refused"
    with pytest.raises(api.ApiError):
        api.dispatch(env.ctx, person("owner"), {"op": "host.report", "items": []})


def test_activity_blocks_name_the_sessions_actual_agent(env):
    card, command = _assigned(env)
    session = _deliver(env, command)
    _report(env, {"type": "message", "session": session["id"], "command": command["id"], "kind": "status",
                  "body": "", "payload": {"key": "sys.session.memory", "params": {"agent": "@wrong"},
                                           "activity": {"type": "memory", "state": "returned"}}})
    message = _last_message(env, card)
    assert message["payload"]["params"]["agent"] == "@analyst"
    assert message["payload"]["command"] == command["id"]


def test_unknown_item_refuses_the_whole_report(env):
    card, command = _assigned(env)
    session = _deliver(env, command)
    with pytest.raises(api.ApiError) as exc:
        _report(
            env,
            {"type": "session", "session": session["id"], "state": "working"},
            {"type": "reboot"},
        )
    assert exc.value.kind == "invalid" and env.store.session(session["id"])["state"] == "new"


def test_host_started_fails_live_sessions_and_requeues_unstarted(env):
    card, command = _assigned(env)
    session = _deliver(env, command)
    _report(env, {"type": "session", "session": session["id"], "state": "working"})
    other = env.card(assignee="@analyst", status="To Do", title="Other")
    started = env.store.enqueue(
        {"key": "x", "agent": "analyst", "thread": other.thread, "card": other.id, "kind": "signal",
         "text": "[boss] go", "initiator": "root"}
    )
    other_session = _deliver(env, started)
    env.store.command_state(started["id"], "started")
    _report(env, {"type": "session", "session": other_session["id"], "state": "working"})
    _report(env, {"type": "host", "phase": "started"})
    assert env.store.session(session["id"])["state"] == "failed"
    assert env.store.session(session["id"])["error"] == "host_restart"
    assert env.store.command(command["id"])["state"] == "queued"
    assert env.store.command(started["id"])["state"] == "failed"
    assert env.store.command(started["id"])["reason"] == "host_restart"
    line = _last_message(env, card)
    assert line["kind"] == "status" and line["author"] == "system"
    assert line["payload"]["key"] == "sys.session.failed_restart"


@pytest.mark.parametrize("state", ["idle", "queued"])
def test_a_host_restart_exits_idle_and_waiting_sessions_quietly(env, state):
    """A session with no turn under way lost nothing — it exits, with no failure line in the thread."""
    card, command = _assigned(env)
    session = _deliver(env, command)
    _report(env, {"type": "session", "session": session["id"], "state": state})
    seen = len(env.store.thread_messages(card.thread))
    _report(env, {"type": "host", "phase": "started"})
    assert env.store.session(session["id"])["state"] == "exited"
    assert len(env.store.thread_messages(card.thread)) == seen


def test_host_started_fails_inflight_commands_and_requeues_the_rest(env):
    """Stage-1 review: a command the crashed host had written to a process (its in-flight journal) may have run;
    it fails with host_restart instead of running a second time. Others it held are queued again."""
    card, command = _assigned(env)
    _deliver(env, command)
    other = env.card(assignee="@analyst", status="To Do", title="Other")
    [waiting] = env.store.commands(card=other.id) or [env.store.enqueue(
        {"key": "y", "agent": "analyst", "thread": other.thread, "card": other.id, "kind": "signal",
         "text": "[boss] go", "initiator": "root"})]
    _deliver(env, waiting)
    _report(env, {"type": "host", "phase": "started", "inflight": [command["id"]]})
    assert env.store.command(command["id"])["state"] == "failed"
    assert env.store.command(command["id"])["reason"] == "host_restart"
    assert env.store.command(waiting["id"])["state"] == "queued"
    with pytest.raises(api.ApiError) as exc:
        _report(env, {"type": "host", "phase": "started", "inflight": ["not a command id"]})
    assert exc.value.kind == "invalid"


def test_turn_runs_and_messages_land_in_thread_with_session(env):
    card, command = _assigned(env)
    session = _deliver(env, command)
    _report(
        env,
        {"type": "session", "session": session["id"], "state": "working", "slot": "analyst/1",
         "runtime_session_id": "0b9c3f1e-1111-2222-3333-444455556666"},
        {"type": "turn", "session": session["id"], "command": command["id"], "phase": "started"},
        {"type": "message", "session": session["id"], "kind": "tool_call", "body": "Bash: kd-task view KD-1",
         "payload": {"tool": "Bash", "id": "t1"}},
        {"type": "message", "session": session["id"], "kind": "text", "body": "Принял", "payload": {}},
        {"type": "turn", "session": session["id"], "command": command["id"], "phase": "ended", "ok": True},
        {"type": "session", "session": session["id"], "state": "idle"},
    )
    messages = env.store.messages(card.id)
    assert [m["kind"] for m in messages[-2:]] == ["tool_call", "text"]
    assert messages[-1]["author"] == "agent:analyst" and messages[-1]["body"] == "Принял"
    row = env.store.session(session["id"])
    assert row["state"] == "idle" and row["started"]
    assert row["runtime_session_id"].startswith("0b9c3f1e") and row["slot"] == "analyst/1"
    assert env.store.command(command["id"])["state"] == "done"
    kinds = [e["kind"] for e in env.store.events_since(0)]
    assert "run.started" in kinds and "run.finished" in kinds and "run.waiting_user" not in kinds
    # the session's messages are not the agent's own board writes (its write limit)
    assert env.store.count_events("agent:analyst", "2000-01-01T00:00:00Z") == 0


def test_turn_start_before_the_delivery_ack(env):
    card, command = _assigned(env)
    session = env.store.session_for(command["thread"], "analyst")
    _report(env, {"type": "turn", "session": session["id"], "command": command["id"], "phase": "started"})
    assert env.store.command(command["id"])["state"] == "started"
    assert env.store.command(command["id"])["session"] == session["id"]


def test_turn_end_with_open_question_is_waiting_user(env):
    card, command = _assigned(env)
    session = _deliver(env, command)
    _report(env, {"type": "turn", "session": session["id"], "command": command["id"], "phase": "started"})
    env.call("digi", {"op": "decision.open", "type": "question", "card": card.id, "agent": "analyst",
                     "payload": {"text": "Какой сервер?"}})
    _report(env, {"type": "turn", "session": session["id"], "command": command["id"], "phase": "ended", "ok": True})
    last = env.store.events_since(0)[-1]
    assert last["kind"] == "run.waiting_user" and last["card"] == card.id
    assert last["payload"]["agent"] == "analyst"


def test_failed_turn_and_session_line(env):
    card, command = _assigned(env)
    session = _deliver(env, command)
    _report(
        env,
        {"type": "turn", "session": session["id"], "command": command["id"], "phase": "started"},
        {"type": "turn", "session": session["id"], "command": command["id"], "phase": "ended", "ok": False,
         "error": "exit 1"},
        {"type": "session", "session": session["id"], "state": "failed", "error": "exit 1"},
    )
    assert env.store.command(command["id"])["state"] == "failed"
    line = _last_message(env, card)
    assert line["payload"]["key"] == "sys.session.failed" and line["payload"]["params"]["error"] == "exit 1"
    row = env.store.session(session["id"])
    assert row["ended"] and row["error"] == "exit 1"


def test_an_auth_failed_session_line_names_the_remedy(env):
    """the thread says Claude refused the agent's credentials and how to renew the host's token."""
    card, command = _assigned(env)
    session = _deliver(env, command)
    _report(
        env,
        {"type": "turn", "session": session["id"], "command": command["id"], "phase": "started"},
        {"type": "turn", "session": session["id"], "command": command["id"], "phase": "ended", "ok": False,
         "error": "auth_failed"},
        {"type": "session", "session": session["id"], "state": "failed", "error": "auth_failed"},
    )
    line = _last_message(env, card)
    assert line["payload"]["key"] == "sys.session.auth_failed" and line["payload"]["params"]["agent"].startswith("@")
    assert env.store.session(session["id"])["error"] == "auth_failed"


def test_a_command_that_never_started_fails_without_a_run(env):
    card, command = _assigned(env)
    session = _deliver(env, command)
    seq = env.store.last_seq()
    _report(env, {"type": "turn", "session": session["id"], "command": command["id"], "phase": "ended", "ok": False,
                  "error": "session_failed"})
    assert env.store.command(command["id"])["state"] == "failed"
    # no run; the card left with nobody is BOSS's problem
    assert [e["kind"] for e in env.store.events_since(seq)] == ["command.failed", "problem.raised"]


def test_enqueue_agent_only_for_itself(env):
    out = env.call("digi-analyst", {"op": "command.enqueue", "agent": "analyst", "text": "[self] check", "key": "a1"})
    assert out["command"]["agent"] == "analyst" and out["command"]["card"] == ""
    with pytest.raises(api.ApiError) as exc:
        env.call("digi-analyst", {"op": "command.enqueue", "agent": "dev", "text": "x", "key": "a2"})
    assert exc.value.kind == "refused"
    card = env.card(assignee="@analyst")
    by_executor = env.call(
        "digi", {"op": "command.enqueue", "agent": "analyst", "text": "[boss] directive", "card": card.id, "key": "b1"}
    )
    assert by_executor["command"]["thread"] == card.thread and by_executor["command"]["initiator"] == "digi"
    for bad in ({"text": "two\nlines"}, {"text": "x" * 2001}, {"key": "bad key"}, {"agent": "nobody"}):
        request = {"op": "command.enqueue", "agent": "analyst", "text": "ok", "key": "c1", **bad}
        with pytest.raises(api.ApiError):
            env.call("root", request)


def test_an_agent_reads_no_other_agents_chat(env):
    """The owner's chat with one agent is not another agent's to read (Task 4b: the agent is the caller's own)."""
    with pytest.raises(api.ApiError) as exc:
        env.call("digi-dev", {"op": "thread.messages", "agent": "analyst"})
    assert exc.value.kind == "refused"
    assert env.call("digi-analyst", {"op": "thread.messages", "agent": "analyst"})["messages"] == []
    assert "messages" in env.call("root", {"op": "thread.messages", "agent": "analyst"})


def test_command_and_session_lists_by_card_and_agent(env):
    card, command = _assigned(env)
    session = _deliver(env, command)
    out = api.dispatch(env.ctx, person("owner"), {"op": "command.list", "card": card.id})
    assert [c["id"] for c in out["commands"]] == [command["id"]]
    out = api.dispatch(env.ctx, person("owner"), {"op": "session.list", "card": card.id})
    assert [s["id"] for s in out["sessions"]] == [session["id"]]
    by_agent = api.dispatch(env.ctx, person("anna"), {"op": "session.list", "agent": "analyst"})
    assert [s["id"] for s in by_agent["sessions"]] == [session["id"]]
    with pytest.raises(api.ApiError) as exc:
        api.dispatch(env.ctx, person("vera"), {"op": "session.list", "agent": "analyst"})
    assert exc.value.kind == "refused"


def test_continue_only_failed_stopped_exited(env):
    card, command = _assigned(env)
    session = _deliver(env, command)
    env.store.session_update(session["id"], state="working")
    request = {"op": "user.session.continue", "session": session["id"]}
    with pytest.raises(api.ApiError) as exc:
        api.dispatch(env.ctx, person("owner"), request)
    assert exc.value.kind == "refused"
    env.store.session_update(session["id"], state="failed", error="exit 1")
    with pytest.raises(api.ApiError):
        api.dispatch(env.ctx, person("anna"), request)  # a partner has no agents.control
    first = api.dispatch(env.ctx, person("owner"), request)["command"]
    again = api.dispatch(env.ctx, person("owner"), request)["command"]
    assert first["id"] == again["id"] and first["kind"] == "continue"
    assert card.id in first["text"] and "continue" in first["text"]


def test_continue_a_live_session_whose_last_turn_failed(env):
    """The card's «retry»: the turn failed but the session lives on — continue asks it again."""
    card, command = _assigned(env)
    session = _deliver(env, command)
    env.store.session_update(session["id"], state="idle")
    request = {"op": "user.session.continue", "session": session["id"]}
    with pytest.raises(api.ApiError):
        api.dispatch(env.ctx, person("owner"), request)  # its turn did not fail
    env.store.command_state(command["id"], "failed", session=session["id"], error="turn failed")
    out = api.dispatch(env.ctx, person("owner"), request)["command"]
    assert out["kind"] == "continue" and "failed" in out["text"]


def test_stop_enqueues_a_stop(env):
    card, command = _assigned(env)
    session = _deliver(env, command)
    env.store.session_update(session["id"], state="working")
    out = api.dispatch(env.ctx, person("owner"), {"op": "user.session.stop", "session": session["id"]})
    assert out["command"]["kind"] == "stop"
    env.store.session_update(session["id"], state="exited")
    with pytest.raises(api.ApiError):
        api.dispatch(env.ctx, person("owner"), {"op": "user.session.stop", "session": session["id"]})


def test_agent_message_needs_agents_chat_and_carries_no_text_in_command(env):
    out = api.dispatch(
        env.ctx, person("anna"), {"op": "user.agent.message", "agent": "analyst", "text": "секрет: привет"}
    )
    assert out["message"]["author"] == "user:anna" and out["message"]["body"] == "секрет: привет"
    [command] = env.store.commands(agent="analyst")
    assert command["kind"] == "chat" and "секрет" not in command["text"] and command["card"] == ""
    with pytest.raises(api.ApiError) as exc:
        api.dispatch(env.ctx, person("vera"), {"op": "user.agent.message", "agent": "analyst", "text": "x"})
    assert exc.value.kind == "refused"
    with pytest.raises(api.ApiError) as exc:
        api.dispatch(env.ctx, person("owner"), {"op": "user.agent.message", "agent": "dev", "text": "x"})
    assert exc.value.kind == "refused"  # dev is not on the session host
    chat = api.dispatch(env.ctx, person("owner"), {"op": "thread.messages", "agent": "analyst"})
    assert [m["body"] for m in chat["messages"]] == ["секрет: привет"] and chat["watermark"] > 0


def test_chat_events_visible_only_with_agents_chat(env):
    api.dispatch(env.ctx, person("owner"), {"op": "user.agent.message", "agent": "analyst", "text": "hi"})
    events = env.store.events_since(0)
    for_anna = api.event_filter(env.ctx, person("anna"))(events)
    for_vera = api.event_filter(env.ctx, person("vera"))(events)
    assert any(e["kind"] == "message.added" and e["card"] == "" for e in for_anna)
    assert any(e["kind"] == "command.queued" for e in for_anna)
    assert not any(e["card"] == "" and e["kind"] in ("message.added", "command.queued") for e in for_vera)


def test_a_report_item_that_raises_is_refused_as_invalid_and_the_rest_apply(env, monkeypatch, capsys):
    card, command = _assigned(env)
    session = _deliver(env, command)

    def broken(ctx, item):
        raise KeyError("slot")

    monkeypatch.setitem(sessions_api._ITEMS, "message", broken)
    working = {"type": "session", "session": session["id"], "state": "working"}
    bad = {"type": "message", "session": session["id"], "kind": "text", "body": "x"}
    started = {"type": "turn", "session": session["id"], "command": command["id"], "phase": "started"}
    with pytest.raises(api.ApiError) as exc:
        _report(env, working, bad, started)
    # not "unavailable": the host's reporter would send the same batch for ever
    assert exc.value.kind == "invalid" and "item 1" in str(exc.value)
    assert "KeyError" in capsys.readouterr().err
    assert env.store.session(session["id"])["state"] == "new"
    # the host sends a refused batch item by item: the bad one is refused for good, the others apply
    _report(env, working)
    with pytest.raises(api.ApiError) as exc:
        _report(env, bad)
    assert exc.value.kind == "invalid" and "item 0" in str(exc.value)
    _report(env, started)
    assert env.store.session(session["id"])["state"] == "working"
    assert env.store.command(command["id"])["state"] == "started"


# --- the host reports the model and effort a session runs on ------------------------------------------
def test_a_session_report_carries_its_model_and_effort(env):
    card, command = _assigned(env)
    session = _deliver(env, command)
    _report(env, {"type": "session", "session": session["id"], "state": "starting", "slot": "analyst/1",
                  "model": "or-x", "effort": "high"})
    row = env.store.session(session["id"])
    assert row["model"] == "or-x" and row["effort"] == "high"
    _report(env, {"type": "session", "session": session["id"], "state": "idle"})
    row = env.store.session(session["id"])
    assert row["model"] == "or-x" and row["effort"] == "high"  # a report without the fields changes nothing
    _report(env, {"type": "session", "session": session["id"], "state": "starting", "model": "", "effort": ""})
    row = env.store.session(session["id"])
    assert row["model"] == "" and row["effort"] == ""  # back on the subscription


def test_a_process_start_on_a_model_or_effort_is_a_line_in_the_thread(env):
    """Final review (Important 2): the owner reads in the thread what a turn runs on (spec §3.4) — once per
    change of the pair, never for the plain subscription."""
    card, command = _assigned(env)
    session = _deliver(env, command)
    before = len(env.store.thread_messages(card.thread))
    _report(env, {"type": "session", "session": session["id"], "state": "starting", "slot": "analyst/1",
                  "model": "", "effort": ""})
    assert len(env.store.thread_messages(card.thread)) == before  # the subscription, the agent's effort: nothing
    _report(env, {"type": "session", "session": session["id"], "state": "starting", "model": "or-x", "effort": "high"})
    line = env.store.thread_messages(card.thread)[-1]
    assert line["kind"] == "status" and line["payload"]["key"] == "sys.session.model"
    assert line["payload"]["params"] == {"model": "or-x", "effort": "high"}
    _report(env, {"type": "session", "session": session["id"], "state": "idle"})
    _report(env, {"type": "session", "session": session["id"], "state": "starting", "model": "or-x", "effort": "high"})
    assert len(env.store.thread_messages(card.thread)) == before + 1  # the same pair again: no second line
    _report(env, {"type": "session", "session": session["id"], "state": "starting", "model": "", "effort": "high"})
    line = env.store.thread_messages(card.thread)[-1]
    assert line["payload"]["params"] == {"model": "", "effort": "high"}  # the subscription with an effort is said


@pytest.mark.parametrize("bad", [{"model": "Bad/Name"}, {"model": 3}, {"effort": "ultra"}, {"effort": None}])
def test_a_bad_model_or_effort_in_a_session_report_is_invalid(env, bad):
    card, command = _assigned(env)
    session = _deliver(env, command)
    with pytest.raises(api.ApiError) as exc:
        _report(env, {"type": "session", "session": session["id"], "state": "starting", **bad})
    assert exc.value.kind == "invalid"
    assert env.store.session(session["id"])["state"] != "starting"


# --- a session waiting for a slot reports its place in the agent's queue ------------------------------
def test_a_waiting_session_keeps_its_place_and_since_when_it_waits(env):
    card, command = _assigned(env)
    session = _deliver(env, command)
    seq = env.store.last_seq()
    _report(env, {"type": "session", "session": session["id"], "state": "queued", "position": 2})
    row = env.store.session(session["id"])
    since = row["queued_since"]
    assert row["state"] == "queued" and row["queue_position"] == 2 and since
    env.now = "2026-12-31T23:59:59Z"
    _report(env, {"type": "session", "session": session["id"], "state": "queued", "position": 1})
    row = env.store.session(session["id"])
    assert row["queue_position"] == 1 and row["queued_since"] == since  # moved up, waits since the same moment
    # every move is an event: the cabinet's views refresh on it
    events = [e for e in env.store.events_since(seq) if e["kind"] == "session.changed"]
    assert [e["payload"].get("position") for e in events] == [2, 1]
    _report(env, {"type": "session", "session": session["id"], "state": "starting", "slot": "analyst/1"})
    row = env.store.session(session["id"])
    assert row["queue_position"] == 0 and row["queued_since"] == ""
    # a host restart ends a waiting session: it keeps no place for the next delivery to show
    _report(env, {"type": "session", "session": session["id"], "state": "queued", "position": 3})
    _report(env, {"type": "host", "phase": "started", "inflight": []})
    row = env.store.session(session["id"])
    assert row["state"] == "exited" and row["queue_position"] == 0 and row["queued_since"] == ""


@pytest.mark.parametrize("bad", [0, -1, "1", True, 10001])
def test_a_bad_queue_position_is_invalid(env, bad):
    card, command = _assigned(env)
    session = _deliver(env, command)
    with pytest.raises(api.ApiError) as exc:
        _report(env, {"type": "session", "session": session["id"], "state": "queued", "position": bad})
    assert exc.value.kind == "invalid"


def test_a_session_report_names_a_subscription_model(env):
    card, command = _assigned(env)
    session = _deliver(env, command)
    _report(env, {"type": "session", "session": session["id"], "state": "starting", "slot": "analyst/1",
                  "model": "claude:sonnet", "effort": ""})
    assert env.store.session(session["id"])["model"] == "claude:sonnet"
    line = env.store.thread_messages(card.thread)[-1]
    assert line["payload"]["key"] == "sys.session.model" and line["payload"]["params"]["model"] == "claude:sonnet"
    for bad in ("claude:", "claude:x y", "x:opus"):
        with pytest.raises(api.ApiError) as exc:
            _report(env, {"type": "session", "session": session["id"], "state": "starting", "model": bad})
        assert exc.value.kind == "invalid"
