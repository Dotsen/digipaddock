"""The owner's approval of a BOSS ``promote``: Inbox -> To Do happens only here.

A Claude Code card gives the executor no checkable fact that the owner pressed it, so BOSS no longer
runs ``promote``. ``kd-boss-board propose-promote`` (as the board owner, through BOSS's base channel)
checks the card and its route with the same ``boss_board.plan`` and stores a proposal: the exact
revision, a snapshot of the routing fields and the reason, valid for an hour; a live, undecided proposal
of the same card revision and reason is returned instead of a new one (a decided one never is: the board
owner may traverse the decision directory to see that), and proposals expired more than a week ago are
removed. Nothing on the board changes. The cabinet (a ``promote``/``delegate`` decision, as the cabinet user) shows
the proposal and, on the owner's approval, under the proposal's lock: refuses a decided one, checks the shown hash, the time, the BOSS
channel switch (``boss/base.json``), the card's exact revision and fields, then runs the one write
through ``kd.boss_exec`` (routing check, rate limits, audit intent before and result after; the
cabinet's ``card.promote``, which only the cabinet and root may call) and
records the decision. A rejection is recorded and changes nothing. A decision is final: a second POST
sees it. If the record fails after the edit, the result is uncertain; the card's new revision keeps a
retry from editing twice.

A proposal of kind ``delegate`` (``kd-boss-board propose-delegate``) asks the owner to let BOSS split
the goal into subtasks within the limits in ``scope`` (from ``common/coordination/delegation.json`` and the routes:
projects routed to a registered agent). Approval writes the grant (``kd.delegation``, id = the proposal's) and a
comment on the goal; the goal itself is not moved.
"""

import hashlib
import hmac
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from typing import Optional, Tuple

from kd import agents, atomic_file, boss_board, boss_exec, cap_units, clock, delegation
from kd.cabinet import client as cabinet_client

PROPOSALS = "/var/lib/digipaddock/boss-promotions/proposals"
DECISIONS = "/var/lib/digipaddock/boss-promotions/decisions"
CHANNEL_SWITCH = cap_units.grants_path("boss", "base")
# as long as its decision stays open in the cabinet (kd.cabinet.decisions.TTL_SECONDS: promote, delegate), so an
# approval within the decision's term is never refused as late
TTL = 72 * 3600
KEEP = 7 * 86400  # an expired proposal stays readable this long
MAX_FILE = 16 * 1024
_ID = re.compile(r"[0-9a-f]{32}")


class Missing(Exception):
    """No such proposal (or it cannot be read)."""


# Why an approval is refused: the page names the cause, never a bare «the card changed»
BOSS_OFF, EXPIRED, CARD_CHANGED, PROPOSAL_CHANGED = "boss_off", "proposal_expired", "card_changed", "proposal_changed"


class _Refusal(Exception):
    def __init__(self, message: str, reason: str = ""):
        super().__init__(message)
        self.reason = reason


class Conflict(_Refusal):
    """The proposal, the card, its revision or the time changed: nothing was changed; a new proposal is needed."""


class Unavailable(_Refusal):
    """Nothing was changed (the card is busy, BOSS is off — a release reinstalls it —, the board or the journal is
    unavailable); the owner may retry."""


class Uncertain(Exception):
    """The edit ran but its record was lost: check boss.jsonl; a retry is refused by the card's new revision."""


@dataclass(frozen=True)
class Proposal:
    id: str
    card: str
    revision: int
    title: str
    project: str
    assignee: str
    reason: str
    created: float
    expires: float
    kind: str = "promote"  # promote | delegate
    scope: str = ""  # delegate: the limits shown to the owner, canonical JSON

    @property
    def sha256(self) -> str:
        data = json.dumps(
            asdict(self), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(data.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Settings:
    """What the approval page needs to run a promote (the stage's paths by default)."""

    proposals: str = PROPOSALS
    decisions: str = DECISIONS
    client: object = field(default_factory=cabinet_client.Client)
    registry_path: str = agents.REGISTRY
    audit_path: str = boss_exec.AUDIT
    channel_switch: str = CHANNEL_SWITCH
    grants: str = delegation.GRANTS
    children: str = delegation.CHILDREN


def _request(card_id: str, revision: int, reason: str) -> dict:
    return {
        "op": "promote",
        "actor": boss_board.ACTOR,
        "task": card_id,
        "revision": revision,
        "reason": reason,
    }


def propose(
    card: boss_board.Card,
    routing: boss_board.Routing,
    revision: int,
    reason: str,
    *,
    now: float,
    root: str = PROPOSALS,
    decisions: str = DECISIONS,
) -> Proposal:
    """Check the promote as the edit would (``boss_board.Refused`` otherwise) and store it; the board is untouched."""
    boss_board.plan(_request(card.id, revision, reason), routing, card)
    proposal = Proposal(
        id=uuid.uuid4().hex,
        card=card.id,
        revision=revision,
        title=card.title,
        project=card.project,
        assignee=card.assignees[0],
        reason=reason.strip(),
        created=float(now),
        expires=float(now) + TTL,
    )
    live = _sweep(root, proposal, decisions)
    if live is not None:
        return live
    data = json.dumps(asdict(proposal), ensure_ascii=False, sort_keys=True).encode(
        "utf-8"
    )
    atomic_file.replace(os.path.join(root, f"{proposal.id}.json"), data, 0o640)
    return proposal


def propose_delegate(
    card: boss_board.Card,
    routing: boss_board.Routing,
    revision: int,
    reason: str,
    limits: delegation.Limits,
    *,
    now: float,
    root: str = PROPOSALS,
    decisions: str = DECISIONS,
) -> Proposal:
    """Store the owner's question «may BOSS split this goal?»; the board is untouched (``boss_board.Refused`` otherwise)."""
    boss_board.plan(
        {"op": "comment", "actor": boss_board.ACTOR, "task": card.id, "revision": revision, "reason": reason},
        routing,
        card,
    )  # the revision and the reason, checked as for any BOSS edit
    if card.status == boss_board.DONE:
        raise boss_board.Refused("the goal is Done")
    projects = {
        project: agent
        for project, agent in sorted(routing.projects.items())
        if agent in routing.assignees and agent != boss_board.OWNER
    }
    if not projects:
        raise boss_board.Refused("no project is routed to an agent")
    scope = json.dumps(
        {"maxChildren": limits.max_children, "maxActive": limits.max_active, "days": limits.days,
         "projects": projects},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    proposal = Proposal(
        id=uuid.uuid4().hex,
        card=card.id,
        revision=revision,
        title=card.title,
        project=card.project,
        assignee=",".join(card.assignees),
        reason=reason.strip(),
        created=float(now),
        expires=float(now) + TTL,
        kind="delegate",
        scope=scope,
    )
    live = _sweep(root, proposal, decisions)
    if live is not None:
        return live
    data = json.dumps(asdict(proposal), ensure_ascii=False, sort_keys=True).encode("utf-8")
    atomic_file.replace(os.path.join(root, f"{proposal.id}.json"), data, 0o640)
    return proposal


def scope_of(proposal: Proposal) -> dict:
    """The limits of a ``delegate`` proposal (``Missing`` if they cannot be read)."""
    try:
        raw = json.loads(proposal.scope)
        limits = delegation.Limits(raw["maxChildren"], raw["maxActive"], raw["days"])
        projects = raw["projects"]
        if not isinstance(projects, dict) or not projects or not all(
            isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in limits.__dict__.values()
        ):
            raise ValueError("scope")
    except (ValueError, KeyError, TypeError) as exc:
        raise Missing("пределы предложения не читаются") from exc
    return {"limits": limits, "projects": {str(k): str(v) for k, v in projects.items()}}


def _sweep(root: str, new: Proposal, decisions: str) -> Optional[Proposal]:
    """Remove proposals expired over ``KEEP`` ago; return a live, undecided one of the same card revision and reason."""
    live = None
    for entry in sorted(os.listdir(root)):
        name, ext = os.path.splitext(entry)
        if ext != ".json" or not _ID.fullmatch(name):
            continue
        try:
            proposal = load(name, root)
        except Missing:
            continue
        if proposal.expires < new.created - KEEP:
            try:
                os.unlink(os.path.join(root, entry))
            except FileNotFoundError:
                pass
        elif (
            (proposal.kind, proposal.card, proposal.revision, proposal.reason)
            == (new.kind, new.card, new.revision, new.reason)
            and new.created < proposal.expires
            and _undecided(proposal.id, decisions)
        ):
            live = proposal
    return live


def _undecided(proposal_id: str, decisions: str) -> bool:
    """No decision recorded: the proposer may only traverse the decision directory, so it asks for the one name;
    anything but "no such file" (no access, an error) counts as decided and gets a new proposal."""
    try:
        os.lstat(os.path.join(decisions, f"{proposal_id}.json"))
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def load(proposal_id: str, root: str = PROPOSALS) -> Proposal:
    if not isinstance(proposal_id, str) or not _ID.fullmatch(proposal_id):
        raise Missing("нет такого предложения")
    try:
        data = atomic_file.read(os.path.join(root, f"{proposal_id}.json"), MAX_FILE)
    except OSError as exc:
        raise Missing("предложение не читается") from exc
    if not data or len(data) > MAX_FILE:
        raise Missing("нет такого предложения")
    try:
        raw = json.loads(data.decode("utf-8"))
        names = {f.name for f in fields(Proposal)}
        if not isinstance(raw, dict) or set(raw) != names or raw["kind"] not in ("promote", "delegate"):
            raise ValueError("keys")
        for name in names - {"created", "expires", "revision"}:
            if not isinstance(raw[name], str):
                raise ValueError(name)
        # a cabinet revision is an integer; a proposal of a Backlog hash is not readable any more
        if isinstance(raw["revision"], bool) or not isinstance(raw["revision"], int) or raw["revision"] < 1:
            raise ValueError("revision")
        for name in ("created", "expires"):
            if isinstance(raw[name], bool) or not isinstance(raw[name], (int, float)):
                raise ValueError(name)
            raw[name] = float(raw[name])
    except (UnicodeDecodeError, ValueError) as exc:
        raise Missing("предложение не читается") from exc
    proposal = Proposal(**raw)
    if proposal.id != proposal_id:
        raise Missing("предложение не читается")
    return proposal


def decision(proposal_id: str, decisions: str = DECISIONS) -> Optional[dict]:
    """The recorded decision, or None while the proposal is open."""
    data = atomic_file.read(os.path.join(decisions, f"{proposal_id}.json"), MAX_FILE)
    if not data:
        return None
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {
            "decision": "unreadable"
        }  # a decision exists: the proposal stays closed
    return value if isinstance(value, dict) else {"decision": "unreadable"}


def problem(
    proposal: Proposal, card: Optional[boss_board.Card], now: float, channel_switch: str
) -> Optional[Tuple[str, str]]:
    """Why the proposal cannot be approved now — ``(reason, text)`` — or None. The page asks the same before it
    offers the approval. ``BOSS_OFF`` passes: its switch is back once a release has reinstalled it."""
    if now >= proposal.expires:
        return EXPIRED, "срок предложения истёк"
    if not os.path.isfile(channel_switch):
        return BOSS_OFF, "канал BOSS выключен"
    if card is None:
        return CARD_CHANGED, "карточка не читается"
    if proposal.kind == "delegate":
        if card.revision != proposal.revision:
            return CARD_CHANGED, "карточка изменилась после предложения"
        return (CARD_CHANGED, "цель уже Done") if card.status == boss_board.DONE else None
    if (card.revision, card.title, card.project, card.assignees) != (
        proposal.revision,
        proposal.title,
        proposal.project,
        (proposal.assignee,),
    ):
        return CARD_CHANGED, "карточка изменилась после предложения"
    return None


ROUTE_MISMATCH = "route_mismatch"


def route_mismatch(snapshot: dict, client, registry_path: str) -> Optional[str]:
    """Why a proposal's snapshot of the routes (project -> agent) no longer holds, or None (the owner may
    reroute or delete a project after BOSS proposed; a stale snapshot is refused, BOSS proposes again)."""
    try:
        current = boss_exec.load_routing(client, registry_path).projects
    except boss_exec.Failure as exc:
        raise Unavailable(str(exc)) from exc
    for project, agent in sorted(snapshot.items()):
        if current.get(project) != agent:
            now = current.get(project, "проекта больше нет")
            return f"{ROUTE_MISMATCH}: маршрут проекта {project} изменился ({agent} → {now}); предложите заново"
    return None


def decide(
    proposal_id: str,
    shown_sha256: str,
    approve: bool,
    *,
    client,
    registry_path: str,
    audit_path: str,
    caller: str,
    now: float,
    proposals: str = PROPOSALS,
    decisions: str = DECISIONS,
    channel_switch: str = CHANNEL_SWITCH,
    grants: str = delegation.GRANTS,
) -> str:
    """``approved``, ``rejected`` or ``already-decided``; ``Conflict``, ``Unavailable`` or ``Uncertain`` otherwise."""
    proposal = load(proposal_id, proposals)
    record = os.path.join(decisions, f"{proposal.id}.json")
    with atomic_file.locked(os.path.join(decisions, proposal.id), 0o600):
        if decision(proposal.id, decisions) is not None:
            return "already-decided"
        if not isinstance(shown_sha256, str) or not hmac.compare_digest(
            proposal.sha256, shown_sha256
        ):
            raise Conflict("предложение изменилось после показа страницы", PROPOSAL_CHANGED)
        base = {
            "id": proposal.id,
            "card": proposal.card,
            "revision": proposal.revision,
            "caller": caller,
            "at": now,
        }
        if not approve:
            try:
                _record(record, {**base, "decision": "rejected"})
            except OSError as exc:
                raise Unavailable("отказ не записан") from exc
            return "rejected"
        try:
            card = boss_exec.load_card(client, proposal.card)
        except boss_exec.Failure:
            card = None
        why = problem(proposal, card, now, channel_switch)
        if why:
            reason, text = why
            raise (Unavailable if reason == BOSS_OFF else Conflict)(text, reason)
        if proposal.kind == "delegate":
            stale = route_mismatch(scope_of(proposal)["projects"], client, registry_path)
            if stale:
                raise Conflict(stale, ROUTE_MISMATCH)
            return _delegate(proposal, base, record, card, client=client, registry_path=registry_path,
                             audit_path=audit_path, caller=caller, now=now, grants=grants)
        try:
            _, revision = boss_exec.execute(
                _request(proposal.card, proposal.revision, proposal.reason),
                client=client,
                registry_path=registry_path,
                audit_path=audit_path,
                caller=caller,
                now=lambda: now,
                audit_extra={"proposal": proposal.id, "decision": "approved"},
            )
        except boss_exec.Failure as exc:
            if exc.applied:
                raise Uncertain(
                    f"результат неизвестен: сверить журнал boss.jsonl ({exc})"
                ) from exc
            if exc.code == boss_exec.EXIT_CONFLICT:
                raise Conflict(str(exc), CARD_CHANGED) from exc
            if exc.code == boss_exec.EXIT_REFUSED:
                raise Conflict(str(exc)) from exc
            raise Unavailable(str(exc)) from exc
        try:
            _record(record, {**base, "decision": "approved", "newRevision": revision})
        except OSError as exc:
            raise Uncertain(
                "карточка переведена, решение не записано: результат неизвестен, "
                "сверить журнал boss.jsonl"
            ) from exc
        return "approved"


def _delegate(proposal, base, record, card, *, client, registry_path, audit_path, caller, now, grants) -> str:
    """Write the grant (once: id = the proposal's), record the decision, then comment on the goal (best effort)."""
    scope = scope_of(proposal)
    limits = scope["limits"]
    try:
        current = delegation.active_grant_for(grants, proposal.card, now)
        existing = None
        try:
            existing = delegation.load_grant(grants, proposal.id)
        except delegation.GrantError:
            pass
        if current is not None and current.id != proposal.id:
            raise Conflict(f"у цели уже есть действующее разрешение {current.id[:8]}")
        if existing is None:
            delegation.write_grant(grants, delegation.Grant(
                id=proposal.id, goal=proposal.card, projects=scope["projects"], max_children=limits.max_children,
                max_active=limits.max_active, created=float(now), expires=float(now) + limits.days * 86400,
                state=delegation.ACTIVE, approved_by=caller,
            ))
    except (OSError, delegation.GrantError) as exc:
        raise Unavailable("разрешение не записано") from exc
    try:
        _record(record, {**base, "decision": "approved", "grant": proposal.id})
    except OSError as exc:
        raise Uncertain("разрешение записано, решение — нет: обновите страницу и повторите") from exc
    until = datetime.fromtimestamp(now + limits.days * 86400, clock.zone()).strftime("%d.%m")
    projects = ", ".join(f"{p} → {a}" for p, a in sorted(scope["projects"].items()))
    try:
        boss_exec.execute(
            {"op": "comment", "actor": boss_board.ACTOR, "task": proposal.card, "revision": card.revision,
             "reason": f"владелец разрешил разбить цель на подзадачи: до {limits.max_children} "
                       f"(одновременно до {limits.max_active}), {projects}, до {until}; "
                       f"разрешение {proposal.id[:8]}"},
            client=client, registry_path=registry_path,
            audit_path=audit_path, caller=caller, now=lambda: now,
            audit_extra={"proposal": proposal.id, "decision": "approved", "grant": proposal.id},
        )
    except boss_exec.Failure:
        pass  # the grant is what counts; the comment only tells the board
    return "approved"


def revoke_delegation(proposal_id: str, *, grants: str = delegation.GRANTS, caller: str, now: float):
    """The owner's "Withdraw": no new subtasks and no ``work`` directive on them after this."""
    try:
        return delegation.revoke(grants, proposal_id, caller, now)
    except delegation.GrantError as exc:
        raise Missing("нет такого разрешения") from exc
    except OSError as exc:
        raise Unavailable("хранилище разрешений недоступно") from exc


def _record(path: str, value: dict) -> None:
    atomic_file.replace(
        path,
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        0o600,
    )
