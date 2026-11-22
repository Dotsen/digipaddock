"""How agent-launch starts an agent's session (BOSS spec 2026-10-06, sections 5.2-5.2b).

Every agent runs the generated runtime (``"runtime": "generated"`` in the runtime registry,
``kd.agents.runtime_of``) on the session host: agent-launch calls ``main`` as the
agent's own user, and ``main`` refuses an agent of any other runtime (the legacy
launch and the digipaddock panels are gone).

The generated launch is the strict-sandbox contract that dev runs today, made
common: the session starts in the agent's own writable ``work/``, the root-owned agent
folder joins with ``--add-dir``, the compiled settings and launch settings reach Claude
Code as one ``--settings`` and the compiled model, if any, becomes ``--model``. The
compiled policy is ``<agent folder>/.claude/policy.json``, byte for byte as
``kd.policy.render_policy`` writes it. A missing, untrusted, non-canonical or malformed
policy, a policy of an older schema, a sandbox weaker than fail-closed, an unknown model
or an unsafe ``work/`` (an agent-owned ``.claude/`` or ``.mcp.json`` in it included:
Claude Code loads them from the cwd) refuses the launch: nothing falls back to other
settings or to an unsandboxed session. The full policy check
(``check_policy`` against the registry) belongs to the installer; the launcher checks
what it hands over.

``main`` prints a plan of ASCII lines: ``generated``, the model (an empty line for the
platform default), the effort (an empty line for the platform default) and the
merged settings as compact JSON. The model is the session's thread's own when the
policy names one (``thread_models``: the "Monitor" session), else the agent's.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from kd import agents

GENERATED = agents.GENERATED_RUNTIME
POLICY_FILE = ".claude/policy.json"  # relative to the agent folder
WORK = "work"  # manifest schema v1 fixes runtime.working_directory to work
ROOT_UID = 0
# The bound kd.registry puts on JSON files; it also keeps the merged --settings below
# Linux's 128 KiB limit for a single argument.
MAX_POLICY_BYTES = 64 * 1024
# Project settings, hooks and MCP servers Claude Code loads from its cwd (besides the
# CLAUDE.md placeholders), and the kind of path root may put there instead.
WORK_PROJECT_CONFIG = {".claude": "dir", ".mcp.json": "file"}
# What Claude Code itself creates in its cwd's .claude while it runs (the sandbox write
# log): the agent's own work/.claude holding only this directory is not project config,
# nor are the empty read-only mount points the sandbox keeps there while a command runs.
WORK_RUNTIME_DIRS = frozenset({".cc-writes"})
# Group or other write bits; a POSIX ACL that grants write shows up in the group bits.
SHARED_WRITE = stat.S_IWGRP | stat.S_IWOTH


class LaunchError(ValueError):
    """A runtime that must not start; the message names no secret and no file content."""


@dataclass(frozen=True)
class Node:
    """One path as lstat sees it, without following a symlink."""

    kind: str  # "dir", "file", "link" or "other"
    uid: int
    mode: int  # permission bits only


@dataclass(frozen=True)
class LaunchPlan:
    """A checked generated launch: ``model`` for --model, ``settings`` for --settings,
    ``effort`` for --effort."""

    model: str | None
    settings: str
    effort: str | None = None


class Filesystem(Protocol):
    def node(self, path: str) -> Node | None:
        """The path itself (lstat), or None when it does not exist."""

    def read(self, path: str, limit: int) -> tuple[Node, bytes]:
        """Open without following a symlink; a regular file gives at most limit + 1 bytes."""

    def names(self, path: str) -> list[str]:
        """Entry names of a directory the caller has already seen as a real directory."""


def node_of(info: os.stat_result) -> Node:
    if stat.S_ISLNK(info.st_mode):
        kind = "link"
    elif stat.S_ISDIR(info.st_mode):
        kind = "dir"
    elif stat.S_ISREG(info.st_mode):
        kind = "file"
    else:
        kind = "other"
    return Node(kind, info.st_uid, stat.S_IMODE(info.st_mode))


class HostFilesystem:
    """The real filesystem; the file is checked on the descriptor it is read from."""

    def node(self, path: str) -> Node | None:
        try:
            return node_of(os.lstat(path))
        except FileNotFoundError:
            return None

    def read(self, path: str, limit: int) -> tuple[Node, bytes]:
        # O_NONBLOCK: a FIFO in place of the file must not hang the launch.
        flags = os.O_RDONLY
        for name in ("O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC"):
            flags |= getattr(os, name, 0)
        fd = os.open(path, flags)
        try:
            node = node_of(os.fstat(fd))
            if node.kind != "file":
                return node, b""
            chunks, size = [], 0
            while size <= limit:
                chunk = os.read(fd, limit + 1 - size)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            return node, b"".join(chunks)
        finally:
            os.close(fd)

    def names(self, path: str) -> list[str]:
        return sorted(os.listdir(path))


def _plain_absolute(path: str) -> bool:
    pure = PurePosixPath(path)
    return pure.is_absolute() and str(pure) == path and ".." not in pure.parts


def _root_owned(node: Node | None, kind: str) -> bool:
    return (
        node is not None
        and node.kind == kind
        and node.uid == ROOT_UID
        and not node.mode & SHARED_WRITE
    )


def _require_root_dirs(fs: Filesystem, path: str) -> None:
    """Every directory from / down to ``path``: root's, and only root may write it."""
    pure = PurePosixPath(path)
    for directory in [*reversed(pure.parents), pure]:
        if not _root_owned(fs.node(str(directory)), "dir"):
            raise LaunchError(f"{directory} must be a root-owned directory")


def _read_policy(fs: Filesystem, path: str) -> Any:
    from kd.policy import render_policy

    try:
        node, raw = fs.read(path, MAX_POLICY_BYTES)
    except FileNotFoundError:
        raise LaunchError(f"compiled policy {path} is missing") from None
    except OSError:
        raise LaunchError(f"compiled policy {path} cannot be opened safely") from None
    if not _root_owned(node, "file"):
        raise LaunchError(f"compiled policy {path} must be a root-owned file")
    if len(raw) > MAX_POLICY_BYTES:
        raise LaunchError(f"compiled policy {path} is too large")
    try:
        policy = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except ValueError:
        raise LaunchError(f"compiled policy {path} is not valid JSON") from None
    # Only the compiler's own rendering: no hand edit, duplicate key or reordering.
    if render_policy(policy) != raw:
        raise LaunchError(f"compiled policy {path} is not the compiler's rendering")
    return policy


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite number {value}")


def _same(value: Any, expected: Any) -> bool:
    # json.dumps keeps true apart from 1 and [] apart from false, unlike ==.
    return json.dumps(value, sort_keys=True) == json.dumps(expected, sort_keys=True)


def _exact(value: Any, fields: Collection, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != set(fields):
        raise LaunchError(f"compiled policy: {label} fields must be {sorted(fields)}")
    return value


def _strings(value: Any, label: str) -> None:
    if not isinstance(value, list) or not all(isinstance(i, str) for i in value):
        raise LaunchError(f"compiled policy: {label} must be a list of text")


def _check_hooks(hooks: Any) -> None:
    groups = _exact(hooks, {"PreToolUse"}, "hooks")["PreToolUse"]
    if not isinstance(groups, list):
        raise LaunchError("compiled policy: PreToolUse must be a list")
    for group in groups:
        group = _exact(group, {"matcher", "hooks"}, "hook group")
        if not isinstance(group["matcher"], str) or not isinstance(
            group["hooks"], list
        ):
            raise LaunchError("compiled policy: malformed hook group")
        for hook in group["hooks"]:
            hook = _exact(hook, {"type", "command", "timeout"}, "hook")
            timeout = hook["timeout"]
            if (
                hook["type"] != "command"
                or not isinstance(hook["command"], str)
                or not hook["command"]
                or type(timeout) is not int
                or timeout <= 0
            ):
                raise LaunchError("compiled policy: malformed hook")


def _check_sandbox(launch: Any, channels: bool) -> None:
    from kd.policy import (
        CHANNEL_SOCKETS,
        FILESYSTEM_FIELDS,
        NETWORK_FIELDS,
        RUN_DIRS,
        SANDBOX_FIELDS,
        SANDBOX_FLAGS,
    )
    from kd.registry import FAIL_CLOSED_SANDBOX

    sandbox = _exact(launch, {"sandbox"}, "launch settings")["sandbox"]
    sandbox = _exact(sandbox, SANDBOX_FIELDS, "sandbox")
    network = _exact(sandbox["network"], NETWORK_FIELDS, "sandbox network")
    # Unix sockets are open exactly when the agent has channels.
    expected = {**FAIL_CLOSED_SANDBOX["network"], CHANNEL_SOCKETS: channels}
    weaker = [
        key for key, value in SANDBOX_FLAGS.items() if not _same(sandbox[key], value)
    ] + [
        f"network.{key}"
        for key, value in expected.items()
        if not _same(network[key], value)
    ]
    if weaker:
        raise LaunchError(f"compiled sandbox is not fail-closed: {', '.join(weaker)}")
    _strings(network["allowedDomains"], "allowedDomains")
    _strings(sandbox["excludedCommands"], "excludedCommands")
    if sandbox["excludedCommands"]:
        raise LaunchError(
            "compiled sandbox excludes commands: channels replace exclusions"
        )
    filesystem = _exact(sandbox["filesystem"], FILESYSTEM_FIELDS, "sandbox filesystem")
    for field, value in sorted(filesystem.items()):
        _strings(value, field)
    if channels and not set(RUN_DIRS) <= set(filesystem["denyRead"]):
        raise LaunchError("compiled sandbox opens channel sockets without hiding /run")


def check_compiled(
    policy: Any,
    *,
    name: str,
    user: str,
    space: str = "chat",
    egress: tuple[str, Mapping[str, Any], str] | None = None,
    base_url: str = "",
) -> LaunchPlan:
    """The launch an already trusted compiled policy describes, or LaunchError. ``space`` is the agent thread
    the session is of: its own model when the policy names one for it (``thread_models``), else the agent's.
    ``egress`` — a card session's probe exit (its name, its entry of host/egress.json and its country, ``""`` for
    none): the exit's names join ``allowedDomains`` and the session's ``env`` points Claude Code's upstream proxy at
    the exit, everything else of the agent (and ``base_url``'s host, a card's provider model) past it."""
    from kd.manifest import (
        THREAD_SPACES,
        ManifestError,
        check_effort,
        check_identity,
        check_thread_models,
    )
    from kd.policy import (
        EFFORT_FIELD,
        PERMISSION_FIELDS,
        PERMISSION_MODES,
        SCHEMA_VERSION,
        THREAD_MODELS_FIELD,
        policy_fields,
    )

    if space != "chat" and space not in THREAD_SPACES:
        raise LaunchError(f"unknown thread space {space!r}")

    old = policy.get("schema_version") if isinstance(policy, Mapping) else None
    if type(old) is int and 0 < old < SCHEMA_VERSION:
        # An install older than this code: its fields differ, so say what to do before judging them.
        raise LaunchError(
            f"compiled policy of {name} has schema_version {old}, older than this"
            f" launcher ({SCHEMA_VERSION}): reinstall the agent (new_agent.sh --install {name})"
        )
    policy = _exact(policy, policy_fields(policy), "policy")
    version = policy["schema_version"]
    if type(version) is not int or version != SCHEMA_VERSION:
        raise LaunchError("compiled policy: unsupported schema_version")
    if policy["agent"] != name or policy["user"] != user:
        raise LaunchError(f"compiled policy is not the policy of {name} as {user}")
    try:
        # The manifest's own rule: a model is a known alias or Claude identifier,
        # never an option or free text.
        check_identity(policy["agent"], policy["user"], policy["model"])
        effort = policy.get(EFFORT_FIELD)
        if EFFORT_FIELD in policy:
            check_effort(effort)
        thread_models = (
            check_thread_models(policy[THREAD_MODELS_FIELD])
            if THREAD_MODELS_FIELD in policy
            else {}
        )
    except ManifestError as exc:
        raise LaunchError(f"compiled policy: {exc}") from None
    settings = _exact(policy["settings"], {"permissions", "hooks"}, "settings")
    permissions = _exact(settings["permissions"], PERMISSION_FIELDS, "permissions")
    for key, value in PERMISSION_MODES.items():
        if not _same(permissions[key], value):
            raise LaunchError(f"compiled policy: permissions {key} must be {value}")
    for field in ("additionalDirectories", "allow", "ask", "deny"):
        _strings(permissions[field], field)
    _check_hooks(settings["hooks"])
    if not isinstance(policy["channels"], list):
        raise LaunchError("compiled policy: channels must be a list")
    _check_sandbox(policy["launch_settings"], bool(policy["channels"]))
    merged = {**settings, **policy["launch_settings"]}
    if egress is not None:
        merged = _with_exit(merged, policy, name, *egress, base_url=base_url)
    return LaunchPlan(
        thread_models.get(space, policy["model"]),
        json.dumps(merged, sort_keys=True, separators=(",", ":")),
        effort,
    )


EGRESS_CAPABILITY = "net.egress"
WEB_TOOLS = ("WebFetch", "WebSearch")
ANY_HOST_RULE = "WebFetch(domain:*)"


def _with_exit(
    merged: dict,
    policy: Mapping[str, Any],
    name: str,
    exit_name: str,
    entry: Mapping[str, Any],
    country: str,
    *,
    base_url: str,
) -> dict:
    from kd import egress

    if EGRESS_CAPABILITY not in policy["capabilities"]:
        raise LaunchError(
            f"{name} does not hold {EGRESS_CAPABILITY}: no probe exit for it"
        )
    if name not in entry["agents"]:
        raise LaunchError(f"probe exit {exit_name} does not list {name}")
    out = json.loads(json.dumps(merged))
    network = out["sandbox"]["network"]
    compiled = list(network["allowedDomains"])
    if egress.any_host(entry):
        # the deployment's web access. Claude Code refuses a bare "*" among allowedDomains; its sandbox
        # reads the WebFetch rule of every domain as every host (2.1.186+, the stage probe of 15.11.2026: with the
        # rule a sandboxed request reaches the exit, without it the sandbox answers 403). The web tools every
        # compiled policy denies are this session's: what they fetch leaves through the exit too.
        permissions = out["permissions"]
        permissions["deny"] = [r for r in permissions["deny"] if r not in WEB_TOOLS]
        permissions["allow"] += [
            r for r in (*WEB_TOOLS, ANY_HOST_RULE) if r not in permissions["allow"]
        ]
    else:
        network["allowedDomains"] = sorted({*compiled, *entry["domains"]})
    proxy = f"http://127.0.0.1:{entry['port']}"
    out["env"] = {
        "HTTPS_PROXY": proxy,
        "HTTP_PROXY": proxy,
        "NO_PROXY": egress.no_proxy(compiled, base_url),
        "KD_EGRESS": exit_name,
        "KD_EGRESS_HOSTS": ",".join(entry["domains"]),
    }
    if country:  # the country of the exit's tunnel port, from the host's table
        out["env"]["KD_EGRESS_COUNTRY"] = country
    return out


def _read_exits(fs: Filesystem, path: str) -> Mapping[str, Any] | None:
    """The checked root-owned host/egress.json of the release tree; None when the host has none."""
    from kd import egress

    if fs.node(path) is None:
        return None
    _require_root_dirs(fs, str(PurePosixPath(path).parent))
    try:
        node, raw = fs.read(path, MAX_POLICY_BYTES)
    except OSError:
        raise LaunchError(f"{path} cannot be opened safely") from None
    if not _root_owned(node, "file") or len(raw) > MAX_POLICY_BYTES:
        raise LaunchError(f"{path} must be a root-owned file")
    try:
        return egress.check(
            json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
        )
    except ValueError as exc:  # EgressError and broken JSON alike
        raise LaunchError(str(exc)) from None


def _read_exit(
    fs: Filesystem, path: str, exit_name: str, name: str, warn: Callable[[str], None]
) -> tuple[str, Mapping[str, Any], str] | None:
    """The entry (and country) of the exit the session host named for a card's session. the name was the card's at the
    delivery; a session that waited for a slot may start after host/egress.json dropped the exit or stopped listing
    the agent — then None (the session starts as one no card named an exit for) and one line through ``warn``. A
    malformed name or file still refuses: that is a broken release, not a changed exit."""
    from kd import egress

    if not egress.EXIT.fullmatch(exit_name or ""):
        raise LaunchError("bad probe exit name")
    config = _read_exits(fs, path)
    entry = (config.get("exits") or {}).get(exit_name) if config else None
    if entry is None or name not in entry["agents"]:
        warn(f"probe exit {exit_name} is no longer {name}'s on this host: the session starts without it")
        return None
    return exit_name, entry, egress.country_of(config, exit_name)


def _standing_exit(
    fs: Filesystem, path: str, name: str, policy: Mapping[str, Any]
) -> tuple[str, Mapping[str, Any], str] | None:
    """the exit every session of the agent holds, a card's or not — the host's ``standing`` exit when it
    lists the agent and the agent's compiled policy holds net.egress; None otherwise (a host without exits, an
    agent the exit does not list, a policy compiled before the agent got the capability)."""
    from kd import egress

    capabilities = policy.get("capabilities") if isinstance(policy, Mapping) else None
    if not isinstance(capabilities, list) or EGRESS_CAPABILITY not in capabilities:
        return None
    config = _read_exits(fs, path)
    exit_name = egress.standing_for(config, name) if config else ""
    if not exit_name:
        return None
    return exit_name, egress.exit_of(config, exit_name), egress.country_of(config, exit_name)


def _check_work(fs: Filesystem, work: str, uid: int) -> None:
    """work/ is the agent's own writable cwd; the instruction files Claude Code loads
    from a cwd stay root's placeholders, so the agent cannot instruct its next session."""
    from kd.policy import WORK_PLACEHOLDERS

    node = fs.node(work)
    if (
        node is None
        or node.kind != "dir"
        or node.uid != uid
        or node.mode & SHARED_WRITE
    ):
        raise LaunchError(f"{work} must be a directory of the agent's own user")
    for item in WORK_PLACEHOLDERS:
        path = f"{work}/{item}"
        if not _root_owned(fs.node(path), "file"):
            raise LaunchError(f"{path} must be a root-owned placeholder file")
    # Project settings, hooks and MCP servers that Claude Code loads from the cwd, outside
    # the sandbox: absent, or root's.
    for item, kind in WORK_PROJECT_CONFIG.items():
        path = f"{work}/{item}"
        node = fs.node(path)
        if node is None or _root_owned(node, kind):
            continue
        runtime = _runtime_only if kind == "dir" else _mount_point
        if not runtime(fs, path, uid):
            raise LaunchError(f"{path} must be absent or root's")


def _mount_point(fs: Filesystem, path: str, uid: int) -> bool:
    """An empty read-only file of the agent's: Claude Code's sandbox keeps one in place of
    each denied path that does not exist while a command runs; it loads nothing."""

    def read_only(node: Node) -> bool:
        return node.kind == "file" and node.uid == uid and not node.mode & 0o222

    node = fs.node(path)
    if node is None or not read_only(node):
        return False
    node, data = fs.read(path, 0)
    return read_only(node) and not data


def _runtime_only(fs: Filesystem, path: str, uid: int) -> bool:
    """The agent's own directory holding nothing but Claude Code's runtime directories
    and its sandbox's mount points."""

    def own_dir(node: Node | None) -> bool:
        return (
            node is not None
            and node.kind == "dir"
            and node.uid == uid
            and not node.mode & SHARED_WRITE
        )

    if not own_dir(fs.node(path)):
        return False
    return all(
        own_dir(fs.node(f"{path}/{name}"))
        if name in WORK_RUNTIME_DIRS
        else _mount_point(fs, f"{path}/{name}", uid)
        for name in fs.names(path)
    )


def plan_generated(
    name: str,
    folder: str,
    *,
    uid: int,
    user: str,
    agents_root: str,
    fs: Filesystem,
    registry: str | None = None,
    space: str = "chat",
    egress: str = "",
    egress_path: str | None = None,
    base_url: str = "",
    standing: bool = True,
    warn: Callable[[str], None] | None = None,
) -> LaunchPlan:
    """Check every input of a generated launch; raises LaunchError on the first doubt. ``egress`` — the card
    session's probe exit the session host named (``agent-launch --egress``); without one the session holds the
    host's standing exit, when the agent has one — unless ``standing`` is off (a measuring probe is no
    session of the agent's work and holds no exit). An exit the host's file no longer gives the agent
    is not held: ``warn`` (stderr by default) gets one line naming it and the session goes on as one without."""
    from kd import egress as egress_config

    if not _plain_absolute(agents_root) or folder != f"{agents_root}/{name}":
        raise LaunchError(f"agent folder {folder} is not {name} under the agents root")
    _require_root_dirs(fs, f"{folder}/.claude")
    policy = _read_policy(fs, f"{folder}/{POLICY_FILE}")
    if agents.user_of(name, registry or agents.REGISTRY) != user:
        raise LaunchError(f"{name} must run as the user agents.json names")
    exits_path = egress_path or egress_config.PATH
    exit_entry = None
    if egress:
        exit_entry = _read_exit(fs, exits_path, egress, name, warn or _stderr)
    if exit_entry is None and standing:
        exit_entry = _standing_exit(fs, exits_path, name, policy)
    plan = check_compiled(
        policy, name=name, user=user, space=space, egress=exit_entry, base_url=base_url
    )
    _check_work(fs, f"{folder}/{WORK}", uid)
    return plan


def _stderr(line: str) -> None:
    print(f"agent-launch: {line}", file=sys.stderr)


def render_plan(plan: LaunchPlan) -> str:
    """The plan for agent-launch: one value per line, no shell syntax to evaluate."""
    lines = [GENERATED, plan.model or "", plan.effort or "", plan.settings]
    if not plan.settings:
        raise LaunchError("launch plan has no settings")
    if any(not line.isascii() for line in lines):
        raise LaunchError("launch plan must be ASCII text")
    if any(ch in line for line in lines for ch in "\n\r\0"):
        raise LaunchError("launch plan values must be single lines")
    return "\n".join(lines) + "\n"


def _host_identity() -> tuple[int, str]:
    import pwd

    uid = os.geteuid()
    return uid, pwd.getpwuid(uid).pw_name


def configured_agents_root(code_root: Path | None = None) -> str:
    """The agents root from the deployment's resource table (data, not code)."""
    from kd.registry import RESOURCES, RegistryError, read_json_object

    root = code_root or Path(__file__).resolve().parents[2]
    try:
        table = read_json_object(root / RESOURCES)
    except RegistryError as exc:
        raise LaunchError(f"agent-resources.json: {exc}") from None
    layout = table.get("layout")
    value = layout.get("agents") if isinstance(layout, dict) else None
    if not isinstance(value, str) or not _plain_absolute(value):
        raise LaunchError(
            "agent-resources.json: layout.agents must be an absolute path"
        )
    return value


def main(
    argv: Sequence[str],
    *,
    registry: str | None = None,
    fs: Filesystem | None = None,
    identity: Callable[[], tuple[int, str]] | None = None,
    agents_root: str | None = None,
) -> int:
    """``agent-launch`` entry: <name> <agent folder> [<space> [<exit>]] — the agent thread the session is of
    (``chat``, the default, or ``monitor``), for its own model; the probe exit of a card's session."""
    try:
        if len(argv) not in (2, 3, 4):
            raise LaunchError(
                "usage: kd.launch <name> <agent folder> [<space> [<exit>]]"
            )
        name, folder = argv[:2]
        space = argv[2] if len(argv) >= 3 else "chat"
        exit_name = argv[3] if len(argv) == 4 else ""
        if exit_name and space != "chat":
            raise LaunchError("a probe exit is a card session's")
        from kd.manifest import THREAD_SPACES

        if (
            space != "chat" and space not in THREAD_SPACES
        ):  # before anything of the runtime is looked at
            raise LaunchError(f"unknown thread space {space!r}")
        # Second line behind agent-launch, which launches only a generated agent.
        if agents.runtime_of(name, registry or agents.REGISTRY) != GENERATED:
            raise LaunchError(
                f"{name} is not a generated agent: only the generated runtime is launched"
            )
        uid, user = (identity or _host_identity)()
        plan = plan_generated(
            name,
            folder,
            uid=uid,
            user=user,
            agents_root=configured_agents_root()
            if agents_root is None
            else agents_root,
            fs=fs or HostFilesystem(),
            registry=registry,
            space=space,
            egress=exit_name,
            # read only by a launch with an exit, the card's or the standing one
            base_url=os.environ.get("ANTHROPIC_BASE_URL", ""),
        )
        sys.stdout.write(render_plan(plan))
        return 0
    except (ValueError, OSError) as exc:
        # kd.agents reports a broken registry as ValueError; nothing here prints data.
        print(f"agent-launch: {exc}", file=sys.stderr)
        return 1
