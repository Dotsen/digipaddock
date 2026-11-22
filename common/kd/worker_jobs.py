"""Worker jobs (BOSS spec 5.7, 5.8.6, C04): admission by card and the job's definition.

A job is one request of the coordinator for one card: a specialization, its skills and a brief.
The card names the agent (its one assignee, who must own registered slots), the agent's
manifest names the type, and the type names the rules: a developer job needs an owner-approved
card (To Do or In Progress) of a registered repository and may write its own working copy; an
analyst job runs on any open card without effects (write ``none``). Specialization and skills
come from the registry of that type; their limits and text hashes travel with the job.

kd-worker submits the admitted job's whole definition to the cabinet (``job.submit``): the
cabinet keeps one active writer per card and the daily limits, hands the job to the session host and
records its end; the host runs the root runner (kd.worker_runner) in a free worker slot of the agent.
Nothing re-queues a job: a retry is a new request.
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from kd import worker_packs, worker_slots
from kd.admin_task import Card
from kd.specializations import Registry

REPOS = "/etc/digipaddock/repos.json"
APPROVED = ("To Do", "In Progress")
OPEN = ("Inbox", "To Do", "In Progress", "Review", "Blocked")
# Type -> card statuses a job may run on (spec 5.8.6); a type missing here is not in this delivery.
PRECONDITIONS: dict[str, tuple[str, ...]] = {"developer": APPROVED, "analyst": OPEN}
OWNER_CALLER = "root"
JOB_ID = re.compile(r"[0-9a-f]{32}")
PROJECT = re.compile(r"[a-z][a-z0-9-]{0,31}")
ASSIGNEE = re.compile(r"[a-z][a-z0-9_-]{0,31}")  # admin_task.from_record strips the "@"
NO_BLOCK = "the session ended without the specialization's final JSON block"

EXIT_ERROR, EXIT_REFUSED, EXIT_CONFLICT, EXIT_RATE = 1, 2, 3, 5  # 4 (all slots taken) is gone: a job queues
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


class JobError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def clean(text: object, limit: int = 300) -> str:
    return _CONTROL.sub(" ", str(text)).strip()[:limit]


_SHA = re.compile(r"[0-9a-f]{40}")
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")
PULL_KEYS = frozenset({"number", "head", "target"})


def check_pull(value: Any) -> dict[str, Any]:
    """The pull request a job reads — ``{number, head, target}``: its number at the forge, the full
    SHA of the head the job is about, the branch it goes into. ValueError otherwise. One check for the cabinet,
    the session host and the runner: each of them puts these three into a git command or hands them on."""
    if not isinstance(value, dict) or set(value) != PULL_KEYS:
        raise ValueError("pull is {number, head, target}")
    number, head, target = value["number"], value["head"], value["target"]
    if not isinstance(number, int) or isinstance(number, bool) or not 1 <= number <= 10**9:
        raise ValueError("pull.number is a pull request's number")
    if not isinstance(head, str) or not _SHA.fullmatch(head):
        raise ValueError("pull.head is a full SHA")
    if (not isinstance(target, str) or not _BRANCH.fullmatch(target) or ".." in target or "//" in target
            or target.endswith(("/", ".", ".lock"))):
        raise ValueError("pull.target is a branch name")
    return {"number": number, "head": head, "target": target}


def reads_pull(definition: Mapping[str, Any]) -> bool:
    """Whether a job of this definition may be on a pull request: a developer's that writes nothing — the head of
    somebody's pull request is read, never built on."""
    limits = definition.get("limits")
    return definition.get("type") == "developer" and isinstance(limits, Mapping) and limits.get("write") == "none"


def new_job_id() -> str:
    return secrets.token_hex(16)


@dataclass(frozen=True)
class Request:
    card: str
    spec: str
    skills: tuple[str, ...]
    brief: str


@dataclass(frozen=True)
class Admission:
    """What the card and the registry allow: the agent, its type, the repository (developer),
    whether the job writes, the limits of the specialization and the built prompt."""

    agent: str
    type: str
    repo: Optional[str]
    writer: bool
    limits: dict[str, Any]
    prompt: worker_packs.WorkerPrompt


def registered_repos(path: str = REPOS) -> frozenset[str]:
    """Project names of the repository registry (agents/dev/install/add_repo.sh writes it)."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise JobError(EXIT_ERROR, "repository registry is not readable") from exc
    if not isinstance(data, dict) or not all(
        isinstance(name, str) and PROJECT.fullmatch(name) for name in data
    ):
        raise JobError(EXIT_ERROR, "repository registry is invalid")
    return frozenset(data)


def _agent_of(card: Card, config: worker_slots.SlotConfig) -> str:
    if (
        len(card.assignees) != 1
        or not isinstance(card.assignees[0], str)
        or not ASSIGNEE.fullmatch(card.assignees[0])
    ):
        raise JobError(EXIT_REFUSED, f"{card.task_id} needs exactly one assignee @<agent>")
    agent = card.assignees[0]
    if agent not in config.agents:
        raise JobError(EXIT_REFUSED, f"no worker slots for @{agent}")
    return agent


def admit(
    request: Request,
    card: Card,
    *,
    repos: frozenset[str],
    registry: Registry,
    config: worker_slots.SlotConfig,
    types: Mapping[str, str],
) -> Admission:
    """Check one request against its card, the agent's type and the registry of that type."""
    if card.task_id != request.card:
        raise JobError(EXIT_ERROR, "card lookup mismatch")
    agent = _agent_of(card, config)
    agent_type = types.get(agent)
    if not isinstance(agent_type, str):
        raise JobError(EXIT_ERROR, f"the manifest of {agent} names no type")
    statuses = PRECONDITIONS.get(agent_type)
    if statuses is None:
        raise JobError(EXIT_REFUSED, f"{agent_type} workers are not in this delivery")
    if card.status not in statuses:
        raise JobError(
            EXIT_REFUSED, f"{card.task_id} is not approved for {agent_type} work (status {clean(card.status, 40)})"
        )
    repo: Optional[str] = None
    if agent_type == "developer":
        if card.project not in repos:
            raise JobError(
                EXIT_REFUSED, f"{card.task_id}: project {clean(card.project, 40)} is not a registered repository"
            )
        repo = card.project
    try:
        prompt = worker_packs.build_prompt(
            registry, request.spec, request.skills, card=request.card, brief=request.brief, agent_type=agent_type
        )
    except worker_packs.PackError as exc:
        raise JobError(EXIT_REFUSED, clean(exc)) from exc
    entry = registry.specs[prompt.spec].limits
    write = "none" if agent_type == "analyst" else entry.write  # an analyst worker has no effects
    limits = {"write": write, "capabilities": list(entry.capabilities), "network": entry.network}
    return Admission(agent, agent_type, repo, write == "repo", limits, prompt)


def definition(
    request: Request,
    admission: Admission,
    *,
    job_id: str,
    caller: str,
    requester: str,
    config: worker_slots.SlotConfig,
) -> dict[str, Any]:
    """The whole job as the cabinet keeps it and the host's runner reads it: ``caller`` is the requester's Unix
    user (``root`` for the owner), ``requester`` the agent woken about the end (empty for the owner)."""
    return {
        "id": job_id,
        "card": request.card,
        "agent": admission.agent,
        "type": admission.type,
        "repo": admission.repo,
        "spec": admission.prompt.spec,
        "skills": [name for name, _, _ in admission.prompt.skills],
        "prompt": admission.prompt.provenance(),
        "limits": dict(admission.limits),
        "brief": request.brief,
        "caller": caller,
        "requester": requester,
        "writer": admission.writer,
        "timeout": config.timeout_minutes * 60,
        "model": config.model,
    }
