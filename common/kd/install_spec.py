"""The installation an overlay declares: host/install.json and host/projects.json.

install.json — what bootstrap.sh installs on the host:
    {"schema_version": 1, "naming": {"prefix", "commands", "cards"}, "roles": {<role>: {"slots": n}},
     "components": [...], "pipeline": "observe" | "enforce", "hindsight": bool, "behind_nat": bool}
A role is an agent of the code tree (agents/<role>/agent.json); a component is one of COMPONENTS.
projects.json — the board's projects and the passports of their repositories:
    {"schema_version": 1, "projects": {<id>: {"name", "route", "done_by", "memory_bank"[, "repo": {...}]}}}
route is @owner or a role other than the coordinator, done_by "" (the owner's acceptance) or a role, repo an entry of
/etc/digipaddock/repos.json exactly as add_repo.sh writes it (dev_agent.repos checks it).
"""

import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Dict, NamedTuple, Optional, Tuple

from kd import memory_bank, naming

TREE = Path(__file__).resolve().parents[2]
COMPONENTS = ("review", "repo-attach", "transcribe", "e2e-runtime", "devcheck", "mirror", "worker-slots", "egress")
PIPELINE = ("observe", "enforce")
COORDINATOR = "boss"
_INSTALL_KEYS = {"schema_version", "naming", "roles", "components", "pipeline", "hindsight", "behind_nat"}
_PROJECT_KEYS = {"name", "route", "done_by", "memory_bank"}
PROJECT_ID = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
NAME_MAX = 60


class InstallSpecError(ValueError):
    pass


class Install(NamedTuple):
    naming: dict
    roles: Dict[str, int]
    components: Tuple[str, ...]
    pipeline: str
    hindsight: bool
    behind_nat: bool


class Project(NamedTuple):
    id: str
    name: str
    route: str
    done_by: str
    memory_bank: str
    repo: Optional[dict]


def _read(path, keys_hint):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InstallSpecError("{}: unreadable ({})".format(path, exc.__class__.__name__))
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise InstallSpecError("{}: expected {{schema_version: 1, {}}}".format(path, keys_hint))
    return data


def known_roles(tree=TREE) -> set:
    """The agents of a code tree: agents/<role>/agent.json."""
    return {p.parent.name for p in Path(tree).glob("agents/*/agent.json")}


def load_install(path, *, known_roles=None) -> Install:
    """install.json checked; ``known_roles`` (the code tree's agents) — a role outside them is an error."""
    data = _read(path, "naming, roles, components, pipeline, hindsight, behind_nat")

    def bad(field, what):
        return InstallSpecError("{}: {}: {}".format(path, field, what))

    if set(data) != _INSTALL_KEYS:
        raise InstallSpecError("{}: keys are exactly {}".format(path, ", ".join(sorted(_INSTALL_KEYS))))
    names = data["naming"]
    if not isinstance(names, dict) or set(names) != {"prefix", "commands", "cards"}:
        raise bad("naming", "{prefix, commands, cards}")
    for key, shape in (("prefix", naming.PREFIX_RE), ("commands", naming.COMMANDS_RE), ("cards", naming.CARDS_RE)):
        if not isinstance(names[key], str) or not shape.match(names[key]):
            raise bad("naming." + key, "must match " + shape.pattern)
    if names["prefix"] in (names["commands"], naming.CANON_COMMANDS):  # naming.load(), the same rule
        raise bad("naming.prefix", "not a command prefix: commands and users would share one name")
    if names["commands"] in naming.BUSY_COMMANDS:
        raise bad("naming.commands", "not a prefix of tools the host already has")
    roles = data["roles"]
    if not isinstance(roles, dict) or not roles:
        raise bad("roles", "a non-empty object role -> {slots}")
    slots = {}
    for role, spec in roles.items():
        if known_roles is not None and role not in known_roles:
            raise bad("roles." + str(role), "not an agent of the code tree ({})".format(", ".join(sorted(known_roles))))
        n = spec.get("slots") if isinstance(spec, dict) and set(spec) == {"slots"} else None
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 8:
            raise bad("roles." + str(role), "{slots: 1..8}")
        slots[role] = n
    parts = data["components"]
    if not isinstance(parts, list) or not all(p in COMPONENTS for p in parts) or len(set(parts)) != len(parts):
        raise bad("components", "distinct names of " + ", ".join(COMPONENTS))
    if data["pipeline"] not in PIPELINE:
        raise bad("pipeline", " or ".join(PIPELINE))
    for key in ("hindsight", "behind_nat"):
        if not isinstance(data[key], bool):
            raise bad(key, "true or false")
    return Install(dict(names), slots, tuple(parts), data["pipeline"], data["hindsight"], data["behind_nat"])


def _check_repos(path, entries):
    """The passports as dev_agent.repos.load reads /etc/digipaddock/repos.json (one rule, add_repo.sh's)."""
    dev = str(TREE / "agents" / "dev")
    if dev not in sys.path:
        sys.path.insert(0, dev)
    from dev_agent import repos  # noqa: PLC0415

    with tempfile.TemporaryDirectory(prefix="kd-projects.") as tmp:
        target = Path(tmp) / "repos.json"
        target.write_text(json.dumps(entries), encoding="utf-8")
        try:
            repos.load(str(target))
        except repos.RepoError as exc:
            raise InstallSpecError("{}: {}".format(path, exc))


def load_projects(path, *, roles=None) -> Dict[str, Project]:
    """projects.json checked; ``roles`` (install.json's) — a route or done_by outside them is an error."""
    data = _read(path, "projects")
    if set(data) != {"schema_version", "projects"} or not isinstance(data["projects"], dict) or not data["projects"]:
        raise InstallSpecError("{}: expected {{schema_version: 1, projects: {{<id>: ...}}}}".format(path))
    out = {}
    for pid, item in data["projects"].items():
        def bad(what, pid=pid):
            return InstallSpecError("{}: {}: {}".format(path, pid, what))

        if not isinstance(pid, str) or not PROJECT_ID.match(pid):
            raise bad("id — 1 to 32 latin letters, digits or dashes, a letter first")
        if not isinstance(item, dict) or not _PROJECT_KEYS <= set(item) <= _PROJECT_KEYS | {"repo"}:
            raise bad("keys are name, route, done_by, memory_bank[, repo]")
        name = item["name"]
        if not isinstance(name, str) or not name.strip() or len(name) > NAME_MAX or any(ord(c) < 32 for c in name):
            raise bad("name — text of 1 to {} characters".format(NAME_MAX))
        route = item["route"]
        role = route[1:] if isinstance(route, str) and route.startswith("@") else None
        if route != "@owner" and (role is None or role == COORDINATOR or (roles is not None and role not in roles)):
            raise bad("route — @owner or a role of the host other than @" + COORDINATOR)
        done_by = item["done_by"]
        if done_by != "" and (not isinstance(done_by, str) or (roles is not None and done_by not in roles)):
            raise bad("done_by — '' (the owner's acceptance) or a role of the host")
        bank = item["memory_bank"]
        if bank != "" and not (isinstance(bank, str) and memory_bank.BANK.fullmatch(bank)):
            raise bad("memory_bank — '' or a Hindsight bank id")
        repo = item.get("repo")
        if repo is not None and not isinstance(repo, dict):
            raise bad("repo — an entry of repos.json")
        out[pid] = Project(pid, name.strip(), route, done_by, bank, repo)
    entries = repos_json(out)
    if entries:
        _check_repos(path, entries)
    return out


def repos_json(projects) -> dict:
    """/etc/digipaddock/repos.json of these projects: the passports, keyed by project."""
    return {p.id: p.repo for p in projects.values() if p.repo is not None}


def release_project(projects) -> Optional[str]:
    """The project whose code kd-release deploys (the release's role code)."""
    for p in projects.values():
        if p.repo and p.repo.get("release") and p.repo.get("role", "code") == "code":
            return p.id
    return None
