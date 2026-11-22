"""kd-diag — typed diagnostic help without the owner (R31).

    kd-diag check <target> --card KD-<n> [--key <key>]   probe one catalog target for your approved card
    kd-diag list                                         the catalog's targets and their probes

An agent whose work needs the health of a platform service (the memory API, its worker) does not ask the owner
to run curl: it asks kd-diag, the base user's executor, reached through the agent's base capability channel.
The catalog (``common/coordination/diagnostics.json``) names every target with one typed probe; nothing the
caller passes is a URL, a command or a header. ``http-health`` is one GET to a loopback ``http://`` URL with an
explicit port: the status line only — no redirect is followed, no body is read — or why it failed
(``refused``, ``timeout``, ``reset``, ``error``). ``cabinet-query`` names one of the cabinet's own
read-only queries (``kd.cabinet.diag_api.QUERIES``): the cabinet runs its fixed SQL, kd-diag prints the rows
after the result line and the thread keeps only their count; a query is never reused — each check reads anew.

Only the card's single assignee may check, and only while the card is in To Do or In Progress (the owner
approved the work). The result is a message of the card's thread (``diagnostic``, author ``system:diag``), so
the owner and the coordinator see what was checked and why. The same key (by default the target) on the card
within ``reuse_s`` answers the stored result without a new probe; at most ``per_card_hour`` new results per
card per hour. A target the catalog lacks is recorded as ``capability_missing``: platform work for @boss, not a
question for the owner.

Exit codes: 0 done, 1 error (the catalog or the cabinet), 2 refused, 4 capability_missing, 5 rate limit.
"""

import argparse
import json
import re
import socket
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional
from urllib.parse import urlsplit

from kd import clock, ids
from kd.cabinet.client import CabinetError

CATALOG = "/opt/digipaddock/common/coordination/diagnostics.json"
HTTP, QUERY = "http-health", "cabinet-query"
OPS = (HTTP, QUERY)
CATEGORIES = ("refused", "timeout", "reset", "error")
MISSING = "capability_missing"
NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")
KEY = re.compile(r"[a-z0-9][a-z0-9._:-]{0,63}")
CARD = ids.CARD
PATH = re.compile(r"/[A-Za-z0-9._~/-]{0,200}")
LOOPBACK = ("127.0.0.1", "::1")
STATUS_LINE = re.compile(rb"HTTP/1\.[01] ([1-5][0-9]{2})(?: [^\r\n]*)?")
STATUS_MAX = 1024
LIMITS = {"timeout_s": (1, 10), "per_card_hour": (1, 100), "reuse_s": (0, 3600)}
EXIT_ERROR, EXIT_REFUSED, EXIT_MISSING, EXIT_RATE = 1, 2, 4, 5


class DiagError(ValueError):
    """The catalog cannot be used; the text says why."""


class Refused(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Limits:
    timeout_s: int
    per_card_hour: int
    reuse_s: int


@dataclass(frozen=True)
class Target:
    name: str
    op: str
    url: str
    area: str
    query: str = ""


@dataclass(frozen=True)
class Catalog:
    limits: Limits
    targets: Dict[str, Target]


@dataclass(frozen=True)
class Probe:
    status: Optional[int]
    category: str  # http | refused | timeout | reset | error
    ms: int


def check_url(url) -> tuple:
    """(host, port, path) of a loopback ``http://host:port/path`` with nothing else in it, or DiagError."""
    if not isinstance(url, str) or any(c in url for c in "?#@ \t\r\n\\"):
        raise DiagError(f"{url!r}: only http://<loopback>:<port>/<path>")
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        port = None
    if parts.scheme != "http" or parts.hostname not in LOOPBACK or port is None or not PATH.fullmatch(parts.path):
        raise DiagError(f"{url!r}: only http://<loopback>:<port>/<path>")
    return parts.hostname, port, parts.path


def _exact(obj, keys, what):
    if not isinstance(obj, dict) or set(obj) != set(keys):
        raise DiagError(f"{what} must have exactly {sorted(keys)}")
    return obj


def load_catalog(path) -> Catalog:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise DiagError(f"{path}: {exc.__class__.__name__}") from exc
    _exact(data, ("schema_version", "limits", "targets"), "the catalog")
    if data["schema_version"] != 1:
        raise DiagError("schema_version must be 1")
    limits = _exact(data["limits"], LIMITS, "limits")
    for name, (low, high) in LIMITS.items():
        value = limits[name]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise DiagError(f"limits.{name} must be an integer {low}..{high}")
    if not isinstance(data["targets"], dict):
        raise DiagError("targets must be an object")
    targets = {}
    for name, entry in data["targets"].items():
        if not NAME.fullmatch(name):
            raise DiagError(f"invalid target name {name!r}")
        op = entry.get("op") if isinstance(entry, dict) else None
        if op not in OPS:
            raise DiagError(f"target {name}: op must be one of {list(OPS)}")
        _exact(entry, ("op", "url", "area") if op == HTTP else ("op", "query", "area"), f"target {name}")
        if not isinstance(entry["area"], str) or not NAME.fullmatch(entry["area"]):
            raise DiagError(f"target {name}: invalid area")
        if op == HTTP:
            check_url(entry["url"])
            targets[name] = Target(name, op, entry["url"], entry["area"])
        else:
            if not isinstance(entry["query"], str) or not NAME.fullmatch(entry["query"]):
                raise DiagError(f"target {name}: invalid query name")
            targets[name] = Target(name, op, "", entry["area"], entry["query"])
    return Catalog(Limits(**limits), targets)


def _status_line(sock, deadline: float) -> bytes:
    """The response's first line, read before ``deadline`` (one bound for the whole probe, not per read)."""
    data = b""
    while b"\r\n" not in data:
        left = deadline - time.monotonic()
        if left <= 0:
            raise socket.timeout("no status line in time")
        sock.settimeout(left)
        chunk = sock.recv(1024)
        if not chunk:
            raise ConnectionResetError("closed before a status line")
        data += chunk
        if len(data) > STATUS_MAX:
            raise ValueError("no status line")
    return data.split(b"\r\n", 1)[0]


def probe(url, timeout) -> Probe:
    """One GET: the status line and nothing more (no redirect, no body), or the category of the failure;
    ``timeout`` bounds the whole probe."""
    host, port, path = check_url(url)
    started = time.monotonic()
    name = f"[{host}]" if ":" in host else host
    request = (f"GET {path} HTTP/1.1\r\nHost: {name}:{port}\r\nConnection: close\r\nUser-Agent: kd-diag\r\n\r\n")
    status, category = None, "error"
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(request.encode("ascii"))
            found = STATUS_LINE.fullmatch(_status_line(sock, started + timeout))
            if found:
                status, category = int(found.group(1)), "http"
    except socket.timeout:
        category = "timeout"
    except ConnectionRefusedError:
        category = "refused"
    except ConnectionResetError:
        category = "reset"
    except (OSError, ValueError):
        category = "error"
    return Probe(status, category, int((time.monotonic() - started) * 1000))


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Refused(EXIT_REFUSED, f"usage: {message}")


def parse(argv):
    parser = _Parser(prog="kd-diag", allow_abbrev=False, add_help=False)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    check = commands.add_parser("check", allow_abbrev=False, add_help=False)
    check.add_argument("target")
    check.add_argument("--card", required=True)
    check.add_argument("--key")
    commands.add_parser("list", allow_abbrev=False, add_help=False)
    args = parser.parse_args(argv)
    if args.command == "check":
        if not NAME.fullmatch(args.target):
            raise Refused(EXIT_REFUSED, "usage: the target is a catalog name (kd-diag list)")
        if not CARD.fullmatch(args.card):
            raise Refused(EXIT_REFUSED, "usage: --card KD-<n>")
        if args.key is not None and not KEY.fullmatch(args.key):
            raise Refused(EXIT_REFUSED, "usage: --key is [a-z0-9][a-z0-9._:-]{0,63}")
    return args


def channel_caller(environ):
    """(agent, Unix user) of the capability channel that started this call, or None."""
    agent, user = environ.get("KD_AGENT", ""), environ.get("KD_CALLER_USER", "")
    return (agent, user) if agent or user else None


def _call(client, request):
    try:
        return client.call(request)
    except CabinetError as exc:
        if exc.kind in ("refused", "invalid", "not_found"):
            raise Refused(EXIT_REFUSED, str(exc)) from exc
        raise Refused(EXIT_ERROR, f"the cabinet is unavailable ({exc})") from exc
    except OSError as exc:
        raise Refused(EXIT_ERROR, f"the cabinet is unavailable ({exc.__class__.__name__})") from exc


def _show(target, record, reused, out, lines=()) -> int:
    if record.get("category") == MISSING:
        out.write(f"capability_missing: no diagnostic {target} in the catalog (kd-diag list); ask @boss for"
                  " platform work; do not ask the owner to run it\n")
        return EXIT_MISSING
    if "status" in record:
        result = f"HTTP {record['status']}"
    elif "rows" in record:
        result = f"{record['rows']} rows"
    else:
        result = record["category"]
    line = f"diag {target}: {result} at {record['at']} (checked by kd-diag for @{record['agent']})"
    out.write(line + (" (reused)" if reused else "") + "\n")
    out.writelines(f"{row}\n" for row in lines)
    return 0


def _query(client, card, agent, entry):
    """(outcome, lines) of a cabinet query; the cabinet checks the caller and the card again."""
    started = time.monotonic()
    lines = _call(client, {"op": "diag.query", "card": card, "agent": agent, "query": entry.query})["lines"]
    return {"rows": len(lines), "ms": int((time.monotonic() - started) * 1000)}, lines


def check(args, catalog: Catalog, *, caller, client, probe, now, out) -> int:
    if caller is None or not NAME.fullmatch(caller[0] or "") or not caller[1]:
        raise Refused(EXIT_REFUSED, "no capability channel names the caller")
    agent, target, key, limits = caller[0], args.target, args.key or args.target, catalog.limits
    entry = catalog.targets.get(target)
    reuse = 0 if entry is not None and entry.op == QUERY else limits.reuse_s  # rows are read anew every time
    found = _call(client, {"op": "diag.recent", "card": args.card, "agent": agent, "target": target, "key": key,
                           "within_s": reuse})
    if found["record"] is not None:
        return _show(target, found["record"], True, out)
    if found["count_hour"] >= limits.per_card_hour:
        raise Refused(EXIT_RATE, f"rate limit: {limits.per_card_hour} diagnostics per card per hour; the earlier"
                                 f" results are in the thread of {args.card}")
    lines = []
    if entry is None:
        op, area, outcome = "", "", {"category": MISSING, "ms": 0}
    elif entry.op == QUERY:
        op, area = entry.op, entry.area
        outcome, lines = _query(client, args.card, agent, entry)
    else:
        result = probe(entry.url, limits.timeout_s)
        op, area = entry.op, entry.area
        outcome = ({"status": result.status, "ms": result.ms} if result.category == "http"
                   else {"category": result.category, "ms": result.ms})
    written = _call(client, {"op": "diag.record", "card": args.card, "agent": agent, "target": target, "probe": op,
                             "key": key, "area": area, "outcome": outcome, "at": clock.iso(now()), "reuse_s": reuse})
    return _show(target, written["record"], written["reused"], out, lines)


def main(argv=None, *, caller, client, catalog_path=CATALOG, probe=probe, now=lambda: datetime.now(timezone.utc),
         out=sys.stdout, err=sys.stderr) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = parse(argv)
        catalog = load_catalog(catalog_path)
        if args.command == "list":
            for name in sorted(catalog.targets):
                out.write(f"{name} {catalog.targets[name].op}\n")
            return 0
        return check(args, catalog, caller=caller, client=client, probe=probe, now=now, out=out)
    except Refused as exc:
        err.write(f"kd-diag: {exc}\n")
        return exc.code
    except DiagError as exc:
        err.write(f"kd-diag: the catalog is broken: {exc}\n")
        return EXIT_ERROR
