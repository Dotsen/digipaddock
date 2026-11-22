"""Read-only check and plan for installing an agent from its manifest (BOSS spec 2026-10-06, 5.4).

``build_plan`` checks the repository side (every manifest, the registry, the host data, the
compiled effective policy, the board grant and the rendered instruction) and the system side
(accounts and their groups, the installed agents, dependencies and conflicting files). It
returns every system action an install would take, in the order it runs, with the exact
files, ACL entries and channel units; ``render_plan`` prints it.

The plan shows the rights that are really granted, where they are granted. The board grant
is decided by ``kd.board_grant``; A08 prepares the user without it and applies every ACL
and channel grant only after the policy files have been published.

Nothing here writes. The system is read only through ``HostView``, whose root a test
replaces with a fixture directory. It never reads a file inside a home (it only inspects the
base user's Claude Code link, so a check on a host runs as root), and the planner reads no
secret: tokens, keys and logins appear in a plan as paths at most. Deployment specifics
(paths, unit names) come from the resource table and ``common/agent-host.json``.
``kd.install_apply`` executes the checked plan; this module remains read-only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import re
import stat
import sys
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kd import cap_grants, cap_units, naming, specializations
from kd.agent_text import (
    AgentTextError,
    read_instruction_file,
    render_instruction_files,
    render_playbook,
)
from kd.agents import DEFAULT_USER, GENERATED_RUNTIME, NAME_RE, REGISTRY, USER_RE
from kd.board_grant import (
    BACKLOG_DIR,
    BOARD_RESOURCE,
    BoardGrant,
    BoardGrantError,
    board_grant,
    grant_from_acl,
)
from kd.manifest import (
    SERVICE_USERS,
    AgentManifest,
    ManifestError,
    validate_all,
)
from kd.policy import (
    HOME_ROOT,
    WORK_PLACEHOLDERS,
    PolicyError,
    compile_policy,
    render_policy,
)
from kd.registry import (
    EXECUTOR_MODULES,
    Registry,
    RegistryError,
    board_routes,
    is_within,
    load_registry,
    read_json_object,
    validate_registry,
    validate_routes,
)

# Repository data, relative to the repository root.
HOST_DATA = "common/agent-host.json"
TEMPLATE = "common/agent-template/CLAUDE.common.md"
HINDSIGHT_MAP = "common/hindsight/coding-agent.json"
SPECS_DIR = "common/specializations"  # the specialization registry
ADD_USER = "common/install/add_user.sh"
# What every agent with capability channels runs from the code.
CAP_RUNTIME = ("common/bin/kd-cap", "common/bin/kd-cap-expect", "common/bin/kd-cap-serve", cap_units.TARGET_SOURCE)
# The runtime agent registry, by its name in the resource table.
REGISTRY_RESOURCE = "agent-registry"

# Generic system locations (Linux, systemd, sudo, Claude Code); no deployment data.
FLOOR = "/etc/claude-code/managed-settings.json"
PASSWD = "/etc/passwd"
GROUP = "/etc/group"
SUDOERS_DIR = "/etc/sudoers.d"
# The session host starts every agent through it (kd.session_host.claude.LAUNCH).
LAUNCHER = "/usr/local/bin/agent-launch"
UNIT_DIR = "/etc/systemd/system"
WRAPPER_DIR = "/usr/local/bin"
COMMAND_DIRS = (
    "/usr/local/sbin",
    "/usr/local/bin",
    "/usr/sbin",
    "/usr/bin",
    "/sbin",
    "/bin",
)
# Installed before any agent (the host setup): the installer, the launch and the units;
# sudo only lists grants (verify: an agent user and the base user hold none).
BASE_COMMANDS = (
    "flock",
    "pgrep",
    "pkill",
    "python3",
    "runuser",
    "sudo",
    "systemctl",
    "useradd",
)
# What add_user.sh needs besides them: downloads, the Hindsight client, and the base
# user's Claude Code, whose version every agent user gets.
ADD_USER_COMMANDS = ("curl", "node")
BASE_CLAUDE = ".local/bin/claude"
# What add_user.sh does while preparing the user, before its board grant; tests pin every
# source and path to the script, so the plan cannot drift from it.
HINDSIGHT_ENV = "/etc/hindsight/coding-agent.env"
ADD_USER_EFFECTS = (
    (
        "group",
        "digipaddock-cabinet",
        "{user} joins it through common/install/cabinet_setup.sh grant: the board's socket"
        " (KD-225)",
    ),
    (
        "download",
        ".local/bin/claude",
        "Claude Code, the base user's version, from https://claude.ai/install.sh"
        " (network)",
    ),
    ("dir", ".config", "{owner} 0700, made by {user}"),
    (
        "plugin",
        "hindsight",
        f"if {HINDSIGHT_ENV} exists: @vectorize-io/hindsight-coding-agents through npx"
        " for {user} (network)",
    ),
    (
        "file",
        ".hindsight/coding-agent.json",
        "{owner} 0600, made by {user} if Hindsight runs: holds the API key"
        " (value not shown)",
    ),
)
# command -> package; the installer adds a missing one (spec 5.2b: every agent is sandboxed).
SANDBOX_PACKAGES = {"bwrap": "bubblewrap", "socat": "socat"}
ACL_PACKAGES = {"setfacl": "acl", "getfacl": "acl"}
# The sandbox profile needs AppArmor's userland: a minimal Ubuntu has the kernel's restriction without it.
APPARMOR_PACKAGES = {"apparmor_parser": "apparmor"}
PACKAGE_INSTALLER = "apt-get"
USERNS_SYSCTL = "/proc/sys/kernel/apparmor_restrict_unprivileged_userns"
BWRAP_PROFILE = "/etc/apparmor.d/bwrap"
# Lets bwrap create user namespaces where AppArmor restricts them (Ubuntu 24.04): a
# host-wide relaxation for every user, accepted for the sandbox (spec 5.2b); the commands
# inside the sandbox keep their confinement.
BWRAP_PROFILE_TEXT = """abi <abi/4.0>,
include <tunables/global>

profile bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
  include if exists <local/bwrap>
}
"""
PRIVILEGED_GROUPS = frozenset({"admin", "sudo", "wheel"})
TRUST_FILE = ".claude.json"
MAX_STATE_BYTES = 1024 * 1024
MAX_LINKS = 16

HOST_FIELDS = frozenset(
    {
        "schema_version",
        "unit_dependencies",
        "group_grants",
    }
)
# 2: no panel units and no Telegram consumers; 3: no agents without a manifest and no
# legacy runtimes to migrate; 4: the owner's language is the deployment's (host.json owner_language)
HOST_SCHEMA = 4
_UNIT_FILE_RE = re.compile(r"[a-z][a-z0-9-]{0,63}@?\.(?:service|timer)\Z")
_COMMAND_RE = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
_GROUP_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}\Z")
_PLAYBOOK_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\.md\Z")
_DIGITS = re.compile(r"[0-9]{1,10}\Z")


class PlanError(ValueError):
    """The agent must not be installed; ``errors`` lists every problem found."""

    def __init__(self, errors: str | Iterable[str]) -> None:
        self.errors = (errors,) if isinstance(errors, str) else tuple(errors)
        super().__init__("; ".join(self.errors))


# --- the narrow read-only view of the system ----------------------------------------------


def _is_host_path(value: str) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("/")
        and len(value) <= 4096
        and posixpath.normpath(value) == value
        and not value.startswith("//")
        and "\\" not in value
        and "\0" not in value
    )


class HostView:
    """Read-only access to the system state below ``root``: "/" on a host, a fixture in tests.

    Every path is an absolute host path. Nothing here writes; nothing reads a file or lists
    a directory inside a home, where the agents' logins, channel tokens and keys live.
    ``reads`` and ``listings`` record each file read and directory listed, so a test can
    prove which state a check consulted.
    """

    def __init__(self, root: str | os.PathLike[str], routes=None) -> None:
        self.root = Path(root)
        self.reads: list[str] = []
        self.listings: list[str] = []
        self._routes = routes  # () -> the board's routes: a test's fixture; None — the cabinet below ``root``

    def board_routes(self) -> dict[str, str]:
        """project -> route as the cabinet holds them; ``RegistryError`` when it does not answer."""
        from kd.cabinet import client, server  # lazily: the cabinet's API imports the registry modules

        self.reads.append(server.SOCKET)  # the check consulted the cabinet
        if self._routes is not None:
            return self._routes()
        return board_routes(client.Client(str(self.path(server.SOCKET))))

    def path(self, absolute: str) -> Path:
        if not _is_host_path(absolute):
            raise PlanError(f"unsafe: not a normalized absolute path {absolute!r}")
        return self.root.joinpath(*absolute.split("/")[1:])

    def kind(self, absolute: str) -> str | None:
        """file, dir, symlink or other for the path itself (not followed); None if absent."""
        try:
            mode = os.lstat(self.path(absolute)).st_mode
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError as exc:
            raise PlanError(
                f"cannot inspect {absolute} ({exc.__class__.__name__})"
            ) from exc
        if stat.S_ISLNK(mode):
            return "symlink"
        if stat.S_ISDIR(mode):
            return "dir"
        return "file" if stat.S_ISREG(mode) else "other"

    def readlink(self, absolute: str) -> str:
        try:
            return os.readlink(self.path(absolute))
        except OSError as exc:
            raise PlanError(
                f"cannot read the link {absolute} ({exc.__class__.__name__})"
            ) from exc

    def is_regular(self, absolute: str) -> bool:
        """A regular file at the path, following its own symlinks inside the root."""
        current = absolute
        for _ in range(MAX_LINKS):
            kind = self.kind(current)
            if kind != "symlink":
                return kind == "file"
            target = posixpath.join(posixpath.dirname(current), self.readlink(current))
            current = posixpath.normpath(target)
        return False

    def command(self, name: str) -> str | None:
        """Where a command is installed, searching the fixed system directories only."""
        for directory in COMMAND_DIRS:
            if self.is_regular(f"{directory}/{name}"):
                return f"{directory}/{name}"
        return None

    def same_directory(self, absolute: str, other: Path) -> bool:
        try:
            return os.path.samefile(self.path(absolute), other)
        except OSError:
            return False

    def list_dir(self, absolute: str) -> list[str] | None:
        """The sorted names in a directory that is not a symlink; None if absent."""
        if is_within(absolute, HOME_ROOT):
            raise PlanError(f"unsafe: the check never lists a home ({absolute})")
        kind = self.kind(absolute)
        if kind is None:
            return None
        if kind != "dir":
            raise PlanError(f"conflict: {absolute} is a {kind}, not a directory")
        self.listings.append(absolute)
        try:
            return sorted(os.listdir(self.path(absolute)))
        except OSError as exc:
            raise PlanError(
                f"cannot list {absolute} ({exc.__class__.__name__})"
            ) from exc

    def _readable(self, absolute: str) -> bool:
        if is_within(absolute, HOME_ROOT):
            raise PlanError(f"unsafe: the check never reads inside a home ({absolute})")
        kind = self.kind(absolute)
        if kind not in (None, "file"):
            raise PlanError(f"conflict: {absolute} is a {kind}, not a regular file")
        if kind is None:
            return False
        self.reads.append(absolute)
        return True

    def read_bytes(self, absolute: str, limit: int = MAX_STATE_BYTES) -> bytes | None:
        """A bounded regular file that is not a symlink; None if absent."""
        if not self._readable(absolute):
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(self.path(absolute), flags)
        except OSError as exc:
            raise PlanError(
                f"cannot read {absolute} ({exc.__class__.__name__})"
            ) from exc
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise PlanError(f"conflict: {absolute} is not a regular file")
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise PlanError(f"conflict: {absolute} is larger than {limit} bytes")
        return data

    def read_json_object(self, absolute: str) -> dict[str, Any] | None:
        """A bounded JSON object without duplicate keys (kd.registry rules); None if absent."""
        if not self._readable(absolute):
            return None
        try:
            return read_json_object(self.path(absolute))
        except RegistryError as exc:
            raise PlanError(f"{absolute}: {exc}") from exc


# --- the host data ------------------------------------------------------------------------


@dataclass(frozen=True)
class HostData:
    unit_commands: tuple[str, ...]
    unit_files: tuple[str, ...]
    group_grants: Mapping[str, str]  # group -> the capability whose right membership is


def _records(
    value: Any, fields: frozenset[str], label: str, errors: list[str]
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        errors.append(f"{label}: must be a list")
        return []
    good = []
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != fields:
            errors.append(f"{label} {index}: fields must be exactly {sorted(fields)}")
        else:
            good.append(item)
    return good


def _sorted_unique(keys: list[Any], label: str, errors: list[str]) -> None:
    if len(keys) != len(set(keys)):
        errors.append(f"{label}: duplicate entries")
    elif keys != sorted(keys):
        errors.append(f"{label}: entries must be sorted")


def _names(
    value: Any, pattern: re.Pattern[str], label: str, errors: list[str]
) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and pattern.fullmatch(item) for item in value
    ):
        errors.append(f"{label}: invalid names")
        return ()
    _sorted_unique(list(value), label, errors)
    return tuple(value)


def load_host_data(repo: str | os.PathLike[str], registry: Registry) -> HostData:
    """Read and check ``common/agent-host.json``; fails closed on any unknown field."""
    label = HOST_DATA
    try:
        data = read_json_object(Path(repo) / HOST_DATA)
    except RegistryError as exc:
        raise PlanError(f"{label}: {exc}") from exc
    if set(data) != HOST_FIELDS:
        raise PlanError(f"{label}: fields must be exactly {sorted(HOST_FIELDS)}")
    errors: list[str] = []
    if type(data["schema_version"]) is not int or data["schema_version"] != HOST_SCHEMA:
        errors.append(f"{label}: unsupported schema_version")

    dependencies = data["unit_dependencies"]
    unit_commands: tuple[str, ...] = ()
    unit_files: tuple[str, ...] = ()
    if not isinstance(dependencies, dict) or set(dependencies) != {"commands", "units"}:
        errors.append(f"{label}: unit_dependencies must be exactly commands and units")
    else:
        unit_commands = _names(
            dependencies["commands"], _COMMAND_RE, f"{label} unit commands", errors
        )
        unit_files = _names(
            dependencies["units"], _UNIT_FILE_RE, f"{label} unit files", errors
        )

    grants = {}
    for item in _records(
        data["group_grants"],
        frozenset({"group", "capability"}),
        f"{label} group_grants",
        errors,
    ):
        group, capability = item["group"], item["capability"]
        if not isinstance(group, str) or not _GROUP_RE.fullmatch(group):
            errors.append(f"{label}: invalid group")
        elif group in PRIVILEGED_GROUPS:
            errors.append(f"{label}: group {group} is never a capability grant")
        elif not isinstance(capability, str) or capability not in registry.capabilities:
            errors.append(f"{label}: unknown capability for group {group}")
        elif group in grants:
            errors.append(f"{label} group_grants: duplicate entries")
        else:
            grants[group] = capability
    if list(grants) != sorted(grants):
        errors.append(f"{label} group_grants: entries must be sorted")
    if errors:
        raise PlanError(errors)
    return HostData(unit_commands, unit_files, grants)


# --- reading the system state -------------------------------------------------------------


@dataclass(frozen=True)
class _Account:
    uid: int
    gid: int
    home: str


Groups = Mapping[str, tuple[int, frozenset[str]]]


def _lines(data: bytes, path: str) -> list[str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PlanError(f"{path}: not UTF-8") from exc
    return [line for line in text.splitlines() if line.strip()]


def _accounts(host: HostView) -> tuple[dict[str, _Account], dict[str, Any]]:
    passwd, group = host.read_bytes(PASSWD), host.read_bytes(GROUP)
    if passwd is None or group is None:
        raise PlanError(f"missing dependency: {PASSWD} and {GROUP}")
    users = {}
    for line in _lines(passwd, PASSWD):
        fields = line.split(":")
        if len(fields) != 7 or not all(_DIGITS.fullmatch(f) for f in fields[2:4]):
            raise PlanError(f"{PASSWD}: unsupported line")
        users[fields[0]] = _Account(int(fields[2]), int(fields[3]), fields[5])
    groups = {}
    for line in _lines(group, GROUP):
        fields = line.split(":")
        if len(fields) != 4 or not _DIGITS.fullmatch(fields[2]):
            raise PlanError(f"{GROUP}: unsupported line")
        members = frozenset(m for m in fields[3].split(",") if m)
        groups[fields[0]] = (int(fields[2]), members)
    return users, groups


def _groups_of(user: str, account: _Account, groups: Groups) -> set[str]:
    return {
        name
        for name, (gid, members) in groups.items()
        if gid == account.gid or user in members
    }


def _runtime_agents(data: Mapping[str, Any], path: str) -> dict[str, str]:
    """The installed agent registry under the kd.agents rules: name -> user."""
    agents: dict[str, str] = {}
    owners: dict[str, str] = {}
    for name, item in sorted(data.items()):
        if (
            not NAME_RE.fullmatch(name)
            or not isinstance(item, dict)
            or not set(item) <= {"user", "runtime"}
        ):
            raise PlanError(f"{path}: invalid entry {name!r}")
        user = item.get("user", DEFAULT_USER)
        if not isinstance(user, str) or not USER_RE.fullmatch(user):
            raise PlanError(f"{path}: invalid user of {name}")
        if "runtime" in item and item["runtime"] != GENERATED_RUNTIME:
            raise PlanError(f"{path}: invalid runtime of {name}")
        if user != DEFAULT_USER:
            if user in owners:
                raise PlanError(f"{path}: user {user} serves two agents")
            owners[user] = name
        agents[name] = user
    return agents


# --- the plan -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Action:
    """One system action of the install, in the order it would run."""

    kind: str
    target: str
    detail: str = ""


@dataclass(frozen=True)
class GeneratedFile:
    """A file the install writes with exactly this content; ``show`` prints it in the plan."""

    path: str
    owner: str
    mode: str
    content: bytes
    show: bool


@dataclass(frozen=True)
class Plan:
    manifest: AgentManifest
    policy: Mapping[str, Any]
    actions: tuple[Action, ...]
    files: tuple[GeneratedFile, ...]
    owner_steps: tuple[str, ...]


@dataclass(frozen=True)
class _Inputs:
    manifests: Mapping[str, AgentManifest]
    manifest: AgentManifest
    registry: Registry
    host_data: HostData
    policy_channels: bool = False  # the compiled policy has capability channels


@dataclass(frozen=True)
class _Facts:
    user_present: bool
    service_users: tuple[tuple[str, bool], ...]
    packages: tuple[tuple[str, str], ...]  # (package, missing command)
    apparmor_profile: bool
    links: tuple[tuple[str, str, bool], ...]  # (link, target, present)
    uid: int | None = None  # the agent user's, None before its creation


def _load_inputs(repo: Path, name: str) -> _Inputs:
    try:
        manifests = validate_all(repo)
    except ManifestError as exc:
        raise PlanError(f"bad manifest: {exc}") from exc
    manifest = manifests.get(name)
    if manifest is None:
        raise PlanError(f"bad manifest: no agents/{name}/agent.json")
    try:
        registry = load_registry(repo)
        validate_registry(registry)
    except RegistryError as exc:
        raise PlanError([f"registry: {e}" for e in exc.errors]) from exc
    return _Inputs(manifests, manifest, registry, load_host_data(repo, registry))


def _registry_path(registry: Registry) -> str:
    resource = registry.resources.resources.get(REGISTRY_RESOURCE)
    if (
        resource is None
        or resource.path is None
        or resource.kind != "file"
        or resource.secret
    ):
        raise PlanError(
            f"registry: resource {REGISTRY_RESOURCE} must be a public file path"
        )
    # agent-launch, the session host and add_user.sh read kd.agents.REGISTRY; a registry written
    # elsewhere would install and verify cleanly while the launcher still saw "legacy".
    if resource.path != REGISTRY:
        raise PlanError(
            f"registry: resource {REGISTRY_RESOURCE} must be {REGISTRY}, the file agent-launch reads"
        )
    return resource.path


def _board_path(registry: Registry) -> str:
    resource = registry.resources.resources.get(BOARD_RESOURCE)
    if resource is None or resource.path is None or resource.kind != "dir":
        raise PlanError(f"registry: resource {BOARD_RESOURCE} must be a directory path")
    return resource.path


def _board_grant(
    repo: Path, inputs: _Inputs, policy: Mapping[str, Any]
) -> BoardGrant | None:
    """What add_user.sh will grant on the board: kd.board_grant decides, and it must equal
    the compiled acl. None for the base user, which owns the board files and which
    add_user.sh never takes."""
    user = inputs.manifest.user
    if user == DEFAULT_USER:
        return None
    try:
        grant = board_grant(user, repo)
        board = _board_path(inputs.registry)
        compiled = grant_from_acl(policy["acl"], user, board, grant.path)
        if compiled != grant:
            raise PlanError("board grant: kd.board_grant differs from the policy")
    except BoardGrantError as exc:
        raise PlanError(f"board grant: {exc}") from exc
    return grant


def board_acl(access: str, user: str) -> tuple[str, str]:
    """The ACL entries add_user.sh sets for a board grant: (directories, files).

    read is a named read-only entry; none sets nothing. Tests pin these strings to
    add_user.sh.
    """
    if access == "read":
        return f"u:{user}:rX,d:u:{user}:rX", f"u:{user}:rX"
    return "", ""


def _held_wrappers(inputs: _Inputs) -> list[dict[str, Any]]:
    capabilities = inputs.registry.capabilities
    return [
        wrapper
        for name in sorted(inputs.manifest.capabilities)
        for wrapper in capabilities[name]["wrappers"]
    ]


def _check_code(
    host: HostView, repo: Path, inputs: _Inputs, errors: list[str]
) -> tuple[tuple[str, str, bool], ...]:
    """The deployed code is this repository, holds every wrapper the agent runs, and each
    wrapper's PATH link is absent or already the right one."""
    layout = inputs.registry.resources.layout
    code = layout["code"]
    if not host.same_directory(code, repo):
        errors.append(f"conflict: {code} is not the repository this check runs from")
    needed = set()
    links = []
    for wrapper in _held_wrappers(inputs):
        if wrapper["source"] == "platform":
            continue
        needed.add(wrapper["source"])
        if wrapper["executor"] is not None:
            needed.add(wrapper["executor"])
        link, target = (
            f"{WRAPPER_DIR}/{wrapper['command']}",
            f"{code}/{wrapper['source']}",
        )
        kind = host.kind(link)
        if kind is None:
            links.append((link, target, False))
        elif kind == "symlink" and host.readlink(link) == target:
            links.append((link, target, True))
        else:
            errors.append(f"conflict: {link} exists and is not the link to {target}")
    if inputs.policy_channels:
        needed.update(CAP_RUNTIME)
    # Planned wrappers (kd.registry.PLANNED_FILES) may be named before they exist; an
    # install needs them all.
    errors += [
        f"missing dependency: {relative} is not in the repository"
        for relative in sorted(needed)
        if not _is_repo_file(repo, relative)
    ]
    return tuple(sorted(set(links)))  # a wrapper split by forms has one link


def _is_repo_file(repo: Path, relative: str) -> bool:
    path = repo / relative
    return not path.is_symlink() and path.is_file()


def _check_commands(
    host: HostView, inputs: _Inputs, policy: Mapping[str, Any], errors: list[str]
) -> tuple[tuple[str, str], ...]:
    required = (
        set(BASE_COMMANDS)
        | set(inputs.host_data.unit_commands)
        | {w["command"] for w in _held_wrappers(inputs) if w["source"] == "platform"}
    )
    if inputs.manifest.user != DEFAULT_USER:
        required |= set(ADD_USER_COMMANDS)
        claude = f"{HOME_ROOT}/{DEFAULT_USER}/{BASE_CLAUDE}"
        # Inspected, never read: add_user.sh gives every agent user this version.
        if host.kind(claude) not in ("file", "symlink"):
            errors.append(f"missing dependency: Claude Code of {DEFAULT_USER} {claude}")
    errors += [
        f"missing dependency: command {command}"
        for command in sorted(required)
        if host.command(command) is None
    ]
    installable = dict(SANDBOX_PACKAGES)
    if policy["acl"] or inputs.manifest.user != DEFAULT_USER:
        installable.update(ACL_PACKAGES)
    if _needs_bwrap_profile(host):
        installable.update(APPARMOR_PACKAGES)
    missing_packages: dict[str, list[str]] = {}
    for command, package in sorted(installable.items()):
        if host.command(command) is None:
            missing_packages.setdefault(package, []).append(command)
    packages = tuple(
        (package, ", ".join(commands))
        for package, commands in sorted(missing_packages.items())
    )
    if packages and host.command(PACKAGE_INSTALLER) is None:
        errors.append(f"missing dependency: command {PACKAGE_INSTALLER}")
    if policy["channels"] and host.command("systemd-tmpfiles") is None:
        errors.append("missing dependency: command systemd-tmpfiles")
    if not host.is_regular(LAUNCHER):
        errors.append(f"missing dependency: {LAUNCHER}")
    return packages


def _check_accounts(
    host: HostView, inputs: _Inputs, policy: Mapping[str, Any], errors: list[str]
) -> tuple[int | None, tuple[tuple[str, bool], ...]]:
    users, groups = _accounts(host)
    manifest = inputs.manifest
    if DEFAULT_USER not in users:
        errors.append(f"missing dependency: base user {DEFAULT_USER}")
    service = sorted(
        {w["run_as"] for w in _held_wrappers(inputs) if w["run_as"] in EXECUTOR_MODULES}
    )
    agent_users = {m.user for m in inputs.manifests.values()} | {DEFAULT_USER}
    # Every policy closes /home/<user> of the host's agents; a different home escapes it.
    for user in sorted(agent_users | set(service)):
        account = users.get(user)
        if account is not None and account.home != f"{HOME_ROOT}/{user}":
            errors.append(f"conflict: the home of {user} is not {HOME_ROOT}/{user}")
        if host.kind(f"{HOME_ROOT}/{user}") not in (None, "dir"):
            errors.append(f"conflict: {HOME_ROOT}/{user} is not a directory")
    # The agent user, the base user and every service account must not be root.
    for user in sorted({manifest.user, DEFAULT_USER} | set(EXECUTOR_MODULES) | SERVICE_USERS):
        account = users.get(user)
        if account is not None and (account.uid == 0 or account.gid == 0):
            errors.append(f"conflict: {user} has the root uid or group")
    account = users.get(manifest.user)
    if account is not None:
        errors += _group_errors(manifest, account, groups, inputs.host_data)
    uid = None if account is None else account.uid
    return uid, tuple((u, u in users) for u in service)


def _group_errors(
    manifest: AgentManifest, account: _Account, groups: Groups, host_data: HostData
) -> list[str]:
    """An existing user's groups are a whitelist: its own private group as the primary
    group, plus the groups whose right a held capability is; anything else may grant a
    right that no capability gives (sudo, docker, disk, another agent's group)."""
    user = manifest.user
    errors = []
    own = groups.get(user)
    if own is None or own[0] != account.gid or not own[1] <= {user}:
        errors.append(
            f"conflict: the primary group of {user} is not its own group {user}"
            " without other members"
        )
    allowed = {
        group
        for group, capability in host_data.group_grants.items()
        if capability in manifest.capabilities
    }
    errors += [
        f"conflict: {user} is in group {group}, which is neither its own group nor"
        " a group of its capabilities"
        for group in sorted(_groups_of(user, account, groups) - {user} - allowed)
    ]
    return errors


def _installed_agents(host: HostView, inputs: _Inputs) -> set[str]:
    """Agents installed on the host: every folder in the agents root except the board,
    and every entry of the runtime registry (agents under the base user have none)."""
    layout = inputs.registry.resources.layout
    root = layout["agents"]
    names = host.list_dir(root)
    if names is None:
        raise PlanError(f"missing dependency: agents root {root}")
    board = _board_path(inputs.registry)
    found = set()
    for name in names:
        path = f"{root}/{name}"
        kind = host.kind(path)
        if path == board or kind == "file":
            continue  # the board, or a plain file such as a note: not an agent
        if kind != "dir":
            raise PlanError(f"conflict: {path} is a {kind}, not an agent folder")
        if not NAME_RE.fullmatch(name):
            raise PlanError(f"conflict: unexpected entry {name!r} in {root}")
        found.add(name)
    return found


def _check_agents(
    host: HostView,
    inputs: _Inputs,
    runtime: Mapping[str, str],
    errors: list[str],
) -> None:
    """Every installed agent has a manifest and the runtime registry agrees with it; no home
    stays open (a manifest's user is unique: kd.manifest.validate_all)."""
    manifests = inputs.manifests
    path = _registry_path(inputs.registry)
    for name in sorted(_installed_agents(host, inputs) | set(runtime)):
        known = manifests.get(name)
        if known is None:
            errors.append(f"conflict: installed agent {name} has no manifest")
        elif name in runtime and runtime[name] != known.user:
            errors.append(f"conflict: {path} runs {name} as {runtime[name]}, not {known.user}")


def _agent_paths(folder: str) -> tuple[tuple[str, str], ...]:
    return (
        (folder, "dir"),
        (f"{folder}/.claude", "dir"),
        (f"{folder}/.claude/policy.json", "file"),
        (f"{folder}/.claude/launch-settings.json", "file"),
        (f"{folder}/.claude/settings.json", "file"),
        (f"{folder}/CLAUDE.md", "file"),
        (f"{folder}/memory", "dir"),
        (f"{folder}/memory/notes.md", "file"),
        (f"{folder}/playbooks", "dir"),
        (f"{folder}/playbooks/spec", "dir"),
        (f"{folder}/playbooks/skill", "dir"),
        (f"{folder}/work", "dir"),
        *((f"{folder}/work/{item}", "file") for item in WORK_PLACEHOLDERS),
    )


def _made_by_setup(registry: Registry, policy: Mapping[str, Any]) -> set[str]:
    """Read-grant resources of a capability whose own setup runs in this install: the setup makes them before the
    grants (INSTALL_ORDER), so on a new host they are not there yet (repo.read's checkouts, the forge and
    exec journals). A setup that does not make its resource fails the install at the grant."""
    held = {step.split(":", 1)[0] for step in policy["install_steps"]["install"] if step.endswith(":setup")}
    return {
        resource["name"]
        for name in held
        for resource in registry.capabilities[name]["resources"]
        if resource["access"] == "read-grant"
    }


def _check_files(
    host: HostView,
    inputs: _Inputs,
    policy: Mapping[str, Any],
    grant: BoardGrant | None,
    errors: list[str],
) -> bool:
    """Existing files must have the kind the install writes; grant targets and the units it
    depends on must exist. Returns whether the bwrap profile is needed."""
    folder = f"{inputs.registry.resources.layout['agents']}/{inputs.manifest.name}"
    for path, expected in _agent_paths(folder):
        kind = host.kind(path)
        if kind not in (None, expected):
            errors.append(
                f"conflict: {path} is a {kind}, the install needs a {expected}"
            )
    table = inputs.registry.resources.resources
    made = _made_by_setup(inputs.registry, policy)
    targets = [
        (entry["path"], table[entry["resource"]].kind, entry["resource"] in made)
        for entry in policy["acl"]
        if entry["resource"] != BOARD_RESOURCE
    ]
    if grant is not None and grant.access != "none":
        targets.append((grant.path, "dir", False))
    for path, expected, by_setup in targets:
        kind = host.kind(path)
        if kind is None and by_setup:
            continue
        if kind != expected:
            errors.append(
                f"missing dependency: grant target {path} ({expected})"
                if kind is None
                else f"conflict: grant target {path} is a {kind}"
            )
    errors += [
        f"missing dependency: unit {UNIT_DIR}/{name}"
        for name in sorted(inputs.host_data.unit_files)
        if host.kind(f"{UNIT_DIR}/{name}") != "file"
    ]
    profile = host.kind(BWRAP_PROFILE)
    if profile not in (None, "file"):
        errors.append(f"conflict: {BWRAP_PROFILE} is a {profile}")
    elif _userns_restricted(host) and profile == "file":
        if host.read_bytes(BWRAP_PROFILE, 4096) != BWRAP_PROFILE_TEXT.encode():
            errors.append(f"conflict: {BWRAP_PROFILE} differs from the sandbox profile")
    return _needs_bwrap_profile(host)


def _userns_restricted(host: HostView) -> bool:
    restricted = host.read_bytes(USERNS_SYSCTL, 64)
    return restricted is not None and restricted.strip() == b"1"


def _needs_bwrap_profile(host: HostView) -> bool:
    """The kernel restricts unprivileged user namespaces and the sandbox profile is not written yet."""
    return _userns_restricted(host) and host.kind(BWRAP_PROFILE) is None


def _check_channels(host: HostView, inputs: _Inputs, errors: list[str]) -> None:
    """The channel directories are absent or directories."""
    name = inputs.manifest.name
    for path in (f"{cap_units.AGENT_BIN}/{name}", f"{cap_grants.GRANTS_DIR}/{name}"):
        kind = host.kind(path)
        if kind not in (None, "dir"):
            errors.append(f"conflict: {path} is a {kind}, the install needs a dir")


def _inspect(
    host: HostView,
    repo: Path,
    inputs: _Inputs,
    policy: Mapping[str, Any],
    grant: BoardGrant | None,
) -> _Facts:
    errors: list[str] = []
    path = _registry_path(inputs.registry)
    runtime = _runtime_agents(host.read_json_object(path) or {}, path)
    links = _check_code(host, repo, inputs, errors)
    packages = _check_commands(host, inputs, policy, errors)
    uid, service = _check_accounts(host, inputs, policy, errors)
    _check_agents(host, inputs, runtime, errors)
    apparmor = _check_files(host, inputs, policy, grant, errors)
    _check_channels(host, inputs, errors)
    if errors:
        raise PlanError(errors)
    return _Facts(uid is not None, service, packages, apparmor, links, uid)


def _channel_actions(
    plan: _Builder, inputs: _Inputs, policy: Mapping[str, Any], facts: _Facts, target: bytes
) -> None:
    """Units and links of the agent's capability channels, then their grants files: the
    channels' switch, written last."""
    channels = policy["channels"]
    if not channels:
        return
    name, user = inputs.manifest.name, inputs.manifest.user
    code = inputs.registry.resources.layout["code"]
    try:
        plan.file(cap_units.TARGET_PATH, "root:root", "0644", target)
        for path, content in cap_units.unit_files(name, user, code, channels):
            plan.file(path, "root:root", "0644", content)
        plan.add("dir", f"{cap_units.AGENT_BIN}/{name}", "root:root 0755, only these links; first in the agent's PATH")
        for link, client in cap_units.links(name, code, channels):
            plan.add("link", link, f"-> {client}")
        plan.add("unit", cap_units.TARGET, "enable")
        for channel in channels:
            plan.add(
                "unit",
                cap_units.socket_name(name, channel["key"]),
                f"enable and restart; each call runs common/bin/kd-cap-serve as {channel['run_as']}",
            )
        for channel in channels:
            path = cap_units.grants_path(name, channel["key"])
            if facts.uid is None:
                plan.add("file", path, f"root:root 0644, rendered with the uid of {user} after its creation")
            else:
                plan.file(path, "root:root", "0644", cap_grants.render(name, facts.uid, channel), show=True)
    except ValueError as exc:
        raise PlanError(f"unsafe: channel of {name}: {exc}") from exc


def _acl_detail(entry: Mapping[str, str], kind: str) -> str:
    perms = "rw" if entry["access"] == "write" else "r"
    user = entry["user"]
    if kind == "dir":
        return (
            f"{entry['resource']} {entry['access']}:"
            f" u:{user}:{perms}X d:u:{user}:{perms}X, recursive"
        )
    return f"{entry['resource']} {entry['access']}: u:{user}:{perms}"


def _digest(content: bytes) -> str:
    return f"sha256={hashlib.sha256(content).hexdigest()} bytes={len(content)}"


class _Builder:
    def __init__(self) -> None:
        self.actions: list[Action] = []
        self.files: list[GeneratedFile] = []

    def add(self, kind: str, target: str, detail: str = "") -> None:
        self.actions.append(Action(kind, target, detail))

    def file(
        self, path: str, owner: str, mode: str, content: bytes, *, show: bool = False
    ) -> None:
        self.files.append(GeneratedFile(path, owner, mode, content, show))
        self.add("file", path, f"{owner} {mode} {_digest(content)}")


def _playbooks(repo: Path, name: str, host: Mapping[str, str]) -> list[tuple[str, bytes]]:
    """The agent's playbooks as installed: ``{{host.<key>}}`` filled in; ``*.example.md`` (the public
    stand-in of an overlay file) is never installed."""
    directory = repo / "agents" / name / "playbooks"
    if not directory.exists() and not directory.is_symlink():
        return []
    if directory.is_symlink() or not directory.is_dir():
        raise PlanError(f"bad manifest: agents/{name}/playbooks is not a directory")
    found = []
    for path in sorted(directory.iterdir()):
        if (
            not _PLAYBOOK_RE.fullmatch(path.name)
            or path.is_symlink()
            or not path.is_file()
        ):
            raise PlanError(
                f"bad manifest: agents/{name}/playbooks holds only regular .md files"
            )
        if path.name.endswith(".example.md"):
            continue
        try:
            text = render_playbook(read_instruction_file(path), host)
        except AgentTextError as exc:
            raise PlanError(f"bad manifest: playbook {path.name}: {exc}") from exc
        found.append((path.name, text.encode("utf-8")))
    return found


def _user_actions(
    plan: _Builder, inputs: _Inputs, grant: BoardGrant | None, facts: _Facts
) -> None:
    """add_user.sh --prepare: user and tools, but no capability grant."""
    manifest = inputs.manifest
    user, home = manifest.user, f"{HOME_ROOT}/{manifest.user}"
    if grant is None:
        plan.add("user", user, "the base user: present, not created")
        return
    state = (
        "present, re-run"
        if facts.user_present
        else "useradd --create-home --user-group --shell /bin/bash"
    )
    plan.add(
        "user",
        user,
        f"{ADD_USER} {user} --prepare: {state}; {home} and {HOME_ROOT}/{DEFAULT_USER} 0750",
    )
    owner = f"{user}:{user}"
    for kind, target, detail in ADD_USER_EFFECTS:
        path = target if kind in ("plugin", "group") else f"{home}/{target}"
        plan.add(kind, path, detail.format(user=user, owner=owner))


def _board_actions(
    plan: _Builder, inputs: _Inputs, grant: BoardGrant | None, facts: _Facts
) -> None:
    """add_user.sh --grant: only after the generated files and prerequisites exist."""
    manifest, registry = inputs.manifest, inputs.registry
    user = manifest.user
    backlog = f"{_board_path(registry)}/{BACKLOG_DIR}"
    if grant is None:
        plan.add("board-grant", backlog, f"no grant: {user} owns the board files")
        return
    plan.add(
        "board-grant",
        grant.path,
        f"{grant.access}: decided by kd.board_grant from the compiled acl;"
        f" {ADD_USER} --grant applies it after policy publication",
    )
    directories, files = board_acl(grant.access, user)
    if directories:
        plan.add("acl", grant.path, f"{directories} (by root, on the directory itself)")
        plan.add(
            "acl",
            f"{grant.path}/**",
            f"directories {directories}, files {files} (only entries owned by"
            f" {DEFAULT_USER}, set by {DEFAULT_USER})",
        )
    plan.add("verify", user, f"can write nothing in {grant.path}")


def _specializations(repo: Path, manifest: AgentManifest) -> list[tuple[str, bytes]]:
    """``(name, text bytes)`` of the specializations of the agent's type it may hold (spec
    5.8.4): installed root-owned under playbooks/spec/, read by the agent when it takes a card.
    The bytes are the registry file's, so the installed hash is the registry hash."""
    try:
        registry = specializations.load_registry(repo / SPECS_DIR, require_root_owner=False)
    except specializations.SpecError as exc:
        raise PlanError(f"registry: specializations: {exc}") from exc
    return [
        (spec.name, spec.text.encode("utf-8"))
        for spec in specializations.installable(registry, manifest.profile, manifest.capabilities)
    ]


def _assemble(
    inputs: _Inputs,
    policy: Mapping[str, Any],
    grant: BoardGrant | None,
    instructions: str,
    playbooks: list[tuple[str, bytes]],
    facts: _Facts,
    cap_target: bytes = b"",
    specs: list[tuple[str, bytes]] = (),
    skills: list[tuple[str, bytes]] = (),
) -> Plan:
    manifest, registry = inputs.manifest, inputs.registry
    name, user = manifest.name, manifest.user
    folder = f"{registry.resources.layout['agents']}/{name}"
    home = f"{HOME_ROOT}/{user}"
    mine = f"{user}:{user}"
    plan = _Builder()

    for package, command in facts.packages:
        plan.add(
            "package",
            package,
            f"{PACKAGE_INSTALLER} install; {command}"
            f" {'are' if ',' in command else 'is'} missing",
        )
    for service, present in facts.service_users:
        plan.add(
            "service-user",
            service,
            "present"
            if present
            else f"create: system user, no login shell, home {HOME_ROOT}/{service} 0700",
        )
    _user_actions(plan, inputs, grant, facts)
    # The entry is generated before the policy exists: until policy.json is published the
    # launch fails closed.
    entry = {"runtime": GENERATED_RUNTIME, "user": user}
    plan.add(
        "registry",
        _registry_path(registry),
        f"set {name} = {json.dumps(entry, sort_keys=True)}",
    )

    plan.add("dir", folder, "root:root 0755")
    plan.add("dir", f"{folder}/.claude", "root:root 0755")
    plan.file(f"{folder}/CLAUDE.md", "root:root", "0644", instructions.encode("utf-8"))
    plan.file(
        f"{folder}/.claude/policy.json",
        "root:root",
        "0644",
        render_policy(policy),
        show=True,
    )
    plan.file(
        f"{folder}/.claude/settings.json",
        "root:root",
        "0644",
        render_policy(policy["settings"]),
        show=True,
    )
    plan.file(
        f"{folder}/.claude/launch-settings.json",
        "root:root",
        "0644",
        render_policy(policy["launch_settings"]),
        show=True,
    )
    if playbooks or specs or skills:
        plan.add("dir", f"{folder}/playbooks", "root:root 0755")
        for file_name, content in playbooks:
            plan.file(f"{folder}/playbooks/{file_name}", "root:root", "0644", content)
    if specs:
        plan.add("dir", f"{folder}/playbooks/spec", "root:root 0755")
        for spec_name, content in specs:
            plan.file(f"{folder}/playbooks/spec/{spec_name}.md", "root:root", "0644", content)
    if skills:
        plan.add("dir", f"{folder}/playbooks/skill", "root:root 0755")
        for skill_name, content in skills:
            plan.file(f"{folder}/playbooks/skill/{skill_name}.md", "root:root", "0644", content)
    # the agent's data is its own: 0750/0640 keep the other agents and local users out (global review 15.10.2026)
    plan.add("dir", f"{folder}/memory", f"{mine} 0750")
    plan.add(
        "file",
        f"{folder}/memory/notes.md",
        f"{mine} 0640, created empty by {user} if absent",
    )
    plan.add("dir", f"{folder}/work", f"{mine} 0750")
    placeholder = (
        "Root-owned placeholder: your instructions are ../CLAUDE.md"
        f" ({folder}/CLAUDE.md).\n"
    )
    for item in WORK_PLACEHOLDERS:
        text = placeholder if item == "CLAUDE.md" else ""
        plan.file(f"{folder}/work/{item}", "root:root", "0644", text.encode("utf-8"))

    for link, target, present in facts.links:
        plan.add("link", link, f"-> {target}" + (" (present)" if present else ""))
    plan.add(
        "trust",
        f"{home}/{TRUST_FILE}",
        f"by {user}: hasTrustDialogAccepted for {folder} and {folder}/work",
    )
    # with the trust step, by the agent's user (kd.workspace) — the shared notes split once
    plan.add(
        "migrate",
        f"{folder}/memory/notes.md",
        f"by {user}, once: original to memory/legacy/, one-card entries to memory/tasks/, map memory/legacy/migration.md;"
        " session folders memory/tasks, memory/chats, work/tmp/cards, work/tmp/chats",
    )
    if facts.apparmor_profile:
        plan.file(
            BWRAP_PROFILE, "root:root", "0644", BWRAP_PROFILE_TEXT.encode(), show=True
        )
        plan.add(
            "reload",
            "apparmor",
            "host-wide: bwrap may create user namespaces for every user"
            " (accepted host risk for the sandbox, spec 5.2b)",
        )
    # A module's own install steps (spec 5.4) run once the user's data exists and before
    # any grant; the same scripts check on verify and switch services off on disable.
    code = registry.resources.layout["code"]
    for capability in sorted(manifest.capabilities):
        setup = registry.capabilities[capability]["setup"]
        if setup is not None:
            plan.add(
                "setup",
                f"{code}/{setup}",
                f"{capability}: install, check and disable as root with {name} {user}",
            )
    # Every agent-specific grant comes after the published, fail-closed runtime policy.
    _board_actions(plan, inputs, grant, facts)
    table = registry.resources.resources
    for acl in policy["acl"]:
        if acl["resource"] != BOARD_RESOURCE:
            plan.add("acl", acl["path"], _acl_detail(acl, table[acl["resource"]].kind))
    _channel_actions(plan, inputs, policy, facts, cap_target)
    plan.add(
        "verify",
        name,
        "installed files and grants match this plan; channels: grants files match the"
        f" policy, sockets active; sudo -n -l -U {user} grants nothing, nor"
        f" {DEFAULT_USER} as {user}; sandbox preflight as {user}",
    )
    steps = [
        "if the host has no Claude token yet: claude setup-token, then"
        f" {naming.command('kd-secret')} put claude-token (once for all agents; a login of {user} also works);"
        f" the session host starts {name} after session_host_setup.sh enable {name}"
    ]
    return Plan(manifest, policy, tuple(plan.actions), tuple(plan.files), tuple(steps))


def build_plan(repo: str | os.PathLike[str], host: HostView, name: str) -> Plan:
    """Check everything an install of ``name`` needs and list its actions; never writes.

    ``repo`` is the repository the check runs from, which must be the deployed code root;
    ``host`` reads the system. Raises ``PlanError`` with every problem found at the first
    failing stage: repository data, then the compiled policy, the board grant and the
    instruction, then the system state.
    """
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise PlanError("invalid agent name")
    repo = Path(repo)
    inputs = _load_inputs(repo, name)
    try:  # no board project may route to an agent the repository no longer has (the cabinet's table)
        validate_routes(host.board_routes(), inputs.manifests)
    except RegistryError as exc:
        raise PlanError([f"registry: {e}" for e in exc.errors]) from exc
    manifest = inputs.manifest
    floor = host.read_json_object(FLOOR)
    if floor is None:
        raise PlanError(f"missing dependency: managed settings {FLOOR}")
    try:
        policy = compile_policy(
            manifest, inputs.registry, floor=floor, agents=inputs.manifests
        )
    except PolicyError as exc:
        raise PlanError([f"policy: {e}" for e in exc.errors]) from exc
    inputs = _Inputs(inputs.manifests, manifest, inputs.registry, inputs.host_data, bool(policy["channels"]))
    grant = _board_grant(repo, inputs, policy)
    try:  # the texts name no host; host.json and the overlay's includes fill them in
        deployment = naming.load_host()
    except naming.NamingError as exc:
        raise PlanError(f"missing dependency: {exc}") from exc
    try:
        instructions = render_instruction_files(
            repo / "agents" / name / "CLAUDE.md",
            agent=name,
            owner_language=deployment["owner_language"],  # the language the agents write to the owner in
            board_write="board.write" in manifest.capabilities,
            data_read="data.read" in manifest.capabilities,  # the kd-data section
            common_path=repo / TEMPLATE,
            hindsight_path=repo / HINDSIGHT_MAP,
            tree=repo,
            host=deployment,
            # the Hindsight tools come with the plugin, which add_user.sh installs only beside this env
            memory=host.kind(HINDSIGHT_ENV) is not None,
        )
    except AgentTextError as exc:
        raise PlanError(f"bad manifest: instructions: {exc}") from exc
    playbooks = _playbooks(repo, name, deployment)
    specs = _specializations(repo, manifest)
    instruction_registry = specializations.load_registry(repo / SPECS_DIR, require_root_owner=False)
    skills = [(s.name, s.text.encode('utf-8')) for s in specializations.installable_skills(
        instruction_registry, manifest.profile, manifest.capabilities)]
    facts = _inspect(host, repo, inputs, policy, grant)
    cap_target = (repo / cap_units.TARGET_SOURCE).read_bytes() if policy["channels"] else b""
    return _assemble(inputs, policy, grant, instructions, playbooks, facts, cap_target, specs, skills)


def render_plan(plan: Plan) -> str:
    """The plan as text: header, numbered actions, policy files in full, owner steps."""
    manifest, policy = plan.manifest, plan.policy
    kinds = [action.kind for action in plan.actions]
    board_step = kinds.index("board-grant") + 1
    lines = [
        f"plan for agent {manifest.name}",
        f"  user {manifest.user}, profile {manifest.profile},"
        f" model {manifest.model or 'default'}, sandbox {manifest.sandbox_profile}",
        f"  capabilities {', '.join(policy['capabilities'])}",
        "  capability install steps"
        f" {', '.join(policy['install_steps']['install']) or 'none'}",
        f"actions, in order: the board grant is deferred (step {board_step});"
        " every ACL and channel grant follows policy publication:",
    ]
    for index, action in enumerate(plan.actions, 1):
        detail = f": {action.detail}" if action.detail else ""
        lines.append(f"{index:3d}. {action.kind} {action.target}{detail}")
    shown = [f for f in plan.files if f.show]
    if shown:
        lines.append("files in full:")
    for item in shown:
        lines.append(f"--- {item.path} ({item.owner} {item.mode})")
        lines.append(item.content.decode("utf-8").rstrip("\n"))
        lines.append("--- end")
    lines.append("owner steps after the install:")
    lines += [f"  - {step}" for step in plan.owner_steps]
    return "\n".join(_printable(line) for line in lines) + "\n"


def _printable(text: str) -> str:
    # Diagnostics and plans reach a terminal: no control or format characters pass.
    return "".join(
        char if char == "\n" or unicodedata.category(char)[0] != "C" else "?"
        for char in text
    )


def runtime_check(repo: Path, name: str, err, findings=None) -> int:
    """On the host only: the role being checked must still start with this code (a schema
    change stops it at its next restart). only its own problems refuse — another
    role a reinstall has not reached yet is a warning (the factory installs roles in turns,
    and kd-release's postflight still checks the whole host)."""
    if findings is None:
        from kd.install_apply import generated_runtime_findings as findings
    from kd.install_apply import ApplyError

    try:
        found = findings(repo)
    except (ApplyError, OSError, ValueError) as exc:
        found = [(name, f"registry: {exc}")]
    own = [why for agent, why in found if agent == name]
    for agent, why in found:
        if agent != name:
            err.write(
                f"warning: generated runtime of {_printable(agent)}:"
                f" {_printable(why)} (not {_printable(name)}'s: not refused here)\n".encode()
            )
    for why in own:
        err.write(
            f"check failed: generated runtime refused: {_printable(name)}:"
            f" {_printable(why)}\n".encode()
        )
    err.flush()
    return 1 if own else 0


def main(argv: Sequence[str] | None = None, *, routes=None) -> int:
    """``--check AGENT`` or ``--plan AGENT`` over ``--root`` (default /); never writes. ``routes``: the board's
    projects in tests (``HostView``); the cabinet's answer on a host."""
    parser = argparse.ArgumentParser(
        prog="new_agent.sh",
        allow_abbrev=False,
        description="Check or plan an agent install from its manifest; changes nothing.",
    )
    parser.add_argument("--root", default="/", help="system root to inspect")
    parser.add_argument(
        "--repo",
        default=str(Path(__file__).resolve().parents[2]),
        help="repository root (default: the one holding this module)",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", metavar="AGENT")
    mode.add_argument("--plan", metavar="AGENT")
    args = parser.parse_args(argv)
    name = args.check if args.check is not None else args.plan
    out = sys.stdout.buffer
    try:
        plan = build_plan(args.repo, HostView(args.root, routes=routes), name)
    except PlanError as exc:
        label = _printable(name) if NAME_RE.fullmatch(name) else "the agent"
        text = f"check failed for {label}:\n" + "".join(
            f"  - {_printable(error)}\n" for error in exc.errors
        )
        sys.stdout.flush()
        sys.stderr.buffer.write(text.encode("utf-8"))
        sys.stderr.flush()
        return 1
    if args.check is not None:
        if os.name == "posix" and Path(args.root) == Path("/"):
            if runtime_check(Path(args.repo), name, sys.stderr.buffer) != 0:
                return 1
        text = f"check ok: {name}, {len(plan.actions)} actions; --plan lists them\n"
    else:
        text = render_plan(plan)
    out.write(text.encode("utf-8"))
    out.flush()
    return 0
