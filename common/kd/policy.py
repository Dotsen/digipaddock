"""Compile an agent's normalized effective policy (BOSS spec 2026-10-06, section 5.2a).

``compile_policy`` turns a validated manifest and the validated registry into separate
artifacts: Claude Code settings (permissions and hooks), root-owned launch settings with
the sandbox, capability channels, ACL grants and capability install steps.
Rights come only from the selected capabilities; the platform base adds nothing but the
agent's own folder and notes and the memory tools.
Restrictions (deny rules, sandbox deny paths) come from the platform, the capabilities
and the resource table, and only ever narrow.

The result is deterministic (sorted lists; ``render_policy`` sorts keys) and passes
``check_policy`` before it is returned, so a compiler bug or tampered registry data
fails closed instead of producing a wider policy. Nothing here touches the filesystem.
``effective_view`` and ``compare_views`` compare two policies (the worker's with its agent's, a snapshot with
the tests' baseline).
"""

from __future__ import annotations

import json
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field as _field
from typing import Any

from kd import naming
from kd.agents import DEFAULT_USER
from kd.cap_client import CAP_DIR
from kd.manifest import (
    SERVICE_USERS,
    AgentManifest,
    ManifestError,
    check_effort,
    check_identity,
    check_thread_models,
)
from kd.registry import (
    DISABLE_ORDER,
    FAIL_CLOSED_SANDBOX,
    DIRECT_READS,
    GRANTS,
    INSTALL_ORDER,
    OWN_FILE_DIR,
    Entry,
    Registry,
    RegistryError,
    capability_union,
    guard_entry,
    is_within,
    validate_registry,
    validate_selection,
)

SCHEMA_VERSION = 7  # 2: channels; 3: defaultMode dontAsk; 4: no panels, no Telegram;
# 5: the sandbox unsets the host's Claude token; 6: a provider's key;
# 7: the native Anthropic API key; reinstall generated agents after release.
HOME_ROOT = "/home"
TOOL_OUTPUTS = ".claude/projects"  # the agent's sessions and their saved tool outputs
# the PreToolUse handler of an agent without a guard records its channel calls.
CAP_EXPECT_HOOK = "common/bin/kd-cap-expect"
# the PermissionRequest hook of the managed floor hands every agent's event to the base user, the one
# writer
# of the private permission audit; no Bash rule names it. It rides on an existing base channel (every agent of the
# host holds board.read): an agent without one opens no socket for it, and its hook reports the audit unavailable.
PERMISSION_AUDIT = "common/bin/kd-permlog"
# Probes P3: the sandbox sees no socket under /run but its own channel directory.
RUN_DIRS = ("/run", "/var/run")
CHANNEL_SOCKETS = "allowAllUnixSockets"
PLATFORM_BIN = "/usr/local/bin"
# The channel service starts a command with a clean environment; kd-task runs in the base channel and
# reaches the board through the cabinet socket.
CHANNEL_FIELDS = frozenset({"key", "run_as", "commands", "programs"})
GUARD_TIMEOUT = 30
# dontAsk: what no allow rule or PreToolUse allow approves is refused, never asked of the owner — also a call
# whose guard timed out (a probe on the factory host, 11.10.2026). The managed floor leaves the mode to this.
PERMISSION_MODES = {
    "defaultMode": "dontAsk",
    "disableAutoMode": "disable",
    "disableBypassPermissionsMode": "disable",
}
MEMORY_TOOLS = (
    "mcp__hindsight__hindsight_capture_initiative",
    "mcp__hindsight__hindsight_diagnose",
    "mcp__hindsight__hindsight_ingest_document",
    "mcp__hindsight__hindsight_list_knowledge_pages",
    "mcp__hindsight__hindsight_read_knowledge_page",
    "mcp__hindsight__hindsight_reflect",
    "mcp__hindsight__hindsight_search_knowledge_pages",
    "mcp__hindsight__hindsight_sync_status",
)
# A sandboxed shell reads its working tree; its Bash is approved by its guard.
SHELL_TOOLS = ("Glob", "Grep", "Read")
PLATFORM_DENY = (
    "Bash(rsync *)",
    "Bash(scp *)",
    "Bash(sftp *)",
    "Bash(ssh *)",
    "Bash(su *)",
    "Bash(sudo *)",
    # Claude Code loads the rules of a .claude/rules/ as instructions: no session writes one anywhere (KD-9482)
    "Edit(//**/.claude/rules/**)",
    "Monitor",
    "Read(//**/.git-credentials)",
    "Skill(hindsight-coding-agent)",
    "WebFetch",
    "WebSearch",
)
# git.develop: history, remotes and credentials stay with the wrappers; no package
# managers, fetchers or nested shells next to the guard.
DEVELOPER_DENY = (
    "Bash(apt*)",
    "Bash(bash -c *)",
    "Bash(bun *)",
    "Bash(curl *)",
    "Bash(git -C *)",
    "Bash(git -c *)",
    "Bash(git config *)",
    "Bash(git filter-branch*)",
    "Bash(git pull*)",
    "Bash(git push*)",
    "Bash(git rebase*)",
    "Bash(git remote *)",
    "Bash(git reset --hard*)",
    "Bash(git submodule*)",
    "Bash(git tag *)",
    "Bash(npm *)",
    "Bash(npx *)",
    "Bash(pip *)",
    "Bash(pip3 *)",
    "Bash(sh -c *)",
    "Bash(wget *)",
)
CAPABILITY_DENY = {
    "git.develop": DEVELOPER_DENY,
}
# The agent's own home: Claude login, memory client and SSH keys.
HOME_READ_DENY = (
    ".claude/.credentials.json",
    ".hindsight/**",
    ".ssh/**",
)
HOME_EDIT_DENY = (
    ".bash_profile",
    ".bashrc",
    ".bun/**",
    ".claude.json",
    ".claude/**",
    ".config/**",
    ".hindsight/**",
    ".local/**",
    ".profile",
    ".ssh/**",
)
HOME_SANDBOX_DENY_READ = (".claude", ".hindsight", ".ssh")
HOME_SANDBOX_DENY_WRITE = (
    ".bash_profile",
    ".bashrc",
    ".bun",
    ".claude",
    ".claude.json",
    ".config",
    ".local",
    ".profile",
    ".ssh",
)
# The agent never rewrites its own instructions or settings, in its folder or in work/.
AGENT_DIR_EDIT_DENY = (
    ".claude/**",
    "CLAUDE.md",
    "playbooks/**",
    "work/**/.claude/**",
    "work/**/.mcp.json",
    "work/.claude/**",
    "work/.mcp.json",
    "work/CLAUDE.local.md",
    "work/CLAUDE.md",
    # a repository at work/'s top (WORK_GIT below), and a CLAUDE.local.md anywhere under work/: Claude Code loads a
    # folder's CLAUDE.md and CLAUDE.local.md once a file there is read, so no session leaves one for a later session
    # to obey (KD-9482); a folder's CLAUDE.md is denied by WORK_INSTRUCTIONS below
    "work/.git",
    "work/.git/**",
    "work/**/CLAUDE.local.md",
    # the shared notes only through kd-notes add (appended under a lock), the split's original never
    "memory/notes.md",
    "memory/legacy/**",
)
# A repository at work/'s top would be the one git finds for the session's cwd, and Claude Code runs git there outside
# the sandbox (kd.launch refuses a launch while there is one). Clones live a level below, work/<Repo>/.git.
WORK_GIT = ".git"
# A folder's CLAUDE.md under work/: a developer's clones (work/<Repo>/…) keep their tracked CLAUDE.md editable for a
# pull request, so the holder of git.develop is denied it in its temp folder only, which holds no clone.
WORK_INSTRUCTIONS = "work/**/CLAUDE.md"
CLONE_HOLDER = "git.develop"
CLONE_HOLDER_INSTRUCTIONS = "work/tmp/**/CLAUDE.md"
# what a session edits of its folder — its own notes (memory/tasks/<card>.md, memory/chats/<thread>.md)
SESSION_NOTES = ("memory/chats/**", "memory/tasks/**")
SANDBOX_SHARED_NOTES = ("memory/legacy", "memory/notes.md")
WORK_PLACEHOLDERS = ("CLAUDE.local.md", "CLAUDE.md")
FORBIDDEN_TOOLS = frozenset(
    {"Monitor", "NotebookEdit", "Skill", "WebFetch", "WebSearch", "Write"}
)
# A Bash allow that starts one of these runs arbitrary code, privileges or network
# transfers; no wrapper form may be spelled as one of them.
FORBIDDEN_COMMANDS = frozenset(
    {
        "bash",
        "bun",
        "curl",
        "dash",
        "env",
        "eval",
        "exec",
        "nc",
        "node",
        "npm",
        "npx",
        "perl",
        "pip",
        "pip3",
        "python",
        "python3",
        "rsync",
        "ruby",
        "scp",
        "sftp",
        "sh",
        "socat",
        "ssh",
        "su",
        "sudo",
        "wget",
        "xargs",
        "zsh",
    }
)
POLICY_FIELDS = frozenset(
    {
        "schema_version",
        "agent",
        "user",
        "profile",
        "model",
        "capabilities",
        "sandbox_profile",
        "bash_approval",
        "settings",
        "launch_settings",
        "channels",
        "acl",
        "install_steps",
    }
)
# optional — present only for an agent whose manifest sets an effort.
EFFORT_FIELD = "effort"
# optional — ``{space: model}`` only for an agent whose manifest names a thread's own model.
THREAD_MODELS_FIELD = "thread_models"
OPTIONAL_FIELDS = (EFFORT_FIELD, THREAD_MODELS_FIELD)
PERMISSION_FIELDS = frozenset(
    {*PERMISSION_MODES, "additionalDirectories", "allow", "ask", "deny"}
)
SANDBOX_FLAGS = {k: v for k, v in FAIL_CLOSED_SANDBOX.items() if k != "network"}
SANDBOX_FIELDS = frozenset({*FAIL_CLOSED_SANDBOX, "excludedCommands", "filesystem"})
FILESYSTEM_FIELDS = frozenset({"allowRead", "denyRead", "denyWrite"})
NETWORK_FIELDS = frozenset({*FAIL_CLOSED_SANDBOX["network"], "allowedDomains"})


class PolicyError(ValueError):
    """A policy that must not be installed; ``errors`` lists every problem found."""

    def __init__(self, errors: str | Iterable[str]) -> None:
        self.errors = (errors,) if isinstance(errors, str) else tuple(errors)
        super().__init__("; ".join(self.errors))


def _path_rule(tool: str, path: str, kind: str) -> str:
    # Claude Code marks an absolute path with "//"; a directory rule covers what is below.
    return f"{tool}(/{path}/**)" if kind == "dir" else f"{tool}(/{path})"


def wrapper_rules(wrapper: Entry) -> list[str]:
    """The Bash permission rules for one wrapper's finite command forms."""
    command, forms = wrapper["command"], wrapper["subcommands"]
    if not forms:
        return [f"Bash({command} *)"]
    return [
        f"Bash({command} {form})"
        if form.endswith("--help")
        else f"Bash({command} {form} *)"
        for form in forms
    ]


@dataclass(frozen=True)
class _Agent:
    """Facts about one agent that both the compiler and the checker derive."""

    name: str
    user: str
    held: tuple[str, ...]
    code: str
    folder: str
    home: str
    work: str
    shell: bool
    guard: str | None


def _agent(registry: Registry, name: str, user: str, held: Iterable[str]) -> _Agent:
    layout = registry.resources.layout
    held = tuple(sorted(held))
    capabilities = registry.capabilities
    shell = any(capabilities[c]["shell"] == "sandboxed" for c in held)
    validators = sorted({capabilities[c]["validator"] for c in held} - {None})
    if len(validators) > 1:
        raise PolicyError(f"one agent runs one guard, not {', '.join(validators)}")
    guard = guard_entry(validators[0]) if validators else None
    if validators and guard is None:
        raise PolicyError(f"validator {validators[0]} has no hook entry")
    if shell and guard is None:
        raise PolicyError("a sandboxed shell needs its guard")
    folder = f"{layout['agents']}/{name}"
    return _Agent(
        name,
        user,
        held,
        layout["code"],
        folder,
        f"{HOME_ROOT}/{user}",
        f"{folder}/work",
        shell,
        guard,
    )


def _held_resources(registry: Registry, agent: _Agent) -> list[tuple[str, str]]:
    return sorted(
        {
            (item["name"], item["access"])
            for name in agent.held
            for item in registry.capabilities[name]["resources"]
        }
    )


def _channels(agent: _Agent, registry: Registry) -> list[Entry]:
    """the self channel runs the agent's channel commands as the agent itself;
    one channel per executor user runs the executors its held wrappers reach."""
    capabilities = registry.capabilities
    wrappers = [w for name in agent.held for w in capabilities[name]["wrappers"]]
    commands = {}
    for command in sorted(capability_union(capabilities, agent.held, "channel_commands")):
        own = [w for w in wrappers if w["command"] == command]
        sources = {w["source"] for w in own}
        if len(sources) != 1:
            raise PolicyError(f"channel command {command} is not a held wrapper")
        (source,) = sources
        commands[command] = {
            "program": f"{PLATFORM_BIN}/{command}"
            if source == "platform"
            else f"{agent.code}/{source}",
            "args": [],
            "subcommands": []
            if any(not w["subcommands"] for w in own)
            else sorted({form for w in own for form in w["subcommands"]}),
            "env": {},
        }
    channels = []
    if commands:
        channels.append(
            {"key": "self", "run_as": agent.user, "commands": commands, "programs": []}
        )
    keys = {
        naming.service_user("exec"): "exec",
        naming.service_user("forge"): "forge",
        naming.service_user("observe"): "host",
        naming.service_user("data"): "data",
        DEFAULT_USER: "base",
    }
    executors: dict[str, set[str]] = {}
    for wrapper in wrappers:
        if wrapper["run_as"] != "self":
            executors.setdefault(wrapper["run_as"], set()).add(
                f"{agent.code}/{wrapper['executor']}"
            )
    if DEFAULT_USER in executors:
        executors[DEFAULT_USER].add(f"{agent.code}/{PERMISSION_AUDIT}")
    for run_as, programs in sorted(executors.items()):
        if run_as not in keys:
            raise PolicyError(f"no channel runs as {run_as}")
        channels.append(
            {"key": keys[run_as], "run_as": run_as, "commands": {}, "programs": sorted(programs)}
        )
    return sorted(channels, key=lambda channel: channel["key"])


def _channel_reads(agent: _Agent, channels: list[Entry]) -> set[str]:
    """The one directory under /run the sandbox sees: the agent's own channel socket."""
    return {f"{CAP_DIR}/{agent.name}"} if channels else set()


def _tool_outputs(agent: _Agent) -> set[str]:
    """The one directory under the hidden ~/.claude the sandbox sees: Claude Code saves a long tool output
    there and shows the agent only its start and the path. Its login and channels stay hidden."""
    return {f"{agent.home}/{TOOL_OUTPUTS}"}


def _hooks(agent: _Agent, channels: list[Entry]) -> list[Entry]:
    hooks = []
    if agent.guard is None and any(c["key"] == "self" for c in channels):
        expect = f"{agent.code}/{CAP_EXPECT_HOOK} || exit 2"
        hooks.append(
            {
                "matcher": "Bash",
                "hooks": [{"type": "command", "command": expect, "timeout": GUARD_TIMEOUT}],
            }
        )
    if agent.guard is not None:
        guard = f"{agent.code}/{agent.guard} || exit 2"
        hooks.append(
            {
                "matcher": "Bash",
                "hooks": [{"type": "command", "command": guard, "timeout": GUARD_TIMEOUT}],
            }
        )
    return hooks


@dataclass
class _Rules:
    """Rules collected while compiling; every field ends up sorted in the policy."""

    allow: set[str] = _field(default_factory=set)
    ask: set[str] = _field(default_factory=set)
    deny: set[str] = _field(default_factory=set)
    directories: set[str] = _field(default_factory=set)
    allow_read: set[str] = _field(default_factory=set)
    deny_read: set[str] = _field(default_factory=set)
    deny_write: set[str] = _field(default_factory=set)
    acl: dict[str, str] = _field(default_factory=dict)


def _host_folders(registry: Registry, agents: Mapping[str, AgentManifest]) -> list[str]:
    root = registry.resources.layout["agents"]
    return [f"{root}/{item.name}" for item in agents.values()]


def _platform_rules(
    rules: _Rules,
    agent: _Agent,
    registry: Registry,
    others: Iterable[str],
    folders: Iterable[str] = (),
) -> None:
    """The base every agent gets: its own folder and notes, memory tools;
    and the restrictions that hold for every agent on the host (the other agents' homes
    and folders: their notes, texts, patches and owner chat are theirs — global review 15.10.2026)."""
    rules.allow |= {
        f"Read(/{agent.folder}/**)",
        *(f"Edit(/{agent.folder}/{item})" for item in SESSION_NOTES),
        *MEMORY_TOOLS,
    }
    rules.deny |= set(PLATFORM_DENY)
    rules.deny |= {f"Edit(/{agent.folder}/{item})" for item in AGENT_DIR_EDIT_DENY}
    instructions = CLONE_HOLDER_INSTRUCTIONS if CLONE_HOLDER in agent.held else WORK_INSTRUCTIONS
    rules.deny.add(f"Edit(/{agent.folder}/{instructions})")
    rules.deny_write |= {f"{agent.folder}/{item}" for item in SANDBOX_SHARED_NOTES}
    rules.deny |= {f"Read(/{agent.home}/{item})" for item in HOME_READ_DENY}
    rules.deny |= {f"Edit(/{agent.home}/{item})" for item in HOME_EDIT_DENY}
    rules.deny_read |= {f"{agent.home}/{item}" for item in HOME_SANDBOX_DENY_READ}
    rules.deny_write |= {f"{agent.home}/{item}" for item in HOME_SANDBOX_DENY_WRITE}
    rules.deny_write |= {f"{agent.work}/{item}" for item in (*WORK_PLACEHOLDERS, WORK_GIT)}
    rules.deny_read |= set(RUN_DIRS)
    rules.deny_write |= set(RUN_DIRS)
    rules.deny.add(_path_rule("Edit", agent.code, "dir"))
    rules.deny_write.add(agent.code)
    for user in sorted(set(others) - {agent.user}):
        home = f"{HOME_ROOT}/{user}"
        rules.deny |= {_path_rule("Read", home, "dir"), _path_rule("Edit", home, "dir")}
        rules.deny_read.add(home)
    for folder in sorted(set(folders) - {agent.folder}):
        rules.deny |= {_path_rule("Read", folder, "dir"), _path_rule("Edit", folder, "dir")}
        rules.deny_read.add(folder)
    table = registry.resources
    for item in table.protected:
        if item.kind == "pattern":
            rules.deny |= {f"{tool.title()}(/{item.path})" for tool in item.deny}
            continue
        if "read" in item.deny:
            rules.deny.add(_path_rule("Read", item.path, item.kind))
            rules.deny_read.add(item.path)
        if "edit" in item.deny:
            rules.deny.add(_path_rule("Edit", item.path, item.kind))
            rules.deny_write.add(item.path)
    for resource in table.resources.values():
        # A secret in another user's home is already behind that home's deny.
        if (
            resource.secret
            and resource.path is not None
            and not _within_any(resource.path, rules.deny_read)
        ):
            rules.deny.add(_path_rule("Read", resource.path, resource.kind))
            rules.deny_read.add(resource.path)


def _wrapper_permissions(rules: _Rules, agent: _Agent, registry: Registry) -> None:
    """Allow or ask for the held wrappers; deny every wrapper the agent does not hold."""
    capabilities = registry.capabilities
    if agent.shell:
        rules.allow |= {*SHELL_TOOLS, f"Edit(/{agent.work}/**)"}
    held_commands = set()
    for name in agent.held:
        rules.deny |= set(CAPABILITY_DENY.get(name, ()))
        for wrapper in capabilities[name]["wrappers"]:
            held_commands.add(wrapper["command"])
            forms = set(wrapper_rules(wrapper))
            if wrapper["approval"] == "ask":
                rules.ask |= forms
            elif not agent.shell:
                # Beside a guard that approves Bash, an allow rule would let a command
                # through unchecked whenever the hook times out.
                rules.allow |= forms
    for name, entry in sorted(capabilities.items()):
        if name in agent.held:
            continue
        for wrapper in entry["wrappers"]:
            if wrapper["command"] in held_commands:
                rules.deny |= set(wrapper_rules(wrapper))  # a split tool's other forms
            else:
                rules.deny.add(f"Bash({wrapper['command']} *)")


def _sandbox_own_files(registry: Registry, agent: _Agent) -> set[str]:
    """Own files a capability needs inside the sandbox: only one whose shell runs there
    and whose tools run there too, not in a channel.

    The sysadmin's read-only key is used by ssh-ro in its channel, outside the sandbox, so
    it stays behind the deny on .ssh; dev's kd-pr pushes from inside it.
    """
    table = registry.resources.resources
    return {
        path
        for name in agent.held
        if registry.capabilities[name]["shell"] == "sandboxed"
        and not registry.capabilities[name]["sandbox_ceiling"]["channel_commands"]
        for item in registry.capabilities[name]["resources"]
        if item["access"] == "own-file"
        for path in table[item["name"]].paths(agent.home, agent.name)
    }


def _resource_grants(rules: _Rules, agent: _Agent, registry: Registry) -> None:
    """Own files open in a sandboxed shell; granted paths are read here and written
    only through their tools; public data stays readable for the agent's wrappers."""
    table = registry.resources.resources
    held = _held_resources(registry, agent)
    rules.allow_read |= _sandbox_own_files(registry, agent)
    for name, access in held:
        resource = table[name]
        if access in DIRECT_READS:
            rules.allow.add(_path_rule("Read", resource.path, resource.kind))
            rules.deny.add(_path_rule("Edit", resource.path, resource.kind))
            rules.deny_write.add(resource.path)
            if resource.kind == "dir":
                rules.directories.add(resource.path)
        if access in GRANTS:
            level = "write" if access == "write-grant" else "read"
            rules.acl[name] = "write" if rules.acl.get(name) == "write" else level
    for name, access in held:
        path = table[name].path
        if access == "read-public" and path and _within_any(path, rules.deny_read):
            rules.allow_read.add(path)


def compile_policy(
    manifest: AgentManifest,
    registry: Registry,
    *,
    floor: Mapping[str, Any],
    agents: Mapping[str, AgentManifest],
) -> dict[str, Any]:
    """Build and check the effective policy of one agent; pure, raises PolicyError.

    ``registry`` comes from ``load_registry`` and is validated here again (spec 5.2a:
    the compiler never trusts unchecked registry data); ``floor`` is the host's managed
    settings and ``agents`` every manifest on the host, this one included (other
    agents' homes are denied to this one).
    """
    try:
        validate_registry(registry)
    except RegistryError as exc:
        raise PolicyError(exc.errors) from exc
    return _compile_policy(manifest, registry, floor=floor, agents=agents)


def _compile_policy(
    manifest: AgentManifest,
    registry: Registry,
    *,
    floor: Mapping[str, Any],
    agents: Mapping[str, AgentManifest],
) -> dict[str, Any]:
    """compile_policy after the registry check; tests call it with registry data that
    the check would refuse, to prove the compiler's own refusals."""
    _check_identity(manifest.name, manifest.user, manifest.model)
    _check_host(agents)
    if agents.get(manifest.name) != manifest:
        raise PolicyError("the host agents must include this manifest")
    users = [item.user for item in agents.values()]
    if len(users) != len(set(users)):
        raise PolicyError("two host agents share one user")
    try:
        validate_selection(
            registry,
            manifest.profile,
            manifest.capabilities,
            manifest.sandbox_profile,
        )
    except RegistryError as exc:
        raise PolicyError(exc.errors) from exc
    agent = _agent(
        registry,
        manifest.name,
        manifest.user,
        manifest.capabilities,
    )
    capabilities = registry.capabilities
    channels = _channels(agent, registry)
    rules = _Rules()
    _platform_rules(rules, agent, registry, _host_users(agents), _host_folders(registry, agents))
    _wrapper_permissions(rules, agent, registry)
    _resource_grants(rules, agent, registry)
    rules.allow_read |= _channel_reads(agent, channels) | _tool_outputs(agent)

    settings = json.loads(
        json.dumps(registry.sandboxes[manifest.sandbox_profile]["settings"])
    )
    sandbox = {key: value for key, value in settings.items() if key != "network"}
    # nothing leaves the sandbox; a channel command reaches its service.
    sandbox["excludedCommands"] = []
    sandbox["filesystem"] = {
        "allowRead": sorted(rules.allow_read),
        "denyRead": sorted(rules.deny_read),
        "denyWrite": sorted(rules.deny_write),
    }
    sandbox["network"] = {
        **settings["network"],
        CHANNEL_SOCKETS: bool(channels),
        "allowedDomains": sorted(
            capability_union(capabilities, agent.held, "network_domains")
        ),
    }
    policy = {
        "schema_version": SCHEMA_VERSION,
        **_manifest_fields(manifest),
        "bash_approval": "validator" if agent.shell else "rules",
        "settings": {
            "permissions": {
                **PERMISSION_MODES,
                "additionalDirectories": sorted(rules.directories),
                "allow": sorted(rules.allow),
                "ask": sorted(rules.ask),
                "deny": sorted(rules.deny),
            },
            "hooks": {"PreToolUse": _hooks(agent, channels)},
        },
        "launch_settings": {"sandbox": sandbox},
        "channels": channels,
        "acl": [
            {
                "user": agent.user,
                "resource": name,
                "path": registry.resources.resources[name].path,
                "access": access,
            }
            for name, access in sorted(rules.acl.items())
        ],
        "install_steps": _install_steps(registry, agent.held),
    }
    _check_policy(policy, registry, floor=floor, agents=agents)
    return policy


def _install_steps(registry: Registry, held: Iterable[str]) -> dict[str, list[str]]:
    """``capability:step`` lifecycle steps: grants and channels go last on install and
    first on disable."""
    lifecycle = {name: registry.capabilities[name]["lifecycle"] for name in held}
    return {
        phase: [
            f"{name}:{step}"
            for step in order
            for name in sorted(lifecycle)
            if step in lifecycle[name][phase]
        ]
        for phase, order in (
            ("install", INSTALL_ORDER),
            ("check", INSTALL_ORDER),
            ("disable", DISABLE_ORDER),
        )
    }


def render_policy(policy: Mapping[str, Any]) -> bytes:
    """The canonical bytes of a policy: two runs over the same input are identical."""
    text = json.dumps(policy, indent=2, sort_keys=True, ensure_ascii=False)
    return (text + "\n").encode("utf-8")


# --- rule coverage --------------------------------------------------------------------


def _split_rule(rule: str) -> tuple[str, str | None]:
    if rule.endswith(")") and "(" in rule:
        tool, _, rest = rule.partition("(")
        return tool, rest[:-1]
    return rule, None


def _bash_kind(pattern: str) -> tuple[str, str]:
    if pattern.endswith(" *") and "*" not in pattern[:-2]:
        return "word", pattern[:-2]
    if pattern.endswith("*") and "*" not in pattern[:-1]:
        return "prefix", pattern[:-1]
    if "*" not in pattern:
        return "exact", pattern
    return "opaque", pattern


def _bash_covers(outer: str, inner: str) -> bool:
    # Conservative on purpose: a "x *" rule may or may not match a bare "x" in Claude
    # Code, so as the outer rule it is assumed not to, and as the inner rule it is
    # assumed to. A coverage claim therefore never hides a widening.
    okind, oprefix = _bash_kind(outer)
    ikind, iprefix = _bash_kind(inner)
    if "opaque" in (okind, ikind) or okind == "exact":
        return False
    if okind == "prefix":
        return iprefix.startswith(oprefix)
    return iprefix.startswith(oprefix + " ")


def _path_covers(outer: str, inner: str) -> bool:
    if not outer.endswith("/**") or "*" in outer[:-3]:
        return False
    base = outer[:-3]
    return inner == base or inner.startswith(base + "/")


def covers(outer: str, inner: str) -> bool:
    """True only when every call that ``inner`` matches is certainly matched by ``outer``."""
    if outer == inner:
        return True
    otool, opattern = _split_rule(outer)
    itool, ipattern = _split_rule(inner)
    if otool != itool:
        return False
    if opattern is None:
        return True
    if ipattern is None:
        return False
    if otool == "Bash":
        return _bash_covers(opattern, ipattern)
    return _path_covers(opattern, ipattern)


def _covered(rule: str, rules: Iterable[str]) -> bool:
    return any(covers(other, rule) for other in rules)


def _acl_covered(item: str, items: Iterable[str]) -> bool:
    """``access:resource`` is present when the same or a wider access is: write includes read."""
    access, _, resource = item.partition(":")
    present = set(items)
    return item in present or (access == "read" and f"write:{resource}" in present)


def _conflicts(
    allow: Collection[str],
    ask: Collection[str],
    deny: Collection[str],
    floor: Mapping[str, Any],
) -> list[str]:
    """allow/ask/deny contradictions in the policy and against the managed floor.

    A deny or ask that carves an exception out of a broader allow is intended; an allow
    that a deny or ask swallows, or an ask that a deny swallows, is a contradiction.
    The agent may still deny what the floor only asks for.
    """
    floor_ask = list(floor.get("ask", []))
    floor_deny = list(floor.get("deny", []))
    errors = []
    for rule in sorted(allow):
        if _covered(rule, [*deny, *floor_deny]):
            errors.append(f"allow {rule} conflicts with a deny rule")
        if _covered(rule, [*ask, *floor_ask]):
            errors.append(f"allow {rule} conflicts with an ask rule")
    for rule in sorted(ask):
        if _covered(rule, [*deny, *floor_deny]):
            errors.append(f"ask {rule} conflicts with a deny rule")
    return errors


# --- the checker ----------------------------------------------------------------------


def _is_sorted_strings(value: Any) -> bool:
    return (
        isinstance(value, list)
        and all(isinstance(item, str) for item in value)
        and value == sorted(set(value))
    )


def _keys(value: Any, expected: Collection[str], label: str) -> list[str]:
    if not isinstance(value, dict) or set(value) != set(expected):
        return [f"{label}: fields must be exactly {sorted(expected)}"]
    return []


def policy_fields(policy: Any) -> frozenset[str]:
    """The exact fields a policy must have: ``effort`` and ``thread_models`` only ride along when present."""
    if isinstance(policy, Mapping):
        return POLICY_FIELDS | {f for f in OPTIONAL_FIELDS if f in policy}
    return POLICY_FIELDS


def _structure(policy: Any) -> list[str]:
    errors = _keys(policy, policy_fields(policy), "policy")
    if errors:
        return errors
    if EFFORT_FIELD in policy:
        try:
            check_effort(policy[EFFORT_FIELD])
        except ManifestError:
            errors.append("policy: effort must be a Claude Code effort level")
    if THREAD_MODELS_FIELD in policy:
        try:
            check_thread_models(policy[THREAD_MODELS_FIELD])
        except ManifestError as exc:
            errors.append(f"policy: thread_models: {exc}")
    if type(policy["schema_version"]) is not int or policy["schema_version"] != SCHEMA_VERSION:
        errors.append("policy: unsupported schema_version")
    for field in ("agent", "user", "profile", "sandbox_profile", "bash_approval"):
        if not isinstance(policy[field], str):
            errors.append(f"policy: {field} must be text")
    if policy["model"] is not None and not isinstance(policy["model"], str):
        errors.append("policy: model must be text or null")
    if not _is_sorted_strings(policy["capabilities"]):
        errors.append("policy: capabilities must be sorted unique names")
    settings = policy["settings"]
    errors += _keys(settings, {"permissions", "hooks"}, "settings")
    if not errors:
        permissions = settings["permissions"]
        errors += _keys(permissions, PERMISSION_FIELDS, "permissions")
        errors += _keys(settings["hooks"], {"PreToolUse"}, "hooks")
        if not errors:
            for field in ("additionalDirectories", "allow", "ask", "deny"):
                if not _is_sorted_strings(permissions[field]):
                    errors.append(f"permissions: {field} must be sorted unique rules")
    launch = policy["launch_settings"]
    problems = _keys(launch, {"sandbox"}, "launch_settings")
    if not problems:
        sandbox = launch["sandbox"]
        problems = _keys(sandbox, SANDBOX_FIELDS, "sandbox")
        if not problems:
            problems += _keys(sandbox["filesystem"], FILESYSTEM_FIELDS, "filesystem")
            problems += _keys(sandbox["network"], NETWORK_FIELDS, "network")
        if not problems:
            lists = [
                ("excludedCommands", sandbox["excludedCommands"]),
                ("allowedDomains", sandbox["network"]["allowedDomains"]),
                *sandbox["filesystem"].items(),
            ]
            problems += [
                f"sandbox: {field} must be sorted unique entries"
                for field, value in lists
                if not _is_sorted_strings(value)
            ]
    errors += problems
    for field, keys in (
        ("channels", CHANNEL_FIELDS),
        ("acl", {"user", "resource", "path", "access"}),
    ):
        items = policy[field]
        if not isinstance(items, list) or any(_keys(i, keys, field) for i in items):
            errors.append(f"{field}: entries must have exactly {sorted(keys)}")
    errors += _keys(policy["install_steps"], {"install", "check", "disable"}, "steps")
    return errors


def _permission_errors(
    policy: Mapping[str, Any], registry: Registry, agent: _Agent
) -> list[str]:
    permissions = policy["settings"]["permissions"]
    capabilities, table = registry.capabilities, registry.resources
    errors = [
        f"permissions: {key} must be {value}"
        for key, value in PERMISSION_MODES.items()
        if permissions[key] != value
    ]
    if policy["bash_approval"] != ("validator" if agent.shell else "rules"):
        errors.append("bash_approval does not match the shell capabilities")
    granted = {
        name: table.resources[name]
        for name, access in _held_resources(registry, agent)
        if access in DIRECT_READS
    }
    allowed_bash, allowed_ask = set(), set()
    for name in agent.held:
        for wrapper in capabilities[name]["wrappers"]:
            target = allowed_ask if wrapper["approval"] == "ask" else allowed_bash
            target |= set(wrapper_rules(wrapper))
    read_rules = {f"Read(/{agent.folder}/**)"} | {
        _path_rule("Read", r.path, r.kind) for r in granted.values()
    }
    edit_rules = {f"Edit(/{agent.folder}/{item})" for item in SESSION_NOTES}
    tools = set(MEMORY_TOOLS)
    if agent.shell:
        tools |= set(SHELL_TOOLS)
        edit_rules.add(f"Edit(/{agent.work}/**)")
    for rule in permissions["allow"]:
        tool, pattern = _split_rule(rule)
        if tool in FORBIDDEN_TOOLS or tool.startswith("mcp__") and "*" in tool:
            errors.append(f"dangerous allow {rule}")
        elif tool == "Bash":
            if pattern is None or pattern.split(" ", 1)[0] in FORBIDDEN_COMMANDS:
                errors.append(f"dangerous allow {rule}")
            elif agent.shell or rule not in allowed_bash:
                errors.append(f"allow {rule} is not a held wrapper form")
        elif rule not in tools | read_rules | edit_rules:
            errors.append(f"allow {rule} is not granted by a capability")
    for rule in permissions["ask"]:
        if rule not in allowed_ask:
            errors.append(f"ask {rule} is not a held wrapper form")
    deny = permissions["deny"]
    errors += [
        f"missing deny {rule}"
        for resource in table.resources.values()
        if resource.secret and resource.path is not None
        for rule in [_path_rule("Read", resource.path, resource.kind)]
        if not _covered(rule, deny)
    ]
    directories = {r.path for r in granted.values() if r.kind == "dir"}
    errors += [
        f"additional directory {path} is not a granted resource"
        for path in permissions["additionalDirectories"]
        if path not in directories
    ]
    # A deny carve-out is not enough for a named grant: it must not hold a secret at all.
    secrets = [
        r.path for r in table.resources.values() if r.secret and r.path is not None
    ]
    errors += [
        f"granted {name} would expose secret {secret}"
        for name, resource in sorted(granted.items())
        for secret in secrets
        if is_within(secret, resource.path)
    ]
    return errors


def _sandbox_errors(
    policy: Mapping[str, Any], registry: Registry, agent: _Agent, channels: list[Entry]
) -> list[str]:
    sandbox = policy["launch_settings"]["sandbox"]
    profile = registry.sandboxes[policy["sandbox_profile"]]["ceiling"]
    capabilities, table = registry.capabilities, registry.resources
    errors = [
        f"sandbox: {key} must be {json.dumps(value)} (fail-closed)"
        for key, value in SANDBOX_FLAGS.items()
        if json.dumps(sandbox[key], sort_keys=True) != json.dumps(value, sort_keys=True)
    ]
    network = sandbox["network"]
    expected = {**FAIL_CLOSED_SANDBOX["network"], CHANNEL_SOCKETS: bool(channels)}
    errors += [
        f"sandbox network: {key} must be {json.dumps(value)}"
        for key, value in expected.items()
        if json.dumps(network[key]) != json.dumps(value)
    ]
    domains = capability_union(capabilities, agent.held, "network_domains")
    domains &= set(profile["network_domains"])
    errors += [
        f"sandbox network: {domain} is outside the held capabilities and profile"
        for domain in network["allowedDomains"]
        if domain not in domains
    ]
    if sandbox["excludedCommands"] != []:
        errors.append("sandbox: excludedCommands must be empty (KD-194: channel commands)")
    filesystem = sandbox["filesystem"]
    tool_outputs = _tool_outputs(agent)
    readable = _sandbox_own_files(registry, agent) | _channel_reads(agent, channels) | tool_outputs | {
        table.resources[name].path
        for name, access in _held_resources(registry, agent)
        if access == "read-public"
    }
    errors += [
        f"sandbox: allowRead {path} is not the agent's own or public resource"
        for path in filesystem["allowRead"]
        if path not in readable or not _is_plain_path(path)
    ]
    # In any home the sandbox opens only the agent's own SSH identity and saved tool outputs:
    # never its Claude login, bot channels or memory keys, never another user's files.
    own_keys = f"{agent.home}/{OWN_FILE_DIR}"
    errors += [
        f"sandbox: allowRead {path} is outside the agent's own {OWN_FILE_DIR}/ and {TOOL_OUTPUTS}"
        for path in filesystem["allowRead"]
        if is_within(path, HOME_ROOT)
        and not (_is_plain_path(path) and (is_within(path, own_keys) or path in tool_outputs))
    ]
    secrets = [
        r.path for r in table.resources.values() if r.secret and r.path is not None
    ]
    errors += [
        f"sandbox: allowRead {path} lies inside secret {secret}"
        for path in filesystem["allowRead"]
        for secret in secrets
        if is_within(path, secret)
    ]
    secrets.append(f"{agent.home}/.ssh")
    errors += [
        f"sandbox: secret {path} is not denied"
        for path in secrets
        if not any(is_within(path, d) for d in filesystem["denyRead"])
    ]
    if not any(is_within(agent.code, d) for d in filesystem["denyWrite"]):
        errors.append(f"sandbox: code root {agent.code} is writable")
    return errors


def _grant_errors(
    policy: Mapping[str, Any], registry: Registry, agent: _Agent, channels: list[Entry]
) -> list[str]:
    table = registry.resources
    errors = []
    if policy["channels"] != channels:
        errors.append("channels must be exactly the held channel commands and executors")
    granted = {
        (name, "write" if access == "write-grant" else "read")
        for name, access in _held_resources(registry, agent)
        if access in GRANTS
    }
    for item in policy["acl"]:
        name, access = item["resource"], item["access"]
        resource = table.resources.get(name)
        if (
            item["user"] != agent.user
            or (name, access) not in granted
            or resource is None
            or item["path"] != resource.path
        ):
            errors.append(f"acl: {access} on {name} is not a held grant")
    if policy["settings"]["hooks"]["PreToolUse"] != _hooks(agent, channels):
        errors.append("hooks must be exactly the held guard and the channel hooks")
    steps = policy["install_steps"]
    if steps != _install_steps(registry, agent.held):
        errors.append("install steps must follow the held capability lifecycles")
    return errors


def _check_identity(name: Any, user: Any, model: Any) -> None:
    try:
        check_identity(name, user, model)
    except ManifestError as exc:
        raise PolicyError(f"agent identity: {exc}") from exc


def _manifest_fields(manifest: AgentManifest) -> dict[str, Any]:
    """The policy's identity and selection fields exactly as compile_policy writes them.

    ``effort`` is written only when the manifest sets it: the policy of an agent
    without it stays byte for byte the one installed before the field existed."""
    fields = {
        "agent": manifest.name,
        "user": manifest.user,
        "model": manifest.model,
        "profile": manifest.profile,
        "capabilities": sorted(manifest.capabilities),
        "sandbox_profile": manifest.sandbox_profile,
    }
    if manifest.effort is not None:
        fields[EFFORT_FIELD] = manifest.effort
    if manifest.thread_models:  # the same rule — absent from every other policy
        fields[THREAD_MODELS_FIELD] = dict(manifest.thread_models)
    return fields


def _check_host(agents: Mapping[str, AgentManifest]) -> None:
    # Every host agent's user names a home that the compiled rules deny or open.
    for key, item in agents.items():
        _check_identity(item.name, item.user, item.model)
        if key != item.name:
            raise PolicyError(f"host agent {key} is listed under another name")


def _host_users(agents: Mapping[str, AgentManifest]) -> list[str]:
    return [*(item.user for item in agents.values()), *SERVICE_USERS, DEFAULT_USER]


def _restriction_errors(
    policy: Mapping[str, Any],
    registry: Registry,
    agent: _Agent,
    agents: Mapping[str, AgentManifest],
) -> list[str]:
    """Every restriction that the platform, the held capabilities and the resource table
    require must be present (a broader rule counts): a stripped policy fails here."""
    required = _Rules()
    _platform_rules(required, agent, registry, _host_users(agents), _host_folders(registry, agents))
    _wrapper_permissions(required, agent, registry)
    _resource_grants(required, agent, registry)
    deny = policy["settings"]["permissions"]["deny"]
    filesystem = policy["launch_settings"]["sandbox"]["filesystem"]
    errors = [
        f"missing deny {rule}"
        for rule in sorted(required.deny)
        if not _covered(rule, deny)
    ]
    for field in ("denyRead", "denyWrite"):
        errors += [
            f"sandbox: missing {field} {path}"
            for path in sorted(getattr(required, _SANDBOX_RULES[field]))
            if not _within_any(path, filesystem[field])
        ]
    # Every other agent's own files (its SSH keys) stay closed, whatever else changes.
    for other in sorted(agents.values(), key=lambda item: item.name):
        if other.user == agent.user:
            continue
        home = f"{HOME_ROOT}/{other.user}"
        for resource in registry.resources.resources.values():
            for path in resource.paths(home, other.name) if resource.home else ():
                rule = _path_rule("Read", path, "file")
                if not _covered(rule, deny) or not _within_any(
                    path, filesystem["denyRead"]
                ):
                    errors.append(f"secret {path} of {other.name} is not denied")
    return errors


_SANDBOX_RULES = {"denyRead": "deny_read", "denyWrite": "deny_write"}


def check_policy(
    policy: Mapping[str, Any],
    registry: Registry,
    *,
    floor: Mapping[str, Any],
    agents: Mapping[str, AgentManifest],
) -> None:
    """Refuse a policy that is malformed, wider than its capabilities or contradictory.

    Independent of how the policy was built: the registry is validated first (an
    installer's check and postflight get no unchecked registry data), then dangerous
    or unheld allows, allow/ask/deny conflicts (also against the managed ``floor``), a
    missing platform, capability or resource restriction (``agents`` are the host's
    agents, whose homes stay closed), bypass or unsandboxed settings, network beyond the
    held capabilities and the sandbox profile, excluded commands, readable secrets,
    channels other than the held ones, foreign ACL entries, and a missing guard
    all fail.
    """
    try:
        validate_registry(registry)
    except RegistryError as exc:
        raise PolicyError(exc.errors) from exc
    _check_policy(policy, registry, floor=floor, agents=agents)


def _check_policy(
    policy: Mapping[str, Any],
    registry: Registry,
    *,
    floor: Mapping[str, Any],
    agents: Mapping[str, AgentManifest],
) -> None:
    """check_policy after the registry check; _compile_policy ends with it."""
    errors = _structure(policy)
    if errors:
        raise PolicyError(errors)
    _check_identity(policy["agent"], policy["user"], policy["model"])
    _check_host(agents)
    host = agents.get(policy["agent"])
    if host is None:
        raise PolicyError("the host agents must include this agent")
    # The manifest is the source of truth: a policy whose selection is merely valid for
    # the registry but not the one compile_policy derives from the manifest (an extra
    # capability, a Telegram channel the manifest lacks, another model or effort) is refused.
    expected = _manifest_fields(host)
    mismatched = [
        field
        for field, value in expected.items()
        if field not in policy
        or type(policy[field]) is not type(value)
        or policy[field] != value
    ]
    mismatched += [f for f in OPTIONAL_FIELDS if f in policy and f not in expected]
    if mismatched:
        raise PolicyError(
            "policy differs from the host manifest in: " + ", ".join(mismatched)
        )
    try:
        validate_selection(
            registry, host.profile, host.capabilities, host.sandbox_profile
        )
        agent = _agent(
            registry,
            host.name,
            host.user,
            host.capabilities,
        )
    except RegistryError as exc:
        raise PolicyError(exc.errors) from exc
    channels = _channels(agent, registry)
    permissions = policy["settings"]["permissions"]
    floor_permissions = floor.get("permissions", {})
    errors = _permission_errors(policy, registry, agent)
    errors += _conflicts(
        permissions["allow"], permissions["ask"], permissions["deny"], floor_permissions
    )
    errors += _restriction_errors(policy, registry, agent, agents)
    errors += _sandbox_errors(policy, registry, agent, channels)
    errors += _grant_errors(policy, registry, agent, channels)
    if errors:
        raise PolicyError(errors)


# --- comparison with a baseline -------------------------------------------------------


@dataclass(frozen=True, order=True)
class Difference:
    """One semantic difference between two effective views of the same agent."""

    change: str  # "added" or "removed"
    item: str  # "<dimension>:<value>"
    widens: bool


def _hook_items(hooks: Mapping[str, Any]) -> set[str]:
    return {
        f"{event}|{group['matcher']}|{hook['command']}|{hook.get('timeout')}"
        for event, groups in hooks.items()
        for group in groups
        for hook in group["hooks"]
    }


def _flag_items(sandbox: Mapping[str, Any]) -> set[str]:
    network = sandbox.get("network", {})
    return {f"{key}={json.dumps(sandbox.get(key), sort_keys=True)}" for key in SANDBOX_FLAGS} | {
        f"network.{key}={json.dumps(network.get(key))}"
        for key in FAIL_CLOSED_SANDBOX["network"]
    }


def effective_view(
    *,
    floor: Mapping[str, Any],
    settings: Mapping[str, Any],
    launch_settings: Mapping[str, Any] | None,
    bash_approval: str,
    acl: Iterable[str],
) -> dict[str, Any]:
    """What Claude Code enforces for one agent: managed floor plus its settings."""
    floor_permissions = floor.get("permissions", {})
    permissions = settings.get("permissions", {})
    modes = {**permissions, **floor_permissions}  # managed settings take precedence
    view: dict[str, Any] = {
        "mode": {
            f"{key}={json.dumps(modes[key])}"
            for key in (*PERMISSION_MODES, "blockReadsOutsideWorkingDirectories")
            if key in modes
        },
        "bash_approval": {bash_approval},
        "hook": _hook_items(floor.get("hooks", {}))
        | _hook_items(settings.get("hooks", {})),
    }
    for field in ("allow", "ask", "deny", "additionalDirectories"):
        view[field] = set(floor_permissions.get(field, [])) | set(
            permissions.get(field, [])
        )
    acl = set(acl)
    view["acl"] = {
        item
        for item in acl
        if not item.startswith("read:") or f"write:{item[5:]}" not in acl
    }
    sandbox = None if launch_settings is None else launch_settings.get("sandbox")
    if sandbox is not None:
        filesystem = sandbox.get("filesystem", {})
        sandbox = {
            "flag": _flag_items(sandbox),
            "excludedCommands": set(sandbox.get("excludedCommands", [])),
            "allowedDomains": set(sandbox["network"].get("allowedDomains", [])),
            **{
                key: set(filesystem.get(key, []))
                for key in ("allowRead", "allowWrite", "denyRead", "denyWrite")
            },
        }
    view["sandbox"] = sandbox
    return view


# defaultMode -> the modes that refuse whatever it would allow or ask: dontAsk refuses what default asks
# the owner. Any other change of a mode widens.
_STRICTER_MODES = {"default": ("dontAsk",)}


def _default_mode(modes: set[str]) -> str:
    """The defaultMode of a view's modes; Claude Code's own default when none is set."""
    for item in modes:
        key, _, value = item.partition("=")
        if key == "defaultMode":
            return json.loads(value)
    return "default"


def compare_views(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[Difference]:
    """Semantic differences from ``old`` to ``new``; ``widens`` marks a wider right.

    Rules count as present when another rule of the same list covers them, so a
    narrower spelling of the same right is not a difference.
    """
    found: list[Difference] = []
    stricter = _default_mode(new["mode"]) in _STRICTER_MODES.get(
        _default_mode(old["mode"]), ()
    )
    for field in ("mode", "bash_approval"):
        found += [
            Difference(d.change, d.item, False)
            if stricter and d.item.startswith("mode:defaultMode=")
            else d
            for d in _set_changes(
                field, old[field], new[field], None, widens=(True, True)
            )
        ]
    found += _set_changes("hook", old["hook"], new["hook"], None, widens=(False, True))
    found += _set_changes("allow", old["allow"], new["allow"], _covered)
    found += _set_changes("deny", old["deny"], new["deny"], _covered, (False, True))
    found += _set_changes("acl", old["acl"], new["acl"], _acl_covered)
    found += _set_changes(
        "additionalDirectories",
        old["additionalDirectories"],
        new["additionalDirectories"],
        _within_any,
    )
    # An ask widens only where a deny becomes an ask, or an ask becomes an allow.
    found += [
        Difference("added", f"ask:{rule}", _covered(rule, old["deny"]))
        for rule in sorted(new["ask"])
        if not _covered(rule, old["ask"])
    ]
    found += [
        Difference("removed", f"ask:{rule}", not _covered(rule, new["deny"]))
        for rule in sorted(old["ask"])
        if not _covered(rule, new["ask"])
    ]
    old_box, new_box = old["sandbox"], new["sandbox"]
    if old_box is None and new_box is not None:
        # A new fail-closed sandbox only narrows an unsandboxed agent; a weaker flag
        # (say, auto-allowed Bash) can still widen what it may do without a prompt.
        found.append(Difference("added", "sandbox:enabled", False))
        found += [
            Difference("added", f"sandbox.flag:{item}", True)
            for item in sorted(new_box["flag"] - _flag_items(FAIL_CLOSED_SANDBOX))
        ]
    elif old_box is not None and new_box is None:
        found.append(Difference("removed", "sandbox:enabled", True))
    elif old_box is not None:
        # A flag widens when it leaves its fail-closed value and narrows when it returns
        # to it (a worker closes the developer's channel sockets).
        closed = _flag_items(FAIL_CLOSED_SANDBOX)
        found += [
            Difference("added", f"sandbox.flag:{item}", item not in closed)
            for item in sorted(new_box["flag"] - old_box["flag"])
        ] + [
            Difference("removed", f"sandbox.flag:{item}", item in closed)
            for item in sorted(old_box["flag"] - new_box["flag"])
        ]
        for field, within, widens in (
            ("excludedCommands", None, (True, False)),
            ("allowedDomains", None, (True, False)),
            ("allowRead", _within_any, (True, False)),
            ("allowWrite", _within_any, (True, False)),
            ("denyRead", _within_any, (False, True)),
            ("denyWrite", _within_any, (False, True)),
        ):
            found += _set_changes(
                f"sandbox.{field}", old_box[field], new_box[field], within, widens
            )
    return sorted(found)


def _within_any(path: str, roots: Iterable[str]) -> bool:
    return any(is_within(path, root) for root in roots)


def _is_plain_path(path: str) -> bool:
    # A sandbox path is taken literally: no relative parts, empty segments or globs.
    segments = path.split("/")
    return (
        path.startswith("/")
        and all(segment not in ("", ".", "..") for segment in segments[1:])
        and not set("*?[]{}") & set(path)
    )


def _set_changes(
    field: str,
    old: Collection[str],
    new: Collection[str],
    present: Any,
    widens: tuple[bool, bool] = (True, False),
) -> list[Difference]:
    """Items of one dimension missing on the other side; ``widens`` = (added, removed)."""
    test = present or (lambda item, items: item in items)
    return [
        Difference("added", f"{field}:{item}", widens[0])
        for item in sorted(new)
        if not test(item, old)
    ] + [
        Difference("removed", f"{field}:{item}", widens[1])
        for item in sorted(old)
        if not test(item, new)
    ]
