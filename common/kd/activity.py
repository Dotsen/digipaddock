"""Validate the trusted worker-activity input consumed by attention rules."""

from datetime import datetime, timezone
import re
from typing import Any

from kd import ids


_CARD = ids.CARD
_WORKER = re.compile(r"@[a-z][a-z0-9_-]{0,31}\Z")
_ACTIONS = frozenset({"claim", "progress", "review_submission", "artifact_link"})
_FIELDS = frozenset(
    {"schemaVersion", "cardId", "worker", "action", "at", "cardRevision"}
)


def parse_record(value: object) -> dict[str, Any]:
    """Return a normalized record or reject unknown/untrusted journal syntax."""
    if (
        not isinstance(value, dict)
        or value.keys() != _FIELDS
        or type(value.get("schemaVersion")) is not int
    ):
        raise ValueError("invalid activity record fields")
    if value["schemaVersion"] != 1:
        raise ValueError("unsupported activity schema")
    for field, pattern in (
        ("cardId", _CARD),
        ("worker", _WORKER),
    ):
        if not isinstance(value[field], str) or not pattern.fullmatch(value[field]):
            raise ValueError(f"invalid {field}")
    revision = value["cardRevision"]  # the cabinet's change counter of the card
    if type(revision) is not int or revision < 1:
        raise ValueError("invalid cardRevision")
    if value["worker"] in {"@boss", "@owner"}:
        raise ValueError("coordinator is not a worker")
    if not isinstance(value["action"], str) or value["action"] not in _ACTIONS:
        raise ValueError("invalid action")
    at = value["at"]
    if not isinstance(at, str) or not re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", at
    ):
        raise ValueError("invalid at")
    try:
        timestamp = datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise ValueError("invalid at") from exc
    return {**value, "at": timestamp}

