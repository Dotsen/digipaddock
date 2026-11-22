"""The models of other providers over HTTP: the Settings → "Models" section and the card's chips.

``GET /api/models`` — every signed-in person (a partner sees the names only, ``llm_api.READ_FIELDS``);
``GET /api/models/providers`` — the catalog's kinds without their secret names, with whether the host holds each
kind's key; ``POST``, ``PATCH``, ``DELETE /api/models[/{id}]`` and ``POST /api/models/{id}/probe`` — ``settings``.
A model a card or a project names answers 409 ``model_referenced`` to a delete: disable it instead.
"""

from fastapi import APIRouter, Depends, Request

from kd import llm_models, model_credentials, model_keys
from kd.cabinet import api, llm_api
from kd.cabinet.web import deps
from kd.cabinet.web.cards import _settings, call
from kd.cabinet.web.errors import Failure

router = APIRouter()
UPDATE_FIELDS = ("title", "upstream", "base_url", "price_in", "price_out", "price_cache_read", "price_cache_write",
                 "context", "enabled")
CREATE_FIELDS = ("id", "provider") + UPDATE_FIELDS[:-1]


def _key_owner(request, user):
    _settings(request, user)
    if ("owner", "*") not in deps.svc(request).users.assignments(user.id):
        raise Failure(403, "refused", "only an owner changes provider keys")


async def _key_call(request, user, provider, op):
    try:
        return await deps.svc(request).model_keys.call({**op, "user": user.id, "provider": provider})
    except model_keys.Refused as exc:
        status = 503 if exc.kind == "unavailable" else 400 if exc.kind == "invalid" else 403
        raise Failure(status, exc.kind, str(exc)) from None


@router.post("/api/models/providers/{provider}/key/challenge")
async def key_challenge(provider: str, request: Request, user: deps.User = Depends(deps.writer)):
    _key_owner(request, user)
    body = await deps.json_body(request)
    if set(body) - {"action", "digest"}:
        raise Failure(400, "invalid", "only action and digest are accepted")
    return await _key_call(request, user, provider, {"op": "challenge", **body})


@router.post("/api/models/providers/{provider}/key")
async def key_apply(provider: str, request: Request, user: deps.User = Depends(deps.writer)):
    _key_owner(request, user)
    if len(await request.body()) > model_keys.LINE_MAX:
        raise Failure(400, "invalid", "request too large")
    body = await deps.json_body(request)
    if set(body) - {"action", "challenge_id", "credential", "key"}:
        raise Failure(400, "invalid", "unknown field")
    if not isinstance(body.get("credential"), dict) or not isinstance(body.get("challenge_id"), str):
        raise Failure(403, "stepup_required", "an owner passkey signature is required")
    # Never dispatch this body through the board, decisions, transcripts or the journal.
    return await _key_call(request, user, provider, {"op": "apply", **body})


@router.get("/api/model-credentials/subscription")
async def subscription(request: Request, user: deps.User = Depends(deps.current_user)):
    _key_owner(request, user)
    result = await _key_call(request, user, model_credentials.SUBSCRIPTION, {"op": "status"})
    return {**result["subscription"], "probe": deps.svc(request).ctx.store.get_state("subscription_probe") or {}}


@router.post("/api/model-credentials/subscription/probe")
async def subscription_probe(request: Request, user: deps.User = Depends(deps.writer)):
    _key_owner(request, user)
    service = deps.svc(request)

    def save():
        held = service.ctx.store.get_state("subscription_probe") or {}
        if held.get("state") in ("requested", "running"):
            return held
        probe = {"state": "requested", "at": service.now()}
        with service.ctx.store.transaction():
            service.ctx.store.set_state("subscription_probe", probe)
        return probe

    return await service.locked(save)


@router.get("/api/models")
async def models(request: Request, user: deps.User = Depends(deps.current_user)):
    return (await call(request, user, {"op": "llm.list"}))["models"]


@router.get("/api/models/providers")
async def providers(request: Request, user: deps.User = Depends(deps.current_user)):
    """The kinds a model can be of: ``{id, title, base_url, hint, caching, key}`` — never the secret's name."""
    env_dir = llm_api.env_dir(deps.svc(request).ctx)
    return [
        {"id": kind, "title": entry["title"], "base_url": entry["base_url"], "hint": entry["hint"],
         "caching": entry["caching"], "key": llm_models.key_present(kind, env_dir=env_dir),
         "manage_key": deps.svc(request).users.can(user.id, "settings")
                       and ("owner", "*") in deps.svc(request).users.assignments(user.id),
         "key_type": "anthropic_api" if kind == "anthropic" else "openai_api" if kind == "openai" else "provider_api"}
        for kind, entry in llm_models.providers().items()
    ]


@router.post("/api/models")
async def create(request: Request, user: deps.User = Depends(deps.writer)):
    body = await deps.json_body(request)
    _settings(request, user)
    op = {"op": "llm.create", **{k: body[k] for k in CREATE_FIELDS if k in body}}
    return (await call(request, user, op))["model"]


@router.patch("/api/models/{model_id}")
async def update(model_id: str, request: Request, user: deps.User = Depends(deps.writer)):
    """Any of ``UPDATE_FIELDS``; the id and the kind never change (an unknown field is refused by the board)."""
    body = await deps.json_body(request)
    _settings(request, user)
    op = {"op": "llm.update", "id": model_id, **{k: v for k, v in body.items() if k != "id"}}
    return (await call(request, user, op))["model"]


@router.delete("/api/models/{model_id}")
async def delete(model_id: str, request: Request, user: deps.User = Depends(deps.writer)):
    _settings(request, user)
    try:
        return await call(request, user, {"op": "llm.delete", "id": model_id})
    except api.ApiError as exc:
        if exc.reason == "model_referenced":  # a conflict with the board's state, not a missing permission
            raise api.ApiError("conflict", str(exc), reason=exc.reason) from None
        raise


@router.post("/api/models/{model_id}/probe")
async def probe(model_id: str, request: Request, user: deps.User = Depends(deps.writer)):
    """Ask the host to try the model; the row shows ``probe.state = requested`` until the result lands."""
    _settings(request, user)
    return (await call(request, user, {"op": "llm.probe.request", "id": model_id}))["model"]
