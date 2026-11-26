"""A delivery names its role's session context limit; the host starts the process with Claude Code's
auto-compact window set to it, fresh or resumed, and restarts an idle process when the limit changes."""

import json

import pytest

from kd import context_limit, session_config
from kd.session_host import claude, core, protocol

A = "a" * 32
SID = "0b9c3f1e-1111-2222-3333-444455556666"


def _line(**over):
    fields = {
        "op": "deliver",
        "command": "1" * 32,
        "session": A,
        "agent": "dev",
        "thread": 9,
        "card": "KD-7",
        "text": "[board] KD-7 assigned to you",
        "resume": "",
    }
    fields.update(over)
    return json.dumps(fields).encode() + b"\n"


def _req(**over):
    return json.loads(_line(**over))


def _host():
    return core.Host(
        session_config.HostConfig({"dev": 1}, 600), {"dev": "digi-dev"}, lambda: 1000.0
    )


def kinds(actions):
    return [type(a).__name__ for a in actions]


def _spawn(actions):
    [spawn] = [a for a in actions if isinstance(a, core.Spawn)]
    return spawn


# --- the protocol -------------------------------------------------------------------------------------------
def test_the_protocol_takes_a_limit_in_range_on_any_session():
    assert protocol.parse(_line(context_limit=400000))["context_limit"] == 400000
    assert (
        protocol.parse(_line(context_limit=100000, card=""))["context_limit"] == 100000
    )  # a chat too
    assert (
        protocol.parse(_line(context_limit=1000000, card="", space="monitor"))[
            "context_limit"
        ]
        == 1000000
    )
    assert "context_limit" not in protocol.parse(
        _line()
    )  # none: Claude Code's own window


@pytest.mark.parametrize(
    "bad", [99999, 1000001, 0, "400000", 400000.0, True, None, "none"], ids=repr
)
def test_the_protocol_refuses_a_limit_out_of_range(bad):
    with pytest.raises(protocol.ProtocolError) as exc:
        protocol.parse(_line(context_limit=bad))
    assert exc.value.kind == "invalid" and "context_limit" in str(exc.value)


def test_a_jobs_definition_takes_a_limit_in_range_only():
    definition = {"id": "1" * 32, "agent": "dev", "timeout": 600}
    line = {"op": "job", "job": "1" * 32, "agent": "dev"}
    ok = protocol.parse(
        json.dumps(
            {**line, "definition": {**definition, "context_limit": 300000}}
        ).encode()
    )
    assert ok["definition"]["context_limit"] == 300000
    with pytest.raises(protocol.ProtocolError):
        protocol.parse(
            json.dumps(
                {**line, "definition": {**definition, "context_limit": 5}}
            ).encode()
        )


# --- the spawn ----------------------------------------------------------------------------------------------
def test_the_process_spawns_with_the_deliverys_limit_fresh_and_resumed():
    spawn = _spawn(_host().deliver(_req(context_limit=250000))[1])
    assert (spawn.resume, spawn.context_limit) == ("", 250000)
    spawn = _spawn(_host().deliver(_req(resume=SID, context_limit=250000))[1])
    assert (spawn.resume, spawn.context_limit) == (
        SID,
        250000,
    )  # a resume takes the limit in force too
    assert _spawn(_host().deliver(_req())[1]).context_limit == 0


def test_an_idle_process_restarts_when_its_roles_limit_changes():
    host = _host()
    host.deliver(_req(context_limit=400000))
    host.output(A)
    host.runtime(A, SID)
    host.turn_ended(A, True)
    assert host._sessions[A].proc == "idle"
    ack, actions = host.deliver(_req(command="2" * 32, context_limit=200000))
    assert "Close" in kinds(actions) and "Write" not in kinds(actions)
    spawn = _spawn(host.exited(A, 0))
    assert (spawn.context_limit, spawn.resume) == (
        200000,
        SID,
    )  # the same conversation, the new window


def test_a_working_process_finishes_its_turn_before_the_new_limit():
    host = _host()
    host.deliver(_req(context_limit=400000))
    host.output(A)
    ack, actions = host.deliver(
        _req(command="2" * 32)
    )  # the owner chose no limit meanwhile
    assert "Close" not in kinds(actions) and ack["state"] == "running"
    assert "Close" in kinds(host.turn_ended(A, True))
    assert _spawn(host.exited(A, 0)).context_limit == 0


def test_the_same_limit_keeps_the_process():
    host = _host()
    host.deliver(_req(context_limit=400000))
    host.output(A)
    host.turn_ended(A, True)
    ack, actions = host.deliver(_req(command="2" * 32, context_limit=400000))
    assert (
        ack == {"state": "running"}
        and "Close" not in kinds(actions)
        and "Write" in kinds(actions)
    )


# --- the launch's environment -------------------------------------------------------------------------------
def test_the_limit_reaches_claude_as_its_auto_compact_window():
    assert context_limit.environment(400000) == {
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "400000"
    }
    assert context_limit.environment(0) == {}
    with pytest.raises(ValueError):
        context_limit.environment(5)
    argv = claude.argv(
        "dev",
        "digi-dev",
        "digipaddock-session-ab12",
        SID,
        claude.context_args(A, 9, "KD-7"),
        setenv=context_limit.environment(400000),
    )
    assert "--setenv=CLAUDE_CODE_AUTO_COMPACT_WINDOW=400000" in argv
    assert argv.index("--setenv=CLAUDE_CODE_AUTO_COMPACT_WINDOW=400000") < argv.index(
        claude.LAUNCH
    )
    assert argv[-1] == SID  # resumed: the same variable, before agent-launch
