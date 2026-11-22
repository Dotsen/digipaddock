"""Token usage of the agents: per turn of a hosted session and per worker job, in the cabinet's
``usage_log``.

Claude reports a resumed session's model usage cumulatively (``modelUsage`` on every ``result``), so a turn's
own usage is the difference from the session's last total (``sessions.usage_total``). A total that is smaller in
any field, or lacks a model the last one had, belongs to a new chain (a fresh Claude session behind the same
cabinet session) and counts whole. A report that never reached the cabinet is counted by the next one: its total
contains it. A job is one fresh Claude session: its total is its usage.

A session or a job on a cabinet model reports its usage under the model's slug, and what the process
reported as cost is not money (Claude Code prices what it believes the model is): with ``prices`` — the model's row
of ``llm_models`` — each model of the turn's share or the job's total is costed by that table.
"""

import math
import re
from typing import Mapping, Optional

from kd import llm_models
from kd.cabinet import users

FIELDS = ("in", "out", "cache_read", "cache_write", "cost")
_COLUMNS = (("in", "input"), ("out", "output"), ("cache_read", "cache_read"), ("cache_write", "cache_write"),
            ("cost", "cost"))
MODEL = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
MODELS_MAX = 8
VERSION_STATE = "model_version:"  # + a subscription model's reference: the name its alias served last
COUNT_MAX = 10 ** 15
COST_MAX = 10 ** 6


def _count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= COUNT_MAX


def check_model(data) -> dict:
    """One model's usage: the five fields, token counts ≥ 0, a finite cost ≥ 0."""
    if not isinstance(data, Mapping) or set(data) != set(FIELDS):
        raise ValueError(f"a model's usage is {{{', '.join(FIELDS)}}}")
    cost = data["cost"]
    if not all(_count(data[f]) for f in FIELDS[:-1]) or isinstance(cost, bool) or not isinstance(cost, (int, float)) \
            or not math.isfinite(cost) or not 0 <= cost <= COST_MAX:
        raise ValueError("usage counts and cost are numbers ≥ 0")
    return {f: data[f] for f in FIELDS}


def check(value) -> dict:
    """A reported usage ``{"models": {<model>: {in, out, cache_read, cache_write, cost}}, "requests": n}``."""
    if not isinstance(value, Mapping) or set(value) != {"models", "requests"}:
        raise ValueError("usage is {models, requests}")
    models, requests = value["models"], value["requests"]
    if not _count(requests):
        raise ValueError("requests is a count ≥ 0")
    if not isinstance(models, Mapping) or len(models) > MODELS_MAX:
        raise ValueError(f"models is an object of up to {MODELS_MAX} models")
    for name in models:
        if not isinstance(name, str) or not MODEL.fullmatch(name):
            raise ValueError(f"invalid model name {name!r}")
    return {"models": {name: check_model(data) for name, data in models.items()}, "requests": requests}


def _minus(a, b, field):
    return round(a - b, 9) if field == "cost" else a - b


def delta(previous: Mapping, total: Mapping) -> dict:
    """``total − previous`` per model and field; a total of a new chain counts whole. A chain that goes on under
    other names (a process resumed on another model reports the whole history under the new name, in
    either direction) counts only what the new names added: what the names gone from ``total`` had is what it
    carried over, taken off the new names in order, never below zero (a fresh process on a new name — the history
    dropped — loses at most its first turn to that)."""
    gone = [m for m in previous if m not in total]
    if not gone:
        if any(total[m][f] < previous[m][f] for m in previous for f in FIELDS):
            return {m: dict(v) for m, v in total.items()}
        return {m: {f: _minus(v[f], previous.get(m, {}).get(f, 0), f) for f in FIELDS} for m, v in total.items()}
    carried = {f: sum(previous[m][f] for m in gone) for f in FIELDS}
    out = {}
    for m in sorted(total):
        if m in previous:
            out[m] = {f: max(_minus(total[m][f], previous[m][f], f), 0) for f in FIELDS}
            continue
        out[m] = {}
        for f in FIELDS:
            taken = min(total[m][f], carried[f])
            carried[f] = _minus(carried[f], taken, f)
            out[m][f] = _minus(total[m][f], taken, f)
    return out


def priced(models: Mapping, prices: Mapping) -> dict:
    """A copy of ``models`` with each model's cost from ``prices`` (a cabinet model's row: ``price_in``,
    ``price_out``, ``price_cache_read``, ``price_cache_write`` per million tokens)."""
    return {m: {**v, "cost": llm_models.cost(prices, v)} for m, v in models.items()}


def version(store, ref) -> str:
    """The model a subscription model's alias served last (``claude-opus-5-5``); ``""`` while none was seen."""
    found = store.get_state(VERSION_STATE + ref) if llm_models.is_ref(ref) else None
    return str(found.get("model", "")) if isinstance(found, Mapping) else ""


def saw(store, ref, names, at: str) -> None:
    """A run on the subscription model ``ref`` reported ``names`` (the costliest first): the one its alias names is
    the version the alias serves now. Kept in the cabinet's state since it was first seen, and its change is a
    model event — the pages read their list again. Inside the caller's transaction. Nothing for a model of another
    kind, a reference gone from the catalog, or a run that reported only other models; a catalog that cannot be
    read never takes the usage record with it."""
    if not llm_models.is_ref(ref):
        return
    try:
        entry = llm_models.claude_models().get(ref)
    except ValueError:
        return
    name = llm_models.served(entry["upstream"], names) if entry else ""
    before = version(store, ref)
    if not name or name == before:
        return
    store._put_state(VERSION_STATE + ref, {"model": name, "at": at})
    store._event("system", "llm_model.changed", "", {"model": ref, "version": {"from": before, "to": name}})


def _names(models: Mapping) -> list:
    """The models with any usage, the costliest first (then by output, then by name)."""
    used = {m: v for m, v in models.items() if any(v[f] for f in FIELDS)}
    return sorted(used, key=lambda m: (-used[m]["cost"], -used[m]["out"], m))


def _insert(store, *, kind, source, agent, card, models: Mapping, requests: int, at: str, runtime: str = "",
            command: str = "") -> int:
    used = {m: v for m, v in models.items() if any(v[f] for f in FIELDS)}
    if not used:
        return 0
    top = max(sorted(used), key=lambda m: used[m]["cost"])  # the turn's API calls, on its costliest model
    for model in sorted(used):
        values = used[model]
        store._conn.execute(
            "INSERT INTO usage_log (at, agent, kind, source, card, model, requests, input, output, cache_read,"
            " cache_write, cost, runtime, command) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (at, agent, kind, source, card, model, requests if model == top else 0,
             *(values[f] for f, _ in _COLUMNS), runtime, command),
        )
    return len(used)


def record_turn(store, session: Mapping, command: Mapping, usage: Mapping, at: str, *,
                prices: Optional[Mapping] = None) -> int:
    """The turn's rows (its share of the session's new total) and the session's totals; the number of rows.
    A session from before the ledger (``usage_total`` null since migration 7) resumes with its whole history,
    which kd-usage counted from files: that first total is only the baseline. With ``prices`` the share is costed
    by them (the raw total stays the baseline of the next turn)."""
    total = usage["models"]
    if session.get("usage_total") is None:
        store.session_update(session["id"], usage_total=dict(total))
        return 0
    turn = delta(session["usage_total"], total)
    if prices is not None:
        turn = priced(turn, prices)
    with store.transaction():
        rows = _insert(store, kind="turn", source=session["id"], agent=session["agent"], card=command["card"],
                       models=turn, requests=usage["requests"], at=at,
                       runtime=session.get("runtime_session_id") or "", command=command["id"])
        saw(store, session.get("model") or "", _names(turn), at)
        summed = dict(session.get("usage") or {})
        for values in turn.values():
            for f in FIELDS:
                summed[f] = round(summed.get(f, 0) + values[f], 6) if f == "cost" else summed.get(f, 0) + values[f]
        store.session_update(session["id"], usage_total=dict(total), usage=summed)
    return rows


def record_job(store, job: Mapping, usage: Mapping, at: str, *, prices: Optional[Mapping] = None,
               model: str = "") -> int:
    """The job's rows; ``model`` — the model it ran on, when it names one (a subscription model's
    reference keeps the version it served)."""
    models = usage["models"] if prices is None else priced(usage["models"], prices)
    with store.transaction():
        rows = _insert(store, kind="job", source=job["id"], agent=job["agent"], card=job.get("card", ""),
                       models=models, requests=usage["requests"], at=at)
        saw(store, model, _names(usage["models"]), at)
        return rows


def day(store, start: str, end: str) -> dict:
    """Usage stamped in ``[start, end)`` per agent and model, with how many sessions and jobs it came from; per
    agent, its sessions and jobs counted once across models (``agents``) and the Claude sessions whose turns the
    ledger has from ``start`` on (``counted``: kd-usage leaves their transcripts out); and each agent's first stamp
    ever (``since``: the reports before it are in kd-usage's files); ``categories`` — the monitor's share of
    ``rows``."""
    users.parse_time(start), users.parse_time(end)  # ValueError on a malformed time
    rows = store._conn.execute(
        "SELECT agent, model, SUM(requests), COUNT(DISTINCT CASE WHEN kind='turn' THEN source END),"
        " COUNT(DISTINCT CASE WHEN kind='job' THEN source END), SUM(input), SUM(output), SUM(cache_read),"
        " SUM(cache_write), SUM(cost) FROM usage_log WHERE at>=? AND at<? GROUP BY agent, model"
        " ORDER BY agent, model",
        (start, end),
    ).fetchall()
    keys = ("agent", "model", "requests", "sessions", "jobs", "input", "output", "cache_read", "cache_write", "cost")
    out = [dict(zip(keys, row)) for row in rows]
    for row in out:
        row["cost"] = round(row["cost"], 6)
    agents = {agent: {"sessions": sessions, "jobs": jobs} for agent, sessions, jobs in store._conn.execute(
        "SELECT agent, COUNT(DISTINCT CASE WHEN kind='turn' THEN source END),"
        " COUNT(DISTINCT CASE WHEN kind='job' THEN source END) FROM usage_log WHERE at>=? AND at<? GROUP BY agent",
        (start, end)).fetchall()}
    counted = {}
    for agent, runtime in store._conn.execute(
            "SELECT DISTINCT agent, runtime FROM usage_log WHERE at>=? AND runtime!='' ORDER BY agent, runtime",
            (start,)).fetchall():
        counted.setdefault(agent, []).append(runtime)
    since = dict(store._conn.execute("SELECT agent, MIN(at) FROM usage_log GROUP BY agent").fetchall())
    return {"rows": out, "agents": agents, "counted": counted, "since": since,
            "categories": categories(store, start, end)}


# A turn of the monitor — one in the agent's "Monitor" thread, or (before it) the monitor's wake in its chat
MONITOR_TURN = (
    "t.kind='agent_monitor' OR (t.kind='agent_chat' AND c.kind='signal' AND c.card='' AND c.text LIKE '[монитор]%')"
)


def categories(store, start: str, end: str) -> list:
    """The part of ``rows`` that is a category of its own (``monitor`` — the monitor's turns, before the
    "Monitor" thread too): per agent and model, its turns, calls, tokens and cost. Already inside ``rows``."""
    found = store._conn.execute(
        "SELECT u.agent, u.model, COUNT(DISTINCT u.command), SUM(u.requests), SUM(u.input), SUM(u.output),"
        " SUM(u.cache_read), SUM(u.cache_write), SUM(u.cost) FROM usage_log u JOIN commands c ON c.id=u.command"
        f" JOIN threads t ON t.id=c.thread WHERE u.at>=? AND u.at<? AND u.kind='turn' AND ({MONITOR_TURN})"
        " GROUP BY u.agent, u.model ORDER BY u.agent, u.model",
        (start, end),
    ).fetchall()
    keys = ("agent", "model", "turns", "requests", "input", "output", "cache_read", "cache_write", "cost")
    out = [{"category": "monitor", **dict(zip(keys, row))} for row in found]
    for row in out:
        row["cost"] = round(row["cost"], 6)
    return out
