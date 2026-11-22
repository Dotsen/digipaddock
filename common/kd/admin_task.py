"""An admin card of the board, read the same way by the approval service and the executor.
The board is the cabinet; ``from_record`` makes the card of a ``card.get`` record
(``kd.cabinet.client.admin_card``).

A card is data, never permission: the agent that works on it can edit it. This module only answers
"which card is this, is it an admin card of the sysadmin, is it active, what scope did the owner see".
The scope hash covers the fields that describe the work (id, title, project, assignee, servers,
description, acceptance criteria, Definition of Done); notes, comments, summary, status,
dependencies, dates and checked boxes are progress, so they never require a new decision.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Dict, Mapping, Tuple

from kd import ids

TASK_ID = ids.CARD
ALIAS = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
SERVER_LABEL = "server:"
ACTIVE = ("To Do", "In Progress")
# the sections of a cabinet card, from its columns (empty ones are left out)
RECORD_SECTIONS = (("Description", "description"), ("Acceptance Criteria", None), ("Definition of Done", "dod"),
                   ("Implementation Plan", "plan"), ("Implementation Notes", "notes"), ("Final Summary", "summary"))
PROJECT = "admin"
ASSIGNEE = "sysadmin"

_CHECKBOX = re.compile(r"^- \[[ xX]\] ", re.M)


class CardError(ValueError):
    """The card cannot be used for an approval or a run; the text says why."""


@dataclass(frozen=True)
class Card:
    task_id: str
    title: str
    status: str
    project: str
    assignees: Tuple[str, ...]
    servers: Tuple[str, ...]
    labels: Tuple[str, ...]
    sections: Dict[str, str] = field(compare=False)
    revision: int = 0  # the cabinet's compare-and-set counter
    path: str = ""

    def scope(self) -> dict:
        return {
            "schema": 1,
            "id": self.task_id,
            "title": self.title,
            "project": self.project,
            "assignees": sorted(self.assignees),
            "servers": sorted(self.servers),
            "description": self.sections.get("Description", ""),
            "acceptance_criteria": _unchecked(self.sections.get("Acceptance Criteria", "")),
            "definition_of_done": _unchecked(self.sections.get("Definition of Done", "")),
        }

    @property
    def scope_sha256(self) -> str:
        return hashlib.sha256(canonical(self.scope())).hexdigest()


def canonical(value) -> bytes:
    """The one JSON encoding hashed by the service and checked by the executor."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def check_task_id(task_id: object) -> str:
    if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
        raise CardError(f"invalid task id: {task_id!r}")
    return task_id


def from_record(record: Mapping) -> Card:
    """The card of a cabinet record (``CardRecord.to_dict``): the same scope as the Backlog.md card had.

    The acceptance criteria become ``- [x] text`` lines (unchecked in the scope); ``server:`` labels and
    the ``servers`` rows both name servers, each by a valid alias. CardError for a record that is not
    one card as the cabinet writes it.
    """
    task_id = record.get("id")
    if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
        raise CardError(f"the card has no valid id: {task_id!r}")
    for key in ("title", "status", "project"):
        if not isinstance(record.get(key), str):
            raise CardError(f"{task_id}: {key} must be text")
    if not isinstance(record.get("assignee", ""), str):
        raise CardError(f"{task_id}: assignee must be text")
    revision = record.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise CardError(f"{task_id}: invalid revision {revision!r}")
    labels, listed = record.get("labels", []), record.get("servers", [])
    if not isinstance(labels, list) or not isinstance(listed, list) or not all(
            isinstance(v, str) for v in labels + listed):
        raise CardError(f"{task_id}: labels and servers must be lists of text")
    servers = set(listed) | {v[len(SERVER_LABEL):] for v in labels if v.startswith(SERVER_LABEL)}
    for alias in servers:
        if not ALIAS.fullmatch(alias):
            raise CardError(f"{task_id}: invalid server {alias!r}")
    acceptance = record.get("acceptance", [])
    if not isinstance(acceptance, list) or not all(
            isinstance(a, Mapping) and isinstance(a.get("text"), str) for a in acceptance):
        raise CardError(f"{task_id}: unreadable acceptance criteria")
    criteria = "\n".join(f"- [{'x' if a.get('checked') else ' '}] {a['text']}" for a in acceptance)
    sections = {}
    for name, column in RECORD_SECTIONS:
        value = criteria if column is None else record.get(column, "")
        if not isinstance(value, str):
            raise CardError(f"{task_id}: {column} must be text")
        if value:
            sections[name] = value
    assignee = record.get("assignee", "")
    return Card(
        task_id=task_id,
        title=record["title"],
        status=record["status"],
        project=record["project"],
        assignees=(assignee[1:] if assignee.startswith("@") else assignee,) if assignee else (),
        servers=tuple(sorted(servers)),
        labels=tuple(labels),
        sections=sections,
        revision=revision,
    )


def check_admin(card: Card, project: str = PROJECT, assignee: str = ASSIGNEE) -> None:
    if card.project != project:
        raise CardError(f"{card.task_id}: project is {card.project!r}, not {project!r}")
    if card.assignees != (assignee,):
        raise CardError(f"{card.task_id}: the assignee must be exactly @{assignee}, not {list(card.assignees)}")


def check_active(card: Card) -> None:
    if card.status not in ACTIVE:
        raise CardError(f"{card.task_id}: status {card.status!r} does not allow work (only {', '.join(ACTIVE)})")


def _unchecked(text: str) -> str:
    return _CHECKBOX.sub("- [ ] ", text)
