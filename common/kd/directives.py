"""The directive store: the coordinator's verified instructions to one worker on one card.

One JSON file of the base user, rewritten whole under its lock (``kd.atomic_file``). A directive
is ``pending`` until its wake was queued as a cabinet command for the addressee's session (``delivered``), ``read`` once the
addressee showed it with ``kd-work directive <id>``, then ``replied`` or, after ``TTL``,
``expired``. Only the addressee reads and answers it; a repeated send of the same open directive
returns it, and a repeated identical answer changes nothing. A reply or an expiry stays
``unseen`` until the coordinator lists it; ``bossTold`` remembers which of them a successful wake already
announced, so a new one is announced at once. A withdrawn directive the worker may already have waits in
``notices`` until the worker is told. Closed directives are kept for ``KEEP`` seconds.
A damaged store raises ``CorruptStore``: it is never read as empty.

This module checks the directive itself; whether the coordinator may send it (the card, its
assignee and status) is the executor's decision (``kd-nudge direct``).
"""

import json
import re
import secrets
from dataclasses import asdict, dataclass, fields, replace
from typing import Iterable, List, Optional, Tuple

from kd import atomic_file, ids

STORE = "/var/log/digipaddock-board/directives.json"
SCHEMA = 1
KINDS = ("work", "clarify", "status", "pause")
REPLIES = ("accepted", "done", "blocked", "refused")
OPEN = ("pending", "delivered", "read")
# Which kinds a card's status admits (spec 5.1): work only on a card the owner already approved.
KIND_STATUSES = {
    "work": ("To Do", "In Progress", "Blocked"),
    "clarify": ("Inbox", "To Do", "In Progress", "Blocked", "Review"),
    "status": ("Inbox", "To Do", "In Progress", "Blocked", "Review"),
    "pause": ("Inbox", "To Do", "In Progress", "Blocked", "Review"),
}
# What each kind allows; the worker sees this line with the text.
KIND_MEANING = {
    "work": "start or continue the work on this card within your own rules and approvals",
    "clarify": "answer the question; do not start or change the work",
    "status": "report where the card stands; do not start or change the work",
    "pause": "stop at the next safe point and report",
}
GRANT_KINDS = ("work", "clarify")  # on a subtask of a revoked or expired grant only status and pause remain
TTL = 24 * 3600
KEEP = 7 * 24 * 3600
BOSS_WAKE_INTERVAL = 3600
# What the worker reads on an expired or withdrawn directive (the withdrawal notice leads it here).
CLOSED = "this directive is closed (withdrawn or expired): stop the work it asked for at a safe point and do not reply"
MAX_TEXT = 4096
MAX_BYTES = 1024 * 1024
MAX_OPEN = 50

_ID = re.compile(r"[0-9a-f]{16}")
_AGENT = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_CARD = ids.CARD
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f​-‏ -‮⁠-⁩﻿]")


class DirectiveError(Exception):
    """Nothing was changed."""


class Refused(DirectiveError):
    """The request itself is invalid."""


class Missing(DirectiveError):
    """No such directive."""


class NotYours(DirectiveError):
    """The directive is addressed to another agent."""


class Closed(DirectiveError):
    """The directive expired or was already answered differently."""


class CorruptStore(DirectiveError):
    """The store exists but is not a valid directive store."""


@dataclass(frozen=True)
class Directive:
    id: str
    agent: str
    card: str
    kind: str
    message: str
    revision: int  # the card's cabinet revision when the directive was sent
    created: float
    expires: float
    state: str
    attempts: int
    delivered: float
    reply: str
    reply_message: str
    replied: float
    seen: bool


_FIELDS = {f.name for f in fields(Directive)}


def check_message(text: object) -> str:
    """One line of plain text, at most ``MAX_TEXT`` UTF-8 bytes."""
    if not isinstance(text, str) or _CONTROL.search(text):
        raise Refused("the message must be one line of plain text")
    text = text.strip()
    if not text or len(text.encode("utf-8")) > MAX_TEXT:
        raise Refused(f"the message is empty or longer than {MAX_TEXT} bytes")
    return text


def _check(agent: str, card: str, kind: str, revision: int) -> None:
    if not isinstance(agent, str) or not _AGENT.fullmatch(agent):
        raise Refused("invalid agent")
    if not isinstance(card, str) or not _CARD.fullmatch(card):
        raise Refused("invalid card")
    if kind not in KINDS:
        raise Refused(f"kind must be one of {', '.join(KINDS)}")
    if type(revision) is not int or revision < 1:
        raise Refused("invalid card revision")


def _load(path: str) -> dict:
    data = atomic_file.read(path, MAX_BYTES)
    if not data:
        return {"schemaVersion": SCHEMA, "bossWoken": 0.0, "directives": []}
    if len(data) > MAX_BYTES:
        raise CorruptStore("the directive store is too large")
    try:
        raw = json.loads(data.decode("utf-8"))
        if not isinstance(raw, dict) or raw.get("schemaVersion") != SCHEMA:
            raise ValueError("schema")
        items = raw["directives"]
        if not isinstance(items, list) or not isinstance(raw.get("bossWoken"), (int, float)):
            raise ValueError("shape")
        for key in ("notices", "bossTold"):
            ids = raw.get(key, [])
            if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
                raise ValueError(key)
        for item in items:
            if not isinstance(item, dict) or set(item) != _FIELDS or not _ID.fullmatch(str(item["id"])):
                raise ValueError("directive")
    except (UnicodeDecodeError, ValueError, KeyError) as exc:
        raise CorruptStore("the directive store is damaged") from exc
    return raw


def _directives(raw: dict) -> List[Directive]:
    return [Directive(**item) for item in raw["directives"]]


def _save(path: str, raw: dict, items: Iterable[Directive], now: float) -> None:
    kept = [
        item
        for item in items
        if item.state in OPEN or now - max(item.created, item.replied) < KEEP
    ]
    ids = {item.id for item in kept}
    raw = {**raw, "directives": [asdict(item) for item in kept]}
    for key in ("notices", "bossTold"):  # never longer than the directives they name
        if key in raw:
            raw[key] = [i for i in raw[key] if isinstance(i, str) and i in ids]
    data = json.dumps(raw, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(data) > MAX_BYTES:
        raise Refused("the directive store is full")
    atomic_file.replace(path, data, 0o640)


def _update(path: str, change, now: float):
    """Run ``change(raw, items) -> (items, result)`` under the store's lock and save."""
    with atomic_file.locked(path):
        raw = _load(path)
        items, result = change(raw, _directives(raw))
        _save(path, raw, items, now)
    return result


def _find(items: List[Directive], directive_id: str) -> Tuple[int, Directive]:
    if not isinstance(directive_id, str) or not _ID.fullmatch(directive_id):
        raise Missing("no such directive")
    for index, item in enumerate(items):
        if item.id == directive_id:
            return index, item
    raise Missing("no such directive")


def send(path, *, agent, card, kind, message, revision, now) -> Tuple[Directive, bool]:
    """A new pending directive, or the same open one again (``False``)."""
    _check(agent, card, kind, revision)
    message = check_message(message)

    def change(raw, items):
        for item in items:
            if item.state in OPEN and (item.agent, item.card, item.kind, item.message) == (
                agent, card, kind, message,
            ) and now < item.expires:
                return items, (item, False)
        if sum(1 for item in items if item.state in OPEN) >= MAX_OPEN:
            raise Refused(f"too many open directives ({MAX_OPEN})")
        item = Directive(
            id=secrets.token_hex(8), agent=agent, card=card, kind=kind, message=message,
            revision=revision, created=float(now), expires=float(now) + TTL, state="pending",
            attempts=0, delivered=0.0, reply="", reply_message="", replied=0.0, seen=True,
        )
        return [*items, item], (item, True)

    return _update(path, change, now)


def _set(path, directive_id, now, edit):
    def change(raw, items):
        index, item = _find(items, directive_id)
        new = edit(item)
        items = list(items)
        items[index] = new
        return items, new

    return _update(path, change, now)


def mark_delivered(path, directive_id, now) -> Directive:
    return _set(
        path, directive_id, now,
        lambda item: replace(item, state="delivered", delivered=float(now))
        if item.state == "pending" else item,
    )


def mark_attempt(path, directive_id, now) -> Directive:
    return _set(path, directive_id, now, lambda item: replace(item, attempts=item.attempts + 1))


def _closed(item: Directive, now: float) -> bool:
    return item.state == "expired" or (item.state in OPEN and now >= item.expires)


def read_for(path, directive_id, agent, now) -> Directive:
    """The directive for its addressee; an open one becomes ``read``."""

    def edit(item):
        if item.agent != agent:
            raise NotYours("this directive is addressed to another agent")
        if _closed(item, now):
            raise Closed(CLOSED)
        return replace(item, state="read") if item.state in OPEN else item

    return _set(path, directive_id, now, edit)


def reply(path, directive_id, agent, state, message, now) -> Tuple[Directive, bool]:
    if state not in REPLIES:
        raise Refused(f"the answer must be one of {', '.join(REPLIES)}")
    message = check_message(message)

    def change(raw, items):
        index, item = _find(items, directive_id)
        if item.agent != agent:
            raise NotYours("this directive is addressed to another agent")
        if item.state == "replied":
            if (item.reply, item.reply_message) == (state, message):
                return items, (item, False)
            raise Closed("this directive is already answered")
        if _closed(item, now):
            raise Closed(CLOSED)
        new = replace(item, state="replied", reply=state, reply_message=message,
                      replied=float(now), seen=False)
        items = list(items)
        items[index] = new
        return items, (new, True)

    return _update(path, change, now)


def withdraw(path, directive_id, now, notify: bool = False) -> Directive:
    """An open directive that no longer applies (switch off, card moved, grant revoked) becomes ``expired``.
    ``notify``: the worker may already have it, so a withdrawal notice waits in the store until it is sent."""

    def change(raw, items):
        index, item = _find(items, directive_id)
        if item.state not in OPEN:
            return items, item
        new = replace(item, state="expired", seen=False)
        if notify:
            raw["notices"] = sorted(set(raw.get("notices", [])) | {item.id})
        items = list(items)
        items[index] = new
        return items, new

    return _update(path, change, now)


def notices(path) -> List[Directive]:
    """Withdrawn directives whose worker has not been told yet."""
    raw = _load(path)
    wanted = set(raw.get("notices", []))
    return [item for item in _directives(raw) if item.id in wanted]


def notice_sent(path, directive_id, now) -> None:
    def change(raw, items):
        raw["notices"] = [i for i in raw.get("notices", []) if i != directive_id]
        return items, None

    _update(path, change, now)


def expire(path, now) -> List[Directive]:
    """Open directives past their deadline become ``expired`` (unseen); returns them."""

    def change(raw, items):
        done, out = [], []
        for item in items:
            if item.state in OPEN and now >= item.expires:
                item = replace(item, state="expired", seen=False)
                done.append(item)
            out.append(item)
        return out, done

    return _update(path, change, now)


def _read(path: str) -> List[Directive]:
    return _directives(_load(path))


def listing(path, agent: Optional[str] = None) -> List[Directive]:
    items = sorted(_read(path), key=lambda item: item.created)
    return [item for item in items if agent is None or item.agent == agent]


def due(path, now) -> List[Directive]:
    return [item for item in listing(path) if item.state == "pending" and now < item.expires]


def unseen(path) -> List[Directive]:
    return [item for item in listing(path) if not item.seen]


def mark_seen(path, ids, now) -> None:
    wanted = set(ids)

    def change(raw, items):
        return [replace(item, seen=True) if item.id in wanted else item for item in items], None

    _update(path, change, now)


def boss_wake_due(path, now, interval: float = BOSS_WAKE_INTERVAL) -> bool:
    """An unseen answer the coordinator was not woken for yet is due at once; otherwise once per ``interval``."""
    raw = _load(path)
    unseen_ids = {item.id for item in _directives(raw) if not item.seen}
    return bool(unseen_ids - set(raw.get("bossTold", []))) or now - float(raw["bossWoken"]) >= interval


def mark_boss_woken(path, now) -> None:
    """The coordinator was woken: it was told of every answer unseen now."""
    with atomic_file.locked(path):
        raw = _load(path)
        raw["bossWoken"] = float(now)
        raw["bossTold"] = sorted(item.id for item in _directives(raw) if not item.seen)
        _save(path, raw, _directives(raw), now)
