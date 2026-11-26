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


# A width the agent picks (a format's ":N", --cols/--columns/--width) cuts a command line where it likes:
# "https://user:pass@host" cut before "@", or a bearer token cut short, is no longer a secret the masks recognise
# (review 26.11.2026), and moving the cut a character at a time would print a password piece by piece.
PS_WIDTH = re.compile(r":\d|^--(cols|columns|width)(=|$)", re.ASCII)


def _ps(args: List[str]) -> List[str]:
    value_next = False
    for arg in args:
        if not PS_ARG.match(arg):
            raise Deny("ps: недопустимый аргумент: " + arg)
        if PS_WIDTH.search(arg):
            raise Deny("ps: ширина столбца запрещена (" + arg + ") — обрезанная командная строка прячет пароль от "
                       "маски; уберите :N, --cols, --columns, --width")
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


def _no_json(name: str, opts: Opts) -> None:
    """The journal's json outputs print a field with a non-printable byte (a colour code is enough) as an array of
    numbers: "password=x" becomes [112,97,…], which no mask reads (review 26.11.2026)."""
    if any(opt in ("-o", "--output") and (value or "").startswith("json") for opt, value in opts):
        raise Deny(name + ": вывод json запрещён — сообщение с непечатным байтом json печатает массивом чисел, "
                          "маска его не читает; используйте -o short-iso, verbose или cat")


def _systemctl(args: List[str]) -> List[str]:
    opts, positional = parse_opts(
        args, "alqr", "pPnto",
        ("--all", "--full", "--quiet", "--no-pager", "--no-legend", "--plain", "--value", "--failed",
         "--recursive", "--reverse", "--after", "--before"),
        ("--property", "--lines", "--type", "--state", "--output"))
    # --value and -P print a property's value without "Name=": the mask of a unit's environment keys on
    # "Environment=", so its values would come out under the name masks only (review 26.11.2026)
    if any(opt in ("--value", "-P") for opt, _ in opts):
        raise Deny("systemctl: --value и -P запрещены — значение без имени свойства маска не узнаёт; "
                   "читайте systemctl show -p <Свойство> <юнит>: строка Свойство=значение")
    _no_json("systemctl", opts)
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


# never allowed: --vacuum-*, --rotate, --flush, --(smart-)relinquish-var, --sync, --setup-keys,
# --update-catalog, --cursor-file, --file, -D/--directory, --root, --image, -f/--follow, -M/--machine
JOURNAL_OPTIONS = dict(
    flags="abekmqrxlN", valued="unpoSUtgFc",
    long_flags=("--no-pager", "--all", "--full", "--no-full", "--reverse", "--merge", "--utc", "--no-hostname",
                "--catalog", "--quiet", "--dmesg", "--list-boots", "--disk-usage", "--pager-end", "--system",
                "--fields", "--show-cursor"),
    long_valued=("--unit", "--user-unit", "--priority", "--output", "--since", "--until", "--identifier", "--grep",
                 "--field", "--cursor", "--after-cursor", "--facility", "--output-fields", "--case-sensitive",
                 "--exclude-identifier"),
    long_optional=("--boot", "--lines"),
    numbers=True)
# journalctl -g matches the raw journal, so the gate splits its output into entries and drops every entry the masks
# change (run_journal_grep): only outputs where an entry's further lines are indented, or an entry is one line.
JOURNAL_GREP_OUTPUTS = frozenset({"short", "short-full", "short-iso", "short-iso-precise", "short-precise",
                                  "short-monotonic", "short-unix", "short-delta", "with-unit", "verbose"})


def _journalctl(args: List[str]) -> List[str]:
    opts, positional = parse_opts(args, **JOURNAL_OPTIONS)
    _no_json("journalctl", opts)
    if any(opt in ("-g", "--grep") for opt, _ in opts):
        if any(opt == "--show-cursor" for opt, _ in opts):
            raise Deny("journalctl: -g с --show-cursor нельзя — курсор назвал бы последнюю совпавшую запись, "
                       "даже скрытую")
        outputs = [value for opt, value in opts if opt in ("-o", "--output")]
        if outputs and outputs[-1] not in JOURNAL_GREP_OUTPUTS:
            raise Deny("journalctl: -g только с выводом " + ", ".join(sorted(JOURNAL_GREP_OUTPUTS))
                       + " — шлюз делит его на записи и убирает те, где маска скрыла секрет")
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


def journal_grep(args: Sequence[str]) -> Optional[Tuple[List[str], Optional[int], bool]]:
    """journalctl -g as the gate runs it: (journalctl's arguments, how many entries to keep — None for all, whether
    to turn the kept entries back into time order); None without -g.

    The gate keeps its own -n window: journalctl's window of the last N raw matches would hold the entries the gate
    drops, and which of them fell in it would say whether a secret matched. So journalctl gets --lines=all (it wins
    over -n and -e, being the last), the newest first when the newest N are wanted, and -q (no "-- No entries --",
    which would tell a dropped match from none)."""
    opts, _ = parse_opts(args, **JOURNAL_OPTIONS)
    if not any(opt in ("-g", "--grep") for opt, _ in opts):
        return None
    lines = [value for opt, value in opts if opt in ("-n", "--lines")]
    count, oldest = None, False
    if lines:
        last = "10" if lines[-1] is None else lines[-1]  # a bare --lines is 10
        if last != "all":
            count, oldest = int(last.lstrip("+")), last.startswith("+")
    elif any(opt in ("-e", "--pager-end") for opt, _ in opts):
        count = 1000  # -e implies -n 1000
    extra = [] if count is None else ["--lines=all"] + ([] if oldest else ["--reverse"])
    extra.append("-q")
    reverse = any(opt in ("-r", "--reverse") for opt, _ in opts)
    words = list(args)
    at = words.index("--") if "--" in words else len(words)  # "--" is never a value here: _journalctl refuses it
    return words[:at] + extra + words[at:], count, count is not None and not oldest and not reverse


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


_CAT = _files("cat", "AbeEnsTtuv")
CAT_RESHAPE = "AeTtv"


def _cat(args: List[str]) -> List[str]:
    """cat without the forms that print non-printing characters as text: "password<Tab>= x" shown as
    "password^I= x", or a UTF-8 byte 0xA0 as "M- " (a space inside a value), is no longer a pair the masks recognise
    (review 26.11.2026)."""
    opts, _ = parse_opts(args, "AbeEnsTtuv")
    shaped = sorted({opt for opt, _ in opts if opt[1:] in CAT_RESHAPE})
    if shaped:
        raise Deny("cat: " + ", ".join(shaped) + " запрещено — ^I вместо табуляции и M- вместо байтов меняют текст "
                   "так, что маска не узнаёт секрет; можно -n, -b, -s, -E")
    return _CAT(args)


VALIDATORS.update({
    "cat": _cat,
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
# rest are refused. grep never reads a file itself: the gateway feeds it the masked text (run_grep), so a pattern, a
# count or a file name answers nothing about a hidden value.

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
RUN_AS = {"zcat": "gzip", "zgrep": "grep"}  # zgrep is grep over decompression inside the gateway (run_grep), without the zgrep script


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


def _read(path: str, errors: List[str]) -> Iterator[bytes]:
    """A file in chunks; one that cannot be opened, a directory or not a regular file goes to errors as grep says it."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))  # a FIFO must not hang the open
    except OSError as exc:
        errors.append("%s: %s" % (path, exc.strerror))
        return
    try:
        mode = os.fstat(fd).st_mode
        if stat.S_ISDIR(mode):
            errors.append("%s: Is a directory" % path)
            return
        if not stat.S_ISREG(mode):
            errors.append("%s: не обычный файл" % path)
            return
        while True:
            piece = os.read(fd, 65536)
            if not piece:
                return
            yield piece
    except OSError as exc:
        errors.append("%s: %s" % (path, exc.strerror))
    finally:
        os.close(fd)


def run_grep(command: List[str], name: str = "grep", timeout: float = TIMEOUT, limit: int = LIMIT,
             env: Optional[Dict[str, str]] = None) -> Result:
    """grep and zgrep over the masked text: the gateway reads each file itself (zgrep: decompresses it, without the
    zgrep script, CVE-2022-1271), masks it as it would mask its output (blind_stream) and feeds that into grep's stdin.

    So a pattern never tests a hidden value: grep -c, -l, -v and a line found or not answer the same whatever the
    secret is (review 26.11.2026). command is materialize() of the plan: grep, options, "--", files. The file name
    goes through --label; with several files and no -h — with -H, as grep and zgrep do. The timeout and the output
    limit are shared by all files. Exit code: 2 — a failure in at least one file, otherwise 0 if anything matched
    anywhere, otherwise 1."""
    opts, files = _grep_parse(command[1:])
    prefix = command[:len(command) - len(files) - 1]  # without "--" and the files
    if len(files) > 1 and not any(opt in ("-h", "--no-filename") for opt, _ in opts):
        prefix = prefix + ["-H"]
    quiet = any(opt in ("-s", "--no-messages") for opt, _ in opts)
    deadline = time.monotonic() + timeout
    output: List[bytes] = []
    size, codes, truncated, timed_out = 0, [], False, False
    for path in files:
        left = deadline - time.monotonic()
        if left <= 0:
            timed_out = True
            break
        errors: List[str] = []
        source = _gunzip(path, errors) if name == "zgrep" else _read(path, errors)
        result = run_limited(prefix + ["--label=" + path], timeout=left, limit=limit - size, env=env,
                             feed=blind_stream(source, errors))
        said = [] if quiet and name == "grep" else errors
        piece = result.output + "".join(name + ": " + e + "\n" for e in said).encode("utf-8", "replace")
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


NO_ENTRIES = b"-- No entries --"


def _would_mask(entry: bytes) -> bool:
    """The output masks would hide something in entry (the cheap search first: most entries have no hint)."""
    if PEM_MARK not in entry and b"://" not in entry and not _hinted_lines(entry):  # PEM_RE, EDGE_END_RE, the rest
        return False
    return redact_counted(entry)[1] > 0


def run_journal_grep(command: List[str], count: Optional[int], flip: bool, timeout: float = TIMEOUT,
                     limit: int = LIMIT, env: Optional[Dict[str, str]] = None) -> Result:
    """journalctl -g whose answer says nothing about a masked value (review 26.11.2026).

    journalctl matches its pattern against the raw journal, so an entry the masks would change is never a result:
    whether it matched would say whether its secret fits the pattern. The output (command is journal_grep's) is split
    into entries — a line indented or starting with "-- " (a catalog text) belongs to the entry above — and the rest
    are kept, up to count of them (journal_grep asked for the newest first when the newest are wanted; flip turns
    them back into time order) and up to limit bytes. Exit code: journalctl's own failure if it ended by itself with
    one, otherwise 0 when an entry is kept and 1 when none is — the same whether nothing matched or only hidden
    entries did."""
    proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=env, start_new_session=True)
    kept: List[bytes] = []
    state = {"size": 0, "truncated": False, "done": False}

    def keep(entry: bytes) -> None:
        if count is not None and len(kept) >= count:
            state["done"] = True
        elif not _would_mask(entry):
            if state["size"] + len(entry) > limit:
                state["truncated"] = state["done"] = True
                return
            kept.append(entry)
            state["size"] += len(entry)
            state["done"] = count is not None and len(kept) >= count

    def pump() -> None:
        entry: List[bytes] = []
        for line in iter(proc.stdout.readline, b""):
            if line.startswith(NO_ENTRIES):  # it would tell "nothing matched" from "only hidden entries matched"
                continue
            if entry and line[:1] not in (b" ", b"\t") and not line.startswith(b"-- "):
                keep(b"".join(entry))
                entry = []
                if state["done"]:
                    return
            entry.append(line)
        if entry:
            keep(b"".join(entry))

    reader = threading.Thread(target=pump)
    reader.daemon = True
    reader.start()
    deadline = time.monotonic() + timeout
    timed_out = ran_out = False
    try:
        while reader.is_alive():
            if time.monotonic() >= deadline:
                timed_out = True
                break
            reader.join(0.1)
        if not timed_out and not state["done"]:
            try:
                proc.wait(timeout=max(deadline - time.monotonic(), 0.1))
            except subprocess.TimeoutExpired:
                timed_out = True
        ran_out = proc.poll() is not None and not state["done"]  # journalctl ended by itself
    finally:
        _stop(proc)
    reader.join(5)
    code = proc.returncode if ran_out and proc.returncode not in (0, 1) else 0 if kept else 1
    return Result(code, b"".join(kept[::-1] if flip else kept), bool(state["truncated"]), timed_out)


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
# stays — the masks by name would leave a secret under a plain name (review 04.11.2026). systemctl cat prints a unit
# file as written: systemd allows spaces around "=" and continues a line that ends with "\" (review 26.11.2026).
ENV_LINE_RE = re.compile(rb"^([ \t]*Environment[ \t]*=[ \t]*)((?:[^\n]*\\\n)*[^\n]*)$", re.M)
ENV_NAME_RE = re.compile(rb"^[A-Za-z_][A-Za-z0-9_]*=")
CONTINUED = b"\\\n"
# SetCredential=ID:VALUE puts the secret itself into the unit file; the ID stays.
CREDENTIAL_RE = re.compile(rb"^([ \t]*SetCredential[ \t]*=[ \t]*[^:\n]*:)[^\n]+$", re.M)


def _env_gap(line: bytes, i: int) -> int:
    """The length of the separator at i (a space, a tab, a continued line), 0 inside an assignment."""
    if line[i:i + 2] == CONTINUED:
        return 2
    return 1 if line[i:i + 1] in (b" ", b"\t", b"\n") else 0


def _env_values(line: bytes) -> Tuple[bytes, int]:
    """Environment=…'s assignments with their values masked; quotes keep a value with spaces in one assignment.

    A masked quoted value keeps the continued lines it spanned, so the text keeps its line count."""
    out, masked, i = [], 0, 0
    while i < len(line):
        gap = _env_gap(line, i)
        if gap:
            out.append(line[i:i + gap])
            i += gap
            continue
        j, quoted = i, False
        while j < len(line) and (quoted or not _env_gap(line, j)):
            if line[j:j + 1] == b'"':
                quoted = not quoted
            j += 1
        token = line[i:j]
        bare = token[1:] if token.startswith(b'"') else token
        name = ENV_NAME_RE.match(bare)
        if name and len(bare) > name.end() and bare[name.end():] != b'"':
            out.append(token[:len(token) - len(bare)] + name.group(0) + MASK + CONTINUED * token.count(b"\n")
                       + (b'"' if token.startswith(b'"') else b""))
            masked += 1
        else:
            out.append(token)
        i = j
    return b"".join(out), masked


def _fixed(replacement: bytes, total: List[int]) -> Callable:
    def sub(match) -> bytes:
        total[0] += 1
        return match.expand(replacement)
    return sub


def _sub(regex, replacement: bytes) -> Callable[[bytes, List[int]], bytes]:
    def apply(data: bytes, total: List[int]) -> bytes:
        return regex.sub(_fixed(replacement, total), data)
    return apply


def _mysql_lines(data: bytes, total: List[int]) -> bytes:
    return MYSQL_LINE_RE.sub(lambda match: MYSQL_P_RE.sub(_fixed(rb"\1***", total), match.group(0)), data)


def _env_lines(data: bytes, total: List[int]) -> bytes:
    def env_line(match) -> bytes:
        values, masked = _env_values(match.group(2))
        total[0] += masked
        return match.group(1) + values
    return ENV_LINE_RE.sub(env_line, data)


def _key_values(data: bytes, total: List[int]) -> bytes:
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
    return KV_RE.sub(key_value, data)


class LineMask(NamedTuple):
    """A mask that keeps to one line (or to one unit-file line continued with "\\"), with its hint: what its every
    match needs in the lower-cased line — one of words (and then matching at that word, if given), or hint."""
    name: str
    apply: Callable[[bytes, List[int]], bytes]
    words: Tuple[bytes, ...]
    then: Optional[Callable]  # re.match of a compiled pattern
    hint: Optional[Callable]  # re.finditer of a compiled pattern


def _line_mask(name: str, apply: Callable[[bytes, List[int]], bytes], words: Sequence[bytes] = (),
               then: Optional[bytes] = None, hint: Optional[bytes] = None) -> LineMask:
    """A mask without a hint is refused: blind() would never run it, and grep would see its secrets."""
    if not words and hint is None:
        raise ValueError("line mask %s has no hint" % name)
    return LineMask(name, apply, tuple(words), re.compile(then).match if then else None,
                    re.compile(hint).finditer if hint else None)


# The line masks in the order they are applied. A hint is a necessary condition, never a guess: blind() runs a mask
# only over the lines holding its hint, so a match without it would stay unmasked in what grep sees
# (test_every_mask_has_its_own_hint).
LINE_MASKS = (
    _line_mask("url_creds", _sub(URL_CREDS_RE, rb"\1***:***@"), [b"://"], then=rb"://[^\s/@:'\"]*:[^\s/@'\"]*@"),
    _line_mask("bearer", _sub(BEARER_RE, rb"\1***"), [b"bearer"]),
    _line_mask("basic", _sub(BASIC_RE, rb"\1***"), [b"basic"]),
    _line_mask("telegram", _sub(TELEGRAM_RE, b"***"), hint=rb":[a-z0-9_-]{35}"),
    _line_mask("mysql", _mysql_lines, [b"mysql"]),
    _line_mask("opt_p", _sub(OPT_P_RE, rb"\1\2***"), [b"-p ", b"-p\t", b"--pass"]),
    _line_mask("opt_u", _sub(OPT_U_RE, rb"\1\2\3***"), [b"-u ", b"-u\t", b"-u=", b"--user"]),
    _line_mask("environment", _env_lines, [b"environment"]),
    _line_mask("set_credential", _sub(CREDENTIAL_RE, rb"\1***"), [b"setcredential"]),
    # every name of _SECRET_NAMES ends with one of these, then comes KV_RE's separator
    _line_mask("key_value", _key_values, [b"key", b"pass", b"pwd", b"sig", b"secret", b"token"],
               then=rb"(?:key|secret|token|signature|password|passwd|pass|pwd|sig)[\"']?[ \t]*[=:,]"),
)


PEM_BEGIN_RE = re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----")
PEM_END_RE = re.compile(rb"-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----")


def _pem_lines(data: bytes, inside: bool = False) -> Tuple[bytes, bool]:
    """data with every private key masked line for line: its BEGIN line from the marker on shows the key as PEM_RE
    shows it whole, each further line of the key up to its END marker is ***, the text after END stays. PEM_RE joins
    a key into one line; grep reads one line at a time and would print a body line it matched without the BEGIN line
    the output's PEM_RE needs (KD-9483) — this keeps the line count and masks those lines. ``inside`` — data starts
    inside a key an earlier block of the same file began; → (the text, whether it ends inside a key)."""
    if not inside and PEM_MARK not in data:
        return data, False
    out = []  # type: List[bytes]
    pos, size = 0, len(data)
    while pos < size:
        shown = False  # the key's current line already shows its mask (the BEGIN line)
        if not inside:
            begin = PEM_BEGIN_RE.search(data, pos)
            if begin is None:
                out.append(data[pos:])
                break
            out.append(data[pos:begin.start()] + _PEM_MASKED)
            pos, inside, shown = begin.end(), True, True
        end = PEM_END_RE.search(data, pos)
        lines = data.count(b"\n", pos, size if end is None else end.start())
        tail = b""
        if end is None and data.endswith(b"\n"):  # the key goes on in the next block, from the start of a line
            lines, tail = lines - 1, b"\n"
        out.append((b"" if shown else MASK) + b"\n***" * lines + tail)
        if end is None:
            return b"".join(out), True
        pos, inside = end.end(), False
    return b"".join(out), inside


def _mask_lines(data: bytes, total: List[int]) -> bytes:
    """Every mask that keeps to one line: a private key line for line (_pem_lines), then LINE_MASKS. None of them adds
    or removes a line break, so the masked text keeps its line numbers. In redact_counted PEM_RE has joined every key
    into one line already, which _pem_lines leaves as it is."""
    data = _pem_lines(data)[0]
    for mask in LINE_MASKS:
        data = mask.apply(data, total)
    return data


def redact_counted(data: bytes) -> Tuple[bytes, int]:
    """(the output with secrets masked, how many places were masked)."""
    total = [0]
    data = PEM_RE.sub(_fixed(_PEM_MASKED, total), data)
    data = EDGE_END_RE.sub(_fixed(b"://***", total), data)
    return _mask_lines(data, total), total[0]


def redact(data: bytes) -> bytes:
    return redact_counted(data)[0]


# --- what a pattern sees: the masked text ------------------------------------------
# A pattern run over the raw file answers about the secret even when no secret is printed: grep -c '^password=a'
# says 0 or 1, and the presence of a masked line says the same (review 26.11.2026). grep and zgrep therefore match the
# text the gateway would show — the file through _mask_lines, line for line. Running the masks over every byte in
# Python is slow (3-6 MB/s), so each mask of LINE_MASKS runs only over the lines holding its own hint, found by a
# byte search; a line without it is the same masked. An access log with referrers, user agents and paths like
# /signin goes at about 80-130 MB/s (8 MB, review 26.11.2026); a log with "token=" on every line at about 10 MB/s.
BLIND_BLOCK = 1 << 20
BLIND_LINE_MAX = 64 << 20  # a "line" longer than this is not read on: it could not be masked in one piece


def _line_end(data: bytes, pos: int) -> int:
    """The end of the line at pos (its "\\n" or the end of data), over lines continued with a trailing "\\"."""
    end = data.find(b"\n", pos)
    while end > 0 and data[end - 1:end] == b"\\":
        end = data.find(b"\n", end + 1)
    return len(data) if end < 0 else end


def _hits(mask: LineMask, low: bytes) -> List[int]:
    """Where mask's hint is in low (lower-cased data): at least one place on every line holding it."""
    hits = [found.start() for found in mask.hint(low)] if mask.hint else []
    for word in mask.words:
        at = low.find(word)
        while at >= 0:
            if mask.then is None or mask.then(low, at):
                hits.append(at)
                line_end = low.find(b"\n", at)
                at = -1 if line_end < 0 else low.find(word, line_end)
            else:
                at = low.find(word, at + 1)
    return hits


def _spans(data: bytes, hits: List[int]) -> List[Tuple[int, int]]:
    """(start, end) of the lines holding hits, in order and apart."""
    spans: List[Tuple[int, int]] = []
    end = -1
    for at in sorted(hits):
        if at < end:  # on a line already taken (a continued line too)
            continue
        end = _line_end(data, at)
        spans.append((data.rfind(b"\n", 0, at) + 1, end))
    return spans


def _hinted_lines(data: bytes) -> List[Tuple[int, int]]:
    """(start, end) of the lines some line mask could change. Where none could, none does: the first mask to change
    anything had its hint in data as it is."""
    low = data.lower()
    return _spans(data, [at for mask in LINE_MASKS for at in _hits(mask, low)])


def _apply_on(data: bytes, spans: List[Tuple[int, int]], mask: LineMask) -> bytes:
    """data with mask applied to the lines of spans only — data itself when it changed nothing."""
    joined = b"\n".join(data[start:end] for start, end in spans)
    masked = mask.apply(joined, [0])
    if masked == joined:
        return data
    lines = masked.split(b"\n")
    if len(lines) != joined.count(b"\n") + 1:  # the mask changed the line count: apply it to everything instead
        return mask.apply(data, [0])
    out, last, k = [], 0, 0
    for start, end in spans:
        count = data.count(b"\n", start, end) + 1
        out.append(data[last:start])
        out.append(b"\n".join(lines[k:k + count]))
        k += count
        last = end
    out.append(data[last:])
    return b"".join(out)


def blind(data: bytes) -> bytes:
    """Whole lines as a pattern may see them: _mask_lines(data) byte for byte, each mask run only on its lines."""
    return _blind(data)[0]


def _blind(data: bytes, inside: bool = False) -> Tuple[bytes, bool]:
    """blind() of one block; ``inside`` — the block starts inside a private key (_pem_lines) → (text, inside)."""
    data, inside = _pem_lines(data, inside)
    low = data.lower()
    for mask in LINE_MASKS:
        spans = _spans(data, _hits(mask, low))
        if spans:
            masked = _apply_on(data, spans, mask)
            if masked is not data:
                data, low = masked, masked.lower()
    return data, inside


def _cut(data: bytes) -> int:
    """Where whole lines end in data: after a "\\n" that does not continue a line; 0 — nowhere yet."""
    end = data.rfind(b"\n")
    while end > 0 and data[end - 1:end] == b"\\":
        end = data.rfind(b"\n", 0, end)
    return end + 1


def blind_stream(chunks: Iterable[bytes], errors: List[str]) -> Iterator[bytes]:
    """chunks as blind() sees them, in blocks of whole lines; a private key that begins in one block is masked on into
    the next. A line longer than BLIND_LINE_MAX ends the stream (into errors): its secret could fall across two
    blocks."""
    pending: List[bytes] = []
    size, inside = 0, False
    for chunk in chunks:
        pending.append(chunk)
        size += len(chunk)
        if size < BLIND_BLOCK or (b"\n" not in chunk and size <= BLIND_LINE_MAX):
            continue  # no new line has ended
        data = b"".join(pending)
        cut = _cut(data)
        if not cut and len(data) > BLIND_LINE_MAX:
            cut = data.rfind(b"\n") + 1
            if not cut:
                errors.append("строка длиннее %d МБ — дальше файл не читается" % (BLIND_LINE_MAX >> 20))
                return
        if cut:
            block, inside = _blind(data[:cut], inside)
            yield block
            data = data[cut:]
        pending, size = [data], len(data)
    data = b"".join(pending)
    if data:
        yield _blind(data, inside)[0]


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
        journal = journal_grep(plan.argv[1:]) if plan.argv[0] == "journalctl" else None
        if journal is not None:
            plan = Plan(["journalctl"] + journal[0], False)
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
    elif plan.argv[0] in ("grep", "zgrep"):
        result = run_grep(command, plan.argv[0], env=CHILD_ENV)
    elif journal is not None:
        result = run_journal_grep(command, journal[1], journal[2], env=CHILD_ENV)
    else:
        result = run_limited(command, timeout=timeout, env=CHILD_ENV)
    output, hidden = redact_counted(result.output)
    if plan.argv[0] in ("grep", "zgrep"):
        # grep read the masked text, so its lines come masked already: count the marks (a URL's "***:***@" is one;
        # a file's own "***" counts too)
        hidden = output.count(MASK) - output.count(b"***:***@")
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
