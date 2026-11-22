"""Diagnostics in a card's thread (R31): what kd-diag checked for the card's assignee.

kd-diag (the base user's executor) probes a catalog target and records the result here: one ``diagnostic``
message of the card's thread, author ``system:diag``, whose catalog text names the target, the result, the time
and the agent that asked, and whose ``diag`` payload keeps the structured result. The same (target, key) on the
card within the executor's reuse window answers the stored message, nothing new. Both operations are the
executor's only, and only for the card's single assignee while the card is in To Do or In Progress.

``diag.query`` runs one of the cabinet's own read-only queries (``QUERIES``) for the same caller and
card: fixed SQL, rows as ``section<TAB>object<TAB>key=value …`` lines, with no secret in them — a push
subscription shows its endpoint's host only, never the endpoint, its keys or a notification's body. The
executor prints the lines to the agent and records only their count in the thread (``rows``).
"""

import json
import re
from typing import Optional
from urllib.parse import urlsplit

from kd import agents, ids
from kd.cabinet import api, store, users as people

AUTHOR = "system:diag"
APPROVED = ("To Do", "In Progress")
CARD = ids.CARD
NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")
KEY = re.compile(r"[a-z0-9][a-z0-9._:-]{0,63}")
STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
HTTP, QUERY = "http-health", "cabinet-query"
PROBES = (HTTP, QUERY)
CATEGORIES = ("refused", "timeout", "reset", "error")
MISSING = "capability_missing"
WINDOW_MAX = 3600
MS_MAX = 600_000
ROWS_MAX = 10_000
LAST = 40  # the newest notifications and deliveries a push query shows
VALUE_MAX = 60
RECORD_FIELDS = frozenset({"op", "card", "agent", "target", "probe", "key", "area", "outcome", "at", "reuse_s"})
QUERY_FIELDS = frozenset({"op", "card", "agent", "query"})


def _invalid(message):
    return api.ApiError("invalid", message)


def _text(value, pattern, name) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise _invalid(f"invalid {name}")
    return value


def _count(value, low, high, name) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise _invalid(f"{name} must be an integer {low}..{high}")
    return value


def _agent(ctx, name) -> str:
    _text(name, agents.NAME_RE, "agent")
    try:
        names = agents.names(ctx.registry_path)
    except ValueError as exc:
        raise api.ApiError("unavailable", str(exc))
    if name not in names:
        raise _invalid(f"{name} is not a registered agent")
    return name


def _approved(ctx, card_id: str, agent: str):
    """The card, when ``agent`` is its single assignee and the owner approved its work."""
    try:
        card = ctx.store.card(card_id)
    except store.NotFound:
        raise api.ApiError("not_found", f"{card_id} not found")
    if card.assignee != "@" + agent or card.status not in APPROVED:
        raise api.ApiError("refused", f"{card_id}: not your approved card (you alone, in To Do or In Progress)")
    return card


def _outcome(value, probe: str) -> dict:
    """``{status, ms}`` of an answered probe, ``{rows, ms}`` of a query, or ``{category, ms}`` of a failed probe
    or a missing target."""
    if not isinstance(value, dict) or set(value) not in ({"status", "ms"}, {"rows", "ms"}, {"category", "ms"}):
        raise _invalid("outcome is {status, ms}, {rows, ms} or {category, ms}")
    ms = _count(value["ms"], 0, MS_MAX, "outcome.ms")
    if "status" in value:
        if probe != HTTP:
            raise _invalid("a status needs an http probe")
        return {"status": _count(value["status"], 100, 599, "outcome.status"), "ms": ms}
    if "rows" in value:
        if probe != QUERY:
            raise _invalid("rows need a query")
        return {"rows": _count(value["rows"], 0, ROWS_MAX, "outcome.rows"), "ms": ms}
    category = value["category"]
    if category == MISSING and probe == "":
        return {"category": category, "ms": ms}
    if category in CATEGORIES and probe in PROBES:
        return {"category": category, "ms": ms}
    raise _invalid("unknown category, or a probe with a missing target")


def _since(ctx, seconds: int) -> str:
    return people.shift(ctx.now(), seconds=-seconds)


def _found(s, thread: int, target: str, key: str, since: str) -> Optional[tuple]:
    """(seq, diag) of the newest diagnostic of (target, key) in the thread since ``since``."""
    rows = s._conn.execute(
        "SELECT seq, payload FROM messages WHERE thread=? AND kind='diagnostic' AND author=? AND created>=?"
        " ORDER BY seq DESC",
        (thread, AUTHOR, since),
    ).fetchall()
    for row in rows:
        diag = json.loads(row["payload"]).get("diag") or {}
        if diag.get("target") == target and diag.get("key") == key:
            return row["seq"], diag
    return None


def _hour(ctx, thread: int) -> int:
    return ctx.store._conn.execute(
        "SELECT COUNT(*) FROM messages WHERE thread=? AND kind='diagnostic' AND author=? AND created>=?",
        (thread, AUTHOR, _since(ctx, 3600)),
    ).fetchone()[0]


def _line(diag: dict) -> tuple:
    """The catalog key and parameters of the thread line."""
    if diag.get("category") == MISSING:
        return "diag.missing", {"target": diag["target"]}
    common = {"target": diag["target"], "time": diag["at"], "agent": "@" + diag["agent"]}
    if "status" in diag:
        return "diag.result.http", {**common, "status": str(diag["status"])}
    if "rows" in diag:
        return "diag.result.rows", {**common, "rows": str(diag["rows"])}
    return "diag.result.error", {**common, "category": diag["category"]}


def _recent(ctx, principal, request):
    card_id = _text(request.get("card"), CARD, "card")
    agent = _agent(ctx, request.get("agent"))
    target = _text(request.get("target"), NAME, "target")
    key = _text(request.get("key"), KEY, "key")
    within = _count(request.get("within_s"), 0, WINDOW_MAX, "within_s")
    card = _approved(ctx, card_id, agent)
    found = _found(ctx.store, card.thread, target, key, _since(ctx, within)) if within else None
    return {"record": found[1] if found else None, "count_hour": _hour(ctx, card.thread)}


def _record(ctx, principal, request):
    if set(request) != RECORD_FIELDS:
        raise _invalid(f"diag.record takes exactly {sorted(RECORD_FIELDS - {'op'})}")
    card_id = _text(request["card"], CARD, "card")
    agent = _agent(ctx, request["agent"])
    target = _text(request["target"], NAME, "target")
    key = _text(request["key"], KEY, "key")
    probe = request["probe"]
    if probe != "" and probe not in PROBES:
        raise _invalid("unknown probe")
    if probe == "" and request["area"] != "":
        raise _invalid("a missing target has no area")
    area = "" if probe == "" else _text(request["area"], NAME, "area")
    outcome = _outcome(request["outcome"], probe)
    at = _text(request["at"], STAMP, "at")
    reuse = _count(request["reuse_s"], 0, WINDOW_MAX, "reuse_s")
    s = ctx.store
    with s.transaction():
        card = _approved(ctx, card_id, agent)
        found = _found(s, card.thread, target, key, _since(ctx, reuse)) if reuse else None
        if found is not None:
            return {"record": found[1], "message": found[0], "reused": True}
        diag = {"target": target, "op": probe, "key": key, **outcome, "at": at, "agent": agent, "area": area}
        text_key, params = _line(diag)
        message = s.add_thread_message(
            card.thread, author=AUTHOR, kind="diagnostic", body="",
            payload={"key": text_key, "params": params, "diag": diag}, via="diag.recorded",
        )
        s._event(AUTHOR, "diag.recorded", card.id, {"target": target, "key": key, "agent": agent, **outcome},
                 card.thread)
        return {"record": diag, "message": message["seq"], "reused": False}


def _clean(value) -> str:
    """A value of a row: one printable line, short; empty — ``-``."""
    return re.sub(r"[\x00-\x1f\x7f\s]+", " ", str(value)).strip()[:VALUE_MAX] or "-"


def _host(endpoint) -> str:
    """The push gateway's host of a subscription: the endpoint's path is the device's token, never shown."""
    try:
        return _clean(urlsplit(endpoint).hostname or "")
    except ValueError:
        return "-"


def _pairs(row, names) -> str:
    return " ".join(f"{name}={_clean(row[name])}" for name in names)


def _push(conn) -> list:
    """what the push path holds — subscriptions (host only), each person's preferences and quiet hours,
    the newest ``LAST`` notifications (title, no body) and device deliveries."""
    lines = []
    for row in conn.execute("SELECT id, user, device, created, last_ok, endpoint FROM push_subscriptions ORDER BY id"):
        lines.append(f"sub\t{row['id']}\tuser={_clean(row['user'])} host={_host(row['endpoint'])} "
                     + _pairs(row, ("created", "last_ok", "device")))
    for row in conn.execute("SELECT user, kind, mode FROM notification_prefs ORDER BY user, kind"):
        lines.append(f"pref\t{_clean(row['user'])}\t{_pairs(row, ('kind', 'mode'))}")
    for row in conn.execute(
            "SELECT id, user, kind, tag, due, sent, result, attempts, tried, reply, title FROM notifications"
            " ORDER BY id DESC LIMIT ?", (LAST,)):
        lines.append(f"note\t{row['id']}\t" + _pairs(row, ("user", "kind", "tag", "due", "sent", "result", "attempts",
                                                              "tried", "reply", "title")))
    for row in conn.execute(
            "SELECT notification, subscription, sent, result, attempts, tried FROM push_deliveries"
            " ORDER BY notification DESC, subscription LIMIT ?", (LAST,)):
        lines.append(f"delivery\t{row['notification']}\t"
                     + _pairs(row, ("subscription", "sent", "result", "attempts", "tried")))
    return lines


QUERIES = {"push": _push}


def _query(ctx, principal, request):
    if set(request) != QUERY_FIELDS:
        raise _invalid(f"diag.query takes exactly {sorted(QUERY_FIELDS - {'op'})}")
    card_id = _text(request["card"], CARD, "card")
    agent = _agent(ctx, request["agent"])
    name = _text(request["query"], NAME, "query")
    if name not in QUERIES:
        raise _invalid(f"unknown query; known: {sorted(QUERIES)}")
    _approved(ctx, card_id, agent)
    return {"lines": QUERIES[name](ctx.store._conn)}


OPS = {
    "diag.recent": (frozenset({"executor"}), _recent),
    "diag.record": (frozenset({"executor"}), _record),
    "diag.query": (frozenset({"executor"}), _query),
}
api.OPS.update(OPS)  # the board API serves them (api imports this module at its end)
