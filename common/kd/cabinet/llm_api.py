"""The cabinet's models of other providers: what a card's sessions may run on besides the subscription.

A model is a row of ``llm_models`` (``kd.cabinet.store``): a slug the owner chose, a provider kind of the catalog
``common/coordination/llm-providers.json`` (``kd.llm_models``), the model id the provider knows, prices per million
tokens and a context size. The key of a kind is root's file ``/etc/digipaddock/llm/<kind>.env`` (``kd-secret put
llm-<kind>``): the cabinet reports whether it exists (``key``), never its content.

``llm.list`` — every reader; a person without ``settings`` sees ``READ_FIELDS`` only (the card's chip needs the
names). ``llm.create|update|delete`` — ``settings`` (root and the cabinet's CLI too); the id and the kind never change
(the usage ledger counts under the id); a model a card or a project names is not deleted (``model_referenced``) —
disable it instead. ``llm.probe.request`` (``settings``) marks the model for the host's probe; the dispatcher sends
it (``kd.cabinet.dispatcher``) and the host reports its outcome as a ``probe`` item of ``host.report``
(``report_item``); ``llm.probe.result`` is root's own word on a model.

the models of the Claude subscription (``kd.llm_models.claude_models``: ``claude:opus`` and its kin) are
listed first, ``builtin``: always enabled, never edited or deleted, without prices — their usage is counted under the
names Claude Code reports. They are no rows of the table: ``rows`` and ``row`` add them, and the probe of one is kept
in the cabinet's ``state`` (``model_probe:<ref>``).

``effective(store, card)`` is the pair a session of the card runs on: the card's model, else the project's, else
``None`` (the subscription); the card's effort, else the project's, else ``""`` (the agent's own). A disabled model
is returned as it is — the caller (the dispatcher) decides that a disabled model refuses the command.
"""

import os
import re
from typing import List, Mapping, Optional, Tuple

from kd import claude_token, llm_models, model_credentials
from kd.cabinet import api, store, usage

SUBSCRIPTION_PROBE = "@claude-subscription"  # cannot collide with a cabinet model slug
READ_FIELDS = ("id", "title", "provider", "enabled", "key", "experimental", "builtin", "version")
PROBE_STATE = "model_probe:"  # + the reference: a subscription model's probe, in the cabinet's state
CREATE_KEYS = frozenset({"op"} | store.Store._LLM_CREATE)
UPDATE_KEYS = frozenset({"op", "id"} | (store.Store._LLM_UPDATE - {"probe"}))  # the probe is the host's
MANAGERS = frozenset({"user", "root", "cabinet"})
CONTEXT_MIN, CONTEXT_MAX = 1000, 10_000_000
PRICE_MAX = 10_000
DETAIL_MAX = 300
PROBE_ID = re.compile(r"[0-9a-f]{32}")
NO_KEY = "no key of the {provider} provider on the host"  # a probe's detail; the thread's line is the dispatcher's
PROBE_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9,._:/\[\]-]{0,127}")  # the names a result's usage lists, by commas


def env_dir(ctx) -> str:
    """Where the host keeps the kinds' key files (a test's context points elsewhere)."""
    return getattr(ctx, "llm_env_dir", None) or llm_models.ENV_DIR


def key_present(ctx, provider: str) -> bool:
    try:
        return llm_models.key_present(provider, env_dir=env_dir(ctx))
    except ValueError:  # a kind the catalog no longer has: no key for it either
        return False


def claude_key(ctx) -> bool:
    """Whether the host's Claude token file is in place — the «key» of every subscription model (its content is
    root's)."""
    return os.path.isfile(getattr(ctx, "claude_env", None) or claude_token.PATH)


def _builtin(db: store.Store, entry: Mapping) -> dict:
    """A subscription model in a table row's shape: no prices, no context, always enabled."""
    return {"id": entry["id"], "provider": llm_models.SUBSCRIPTION, "base_url": "", "upstream": entry["upstream"],
            "title": entry["title"], "price_in": 0.0, "price_out": 0.0, "price_cache_read": 0.0,
            "price_cache_write": 0.0, "context": 0, "enabled": 1,
            "probe": db.get_state(PROBE_STATE + entry["id"]) or {}, "created": "", "updated": "", "builtin": True,
            "version": usage.version(db, entry["id"])}  # the model its alias served last


def _own(found: Mapping) -> dict:
    """A row of the table as a model: not a builtin; its ``upstream`` is its version, so ``version`` is empty."""
    return {**found, "builtin": False, "version": ""}


def rows(db: store.Store) -> List[dict]:
    """Every model a card, a project or a council may name: the subscription's first, then the cabinet's own."""
    return ([_builtin(db, entry) for entry in llm_models.claude_models().values()]
            + [_own(found) for found in db.llm_models()])


def row(db: store.Store, ref) -> Optional[dict]:
    """The model ``ref`` names — a subscription model of the catalog or a row of the table — or None."""
    if llm_models.is_ref(ref):
        entry = llm_models.claude_models().get(ref)
        return None if entry is None else _builtin(db, entry)
    if not isinstance(ref, str) or not llm_models.SLUG.fullmatch(ref):
        return None
    found = db.llm_model(ref)
    return None if found is None else _own(found)


def set_probe(db: store.Store, model_id: str, probe: Mapping, *, actor: str) -> dict:
    """The model's probe as it now stands (a subscription model's lives in the state); the model's row."""
    if llm_models.is_ref(model_id):
        with db.transaction():  # no row to change: the state and the event a row's change would be
            before = db.get_state(PROBE_STATE + model_id) or {}
            db.set_state(PROBE_STATE + model_id, dict(probe))
            db._event(actor, "llm_model.changed", "", {"model": model_id, "probe": {"from": before, "to": dict(probe)}})
            if probe.get("ok") is True and probe.get("model"):  # what answered is the alias's version
                usage.saw(db, model_id, str(probe["model"]).split(","), str(probe.get("at") or db._clock()))
        return row(db, model_id)
    return _own(db.update_llm_model(model_id, {"probe": probe}, actor=actor))


def missing_key(ctx, row: Mapping) -> bool:
    """Whether a session on the model cannot start for want of its kind's key on the host. A subscription model
    never says so: it starts as a session without a model does, on the host's token or the user's stored login."""
    return not row.get("builtin") and not key_present(ctx, row["provider"])


def view(ctx, row: Mapping, full: bool = True) -> dict:
    """A model as the socket gives it: the row with ``key`` (the kind's key file exists; the host's Claude token for
    a subscription model), ``experimental`` (not a Claude: Anthropic does not support it through Claude Code) and
    ``builtin``, and ``version`` (the model a subscription model's alias served last, ``""`` for the rest);
    ``READ_FIELDS`` only without ``full``."""
    builtin = bool(row.get("builtin"))
    out = {**row, "builtin": builtin, "version": row.get("version", ""),
           "key": claude_key(ctx) if builtin else key_present(ctx, row["provider"]),
           "experimental": not builtin and llm_models.experimental(row["upstream"])}
    return out if full else {k: out[k] for k in READ_FIELDS}


def spec_of(row: Mapping) -> dict:
    """``{id, provider, upstream, base_url}`` — what a runtime needs of a model (``kd.llm_models.environment``)."""
    return {k: row[k] for k in llm_models.SPEC_FIELDS}


def effective(db: store.Store, card: store.CardRecord) -> Tuple[Optional[dict], str]:
    """``(model row or None, effort)`` of the card: the card's, else its project's, else the subscription and ``""``."""
    project = db.project(card.project) or {}
    model = card.model or project.get("model", "")
    effort = card.effort or project.get("effort", "")
    return (row(db, model) if model else None), effort


def check_model(db: store.Store, value) -> str:
    """``""`` (the agent's own model on the subscription), a subscription model's reference or the slug of an
    enabled model; ApiError otherwise."""
    if value == "":
        return ""
    if not isinstance(value, str) or not (llm_models.SLUG.fullmatch(value) or llm_models.is_ref(value)):
        raise api.ApiError("invalid", "model is a cabinet model's id, claude:<alias> or empty")
    found = row(db, value)
    if found is None:
        raise api.ApiError("invalid", f"unknown model {value!r}")
    if not found["enabled"]:
        raise api.ApiError("invalid", f"model {value} is disabled")
    return value


def check_effort(value) -> str:
    """``""`` (the project's, else the agent's) or an effort level; ApiError otherwise."""
    try:
        return llm_models.check_effort(value)
    except ValueError as exc:
        raise api.ApiError("invalid", str(exc)) from None


def _manager(ctx, principal):
    api._require(ctx, principal, "settings")


def _key_changed(ctx, principal, request):
    """Root's secret-free audit after the signed filesystem effect; never accepts arbitrary payloads."""
    if (set(request) != {"op", "user", "provider", "action"}
            or not isinstance(request.get("user"), str)
            or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", request["user"])
            or not isinstance(request.get("provider"), str)
            or not model_credentials.known(request["provider"])
            or request.get("action") not in ("put", "delete")):
        raise api.ApiError("invalid", "invalid provider-key audit")
    with ctx.store.transaction():
        ctx.store._event("user:" + request["user"], "llm_key.changed", "",
                         {"provider": request["provider"], "action": request["action"]})
    return {"ok": True}


def _keys(request, allowed):
    extra = set(request) - allowed
    if "new_id" in extra:
        raise api.ApiError("invalid", "a model's id never changes")
    if extra:
        raise api.ApiError("invalid", f"unknown field: {', '.join(sorted(extra))}")


def _existing(ctx, model_id, *, own: bool = False) -> dict:
    """The model the request names; with ``own`` only a row of the table — a subscription model is not the owner's
    to change (``model_builtin``)."""
    if not isinstance(model_id, str) or not (llm_models.SLUG.fullmatch(model_id) or llm_models.is_ref(model_id)):
        raise api.ApiError("invalid", "invalid model id")
    found = row(ctx.store, model_id)
    if found is None:
        raise api.ApiError("not_found", f"no model {model_id}")
    if own and found["builtin"]:
        raise api.ApiError("refused", f"{model_id} is a model of the Claude subscription: it is not edited or deleted",
                           reason="model_builtin")
    return found


def _values(request, keys) -> dict:
    """The store's values among ``request``, with the API's own ranges (the store holds the wider ones)."""
    values = {k: request[k] for k in keys if k in request}
    if "context" in values:
        context = values["context"]
        if isinstance(context, bool) or not isinstance(context, int) or not CONTEXT_MIN <= context <= CONTEXT_MAX:
            raise api.ApiError("invalid", f"context is {CONTEXT_MIN} to {CONTEXT_MAX} tokens")
    for key in store.Store._LLM_PRICES:
        price = values.get(key)
        if price is not None and (isinstance(price, bool) or not isinstance(price, (int, float))
                                  or not 0 <= price <= PRICE_MAX):
            raise api.ApiError("invalid", f"{key} is USD per million tokens, 0 to {PRICE_MAX}")
    return values


def _list(ctx, principal, request):
    full = principal.kind != "user" or api.allowed(ctx, principal, "settings")
    return {"models": [view(ctx, found, full) for found in rows(ctx.store)]}


def _create(ctx, principal, request):
    _manager(ctx, principal)
    _keys(request, CREATE_KEYS)
    missing = sorted(CREATE_KEYS - {"op"} - set(request))
    if missing:
        raise api.ApiError("invalid", f"missing field: {', '.join(missing)}")
    values = _values(request, store.Store._LLM_CREATE)
    try:
        row = ctx.store.create_llm_model(values, actor=api.card_actor(principal))
    except store.Invalid as exc:
        raise api.ApiError("invalid", str(exc)) from None
    except store.Conflict:
        raise api.ApiError("invalid", f"model {request['id']} exists") from None
    return {"model": view(ctx, row)}


def _update(ctx, principal, request):
    _manager(ctx, principal)
    _keys(request, UPDATE_KEYS)
    found = _existing(ctx, request.get("id"), own=True)
    changes = _values(request, store.Store._LLM_UPDATE - {"probe"})
    if not changes:
        raise api.ApiError("invalid", "nothing to change: " + ", ".join(sorted(UPDATE_KEYS - {"op", "id"})))
    try:
        row = ctx.store.update_llm_model(found["id"], changes, actor=api.card_actor(principal))
    except store.Invalid as exc:
        raise api.ApiError("invalid", str(exc)) from None
    return {"model": view(ctx, row)}


def _delete(ctx, principal, request):
    _manager(ctx, principal)
    _keys(request, frozenset({"op", "id"}))
    found = _existing(ctx, request.get("id"), own=True)
    refs = ctx.store.llm_model_refs(found["id"])
    if refs["cards"] or refs["projects"]:
        named = ", ".join(refs["projects"] + refs["cards"][:5])
        raise api.ApiError("refused", f"model {found['id']} is set on {named}: disable it instead",
                           reason="model_referenced")
    try:
        ctx.store.delete_llm_model(found["id"], actor=api.card_actor(principal))
    except store.Invalid as exc:  # a reference written between the look and the delete
        raise api.ApiError("refused", str(exc), reason="model_referenced") from None
    return {"deleted": found["id"]}


def _probe_request(ctx, principal, request):
    """The owner asks the host to try the model: the row says so (``{state: requested, at}``) until the dispatcher
    sends it (``{state: running, at, id}``) and the host's result lands (``{at, ok, model, ms, detail}``)."""
    _manager(ctx, principal)
    _keys(request, frozenset({"op", "id"}))
    found = _existing(ctx, request.get("id"))
    probe = {"state": "requested", "at": ctx.now()}
    return {"model": view(ctx, set_probe(ctx.store, found["id"], probe, actor=api.card_actor(principal)))}


def _result_fields(request) -> tuple:
    """``(ok, model, ms, detail)`` of a probe's result as the host or root gives it."""
    ok, detail = request.get("ok"), request.get("detail", "")
    model, ms = request.get("model", ""), request.get("ms", 0)
    if not isinstance(ok, bool) or not isinstance(detail, str):
        raise api.ApiError("invalid", "ok is a flag, detail a text")
    if model != "" and not (isinstance(model, str) and PROBE_MODEL.fullmatch(model)):
        raise api.ApiError("invalid", "model is the name the provider answered as, or empty")
    if isinstance(ms, bool) or not isinstance(ms, int) or ms < 0:
        raise api.ApiError("invalid", "ms is a whole number of milliseconds")
    return ok, model, ms, detail[:DETAIL_MAX]


def probe_done(ctx, model_id: str, ok: bool, model: str, ms: int, detail: str, *, actor: str) -> dict:
    """The probe's result replaces whatever state the model's probe was in."""
    probe = {"at": ctx.now(), "ok": ok, "model": model, "ms": ms, "detail": detail[:DETAIL_MAX]}
    if model_id == SUBSCRIPTION_PROBE:
        with ctx.store.transaction():
            ctx.store.set_state("subscription_probe", probe)
            ctx.store._event(actor, "llm_key.probe", "", {"provider": model_id, **probe})
        return probe
    return set_probe(ctx.store, model_id, probe, actor=actor)


def probe_waiting(db: store.Store, probe_id: str):
    """The model whose probe the dispatcher sent under ``probe_id`` (requested with the id, or running), or None."""
    for found in rows(db):
        probe = found["probe"]
        if probe.get("id") == probe_id and probe.get("state") in ("requested", "running"):
            return found
    return None


def _probe_result(ctx, principal, request):
    """What the host found, by model id (root's own word; the host reports by probe id — ``report_item``)."""
    _keys(request, frozenset({"op", "id", "ok", "model", "ms", "detail"}))
    found = _existing(ctx, request.get("id"))
    done = probe_done(ctx, found["id"], *_result_fields(request), actor=api.card_actor(principal))
    return {"model": view(ctx, done)}


def report_item(ctx, item) -> None:
    """The host's ``{type: probe, probe, ok, model, ms, detail}``: the result of the model waiting for that probe;
    a probe no model waits for (the cabinet forgot it: a restart, a timeout) is nothing to record."""
    probe_id = item.get("probe")
    if not isinstance(probe_id, str) or not PROBE_ID.fullmatch(probe_id):
        raise api.ApiError("invalid", "probe is a 32-hex id")
    fields = _result_fields(item)
    subscription = ctx.store.get_state("subscription_probe") or {}
    if subscription.get("id") == probe_id and subscription.get("state") in ("requested", "running"):
        probe_done(ctx, SUBSCRIPTION_PROBE, *fields, actor="system")
        return
    waiting = probe_waiting(ctx.store, probe_id)
    if waiting is not None:
        probe_done(ctx, waiting["id"], *fields, actor="system")


OPS = {
    "llm.key.changed": (frozenset({"root"}), _key_changed),
    "llm.list": (api.READERS, _list),
    "llm.create": (MANAGERS, _create),
    "llm.update": (MANAGERS, _update),
    "llm.delete": (MANAGERS, _delete),
    "llm.probe.request": (MANAGERS, _probe_request),
    "llm.probe.result": (api.ROOTISH, _probe_result),
}
api.OPS.update(OPS)  # the board API serves them (api imports this module at its end)
