"""kd-data — the executor of data.read (database -> server, the gate's checks, the journal)."""

from __future__ import annotations

import io
import json
import shlex

import pytest

from kd import data_read
from gate import agent_ro_gate as gate

INVENTORY = {
    "home": "example-stage",
    "prod": "example-prod",
    "servers": {
        "example-prod": {
            "host": "203.0.113.20",
            "exec_user": "root",
            "data_databases": ["proddb", "archivedb"],
        },
        "example-stage": {
            "host": "203.0.113.10",
            "exec_user": "root",
            "data_databases": ["testdb"],
        },
        "example-node": {"host": "203.0.113.30", "exec_user": "root"},
    },
}


@pytest.fixture
def env(tmp_path):
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps(INVENTORY), encoding="utf-8")
    enabled = tmp_path / "servers.enabled"
    enabled.write_text("example-prod\nexample-stage\nexample-node\n", encoding="utf-8")
    return {
        "inventory": inventory,
        "enabled": enabled,
        "journal": tmp_path / "data.jsonl",
    }


def call(env, argv, agent="analyst", result=None):
    out, err, calls = io.BytesIO(), io.StringIO(), []

    def runner(argv, timeout, limit):
        calls.append((argv, timeout, limit))
        return result or gate.Result(0, b"1\n1\n", False, False)

    code = data_read.main(
        argv,
        agent=agent,
        home="/home/kd-data",
        inventory_path=str(env["inventory"]),
        enabled_path=str(env["enabled"]),
        journal_path=str(env["journal"]),
        runner=runner,
        out=out,
        err=err,
    )
    return code, out.getvalue(), err.getvalue(), calls


def records(env):
    return [
        json.loads(line)
        for line in env["journal"].read_text(encoding="utf-8").splitlines()
    ]


@pytest.mark.parametrize("agent", ["analyst", "dev", "boss"])
def test_select_reaches_the_gate_of_the_database_server(env, agent):
    code, out, err, calls = call(env, ["testdb", "SELECT 1"], agent=agent)
    assert (code, out, err) == (0, b"1\n1\n", "")
    ((argv, timeout, limit),) = calls
    assert (
        argv[0] == data_read.SSH
        and "-i" in argv
        and argv[argv.index("-i") + 1].endswith("agent_data_ed25519")
    )
    assert argv[-3:] == ["agent-ro@203.0.113.10", "--", "mysql -D testdb -e 'SELECT 1'"]
    assert "StrictHostKeyChecking=yes" in argv and "BatchMode=yes" in argv
    assert timeout > gate.DATA_TIMEOUT and limit >= gate.LIMIT
    # the remote command is exactly what the server's gate accepts in data mode
    assert gate.check_data(argv[-1]).argv[0] == "mysql"
    start, result = records(env)
    assert (
        start["event"] == "start"
        and start["agent"] == agent
        and start["server"] == "example-stage"
    )
    assert start["database"] == "testdb" and start["sql"] == "SELECT 1" and start["ts"]
    assert result == {
        **result,
        "event": "result",
        "id": start["id"],
        "agent": agent,
        "server": "example-stage",
        "database": "testdb",
        "rc": 0,
        "bytes": 4,
    }


def test_quotes_and_unicode_reach_the_gate_as_one_statement(env):
    sql = "SELECT id FROM t WHERE name = 'Кафе \"Юг\"' AND note <> 'it''s'"
    code, _, _, calls = call(env, ["testdb", sql])
    assert code == 0
    remote = calls[0][0][-1]
    assert shlex.split(remote) == ["mysql", "-D", "testdb", "-e", sql]
    assert gate.check_data(remote).argv[-1] == sql


@pytest.mark.parametrize(
    "sql", ["SELECT `id` FROM t", "SELECT 1\nFROM t", "SELECT '$x'", "SELECT 1 | 2"]
)
def test_shell_characters_are_refused_in_sql_terms(env, sql):
    code, _, err, calls = call(env, ["testdb", sql])
    assert code == data_read.EXIT_DENIED and calls == [] and "one line without" in err
    assert records(env)[0]["event"] == "refused"


def test_the_server_follows_the_database(env):
    call(env, ["archivedb", "SHOW TABLES"])
    assert records(env)[0]["server"] == "example-prod"


@pytest.mark.parametrize(
    "sql, why",
    [
        ("INSERT INTO t VALUES (1)", "SELECT, SHOW"),
        ("UPDATE t SET a = 1", "SELECT, SHOW"),
        ("DELETE FROM t", "SELECT, SHOW"),
        ("SELECT 1; SELECT 2", "метасимволы"),
        ("SELECT 1 /* x */", "комментарии"),
        ("SELECT 1 # x", "комментарии"),
        ("SELECT sleep(60)", "sleep"),
    ],
)
def test_writes_several_statements_and_comments_are_refused_before_ssh(env, sql, why):
    code, _, err, calls = call(env, ["testdb", sql])
    assert code == data_read.EXIT_DENIED and calls == [] and why in err
    (record,) = records(env)
    assert (
        record["event"] == "refused"
        and record["sql"] == sql
        and record["rc"] == data_read.EXIT_DENIED
    )


def test_unknown_database_is_refused_with_the_list(env):
    code, _, err, calls = call(env, ["otherdb", "SELECT 1"])
    assert code == data_read.EXIT_DENIED and calls == []
    assert "unknown database 'otherdb'" in err and "archivedb, proddb, testdb" in err
    assert records(env)[0]["event"] == "refused"


def test_a_server_without_its_gate_is_refused(env):
    env["enabled"].write_text("example-prod\n", encoding="utf-8")
    code, _, err, calls = call(env, ["testdb", "SELECT 1"])
    assert code == data_read.EXIT_DENIED and calls == [] and "not connected" in err


def test_usage(env):
    code, _, err, calls = call(env, ["testdb"])
    assert code == data_read.EXIT_USAGE and calls == [] and "usage: kd-data" in err
    code, _, _, _ = call(env, ["testdb", "   "])
    assert code == data_read.EXIT_USAGE


def test_no_agent_no_call(env):
    code, _, err, calls = call(env, ["testdb", "SELECT 1"], agent="")
    assert code == data_read.EXIT_DENIED and calls == [] and not env["journal"].exists()


def test_no_journal_no_call(env, tmp_path):
    env["journal"] = tmp_path / "missing" / "data.jsonl"
    code, _, err, calls = call(env, ["testdb", "SELECT 1"])
    assert code == data_read.EXIT_ERROR and calls == [] and "nothing ran" in err


def test_truncation_and_timeout_are_marked_and_journaled(env):
    code, out, _, _ = call(
        env, ["testdb", "SELECT 1"], result=gate.Result(0, b"x" * 8, True, False)
    )
    assert code == 0 and b"output cut" in out
    code, out, _, _ = call(
        env, ["testdb", "SELECT 1"], result=gate.Result(-9, b"", False, True)
    )
    assert code == 124 and b"stopped after" in out
    results = [r for r in records(env) if r["event"] == "result"]
    assert [r["truncated"] for r in results] == [True, False] and results[1][
        "timed_out"
    ] is True


@pytest.mark.parametrize(
    "servers",
    [
        {
            "a": {"host": "203.0.113.1", "data_databases": ["x"]},
            "b": {"host": "203.0.113.2", "data_databases": ["x"]},
        },
        {"a": {"host": "203.0.113.1", "data_databases": ["bad-name"]}},
        {"a": {"host": "not-an-ip", "data_databases": ["x"]}},
        {"a": {"host": "203.0.113.1", "data_databases": "x"}},
    ],
)
def test_a_broken_inventory_is_an_error_not_a_guess(env, servers):
    env["inventory"].write_text(json.dumps({"servers": servers}), encoding="utf-8")
    code, _, err, calls = call(env, ["x", "SELECT 1"])
    assert code == data_read.EXIT_ERROR and calls == [] and "inventory" in err
