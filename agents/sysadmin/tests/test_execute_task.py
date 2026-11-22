"""``ssh-exec --task`` is gone — a plan step's exact text is a ``script`` step run by ``kd-admin-run``.

The old free command (any command on a server of a shell step for the grant's lifetime) is refused before SSH
with the hint; a run without ``--task`` still names the plan as before.
"""

import io
import json

import pytest

from gate.agent_ro_gate import Result
from sysadmin import execute, inventory


class Runner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, timeout, limit, kill_on_limit):
        self.calls.append(argv)
        return Result(0, b"done\n", False, False)


@pytest.fixture(autouse=True)
def all_servers_enabled(monkeypatch, enabled_all):
    monkeypatch.setattr(inventory, "ENABLED", enabled_all)


def run(tmp_path, argv):
    runner, sent, out, err = Runner(), [], io.BytesIO(), io.StringIO()
    log = tmp_path / "exec.jsonl"
    code = execute.main(argv, env={"KD_CALLER_UID": "4242"}, log_path=log, runner=runner,
                        notify=lambda *notice: sent.append(notice) or True, out=out, err=err)
    records = [json.loads(line) for line in log.read_text("utf-8").splitlines()] if log.exists() else []
    return code, err.getvalue(), runner.calls, sent, records


def test_ssh_exec_task_is_refused_with_the_script_hint(tmp_path):
    code, err, calls, sent, log = run(tmp_path, ["--agent", "sysadmin", "--task", "KD-123", "node-c",
                                                 "cp /etc/nginx/nginx.conf /tmp/x && nginx -t"])
    assert code == 2 and calls == [] and sent == []
    assert "ssh-exec: отказ, ничего не выполнено" in err and execute.SHELL_GONE in err
    assert [r["event"] for r in log] == ["task_refused"]
    assert log[0]["task"] == "KD-123" and log[0]["servers"] == ["node-c"] and "shell steps are gone" in log[0]["reason"]


def test_ssh_exec_task_on_a_group_is_refused_the_same_way(tmp_path):
    code, err, calls, _, log = run(tmp_path, ["--task", "KD-123", "node-f,node-h", "uptime"])
    assert code == 2 and calls == [] and log[0]["servers"] == ["node-f", "node-h"]


def test_ssh_exec_without_task_still_names_the_plan(tmp_path):
    code, err, calls, sent, log = run(tmp_path, ["--agent", "sysadmin", "node-c", "uptime"])
    assert code == 2 and calls == [] and sent == []
    assert execute.NO_TASK in err and log[0]["event"] == "task_refused" and log[0]["command"] == "uptime"


def test_execute_has_no_task_authorization_left():
    assert not hasattr(execute, "authorize_task")
