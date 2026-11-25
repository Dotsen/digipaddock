"""The projects' deploys over HTTP (KD-9443), for people with ``deploys.read`` (the owner holds every permission):

- ``GET /api/deploys`` — each environment's last run, ``stale`` past two minutes without a good read, and the
  platform's last release;
- ``GET /api/deploys/runs?project&env&before&limit`` — the journal, newest first; ``next`` is the ``before`` of the
  older page;
- ``GET /api/deploys/alerts`` — failed runs that ended a card's ``deploy`` wait, nobody saw yet (the inbox);
  ``POST /api/deploys/alerts/seen {project, run}`` — seen.

The button «Deploy to prod» (``deploy_prod``), the owner's only — a person holding every permission:

- ``POST /api/deploys/launch/challenge {project, env, skip_tests}`` — no typed word (the owner's choice): kd-review
  names the head of the environment's branch (``deploy_head``; the cabinet has no network), and the person gets a
  passkey request over the launch (``kd.deploy_signature``) with ``userVerification: discouraged`` — the one
  signature of the cabinet without a fingerprint or PIN — and the commit it binds;
- ``POST /api/deploys/launch {challenge_id, credential}`` — the cabinet checks the assertion against the person's own
  key (no user verification asked), kd-review checks it again with its own registry, the spent nonce and the branch's
  live head and calls ``workflow_dispatch`` with its own token; the launch — dispatched or refused — is recorded
  (``deploy.launched``). The Forgejo token is kd-review's only: the cabinet never reads it.
"""

import json
import secrets

from fastapi import APIRouter, Depends, Request

from kd import deploy_signature
from kd.cabinet import api, passkeys, review_client, users
from kd.cabinet.deploys import SHA40
from kd.cabinet.web import deps, stepup
from kd.cabinet.web.cards import call
from kd.cabinet.web.decisions import CABINET, _assertion, _signer_ids
from kd.cabinet.web.errors import Failure

router = APIRouter()
CHALLENGE_SECONDS = deploy_signature.TERM_SECONDS  # the brief's ten minutes; kd-review takes no more


@router.get("/api/deploys")
async def deploys(request: Request, user: deps.User = Depends(deps.current_user)):
    found = await call(request, user, {"op": "deploy.list"})
    found.pop("ok", None)
    return found


@router.get("/api/deploys/runs")
async def runs(
    request: Request,
    user: deps.User = Depends(deps.current_user),
    project: str = "",
    env: str = "",
    before: str = "",
    limit: int = 50,
):
    found = await call(
        request,
        user,
        {
            "op": "deploy.runs",
            "project": project,
            "env": env,
            "before": before,
            "limit": limit,
        },
    )
    return {"items": found["items"], "next": found["next"]}


@router.get("/api/deploys/alerts")
async def alerts(request: Request, user: deps.User = Depends(deps.current_user)):
    return {"items": (await call(request, user, {"op": "deploy.alerts"}))["items"]}


@router.post("/api/deploys/alerts/seen")
async def seen(request: Request, user: deps.User = Depends(deps.writer)):
    body = await deps.json_body(request)
    found = await call(request, user, {"op": "deploy.alert.seen", "project": body.get("project"),
                                       "run": body.get("run")})
    return {"seen": found["seen"]}


def _review_failure(exc: Exception) -> Failure:
    if isinstance(exc, review_client.ReviewTimeout):  # it may have dispatched: the page shows the run if it did
        return Failure(503, "review_timeout", str(exc))
    if isinstance(exc, review_client.ReviewUnavailable):
        return Failure(503, "unavailable", str(exc))
    if isinstance(exc, review_client.ReviewRefused) and exc.kind == "stale":
        return Failure(409, "stale", str(exc))
    return Failure(403, "refused", str(exc))


@router.post("/api/deploys/launch/challenge")
async def launch_challenge(request: Request, user: deps.User = Depends(deps.writer)):
    """The passkey request over a prod launch: the environment, the commit at its branch's head now, skip_tests."""
    service = deps.svc(request)
    body = await deps.json_body(request)
    skip_tests = body.get("skip_tests", False)
    if not isinstance(skip_tests, bool):
        raise Failure(400, "invalid", "skip_tests is true or false")
    # the board answers who may: the owner only, a manual environment the reader reports
    env = (await call(request, user, {"op": "deploy.launchable", "project": body.get("project"),
                                      "env": body.get("env")}))["env"]
    if skip_tests and not env["skip_tests"]:
        raise Failure(400, "invalid", f"{env['project']}:{env['env']} has no skip_tests")
    try:
        head = await service.review.call({"op": "deploy_head", "project": env["project"], "repo": env["repo"],
                                          "env": env["env"]})
    except (review_client.ReviewRefused, review_client.ReviewUnavailable) as exc:
        raise _review_failure(exc)
    sha = head.get("sha")
    if not isinstance(sha, str) or not SHA40.fullmatch(sha) or head.get("branch") != env["branch"]:
        raise Failure(503, "unavailable", "kd-review named no head of the branch")
    allow = await _signer_ids(request, user)
    if not allow:
        raise Failure(403, "setup_required",
                      "no passkey of yours signs for kd-review yet: root runs kd-signers sync --apply")
    payload = {"kind": deploy_signature.TYPE, "project": env["project"], "repo": env["repo"], "env": env["env"],
               "workflow": env["workflow"], "branch": env["branch"], "sha": sha, "skip_tests": skip_tests}
    launch, nonce = secrets.token_hex(16), secrets.token_hex(16)
    expires = users.shift(service.now(), seconds=CHALLENGE_SECONDS)
    value = deploy_signature.challenge(launch, payload, user.id, expires, nonce)
    challenge_id = secrets.token_urlsafe(16)
    service.challenges.put("deploy", challenge_id, {"launch": launch, "payload": payload, "user": user.id,
                                                    "session": user.session_id, "expires": expires,
                                                    "nonce": nonce, "challenge": value}, ttl=CHALLENGE_SECONDS)
    options_json, _ = passkeys.authentication_options(service.cfg, challenge=value, allow=allow,
                                                      user_verification=False)
    return {"challenge_id": challenge_id, "options": json.loads(options_json), "expires": expires,
            "launch": {k: payload[k] for k in ("project", "env", "branch", "sha", "skip_tests")}}


@router.post("/api/deploys/launch")
async def launch(request: Request, user: deps.User = Depends(deps.writer)):
    service = deps.svc(request)
    body = await deps.json_body(request)
    challenge_id = body.get("challenge_id")
    if not isinstance(challenge_id, str):
        raise Failure(400, "invalid", "challenge_id is required")
    held = service.challenges.pop("deploy", challenge_id)
    if held is None or held["expires"] <= service.now():
        raise Failure(410, "expired", "the challenge expired or was used; ask again")
    if (held["user"], held["session"]) != (user.id, user.session_id):
        raise Failure(403, "refused", "the challenge belongs to another session")
    payload = held["payload"]
    # the owner still (a role taken away since the challenge starts nothing)
    await call(request, user, {"op": "deploy.launchable", "project": payload["project"], "env": payload["env"]})
    if body.get("credential") is None:
        raise Failure(403, "stepup_required", "a prod deploy is signed with a passkey")
    assertion = _assertion(body["credential"])
    await stepup.verify_passkey(request, user, assertion, held["challenge"], signing=True, user_verification=False)
    record = {"id": held["launch"], "user": user.id, **{k: payload[k] for k in ("project", "env", "sha", "skip_tests")}}
    try:
        answer = await service.review.call({"op": "deploy", "launch": held["launch"], "payload": payload,
                                            "user": user.id, "expires": held["expires"], "nonce": held["nonce"],
                                            "assertion": assertion})
    except (review_client.ReviewRefused, review_client.ReviewUnavailable) as exc:
        failure = _review_failure(exc)
        outcome = exc.kind if isinstance(exc, review_client.ReviewRefused) else failure.kind
        await _record(service, {**record, "outcome": outcome, "message": str(exc)[:500]})
        raise failure
    await _record(service, {**record, "outcome": "dispatched", "message": ""})
    return {"state": answer.get("state", "dispatched"), "sha": payload["sha"], "project": payload["project"],
            "env": payload["env"], "skip_tests": payload["skip_tests"]}


async def _record(service, launch: dict) -> None:
    try:
        await service.call(CABINET, {"op": "deploy.launched", "launch": launch})
    except api.ApiError:
        pass  # kd-review's journal keeps the launch all the same
