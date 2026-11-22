"""the host's one-year Claude token (claude setup-token) in a catalog secret only root reads."""

import json
import os
from pathlib import Path

import pytest

from kd import claude_token, install_catalog, naming

REPO = Path(__file__).resolve().parents[2]
HOST = naming.load_host(str(REPO / "host" / "host.example.json"))
TOKEN = "sk-ant-oat01-test"


def test_read_value(tmp_path):
    path = tmp_path / "claude.env"
    path.write_text(f"# the token\nCLAUDE_CODE_OAUTH_TOKEN={TOKEN}\n", encoding="utf-8")
    assert claude_token.read(path) == TOKEN


def test_read_crlf_and_spaces(tmp_path):
    path = tmp_path / "claude.env"
    path.write_bytes(f"CLAUDE_CODE_OAUTH_TOKEN = {TOKEN} \r\n".encode())
    assert claude_token.read(path) == TOKEN


def test_read_missing_or_without_key_is_empty(tmp_path):
    assert claude_token.read(tmp_path / "none.env") == ""
    other = tmp_path / "other.env"
    other.write_text("OTHER=1\n", encoding="utf-8")
    assert claude_token.read(other) == ""
    empty = tmp_path / "empty.env"
    empty.write_text("CLAUDE_CODE_OAUTH_TOKEN=\n", encoding="utf-8")
    assert claude_token.read(empty) == ""
    assert claude_token.read(tmp_path) == ""  # a directory: unreadable, not a traceback


def test_problem_accepts_one_token_line(tmp_path):
    path = tmp_path / "claude.env"
    path.write_bytes(f"# put 05.11\n\nCLAUDE_CODE_OAUTH_TOKEN = {TOKEN} \r\n".encode())
    assert claude_token.problem(path) == ""


@pytest.mark.parametrize(
    ("text", "said"),
    [
        ("CLAUDE_CODE_OAUTH_TOKEN=\n", "no value"),
        (f'CLAUDE_CODE_OAUTH_TOKEN="{TOKEN}"\n', "quotes"),
        (f"CLAUDE_CODE_OAUTH_TOKEN='{TOKEN}'\n", "quotes"),
        (f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN} x\n", "letters, digits"),
        (
            f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN}\nCLAUDE_CODE_OAUTH_TOKEN={TOKEN}\n",
            "one line",
        ),
        (f"export CLAUDE_CODE_OAUTH_TOKEN={TOKEN}\n", "only"),
        (f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN}\nOTHER=1\n", "only"),
        ("# nothing\n", "no CLAUDE_CODE_OAUTH_TOKEN"),
    ],
)
def test_problem_names_what_is_wrong_without_the_value(tmp_path, text, said):
    """systemd and the runner must read the same token — one line, a bare value, nothing else."""
    path = tmp_path / "claude.env"
    path.write_text(text, encoding="utf-8")
    reason = claude_token.problem(path)
    assert said in reason and TOKEN not in reason
    assert claude_token.read(path) == ""  # what the check refuses, no process gets


def test_problem_of_a_missing_or_unreadable_file(tmp_path):
    assert claude_token.problem(tmp_path / "none.env") == "no file"
    assert claude_token.problem(tmp_path) == "unreadable"


def test_check_command_exits_1_with_the_reason(tmp_path, capsys):
    path = tmp_path / "claude.env"
    path.write_text("CLAUDE_CODE_OAUTH_TOKEN=\n", encoding="utf-8")
    assert claude_token.main(["check", str(path)]) == 1
    assert "no value" in capsys.readouterr().out
    path.write_text(f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN}\n", encoding="utf-8")
    assert claude_token.main(["check", str(path)]) == 0
    assert claude_token.main([]) == 2


def test_env_empty_without_token(tmp_path):
    assert claude_token.env(tmp_path / "none.env") == {}
    path = tmp_path / "claude.env"
    path.write_text(f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN}\n", encoding="utf-8")
    assert claude_token.env(path) == {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN}


def test_age_days_from_mtime(tmp_path):
    path = tmp_path / "claude.env"
    path.write_text(f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN}\n", encoding="utf-8")
    os.utime(path, (1_000_000, 1_000_000))
    assert claude_token.age_days(path, now=lambda: 1_000_000 + 340 * 86400) == 340
    assert claude_token.age_days(tmp_path / "none.env") is None


def test_no_agent_reads_the_token():
    """Root 0600 already; the rules, the resources registry and the host reader refuse it too, like pult.env."""
    settings = json.loads(
        (REPO / "common" / "claude" / "managed-settings.template.json").read_text(
            encoding="utf-8"
        )
    )
    assert f"Read(/{claude_token.PATH})" in settings["permissions"]["deny"]
    resources = json.loads(
        (REPO / "common" / "agent-resources.json").read_text(encoding="utf-8")
    )
    assert {"kind": "file", "path": claude_token.PATH, "deny": ["read"]} in resources[
        "protected"
    ]
    never = (
        (REPO / "common" / "install" / "host_read_setup.sh")
        .read_text(encoding="utf-8")
        .split("NEVER=(", 1)[1]
    )
    assert claude_token.PATH in never.split(")", 1)[0]


def test_catalog_has_claude_token():
    by = {s.name: s for s in install_catalog.secrets(host=HOST, prefix="digi")}
    s = by["claude-token"]
    assert (s.path, s.owner, s.group, s.mode) == (
        claude_token.PATH,
        "root",
        "root",
        0o600,
    )
    assert s.format == "CLAUDE_CODE_OAUTH_TOKEN=<token>"
    assert set(s.wanted_by) == {"dev", "boss", "sysadmin", "analyst"}
    assert "claude setup-token" in s.how


def test_no_read_rule_reaches_into_proc():
    """Claude Code makes every Read deny a bwrap mount of the sandbox too; under /proc (the sandbox's
    own, no outer pids) bwrap cannot create it and every Bash command of every agent failed before it ran."""
    settings = json.loads(
        (REPO / "common" / "claude" / "managed-settings.template.json").read_text(
            encoding="utf-8"
        )
    )
    rules = settings["permissions"]["deny"] + settings["permissions"].get("ask", [])
    assert [r for r in rules if r.startswith(("Read(//proc", "Read(/proc"))] == []


def test_sandboxed_bash_never_gets_the_token():
    """The subprocess scrub left sandbox placeholders in work/; instead the fail-closed sandbox of
    every profile (and so every compiled policy and worker) unsets the token before each Bash command."""
    from kd.registry import FAIL_CLOSED_SANDBOX

    deny = {"name": claude_token.KEY, "mode": "deny"}
    assert deny in FAIL_CLOSED_SANDBOX["credentials"]["envVars"]
    for path in sorted((REPO / "common" / "agent-sandboxes").glob("*.json")):
        settings = json.loads(path.read_text(encoding="utf-8"))["settings"]
        assert deny in settings["credentials"]["envVars"], path.name
    fixtures = REPO / "common" / "tests" / "fixtures" / "policy"
    for name in ("dev", "sysadmin"):
        text = (fixtures / f"{name}.policy.json").read_text(encoding="utf-8")
        sandbox = json.loads(text)["launch_settings"]["sandbox"]
        assert deny in sandbox["credentials"]["envVars"], name
