"""The sysadmin's inventory and the assembly of ssh commands for ssh-ro and ssh-exec."""
import json
import os
import re
import shlex
from pathlib import Path
from typing import Dict, List, NamedTuple, Set, Tuple

INVENTORY = Path(__file__).resolve().parent.parent / "inventory.json"
ENABLED = Path("/etc/digipaddock/servers.enabled")  # servers with the gateway installed (deploy_target.sh maintains it)
ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
SSH = "/usr/bin/ssh"  # an absolute path: PATH is not trusted
# the ro key is in the home of the user ssh-ro runs as (<prefix>-sysadmin; agent after a rollback); servers_setup.sh
RO_KEY_FILE = ".ssh/agent_ro_ed25519"
RO_KNOWN_HOSTS_FILE = ".ssh/known_hosts"
RO_USER = "agent-ro"
EXEC_MAX = 15  # servers in one group ssh-exec


class InventoryError(Exception):
    pass


class Server(NamedTuple):
    alias: str
    host: str
    exec_user: str
    exec_sudo: bool


def _data(path=None) -> dict:
    path = path or INVENTORY
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("servers"), dict):
            raise ValueError("no servers")
        return data
    except (OSError, ValueError) as exc:
        raise InventoryError(f"инвентарь {path} не читается или повреждён: {exc!r}")


def load(path=None) -> Dict[str, Server]:
    path = path or INVENTORY
    try:
        data = _data(path)
        return {
            alias: Server(alias, entry["host"], entry.get("exec_user", "root"), bool(entry.get("exec_sudo", False)))
            for alias, entry in data["servers"].items()
        }
    except (OSError, KeyError, ValueError, TypeError, AttributeError) as exc:
        raise InventoryError(f"инвентарь {path} не читается или повреждён: {exc!r}")


TZ_RE = re.compile(r"UTC|[A-Z][A-Za-z_]+(?:/[A-Za-z0-9_+-]+){1,2}")  # an IANA name, as host.json timezone


def zones(path=None) -> dict:
    """{alias → ZoneInfo} of the servers whose "tz" names a known zone: the server's clock and its applications'
    naive times. A server without one — the host's zone and the clock shift; an unreadable inventory — {}."""
    from zoneinfo import ZoneInfo  # the monitor's host only (Python 3.9+)

    try:
        servers = _data(path)["servers"]
    except InventoryError:
        return {}
    out = {}
    for alias, entry in servers.items():
        tz = entry.get("tz") if isinstance(entry, dict) else None
        if isinstance(tz, str) and TZ_RE.fullmatch(tz):
            try:
                out[alias] = ZoneInfo(tz)
            except (KeyError, ValueError, OSError):  # ZoneInfoNotFoundError is a KeyError
                pass
    return out


def _role(key: str, path=None) -> str:
    """The alias the inventory names for a role (home — the factory's own host, prod — production)."""
    alias = _data(path).get(key)
    servers = load(path)
    if not isinstance(alias, str) or alias not in servers:
        raise InventoryError(f"в инвентаре нет «{key}» — алиаса сервера из servers")
    return alias


def home(path=None) -> Tuple[str, str]:
    """(alias, address) of the factory's own host: the sysadmin never blocks or reboots it."""
    alias = _role("home", path)
    return alias, load(path)[alias].host


def prod(path=None) -> str:
    """The production server's alias: last in a group ssh-exec."""
    return _role("prod", path)


def enabled(path=ENABLED) -> Set[str]:
    try:
        return {line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()}
    except FileNotFoundError:
        return set()
    except (OSError, ValueError) as exc:
        raise InventoryError(f"список подключённых серверов {path} не читается: {exc!r}")


def get(alias: str, path=None, enabled_path=ENABLED) -> Server:
    if not ALIAS_RE.fullmatch(alias or ""):
        raise InventoryError(f"неверное имя сервера: {alias!r}")
    servers = load(path)
    if alias not in servers:
        raise InventoryError(f"сервера {alias!r} нет в инвентаре; есть: {', '.join(sorted(servers))}")
    if alias not in enabled(enabled_path):
        # an agent-ro login to a server without the gateway is "Invalid user", and fail2ban bans the factory host's IP
        raise InventoryError(f"сервер {alias!r} ещё не подключён: на нём нет шлюза (deploy_target.sh)")
    return servers[alias]


def exec_aliases(spec: str) -> List[str]:
    """The server list of a group ssh-exec "a,b,c": the owner's card must name every server, so no all, no empty names
    and no repeats; from 2 to EXEC_MAX; production (the inventory's prod) only last."""
    aliases = spec.split(",")
    if "all" in aliases:
        raise InventoryError("all для ssh-exec не принимается: перечислите серверы через запятую — "
                             "на карточке должен быть каждый")
    if "" in aliases:
        raise InventoryError(f"неверный список серверов: {spec!r} — пустой элемент")
    if not 2 <= len(aliases) <= EXEC_MAX:
        raise InventoryError(f"в списке от 2 до {EXEC_MAX} серверов, а не {len(aliases)}")
    for i, alias in enumerate(aliases):
        if not ALIAS_RE.fullmatch(alias):
            raise InventoryError(f"неверное имя сервера: {alias!r}")
        if alias in aliases[:i]:
            raise InventoryError(f"сервер {alias!r} в списке дважды")
    production = prod()
    if production in aliases[:-1]:
        raise InventoryError(f"{production} — только последним в списке")
    return aliases


def exec_targets(spec: str, path=None, enabled_path=ENABLED) -> List[Server]:
    """The servers of a group ssh-exec in order; any unknown or unattached name is a refusal before anything runs."""
    return [get(alias, path, enabled_path) for alias in exec_aliases(spec)]


def join_command(args: List[str]) -> str:
    """One argument is a ready command line (`ssh-ro x 'journalctl -n 5'`); several are an argv, each one quoted."""
    if not args or not " ".join(args).strip():
        raise InventoryError("пустая команда")
    if len(args) == 1:
        return args[0]
    return " ".join(shlex.quote(a) for a in args)


def ssh_argv(key: str, known_hosts: str, user: str, host: str, remote: str) -> List[str]:
    """ssh without foreign settings: only our key, no agent and nothing interactive."""
    return [
        SSH, "-F", "/dev/null", "-i", key,
        "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts}",
        "-o", "GlobalKnownHostsFile=/dev/null", "-o", "UpdateHostKeys=no",
        "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30",
        "-T", f"{user}@{host}", "--", remote,
    ]


def ro_argv(args: List[str], path=None, enabled_path=ENABLED) -> List[str]:
    """ssh-ro <server> <command…> → ssh to agent-ro@<host>; on the server agent-ro-gate awaits the command."""
    if len(args) < 2:
        raise InventoryError("использование: ssh-ro <сервер> '<команда>'")
    server = get(args[0], path, enabled_path)
    home = own_home()
    return ssh_argv(f"{home}/{RO_KEY_FILE}", f"{home}/{RO_KNOWN_HOSTS_FILE}", RO_USER, server.host,
                    join_command(args[1:]))


def own_home() -> str:
    """The home of the process's user — from the user database, not from $HOME."""
    try:
        import pwd
    except ImportError:  # Windows: tests only
        return os.path.expanduser("~")
    return pwd.getpwuid(os.getuid()).pw_dir


def ro_targets(args: List[str], path=None, enabled_path=ENABLED) -> List[Tuple[str, List[str]]]:
    """[(alias, argv)] for ssh-ro: "server", "server1,server2" or "all" — every attached one, in inventory order.
    The agent needs no for loop with $s: Claude Code would send such a command to the owner as a card."""
    if len(args) < 2:
        raise InventoryError("использование: ssh-ro <сервер|сервер1,сервер2|all> '<команда>'")
    spec = args[0]
    if spec == "all":
        on = enabled(enabled_path)
        aliases = [alias for alias in load(path) if alias in on]
        if not aliases:
            raise InventoryError("нет ни одного подключённого сервера")
    elif "," in spec:
        aliases = []
        for alias in spec.split(","):
            if not alias:
                raise InventoryError(f"неверный список серверов: {spec!r}")
            if alias not in aliases:
                aliases.append(alias)
    else:
        aliases = [spec]
    return [(alias, ro_argv([alias] + args[1:], path, enabled_path)) for alias in aliases]
