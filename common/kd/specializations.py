"""Specialization registry (BOSS spec 5.8.4-5.8.5): one root-owned, versioned set of
instruction texts per agent type. A persistent agent reads a text as a playbook, a worker as
a prompt (kd.worker_packs); a card label ``spec:<name>`` selects one.

An entry adds knowledge, a result format and the *limits* of a worker, never rights: no
field names a tool, a path or a permission, and a text cannot widen the agent's policy.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Collection, Iterable, Mapping, Optional

from kd import naming
from kd.manifest import ManifestError, load_manifest

REPO_ROOT = Path("/opt/digipaddock")  # the deployed, root-owned release tree
SPECS_ROOT = Path(__file__).resolve().parents[1] / "specializations"
INDEX = "specializations.json"
SCHEMA_VERSION = 1
TYPES = ("analyst", "coordinator", "developer", "sysadmin")  # profile names, spec 5.8.2
WRITES = ("none", "temp", "repo")
NETWORKS = ("none",)  # a worker has no network until a channel for workers exists
SECTIONS = ("Purpose", "Inputs", "Procedure", "Output", "Limits")
LABEL_PREFIX = "spec:"
SKILL_PREFIX = "skill:"
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9]*(?:\.[a-z][a-z0-9]*)+$")
MAX_TEXT = 16 * 1024
TOP_FIELDS = frozenset({"schema_version", "max_skills", "specializations", "skills"})
SPEC_FIELDS = frozenset({"type", "version", "file", "default", "skills", "result", "requires", "limits"})
LIMIT_FIELDS = frozenset({"write", "capabilities", "network"})
SKILL_FIELDS = frozenset({"version", "file", "conflicts"})
_TITLE = re.compile(r"# (Specialization|Skill): ([a-z][a-z0-9-]{0,31}) \(v([1-9][0-9]*)\)")
_H2 = re.compile(r"^## (.*)$", re.MULTILINE)
_H1 = re.compile(r"^#(?!#)", re.MULTILINE)
_FENCE = re.compile(r"^```.*?^```[ \t]*$", re.MULTILINE | re.DOTALL)  # an example in a fence is prose


class SpecError(ValueError):
    pass


@dataclass(frozen=True)
class Limits:
    write: str
    capabilities: tuple[str, ...]
    network: str


@dataclass(frozen=True)
class Specialization:
    name: str
    type: str
    version: int
    file: str
    text: str
    sha256: str
    default: bool
    skills: tuple[str, ...]
    result: tuple[str, ...]
    requires: tuple[str, ...]
    limits: Limits


@dataclass(frozen=True)
class Skill:
    name: str
    version: int
    file: str
    text: str
    sha256: str
    conflicts: tuple[str, ...]


@dataclass(frozen=True)
class Registry:
    max_skills: int
    specs: Mapping[str, Specialization]  # read-only views: a loaded registry is never edited
    skills: Mapping[str, Skill]


def _fields(entry: Any, allowed: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise SpecError(f"{label}: not an object")
    extra = sorted(set(entry) - allowed)
    if extra:
        raise SpecError(f"{label}: unknown field {', '.join(extra)}")
    missing = sorted(allowed - set(entry))
    if missing:
        raise SpecError(f"{label}: missing {', '.join(missing)}")
    return entry


def _names(value: Any, pattern: re.Pattern, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) and pattern.fullmatch(v) for v in value):
        raise SpecError(f"{label}: bad name list")
    if len(set(value)) != len(value):
        raise SpecError(f"{label}: duplicate name")
    return tuple(value)


def _version(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise SpecError(f"{label}: bad version")
    return value


def _check_owner(path: Path, lstat: Any, *, directory: bool) -> None:
    st = lstat(path)
    kind_ok = stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode)
    if not kind_ok:
        raise SpecError(f"{path}: not a {'directory' if directory else 'regular file'}")
    if st.st_uid != 0:
        raise SpecError(f"{path}: not owned by root")
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise SpecError(f"{path}: group or world writable")


def _read(root: Path, file: str, label: str, owner: bool, lstat: Any) -> tuple[str, str]:
    path = root / file
    if owner:
        _check_owner(path.parent, lstat, directory=True)
        _check_owner(path, lstat, directory=False)
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise SpecError(f"{label}: {file} is not a regular file")
        data = path.read_bytes()
    except OSError as exc:
        raise SpecError(f"{label}: {file} unreadable ({exc.strerror})") from exc
    if len(data) > MAX_TEXT:
        raise SpecError(f"{label}: {file} is larger than {MAX_TEXT} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SpecError(f"{label}: {file} is not UTF-8") from exc
    # the text with the host's command and card names; installed, hashed and compared as such
    text = naming.rewrite(text)
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def _check_text(kind: str, name: str, version: int, text: str, result: tuple[str, ...] = ()) -> None:
    label = f"{kind.lower()} {name}"
    match = _TITLE.fullmatch(text.split("\n", 1)[0])
    if not match or (match.group(1), match.group(2), int(match.group(3))) != (kind, name, version):
        raise SpecError(f"{label}: first line must be '# {kind}: {name} (v{version})'")
    prose = _FENCE.sub("", text)
    if len(_H1.findall(prose)) != 1:
        raise SpecError(f"{label}: one title only")
    if kind == "Specialization":
        headings = list(_H2.finditer(prose))
        if tuple(h.group(1) for h in headings) != SECTIONS:
            raise SpecError(f"{label}: sections must be {', '.join(SECTIONS)}, in this order")
        ends = [h.start() for h in headings[1:]] + [len(prose)]
        output = prose[headings[SECTIONS.index("Output")].end():ends[SECTIONS.index("Output")]]
        missing = [key for key in result if f"`{key}`" not in output]
        if missing:
            raise SpecError(f"{label}: Output does not describe {', '.join(missing)}")


def _limits(value: Any, label: str) -> Limits:
    entry = _fields(value, LIMIT_FIELDS, f"{label} limits")
    if entry["write"] not in WRITES:
        raise SpecError(f"{label}: limits.write must be one of {', '.join(WRITES)}")
    if entry["network"] not in NETWORKS:
        raise SpecError(f"{label}: limits.network must be one of {', '.join(NETWORKS)}")
    capabilities = _names(entry["capabilities"], CAPABILITY_RE, f"{label} limits.capabilities")
    return Limits(entry["write"], capabilities, entry["network"])


def _result(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(k, str) and KEY_RE.fullmatch(k) for k in value):
        raise SpecError(f"{label}: result must be a non-empty list of keys")
    if "status" in value or len(set(value)) != len(value):
        raise SpecError(f"{label}: result keys must be unique and not 'status' (the frame adds it)")
    return tuple(value)


def load_registry(
    root: str | os.PathLike[str] = SPECS_ROOT, *, require_root_owner: bool = True, lstat: Any = os.lstat
) -> Registry:
    """Read and check the index and every text it names; never writes."""
    root = Path(root)
    if require_root_owner:
        _check_owner(root, lstat, directory=True)
        _check_owner(root / INDEX, lstat, directory=False)
    try:
        index = json.loads((root / INDEX).read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise SpecError(f"{INDEX} unreadable: {exc}") from exc
    index = _fields(index, TOP_FIELDS, INDEX)
    if index["schema_version"] != SCHEMA_VERSION:
        raise SpecError(f"{INDEX}: schema_version must be {SCHEMA_VERSION}")
    limit = index["max_skills"]
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 4:
        raise SpecError(f"{INDEX}: max_skills must be 1..4")
    for group in ("specializations", "skills"):
        if not isinstance(index[group], dict) or not all(NAME_RE.fullmatch(n) for n in index[group]):
            raise SpecError(f"{INDEX}: {group} name must match {NAME_RE.pattern}")
    skills: dict[str, Skill] = {}
    for name, raw in sorted(index["skills"].items()):
        label = f"skill {name}"
        entry = _fields(raw, SKILL_FIELDS, label)
        version = _version(entry["version"], label)
        if entry["file"] != f"skills/{name}.v{version}.md":
            raise SpecError(f"{label}: file must be skills/{name}.v{version}.md")
        text, digest = _read(root, entry["file"], label, require_root_owner, lstat)
        _check_text("Skill", name, version, text)
        skills[name] = Skill(name, version, entry["file"], text, digest, _names(entry["conflicts"], NAME_RE, label))
    specs: dict[str, Specialization] = {}
    for name, raw in sorted(index["specializations"].items()):
        label = f"specialization {name}"
        entry = _fields(raw, SPEC_FIELDS, label)
        type_ = entry["type"]
        if type_ not in TYPES:
            raise SpecError(f"{label}: type must be one of {', '.join(TYPES)}")
        version = _version(entry["version"], label)
        if entry["file"] != f"{type_}/{name}.v{version}.md":
            raise SpecError(f"{label}: file must be {type_}/{name}.v{version}.md")
        if type(entry["default"]) is not bool:
            raise SpecError(f"{label}: default must be true or false")
        result = _result(entry["result"], label)
        requires = _names(entry["requires"], CAPABILITY_RE, f"{label} requires")
        limits = _limits(entry["limits"], label)
        text, digest = _read(root, entry["file"], label, require_root_owner, lstat)
        _check_text("Specialization", name, version, text, result)
        specs[name] = Specialization(
            name, type_, version, entry["file"], text, digest, entry["default"],
            _names(entry["skills"], NAME_RE, f"{label} skills"), result, requires, limits,
        )
    for item in (*specs.values(), *skills.values()):
        refs = item.skills if isinstance(item, Specialization) else item.conflicts
        unknown = sorted(set(refs) - set(skills))
        if unknown:
            raise SpecError(f"{item.name}: unknown skill {', '.join(unknown)}")
    for type_ in TYPES:
        defaults = [s.name for s in specs.values() if s.type == type_ and s.default]
        if len(defaults) > 1:
            raise SpecError(f"{type_}: more than one default specialization ({', '.join(defaults)})")
    return Registry(limit, MappingProxyType(specs), MappingProxyType(skills))


def for_type(registry: Registry, type_: str) -> dict[str, Specialization]:
    return {name: s for name, s in registry.specs.items() if s.type == type_}


def installable(registry: Registry, type_: str, capabilities: Collection[str]) -> list[Specialization]:
    """The specializations of ``type_`` an agent holding ``capabilities`` gets (spec 5.8.4)."""
    held = set(capabilities)
    return [s for name, s in sorted(for_type(registry, type_).items()) if set(s.requires) <= held]


def select(
    registry: Registry, type_: str, capabilities: Collection[str], labels: Iterable[str]
) -> Optional[Specialization]:
    """The specialization a card's labels choose for an agent: its one ``spec:`` label, or
    the type's default; None when there is neither. Several labels, or a name that is not
    installed for this agent (unknown, another type's, a capability it lacks), raise
    SpecError: the agent never guesses (spec 5.8.5)."""
    names = [l[len(LABEL_PREFIX):] for l in labels if isinstance(l, str) and l.startswith(LABEL_PREFIX)]
    if len(names) > 1:
        raise SpecError(f"several spec: labels on one card ({', '.join(sorted(names))})")
    allowed = {s.name: s for s in installable(registry, type_, capabilities)}
    if not names:
        return next((s for s in allowed.values() if s.default), None)
    if names[0] not in allowed:
        raise SpecError(f"spec:{names[0]} is not a specialization of this agent")
    return allowed[names[0]]


def selected_skills(registry: Registry, spec: Optional[Specialization], names: Iterable[str]) -> tuple[Skill, ...]:
    """One shared selection rule for persistent sessions and worker prompt packs."""
    chosen = list(names)
    for name in chosen:
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise SpecError(f"bad skill name {name!r}")
        if name not in registry.skills:
            raise SpecError(f"unknown skill {name}")
    if len(set(chosen)) != len(chosen):
        raise SpecError("duplicate skill")
    if len(chosen) > registry.max_skills:
        raise SpecError(f"at most {registry.max_skills} skills")
    for name in chosen:
        if spec is None or name not in spec.skills:
            raise SpecError(f"skill {name} is not compatible with {spec.name if spec else 'no specialization'}")
        clash = sorted(set(registry.skills[name].conflicts) & set(chosen))
        if clash:
            raise SpecError(f"skill {name} conflicts with {', '.join(clash)}")
    return tuple(registry.skills[n] for n in sorted(chosen))


def skills_from_labels(registry: Registry, spec: Optional[Specialization], labels: Iterable[str]) -> tuple[Skill, ...]:
    return selected_skills(registry, spec, [v[len(SKILL_PREFIX):] for v in labels if v.startswith(SKILL_PREFIX)])


def installable_skills(registry: Registry, type_: str, capabilities: Collection[str]) -> tuple[Skill, ...]:
    names = {n for s in installable(registry, type_, capabilities) for n in s.skills}
    return tuple(registry.skills[n] for n in sorted(names))


def note(spec: Specialization, skills: Iterable[Skill] = ()) -> str:
    """The line a persistent agent's card note records when it takes the card (spec 5.8.4)."""
    return "\n".join([f"spec {spec.name} v{spec.version} sha256={spec.sha256}",
                      *(f"skill {s.name} v{s.version} sha256={s.sha256}" for s in skills)])


def provenance(spec: Specialization, skills: Iterable[Skill] = ()) -> dict[str, Any]:
    """What a job result records: the exact texts the work ran with."""
    return {
        "spec": spec.name,
        "version": spec.version,
        "sha256": spec.sha256,
        "skills": {s.name: {"version": s.version, "sha256": s.sha256} for s in skills},
    }


def agent_specs(registry: Registry, repo_root: str | os.PathLike[str] = REPO_ROOT) -> dict[str, frozenset[str]]:
    """``@<agent>`` -> names of its installed specializations, from ``agents/*/agent.json``."""
    out: dict[str, frozenset[str]] = {}
    agents_dir = Path(repo_root) / "agents"
    if agents_dir.is_symlink() or not agents_dir.is_dir():
        raise SpecError("agents directory is missing or linked")
    for agent_dir in sorted(agents_dir.iterdir()):
        if agent_dir.is_symlink():
            raise SpecError(f"linked agent directory {agent_dir.name}")
        path = agent_dir / "agent.json"
        if not agent_dir.is_dir() or not (path.exists() or path.is_symlink()):
            continue
        try:
            manifest = load_manifest(path)
        except ManifestError as exc:
            raise SpecError(f"{agent_dir.name}: {exc}") from exc
        out["@" + manifest.name] = frozenset(
            s.name for s in installable(registry, manifest.profile, manifest.capabilities)
        )
    return out
