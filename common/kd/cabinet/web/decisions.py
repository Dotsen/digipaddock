"""Decisions over HTTP: what a decision asks, and the person's answer to it.

``GET /api/decisions[?state=&card=]`` and ``GET /api/decisions/{id}`` give ``{decision, view}``; the
view is what the decision page shows for its type: a plan's steps with their exact commands, a
critical action, a BOSS proposal with the card's description and AC, a question. ``stale`` says the
decision no longer matches what it was opened over (the card or the proposal moved on). A BOSS proposal's view asks
the approval's own checks (``boss_promote.problem``): ``blocked`` — why it cannot be approved for now (BOSS is
off while a release reinstalls it), so the page offers no approval the board would refuse.

Approving is two calls. ``challenge`` binds the decisions, the person, the cards' current revisions and
a five-minute term into one value (``Decisions.challenge``, hashed with a fresh nonce; the stored
signature keeps the term and the nonce). ``approve`` takes a passkey assertion over that value (or a TOTP code, five misses per decision and only a passkey is left) and only then asks the
board, as the cabinet itself, to approve; the board checks state, term, permission and the bound
revisions again. A type without step-up (promote, question) still goes through a challenge, which
holds the revisions the page showed, but needs no signature.

A pull request (``pr_approve``) is signed alone, with a passkey only, and its value binds the mode the
person chose (approve, merge, release: ``pr_signature.challenge``). The Approve in Forgejo is kd-review's: the
board checks the decision first, kd-review checks the signature itself and posts the Approve, and only then the
board records the decision with kd-review's review id — the two kd-review calls run outside the service's lock.
Remarks are recorded first and reach Forgejo best effort.
"""

import hashlib
import json
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, Request

from kd import admin_action, boss_board, boss_promote, factory_ops, pr_signature
from kd import admin_operations as ops
from kd.cabinet import (
    api, briefs, councils, decisions, effects, passkeys, prs_api, questions, review_client, store, users,
)
from kd.cabinet.web import deps, stepup
from kd.cabinet.web.cards import call
from kd.cabinet.web.errors import Failure

router = APIRouter()
CHALLENGE_SECONDS = 300
TOTP_TRIES = 5  # wrong codes per decision; then only a passkey signs it
CABINET = api.Principal("cabinet", -1, "cabinet")
LIST_STATES = decisions.STATES + ("all",)
COMMENTS_MAX, COMMENT_MAX = 30, 1000  # line remarks on a PR
SIDES = ("new", "old")


# --- views ----------------------------------------------------------------------------------------
def _card(service, decision) -> Optional[dict]:
    try:
        return api.compact(service.ctx.store.card(decision["card"]))
    except store.NotFound:
        return None


def _mechanics(op, deployed: str = "") -> str:
    if op.get("component"):  # the component, its mode and the tree the signature covers — the one deployed
        now = factory_ops.short(deployed)
        tree = f"ревизия на момент подписи (сейчас {now})" if deployed else "ревизия на момент подписи"
        return f"{op['verb']} · {op['component']} {op.get('mode', '')} · {tree}"
    return f"{op['verb']} · {', '.join(op.get('agents') or ())}"


def _deployed(service) -> str:
    from kd.cabinet import operations

    try:
        return operations.deployed(service.ctx)
    except api.ApiError:
        return ""


def _command(service, op) -> str:
    """A component step's exact commands from the deployed catalog (the run, then its check): what the signature
    lets the root runner execute; "" for a reinstall or a step the catalog no longer has."""
    if not op.get("component"):
        return ""
    try:
        mode = service.ctx.factory().mode(op["component"], op.get("mode", ""))
    except Exception:  # noqa: BLE001 — the view's problem line says why the plan is stale
        return ""
    lines = [" ".join(mode.run)] + ([" ".join(mode.check)] if mode.check else [])
    if mode.drain == "all":
        lines.insert(0, "# после конца ходов всех ролей (пауза, очередь сохраняется)")
    return "\n".join(lines)


def _factory_plan(service, decision) -> dict:
    """The developer's plan of factory operations: the steps travel in the decision; the view checks the
    card and the plan as the approval will."""
    payload = decision["payload"]
    plan, problem = payload["proposal"], None
    try:
        record = service.ctx.store.card(decision["card"])
        card, proposal = api.factory_plan(service.ctx, record, plan)
        stale = (proposal.sha256, card.scope_sha256) != (payload["proposal_sha256"], payload["card_scope_sha256"])
    except (store.NotFound, api.ApiError) as exc:
        stale, problem = True, str(exc)
    # an open plan shows the tree a signature now covers (the challenge binds it); a decided one names none
    component = any(op.get("component") for op in plan.get("operations") or ())
    deployed = _deployed(service) if component and decision.get("state", "open") == "open" else ""
    steps = [{"n": n, "what": op["what"] or op["verb"], "mechanics": _mechanics(op, deployed),
              "effect": op["effect"], "risk": op["risk"], "rollback": op["rollback"], "command": _command(service, op),
              "servers": [], "agents": list(op.get("agents") or ()), "production": False}
             for n, op in enumerate(plan.get("operations", ()), 1)]
    view = {"card": _card(service, decision), "policy": "factory", "intent": plan.get("intent", ""),
            "basis": plan.get("basis", ""), "steps": steps, "ttl_options": list(effects.TTL_HOURS), "stale": stale}
    if deployed:
        view["deployed"] = deployed
    if problem:
        view["problem"] = problem
    return view


def _admin_plan(service, decision) -> dict:
    payload, admin = decision["payload"], service.ctx.admin
    if payload.get("policy") == "factory":
        return _factory_plan(service, decision)
    if admin is None:
        return {"stale": True, "problem": "admin approvals are not served here"}
    seen = admin.view(payload["task_id"], users.parse_time(service.now()))
    proposal, steps = seen.proposal, []
    for n, op in enumerate(proposal.operations if proposal else (), 1):
        try:
            command = ops.remote_command(op, admin.catalog())
        except (ops.ProposalError, ops.CatalogError, KeyError):
            command = ""  # a stored plan the current catalog no longer describes: the page shows the mechanics
        steps.append(
            {
                "n": n,
                "what": op.what or ops.mechanics(op),
                "mechanics": ops.mechanics(op),
                "effect": op.effect,
                "risk": {"level": op.risk_level, "why": op.risk_why},
                "rollback": op.rollback,
                "command": command,
                # the page shows every step's command unfolded for the owner to read before signing — a
                # script step's is its exact text and is labelled as a script; a typed step's (a cron line,
                # a dropped file's text) is shown whole too
                "kind": "script" if op.verb == "script" else "typed",
                "servers": [op.server],
                "production": op.server == admin_action.production(),
            }
        )
    bound = (payload["proposal_sha256"], payload["card_scope_sha256"])
    view = {
        "card": _card(service, decision),
        "intent": proposal.intent if proposal else "",
        "basis": proposal.basis if proposal else "",
        "steps": steps,
        "ttl_options": list(effects.TTL_HOURS),
        "stale": proposal is None
        or seen.card is None
        or (proposal.sha256, seen.card.scope_sha256) != bound,
    }
    if seen.problem:
        view["problem"] = seen.problem
    return view


def _admin_critical(service, decision) -> dict:
    payload, admin = decision["payload"], service.ctx.admin
    if admin is None:
        return {"stale": True, "problem": "admin approvals are not served here"}
    seen = admin.view_action(
        payload["task_id"], payload["proposal_id"], users.parse_time(service.now())
    )
    action = seen.action
    if action is None:
        return {"stale": True, "problem": seen.problem or "the action cannot be read"}
    bound = (payload["proposal_sha256"], payload["card_scope_sha256"])
    view = {
        "card": _card(service, decision),
        "intent": action.intent,
        "basis": action.basis,
        "effect": action.effect,
        "risk": {"level": action.risk_level, "why": action.risk_why},
        "rollback": action.rollback,
        "command": action.command,
        "servers": list(action.servers),
        "production": admin_action.production() in action.servers,
        "stale": seen.card is None or (action.sha256, seen.card.scope_sha256) != bound,
    }
    if seen.problem:
        view["problem"] = seen.problem
    return view


def _boss(service, decision) -> dict:
    payload, settings = decision["payload"], service.ctx.promote
    if settings is None:
        return {"stale": True, "problem": "BOSS proposals are not served here"}
    try:
        proposal = boss_promote.load(payload["proposal_id"], settings.proposals)
        card = service.ctx.store.card(decision["card"])
    except (boss_promote.Missing, store.NotFound) as exc:
        return {"stale": True, "problem": str(exc)}
    view = {
        "title": proposal.title,
        "reason": proposal.reason,
        "revision": proposal.revision,
        "project": proposal.project,
        "assignee": proposal.assignee,
        "description": card.description,
        "acceptance": card.to_dict()["acceptance"],
        "operations": [dict(op) for op in card.operations],  # what approving the brief grants
        "stale": proposal.sha256 != payload["proposal_sha256"]
        or card.revision != proposal.revision,
    }
    now = users.parse_time(service.now()).timestamp()
    why = boss_promote.problem(proposal, boss_board.card_from(card.to_dict()), now, settings.channel_switch)
    if why and why[0] == boss_promote.BOSS_OFF:
        view["blocked"] = why[0]
    elif why:
        view.update(stale=True, problem=why[1])
    if proposal.kind == "promote" and briefs.state(service.ctx.store, card)["state"] == "stale":
        view.update(stale=True, brief_stale=True)  # the approval would refuse it (brief_stale): say so first
    if proposal.kind == "delegate":
        try:
            scope = boss_promote.scope_of(proposal)
        except boss_promote.Missing as exc:
            return {**view, "stale": True, "problem": str(exc)}
        limits = scope["limits"]
        view["scope"] = {
            "max_children": limits.max_children,
            "max_active": limits.max_active,
            "days": limits.days,
            "projects": scope["projects"],
        }
    return view


def _pr(service, decision) -> dict:
    """The PR as the forge executor last published it; its files only while they are of this decision's head.
    ``council`` — what the card's council of the code said of this head (``councils.pull_review``);
    absent on a card without critics of the code."""
    payload = decision["payload"]
    review = councils.pull_review(service.ctx.store, decision["card"], payload)
    carried = (decision.get("answer") or {}).get("carried_from") if decision["state"] == "approved" else None
    return {
        **({"council": review} if review else {}),
        # the owner's signature of another head carried here (KD-9442): the head it was given on, and its mode
        **({"carried": {**carried, "mode": decision["answer"].get("mode", "")}} if carried else {}),
        "card": _card(service, decision),
        **prs_api.pull_view(service.ctx.store, payload),
        # a chain plan's open pull requests: with several, the release tick is one rollout after the last merge
        "plan_pulls": prs_api.plan_pulls(service.ctx.store, decision["card"]),
    }


def _trust(service, decision) -> dict:
    """What becomes trusted: the kind, its P-n or server or address, who proposed it and why, how the
    monitor applied it; ``stale`` — its P-n closed or is gone."""
    p, store_ = decision["payload"], service.ctx.store
    view = {"kind": p.get("kind", ""), "proposed_by": p.get("proposed_by", decision["requested_by"]),
            "text": p.get("text", ""), "label": p.get("label", ""), "server": p.get("server", ""),
            "cidr": p.get("cidr", ""), "apply": {"state": decision.get("apply_state", ""),
                                                 "detail": decision.get("apply_detail", ""),
                                                 "at": decision.get("applied_at", "")}, "stale": False}
    if p.get("incident"):
        row = store_._conn.execute("SELECT * FROM incidents WHERE n=?", (p["incident"],)).fetchone()
        view["incident"] = {"id": f"P-{p['incident']}", "check": p.get("check", ""), "subject": p.get("subject", ""),
                            "value": p.get("value", ""), "status": row["status"] if row else "",
                            "level": row["level"] if row else ""}
        view["stale"] = row is None or row["status"] == "resolved"
    return view


VIEWS = {
    "admin_plan": _admin_plan,
    "admin_critical": _admin_critical,
    "promote": _boss,
    "delegate": _boss,
    "question": lambda service, decision: questions.view(decision),
    prs_api.TYPE: _pr,
    decisions.TRUST: _trust,
}


def view_of(service, decision) -> dict:
    """What the page shows for the decision; ``{}`` for a type nothing opens yet."""
    build = VIEWS.get(decision["type"])
    view = build(service, decision) if build else {}
    if decision["state"] == "open" and decision.get("question"):  # the agent owes the person an answer
        view["asked_open"] = decisions.Decisions(service.ctx.store).asked_open(decision)
    return view


async def _with_view(service, decision) -> dict:
    view = await service.locked(lambda: view_of(service, decision))
    return {"decision": decision, "view": view}


@router.get("/api/decisions")
async def listing(
    request: Request,
    user: deps.User = Depends(deps.current_user),
    state: str = "open",
    card: Optional[str] = None,
    proposal: Optional[str] = None,
):
    """Decisions the person may see; ``proposal`` (32 hex) — the one opened for that proposal (the old
    /boss/promotions/<id> links reach their decision through it)."""
    if state not in LIST_STATES:
        raise Failure(400, "invalid", f"state must be one of {', '.join(LIST_STATES)}")
    if proposal is not None and not api.DECISION_ID.fullmatch(proposal):
        raise Failure(400, "invalid", "proposal must be 32 hex characters")
    op = {"op": "decision.list", **({} if state == "all" else {"state": state})}
    if card:
        op["card"] = card
    found = (await call(request, user, op))["decisions"]
    if proposal:
        found = [d for d in found if d["payload"].get("proposal_id") == proposal]
    service = deps.svc(request)
    return [await _with_view(service, d) for d in found]


@router.get("/api/decisions/{decision_id}")
async def one(
    decision_id: str, request: Request, user: deps.User = Depends(deps.current_user)
):
    found = (await call(request, user, {"op": "decision.get", "id": decision_id}))[
        "decision"
    ]
    return await _with_view(deps.svc(request), found)


@router.get("/api/decisions/{decision_id}/diff")
async def diff(decision_id: str, request: Request, user: deps.User = Depends(deps.current_user)):
    """The PR's diff as published for this decision's head; 409 ``stale`` once the PR moved on."""
    found = (await call(request, user, {"op": "decision.get", "id": decision_id}))["decision"]
    if found["type"] != prs_api.TYPE:
        raise Failure(404, "not_found", f"{decision_id}: not a pull request")
    payload, service = found["payload"], deps.svc(request)
    row = await service.locked(lambda: service.ctx.store.pr(payload["repo"], payload["number"]))
    if row is None or row["head"] != payload["head"]:
        raise Failure(409, "stale", f"{decision_id}: the PR moved on from {payload['head'][:7]}")
    return {"diff": row["diff"], "truncated": row["truncated"], "head": row["head"]}


# --- approving ------------------------------------------------------------------------------------
def _ids(value) -> list:
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= api.BATCH_MAX
        or not all(isinstance(i, str) and api.DECISION_ID.fullmatch(i) for i in value)
        or len(set(value)) != len(value)
    ):
        raise Failure(400, "invalid", f"ids must be 1 to {api.BATCH_MAX} decision ids")
    return value


PASSKEY_ONLY = "a server or factory plan, a critical command or a monitor trust is signed with a passkey"


def _passkey_only(decision: dict) -> bool:
    """a plan (a server's or the factory's), a critical command and a trust of the monitor are signed with a
    passkey only: the targets' exec gate verifies a passkey assertion — a TOTP code is no signature there —
    and the security model promises one for each of them."""
    return decision["type"] in ("admin_critical", "admin_plan", "monitor_trust")


@router.post("/api/decisions/challenge")
async def challenge(request: Request, user: deps.User = Depends(deps.writer)):
    """One value for the person to sign over the decisions as the page shows them now."""
    service = deps.svc(request)
    body = await deps.json_body(request)
    ids = _ids(body.get("ids"))
    found = [
        (await call(request, user, {"op": "decision.get", "id": i}))["decision"]
        for i in ids
    ]
    if len({d["type"] for d in found}) != 1:
        raise Failure(400, "invalid", "a batch holds decisions of one type")
    now = service.now()
    for d in found:
        if d["state"] != "open":
            raise Failure(409, "conflict", f"{d['id']}: the decision is {d['state']}")
        if d["expires"] and d["expires"] <= now:
            raise Failure(410, "expired", f"{d['id']}: the decision expired")
        if not service.users.can(user.id, d["decide_permission"], d["project"] or None):
            raise Failure(403, "refused", f"{d['id']}: not yours to decide")
    if found[0]["type"] == prs_api.TYPE:
        if len(found) != 1:
            raise Failure(400, "invalid", "a pull request is approved alone")
        return await _pr_challenge(request, user, found[0], body.get("mode"))
    step_up = any(d["step_up"] for d in found)
    passkey_only = any(_passkey_only(d) for d in found)
    allow = service.users.credential_ids(user.id, signing=True) if step_up else []
    totp = step_up and not passkey_only and service.users.has_second_factor(user.id)["totp"]
    if step_up and not allow and not totp:
        raise Failure(403, "setup_required",
                      "add a passkey first: a plan, a critical command or a monitor trust is signed with one"
                      if passkey_only else "add a passkey or TOTP first")
    revisions = {decisions.basis_key(d): api.decision_basis(service.ctx, d) for d in found}
    for d in found:  # the tree a component step will be signed for
        tree = api.deployed_basis(service.ctx, d)
        if tree is not None:
            revisions[api.deployed_key(d)] = tree
    expires = users.shift(now, seconds=CHALLENGE_SECONDS)
    try:
        digests = service.decisions.digests(
            ids, user=user.id, expires=expires, revisions=revisions
        )
    except decisions.DecisionError as exc:
        raise api.ApiError(exc.kind, str(exc))
    bound = decisions.Decisions.bound(digests)
    # the spec's digest is the same for two requests in one second; a nonce makes every challenge fresh
    nonce = secrets.token_bytes(16)
    value = hashlib.sha256(bound + nonce).digest()
    challenge_id = secrets.token_urlsafe(16)
    # the binding is what a target recomputes the signed value from — the revisions the challenge saw
    # and, for a batch, the digest of every decision in it (one decision: its digest is its own)
    binding = {"expires": expires, "nonce": nonce.hex(), "revisions": revisions}
    if len(ids) > 1:
        binding["digests"] = digests
    service.challenges.put(
        "decision",
        challenge_id,
        {
            "ids": ids,
            "user": user.id,
            "session": user.session_id,
            "expires": expires,
            "revisions": revisions,
            "challenge": value,
            "binding": binding,
            "step_up": step_up,
            "passkey_only": passkey_only,
        },
    )
    options = None
    if allow:
        options_json, _ = passkeys.authentication_options(
            service.cfg, challenge=value, allow=allow
        )
        options = json.loads(options_json)
    return {
        "challenge_id": challenge_id,
        "options": options,
        "step_up": step_up,
        "totp": bool(totp),
        "expires": expires,
        "revisions": revisions,
    }


async def _signer_ids(request: Request, user: deps.User) -> list:
    """The person's signing passkeys that kd-review holds too: a key it does not know would sign here and be
    refused there."""
    service = deps.svc(request)
    try:
        known = (await service.review.call({"op": "keys", "user": user.id}))["credential_ids"]
        known = set(known)
    except review_client.ReviewUnavailable as exc:
        raise Failure(503, "unavailable", str(exc))
    except (review_client.ReviewRefused, KeyError, TypeError) as exc:
        raise Failure(503, "unavailable", f"kd-review did not list the keys: {exc}")
    return [c for c in service.users.credential_ids(user.id, signing=True) if prs_api.b64url(c) in known]


async def _ended(service, payload: dict, state: str) -> None:
    """kd-review saw the PR merged or closed in Forgejo (not through kd-merge): the board records it, which ends
    its open decision and lets "Accept" through."""
    try:
        await service.call(CABINET, {"op": "pr.state", "repo": payload["repo"], "number": payload["number"],
                                     "state": state})
    except api.ApiError:
        pass  # the answer is 409 stale either way; the next publish records it


async def _live(request: Request, decision: dict) -> None:
    """The PR in Forgejo before the person is asked to sign: ended or moved on there — no prompt."""
    service, payload = deps.svc(request), decision["payload"]
    try:
        live = await service.review.call({"op": "live", **{k: payload[k] for k in ("project", "repo", "number")}})
    except review_client.ReviewUnavailable as exc:
        raise Failure(503, "unavailable", str(exc))
    except review_client.ReviewRefused as exc:
        raise Failure(403, "refused", str(exc))
    if live.get("state") in prs_api.ENDED:
        await _ended(service, payload, live["state"])
        raise Failure(409, "stale", f"{decision['id']}: PR #{payload['number']} is {live['state']} in Forgejo")
    if live.get("head") != payload["head"]:
        raise Failure(409, "stale", f"{decision['id']}: PR #{payload['number']} moved on in Forgejo from "
                                    f"{payload['head'][:7]}; the new head comes with dev's next publish")
    # kd-review would refuse it (a kd-review from before the release answers no target and checks none)
    if payload.get("target") and "target" in live and live["target"] != payload["target"]:
        raise Failure(409, "stale", f"{decision['id']}: PR #{payload['number']} targets {live['target'] or '?'} "
                                    f"in Forgejo, not {payload['target']}; dev's next publish brings the new target")


async def _pr_challenge(request: Request, user: deps.User, decision: dict, mode) -> dict:
    service = deps.svc(request)
    payload = decision["payload"]
    if mode not in pr_signature.MODES:
        raise Failure(400, "invalid", f"mode must be one of {', '.join(pr_signature.MODES)}")
    if mode == "release" and payload.get("release") is not True:
        raise Failure(400, "invalid", "this project is not released from the cabinet")
    if (await service.locked(lambda: _pr(service, decision)))["stale"]:
        raise Failure(409, "stale", f"{decision['id']}: the PR moved on from {payload['head'][:7]}")
    await _live(request, decision)
    allow = await _signer_ids(request, user)
    if not allow:
        raise Failure(
            403,
            "setup_required",
            f"no passkey of yours signs pull requests yet: root runs kd-signers sync --apply (kd-review takes the "
            f"cabinet's signing keys of {user.id}); a server's exec gate takes them only from the owner's machine: "
            f"deploy_target.sh <alias> --replace-signers",
        )
    revision = service.ctx.store.card(decision["card"]).revision
    expires = users.shift(service.now(), seconds=CHALLENGE_SECONDS)
    nonce = secrets.token_hex(16)
    value = pr_signature.challenge(decision["id"], payload, revision, user.id, expires, nonce, mode)
    challenge_id = secrets.token_urlsafe(16)
    revisions = {decision["card"]: revision}
    service.challenges.put(
        "decision",
        challenge_id,
        {
            "ids": [decision["id"]],
            "user": user.id,
            "session": user.session_id,
            "expires": expires,
            "revisions": revisions,
            "challenge": value,
            "binding": {"expires": expires, "nonce": nonce, "mode": mode},
            "step_up": True,
            "pr": {"decision": decision["id"], "payload": payload, "card_revision": revision, "mode": mode},
        },
    )
    options_json, _ = passkeys.authentication_options(service.cfg, challenge=value, allow=allow)
    return {
        "challenge_id": challenge_id,
        "options": json.loads(options_json),
        "step_up": True,
        "totp": False,
        "expires": expires,
        "revisions": revisions,
        "mode": mode,
    }


def _assertion(value) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    if not isinstance(value, dict):
        raise Failure(400, "invalid", "credential must be the passkey assertion")
    return value


async def _totp(request: Request, user: deps.User, held: dict, code: str) -> dict:
    """A TOTP code in place of a passkey; the record of it the grant keeps (never the code)."""
    service = deps.svc(request)
    for decision_id in held["ids"]:
        found = service.decisions.get(decision_id)
        if found is not None and found["totp_failures"] >= TOTP_TRIES:
            raise Failure(
                423, "locked", "too many wrong codes here; sign with a passkey"
            )
    key, now = f"totp|{user.id}", service.now()
    if service.users.too_many(key, now):
        raise Failure(
            429, "rate_limited", "too many wrong codes; use a passkey or wait"
        )
    if not await service.locked(
        lambda: service.users.totp_verify(user.id, code, service.cfg.secret, now)
    ):

        def failed():
            service.users.failed_attempt(key, now)
            for decision_id in held["ids"]:
                service.decisions.totp_failure(decision_id)

        await service.locked(failed)
        raise Failure(403, "stepup_required", "the code was not accepted")
    return {"id": "totp", "type": "totp", "user": user.id}


@router.post("/api/decisions/approve")
async def approve(request: Request, user: deps.User = Depends(deps.writer)):
    service = deps.svc(request)
    body = await deps.json_body(request)
    challenge_id = body.get("challenge_id")
    if not isinstance(challenge_id, str):
        raise Failure(400, "invalid", "challenge_id is required")
    held = service.challenges.pop("decision", challenge_id)
    if held is None or held["expires"] <= service.now():
        raise Failure(410, "expired", "the challenge expired or was used; ask again")
    if (held["user"], held["session"]) != (user.id, user.session_id):
        raise Failure(403, "refused", "the challenge belongs to another session")
    if held.get("pr"):
        return await _pr_approve(request, user, held, body)
    signature = credential = None
    if held["step_up"]:
        if body.get("credential") is not None:
            assertion = _assertion(body["credential"])
            row = await stepup.verify_passkey(
                request, user, assertion, held["challenge"], signing=True
            )
            signature, credential = assertion, row["id"]
        elif held.get("passkey_only"):
            raise Failure(403, "stepup_required", PASSKEY_ONLY)
        elif isinstance(body.get("totp"), str):
            signature = await _totp(request, user, held, body["totp"])
        else:
            raise Failure(403, "stepup_required", "sign with a passkey or a TOTP code")
        # what an auditor needs to recompute the signed value: Decisions.challenge(...) + nonce
        signature = {**signature, "binding": held["binding"]}
    op = {
        "op": "decision.approve",
        "ids": held["ids"],
        "user": user.id,
        "signature": signature,
        "credential": credential,
        "batch_id": secrets.token_hex(16) if len(held["ids"]) > 1 else "",
        "revisions": held["revisions"],
    }
    if body.get("ttl_hours") is not None:
        op["ttl_hours"] = body["ttl_hours"]
    return await service.call(CABINET, op)


async def _pr_approve(request: Request, user: deps.User, held: dict, body: dict) -> dict:
    service = deps.svc(request)
    if body.get("credential") is None:
        raise Failure(403, "stepup_required", "a pull request is signed with a passkey")
    assertion = _assertion(body["credential"])
    row = await stepup.verify_passkey(request, user, assertion, held["challenge"], signing=True)
    pr = held["pr"]
    op = {
        "op": "decision.approve",
        "ids": held["ids"],
        "user": user.id,
        "signature": {**assertion, "binding": held["binding"]},
        "credential": row["id"],
        "batch_id": "",
        "revisions": held["revisions"],
    }
    await service.call(CABINET, {**op, "check": True})
    request_ = {"op": "approve", **pr, "user": user.id, "expires": held["expires"],
                "nonce": held["binding"]["nonce"], "assertion": assertion}
    try:
        review = await service.review.call(request_)
    except review_client.ReviewTimeout as exc:  # it may have posted: pressing again finds its Approve
        raise Failure(503, "review_timeout", str(exc))
    except review_client.ReviewUnavailable as exc:
        raise Failure(503, "unavailable", str(exc))
    except review_client.ReviewRefused as exc:
        if exc.live:
            await _ended(service, pr["payload"], exc.live)
        raise Failure(409, "stale", str(exc)) if exc.kind == "stale" else Failure(403, "refused", str(exc))
    number = review.get("review")
    if isinstance(number, bool) or not isinstance(number, int) or review.get("head") != pr["payload"]["head"]:
        raise Failure(503, "unavailable", "kd-review answered no review on this head")
    try:
        return await service.call(CABINET, {**op, "review": {"id": number, "head": review["head"]}})
    except api.ApiError as exc:
        raise Failure(
            409, "approved_in_forge",
            f"the Approve is already in Forgejo (review {number}); press again to finish the record ({exc})",
        )


# --- the other answers ----------------------------------------------------------------------------
FORM_FIELDS = ("answers", "comment", "form_sha256")  # a reply to a question form


async def _answer(request: Request, user: deps.User, decision_id: str, kind: str):
    body = await deps.json_body(request)
    op = {"op": f"decision.{kind}", "id": decision_id, "text": body.get("text")}
    if kind == "reply" and "answers" in body:
        op = {"op": op["op"], "id": decision_id, **{k: body.get(k) for k in FORM_FIELDS}}
    return (await call(request, user, op))["decision"]


def _comments(value) -> list:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > COMMENTS_MAX:
        raise Failure(400, "invalid", f"comments: a list of at most {COMMENTS_MAX}")
    out = []
    for c in value:
        line = c.get("line") if isinstance(c, dict) else None
        if (
            not isinstance(c, dict)
            or not isinstance(c.get("path"), str)
            or not 0 < len(c["path"]) <= prs_api.PATH_MAX
            or isinstance(line, bool)
            or not isinstance(line, int)
            or line < 1
            or c.get("side") not in SIDES
            or not isinstance(c.get("body"), str)
            or not c["body"].strip()
            or len(c["body"]) > COMMENT_MAX
        ):
            raise Failure(400, "invalid", f"a comment is {{path, line >= 1, side: new|old, body <= {COMMENT_MAX}}}")
        out.append({"path": c["path"], "line": line, "side": c["side"], "body": c["body"]})
    return out


async def _pr_remarks(request: Request, user: deps.User, decision: dict) -> dict:
    """The person's text and line remarks: the board's rejection first (it wakes dev), then a COMMENT review."""
    service = deps.svc(request)
    body = await deps.json_body(request)
    text = body.get("text")
    if text is not None and not isinstance(text, str):
        raise Failure(400, "invalid", "text must be a string")
    text = (text or "").strip()
    comments = _comments(body.get("comments"))
    lines = "\n".join(f"- `{c['path']}:{c['line']}` — {c['body']}" for c in comments)
    recorded = "\n\n".join(part for part in (text, lines) if part)
    if len(recorded) > decisions.TEXT_MAX:
        raise Failure(400, "invalid", f"the remarks are over {decisions.TEXT_MAX} characters")
    out = (await call(request, user, {"op": "decision.reject", "id": decision["id"], "text": recorded}))["decision"]
    payload = decision["payload"]
    try:
        await service.review.call(
            {"op": "comment", **{k: payload[k] for k in ("project", "repo", "number", "head")}, "body": text,
             "comments": comments}
        )
        forge = "posted"
    except review_client.ReviewUnavailable:
        forge = "failed"
    except review_client.ReviewRefused as exc:
        if exc.live:
            await _ended(service, payload, exc.live)
        forge = "failed"
    return {"decision": out, "forge": forge}


@router.post("/api/decisions/{decision_id}/reject")
async def reject(
    decision_id: str, request: Request, user: deps.User = Depends(deps.writer)
):
    found = (await call(request, user, {"op": "decision.get", "id": decision_id}))["decision"]
    if found["type"] == prs_api.TYPE:
        return await _pr_remarks(request, user, found)
    return await _answer(request, user, decision_id, "reject")


@router.post("/api/decisions/{decision_id}/ask")
async def ask(
    decision_id: str, request: Request, user: deps.User = Depends(deps.writer)
):
    return await _answer(request, user, decision_id, "ask")


@router.post("/api/decisions/{decision_id}/reply")
async def reply(
    decision_id: str, request: Request, user: deps.User = Depends(deps.writer)
):
    return await _answer(request, user, decision_id, "reply")


@router.post("/api/decisions/{decision_id}/revoke")
async def revoke(
    decision_id: str, request: Request, user: deps.User = Depends(deps.writer)
):
    op = {"op": "decision.revoke", "id": decision_id}
    return (await call(request, user, op))["decision"]
