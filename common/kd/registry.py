"""Load and validate the agent registries (BOSS spec 2026-10-06, sections 4.2 and 5.1-5.2b).

Profiles carry role defaults only, capabilities are the finite source of rights and sandbox
profiles are fail-closed ceilings. The board's projects and their routes are the cabinet's
: ``board_routes`` asks it, and an absent cabinet is an error, never an empty board.
``load_registry`` reads the tracked JSON under one repository root and refuses malformed
files; ``validate_registry`` is pure and checks references and security rules;
``validate_selection`` checks one agent's profile, capabilities and sandbox choice.
``kd.manifest.validate_all``, the policy compiler and the installer check call this one
module, so no caller (and no test) keeps a second copy of these rules. Every refusal raises
``RegistryError`` that lists all problems found at that stage.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kd import naming

PROFILES = "common/agent-profiles"
CAPABILITIES = "common/agent-capabilities"
SANDBOXES = "common/agent-sandboxes"
RESOURCES = "common/agent-resources.json"
# Everything load_registry reads besides the files that capabilities reference.
REGISTRY_PATHS = (PROFILES, CAPABILITIES, SANDBOXES, RESOURCES)

MAX_JSON_BYTES = 64 * 1024  # the same bound as kd.manifest
CAPABILITY_RE = re.compile(r"[a-z][a-z0-9]*(?:\.[a-z][a-z0-9]*)+\Z")
NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
AGENT_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
COMMAND_RE = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
# Subcommand words, optionally ending in --help; a bare --help is the tool's own help.
# No other option is a form: options belong to the module's validator, not to the registry.
SUBCOMMAND_RE = re.compile(
    r"(?:[a-z][a-z0-9-]*(?: [a-z][a-z0-9-]*)*(?: --help)?|--help)\Z"
)
DOMAIN_RE = re.compile(
    r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+\Z"
)
REPO_PATH_RE = re.compile(r"(?:common|agents)(?:/[A-Za-z0-9_][A-Za-z0-9_.-]*)+\Z")
BIN_PATH_RE = re.compile(
    r"(?:common/bin|agents/[a-z][a-z0-9-]*/bin)/[A-Za-z0-9_][A-Za-z0-9_.-]*\Z"
)
UNSAFE_CHARS = frozenset("*?[]{}$`\\")
PATH_FIELDS = frozenset({"source", "executor", "validator", "setup"})

# Spec section 5.2: the finite capability table; the module comes from section 5.1.
# A new capability is an amendment of the spec, this table and its module's code.
CAPABILITY_MODULES = {
    "board.read": "board",
    "board.write": "board",
    "board.coordinate": "coordinator",
    "agent.nudge": "coordinator",
    "usage.read": "usage",
    "git.develop": "forge",
    "forge.review": "forge",
    "git.mirror": "forge",  # git fetch from the read-only code mirror (host/host.json mirror_remote)
    "servers.read": "servers",
    "servers.execute": "servers",
    "monitor.manage": "servers",
    "docs.read": "docs",
    "repo.read": "repo",  # read-only checkouts
    "worker.request": "worker",  # C07: worker jobs through the base-user executor kd-worker
    "diag.request": "diag",  # R31: catalog health probes through the base-user executor kd-diag
    "factory.request": "factory",  # the factory catalog's operations through the base-user executor kd-factory
    "host.read": "host",  # the journal, systemd and platform files through the host reader kd-host-read
    "e2e.request": "e2e",  # fixed smoke checks in an isolated prepared runtime
    "monitor.read": "monitor",  # the monitor's incidents and servers through the base-user executor kd-monitor-read
    "data.read": "data",  # read-only SQL on the servers' product databases through the executor kd-data
    "net.egress": "net",  # the autonomy design, piece 2: a consented card session's probe exit (host/egress.json)
}
MODULES = frozenset(CAPABILITY_MODULES.values())
# Spec section 5.2: each of these belongs to exactly one profile, which offers it.
PROFILE_ONLY = {"board.coordinate": "coordinator", "monitor.manage": "sysadmin"}
# Third-party tools a capability may call directly ("source": "platform"). Everything
# else is a repository wrapper with its own validation; a new tool here is a code change.
# none is left; the board is the cabinet, read and written through kd-task.
PLATFORM_COMMANDS: frozenset = frozenset()
# Owner approval in the loop (spec 3.5): the pin follows the wrapper's source, and the
# source keeps its one command name: the sysadmin guard, the executors and the owner know
# these commands by name. the owner's consent is the Forgejo Approve and the task
# grant checked by the executor, not a Claude Code card.
PINNED_WRAPPERS = {
    "agents/dev/bin/kd-merge": (naming.command("kd-merge"), "allow"),
    "agents/sysadmin/bin/ssh-exec": ("ssh-exec", "allow"),
    # the plan for the owner's approval page; it grants nothing by itself.
    "agents/sysadmin/bin/kd-admin-propose": (naming.command("kd-admin-propose"), "allow"),
    # one operation of the owner's grant; the executor re-checks the grant before SSH.
    "agents/sysadmin/bin/kd-admin-run": (naming.command("kd-admin-run"), "allow"),
    # one exact critical command for the owner's page; it grants nothing by itself.
    "agents/sysadmin/bin/kd-admin-propose-action": (naming.command("kd-admin-propose-action"), "allow"),
    # the owner-approved critical command, once; the service consumes the approval before SSH.
    "agents/sysadmin/bin/kd-admin-critical": (naming.command("kd-admin-critical"), "allow"),
}
# The merge executor also serves kd-forge (reads and comments, allowed without a card) and kd-mirror-fetch, so it
# cannot ask for every call; only its reviewed shims may reach it.
EXECUTOR_SOURCES = {
    # the server executor checks the task grant itself; only ssh-exec reaches it.
    "agents/sysadmin/bin/ssh_exec.py": frozenset({"agents/sysadmin/bin/ssh-exec"}),
    # kd-mirror-fetch (git.mirror): a branch of the code mirror into its bare copy, no card (review 04.11.2026)
    "agents/dev/bin/forge_exec.py": frozenset(
        {"agents/dev/bin/kd-forge", "agents/dev/bin/kd-merge", "agents/dev/bin/kd-mirror-fetch"}
    ),
    # The grant executor allows without a card, so only its own wrapper may reach it.
    "agents/sysadmin/bin/admin_run.py": frozenset({"agents/sysadmin/bin/kd-admin-run"}),
    "agents/sysadmin/bin/admin_critical.py": frozenset({"agents/sysadmin/bin/kd-admin-critical"}),
}
# Only repository wrappers are channel commands.
PLATFORM_CHANNELS: dict = {}

# Capabilities name resources; only the resource table (agent-resources.json) binds a
# name to a canonical path, so a registry entry can never open a new path by spelling it.
# own-file: a secret owned by the agent's own uid in its own home (its SSH key), readable
# by that uid only; every other secret is reached only through a wrapper. read-direct:
# world-readable data the agent opens with its Read tool, with no ACL (another project's
# files are never re-permissioned); read-public: data only the agent's wrappers read.
ACCESS_MODES = frozenset(
    {"own-file", "read-direct", "read-grant", "read-public", "via-wrapper", "write-grant"}
)
GRANTS = frozenset({"read-grant", "write-grant"})
# What the agent's own Read tool opens: an allow rule and, for a directory, a working
# directory of the session.
DIRECT_READS = GRANTS | {"read-direct"}
# The resource table may hold absolute paths only below these generic system roots; the
# deployment's own directories below them are data, never code.
SYSTEM_PREFIXES = (
    "/etc/",
    "/home/",
    "/opt/",
    "/srv/",
    "/var/lib/",
    "/var/log/",
    "/var/www/",
)
HOME_PLACEHOLDER = "{agent}"  # the only substitution: the agent's name
OWN_FILE_DIR = ".ssh"  # own-file secrets are the agent's SSH identity, nothing else
HOMES = "/home"
PROTECTION_DENIALS = frozenset({"edit", "read"})
# Executor accounts that a module creates, by the module each serves (and only that one).
EXECUTOR_MODULES = {
    naming.service_user("forge"): "forge",
    naming.service_user("exec"): "servers",
    naming.service_user("observe"): "host",
    naming.service_user("data"): "data",
}
# The base user runs the board executors (kd-task, kd-work); root never runs a channel.
RUN_AS = frozenset({"self", naming.base_user(), *EXECUTOR_MODULES})
# Executables added by later plan tasks (B02, B05); data may name them before they exist.
PLANNED_FILES = frozenset({"common/bin/kd-boss-board", "common/bin/kd-nudge"})
INSTALL_ORDER = (
    "executor-user",
    "setup",
    "wrappers",
    "read-grants",
    "write-grants",
    "channels",
)
# Grants go first, the write grant before the read grant; the module's own setup script
# switches its services off last; user data stays.
DISABLE_ORDER = ("channels", "write-grants", "read-grants", "setup")
# A module's own install steps (spec 5.4): one script in an install directory, run as root
# with install, check or disable.
_SETUP_RE = re.compile(r"(agents/[a-z][a-z0-9-]*|common)/install/[a-z][a-z0-9_]*_setup\.sh\Z")
FAIL_CLOSED_SANDBOX = {
    "enabled": True,
    "failIfUnavailable": True,
    "autoAllowBashIfSandboxed": False,
    "allowUnsandboxedCommands": False,
    "enableWeakerNestedSandbox": False,
    "enableWeakerNetworkIsolation": False,
    # the host's Claude token rides in claude's environment; no sandboxed command gets it; nor
    # the key of a provider a card's session runs on
    "credentials": {"envVars": [{"name": "CLAUDE_CODE_OAUTH_TOKEN", "mode": "deny"},
                                {"name": "ANTHROPIC_AUTH_TOKEN", "mode": "deny"},
                                {"name": "ANTHROPIC_API_KEY", "mode": "deny"}]},
    "network": {
        "strictAllowlist": True,
        "allowUnixSockets": [],
        "allowAllUnixSockets": False,
        "allowLocalBinding": False,
    },
}

CAPABILITY_FIELDS = frozenset(
    {
        "schema_version",
        "name",
        "module",
        "description",
        "shell",
        "dependencies",
        "conflicts",
        "resources",
        "wrappers",
        "validator",
        "sandbox_ceiling",
        "lifecycle",
        "setup",
    }
)
RESOURCE_FIELDS = frozenset({"name", "access"})
WRAPPER_FIELDS = frozenset(
    {"command", "source", "subcommands", "approval", "run_as", "executor"}
)
CAPABILITY_CEILING_FIELDS = frozenset({"network_domains", "channel_commands"})
LIFECYCLE_FIELDS = frozenset({"install", "check", "disable"})
PROFILE_FIELDS = frozenset(
    {"schema_version", "name", "defaults", "capability_ceiling", "sandbox_profiles"}
)
PROFILE_DEFAULT_FIELDS = frozenset({"role"})
SANDBOX_FIELDS = frozenset(
    {"schema_version", "name", "description", "settings", "ceiling"}
)
SANDBOX_CEILING_FIELDS = frozenset({"network_domains", "channel_commands", "writable"})
RESOURCE_TABLE_FIELDS = frozenset(
    {"schema_version", "layout", "resources", "protected"}
)
LAYOUT_FIELDS = frozenset({"agents", "code"})
PATH_RESOURCE_FIELDS = frozenset({"kind", "path", "secret"})
HOME_RESOURCE_FIELDS = frozenset({"kind", "home", "secret"})
PLANNED_RESOURCE_FIELDS = frozenset({"kind", "planned", "secret"})
_TASK_RE = re.compile(r"[A-Z][0-9]{2}\Z")  # a plan task id such as B02
PROTECTION_FIELDS = frozenset({"kind", "path", "deny"})
_GUARD_RE = re.compile(r"(agents/[a-z][a-z0-9-]*)/[a-z][a-z0-9_]*/guard\.py\Z")
_SEGMENT = re.compile(r"[A-Za-z0-9_.@+-]+\Z")
_PATTERN_SEGMENT = re.compile(r"[A-Za-z0-9_.@+*-]+\Z")

Entry = dict[str, Any]


class RegistryError(ValueError):
    """Registry data that must not be trusted as policy; ``errors`` lists every problem."""

    def __init__(self, errors: str | Iterable[str]) -> None:
        self.errors = (errors,) if isinstance(errors, str) else tuple(errors)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True)
class Resource:
    """One named resource: an absolute canonical ``path``, or paths in the owner's home.

    ``home`` entries are relative to the home of the agent that holds the resource with
    own-file access; ``{agent}`` stands for that agent's name.
    """

    name: str
    kind: str
    secret: bool
    path: str | None
    home: tuple[str, ...]
    # A resource of a later task whose place is not decided yet: it has no path, is
    # never a secret and is reached only through a planned wrapper.
    planned: str | None = None

    def paths(self, home: str, agent: str) -> tuple[str, ...]:
        """Canonical paths; a home resource resolves in the given home only."""
        if self.path is not None:
            return (self.path,)
        return tuple(
            f"{home}/{item.replace(HOME_PLACEHOLDER, agent)}" for item in self.home
        )


@dataclass(frozen=True)
class Protection:
    """A deployment path denied to every agent; ``kind`` is dir, file or pattern."""

    kind: str
    path: str
    deny: tuple[str, ...]


@dataclass(frozen=True)
class ResourceTable:
    """The one table of deployment paths: layout roots, named resources, protections."""

    layout: dict[str, str]
    resources: dict[str, Resource]
    protected: tuple[Protection, ...]


@dataclass(frozen=True)
class Registry:
    """Well-formed registry data loaded from one repository root.

    ``present_files`` holds the referenced repository files that exist as regular files,
    so ``validate_registry`` can check references without touching the filesystem.
    """

    profiles: dict[str, Entry]
    capabilities: dict[str, Entry]
    sandboxes: dict[str, Entry]
    present_files: frozenset[str]
    resources: ResourceTable


def _unique_keys(pairs: list[tuple[str, Any]]) -> Entry:
    result: Entry = {}
    for key, value in pairs:
        if key in result:
            raise RegistryError(f"invalid JSON: duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise RegistryError(f"invalid JSON: non-finite number {value}")


def read_json_file(path: str | os.PathLike[str]) -> Any:
    """Read one bounded, regular, non-symlink UTF-8 JSON file; never writes.

    Duplicate keys and non-finite numbers are refused, because a second key or NaN could
    silently change what a reviewer read in the file.
    """
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise RegistryError("symlink is not allowed")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RegistryError("unreadable file") from exc
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_JSON_BYTES:
            raise RegistryError("not a bounded regular file")
        raw = stream.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise RegistryError("not a bounded regular file")
    try:
        # Repository JSON names users by naming tokens (@base@, @exec@, @agent:<name>@ ...) and spells commands
        # and cards as kd-<word> and KD-<n>: the host's names from here on.
        text = naming.rewrite(naming.render(raw.decode("utf-8")))
    except naming.NamingError as exc:
        raise RegistryError(f"naming token: {exc}") from exc
    except ValueError as exc:
        raise RegistryError("invalid JSON") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_unique_keys,
            parse_constant=_reject_constant,
        )
    except RegistryError:
        raise
    except ValueError as exc:
        raise RegistryError("invalid JSON") from exc


def read_json_object(path: str | os.PathLike[str]) -> Entry:
    """``read_json_file`` for a file whose top level must be a JSON object."""
    value = read_json_file(path)
    if not isinstance(value, dict):
        raise RegistryError("top level must be a JSON object")
    return value


def _canonical(value: Any) -> str:
    # json.dumps keeps true distinct from 1, unlike ==.
    return json.dumps(value, sort_keys=True)


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and 0 < naming.repository_length(value) <= 300  # as written


def _is_choice(value: Any, choices: Collection[str]) -> bool:
    return isinstance(value, str) and value in choices


def _load_directory(
    directory: Path, pattern: re.Pattern[str]
) -> tuple[dict[str, Entry], list[str]]:
    if directory.is_symlink() or not directory.is_dir():
        return {}, [f"{directory.name}: missing or linked registry directory"]
    entries: dict[str, Entry] = {}
    errors: list[str] = []
    for path in sorted(directory.iterdir()):
        label = f"{directory.name}/{path.name}"
        if path.suffix != ".json" or path.is_symlink() or not path.is_file():
            errors.append(f"{label}: only regular .json files are allowed")
            continue
        stem = path.name[: -len(".json")]
        if not pattern.fullmatch(stem):
            errors.append(f"{label}: invalid registry name")
            continue
        try:
            entry = read_json_object(path)
        except RegistryError as exc:
            errors.append(f"{label}: {exc}")
            continue
        if entry.get("name") != stem:
            errors.append(f"{label}: name does not match the file name")
            continue
        entries[stem] = entry
    return entries, errors


def _scan(value: Any, label: str, field: str = "") -> list[str]:
    """Reject wildcards, expansion, control characters and paths outside path fields."""
    if isinstance(value, dict):
        return [e for key, item in value.items() for e in _scan(item, label, key)]
    if isinstance(value, list):
        return [e for item in value for e in _scan(item, label, field)]
    if not isinstance(value, str):
        return []
    errors = []
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        errors.append(f"{label}: control character in {field}")
    if UNSAFE_CHARS & set(value):
        errors.append(f"{label}: wildcard or expansion character in {field}")
    if value.startswith(("/", "~")) or "//" in value:
        errors.append(f"{label}: absolute path in {field}")
    if ".." in value:
        errors.append(f"{label}: path traversal in {field}")
    if "/" in value and field not in PATH_FIELDS:
        errors.append(f"{label}: path outside a path field in {field}")
    return errors


def _fields(entry: Any, expected: frozenset[str], label: str) -> list[str]:
    if not isinstance(entry, dict):
        return [f"{label}: must be a JSON object"]
    if set(entry) != expected:
        return [f"{label}: fields must be exactly {sorted(expected)}"]
    return []


def _schema(entry: Entry, label: str) -> list[str]:
    version = entry["schema_version"]
    if type(version) is not int or version != 1:
        return [f"{label}: unsupported schema_version"]
    return []


def _names(
    value: Any, pattern: re.Pattern[str], label: str, *, ordered: bool = True
) -> list[str]:
    """A list of identifiers: valid, unique and, for sets, sorted."""
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        return [f"{label}: must be a list of strings"]
    errors = []
    if any(not pattern.fullmatch(item) for item in value):
        errors.append(f"{label}: invalid names")
    if len(value) != len(set(value)):
        errors.append(f"{label}: duplicate entries")
    elif ordered and value != sorted(value):
        errors.append(f"{label}: entries must be sorted")
    return errors


def _repo_path(value: Any) -> bool:
    return isinstance(value, str) and REPO_PATH_RE.fullmatch(value) is not None


def _wrapper_shape(label: str, wrapper: Any) -> list[str]:
    errors = _fields(wrapper, WRAPPER_FIELDS, label)
    if errors:
        return errors
    if not isinstance(wrapper["command"], str) or not COMMAND_RE.fullmatch(
        wrapper["command"]
    ):
        errors.append(f"{label}: invalid command")
    if wrapper["source"] != "platform" and not _repo_path(wrapper["source"]):
        errors.append(f"{label}: source must be platform or a repository path")
    errors += _names(
        wrapper["subcommands"], SUBCOMMAND_RE, f"{label} subcommands", ordered=False
    )
    if not _is_choice(wrapper["approval"], frozenset({"allow", "ask"})):
        errors.append(f"{label}: approval must be allow or ask")
    if not _is_choice(wrapper["run_as"], RUN_AS):
        errors.append(f"{label}: run_as must be one of {sorted(RUN_AS)}")
    if wrapper["executor"] is not None and not _repo_path(wrapper["executor"]):
        errors.append(f"{label}: executor must be null or a repository path")
    return errors


def _capability_shape(label: str, entry: Entry) -> list[str]:
    errors = _fields(entry, CAPABILITY_FIELDS, label)
    if errors:
        return errors
    errors += _schema(entry, label)
    if not _is_choice(entry["module"], MODULES):
        errors.append(f"{label}: unknown module")
    if not _is_text(entry["description"]):
        errors.append(f"{label}: description must be short text")
    if not _is_choice(entry["shell"], frozenset({"none", "sandboxed"})):
        errors.append(f"{label}: shell must be none or sandboxed")
    errors += _names(entry["dependencies"], CAPABILITY_RE, f"{label} dependencies")
    errors += _names(entry["conflicts"], CAPABILITY_RE, f"{label} conflicts")
    resources = entry["resources"]
    if not isinstance(resources, list):
        errors.append(f"{label}: resources must be a list")
    else:
        for index, resource in enumerate(resources):
            item = f"{label} resource {index}"
            problems = _fields(resource, RESOURCE_FIELDS, item)
            if not problems and not (
                isinstance(resource["name"], str)
                and NAME_RE.fullmatch(resource["name"])
            ):
                problems.append(f"{item}: invalid name")
            if not problems and not _is_choice(resource["access"], ACCESS_MODES):
                problems.append(f"{item}: unknown access")
            errors += problems
        names = [str(r.get("name")) for r in resources if isinstance(r, dict)]
        if len(names) != len(set(names)):
            errors.append(f"{label}: duplicate resources")
    wrappers = entry["wrappers"]
    # Only a capability that reads its data with the Read tool alone has no tool to run. A capability that would only
    # open a host to a sandboxed shell has none either (git.mirror fetches through its executor, review 04.11.2026).
    direct_only = (
        isinstance(resources, list)
        and bool(resources)
        and all(isinstance(r, dict) and r.get("access") == "read-direct" for r in resources)
    )
    if not isinstance(wrappers, list) or not (wrappers or direct_only):
        errors.append(f"{label}: wrappers must be a non-empty list")
    else:
        for index, wrapper in enumerate(wrappers):
            errors += _wrapper_shape(f"{label} wrapper {index}", wrapper)
    if entry["validator"] is not None and not _repo_path(entry["validator"]):
        errors.append(f"{label}: validator must be null or a repository path")
    setup = entry["setup"]
    if setup is not None and not (isinstance(setup, str) and _SETUP_RE.fullmatch(setup)):
        errors.append(
            f"{label}: setup script must be null or <agents/name|common>/install/<x>_setup.sh"
        )
    ceiling = entry["sandbox_ceiling"]
    problems = _fields(ceiling, CAPABILITY_CEILING_FIELDS, f"{label} sandbox_ceiling")
    if not problems:
        # Unique, kept in token order: the deployment's host values decide how they sort.
        problems += _names(ceiling["network_domains"], DOMAIN_RE, f"{label} network", ordered=False)
        problems += _names(
            ceiling["channel_commands"], COMMAND_RE, f"{label} channel_commands"
        )
    errors += problems
    lifecycle = entry["lifecycle"]
    problems = _fields(lifecycle, LIFECYCLE_FIELDS, f"{label} lifecycle")
    if not problems:
        allowed = {"install": INSTALL_ORDER, "check": INSTALL_ORDER}
        for phase in ("install", "check", "disable"):
            steps = lifecycle[phase]
            vocabulary = allowed.get(phase, DISABLE_ORDER)
            if not isinstance(steps, list) or any(s not in vocabulary for s in steps):
                problems.append(f"{label}: unknown lifecycle step in {phase}")
            elif len(steps) != len(set(steps)):
                problems.append(f"{label}: duplicate lifecycle step in {phase}")
    errors += problems
    return errors


def _profile_shape(label: str, entry: Entry) -> list[str]:
    errors = _fields(entry, PROFILE_FIELDS, label)
    if errors:
        return errors
    errors += _schema(entry, label)
    defaults = entry["defaults"]
    if not isinstance(defaults, dict) or set(defaults) != PROFILE_DEFAULT_FIELDS:
        errors.append(f"{label}: defaults may only hold the role text, never rights")
    elif not _is_text(defaults["role"]):
        errors.append(f"{label}: role must be short text")
    ceiling = entry["capability_ceiling"]
    errors += _names(ceiling, CAPABILITY_RE, f"{label} capability_ceiling")
    if ceiling == []:
        errors.append(f"{label}: capability_ceiling must not be empty")
    errors += _names(entry["sandbox_profiles"], NAME_RE, f"{label} sandbox_profiles")
    return errors


def _sandbox_shape(label: str, entry: Entry) -> list[str]:
    errors = _fields(entry, SANDBOX_FIELDS, label)
    if errors:
        return errors
    errors += _schema(entry, label)
    if not _is_text(entry["description"]):
        errors.append(f"{label}: description must be short text")
    if _canonical(entry["settings"]) != _canonical(FAIL_CLOSED_SANDBOX):
        errors.append(f"{label}: settings must equal the fail-closed sandbox (5.2b)")
    ceiling = entry["ceiling"]
    problems = _fields(ceiling, SANDBOX_CEILING_FIELDS, f"{label} ceiling")
    if not problems:
        problems += _names(ceiling["network_domains"], DOMAIN_RE, f"{label} network", ordered=False)
        problems += _names(
            ceiling["channel_commands"], COMMAND_RE, f"{label} channel_commands"
        )
        if ceiling["writable"] != ["work"]:
            problems.append(f"{label}: writable ceiling must be the working directory")
    errors += problems
    return errors


def board_routes(client=None) -> dict[str, str]:
    """project -> route of the board as the cabinet holds it (its projects table is the only source).

    The installer runs as root on the cabinet's host; a cabinet that does not answer is a ``RegistryError``
    naming it — an install never goes on as if the board had no projects."""
    from kd.cabinet import client as cabinet_client  # lazily: the cabinet's API imports this module's users

    try:
        routes = cabinet_client.routes(client or cabinet_client.Client())
    except cabinet_client.CabinetError as exc:
        raise RegistryError(f"board projects: the cabinet did not answer ({exc}); start digipaddock-cabinet first") from exc
    errors = _route_shape(routes)
    if errors:
        raise RegistryError(errors)
    return routes


def _route_shape(routes: Any) -> list[str]:
    """The cabinet's answer maps board projects to assignees."""
    if not isinstance(routes, dict):
        return ["board projects: routes must map projects to assignees"]
    errors = _scan(routes, "board projects")
    for project, target in sorted(routes.items()):
        if not isinstance(project, str) or not NAME_RE.fullmatch(project):
            errors.append("board projects: invalid project")
        elif not isinstance(target, str) or not (
            target == "@owner"
            or (target.startswith("@") and AGENT_RE.fullmatch(target[1:]))
        ):
            errors.append(f"board projects: route target for {project} must be an assignee")
    return errors


def _segments_ok(segments: list[str], pattern: re.Pattern[str]) -> bool:
    # Only-dot segments ("." and "..") would let a normalized-looking path move elsewhere.
    return all(pattern.fullmatch(s) and not set(s) <= {"."} for s in segments)


def _below_prefix(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 255:
        return False
    prefix = next((p for p in SYSTEM_PREFIXES if value.startswith(p)), None)
    return prefix is not None and len(value) > len(prefix)


def _is_absolute(value: Any) -> bool:
    """A normalized absolute path strictly below a known system prefix, without globs."""
    return _below_prefix(value) and _segments_ok(value[1:].split("/"), _SEGMENT)


def _is_pattern(value: Any) -> bool:
    """A deny-only glob strictly below a known system prefix: * and ** only."""
    return _below_prefix(value) and _segments_ok(value[1:].split("/"), _PATTERN_SEGMENT)


def _is_home_relative(value: Any) -> bool:
    if not isinstance(value, str) or not 0 < len(value) <= 255:
        return False
    plain = value.replace(HOME_PLACEHOLDER, "x")
    return _segments_ok(plain.split("/"), _SEGMENT)


def _resource_shape(name: str, entry: Any) -> tuple[Resource | None, list[str]]:
    label = f"resource {name}"
    if not isinstance(entry, dict):
        return None, [f"{label}: must be a JSON object"]
    if "home" in entry:
        fields = HOME_RESOURCE_FIELDS
    elif "planned" in entry:
        fields = PLANNED_RESOURCE_FIELDS
    else:
        fields = PATH_RESOURCE_FIELDS
    errors = _fields(entry, fields, label)
    if errors:
        return None, errors
    if not _is_choice(entry["kind"], ("dir", "file")):
        errors.append(f"{label}: kind must be dir or file")
    if not isinstance(entry["secret"], bool):
        errors.append(f"{label}: secret must be true or false")
    if "planned" in entry:
        if not isinstance(entry["planned"], str) or not _TASK_RE.fullmatch(
            entry["planned"]
        ):
            errors.append(f"{label}: planned must name a plan task such as B02")
        if entry["secret"] is not False:
            errors.append(f"{label}: a planned resource has no path to deny a secret")
        if errors:
            return None, errors
        return Resource(name, entry["kind"], False, None, (), entry["planned"]), []
    if "home" in entry:
        home = entry["home"]
        if (
            not isinstance(home, list)
            or not home
            or not all(_is_home_relative(item) for item in home)
        ):
            errors.append(f"{label}: home must list relative paths in the agent's home")
        elif home != sorted(set(home)):
            errors.append(f"{label}: home paths must be sorted and unique")
        elif not all(item.startswith(OWN_FILE_DIR + "/") for item in home):
            # Claude login, bot channels and memory keys stay closed even to their owner.
            errors.append(f"{label}: own files must lie in the agent's {OWN_FILE_DIR}/")
        if entry["secret"] is not True or entry["kind"] != "file":
            errors.append(f"{label}: a home resource must be a secret file")
        if errors:
            return None, errors
        return Resource(name, "file", True, None, tuple(home)), []
    if not _is_absolute(entry["path"]):
        errors.append(
            f"{label}: path must be a normalized absolute path below {SYSTEM_PREFIXES}"
        )
    if errors:
        return None, errors
    return Resource(name, entry["kind"], entry["secret"], entry["path"], ()), []


def _protection_shape(label: str, entry: Any) -> tuple[Protection | None, list[str]]:
    errors = _fields(entry, PROTECTION_FIELDS, label)
    if errors:
        return None, errors
    kind, path, deny = entry["kind"], entry["path"], entry["deny"]
    if not _is_choice(kind, ("dir", "file", "pattern")):
        errors.append(f"{label}: kind must be dir, file or pattern")
    elif not (_is_pattern(path) if kind == "pattern" else _is_absolute(path)):
        errors.append(f"{label}: invalid {kind} path below {SYSTEM_PREFIXES}")
    if (
        not isinstance(deny, list)
        or not deny
        or any(not isinstance(item, str) for item in deny)
        or not set(deny) <= PROTECTION_DENIALS
        or deny != sorted(set(deny))
    ):
        errors.append(f"{label}: deny must be a sorted subset of edit and read")
    if errors:
        return None, errors
    return Protection(kind, path, tuple(deny)), []


# A deployment's own secret paths on the agents' host (its applications' credential files) are not in the public
# table: host/protected.json of the overlay, next to host.json, adds them — it can only add a denial, never drop one
# (review 04.11.2026). No file — none; a file that does not parse fails the registry, as a broken table does.
PROTECTED_NAME = "protected.json"


def _deployment_protected() -> tuple[list[Protection], list[str]]:
    path = Path(naming.host_json_path()).parent / PROTECTED_NAME
    label = f"host/{PROTECTED_NAME}"
    if not path.is_file():
        return [], []
    try:
        data = read_json_object(path)
    except RegistryError as exc:
        return [], [f"{label}: {exc}"]
    if (
        set(data) != {"schema_version", "protected"}
        or data["schema_version"] != 1
        or not isinstance(data["protected"], list)
    ):
        return [], [f"{label}: expected {{schema_version: 1, protected: [...]}}"]
    found, errors = [], []
    for index, entry in enumerate(data["protected"]):
        protection, problems = _protection_shape(f"{label} {index}", entry)
        errors += problems
        if protection is not None:
            found.append(protection)
    return found, errors


def _load_resources(root: Path) -> tuple[ResourceTable | None, list[str]]:
    label = "agent-resources.json"
    try:
        table = read_json_object(root / RESOURCES)
    except RegistryError as exc:
        return None, [f"{label}: {exc}"]
    errors = _fields(table, RESOURCE_TABLE_FIELDS, label)
    if errors:
        return None, errors
    errors += _schema(table, label)
    layout = table["layout"]
    problems = _fields(layout, LAYOUT_FIELDS, f"{label} layout")
    if not problems:
        problems = [
            f"{label} layout: {key} must be a normalized absolute path"
            f" below {SYSTEM_PREFIXES}"
            for key, value in sorted(layout.items())
            if not _is_absolute(value)
        ]
    errors += problems
    resources: dict[str, Resource] = {}
    entries = table["resources"]
    if not isinstance(entries, dict):
        errors.append(f"{label}: resources must be a JSON object")
        entries = {}
    for name, entry in sorted(entries.items()):
        if not NAME_RE.fullmatch(name):
            errors.append(f"{label}: invalid resource name {name!r}")
            continue
        resource, problems = _resource_shape(name, entry)
        errors += problems
        if resource is not None:
            resources[name] = resource
    protected: list[Protection] = []
    entries = table["protected"]
    if not isinstance(entries, list):
        errors.append(f"{label}: protected must be a list")
        entries = []
    for index, entry in enumerate(entries):
        protection, problems = _protection_shape(f"protected {index}", entry)
        errors += problems
        if protection is not None:
            protected.append(protection)
    extra, problems = _deployment_protected()
    errors += problems
    protected += extra
    # Unique, kept in path order: a deployment value (@host:docs_dir@) decides where its entry sorts.
    paths = [item.path for item in protected]
    if len(paths) != len(set(paths)):
        errors.append(f"{label}: protected paths must be unique")
    protected.sort(key=lambda item: item.path)
    if errors:
        return None, errors
    return ResourceTable(dict(layout), resources, tuple(protected)), []


def is_within(path: str, root: str) -> bool:
    """True when the plain absolute ``path`` is ``root`` or lies below it."""
    return path == root or path.startswith(root + "/")


def guard_entry(validator: str) -> str | None:
    """The PreToolUse hook script that runs a validator module, or None if it is not one.

    Today each guard is ``agents/<agent>/<package>/guard.py`` run by that agent's
    ``bin/deny_guard.py``; the hook command is compiled from this entry script.
    """
    match = _GUARD_RE.fullmatch(validator)
    return None if match is None else f"{match.group(1)}/bin/deny_guard.py"


def referenced_files(capabilities: dict[str, Entry]) -> set[str]:
    """Repository files that capability entries name: wrappers, executors, validators,
    the hook scripts that run the validators and module setup scripts."""
    paths = set()
    for entry in capabilities.values():
        if entry["setup"] is not None:
            paths.add(entry["setup"])
        if entry["validator"] is not None:
            paths.add(entry["validator"])
            entry_script = guard_entry(entry["validator"])
            if entry_script is not None:
                paths.add(entry_script)
        for wrapper in entry["wrappers"]:
            if wrapper["source"] != "platform":
                paths.add(wrapper["source"])
            if wrapper["executor"] is not None:
                paths.add(wrapper["executor"])
    return paths


def _is_repo_file(root: Path, relative: str) -> bool:
    path = root / relative
    return not path.is_symlink() and path.is_file()


def _entry_shapes(profiles: Any, sandboxes: Any, capabilities: Any) -> list[str]:
    """The fields of every entry and the strings in it (wildcards, paths, control
    characters); load_registry and validate_registry share this one check."""
    errors = []
    for entries, kind, shape in (
        (profiles, "profile", _profile_shape),
        (sandboxes, "sandbox profile", _sandbox_shape),
        (capabilities, "capability", _capability_shape),
    ):
        if not isinstance(entries, dict):
            errors.append(f"{kind} entries must be a mapping")
            continue
        for name, entry in sorted(entries.items()):
            label = f"{kind} {name}"
            if not isinstance(entry, dict) or entry.get("name") != name:
                errors.append(f"{label}: name does not match its key")
                continue
            errors += _scan(entry, label) + shape(label, entry)
    return errors


def load_registry(root: str | os.PathLike[str]) -> Registry:
    """Read and shape-check every registry file under a repository root; never writes."""
    root = Path(root)
    profiles, errors = _load_directory(root / PROFILES, NAME_RE)
    sandboxes, more = _load_directory(root / SANDBOXES, NAME_RE)
    errors += more
    capabilities, more = _load_directory(root / CAPABILITIES, CAPABILITY_RE)
    errors += more
    errors += _entry_shapes(profiles, sandboxes, capabilities)
    resources, more = _load_resources(root)
    errors += more
    if errors or resources is None:
        raise RegistryError(errors)
    present = frozenset(
        path for path in referenced_files(capabilities) if _is_repo_file(root, path)
    )
    return Registry(profiles, capabilities, sandboxes, present, resources)


def _bin_file(item: str, path: str, present: frozenset[str]) -> list[str]:
    if not BIN_PATH_RE.fullmatch(path):
        return [f"{item}: {path} is not under a bin directory"]
    if path in PLANNED_FILES or path in present:
        return []
    return [f"{item}: {path} does not exist"]


def _pinned_wrapper_rules(item: str, wrapper: Entry) -> list[str]:
    command, source, executor = (
        wrapper["command"],
        wrapper["source"],
        wrapper["executor"],
    )
    errors = []
    pinned = PINNED_WRAPPERS.get(source)
    if pinned is not None:
        canonical, approval = pinned
        if command != canonical:
            errors.append(f"{item}: {source} must be published as {canonical}")
        if wrapper["approval"] != approval:
            errors.append(f"{item}: approval of {canonical} must be {approval}")
    for other_source, (canonical, _approval) in PINNED_WRAPPERS.items():
        if command == canonical and source != other_source:
            errors.append(
                f"{item}: the name {canonical} is reserved for {other_source}"
            )
    allowed = EXECUTOR_SOURCES.get(executor)
    if allowed is not None and source not in allowed:
        errors.append(f"{item}: {executor} is reached only from {sorted(allowed)}")
    return errors


def _wrapper_rules(
    name: str, module: str, wrapper: Entry, present: frozenset[str]
) -> list[str]:
    item = f"capability {name} wrapper {wrapper['command']}"
    run_as, executor = wrapper["run_as"], wrapper["executor"]
    errors = []
    if wrapper["source"] == "platform":
        if wrapper["command"] not in PLATFORM_COMMANDS:
            errors.append(
                f"{item}: platform command {wrapper['command']} is not one of"
                f" {sorted(PLATFORM_COMMANDS)}"
            )
        if not wrapper["subcommands"]:
            errors.append(f"{item}: a platform tool needs a finite subcommand list")
        if run_as != "self":
            errors.append(f"{item}: a platform tool runs as the agent itself")
    else:
        errors += _bin_file(item, wrapper["source"], present)
    errors += _pinned_wrapper_rules(item, wrapper)
    if (run_as == "self") != (executor is None):
        errors.append(
            f"{item}: an executor is required exactly when run_as is not self"
        )
    if executor is not None:
        errors += _bin_file(item, executor, present)
    if run_as in EXECUTOR_MODULES and EXECUTOR_MODULES[run_as] != module:
        errors.append(
            f"{item}: service user {run_as} belongs to module {EXECUTOR_MODULES[run_as]}"
        )
    return errors


def _lifecycle_rules(label: str, entry: Entry) -> list[str]:
    wrappers = entry["wrappers"]
    needed = {
        "executor-user": any(w["run_as"] in EXECUTOR_MODULES for w in wrappers),
        "setup": entry["setup"] is not None,
        "wrappers": any(w["source"] != "platform" for w in wrappers),
        "read-grants": any(r["access"] == "read-grant" for r in entry["resources"]),
        "write-grants": any(r["access"] == "write-grant" for r in entry["resources"]),
        "channels": bool(entry["sandbox_ceiling"]["channel_commands"]),
    }
    install = [step for step in INSTALL_ORDER if needed[step]]
    disable = [step for step in DISABLE_ORDER if needed[step]]
    lifecycle = entry["lifecycle"]
    errors = []
    if lifecycle["install"] != install:
        errors.append(f"{label}: lifecycle install must be {install} (grants last)")
    if lifecycle["check"] != install:
        errors.append(f"{label}: lifecycle check must verify every install step")
    if lifecycle["disable"] != disable:
        errors.append(f"{label}: lifecycle disable must be {disable} (grants first)")
    return errors


def _capability_rules(name: str, entry: Entry, registry: Registry) -> list[str]:
    label = f"capability {name}"
    capabilities = registry.capabilities
    errors = []
    if name not in CAPABILITY_MODULES:
        errors.append(f"{label}: not in the spec 5.2 capability table")
    elif entry["module"] != CAPABILITY_MODULES[name]:
        errors.append(f"{label}: module must be {CAPABILITY_MODULES[name]}")
    dependencies, conflicts = set(entry["dependencies"]), set(entry["conflicts"])
    for reference in sorted((dependencies | conflicts) - set(capabilities)):
        errors.append(f"{label}: unknown capability {reference}")
    if name in dependencies | conflicts:
        errors.append(f"{label}: refers to itself")
    if dependencies & conflicts:
        errors.append(f"{label}: both a dependency and a conflict")
    table = registry.resources.resources
    for resource in entry["resources"]:
        known, access = table.get(resource["name"]), resource["access"]
        if known is None:
            errors.append(f"{label}: unknown resource {resource['name']}")
        elif known.home and access != "own-file":
            errors.append(f"{label}: home resource {known.name} must be own-file")
        elif access == "own-file" and not known.home:
            errors.append(f"{label}: own-file needs a home resource, not {known.name}")
        elif known.secret and access not in ("own-file", "via-wrapper"):
            errors.append(f"{label}: secret resource {known.name} needs a wrapper")
        elif known.planned is not None and (
            access != "via-wrapper"
            or not any(
                {w["source"], w["executor"]} & PLANNED_FILES for w in entry["wrappers"]
            )
        ):
            errors.append(
                f"{label}: planned resource {known.name} is reached only through"
                " a planned wrapper"
            )
        if access == "write-grant":
            # no capability grants a file write; the board's writers are executors.
            errors.append(f"{label}: write grant on {resource['name']} is not allowed")
    repo_commands, executor_commands, platform_commands = set(), set(), set()
    for wrapper in entry["wrappers"]:
        errors += _wrapper_rules(name, entry["module"], wrapper, registry.present_files)
        if wrapper["source"] == "platform":
            platform_commands.add(wrapper["command"])
        else:
            repo_commands.add(wrapper["command"])
        if wrapper["run_as"] != "self":
            executor_commands.add(wrapper["command"])
    ceiling = entry["sandbox_ceiling"]
    channels = set(ceiling["channel_commands"])
    own = PLATFORM_CHANNELS.get(name, frozenset()) & platform_commands
    if not channels <= repo_commands | own:
        errors.append(f"{label}: only its own repository wrappers may be channel commands")
    # An executor is reached only through its channel; inside the sandbox the call goes
    # through the self channel.
    if not executor_commands <= channels:
        errors.append(f"{label}: a wrapper with an executor must be a channel command")
    if ceiling["network_domains"] and entry["shell"] != "sandboxed":
        errors.append(f"{label}: sandbox network is only for a sandboxed shell")
    validator = entry["validator"]
    if validator is not None and validator not in registry.present_files:
        errors.append(f"{label}: validator {validator} does not exist")
    elif validator is not None:
        hook = guard_entry(validator)
        if hook is None:
            errors.append(f"{label}: validator {validator} is not a guard module")
        elif hook not in registry.present_files:
            errors.append(f"{label}: validator hook {hook} does not exist")
    setup = entry["setup"]
    if setup is not None and setup not in registry.present_files:
        errors.append(f"{label}: setup script {setup} does not exist")
    # A shell inside the sandbox is still logically dangerous in work/ (spec 5.2b): it
    # always comes with a semantic guard.
    if entry["shell"] == "sandboxed" and validator is None:
        errors.append(f"{label}: a sandboxed shell needs a validator")
    errors += _lifecycle_rules(label, entry)
    return errors


def _registry_rules(registry: Registry) -> list[str]:
    capabilities = registry.capabilities
    errors = []
    for name in sorted(set(CAPABILITY_MODULES) - set(capabilities)):
        errors.append(f"missing spec capability {name}")
    for name, entry in sorted(capabilities.items()):
        for other in entry["conflicts"]:
            if other in capabilities and name not in capabilities[other]["conflicts"]:
                errors.append(
                    f"capability {name}: conflict with {other} must be symmetric"
                )
    state: dict[str, str] = {}

    def visit(name: str) -> None:
        if state.get(name) == "active":
            errors.append(f"dependency cycle through {name}")
        if name in state:
            return
        state[name] = "active"
        for dependency in capabilities[name]["dependencies"]:
            if dependency in capabilities:
                visit(dependency)
        state[name] = "done"

    for name in sorted(capabilities):
        visit(name)
    owners: dict[str, list[tuple[str, Entry]]] = {}
    for name, entry in capabilities.items():
        for wrapper in entry["wrappers"]:
            earlier = owners.setdefault(wrapper["command"], [])
            if earlier and not all(
                _split_wrapper((name, wrapper), other) for other in earlier
            ):
                errors.append(f"wrapper command {wrapper['command']} is declared twice")
            earlier.append((name, wrapper))
    used = {r["name"] for entry in capabilities.values() for r in entry["resources"]}
    for name in sorted(set(registry.resources.resources) - used):
        errors.append(f"resource {name} is not used by any capability")
    return errors + _exposure_rules(registry)


def _split_wrapper(first: tuple[str, Entry], second: tuple[str, Entry]) -> bool:
    """Two declarations of one command by disjoint forms.

    A repository wrapper has one owner for its lifecycle and channel, so it is split only
    inside its capability with the same source, executor and run_as: its forms then differ
    in approval. A platform tool may be split between capabilities (board.read, board.write).
    """
    (name, wrapper), (other_name, other) = first, second
    forms, other_forms = set(wrapper["subcommands"]), set(other["subcommands"])
    if forms & other_forms:
        return False
    if wrapper["source"] == "platform" == other["source"]:
        return True
    return (
        name == other_name
        and bool(forms)
        and bool(other_forms)
        and all(wrapper[key] == other[key] for key in ("source", "executor", "run_as"))
    )


def _exposure_rules(registry: Registry) -> list[str]:
    """A resource an agent reads directly never contains a secret or a protected path.

    A granted resource also never lies inside such a path: the compiled read allow would
    contradict the deny. Only read-public data may sit inside a protected, non-secret
    directory; the compiler then opens exactly that file in the holder's sandbox. Inside
    a secret directory nothing is public. read-direct data is checked like a grant, but
    a protection that denies only edits (project documentation) is no obstacle to reading it.
    """
    table = registry.resources
    hidden = {
        resource.path: f"secret {name}"
        for name, resource in table.resources.items()
        if resource.secret and resource.path is not None
    }
    read_hidden = dict(hidden)
    for item in table.protected:
        if item.kind != "pattern":
            hidden.setdefault(item.path, "protected path")
            if "read" in item.deny:
                read_hidden.setdefault(item.path, "protected path")
    readable = {
        (r["name"], r["access"])
        for entry in registry.capabilities.values()
        for r in entry["resources"]
        if r["access"] in DIRECT_READS | {"read-public"}
    }
    errors = []
    for name, access in sorted(readable):
        resource = table.resources.get(name)
        if resource is None or resource.path is None:
            continue
        # A home holds its user's own secrets (own-file); no other agent reads into it.
        if is_within(resource.path, HOMES):
            errors.append(f"resource {name}: {access} reaches into a user home")
        targets = read_hidden if access == "read-direct" else hidden
        for target, what in sorted(targets.items()):
            if is_within(target, resource.path):
                errors.append(f"resource {name}: {access} would expose {what} {target}")
            elif is_within(resource.path, target) and (
                access in DIRECT_READS or what.startswith("secret")
            ):
                errors.append(f"resource {name}: {access} lies inside {what} {target}")
    return errors


def capability_union(
    capabilities: dict[str, Entry], names: Iterable[str], field: str
) -> set[str]:
    """Union of one sandbox ceiling field over the named capabilities."""
    return {
        item for name in names for item in capabilities[name]["sandbox_ceiling"][field]
    }


def _profile_rules(name: str, entry: Entry, registry: Registry) -> list[str]:
    capabilities, sandboxes = registry.capabilities, registry.sandboxes
    label = f"profile {name}"
    ceiling = set(entry["capability_ceiling"])
    errors = [
        f"{label}: unknown capability {c}" for c in sorted(ceiling - set(capabilities))
    ]
    errors += [
        f"{label}: unknown sandbox profile {s}"
        for s in entry["sandbox_profiles"]
        if s not in sandboxes
    ]
    if errors:
        return errors
    for capability in sorted(ceiling):
        if set(capabilities[capability]["dependencies"]) - ceiling:
            errors.append(f"{label}: ceiling is not closed under dependencies")
        if set(capabilities[capability]["conflicts"]) & ceiling:
            errors.append(f"{label}: ceiling holds conflicting capabilities")
    for capability, owner in sorted(PROFILE_ONLY.items()):
        if (capability in ceiling) != (name == owner):
            errors.append(
                f"{label}: {capability} belongs to the {owner} profile only,"
                " which always offers it"
            )
    shells = [c for c in ceiling if capabilities[c]["shell"] == "sandboxed"]
    if shells and not entry["sandbox_profiles"]:
        errors.append(f"{label}: a sandboxed shell needs a sandbox profile")
    domains = capability_union(capabilities, ceiling, "network_domains")
    channels = capability_union(capabilities, ceiling, "channel_commands")
    for sandbox in entry["sandbox_profiles"]:
        limit = sandboxes[sandbox]["ceiling"]
        if not domains <= set(limit["network_domains"]) or not channels <= set(
            limit["channel_commands"]
        ):
            errors.append(
                f"{label}: capability ceiling exceeds sandbox profile {sandbox}"
            )
    return errors


def _sandbox_rules(registry: Registry) -> list[str]:
    errors = []
    for name, entry in sorted(registry.sandboxes.items()):
        users = [p for p in registry.profiles.values() if name in p["sandbox_profiles"]]
        if not users:
            errors.append(f"sandbox profile {name}: no profile uses it")
            continue
        names = {c for profile in users for c in profile["capability_ceiling"]}
        domains = capability_union(registry.capabilities, names, "network_domains")
        channels = capability_union(registry.capabilities, names, "channel_commands")
        ceiling = entry["ceiling"]
        if sorted(ceiling["network_domains"]) != sorted(domains) or ceiling[
            "channel_commands"
        ] != sorted(channels):
            errors.append(
                f"sandbox profile {name}: ceiling must equal what its capabilities need"
            )
    return errors


def _resource_rules(table: ResourceTable) -> list[str]:
    """The path invariants again, on the loaded objects: a Registry built or changed in
    memory must not skip what load_registry checks in the file."""
    errors = [
        f"layout {key} is not a normalized absolute path"
        for key, value in sorted(table.layout.items())
        if not _is_absolute(value)
    ]
    for name, resource in sorted(table.resources.items()):
        if resource.planned is not None:
            if resource.path is not None or resource.home or resource.secret:
                errors.append(f"resource {name}: a planned resource has no path")
        elif resource.home:
            if resource.path is not None or not resource.secret:
                errors.append(f"resource {name}: a home resource is a secret file")
            if not all(
                _is_home_relative(item) and item.startswith(OWN_FILE_DIR + "/")
                for item in resource.home
            ):
                errors.append(
                    f"resource {name}: own files must lie in the agent's {OWN_FILE_DIR}/"
                )
        elif resource.path is not None and not _is_absolute(resource.path):
            errors.append(f"resource {name}: path is not a normalized absolute path")
    for item in table.protected:
        valid = _is_pattern if item.kind == "pattern" else _is_absolute
        if not valid(item.path) or not set(item.deny) <= PROTECTION_DENIALS:
            errors.append(f"protected {item.path}: invalid protection")
    return errors


def validate_registry(registry: Registry) -> None:
    """Check references and security rules of a loaded registry; pure, raises on any error.

    The shape of every entry is checked again first: a Registry built or changed in
    memory must not skip what load_registry checks in the files.
    """
    errors = _entry_shapes(registry.profiles, registry.sandboxes, registry.capabilities)
    errors += _resource_rules(registry.resources)
    if errors:
        raise RegistryError(errors)  # the rules below assume well-formed entries
    for name, entry in sorted(registry.capabilities.items()):
        errors += _capability_rules(name, entry, registry)
    errors += _registry_rules(registry)
    for name, entry in sorted(registry.profiles.items()):
        errors += _profile_rules(name, entry, registry)
    if errors:
        raise RegistryError(errors)  # the sandbox rules assume consistent profiles
    errors = _sandbox_rules(registry)
    if errors:
        raise RegistryError(errors)


def validate_routes(routes: dict[str, str], agents: Collection[str]) -> None:
    """Every route target (``board_routes``) is the owner or one of the given agents."""
    errors = [
        f"board projects: project {project} routes to unknown agent {target}"
        for project, target in sorted(routes.items())
        if target != "@owner" and target[1:] not in agents
    ]
    if errors:
        raise RegistryError(errors)


def validate_selection(
    registry: Registry,
    profile: str,
    capabilities: Collection[str],
    sandbox_profile: str,
) -> None:
    """One agent's choice must stay inside its profile's capability and sandbox ceilings."""
    entry = registry.profiles.get(profile)
    if entry is None:
        raise RegistryError(f"unknown profile {profile}")
    selected = set(capabilities)
    errors = [
        f"unknown capability {c}" for c in sorted(selected - set(registry.capabilities))
    ]
    if errors:
        raise RegistryError(errors)
    errors = [
        f"capability {c} is outside the {profile} profile ceiling"
        for c in sorted(selected - set(entry["capability_ceiling"]))
    ]
    if sandbox_profile not in entry["sandbox_profiles"]:
        errors.append(
            f"sandbox profile {sandbox_profile} is not allowed for profile {profile}"
        )
    for name in sorted(selected):
        capability = registry.capabilities[name]
        if not set(capability["dependencies"]) <= selected:
            errors.append(f"missing capability dependency of {name}")
        if set(capability["conflicts"]) & selected:
            errors.append(f"incompatible capabilities with {name}")
    if errors:
        raise RegistryError(errors)
