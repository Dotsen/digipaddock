import io
import json
import subprocess
from pathlib import Path

import pytest

COMMON = Path(__file__).resolve().parent.parent


def run(kd_permlog, event, env=None, raw=None):
    lines = []
    stdin = io.StringIO(raw if raw is not None else json.dumps(event, ensure_ascii=False))
    code = kd_permlog.main(stdin=stdin, env={"KD_AGENT": "sysadmin"} if env is None else env, log=lines.append)
    return code, [json.loads(line) for line in lines]


def test_bash_card_is_logged_with_agent_command_and_description(kd_permlog, capsys):
    code, lines = run(kd_permlog, {"hook_event_name": "PermissionRequest", "session_id": "s-1",
                                   "cwd": "/srv/digipaddock-agents/sysadmin", "tool_name": "Bash",
                                   "tool_input": {"command": "kd-monitor list | awk '{print $1}'",
                                                  "description": "List open monitor problems"}})
    assert code == 0
    assert lines == [{"agent": "sysadmin", "tool": "Bash", "input": "kd-monitor list | awk '{print $1}'",
                      "description": "List open monitor problems", "session": "s-1"}]
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""  # an empty output: the hook takes no decision about the prompt


def test_file_tool_logs_path(kd_permlog):
    _, lines = run(kd_permlog, {"tool_name": "Read", "tool_input": {"file_path": "/var/lib/digipaddock/x.json"}})
    assert lines[0]["tool"] == "Read" and lines[0]["input"] == "/var/lib/digipaddock/x.json"


def test_other_tool_logs_compact_json_input(kd_permlog):
    _, lines = run(kd_permlog, {"tool_name": "mcp__x__y", "tool_input": {"a": "б"}})
    assert lines[0]["input"] == '{"a": "б"}'


def test_the_agent_comes_from_the_channel_never_from_the_event(kd_permlog):
    # the base channel names the caller; an event's cwd names nothing
    _, lines = run(kd_permlog, {"tool_name": "Bash", "cwd": "/srv/digipaddock-agents/boss",
                                "tool_input": {"command": "touch x"}}, env={"KD_AGENT": "dev"})
    assert lines[0]["agent"] == "dev"
    _, lines = run(kd_permlog, {"tool_name": "Bash", "cwd": "/srv/digipaddock-agents/boss",
                                "tool_input": {"command": "touch x"}}, env={})
    assert lines == []


def test_long_input_is_truncated(kd_permlog):
    _, lines = run(kd_permlog, {"tool_name": "Bash", "tool_input": {"command": "x" * 10000, "description": "d" * 1000}})
    assert len(lines[0]["input"]) == kd_permlog.LIMIT and len(lines[0]["description"]) == 200


PEM = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----"  # privacy-scan: allow (fake sample)


@pytest.mark.parametrize("command, secret", [
    ("curl -H 'Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc' https://x", "eyJhbGciOiJIUzI1NiJ9.abc"),
    ('curl -H "Authorization: Basic dXNlcjpzM2NyM3Q=" https://x', "dXNlcjpzM2NyM3Q="),
    ("curl -u admin:s3cr3tPass https://x", "s3cr3tPass"),
    ("KD_TOKEN=tok123456 kd-run", "tok123456"),
    ("export GITHUB_TOKEN=ghp_abcdef123", "ghp_abcdef123"),
    ("mysql --password=hunter2 -e 'select 1'", "hunter2"),
    ("mysql -uroot -phunter2 db", "hunter2"),
    ("curl 'https://api.x/v1?user=1&access_token=AbCd1234&page=2'", "AbCd1234"),
    ("curl https://h/x?sig=deadbeef99", "deadbeef99"),
    ("git clone https://bob:pa55word@forge.x/repo.git", "pa55word"),
    ("curl https://api.telegram.org/bot123456789:" + "A" * 35 + "/getMe", "A" * 35),
    ("printf '%s' '" + PEM + "' > k", "b3BlbnNzaC1rZXktdjEAAAAA"),
])
def test_secrets_are_masked_before_logging(kd_permlog, command, secret):
    _, lines = run(kd_permlog, {"tool_name": "Bash", "tool_input": {"command": command, "description": command}})
    assert secret not in lines[0]["input"] and "***" in lines[0]["input"]
    assert secret not in lines[0]["description"]


def test_secrets_in_whole_tool_input_are_masked(kd_permlog):
    _, lines = run(kd_permlog, {"tool_name": "mcp__x__y", "tool_input": {"url": "https://h", "api_key": "k-998877"}})
    assert "k-998877" not in lines[0]["input"] and '"api_key": "***"' in lines[0]["input"]


def test_secret_cut_at_scan_edge_is_masked(kd_permlog):
    command = "x" * (kd_permlog.SCAN - 20) + " https://user:longpassword@host"
    assert "longpass" not in kd_permlog.redact(command)


def test_ordinary_commands_are_untouched(kd_permlog):
    command = "kd-monitor list | awk '{print $1}' && wg show wg0 public key: AbC= && basic settings"
    assert kd_permlog.redact(command) == command


@pytest.mark.parametrize("raw", ["", "not json", "[1, 2]", '{"tool_input": "str"}'])
def test_bad_input_never_fails_and_prints_nothing(kd_permlog, capsys, raw):
    code, _ = run(kd_permlog, None, raw=raw)
    assert code == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_log_failure_is_swallowed(kd_permlog):
    def broken(line):
        raise OSError("no /dev/log")

    assert kd_permlog.main(stdin=io.StringIO('{"tool_name": "Bash"}'), env={}, log=broken) == 0


def test_audit_file_is_documented(kd_permlog):
    assert kd_permlog.AUDIT == "/var/log/digipaddock-permission/permission.jsonl"
    assert kd_permlog.AUDIT in (COMMON / "bin/kd-permlog").read_text(encoding="utf-8")
    assert "syslog" not in (COMMON / "bin/kd-permlog").read_text(encoding="utf-8")  # out of the journal


def _read(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_records_append_to_the_audit_file(kd_permlog, tmp_path):
    audit = tmp_path / "permission.jsonl"
    for n in range(3):
        kd_permlog.to_file(json.dumps({"agent": "dev", "n": n}), "dev", audit, now=lambda: 1000.0)
    assert [r["n"] for r in _read(audit)] == [0, 1, 2]
    assert oct(audit.stat().st_mode & 0o777) in ("0o600", "0o666")  # 0600 on Linux; Windows ignores modes


def test_records_are_limited_per_agent_and_minute(kd_permlog, tmp_path):
    audit = tmp_path / "permission.jsonl"
    for n in range(kd_permlog.RATE + 5):
        kd_permlog.to_file(json.dumps({"agent": "dev", "n": n}), "dev", audit, now=lambda: 60.0)
    kd_permlog.to_file(json.dumps({"agent": "boss", "n": 0}), "boss", audit, now=lambda: 60.0)
    records = _read(audit)
    assert len([r for r in records if r.get("agent") == "dev" and "n" in r]) == kd_permlog.RATE
    assert [r for r in records if r.get("limit")] == [{"agent": "dev", "limit": f"over {kd_permlog.RATE} a minute"}]
    assert records[-1] == {"agent": "boss", "n": 0}  # another agent's budget is its own
    kd_permlog.to_file(json.dumps({"agent": "dev", "n": "next"}), "dev", audit, now=lambda: 121.0)
    assert _read(audit)[-1]["n"] == "next"  # a new minute, a new budget


def test_an_oversized_event_is_one_short_record(kd_permlog):
    lines = []
    raw = io.StringIO(json.dumps({"tool_name": "Write", "tool_input": {"file_path": "/x", "content": "y" * (2 << 20)}}))
    assert kd_permlog.main(stdin=raw, env={"KD_AGENT": "dev"}, log=lines.append) == 0
    assert [json.loads(line) for line in lines] == [{"agent": "dev", "tool": "?", "input": "(event over 1 MiB)",
                                                     "description": "", "session": ""}]


def test_the_hook_never_blocks_and_never_echoes_the_event(kd_permlog):
    err = io.StringIO()

    def refused(argv, **kwargs):
        assert argv[1:3] == ["run", "base"] and argv[3].endswith("/common/bin/kd-permlog")
        assert kwargs["stdin"] is None  # the event goes to the channel unread
        return type("Done", (), {"returncode": 126})()

    assert kd_permlog.hook(run=refused, stderr=err) == 0
    assert err.getvalue() == "kd-permlog: audit channel unavailable\n"

    def broken(argv, **kwargs):
        raise OSError("no kd-cap")

    err = io.StringIO()
    assert kd_permlog.hook(run=broken, stderr=err) == 0 and err.getvalue() == "kd-permlog: audit channel unavailable\n"


def test_script_shebang_and_lf():
    data = (COMMON / "bin/kd-permlog").read_bytes()
    # isolated (KD-9474): it runs as the agent outside the sandbox, so nothing of the agent's home (a .pth in
    # ~/.local/lib, PYTHON* variables, a python3 earlier on PATH) is loaded into it
    assert data.startswith(b"#!/usr/bin/python3 -I\n") and b"\r" not in data


def test_script_is_executable_in_git_index():
    out = subprocess.run(["git", "ls-files", "-s", "common/bin/kd-permlog"], cwd=COMMON.parent,
                         capture_output=True, text=True)
    if out.returncode != 0 or not out.stdout.strip():
        pytest.skip("файл ещё не в индексе git")
    assert out.stdout.split()[0] == "100755"


def test_a_failed_write_is_a_failure_the_hook_reports(kd_permlog, capsys):
    # A lost record is not silent — the executor exits 1 and the hook prints its line
    def broken(line):
        raise OSError("no audit dir")

    raw = io.StringIO('{"tool_name": "Bash", "tool_input": {"command": "ls"}}')
    assert kd_permlog.main(stdin=raw, env={"KD_AGENT": "dev"}, log=broken) == 1
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""
