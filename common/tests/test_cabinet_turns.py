"""a session's turns and their steps; a tool's masked output kept apart from the thread, shown only to
whoever controls the agent."""

import json

import pytest

from kd import session_config
from kd.cabinet import api, users
from cabinet_env import CabinetEnv

HOSTED = session_config.HostConfig({"analyst": 1}, 600)
SECRET_LOOKING = "строка вывода команды"
MODEL = {"in": 10, "out": 5, "cache_read": 0, "cache_write": 0}


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


def _assigned(env, title="Card"):
    card = env.card(assignee="@analyst", status="Inbox", title=title)
    env.store.edit_card(card.id, None, {"status": "To Do"}, actor="user:owner")
    [command] = env.store.commands(card=card.id)
    return env.store.card(card.id), command


def _deliver(env, command):
    session = env.store.session_for(command["thread"], command["agent"])
    env.store.command_state(command["id"], "delivered", session=session["id"])
    return session


def _report(env, *items):
    return env.call("root", {"op": "host.report", "items": list(items)})


def _turn(env, session, command, cost=None):
    _report(env, {"type": "turn", "session": session["id"], "command": command["id"], "phase": "started"})
    yield
    end = {"type": "turn", "session": session["id"], "command": command["id"], "phase": "ended", "ok": True}
    if cost is not None:
        end["usage"] = {"models": {"claude-sonnet-5-5": {**MODEL, "cost": cost}}, "requests": 1}
    _report(env, end)


def _call(env, user, request):
    return api.dispatch(env.ctx, person(user), request)


def _step(session, command, kind, body="", payload=None, output=None):
    item = {"type": "message", "session": session["id"], "kind": kind, "body": body, "payload": payload or {}}
    if command is not None:
        item["command"] = command["id"]
    if output is not None:
        item["output"] = output
    return item


def _result(session, command, n, text=SECRET_LOOKING):
    return _step(session, command, "tool_result", payload={"id": f"toolu_{n}", "ok": True, "size": 900},
                 output={"text": text, "size": 900, "truncated": False})


def test_tool_output_is_stored_apart_from_the_thread(env):
    card, command = _assigned(env)
    session = _deliver(env, command)
    turn = _turn(env, session, command)
    next(turn)
    _report(env, _step(session, command, "tool_call", "Bash: ls", {"tool": "Bash", "id": "toolu_1"}),
            _result(session, command, 1))
    message = env.store.messages(card.id)[-1]
    assert message["kind"] == "tool_result"
    # the session names where the thread reads the output from
    assert message["payload"] == {"id": "toolu_1", "ok": True, "size": 900, "command": command["id"], "output": True,
                                  "session": session["id"]}
    assert env.store.tool_output(card.thread, message["seq"]) == {
        "text": SECRET_LOOKING, "size": 900, "truncated": False}
    assert env.call("root", {"op": "session.output", "session": session["id"], "seq": message["seq"]})[
        "output"] == {"text": SECRET_LOOKING, "size": 900, "truncated": False}
    for bad in ({"text": "x" * 4001, "size": 1, "truncated": True}, {"text": "x", "size": -1, "truncated": False},
                {"text": "x", "size": 1, "truncated": "no"}, "text"):
        with pytest.raises(api.ApiError) as exc:
            _report(env, _step(session, command, "tool_result", payload={"id": "t", "ok": True, "size": 1},
                               output=bad))
        assert exc.value.kind == "invalid"
    with pytest.raises(api.ApiError) as exc:  # only a tool result has output
        _report(env, _step(session, command, "text", "ok", output={"text": "x", "size": 1, "truncated": False}))
    assert exc.value.kind == "invalid"


def test_thread_messages_carry_no_output(env):
    card, command = _assigned(env)
    session = _deliver(env, command)
    next(_turn(env, session, command))
    _report(env, _result(session, command, 1))
    for user in ("owner", "anna", "vera"):
        thread = _call(env, user, {"op": "thread.messages", "card": card.id})
        assert SECRET_LOOKING not in json.dumps(thread, ensure_ascii=False)
    assert SECRET_LOOKING not in json.dumps(env.store.events_since(0), ensure_ascii=False)


def test_output_needs_agents_control(env):
    card, command = _assigned(env)
    session = _deliver(env, command)
    next(_turn(env, session, command))
    _report(env, _result(session, command, 1))
    seq = env.store.messages(card.id)[-1]["seq"]
    for user in ("anna", "vera"):
        for request in ({"op": "session.turns", "session": session["id"]},
                        {"op": "session.output", "session": session["id"], "seq": seq}):
            with pytest.raises(api.ApiError) as exc:
                _call(env, user, request)
            assert exc.value.kind == "refused"
    found = _call(env, "owner", {"op": "session.output", "session": session["id"], "seq": seq})
    assert found["output"]["text"] == SECRET_LOOKING
    with pytest.raises(api.ApiError) as exc:
        _call(env, "owner", {"op": "session.output", "session": session["id"], "seq": seq + 1})
    assert exc.value.kind == "not_found"
    with pytest.raises(api.ApiError) as exc:
        _call(env, "owner", {"op": "session.output", "session": session["id"], "seq": 0})
    assert exc.value.kind == "invalid"
    for user in ("digi", "digi-analyst"):  # the agents and executors never read it
        with pytest.raises(api.ApiError) as exc:
            env.call(user, {"op": "session.turns", "session": session["id"]})
        assert exc.value.kind == "refused"


def test_turns_group_steps_by_command_with_cost(env):
    card, first = _assigned(env)
    session = _deliver(env, first)
    turn = _turn(env, session, first, cost=0.25)
    next(turn)
    _report(env, _step(session, first, "text", "смотрю карточку"),
            _step(session, first, "tool_call", "Bash: ls", {"tool": "Bash", "id": "toolu_1"}),
            _result(session, first, 1))
    next(turn, None)
    second = env.store.enqueue({"key": "again", "agent": "analyst", "thread": card.thread, "card": card.id,
                                "kind": "message", "text": "[board] KD-1: a message for you", "initiator": "user:owner"})
    env.store.command_state(second["id"], "delivered", session=session["id"])
    turn = _turn(env, session, second, cost=0.75)  # Claude's total so far: this turn is 0.5 of it
    next(turn)
    _report(env, _step(session, second, "text", "x" * 900))
    next(turn, None)
    turns = _call(env, "owner", {"op": "session.turns", "session": session["id"]})["turns"]
    assert [t["command"] for t in turns] == [second["id"], first["id"]]
    older = turns[1]
    assert (older["kind"], older["state"], older["initiator"]) == ("assigned", "done", first["initiator"])
    assert older["cost"] == pytest.approx(0.25) and turns[0]["cost"] == pytest.approx(0.5)
    assert [s["kind"] for s in older["steps"]] == ["text", "tool_call", "tool_result"]
    assert older["steps"][1] == {"seq": older["steps"][1]["seq"], "kind": "tool_call", "body": "Bash: ls",
                                 "tool": "Bash", "id": "toolu_1", "ok": None, "size": None, "output": False,
                                 "status": None}
    assert older["steps"][0]["id"] == "" and older["steps"][2]["id"] == "toolu_1"
    assert older["steps"][2]["ok"] is True and older["steps"][2]["output"] is True
    assert older["steps"][2]["tool"] == "Bash"  # the call it answers
    assert older["steps"][2]["size"] == 900 and SECRET_LOOKING not in json.dumps(turns, ensure_ascii=False)
    assert len(turns[0]["steps"][0]["body"]) == 500 and len(turns[0]["text"]) <= 300
    assert _call(env, "owner", {"op": "session.turns", "session": session["id"], "limit": 1})["turns"][0][
        "command"] == second["id"]
    for bad in (0, 21, "5"):
        with pytest.raises(api.ApiError) as exc:
            _call(env, "owner", {"op": "session.turns", "session": session["id"], "limit": bad})
        assert exc.value.kind == "invalid"


def test_turns_of_an_old_session_have_no_steps(env):
    """Messages written before the change carry no command: the turn is listed, its steps are not guessed."""
    card, command = _assigned(env)
    session = _deliver(env, command)
    turn = _turn(env, session, command)
    next(turn)
    _report(env, _step(session, None, "text", "старый ход"))
    next(turn, None)
    [only] = _call(env, "owner", {"op": "session.turns", "session": session["id"]})["turns"]
    assert only["command"] == command["id"] and only["steps"] == [] and only["cost"] == 0


def test_a_command_of_another_session_is_invalid(env):
    card, command = _assigned(env)
    session = _deliver(env, command)
    other_card, other = _assigned(env, title="Other")
    _deliver(env, other)
    for value in (other["id"], "f" * 32, "not hex"):
        with pytest.raises(api.ApiError) as exc:
            _report(env, _step(session, {"id": value}, "text", "ok"))
        assert exc.value.kind in ("invalid", "not_found")
    assert all(m["kind"] != "text" for m in env.store.messages(card.id))


def test_turn_messages_use_the_command_index(env):
    # "Turn" asks one turn at a time (up to 20 a page) under the service's single writer; without an
    # index each is a scan of the whole thread with a JSON parse per message.
    from kd.cabinet import store
    plan = env.store._conn.execute("EXPLAIN QUERY PLAN " + store.TURN_MESSAGES, (1, "s", "c", 5)).fetchall()
    assert any("messages_command" in " ".join(str(v) for v in tuple(row)) for row in plan), plan


def test_a_turns_cost_and_models_use_the_ledgers_command_index(env):
    # "Turn" asks the ledger twice per turn; the index on `command` is partial (`command != ''`)
    # and serves a statement only when the statement says so — otherwise each is a scan of the whole ledger.
    from kd.cabinet import store
    for statement in (store.TURN_COST, store.TURN_MODELS):
        plan = env.store._conn.execute("EXPLAIN QUERY PLAN " + statement, ("c",)).fetchall()
        assert any("usage_log_command" in " ".join(str(v) for v in tuple(row)) for row in plan), (statement, plan)


def test_tool_outputs_older_than_the_keep_go_at_the_next_write(env):
    # Final review: excerpts were kept for ever; a write drops those older than OUTPUT_KEEP_DAYS.
    from kd.cabinet import store
    excerpt = {"text": "a", "size": 1, "truncated": False}
    env.store._clock = lambda: "2026-09-01T00:00:00Z"
    env.store.tool_output_put(1, 1, session="s", command="c", output=excerpt)
    env.store._clock = lambda: "2026-09-30T00:00:00Z"
    env.store.tool_output_put(1, 2, session="s", command="c", output=excerpt)
    assert env.store.tool_output(1, 1) is not None  # 29 days: kept
    env.store._clock = lambda: "2026-10-18T00:00:00Z"
    env.store.tool_output_put(1, 3, session="s", command="c", output=excerpt)
    assert store.OUTPUT_KEEP_DAYS == 30
    assert env.store.tool_output(1, 1) is None and env.store.tool_output(1, 2) is not None


# --- a turn names the models that served it, as the usage ledger holds them -------------------------------
def test_a_turn_names_the_models_that_served_it(env):
    card, first = _assigned(env)
    session = _deliver(env, first)
    turn = {"type": "turn", "session": session["id"], "command": first["id"]}
    used = {"claude-haiku-4-5": {**MODEL, "cost": 0.01}, "claude-opus-5-5": {**MODEL, "cost": 0.4}}
    _report(env, dict(turn, phase="started"),
            dict(turn, phase="ended", ok=True, usage={"models": used, "requests": 2}))
    second = env.store.enqueue({"key": "again", "agent": "analyst", "thread": card.thread, "card": card.id,
                                "kind": "message", "text": "[board] KD-1: a message for you", "initiator": "user:owner"})
    env.store.command_state(second["id"], "delivered", session=session["id"])
    for _ in _turn(env, session, second):  # a turn that reported no usage
        pass
    turns = _call(env, "owner", {"op": "session.turns", "session": session["id"]})["turns"]
    assert [t["command"] for t in turns] == [second["id"], first["id"]]
    assert turns[1]["served"] == ["claude-opus-5-5", "claude-haiku-4-5"]  # the costliest first
    assert turns[0]["served"] == []
