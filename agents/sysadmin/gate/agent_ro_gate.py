#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""agent-ro-gate — the sysadmin agent's read gateway on a target server.

A forced command (command=…) in authorized_keys of the user agent-ro.
Takes SSH_ORIGINAL_COMMAND, splits it with shlex and runs it WITHOUT a shell — only
whitelisted commands, with options and paths checked. Timeout 60 s, output
up to 1 MB. Commands that need root — only the exact forms of SUDO_EXACT
through sudo -n (binaries outside SAFE_PATH come from SUDO_PATHS: agent-sec-snapshot and
agent-hindsight-usage are in sudoers with the sha256 of their content; a form without arguments
has `""` in sudoers; `agent-ro-gate --sudoers` prints the sudoers lines).

A known limitation: the check→open race (TOCTOU) and hard links made by users who
can write to readable directories are outside the threat model (the gateway confines an LLM agent, not hostile
local users; fs.protected_symlinks/hardlinks are on by default).

Python 3.6+ and the stdlib only: targets with AlmaLinux 8 have Python 3.6 as the system Python.
"""
import gzip
import hashlib
import json
import os
import posixpath
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import zlib
from typing import Callable, Dict, Iterable, Iterator, List, NamedTuple, Optional, Sequence, Tuple

try:
    import syslog
except ImportError:  # Windows: local tests.
    syslog = None

TIMEOUT = 60
LIMIT = 1024 * 1024
SAFE_PATH = "/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin"  # Certbot is often installed from Snap.
CHILD_ENV = {
    "PATH": SAFE_PATH, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
    "PAGER": "cat", "SYSTEMD_PAGER": "cat", "SYSTEMD_COLORS": "0",
}

SHELL_META = re.compile(r"[;&$`\n\r]")
# "|" only inside a pattern's value (journalctl -g/--grep, grep/zgrep -e/--regexp); elsewhere the validators decide
PIPE_COMMANDS = {"journalctl", "grep", "zgrep"}
PIPE_WHY = ("метасимвол | разрешён только внутри шаблона: journalctl -g \"a|b\", grep -e \"a|b\"; "
            "конвейеров нет — фильтруйте вывод на своей стороне")
REDIRECT_META = re.compile(r"[<>]")
NUMBER = re.compile(r"^[+-]?\d+[kKmMgGbB]?$", re.ASCII)
WORD = re.compile(r"^[A-Za-z0-9_.,:=%+@/-]+$", re.ASCII)
UNIT = re.compile(r"^[A-Za-z0-9@:._\\*-]+$", re.ASCII)


class Deny(Exception):
    """The command is not allowed; the text is the reason, the agent sees it."""


class Plan(NamedTuple):
    argv: List[str]  # argv[0] is the command name without a path.
    sudo: bool


class Result(NamedTuple):
    returncode: int
    output: bytes
    truncated: bool
    timed_out: bool


Validator = Callable[[List[str]], List[str]]
Opts = List[Tuple[str, Optional[str]]]
VALIDATORS: Dict[str, Validator] = {}

# Run only these exact command forms as root, via sudo -n. The sudoers entries come from this list.
SUDO_EXACT = (
    ("wg", "show"), ("wg", "show", "interfaces"),
    ("wg", "show", "all", "latest-handshakes"), ("wg", "show", "all", "endpoints"),
    ("wg", "show", "all", "transfer"), ("wg", "show", "all", "allowed-ips"),
    ("crontab", "-l", "-u", "root"),
    ("iptables", "-S"), ("iptables", "-t", "nat", "-S"), ("iptables", "-t", "mangle", "-S"),
    ("iptables", "-t", "nat", "-L", "-n", "-v"), ("iptables", "-t", "mangle", "-L", "-n", "-v"),
    ("ip6tables", "-S"), ("ip6tables", "-t", "nat", "-S"), ("ip6tables", "-t", "mangle", "-S"),
    ("nft", "list", "ruleset"),
    ("docker", "ps"), ("docker", "ps", "-a"),
    ("ss", "-tlnpu"),
    ("du", "-sh", "/var/lib/mysql"), ("du", "-h", "--max-depth=1", "/var/lib/mysql"),
    ("dmesg", "-T"), ("dmesg", "-T", "--level=err,warn"),
    ("needrestart", "-b", "-r", "l"),
    ("certbot", "certificates"),
    ("agent-sec-snapshot",),                       # Security snapshot for the monitor.
    ("ufw", "status", "verbose"), ("fail2ban-client", "status", "sshd"),
    ("dnf", "-q", "-C", "check-update", "--security"),  # -C: the system cache only (dnf-makecache), no network
    ("dnf", "needs-restarting", "-r"),
    ("ls", "-la", "/etc/sudoers.d"),  # For the root-owned directory at mode 0750, show names, owners, and permissions only.
    ("ssh-keygen", "-lf", "/root/.ssh/authorized_keys"),  # Show root key fingerprints, types, and comments. Keep the keys themselves private.
    ("agent-hindsight-usage",),  # Report only numeric Hindsight usage from its units’ PrivateTmp and usage.jsonl.
)
# Outside SAFE_PATH, use only these binaries by absolute path. Keep SAFE_PATH fixed.
SUDO_PATHS = {"agent-sec-snapshot": "/usr/local/bin/agent-sec-snapshot",
              "agent-hindsight-usage": "/usr/local/bin/agent-hindsight-usage"}


def _binary(name: str, which) -> Optional[str]:
    return which(SUDO_PATHS.get(name, name), path=SAFE_PATH)


def parse_opts(args: Sequence[str], flags: str = "", valued: str = "", long_flags: Sequence[str] = (),
               long_valued: Sequence[str] = (), long_optional: Sequence[str] = (),
               numbers: bool = False) -> Tuple[Opts, List[str]]:
    """A getopt-like parse: (options, positionals). An unknown option is a Deny.

    flags/valued are short options without and with a value; long_valued takes its value
    through '=' or as the next word, long_optional only through '='.
    numbers=True — words like -1 are positional (journalctl -b -1, last -20).
    """
    for long_list in (long_flags, long_valued, long_optional):
        if isinstance(long_list, str):  # Without a comma, ("--all") is a string and `in` would test for a substring.
            raise TypeError("long_* должны быть кортежем/списком, а не строкой: " + long_list)
    opts: Opts = []
    positional: List[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            positional.extend(args[i + 1:])
            break
        if arg.startswith("--"):
            name, eq, value = arg.partition("=")
            if name in long_valued:
                if not eq:
                    i += 1
                    if i >= len(args):
                        raise Deny("нет значения у " + name)
                    value = args[i]
                opts.append((name, value))
            elif name in long_optional:
                opts.append((name, value if eq else None))
            elif name in long_flags and not eq:
                opts.append((name, None))
            else:
                raise Deny("опция не разрешена: " + arg)
        elif arg.startswith("-") and len(arg) > 1 and not (numbers and NUMBER.match(arg)):
            j = 1
            while j < len(arg):
                letter = arg[j]
                if letter in valued:
                    value = arg[j + 1:]
                    if not value:
                        i += 1
                        if i >= len(args):
                            raise Deny("нет значения у -" + letter)
                        value = args[i]
                    opts.append(("-" + letter, value))
                    break
                if letter not in flags:
                    raise Deny("опция не разрешена: -" + letter)
                opts.append(("-" + letter, None))
                j += 1
        else:
            positional.append(arg)
        i += 1
    return opts, positional


def _mysql_sql_at(args: Sequence[str]) -> Tuple[Optional[int], Optional[str]]:
    """(the index of the word carrying SQL; the SQL itself) — or (None, None) when there is no -e/--execute."""
    for i, arg in enumerate(args):
        if arg in ("-e", "--execute"):
            return (i + 1, args[i + 1]) if i + 1 < len(args) else (None, "")
        if arg.startswith("--execute="):
            return i, arg.split("=", 1)[1]
        if arg.startswith("-e") and not arg.startswith("--") and len(arg) > 2:
            return i, arg[2:]
    return None, None


# A mirror of sysadmin.denylist._sql_of (the same forms: -e SQL, -eSQL, --execute SQL, --execute=SQL): a change in
# one is repeated in the other.
def mysql_sql(args: Sequence[str]) -> Optional[str]:
    """The SQL of mysql -e/--execute (or None; "" — -e has no value): only inside it are < and > allowed."""
    return _mysql_sql_at(args)[1]


# --- system commands ----------------------------------------------------------

def _flags_only(name: str, flags: str = "", long_flags: Sequence[str] = ()) -> Validator:
    def validate(args: List[str]) -> List[str]:
        _, positional = parse_opts(args, flags, "", long_flags)
        if positional:
            raise Deny(name + ": лишние аргументы: " + " ".join(positional))
        return list(args)
    return validate


def _only(name: str, forms: Sequence[Sequence[str]]) -> Validator:
    def validate(args: List[str]) -> List[str]:
        if list(args) not in [list(form) for form in forms]:
            raise Deny(name + ": разрешено только: " + "; ".join(" ".join([name] + list(f)) for f in forms))
        return list(args)
    return validate


PS_ARG = re.compile(r"^[A-Za-z0-9_,=%+:.-]+$", re.ASCII)


# BSD's e prints every process's environment after its command: the same secrets as /proc/<pid>/environ. A letter
# cluster with an e is refused unless it is a known UNIX form where -e means "every process" (review 04.11.2026);
# a format of one word with an e takes "=" (-o user=).
PS_UNIX_E = frozenset({"-e", "-ef", "-eF", "-eo", "-eH", "-el", "-ely", "-eLf", "-ejH", "-efH", "-efw", "-efww",
                       "-ew", "-eww", "-eO", "-ejf"})
PS_LETTERS = re.compile(r"^-?[A-Za-z]+$", re.ASCII)
# UNIX options whose next word is a command, user, group, pid, session or tty: that word is not a letter cluster
# (ps -C node, ps -u postgres). A format (-o, -O, --format, --sort) stays under the rule above: one word takes "=".
PS_VALUED = "CGgpqstUu"
PS_VALUED_LONG = frozenset({"--pid", "--ppid", "--user", "--User", "--group", "--Group", "--sid", "--tty",
                            "--cols", "--columns", "--lines", "--rows", "--width"})


def _ps_value_next(arg: str) -> bool:
    """A UNIX cluster whose first valued option is its last letter (-u, -fu): the next word is that value. In -uroot
    the value is attached ("root"), so the next word is an option again — ps -uroot e prints environments."""
    if not (arg.startswith("-") and not arg.startswith("--") and PS_LETTERS.match(arg)):
        return False
    letters = arg[1:]
    first = next((i for i, c in enumerate(letters) if c in PS_VALUED), None)
    return first == len(letters) - 1


def _ps(args: List[str]) -> List[str]:
    value_next = False
    for arg in args:
        if not PS_ARG.match(arg):
            raise Deny("ps: недопустимый аргумент: " + arg)
        if value_next:
            value_next = False
            continue
        value_next = arg in PS_VALUED_LONG or _ps_value_next(arg)
        if PS_LETTERS.match(arg) and "e" in arg and arg not in PS_UNIX_E:
            raise Deny("ps: " + arg + " — флаг e (окружение процессов) запрещён; все процессы: ps -e или ps ax, "
                       "формат из одного слова: -o " + arg.lstrip("-") + "=")
    return list(args)


def _df(args: List[str]) -> List[str]:
    _, positional = parse_opts(
        args, "ahHiklPT", "tx",
        ("--all", "--human-readable", "--si", "--inodes", "--local", "--portability", "--print-type", "--total"),
        ("--type", "--exclude-type", "--block-size"), ("--output",))
    for path in positional:
        if not re.match(r"^/[\w./@:-]*$", path, re.ASCII):
            raise Deny("df: нужен абсолютный путь: " + path)
    return list(args)


def _top(args: List[str]) -> List[str]:
    opts, positional = parse_opts(args, "bcHiSsw", "nopu")
    counts = [value for opt, value in opts if opt == "-n"]
    if positional or ("-b", None) not in opts or counts != ["1"]:
        raise Deny("top: только пакетный режим, один снимок: top -bn1")
    return list(args)


SS_FILTER = re.compile(r"^[A-Za-z0-9_.:=!()\[\]/%-]+$", re.ASCII)


def _ss(args: List[str]) -> List[str]:
    _, positional = parse_opts(
        args, "HOnraloempisZz46tudwxSMbT0", "fA",
        ("--numeric", "--resolve", "--all", "--listening", "--options", "--extended", "--memory", "--processes",
         "--info", "--summary", "--tcp", "--udp", "--raw", "--unix", "--ipv4", "--ipv6", "--no-header", "--oneline"),
        ("--family", "--query"))
    for word in positional:
        if not SS_FILTER.match(word):
            raise Deny("ss: недопустимый фильтр: " + word)
    return list(args)


IP_OPTIONS = {"-4", "-6", "-s", "-stats", "-d", "-details", "-br", "-brief", "-j", "-json", "-p", "-pretty",
              "-o", "-oneline", "-h", "-human", "-r", "-resolve", "-iec", "-t", "-timestamp", "-c=never",
              "-color=never"}
IP_OBJECTS = {"a": "addr", "addr": "addr", "address": "addr", "r": "route", "ro": "route", "route": "route",
              "ru": "rule", "rule": "rule", "l": "link", "link": "link", "n": "neigh", "neigh": "neigh",
              "neighbor": "neigh", "neighbour": "neigh", "netconf": "netconf"}
IP_VIEW = {"show", "list", "lst", "ls"}
IP_CHANGE = {"add", "append", "change", "chg", "del", "delete", "flush", "prepend", "replace", "restore", "save",
             "set", "exec", "monitor", "test"}


def _ip(args: List[str]) -> List[str]:
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] not in IP_OPTIONS:
            raise Deny("ip: опция не разрешена: " + args[i])
        i += 1
    if i >= len(args) or args[i] not in IP_OBJECTS:
        raise Deny("ip: разрешены addr, route, rule, link, neigh, netconf — только просмотр")
    obj, rest = IP_OBJECTS[args[i]], args[i + 1:]
    allowed = IP_VIEW | ({"get"} if obj == "route" else set())
    if rest and rest[0] not in allowed:
        raise Deny("ip: только просмотр: " + ", ".join(sorted(allowed)))
    for word in rest:
        if word in IP_CHANGE or not WORD.match(word):
            raise Deny("ip: недопустимый аргумент: " + word)
    return list(args)


SYSTEMCTL_VERBS = {"status", "show", "cat", "is-active", "is-failed", "is-enabled", "is-system-running",
                   "list-units", "list-timers", "list-unit-files", "list-sockets", "list-dependencies"}


def _systemctl(args: List[str]) -> List[str]:
    _, positional = parse_opts(
        args, "alqr", "pPnto",
        ("--all", "--full", "--quiet", "--no-pager", "--no-legend", "--plain", "--value", "--failed",
         "--recursive", "--reverse", "--after", "--before"),
        ("--property", "--lines", "--type", "--state", "--output"))
    if not positional or positional[0] not in SYSTEMCTL_VERBS:
        raise Deny("systemctl: только просмотр: " + ", ".join(sorted(SYSTEMCTL_VERBS)))
    for unit in positional[1:]:
        if not UNIT.match(unit):
            raise Deny("systemctl: недопустимое имя юнита: " + unit)
    return list(args)


JOURNAL_MATCH = re.compile(r"^([A-Z_][A-Z0-9_]*=.+|\+|[+-]?\d+|/[\w./+-]+)$", re.ASCII)
PRINTABLE_ASCII = re.compile(r"^[\x20-\x7e]*$")
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
JOURNAL_LINES = re.compile(r"^(\+?\d+|all)$", re.ASCII)


def _journalctl(args: List[str]) -> List[str]:
    # never allowed: --vacuum-*, --rotate, --flush, --(smart-)relinquish-var, --sync, --setup-keys,
    # --update-catalog, --cursor-file, --file, -D/--directory, --root, --image, -f/--follow, -M/--machine
    opts, positional = parse_opts(
        args, "abekmqrxlN", "unpoSUtgFc",
        ("--no-pager", "--all", "--full", "--no-full", "--reverse", "--merge", "--utc", "--no-hostname",
         "--catalog", "--quiet", "--dmesg", "--list-boots", "--disk-usage", "--pager-end", "--system",
         "--fields", "--show-cursor"),
        ("--unit", "--user-unit", "--priority", "--output", "--since", "--until", "--identifier", "--grep",
         "--field", "--cursor", "--after-cursor", "--facility", "--output-fields", "--case-sensitive",
         "--exclude-identifier"),
        ("--boot", "--lines"),
        numbers=True)
    for opt, value in opts:
        if value is None:
            continue
        if "|" in value and opt not in ("-g", "--grep"):
            raise Deny(PIPE_WHY)
        # The value of journalctl -n is optional, so journalctl parses -n --rotate as two flags.
        if opt in ("-n", "--lines") and not JOURNAL_LINES.match(value):
            raise Deny("journalctl: -n — число или all: " + value)
        if value.startswith("-") and not NUMBER.match(value):
            raise Deny("journalctl: значение не может быть опцией: " + value)
        if opt in ("-g", "--grep"):  # free text (logs may be in any language): only control characters are refused
            if CONTROL_CHARS.search(value):
                raise Deny("journalctl: управляющие символы в значении " + opt)
        elif not PRINTABLE_ASCII.match(value):
            raise Deny("journalctl: значение — только печатный ASCII: " + opt)
    for word in positional:
        if "|" in word:
            raise Deny(PIPE_WHY)
        if not JOURNAL_MATCH.match(word):
            raise Deny("journalctl: недопустимый аргумент: " + word)
    return list(args)


def _last(args: List[str]) -> List[str]:
    _, positional = parse_opts(
        args, "aFiwxdR", "nst", ("--fullnames", "--fulltimes", "--ip", "--dns", "--system", "--nohostname"),
        ("--limit", "--since", "--until", "--present"), numbers=True)
    for word in positional:
        if not re.match(r"^[A-Za-z0-9_.:-]+$", word, re.ASCII):
            raise Deny("last: недопустимый аргумент: " + word)
    return list(args)


def _lsblk(args: List[str]) -> List[str]:
    _, positional = parse_opts(
        args, "abdDfJlmnpPrSstz", "oe",
        ("--all", "--bytes", "--nodeps", "--discard", "--fs", "--json", "--list", "--perms", "--noheadings",
         "--paths", "--pairs", "--raw", "--scsi", "--topology", "--tree"),
        ("--output", "--exclude"))
    for device in positional:
        if not re.match(r"^/dev/[\w/-]+$", device, re.ASCII):
            raise Deny("lsblk: только устройства /dev/…: " + device)
    return list(args)


VALIDATORS.update({
    "uptime": _flags_only("uptime", "ps", ("--pretty", "--since")),
    "free": _flags_only("free", "bkmghltvw", ("--bytes", "--kilo", "--mega", "--giga", "--human", "--si",
                                              "--lohi", "--total", "--wide", "--committed")),
    "uname": _flags_only("uname", "asnrvmpio", ("--all",)),
    "who": _flags_only("who", "abdHlmpqrsTu", ("--all", "--boot", "--heading", "--count")),
    "last": _last,
    "lsblk": _lsblk,
    "df": _df,
    "ps": _ps,
    "top": _top,
    "ss": _ss,
    "ip": _ip,
    "systemctl": _systemctl,
    "journalctl": _journalctl,
    "crontab": _only("crontab", [["-l"]]),
    "apt": _only("apt", [["list", "--upgradable"]]),
    "needs-restarting": _only("needs-restarting", [["-r"]]),
    "dpkg": _only("dpkg", [["--audit"]]),  # These forms leave the package state untouched. Other dpkg forms can write.
})


def check(raw: str) -> Plan:
    """A line from SSH_ORIGINAL_COMMAND → Plan; everything not allowed is a Deny."""
    if not raw or not raw.strip():
        raise Deny("нужна команда: интерактивный вход запрещён")
    if len(raw) > 4096:
        raise Deny("команда слишком длинная: " + str(len(raw)) + " символов, максимум 4096")
    if SHELL_META.search(raw):
        raise Deny("метасимволы shell запрещены (; & $ ` и перевод строки; | — только внутри шаблона grep -e): "
                   "фильтруйте вывод на своей стороне")
    try:
        argv = shlex.split(raw)
    except ValueError as exc:
        raise Deny("не удалось разобрать команду: " + str(exc))
    name = argv[0]
    if name not in PIPE_COMMANDS and any("|" in arg for arg in argv):
        raise Deny(PIPE_WHY)
    sql_at = _mysql_sql_at(argv[1:])[0] if name == "mysql" else None
    exempt = None if sql_at is None else sql_at + 1  # the word carrying SQL, in argv (the index was counted from argv[1:])
    if name == "sqlite3" and len(argv) == 3:
        exempt = 2  # sqlite3 <file> "<SELECT…>"
    for i, arg in enumerate(argv):
        if REDIRECT_META.search(arg) and i != exempt:
            raise Deny("символы < и > запрещены (кроме SQL в mysql -e и sqlite3)")
    if "/" in name:
        raise Deny("команда — только имя, без пути: " + name)
    if tuple(argv) in SUDO_EXACT:
        return Plan(argv, True)
    validator = VALIDATORS.get(name)
    if validator is None:
        raise Deny("команды нет в белом списке: " + name + ". Разрешены: " + ", ".join(sorted(VALIDATORS)))
    return Plan([name] + validator(argv[1:]), False)


# --- files and paths -----------------------------------------------------------

ROOTS = (  # (root, whether its content may be read)
    ("/var/log", True), ("/etc", True), ("/var/www", True), ("/opt", True),
    ("/proc", True), ("/sys/class/net", True), ("/backups", False),
)
FORBIDDEN = tuple(re.compile(p, re.ASCII) for p in (
    r"^/etc/g?shadow",
    r"^/etc/ssh/ssh_host_[^/]*_key$",
    r"(^|/)private_key\.json$",
    r"(^|/)\.?htpasswd",  # and .htpasswd.bak, htpasswd-prod
    r"(^|/)\.git/(.+/)?config(\.worktree)?$",  # and .git/modules/<name>/config
    r"(^|/)\.envrc$",
    r"(^|/)vpn/[^/]*\.conf$",
    r"(^|/)\.ssh(/|$)",
    r"_ed25519",
    r"\.key$",
    r"(^|/)privkey[^/]*\.pem$",
    r"(^|/)[.#]?config\.ini([.~_#-][^/]*)?$",  # config.ini-prod, config.ini_bak, #config.ini#
    r"(^|/)\.env([.~_-][^/]*)?$",
    r"\.env$",
    r"(^|/)\.my(-data)?\.cnf$",
    r"(^|/)\.git-credentials$",
    r"(^|/)\.netrc$",
    r"^/etc/wireguard/.",
    r"^/etc/[^/]+-backup\.conf$",  # an application's backup credentials (<app>-backup.conf)
    r"^/etc/mysql/debian\.cnf$",
    r"^/etc/letsencrypt/(archive|keys)/",
    r"(^|/)openvpn/auth\.txt$",
    # and /proc/<pid>/task/<tid>/…; cmdline is NUL-separated, so the masks (spaces around an option) never see a
    # password in it — a process's arguments come only through ps, joined by spaces (review 04.11.2026)
    r"^/proc/(.+/)?(environ|root|cwd|fd|map_files|mem|cmdline|pagemap|stack|syscall|auxv)(/|$)",
    r"^/proc/(kcore|kmsg|kallsyms|kpagecount|kpageflags|kpagecgroup|vmcore|sysrq-trigger)$",  # memory, a blocking log
    r"^/root(/|$)",
    r"^/home(/|$)",
))
# A deployment's own secret paths (its applications' credential files, journals of their APIs) are not in this
# public list: target_setup.sh --forbidden installs them as a root-owned JSON file of regular expressions,
# {"schema_version": 1, "forbidden": ["(^|/)my_app_keys\\.json$", …]}. A file that is not root's, is writable by
# others, or does not parse refuses every path: the gate never runs with half of its list.
EXTRA_FORBIDDEN = "/etc/agent-ro-gate/forbidden.json"
_extra = None  # type: Optional[tuple]


def extra_forbidden(path: str = EXTRA_FORBIDDEN) -> tuple:
    """The deployment's patterns (compiled), () without the file; Deny when the file cannot be trusted."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return ()
    except OSError:
        raise Deny("список запретов внедрения недоступен: " + path)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
        raise Deny("список запретов внедрения не root-овский или открыт на запись: " + path)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if not (isinstance(data, dict) and data.get("schema_version") == 1 and isinstance(data.get("forbidden"), list)
                and all(isinstance(p, str) and p for p in data["forbidden"])):
            raise ValueError("shape")
        return tuple(re.compile(p, re.ASCII) for p in data["forbidden"])
    except (OSError, ValueError, re.error):
        raise Deny("список запретов внедрения не читается: " + path)


def _forbidden() -> tuple:
    global _extra
    if _extra is None:
        _extra = extra_forbidden(EXTRA_FORBIDDEN)
    return FORBIDDEN + _extra


# Where symlinks from the roots lead legitimately: /etc/resolv.conf → /run/systemd, units → /usr/lib,
# certificates → /usr/share, /sys/class/net/* → /sys/devices. There are no secrets there.
REAL_EXTRA = ("/run/systemd", "/usr/lib", "/lib", "/usr/share", "/sys/devices")
# Single files outside the roots — the exact path only and no symlink: "is a reboot needed" on servers without
# needrestart; the rest of /run (sockets, /run/user, service secrets) is closed.
EXACT_FILES = ("/run/reboot-required",)
COUNT_OPTS = {"-n", "--lines"}  # head/tail -c/--bytes are forbidden: a cut by bytes tears a URL with a password in the middle


def realpath(path: str) -> str:
    """The path with symlinks resolved; on Windows (local tests) — normalization only."""
    if os.name != "posix":
        return posixpath.normpath(path)
    try:
        return os.path.realpath(path)
    except OSError:
        raise Deny("путь недоступен: " + path)


def _within(path: str, roots: Sequence[str]) -> bool:
    return any(path == root or path.startswith(root + "/") for root in roots)


def _clean(path: str) -> str:
    return posixpath.normpath(re.sub("/+", "/", path))


def check_path(path: str, need_content: bool) -> None:
    if not path.startswith("/"):
        raise Deny("нужен абсолютный путь: " + path)
    if ".." in path.split("/"):
        raise Deny("'..' в пути запрещено: " + path)
    clean = _clean(path)
    real = realpath(clean)
    for candidate in (clean, real):
        for pattern in _forbidden():
            if pattern.search(candidate):
                raise Deny("путь запрещён: " + path)
    if clean in EXACT_FILES and real == clean:
        return
    root = next(((r, content) for r, content in ROOTS if _within(clean, [r])), None)
    if root is None:
        raise Deny("путь вне разрешённых каталогов (" + ", ".join(r for r, _ in ROOTS) + "): " + path)
    if need_content and not root[1]:
        raise Deny("в " + root[0] + " разрешён только листинг: " + path)
    readable = [r for r, content in ROOTS if content or not need_content]
    if not _within(real, readable + list(REAL_EXTRA)):
        raise Deny("симлинк ведёт за пределы разрешённых каталогов: " + path + " → " + real)


def _files(name: str, flags: str, valued: str = "", long_flags: Sequence[str] = (),
           long_valued: Sequence[str] = (), long_optional: Sequence[str] = (),
           need_content: bool = True, counts: bool = False) -> Validator:
    def validate(args: List[str]) -> List[str]:
        opts, positional = parse_opts(args, flags, valued, long_flags, long_valued, long_optional)
        for opt, value in opts:
            if counts and opt in COUNT_OPTS and value is not None and not NUMBER.match(value):
                raise Deny(name + ": " + opt + " — только число: " + value)
        if not positional:
            raise Deny(name + ": укажите абсолютный путь")
        for path in positional:
            check_path(path, need_content)
        return list(args)
    return validate


FIND_FLAGS = {"-print", "-print0", "-ls", "-empty", "-xdev", "-mount", "-not", "!", "-o", "-or", "-a", "-and",
              "(", ")", "-prune", "-nouser", "-nogroup", "-daystart", "-depth", "-readable", "-executable",
              "-true", "-false", "-quit"}
FIND_VALUED = {"-name", "-iname", "-path", "-ipath", "-wholename", "-iwholename", "-regex", "-iregex",
               "-regextype", "-type", "-xtype", "-mtime", "-mmin", "-atime", "-amin", "-ctime", "-cmin", "-size",
               "-user", "-group", "-uid", "-gid", "-perm", "-links", "-inum", "-maxdepth", "-mindepth", "-printf",
               "-newer", "-samefile"}
FIND_PATH_VALUED = {"-newer", "-samefile"}


def _find(args: List[str]) -> List[str]:
    i = 0
    starts: List[str] = []
    while i < len(args) and not (args[i].startswith("-") or args[i] in ("!", "(", ")")):
        starts.append(args[i])
        i += 1
    if not starts:
        raise Deny("find: укажите абсолютный путь")
    has_maxdepth = False
    while i < len(args):
        word = args[i]
        if word in FIND_FLAGS:
            i += 1
            continue
        if word not in FIND_VALUED:
            raise Deny("find: выражение не разрешено: " + word)
        if i + 1 >= len(args):
            raise Deny("find: нет значения у " + word)
        value = args[i + 1]
        if word == "-maxdepth":
            if not re.match(r"^[0-9]+$", value):
                raise Deny("find: -maxdepth — только число")
            has_maxdepth = True
        if word in FIND_PATH_VALUED:
            check_path(value, need_content=False)
        i += 2
    if not has_maxdepth:
        raise Deny("find: обязательно -maxdepth N")
    for path in starts:
        check_path(path, need_content=False)
    return list(args)


VALIDATORS.update({
    "cat": _files("cat", "AbeEnsTtuv"),
    "head": _files("head", "qvz", "n", ("--quiet", "--verbose", "--zero-terminated"), ("--lines",),
                   counts=True),
    "tail": _files("tail", "qvz", "n", ("--quiet", "--verbose", "--zero-terminated"), ("--lines",),
                   counts=True),
    "ls": _files(
        "ls", "aAbBcCdfFgGhHiklLmnNopqQrRsStuUvxXZ1", "IwT",
        ("--all", "--almost-all", "--human-readable", "--si", "--inode", "--numeric-uid-gid", "--recursive",
         "--reverse", "--size", "--directory", "--classify", "--full-time", "--group-directories-first",
         "--dereference", "--no-group", "--author", "--escape", "--literal"),
        ("--time-style", "--sort", "--format", "--time", "--block-size", "--ignore", "--width",
         "--indicator-style", "--quoting-style"),
        ("--color",), need_content=False),
    "stat": _files("stat", "Lft", "c", ("--dereference", "--file-system", "--terse"), ("--format", "--printf"),
                   need_content=False),
    "du": _files(
        "du", "abchHklmsSxLD0", "dBt",
        ("--all", "--apparent-size", "--bytes", "--total", "--human-readable", "--si", "--summarize",
         "--one-file-system", "--count-links", "--dereference", "--separate-dirs", "--null", "--time"),
        ("--max-depth", "--block-size", "--threshold", "--exclude", "--time-style"), need_content=False),
    "find": _find,
})


# --- search in files: grep, zgrep, zcat -------------------------------------------
# The output window is the same 1 MB, but grep searches the whole file: a line past the first megabyte is found;
# to read on — grep -A N or tail -n +N. A closed list of options; -r/-R/-d/-D/-f/-P/--include/--exclude and the
# rest are refused.

# there is no -o/--only-matching: the masks do not recognize a fragment without "name=", "://user:" or "Bearer"
GREP_FLAGS = "FEinchHlvwxas"
GREP_VALUED = "mABCe"
GREP_LONG_FLAGS = ("--fixed-strings", "--extended-regexp", "--ignore-case", "--line-number", "--count",
                   "--no-filename", "--with-filename", "--files-with-matches", "--invert-match", "--word-regexp",
                   "--line-regexp", "--text", "--no-messages")
GREP_LONG_VALUED = ("--max-count", "--after-context", "--before-context", "--context", "--regexp")
GREP_PATTERNS = ("-e", "--regexp")
GREP_COUNTS = ("-m", "-A", "-B", "-C", "--max-count", "--after-context", "--before-context", "--context")
GREP_SYNTAX = ("-F", "-E", "--fixed-strings", "--extended-regexp")
DIGITS = re.compile(r"^[0-9]+$", re.ASCII)
LOG_ROOT = "/var/log"  # zcat/zgrep — only here; outside it grep first looks for PRIVATE KEY in the file
GZ_PATH = re.compile(r"^/[A-Za-z0-9_.,:@+/-]+\.gz$", re.ASCII)  # no spaces or quotes in the name (CVE-2022-1271)
PEM_MARK = b"PRIVATE KEY"


def _grep_parse(args: Sequence[str]) -> Tuple[Opts, List[str]]:
    return parse_opts(args, GREP_FLAGS, GREP_VALUED, GREP_LONG_FLAGS, GREP_LONG_VALUED)


def _grep_args(name: str, args: List[str]) -> Tuple[List[str], List[str]]:
    """(options for grep, files): the options in normal form, the pattern always through -e, without -F — with -E."""
    opts, positional = _grep_parse(args)
    words: List[str] = []
    patterns = 0
    for opt, value in opts:
        if value is None:
            words.append(opt)
            continue
        if opt in GREP_COUNTS and not DIGITS.match(value):
            raise Deny(name + ": " + opt + " — только число: " + value)
        if opt in GREP_PATTERNS:
            patterns += 1
            if CONTROL_CHARS.search(value):
                raise Deny(name + ": управляющие символы в шаблоне")
        elif "|" in value:
            raise Deny(PIPE_WHY)
        words.extend([opt, value])
    if not patterns:
        if not positional:
            raise Deny(name + ": нужен шаблон и путь: " + name + " -F \"текст\" /var/log/…")
        pattern, positional = positional[0], positional[1:]
        if "|" in pattern:
            raise Deny(name + ": | в шаблоне — только через -e: " + name + " -e \"a|b\" …")
        if CONTROL_CHARS.search(pattern):
            raise Deny(name + ": управляющие символы в шаблоне")
        words.extend(["-e", pattern])
    syntax = {opt.lstrip("-")[0].upper() for opt, _ in opts if opt in GREP_SYNTAX}
    if len(syntax) > 1:
        raise Deny(name + ": -F и -E вместе нельзя: фиксированная строка или регулярное выражение")
    if not syntax:
        words.insert(0, "-E")  # the pattern is a fixed string (-F) or -E; "a|b" is an alternation
    if not positional:
        raise Deny(name + ": укажите абсолютный путь")
    for path in positional:
        if "|" in path:
            raise Deny(PIPE_WHY)
    return words, positional


def _holds_private_key(path: str, deadline: float) -> bool:
    """A regular file contains "PRIVATE KEY". Unreadable or not a regular file (a directory, a FIFO) — False: grep says so itself."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return False
        tail = b""
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                return False
            if PEM_MARK in tail + chunk:
                return True
            tail = chunk[-(len(PEM_MARK) - 1):]
            if time.monotonic() > deadline:
                raise Deny("файл слишком большой для проверки на приватный ключ за %d с: %s" % (TIMEOUT, path))
    except OSError:
        return False
    finally:
        os.close(fd)


def _grep(args: List[str]) -> List[str]:
    words, paths = _grep_args("grep", args)
    deadline = time.monotonic() + TIMEOUT
    for path in paths:
        check_path(path, need_content=True)
        if not _within(realpath(_clean(path)), [LOG_ROOT]) and _holds_private_key(path, deadline):
            # grep returns lines one at a time: a PEM body line without BEGIN is invisible to redact_counted's masks
            raise Deny("grep: в файле приватный ключ (PRIVATE KEY) — grep мог бы вывести тело ключа без строки "
                       "BEGIN, маска его не скроет: " + path)
    return words + ["--"] + paths


# a test environment's journals (the "read" directories of its catalog entry) are archived as *.gz beside
# the application; zcat/zgrep read them too. The list is the exec gate's root-owned file of this server; anything
# wrong with it — only /var/log, as before.
TEST_ENVS_JSON = "/etc/agent-exec-gate/test-envs.json"
TEST_ENVS_OWNER = 0
_READ_DIR = re.compile(r"^(/[A-Za-z0-9_][A-Za-z0-9._-]*)+$", re.ASCII)


def test_env_reads(path: Optional[str] = None) -> Tuple[str, ...]:
    """The directories this server's test environments open for reading; () without a trustworthy list."""
    path = TEST_ENVS_JSON if path is None else path
    try:
        st = os.lstat(path)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != TEST_ENVS_OWNER or st.st_mode & 0o022
                or st.st_size > 64 * 1024):
            return ()
        with open(path, encoding="utf-8") as handle:
            envs = json.load(handle)["envs"]
        if not all(isinstance(env.get("read", []), list) for env in envs.values()):
            return ()
        reads = tuple(sorted({env["dir"] + "/" + read for env in envs.values() for read in env.get("read", [])}))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return ()
    return reads if all(_READ_DIR.match(r) and ".." not in r.split("/") for r in reads) else ()


def check_gz(path: str) -> None:
    """zcat/zgrep: only *.gz under /var/log or in the read directories of test environments — both the path and the
    symlink's target — plus the ordinary check_path."""
    check_path(path, need_content=True)
    clean = _clean(path)
    real = realpath(clean)
    roots = [LOG_ROOT] + list(test_env_reads())
    if not (_within(clean, roots) and _within(real, roots)):
        raise Deny("zcat/zgrep — только файлы *.gz под " + LOG_ROOT + " и в журналах тестовых окружений: " + path
                   + ("" if clean == real else " → " + real))
    if not (GZ_PATH.match(clean) and GZ_PATH.match(real)):
        raise Deny("zcat/zgrep — только файлы *.gz (имя без пробелов и кавычек): " + path)


def _zgrep(args: List[str]) -> List[str]:
    words, paths = _grep_args("zgrep", args)
    for path in paths:
        check_gz(path)
    return words + ["--"] + paths


def _zcat(args: List[str]) -> List[str]:
    if not args:
        raise Deny("zcat: укажите путь: zcat /var/log/….gz")
    for arg in args:
        if arg.startswith("-"):
            raise Deny("zcat: опции не разрешены: " + arg)
        check_gz(arg)
    return ["-cd", "--"] + list(args)  # runs as gzip -cd: without zcat's shell wrapper


_TAIL = VALIDATORS["tail"]


def _tail(args: List[str]) -> List[str]:
    """tail as before, and like grep it never reads a file with a private key outside /var/log: tail starts where
    it is told (-n +2, -c), past the BEGIN line PEM_RE masks from — cat and head always start at the top
    (review 04.11.2026)."""
    out = _TAIL(args)
    _, paths = parse_opts(args, "qvz", "n", ("--quiet", "--verbose", "--zero-terminated"), ("--lines",))
    deadline = time.monotonic() + TIMEOUT
    for path in paths:
        if not _within(realpath(_clean(path)), [LOG_ROOT]) and _holds_private_key(path, deadline):
            raise Deny("tail: в файле приватный ключ (PRIVATE KEY) — tail мог бы вывести тело ключа без строки "
                       "BEGIN, маска его не скроет: " + path)
    return out


VALIDATORS.update({"grep": _grep, "zgrep": _zgrep, "zcat": _zcat, "tail": _tail})
RUN_AS = {"zcat": "gzip", "zgrep": "grep"}  # zgrep is grep over decompression inside the gateway (run_zgrep), without the zgrep script


# --- MySQL ---------------------------------------------------------------------

MY_CNF = "/home/agent-ro/.my.cnf"
SQL_TIMEOUT_MS = 30000  # a runaway SELECT in production is a known class of incidents
SQL_READ = re.compile(r"^\s*(select|show|explain|describe|desc)\b", re.I)
SQL_FORBIDDEN = re.compile(
    r"\binto\s+(outfile|dumpfile|@)|\bfor\s+(update|share)\b|\block\s+in\s+share\s+mode\b"
    r"|\b(load_file|benchmark|sleep|sys_exec|sys_eval)\s*\(|\bload\s+data\b",
    re.I)
SQL_COMMENT = re.compile(r"/\*|#|--(\s|$)")
DB_NAME = re.compile(r"^[A-Za-z0-9_]+$")


def _mysql(args: List[str]) -> List[str]:
    opts, positional = parse_opts(
        args, "tNBEsrHX", "eD",
        ("--table", "--batch", "--vertical", "--skip-column-names", "--silent", "--raw", "--html", "--xml"),
        ("--execute", "--database"))
    sqls = [value or "" for opt, value in opts if opt in ("-e", "--execute")]
    if len(sqls) != 1:
        raise Deny('mysql: нужен ровно один -e "SELECT…"')
    if "\\" in sqls[0]:
        # the mysql client runs \! (shell), \T (tee into a file), \. (source) even inside -e
        raise Deny("mysql: символ \\ запрещён (клиентские команды mysql); для вертикального вывода — -E")
    if SQL_COMMENT.search(sqls[0]):
        # /**/ breaks the whitespace parts of SQL_FORBIDDEN (sleep/**/(5)), /*+ … */ lifts MAX_EXECUTION_TIME
        raise Deny("mysql: комментарии в SQL (/* */, #, -- ) запрещены")
    if not SQL_READ.match(sqls[0]):
        raise Deny("mysql: только SELECT, SHOW, EXPLAIN, DESCRIBE; изменения выполняет владелец")
    if SQL_FORBIDDEN.search(sqls[0]):
        raise Deny("mysql: INTO OUTFILE/DUMPFILE/@, FOR UPDATE/SHARE, LOCK IN SHARE MODE, LOAD DATA, load_file(), "
                   "sleep(), benchmark() запрещены")
    databases = [value or "" for opt, value in opts if opt in ("-D", "--database")] + positional
    if len(positional) > 1 or not all(DB_NAME.match(db) for db in databases):
        raise Deny("mysql: недопустимое имя базы")
    return ["--defaults-file=" + MY_CNF, "--init-command=SET SESSION MAX_EXECUTION_TIME=%d" % SQL_TIMEOUT_MS] + list(args)


VALIDATORS["mysql"] = _mysql

# --- SQLite ----------------------------------------------------------
# sqlite3 <file> "<SELECT…>": the gate reads the file itself with Python's module — a target needs no sqlite3
# binary, and the boundary is SQLite's own: the file is opened read-only and the authorizer lets through reads
# only (no write, ATTACH, PRAGMA or extension), whatever the text of the statement says.

SQLITE_TIMEOUT = 30
# One value can be built whole inside SQLite (zeroblob, randomblob, printf) before the gate sees a byte of it, and
# no step counter runs meanwhile: the read's process gets a ceiling on its address space instead.
SQLITE_MEMORY = 512 * 1024 * 1024
SQLITE_READ = re.compile(r"^\s*(select|explain|with)\b", re.I)
# sqlite3's action codes (sqlite3.h): SQLITE_READ, SQLITE_SELECT, SQLITE_FUNCTION, SQLITE_RECURSIVE — by number,
# Python 3.6 names only some of them
_SQLITE_ALLOWED = frozenset((20, 21, 31, 33))
_SQLITE_FUNCTION = 31
_CELL_ESCAPES = {"\\": "\\\\", "\t": "\\t", "\n": "\\n", "\r": "\\r"}


def _sqlite(args: List[str]) -> List[str]:
    if len(args) != 2:
        raise Deny('sqlite3: нужны файл и один запрос: sqlite3 /путь/к/файлу "SELECT …"')
    path, sql = args
    check_path(path, need_content=True)
    if not SQLITE_READ.match(sql):
        raise Deny("sqlite3: только SELECT (и EXPLAIN, WITH … SELECT); список таблиц — "
                   "SELECT name FROM sqlite_master")
    return [path, sql]


VALIDATORS["sqlite3"] = _sqlite


def _sqlite_cell(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bytes):
        return "x'%s'" % value.hex()
    if isinstance(value, str):
        return re.sub(r"[\\\t\n\r]", lambda m: _CELL_ESCAPES[m.group()], value)
    return str(value)


def _cap_memory(limit: int = SQLITE_MEMORY) -> None:
    """A ceiling on this process's address space, for good: the gate is one command long."""
    try:
        import resource

        hard = resource.getrlimit(resource.RLIMIT_AS)[1]
        if hard != resource.RLIM_INFINITY:
            limit = min(limit, hard)
        resource.setrlimit(resource.RLIMIT_AS, (limit, hard))
    except (ImportError, OSError, ValueError):
        pass  # no such limit here: the size checks of the read still hold


def _sqlite_size(row) -> int:
    return sum(len(cell) for cell in row if isinstance(cell, (bytes, str)))


def run_sqlite(path: str, sql: str, timeout: float = SQLITE_TIMEOUT, limit: int = LIMIT) -> Result:
    """One reading statement over a SQLite file: a header line and the rows, cells joined by a tab."""
    import pathlib
    import sqlite3

    def fail(reason: str, timed_out: bool = False) -> Result:
        return Result(1, ("sqlite3: %s\n" % reason).encode("utf-8"), False, timed_out)

    if not os.path.isfile(path):
        return fail("файла нет или это не обычный файл: " + path)
    state = {"timed_out": False}
    deadline = time.monotonic() + timeout

    def progress() -> int:
        if time.monotonic() > deadline:
            state["timed_out"] = True
            return 1
        return 0

    def authorizer(action, first, second, database, source) -> int:
        if action == _SQLITE_FUNCTION and (second or "").lower() == "load_extension":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK if action in _SQLITE_ALLOWED else sqlite3.SQLITE_DENY

    conn = None
    chunks, size, truncated = [], 0, False
    try:
        conn = sqlite3.connect(pathlib.Path(path).as_uri() + "?mode=ro", uri=True, timeout=1, isolation_level=None)
        conn.text_factory = lambda raw: raw.decode("utf-8", "replace")
        conn.set_progress_handler(progress, 10000)
        conn.set_authorizer(authorizer)
        cursor = conn.execute(sql)
        row = [column[0] for column in cursor.description or ()]
        while row is not None:
            if size + _sqlite_size(row) > limit:  # before a large cell is copied into text
                truncated = True
                break
            line = ("\t".join(_sqlite_cell(cell) for cell in row) + "\n").encode("utf-8")
            if size + len(line) > limit:
                truncated = True
                break
            chunks.append(line)
            size += len(line)
            row = cursor.fetchone()
    except MemoryError:
        chunks = []
        return fail("результат слишком велик для шлюза")
    except (sqlite3.Error, sqlite3.Warning, ValueError) as exc:
        if state["timed_out"]:
            return Result(1, b"".join(chunks), False, True)
        reason = str(exc)
        if "not authorized" in reason or "readonly" in reason:
            reason = "запрос не разрешён: шлюз только читает (%s)" % reason
        return fail(reason)
    finally:
        if conn is not None:
            conn.close()
    return Result(0, b"".join(chunks), truncated, False)


# the data key's forced command is `agent-ro-gate --data`: mysql only, as the MySQL user of kd-data (no
# secret columns, 2 connections), with its own shorter limit; the same checks, masking and 1 MB as above.
DATA_CNF = "/home/agent-ro/.my-data.cnf"
DATA_TIMEOUT = 35


def check_data(raw: str) -> Plan:
    """SSH_ORIGINAL_COMMAND of the data key → Plan; anything but one checked mysql statement — Deny."""
    plan = check(raw)
    if plan.sudo or plan.argv[0] != "mysql":
        raise Deny("ключ данных пускает только к mysql: один SELECT, SHOW, EXPLAIN или DESCRIBE")
    if not plan.argv[1].startswith("--defaults-file="):
        raise Deny("mysql: нет файла пользователя")
    return Plan([plan.argv[0], "--defaults-file=" + DATA_CNF] + plan.argv[2:], False)


# --- commands as root: exact forms through sudo only ---------------------------

def _sudo_only(name: str) -> Validator:
    def validate(args: List[str]) -> List[str]:
        forms = "\n".join("  " + " ".join(form) for form in SUDO_EXACT if form[0] == name)
        raise Deny(name + ": только точные формы:\n" + forms)
    return validate


for _name in sorted({form[0] for form in SUDO_EXACT} - set(VALIDATORS)):
    VALIDATORS[_name] = _sudo_only(_name)


def _sudo_escape(arg: str) -> str:
    return re.sub(r"([,:=\\])", r"\\\1", arg)


def _digest(path: str) -> Optional[str]:
    """The sha256 of the installed file (hex) for sudoers' Digest_Spec; None — the file cannot be read."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def sudoers_lines(which=shutil.which, digest=_digest) -> List[str]:
    """The lines of /etc/sudoers.d/agent-ro: exact forms only; none for a command the server does not have.

    The binaries of SUDO_PATHS live in /usr/local/bin (root:staff 2775 on Ubuntu — a member of staff can replace the
    file, and sudo does not check the file's owner), so their line carries a Digest_Spec `sha256:<hex>` of the content
    computed now: sudo will not run a replaced file. The file could not be read — there is no line."""
    lines = ["# agent-ro-gate --sudoers: точные команды с root для шлюза agent-ro (спека §4.3)"]
    for form in SUDO_EXACT:
        binary = _binary(form[0], which)
        if not binary:
            continue
        args = [_sudo_escape(a) for a in form[1:]] or ['""']  # "" means "no arguments", otherwise sudo allows any
        command = " ".join([binary] + args)
        if form[0] in SUDO_PATHS:
            hexdigest = digest(binary)
            if not hexdigest or not re.fullmatch(r"[0-9a-f]{64}", hexdigest):
                continue
            command = "sha256:" + hexdigest + " " + command  # the Digest_Spec goes right before the command, without _sudo_escape
        lines.append("agent-ro ALL=(root) NOPASSWD: " + command)
    return lines


def materialize(plan: Plan, which=shutil.which) -> List[str]:
    binary = _binary(RUN_AS.get(plan.argv[0], plan.argv[0]), which)
    if not binary:
        raise Deny("на этом сервере нет команды: " + plan.argv[0])
    argv = [binary] + plan.argv[1:]
    if plan.sudo:
        argv = [which("sudo", path=SAFE_PATH) or "/usr/bin/sudo", "-n"] + argv
    return argv


# --- execution -----------------------------------------------------------------

def _signal(proc: "subprocess.Popen", sig: int) -> None:
    """The signal goes to the process's whole group (it leads its own session), otherwise to it alone."""
    try:
        os.killpg(proc.pid, sig)
    except AttributeError:  # Windows: local tests.
        proc.send_signal(sig)
    except (ProcessLookupError, PermissionError):
        try:
            proc.send_signal(sig)
        except OSError:
            pass


def _stop(proc: "subprocess.Popen") -> None:
    if proc.poll() is not None:
        return  # it ended by itself: the descendants it left (daemons of an approved command) are not touched
    _signal(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    _signal(proc, getattr(signal, "SIGKILL", signal.SIGTERM))
    proc.wait()


def run_limited(argv: List[str], timeout: float = TIMEOUT, limit: int = LIMIT,
                env: Optional[Dict[str, str]] = None, kill_on_limit: bool = True,
                feed: Optional[Iterable[bytes]] = None) -> Result:
    """Run without a shell; the result holds at most limit bytes of output (stdout+stderr), the time is at most timeout.

    The reader thread only reads; the main thread controls the process. On overflow:
    kill_on_limit=True (the gateway) — the process is stopped; False (ssh-exec) — the extra output
    is read and dropped, the command runs to its end (otherwise an approved dpkg/rsync would die
    halfway of SIGPIPE). A timeout and any exception (one from a signal handler too)
    stop the process and its whole group (start_new_session + killpg, no orphaned descendants): SIGTERM, SIGKILL 3 s later. Used by the gateway and ssh_exec.py.
    feed — chunks for stdin (zgrep: decompression inside the gateway); otherwise stdin is /dev/null.
    """
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL if feed is None else subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, start_new_session=True)
    chunks: List[bytes] = []
    state = {"size": 0, "truncated": False}

    def push() -> None:
        try:
            for piece in feed:
                proc.stdin.write(piece)
        except OSError:  # BrokenPipe: grep has finished (-m, -l) or was stopped
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    writer = None
    if feed is not None:
        writer = threading.Thread(target=push)
        writer.daemon = True
        writer.start()

    def pump() -> None:
        while True:
            chunk = proc.stdout.read1(65536)
            if not chunk:
                return
            room = limit - state["size"]
            if room > 0:
                chunks.append(chunk[:room])
                state["size"] += min(len(chunk), room)
            if len(chunk) > room:
                state["truncated"] = True
                if kill_on_limit:
                    return

    reader = threading.Thread(target=pump)
    reader.daemon = True
    reader.start()
    deadline = time.monotonic() + timeout
    timed_out = False
    try:
        while proc.poll() is None:
            if time.monotonic() >= deadline:
                timed_out = True
                break
            if kill_on_limit and state["truncated"]:
                break
            if reader.is_alive():
                reader.join(0.1)
            else:
                time.sleep(0.1)
    finally:
        _stop(proc)  # already ended — does nothing
    reader.join(5)  # a grandchild may have inherited the pipe and hold it — it is not waited for longer than 5 s
    if writer is not None:
        writer.join(5)
    code = proc.returncode if proc.returncode is not None else -9
    return Result(code, b"".join(chunks[:]), bool(state["truncated"]), timed_out)


def _gunzip(path: str, errors: List[str]) -> Iterator[bytes]:
    """Decompress a .gz in chunks; a failure (not gzip, truncated) goes to errors, the stream simply ends."""
    try:
        with gzip.open(path, "rb") as f:
            while True:
                piece = f.read(65536)
                if not piece:
                    return
                yield piece
    except (OSError, EOFError, zlib.error) as exc:
        errors.append("%s: ошибка распаковки: %s" % (path, exc))


def run_zgrep(command: List[str], timeout: float = TIMEOUT, limit: int = LIMIT,
              env: Optional[Dict[str, str]] = None) -> Result:
    """zgrep without the zgrep script (CVE-2022-1271): the gateway itself decompresses each file into grep's stdin.

    command is materialize() of the zgrep plan: grep, options, "--", files. The file name goes through --label; with
    several files and no -h — with -H, as zgrep does. The timeout and the output limit are shared by all files. Exit
    code: 2 — a failure in at least one file, otherwise 0 if anything matched anywhere, otherwise 1."""
    opts, files = _grep_parse(command[1:])
    prefix = command[:len(command) - len(files) - 1]  # without "--" and the files
    if len(files) > 1 and not any(opt in ("-h", "--no-filename") for opt, _ in opts):
        prefix = prefix + ["-H"]
    deadline = time.monotonic() + timeout
    output: List[bytes] = []
    size, codes, truncated, timed_out = 0, [], False, False
    for path in files:
        left = deadline - time.monotonic()
        if left <= 0:
            timed_out = True
            break
        errors: List[str] = []
        result = run_limited(prefix + ["--label=" + path], timeout=left, limit=limit - size, env=env,
                             feed=_gunzip(path, errors))
        piece = result.output + "".join("zgrep: " + e + "\n" for e in errors).encode("utf-8", "replace")
        piece = piece[:limit - size]
        output.append(piece)
        size += len(piece)
        codes.append(2 if errors else result.returncode)
        if result.timed_out:
            timed_out = True
            break
        if result.truncated or size >= limit:
            truncated = True
            break
    code = 2 if any(c not in (0, 1) for c in codes) else 0 if 0 in codes else 1
    return Result(code, b"".join(output), truncated, timed_out)


# --- masking secrets in the output ------------------------------------------------
# One point: every byte the gateway returns to the agent goes through redact_counted (main).
# Byte regular expressions, no decode: non-UTF-8 output does not break.

MASK = b"***"
_PEM_MASKED = b"-----BEGIN PRIVATE KEY-----***-----END PRIVATE KEY-----"
PEM_RE = re.compile(
    rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)", re.S)
# The scheme is bounded by {0,31}: without an upper bound the search for "://" over a long alphanumeric run is
# quadratic.
URL_CREDS_RE = re.compile(rb"([A-Za-z][A-Za-z0-9+.-]{0,31}://)[^\s/@:'\"]*:[^\s/@'\"]*@")
BEARER_RE = re.compile(rb"(\bbearer[ \t]+)[A-Za-z0-9._~+/=-]{8,}", re.I)
BASIC_RE = re.compile(rb"(\bauthorization[\"']?[ \t]*[:=][ \t]*[\"']?basic[ \t]+)[A-Za-z0-9._~+/=-]+", re.I)
# A Telegram bot token; in a URL it is "bot<id>:<token>" — a letter on the left, hence "not a digit" rather than \b.
TELEGRAM_RE = re.compile(rb"(?<![0-9])\d{8,10}:[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])")
# mysql -pPASSWORD (no space) in a line with mysql/mysqldump/mysqladmin; a bare -p (a password prompt) is left alone.
# Lines with mysql are selected first, then -p is looked for inside the line: a lazy search from every mysql would
# be quadratic.
MYSQL_LINE_RE = re.compile(rb"^[^\n]*\bmysql(?:dump|admin)?\b[^\n]*$", re.M)
MYSQL_P_RE = re.compile(rb"([ \t]-p)[^\s'\"-][^\s'\"]*")
# -p PASSWORD / --password PASSWORD separated by a space in command lines (CGroup of systemctl status, ps aux): this
# is how a proxy daemon on one target showed its password. A number of up to 5 digits is left alone — it is a port
# or a pid (ssh -p 22, ps -p 1234); a value starting with "-" is the next flag, a bare -p (a password prompt) stays.
OPT_P_RE = re.compile(
    rb"(?<![A-Za-z0-9_.-])(-p|--pass(?:w(?:or)?d)?)([ \t]+)(?!-)(?![0-9]{1,5}(?![^\s'\"]))[^\s'\"]+")
# curl -u user:pass / --user user:pass / --user=user:pass (ps aux, CGroup, the journal): the password after the colon.
# A bare -u <name> without a colon (ps -u root, id -u, useradd -u 1000, sudo -u digi) is left alone.
OPT_U_RE = re.compile(rb"(?<![A-Za-z0-9_.-])(-u|--user)([ \t]+|=)([^\s'\":=]+:)[^\s'\"]+")
# The edge of the output: the cut at LIMIT (1 MB) can tear a URL with a password at the end — the fragment is hidden.
# The start of the output is always a whole line (head/tail without -c), so there is no rule for the start: it used
# to mask unit names like "app-agent@…".
EDGE_END_RE = re.compile(rb"://[^\s/@'\"]*\Z")
_SECRET_NAMES = (
    rb"client_secret|aws_secret_access_key|secret_key|access_token|auth_token|refresh_token|api[_-]?key|"
    rb"private_?key|presharedkey|signature|password|passwd|secret|token|pass|pwd|sig|key"
)
# name [quote] (= | => | : | "," only after a quote — PHP define('DB_PASSWORD', 'x')) value. Left of the name is
# not a letter or digit, so monkey=1 is intact while x_api_key=1 and --password=x are masked. Whitespace is only
# [ \t] (not \n): a "Password:" at the end of a line does not eat the next one, an unquoted "password, then …" is
# not a pair. The value: quoted — up to the closing quote (or the end of the line), unquoted — up to & space quote
# , ; }. Skipped: "(hidden)" (wg show), what is already hidden, and PEM (PEM_RE masks it).
KV_RE = re.compile(
    rb"(?<![A-Za-z0-9])(?P<name>" + _SECRET_NAMES + rb")(?P<q1>[\"'])?(?P<sep>[ \t]*(?:=>|=|:|(?(q1),|(?!)))[ \t]*)"
    rb"(?:(?P<q>[\"'])(?!\(hidden\)|\*\*\*|-----BEGIN)(?P<qv>[^\n]*?)(?P<end>(?P=q)|$)"
    rb"|(?!\(hidden\)|\*\*\*|-----BEGIN)(?P<uv>[^&\s'\",;}\x00]+))",
    re.I | re.M)


# A unit's environment (systemctl show / systemctl cat: Environment=A=1 "B=two words"): every value is masked, its name
# stays — the masks by name would leave a secret under a plain name (review 04.11.2026).
ENV_LINE_RE = re.compile(rb"^([ \t]*Environment=)([^\n]*)$", re.M)
ENV_NAME_RE = re.compile(rb"^[A-Za-z_][A-Za-z0-9_]*=")


def _env_values(line: bytes) -> Tuple[bytes, int]:
    """Environment=…'s assignments with their values masked; quotes keep a value with spaces in one assignment."""
    out, masked, i = [], 0, 0
    while i < len(line):
        if line[i:i + 1] in (b" ", b"\t"):
            out.append(line[i:i + 1])
            i += 1
            continue
        j, quoted = i, False
        while j < len(line) and (quoted or line[j:j + 1] not in (b" ", b"\t")):
            if line[j:j + 1] == b'"':
                quoted = not quoted
            j += 1
        token = line[i:j]
        bare = token[1:] if token.startswith(b'"') else token
        name = ENV_NAME_RE.match(bare)
        if name and len(bare) > name.end() and bare[name.end():] != b'"':
            out.append(token[:len(token) - len(bare)] + name.group(0) + MASK + (b'"' if token.startswith(b'"') else b""))
            masked += 1
        else:
            out.append(token)
        i = j
    return b"".join(out), masked


def redact_counted(data: bytes) -> Tuple[bytes, int]:
    """(the output with secrets masked, how many places were masked)."""
    total = [0]

    def fixed(replacement: bytes) -> Callable:
        def sub(match) -> bytes:
            total[0] += 1
            return match.expand(replacement)
        return sub

    def key_value(match) -> bytes:
        whole = match.group(0)
        if match.group("name").lower() == b"key" and b":" in match.group("sep") and not match.group("q1"):
            return whole  # "public key: …" (wg show) is no secret; a bare key is masked only with "=" or in quotes
        if match.group("q"):
            if not match.group("qv"):
                return whole
            total[0] += 1
            return whole[:match.start("q") - match.start()] + match.group("q") + MASK + match.group("end")
        total[0] += 1
        return whole[:match.start("uv") - match.start()] + MASK

    def mysql_line(match) -> bytes:
        return MYSQL_P_RE.sub(fixed(rb"\1***"), match.group(0))

    data = PEM_RE.sub(fixed(_PEM_MASKED), data)
    data = EDGE_END_RE.sub(fixed(b"://***"), data)
    data = URL_CREDS_RE.sub(fixed(rb"\1***:***@"), data)
    data = BEARER_RE.sub(fixed(rb"\1***"), data)
    data = BASIC_RE.sub(fixed(rb"\1***"), data)
    data = TELEGRAM_RE.sub(fixed(b"***"), data)
    data = MYSQL_LINE_RE.sub(mysql_line, data)
    data = OPT_P_RE.sub(fixed(rb"\1\2***"), data)
    data = OPT_U_RE.sub(fixed(rb"\1\2\3***"), data)
    def env_line(match) -> bytes:
        values, masked = _env_values(match.group(2))
        total[0] += masked
        return match.group(1) + values

    data = ENV_LINE_RE.sub(env_line, data)
    data = KV_RE.sub(key_value, data)
    return data, total[0]


def redact(data: bytes) -> bytes:
    return redact_counted(data)[0]


def _safe(text: str) -> str:
    """On Python 3.6 with the C locale, non-ASCII from the environment arrives as surrogates — do not crash on them."""
    return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def _log(message: str) -> None:
    if syslog is not None:
        syslog.openlog("agent-ro-gate", 0, syslog.LOG_AUTHPRIV)
        syslog.syslog(syslog.LOG_INFO, _safe(message)[:1000])


def _write(stream, text: str) -> None:
    stream.write(text.encode("utf-8", "surrogateescape"))


def main(argv=None, environ=None, out=None, err=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    environ = os.environ if environ is None else environ
    out = sys.stdout.buffer if out is None else out
    err = sys.stderr.buffer if err is None else err
    if argv == ["--sudoers"]:
        _write(out, "\n".join(sudoers_lines()) + "\n")
        return 0
    data = argv == ["--data"]
    if argv and not data:
        _write(err, "usage: agent-ro-gate [--sudoers|--data]\n")
        return 2
    raw = environ.get("SSH_ORIGINAL_COMMAND", "")
    tag = "data " if data else ""
    try:
        plan = check_data(raw) if data else check(raw)
        sqlite = plan.argv[0] == "sqlite3"  # read in-process, no binary to find
        command = [] if sqlite else materialize(plan)
    except Deny as exc:
        _log("%sdeny (%s): %s" % (tag, exc, raw))
        _write(err, "agent-ro-gate: отказ: %s\n" % exc)
        return 126
    _log("%sallow: %s" % (tag, raw))
    timeout = DATA_TIMEOUT if data else TIMEOUT
    if sqlite:
        timeout = SQLITE_TIMEOUT
        _cap_memory()
        result = run_sqlite(plan.argv[1], plan.argv[2])
    elif plan.argv[0] == "zgrep":
        result = run_zgrep(command, env=CHILD_ENV)
    else:
        result = run_limited(command, timeout=timeout, env=CHILD_ENV)
    output, hidden = redact_counted(result.output)
    out.write(output)
    if hidden:
        _write(out, "\n[agent-ro-gate: скрыто секретов: %d]\n" % hidden)
    if result.truncated:
        _write(out, "\n[agent-ro-gate: вывод обрезан до %d КБ]\n" % (LIMIT // 1024))
    if result.timed_out:
        _write(out, "\n[agent-ro-gate: прервано по таймауту %d с]\n" % timeout)
        out.flush()
        return 124
    out.flush()
    return 0 if result.truncated else result.returncode


if __name__ == "__main__":
    sys.exit(main())
