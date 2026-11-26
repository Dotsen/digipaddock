"""kd-host-read: the platform's journal, systemd state and files for every agent.

The executor runs as the host reader (``naming.service_user("observe")``), the one platform user in the
group systemd-journal, behind the agent's ``host`` capability channel. The channel names the agent
(``KD_AGENT``); nothing the caller passes widens what it reads:

- ``journalctl`` — the executor builds the whole argv: ``-o json`` and the trusted matches
  ``_SYSTEMD_UNIT=<unit> + UNIT=<unit> _PID=1`` of catalog units; options only narrow (time, priority,
  text). Records of the private audit (``audit_identifiers``) are dropped for everyone; for an ordinary
  agent also syslog/audit records of authentication processes (``private_comm`` by the trusted ``_COMM``,
  ``private_identifiers``): PAM lines of runuser run under kd-release's own unit. An agent of
  ``full_journal`` reads every unit. ``--operation <id>`` reads every runner unit of a factory
  operation (``digipaddock-operation-<id12>[-<n>]``) for the agent the cabinet says leads its card now
  (``operation.reader``; no answer — refused) and for an agent of ``full_journal``.
- ``systemctl`` — read verbs only, ``--no-pager --no-ask-password``, ``status`` always ``--lines=0``.
- files — ``ls|stat|cat|head|tail|grep`` of paths whose canonical form lies under a catalog root,
  opened without following a last-component link and re-checked through ``/proc/self/fd``.
- ``sys`` — fixed reads of host state (df, free, uptime, ss, ps, ip).

Every call: at most ``LIMIT_BYTES`` of output, ``TIMEOUT`` seconds per program.

No output is masked: the catalog is the boundary (its units and roots hold no secret), so the read gateway's
refusals that guard its masks (KD-9481) have nothing to guard here (KD-9483). ``systemctl show --value`` prints what
``show -p`` prints with the names; entries are formatted here from their fields, with no json output; ``cat`` takes
no option and ``sys ps`` is one fixed form; private and audit records are dropped before ``-n`` is counted and the
exit code does not depend on a match, so ``-g`` answers nothing about a dropped record.
"""

from __future__ import annotations

import collections
import gzip
import io
import json
import os
import re
import stat
import subprocess
import threading
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_DENIED = 0, 1, 2, 3
LIMIT_BYTES = 1 << 20
TIMEOUT = 10
DEFAULT_LINES, MAX_LINES = 200, 5000
CATALOG = "/opt/digipaddock/common/coordination/host-read.json"
STATE = "/etc/digipaddock/host-read.json"  # root-owned: {"disabled": [agent, ...]} (spec 7)
JOURNALCTL, SYSTEMCTL = "/usr/bin/journalctl", "/usr/bin/systemctl"
TRUNCATED = "… truncated (1 MiB)\n"
CUT_EARLIER = "… earlier entries cut (1 MiB)\n"
CUT_OLDER = "… older entries cut (1 MiB)\n"
# systemctl status: a unit's process tree prints every process's command line (the owner's session too); the
# tree goes, its CGroup line stays
CGROUP_RE = re.compile(r"^(\s+CGroup: \S.*)$")

UNIT_RE = re.compile(r"[a-z0-9][a-z0-9@:._-]*\.(?:service|socket|timer|path|scope|slice|mount|target)\Z")
UNIT_ARG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9@:._\-*?\[\]]{0,200}\Z")  # systemctl names and globs
TIME_RE = re.compile(r"[0-9A-Za-z:+. -]{1,40}\Z")
LEVEL = r"(?:[0-7]|emerg|alert|crit|err|warning|notice|info|debug)"
PRIORITY_RE = re.compile(rf"{LEVEL}(?:\.\.{LEVEL})?\Z")
WORD_RE = re.compile(r"[a-z][a-z,-]{0,63}\Z")
ROOT_NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
PROPERTY_RE = re.compile(r"[A-Za-z][A-Za-z0-9,]{0,255}\Z")
OPERATION_RE = re.compile(r"[0-9a-f]{12,32}\Z")
OPERATION_BATCHES = 16  # the most batches an operation has: one role each (the cabinet's limit of roles)
OUTPUTS = {"short-iso", "short", "cat"}
SYSTEMCTL_VERBS = frozenset(
    {"status", "show", "cat", "list-units", "list-timers", "list-unit-files", "is-active", "is-enabled", "is-failed"}
)
SYSTEMCTL_FLAGS = frozenset({"--all", "-a", "--failed", "--full", "-l", "--no-legend", "--value", "--plain"})
SYS_READS = {
    "df": ["/usr/bin/df", "-h"],
    "free": ["/usr/bin/free", "-m"],
    "uptime": ["/usr/bin/uptime"],
    "ss": ["/usr/bin/ss", "-tuln"],
    # the command name only: another process's arguments are not the host's state
    "ps": ["/usr/bin/ps", "-eo", "pid,user,etime,pcpu,pmem,rss,comm", "--sort=-pcpu"],
    "ip": ["/usr/bin/ip", "-brief", "address"],
}
# A catalog root may never be or hold these: secrets, signatures, other agents, the raw journal, mixed logs.
CLOSED = (
    "/etc/digipaddock",
    "/var/lib/digipaddock/cabinet",
    "/var/lib/digipaddock/admin-grants",
    "/var/lib/digipaddock/grants",
    "/var/lib/digipaddock/mirror",
    "/var/lib/digipaddock/worker",
    "/var/lib/digipaddock/board",
    "/var/lib/digipaddock/monitor",
    "/var/lib/digipaddock/boss-promotions/decisions",
    "/var/lib/digipaddock/review",
    "/var/lib/digipaddock/signers",
    "/var/log/journal",
    "/run/log",
    "/var/log/syslog",
    "/var/log/auth.log",
    "/var/log/digipaddock-permission",
    "/home",
    "/root",
    "/srv/digipaddock-agents",
)
USAGE = """usage: kd-host-read journalctl -u <unit>... [-n N] [--since T] [--until T] [-p P] [-g RE] [-b] [-r] [-o short-iso|cat]
       kd-host-read journalctl --operation <id> [the same options]   (a factory operation of a card you lead)
       kd-host-read journalctl --units
       kd-host-read systemctl status|show|cat|list-units|list-timers|list-unit-files|is-active|is-enabled|is-failed [options] [units]
       kd-host-read ls [-l] [-a] <path> | stat <path> | cat <path> | head|tail [-n N] <path>
       kd-host-read grep [-i] [-n] [-c] [-v] [-F] [-m N] (<pattern> | -e <pattern>) <path>
       kd-host-read paths                 (<path> is <name>[/<path under it>], e.g. nginx/error.log)
       kd-host-read sys df|free|uptime|ss|ps|ip
"""


class TimedOut(Exception):
    """journalctl was stopped by the call's time limit: what was read is not the whole answer."""


class Refused(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _usage(message: str) -> Refused:
    return Refused(EXIT_USAGE, f"usage: {message}\n{USAGE}")


@dataclass(frozen=True)
class Catalog:
    units: frozenset
    full_journal: frozenset
    roots: tuple
    names: dict  # root name → path: a path argument may start with a name (Claude Code asks for absolute ones)
    private_comm: frozenset
    private_identifiers: frozenset
    audit_identifiers: frozenset


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root + "/")


def _plain(path: str) -> bool:
    parts = path.split("/")
    return (
        path.startswith("/")
        and len(path) > 1
        and all(part not in ("", ".", "..") for part in parts[1:])
        and not set("*?[]{}\\") & set(path)
    )


def parse_catalog(data: dict) -> Catalog:
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("host-read catalog: schema_version 1 expected")

    def names(key: str, pattern=None) -> frozenset:
        value = data.get(key)
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            raise ValueError(f"host-read catalog: {key} is a list of names")
        for v in value:
            if pattern is not None and not pattern.fullmatch(v):
                raise ValueError(f"host-read catalog: bad {key} entry {v!r}")
        return frozenset(value)

    named = data.get("roots")
    if not isinstance(named, dict) or not named:
        raise ValueError("host-read catalog: roots maps names to paths")
    for name in named:
        if not isinstance(name, str) or not ROOT_NAME_RE.fullmatch(name):
            raise ValueError(f"host-read catalog: bad root name {name!r}")
    roots = list(named.values())
    for root in roots:
        if not isinstance(root, str) or not _plain(root):
            raise ValueError(f"host-read catalog: root {root!r} is not a plain absolute path")
        if root in ("/var/lib/digipaddock", "/var/log", "/run", "/etc", "/var", "/var/lib"):
            raise ValueError(f"host-read catalog: root {root} opens a mixed tree")
        for closed in CLOSED:
            if _within(root, closed) or _within(closed, root):
                raise ValueError(f"host-read catalog: root {root} reaches the closed {closed}")
    return Catalog(
        units=names("units", UNIT_RE),
        full_journal=names("full_journal", re.compile(r"[a-z][a-z0-9-]{0,31}\Z")),
        roots=tuple(roots),
        names=dict(named),
        private_comm=names("private_comm"),
        private_identifiers=names("private_identifiers"),
        audit_identifiers=names("audit_identifiers"),
    )


def load_catalog(path=CATALOG) -> Catalog:
    return parse_catalog(json.loads(Path(path).read_text(encoding="utf-8")))


def load_disabled(path=STATE) -> frozenset:
    """Agents whose host reads root switched off; an unreadable state switches everyone off."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return frozenset()
    disabled = data.get("disabled") if isinstance(data, dict) else None
    if not isinstance(disabled, list) or not all(isinstance(a, str) for a in disabled):
        raise ValueError(f"{path}: disabled is a list of agent names")
    return frozenset(disabled)


class Capped:
    """A text sink that keeps at most LIMIT_BYTES and then says so once."""

    def __init__(self, out, limit: int = LIMIT_BYTES) -> None:
        self.out, self.left, self.full = out, limit, False

    def write(self, text: str) -> None:
        if self.full:
            return
        data = text.encode("utf-8", "replace")
        if len(data) > self.left:
            self.out.write(data[: self.left].decode("utf-8", "ignore") + TRUNCATED)
            self.left, self.full = 0, True
            return
        self.out.write(text)
        self.left -= len(data)


# --- journal ----------------------------------------------------------------------------------------


def _unit(name: str) -> str:
    unit = name if "." in name else f"{name}.service"
    if not UNIT_RE.fullmatch(unit):
        raise _usage(f"not a unit name: {name}")
    return unit


def _int(value: str, low: int, high: int, what: str) -> int:
    if not re.fullmatch(r"[0-9]{1,6}", value) or not low <= int(value) <= high:
        raise _usage(f"{what} is a number from {low} to {high}")
    return int(value)


def _split(args: list, short: dict, long: dict, flags: dict) -> tuple:
    """(options, positionals): ``short``/``long`` take a value (``-x V``, ``--x=V``, ``--x V``), ``flags`` none.
    Every name maps to its canonical key; anything else is refused."""
    options, positional, i = [], [], 0
    while i < len(args):
        arg = args[i]
        if arg in flags:
            options.append((flags[arg], None))
        elif arg in short or arg in long:
            if i + 1 >= len(args):
                raise _usage(f"{arg} needs a value")
            options.append(((short.get(arg) or long[arg]), args[i + 1]))
            i += 1
        elif arg.startswith("--") and "=" in arg and arg.split("=", 1)[0] in long:
            name, value = arg.split("=", 1)
            options.append((long[name], value))
        elif arg.startswith("-"):
            raise _usage(f"option {arg} is not allowed")
        else:
            positional.append(arg)
        i += 1
    return options, positional


def operation_units(operation: str) -> list:
    """Every runner unit a factory operation may have had on the session host (``session_host.operation.unit``):
    one per batch (a role per batch at most, 16 roles), and the one of a request written before batches."""
    stem = f"digipaddock-operation-{operation[:12]}"
    return [f"{stem}.service"] + [f"{stem}-{n}.service" for n in range(1, OPERATION_BATCHES + 1)]


def _operation_reader(operation: str, agent: str) -> str:
    """The cabinet's ``operation.reader``: "" when ``agent`` leads the operation's card now, else why. No answer is a
    refusal: who may read is the cabinet's record, never the caller's word."""
    from kd.cabinet import client as cabinet_client

    try:
        answer = cabinet_client.Client().call({"op": "operation.reader", "operation": operation, "agent": agent})
    except (cabinet_client.CabinetError, OSError, ValueError):
        return "the cabinet did not answer"
    if not isinstance(answer, dict) or answer.get("allowed") is not True:
        reason = answer.get("reason") if isinstance(answer, dict) else ""
        return reason if isinstance(reason, str) and WORD_RE.fullmatch(reason.replace("_", "-")) else "refused"
    return ""


def journal_request(args: list, agent: str, catalog: Catalog, reader: Callable = _operation_reader) -> tuple:
    """(argv of journalctl, lines to keep, output mode, oldest first)."""
    options, positional = _split(
        args,
        {"-u": "unit", "-n": "lines", "-S": "since", "-U": "until", "-p": "priority", "-g": "grep", "-o": "output"},
        {"--unit": "unit", "--lines": "lines", "--since": "since", "--until": "until", "--priority": "priority",
         "--grep": "grep", "--output": "output", "--operation": "operation"},
        {"-b": "boot", "--boot": "boot", "-r": "reverse", "--reverse": "reverse", "--no-pager": "nopager"},
    )
    if positional:
        raise _usage("journal matches are built by kd-host-read; name units with -u")
    full = agent in catalog.full_journal
    units, lines, mode, reverse = [], DEFAULT_LINES, "short-iso", False
    narrow = []
    operations = [value for key, value in options if key == "operation"]
    if operations:  # the journal of a factory operation's runner units
        if len(operations) > 1 or any(key == "unit" for key, _ in options):
            raise _usage("--operation names one operation and no other unit")
        if not OPERATION_RE.fullmatch(operations[0]):
            raise _usage("--operation is an operation id or its first 12 hex digits")
        units = operation_units(operations[0])
    for key, value in options:
        if key == "unit":
            unit = _unit(value)
            if not full and unit not in catalog.units:
                raise Refused(EXIT_DENIED, f"unit {unit} is not in your journal list (kd-host-read journalctl --units)\n")
            units.append(unit)
        elif key == "lines":
            lines = _int(value, 1, MAX_LINES, "-n")
        elif key in ("since", "until"):
            if not TIME_RE.fullmatch(value):
                raise _usage(f"--{key} is a time such as -1h, today, 2026-10-22 10:00")
            narrow.append(f"--{key}={value}")
        elif key == "priority":
            if not PRIORITY_RE.fullmatch(value):
                raise _usage("-p is a level 0-7 or a name, or a range a..b")
            narrow.append(f"--priority={value}")
        elif key == "grep":
            if not 0 < len(value) <= 200 or "\n" in value:
                raise _usage("-g is a pattern up to 200 characters")
            narrow.append(f"--grep={value}")
        elif key == "output":
            if value not in OUTPUTS:
                raise _usage("-o is short-iso or cat")
            mode = value
        elif key == "boot":
            narrow.append("-b")
        elif key == "reverse":
            reverse = True
    if not units and not full:
        raise Refused(EXIT_DENIED, "name a unit with -u; your journal list: kd-host-read journalctl --units\n")
    why = reader(operations[0], agent) if operations and not full else ""  # asked once every option is valid
    if why:
        raise Refused(EXIT_DENIED, f"operation {operations[0]}: {why} — its journal is read by the agent "
                                   "leading its card and by sysadmin\n")
    argv = [JOURNALCTL, "--no-pager", "-q", "--all", "-o", "json", "-r", *narrow]
    if units:
        unique = sorted(set(units))
        argv += [f"_SYSTEMD_UNIT={u}" for u in unique] + ["+"] + [f"UNIT={u}" for u in unique] + ["_PID=1"]
    return argv, lines, mode, not reverse


def keep_entry(entry: dict, full: bool, catalog: Catalog) -> bool:
    ident = entry.get("SYSLOG_IDENTIFIER")
    if ident in catalog.audit_identifiers:
        return False
    if full:
        return True
    if entry.get("_TRANSPORT") == "audit":
        return False
    if ident in catalog.private_identifiers:
        return False
    return not (entry.get("_TRANSPORT") in ("syslog", "journal") and entry.get("_COMM") in catalog.private_comm)


def _text(value) -> str:
    if isinstance(value, list):  # journalctl -o json: a non-UTF-8 field is a byte array
        return bytes(v & 0xFF for v in value if isinstance(v, int)).decode("utf-8", "replace")
    return "" if value is None else str(value)


def format_entry(entry: dict, mode: str) -> str:
    message = _text(entry.get("MESSAGE")).rstrip("\n")
    if mode == "cat":
        return message + "\n"
    try:
        moment = datetime.fromtimestamp(int(entry.get("__REALTIME_TIMESTAMP", "0")) / 1e6, tz=timezone.utc)
    except (TypeError, ValueError):
        moment = datetime.fromtimestamp(0, tz=timezone.utc)
    stamp = moment.strftime("%Y-%m-%dT%H:%M:%S%z") if mode == "short-iso" else moment.strftime("%b %d %H:%M:%S")
    ident = _text(entry.get("SYSLOG_IDENTIFIER") or entry.get("_COMM") or "?")
    pid = _text(entry.get("_PID") or entry.get("SYSLOG_PID") or "")
    return f"{stamp} {ident}{f'[{pid}]' if pid else ''}: {message}\n"


def read_journal(args: list, agent: str, catalog: Catalog, source: Callable, out: Capped,
                 reader: Callable = _operation_reader) -> int:
    if args == ["--units"]:
        if agent in catalog.full_journal:
            out.write("every unit (the full journal, without the private audit)\n")
        out.write("".join(f"{u}\n" for u in sorted(catalog.units)))
        out.write("and a factory operation of a card you lead: --operation <id>\n")
        return EXIT_OK
    argv, limit, mode, oldest_first = journal_request(args, agent, catalog, reader)
    full = agent in catalog.full_journal
    # the source is newest first: the budget keeps the newest entries, a cut drops the older ones
    kept, size, cut, timed_out = [], 0, False, False
    budget = LIMIT_BYTES - 200
    try:
        for line in source(argv):
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict) or not keep_entry(entry, full, catalog):
                continue
            text = format_entry(entry, mode)
            size += len(text.encode("utf-8", "replace"))
            if size > budget:
                cut = True
                break
            kept.append(text)
            if len(kept) >= limit:
                break
    except TimedOut:
        timed_out = True
    if cut and oldest_first:
        out.write(CUT_EARLIER)
    for text in reversed(kept) if oldest_first else kept:
        out.write(text)
    if cut and not oldest_first:
        out.write(CUT_OLDER)
    if timed_out:
        out.write(f"… timed out after {TIMEOUT} s: narrow the read (--since, -n, -g)\n")
        return EXIT_ERROR
    return EXIT_OK


def journal_source(argv: list) -> Iterator[str]:
    """journalctl's lines; it is killed when the reader stops or after TIMEOUT seconds, and then TimedOut is raised."""
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            env=clean_env(), close_fds=True)
    fired = threading.Event()

    def stop() -> None:
        fired.set()
        proc.kill()

    timer = threading.Timer(TIMEOUT, stop)
    timer.start()
    try:
        for raw in proc.stdout:
            yield raw.decode("utf-8", "replace")
    finally:
        timer.cancel()
        proc.kill()
        proc.wait()
    if fired.is_set():
        raise TimedOut()


# --- systemctl and fixed reads -------------------------------------------------------------------


def systemctl_args(args: list) -> list:
    if not args or args[0] not in SYSTEMCTL_VERBS:
        raise _usage("systemctl takes a read verb")
    verb, rest = args[0], args[1:]
    if len(rest) > 32:
        raise _usage("too many arguments")
    options, positional = _split(
        rest, {"-p": "property"}, {"--property": "property", "--state": "state", "--type": "type"},
        {flag: flag for flag in SYSTEMCTL_FLAGS} | {"--no-pager": "--no-pager"},
    )
    argv = [SYSTEMCTL, "--no-pager", "--no-ask-password", "--system"]
    if verb == "status":
        argv.append("--lines=0")
    argv.append(verb)
    for key, value in options:
        if key == "property":
            if not PROPERTY_RE.fullmatch(value):
                raise _usage("-p is a property name list")
            argv.append(f"--property={value}")
        elif key in ("state", "type"):
            if not WORD_RE.fullmatch(value):
                raise _usage(f"--{key} is a word list")
            argv.append(f"--{key}={value}")
        elif key != "--no-pager":
            argv.append(key)
    for name in positional:
        if not UNIT_ARG_RE.fullmatch(name):
            raise _usage(f"not a unit name or pattern: {name}")
    if positional:
        argv += ["--", *positional]
    return argv


def clean_env() -> dict:
    return {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "SYSTEMD_PAGER": "", "PAGER": "cat", "SYSTEMD_COLORS": "0",
            "SYSTEMD_PAGERSECURE": "1", "SYSTEMD_LESS": "", "LESSOPEN": "", "LESSCLOSE": ""}


def command_source(argv: list) -> tuple:
    """(exit code, stdout, stderr) of a fixed read with the call's time limit."""
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, env=clean_env(), timeout=TIMEOUT,
                              close_fds=True)
    except subprocess.TimeoutExpired:
        return 124, b"", b"kd-host-read: timed out\n"
    return done.returncode, done.stdout, done.stderr


def drop_process_tree(text: str) -> str:
    """systemctl status without the lines under each CGroup line (the process tree with full command lines)."""
    kept, in_tree = [], False
    for line in text.splitlines(keepends=True):
        match = CGROUP_RE.match(line.rstrip("\n"))
        if match:
            kept.append(match.group(1) + " (processes not shown)\n")
            in_tree = True
            continue
        if in_tree and (not line.strip() or line[:1].isspace()):
            continue  # the tree is indented; a blank line or a new unit's header (column 0) ends it
        in_tree = False
        kept.append(line)
    return "".join(kept)


def _program(argv: list, run: Callable, out: Capped) -> int:
    code, stdout, stderr = run(argv)
    text = stdout.decode("utf-8", "replace")
    if argv[:1] == [SYSTEMCTL] and "--lines=0" in argv:  # status (systemctl_args)
        text = drop_process_tree(text)
    out.write(text)
    out.write(stderr.decode("utf-8", "replace"))
    return code


# --- files -------------------------------------------------------------------------------------------


def _path(value: str, catalog: Catalog) -> str:
    if "\0" in value:
        raise _usage("a path has no NUL")
    if not value.startswith("/"):  # <root name>[/<path under it>]
        name, _, rest = value.partition("/")
        if name not in catalog.names:
            raise _usage(f"{name or value} is not a platform path name (kd-host-read paths)")
        value = catalog.names[name] + (f"/{rest}" if rest else "")
    real = os.path.realpath(value)
    if not any(_within(real, root) for root in catalog.roots):
        raise Refused(EXIT_DENIED, f"{value} is outside the readable platform paths (kd-host-read paths)\n")
    return real


def _open(value: str, catalog: Catalog, want_dir: bool = False) -> tuple:
    """(fd, stat) of a path under a root; a last-component link, a swap or a special file is refused."""
    real = _path(value, catalog)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    if want_dir:
        flags |= getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(real, flags)
    except OSError as exc:
        raise Refused(EXIT_DENIED, f"{value}: {exc.strerror}\n") from exc
    info = os.fstat(fd)
    seen = os.path.realpath(f"/proc/self/fd/{fd}")
    kind_ok = stat.S_ISDIR(info.st_mode) if want_dir else stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)
    if not kind_ok or not any(_within(seen, root) for root in catalog.roots):
        os.close(fd)
        raise Refused(EXIT_DENIED, f"{value}: not a readable file of the platform\n")
    return fd, info


def _lines(value: str, catalog: Catalog) -> Iterator[str]:
    fd, info = _open(value, catalog)
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise Refused(EXIT_DENIED, f"{value}: not a regular file\n")
    raw = os.fdopen(fd, "rb")
    stream = gzip.GzipFile(fileobj=raw) if value.endswith(".gz") else raw
    with raw, io.TextIOWrapper(stream, encoding="utf-8", errors="replace", newline="") as text:
        yield from text


def _ls(args: list, catalog: Catalog, out: Capped) -> int:
    options, positional = _split(args, {}, {}, {"-l": "long", "-a": "all", "-la": "both", "-al": "both"})
    if len(positional) != 1:
        raise _usage("ls takes one path")
    keys = {k for k, _ in options}
    long, every = bool(keys & {"long", "both"}), bool(keys & {"all", "both"})
    fd, info = _open(positional[0], catalog)
    try:
        if not stat.S_ISDIR(info.st_mode):
            out.write(_describe(positional[0].rsplit("/", 1)[-1], info, long))
            return EXIT_OK
        for entry in sorted(os.scandir(fd), key=lambda e: e.name):
            if every or not entry.name.startswith("."):
                out.write(_describe(entry.name, entry.stat(follow_symlinks=False), long))
    finally:
        os.close(fd)
    return EXIT_OK


def _owner(uid: int, gid: int) -> str:
    try:
        import grp
        import pwd

        return f"{pwd.getpwuid(uid).pw_name} {grp.getgrgid(gid).gr_name}"
    except (ImportError, KeyError):
        return f"{uid} {gid}"


def _describe(name: str, info: os.stat_result, long: bool) -> str:
    if not long:
        return name + "\n"
    moment = datetime.fromtimestamp(info.st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{stat.filemode(info.st_mode)} {_owner(info.st_uid, info.st_gid)} {info.st_size} {moment} {name}\n"


def files(op: str, args: list, catalog: Catalog, out: Capped) -> int:
    if op == "paths":
        out.write("".join(f"{name}  {root}\n" for name, root in sorted(catalog.names.items())))
        return EXIT_OK
    if op == "ls":
        return _ls(args, catalog, out)
    if op == "stat":
        if len(args) != 1:
            raise _usage("stat takes one path")
        fd, info = _open(args[0], catalog)
        os.close(fd)
        out.write(_describe(args[0], info, True))
        return EXIT_OK
    if op == "cat":
        if len(args) != 1:
            raise _usage("cat takes one path")
        for line in _lines(args[0], catalog):
            out.write(line)
            if out.full:
                break
        return EXIT_OK
    if op in ("head", "tail"):
        options, positional = _split(args, {"-n": "lines"}, {"--lines": "lines"}, {})
        if len(positional) != 1:
            raise _usage(f"{op} takes one path")
        count = 10
        for _, value in options:
            count = _int(value, 1, 100000, "-n")
        if op == "head":
            for number, line in enumerate(_lines(positional[0], catalog)):
                if number >= count or out.full:
                    break
                out.write(line)
        else:
            for line in collections.deque(_lines(positional[0], catalog), maxlen=count):
                out.write(line)
        return EXIT_OK
    if op == "grep":
        return _grep(args, catalog, out)
    raise _usage(f"unknown command {op}")


def _grep(args: list, catalog: Catalog, out: Capped) -> int:
    options, positional = _split(
        args, {"-m": "max", "-e": "pattern"}, {"--max-count": "max", "--regexp": "pattern"},
        {"-i": "i", "-n": "n", "-c": "c", "-v": "v", "-F": "F", "-E": "E"},
    )
    given = [value for key, value in options if key == "pattern"]
    if len(given) > 1 or len(positional) != 2 - len(given):
        raise _usage("grep takes a pattern (or -e PATTERN) and one path")
    keys = {k for k, _ in options}
    limit = None
    for key, value in options:
        if key == "max":
            limit = _int(value, 1, 100000, "-m")
    pattern = given[0] if given else positional.pop(0)
    pattern = re.escape(pattern) if "F" in keys else pattern
    try:
        regex = re.compile(pattern, re.IGNORECASE if "i" in keys else 0)
    except re.error as exc:
        raise _usage(f"bad pattern: {exc}") from exc
    found = 0
    for number, line in enumerate(_lines(positional[0], catalog), 1):
        if bool(regex.search(line)) == ("v" in keys):
            continue
        found += 1
        if "c" not in keys:
            out.write(f"{number}:{line}" if "n" in keys else line)
        if (limit and found >= limit) or out.full:
            break
    if "c" in keys:
        out.write(f"{found}\n")
    return EXIT_OK if found else 1


# --- entry -------------------------------------------------------------------------------------------

FILE_OPS = frozenset({"ls", "stat", "cat", "head", "tail", "grep", "paths"})


def main(argv: list, *, agent: str, catalog: Catalog, disabled: Iterable = (), journal=journal_source,
         command=command_source, out=None, operation_reader: Callable = _operation_reader) -> int:
    sink = Capped(out)
    try:
        if not agent:
            raise Refused(EXIT_DENIED, "kd-host-read: no calling agent (run it as yourself, not through another channel)\n")
        if agent in frozenset(disabled):
            raise Refused(EXIT_DENIED, f"host reads of @{agent} are switched off by root\n")
        if not argv:
            raise _usage("a command")
        op, args = argv[0], list(argv[1:])
        if op == "journalctl":
            return read_journal(args, agent, catalog, journal, sink, operation_reader)
        if op == "systemctl":
            return _program(systemctl_args(args), command, sink)
        if op == "sys":
            if len(args) != 1 or args[0] not in SYS_READS:
                raise _usage("sys df|free|uptime|ss|ps|ip")
            return _program(list(SYS_READS[args[0]]), command, sink)
        if op in FILE_OPS:
            return files(op, args, catalog, sink)
        raise _usage(f"unknown command {op}")
    except Refused as exc:
        sink.write(str(exc))
        return exc.code
    except (OSError, EOFError, zlib.error) as exc:  # a file vanished or a rotation is broken: one line, no traceback
        sink.write(f"kd-host-read: {exc.__class__.__name__}: {exc}\n")
        return EXIT_ERROR
