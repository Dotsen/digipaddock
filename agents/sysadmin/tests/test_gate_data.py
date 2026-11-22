"""the data key's forced command `agent-ro-gate --data` — mysql only, as the kd-data MySQL user."""

import io

import pytest

from gate import agent_ro_gate as gate


def run_main(argv, raw, monkeypatch, result=None):
    out, err, calls = io.BytesIO(), io.BytesIO(), []
    monkeypatch.setattr(
        gate,
        "materialize",
        lambda plan, which=None: ["/usr/bin/" + plan.argv[0]] + plan.argv[1:],
    )
    monkeypatch.setattr(
        gate,
        "run_limited",
        lambda argv, timeout=gate.TIMEOUT, limit=gate.LIMIT, env=None: (
            calls.append((argv, timeout))
            or (result or gate.Result(0, b"1\n1\n", False, False))
        ),
    )
    code = gate.main(argv, {"SSH_ORIGINAL_COMMAND": raw}, out, err)
    return code, out.getvalue(), err.getvalue().decode("utf-8"), calls


def test_data_mode_runs_mysql_as_the_data_user_with_its_limit(monkeypatch):
    code, out, err, calls = run_main(
        ["--data"], "mysql -D exampletest -e 'SELECT 1'", monkeypatch
    )
    assert code == 0 and out == b"1\n1\n" and err == ""
    ((argv, timeout),) = calls
    assert argv[:3] == [
        "/usr/bin/mysql",
        "--defaults-file=" + gate.DATA_CNF,
        "--init-command=SET SESSION MAX_EXECUTION_TIME=30000",
    ]
    assert gate.DATA_CNF != gate.MY_CNF and timeout == gate.DATA_TIMEOUT
    assert argv[-4:] == ["-D", "exampletest", "-e", "SELECT 1"]


@pytest.mark.parametrize(
    "raw",
    [
        "tail -n 5 /var/log/syslog",
        "uptime",
        "cat /etc/hostname",
        "journalctl -n 5 --no-pager",
        "ss -tlnpu",  # a root form of the sysadmin's key
        "agent-sec-snapshot",
        "",
    ],
)
def test_data_mode_refuses_everything_but_mysql(raw, monkeypatch):
    code, _, err, calls = run_main(["--data"], raw, monkeypatch)
    assert code == 126 and calls == [] and "отказ" in err


@pytest.mark.parametrize(
    "raw",
    [
        "mysql -D exampletest -e 'INSERT INTO t VALUES (1)'",
        "mysql -D exampletest -e 'UPDATE t SET a = 1'",
        "mysql -D exampletest -e 'DELETE FROM t'",
        "mysql -D exampletest -e 'SELECT 1; SELECT 2'",
        "mysql -D exampletest -e 'SELECT 1 /* x */'",
        "mysql -D exampletest -e 'SELECT 1 -- x'",
        "mysql -D exampletest -e 'SELECT sleep(100)'",
        "mysql --defaults-file=/home/agent-ro/.my.cnf -e 'SELECT 1'",
        "mysql -u root -e 'SELECT 1'",
    ],
)
def test_data_mode_keeps_the_mysql_rules(raw, monkeypatch):
    code, _, _, calls = run_main(["--data"], raw, monkeypatch)
    assert code == 126 and calls == []


def test_sysadmin_mode_is_unchanged(monkeypatch):
    code, _, _, calls = run_main(
        [], "uptime", monkeypatch, gate.Result(0, b"up\n", False, False)
    )
    assert code == 0 and calls == [(["/usr/bin/uptime"], gate.TIMEOUT)]
    _, _, _, calls = run_main([], "mysql -e 'SELECT 1'", monkeypatch)
    assert calls[0][0][1] == "--defaults-file=" + gate.MY_CNF


def test_data_mode_marks_truncation_and_timeout(monkeypatch):
    code, out, _, _ = run_main(
        ["--data"],
        "mysql -D db -e 'SELECT 1'",
        monkeypatch,
        gate.Result(-15, b"x" * 10, True, False),
    )
    assert code == 0 and "обрезан".encode() in out
    code, out, _, _ = run_main(
        ["--data"],
        "mysql -D db -e 'SELECT 1'",
        monkeypatch,
        gate.Result(-9, b"", False, True),
    )
    assert code == 124 and ("таймауту %d с" % gate.DATA_TIMEOUT).encode() in out


def test_data_mode_masks_secrets(monkeypatch):
    row = b"id\tnote\n1\tpassword=hunter2hunter2\n"
    _, out, _, _ = run_main(
        ["--data"],
        "mysql -D db -e 'SELECT id, note FROM t'",
        monkeypatch,
        gate.Result(0, row, False, False),
    )
    assert b"hunter2hunter2" not in out


def test_unknown_argument_is_usage(monkeypatch):
    code, _, err, calls = run_main(["--other"], "uptime", monkeypatch)
    assert code == 2 and calls == [] and "--data" in err


def test_data_cnf_is_not_readable_through_the_sysadmin_key():
    with pytest.raises(gate.Deny):
        gate.check("cat /home/agent-ro/.my-data.cnf")
    assert any(p.search("/home/agent-ro/.my-data.cnf") for p in gate.FORBIDDEN)
