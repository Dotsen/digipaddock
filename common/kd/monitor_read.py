"""kd-monitor-read — the monitor's incidents, servers and checks for an agent, read-only.

    kd-monitor-read incidents [--status S] [--server A] [--level L] [--since T] [--until T] [--q TEXT]
                              [--limit N] [--cursor C] [--json]
    kd-monitor-read incident P-<n> [--json]          status, level, server, check, value, timeline, cards
    kd-monitor-read servers [--json]                 every server with its worst level and open incidents
    kd-monitor-read server <alias> [--since T] [--all] [--limit N] [--json]
                                                     checks, open incidents, cards, the week's history

The base user's executor, reached through the agent's base capability channel like kd-task: the channel names the
caller (``KD_AGENT``, ``KD_CALLER_USER``); the caller must be the agent's own user and its manifest must hold
``monitor.read``. The cabinet checks that manifest again (``kd.cabinet.api.executor_capability``) and serves only
its reads — ``incident.query``, ``incident.get``, ``server.list``, ``server.get``; nothing here acknowledges, mutes,
trusts, links or asks for a check.

Every string of the cabinet's answer goes through the gate's masks (``kd.redact.text``) before anything is printed;
when the masks cannot be loaded the command prints nothing and fails. ``server`` shows by default only the status
changes of the week (``level`` differs from ``prev``), newest first, at most ``--limit``; ``--all`` shows every row.

Exit codes: 0 done, 1 error (the cabinet, the masks or the manifest), 2 refused or invalid.
"""

import argparse
import json
import re
import shlex
import sys
from pathlib import Path

from kd import agents, redact
from kd.cabinet.client import CabinetError
from kd.manifest import ManifestError, load_manifest

CAPABILITY = "monitor.read"
REPO_ROOT = Path("/opt/digipaddock")
EXIT_ERROR, EXIT_REFUSED = 1, 2
STATUSES = ("active", "open", "acknowledged", "resolved", "all")
LEVELS = ("warn", "crit")
INCIDENT = re.compile(r"P-[1-9][0-9]{0,8}")
ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
MOMENT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(T[0-9]{2}:[0-9]{2}:[0-9]{2}Z)?")  # UTC, as the cabinet stores it
INCIDENTS_DEFAULT, INCIDENTS_MAX = 20, 100
HISTORY_DEFAULT, HISTORY_MAX = 50, 1000
LINE_MAX = 400  # characters of one printed line; the --json answer is whole


class Refused(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Refused(EXIT_REFUSED, f"usage: {message}")


def _count(low, high):
    def parse_(value):
        if not value.isdigit() or not low <= int(value) <= high:
            raise argparse.ArgumentTypeError(f"a number from {low} to {high}")
        return int(value)
    return parse_


def _moment(value):
    if not MOMENT.fullmatch(value):
        raise argparse.ArgumentTypeError("a date (YYYY-MM-DD) or a UTC moment (YYYY-MM-DDTHH:MM:SSZ)")
    return value


def parse(argv):
    parser = _Parser(prog="kd-monitor-read", allow_abbrev=False, add_help=False)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    found = commands.add_parser("incidents", allow_abbrev=False, add_help=False)
    found.add_argument("--status", choices=STATUSES, default="active")
    found.add_argument("--server")
    found.add_argument("--level", choices=LEVELS)
    found.add_argument("--since", type=_moment)
    found.add_argument("--until", type=_moment)
    found.add_argument("--q")
    found.add_argument("--limit", type=_count(1, INCIDENTS_MAX), default=INCIDENTS_DEFAULT)
    found.add_argument("--cursor")
    one = commands.add_parser("incident", allow_abbrev=False, add_help=False)
    one.add_argument("incident")
    commands.add_parser("servers", allow_abbrev=False, add_help=False)
    server = commands.add_parser("server", allow_abbrev=False, add_help=False)
    server.add_argument("alias")
    server.add_argument("--since", type=_moment)
    server.add_argument("--all", action="store_true")
    server.add_argument("--limit", type=_count(1, HISTORY_MAX), default=HISTORY_DEFAULT)
    for sub in (found, one, commands.choices["servers"], server):
        sub.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "incident" and not INCIDENT.fullmatch(args.incident):
        raise Refused(EXIT_REFUSED, "usage: the incident is P-<n>")
    if getattr(args, "server", None) is not None and not ALIAS.fullmatch(args.server):
        raise Refused(EXIT_REFUSED, "usage: --server is a server alias")
    if args.command == "server" and not ALIAS.fullmatch(args.alias):
        raise Refused(EXIT_REFUSED, "usage: the server is its alias (kd-monitor-read servers)")
    if getattr(args, "q", None) is not None and len(args.q) > 200:
        raise Refused(EXIT_REFUSED, "usage: --q is at most 200 characters")
    if getattr(args, "cursor", None) is not None and len(args.cursor) > 1000:
        raise Refused(EXIT_REFUSED, "usage: --cursor is the one the last page printed")
    return args


def caller(environ, registry, repo_root) -> str:
    """The agent of the capability channel, its own user calling, with monitor.read in its deployed manifest."""
    agent, user = environ.get("KD_AGENT", ""), environ.get("KD_CALLER_USER", "")
    if not agents.NAME_RE.fullmatch(agent) or not user:
        raise Refused(EXIT_REFUSED, "no capability channel names the caller")
    try:
        own = agents.user_of(agent, registry)
    except ValueError as exc:
        raise Refused(EXIT_REFUSED, str(exc)) from exc
    if own == agents.DEFAULT_USER or own != user:
        raise Refused(EXIT_REFUSED, "the caller is not the agent's own user")
    try:
        manifest = load_manifest(Path(repo_root) / "agents" / agent / "agent.json")
    except (ManifestError, OSError) as exc:
        raise Refused(EXIT_ERROR, f"agent manifest is not readable ({exc.__class__.__name__})") from exc
    if CAPABILITY not in manifest.capabilities:
        raise Refused(EXIT_REFUSED, f"kd-monitor-read needs the {CAPABILITY} capability")
    return agent


def masked(value):
    """``value`` with every string through the gate's masks; Refused when the masks are unavailable."""
    if isinstance(value, str):
        out = redact.text(value)
        if out is None:
            raise Refused(EXIT_ERROR, "the secret masks are unavailable; nothing is shown")
        return out
    if isinstance(value, dict):
        return {masked(k): masked(v) for k, v in value.items()}
    if isinstance(value, list):
        return [masked(v) for v in value]
    return value


def _call(client, request):
    try:
        return client.call(request)
    except CabinetError as exc:
        if exc.kind in ("refused", "invalid", "not_found"):
            raise Refused(EXIT_REFUSED, str(exc)) from exc
        raise Refused(EXIT_ERROR, f"the cabinet is unavailable ({exc})") from exc
    except OSError as exc:
        raise Refused(EXIT_ERROR, f"the cabinet is unavailable ({exc.__class__.__name__})") from exc


def request_of(args, agent) -> dict:
    if args.command == "incidents":
        request = {"op": "incident.query", "status": args.status, "limit": args.limit}
        for key in ("server", "level", "since", "until", "q", "cursor"):
            if getattr(args, key) is not None:
                request[key] = getattr(args, key)
    elif args.command == "incident":
        request = {"op": "incident.get", "incident": args.incident}
    elif args.command == "servers":
        request = {"op": "server.list"}
    else:
        request = {"op": "server.get", "alias": args.alias}
    return {**request, "agent": agent}


# --- text ----------------------------------------------------------------------------------------------
def _cut(line: str) -> str:
    """One printed line: the indent kept, newlines and runs of blanks of the values folded, cut at LINE_MAX."""
    indent = line[:len(line) - len(line.lstrip(" "))]
    line = indent + " ".join(line.split())
    return line if len(line) <= LINE_MAX else line[:LINE_MAX - 1] + "…"


def _subject(row) -> str:
    return f"{row.get('check', '')} {row.get('subject', '')}".strip()


def _card(card) -> str:
    return f"{card['id']} · {card['status']} · {card.get('assignee') or '—'} · {card.get('title', '')}"


def _incident_line(row) -> str:
    return (f"{row['id']} · {row['level']} · {row['status']} · {row['server']} · {_subject(row)} · "
            f"{row.get('value', '')} · opened {row['opened_at']}")


def _payload(payload: dict) -> str:
    skip = ("incident", "server", "check", "subject")
    parts = []
    for key, value in payload.items():
        if key in skip or value in ("", None, [], {}):
            continue
        shown = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        parts.append(f"{key}={shown}")
    return " ".join(parts)


def incidents_text(answer, args) -> list:
    counts = answer.get("counts") or {}
    lines = [f"incidents ({args.status}): {answer['total']} — crit {counts.get('crit', 0)}, warn {counts.get('warn', 0)}"]
    lines += [_incident_line(row) + (f" · {row['assignee']}" if row.get("assignee") else "")
              for row in answer["items"]]
    if answer.get("next_cursor"):
        flags = "".join(f" --{key} {getattr(args, key)}" for key in ("status", "server", "level", "since", "until")
                        if getattr(args, key) is not None)
        flags += f" --q {shlex.quote(args.q)}" if args.q is not None else ""
        lines.append(f"more: kd-monitor-read incidents{flags} --limit {args.limit} --cursor {answer['next_cursor']}")
    return lines


def incident_text(answer) -> list:
    row = answer["incident"]
    lines = [f"{row['id']} · {row['level']} (max {row['max_level']}) · {row['status']} · server {row['server']} · "
             f"check {_subject(row)} · value {row.get('value', '')}"]
    if row.get("text"):
        lines.append(f"text: {row['text']}")
    when = [f"opened {row['opened_at']}", f"last seen {row['last_seen']}"]
    when += [f"{label} {row[key]}" for key, label in (("resolved_at", "resolved"), ("muted_until", "muted until"),
                                                      ("fixed_card", "fixed by")) if row.get(key)]
    lines.append(" · ".join(when))
    current = row.get("current")
    if current:
        lines.append(f"current: {current['level']} {current.get('value', '')} at {current.get('at', '')} "
                     f"({current.get('freshness', '')})")
    investigation = row.get("investigation") or {}
    if investigation.get("state"):
        report = (investigation.get("report") or {}).get("summary", "")
        lines.append(f"investigation: {investigation['state']}" + (f" — {report}" if report else ""))
    timeline = answer.get("timeline") or []
    lines.append(f"timeline ({len(timeline)}):")
    lines += [f"  {e['ts']} {e['kind']} by {e['actor']}: {_payload(e.get('payload') or {})}" for e in timeline]
    cards = answer.get("cards") or []
    lines.append(f"cards ({len(cards)}):" if cards else "cards: none")
    lines += [f"  {_card(c)}" for c in cards]
    return lines


def servers_text(answer) -> list:
    lines = []
    for s in answer["items"]:
        checks = s.get("checks") or {}
        state = "reachable" if s.get("reachable") else f"unreachable ×{s.get('unreachable_count', 0)}"
        lines.append(f"{s['alias']} · {s.get('worst') or '—'} · {state}" + ("" if s.get("enabled") else " · disabled")
                     + f" · checks crit {checks.get('crit', 0)} warn {checks.get('warn', 0)} ok {checks.get('ok', 0)}"
                     f" · open incidents {s.get('incidents', 0)} · observed {s.get('observed_at') or '—'}")
    return lines or ["no servers"]


def _since(value: str) -> str:
    return value + "T00:00:00Z" if len(value) == 10 else value


def server_text(answer, args) -> list:
    s = answer["server"]
    state = "reachable" if s.get("reachable") else f"unreachable ×{s.get('unreachable_count', 0)}"
    lines = [f"server {s['alias']} · {s.get('worst') or '—'} · {state}" + ("" if s.get("enabled") else " · disabled")
             + f" · observed {s.get('observed_at') or '—'} ({s.get('freshness', '')})"]
    checks = answer.get("checks") or []
    lines.append(f"checks ({len(checks)}):")
    order = {"crit": 0, "warn": 1, "ok": 2}
    for c in sorted(checks, key=lambda c: (order.get(c["level"], 3), c["check"], c.get("subject", ""))):
        lines.append(f"  {c['level']} {_subject(c)} {c.get('value', '')} at {c.get('at', '')} ({c.get('freshness', '')})"
                     + (f" — {c['text']}" if c.get("text") else ""))
    active = [i for i in answer.get("incidents") or [] if i["status"] != "resolved"]
    lines.append(f"open incidents ({len(active)}):" if active else "open incidents: none")
    lines += [f"  {_incident_line(i)}" for i in active]
    cards = answer.get("cards") or []
    lines.append(f"cards ({len(cards)}):" if cards else "cards: none")
    lines += [f"  {_card(c)}" for c in cards]
    history = answer.get("history") or []
    if not args.all:
        history = [h for h in history if h.get("level") != h.get("prev")]
    if args.since:
        history = [h for h in history if h.get("at", "") >= _since(args.since)]
    shown = history[:args.limit]
    what = "every row" if args.all else "status changes"
    lines.append(f"history ({what}, 7 days): {len(shown)} of {len(history)}")
    lines += [f"  {h['at']} {_subject(h)} {h.get('prev', '')}→{h['level']} {h.get('value', '')}"
              + (f" — {h['text']}" if h.get("text") else "") for h in shown]
    if len(shown) < len(history):
        lines.append(f"more: kd-monitor-read server {args.alias} --since <later moment> or --limit {HISTORY_MAX}")
    return lines


def render(args, answer) -> str:
    answer = {k: v for k, v in answer.items() if k != "ok"}
    if args.json:
        return json.dumps(answer, ensure_ascii=False, separators=(",", ":")) + "\n"
    lines = {"incidents": lambda: incidents_text(answer, args), "incident": lambda: incident_text(answer),
             "servers": lambda: servers_text(answer), "server": lambda: server_text(answer, args)}[args.command]()
    return "".join((line if line.startswith("more: ") else _cut(line)) + "\n" for line in lines)  # a command whole


def main(argv=None, *, env, client, registry_path=agents.REGISTRY, repo_root=REPO_ROOT,
         out=sys.stdout, err=sys.stderr) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        agent = caller(env, registry_path, repo_root)
        args = parse(argv)
        answer = masked(_call(client, request_of(args, agent)))
        text = render(args, answer)  # whole before the first byte: a failure prints nothing
    except Refused as exc:
        err.write(f"kd-monitor-read: {exc}\n")
        return exc.code
    out.write(text)
    return 0
