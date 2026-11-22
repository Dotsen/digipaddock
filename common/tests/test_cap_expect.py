import os

import pytest

from kd import cap_expect

posix = pytest.mark.skipif(
    os.name != "posix", reason="rename semantics and modes need POSIX"
)


@posix
def test_record_then_consume_once(tmp_path):
    cap_expect.record(
        "sysadmin",
        "s1",
        "toolu_1",
        [["ssh-ro", "node-c", "uptime"]],
        root=str(tmp_path),
        now=100.0,
    )
    assert cap_expect.consume(
        "sysadmin", ["ssh-ro", "node-c", "uptime"], root=str(tmp_path), now=101.0
    )
    assert not cap_expect.consume(
        "sysadmin", ["ssh-ro", "node-c", "uptime"], root=str(tmp_path), now=101.0
    )


@posix
def test_two_identical_calls_consume_two_records(tmp_path):
    for tool in ("toolu_1", "toolu_2"):
        cap_expect.record(
            "sysadmin",
            "s1",
            tool,
            [["ssh-ro", "node-c", "uptime"]],
            root=str(tmp_path),
            now=100.0,
        )
    argv = ["ssh-ro", "node-c", "uptime"]
    assert cap_expect.consume("sysadmin", argv, root=str(tmp_path), now=101.0)
    assert cap_expect.consume("sysadmin", argv, root=str(tmp_path), now=101.0)
    assert not cap_expect.consume("sysadmin", argv, root=str(tmp_path), now=101.0)


@posix
def test_one_command_with_two_calls_records_both(tmp_path):
    calls = [["kd-task", "claim", "KD-5"], ["backlog", "task", "view", "KD-5"]]
    cap_expect.record("sysadmin", "s1", "toolu_1", calls, root=str(tmp_path), now=100.0)
    assert cap_expect.consume("sysadmin", calls[1], root=str(tmp_path), now=101.0)
    assert cap_expect.consume("sysadmin", calls[0], root=str(tmp_path), now=101.0)


@posix
def test_expired_record_is_not_consumed_and_is_pruned(tmp_path):
    cap_expect.record(
        "sysadmin",
        "s1",
        "toolu_1",
        [["kd-task", "claim", "KD-1"]],
        root=str(tmp_path),
        now=100.0,
    )
    assert not cap_expect.consume(
        "sysadmin",
        ["kd-task", "claim", "KD-1"],
        root=str(tmp_path),
        now=100.0 + cap_expect.TTL + 1,
    )
    cap_expect.record(
        "sysadmin",
        "s1",
        "toolu_2",
        [],
        root=str(tmp_path),
        now=100.0 + cap_expect.TTL + 2,
    )
    assert list((tmp_path / "sysadmin").iterdir()) == []


@posix
def test_argv_must_match_exactly(tmp_path):
    cap_expect.record(
        "sysadmin",
        "s1",
        "toolu_1",
        [["ssh-ro", "node-c", "uptime"]],
        root=str(tmp_path),
        now=1.0,
    )
    assert not cap_expect.consume(
        "sysadmin", ["ssh-ro", "node-c", "uptime", "x"], root=str(tmp_path), now=2.0
    )
    assert not cap_expect.consume(
        "sysadmin", ["ssh-ro", "node-c"], root=str(tmp_path), now=2.0
    )
    assert not cap_expect.consume(
        "dev", ["ssh-ro", "node-c", "uptime"], root=str(tmp_path), now=2.0
    )


@posix
def test_consume_without_store_is_false(tmp_path):
    assert not cap_expect.consume(
        "sysadmin", ["ssh-ro"], root=str(tmp_path / "none"), now=1.0
    )


@posix
def test_records_are_private(tmp_path):
    cap_expect.record(
        "sysadmin", "s1", "toolu_1", [["ssh-ro", "node-c"]], root=str(tmp_path), now=1.0
    )
    (record,) = (tmp_path / "sysadmin").iterdir()
    assert record.stat().st_mode & 0o077 == 0


@posix
@pytest.mark.parametrize(
    "session, tool", [("s1", "../x"), ("s/1", "t"), ("", "t"), ("s", "")]
)
def test_record_refuses_bad_ids(tmp_path, session, tool):
    with pytest.raises(ValueError):
        cap_expect.record(
            "sysadmin", session, tool, [["a"]], root=str(tmp_path), now=1.0
        )


@posix
def test_too_many_pending_calls_are_refused(tmp_path):
    with pytest.raises(ValueError, match="too many"):
        cap_expect.record(
            "sysadmin",
            "s1",
            "t",
            [["a"]] * (cap_expect.MAX_RECORDS + 1),
            root=str(tmp_path),
            now=1.0,
        )


def _self_file(tmp_path, commands):
    from kd import cap_grants

    channel = {
        "key": "self",
        "run_as": "digi-agentx",
        "commands": {
            c: {"program": f"/opt/{c}", "args": [], "subcommands": [], "env": {}}
            for c in commands
        },
        "programs": [],
    }
    (tmp_path / "g" / "agentx").mkdir(parents=True)
    (tmp_path / "g" / "agentx" / "self.json").write_bytes(
        cap_grants.render("agentx", 1000, channel)
    )


def _event(command, tool="toolu_A"):
    event = {
        "tool_name": "Bash",
        "session_id": "sess-1",
        "tool_input": {"command": command},
    }
    if tool:
        event["tool_use_id"] = tool
    return event


def _hook(tmp_path):
    return cap_expect.after_bash(
        "agentx",
        grants_root=str(tmp_path / "g"),
        root=str(tmp_path / "e"),
        now=lambda: 10.0,
    )


@posix
def test_after_bash_records_channel_calls(tmp_path):
    _self_file(tmp_path, ["ssh-ro"])
    assert _hook(tmp_path)(_event("ssh-ro node-c 'echo $(uptime)' | head -3")) is None
    assert cap_expect.consume(
        "agentx",
        ["ssh-ro", "node-c", "echo $(uptime)"],
        root=str(tmp_path / "e"),
        now=11.0,
    )


@posix
def test_after_bash_refuses_cd_compound_and_dynamic_args_without_records(tmp_path):
    _self_file(tmp_path, ["ssh-ro"])
    assert "отдельным вызовом" in _hook(tmp_path)(_event("cd work && cat f"))
    assert "буквальными" in _hook(tmp_path)(_event('ssh-ro "$H" uptime'))
    store = tmp_path / "e" / "agentx"
    assert not store.exists() or not list(store.iterdir())


@posix
def test_after_bash_without_channel_calls_records_nothing(tmp_path):
    _self_file(tmp_path, ["ssh-ro"])
    assert _hook(tmp_path)(_event("ls -la; echo $(date)", tool=None)) is None
    assert not (tmp_path / "e").exists()


@posix
def test_after_bash_without_self_file_records_nothing(tmp_path):
    assert _hook(tmp_path)(_event("ssh-ro node-c uptime")) is None
    assert not (tmp_path / "e").exists()


@posix
def test_after_bash_needs_the_tool_use_id(tmp_path):
    _self_file(tmp_path, ["ssh-ro"])
    assert "ожидание" in _hook(tmp_path)(_event("ssh-ro node-c uptime", tool=None))


@posix
def test_store_failure_is_a_refusal(tmp_path):
    _self_file(tmp_path, ["ssh-ro"])
    (tmp_path / "e").write_text("not a directory")
    assert "ожидание" in _hook(tmp_path)(_event("ssh-ro node-c uptime"))


@posix
def test_standalone_hook_for_an_agent_without_a_guard(tmp_path):
    import io
    import json

    _self_file(tmp_path, ["kd-boss-board"])
    registry = tmp_path / "agents.json"
    registry.write_text(json.dumps({"agentx": {"user": "digi-agentx"}}))
    run = lambda command, user="digi-agentx": cap_expect.standalone_hook(  # noqa: E731
        stdin=io.StringIO(json.dumps(_event(command))),
        stderr=io.StringIO(),
        stdout=io.StringIO(),
        user=user,
        registry=str(registry),
        grants_root=str(tmp_path / "g"),
        root=str(tmp_path / "e"),
        now=lambda: 10.0,
    )
    assert run("kd-boss-board revision KD-1") == 0
    assert cap_expect.consume(
        "agentx",
        ["kd-boss-board", "revision", "KD-1"],
        root=str(tmp_path / "e"),
        now=11.0,
    )
    assert run("cd /srv && kd-boss-board revision KD-1") == 2
    assert run("kd-boss-board revision KD-1", user="digi-other") == 2
