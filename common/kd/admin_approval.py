"""The owner's approval of an admin card, shared by the web page and ``kd-admin-task``.

Approving re-reads everything the owner saw: the card, the agent's proposal (validated again against
the current catalog and enabled inventory) and the grant store. The page's hashes must still match,
otherwise nothing is written. An Inbox card is moved to To Do first by the cabinet's ``card.promote``
(revision-checked); a failure there leaves no grant, and a failure after it leaves To Do without
a grant, which the page reports as an unfinished approval. Cards are read through the cabinet socket.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Iterable, List, Optional, Tuple

from kd import admin_action, admin_grant, admin_proposal, admin_signed, admin_task
from kd import admin_operations as ops
from kd.cabinet import client as cabinet_client

GRANTS = "/var/lib/digipaddock/admin-grants"
PROMOTE_REASON = "план одобрен на странице"
# a plan whose every step belongs to a test environment is approved by rule, without the owner
TEST_ZONE_TTL = timedelta(hours=24)
RULE_REASON = "план тестового стенда одобрен правилом"
# a card that carries a brief is started by a person: the start approves the brief and grants its "Operations"
BRIEF_LABELS = frozenset({"brief-ready", "spec:task-brief"})


class ApprovalError(Exception):
    """Nothing was granted or revoked."""


class Refused(ApprovalError):
    """The request cannot be approved as it stands."""


class Conflict(ApprovalError):
    """The card, the proposal or the grant changed since the page was shown."""


class Unavailable(ApprovalError):
    """The board or the grant store failed; nothing may be assumed granted."""


@dataclass(frozen=True)
class Paths:
    proposals: str = admin_proposal.ROOT
    grants: str = GRANTS


@dataclass(frozen=True)
class View:
    task_id: str
    card: Optional[admin_task.Card]
    stored: Optional[admin_proposal.Stored]
    proposal: Optional[ops.Proposal]
    grant: Optional[admin_grant.Grant]
    grant_problem: Optional[str]  # why the grant does not work now
    problem: Optional[str]  # what the page must say first
    can_approve: bool

    @property
    def usable(self) -> bool:
        return self.grant is not None and self.grant_problem is None


@dataclass(frozen=True)
class ZoneApproval:
    """An approval by the test-zone rule: ``fresh`` is False when the same plan was asked again while its approval
    lives."""
    grant: admin_grant.Grant
    envs: Tuple[str, ...]
    steps: int
    fresh: bool


@dataclass(frozen=True)
class ActionView:
    task_id: str
    proposal_id: str
    card: Optional[admin_task.Card]
    stored: Optional[admin_proposal.Stored]
    action: Optional[admin_action.Action]
    grant: Optional[admin_action.ActionGrant]
    problem: Optional[str]
    can_approve: bool

    def live(self, now: datetime) -> bool:
        return self.grant is not None and self.grant.state == "active" and now < self.grant.expires_at


class Service:
    def __init__(self, paths: Paths, *, catalog: Callable[[], ops.Catalog], inventory: Callable[[], Iterable[str]],
                 enabled: Callable[[], Iterable[str]], agent_uid: Optional[int] = None,
                 approve_uid: Optional[int] = None, client=None, deny: Optional[admin_action.Deny] = None):
        self.paths = paths
        self.catalog = catalog
        self.inventory = inventory
        self.enabled = enabled
        self.agent_uid = agent_uid
        self.store = admin_grant.Store(paths.grants, approve_uid)
        self.actions = admin_action.ActionStore(paths.grants, approve_uid)
        self.client = client if client is not None else cabinet_client.Client()
        self.deny = deny

    # --- critical actions (stage E) ----------------------------------------------------------------

    def action_ids(self, task_id: str) -> List[str]:
        try:
            return admin_proposal.action_ids(self.paths.proposals, task_id)
        except admin_proposal.ProposalStoreError:
            return []

    def _action(self, stored: admin_proposal.Stored, card: admin_task.Card, proposal_id: str) -> admin_action.Action:
        if self.deny is None:
            raise ops.ProposalError("the denylist is not available; nothing can be approved")
        action = admin_action.validate_action(stored.data, self.inventory(), self.enabled(),
                                              card_servers=card.servers, deny=self.deny)
        if action.task_id != card.task_id or action.proposal_id != proposal_id:
            raise ops.ProposalError("the stored action does not match its id or card")
        return action

    def view_action(self, task_id: str, proposal_id: str, now: datetime) -> ActionView:
        task_id = admin_task.check_task_id(task_id)
        card = stored = action = grant = None
        problem = None
        try:
            card = cabinet_client.admin_card(self.client, task_id)
            admin_task.check_admin(card)
        except admin_task.CardError as exc:
            problem = f"карточка не подходит: {exc}"
        try:
            stored = admin_proposal.load_action(self.paths.proposals, task_id, proposal_id, self.agent_uid)
            if stored is None:
                problem = problem or "такого предложения действия нет"
            elif card is not None:
                action = self._action(stored, card, proposal_id)
        except (admin_proposal.ProposalStoreError, ops.ProposalError, ValueError) as exc:
            problem = problem or f"действие не проходит проверку: {exc}"
        store_ok = True
        try:
            grant = self.actions.load(task_id, proposal_id)
        except admin_grant.GrantError as exc:
            problem = problem or f"хранилище разрешений недоступно: {exc}"
            store_ok = False
        live = grant is not None and grant.state == "active" and now < grant.expires_at
        can_approve = (card is not None and action is not None and store_ok and card.status in admin_proposal.OPEN
                       and not live)
        return ActionView(task_id, proposal_id, card, stored, action, grant, problem, can_approve)

    def approve_action(self, task_id: str, proposal_id: str, *, card_sha256: str, action_sha256: str, via: str,
                       now: datetime, approved_by: str = "owner", signer: str = "",
                       signature: str = "") -> admin_action.ActionGrant:
        card = self._card(task_id)
        try:
            stored = admin_proposal.load_action(self.paths.proposals, card.task_id, proposal_id, self.agent_uid)
            if stored is None:
                raise Refused(f"{card.task_id}: no action {proposal_id}")
            action = self._action(stored, card, proposal_id)
        except (admin_proposal.ProposalStoreError, ops.ProposalError, ValueError) as exc:
            raise Refused(str(exc)) from exc
        if card.scope_sha256 != card_sha256 or action.sha256 != action_sha256:
            raise Conflict(f"{card.task_id}: the card or the action changed; reload the page")
        if card.status == "Inbox":
            card = self._to_do(card)
        elif card.status not in admin_task.ACTIVE:
            raise Refused(f"{card.task_id}: status {card.status} cannot be approved")
        try:
            admin_signed.write_action(self.paths.grants, stored, action.as_dict(), proposal_id)
            return self.actions.approve(card, action, now=now, via=via, expect_card_sha256=card_sha256,
                                        expect_action_sha256=action_sha256, approved_by=approved_by,
                                        signer=signer, signature=signature)
        except admin_grant.Conflict as exc:
            raise Conflict(str(exc)) from exc
        except (admin_grant.GrantError, admin_signed.SignedError, OSError) as exc:
            raise Unavailable(f"the action approval was not written: {exc}") from exc

    def revoke_action(self, task_id: str, proposal_id: str, *, action_id: Optional[str], via: str,
                      now: datetime, by: str = "owner") -> admin_action.ActionGrant:
        try:
            current = self.actions.load(task_id, proposal_id)
            if current is None:
                raise Refused(f"{task_id}: no approval of action {proposal_id}")
            if action_id is not None and current.action_id != action_id:
                raise Conflict(f"{task_id}: the action approval changed; reload the page")
            return self.actions.revoke(task_id, proposal_id, now=now, via=via, by=by)
        except (admin_grant.GrantError, OSError) as exc:
            raise Unavailable(f"the action approval was not revoked: {exc}") from exc

    def consume(self, task_id: str, proposal_id: str, action_sha256: str, now: datetime) -> admin_action.ActionGrant:
        """For the exec user only (the socket checks the peer): mark the approved action used before SSH."""
        card = cabinet_client.admin_card(self.client, admin_task.check_task_id(task_id))
        return self.actions.consume(task_id, proposal_id, action_sha256, card, now)

    def view(self, task_id: str, now: datetime) -> View:
        task_id = admin_task.check_task_id(task_id)
        card = stored = proposal = grant = None
        grant_problem = problem = None
        try:
            card = cabinet_client.admin_card(self.client, task_id)
            admin_task.check_admin(card)
        except admin_task.CardError as exc:
            problem = f"карточка не подходит: {exc}"
        try:
            stored = admin_proposal.load(self.paths.proposals, task_id, self.agent_uid)
            if stored is None:
                problem = problem or "предложения нет: сисадмин присылает план командой kd-admin-propose"
            elif card is not None:
                proposal = self._validate(stored, card)
        except (admin_proposal.ProposalStoreError, ops.ProposalError, ops.CatalogError) as exc:
            problem = problem or f"предложение не проходит проверку: {exc}"
        except ValueError as exc:  # inventory or enabled list unreadable
            problem = problem or f"инвентарь недоступен: {exc}"
        try:
            grant = self.store.load(task_id)
        except admin_grant.GrantError as exc:
            problem = problem or f"хранилище разрешений недоступно: {exc}"
            grant_problem = str(exc)
        store_ok = grant_problem is None
        if grant is not None:
            try:
                if card is None:
                    raise admin_grant.GrantError("the card cannot be read")
                admin_grant.check_usable(grant, card, now)
            except (admin_grant.GrantError, admin_task.CardError) as exc:
                grant_problem = str(exc)
        elif card is not None and card.status in admin_task.ACTIVE and problem is None:
            problem = (f"карточка в {card.status}, разрешения нет — нужно завершить согласование: "
                       "без него сисадмин не выполнит ни одной операции")
        can_approve = (card is not None and proposal is not None and store_ok
                       and card.status in admin_proposal.OPEN)
        return View(task_id, card, stored, proposal, grant, grant_problem, problem, can_approve)

    def approve_test_zone(self, task_id: str, *, card_sha256: str, proposal_sha256: str,
                          now: datetime) -> Optional[ZoneApproval]:
        """the approval of a plan whose every step belongs to a test environment of the catalog — by rule,
        for ``TEST_ZONE_TTL``, with no signature; None for any other plan and for a card whose approval the owner
        revoked (he decides those). The grant names the rule: the executor runs its steps only while they still are
        a test environment's, and a target only those of its own list."""
        card = self._card(task_id)
        try:
            stored = admin_proposal.load(self.paths.proposals, card.task_id, self.agent_uid)
            proposal = self._validate(stored, card) if stored is not None else None
            envs = ops.test_zone(proposal, self.catalog()) if proposal is not None else ()
        except (admin_proposal.ProposalStoreError, ops.ProposalError, ops.CatalogError, ValueError) as exc:
            raise Refused(str(exc)) from exc
        if not envs or (card.status == "Inbox" and BRIEF_LABELS & set(card.labels)):
            return None
        if card.scope_sha256 != card_sha256 or proposal.sha256 != proposal_sha256:
            raise Conflict(f"{card.task_id}: the card or the proposal changed; propose again")
        try:
            current = self.store.load(card.task_id)
        except admin_grant.GrantError as exc:
            raise Unavailable(f"the grant store is unreadable: {exc}") from exc
        if current is not None and current.state == "revoked":
            return None  # the owner took this card's approval back: its next plan, reworded or not, is his to decide
        if current is not None and current.proposal_sha256 == proposal.sha256:
            try:
                admin_grant.check_usable(current, card, now)
                return ZoneApproval(current, envs, len(proposal.operations), False)
            except (admin_grant.GrantError, admin_task.CardError):
                pass  # expired or the card moved on: a new approval below
        grant = self.approve(card.task_id, card_sha256=card_sha256, proposal_sha256=proposal_sha256,
                             ttl=TEST_ZONE_TTL, via="rule", now=now, approved_by=admin_grant.TEST_ZONE,
                             reason=RULE_REASON)
        return ZoneApproval(grant, envs, len(proposal.operations), True)

    def approve(self, task_id: str, *, card_sha256: str, proposal_sha256: str, ttl: timedelta, via: str,
                now: datetime, approved_by: str = "owner", signer: str = "", signature: str = "",
                reason: str = PROMOTE_REASON) -> admin_grant.Grant:
        if not timedelta(0) < ttl <= admin_grant.MAX_TTL:
            raise Refused(f"ttl must be within {admin_grant.MAX_TTL.days} days")
        card = self._card(task_id)
        try:
            stored = admin_proposal.load(self.paths.proposals, card.task_id, self.agent_uid)
        except admin_proposal.ProposalStoreError as exc:
            raise Refused(str(exc)) from exc
        if stored is None:
            raise Refused(f"{card.task_id}: no proposal")
        try:
            proposal = self._validate(stored, card)
        except (ops.ProposalError, ops.CatalogError, ValueError) as exc:
            raise Refused(str(exc)) from exc
        if card.scope_sha256 != card_sha256 or proposal.sha256 != proposal_sha256:
            raise Conflict(f"{card.task_id}: the card or the proposal changed; reload the page")
        if card.status == "Inbox":
            card = self._to_do(card, reason, approved_by if approved_by == admin_grant.TEST_ZONE else "")
        elif card.status not in admin_task.ACTIVE:
            raise Refused(f"{card.task_id}: status {card.status} cannot be approved")
        try:
            # the signed text first, for the exec user who cannot read the proposals; then the grant
            admin_signed.write_plan(self.paths.grants, stored, proposal.as_dict(), proposal.sha256)
            return self.store.approve(card, proposal, now=now, ttl=ttl, via=via,
                                      expect_card_sha256=card_sha256, expect_proposal_sha256=proposal_sha256,
                                      approved_by=approved_by, signer=signer, signature=signature)
        except admin_grant.Conflict as exc:
            raise Conflict(str(exc)) from exc
        except (admin_grant.GrantError, admin_signed.SignedError, OSError) as exc:
            raise Unavailable(f"the grant was not written: {exc}") from exc

    def backfill(self, now: datetime) -> List[str]:
        """signed copies for the live grants approved before the copies existed (the cabinet's start)."""
        return admin_signed.backfill(self.paths.grants, self.paths.proposals, grant_uid=self.store.owner_uid,
                                     agent_uid=self.agent_uid, now=now)

    def revoke(self, task_id: str, *, grant_id: Optional[str], via: str, now: datetime,
               by: str = "owner") -> admin_grant.Grant:
        task_id = admin_task.check_task_id(task_id)
        try:
            current = self.store.load(task_id)
            if current is None:
                raise Refused(f"{task_id}: no grant to revoke")
            if grant_id is not None and current.grant_id != grant_id:
                raise Conflict(f"{task_id}: the grant changed; reload the page")
            return self.store.revoke(task_id, now=now, via=via, by=by)
        except (admin_grant.GrantError, OSError) as exc:
            raise Unavailable(f"the grant was not revoked: {exc}") from exc

    def _card(self, task_id: str) -> admin_task.Card:
        try:
            card = cabinet_client.admin_card(self.client, task_id)
            admin_task.check_admin(card)
        except admin_task.CardError as exc:
            raise Refused(str(exc)) from exc
        return card

    def _validate(self, stored: admin_proposal.Stored, card: admin_task.Card) -> ops.Proposal:
        proposal = ops.validate_proposal(stored.data, self.catalog(), self.inventory(), self.enabled(),
                                         card_servers=card.servers)
        if proposal.task_id != card.task_id:
            raise ops.ProposalError(f"the proposal is for {proposal.task_id}, the card is {card.task_id}")
        return proposal

    def _to_do(self, card: admin_task.Card, reason: str = PROMOTE_REASON, rule: str = "") -> admin_task.Card:
        """The owner's Inbox -> To Do, revision-checked by the cabinet (the approve user's ``card.promote``); with
        ``rule`` — the test zone's start, which is nobody's word."""
        try:
            moved = self.client.call({"op": "card.promote", "card": card.task_id, "revision": card.revision,
                                      "reason": reason, "quiet": True,  # the approval wakes
                                      **({"rule": rule} if rule else {})})["card"]
        except cabinet_client.CabinetError as exc:
            if exc.kind == "conflict":
                raise Conflict(f"{card.task_id}: the card changed; reload the page") from exc
            if exc.kind in ("refused", "invalid", "not_found"):
                raise Refused(f"{card.task_id}: {exc}") from exc
            raise Unavailable(f"{card.task_id}: the board edit failed ({exc}); reload the page") from exc
        try:
            moved = admin_task.from_record(moved)
        except admin_task.CardError as exc:
            raise Unavailable(f"{card.task_id}: the board answered an unreadable card ({exc})") from exc
        if moved.status != "To Do" or moved.scope_sha256 != card.scope_sha256:
            raise Conflict(f"{card.task_id}: the card changed; reload the page")
        return moved
