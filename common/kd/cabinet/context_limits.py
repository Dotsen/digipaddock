"""The owner's session context limit of each agent role: where Claude Code compacts a role's conversations.

One value per role of the agent registry, kept in the cabinet's ``state`` under ``context_limit:<agent>``: a whole
number of tokens (``kd.context_limit.MIN`` to ``MAX``) or ``"none"`` — no limit, the model's own window. A role the
owner never set (a new one of the registry included) is on ``kd.context_limit.DEFAULT``. Each change is one
transaction with its ``context_limit.changed`` event, the old and the new value.

``context.list`` and ``context.set`` — ``settings`` (root and the cabinet's CLI too), as the models are. The
dispatcher puts the value in force into every delivery of the role (``context_limit``) and into the definition of
each of its worker jobs as it sends them (``effective``), so a change applies from the role's next process.
"""

from typing import List

from kd import agents, context_limit
from kd.cabinet import api, store

STATE = "context_limit:"  # + the agent
NONE = "none"  # the owner's explicit choice of no limit
MANAGERS = frozenset({"user", "root", "cabinet"})
RANGE = "context_limit_range"  # the reason of a refused value: the page says it in the owner's words


def _stored(db: store.Store, agent: str):
    """The role's value as the owner set it — tokens or ``NONE`` — else the default."""
    value = db.get_state(STATE + agent)
    if value == NONE:
        return NONE
    try:
        return context_limit.check(value)
    except ValueError:  # never set (or not a value this module wrote): the default
        return context_limit.DEFAULT


def effective(db: store.Store, agent: str) -> int:
    """The limit a launch of ``agent`` takes now, in tokens; 0 — no limit (nothing is sent)."""
    value = _stored(db, agent)
    return 0 if value == NONE else value


def _row(db: store.Store, agent: str) -> dict:
    return {
        "agent": agent,
        "limit": _stored(db, agent),
        "set": db.get_state(STATE + agent) is not None,
    }


def _names(ctx) -> List[str]:
    try:
        return agents.names(ctx.registry_path)
    except (OSError, ValueError) as exc:
        raise api.ApiError(
            "unavailable", f"agent registry unreadable ({exc.__class__.__name__})"
        ) from None


def _value(value):
    """``NONE`` or a limit in range; ApiError ``context_limit_range`` for anything else (a text, a fraction)."""
    if value == NONE:
        return NONE
    try:
        return context_limit.check(value)
    except ValueError as exc:
        raise api.ApiError("invalid", f"{exc}, or {NONE!r}", reason=RANGE) from None


def _list(ctx, principal, request):
    api._require(ctx, principal, "settings")
    if set(request) != {"op"}:
        raise api.ApiError("invalid", "context.list takes no fields")
    return {"limits": [_row(ctx.store, agent) for agent in _names(ctx)]}


def _set(ctx, principal, request):
    api._require(ctx, principal, "settings")
    if set(request) != {"op", "agent", "limit"}:
        raise api.ApiError("invalid", "context.set needs agent and limit")
    agent = request["agent"]
    if not isinstance(agent, str) or not agents.NAME_RE.fullmatch(agent):
        raise api.ApiError("invalid", "invalid agent name")
    if agent not in _names(ctx):
        raise api.ApiError("not_found", f"{agent}: no such agent")
    value = _value(request["limit"])
    db = ctx.store
    with db.transaction():
        before = _stored(db, agent)
        if before != value or db.get_state(STATE + agent) is None:
            db._put_state(STATE + agent, value)
        if before != value:
            db._event(
                api.card_actor(principal),
                "context_limit.changed",
                "",
                {"agent": agent, "limit": {"from": before, "to": value}},
            )
    return {"limit": _row(db, agent)}


OPS = {
    "context.list": (MANAGERS, _list),
    "context.set": (MANAGERS, _set),
}
api.OPS.update(OPS)  # the board API serves them (api imports this module at its end)
