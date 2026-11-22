"""Trusted worker-activity journal for the 24-hour attention rule (ADR 0003, B05).

``record`` is the producer: a trusted wrapper calls it *after* it has verified a meaningful
operation (a PR opened, a review submitted). It adds one schema-v1 record to a root-owned,
bounded JSON-lines file under an exclusive lock, replacing the file atomically (``kd.atomic_file``)
and fsyncing before returning.
``read`` is the consumer for ``kd-board watch``.

Attribution is by Unix user, never by a caller-provided name: the worker must be the only agent
of its own ``digi-<role>`` user (``kd.agents``). Agents sharing the legacy ``digi`` user cannot
be told apart, so they get no records and the rule stays unknown for them. The producer reads the
card itself through the cabinet: the worker must be the card's single assignee, and the
record's evidence is the card's revision (the cabinet's change counter) it saw. A repeat with the
same evidence is a keepalive and is refused, so the timer moves only when the card changed.
Nothing is backfilled.

the journal belongs to the base user, which runs the workers' ``kd-work`` executor
(their ``base`` capability channel); the root watch reads it only as a regular file of
that user. ``action="auto"`` lets the producer name the action from the evidence it holds: the
worker's first record on the card is its ``claim``, a card in Review is a ``review_submission``,
anything else ``progress``.
"""

from datetime import datetime
import json
import os
import stat
from typing import Any, List, Optional

from kd import activity, agents, atomic_file, clock
from kd.cabinet import client as cabinet_client

try:
    import fcntl
except ImportError:  # Windows: tests only
    fcntl = None

JOURNAL = "/var/log/digipaddock-board/activity.jsonl"
AUTO = "auto"
MAX_BYTES = 2 * 1024 * 1024
KEEP_BYTES = MAX_BYTES // 2


class Rejected(ValueError):
    """The action is not trusted evidence; nothing was written."""


def record(
    journal: str,
    client,
    card_id: str,
    worker: str,
    action: str,
    caller_user: str,
    now: datetime,
    registry: str = agents.REGISTRY,
) -> dict:
    """Append one verified action and return the stored record, or raise ``Rejected``."""
    name = worker[1:] if isinstance(worker, str) and worker.startswith("@") else ""
    try:
        user = agents.user_of(name, registry)
    except ValueError as exc:
        raise Rejected("unknown worker") from exc
    if user == agents.DEFAULT_USER:
        raise Rejected("worker shares the agent user; actions cannot be attributed")
    if caller_user != user:
        raise Rejected("caller is not the worker's user")
    try:
        card = client.call({"op": "card.get", "card": card_id})["card"]
    except cabinet_client.CabinetError as exc:
        if exc.kind == "not_found":
            raise Rejected(f"{card_id}: card not found") from exc
        raise Rejected("card is not readable") from exc
    if card.get("id") != card_id or card.get("assignee") != worker:
        raise Rejected("worker is not the card's single assignee")
    entry = {
        "schemaVersion": 1,
        "cardId": card_id,
        "worker": worker,
        "action": "progress" if action == AUTO else action,
        "at": clock.iso(now),
        "cardRevision": card.get("revision"),
    }
    try:
        activity.parse_record(entry)
    except ValueError as exc:
        raise Rejected(str(exc)) from exc
    if fcntl is None:
        raise Rejected("the activity journal needs flock (Linux)")
    with atomic_file.locked(journal):
        data = atomic_file.read(journal, MAX_BYTES)
        mine = [
            r
            for r in _parse(data)
            if isinstance(r, dict)
            and r.get("cardId") == card_id
            and r.get("worker") == worker
        ]
        if any(r.get("cardRevision") == entry["cardRevision"] for r in mine):
            raise Rejected("no change since the last record")
        if action == AUTO:
            entry["action"] = (
                "claim"
                if not mine
                else "review_submission"
                if card.get("status") == "Review"
                else "progress"
            )
        line = (json.dumps(entry, sort_keys=True) + "\n").encode()
        if len(data) + len(line) > MAX_BYTES:
            data = _newest(data)
        # The whole file is replaced, never appended in place: a crash leaves the old or the
        # new journal, never a torn last line that would make the whole journal untrusted.
        atomic_file.replace(journal, data + line, 0o640)
    return entry


def read(journal: str = JOURNAL, owner_uid: Optional[int] = None) -> Optional[List[Any]]:
    """Records for ``attention_rules.evaluate``: None without a journal (rule unknown).

    A line that is not JSON is passed on as text, so the detector marks the whole journal
    untrusted instead of silently skipping it; so is a journal that is not a regular file of
    ``owner_uid`` (the base user), when given.
    """
    try:
        fd = os.open(
            journal,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
    except FileNotFoundError:
        return None
    except OSError:
        return ["unreadable journal"]
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or (
            owner_uid is not None and info.st_uid != owner_uid
        ):
            return ["untrusted journal"]
        return _parse(handle.read(MAX_BYTES + 1))


def _parse(data: bytes) -> List[Any]:
    if len(data) > MAX_BYTES:
        return ["journal too large"]
    out: List[Any] = []
    for line in data.decode("utf-8", "replace").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            out.append(line)
    return out


def _newest(data: bytes) -> bytes:
    """The newest whole lines under KEEP_BYTES; the dropped ones are older evidence."""
    if len(data) <= KEEP_BYTES:
        return data
    tail = data[-KEEP_BYTES:]
    cut = tail.find(b"\n")
    return tail[cut + 1 :] if cut >= 0 else b""
