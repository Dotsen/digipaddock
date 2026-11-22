"""Ordinary operations of an approved admin task: a finite, typed catalog.

An operation is a verb from the catalog plus its full parameters, never a shell fragment. The
catalog (``agents/sysadmin/operations.json``, versioned with the code) says which verbs are on and,
per server, the package tool, whether packages may be installed and the exact units that count as
ordinary. A server missing from the catalog has no ordinary operations; production stays out until
the owner adds it. Protected units (ssh, network, VPN, databases, the agents' own infrastructure …)
are refused when the catalog is loaded, so a mistaken entry cannot reach a run.

A ``script`` step carries the exact text that runs on its server: the owner reads and signs
it, the target verifies the signature and runs exactly that text. It needs no catalog entry, so
production can carry it while its typed operations stay off. The old free ``shell`` step (a
server, no command; the sysadmin chose the commands during the grant) is gone: it is refused with a
hint, and a catalog that still lists the verb loads without it.

A proposal is the agent's untrusted plan for one card: the intent and the basis (what the agent
saw) as text for the owner, and the ordered operations. Schema 2 makes
every step explain itself to the owner: ``what`` (one line), ``effect``, ``risk`` (level and why)
and ``rollback``. Its canonical JSON hash is what the owner approves.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import shlex
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, FrozenSet, Iterable, List, Optional, Tuple

from kd import exec_policy, naming
from kd.admin_task import ALIAS, TASK_ID, canonical
from kd.exec_policy import PROTECTED  # noqa: F401 — the protected units, also critical to stop

CATALOG_PATH = Path(__file__).resolve().parents[2] / "agents" / "sysadmin" / "operations.json"
INVENTORY_PATH = CATALOG_PATH.with_name("inventory.json")  # the sysadmin's inventory, same format as ssh-ro reads
ENABLED_PATH = Path("/etc/digipaddock/servers.enabled")  # servers with the gate (deploy_target.sh)
VERBS = ("package.refresh", "service.restart", "service.start", "service.reload", "package.install", "script",
         "app.tool", "app.flag", "package.upgrade", "cron.line", "file.drop", "gate.script")
LEGACY_VERBS = ("shell",)  # listed by catalogs written before the change; dropped when the catalog loads
PACKAGE_TOOLS = ("apt", "dnf")
MAX_OPERATIONS = 30
MAX_SERVERS = 8
MAX_PACKAGES = 10
MAX_TEXT = 2000
SCHEMA_VERSION = 2
RISKS = ("low", "medium", "high")
MAX_WHAT = 200
MAX_EXPLAIN = 500  # effect, rollback and risk.why
MAX_SCRIPT = 16 * 1024  # characters of a script step: the owner reads all of it
SHELL_GONE = "shell steps are gone (KD-9238): put the exact commands into a script step (verb script, key script)"
PACKED_HINT = "a script must stay readable to the owner: no base64, packed or encoded payloads and no decoders or " \
    "unpackers (base64 -d, zcat, gunzip, xz -d …, even in a comment); write a file with a heredoc " \
    "(cat > path <<'EOF' … EOF)"
CRLF_HINT = "script has Windows line endings (CRLF, U+000D): bash on the server would read every line with a " \
    "trailing \\r and a heredoc would write it into the file; save the plan with LF line endings"
# What the owner could not read (kd.exec_policy.PACKED: the target's gate refuses it too).
_PACKED = exec_policy.PACKED
SCHEMA_HINT = "schema_version 2: add basis and what/effect/risk/rollback to every step"
UNIT = re.compile(r"[A-Za-z0-9][A-Za-z0-9@._-]{0,120}\.service")
PACKAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+._-]{0,99}")
VERSION = re.compile(r"[0-9][A-Za-z0-9.+~:_-]{0,99}")
CRITICAL_PATTERN = re.compile(r"[A-Za-z0-9*][A-Za-z0-9@._*-]{0,120}")
# an app — one environment of a project on its server (the catalog's ``apps``)
APP_NAME = re.compile(r"[a-z][a-z0-9-]{0,39}")
APP_USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
_SEGMENT = r"[A-Za-z0-9_][A-Za-z0-9._-]*"
APP_DIR = re.compile(rf"(?:/{_SEGMENT})+")
# An entry is the application's own tree and nothing beyond it (entries are written in
# a pull request to the overlay, so their shape is a boundary). The interpreter is the tree's own, by a relative
# path, or the system's python3 by its bare name — never another program by its path; a tool is a python script
# of the tree; the tree lives under the roots applications live in and is never the platform's own.
APP_PYTHON = re.compile(rf"python3(?:\.[0-9]{{1,2}})?|\./{_SEGMENT}(?:/{_SEGMENT})*|{_SEGMENT}(?:/{_SEGMENT})+")
APP_TOOL = re.compile(rf"(?:\./)?{_SEGMENT}(?:/{_SEGMENT})*\.py")
APP_ROOTS = ("/var/www/", "/opt/", "/srv/")
# a test environment's directories the agents read whole (its journals), relative to its tree; the
# server's installer opens them to the read-only gate's account, whatever the application's own file modes
APP_READ = re.compile(rf"{_SEGMENT}(?:/{_SEGMENT})*")
MAX_READS = 10
PLATFORM_SEGMENT = "digipaddock"  # /opt/digipaddock, /srv/digipaddock-agents, …: the product's own paths
APP_FILE = re.compile(_SEGMENT)
APP_SECTION = re.compile(r"[a-z][a-z0-9_]{0,40}")
APP_ARG = re.compile(r"[A-Za-z0-9_.:=@/,+-]{1,200}")  # one word of a tool's command line, nothing a shell reads
APP_SWITCH = re.compile(r"[a-z][a-z0-9_]{0,60}_enabled")
# a PHP application — the system's php by its bare name and the tree's own ``spark`` with the commands the
# entry lists; and an env file (``.env``) whose switches are the entry's keys, each with the values it may take:
# plain words, so a step can write no address, token or password.
APP_PHP = re.compile(r"php(?:[0-9]\.[0-9])?")
APP_SPARK = re.compile(r"[a-z][a-z0-9:_-]{0,60}")
SPARK = "spark"
APP_ENV_FILE = re.compile(rf"\.?{_SEGMENT}")
APP_ENV_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_.]{0,80}")
APP_ENV_VALUE = re.compile(r"[A-Za-z0-9_.-]{1,40}")
MAX_ENV_KEYS, MAX_ENV_VALUES = 40, 20
MAX_ARGS = 40
UPGRADE_UNIT = ["systemd-run", "--unit=kd-package-upgrade", "--collect"]  # package.upgrade runs there
# agents/sysadmin/gate/agent_typed_op.py on the target — cron.line, file.drop and gate.script run it
TYPED_HELPER = "/usr/local/libexec/agent-typed-op"
TYPED_VERBS = ("cron.line", "file.drop", "gate.script")
ACTIONS = ("set", "remove")
# agents/sysadmin/gate/agent_app_flag.py on the target; outside /usr/local/bin/agent-*, which the exec policy
# refuses a plan step even to name (rule 1)
FLAG_HELPER = "/usr/local/libexec/agent-app-flag"
APP_VERBS = ("app.tool", "app.flag")
APP_CHANGED = "{app}: the catalog entry changed since this plan was written; propose the plan again"
# Characters a browser may hide or reorder (bidi overrides, zero-width, line separators) or that have no meaning:
# the owner must see exactly the text that is stored and run.
_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cn", "Cs", "Zl", "Zp"})


def hidden_char(text: str, allowed: str = "") -> Optional[str]:
    """The first character the owner's page could not show faithfully, as U+XXXX; None when there is none."""
    for ch in text:
        if ch not in allowed and unicodedata.category(ch) in _HIDDEN_CATEGORIES:
            return f"U+{ord(ch):04X}"
    return None


EXPLAINED = frozenset({"what", "effect", "risk", "rollback"})
_KEYS = {
    "package.refresh": frozenset({"server", "verb"}) | EXPLAINED,
    "package.upgrade": frozenset({"server", "verb"}) | EXPLAINED,
    "service.restart": frozenset({"server", "verb", "unit"}) | EXPLAINED,
    "service.start": frozenset({"server", "verb", "unit"}) | EXPLAINED,
    "service.reload": frozenset({"server", "verb", "unit"}) | EXPLAINED,
    "package.install": frozenset({"server", "verb", "packages"}) | EXPLAINED,
    "script": frozenset({"server", "verb", "script"}) | EXPLAINED,
    # ``argv`` is the resolved command a normalized plan carries: the owner signs it, the target checks it
    "app.tool": frozenset({"server", "verb", "app", "tool", "args", "argv"}) | EXPLAINED,
    "app.flag": frozenset({"server", "verb", "app", "section", "key", "value", "argv"}) | EXPLAINED,
    "cron.line": frozenset({"server", "verb", "mark", "action", "line"}) | EXPLAINED,
    "file.drop": frozenset({"server", "verb", "dir", "name", "mark", "action", "text"}) | EXPLAINED,
    "gate.script": frozenset({"server", "verb", "value"}) | EXPLAINED,
}
_PROPOSAL_KEYS = frozenset({"schema_version", "task_id", "intent", "basis", "operations"})


class CatalogError(ValueError):
    """The operations catalog is malformed or lists a protected unit."""


class ProposalError(ValueError):
    """The proposal or one of its operations is not in the catalog; the text says why."""


@dataclass(frozen=True)
class ServerEntry:
    packages: str
    install: bool
    units: FrozenSet[str]


def _platform_account(user: str) -> bool:
    """An account of the factory itself — the base user, an agent's or a service's (``<prefix>-…``), the read
    gateway's on a target: an application never runs as one of them."""
    return user == naming.base_user() or user.startswith(naming.PREFIX + "-") or user == naming.TARGET_USER


def _names(value: object, pattern: "re.Pattern") -> bool:
    return isinstance(value, list) and all(isinstance(v, str) and pattern.fullmatch(v) for v in value)


@dataclass(frozen=True)
class AppEntry:
    """One environment of a project: where its tree lives, who runs its tools, which tools and switches a
    plan step may name. Filled once per environment; the tools' own subcommands are the project's code. the
    tools are python scripts of the tree, commands of the tree's ``spark`` under php, or both; the switches are
    ``…_enabled`` keys of an ini file's sections or the listed keys of an env file with the values each may take.
    ``test`` marks a test environment: its steps — and a reload of the units ``reload`` lists — run without the
    owner's signature on a plan (the test zone); never on the production server. ``read`` names the test
    environment's directories the agents read whole, past the application's own file modes."""
    server: str
    user: str
    dir: str
    python: str = ""
    tools: FrozenSet[str] = frozenset()
    flag_file: str = ""
    flag_sections: FrozenSet[str] = frozenset()
    php: str = ""
    spark: FrozenSet[str] = frozenset()
    flag_format: str = "ini"
    flag_keys: Tuple[Tuple[str, FrozenSet[str]], ...] = ()
    test: bool = False
    reload: FrozenSet[str] = frozenset()
    read: FrozenSet[str] = frozenset()

    def flag_values(self, key: str) -> Optional[FrozenSet[str]]:
        """The values an env switch may take; None for a key the entry does not list."""
        return dict(self.flag_keys).get(key)

    @classmethod
    def from_dict(cls, name: str, entry: object, servers: Dict[str, "ServerEntry"]) -> "AppEntry":
        if not isinstance(entry, dict) or not {"server", "user", "dir"} <= set(entry) or not set(entry) <= {
                "server", "user", "dir", "python", "tools", "php", "spark", "flags", "test", "reload", "read"}:
            raise CatalogError(f"app {name}: keys must be server, user, dir, python and tools, or php and spark, "
                               "and optional flags, test, reload, read")
        server, user, where = (entry[k] for k in ("server", "user", "dir"))
        if server not in set(servers):
            raise CatalogError(f"app {name}: server {server!r} is not a server of the catalog")
        test, reload = entry.get("test", False), entry.get("reload", [])
        if not isinstance(test, bool):
            raise CatalogError(f"app {name}: test must be true or false")
        if not _names(reload, UNIT):
            raise CatalogError(f"app {name}: reload must be a list of exact unit names")
        if reload and not test:
            raise CatalogError(f"app {name}: reload names the units of a test environment — the entry is not "
                               "marked test")
        read = entry.get("read", [])
        if not _names(read, APP_READ) or len(read) > MAX_READS:
            raise CatalogError(f"app {name}: read must be a list of up to {MAX_READS} directories of the tree, by "
                               "relative paths (logs/api_journal)")
        if read and not test:
            raise CatalogError(f"app {name}: read opens directories of a test environment — the entry is not "
                               "marked test")
        for unit in reload:
            if unit not in servers[server].units:
                raise CatalogError(f"app {name}: reload unit {unit} is not an ordinary unit of {server}")
        if not isinstance(user, str) or not APP_USER.fullmatch(user) or user == "root" or _platform_account(user):
            raise CatalogError(f"app {name}: user must be the application's own account, never root or an "
                               "account of the platform")
        if not isinstance(where, str) or not APP_DIR.fullmatch(where) or not where.startswith(APP_ROOTS) or any(
                segment.startswith(PLATFORM_SEGMENT) for segment in where.split("/")):
            raise CatalogError(f"app {name}: dir must be the application's tree under {', '.join(APP_ROOTS)} "
                               "(plain segments; never the platform's own)")
        pythons, phps = {"python", "tools"} & set(entry), {"php", "spark"} & set(entry)
        if len(pythons) == 1 or len(phps) == 1 or not (pythons or phps):
            raise CatalogError(f"app {name}: an entry names python and tools, or php and spark, or both pairs")
        python, tools, php, spark = "", [], "", []
        if pythons:
            python, tools = entry["python"], entry["tools"]
            if not isinstance(python, str) or not APP_PYTHON.fullmatch(python):
                raise CatalogError(f"app {name}: python must be the tree's interpreter by a relative path "
                                   "(./venv/bin/python) or the bare python3")
            if not _names(tools, APP_TOOL):
                raise CatalogError(f"app {name}: tools must be a list of python scripts of the tree, by relative "
                                   "paths")
        if phps:
            php, spark = entry["php"], entry["spark"]
            if not isinstance(php, str) or not APP_PHP.fullmatch(php):
                raise CatalogError(f"app {name}: php must be the system's php by its bare name (php, php8.3)")
            if not _names(spark, APP_SPARK):
                raise CatalogError(f"app {name}: spark must be a list of the tree's spark commands (migrate, "
                                   "key:generate)")
        base = dict(server=server, user=user, dir=where, python=python, tools=frozenset(tools), php=php,
                    spark=frozenset(spark), test=test, reload=frozenset(reload), read=frozenset(read))
        flags = entry.get("flags")
        if flags is None:
            return cls(**base)
        problem = CatalogError(f"app {name}: flags must be {{file: one name in dir, sections: [lowercase names]}} "
                               "or {file, format: env, keys: {name: [plain values]}}")
        if not isinstance(flags, dict) or not isinstance(flags.get("file"), str):
            raise problem
        if set(flags) == {"file", "sections"}:
            if not APP_FILE.fullmatch(flags["file"]) or not _names(flags["sections"], APP_SECTION):
                raise problem
            return cls(flag_file=flags["file"], flag_sections=frozenset(flags["sections"]), **base)
        keys = flags.get("keys")
        if set(flags) != {"file", "format", "keys"} or flags["format"] != "env" or not APP_ENV_FILE.fullmatch(
                flags["file"]) or not isinstance(keys, dict) or not 0 < len(keys) <= MAX_ENV_KEYS:
            raise problem
        for key, values in keys.items():
            if not isinstance(key, str) or not APP_ENV_KEY.fullmatch(key) or not _names(
                    values, APP_ENV_VALUE) or not 0 < len(values) <= MAX_ENV_VALUES:
                raise problem
        return cls(flag_file=flags["file"], flag_format="env",
                   flag_keys=tuple(sorted((key, frozenset(values)) for key, values in keys.items())), **base)


@dataclass(frozen=True)
class Catalog:
    verbs: FrozenSet[str]
    servers: Dict[str, ServerEntry]
    critical_units: Tuple[str, ...] = ()  # the deployment's site units, fnmatch patterns
    apps: Dict[str, AppEntry] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: object, protected: Iterable[str] = ()) -> "Catalog":
        """``protected`` — the servers no test environment may live on (the deployment's production)."""
        if not isinstance(data, dict) or not {"schema_version", "verbs", "servers"} <= set(data) or not set(data) <= {
            "schema_version", "verbs", "servers", "critical_units", "apps"
        }:
            raise CatalogError("catalog keys must be schema_version, verbs, servers and optional critical_units, apps")
        if data["schema_version"] != 1:
            raise CatalogError(f"unknown catalog schema_version {data['schema_version']!r}")
        verbs = data["verbs"]
        if not isinstance(verbs, list) or not all(v in VERBS or v in LEGACY_VERBS for v in verbs):
            raise CatalogError(f"catalog verbs must be some of {', '.join(VERBS)}")
        verbs = [v for v in verbs if v not in LEGACY_VERBS]
        if not isinstance(data["servers"], dict):
            raise CatalogError("catalog servers must be an object")
        servers = {}
        for name, entry in data["servers"].items():
            if not isinstance(name, str) or not ALIAS.fullmatch(name):
                raise CatalogError(f"invalid server name in the catalog: {name!r}")
            if not isinstance(entry, dict) or set(entry) != {"packages", "install", "units"}:
                raise CatalogError(f"{name}: keys must be packages, install, units")
            if entry["packages"] not in PACKAGE_TOOLS or not isinstance(entry["install"], bool):
                raise CatalogError(f"{name}: packages must be apt or dnf, install true or false")
            units = entry["units"]
            if not isinstance(units, list) or not all(isinstance(u, str) and UNIT.fullmatch(u) for u in units):
                raise CatalogError(f"{name}: units must be exact unit names like nginx.service")
            for unit in units:
                if protected_unit(unit):
                    raise CatalogError(f"{name}: {unit} is protected and cannot be an ordinary unit")
            servers[name] = ServerEntry(entry["packages"], entry["install"], frozenset(units))
        critical = data.get("critical_units", [])
        if not isinstance(critical, list) or not all(isinstance(p, str) and CRITICAL_PATTERN.fullmatch(p) for p in critical):
            raise CatalogError("critical_units must be unit patterns like app-* or *.example.com*")
        raw_apps = data.get("apps", {})
        if not isinstance(raw_apps, dict):
            raise CatalogError("catalog apps must be an object")
        apps = {}
        for name, entry in raw_apps.items():
            if not isinstance(name, str) or not APP_NAME.fullmatch(name):
                raise CatalogError(f"invalid app name in the catalog: {name!r}")
            apps[name] = AppEntry.from_dict(name, entry, servers)
            if apps[name].test and apps[name].server in set(protected):
                raise CatalogError(f"app {name}: {apps[name].server} is the production server — a test "
                                   "environment never lives there")
        return cls(frozenset(verbs), servers, tuple(critical), apps)


@dataclass(frozen=True)
class Operation:
    server: str
    verb: str
    unit: Optional[str] = None
    packages: Tuple[Tuple[str, str], ...] = ()
    script: str = ""  # the exact text of a script step
    app: Optional[str] = None  # the catalog's app of an app.tool / app.flag step
    tool: str = ""
    args: Tuple[str, ...] = ()
    section: str = ""
    key: str = ""
    value: str = ""
    argv: Tuple[str, ...] = ()  # the resolved command of an app step: signed with the plan, checked by the target
    mark: str = ""  # the card a cron line or a dropped file belongs to (its copy dir /root/kd-<n>)
    action: str = ""  # set | remove
    line: str = ""  # the cron line
    folder: str = ""  # file.drop: its directory (the plan's key is "dir")
    name: str = ""
    text: str = ""
    what: str = ""
    effect: str = ""
    risk_level: str = ""
    risk_why: str = ""
    rollback: str = ""

    def as_dict(self) -> dict:
        data = {"server": self.server, "verb": self.verb}
        if self.unit is not None:
            data["unit"] = self.unit
        if self.verb == "package.install":
            data["packages"] = [{"name": n, "version": v} for n, v in self.packages]
        if self.verb == "script":
            data["script"] = self.script
        if self.verb == "app.tool":
            data.update(app=self.app, tool=self.tool, args=list(self.args), argv=list(self.argv))
        if self.verb == "app.flag":
            data.update(app=self.app, section=self.section, key=self.key, value=self.value, argv=list(self.argv))
            if not self.section:  # an env switch: the plan names no section
                del data["section"]
        if self.verb == "cron.line":
            data.update(mark=self.mark, action=self.action)
            if self.action == "set":
                data["line"] = self.line
        if self.verb == "file.drop":
            data.update({"dir": self.folder, "name": self.name, "mark": self.mark, "action": self.action})
            if self.action == "set":
                data["text"] = self.text
        if self.verb == "gate.script":
            data["value"] = self.value
        data.update(what=self.what, effect=self.effect, risk={"level": self.risk_level, "why": self.risk_why},
                    rollback=self.rollback)
        return data


@dataclass(frozen=True)
class Proposal:
    task_id: str
    intent: str
    basis: str
    operations: Tuple[Operation, ...]

    @property
    def servers(self) -> Tuple[str, ...]:
        return tuple(sorted({op.server for op in self.operations}))

    def as_dict(self) -> dict:
        return {"schema_version": SCHEMA_VERSION, "task_id": self.task_id, "intent": self.intent,
                "basis": self.basis, "operations": [op.as_dict() for op in self.operations]}

    @property
    def sha256(self) -> str:
        return hashlib.sha256(canonical(self.as_dict())).hexdigest()


def protected_unit(unit: str) -> bool:
    name = unit.lower()
    return any(fnmatch.fnmatchcase(name, pattern.lower()) for pattern in PROTECTED)


def load_catalog(path: Optional[Path] = None) -> Catalog:
    path = path or CATALOG_PATH  # read at call time: the overlay's file
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CatalogError(f"catalog {path} cannot be read: {exc.__class__.__name__}") from exc
    catalog = Catalog.from_dict(data)
    if any(app.test for app in catalog.apps.values()):  # the zone needs to know where production is
        try:
            production = naming.load_host()["prod_alias"]
        except naming.NamingError as exc:
            raise CatalogError(f"catalog {path}: a test environment needs the production alias: {exc}") from None
        catalog = Catalog.from_dict(data, protected=(production,))
    return catalog


def test_env_of(op: "Operation", catalog: Catalog) -> Optional[str]:
    """The test environment a step belongs to, or None: an app step of an entry marked test, or a reload
    of a unit some test entry of that server lists. Read from the catalog of this moment — a mark taken off since
    the plan was written takes the step out of the zone."""
    if op.verb in APP_VERBS:
        entry = catalog.apps.get(op.app or "")
        return op.app if entry is not None and entry.test and entry.server == op.server else None
    if op.verb == "service.reload":
        for name in sorted(catalog.apps):
            entry = catalog.apps[name]
            if entry.test and entry.server == op.server and op.unit in entry.reload:
                return name
    return None


def test_zone(proposal: "Proposal", catalog: Catalog) -> Tuple[str, ...]:
    """The test environments of a plan whose every step belongs to one; () when any step does not."""
    names = [test_env_of(op, catalog) for op in proposal.operations]
    return tuple(sorted(set(names))) if names and all(names) else ()


def test_envs(catalog: Catalog, server: str) -> dict:
    """The list a server keeps (its exec gate reads it): the test entries of that server, whole."""
    envs = {}
    for name in sorted(catalog.apps):
        entry = catalog.apps[name]
        if not entry.test or entry.server != server:
            continue
        env = {"user": entry.user, "dir": entry.dir}
        if entry.python:
            env.update(python=entry.python, tools=sorted(entry.tools))
        if entry.php:
            env.update(php=entry.php, spark=sorted(entry.spark))
        if entry.flag_file and entry.flag_format == "env":
            env["flags"] = {"file": entry.flag_file, "format": "env",
                            "keys": {key: sorted(values) for key, values in entry.flag_keys}}
        elif entry.flag_file:
            env["flags"] = {"file": entry.flag_file, "format": "ini", "sections": sorted(entry.flag_sections)}
        if entry.reload:
            env["reload"] = sorted(entry.reload)
        if entry.read:
            env["read"] = sorted(entry.read)
        envs[name] = env
    return {"schema_version": 1, "envs": envs}


def load_inventory(path: Optional[Path] = None) -> FrozenSet[str]:
    """The aliases of the sysadmin's inventory."""
    path = path or INVENTORY_PATH
    try:
        servers = json.loads(Path(path).read_text(encoding="utf-8"))["servers"]
        if not isinstance(servers, dict):
            raise TypeError("servers")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"inventory {path} cannot be read: {exc.__class__.__name__}") from exc
    return frozenset(servers)


def load_home(path: Optional[Path] = None) -> str:
    """The alias of the factory's own host (the inventory's ``home``); fails closed."""
    path = path or INVENTORY_PATH
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        home = data["home"]
        if not isinstance(home, str) or home not in data["servers"]:
            raise KeyError("home")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"inventory {path}: no home server ({exc.__class__.__name__})") from exc
    return home


# The factory's own host takes no script step (review 04.11.2026): there the exec user is root and a free-form
# text — the denylist reads only its text — reaches the cage itself (grants, policies, other agents' secrets,
# the exec key). Its catalog operations and an exact critical action, signed per command, stay.
HOME_SCRIPT = "{home} is the factory's own host: a plan has no script step there (a catalog operation, or an exact " \
    "critical action through kd-admin-propose-action)"


def load_enabled(path: Path = ENABLED_PATH) -> FrozenSet[str]:
    """The enabled servers; none when the list does not exist."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return frozenset()
    except (OSError, ValueError) as exc:
        raise ValueError(f"enabled server list {path} cannot be read: {exc.__class__.__name__}") from exc
    return frozenset(line.strip() for line in text.splitlines() if line.strip())


def validate_operation(op: object, catalog: Catalog, inventory: Iterable[str], enabled: Iterable[str]) -> Operation:
    if not isinstance(op, dict):
        raise ProposalError("an operation must be an object")
    verb = op.get("verb")
    if verb in LEGACY_VERBS:
        raise ProposalError(SHELL_GONE)
    if verb not in _KEYS:
        raise ProposalError(f"unknown verb {verb!r}; known: {', '.join(VERBS)}")
    if verb not in catalog.verbs:
        raise ProposalError(f"verb {verb} is not enabled in the catalog")
    if "purpose" in op:
        raise ProposalError(SCHEMA_HINT)
    extra = set(op) - _KEYS[verb]
    if extra:
        raise ProposalError(f"{verb}: unexpected keys {sorted(extra)}")
    server = op.get("server")
    if not isinstance(server, str) or not ALIAS.fullmatch(server):
        raise ProposalError(f"invalid server {server!r}: one alias, no lists, globs or all")
    if server not in set(inventory):
        raise ProposalError(f"server {server} is not in the inventory")
    if server not in set(enabled):
        raise ProposalError(f"server {server} is not enabled (no gate)")
    unit, packages, script, app = None, (), "", {}
    if verb == "script":
        script = _script(op.get("script"))
    elif verb in APP_VERBS:
        app = _app_step(op, verb, server, catalog)
    else:
        entry = catalog.servers.get(server)
        if entry is None:
            raise ProposalError(f"server {server} has no ordinary operations in the catalog")
        if verb in TYPED_VERBS:
            app = _typed_step(op, verb, catalog)
        if verb in ("service.restart", "service.start", "service.reload"):
            unit = op.get("unit")
            if not isinstance(unit, str) or not UNIT.fullmatch(unit) or unit not in entry.units:
                raise ProposalError(f"{server}: unit {unit!r} is not an ordinary unit of this server")
        if verb == "package.install":
            if not entry.install:
                raise ProposalError(f"{server}: package install is not allowed on this server")
            packages = _packages(op.get("packages"), server)
    level, why = risk(op.get("risk"))
    return Operation(server, verb, unit=unit, packages=packages, script=script, **app,
                     what=_text(op, "what", MAX_WHAT, allowed=""),
                     effect=_text(op, "effect", MAX_EXPLAIN), risk_level=level, risk_why=why,
                     rollback=_text(op, "rollback", MAX_EXPLAIN))


def _app_step(op: dict, verb: str, server: str, catalog: Catalog) -> dict:
    """The fields of an app.tool / app.flag step with the argv its catalog entry resolves to. A plan that already
    carries an ``argv`` (the stored, signed one) must carry exactly that one: an entry changed after the signature
    is a refusal, never another command under the old signature."""
    name = op.get("app")
    entry = catalog.apps.get(name) if isinstance(name, str) else None
    if entry is None:
        raise ProposalError(f"app {name!r} is not an environment of the catalog")
    if server != entry.server:
        raise ProposalError(f"{name} lives on {entry.server}, not on {server}")
    prefix = ["runuser", "-u", entry.user, "--", "env", "-C", entry.dir]
    if verb == "app.tool":
        tool, args = op.get("tool"), op.get("args")
        spark = tool == SPARK and bool(entry.php)  # the tree's spark under php
        if not isinstance(tool, str) or not (spark or tool in entry.tools):
            raise ProposalError(f"{name}: tool {tool!r} is not a tool of this environment")
        if not isinstance(args, list) or len(args) > MAX_ARGS or not all(
                isinstance(a, str) and APP_ARG.fullmatch(a) for a in args):
            raise ProposalError(f"{name}: args must be a list of up to {MAX_ARGS} plain words "
                                "(letters, digits and _ . : = @ / , + -)")
        if spark and (not args or args[0] not in entry.spark):
            raise ProposalError(f"{name}: spark runs a command of this environment first "
                                f"({', '.join(sorted(entry.spark))}), not {args[0] if args else 'nothing'!r}")
        fields = {"app": name, "tool": tool, "args": tuple(args)}
        argv = prefix + [entry.php if spark else entry.python, tool] + args
    elif entry.flag_format == "env":  # a listed key of the env file takes one of its listed values
        key, value = op.get("key"), op.get("value")
        if "section" in op:
            raise ProposalError(f"{name}: a switch of {entry.flag_file} has no section")
        values = entry.flag_values(key) if isinstance(key, str) else None
        if values is None:
            raise ProposalError(f"{name}: key {key!r} is not a switch of this environment")
        if not isinstance(value, str) or value not in values:
            raise ProposalError(f"{name}: {value!r} is not a value of {key} ({', '.join(sorted(values))})")
        fields = {"app": name, "key": key, "value": value}
        argv = prefix + ["python3", "-I", FLAG_HELPER, "--env", entry.flag_file, key, value]
    else:
        section, key, value = op.get("section"), op.get("key"), op.get("value")
        if not entry.flag_file:
            raise ProposalError(f"{name}: this environment has no switches in the catalog")
        if not isinstance(section, str) or section not in entry.flag_sections:
            raise ProposalError(f"{name}: section {section!r} is not a switch section of this environment")
        if not isinstance(key, str) or not APP_SWITCH.fullmatch(key):
            raise ProposalError(f"{name}: key {key!r} is not a switch (a name ending in _enabled)")
        if value not in ("0", "1"):
            raise ProposalError(f'{name}: value must be "0" or "1"')
        fields = {"app": name, "section": section, "key": key, "value": value}
        argv = prefix + ["python3", "-I", FLAG_HELPER, entry.flag_file, section, key, value]
    if "argv" in op and op["argv"] != argv:
        raise ProposalError(APP_CHANGED.format(app=name))
    return dict(fields, argv=tuple(argv))


def _typed_step(op: dict, verb: str, catalog: Catalog) -> dict:
    """The fields of a cron.line, file.drop or gate.script step; its content is read by the gate's policy
    (kd.exec_policy.cron_problem, drop_problem) here and again on the target."""
    if verb == "gate.script":
        if op.get("value") not in ("on", "off"):
            raise ProposalError('gate.script: value must be "on" or "off"')
        return {"value": op["value"]}
    mark, action = op.get("mark"), op.get("action")
    if not isinstance(mark, str) or not TASK_ID.fullmatch(mark):
        raise ProposalError(f"{verb}: mark must be a card number like KD-123")
    if action not in ACTIONS:
        raise ProposalError(f"{verb}: action must be set or remove")
    field_name = "line" if verb == "cron.line" else "text"
    if (field_name in op) != (action == "set"):
        raise ProposalError(f"{verb}: {field_name} goes with set and only with it")
    if verb == "cron.line":
        line = op.get("line", "")
        problem = exec_policy.cron_problem(line, catalog.critical_units or exec_policy.ALL_UNITS) \
            if action == "set" else None
        if problem:
            raise ProposalError(f"cron.line: {problem}")
        return {"mark": mark, "action": action, "line": line}
    folder, name, text = op.get("dir"), op.get("name"), op.get("text", "")
    problem = exec_policy.drop_problem(folder, name, text if action == "set" else "# remove")
    if problem:
        raise ProposalError(f"file.drop: {problem}")
    return {"mark": mark, "action": action, "folder": folder, "name": name, "text": text}


def risk(value: object) -> Tuple[str, str]:
    """The (level, why) of a step's or an action's ``risk``; ProposalError naming ``risk`` otherwise."""
    if not isinstance(value, dict) or set(value) != {"level", "why"}:
        raise ProposalError("risk must be {\"level\": low|medium|high, \"why\": text}")
    if value["level"] not in RISKS:
        raise ProposalError(f"risk level must be one of {', '.join(RISKS)}")
    try:
        why = _text(value, "why", MAX_EXPLAIN)
    except ProposalError as exc:
        raise ProposalError(f"risk {exc}") from exc
    return value["level"], why


def _packages(value: object, server: str) -> Tuple[Tuple[str, str], ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_PACKAGES:
        raise ProposalError(f"{server}: packages must be a list of 1..{MAX_PACKAGES} entries")
    out: List[Tuple[str, str]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) - {"name", "version"}:
            raise ProposalError(f"{server}: a package has unexpected keys (only name and version)")
        name, version = item.get("name"), item.get("version")
        if not isinstance(name, str) or not PACKAGE.fullmatch(name):
            raise ProposalError(f"{server}: invalid package name {name!r}")
        if not isinstance(version, str) or not VERSION.fullmatch(version):
            raise ProposalError(f"{server}: {name} needs an exact version, not {version!r}")
        if name in (n for n, _ in out):
            raise ProposalError(f"{server}: package {name} listed twice")
        out.append((name, version))
    return tuple(out)


def _script(value: object) -> str:
    """The exact text of a script step: non-empty, bounded, only tabs and newlines beyond printable text, and
    readable — no packed payload the owner could not review."""
    if not isinstance(value, str) or not value.strip():
        raise ProposalError("script must be non-empty text")
    if len(value) > MAX_SCRIPT:
        raise ProposalError(f"script is longer than {MAX_SCRIPT} characters")
    if "\r" in value:
        raise ProposalError(CRLF_HINT)
    hidden = hidden_char(value, "\t\n")
    if hidden:
        raise ProposalError(f"script holds an invisible or control character ({hidden})")
    if _PACKED.search(value):
        raise ProposalError(f"{exec_policy.PACKED_FOUND}: {exec_policy.GUARD}; {PACKED_HINT}")
    return value


def _text(data: dict, key: str, limit: int = MAX_TEXT, allowed: str = "\n") -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ProposalError(f"{key} must be non-empty text up to {limit} characters")
    hidden = hidden_char(value, allowed)
    if hidden:
        raise ProposalError(f"{key} holds an invisible or control character ({hidden})")
    return value


def validate_proposal(data: object, catalog: Catalog, inventory: Iterable[str], enabled: Iterable[str], *,
                      card_servers: Iterable[str], home: Optional[str] = None,
                      deny: Optional[Callable[[str, str], Optional[Tuple[str, str]]]] = None,
                      critical: Optional[Callable[[str], Optional[str]]] = None) -> Proposal:
    """The normalized proposal, or ProposalError; its servers must equal the card's ``server:`` labels.

    The exec gate's policy (kd.exec_policy) reads every step's command — a script's text or a typed step's
    formed command — first, with the very reason the target's gate would give: rule 1 (self-protection), then
    ``critical(text)`` → group (rule 2, the sysadmin's critical classifier with the catalog's site units). ``deny(text,
    server)`` → (rule, why), the sysadmin's denylist, reads every script step. A step they catch is refused here,
    before the owner sees it. They are a check, not the boundary — the target runs only what the owner signed and
    applies the same policy itself."""
    if not isinstance(data, dict):
        raise ProposalError("a proposal must be an object")
    if data.get("schema_version") == 1:
        raise ProposalError(SCHEMA_HINT)
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ProposalError(f"unknown proposal schema_version {data.get('schema_version')!r}")
    extra = set(data) - _PROPOSAL_KEYS
    if extra:
        raise ProposalError(f"unexpected proposal keys {sorted(extra)}")
    task_id = data.get("task_id")
    if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
        raise ProposalError(f"invalid task id {task_id!r}")
    intent, basis = _text(data, "intent"), _text(data, "basis")
    raw = data.get("operations")
    if not isinstance(raw, list) or not raw:
        raise ProposalError("operations must be a non-empty list")
    if len(raw) > MAX_OPERATIONS:
        raise ProposalError(f"at most {MAX_OPERATIONS} operations")
    inventory, enabled = set(inventory), set(enabled)
    operations = tuple(validate_operation(op, catalog, inventory, enabled) for op in raw)
    for number, op in enumerate(operations, 1):
        if op.verb == "script" and home is not None and op.server == home:
            raise ProposalError(HOME_SCRIPT.format(home=home))
        command = remote_command(op, catalog)
        guarded = exec_policy.protect(command)
        if guarded is not None:
            raise ProposalError(f"{op.server}: {guarded}")
        if op.verb == "cron.line" and op.action == "set":
            # the line's command runs as root later: the denylist reads it as it reads a script
            hit = deny(exec_policy.cron_command(op.line), op.server) if deny is not None else None
            if hit is not None:
                raise ProposalError(f"denied by rule {hit[0]} on {op.server}: {hit[1]}; the owner does this by hand")
        if op.verb == "script":
            hit = deny(op.script, op.server) if deny is not None else None
            if hit is not None:
                raise ProposalError(f"denied by rule {hit[0]} on {op.server}: {hit[1]}; the owner does this by hand")
        group = critical(command) if critical is not None else None
        if group is not None:
            raise ProposalError(f"{op.server}: {exec_policy.critical_step(number, group)}")
    proposal = Proposal(task_id, intent, basis, operations)
    if len(proposal.servers) > MAX_SERVERS:
        raise ProposalError(f"at most {MAX_SERVERS} servers")
    labels = tuple(sorted(set(card_servers)))
    if proposal.servers != labels:
        raise ProposalError(f"the card's server: labels {list(labels)} must equal the proposal servers "
                            f"{list(proposal.servers)}")
    return proposal


def remote_argv(op: Operation, catalog: Catalog) -> List[str]:
    if op.verb == "script":
        raise ProposalError("a script step has no argv: its text is the command (remote_command)")
    if op.verb in APP_VERBS:
        return list(op.argv)
    if op.verb == "service.reload":
        return ["systemctl", "reload", op.unit]
    if op.verb == "gate.script":
        return ["python3", "-I", TYPED_HELPER, "script", op.value]
    if op.verb == "cron.line":
        return ["python3", "-I", TYPED_HELPER, "cron", op.mark, op.action] + ([op.line] if op.action == "set" else [])
    if op.verb == "file.drop":
        return ["python3", "-I", TYPED_HELPER, "file", op.folder, op.name, op.mark, op.action] + (
            [op.text] if op.action == "set" else [])
    tool = catalog.servers[op.server].packages
    if op.verb == "package.refresh":
        return ["apt-get", "-q", "update"] if tool == "apt" else ["dnf", "-q", "makecache"]
    if op.verb == "package.upgrade":
        # the whole system, no reboot, in a transient unit of its own: a dropped session does not kill it,
        # a second upgrade cannot start while the first runs (the name is taken), and it ends in the unit's journal
        # (``journalctl -u kd-package-upgrade``). needrestart restarts nothing on its own meanwhile.
        if tool == "apt":
            return UPGRADE_UNIT + ["--setenv=NEEDRESTART_SUSPEND=1", "--setenv=DEBIAN_FRONTEND=noninteractive",
                                   "apt-get", "-q", "-y", "-o", "Dpkg::Options::=--force-confdef", "-o",
                                   "Dpkg::Options::=--force-confold", "full-upgrade"]
        return UPGRADE_UNIT + ["dnf", "-q", "-y", "upgrade"]
    if op.verb == "service.restart":
        return ["systemctl", "restart", op.unit]
    if op.verb == "service.start":
        return ["systemctl", "start", op.unit]
    if op.verb == "package.install":
        if tool == "apt":
            # DEBIAN_FRONTEND covers debconf only; a conffile question on a closed stdin would leave the package
            # unconfigured. The owner approves names and versions, not removals the solver might choose.
            return ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "-q", "-y", "-o",
                    "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold", "--no-remove",
                    "install"] + \
                [f"{n}={v}" for n, v in op.packages]
        return ["dnf", "-q", "-y", "install"] + [f"{n}-{v}" for n, v in op.packages]
    raise ProposalError(f"unknown verb {op.verb!r}")


def remote_command(op: Operation, catalog: Catalog) -> str:
    """The command sent to the target: a script step's exact text; otherwise only catalog words and validated
    parameters, each quoted."""
    if op.verb == "script":
        return op.script
    return shlex.join(remote_argv(op, catalog))


def describe(op: Operation) -> str:
    """One line for the owner's page and messages: the step's ``what``, then the typed operation."""
    return f"{op.what} ({mechanics(op)})" if op.what else mechanics(op)


def mechanics(op: Operation) -> str:
    """The typed operation itself: server, verb and its parameters."""
    if op.verb == "package.refresh":
        return f"{op.server}: обновить индексы пакетов"
    if op.verb == "package.upgrade":
        return f"{op.server}: обновить все пакеты (без перезагрузки, в фоне — юнит kd-package-upgrade)"
    if op.verb == "cron.line" and op.action == "set":
        return f"{op.server}: строка cron root с меткой {op.mark}: {op.line}"
    if op.verb == "cron.line":
        return f"{op.server}: убрать строку cron root с меткой {op.mark}"
    if op.verb == "file.drop" and op.action == "set":
        return f"{op.server}: файл {op.folder}/{op.name} ({len(op.text.splitlines())} строк, копия прежнего — /root/kd-…)"
    if op.verb == "file.drop":
        return f"{op.server}: убрать файл {op.folder}/{op.name}"
    if op.verb == "gate.script":
        return f"{op.server}: {'выключить' if op.value == 'off' else 'включить'} шаги-скрипты на этом сервере"
    if op.verb == "service.restart":
        return f"{op.server}: перезапустить {op.unit}"
    if op.verb == "service.start":
        return f"{op.server}: запустить {op.unit}"
    if op.verb == "service.reload":
        return f"{op.server}: перечитать конфигурацию {op.unit}"
    if op.verb == "app.tool":
        return f"{op.server}: {op.app} — {' '.join((op.tool,) + op.args)}"
    if op.verb == "app.flag" and not op.section:  # an env switch: its argv ends --env <file> <key> <value>
        return f"{op.server}: {op.app} — переключатель {op.argv[-3]} {op.key} = {op.value}"
    if op.verb == "app.flag":
        return f"{op.server}: {op.app} — выключатель [{op.section}] {op.key} = {op.value}"
    if op.verb == "script":
        return f"{op.server}: скрипт, {len(op.script.strip(chr(10)).split(chr(10)))} строк"
    return f"{op.server}: установить " + ", ".join(f"{n} {v}" for n, v in op.packages)
