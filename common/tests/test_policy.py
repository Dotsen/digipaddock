"""The effective policy compiler (BOSS spec 2026-10-06, section 5.2a), shadow-only.

The legacy agents compile from their real manifests (agents/sysadmin, agents/dev) and
BOSS from a fixture; the golden snapshots of the first two stay within the policy baseline
(policy_baseline). Each refusal row breaks one rule and names the error it must produce.
"""

from __future__ import annotations

import copy
import dataclasses
import difflib
import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

import policy_baseline
import pytest

from kd import cap_grants
from kd.manifest import AgentManifest, ManifestError, load_manifest, validate_all
from kd.policy import (
    Difference,
    PolicyError,
    _compile_policy,
    check_policy,
    compare_views,
    compile_policy,
    covers,
    effective_view,
    render_policy,
)
from kd.registry import Registry, load_registry, read_json_file, validate_registry

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures" / "policy"
FLOOR = read_json_file(ROOT / "common/claude/managed-settings.template.json")
LEGACY = ("sysadmin", "dev")
Policy = dict[str, Any]


def _registry() -> Registry:
    registry = load_registry(ROOT)
    validate_registry(registry)
    return registry


def _manifest(name: str) -> AgentManifest:
    base = ROOT if name in LEGACY else FIXTURES
    return load_manifest(base / "agents" / name / "agent.json")


def _compile(
    name: str,
    registry: Registry | None = None,
    *,
    manifest: AgentManifest | None = None,
    floor: dict[str, Any] = FLOOR,
) -> Policy:
    manifest = manifest or _manifest(name)
    return compile_policy(
        manifest, registry or _registry(), floor=floor, agents=_host(manifest)
    )


def _host(manifest: AgentManifest) -> dict[str, AgentManifest]:
    """The host's agents: the two legacy agents plus the one under test."""
    agents = {legacy: _manifest(legacy) for legacy in LEGACY}
    agents[manifest.name] = manifest
    return agents


def _assert_error(error: PolicyError, expected: str) -> None:
    """One of the ``|``-separated texts occurs in one of the reported errors."""
    texts = expected.split("|")
    assert any(t in e for e in error.errors for t in texts), error.errors


def _permissions(policy: Policy) -> dict[str, Any]:
    return policy["settings"]["permissions"]


def _sandbox(policy: Policy) -> dict[str, Any]:
    return policy["launch_settings"]["sandbox"]


# --- determinism ----------------------------------------------------------------------


@pytest.mark.parametrize("name", [*LEGACY, "boss"])
def test_two_runs_give_identical_bytes(name: str) -> None:
    first = render_policy(_compile(name))
    manifest = _manifest(name)
    shuffled = dataclasses.replace(
        manifest, capabilities=tuple(reversed(manifest.capabilities))
    )
    second = render_policy(_compile(name, _registry(), manifest=shuffled))
    assert first == second
    assert first.endswith(b"\n") and b"\r" not in first


def test_host_order_does_not_change_the_policy() -> None:
    manifest = _manifest("dev")
    registry = _registry()
    agents = {name: _manifest(name) for name in LEGACY}
    forward = compile_policy(manifest, registry, floor=FLOOR, agents=agents)
    backward = compile_policy(
        manifest, registry, floor=FLOOR, agents=dict(reversed(agents.items()))
    )
    assert render_policy(forward) == render_policy(backward)


# --- the artifacts --------------------------------------------------------------------


def test_artifacts_are_separate_and_complete() -> None:
    policy = _compile("dev")
    assert set(policy["settings"]) == {"permissions", "hooks"}
    assert set(policy["launch_settings"]) == {"sandbox"}
    # executors are reached through channels; no panel, so no sudoers at all.
    assert "sudoers" not in policy and "telegram" not in policy
    assert {(a["resource"], a["access"]) for a in policy["acl"]} == {
        ("board-tasks", "read"),
        ("code-mirror", "read"),
        ("forge-journal", "read"),
    }
    steps = policy["install_steps"]
    # Grants and channels go last on install and come off first on disable (spec 5.4).
    channels = ["board.read:channels", "board.write:channels", "data.read:channels", "diag.request:channels",
                "e2e.request:channels", "factory.request:channels", "forge.review:channels", "git.mirror:channels",
                "host.read:channels"]
    assert steps["install"][-len(channels):] == channels
    assert steps["disable"][:len(channels)] == channels
    assert steps["check"] == steps["install"]


def test_no_agent_holds_a_write_grant_on_the_board() -> None:
    for name in (*LEGACY, "boss"):
        policy = _compile(name)
        assert all(a["access"] == "read" for a in policy["acl"]), name


def test_board_readers_and_writers_reach_kd_task_only_through_the_base_channel() -> None:
    # board.read holds the kd-task wrapper (view, list, search); the executor writes only for
    # an agent whose manifest also holds board.write. kd-work stays with board.write.
    for name in (*LEGACY, "boss"):
        policy = _compile(name)
        channels = {c["key"]: c for c in policy["channels"]}
        assert "/opt/digipaddock/common/bin/kd-task" in channels["base"]["programs"], name
        assert channels["base"]["run_as"] == "digi"
        assert "kd-task" in channels["self"]["commands"], name
        assert ("/opt/digipaddock/common/bin/kd-work" in channels["base"]["programs"]) == (name != "boss"), name


@pytest.mark.parametrize("name", ["analyst", "boss", "dev", "sysadmin"])
def test_every_agent_reads_the_host_through_the_host_reader(name: str) -> None:
    # kd-host-read runs as the host reader behind the agent's own host channel
    policy = _compile(name)
    channels = {c["key"]: c for c in policy["channels"]}
    assert channels["host"]["run_as"] == "digi-observe"
    assert channels["host"]["programs"] == ["/opt/digipaddock/common/bin/kd-host-read"]
    assert "kd-host-read" in channels["self"]["commands"]
    rules = [r for r in _permissions(policy)["allow"] if r.startswith("Bash(kd-host-read ")]
    assert not rules or "Bash(kd-host-read journalctl *)" in rules, name


@pytest.mark.parametrize("name", ["analyst", "boss", "dev", "sysadmin"])
def test_every_base_channel_takes_the_permission_audit(name: str) -> None:
    # the PermissionRequest hook hands its event to the base user, who alone writes the private audit
    policy = _compile(name)
    (base,) = [c for c in policy["channels"] if c["key"] == "base"]
    assert "/opt/digipaddock/common/bin/kd-permlog" in base["programs"]
    assert not [r for r in _permissions(policy)["allow"] if "kd-permlog" in r]


@pytest.mark.parametrize("name", ["analyst", "boss", "dev", "sysadmin"])
def test_host_read_opens_no_file_or_group_to_the_agent(name: str) -> None:
    # Every platform read goes through the executor; the agent's own reads stay as they were
    manifest = _manifest(name)
    without = dataclasses.replace(manifest, capabilities=tuple(c for c in manifest.capabilities if c != "host.read"))
    wide, narrow = _compile(name, manifest=manifest), _compile(name, manifest=without)
    assert wide["acl"] == narrow["acl"]
    assert _sandbox(wide)["filesystem"] == _sandbox(narrow)["filesystem"]
    assert _sandbox(wide)["network"] == _sandbox(narrow)["network"]
    reads = lambda p: sorted(r for r in _permissions(p)["allow"] if r.startswith(("Read", "Glob", "Grep")))
    assert reads(wide) == reads(narrow)


@pytest.mark.parametrize("name", ["analyst", "boss", "dev", "sysadmin"])
def test_cabinet_push_and_review_secrets_are_closed(name: str) -> None:
    policy = _compile(name)
    deny, closed = _permissions(policy)["deny"], _sandbox(policy)["filesystem"]["denyRead"]
    for path in ("/etc/digipaddock/cabinet.env", "/etc/digipaddock/push.env", "/etc/digipaddock/review"):
        assert path in closed, (name, path)
        assert any(r.startswith(f"Read(/{path}") for r in deny), (name, path)


@pytest.mark.parametrize("name", ["analyst", "boss", "dev", "sysadmin"])
def test_every_agent_finishes_its_turn(name: str) -> None:
    # a turn on the agent's own card ends with kd-work finish; BOSS, without kd-work (board.write),
    # has the same operation as kd-boss-board finish.
    policy = _compile(name)
    command = "kd-boss-board" if name == "boss" else "kd-work"
    channels = {c["key"]: c for c in policy["channels"]}
    assert "finish" in channels["self"]["commands"][command]["subcommands"], name
    rules = [r for r in _permissions(policy)["allow"] if r.startswith(f"Bash({command} ")]
    assert not rules or f"Bash({command} finish *)" in rules, name


@pytest.mark.parametrize("name", ["analyst", "boss", "dev", "sysadmin"])
def test_every_agent_reads_and_answers_its_own_chat(name: str) -> None:
    # an agent on the session host talks to the owner only in the cabinet; kd-task chat
    # (read, --post) is its own chat, so board.read opens it in the policy and in the channel.
    # A sandboxed shell (sysadmin) holds no kd-task allow: there the channel's list is the only gate.
    policy = _compile(name)
    rules = [r for r in _permissions(policy)["allow"] if r.startswith("Bash(kd-task ")]
    assert not rules or "Bash(kd-task chat *)" in rules, name
    channels = {c["key"]: c for c in policy["channels"]}
    assert "chat" in channels["self"]["commands"]["kd-task"]["subcommands"], name


def test_no_policy_reaches_the_backlog_cli() -> None:
    # the board is the cabinet; the Backlog.md CLI is no wrapper and no channel command any more.
    for name in (*LEGACY, "boss"):
        policy = _compile(name)
        assert not [r for r in _permissions(policy)["allow"] if "backlog" in r], name
        assert all("backlog" not in c["commands"] for c in policy["channels"]), name


def _glob(pattern: str) -> re.Pattern[str]:
    """A path rule's pattern as Claude Code matches it (gitignore style): ``/**/`` is any number of folders,
    ``**`` at the end everything below, ``*`` one name's part."""
    parts = re.split(r"(/\*\*/|/\*\*$|\*)", pattern)
    table = {"/**/": "/(?:.*/)?", "/**": "/.*", "*": "[^/]*"}
    return re.compile("".join(table.get(part, re.escape(part)) for part in parts) + "$")


def _denied(policy: Policy, rule: str) -> bool:
    """``Edit(/<path>)`` matches a deny rule of the policy."""
    path = rule[len("Edit(/"):-1]
    return any(
        deny.startswith("Edit(/") and _glob(deny[len("Edit(/"):-1]).match(path)
        for deny in _permissions(policy)["deny"]
    )


@pytest.mark.parametrize("name", [*LEGACY, "boss"])
def test_no_session_makes_work_a_repository(name: str) -> None:
    # KD-9482: Claude Code runs git in its cwd outside the sandbox; a work/.git would be that repository
    policy = _compile(name)
    work = f"/srv/digipaddock-agents/{name}/work"
    assert f"{work}/.git" in _sandbox(policy)["filesystem"]["denyWrite"]
    assert _denied(policy, f"Edit(/{work}/.git)") and _denied(policy, f"Edit(/{work}/.git/config)")
    assert not _denied(policy, f"Edit(/{work}/DigiPaddock/README.md)")  # the clones below stay as they were


@pytest.mark.parametrize("name", [*LEGACY, "boss"])
def test_no_session_leaves_instructions_for_a_later_one(name: str) -> None:
    # KD-9482: Claude Code loads a folder's CLAUDE.md once a file there is read, and .claude/rules as instructions
    policy = _compile(name)
    work = f"/srv/digipaddock-agents/{name}/work"
    for path in ("tmp/cards/KD-1/CLAUDE.md", "tmp/CLAUDE.local.md", "DigiPaddock/CLAUDE.local.md",
                 "DigiPaddock/.claude/rules/x.md"):
        assert _denied(policy, f"Edit(/{work}/{path})"), path
    assert _denied(policy, f"Edit(//home/digi-{name}/.claude/rules/x.md)")
    # the developer edits a clone's tracked CLAUDE.md in a pull request; no one else holds a clone
    clone = [f"Edit(/{work}/DigiPaddock/CLAUDE.md)", f"Edit(/{work}/DigiPaddock.KD-12/agents/dev/CLAUDE.md)"]
    assert [_denied(policy, rule) for rule in clone] == [name != "dev"] * 2


def test_coordinator_policy_is_read_only_without_network() -> None:
    policy = _compile("boss")
    permissions, sandbox = _permissions(policy), _sandbox(policy)
    assert policy["bash_approval"] == "rules"
    assert policy["acl"] == [
        {
            "user": "digi-boss",
            "resource": "board-tasks",
            "path": "/srv/digipaddock-agents/board",
            "access": "read",
        }
    ]
    edits = [rule for rule in permissions["allow"] if rule.startswith("Edit")]
    # only the session's own notes; the shared notes through kd-notes add
    assert edits == ["Edit(//srv/digipaddock-agents/boss/memory/chats/**)",
                     "Edit(//srv/digipaddock-agents/boss/memory/tasks/**)"]
    assert "Edit(//srv/digipaddock-agents/boss/memory/notes.md)" in permissions["deny"]
    assert "/srv/digipaddock-agents/boss/memory/notes.md" in sandbox["filesystem"]["denyWrite"]
    assert sandbox["network"]["allowedDomains"] == []
    assert sandbox["excludedCommands"] == []
    assert "/srv/digipaddock-agents/board" in sandbox["filesystem"]["denyWrite"]
    for rule in (
        "Bash(ssh-exec *)",
        "Bash(ssh-ro *)",
        "Bash(kd-merge *)",
        "Edit(//srv/digipaddock-agents/board/**)",
    ):
        assert rule in permissions["deny"], rule
    assert [c["key"] for c in policy["channels"]] == ["base", "data", "host", "self"]
    # No guard is declared for the coordinator capabilities: the channel hook records its
    # expected calls.
    assert [h["matcher"] for h in policy["settings"]["hooks"]["PreToolUse"]] == ["Bash"]


@pytest.mark.parametrize("name", ["analyst", "boss", "dev", "sysadmin"])
def test_policy_has_no_telegram_or_sudoers(name: str) -> None:
    # no panels and no Telegram: no channel flag, no launch sudoers, no Telegram tools, no typing hook.
    policy = _compile(name)
    assert policy["schema_version"] >= 4
    assert "telegram" not in policy and "sudoers" not in policy
    permissions = _permissions(policy)
    assert not [r for r in permissions["allow"] + permissions["ask"] + permissions["deny"] if "telegram" in r]
    assert [h["matcher"] for h in policy["settings"]["hooks"]["PreToolUse"]] == ["Bash"]
    assert "kd-typing" not in json.dumps(policy)


def test_validator_agent_gets_no_bash_allow() -> None:
    # dev's guard approves Bash; an allow rule would pass a command unchecked
    # whenever the hook times out.
    policy = _compile("dev")
    assert policy["bash_approval"] == "validator"
    assert not [r for r in _permissions(policy)["allow"] if r.startswith("Bash")]
    # kd-merge needs the owner's Approve in Forgejo, not a card.
    assert _permissions(policy)["ask"] == []


def test_own_file_reaches_only_its_holder() -> None:
    dev_agent = _sandbox(_compile("dev"))["filesystem"]
    assert dev_agent["allowRead"] == [
        "/home/digi-dev/.claude/projects",  # its own saved tool outputs
        "/home/digi-dev/.ssh/config",
        "/home/digi-dev/.ssh/dev_ed25519",
        "/run/digipaddock/cap/dev",  # its own channel sockets
    ]
    assert (
        "/home/digi-sysadmin" in dev_agent["denyRead"]
    )  # the sysadmin's key stays closed
    # global review 15.10.2026: the other agents' folders (notes, texts, patches, owner chat) are closed like their homes
    assert "/srv/digipaddock-agents/sysadmin" in dev_agent["denyRead"]
    assert "/srv/digipaddock-agents/dev" not in dev_agent["denyRead"]
    dev_deny = set(_compile("dev")["settings"]["permissions"]["deny"])
    assert {"Read(//srv/digipaddock-agents/sysadmin/**)", "Edit(//srv/digipaddock-agents/sysadmin/**)"} <= dev_deny
    assert "Read(//srv/digipaddock-agents/dev/**)" not in dev_deny
    # ssh-ro reads the sysadmin's key in its channel, outside the sandbox: closed inside.
    sysadmin = _sandbox(_compile("sysadmin"))["filesystem"]
    assert sysadmin["allowRead"] == ["/home/digi-sysadmin/.claude/projects", "/run/digipaddock/cap/sysadmin"]
    assert "/home/digi-sysadmin/.ssh" in sysadmin["denyRead"]
    assert "/home/digi-sysadmin/.claude" in sysadmin["denyRead"]  # its login and channels stay hidden
    assert "/home/digi" in sysadmin["denyRead"]  # M02: the shared home is foreign now
    assert "/home/digi-dev" in sysadmin["denyRead"]
    boss = _sandbox(_compile("boss"))["filesystem"]
    assert not [path for path in boss["allowRead"] if "/.ssh/" in path]
    second = AgentManifest(
        name="dev2",
        user="digi-dev2",
        model=None,
        profile="developer",
        capabilities=("git.develop",),
        sandbox_profile="developer-v1",
        working_directory="work",
    )
    other = _sandbox(_compile("dev2", manifest=second))["filesystem"]
    assert other["allowRead"] == [
        "/home/digi-dev2/.claude/projects",
        "/home/digi-dev2/.ssh/config",
        "/home/digi-dev2/.ssh/dev2_ed25519",
    ]
    assert "/home/digi-dev" in other["denyRead"]



# --- refusals before anything is compiled ---------------------------------------------


def _selection(**changes: Any) -> AgentManifest:
    return dataclasses.replace(_manifest("dev"), **changes)


# fmt: off
SELECTIONS = [
    ("outside-ceiling", {"capabilities": ("board.read", "git.develop", "servers.read")}, "outside the developer profile ceiling"),
    ("wildcard-capability", {"capabilities": ("board.*", "git.develop")}, "unknown capability board.*"),
    ("unknown-capability", {"capabilities": ("git.develop", "shell.root")}, "unknown capability shell.root"),
    ("foreign-sandbox", {"sandbox_profile": "coordinator-v1"}, "not allowed for profile developer"),
    ("unknown-sandbox", {"sandbox_profile": "unsandboxed"}, "not allowed for profile developer"),
    ("missing-dependency", {"capabilities": ("board.write", "git.develop")}, "missing capability dependency"),
    ("unknown-profile", {"profile": "root"}, "unknown profile root"),
]
# fmt: on


@pytest.mark.parametrize(
    ("changes", "expected"),
    [pytest.param(c, e, id=name) for name, c, e in SELECTIONS],
)
def test_compiler_refuses_a_selection_outside_the_profile(
    changes: dict[str, Any], expected: str
) -> None:
    with pytest.raises(PolicyError) as caught:
        _compile("dev", manifest=_selection(**changes))
    _assert_error(caught.value, expected)


# A manifest object built without load_manifest: its identity is checked again.
# fmt: off
IDENTITIES = [
    ("traversal-name", "boss", {"name": "../../etc"}, "invalid agent name"),
    ("service-user", "boss", {"user": "digi-exec"}, "agent user must be digi-boss"),
    ("forge-user", "dev", {"user": "digi-forge"}, "agent user must be digi-dev"),
    ("model-option", "boss", {"model": "opus --dangerously-skip-permissions"}, "invalid model"),
    ("shared-user", "boss", {"user": "digi"}, "agent user must be digi-boss"),
    ("shared-user-sysadmin", "sysadmin", {"user": "digi"}, "agent user must be digi-sysadmin"),
    ("developer-user", "boss", {"user": "digi-dev"}, "agent user must be digi-boss"),
    ("root-user", "boss", {"user": "root"}, "agent user must be digi-boss"),
    ("service-name", "boss", {"name": "exec", "user": "digi-exec"}, "agent name cannot own a user"),
]
# fmt: on


@pytest.mark.parametrize(
    ("agent", "changes", "expected"),
    [pytest.param(a, c, e, id=name) for name, a, c, e in IDENTITIES],
)
def test_compiler_checks_the_agent_identity(
    agent: str, changes: dict[str, Any], expected: str
) -> None:
    manifest = dataclasses.replace(_manifest(agent), **changes)
    with pytest.raises(PolicyError) as caught:
        compile_policy(manifest, _registry(), floor=FLOOR, agents=_host(manifest))
    _assert_error(caught.value, expected)


def test_compiler_checks_every_host_identity() -> None:
    manifest = _manifest("boss")
    agents = _host(manifest)
    agents["dev"] = dataclasses.replace(agents["dev"], user="digi-exec")
    with pytest.raises(PolicyError, match="agent user must be digi-dev"):
        compile_policy(manifest, _registry(), floor=FLOOR, agents=agents)


def test_host_must_list_the_agent_once_per_user() -> None:
    manifest = _manifest("dev")
    with pytest.raises(PolicyError, match="must include this manifest"):
        compile_policy(manifest, _registry(), floor=FLOOR, agents={})
    boss = _manifest("boss")
    twin = dataclasses.replace(boss, name="helper")  # a user follows its name
    with pytest.raises(PolicyError, match="agent user must be digi-helper"):
        compile_policy(
            boss,
            _registry(),
            floor=FLOOR,
            agents={"boss": boss, "helper": twin},
        )
    helper = dataclasses.replace(twin, user="digi-helper")
    with pytest.raises(PolicyError, match="listed under another name"):
        compile_policy(
            boss, _registry(), floor=FLOOR, agents={"boss": boss, "other": helper}
        )


def test_unknown_manifest_field_fails_closed(tmp_path: Path) -> None:
    data = json.loads((FIXTURES / "agents/boss/agent.json").read_text("utf-8"))
    data["permissions"] = {"allow": ["Bash"]}
    path = tmp_path / "boss" / "agent.json"
    path.parent.mkdir()
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ManifestError, match="invalid manifest fields"):
        load_manifest(path)


# --- refusals of dangerous registry data (as if validate_registry were bypassed) ------


Change = Callable[[Registry], object]


def _tampered(change: Change) -> Registry:
    registry = copy.deepcopy(_registry())
    change(registry)
    return registry


def _sandbox_setting(profile: str, key: str, value: Any) -> Change:
    return lambda r: r.sandboxes[profile]["settings"].__setitem__(key, value)


def _wrapper(capability: str, index: int, key: str, value: Any) -> Change:
    return lambda r: r.capabilities[capability]["wrappers"][index].__setitem__(
        key, value
    )


def _ceiling(capability: str, key: str, value: list[str]) -> Change:
    return lambda r: r.capabilities[capability]["sandbox_ceiling"].__setitem__(
        key, value
    )


def _resource_path(name: str, path: str) -> Change:
    def change(registry: Registry) -> None:
        table = registry.resources.resources
        table[name] = dataclasses.replace(table[name], path=path)

    return change


def _resource_home(name: str, home: tuple[str, ...]) -> Change:
    def change(registry: Registry) -> None:
        table = registry.resources.resources
        table[name] = dataclasses.replace(table[name], home=home)

    return change


def _two_guards(registry: Registry) -> None:
    registry.profiles["developer"]["capability_ceiling"].append("servers.read")
    registry.capabilities["git.develop"]["conflicts"].remove("servers.read")
    registry.capabilities["servers.read"]["conflicts"].remove("git.develop")


DANGEROUS_WRAPPER = {
    "command": "bash",
    "source": "platform",
    "subcommands": ["show"],
    "approval": "allow",
    "run_as": "self",
    "executor": None,
}

# fmt: off
TAMPERED = [
    ("unsandboxed", "boss", _sandbox_setting("coordinator-v1", "allowUnsandboxedCommands", True), "allowUnsandboxedCommands must be false"),
    ("sandbox-off", "dev", _sandbox_setting("developer-v1", "enabled", False), "enabled must be true"),
    ("sandbox-optional", "sysadmin", _sandbox_setting("sysadmin-v1", "failIfUnavailable", False), "failIfUnavailable must be true"),
    ("auto-allow", "dev", _sandbox_setting("developer-v1", "autoAllowBashIfSandboxed", True), "autoAllowBashIfSandboxed must be false"),
    ("network-beyond-profile", "dev", _ceiling("git.develop", "network_domains", ["example.com", "forge.example.test"]), "example.com is outside"),
    ("channel-beyond-wrappers", "boss", _ceiling("agent.nudge", "channel_commands", ["git", "kd-nudge"]), "channel command git is not a held wrapper"),
    ("interpreter-wrapper", "boss", lambda r: r.capabilities["usage.read"]["wrappers"].__setitem__(0, DANGEROUS_WRAPPER), "dangerous allow Bash(bash show *)"),
    ("grant-inside-secret", "dev", _resource_path("forge-journal", "/etc/digipaddock/forge/log"), "conflicts with a deny rule"),
    ("grant-over-secret", "sysadmin", _resource_path("exec-journal", "/etc/digipaddock"), "granted exec-journal would expose secret /etc/digipaddock/forge"),
    ("public-inside-secret", "dev", _resource_path("forge-repos", "/etc/digipaddock/forge/digipaddock-merge.env"), "allowRead /etc/digipaddock/forge/digipaddock-merge.env lies inside secret /etc/digipaddock/forge"),
    ("own-file-escape", "dev", _resource_home("forge-ssh-key", ("../agent/.ssh/agent_ro_ed25519",)), "allowRead /home/digi-dev/../agent"),
    ("own-file-credentials", "dev", _resource_home("forge-ssh-key", (".claude/.credentials.json",)), "allowRead /home/digi-dev/.claude/.credentials.json is outside the agent's own .ssh/"),
    ("own-file-memory-key", "dev", _resource_home("forge-ssh-key", (".hindsight/config.json",)), "is outside the agent's own .ssh/"),
    ("two-guards", "dev:git.develop,servers.read", _two_guards, "one agent runs one guard"),
    ("shell-without-guard", "dev:git.develop", lambda r: r.capabilities["git.develop"].__setitem__("validator", None), "a sandboxed shell needs its guard"),
]
# fmt: on


@pytest.mark.parametrize(
    ("agent", "change", "expected"),
    [pytest.param(a, c, e, id=name) for name, a, c, e in TAMPERED],
)
def test_compiler_refuses_dangerous_registry_data(
    agent: str, change: Change, expected: str
) -> None:
    # "agent:cap,cap" selects only these capabilities of the fixture agent.
    agent, _, selected = agent.partition(":")
    manifest = _manifest(agent)
    if selected:
        manifest = dataclasses.replace(
            manifest, capabilities=tuple(selected.split(","))
        )
    registry = _tampered(change)
    with pytest.raises(PolicyError) as caught:
        # The internal entry skips validate_registry, which would refuse this data first.
        _compile_policy(manifest, registry, floor=FLOOR, agents=_host(manifest))
    _assert_error(caught.value, expected)


@pytest.mark.parametrize(
    "change",
    [
        _sandbox_setting("coordinator-v1", "allowUnsandboxedCommands", True),
        _wrapper("servers.execute", 0, "approval", "ask"),
        _resource_home("forge-ssh-key", (".claude/.credentials.json",)),
    ],
    ids=["unsandboxed", "exec-to-ask", "own-file-credentials"],
)
def test_compiler_validates_the_registry_first(change: Change) -> None:
    with pytest.raises(PolicyError):
        _compile("boss", _tampered(change))


def _exec_renamed(registry: Registry) -> None:
    # The review probe: ssh-exec published as kd-exec, its channel command moved along.
    execute = registry.capabilities["servers.execute"]
    execute["wrappers"][0].update(command="kd-exec", approval="allow")
    channels = execute["sandbox_ceiling"]["channel_commands"]
    channels[channels.index("ssh-exec")] = "kd-exec"
    box = registry.sandboxes["sysadmin-v1"]["ceiling"]
    box["channel_commands"] = sorted({*box["channel_commands"], "kd-exec"} - {"ssh-exec"})


# A registry changed in memory that the compiler's own checks cannot see through:
# _compile_policy (no registry check) produces a policy, the public entries refuse it.
# fmt: off
UNCHECKED_REGISTRY = [
    ("subcommand-injection", "boss", _wrapper("usage.read", 0, "subcommands", ["show; curl"]), "Bash(kd-usage show; curl *)", "subcommands: invalid names"),
    ("unknown-field", "boss", lambda r: r.capabilities["usage.read"].__setitem__("permissions", {"allow": ["Bash"]}), "Bash(kd-usage show *)", "capability usage.read: fields must be exactly"),
    ("exec-renamed", "sysadmin", _exec_renamed, '"kd-exec": {', "agents/sysadmin/bin/ssh-exec must be published as ssh-exec"),
]
# fmt: on


@pytest.mark.parametrize(
    ("agent", "change", "allowed", "expected"),
    [pytest.param(a, c, al, e, id=name) for name, a, c, al, e in UNCHECKED_REGISTRY],
)
def test_public_entries_validate_the_registry(
    agent: str, change: Change, allowed: str, expected: str
) -> None:
    registry = _tampered(change)
    manifest = _manifest(agent)
    policy = _compile_policy(manifest, registry, floor=FLOOR, agents=_host(manifest))
    assert allowed in render_policy(policy).decode("utf-8")
    with pytest.raises(PolicyError) as caught:
        check_policy(policy, registry, floor=FLOOR, agents=_host(manifest))
    _assert_error(caught.value, expected)
    with pytest.raises(PolicyError) as caught:
        compile_policy(manifest, registry, floor=FLOOR, agents=_host(manifest))
    _assert_error(caught.value, expected)


def test_floor_ask_beside_an_allow_is_a_conflict() -> None:
    floor = copy.deepcopy(FLOOR)
    floor["permissions"]["ask"] = ["Bash(kd-nudge *)"]
    with pytest.raises(PolicyError, match=r"allow Bash\(kd-nudge \*\) conflicts with an ask"):
        _compile("boss", floor=floor)


def test_floor_deny_swallowing_an_allow_is_a_conflict() -> None:
    floor = copy.deepcopy(FLOOR)
    floor["permissions"]["deny"].append("Bash(kd-usage *)")
    with pytest.raises(PolicyError, match=r"allow Bash\(kd-usage show \*\) conflicts with"):
        _compile("boss", floor=floor)


# --- check_policy refuses tampered output ---------------------------------------------


Tamper = Callable[[Policy], object]


def _add(path: tuple[str, ...], value: Any) -> Tamper:
    def change(policy: Policy) -> None:
        target = policy
        for key in path:
            target = target[key]
        target.append(value)
        if all(isinstance(item, str) for item in target):
            target.sort()

    return change


def _remove(path: tuple[str, ...], value: Any) -> Tamper:
    def change(policy: Policy) -> None:
        target = policy
        for key in path:
            target = target[key]
        target.remove(value)

    return change


def _put(path: tuple[str, ...], value: Any) -> Tamper:
    def change(policy: Policy) -> None:
        target = policy
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return change


PERMS = ("settings", "permissions")
BOX = ("launch_settings", "sandbox")
FS = (*BOX, "filesystem")

# fmt: off
TAMPERING = [
    ("bare-bash", "sysadmin", _add((*PERMS, "allow"), "Bash"), "dangerous allow Bash"),
    ("bash-beside-guard", "dev", _add((*PERMS, "allow"), "Bash(kd-pr *)"), "not a held wrapper form"),
    ("curl-allow", "sysadmin", _add((*PERMS, "allow"), "Bash(curl *)"), "dangerous allow"),
    ("unheld-wrapper", "boss", _add((*PERMS, "allow"), "Bash(ssh-ro *)"), "not a held wrapper form"),
    ("web-fetch", "boss", _add((*PERMS, "allow"), "WebFetch"), "dangerous allow WebFetch"),
    ("edit-anywhere", "dev", _add((*PERMS, "allow"), "Edit(//etc/**)"), "not granted by a capability"),
    ("read-secret", "sysadmin", _add((*PERMS, "allow"), "Read(//etc/digipaddock/pult.env)"), "not granted by a capability"),
    ("bare-read-without-shell", "boss", _add((*PERMS, "allow"), "Read"), "not granted by a capability"),
    ("foreign-ask", "sysadmin", _add((*PERMS, "ask"), "Bash(kd-pr *)"), "ask Bash(kd-pr *) is not a held wrapper form"),
    ("allow-deny-conflict", "boss", _add((*PERMS, "deny"), "Bash(kd-usage *)"), "conflicts with a deny rule"),
    ("ask-deny-conflict", "sysadmin", lambda p: (_add((*PERMS, "ask"), "Bash(kd-zz *)")(p), _add((*PERMS, "deny"), "Bash(kd-zz *)")(p)), "ask Bash(kd-zz *) conflicts with a deny rule"),
    ("drop-sudo-deny", "dev", _remove((*PERMS, "deny"), "Bash(sudo *)"), "missing deny Bash(sudo *)"),
    ("drop-merge-deny", "sysadmin", _remove((*PERMS, "deny"), "Bash(kd-merge *)"), "missing deny Bash(kd-merge *)"),
    ("drop-secret-deny", "boss", _remove((*PERMS, "deny"), "Read(//etc/digipaddock/pult.env)"), "missing deny Read(//etc/digipaddock/pult.env)"),
    ("open-foreign-home", "dev", _remove((*FS, "denyRead"), "/home/digi"), "sandbox: missing denyRead /home/digi"),
    ("open-other-key", "dev", _remove((*PERMS, "deny"), "Read(//home/digi-sysadmin/**)"), "secret /home/digi-sysadmin/.ssh/agent_ro_ed25519 of sysadmin is not denied"),
    ("open-own-credentials", "dev", _remove((*PERMS, "deny"), "Read(//home/digi-dev/.claude/.credentials.json)"), "missing deny Read(//home/digi-dev/.claude/.credentials.json)"),
    ("drop-protected", "boss", _remove((*FS, "denyRead"), "/etc/hindsight"), "sandbox: missing denyRead /etc/hindsight"),
    ("drop-unheld-wrapper", "dev", _remove((*PERMS, "deny"), "Bash(kd-monitor *)"), "missing deny Bash(kd-monitor *)"),
    ("drop-instructions-deny", "sysadmin", _remove((*PERMS, "deny"), "Edit(//srv/digipaddock-agents/sysadmin/CLAUDE.md)"), "missing deny Edit(//srv/digipaddock-agents/sysadmin/CLAUDE.md)"),
    ("writable-board", "boss", _remove((*FS, "denyWrite"), "/srv/digipaddock-agents/board"), "sandbox: missing denyWrite /srv/digipaddock-agents/board"),
    ("writable-placeholder", "dev", _remove((*FS, "denyWrite"), "/srv/digipaddock-agents/dev/work/CLAUDE.md"), "missing denyWrite /srv/digipaddock-agents/dev/work/CLAUDE.md"),
    ("writable-work-git", "sysadmin", _remove((*FS, "denyWrite"), "/srv/digipaddock-agents/sysadmin/work/.git"), "missing denyWrite /srv/digipaddock-agents/sysadmin/work/.git"),
    ("editable-work-git", "dev", _remove((*PERMS, "deny"), "Edit(//srv/digipaddock-agents/dev/work/.git/**)"), "missing deny Edit(//srv/digipaddock-agents/dev/work/.git/**)"),
    ("editable-folder-instructions", "sysadmin", _remove((*PERMS, "deny"), "Edit(//srv/digipaddock-agents/sysadmin/work/**/CLAUDE.md)"), "missing deny Edit(//srv/digipaddock-agents/sysadmin/work/**/CLAUDE.md)"),
    ("editable-temp-instructions", "dev", _remove((*PERMS, "deny"), "Edit(//srv/digipaddock-agents/dev/work/tmp/**/CLAUDE.md)"), "missing deny Edit(//srv/digipaddock-agents/dev/work/tmp/**/CLAUDE.md)"),
    ("editable-rules", "boss", _remove((*PERMS, "deny"), "Edit(//**/.claude/rules/**)"), "missing deny Edit(//**/.claude/rules/**)"),
    ("bypass-mode", "sysadmin", _put((*PERMS, "defaultMode"), "bypassPermissions"), "defaultMode must be dontAsk"),
    ("auto-mode", "dev", _put((*PERMS, "disableAutoMode"), "enable"), "disableAutoMode must be disable"),
    ("extra-directory", "boss", _add((*PERMS, "additionalDirectories"), "/etc"), "additional directory /etc"),
    ("unknown-policy-field", "boss", _put(("bypass",), True), "policy: fields must be exactly"),
    ("traversal-name", "boss", _put(("agent",), "../../etc"), "agent identity: invalid agent name"),
    ("service-user", "boss", _put(("user",), "digi-exec"), "agent identity: agent user must be digi-boss"),
    ("model-option", "boss", _put(("model",), "opus --dangerously-skip-permissions"), "agent identity: invalid model"),
    ("model-type", "boss", _put(("model",), ["opus"]), "model must be text or null"),
    ("unknown-permission-field", "boss", _put((*PERMS, "allowManagedHooksOnly"), False), "permissions: fields must be exactly"),
    ("unknown-sandbox-field", "dev", _put((*BOX, "allowWrite"), ["/"]), "sandbox: fields must be exactly"),
    ("schema", "boss", _put(("schema_version",), 99), "unsupported schema_version"),
    ("unsorted-deny", "boss", lambda p: p["settings"]["permissions"]["deny"].reverse(), "deny must be sorted"),
    ("unsandboxed", "dev", _put((*BOX, "allowUnsandboxedCommands"), True), "allowUnsandboxedCommands must be false"),
    ("sandbox-off", "sysadmin", _put((*BOX, "enabled"), False), "enabled must be true"),
    ("weaker-network", "dev", _put((*BOX, "enableWeakerNetworkIsolation"), True), "enableWeakerNetworkIsolation must be false"),
    ("extra-domain", "dev", _add((*BOX, "network", "allowedDomains"), "example.com"), "example.com is outside"),
    ("boss-domain", "boss", _add((*BOX, "network", "allowedDomains"), "forge.example.test"), "is outside the held capabilities"),
    ("unix-sockets-off", "boss", _put((*BOX, "network", "allowAllUnixSockets"), False), "allowAllUnixSockets must be true"),
    ("local-binding", "sysadmin", _put((*BOX, "network", "allowLocalBinding"), True), "allowLocalBinding must be false"),
    ("extra-exclusion", "dev", _add((*BOX, "excludedCommands"), "git *"), "excludedCommands must be empty"),
    ("open-run", "dev", _remove((*FS, "denyRead"), "/run"), "sandbox: missing denyRead /run"),
    ("foreign-cap-dir", "dev", _add((*FS, "allowRead"), "/run/digipaddock/cap/sysadmin"), "allowRead /run/digipaddock/cap/sysadmin"),
    ("foreign-program", "dev", _add(("channels", 1, "programs"), "/opt/digipaddock/agents/sysadmin/bin/ssh_exec.py"), "channels must be exactly"),
    ("root-channel", "dev", _put(("channels", 1, "run_as"), "root"), "channels must be exactly"),
    ("dropped-channel", "boss", lambda p: p["channels"].pop(), "channels must be exactly"),
    ("malformed-channel", "boss", _put(("channels", 0, "env"), {}), "channels: entries must have exactly"),
    ("foreign-key", "dev", _add((*FS, "allowRead"), "/home/digi/.ssh/agent_ro_ed25519"), "allowRead /home/digi/.ssh/agent_ro_ed25519"),
    ("open-credentials", "dev", _add((*FS, "allowRead"), "/home/digi-dev/.claude/.credentials.json"), "is outside the agent's own .ssh/"),
    ("open-claude-dir", "dev", _add((*FS, "allowRead"), "/home/digi-dev/.claude"), "allowRead /home/digi-dev/.claude is not the agent's own"),
    ("foreign-tool-outputs", "dev", _add((*FS, "allowRead"), "/home/digi-sysadmin/.claude/projects"), "allowRead /home/digi-sysadmin/.claude/projects is not the agent's own"),
    ("open-secret", "sysadmin", _remove((*FS, "denyRead"), "/etc/digipaddock/forge"), "secret /etc/digipaddock/forge is not denied"),
    ("open-protected", "sysadmin", _remove((*FS, "denyRead"), "/etc/digipaddock/pult.env"), "missing denyRead /etc/digipaddock/pult.env"),
    ("open-own-keys", "dev", _remove((*FS, "denyRead"), "/home/digi-dev/.ssh"), "secret /home/digi-dev/.ssh is not denied"),
    ("writable-code", "boss", _remove((*FS, "denyWrite"), "/opt/digipaddock"), "code root /opt/digipaddock is writable"),
    ("sudoers-field", "dev", _put(("sudoers",), [{"user": "digi", "run_as": "digi-dev", "command": "/usr/local/bin/agent-launch dev"}]), "policy: fields must be exactly"),
    ("telegram-field", "boss", _put(("telegram",), False), "policy: fields must be exactly"),
    ("foreign-acl", "dev", _add(("acl",), {"user": "digi-dev", "resource": "exec-journal", "path": "/var/log/digipaddock", "access": "read"}), "read on exec-journal is not a held grant"),
    ("boss-write-acl", "boss", _put(("acl", 0, "access"), "write"), "write on board-tasks is not a held grant"),
    ("drop-guard", "dev", lambda p: p["settings"]["hooks"]["PreToolUse"].pop(0), "hooks must be exactly"),
    ("channels-first", "dev", lambda p: p["install_steps"]["install"].reverse(), "install steps must follow"),
    ("widen-capabilities", "dev", _put(("capabilities",), ["board.read", "git.develop", "servers.read"]), "differs from the host manifest in: capabilities"),
    ("flip-approval", "dev", _put(("bash_approval",), "rules"), "bash_approval does not match"),
]
# fmt: on


@pytest.mark.parametrize(
    ("agent", "tamper", "expected"),
    [pytest.param(a, t, e, id=name) for name, a, t, e in TAMPERING],
)
def test_check_policy_refuses_tampered_output(
    agent: str, tamper: Tamper, expected: str
) -> None:
    registry = _registry()
    policy = copy.deepcopy(_compile(agent, registry))
    check_policy(policy, registry, floor=FLOOR, agents=_host(_manifest(agent)))
    tamper(policy)
    with pytest.raises(PolicyError) as caught:
        check_policy(policy, registry, floor=FLOOR, agents=_host(_manifest(agent)))
    _assert_error(caught.value, expected)


# --- rule coverage and view comparison ------------------------------------------------


# fmt: off
COVERAGE = [
    ("Bash(backlog task *)", "Bash(backlog task list *)", True),
    ("Bash(kd-monitor list*)", "Bash(kd-monitor list *)", True),
    ("Bash(kd-monitor list *)", "Bash(kd-monitor list*)", False),
    ("Bash(ssh *)", "Bash(ssh-ro *)", False),
    ("Bash(git push*)", "Bash(git push --force *)", True),
    ("Bash(kd-monitor --help)", "Bash(kd-monitor --help x)", False),
    ("Bash(kd-task *)", "Bash(kd-task)", False),
    ("Read", "Read(//etc/x/**)", True),
    ("Read(//etc/x/**)", "Read", False),
    ("Read(//home/digi/**)", "Read(//home/digi/.ssh/**)", True),
    ("Read(//home/digi/**)", "Read(//home/digi-dev/.ssh/**)", False),
    ("Read(//var/www/**)", "Read(//var/www/**/config.ini)", True),
    ("Read(//var/www/**/config.ini)", "Read(//var/www/a/config.ini)", False),
    ("Edit(//home/digi/**)", "Read(//home/digi/x)", False),
]
# fmt: on


@pytest.mark.parametrize(("outer", "inner", "expected"), COVERAGE)
def test_rule_coverage_is_conservative(outer: str, inner: str, expected: bool) -> None:
    assert covers(outer, inner) is expected


def _view(policy: Policy) -> dict[str, Any]:
    return effective_view(
        floor=FLOOR,
        settings=policy["settings"],
        launch_settings=policy["launch_settings"],
        bash_approval=policy["bash_approval"],
        acl={f"{a['access']}:{a['resource']}" for a in policy["acl"]},
    )


# fmt: off
WIDENINGS = [
    ("new-allow", _add((*PERMS, "allow"), "Bash(curl *)"), "added allow:Bash(curl *)"),
    ("dropped-deny", _remove((*PERMS, "deny"), "Bash(ssh-ro *)"), "removed deny:Bash(ssh-ro *)"),
    ("deny-to-ask", lambda p: (_remove((*PERMS, "deny"), "Bash(ssh-ro *)")(p), _add((*PERMS, "ask"), "Bash(ssh-ro *)")(p)), "added ask:Bash(ssh-ro *)"),
    ("new-directory", _add((*PERMS, "additionalDirectories"), "/etc"), "added additionalDirectories:/etc"),
    ("dropped-guard", lambda p: p["settings"]["hooks"]["PreToolUse"].pop(0), "removed hook:PreToolUse|Bash|"),
    ("approval-mode", _put(("bash_approval",), "rules"), "added bash_approval:rules"),
    ("new-domain", _add((*BOX, "network", "allowedDomains"), "example.com"), "added sandbox.allowedDomains:example.com"),
    ("new-exclusion", _add((*BOX, "excludedCommands"), "git *"), "added sandbox.excludedCommands:git *"),
    ("new-allow-read", _add((*FS, "allowRead"), "/home/digi"), "added sandbox.allowRead:/home/digi"),
    ("dropped-deny-read", _remove((*FS, "denyRead"), "/home/digi"), "removed sandbox.denyRead:/home/digi"),
    ("dropped-deny-write", _remove((*FS, "denyWrite"), "/opt/digipaddock"), "removed sandbox.denyWrite:/opt/digipaddock"),
    ("weaker-flag", _put((*BOX, "allowUnsandboxedCommands"), True), "added sandbox.flag:allowUnsandboxedCommands=true"),
    ("write-acl", _put(("acl", 2, "access"), "write"), "added acl:write:forge-journal"),
    ("no-sandbox", _put(("launch_settings",), None), "removed sandbox:enabled"),
]
# fmt: on


@pytest.mark.parametrize(
    ("tamper", "expected"),
    [pytest.param(t, e, id=name) for name, t, e in WIDENINGS],
)
def test_compare_views_flags_every_widening(tamper: Tamper, expected: str) -> None:
    policy = _compile("dev")
    wider = copy.deepcopy(policy)
    tamper(wider)
    differences = compare_views(_view(policy), _view(wider))
    flagged = [f"{d.change} {d.item}" for d in differences if d.widens]
    assert any(item.startswith(expected) for item in flagged), differences


def test_ask_to_allow_widens() -> None:
    # No held wrapper asks since the change (kd-wake-self went); an ask that becomes an allow still widens.
    asking = _compile("sysadmin")
    assert asking["settings"]["permissions"]["ask"] == []
    _add((*PERMS, "ask"), "Bash(kd-zz *)")(asking)
    wider = copy.deepcopy(asking)
    _remove((*PERMS, "ask"), "Bash(kd-zz *)")(wider)
    _add((*PERMS, "allow"), "Bash(kd-zz *)")(wider)
    assert Difference("removed", "ask:Bash(kd-zz *)", True) in compare_views(
        _view(asking), _view(wider)
    )


def test_compare_views_sees_narrowing_and_equal_spellings() -> None:
    policy = _compile("sysadmin")
    view = _view(policy)
    assert compare_views(view, view) == []
    narrower = copy.deepcopy(policy)
    _remove((*PERMS, "allow"), "Glob")(narrower)
    _add((*PERMS, "deny"), "Bash(kd-monitor trust *)")(narrower)
    assert compare_views(view, _view(narrower)) == [
        Difference("added", "deny:Bash(kd-monitor trust *)", False),
        Difference("removed", "allow:Glob", False),
    ]
    # A sandbox where there was none only narrows: one structural difference.
    unsandboxed = {**view, "sandbox": None}
    assert compare_views(unsandboxed, view) == [
        Difference("added", "sandbox.flag:network.allowAllUnixSockets=true", True),
        Difference("added", "sandbox:enabled", False),
    ]


def test_weak_sandbox_over_no_sandbox_widens() -> None:
    # An unsandboxed view (the sysadmin before M02): a fail-closed sandbox only narrows it,
    # but one that auto-allows Bash would skip the prompts it asked for.
    policy = _compile("sysadmin")
    unsandboxed = {**_view(policy), "sandbox": None}
    # the channel sockets are the one flag wider than fail-closed.
    assert compare_views(unsandboxed, _view(policy)) == [
        Difference("added", "sandbox.flag:network.allowAllUnixSockets=true", True),
        Difference("added", "sandbox:enabled", False),
    ]
    weaker = copy.deepcopy(policy)
    _put((*BOX, "autoAllowBashIfSandboxed"), True)(weaker)
    assert Difference(
        "added", "sandbox.flag:autoAllowBashIfSandboxed=true", True
    ) in compare_views(unsandboxed, _view(weaker))


def test_checker_needs_the_host_agents() -> None:
    registry = _registry()
    policy = _compile("boss", registry)
    with pytest.raises(PolicyError, match="host agents must include this agent"):
        check_policy(
            policy, registry, floor=FLOOR, agents=_host(_manifest("dev"))
        )


@pytest.mark.parametrize(
    "field, value",
    [
        ("capabilities", ("board.read", "board.coordinate", "agent.nudge")),
        ("model", "opus"),
        ("model", None),
        ("profile", "developer"),
        ("sandbox_profile", "developer-v1"),
        ("effort", "high"),
    ],
)
def test_checker_holds_the_policy_to_its_manifest(field: str, value: Any) -> None:
    # A policy valid for the registry but not the one its manifest compiles to: the
    # installer and postflight must refuse it, the manifest is the source of truth.
    registry = _registry()
    policy = _compile("boss", registry)
    host = dataclasses.replace(_manifest("boss"), **{field: value})
    with pytest.raises(PolicyError, match="differs from the host manifest"):
        check_policy(policy, registry, floor=FLOOR, agents=_host(host))


def test_manifest_effort_is_compiled_only_when_set() -> None:
    # without the field the policy keeps its old fields (installs stay valid);
    # with it the policy carries it, and the checker holds both sides to the manifest.
    registry = _registry()
    plain = _compile("boss", registry)
    assert "effort" not in plain
    host = _host(dataclasses.replace(_manifest("boss"), effort="high"))
    policy = compile_policy(host["boss"], registry, floor=FLOOR, agents=host)
    assert policy["effort"] == "high"
    assert {k: v for k, v in policy.items() if k != "effort"} == plain
    with pytest.raises(PolicyError, match="differs from the host manifest in: effort$"):
        check_policy(policy, registry, floor=FLOOR, agents=_host(_manifest("boss")))
    for bad in ("ultra", None, "--verbose"):
        broken = {**policy, "effort": bad}
        with pytest.raises(PolicyError, match="effort must be a Claude Code effort level"):
            check_policy(broken, registry, floor=FLOOR, agents=host)


def test_thread_models_are_compiled_only_when_set() -> None:
    # An agent without the field compiles byte for byte as before; with it the policy carries
    # it, and the checker holds both sides to the manifest.
    registry = _registry()
    plain = _compile("boss", registry)
    assert "thread_models" not in plain
    host = _host(dataclasses.replace(_manifest("boss"), thread_models=(("monitor", "claude-sonnet-5-5"),)))
    policy = compile_policy(host["boss"], registry, floor=FLOOR, agents=host)
    assert policy["thread_models"] == {"monitor": "claude-sonnet-5-5"}
    assert {k: v for k, v in policy.items() if k != "thread_models"} == plain
    with pytest.raises(PolicyError, match="differs from the host manifest in: thread_models$"):
        check_policy(policy, registry, floor=FLOOR, agents=_host(_manifest("boss")))
    for bad in ({}, {"monitor": "--verbose"}, {"chat": "sonnet"}, "sonnet"):
        with pytest.raises(PolicyError, match="thread_models"):
            check_policy({**policy, "thread_models": bad}, registry, floor=FLOOR, agents=host)


def test_checker_refuses_rights_the_manifest_does_not_select() -> None:
    # The review reproduction: a narrower boss manifest without agent.nudge and usage.read
    # must not accept the policy compiled from the full one.
    registry = _registry()
    policy = _compile("boss", registry)
    narrow = dataclasses.replace(
        _manifest("boss"), capabilities=("board.read", "board.coordinate")
    )
    with pytest.raises(PolicyError, match="differs from the host manifest in: capabilities$"):
        check_policy(policy, registry, floor=FLOOR, agents=_host(narrow))
    # The policy compiled from that manifest passes the same check.
    check_policy(
        _compile("boss", registry, manifest=narrow),
        registry,
        floor=FLOOR,
        agents=_host(narrow),
    )


def test_mode_change_widens_where_the_floor_does_not_pin_it() -> None:
    # On this host the managed floor pins the auto and bypass switches, so an agent cannot
    # change one; without that floor a changed mode is a widening. defaultMode is the
    # compiled policy's own (a managed one beats --settings): changing it widens.
    policy = _compile("boss")
    auto = copy.deepcopy(policy)
    _put((*PERMS, "disableAutoMode"), "enable")(auto)
    looser = copy.deepcopy(policy)
    _put((*PERMS, "defaultMode"), "acceptEdits")(looser)

    def view(item: Policy) -> dict[str, Any]:
        return effective_view(
            floor={},
            settings=item["settings"],
            launch_settings=item["launch_settings"],
            bash_approval=item["bash_approval"],
            acl=[],
        )

    assert compare_views(_view(policy), _view(auto)) == []
    assert Difference("added", 'mode:disableAutoMode="enable"', True) in (
        compare_views(view(policy), view(auto))
    )
    assert Difference("added", 'mode:defaultMode="acceptEdits"', True) in (
        compare_views(_view(policy), _view(looser))
    )


def test_dont_ask_narrows_the_default_mode() -> None:
    # dontAsk refuses what default would ask the owner; the reverse widens.
    strict = _compile("boss")
    asking = copy.deepcopy(strict)
    _put((*PERMS, "defaultMode"), "default")(asking)
    assert all(not d.widens for d in compare_views(_view(asking), _view(strict)))
    assert Difference("added", 'mode:defaultMode="default"', True) in compare_views(
        _view(strict), _view(asking)
    )


# --- golden snapshots against the policy baseline -------------------------------------
#
# fixtures/policy/<agent>.policy.json pin the compiled shadow policy of sysadmin and dev;
# fixtures/policy/baseline.json (policy_baseline) freezes what they granted on 29.10.2026.
# A drifted snapshot is updated only after reviewing its diff; one that widens the
# baseline also lists the widening, with its reason, in baseline.json.


def _snapshot(name: str) -> Policy:
    return json.loads((FIXTURES / f"{name}.policy.json").read_text("utf-8"))


def test_repository_manifests_validate_together() -> None:
    assert set(LEGACY) <= set(validate_all(ROOT))


@pytest.mark.parametrize("name", LEGACY)
def test_fixture_manifest_describes_the_real_agent(name: str) -> None:
    # The plan, launch and add_user tests build synthetic hosts from the fixture copies:
    # they must select the same rights as the real manifest.
    assert load_manifest(FIXTURES / "agents" / name / "agent.json") == _manifest(name)


@pytest.mark.parametrize("name", LEGACY)
def test_golden_snapshot_matches_the_compiler(name: str) -> None:
    expected = (FIXTURES / f"{name}.policy.json").read_bytes()
    actual = render_policy(_compile(name))
    if actual != expected:
        diff = difflib.unified_diff(
            expected.decode("utf-8").splitlines(keepends=True),
            actual.decode("utf-8").splitlines(keepends=True),
            "snapshot",
            "compiled",
        )
        pytest.fail(f"{name}: review, then update the snapshot\n{''.join(diff)}")


@pytest.mark.parametrize("name", LEGACY)
def test_snapshot_passes_the_checker(name: str) -> None:
    check_policy(
        _snapshot(name), _registry(), floor=FLOOR, agents=_host(_manifest(name))
    )


@pytest.mark.parametrize("name", LEGACY)
def test_snapshot_never_widens_the_baseline(name: str) -> None:
    listed = policy_baseline.load()["widenings"].get(name, {})
    assert policy_baseline.widenings(name, _snapshot(name), FLOOR) == sorted(listed)


def test_every_listed_widening_has_its_reason() -> None:
    baseline = policy_baseline.load()
    assert set(baseline["agents"]) == set(LEGACY) == set(policy_baseline.AGENTS)
    for name, listed in baseline["widenings"].items():
        assert name in LEGACY
        assert all(isinstance(reason, str) and reason.strip() for reason in listed.values()), name


def test_the_baseline_sees_a_widening_and_not_a_narrowing() -> None:
    wider = _snapshot("dev")
    _permissions(wider)["allow"].append("Bash(curl *)")
    wider["channels"][0]["programs"].append("/opt/digipaddock/agents/dev/bin/kd-pr")
    floor = copy.deepcopy(FLOOR)
    floor["permissions"]["deny"].remove("Edit(//home/digi-boss/.bashrc)")
    assert policy_baseline.widenings("dev", wider, floor) == [
        "added acl:read:code-mirror",  # listed in baseline.json (git.mirror)
        "added additionalDirectories:/srv/digipaddock-mirror",
        "added allow:Bash(curl *)",
        "added allow:Edit(//srv/digipaddock-agents/dev/memory/chats/**)",  # listed
        "added allow:Edit(//srv/digipaddock-agents/dev/memory/tasks/**)",
        "added channel:base:digi:kd-pr",
        "added channel:data:digi-data:kd-data",  # listed (data.read)
        "added channel:self:digi-dev:kd-data *",
        "added channel:self:digi-dev:kd-forge status",  # listed (the checks of a commit, read-only)
        "added channel:self:digi-dev:kd-mirror-fetch *",
        "added channel:self:digi-dev:kd-notes add",
        "added channel:self:digi-dev:kd-work council",  # listed (the writer and its card's council)
        "added channel:self:digi-dev:kd-work council-reply",
        "removed deny:Edit(//home/digi-boss/.bashrc)",
    ]
    narrower = _snapshot("dev")
    _permissions(narrower)["allow"] = []
    narrower["channels"] = []
    narrower["launch_settings"]["sandbox"]["network"]["allowedDomains"] = []
    narrower["acl"] = []
    narrower["settings"]["permissions"]["additionalDirectories"] = []
    assert policy_baseline.widenings("dev", narrower, FLOOR) == []


def test_a_read_grant_in_place_of_a_write_grant_is_a_narrowing() -> None:
    # write on the board became read; the read was already inside the write.
    old = _view(_snapshot("dev"))
    new = copy.deepcopy(old)
    old["acl"], new["acl"] = {"write:board-tasks"}, {"read:board-tasks"}
    found = {f"{d.change} {d.item}": d.widens for d in compare_views(old, new)}
    assert found == {"removed acl:write:board-tasks": False}
    assert [d.widens for d in compare_views(new, old)] == [True]


def test_sysadmin_sandbox_is_sysadmin_v1() -> None:
    snapshot = _snapshot("sysadmin")
    assert snapshot["sandbox_profile"] == "sysadmin-v1"
    sandbox = _sandbox(snapshot)
    assert sandbox["network"]["allowedDomains"] == []
    assert sandbox["network"]["allowUnixSockets"] == []
    # nothing leaves the sandbox; the server, monitor and board tools reach channels.
    assert sandbox["excludedCommands"] == []
    (self_,) = [c for c in snapshot["channels"] if c["key"] == "self"]
    assert sorted(self_["commands"]) == [
        "kd-admin-critical",
        "kd-admin-propose",
        "kd-admin-propose-action",
        "kd-admin-run",
        "kd-diag",
        "kd-host-read",
        "kd-monitor",
        "kd-notes",
        "kd-task",
        "kd-work",
        "ssh-exec",
        "ssh-ro",
    ]


def test_sysadmin_reads_the_docs_directly() -> None:
    # M02: docs.read keeps the direct reads the sysadmin instructions rely on.
    docs = "Read(//srv/example-docs/**)"
    assert docs in _snapshot("sysadmin")["settings"]["permissions"]["allow"]
    # the text names the same directory as the grant, from host.json (servers.md comes from the overlay)
    assert "`{{host.docs_dir}}`" in (ROOT / "agents/sysadmin/servers.example.md").read_text("utf-8")


def test_executor_channels_are_the_reviewed_ones() -> None:
    # an executor is reached through its channel, not sudo. the panel
    # launch lines went with the panels.
    compiled: dict[str, set[str]] = {}
    for name in LEGACY:
        snapshot = _snapshot(name)
        for channel in snapshot["channels"]:
            if channel["key"] != "self":
                compiled.setdefault(snapshot["user"], set()).update(
                    f"{channel['run_as']}:{PurePosixPath(p).name}" for p in channel["programs"]
                )
        assert "sudoers" not in snapshot, name
    # every worker reaches the base user through kd-work; and through the
    # board executor kd-task; R31: dev through the diagnostics executor kd-diag; and the
    # factory's executor kd-factory; the host reader kd-host-read; the smoke suite kd-e2e;
    # the data user's kd-data.
    assert compiled["digi-dev"] == {
        "digi-data:kd-data",
        "digi-forge:forge_exec.py",
        "digi:kd-diag",
        "digi:kd-e2e",
        "digi:kd-factory",
        "digi:kd-task",
        "digi:kd-work",
        "digi-observe:kd-host-read",
        "digi:kd-permlog",
    }
    # M02: the sysadmin left the shared user; the monitor wakes it through the
    # cabinet, not kd-wake-self. kd-admin-run and kd-admin-critical; the
    # diagnostics executor kd-diag (the cabinet's push path, closed to ssh-ro).
    assert compiled["digi-sysadmin"] == {
        "digi-exec:ssh_exec.py",
        "digi-exec:admin_run.py",
        "digi-exec:admin_critical.py",
        "digi:kd-diag",
        "digi:kd-task",
        "digi:kd-work",
        "digi-observe:kd-host-read",
        "digi:kd-permlog",  # the private permission audit
    }


def test_bash_approval_is_the_guards() -> None:
    # Both guards approve Bash on success (dev's from the start, the sysadmin's since the change).
    for name, guard in (("dev", "agents/dev/dev_agent/guard.py"), ("sysadmin", "agents/sysadmin/sysadmin/guard.py")):
        assert _snapshot(name)["bash_approval"] == "validator"
        assert "allow_on_success=True" in (ROOT / guard).read_text("utf-8")


# The launch contract of an installed policy.json, one fingerprint per schema version.
# agent-launch refuses an installed policy whose fields, modes, sandbox or bytes no longer
# match the code: a release that changes them stops generated agents until reinstall.
LAUNCH_SCHEMA = {
    7: "0cda04974b2adfddf932d8ad977781a40806bcc027dd412325303873c185c676",  # native Anthropic API key scrub
    1: "6d505dc5d2a7a3a9408a60cf25803b9b6646b8972cdfa76a0baa6a4731139975",
    2: "ebd99702bac8975b68acf1d0015978d4940af19aaafe8b4fa7199bf3ddfbc7a6",  # channels
    3: "adbdab39367972f31ca5c02c3c9684ffc533c2619b990205e6c9b1615f2b2384",  # dontAsk
    4: "9665bd99cdc278709bbef8ccbad85ba9c118b4ce95c02da3b70fb6796e26f2b7",  # no telegram, no sudoers
    5: "016a61b1467c30377fb06e010a646c54c2fd04760b69daac43c143a55d6a7def",  # sandbox credentials deny
    6: "6b2b4f419a43addfda18b2c6d0e0830414633578c7f6dd2d615cdffcfdde70e9",  # the provider key too
}


def test_launch_schema_change_bumps_schema_version() -> None:
    from kd import policy
    from kd.registry import FAIL_CLOSED_SANDBOX

    surface = {
        "policy": sorted(policy.POLICY_FIELDS),
        "permissions": sorted(policy.PERMISSION_FIELDS),
        "permission_modes": policy.PERMISSION_MODES,
        "sandbox": sorted(policy.SANDBOX_FIELDS),
        "fail_closed": FAIL_CLOSED_SANDBOX,
        "filesystem": sorted(policy.FILESYSTEM_FIELDS),
        "network": sorted(policy.NETWORK_FIELDS),
        "render": render_policy({"b": ["é", 1], "a": {}}).decode("utf-8"),
    }
    digest = hashlib.sha256(json.dumps(surface, sort_keys=True).encode()).hexdigest()
    assert LAUNCH_SCHEMA.get(policy.SCHEMA_VERSION) == digest, (
        "the launch schema changed: raise policy.SCHEMA_VERSION, pin the new digest"
        f" {digest} here, and reinstall generated agents after the release"
    )


def test_docs_read_opens_the_docs_to_the_read_tool_only() -> None:
    policy = _compile("sysadmin")
    permissions = policy["settings"]["permissions"]
    docs = "/srv/example-docs"
    assert f"Read(/{docs}/**)" in permissions["allow"]
    assert docs in permissions["additionalDirectories"]
    assert f"Edit(/{docs}/**)" in permissions["deny"]
    assert not any(item["resource"] == "project-docs" for item in policy["acl"])


def test_sysadmin_reaches_the_base_user_only_through_kd_diag_kd_task_and_kd_work() -> None:
    policy = _compile("sysadmin")
    (base,) = [c for c in policy["channels"] if c["key"] == "base"]
    assert (base["run_as"], base["programs"], base["commands"]) == (
        "digi",
        [
            "/opt/digipaddock/common/bin/kd-diag",
            "/opt/digipaddock/common/bin/kd-permlog",  # the private permission audit
            "/opt/digipaddock/common/bin/kd-task",
            "/opt/digipaddock/common/bin/kd-work",
        ],
        {},
    )


def test_dev_reaches_the_owner_only_through_kd_work() -> None:
    policy = _compile("dev")
    (base,) = [c for c in policy["channels"] if c["key"] == "base"]
    assert (base["run_as"], base["programs"], base["commands"]) == (
        "digi",
        [
            "/opt/digipaddock/common/bin/kd-diag",
            "/opt/digipaddock/common/bin/kd-e2e",
            "/opt/digipaddock/common/bin/kd-factory",
            "/opt/digipaddock/common/bin/kd-permlog",  # the private permission audit
            "/opt/digipaddock/common/bin/kd-task",
            "/opt/digipaddock/common/bin/kd-work",
        ],
        {},
    )


def test_dev_policy_opens_kd_diag_on_the_base_channel() -> None:
    """R31: dev reaches the diagnostics of the catalog through kd-diag, its executor runs as the base user; the
    sysadmin holds it too, BOSS and the analyst do not."""
    policy = _compile("dev")
    (self_,) = [c for c in policy["channels"] if c["key"] == "self"]
    assert self_["commands"]["kd-diag"] == {
        "program": "/opt/digipaddock/common/bin/kd-diag",
        "args": [],
        "subcommands": ["check", "list"],
        "env": {},
    }
    assert "diag.request" in policy["capabilities"]
    assert "diag.request" in _compile("sysadmin")["capabilities"]
    for name in ("boss", "analyst"):
        assert "Bash(kd-diag *)" in _compile(name)["settings"]["permissions"]["deny"]


# --- capability channels --------------------------------------------------------


def test_channels_replace_capability_sudoers() -> None:
    policy = _compile("sysadmin")
    sandbox = _sandbox(policy)
    assert sandbox["excludedCommands"] == []
    assert sandbox["network"]["allowAllUnixSockets"] is True
    assert {"/run", "/var/run"} <= set(sandbox["filesystem"]["denyRead"])
    assert {"/run", "/var/run"} <= set(sandbox["filesystem"]["denyWrite"])
    assert "/run/digipaddock/cap/sysadmin" in sandbox["filesystem"]["allowRead"]
    assert [c["key"] for c in policy["channels"]] == ["base", "exec", "host", "self"]
    (self_,) = [c for c in policy["channels"] if c["key"] == "self"]
    assert (self_["run_as"], self_["programs"]) == ("digi-sysadmin", [])
    assert self_["commands"]["ssh-exec"] == {
        "program": "/opt/digipaddock/agents/sysadmin/bin/ssh-exec",
        "args": [],
        "subcommands": [],
        "env": {},
    }
    # kd-task reads and writes the board through the cabinet; it reaches the base executor by itself.
    assert "backlog" not in self_["commands"]
    assert self_["commands"]["kd-task"] == {
        "program": "/opt/digipaddock/common/bin/kd-task",
        "args": [],
        "subcommands": ["brief-review", "chat", "claim", "create", "edit", "file", "handoff", "list", "message", "projects", "result",
                        "scope", "search", "thread", "view", "waits"],
        "env": {},
    }
    (exec_,) = [c for c in policy["channels"] if c["key"] == "exec"]
    assert (exec_["run_as"], exec_["commands"]) == ("digi-exec", {})
    assert exec_["programs"] == [
        f"/opt/digipaddock/agents/sysadmin/bin/{name}"
        for name in ("admin_critical.py", "admin_run.py", "ssh_exec.py")
    ]
    assert "sudoers" not in policy


def test_sysadmin_bash_is_approved_by_its_guard() -> None:
    policy = _compile("sysadmin")
    assert policy["bash_approval"] == "validator"
    assert not [r for r in _permissions(policy)["allow"] if r.startswith("Bash")]
    assert "Bash(ssh-exec *)" not in _permissions(policy)["ask"]
    # The floor no longer asks for ssh-exec (a hook allow never overrides an ask).
    assert "Bash(ssh-exec *)" not in FLOOR["permissions"].get("ask", [])


def test_boss_gets_the_channel_hook() -> None:
    policy = _compile("boss")
    (hook,) = [h for h in policy["settings"]["hooks"]["PreToolUse"] if h["matcher"] == "Bash"]
    assert hook["hooks"] == [
        {
            "type": "command",
            "command": "/opt/digipaddock/common/bin/kd-cap-expect || exit 2",
            "timeout": 30,
        }
    ]
    permissions = _permissions(policy)
    assert "Bash(kd-boss-board promote *)" not in permissions["ask"] + permissions["allow"]
    assert "Bash(kd-boss-board propose-promote *)" in permissions["allow"]


@pytest.mark.parametrize("name", ["dev", "analyst", "sysadmin"])
def test_every_documented_kd_work_subcommand_reaches_the_channel(name: str) -> None:
    """Global review 15.10.2026: the agent template told agents to run kd-work ask/answer, and the base
    channel refused both — the capability's subcommands and the template must not drift again."""
    template = (ROOT / "common/agent-template/CLAUDE.common.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"`kd-work (\w+)", template))
    assert {"ask", "answer", "progress"} <= documented
    policy = _compile(name)
    (channel,) = [c for c in policy["channels"] if "kd-work" in c["commands"]]
    grant = cap_grants.parse(json.loads(cap_grants.render(policy["agent"], 1000, channel)), policy["agent"], channel["key"])
    for sub in documented | {"progress", "directive", "reply", "ask", "answer"}:
        cap_grants.command_for(grant, "kd-work", [sub, "KD-1"])
    with pytest.raises(cap_grants.Refused):
        cap_grants.command_for(grant, "kd-work", ["purge", "KD-1"])


@pytest.mark.parametrize("name", [*LEGACY, "boss"])
def test_channels_render_as_grant_files(name: str) -> None:
    policy = _compile(name)
    for channel in policy["channels"]:
        cap_grants.render(policy["agent"], 1000, channel)  # refuses what kd-cap-serve would


def test_unix_sockets_open_only_with_channels() -> None:
    registry = _registry()
    narrow = dataclasses.replace(
        _manifest("boss"), capabilities=("usage.read",)
    )
    policy = _compile("boss", registry, manifest=narrow)
    assert policy["channels"] == []
    assert _sandbox(policy)["network"]["allowAllUnixSockets"] is False
    assert "/run/digipaddock/cap/boss" not in _sandbox(policy)["filesystem"]["allowRead"]
    assert policy["settings"]["hooks"]["PreToolUse"] == []
    _put((*BOX, "network", "allowAllUnixSockets"), True)(policy)
    with pytest.raises(PolicyError, match="allowAllUnixSockets must be false"):
        check_policy(policy, registry, floor=FLOOR, agents=_host(narrow))


def test_analyst_policy_reads_the_checkouts_and_holds_no_shell(tmp_path: Path) -> None:
    # Read/Grep over /srv/digipaddock-repos by grant, history through kd-repo, no git and no network.
    path = tmp_path / "agents" / "analyst" / "agent.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "schema_version": 2, "name": "analyst", "model": "opus",
        "profile": "analyst", "capabilities": ["board.read", "board.write", "docs.read", "repo.read"],
        "runtime": {"sandbox_profile": "analyst-v1", "working_directory": "work"},
    }), encoding="utf-8")
    manifest = load_manifest(path)
    policy = compile_policy(manifest, _registry(), floor=FLOOR, agents=_host(manifest))
    permissions = policy["settings"]["permissions"]
    allow, deny = set(permissions["allow"]), set(permissions["deny"])
    assert {"Bash(kd-repo list *)", "Bash(kd-repo log *)", "Bash(kd-repo show *)", "Bash(kd-repo diff *)",
            "Bash(kd-repo blame *)"} <= allow
    assert "Read(//srv/digipaddock-repos/**)" in allow and "Edit(//srv/digipaddock-repos/**)" in deny
    assert not any(rule.startswith("Bash(git") for rule in allow)
    assert "/srv/digipaddock-repos" in permissions["additionalDirectories"]
    assert {"resource": "repo-checkouts", "path": "/srv/digipaddock-repos", "access": "read", "user": "digi-analyst"} in policy["acl"]
    assert policy["launch_settings"]["sandbox"]["network"]["allowedDomains"] == []
    assert policy["model"] == "opus"
    assert "sudoers" not in policy  # no capability sudo, no panel launch


def test_sysadmin_reads_source_without_developer_or_repository_credentials() -> None:
    policy = _compile("sysadmin")
    permissions = _permissions(policy)
    assert "repo.read" in policy["capabilities"]
    assert "git.develop" not in policy["capabilities"] and "forge.review" not in policy["capabilities"]
    assert "Read(//srv/digipaddock-repos/**)" in permissions["allow"]
    assert "Edit(//srv/digipaddock-repos/**)" in permissions["deny"]
    assert {"resource": "repo-checkouts", "path": "/srv/digipaddock-repos", "access": "read",
            "user": "digi-sysadmin"} in policy["acl"]
    assert not any(item["resource"] == "repo-checkouts" and item["access"] != "read" for item in policy["acl"])
    box = _sandbox(policy)
    assert box["network"]["allowedDomains"] == []
    assert "/srv/digipaddock-repos" in box["filesystem"]["denyWrite"]
    assert not any(".ssh" in path or "/forge" in path for path in box["filesystem"]["allowRead"])
    assert "Bash(kd-repo *)" not in permissions["deny"]
    assert "repo.read:wrappers" in policy["install_steps"]["install"]
    assert not any("forge_exec.py" in program for channel in policy["channels"] for program in channel["programs"])


def test_worker_request_wrapper_runs_only_kd_worker_through_the_base_channel() -> None:
    policy = _compile("boss")
    (base,) = [c for c in policy["channels"] if c["key"] == "base"]
    assert any(p.endswith("/common/bin/kd-worker") for p in base["programs"])
    assert "Bash(kd-worker *)" not in _permissions(policy)["deny"]
    assert not any("worker" in rule for rule in _permissions(policy)["allow"] if "Edit" in rule or "Read" in rule)
    assert "/var/lib/digipaddock/worker" not in json.dumps(_permissions(policy)["allow"])


@pytest.mark.parametrize("name", ("dev", "boss"))
def test_a_policy_holds_net_egress_and_opens_nothing_by_it(name: str) -> None:
    """The autonomy design, piece 2: the capability lets the launch add an exit — a card's or the host's
    standing one; the compiled policy itself opens no host and keeps the web tools denied."""
    policy = _compile(name)
    assert "net.egress" in policy["capabilities"]
    domains = policy["launch_settings"]["sandbox"]["network"]["allowedDomains"]
    assert not any("egress" in d or "*" == d for d in domains)
    permissions = _permissions(policy)
    assert {"WebFetch", "WebSearch"} <= set(permissions["deny"])
    assert not any(rule.startswith("WebFetch") for rule in permissions["allow"])
