"""agent-launch and kd.launch (BOSS plan A04): bounded --model and the strict launch.

The Python tests run kd.launch on a fake filesystem: every input of a generated launch
and every refusal. The bash tests run the launcher itself with stand-ins for sudo, id,
stat and claude: only ``--session`` of a generated agent under its own user starts
anything (no panels, no legacy runtime, no Telegram), and the plan must reach
claude as plain argv, never through shell evaluation. Live bubblewrap/AppArmor behaviour
and root ownership on a real host are stage gates (plan B08 and M01), not covered here.
"""

from __future__ import annotations

import copy
import errno
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from kd import launch
from kd.launch import (
    MAX_POLICY_BYTES,
    LaunchError,
    LaunchPlan,
    Node,
    check_compiled,
    plan_generated,
    render_plan,
)
from kd.manifest import load_manifest
from kd.policy import SCHEMA_VERSION, compile_policy, render_policy
from kd.registry import load_registry, read_json_file

ROOT = Path(__file__).resolve().parents[2]
COMMON = ROOT / "common"
FIXTURES = Path(__file__).parent / "fixtures"
POLICIES = FIXTURES / "policy"
FLOOR = read_json_file(COMMON / "claude/managed-settings.template.json")
AGENTS = "/srv/digipaddock-agents"
AGENT_UID = 1001
USERS = {"boss": "digi-boss", "dev": "digi-dev"}
SWITCHED = {
    name: {"user": user, "runtime": "generated"}
    for name, user in USERS.items()
}
Policy = dict[str, Any]


def _policy_of(name: str) -> Policy:
    """boss compiled from its fixture manifest; dev as its pinned snapshot."""
    if name == "dev":
        return json.loads((POLICIES / "dev.policy.json").read_text("utf-8"))
    host = {
        item: load_manifest(POLICIES / "agents" / item / "agent.json")
        for item in ("sysadmin", "dev", "boss")
    }
    return compile_policy(host[name], load_registry(ROOT), floor=FLOOR, agents=host)


@pytest.fixture(scope="module")
def policies() -> dict[str, Policy]:
    return {name: _policy_of(name) for name in USERS}


@pytest.fixture
def registry(tmp_path: Path) -> str:
    path = tmp_path / "agents.json"
    path.write_text(json.dumps(SWITCHED), encoding="utf-8")
    return str(path)


class FakeFS:
    """Paths with owner and mode, as lstat would report them on the stage host."""

    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.data: dict[str, bytes] = {}

    def node(self, path: str) -> Node | None:
        return self.nodes.get(path)

    def read(self, path: str, limit: int) -> tuple[Node, bytes]:
        node = self.nodes.get(path)
        if node is None:
            raise FileNotFoundError(errno.ENOENT, "missing", path)
        if node.kind == "link":  # O_NOFOLLOW
            raise OSError(errno.ELOOP, "symbolic link", path)
        if node.kind != "file":
            return node, b""
        return node, self.data.get(path, b"")[: limit + 1]

    def names(self, path: str) -> list[str]:
        prefix = path + "/"
        return sorted(
            {p[len(prefix) :].split("/")[0] for p in self.nodes if p.startswith(prefix)}
        )


def installed(name: str, policy: bytes) -> FakeFS:
    """A complete generated install: root's folder and policy, the agent's work/."""
    fs = FakeFS()
    folder = f"{AGENTS}/{name}"
    for directory in ("/", "/srv", AGENTS, folder, f"{folder}/.claude"):
        fs.nodes[directory] = Node("dir", 0, 0o755)
    fs.nodes[f"{folder}/.claude/policy.json"] = Node("file", 0, 0o644)
    fs.data[f"{folder}/.claude/policy.json"] = policy
    fs.nodes[f"{folder}/work"] = Node("dir", AGENT_UID, 0o755)
    for item in ("CLAUDE.md", "CLAUDE.local.md"):
        fs.nodes[f"{folder}/work/{item}"] = Node("file", 0, 0o644)
    return fs


def _plan(
    name: str,
    policy: bytes,
    registry: str,
    *,
    fs: FakeFS | None = None,
    user: str | None = None,
    space: str = "chat",
) -> LaunchPlan:
    return plan_generated(
        name,
        f"{AGENTS}/{name}",
        uid=AGENT_UID,
        user=user or USERS[name],
        agents_root=AGENTS,
        fs=fs or installed(name, policy),
        registry=registry,
        space=space,
    )


def _merged(policy: Policy) -> str:
    merged = {**policy["settings"], **policy["launch_settings"]}
    return json.dumps(merged, sort_keys=True, separators=(",", ":"))


# --- positive: the plan of a coordinator and of the developer -------------------------


def test_boss_plan_is_bounded_model_and_strict_merged_settings(policies, registry):
    policy = policies["boss"]
    plan = _plan("boss", render_policy(policy), registry)
    assert plan == LaunchPlan("sonnet", _merged(policy))
    settings = json.loads(plan.settings)
    assert set(settings) == {"hooks", "permissions", "sandbox"}
    sandbox = settings["sandbox"]
    assert sandbox["enabled"] is True and sandbox["failIfUnavailable"] is True
    assert sandbox["allowUnsandboxedCommands"] is False
    assert sandbox["network"]["allowedDomains"] == []  # BOSS: no Bash network at all
    assert settings["permissions"]["disableBypassPermissionsMode"] == "disable"


def test_dev_agent_plan_keeps_platform_model_and_its_sandbox(policies, registry):
    policy = policies["dev"]
    plan = _plan("dev", render_policy(policy), registry)
    assert plan == LaunchPlan(None, _merged(policy))
    sandbox = json.loads(plan.settings)["sandbox"]
    assert sandbox["network"]["allowedDomains"] == ["forge.example.test"]  # the mirror is the executor's (git.mirror)
    # the board and forge tools reach their channels; nothing leaves the sandbox.
    assert sandbox["excludedCommands"] == []
    assert sandbox["network"]["allowAllUnixSockets"] is True


@pytest.mark.parametrize("old", [1, 2, 3, 4, 5])
def test_an_older_policy_is_refused_with_reinstall_advice(policies, registry, old):
    # a policy installed before this code (with telegram and sudoers) is not judged
    # field by field; the launch says what to do. 4: before the sandbox's credentials deny.
    assert SCHEMA_VERSION == 7
    policy = copy.deepcopy(policies["boss"])
    policy.update(schema_version=old, telegram=False, sudoers=[])
    with pytest.raises(LaunchError) as error:
        _plan("boss", render_policy(policy), registry)
    assert str(error.value) == (
        f"compiled policy of boss has schema_version {old}, older than this launcher (7):"
        " reinstall the agent (new_agent.sh --install boss)"
    )


@pytest.mark.parametrize("model", ["opus", "haiku", "claude-sonnet-4-5"])
def test_every_manifest_model_form_reaches_the_plan(policies, registry, model):
    policy = copy.deepcopy(policies["boss"])
    policy["model"] = model
    assert _plan("boss", render_policy(policy), registry).model == model


def test_plan_lines_hold_no_shell_syntax(policies):
    policy = policies["boss"]
    plan = LaunchPlan("sonnet", _merged(policy))
    assert render_plan(plan) == f"generated\nsonnet\n\n{plan.settings}\n"
    assert render_plan(LaunchPlan(None, "{}")) == "generated\n\n\n{}\n"
    assert render_plan(LaunchPlan("opus", "{}", "high")) == "generated\nopus\nhigh\n{}\n"


def test_policy_without_effort_plans_none(policies, registry):
    # no field, no --effort — the policies installed before the field launch as they did.
    assert "effort" not in policies["boss"]
    assert _plan("boss", render_policy(policies["boss"]), registry).effort is None


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_compiled_effort_reaches_the_plan(policies, registry, effort):
    policy = copy.deepcopy(policies["boss"])
    policy["effort"] = effort
    plan = _plan("boss", render_policy(policy), registry)
    assert (plan.model, plan.effort) == ("sonnet", effort)


def test_a_threads_own_model_reaches_only_its_sessions_plan(policies, registry):
    """The "Monitor" session runs on the thread's model; the chat and the cards keep the agent's."""
    policy = copy.deepcopy(policies["boss"])
    policy["thread_models"] = {"monitor": "claude-sonnet-5-5"}
    policy["model"] = "opus"
    text = render_policy(policy)
    plans = {space: _plan("boss", text, registry, space=space) for space in ("chat", "monitor")}
    assert (plans["chat"].model, plans["monitor"].model) == ("opus", "claude-sonnet-5-5")
    assert plans["chat"].settings == plans["monitor"].settings  # the same rights, only the model differs
    plain = copy.deepcopy(policies["boss"])
    assert "thread_models" not in plain
    assert _plan("boss", render_policy(plain), registry, space="monitor").model == "sonnet"  # none: the agent's


@pytest.mark.parametrize("value", [{}, {"monitor": "--model x"}, {"chat": "sonnet"}, {"monitor": ""}, "sonnet",
                                   {"monitor": {"model": "sonnet"}}], ids=repr)
def test_a_bad_thread_model_refuses_the_launch(policies, registry, value):
    policy = copy.deepcopy(policies["boss"])
    policy["thread_models"] = value
    with pytest.raises(LaunchError, match="threads"):
        _plan("boss", render_policy(policy), registry)


def test_an_unknown_space_is_refused_before_the_runtime(registry, capsys):
    assert launch.main(["boss", FOLDER, "cards"], registry=registry, fs=Untouchable()) == 1
    assert "unknown thread space" in capsys.readouterr().err


@pytest.mark.parametrize(
    "effort", [None, "", "High", "ultra", "--dangerously-skip-permissions", 3], ids=repr
)
def test_unknown_effort_is_refused(policies, registry, effort):
    policy = copy.deepcopy(policies["boss"])
    policy["effort"] = effort
    with pytest.raises(LaunchError, match="invalid effort"):
        _plan("boss", render_policy(policy), registry)


@pytest.mark.parametrize(
    "plan",
    [
        LaunchPlan("sonnet", ""),
        LaunchPlan("sonnet", '{"a":"\u00e9"}'),
        LaunchPlan("son\nnet", "{}"),
        LaunchPlan("sonnet", "{}\r"),
        LaunchPlan("sonnet", "{}\0"),
    ],
    ids=["empty settings", "non-ASCII", "newline", "carriage return", "NUL"],
)
def test_plan_that_bash_would_misread_is_refused(plan):
    with pytest.raises(LaunchError):
        render_plan(plan)


# --- the explicit switch (kd.agents.runtime_of; its own tests are in test_kd_agents) ---


def _write(tmp_path: Path, data: Any) -> str:
    path = tmp_path / "agents.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data), "utf-8")
    return str(path)


@pytest.mark.parametrize(
    "value", ["Generated", "legacy", "", True, None, ["generated"]], ids=repr
)
def test_main_refuses_an_unknown_switch_as_the_second_line(tmp_path, capsys, value):
    path = _write(tmp_path, {"boss": {"user": "digi-boss", "runtime": value}})
    assert launch.main(["boss", FOLDER], registry=path, fs=Untouchable()) == 1
    out = capsys.readouterr()
    assert out.out == "" and "agents.json: boss:" in out.err


# --- refusals: files and owners --------------------------------------------------------

FOLDER = f"{AGENTS}/boss"
POLICY_PATH = f"{FOLDER}/.claude/policy.json"
WORK = f"{FOLDER}/work"


def _put(path: str, node: Node | None) -> Callable[[FakeFS], None]:
    def change(fs: FakeFS) -> None:
        if node is None:
            fs.nodes.pop(path, None)
        else:
            fs.nodes[path] = node

    return change


ROOT_DIR, ROOT_FILE = Node("dir", 0, 0o755), Node("file", 0, 0o644)
AGENT_DIR, AGENT_FILE = Node("dir", AGENT_UID, 0o755), Node("file", AGENT_UID, 0o644)
WORK_CLAUDE, WORK_MCP = f"{WORK}/.claude", f"{WORK}/.mcp.json"
FS_REFUSALS = [
    ("/ group-writable", _put("/", Node("dir", 0, 0o775)), "/ must be"),
    ("/srv not root's", _put("/srv", Node("dir", 1000, 0o755)), "/srv must be"),
    ("agents root world-writable", _put(AGENTS, Node("dir", 0, 0o777)), AGENTS),
    ("agents root sticky like /tmp", _put(AGENTS, Node("dir", 0, 0o1777)), AGENTS),
    ("folder is a symlink", _put(FOLDER, Node("link", 0, 0o777)), FOLDER),
    ("folder missing", _put(FOLDER, None), FOLDER),
    ("folder the agent's", _put(FOLDER, Node("dir", AGENT_UID, 0o755)), FOLDER),
    (
        ".claude the agent's",
        _put(f"{FOLDER}/.claude", Node("dir", AGENT_UID, 0o755)),
        ".claude",
    ),
    (".claude missing", _put(f"{FOLDER}/.claude", None), ".claude"),
    (
        ".claude group-writable",
        _put(f"{FOLDER}/.claude", Node("dir", 0, 0o775)),
        ".claude",
    ),
    ("policy missing", _put(POLICY_PATH, None), "is missing"),
    ("policy is a symlink", _put(POLICY_PATH, Node("link", 0, 0o777)), "opened safely"),
    (
        "policy the agent's",
        _put(POLICY_PATH, Node("file", AGENT_UID, 0o644)),
        "root-owned file",
    ),
    (
        "policy group-writable",
        _put(POLICY_PATH, Node("file", 0, 0o664)),
        "root-owned file",
    ),
    (
        "policy world-writable",
        _put(POLICY_PATH, Node("file", 0, 0o646)),
        "root-owned file",
    ),
    ("policy is a directory", _put(POLICY_PATH, ROOT_DIR), "root-owned file"),
    ("policy is a FIFO", _put(POLICY_PATH, Node("other", 0, 0o644)), "root-owned file"),
    ("work missing", _put(WORK, None), "agent's own user"),
    (
        "work is a symlink",
        _put(WORK, Node("link", AGENT_UID, 0o777)),
        "agent's own user",
    ),
    ("work root's", _put(WORK, ROOT_DIR), "agent's own user"),
    ("work another user's", _put(WORK, Node("dir", 1002, 0o755)), "agent's own user"),
    (
        "work group-writable",
        _put(WORK, Node("dir", AGENT_UID, 0o775)),
        "agent's own user",
    ),
    ("work CLAUDE.md missing", _put(f"{WORK}/CLAUDE.md", None), "CLAUDE.md must be"),
    (
        "work CLAUDE.md the agent's",
        _put(f"{WORK}/CLAUDE.md", Node("file", AGENT_UID, 0o644)),
        "CLAUDE.md must be",
    ),
    (
        "work CLAUDE.md writable",
        _put(f"{WORK}/CLAUDE.md", Node("file", 0, 0o666)),
        "CLAUDE.md must be",
    ),
    (
        "work CLAUDE.local.md a symlink",
        _put(f"{WORK}/CLAUDE.local.md", Node("link", 0, 0o777)),
        "CLAUDE.local.md must be",
    ),
    # Claude Code loads project settings, hooks and MCP servers from work/.
    (
        "work .claude the agent's with settings",
        lambda fs: (
            _put(WORK_CLAUDE, AGENT_DIR)(fs),
            _put(f"{WORK_CLAUDE}/.cc-writes", AGENT_DIR)(fs),
            _put(f"{WORK_CLAUDE}/settings.local.json", AGENT_FILE)(fs),
        ),
        ".claude must be absent",
    ),
    (
        "work .claude the agent's, a read-only placeholder with content",
        lambda fs: (
            _put(WORK_CLAUDE, AGENT_DIR)(fs),
            _put(f"{WORK_CLAUDE}/settings.json", Node("file", AGENT_UID, 0o444))(fs),
            fs.data.__setitem__(f"{WORK_CLAUDE}/settings.json", b"{}"),
        ),
        ".claude must be absent",
    ),
    (
        "work .claude the agent's, an empty writable file",
        lambda fs: (
            _put(WORK_CLAUDE, AGENT_DIR)(fs),
            _put(f"{WORK_CLAUDE}/hooks", AGENT_FILE)(fs),
        ),
        ".claude must be absent",
    ),
    (
        "work .claude the agent's, .cc-writes a symlink",
        lambda fs: (
            _put(WORK_CLAUDE, AGENT_DIR)(fs),
            _put(f"{WORK_CLAUDE}/.cc-writes", Node("link", AGENT_UID, 0o777))(fs),
        ),
        ".claude must be absent",
    ),
    (
        "work .claude the agent's, group-writable",
        lambda fs: (
            _put(WORK_CLAUDE, Node("dir", AGENT_UID, 0o775))(fs),
            _put(f"{WORK_CLAUDE}/.cc-writes", AGENT_DIR)(fs),
        ),
        ".claude must be absent",
    ),
    (
        "work .claude another user's",
        lambda fs: (
            _put(WORK_CLAUDE, Node("dir", 4242, 0o755))(fs),
            _put(f"{WORK_CLAUDE}/.cc-writes", Node("dir", 4242, 0o700))(fs),
        ),
        ".claude must be absent",
    ),
    (
        "work .claude a symlink",
        _put(WORK_CLAUDE, Node("link", 0, 0o777)),
        ".claude must",
    ),
    ("work .claude a root file", _put(WORK_CLAUDE, ROOT_FILE), ".claude must"),
    (
        "work .claude group-writable",
        _put(WORK_CLAUDE, Node("dir", 0, 0o775)),
        ".claude must",
    ),
    (
        "work .mcp.json the agent's",
        _put(WORK_MCP, AGENT_FILE),
        ".mcp.json must be absent",
    ),
    (
        "work .mcp.json a symlink",
        _put(WORK_MCP, Node("link", 0, 0o777)),
        ".mcp.json must",
    ),
    (
        "work .mcp.json writable",
        _put(WORK_MCP, Node("file", 0, 0o666)),
        ".mcp.json must",
    ),
    ("work .mcp.json a directory", _put(WORK_MCP, AGENT_DIR), ".mcp.json must"),
    (
        "work .mcp.json the agent's, read-only with content",
        lambda fs: (
            _put(WORK_MCP, Node("file", AGENT_UID, 0o444))(fs),
            fs.data.__setitem__(WORK_MCP, b"{}"),
        ),
        ".mcp.json must be absent",
    ),
]


@pytest.mark.parametrize(
    "change, expected",
    [row[1:] for row in FS_REFUSALS],
    ids=[r[0] for r in FS_REFUSALS],
)
def test_untrusted_install_is_refused(policies, registry, change, expected):
    fs = installed("boss", render_policy(policies["boss"]))
    change(fs)
    with pytest.raises(LaunchError) as error:
        _plan("boss", b"", registry, fs=fs)
    assert expected in str(error.value)


def test_root_owned_project_config_in_work_is_accepted(policies, registry):
    fs = installed("boss", render_policy(policies["boss"]))
    _put(WORK_CLAUDE, ROOT_DIR)(fs)
    _put(WORK_MCP, ROOT_FILE)(fs)
    assert _plan("boss", b"", registry, fs=fs).model == "sonnet"


@pytest.mark.parametrize("with_log", [False, True])
def test_claude_code_runtime_dir_in_work_is_accepted(policies, registry, with_log):
    """Claude Code creates work/.claude/.cc-writes itself on the first session."""
    fs = installed("boss", render_policy(policies["boss"]))
    _put(WORK_CLAUDE, AGENT_DIR)(fs)
    if with_log:
        _put(f"{WORK_CLAUDE}/.cc-writes", Node("dir", AGENT_UID, 0o700))(fs)
    assert _plan("boss", b"", registry, fs=fs).model == "sonnet"


def test_sandbox_mount_points_in_work_are_accepted(policies, registry):
    """While a sandboxed Bash runs, Claude Code holds empty read-only files in work/.claude
    as mount points for its denied paths (seen on stage 11.10.2026); they load nothing."""
    fs = installed("boss", render_policy(policies["boss"]))
    _put(WORK_CLAUDE, AGENT_DIR)(fs)
    _put(f"{WORK_CLAUDE}/.cc-writes", Node("dir", AGENT_UID, 0o700))(fs)
    for name in ("settings.json", "settings.local.json", "hooks", "skills", "agents"):
        _put(f"{WORK_CLAUDE}/{name}", Node("file", AGENT_UID, 0o444))(fs)
    _put(WORK_MCP, Node("file", AGENT_UID, 0o444))(fs)
    assert _plan("boss", b"", registry, fs=fs).model == "sonnet"


def _raw(policy: Policy, edit: Callable[[bytes], bytes]) -> bytes:
    return edit(render_policy(policy))


def _replace_once(raw: bytes, old: bytes, new: bytes) -> bytes:
    assert raw.count(old) == 1, old  # an edit that misses would test nothing
    return raw.replace(old, new)


RAW_REFUSALS = [
    ("empty", lambda raw: b"", "not valid JSON"),
    ("too large", lambda raw: raw + b" " * MAX_POLICY_BYTES, "too large"),
    (
        "not UTF-8",
        lambda raw: raw.replace(b'"boss"', b'"bo\xffss"', 1),
        "not valid JSON",
    ),
    ("BOM", lambda raw: b"\xef\xbb\xbf" + raw, "not valid JSON"),
    ("truncated", lambda raw: raw[:-10], "not valid JSON"),
    (
        "NaN",
        lambda raw: _replace_once(raw, b'"schema_version": %d' % SCHEMA_VERSION, b'"schema_version": NaN'),
        "not valid JSON",
    ),
    (
        "duplicate key",
        lambda raw: raw.replace(b'"agent": "boss"', b'"agent": "x", "agent": "boss"'),
        "rendering",
    ),
    ("CRLF", lambda raw: raw.replace(b"\n", b"\r\n"), "rendering"),
    (
        "reindented",
        lambda raw: json.dumps(json.loads(raw), sort_keys=True).encode(),
        "rendering",
    ),
    ("no final newline", lambda raw: raw.rstrip(b"\n"), "rendering"),
    ("trailing data", lambda raw: raw + b"\n", "rendering"),
]


@pytest.mark.parametrize(
    "edit, expected",
    [row[1:] for row in RAW_REFUSALS],
    ids=[r[0] for r in RAW_REFUSALS],
)
def test_policy_bytes_other_than_the_compilers_are_refused(
    policies, registry, edit, expected
):
    raw = _raw(policies["boss"], edit)
    with pytest.raises(LaunchError) as error:
        _plan("boss", raw, registry)
    assert expected in str(error.value)


# --- refusals: what the policy says ----------------------------------------------------


def _set(*path: Any) -> Callable[[Policy], None]:
    *keys, value = path

    def change(policy: Policy) -> None:
        target = policy
        for key in keys[:-1]:
            target = target[key]
        target[keys[-1]] = value

    return change


def _drop(*keys: Any) -> Callable[[Policy], None]:
    def change(policy: Policy) -> None:
        target = policy
        for key in keys[:-1]:
            target = target[key]
        del target[keys[-1]]

    return change


SANDBOX = ("launch_settings", "sandbox")
NETWORK = (*SANDBOX, "network")
PERMS = ("settings", "permissions")
HOOK = ("settings", "hooks", "PreToolUse", 0, "hooks", 0)
CONTENT_REFUSALS = [
    ("schema 1", _set("schema_version", 1), "schema_version"),
    ("schema as text", _set("schema_version", "1"), "schema_version"),
    ("schema as true", _set("schema_version", True), "schema_version"),
    ("unknown field", _set("env", {"X": "1"}), "policy fields"),
    ("model field missing", _drop("model"), "policy fields"),
    ("another agent", _set("agent", "dev"), "not the policy of boss"),
    ("another user", _set("user", "digi-dev"), "not the policy of boss"),
    ("unknown model", _set("model", "gpt-4"), "invalid model"),
    (
        "model is an option",
        _set("model", "--dangerously-skip-permissions"),
        "invalid model",
    ),
    ("model with an option", _set("model", "sonnet --verbose"), "invalid model"),
    ("model with substitution", _set("model", "sonnet$(id)"), "invalid model"),
    ("model capitalised", _set("model", "Sonnet"), "invalid model"),
    ("model empty", _set("model", ""), "invalid model"),
    ("model not text", _set("model", 4), "invalid model"),
    ("telegram field", _set("telegram", False), "policy fields"),
    ("sudoers field", _set("sudoers", []), "policy fields"),
    ("settings carry a model", _set("settings", "model", "opus"), "settings fields"),
    ("settings carry env", _set("settings", "env", {"A": "1"}), "settings fields"),
    (
        "settings enable plugins",
        _set("settings", "enabledPlugins", {}),
        "settings fields",
    ),
    ("hooks missing", _drop("settings", "hooks"), "settings fields"),
    ("bypass mode", _set(*PERMS, "defaultMode", "bypassPermissions"), "defaultMode"),
    ("accept edits", _set(*PERMS, "defaultMode", "acceptEdits"), "defaultMode"),
    (
        "bypass not disabled",
        _set(*PERMS, "disableBypassPermissionsMode", "enable"),
        "disableBypass",
    ),
    ("auto mode on", _set(*PERMS, "disableAutoMode", "enable"), "disableAutoMode"),
    (
        "bypass key missing",
        _drop(*PERMS, "disableBypassPermissionsMode"),
        "permissions fields",
    ),
    (
        "extra permission field",
        _set(*PERMS, "blockReadsOutsideWorkingDirectories", False),
        "permissions fields",
    ),
    ("allow not a list", _set(*PERMS, "allow", "Bash"), "allow must be"),
    ("deny holds a number", _set(*PERMS, "deny", [5]), "deny must be"),
    (
        "PreToolUse not a list",
        _set("settings", "hooks", "PreToolUse", {}),
        "PreToolUse",
    ),
    ("extra hook event", _set("settings", "hooks", "SessionStart", []), "hooks fields"),
    ("hook type http", _set(*HOOK, "type", "http"), "malformed hook"),
    ("hook command empty", _set(*HOOK, "command", ""), "malformed hook"),
    ("hook timeout true", _set(*HOOK, "timeout", True), "malformed hook"),
    ("hook timeout zero", _set(*HOOK, "timeout", 0), "malformed hook"),
    ("hook extra key", _set(*HOOK, "async", True), "hook fields"),
    ("sandbox off", _set(*SANDBOX, "enabled", False), "enabled"),
    ("sandbox as 1", _set(*SANDBOX, "enabled", 1), "enabled"),
    (
        "runs without sandbox",
        _set(*SANDBOX, "failIfUnavailable", False),
        "failIfUnavailable",
    ),
    (
        "auto-allowed Bash",
        _set(*SANDBOX, "autoAllowBashIfSandboxed", True),
        "autoAllowBash",
    ),
    (
        "unsandboxed retry",
        _set(*SANDBOX, "allowUnsandboxedCommands", True),
        "allowUnsandboxed",
    ),
    (
        "weaker nested",
        _set(*SANDBOX, "enableWeakerNestedSandbox", True),
        "WeakerNested",
    ),
    (
        "weaker network",
        _set(*SANDBOX, "enableWeakerNetworkIsolation", True),
        "WeakerNetwork",
    ),
    ("open network", _set(*NETWORK, "strictAllowlist", False), "strictAllowlist"),
    (
        "docker socket",
        _set(*NETWORK, "allowUnixSockets", ["/run/docker.sock"]),
        "allowUnixSockets",
    ),
    ("sockets without channels", lambda p: (p.__setitem__("channels", []), _set(*NETWORK, "allowAllUnixSockets", True)(p)), "allowAllUnixSockets"),
    ("channels without sockets", _set(*NETWORK, "allowAllUnixSockets", False), "allowAllUnixSockets"),
    ("channels as text", lambda p: p.__setitem__("channels", "self"), "channels must be a list"),
    ("excluded command", _set(*SANDBOX, "excludedCommands", ["kd-nudge *"]), "excludes commands"),
    ("/run visible", lambda p: p["launch_settings"]["sandbox"]["filesystem"]["denyRead"].remove("/run"), "hiding /run"),
    ("local binding", _set(*NETWORK, "allowLocalBinding", True), "allowLocalBinding"),
    ("extra sandbox key", _set(*SANDBOX, "ignoreViolations", {}), "sandbox fields"),
    ("filesystem missing", _drop(*SANDBOX, "filesystem"), "sandbox fields"),
    (
        "allowWrite added",
        _set(*SANDBOX, "filesystem", "allowWrite", ["/"]),
        "filesystem fields",
    ),
    ("domains as text", _set(*NETWORK, "allowedDomains", "*"), "allowedDomains"),
    (
        "excluded holds a number",
        _set(*SANDBOX, "excludedCommands", [1]),
        "excludedCommands",
    ),
    (
        "extra launch setting",
        _set("launch_settings", "env", {}),
        "launch settings fields",
    ),
]


@pytest.mark.parametrize(
    "change, expected",
    [row[1:] for row in CONTENT_REFUSALS],
    ids=[r[0] for r in CONTENT_REFUSALS],
)
def test_broken_or_weaker_policy_is_refused(policies, registry, change, expected):
    policy = copy.deepcopy(policies["boss"])
    change(policy)
    with pytest.raises(LaunchError) as error:
        _plan("boss", render_policy(policy), registry)
    assert expected in str(error.value)


def test_check_compiled_refuses_a_non_object():
    with pytest.raises(LaunchError, match="policy fields"):
        check_compiled([], name="boss", user="digi-boss")


@pytest.mark.parametrize(
    "folder, root",
    [
        ("/srv/other/boss", AGENTS),
        (f"{AGENTS}/dev", AGENTS),
        ("/srv//digipaddock-agents/boss", "/srv//digipaddock-agents"),
        ("srv/digipaddock-agents/boss", "srv/digipaddock-agents"),
        ("/srv/x/../digipaddock-agents/boss", "/srv/x/../digipaddock-agents"),
    ],
)
def test_folder_outside_the_configured_root_is_refused(
    policies, registry, folder, root
):
    with pytest.raises(LaunchError, match="agents root"):
        plan_generated(
            "boss",
            folder,
            uid=AGENT_UID,
            user="digi-boss",
            agents_root=root,
            fs=installed("boss", render_policy(policies["boss"])),
            registry=registry,
        )


def test_user_other_than_the_registrys_is_refused(policies, registry):
    with pytest.raises(LaunchError, match="must run as"):
        _plan("boss", render_policy(policies["boss"]), registry, user="digi-dev")


def test_unregistered_agent_is_refused(policies, tmp_path):
    # Without an entry kd.agents names the shared user agent, not the policy's.
    path = _write(tmp_path, {"dev": SWITCHED["dev"]})
    with pytest.raises(LaunchError, match="must run as"):
        _plan("boss", render_policy(policies["boss"]), path)


# --- main: the entry agent-launch calls ------------------------------------------------


def _forbidden(*args: Any) -> Any:
    raise AssertionError("a refused launch must not look at the generated runtime")


class Untouchable:
    node = read = staticmethod(_forbidden)


def test_main_refuses_a_legacy_agent(tmp_path, capsys):
    # no legacy launch is left; an entry without the switch starts nothing.
    registry = _write(tmp_path, {"dev": {"user": "digi-dev"}})
    code = launch.main(
        ["dev", f"{AGENTS}/dev"], registry=registry, fs=Untouchable(), identity=_forbidden
    )
    out = capsys.readouterr()
    assert (code, out.out) == (1, "")
    assert "dev is not a generated agent" in out.err


def test_main_generated_prints_the_plan(policies, registry, capsys):
    policy = policies["boss"]
    code = launch.main(
        ["boss", FOLDER],
        registry=registry,
        fs=installed("boss", render_policy(policy)),
        identity=lambda: (AGENT_UID, "digi-boss"),
        agents_root=AGENTS,
    )
    out = capsys.readouterr()
    assert (code, out.err) == (0, "")
    assert out.out == f"generated\nsonnet\n\n{_merged(policy)}\n"


def test_main_refusal_prints_nothing_to_run_and_no_policy_text(
    policies, registry, capsys
):
    policy = copy.deepcopy(policies["boss"])
    policy["settings"]["permissions"]["allow"].append("Read(//srv/TOPSECRET-MARKER)")
    policy["launch_settings"]["sandbox"]["enabled"] = False
    code = launch.main(
        ["boss", FOLDER],
        registry=registry,
        fs=installed("boss", render_policy(policy)),
        identity=lambda: (AGENT_UID, "digi-boss"),
        agents_root=AGENTS,
    )
    out = capsys.readouterr()
    assert code == 1 and out.out == ""
    assert out.err.startswith("agent-launch: compiled sandbox is not fail-closed")
    assert "TOPSECRET" not in out.err


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["boss"],
        ["boss", FOLDER, "yes"],
        ["boss", FOLDER, "no", "x"],
    ],
)
def test_main_refuses_other_arguments(registry, capsys, argv):
    assert launch.main(argv, registry=registry, fs=Untouchable()) == 1
    assert capsys.readouterr().out == ""


def test_main_reports_a_broken_registry(tmp_path, capsys):
    assert launch.main(["boss", FOLDER], registry=_write(tmp_path, "[]")) == 1
    out = capsys.readouterr()
    assert out.out == "" and out.err.startswith("agent-launch: agents.json")


def test_configured_agents_root_comes_from_the_resource_table(tmp_path):
    assert launch.configured_agents_root(ROOT) == AGENTS
    with pytest.raises(LaunchError, match="agent-resources.json"):
        launch.configured_agents_root(tmp_path)
    table = tmp_path / "common" / "agent-resources.json"
    table.parent.mkdir()
    for layout in ({"agents": "relative"}, {"agents": "/srv//x"}, {"code": "/opt"}, []):
        table.write_text(json.dumps({"layout": layout}), "utf-8")
        with pytest.raises(LaunchError, match="layout.agents"):
            launch.configured_agents_root(tmp_path)


# --- the real filesystem ----------------------------------------------------------------


def test_host_filesystem_reads_a_bounded_regular_file(tmp_path):
    path = tmp_path / "policy.json"
    path.write_bytes(b"x" * 10)
    fs = launch.HostFilesystem()
    node, data = fs.read(str(path), 4)
    assert (
        node.kind == "file" and data == b"xxxxx"
    )  # limit + 1: enough to see too large
    assert fs.node(str(tmp_path / "missing")) is None
    assert fs.node(str(tmp_path)).kind == "dir"
    with pytest.raises(FileNotFoundError):
        fs.read(str(tmp_path / "missing"), 4)


@pytest.mark.skipif(os.name != "posix", reason="symlinks and FIFOs: POSIX only")
def test_host_filesystem_never_follows_a_symlink_or_blocks_on_a_fifo(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("{}", "utf-8")
    (tmp_path / "link.json").symlink_to(target)
    fs = launch.HostFilesystem()
    assert fs.node(str(tmp_path / "link.json")).kind == "link"
    with pytest.raises(OSError):
        fs.read(str(tmp_path / "link.json"), 100)
    os.mkfifo(tmp_path / "fifo")
    node, data = fs.read(str(tmp_path / "fifo"), 100)
    assert (node.kind, data) == ("other", b"")


# --- the bash launcher ------------------------------------------------------------------


def _find_bash() -> str | None:
    """A working bash; on Windows Git Bash (System32 bash without WSL is no use)."""
    for candidate in (r"C:\Program Files\Git\bin\bash.exe", shutil.which("bash")):
        if candidate and Path(candidate).exists():
            try:
                done = subprocess.run([candidate, "-c", "true"], timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if done.returncode == 0:
                return candidate
    return None


BASH = _find_bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="no working bash")
LAUNCHER = COMMON / "bin" / "agent-launch"


def _shell_path(path: Path) -> str:
    """A path inside PATH-like lists: Git Bash needs /c/..., a colon would split it."""
    text = path.as_posix()
    if os.name == "nt" and len(text) > 2 and text[1] == ":":
        return f"/{text[0].lower()}{text[2:]}"
    return text


# Stand-ins: id and stat are functions in the script; sudo and claude record argv and env.
FUNCTIONS = (
    "id() { [[ ${1-} == -un ]] && printf '%s\\n' \"$KD_TEST_ID\"; }\n"
    "stat() { printf '%s\\n' \"$KD_TEST_VENV_UID\"; }\n"
)
RECORDER = (
    "#!/bin/bash\n"
    'printf "%s\\0" "$(pwd -P)" "$@" > "$KD_TEST_OUT/{tool}.argv"\n'
    'env -0 > "$KD_TEST_OUT/{tool}.env"\n'
    # what claude reads from its descriptors
    'for v in CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR; do\n'
    '  [[ -z ${!v-} ]] || cat <&"${!v}" > "$KD_TEST_OUT/{tool}.$v"\n'
    'done\n'
)
# Windows Python writes CRLF and a legacy code page; on the stage host python3 gives the
# launcher LF and UTF-8. Git Bash's /c/... arguments ($HOME) reach it as C:/...
PYTHON = (
    '#!/bin/bash\nset -o pipefail\nargs=()\nfor a in "$@"; do\n'
    '  if [[ $a == /[a-z]/* ]] && command -v cygpath >/dev/null; then a=$(cygpath -m "$a"); fi\n'
    '  args+=("$a")\ndone\n'
    '"$KD_TEST_PYTHON" -X utf8 "${args[@]}" | tr -d \'\\r\'\n'
)
# A deploy fault in kd.launch: the launch stops, nothing falls back.
BROKEN_LAUNCH = "def main(:  # deliberately broken\n"
PLAN_REPLAY = '''"""Stand-in for kd.launch: records its argv and replays the plan of the test."""
import json
import os
import sys


def main(argv):
    with open(os.environ["KD_TEST_PYOUT"] + "/launch.json", "w", encoding="utf-8") as f:
        json.dump(list(argv), f)
    with open(os.environ["KD_TEST_PLAN"], encoding="utf-8", newline="") as f:
        sys.stdout.write(f.read())
    return int(os.environ["KD_TEST_PLAN_RC"])
'''


@dataclass
class Run:
    code: int
    stderr: str
    claude: list[str] | None  # cwd, then argv
    claude_env: dict[str, str] | None
    sudo: list[str] | None


class Launcher:
    """agent-launch under test: host paths rewritten into one temporary tree."""

    def __init__(self, tmp: Path, *, launch: str = "real") -> None:
        self.tmp = tmp.resolve()
        self.bin, self.home = self.tmp / "bin", self.tmp / "home"
        self.common, self.srv = self.tmp / "common", self.tmp / "srv"
        self.venv, self.out = self.tmp / "venv", self.tmp / "out"
        self.agent_bin = self.tmp / "agent-bin"
        self.registry, self.plan = self.tmp / "agents.json", self.tmp / "plan.txt"
        (self.home / ".local" / "bin").mkdir(parents=True)
        self.bin.mkdir()
        for path, text in (
            (self.bin / "python3", PYTHON),
            (self.bin / "sudo", RECORDER.replace("{tool}", "sudo")),
            (
                self.home / ".local" / "bin" / "claude",
                RECORDER.replace("{tool}", "claude"),
            ),
        ):
            path.write_text(text, encoding="utf-8", newline="\n")
            path.chmod(0o755)
        shutil.copytree(
            COMMON / "kd",
            self.common / "kd",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        shutil.copyfile(
            COMMON / "naming.default.json", self.common / "naming.default.json"
        )
        # a release tree carries its host values: the launcher's clean environment has no KD_HOST_JSON
        (self.tmp / "host").mkdir()
        shutil.copyfile(
            Path(__file__).resolve().parent / "fixtures" / "host" / "host.json",
            self.tmp / "host" / "host.json",
        )
        # the default names, whatever host runs the tests (a factory host has its own naming.json)
        naming_py = self.common / "kd" / "naming.py"
        line = 'CONFIG = "/etc/digipaddock/naming.json"'
        assert line in naming_py.read_text("utf-8")
        naming_py.write_text(
            naming_py.read_text("utf-8").replace(line, f'CONFIG = "{(self.tmp / "naming.json").as_posix()}"'), "utf-8"
        )
        agents_py = self.common / "kd" / "agents.py"
        text = agents_py.read_text("utf-8")
        line = 'REGISTRY = "/etc/digipaddock/agents.json"'
        assert line in text
        agents_py.write_text(
            text.replace(line, f'REGISTRY = "{self.registry.as_posix()}"'), "utf-8"
        )
        stand_in = {"replay": PLAN_REPLAY, "broken": BROKEN_LAUNCH}
        if launch in stand_in:
            (self.common / "kd" / "launch.py").write_text(stand_in[launch], "utf-8")
        else:
            # The real module reads the agents root from the resource table beside it.
            assert launch == "real", launch
            table = json.loads((COMMON / "agent-resources.json").read_text("utf-8"))
            table["layout"]["agents"] = self.srv.as_posix()
            target = self.common / "agent-resources.json"
            target.write_text(json.dumps(table), "utf-8")

    def folder(self, name: str) -> Path:
        return self.srv / name

    def reset(self) -> None:
        for path in (self.srv, self.venv, self.agent_bin, self.out, self.home / ".claude"):
            shutil.rmtree(path, ignore_errors=True)
        self.out.mkdir()
        self.registry.unlink(missing_ok=True)

    def script(self, text: str) -> str:
        for old, new in (
            ("/opt/digipaddock-venv", _shell_path(self.venv)),
            ("/opt/digipaddock/common", self.common.as_posix()),
            ("/srv/digipaddock-agents", self.srv.as_posix()),
            ("/usr/bin/python3", _shell_path(self.bin / "python3")),
        ):
            assert old in text, old
            text = text.replace(old, new)
        assert "/etc/digipaddock/agent-bin" in text
        text = text.replace("/etc/digipaddock/agent-bin", _shell_path(self.agent_bin))
        return FUNCTIONS + text

    def run(
        self,
        source: Path,
        name: str,
        *,
        user: str,
        venv_uid: str = "1001",
        plan_rc: int = 0,
        args: tuple = ("--session",),
        extra_env: dict | None = None,
    ) -> Run:
        script = self.write(source)
        proc = subprocess.run(
            [BASH, str(script), name, *args],
            env={**self.env(user, venv_uid, plan_rc), **(extra_env or {})},
            capture_output=True,
            timeout=120,
        )
        return Run(
            proc.returncode,
            proc.stderr.decode("utf-8", "replace"),
            self._list("claude.argv"),
            self._env("claude.env"),
            self._list("sudo.argv"),
        )

    def write(self, source: Path) -> Path:
        script = self.tmp / "agent-launch.sh"
        script.write_text(self.script(source.read_text("utf-8")), "utf-8", newline="\n")
        return script

    def env(self, user: str, venv_uid: str = "1001", plan_rc: int = 0) -> dict[str, str]:
        env = {
            "PATH": f"{_shell_path(self.bin)}:/usr/bin:/bin",
            "HOME": _shell_path(self.home),
            "LANG": "C.UTF-8",
            # Git Bash must not rewrite arguments: the JSON settings start with no path.
            "MSYS_NO_PATHCONV": "1",
            "MSYS2_ARG_CONV_EXCL": "*",
            "KD_TEST_PYTHON": _shell_path(Path(sys.executable)),
            "KD_TEST_OUT": _shell_path(self.out),
            "KD_TEST_PYOUT": self.out.as_posix(),
            "KD_TEST_PLAN": self.plan.as_posix(),
            "KD_TEST_PLAN_RC": str(plan_rc),
            "KD_TEST_ID": user,
            "KD_TEST_VENV_UID": venv_uid,
        }
        for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP"):  # Python on Windows
            if key in os.environ:
                env[key] = os.environ[key]
        return env

    def _list(self, name: str) -> list[str] | None:
        path = self.out / name
        if not path.exists():
            return None
        return [item.decode("utf-8") for item in path.read_bytes().split(b"\0")[:-1]]

    def _env(self, name: str) -> dict[str, str] | None:
        items = self._list(name)
        return None if items is None else dict(item.split("=", 1) for item in items)

    # --- what a scenario puts on the host ---

    def write_registry(self, data: Any) -> None:
        text = data if isinstance(data, str) else json.dumps(data)
        self.registry.write_text(text, "utf-8")

    def legacy_strict(self, name: str, *, conflict: bool = False) -> None:
        """Settings files a pre-manifest install left beside work/: nothing may merge them."""
        claude = self.folder(name) / ".claude"
        claude.mkdir(parents=True)
        snapshot = json.loads(
            (ROOT / "common/tests/fixtures/policy/dev.policy.json").read_text("utf-8")
        )
        (claude / "settings.json").write_text(json.dumps(snapshot["settings"]), "utf-8")
        launch_settings = snapshot["launch_settings"]
        if conflict:
            launch_settings["permissions"] = {}
        (claude / "launch-settings.json").write_text(
            json.dumps(launch_settings), "utf-8"
        )
        (self.folder(name) / "work").mkdir()

    def stale_channel(self, name: str) -> None:
        """A Telegram channel an install before the change left in the home: nothing reads it."""
        state = self.home / ".claude" / "channels" / f"tg-{name}"
        state.mkdir(parents=True)
        (state / ".env").write_text("TELEGRAM_BOT_TOKEN=fixture\n", "utf-8")


# --- generated: the plan reaches claude as plain argv ----------------------------------


def _switched(h: Launcher, name: str) -> None:
    h.write_registry({name: SWITCHED[name]})
    # Legacy settings that a fallback would merge (and refuse on the key conflict).
    h.legacy_strict(name, conflict=True)


def _replay(h: Launcher, text: str) -> None:
    h.plan.write_text(text, encoding="utf-8", newline="")


@needs_bash
@pytest.mark.parametrize("owner_uid, first", [("0", True), ("1001", False)])
def test_root_owned_agent_bin_comes_first_in_path(tmp_path, policies, owner_uid, first):
    # The channel client links shadow the wrappers, but only from a root directory.
    h = Launcher(tmp_path, launch="replay")
    h.reset()
    _switched(h, "boss")
    (h.agent_bin / "boss").mkdir(parents=True)
    _replay(h, render_plan(LaunchPlan("sonnet", _merged(policies["boss"]))))
    run = h.run(LAUNCHER, "boss", user="digi-boss", venv_uid=owner_uid)
    assert run.code == 0, run.stderr
    head = run.claude_env["PATH"].split(":")[0]
    assert (head == _shell_path(h.agent_bin / "boss")) is first


@needs_bash
def test_generated_developer_keeps_platform_model(tmp_path, policies):
    h = Launcher(tmp_path, launch="replay")
    h.reset()
    _switched(h, "dev")
    plan = LaunchPlan(None, _merged(policies["dev"]))
    _replay(h, render_plan(plan))
    run = h.run(LAUNCHER, "dev", user="digi-dev")
    assert run.code == 0, run.stderr
    folder = h.folder("dev").as_posix()
    assert json.loads((h.out / "launch.json").read_text("utf-8")) == ["dev", folder, "chat"]
    cwd, *argv = run.claude
    assert cwd.endswith("/srv/dev/work")
    assert argv[:4] == ["--settings", plan.settings, "--add-dir", folder]
    assert argv[4:10] == STREAM and "--model" not in argv


@needs_bash
def test_refused_runtime_starts_nothing(tmp_path):
    h = Launcher(tmp_path, launch="replay")
    h.reset()
    _switched(h, "boss")
    _replay(h, "")
    run = h.run(LAUNCHER, "boss", user="digi-boss", plan_rc=1)
    assert (run.code, run.claude, run.sudo) == (1, None, None)
    assert "failed its check" in run.stderr


@needs_bash
@pytest.mark.parametrize(
    "text",
    [
        "",
        "\n",
        "generated\n",
        "generated\nsonnet\n",
        "generated\nsonnet\n\n",
        "generated\nsonnet\n{}\n",
        "generated\nsonnet\n\n\n",
        "generated\nsonnet\n{}\nextra\n",
        "generated\nsonnet\n\n{}\nextra\n",
        "generated\nsonnet\nHigh\n{}\n",
        "generated\nsonnet\n--verbose\n{}\n",
        "generated\nsonnet\nhigh \n{}\n",
        "legacy\n",
        "legacy\nextra\n",
        "LEGACY\n",
        " legacy\n",
        "bogus\n",
    ],
    ids=repr,
)
def test_malformed_plan_starts_nothing(tmp_path, text):
    h = Launcher(tmp_path, launch="replay")
    h.reset()
    _switched(h, "boss")
    _replay(h, text)
    run = h.run(LAUNCHER, "boss", user="digi-boss")
    assert (run.code, run.claude, run.sudo) == (1, None, None)
    assert "unrecognized launch plan" in run.stderr


@needs_bash
def test_plan_values_are_never_evaluated(tmp_path):
    # kd.launch never prints such values; the launcher must not evaluate them anyway.
    h = Launcher(tmp_path, launch="replay")
    h.reset()
    _switched(h, "boss")
    model = "$(touch pwned-model)`touch pwned-tick`"
    settings = '{"x":"$(touch pwned-settings) ${HOME} `touch pwned-tick2`"}'
    _replay(h, f"generated\n{model}\n\n{settings}\n")
    run = h.run(LAUNCHER, "boss", user="digi-boss")
    assert run.code == 0, run.stderr
    argv = run.claude[1:]
    assert argv[argv.index("--model") + 1] == model
    given = argv[argv.index("--settings") + 1]
    assert json.loads(given)["x"] == json.loads(settings)["x"]
    assert list(h.tmp.rglob("pwned*")) == []


@needs_bash
def test_broken_launch_module_starts_nothing(tmp_path):
    h = Launcher(tmp_path, launch="broken")
    h.reset()
    _switched(h, "boss")
    run = h.run(LAUNCHER, "boss", user="digi-boss")
    assert (run.code, run.claude, run.sudo) == (1, None, None)
    assert "SyntaxError" in run.stderr and "failed its check" in run.stderr


# --- generated with the real kd.launch: no fallback ------------------------------------


@needs_bash
@pytest.mark.skipif(os.name != "posix", reason="host identity (pwd) and owners: POSIX")
def test_switched_agent_without_a_trusted_policy_never_falls_back(tmp_path):
    # Legacy settings and work/ are there to fall back to; the launcher stops anyway.
    # A test tree is not root's, so the trust check refuses it: the directory chain, or
    # under root (whose temporary tree may pass the chain) the missing compiled policy.
    h = Launcher(tmp_path)
    h.reset()
    h.write_registry({"boss": SWITCHED["boss"]})
    h.legacy_strict("boss")
    run = h.run(LAUNCHER, "boss", user="digi-boss")
    assert (run.code, run.claude, run.sudo) == (1, None, None)
    reason = r"agent-launch: (\S+ must be a root-owned directory|compiled policy \S+ is missing)"
    assert re.search(reason, run.stderr), run.stderr
    assert "failed its check" in run.stderr


@needs_bash
def test_unknown_runtime_value_stops_the_launcher_before_kd_launch(tmp_path):
    h = Launcher(tmp_path, launch="broken")
    h.reset()
    h.write_registry({"dev": {"user": "digi-dev", "runtime": "legacy"}})
    h.legacy_strict("dev")
    run = h.run(LAUNCHER, "dev", user="digi-dev")
    assert (run.code, run.claude, run.sudo) == (1, None, None)
    assert "agents.json: dev: неизвестный runtime" in run.stderr
    assert "is not readable" in run.stderr and "SyntaxError" not in run.stderr


# --- --session, the headless launch of the session host ---------------------------------------------
RESUME = "0b9c3f1e-1111-2222-3333-444455556666"
STREAM = ["-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]


def _session_run(tmp_path, policies, *args, user="digi-boss", stale_channel=False, role_config=None,
                 extra_env=None, plan_env=None):
    h = Launcher(tmp_path, launch="replay")
    h.reset()
    _switched(h, "boss")
    if stale_channel:
        h.stale_channel("boss")
    if role_config is not None:  # the agent's Hindsight config, as hindsight_client_for writes it
        (h.home / ".hindsight").mkdir(parents=True, exist_ok=True)
        (h.home / ".hindsight" / "coding-agent.json").write_text(role_config, "utf-8")
    settings = _merged(policies["boss"])
    if plan_env is not None:  # what kd.launch adds for a session with an exit
        settings = json.dumps({**json.loads(settings), "env": plan_env}, sort_keys=True, separators=(",", ":"))
    plan = LaunchPlan("sonnet", settings)
    _replay(h, render_plan(plan))
    return h, plan, h.run(LAUNCHER, "boss", user=user, args=args, extra_env=extra_env)


@needs_bash
def test_session_mode_execs_stream_json_with_model_and_strict_settings(tmp_path, policies):
    h, plan, run = _session_run(tmp_path, policies, "--session")
    assert run.code == 0, run.stderr
    folder = h.folder("boss").as_posix()
    assert json.loads((h.out / "launch.json").read_text("utf-8")) == ["boss", folder, "chat"]
    cwd, *argv = run.claude
    assert cwd.endswith("/srv/boss/work")
    assert argv[:6] == ["--settings", plan.settings, "--add-dir", folder, "--model", "sonnet"]
    assert json.loads(argv[1]) == json.loads(plan.settings)  # no plugin merged in
    assert argv[6:12] == STREAM and argv[12] == "--append-system-prompt"
    assert "cabinet" in argv[13] and "kd-work ask" in argv[13] and len(argv) == 14
    assert "--effort" not in argv
    env = run.claude_env
    assert env["KD_AGENT"] == "boss" and env["DISABLE_AUTOUPDATER"] == "1"
    assert env["CLAUDE_CODE_ENABLE_AWAY_SUMMARY"] == "0"
    # The scrub leaves sandbox placeholders in work/ the next launch refuses; sandbox.credentials
    # keeps the token out of Bash instead
    assert "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB" not in env
    assert env["BASH_DEFAULT_TIMEOUT_MS"] == env["BASH_MAX_TIMEOUT_MS"] == "660000"
    assert run.sudo is None


TOKEN_FD, API_KEY_FD = "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR", "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR"


@needs_bash
@pytest.mark.skipif(os.name != "posix", reason="a pipe across exec: POSIX only")
def test_session_token_reaches_claude_only_through_a_descriptor(tmp_path, policies):
    # The unit's EnvironmentFile put the host's token in the environment; claude gets a pipe instead
    h, _, run = _session_run(tmp_path, policies, "--session", extra_env={"CLAUDE_CODE_OAUTH_TOKEN": "tok-fixture"})
    assert run.code == 0, run.stderr
    env = run.claude_env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env and "tok-fixture" not in "".join(env.values())
    assert (h.out / f"claude.{TOKEN_FD}").read_text("utf-8") == "tok-fixture"
    assert API_KEY_FD not in env


@needs_bash
@pytest.mark.skipif(os.name != "posix", reason="a pipe across exec: POSIX only")
def test_session_provider_keys_go_by_the_api_keys_descriptor(tmp_path, policies):
    # Provider kinds: ANTHROPIC_API_KEY (anthropic) and ANTHROPIC_AUTH_TOKEN (the others; the owner's decision on the
    # card) both reach claude by CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR
    h, _, run = _session_run(tmp_path, policies, "--session", extra_env={"ANTHROPIC_API_KEY": "key-fixture"})
    assert run.code == 0, run.stderr
    assert "ANTHROPIC_API_KEY" not in run.claude_env
    assert (h.out / f"claude.{API_KEY_FD}").read_text("utf-8") == "key-fixture"
    (tmp_path / "bearer").mkdir()
    h, _, run = _session_run(tmp_path / "bearer", policies, "--session",
                             extra_env={"ANTHROPIC_AUTH_TOKEN": "bearer-fixture", "ANTHROPIC_API_KEY": ""})
    assert run.code == 0, run.stderr
    assert "ANTHROPIC_AUTH_TOKEN" not in run.claude_env and "ANTHROPIC_API_KEY" not in run.claude_env
    assert (h.out / f"claude.{API_KEY_FD}").read_text("utf-8") == "bearer-fixture"
    assert TOKEN_FD not in run.claude_env


@needs_bash
def test_session_without_a_token_has_no_descriptor(tmp_path, policies):
    # AC 6: no token file — the stored login, neither a descriptor variable nor an empty pipe
    h, _, run = _session_run(tmp_path, policies, "--session")
    assert run.code == 0, run.stderr
    assert TOKEN_FD not in run.claude_env and API_KEY_FD not in run.claude_env
    assert not list(h.out.glob("claude.*_FILE_DESCRIPTOR"))


@needs_bash
def test_session_mode_passes_the_compiled_effort(tmp_path, policies):
    # the effort of the compiled policy becomes --effort right after --model.
    h = Launcher(tmp_path, launch="replay")
    h.reset()
    _switched(h, "boss")
    plan = LaunchPlan("opus", _merged(policies["boss"]), "high")
    _replay(h, render_plan(plan))
    run = h.run(LAUNCHER, "boss", user="digi-boss")
    assert run.code == 0, run.stderr
    _, *argv = run.claude
    assert argv[:8] == [
        "--settings", plan.settings, "--add-dir", h.folder("boss").as_posix(),
        "--model", "opus", "--effort", "high",
    ]
    assert argv[8:14] == STREAM and argv.count("--effort") == 1


@needs_bash
def test_session_launch_has_no_telegram_env(tmp_path, policies):
    # A channel an older install left in the home is not read: no token, no access mode, no plugin.
    _, _, run = _session_run(tmp_path, policies, "--session", stale_channel=True)
    assert run.code == 0, run.stderr
    assert not [key for key in run.claude_env if key.startswith("TELEGRAM_")]
    assert "--channels" not in run.claude and not any("telegram" in arg for arg in run.claude)


@needs_bash
@pytest.mark.parametrize("args", [(), ("--panel",), ("",)], ids=["none", "panel", "empty"])
def test_launch_without_session_is_refused(tmp_path, policies, args):
    # the panel launch is gone; only the session host starts an agent.
    _, _, run = _session_run(tmp_path, policies, *args)
    assert (run.code, run.claude, run.sudo) == (2, None, None)
    assert ("usage: agent-launch <name> --session [--context <session> <thread> <card|chat|monitor>"
            " [--memory <bank>] [--model <upstream>] [--effort <level>] [--egress <exit>]] [<resume id>]") in run.stderr


@needs_bash
def test_session_mode_with_resume(tmp_path, policies):
    _, _, run = _session_run(tmp_path, policies, "--session", RESUME)
    assert run.code == 0, run.stderr
    assert run.claude[-2:] == ["--resume", RESUME]


@needs_bash
@pytest.mark.parametrize("args", [("--session", "--dangerously-skip-permissions"), ("--session", RESUME, "x"),
                                  ("--other",), ("--session", RESUME.upper())])
def test_session_mode_bad_arguments_refused(tmp_path, policies, args):
    _, _, run = _session_run(tmp_path, policies, *args)
    assert run.code == 2 and run.claude is None and run.sudo is None


SESSION_ID = "a" * 32


# --- the card's model and effort beat the plan's ----------------------------------------------------
@needs_bash
def test_session_model_and_effort_of_the_card_beat_the_plans(tmp_path, policies):
    _, plan, run = _session_run(tmp_path, policies, "--session", "--context", SESSION_ID, "7", "KD-7",
                                "--model", "openai/gpt-5", "--effort", "xhigh", RESUME)
    assert run.code == 0, run.stderr
    _, *argv = run.claude
    assert argv[:8] == ["--settings", plan.settings, "--add-dir", argv[3], "--model", "openai/gpt-5",
                        "--effort", "xhigh"]
    assert argv.count("--model") == 1 and argv.count("--effort") == 1 and argv[-2:] == ["--resume", RESUME]
    assert run.claude_env["KD_SESSION_CARD"] == "KD-7"


@needs_bash
def test_session_model_alone_keeps_the_plans_effort_off(tmp_path, policies):
    _, _, run = _session_run(tmp_path, policies, "--session", "--context", SESSION_ID, "7", "KD-7",
                             "--memory", "coding-agent::x", "--model", "accounts/fireworks/models/x[1m]")
    assert run.code == 0, run.stderr
    _, *argv = run.claude
    assert argv[4:6] == ["--model", "accounts/fireworks/models/x[1m]"] and "--effort" not in argv


@needs_bash
@pytest.mark.parametrize("args", [
    ("--context", SESSION_ID, "7", "KD-7", "--model", "a b"),
    ("--context", SESSION_ID, "7", "KD-7", "--model", "-x"),
    ("--context", SESSION_ID, "7", "KD-7", "--effort", "ultra"),
    ("--context", SESSION_ID, "7", "chat", "--model", "openai/gpt-5"),
    ("--context", SESSION_ID, "7", "chat", "--effort", "high"),
    ("--model", "openai/gpt-5"),
    ("--context", SESSION_ID, "7", "KD-7", "--model"),
    ("--context", SESSION_ID, "7", "KD-7", "--effort", "high", "--model", "openai/gpt-5"),
], ids=["space", "dash", "level", "chat-model", "chat-effort", "no-context", "no-value", "order"])
def test_session_bad_model_or_effort_refused(tmp_path, policies, args):
    _, _, run = _session_run(tmp_path, policies, "--session", *args)
    assert run.code == 2 and run.claude is None


def _note(run: Run) -> str:
    argv = run.claude[1:]
    return argv[argv.index("--append-system-prompt") + 1]


@needs_bash
def test_session_context_of_a_card_reaches_the_note_and_the_environment(tmp_path, policies):
    # the host's checked ids name the session's card and thread; resume still comes last.
    _, _, run = _session_run(tmp_path, policies, "--session", "--context", SESSION_ID, "7", "KD-9045", RESUME)
    assert run.code == 0, run.stderr
    assert run.claude[-2:] == ["--resume", RESUME]
    note = _note(run)
    assert f"This session ({SESSION_ID}, thread 7) is boss's session of card KD-9045" in note
    assert "kd-task view KD-9045" in note and "Never start or continue another card's work here" in note
    assert "cross-card operations your instructions allow" in note  # BOSS's coordination stays
    env = run.claude_env
    assert (env["KD_SESSION_ID"], env["KD_SESSION_THREAD"], env["KD_SESSION_CARD"]) == (SESSION_ID, "7", "KD-9045")


@needs_bash
def test_session_context_of_the_chat_says_it_is_no_cards(tmp_path, policies):
    _, _, run = _session_run(tmp_path, policies, "--session", "--context", SESSION_ID, "12", "chat")
    assert run.code == 0, run.stderr
    note = _note(run)
    assert f"This session ({SESSION_ID}, thread 12) is boss's cabinet chat" in note
    assert "route work into cards" in note and "of card" not in note
    env = run.claude_env
    assert (env["KD_SESSION_ID"], env["KD_SESSION_THREAD"], env["KD_SESSION_CARD"]) == (SESSION_ID, "12", "")


@needs_bash
def test_session_context_of_the_monitor_names_its_thread(tmp_path, policies):
    """The "Monitor" session is told it is that thread, neither the chat nor a card."""
    h, _, run = _session_run(tmp_path, policies, "--session", "--context", SESSION_ID, "13", "monitor")
    assert run.code == 0, run.stderr
    # the plan is asked for the thread: its own model, if the policy names one (AC 2)
    assert json.loads((h.out / "launch.json").read_text("utf-8")) == ["boss", h.folder("boss").as_posix(), "monitor"]
    note = _note(run)
    assert f"This session ({SESSION_ID}, thread 13) is boss's «Монитор» thread" in note
    assert "kd-task chat reads and answers this thread" in note and "cabinet chat, not a card" not in note
    env = run.claude_env
    assert (env["KD_SESSION_THREAD"], env["KD_SESSION_CARD"], env["KD_SESSION_SPACE"]) == ("13", "", "monitor")


BANK = "coding-agent::_Shop.v2"
ROLE_CONFIG = json.dumps({"apiToken": "t0ken", "mapPathToBank": {"/srv/boss": "coding-agent::boss"}})


@needs_bash
def test_session_of_a_bound_card_points_hindsight_at_its_projects_bank(tmp_path, policies):
    """--memory after a card's context — the session's plugin reads a config pinned to that bank."""
    h, _, run = _session_run(tmp_path, policies, "--session", "--context", SESSION_ID, "7", "KD-9045",
                             "--memory", BANK, RESUME, role_config=ROLE_CONFIG)
    assert run.code == 0, run.stderr
    assert run.claude[-2:] == ["--resume", RESUME]
    pinned = h.home / ".hindsight" / "bank-coding-agent%3A%3A_Shop.v2.json"
    data = json.loads(pinned.read_text("utf-8"))
    assert data == {"apiToken": "t0ken", "bankId": BANK}
    assert run.claude_env["HINDSIGHT_CONFIG"].endswith("/.hindsight/bank-coding-agent%3A%3A_Shop.v2.json")
    assert f"Your Hindsight memory in this session is {BANK}" in _note(run)


@needs_bash
def test_a_bound_card_without_hindsight_launches_on_nothing_new(tmp_path, policies):
    h, _, run = _session_run(tmp_path, policies, "--session", "--context", SESSION_ID, "7", "KD-9045",
                             "--memory", BANK)
    assert run.code == 0, run.stderr
    assert "HINDSIGHT_CONFIG" not in run.claude_env and "Your Hindsight memory" not in _note(run)
    assert not (h.home / ".hindsight").exists()


@needs_bash
def test_a_broken_bank_config_keeps_the_role_bank_and_says_so(tmp_path, policies):
    _, _, run = _session_run(tmp_path, policies, "--session", "--context", SESSION_ID, "7", "KD-9045",
                             "--memory", BANK, role_config="[")
    assert run.code == 0, run.stderr
    assert "HINDSIGHT_CONFIG" not in run.claude_env and "Your Hindsight memory" not in _note(run)
    assert "role bank" in run.stderr


@needs_bash
@pytest.mark.parametrize("args", [
    ("--context", SESSION_ID, "12", "chat", "--memory", BANK),
    ("--context", SESSION_ID, "13", "monitor", "--memory", BANK),
    ("--memory", BANK),
    ("--context", SESSION_ID, "7", "KD-1", "--memory", "a/b"),
    ("--context", SESSION_ID, "7", "KD-1", "--memory"),
    ("--context", SESSION_ID, "7", "KD-1", "--memory", BANK, "--memory", BANK),
], ids=["chat", "monitor", "no-context", "bad-bank", "no-bank", "twice"])
def test_memory_only_after_a_cards_context(tmp_path, policies, args):
    _, _, run = _session_run(tmp_path, policies, "--session", *args, role_config=ROLE_CONFIG)
    assert (run.code, run.claude) == (2, None)


@needs_bash
def test_session_without_context_keeps_the_general_note(tmp_path, policies):
    # a host before the change still starts its sessions while a release switches the code
    _, _, run = _session_run(tmp_path, policies, "--session", RESUME)
    assert run.code == 0, run.stderr
    assert "This session (" not in _note(run)
    assert not [key for key in run.claude_env if key.startswith("KD_SESSION_")]


@needs_bash
@pytest.mark.parametrize("context", [
    ("--context",), ("--context", SESSION_ID, "7"), ("--context", SESSION_ID.upper(), "7", "KD-1"),
    ("--context", SESSION_ID, "0", "KD-1"), ("--context", SESSION_ID, "7x", "KD-1"),
    ("--context", SESSION_ID, "7", "kd-1"), ("--context", SESSION_ID, "7", "KD-1; id"),
    ("--context", SESSION_ID, "7", "Chat"), ("--context", SESSION_ID, "7", "KD-1", RESUME, "x"),
], ids=repr)
def test_session_bad_context_refused(tmp_path, policies, context):
    _, _, run = _session_run(tmp_path, policies, "--session", *context)
    assert (run.code, run.claude, run.sudo) == (2, None, None)


@needs_bash
def test_launch_never_sudoes(tmp_path, policies):
    # Another user (the base user of the gone panels) gets a refusal, not a sudo hop.
    _, _, run = _session_run(tmp_path, policies, "--session", user="digi")
    assert (run.code, run.claude, run.sudo) == (2, None, None)
    assert "own user" in run.stderr
    assert "sudo" not in LAUNCHER.read_text("utf-8").split("\n", 7)[-1].replace("sudo hop", "")


@needs_bash
def test_legacy_runtime_is_refused(tmp_path):
    h = Launcher(tmp_path, launch="replay")
    h.reset()
    h.write_registry({"boss": {"user": "digi-boss"}})
    run = h.run(LAUNCHER, "boss", user="digi-boss")
    assert (run.code, run.claude) == (2, None)
    assert "generated runtime" in run.stderr


def test_the_session_note_has_no_telegram():
    # no agent talks Telegram; the note of a hosted session says only where its text goes
    line = next(l for l in LAUNCHER.read_text("utf-8").splitlines() if "SESSION_NOTE=" in l)
    assert "cabinet" in line and "kd-work ask" in line
    assert "Telegram" not in line and "reply tool" not in line


# --- the autonomy design, piece 2: a card session's probe exit -----------------------------

EGRESS_FILE = "/opt/digipaddock/host/egress.json"
EGRESS = json.loads((FIXTURES / "host" / "egress.json").read_text("utf-8"))


def _with_exit(fs: FakeFS, document=None) -> FakeFS:
    for directory in ("/opt", "/opt/digipaddock", "/opt/digipaddock/host"):
        fs.nodes[directory] = Node("dir", 0, 0o755)
    fs.nodes[EGRESS_FILE] = Node("file", 0, 0o644)
    fs.data[EGRESS_FILE] = json.dumps(EGRESS if document is None else document).encode()
    return fs


def _exit_plan(name, policies, registry, *, base_url="", document=None, exit_name="probe"):
    policy = render_policy(policies[name])
    return plan_generated(name, f"{AGENTS}/{name}", uid=AGENT_UID, user=USERS[name], agents_root=AGENTS,
                          fs=_with_exit(installed(name, policy), document), registry=registry, space="chat",
                          egress=exit_name, egress_path=EGRESS_FILE, base_url=base_url)


def test_egress_adds_the_exit_names_and_the_proxy_env(policies, registry):
    from kd import egress

    compiled = policies["dev"]["launch_settings"]["sandbox"]["network"]["allowedDomains"]
    settings = json.loads(_exit_plan("dev", policies, registry).settings)
    assert settings["sandbox"]["network"]["allowedDomains"] == sorted(
        {*compiled, "api.example.test", "example.test"})
    env = settings["env"]
    assert env["HTTPS_PROXY"] == env["HTTP_PROXY"] == "http://127.0.0.1:3140"
    assert env["KD_EGRESS"] == "probe" and env["KD_EGRESS_HOSTS"] == "api.example.test,example.test"
    assert env["NO_PROXY"] == egress.no_proxy(compiled)
    # everything else is the plan without an exit
    plain = json.loads(_plan("dev", render_policy(policies["dev"]), registry).settings)
    settings.pop("env")
    settings["sandbox"]["network"]["allowedDomains"] = compiled
    assert settings == plain


def test_egress_keeps_a_provider_model_off_the_exit(policies, registry):
    settings = json.loads(_exit_plan("dev", policies, registry, base_url="https://llm.example.test/v1").settings)
    assert "llm.example.test" in settings["env"]["NO_PROXY"].split(",")


def test_egress_is_refused_without_the_capability(policies, registry):
    # every agent's manifest holds it; a policy compiled without it still refuses an exit
    bare = {**policies["boss"], "capabilities": [c for c in policies["boss"]["capabilities"] if c != "net.egress"]}
    with pytest.raises(LaunchError, match="net.egress"):
        _exit_plan("boss", {"boss": bare}, registry, document={**EGRESS, "exits": {
            "probe": {**EGRESS["exits"]["probe"], "agents": ["boss"]}}})


# --- a session that waited for a slot starts after the host's file changed --------------------------

UNLISTED = {**EGRESS, "exits": {"probe": {**EGRESS["exits"]["probe"], "agents": ["analyst"]}}, "projects": {}}
WEB_EXIT = {**EGRESS["exits"]["probe"], "port": 3141, "domains": ["*"]}


def _gone_plan(policies, registry, *, document=EGRESS, exit_name="probe", no_file=False):
    lines: list = []
    fs = installed("dev", render_policy(policies["dev"]))
    if not no_file:
        fs = _with_exit(fs, document)
    plan = plan_generated("dev", f"{AGENTS}/dev", uid=AGENT_UID, user=USERS["dev"], agents_root=AGENTS, fs=fs,
                          registry=registry, space="chat", egress=exit_name, egress_path=EGRESS_FILE,
                          warn=lines.append)
    return json.loads(plan.settings), lines


@pytest.mark.parametrize("case", ["not-in-exits", "no-file", "not-listed"])
def test_a_gone_exit_starts_the_session_without_it(policies, registry, case):
    over = {"not-in-exits": {"exit_name": "other"}, "no-file": {"no_file": True},
            "not-listed": {"document": UNLISTED}}[case]
    settings, lines = _gone_plan(policies, registry, **over)
    assert settings == json.loads(_plan("dev", render_policy(policies["dev"]), registry).settings)
    gone = over.get("exit_name", "probe")
    assert len(lines) == 1 and f"probe exit {gone} " in lines[0]


@pytest.mark.parametrize("case", ["not-in-exits", "not-listed"])
def test_a_gone_exit_falls_back_to_the_standing_one(policies, registry, case):
    if case == "not-in-exits":
        document, exit_name = {**EGRESS, "standing": "web", "exits": {**EGRESS["exits"], "web": WEB_EXIT}}, "gone"
    else:
        document, exit_name = {**UNLISTED, "standing": "web", "exits": {**UNLISTED["exits"], "web": WEB_EXIT}}, "probe"
    settings, lines = _gone_plan(policies, registry, document=document, exit_name=exit_name)
    assert settings["env"]["KD_EGRESS"] == "web" and settings["env"]["KD_EGRESS_HOSTS"] == "*"
    assert len(lines) == 1 and exit_name in lines[0]


def test_a_present_exit_warns_nothing(policies, registry):
    settings, lines = _gone_plan(policies, registry)
    assert settings["env"]["KD_EGRESS"] == "probe" and lines == []


def test_a_gone_exit_still_refuses_a_bad_name_or_a_broken_file(policies, registry):
    with pytest.raises(LaunchError, match="bad probe exit name"):
        _gone_plan(policies, registry, exit_name="Probe!")
    with pytest.raises(LaunchError):
        _gone_plan(policies, registry, document={**EGRESS, "standing": "missing"})


def test_main_launches_a_card_session_whose_exit_is_gone(policies, registry, capsys, monkeypatch):
    from kd import egress

    monkeypatch.setattr(egress, "PATH", EGRESS_FILE)
    fs = _with_exit(installed("dev", render_policy(policies["dev"])), UNLISTED)
    argv = ["dev", f"{AGENTS}/dev", "chat", "probe"]
    code = launch.main(argv, registry=registry, fs=fs, identity=lambda: (AGENT_UID, USERS["dev"]),
                       agents_root=AGENTS)
    out = capsys.readouterr()
    assert code == 0 and out.out.startswith("generated\n")
    assert out.err == "agent-launch: probe exit probe is no longer dev's on this host: the session starts without it\n"
    assert launch.main(["dev", f"{AGENTS}/dev", "chat", "Probe!"], registry=registry, fs=fs,
                       identity=lambda: (AGENT_UID, USERS["dev"]), agents_root=AGENTS) == 1
    assert "bad probe exit name" in capsys.readouterr().err


def test_egress_file_must_be_roots(policies, registry):
    policy = render_policy(policies["dev"])
    fs = _with_exit(installed("dev", policy))
    fs.nodes[EGRESS_FILE] = Node("file", AGENT_UID, 0o644)
    with pytest.raises(LaunchError, match="root-owned"):
        plan_generated("dev", f"{AGENTS}/dev", uid=AGENT_UID, user=USERS["dev"], agents_root=AGENTS, fs=fs,
                       registry=registry, egress="probe", egress_path=EGRESS_FILE)


def test_no_egress_plan_has_no_env(policies, registry):
    assert "env" not in json.loads(_plan("dev", render_policy(policies["dev"]), registry).settings)


# --- an exit of every host, and the standing exit of every session ------------------------------------
WEB = {**EGRESS, "exits": {"probe": {**EGRESS["exits"]["probe"], "domains": ["*"]}}}
STANDING = {**WEB, "standing": "probe"}
WEB_RULES = {"WebFetch", "WebSearch", "WebFetch(domain:*)"}


def test_an_exit_of_every_host_opens_the_web(policies, registry):
    from kd import egress

    compiled = policies["dev"]["launch_settings"]["sandbox"]["network"]["allowedDomains"]
    settings = json.loads(_exit_plan("dev", policies, registry, document=WEB).settings)
    # Claude Code takes no bare "*" among allowedDomains: its sandbox reads the WebFetch rule as every host
    assert settings["sandbox"]["network"]["allowedDomains"] == compiled
    permissions = settings["permissions"]
    assert WEB_RULES <= set(permissions["allow"]) and not WEB_RULES & set(permissions["deny"])
    env = settings["env"]
    assert env["KD_EGRESS"] == "probe" and env["KD_EGRESS_HOSTS"] == "*"
    assert env["HTTPS_PROXY"] == env["HTTP_PROXY"] == "http://127.0.0.1:3140"
    assert env["NO_PROXY"] == egress.no_proxy(compiled)
    # everything else is the plan without an exit: only the web tools moved from deny to allow
    plain = json.loads(_plan("dev", render_policy(policies["dev"]), registry).settings)
    assert {"WebFetch", "WebSearch"} <= set(plain["permissions"]["deny"])
    settings.pop("env")
    for field in ("allow", "deny"):
        assert set(permissions[field]) - WEB_RULES == set(plain["permissions"][field]) - WEB_RULES
        settings["permissions"][field] = plain["permissions"][field]
    assert settings == plain


def test_a_named_exit_leaves_the_web_tools_denied(policies, registry):
    permissions = json.loads(_exit_plan("dev", policies, registry).settings)["permissions"]
    assert {"WebFetch", "WebSearch"} <= set(permissions["deny"]) and not WEB_RULES & set(permissions["allow"])


def _standing_plan(name, policy, registry, *, document=STANDING, space="chat", fs=None):
    return plan_generated(name, f"{AGENTS}/{name}", uid=AGENT_UID, user=USERS[name], agents_root=AGENTS,
                          fs=fs or _with_exit(installed(name, render_policy(policy)), document), registry=registry,
                          space=space, egress_path=EGRESS_FILE)


def test_a_standing_exit_reaches_a_session_no_card_named_an_exit_for(policies, registry):
    env = json.loads(_standing_plan("dev", policies["dev"], registry).settings)["env"]
    assert env["KD_EGRESS"] == "probe" and env["KD_EGRESS_HOSTS"] == "*"


@pytest.mark.parametrize("space", ("chat", "monitor"))
def test_a_standing_exit_opens_the_web_to_an_agent_without_a_shell(policies, registry, space):
    # the coordinator runs no command of its own; its web is the two tools, in every thread of its
    listed = {**STANDING, "exits": {"probe": {**STANDING["exits"]["probe"], "agents": ["boss", "dev"]}}}
    settings = json.loads(_standing_plan("boss", policies["boss"], registry, document=listed, space=space).settings)
    permissions = settings["permissions"]
    assert WEB_RULES <= set(permissions["allow"]) and not WEB_RULES & set(permissions["deny"])
    assert settings["env"]["KD_EGRESS"] == "probe"


def test_a_measuring_probe_holds_no_standing_exit(policies, registry):
    policy = render_policy(policies["dev"])
    probe = plan_generated("dev", f"{AGENTS}/dev", uid=AGENT_UID, user=USERS["dev"], agents_root=AGENTS,
                           fs=_with_exit(installed("dev", policy), STANDING), registry=registry,
                           egress_path=EGRESS_FILE, standing=False)
    assert json.loads(probe.settings) == json.loads(_plan("dev", policy, registry).settings)


def test_a_standing_exit_is_only_of_an_agent_that_holds_it(policies, registry):
    plain = json.loads(_plan("dev", render_policy(policies["dev"]), registry).settings)
    # a policy compiled before the agent got net.egress: the launch goes on as it was, with no exit
    older = {**policies["dev"], "capabilities": [c for c in policies["dev"]["capabilities"] if c != "net.egress"]}
    assert json.loads(_standing_plan("dev", older, registry).settings) == plain
    # an exit that does not list the agent, and a host without a standing exit
    other = {**STANDING, "exits": {"probe": {**STANDING["exits"]["probe"], "agents": ["analyst"]}}, "projects": {}}
    assert json.loads(_standing_plan("dev", policies["dev"], registry, document=other).settings) == plain
    assert json.loads(_standing_plan("dev", policies["dev"], registry, document=WEB).settings) == plain


def test_a_cards_exit_beats_the_standing_one(policies, registry):
    document = {**EGRESS, "standing": "web", "exits": {
        **EGRESS["exits"], "web": {**EGRESS["exits"]["probe"], "port": 3141, "domains": ["*"]}}}
    env = json.loads(_exit_plan("dev", policies, registry, document=document).settings)["env"]
    assert env["KD_EGRESS"] == "probe" and env["KD_EGRESS_HOSTS"] == "api.example.test,example.test"


# --- the country of the exit a session holds --------------------------------------------------------
COUNTRIES = {**EGRESS, "standing": "web", "countries": {"GB": "web", "AE": "web-ae"}, "exits": {
    **EGRESS["exits"], "web": WEB_EXIT, "web-ae": {**WEB_EXIT, "port": 3142, "upstream": "127.0.0.1:1151"}}}


def test_a_country_exit_names_its_country(policies, registry):
    env = json.loads(_exit_plan("dev", policies, registry, document=COUNTRIES, exit_name="web-ae").settings)["env"]
    assert (env["KD_EGRESS"], env["KD_EGRESS_COUNTRY"]) == ("web-ae", "AE")
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:3142"


@pytest.mark.parametrize("space", ("chat", "monitor"))
def test_the_standing_exit_names_its_country(policies, registry, space):
    env = json.loads(_standing_plan("dev", policies["dev"], registry, document=COUNTRIES, space=space).settings)["env"]
    assert (env["KD_EGRESS"], env["KD_EGRESS_COUNTRY"]) == ("web", "GB")


def test_an_exit_of_no_country_names_none(policies, registry):
    env = json.loads(_exit_plan("dev", policies, registry, document=COUNTRIES).settings)["env"]
    assert env["KD_EGRESS"] == "probe" and "KD_EGRESS_COUNTRY" not in env


def test_a_standing_exit_needs_roots_valid_file(policies, registry):
    policy = render_policy(policies["dev"])
    fs = _with_exit(installed("dev", policy), STANDING)
    fs.nodes[EGRESS_FILE] = Node("file", AGENT_UID, 0o644)
    with pytest.raises(LaunchError, match="root-owned"):
        _standing_plan("dev", policies["dev"], registry, fs=fs)
    with pytest.raises(LaunchError, match="standing"):
        _standing_plan("dev", policies["dev"], registry, document={**EGRESS, "standing": "missing"})


@needs_bash
def test_session_egress_reaches_kd_launch_after_the_space(tmp_path, policies):
    h, _, run = _session_run(tmp_path, policies, "--session", "--context", SESSION_ID, "7", "KD-7",
                             "--egress", "probe", RESUME)
    assert run.code == 0, run.stderr
    assert json.loads((h.out / "launch.json").read_text("utf-8")) == ["boss", h.folder("boss").as_posix(), "chat",
                                                                      "probe"]


@needs_bash
@pytest.mark.parametrize("args", [
    ("--egress", "probe"),
    ("--context", SESSION_ID, "7", "chat", "--egress", "probe"),
    ("--context", SESSION_ID, "7", "monitor", "--egress", "probe"),
    ("--context", SESSION_ID, "7", "KD-7", "--egress", "Probe!"),
    ("--context", SESSION_ID, "7", "KD-7", "--egress"),
], ids=["no-context", "chat", "monitor", "bad-name", "no-value"])
def test_session_bad_egress_refused(tmp_path, policies, args):
    _, _, run = _session_run(tmp_path, policies, "--session", *args)
    assert run.code == 2 and run.claude is None


PROXY_VARS = {"HTTPS_PROXY": "http://proxy.example.test:3128", "https_proxy": "http://proxy.example.test:3128",
              "HTTP_PROXY": "http://proxy.example.test:3128", "http_proxy": "http://proxy.example.test:3128",
              "NO_PROXY": "example.test", "no_proxy": "example.test", "ALL_PROXY": "socks5://proxy.example.test",
              "all_proxy": "socks5://proxy.example.test"}


@needs_bash
def test_session_egress_drops_an_inherited_proxy(tmp_path, policies):
    # Claude Code keeps a launch-environment proxy over the settings env: the exit's names would leave past the exit
    h, _, run = _session_run(tmp_path, policies, "--session", "--context", SESSION_ID, "7", "KD-7",
                             "--egress", "probe", RESUME, extra_env=PROXY_VARS)
    assert run.code == 0, run.stderr
    assert not set(PROXY_VARS) & set(run.claude_env)


@needs_bash
def test_session_with_a_standing_exit_drops_an_inherited_proxy(tmp_path, policies):
    # no --egress names the standing exit — the plan's env does (kd.launch), in a chat session too
    h, _, run = _session_run(tmp_path, policies, "--session", "--context", SESSION_ID, "7", "chat",
                             extra_env=PROXY_VARS, plan_env={"KD_EGRESS": "web", "KD_EGRESS_HOSTS": "*"})
    assert run.code == 0, run.stderr
    assert not set(PROXY_VARS) & set(run.claude_env)


@needs_bash
def test_session_without_egress_keeps_the_hosts_proxy(tmp_path, policies):
    h, _, run = _session_run(tmp_path, policies, "--session", extra_env=PROXY_VARS)
    assert run.code == 0, run.stderr
    assert run.claude_env["HTTPS_PROXY"] == PROXY_VARS["HTTPS_PROXY"]
