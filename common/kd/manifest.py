"""Strict, read-only validation of repository agent manifests."""

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from kd import naming
from kd.registry import (
    RegistryError,
    load_registry,
    read_json_file,
    validate_registry,
    validate_selection,
)


_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_RESOURCE = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
SCHEMA_VERSION = 2
# Executor and gate accounts created by the installers (<base>-exec and <base>-forge on the
# stage, agent-ro on target servers). An agent running as one of them would hold that
# executor's keys, tokens and journals (spec 5.2); kd.naming reserves their agent names.
SERVICE_USERS = naming.service_users() | {naming.TARGET_USER}
_CAPABILITY = re.compile(r"[a-z][a-z0-9]*(?:\.[a-z][a-z0-9]*)+\Z")
_MODEL = re.compile(
    r"(?:sonnet|opus|haiku|fable|claude-(?:sonnet|opus|haiku|fable)-[a-z0-9-]{3,48})\Z"
)
# Claude Code's reasoning effort levels (``--effort``); absent = the platform default.
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# an agent thread with people besides its chat whose session may run on a model of its own (``threads``:
# ``{"monitor": {"model": "<model>"}}``); absent — every session runs on the agent's ``model``.
THREAD_SPACES = ("monitor",)
COORDINATOR_READS = frozenset({"monitor.read"})  # the monitor's reads the coordinator may hold
_TOP_FIELDS = frozenset(
    {
        "schema_version",
        "name",
        "model",
        "effort",
        "threads",
        "profile",
        "capabilities",
        "runtime",
    }
)
_REQUIRED_FIELDS = frozenset(
    {"schema_version", "name", "profile", "capabilities", "runtime"}
)


class ManifestError(ValueError):
    """An agent manifest, or its cross-references, that must not become policy."""


@dataclass(frozen=True)
class AgentManifest:
    name: str
    user: str
    model: str | None
    profile: str
    capabilities: tuple[str, ...]
    sandbox_profile: str
    working_directory: str
    effort: str | None = None
    # (space, model) of the threads whose sessions run on another model, sorted; () — none
    thread_models: tuple[tuple[str, str], ...] = ()


def _read_json(path: Path) -> Any:
    # The manifest is repository data, but it must not select a symlink target.
    try:
        return read_json_file(path)
    except RegistryError as exc:
        raise ManifestError(f"{path.name}: {exc}") from exc


def _object(
    value: Any, required: frozenset[str], allowed: frozenset[str], label: str
) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or not required <= value.keys()
        or not value.keys() <= allowed
    ):
        raise ManifestError(f"invalid {label} fields")
    return value


def _identifier(value: Any, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ManifestError(f"invalid {label}")
    return value


def _agent_user(name: str) -> str:
    try:
        return naming.agent_user(name)
    except naming.NamingError as exc:
        raise ManifestError("agent name cannot own a user") from exc


def check_identity(name: Any, user: Any, model: Any) -> None:
    """Refuse an agent name, Linux user or model that no manifest may carry.

    load_manifest, the policy compiler and the policy checker share this check: an
    ``AgentManifest`` or a policy built by hand must not put an unchecked name into a
    path or a sudoers line, run as any user but ``<prefix>-<name>`` (so never the base
    user, another agent's or a service account) or pass an option as the model.
    """
    name = _identifier(name, _NAME, "agent name")
    expected = _agent_user(name)
    if user != expected:
        raise ManifestError(f"agent user must be {expected}")
    if model is not None:
        check_model(model)


def check_model(model: Any) -> str:
    """A model is a known alias or Claude identifier, never an option or free text (also the
    worker sessions' model of worker-slots.json, C05)."""
    return _identifier(model, _MODEL, "model")


def check_effort(effort: Any) -> str:
    """An effort is one of Claude Code's levels, never an option or free text."""
    if not isinstance(effort, str) or effort not in EFFORTS:
        raise ManifestError("invalid effort")
    return effort


def check_thread_models(value: Any) -> dict[str, str]:
    """``{space: model}`` of the threads that run on a model of their own: a known space, a known model;
    anything else — an empty map, another key, an option as the model — is refused."""
    if not isinstance(value, dict) or not value or not set(value) <= set(THREAD_SPACES):
        raise ManifestError(f"threads: a map of {', '.join(THREAD_SPACES)} to their model")
    out = {}
    for space, model in value.items():
        try:
            out[space] = check_model(model)
        except ManifestError:
            raise ManifestError(f"threads: invalid model of {space}") from None
    return out


def load_manifest(path: str | Path) -> AgentManifest:
    """Validate one agent.json without consulting runtime files or registries."""
    path = Path(path)
    if path.name != "agent.json":
        raise ManifestError("manifest filename must be agent.json")
    data = _object(_read_json(path), _REQUIRED_FIELDS, _TOP_FIELDS, "manifest")
    if (
        type(data["schema_version"]) is not int
        or data["schema_version"] != SCHEMA_VERSION
    ):
        raise ManifestError("unsupported manifest schema")
    name, model = data["name"], data.get("model")
    if model == "":
        model = None
    # the user is never written down; it derives from the name and the host prefix.
    user = _agent_user(_identifier(name, _NAME, "agent name"))
    check_identity(name, user, model)
    if name != path.parent.name:
        raise ManifestError("manifest name does not match directory")
    profile = _identifier(data["profile"], _RESOURCE, "profile")
    capabilities = data["capabilities"]
    if not isinstance(capabilities, list) or any(
        not isinstance(item, str) or not _CAPABILITY.fullmatch(item)
        for item in capabilities
    ):
        raise ManifestError("invalid capabilities")
    if len(capabilities) != len(set(capabilities)):
        raise ManifestError("duplicate capability")
    runtime = _object(
        data["runtime"],
        frozenset({"sandbox_profile", "working_directory"}),
        frozenset({"sandbox_profile", "working_directory"}),
        "runtime",
    )
    sandbox = _identifier(runtime["sandbox_profile"], _RESOURCE, "sandbox profile")
    if runtime["working_directory"] != "work":
        raise ManifestError("working directory must be work")
    effort = check_effort(data["effort"]) if "effort" in data else None
    thread_models: tuple[tuple[str, str], ...] = ()
    if "threads" in data:  # {"monitor": {"model": "…"}}: the policy keeps {"monitor": "…"}
        threads = data["threads"]
        if not isinstance(threads, dict) or any(
            not isinstance(e, dict) or set(e) != {"model"} for e in threads.values()
        ):
            raise ManifestError('threads: each thread is {"model": "<model>"}')
        found = check_thread_models({space: entry["model"] for space, entry in threads.items()})
        thread_models = tuple(sorted(found.items()))
    return AgentManifest(
        name, user, model, profile, tuple(capabilities), sandbox, "work", effort, thread_models
    )


def _check_coordinator(manifest: AgentManifest) -> None:
    # Independent of the registry's declared conflicts: BOSS never gets executor rights,
    # even if a registry entry forgot to list the conflict. monitor.read is the one
    # monitor capability it may hold: reads of the cabinet's incidents, nothing on a server.
    if manifest.profile == "coordinator":
        if not {"board.read", "board.coordinate"} <= set(manifest.capabilities):
            raise ManifestError("coordinator requires board read and coordinate")
        if any(
            cap.startswith(("git.", "forge.", "servers."))
            or (cap.startswith("monitor.") and cap not in COORDINATOR_READS)
            or cap == "board.write"
            for cap in manifest.capabilities
        ):
            raise ManifestError("coordinator capability conflict")
    elif "board.coordinate" in manifest.capabilities:
        raise ManifestError("board.coordinate requires coordinator profile")


def validate_all(repo_root: str | Path) -> dict[str, AgentManifest]:
    """Validate repo manifests and cross-references; zero manifests is a valid migration state.

    The registry is validated by ``kd.registry`` (the same code the compiler and installer
    use); each manifest's profile must admit its capabilities and sandbox profile.
    """
    root = Path(repo_root)
    agents_dir = root / "agents"
    if agents_dir.is_symlink() or not agents_dir.is_dir():
        raise ManifestError("agents directory is missing or linked")
    manifests = {}
    for agent_dir in sorted(agents_dir.iterdir()):
        if agent_dir.is_symlink():
            raise ManifestError("linked agent directory")
        if not agent_dir.is_dir():
            continue
        path = agent_dir / "agent.json"
        if path.exists() or path.is_symlink():
            manifest = load_manifest(path)
            manifests[manifest.name] = manifest
    if not manifests:
        return {}

    users = {}
    coordinators = []
    for manifest in manifests.values():
        if manifest.user in users:
            raise ManifestError(f"duplicate agent user: {manifest.user}")
        users[manifest.user] = manifest.name
        if manifest.profile == "coordinator":
            coordinators.append(manifest.name)
        _check_coordinator(manifest)
    if len(coordinators) > 1:
        raise ManifestError("multiple coordinators")

    try:
        registry = load_registry(root)
        validate_registry(registry)
        for manifest in manifests.values():
            validate_selection(
                registry,
                manifest.profile,
                manifest.capabilities,
                manifest.sandbox_profile,
            )
    except RegistryError as exc:
        raise ManifestError(str(exc)) from exc
    return manifests
