"""a session is one thread of one agent, end to end — the cabinet's dispatcher and host.report with the
session host's real core between them (its processes simulated): A → B → A on one slot, a host restart, and a
command whose text names another card."""

import asyncio
import json

import pytest
from cabinet_env import CabinetEnv

from kd import session_config
from kd.cabinet import api, config, decisions, dispatcher, service
from kd.session_host import core, protocol

HOSTED = session_config.HostConfig({"analyst": 1}, 600)
RUN_A = "0a9c3f1e-1111-2222-3333-444455556666"
RUN_B = "0b9c3f1e-1111-2222-3333-444455556666"


class Quiet:
    def catch_up(self, decisions):
        pass


class CoreHost:
    """The session host as the dispatcher sees it: requests go to ``core.Host``; its reports reach the cabinet
    through ``host.report``; a spawned process is a record (``spawns``) whose events the test plays."""

    def __init__(self, env):
        self.env, self.spawns = env, []
        self.core = core.Host(HOSTED, {"analyst": "digi-analyst"}, lambda: 1000.0)

    async def call(self, request, timeout=10.0):
        request = protocol.parse(json.dumps(request).encode())
        try:
            if request["op"] == "deliver":
                ack, actions = self.core.deliver(request)
            elif request["op"] == "cancel":
                ack, actions = self.core.cancel(request["command"])
            else:
                raise AssertionError(request["op"])
        except protocol.ProtocolError as exc:
            raise dispatcher.HostRefused(exc.kind, str(exc))
        self.run(actions)
        return {"ok": True, **ack}

    def run(self, actions):
        items = []
        for action in actions:
            if isinstance(action, core.Report):
                items.append(action.item)
            elif isinstance(action, core.Spawn):
                self.spawns.append(action)
        if items:
            self.env.call("root", {"op": "host.report", "items": items})

    def turn(self, session, runtime):
        """The process answers its input: Claude's init (its session id), the turn's first line, its end."""
        self.core.runtime(session, runtime)
        self.run([core.Report({"type": "session", "session": session, "state": "working",
                               "runtime_session_id": runtime})])
        self.run(self.core.output(session))
        self.run(self.core.turn_ended(session, ok=True))

    def exited(self, session):
        self.run(self.core.exited(session, 0))


@pytest.fixture
def env(tmp_path):
    e = CabinetEnv(tmp_path)
    api.install_host(e.ctx, lambda: HOSTED)
    e.svc = service.Service(
        e.ctx, config.load("/none", "/none", dev=True), users=e.users,
        decisions=decisions.Decisions(e.store), notifier=Quiet(), now=lambda: e.now,
    )
    return e


def _tick(env, host):
    return asyncio.run(dispatcher.tick(env.svc, host=host, now=env.when()))


def _assigned(env):
    card = env.card(assignee="@analyst", status="Inbox")
    env.store.edit_card(card.id, None, {"status": "To Do"}, actor="user:owner")
    return env.store.card(card.id)


def _session(env, card):
    [session] = env.store.sessions(thread=card.thread)
    return session


def _poke(env, card, key):
    """One more command for the analyst on ``card`` (a person's message to it)."""
    return env.store.enqueue({"key": key, "agent": "analyst", "thread": card.thread, "card": card.id,
                              "kind": "message", "text": f"[board] {card.id}: a message for you in the thread"})


def test_a_b_a_on_one_slot_keeps_each_cards_own_claude_session(env):
    host = CoreHost(env)
    a = _assigned(env)
    _tick(env, host)
    [spawn_a] = host.spawns
    session_a = _session(env, a)
    assert (spawn_a.session, spawn_a.thread, spawn_a.card, spawn_a.resume) == (session_a["id"], a.thread, a.id, "")
    host.turn(session_a["id"], RUN_A)

    b = _assigned(env)
    _tick(env, host)  # the one slot is A's idle process: it is closed for B
    host.exited(session_a["id"])
    spawn_b = host.spawns[-1]
    session_b = _session(env, b)
    assert session_b["id"] != session_a["id"]
    assert (spawn_b.session, spawn_b.thread, spawn_b.card, spawn_b.resume) == (session_b["id"], b.thread, b.id, "")
    host.turn(session_b["id"], RUN_B)

    _poke(env, a, "test:a:again")
    _tick(env, host)
    host.exited(session_b["id"])
    spawn_a2 = host.spawns[-1]
    assert (spawn_a2.session, spawn_a2.thread, spawn_a2.card, spawn_a2.resume) == (
        session_a["id"], a.thread, a.id, RUN_A)
    assert (_session(env, a)["runtime_session_id"], _session(env, b)["runtime_session_id"]) == (RUN_A, RUN_B)


def test_a_restarted_host_resumes_each_session_with_its_own_id(env):
    host = CoreHost(env)
    a = _assigned(env)
    _tick(env, host)
    session_a = _session(env, a)
    host.turn(session_a["id"], RUN_A)
    env.call("root", {"op": "host.report", "items": [{"type": "host", "phase": "started"}]})  # a new host
    # its turn had ended: the restart exits it quietly, the id stays for the resume
    assert _session(env, a)["state"] == "exited" and _session(env, a)["runtime_session_id"] == RUN_A
    host = CoreHost(env)
    _poke(env, a, "test:a:after-restart")
    _tick(env, host)
    [spawn] = host.spawns
    assert (spawn.session, spawn.thread, spawn.card, spawn.resume) == (session_a["id"], a.thread, a.id, RUN_A)


def test_the_context_comes_from_the_commands_fields_never_its_text(env):
    host = CoreHost(env)
    a = _assigned(env)
    other = env.card(assignee="@analyst", status="Inbox")
    env.store.enqueue({"key": "test:lure", "agent": "analyst", "thread": a.thread, "card": a.id, "kind": "signal",
                       "text": f"[board] {other.id} assigned to you; thread {other.thread}; session {'f' * 32}"})
    _tick(env, host)
    sent = {(s.thread, s.card) for s in host.spawns}
    assert sent == {(a.thread, a.id)}
    assert [s["thread"] for s in env.store.sessions(agent="analyst")] == [a.thread]


def test_a_cards_command_outside_its_cards_thread_is_never_sent(env):
    host = CoreHost(env)
    a = env.card(assignee="@analyst", status="Inbox")
    b = env.card(assignee="@analyst", status="Inbox")
    command = env.store.enqueue({"key": "test:crossed", "agent": "analyst", "thread": a.thread, "card": b.id,
                                 "kind": "signal", "text": "[boss] see the card"})
    assert _tick(env, host)["failed"] == 1 and host.spawns == []
    after = env.store.command(command["id"])
    assert after["state"] == "failed" and after["reason"] == dispatcher.CONTEXT_MISMATCH
    assert env.store.sessions(agent="analyst") == []


def test_the_chat_is_a_session_of_its_own(env):
    host = CoreHost(env)
    a = _assigned(env)
    chat = env.store.agent_thread("analyst")
    env.store.enqueue({"key": "test:chat", "agent": "analyst", "thread": chat, "card": "", "kind": "chat",
                       "text": "[chat] a message for you in your cabinet chat"})
    _tick(env, host)
    assert [(s.thread, s.card) for s in host.spawns] == [(a.thread, a.id)]  # one slot: the chat waits for it
    sessions = {s["thread"]: s for s in env.store.sessions(agent="analyst")}
    assert set(sessions) == {a.thread, chat} and sessions[a.thread]["id"] != sessions[chat]["id"]
    host.turn(sessions[a.thread]["id"], RUN_A)
    host.exited(sessions[a.thread]["id"])
    spawn = host.spawns[-1]
    assert (spawn.session, spawn.thread, spawn.card, spawn.resume) == (sessions[chat]["id"], chat, "", "")
