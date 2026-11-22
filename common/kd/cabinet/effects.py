"""What an approved, refused or revoked decision does outside the cabinet.

Nothing new grants anything: an ``admin_plan`` goes to ``admin_approval.Service.approve`` (the same
grant file and journal the sysadmin's executors read), an ``admin_critical`` to ``approve_action``,
a BOSS ``promote`` or ``delegate`` to ``boss_promote.decide``. The cabinet records the decision only
after the effect succeeded; a failed effect leaves the decision open. A question has no effect.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from kd import admin_approval, admin_grant, admin_task, boss_promote
from kd.cabinet import decisions, users

VIA = "cabinet"
TTL_HOURS = (
    24,
    72,
    168,
)  # what the page offers for a plan; the grant store allows up to seven days


class EffectError(Exception):
    """The effect did not happen (or its result is unknown); ``kind`` is the API error kind."""

    def __init__(self, kind: str, message: str, reason: str = ""):
        super().__init__(message)
        self.kind, self.reason = kind, reason  # reason: why, for the page (boss_promote.BOSS_OFF, ...)


def _moment(now: str) -> datetime:
    return users.parse_time(now).replace(tzinfo=timezone.utc)


def _admin(ctx):
    if getattr(ctx, "admin", None) is None:
        raise EffectError("unavailable", "admin approvals are not served here")
    return ctx.admin


def _promote(ctx):
    if getattr(ctx, "promote", None) is None:
        raise EffectError("unavailable", "BOSS proposals are not served here")
    return ctx.promote


def _admin_errors(call):
    try:
        return call()
    except admin_approval.Conflict as exc:
        raise EffectError("conflict", str(exc)) from exc
    except admin_approval.Refused as exc:
        raise EffectError("refused", str(exc)) from exc
    except (admin_approval.Unavailable, admin_grant.GrantError, OSError) as exc:
        raise EffectError("unavailable", str(exc)) from exc


def _boss_errors(call):
    try:
        return call()
    except boss_promote.Conflict as exc:
        raise EffectError("conflict", str(exc), exc.reason) from exc
    except boss_promote.Missing as exc:
        raise EffectError("not_found", str(exc)) from exc
    except boss_promote.Unavailable as exc:
        raise EffectError("unavailable", str(exc), exc.reason) from exc
    except (boss_promote.Uncertain, OSError) as exc:
        raise EffectError("unavailable", str(exc)) from exc


def ttl(type_: str, ttl_hours: Optional[int]) -> timedelta:
    if ttl_hours is None:
        return timedelta(
            seconds=decisions.TTL_SECONDS.get(type_)
            or admin_grant.DEFAULT_TTL.total_seconds()
        )
    if isinstance(ttl_hours, bool) or ttl_hours not in TTL_HOURS:
        raise EffectError(
            "invalid", f"ttl_hours must be one of {', '.join(map(str, TTL_HOURS))}"
        )
    return timedelta(hours=ttl_hours)


def apply(ctx, decision, *, user, now, ttl_hours=None, signer="", signature="", review=None) -> str:
    """Run the approval's effect; the grant id (or ``""``) for the decision row. A pull request's effect, kd-review's
    Approve in Forgejo, has happened before (the web layer asks it outside the service's lock): its review
    id is the grant."""
    kind, p = decision["type"], decision["payload"]
    by = f"user:{user}"
    if kind == "admin_plan" and p.get("policy") == "factory":
        return _factory_approve(ctx, p, by=by, now=now, ttl_hours=ttl_hours, signer=signer,
                                signature=signature).grant_id
    if kind == "admin_plan":
        admin = _admin(ctx)
        grant = _admin_errors(
            lambda: admin.approve(
                p["task_id"],
                card_sha256=p["card_scope_sha256"],
                proposal_sha256=p["proposal_sha256"],
                ttl=ttl(kind, ttl_hours),
                via=VIA,
                now=_moment(now),
                approved_by=by,
                signer=signer,
                signature=signature,
            )
        )
        return grant.grant_id
    if kind == "admin_critical":
        admin = _admin(ctx)
        grant = _admin_errors(
            lambda: admin.approve_action(
                p["task_id"],
                p["proposal_id"],
                card_sha256=p["card_scope_sha256"],
                action_sha256=p["proposal_sha256"],
                via=VIA,
                now=_moment(now),
                approved_by=by,
                signer=signer,
                signature=signature,
            )
        )
        return grant.action_id
    if kind == "pr_approve":
        if not review:
            raise EffectError("invalid", "a pull request's approval needs kd-review's Approve")
        return str(review["id"])
    if kind in ("promote", "delegate"):
        result = _decide(ctx, decision, True, by, now)
        return p["proposal_id"] if kind == "delegate" and result == "approved" else ""
    return ""


def refuse(ctx, decision, *, user, now) -> None:
    """A rejected BOSS proposal is recorded as such; an admin plan simply stays without a grant."""
    if decision["type"] in ("promote", "delegate"):
        _decide(ctx, decision, False, f"user:{user}", now)


def revoke(ctx, decision, *, user, now) -> None:
    kind, p, by = decision["type"], decision["payload"], f"user:{user}"
    if kind == "admin_plan" and p.get("policy") == "factory":
        _factory_revoke(ctx, p["task_id"], grant_id=decision["grant_id"], by=by, now=now)
    elif kind == "admin_plan":
        admin = _admin(ctx)
        _admin_errors(
            lambda: admin.revoke(
                p["task_id"],
                grant_id=decision["grant_id"] or None,
                via=VIA,
                now=_moment(now),
                by=by,
            )
        )
    elif kind == "admin_critical":
        admin = _admin(ctx)
        _admin_errors(
            lambda: admin.revoke_action(
                p["task_id"],
                p["proposal_id"],
                action_id=decision["grant_id"] or None,
                via=VIA,
                now=_moment(now),
                by=by,
            )
        )
    elif kind == "delegate":
        settings = _promote(ctx)
        _boss_errors(
            lambda: boss_promote.revoke_delegation(
                p["proposal_id"],
                grants=settings.grants,
                caller=by,
                now=_moment(now).timestamp(),
            )
        )
    else:
        raise EffectError("refused", f"a {kind} decision cannot be revoked")


# --- the factory's grants: the same store and format, the factory policy ------------------------
def _grant_errors(call):
    try:
        return call()
    except admin_grant.Conflict as exc:
        raise EffectError("conflict", str(exc)) from exc
    except admin_task.CardError as exc:
        raise EffectError("refused", str(exc)) from exc
    except (admin_grant.GrantError, OSError) as exc:
        raise EffectError("unavailable", f"the grant was not written: {exc}") from exc


def signed_tree(ctx, proposal):
    """A component step covers the tree deployed at the signature — the plan's own
    revision is the tree it was opened on, and a release of another card in between must not void the signature."""
    from kd import factory_ops
    from kd.cabinet import api, operations

    if not factory_ops.binds_tree(proposal.operations):
        return proposal
    try:
        return factory_ops.at_revision(proposal, operations.deployed(ctx))
    except (api.ApiError, factory_ops.ProposalError) as exc:
        raise EffectError("unavailable", f"the deployed revision: {exc}") from exc


def _factory_approve(ctx, p, *, by, now, ttl_hours=None, signer="", signature="", card=None, proposal=None):
    """The owner's approval of exactly the factory plan the page showed: the card and the plan are read and checked
    again, and the grant written by ``admin_grant.Store`` under the factory policy."""
    from kd.cabinet import api, store

    admin = _admin(ctx)
    if card is None or proposal is None:
        try:
            record = ctx.store.card(p["task_id"])
        except store.NotFound as exc:
            raise EffectError("not_found", str(exc)) from exc
        try:
            card, proposal = api.factory_plan(ctx, record, p["proposal"])
        except api.ApiError as exc:
            raise EffectError("refused", str(exc)) from exc
        if (card.scope_sha256, proposal.sha256) != (p["card_scope_sha256"], p["proposal_sha256"]):
            raise EffectError("conflict", f"{card.task_id}: the card or the plan changed; ask for it again")
        proposal = signed_tree(ctx, proposal)
    if card.status not in admin_task.ACTIVE:
        raise EffectError("refused", f"{card.task_id}: status {card.status} — start the card first")
    return _grant_errors(lambda: admin.store.approve(
        card, proposal, now=_moment(now), ttl=ttl("admin_plan", ttl_hours), via=VIA,
        expect_card_sha256=card.scope_sha256, expect_proposal_sha256=proposal.sha256, approved_by=by,
        signer=signer, signature=signature, policy=admin_grant.FACTORY, factory=ctx.factory().scope))


def _factory_revoke(ctx, task_id, *, grant_id, by, now):
    from kd.cabinet import operations

    admin, scope = _admin(ctx), ctx.factory().scope
    current = _grant_errors(lambda: admin.store.load(task_id, policies=(admin_grant.FACTORY,), factory=scope))
    if current is None:
        raise EffectError("refused", f"{task_id}: no grant to revoke")
    if grant_id and current.grant_id != grant_id:
        raise EffectError("conflict", f"{task_id}: the grant changed; reload the page")
    _grant_errors(lambda: admin.store.revoke(task_id, now=_moment(now), via=VIA, by=by,
                                             policies=(admin_grant.FACTORY,), factory=scope))
    operations.on_revoked(ctx, task_id)


BRIEF_INTENT = "Операции одобренного ТЗ v{version}"
BRIEF_BASIS = "ТЗ v{version} одобрено при запуске карточки"


def brief_grant(ctx, record, *, by: str) -> Optional[admin_grant.Grant]:
    """An approved brief whose version has "Operations" grants exactly them on the card's start: the plan
    is that version's steps, the grant the same factory grant, with no second signature — the owner approved the
    version that shows them. Nothing else grants: a note, a comment or a card without such a version gives none.
    A card whose assignee or project is not the factory's gets none either (the steps wait for a plan), and so does
    a start by anyone who may not approve the factory's plans (a partner's promote, root): its steps wait for one."""
    from kd.cabinet import api, briefs

    last = briefs.latest(ctx.store, record.id)
    if last is None or not last["approved_by"] or not last["operations"]:
        return None
    person = by[5:] if by.startswith("user:") else ""
    users = getattr(ctx, "users", None)
    if not person or users is None or not users.can(person, "decide.admin_plan", record.project):
        return None
    if getattr(ctx, "admin", None) is None:
        raise EffectError("unavailable", "the grant store is not served here")
    catalog = ctx.factory()
    if record.assignee[1:] not in catalog.requesters or record.project not in catalog.projects:
        return None
    data = {"schema_version": 2, "task_id": record.id, "intent": BRIEF_INTENT.format(version=last["version"]),
            "basis": BRIEF_BASIS.format(version=last["version"]), "operations": last["operations"]}
    try:
        card, proposal = api.factory_plan(ctx, record, data)
    except api.ApiError as exc:
        raise EffectError("refused", str(exc)) from exc
    proposal = signed_tree(ctx, proposal)
    current = _grant_errors(lambda: ctx.admin.store.load(record.id, policies=(admin_grant.FACTORY,),
                                                         factory=catalog.scope))
    same = (current is not None and current.state == "active" and current.proposal_sha256 == proposal.sha256
            and current.card_scope_sha256 == card.scope_sha256)
    if same:
        return current  # started again: the same grant holds
    return _factory_approve(ctx, {}, by=by, now=ctx.now(), ttl_hours=168, card=card, proposal=proposal)


def _decide(ctx, decision, approve: bool, by: str, now: str) -> str:
    settings, p = _promote(ctx), decision["payload"]
    result = _boss_errors(
        lambda: boss_promote.decide(
            p["proposal_id"],
            p["proposal_sha256"],
            approve,
            client=settings.client,
            registry_path=settings.registry_path,
            audit_path=settings.audit_path,
            caller=by,
            now=_moment(now).timestamp(),
            proposals=settings.proposals,
            decisions=settings.decisions,
            channel_switch=settings.channel_switch,
            grants=settings.grants,
        )
    )
    if result == "already-decided":
        raise EffectError("refused", "the BOSS proposal is already decided")
    return result
