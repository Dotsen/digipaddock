"""Names of one host: Linux users, the commands people and agents type, the card number.

Users are <prefix>, <prefix>-<agent>, <prefix>-<service role>; commands are <commands>-<word>; cards are
<cards>-<n>. All three are host configuration: /etc/digipaddock/naming.json (KD_NAMING_JSON overrides the path),
written at the first deploy; a host without it uses common/naming.default.json. Every user name in code, units,
sudoers and policies derives from here; repository files carry tokens (@base@, @exec@,
@forge@, @repo@, @cabinet@, @push@, @review@, @transcribe@, @observe@, @data@, @agent:<name>@) that render() resolves. A broken file or token fails closed.
The repository spells commands and cards with the defaults (kd-<word>, KD-<n>); rewrite() turns them into this
host's names in what agents and people read (policies, agent texts, the floor, units). Product infrastructure —
units, sockets, groups, paths — keeps the product name digipaddock on every host.
Deployment values (hosts, domains, the forge address and accounts) come from host/host.json of the release
tree (KD_HOST_JSON overrides the path): @host:<key>@ tokens resolve from it.
Python 3.6-compatible and free of kd imports: shell runs it as a script with python3 -I.
"""

import glob
import json
import os
import re
import sys

CONFIG = "/etc/digipaddock/naming.json"
DEFAULT_CONFIG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "naming.default.json"
)
# The release tree is three levels up: <tree>/common/kd/naming.py.
HOST_JSON = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "host", "host.json"
)
HOST_KEYS = (
    "stage_alias", "stage_ip", "prod_alias", "forge_host", "forge_ssh_url", "forge_api",
    "cabinet_host", "cabinet_origin", "docs_dir", "mirror_remote", "forge_owner", "dev_email",
    "forge_dev_user", "forge_merge_user", "forge_review_user", "forge_import_user", "owner_language",
)
# Host values a host.json may leave out: their default. backup_dir — where the host's own dumps go (the Hindsight
# database), the directory a deployment's backup mirror takes. mirror_author — the author and committer of the
# snapshot commits kd-mirror publishes ("Name <email>").
HOST_DEFAULTS = {"timezone": "UTC", "backup_dir": "/var/backups/digipaddock",
                 "mirror_author": "DigiPaddock <mirror@localhost>"}
_NAME = r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62})"  # an ssh alias, a forge account: never an option (-o…)
_HOSTNAME = r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,252})(?::[0-9]{1,5})?"
_PATH = r"/(?:[A-Za-z0-9._-]+/?)*"
# The shape of every value (review M7): host values go into units, nginx, shell and agent texts — one line, no
# separators those read; \Z, not $, so a trailing newline is no match.
HOST_SHAPES = {
    "stage_alias": _NAME, "prod_alias": _NAME, "forge_owner": _NAME,
    "forge_dev_user": _NAME, "forge_merge_user": _NAME, "forge_review_user": _NAME, "forge_import_user": _NAME,
    "stage_ip": r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}|[0-9A-Fa-f:]{2,39}",
    "forge_host": _HOSTNAME, "cabinet_host": _HOSTNAME,
    "forge_ssh_url": r"ssh://[A-Za-z0-9._-]+@" + _HOSTNAME + r"/(?:[A-Za-z0-9._-]+/)*\{repo\}\.git",
    "forge_api": r"https://" + _HOSTNAME + r"(?:/[A-Za-z0-9._-]+)*",
    "cabinet_origin": r"https://" + _HOSTNAME,
    "docs_dir": _PATH, "backup_dir": _PATH,
    "mirror_remote": r"(?:[A-Za-z0-9._-]+@" + _HOSTNAME + r":|ssh://[A-Za-z0-9._-]+@" + _HOSTNAME
                     + r"/)[A-Za-z0-9._/-]+\.git",
    "dev_email": r"[A-Za-z0-9._+-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,252})",
    "mirror_author": r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,79} <[A-Za-z0-9._+-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,252})>",
    "timezone": r"UTC|[A-Z][A-Za-z_]+(?:/[A-Za-z0-9_+-]+){1,2}",  # an IANA zone of /usr/share/zoneinfo
    # the language the owner reads: the agents' replies, the notices, the cabinet's default (its ru and en catalogs)
    "owner_language": r"ru|en",
}
# Host values derived from another one, never read from host.json: mirror_host — the host name of
# mirror_remote, the one network domain of a developer who fetches from the mirror (git.mirror).
HOST_DERIVED = ("mirror_host",)
# memory_banks (optional): an agent's Hindsight bank when it is not coding-agent::<agent> (a bank kept across a
# rename of the agent).
BANK_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,99}\Z")
PREFIX_RE = re.compile(r"[a-z][a-z0-9]{1,7}\Z")
COMMANDS_RE = PREFIX_RE
CARDS_RE = re.compile(r"[A-Z][A-Z0-9]{1,5}\Z")
# How the repository spells a command and a card; rewrite() turns them into the host's.
CANON_COMMANDS = "kd"
CANON_CARDS = "KD"
# Command prefixes of tools a host already has: <commands>-<word> would pass for theirs (apt-get, git-upload-pack).
BUSY_COMMANDS = frozenset({"apt", "git", "npm", "pip", "ssh", "systemd", "dpkg"})
_CONFIG_KEYS = {"schema_version", "prefix", "commands", "cards", "card_aliases"}
# <prefix>-<name> stays within the 32 characters of a Linux user name.
AGENT_NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,22}\Z")
SERVICE_ROLES = ("exec", "forge", "repo", "cabinet", "push", "review", "transcribe", "observe", "data", "ci")  # repo: the checkout refresher of repo.read; cabinet: the board service; push: the Web Push sender, the one cabinet process with network (global review 15.10.2026); review: the approver of dev's PRs, who checks the owner's signature itself; transcribe: the one-shot voice transcription, no network; observe: the host reader of kd-host-read, the one platform user with the journal; data: kd-data, the one holder of the key to the servers' read-only databases; ci: the dev.check tests of a candidate, no socket, no channel, no group
# An agent with one of these names would get a service account, or the planned worker one.
RESERVED_AGENTS = frozenset(SERVICE_ROLES + ("ro", "worker"))
# The read-only user on target servers (and on the stage as a target): not covered here.
TARGET_USER = "agent-ro"
_TOKEN = re.compile(
    r"@(base|exec|forge|repo|cabinet|push|review|transcribe|observe|data|ci|agent:([^@\s]*)|host:([a-z_]+))@"
)
# Whatever is left that looks like a token is a typo: @word:...@, a word that starts like a token
# name in any case (@Base@, @forgee@, @agent@), or @UPPER_CASE@. Plain at-signs (bots, e-mails,
# unit instances, @dev assignees) pass.
_LOOSE = re.compile(
    r"@(?:[A-Za-z]+:[^@\s]*|(?i:base|exec|forge|repo|cabinet|push|review|transcribe|observe|data|ci|agent|host)\w*|[A-Z][A-Z_]*)@"
)
# An unclosed token (@exec, @base/, @agent:dev"): a floor rule with one would stop denying in
# silence. user@host text (git@forge.example) is not a token: a word, dot or plus precedes it.
_UNCLOSED = re.compile(
    r"(?<![\w.%+-])@(?i:base|exec|forge|repo|cabinet|push|review|transcribe|observe|data|ci|agent|host)(?![a-zA-Z0-9])"
)
# A command as the repository spells it, with the path before it: a file of the release tree (common/bin/kd-task,
# agents/dev/bin/kd-merge) keeps its name, a command on PATH (bare, /usr/local/bin/, agent-bin/) takes the host's.
_COMMAND = re.compile(r"(?<![A-Za-z0-9_.-])((?:[A-Za-z0-9_.@-]*/)*)" + CANON_COMMANDS + r"-(?=[a-z0-9])")
_TREE_BIN = re.compile(r"(?:^|/)(?:common|agents/[^/]+)/bin/\Z")
# A card's form (KD-<n>, KD-n, KD-m, KD-id, KD-target, KD-review) takes the host's prefix; a numbered card
# is the platform's own history.
_CARD_FORM = r"-(?=<|(?:[nmN]|id|target|review)\b)"
_CARD = re.compile(r"(?<![A-Za-z0-9_])" + CANON_CARDS + _CARD_FORM)


class NamingError(ValueError):
    pass


def config_path():
    return os.environ.get("KD_NAMING_JSON") or CONFIG


def load(path=None, default=DEFAULT_CONFIG):
    """The host's names {prefix, commands, cards, card_aliases}; the default file only when the host file does not
    exist. commands and cards default to the repository's spelling, card_aliases (earlier card prefixes of this
    host, still accepted in what people type) to none."""
    source = path or config_path()
    if not os.path.lexists(source):
        source = default
    try:
        with open(source, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise NamingError("{}: unreadable ({})".format(source, exc.__class__.__name__))
    if (
        not isinstance(data, dict)
        or not {"schema_version", "prefix"} <= set(data) <= _CONFIG_KEYS
        or type(data["schema_version"]) is not int
        or data["schema_version"] != 1
    ):
        raise NamingError("{}: expected {{schema_version: 1, prefix[, commands, cards, card_aliases]}}".format(source))
    names = {
        "prefix": data["prefix"],
        "commands": data.get("commands", CANON_COMMANDS),
        "cards": data.get("cards", CANON_CARDS),
    }
    for key, shape in (("prefix", PREFIX_RE), ("commands", COMMANDS_RE), ("cards", CARDS_RE)):
        if not isinstance(names[key], str) or not shape.match(names[key]):
            raise NamingError("{}: {} must match {}".format(source, key, shape.pattern))
    if names["prefix"] in (names["commands"], CANON_COMMANDS):
        # users <prefix>-dev and the commands would share one spelling: rewrite() could not tell them apart
        raise NamingError("{}: prefix {!r} must differ from the command prefixes".format(source, names["prefix"]))
    if names["commands"] in BUSY_COMMANDS:
        raise NamingError("{}: commands {!r} is a prefix of tools a host already has".format(source, names["commands"]))
    aliases = data.get("card_aliases", [])
    if (
        not isinstance(aliases, list)
        or not all(isinstance(a, str) and CARDS_RE.match(a) for a in aliases)
        or len(set(aliases)) != len(aliases)
        or names["cards"] in aliases
    ):
        raise NamingError("{}: card_aliases must be distinct card prefixes other than cards".format(source))
    names["card_aliases"] = tuple(aliases)
    return names


def load_prefix(path=None, default=DEFAULT_CONFIG):
    """The host prefix of user names."""
    return load(path, default)["prefix"]


_NAMES = load()
PREFIX = _NAMES["prefix"]
COMMANDS = _NAMES["commands"]
CARDS = _NAMES["cards"]
CARD_ALIASES = _NAMES["card_aliases"]


def host_json_path():
    return os.environ.get("KD_HOST_JSON") or HOST_JSON


def _shaped(key, value):
    return re.match(r"(?:" + HOST_SHAPES[key] + r")\Z", value) and "/../" not in value + "/" and "/./" not in value + "/"


def remote_host(remote):
    """The host name of a git SSH remote of the mirror_remote shape (user@host:path, ssh://user@host[:port]/path),
    without its port."""
    rest = remote.split("://", 1)[1] if "://" in remote else remote
    return re.split(r"[:/]", rest.split("@", 1)[1], 1)[0]


def _derived(host):
    """A HOST_DERIVED value of a host mapping, or None when what it derives from is missing or misshapen."""
    remote = host.get("mirror_remote")
    return remote_host(remote) if isinstance(remote, str) and _shaped("mirror_remote", remote) else None


def load_host(path=None):
    """The deployment values of this release tree: every key of HOST_KEYS is a string of its HOST_SHAPES shape,
    a key of HOST_DEFAULTS is one or takes its default, memory_banks maps agent names to bank ids."""
    source = path or host_json_path()
    try:
        with open(source, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise NamingError("{}: unreadable ({})".format(source, exc.__class__.__name__))
    if (
        not isinstance(data, dict)
        or type(data.get("schema_version")) is not int
        or data["schema_version"] != 1
    ):
        raise NamingError("{}: expected {{schema_version: 1, ...}}".format(source))
    for key in HOST_KEYS:
        if not isinstance(data.get(key), str) or not data[key]:
            raise NamingError("{}: {} must be a non-empty string".format(source, key))
    data = dict(data)
    for key, default in HOST_DEFAULTS.items():
        data.setdefault(key, default)
    for key in HOST_KEYS + tuple(HOST_DEFAULTS):
        value = data[key]
        if not isinstance(value, str) or not _shaped(key, value):
            raise NamingError("{}: {} has not the shape of a {} value: {!r}".format(source, key, key, str(value)[:80]))
    for key in HOST_DERIVED:  # a value host.json names itself is overwritten: it follows its source
        data[key] = _derived(data)
    banks = data.setdefault("memory_banks", {})
    if not isinstance(banks, dict) or not all(
        isinstance(a, str) and AGENT_NAME_RE.match(a) and isinstance(b, str) and BANK_RE.match(b)
        for a, b in banks.items()
    ):
        raise NamingError("{}: memory_banks must map agent names to bank ids".format(source))
    return data


FORGE_ACCOUNTS = ("forge_dev_user", "forge_merge_user", "forge_review_user", "forge_import_user")


def token_files_re():
    """A pattern of the Forgejo accounts' token files (<account>.env, named after the accounts); a broken host.json
    — any *.env file name, so a guard stays closed."""
    try:
        host = load_host()
    except NamingError:
        return r"[\w.-]+\.env\b"
    return "|".join(re.escape(host[key] + ".env") for key in FORGE_ACCOUNTS)


def hindsight_config(config, banks):
    """The agents' Hindsight client config (common/hindsight/coding-agent.json) with this host's banks: an agent of
    ``banks`` (host.json memory_banks) whose folder maps to coding-agent::<agent> maps to its own, settings too."""
    out = json.loads(json.dumps(config))
    folders, settings = out.get("mapPathToBank") or {}, out.get("banks") or {}
    for folder, bank in list(folders.items()):
        agent = folder.rstrip("/").rsplit("/", 1)[-1]
        if agent in banks and bank == "coding-agent::" + agent:
            folders[folder] = banks[agent]
            if bank in settings:
                settings[banks[agent]] = settings.pop(bank)
    return out


def base_user(prefix=None):
    return prefix or PREFIX


def agent_user(name, prefix=None):
    if (
        not isinstance(name, str)
        or not AGENT_NAME_RE.match(name)
        or name in RESERVED_AGENTS
    ):
        raise NamingError("no user for agent name {!r}".format(name))
    return "{}-{}".format(base_user(prefix), name)


def service_user(role, prefix=None):
    if role not in SERVICE_ROLES:
        raise NamingError("unknown service role {!r}".format(role))
    return "{}-{}".format(base_user(prefix), role)


def service_users(prefix=None):
    return frozenset(service_user(role, prefix) for role in SERVICE_ROLES)


def home(user):
    return "/home/" + user


def user_re(prefix=None):
    """Base user or <prefix>-<name>; use with fullmatch."""
    return re.compile(re.escape(base_user(prefix)) + r"(?:-[a-z][a-z0-9-]{0,22})?")


def command(name, commands=None):
    """The host's name of a command the repository spells kd-<word>; other names unchanged."""
    if name.startswith(CANON_COMMANDS + "-"):
        return (commands or COMMANDS) + name[len(CANON_COMMANDS):]
    return name


_OWN = [None]


def _own_commands():
    """The repository's commands: the kd-<word> files of common/bin and agents/*/bin of this tree."""
    if _OWN[0] is None:
        tree = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        found = set()
        for pattern in ("common/bin/", "agents/*/bin/"):
            found.update(os.path.basename(p) for p in glob.glob(os.path.join(tree, pattern, CANON_COMMANDS + "-*")))
        _OWN[0] = frozenset(found)
    return _OWN[0]


def canonical_command(name, commands=None):
    """The repository's name (kd-<word>) of a command typed by this host's name; other names, a tool of the
    host's own that shares the prefix (acme-get) among them, unchanged."""
    own = (commands or COMMANDS) + "-"
    if name.startswith(own) and len(name) > len(own):
        canonical = CANON_COMMANDS + "-" + name[len(own):]
        if canonical in _own_commands():
            return canonical
    return name


def rewrite(text, commands=None, cards=None):
    """``text`` with the repository's command and card spelling turned into the host's (identity on a host that
    keeps the defaults); a file of the release tree keeps its name."""
    commands, cards = commands or COMMANDS, cards or CARDS
    if commands != CANON_COMMANDS:
        text = _COMMAND.sub(
            lambda m: m.group(0) if _TREE_BIN.search(m.group(1)) else m.group(1) + commands + "-", text
        )
    if cards != CANON_CARDS:
        text = _CARD.sub(cards + "-", text)
    return text


def repository_length(text, commands=None, cards=None):
    """len(text) as the repository spells it: a limit on a text is its author's, whatever the host's names."""
    commands, cards = commands or COMMANDS, cards or CARDS
    longer = 0
    if commands != CANON_COMMANDS:
        found = re.findall(r"(?<![A-Za-z0-9_.-])" + re.escape(commands) + r"-(?=[a-z0-9])", text)
        longer += (len(commands) - len(CANON_COMMANDS)) * len(found)
    if cards != CANON_CARDS:
        found = re.findall(r"(?<![A-Za-z0-9_])" + re.escape(cards) + _CARD_FORM, text)
        longer += (len(cards) - len(CANON_CARDS)) * len(found)
    return len(text) - longer


def _resolve(match, prefix, host):
    role, name, key = match.group(1), match.group(2), match.group(3)
    if key is not None:
        if host[0] is None:
            host[0] = load_host()
        if key not in HOST_KEYS + tuple(HOST_DEFAULTS) + HOST_DERIVED:
            raise NamingError("unknown host key {!r}".format(key))
        if key in HOST_DERIVED:  # from its source, whoever built the mapping
            value = _derived(host[0])
        else:
            value = host[0].get(key, HOST_DEFAULTS.get(key))
        if not isinstance(value, str):
            raise NamingError("unknown host key {!r}".format(key))
        return value
    if role == "base":
        return base_user(prefix)
    if name is None:
        return service_user(role, prefix)
    return agent_user(name, prefix)


def render(text, prefix=None, host=None):
    """``text`` with every naming token resolved; a malformed token is an error.

    ``host`` is the mapping for @host:<key>@; without it host.json is read once, on the first such token.
    """
    loaded = [host]
    out = _TOKEN.sub(lambda m: _resolve(m, prefix, loaded), text)
    left = _LOOSE.search(out) or _UNCLOSED.search(out)
    if left:
        raise NamingError("unresolved naming token {!r}".format(left.group(0)))
    return out


def main(argv):
    try:
        if len(argv) == 2 and argv[0] == "render":  # what gets installed: tokens resolved, then the host's names
            with open(argv[1], encoding="utf-8") as f:
                sys.stdout.write(rewrite(render(f.read())))
            return 0
        if len(argv) == 2 and argv[0] == "host":
            if argv[1] not in HOST_KEYS + tuple(HOST_DEFAULTS) + HOST_DERIVED:
                raise NamingError("unknown host key {!r}".format(argv[1]))
            print(load_host()[argv[1]])
            return 0
        if len(argv) == 2 and argv[0] == "cmd":
            if not re.match(CANON_COMMANDS + r"-[a-z0-9][a-z0-9-]*\Z", argv[1]):
                raise NamingError("not a command name: {!r}".format(argv[1]))
            print(command(argv[1]))
            return 0
        if argv == ["names"]:
            print(PREFIX, COMMANDS, CARDS)
            return 0
        if argv == ["user", "base"]:
            print(base_user())
            return 0
        if len(argv) == 2 and argv[0] == "user" and argv[1] in SERVICE_ROLES:
            print(service_user(argv[1]))
            return 0
        if len(argv) == 3 and argv[:2] == ["user", "agent"]:
            print(agent_user(argv[2]))
            return 0
    except (OSError, NamingError) as exc:
        print("naming: {}".format(exc), file=sys.stderr)
        return 1
    print(
        "usage: naming.py user base|exec|forge|repo|cabinet|push|review|transcribe|observe|data|ci|agent <name>"
        " | host <key> | cmd kd-<word> | names | render <file>",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
