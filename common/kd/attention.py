"""Versioned attention automaton for coordinator events (BOSS spec 4.1, B04).

``kd-board watch`` feeds each detector report (``kd.attention_rules``) to ``observe``; the
root-owned state file remembers every event by its key ``(rule, subject, revision)`` — for the rules
by age (``AGE_RULES``) ``(rule, subject)``: a comment does not reset their age — with an
opaque random ID and one of the states::

    pending -> dispatched -> acknowledged -> resolved
    (any open state) -> escalated            when the detector raises the level

An event is ``dispatched`` only after the caller's nudge succeeded (``mark_dispatched``); a
failed nudge (``mark_failed``) leaves it pending for the next tick. A repeated tick changes
nothing and keeps the event's age. An acknowledgement names the action taken and when to look again
(``nextCheck``); ``recheck`` returns an acknowledged event whose check is due to pending, its age kept
(C03). A condition that disappears resolves its event, unless the
report says the condition is merely unknown (time jump, untrusted journal, invalid card), in
which case the event is kept as it is.

Every change runs under an exclusive ``flock`` and replaces the file atomically (temporary file,
fsync, rename, directory fsync). A damaged file is never read as empty: it raises
``CorruptState`` and ``recover`` moves it aside and rebuilds pending events from the board.
No wake, Telegram or LLM here.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
import re
import secrets
import tempfile
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from kd import attention_rules

try:
    import fcntl
except ImportError:  # Windows: tests only
    fcntl = None

# root:agent 2750 directory: kd-boss-board (as agent) reads the list, only root writes.
STATE = "/var/lib/digipaddock/attention/attention.json"
SCHEMA_VERSION = 2  # 2: an acknowledgement's action, reason and next check
STATES = ("pending", "dispatched", "acknowledged", "resolved", "escalated")
OPEN = ("pending", "dispatched", "acknowledged", "escalated")
LEVELS = ("attention", "escalate")
NOTIFY = ("boss", "assignee", "owner")
MAX_EVENTS = 5000
MAX_BYTES = 4 * 1024 * 1024
RETAIN = timedelta(days=7)

_ID = re.compile(r"[0-9a-f]{32}")
_TIME = "%Y-%m-%dT%H:%M:%SZ"
_FIELDS = frozenset(
    {
        "rule",
        "subject",
        "revision",
        "notify",
        "level",
        "state",
        "created",
        "updated",
        "attempts",
        "ownerNotified",
        "nextCheck",
        "action",
        "reason",
    }
)
_ACK_DEFAULTS = {"nextCheck": "", "action": "", "reason": ""}  # what a v1 event and a fresh one carry
_ACTIVITY_RULES = ("in_progress_idle", "review_waiting")
# Rules by age: one event per (rule, card) whatever its revision (C03).
AGE_RULES = ("blocked", "in_progress_idle", "review_waiting", "inbox_waiting", "overdue")
DEFAULT_CHECK = timedelta(minutes=30)  # an acknowledgement without its next check

Key = Tuple[str, str, str]


class AttentionError(RuntimeError):
    """The request cannot be applied; the state file is unchanged."""


class CorruptState(AttentionError):
    """The state file exists but is not a valid automaton state; never treated as empty."""


def new_id() -> str:
    return secrets.token_hex(16)


# --- operations (each one transaction) ------------------------------------------------------


def observe(
    path: str,
    report: "attention_rules.Report",
    now: datetime,
    make_id: Callable[[], str] = new_id,
) -> List[str]:
    """Apply one detector report; return IDs that became pending or escalated in this tick."""
    with _transaction(path) as state:
        return _observe(state, report, _stamp(now), make_id)


def mark_dispatched(path: str, ids: Iterable[str], now: datetime) -> List[str]:
    """After a successful nudge: pending -> dispatched. Events no longer pending are skipped."""
    with _transaction(path) as state:
        return _move(state, ids, ("pending",), "dispatched", _stamp(now))


def mark_failed(path: str, ids: Iterable[str], now: datetime) -> List[str]:
    """After a failed nudge: the events stay pending, with one more attempt counted."""
    stamp = _stamp(now)
    with _transaction(path) as state:
        done = []
        for event_id in _known(state, ids):
            event = state["events"][event_id]
            if event["state"] == "pending":
                event["attempts"] += 1
                event["updated"] = stamp
                done.append(event_id)
        return done


def _owner_due(event) -> bool:
    """An escalation whose owner line is still owed: open at the escalate level, BOSS's ack or not."""
    return event["state"] in OPEN and event["level"] == "escalate" and not event["ownerNotified"]


def mark_owner_notified(path: str, ids: Iterable[str], now: datetime) -> List[str]:
    """The owner line for escalated events was sent; a later tick does not repeat it."""
    stamp = _stamp(now)
    with _transaction(path) as state:
        done = []
        for event_id in _known(state, ids):
            event = state["events"][event_id]
            if _owner_due(event):
                event["ownerNotified"] = True
                event["updated"] = stamp
                done.append(event_id)
        return done


def ack(path: str, event_id: str, now: datetime) -> str:
    """Acknowledge one open event by its exact ID (root, tests); it comes back after ``DEFAULT_CHECK``."""
    if not isinstance(event_id, str) or not _ID.fullmatch(event_id):
        raise AttentionError("unknown event")
    with _transaction(path) as state:
        event = state["events"].get(event_id)
        if event is None:
            raise AttentionError("unknown event")
        if event["state"] == "resolved":
            raise AttentionError("event is already resolved")
        if event["state"] != "acknowledged":
            event.update(state="acknowledged", updated=_stamp(now), nextCheck=_stamp(now + DEFAULT_CHECK))
        return event["state"]


def apply_acks(path: str, acks: Iterable[Dict[str, Any]], now: datetime) -> List[str]:
    """Acknowledgements relayed from the cabinet (``{event, action, next_check, reason}``, the next check in
    minutes from now): unknown, resolved or already acknowledged events are skipped, so replaying the
    journal changes nothing. An escalated event is acknowledged too; its owner line is still sent
    once. An ack without its next check comes back after ``DEFAULT_CHECK``."""
    stamp = _stamp(now)
    with _transaction(path) as state:
        done = []
        for item in acks:
            event_id = item.get("event") if isinstance(item, dict) else None
            event = state["events"].get(event_id) if isinstance(event_id, str) else None
            if event is None or event["state"] not in ("pending", "dispatched", "escalated"):
                continue
            minutes = item.get("next_check")
            due = timedelta(minutes=minutes) if type(minutes) is int and minutes > 0 else DEFAULT_CHECK
            event.update(state="acknowledged", updated=stamp, nextCheck=_stamp(now + due),
                         action=_text(item.get("action"), 64), reason=_text(item.get("reason"), 300))
            done.append(event_id)
        return done


def recheck(path: str, now: datetime) -> List[str]:
    """Acknowledged events whose check is due go back to pending (the condition still holds, or ``observe``
    would have resolved them); their age is kept. Returns their IDs."""
    stamp = _stamp(now)
    with _transaction(path) as state:
        done = []
        for event_id, event in sorted(state["events"].items()):
            if event["state"] == "acknowledged" and (not event["nextCheck"] or event["nextCheck"] <= stamp):
                event.update(state="pending", updated=stamp, **_ACK_DEFAULTS)
                done.append(event_id)
        return done


def _text(value: Any, limit: int) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def owner_due(path: str) -> List[Dict[str, Any]]:
    """Escalated events whose owner line has not been sent yet, oldest first."""
    state = _read(path)
    events = [
        {"id": event_id, "rule": e["rule"], "subject": e["subject"]}
        for event_id, e in sorted(
            state["events"].items(), key=lambda item: (item[1]["created"], item[0])
        )
        if _owner_due(e)
    ]
    return events


def listing(path: str) -> List[Dict[str, Any]]:
    """Open events for the coordinator, oldest first: only validated fields, no card text.

    Reads without the lock (the file is replaced atomically), so the board owner can list
    events from a directory it may read but not write.
    """
    state = _read(path)
    events = [
        {"id": event_id, **{k: v for k, v in event.items() if k != "ownerNotified"}}
        for event_id, event in state["events"].items()
        if event["state"] in OPEN
    ]
    return sorted(events, key=lambda e: (e["created"], e["id"]))


def recover(
    path: str,
    report: "attention_rules.Report",
    now: datetime,
    make_id: Callable[[], str] = new_id,
) -> str:
    """Move a damaged state file aside and rebuild pending events from the current report.

    Returns the path the damaged file was moved to (for the caller's sanitized report line).
    """
    stamp = _stamp(now)
    with _locked(path):
        aside = f"{path}.corrupt-{stamp.replace(':', '')}"
        if os.path.lexists(path):
            os.replace(path, aside)
            _fsync_dir(path)
        state = _empty()
        _observe(state, report, stamp, make_id)
        _write(path, state)
    return aside


# --- the automaton --------------------------------------------------------------------------


def _observe(state, report, stamp: str, make_id) -> List[str]:
    current: Dict[Key, Any] = {}
    for event in report.events:
        current[_match((event.rule, event.subject, event.revision))] = event
    open_ids = {
        _match(_key(event)): event_id
        for event_id, event in state["events"].items()
        if event["state"] in OPEN
    }
    unknown = _unknown(report)
    changed = []
    for key, event in current.items():
        if event.notify not in NOTIFY or event.level not in LEVELS:
            raise AttentionError("invalid detector event")
        event_id = open_ids.get(key)
        if event_id is None:
            event_id = make_id()
            if not _ID.fullmatch(event_id) or event_id in state["events"]:
                raise AttentionError("event id collision")
            escalate = event.level == "escalate"
            state["events"][event_id] = {
                "rule": event.rule,
                "subject": event.subject,
                "revision": event.revision,
                "notify": event.notify,
                "level": event.level,
                "state": "escalated" if escalate else "pending",
                "created": stamp,
                "updated": stamp,
                "attempts": 0,
                "ownerNotified": False,
                **_ACK_DEFAULTS,
            }
            changed.append(event_id)
            continue
        stored = state["events"][event_id]
        stored["revision"] = event.revision  # a rule by age keeps its event; it shows the current revision
        if event.level == "escalate" and stored["level"] != "escalate":
            stored.update(
                level="escalate", notify=event.notify, state="escalated", updated=stamp
            )
            changed.append(event_id)
    for key, event_id in open_ids.items():
        if key not in current and not unknown(key):
            state["events"][event_id].update(state="resolved", updated=stamp)
    _prune(state, stamp)
    return changed


def _unknown(report) -> Callable[[Key], bool]:
    """Keys whose disappearance proves nothing: the detector could not evaluate them."""
    kinds = {(d.kind, d.subject) for d in report.diagnostics}
    time_jump = ("time_jump", "clock") in kinds
    journal = ("activity_invalid", "journal") in kinds
    # A diagnostic hides only the rule it names; without a rule, every rule of its card.
    rules = {(d.rule, d.subject) for d in report.diagnostics if d.rule}
    subjects = {
        d.subject
        for d in report.diagnostics
        if not d.rule and d.kind not in ("time_jump", "activity_invalid")
    }

    def unknown(key: Key) -> bool:
        rule, subject, _ = key
        if time_jump and rule in attention_rules.TIMED_RULES:
            return True
        if journal and rule in _ACTIVITY_RULES:
            return True
        return (rule, subject) in rules or subject in subjects

    return unknown


def _move(state, ids, sources, target, stamp) -> List[str]:
    done = []
    for event_id in _known(state, ids):
        event = state["events"][event_id]
        if event["state"] in sources:
            event["state"] = target
            event["updated"] = stamp
            done.append(event_id)
    return done


def _known(state, ids) -> List[str]:
    ids = list(ids)
    for event_id in ids:
        if not isinstance(event_id, str) or event_id not in state["events"]:
            raise AttentionError("unknown event")
    return ids


def _prune(state, stamp: str) -> None:
    """Resolved events go after RETAIN; above MAX_EVENTS the oldest resolved go first, then the oldest
    pending ones — the automaton keeps ticking (global review 15.10.2026: one agent flipping a card's
    status could fill the journal and stop every escalation for a week); only escalated events are kept
    whatever the count, and the journal fails only when those alone exceed the cap."""
    cutoff = _parse(stamp) - RETAIN
    events = state["events"]
    for event_id in [
        event_id
        for event_id, event in events.items()
        if event["state"] == "resolved" and _parse(event["updated"]) < cutoff
    ]:
        del events[event_id]
    for states in (("resolved",), ("pending", "dispatched", "acknowledged")):
        if len(events) <= MAX_EVENTS:
            return
        oldest = sorted(
            (event_id for event_id, event in events.items() if event["state"] in states),
            key=lambda event_id: (events[event_id]["updated"], event_id),
        )
        for event_id in oldest[: len(events) - MAX_EVENTS]:
            del events[event_id]
    if len(events) > MAX_EVENTS:
        raise AttentionError("too many attention events")


def _key(event) -> Key:
    return (event["rule"], event["subject"], event["revision"])


def _match(key: Key) -> Key:
    """The key an event is found by: a rule by age ignores the revision."""
    rule, subject, revision = key
    return (rule, subject, "*" if rule in AGE_RULES else revision)


# --- storage --------------------------------------------------------------------------------


def _empty() -> Dict[str, Any]:
    return {"schemaVersion": SCHEMA_VERSION, "events": {}}


@contextmanager
def _locked(path: str):
    if fcntl is None:
        raise AttentionError("attention state needs flock (Linux)")
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    fd = os.open(path + ".lock", flags, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


@contextmanager
def _transaction(path: str, write: bool = True):
    """Read, let the caller change, write back atomically; an exception writes nothing."""
    with _locked(path):
        state = _read(path)
        before = json.dumps(state, sort_keys=True)
        yield state
        if write and json.dumps(state, sort_keys=True) != before:
            _write(path, state)


def _read(path: str) -> Dict[str, Any]:
    try:
        fd = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
    except FileNotFoundError:
        return _empty()
    except OSError as exc:
        raise CorruptState("attention state cannot be opened") from exc
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise CorruptState("attention state is too large")
    try:
        state = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CorruptState("attention state is not valid JSON") from exc
    _upgrade(state)
    _validate(state)
    return state


def _upgrade(state: Any) -> None:
    """A v1 state reads as v2: its events get empty acknowledgement fields (a v1 ack is due at once), and the open
    events of one age rule and card that v1 kept per revision merge into the oldest; the rest resolve."""
    if not (isinstance(state, dict) and type(state.get("schemaVersion")) is int and state["schemaVersion"] == 1
            and isinstance(state.get("events"), dict)):
        return
    kept: Dict[Key, str] = {}  # the oldest open event's revision per merged key
    for event_id, event in sorted(state["events"].items(), key=lambda item: (str(item[1].get("created")), item[0])
                                  if isinstance(item[1], dict) else ("", item[0])):
        if not isinstance(event, dict):
            continue
        for field, value in _ACK_DEFAULTS.items():
            event.setdefault(field, value)
        if event.get("state") in OPEN and all(isinstance(event.get(k), str) for k in ("rule", "subject", "revision")):
            key = _match(_key(event))
            if key not in kept:
                kept[key] = event["revision"]
            elif kept[key] != event["revision"]:  # the same key twice stays a damaged file
                event["state"] = "resolved"
    state["schemaVersion"] = SCHEMA_VERSION


def _validate(state: Any) -> None:
    if (
        not isinstance(state, dict)
        or set(state) != {"schemaVersion", "events"}
        or type(state["schemaVersion"]) is not int
        or state["schemaVersion"] != SCHEMA_VERSION
        or not isinstance(state["events"], dict)
    ):
        raise CorruptState("attention state has an unknown shape")
    keys = set()
    for event_id, event in state["events"].items():
        if (
            not _ID.fullmatch(event_id)
            or not isinstance(event, dict)
            or set(event) != _FIELDS
            or event["rule"] not in attention_rules.RULES
            or not all(isinstance(event[k], str) for k in ("subject", "revision"))
            or not 0 < len(event["subject"]) <= 64
            or not 0 < len(event["revision"]) <= 64
            or event["notify"] not in NOTIFY
            or event["level"] not in LEVELS
            or event["state"] not in STATES
            or type(event["attempts"]) is not int
            or event["attempts"] < 0
            or type(event["ownerNotified"]) is not bool
            or not all(isinstance(event[k], str) for k in _ACK_DEFAULTS)
            or (event["nextCheck"] and _parse(event["nextCheck"]) is None)
            or _parse(event["created"]) is None
            or _parse(event["updated"]) is None
        ):
            raise CorruptState("attention state has an invalid event")
        if event["state"] in OPEN:
            if _match(_key(event)) in keys:
                raise CorruptState("attention state has a duplicate open event")
            keys.add(_match(_key(event)))


def _write(path: str, state: Dict[str, Any]) -> None:
    _validate(state)
    data = (
        json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    ).encode()
    directory = os.path.dirname(path) or "."
    fd, temp = tempfile.mkstemp(prefix=".attention.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o640)  # group: the setgid directory's (agent), read only
        os.replace(temp, path)
        _fsync_dir(path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _fsync_dir(path: str) -> None:
    if os.name == "nt":
        return
    fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _stamp(now: datetime) -> str:
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    return now.astimezone(timezone.utc).strftime(_TIME)


def _parse(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, _TIME).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
