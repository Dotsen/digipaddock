"""The factory's operations in the cabinet: ``agent.reinstall`` of generated agents.

A request is one ``operations`` row per logical request (``key``: the release's ``release:<rev>``, a lead's own key):
its card, who asked, the agents and the revision it installs (always the deployed one: a request never installs an
older tree), and the grant it runs under. The developer asks through kd-factory on its card, which needs the card's
factory grant (``admin_grant`` under the factory policy) covering the kind and the agents; kd-release asks for the
card of its pull request under the same grant; root (the owner's ``kd-cabinet operation request`` on the host) needs
none. The same work asked twice is one operation: the same key with the same payload answers the row, another
payload is a conflict, and a request an active operation of the same revision already covers joins it. A newer
request covering a pending one that has not reached the host supersedes it.

The dispatcher hands one operation at a time to the session host and polls it until it ends; the host's report
(``host.report`` item ``operation``) moves it running → succeeded | failed, each move one event in the same
transaction. A failure is a critical notice in the card's thread (a platform alert without a card): the role it names may be
the one that cannot start any more. An operation whose result the host cannot give is failed ``unknown_result`` with
``reconcile`` set: an unknown effect is never a success; a new request starts with the runner's verification.

``component.install`` is the same row with no roles and ``params`` {component, mode, drain}: the card's
grant must have a step of that component and mode signed for the deployed revision; the same component and mode
of that revision active already is joined, nothing supersedes it. Its end in the card's thread says the script's
exit code, its check's and the masked tail of their output — what the runner saw, not a judgement of the task.
"""

import hashlib
import json
import re
import secrets
from pathlib import Path
from typing import Mapping, Optional

from kd import admin_grant, admin_task, clock, factory_ops
from kd.cabinet import api, notices, store

OPERATION_ID = re.compile(r"[0-9a-f]{32}")
KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._+-]{0,95}")  # "+": a pair release's release:<rev>+<deploy>
REVISION = re.compile(r"[0-9a-f]{7,40}")
ACTIVE = ("pending", "running")
FINAL = ("succeeded", "failed", "superseded")
PHASES = ("", "delivered", "draining", "installing", "checking", "resuming")
AGENT_STATES = ("ok", "unchanged", "failed", "skipped")
REASON_MAX = 500
HOST = "system"
SOURCE = "factory"
REQUEST_FIELDS = frozenset({"op", "agent", "card", "kind", "agents", "key", "revision", "requester", "component",
                            "mode"})
COMPONENT = factory_ops.COMPONENT
TAIL_MAX = 1500  # characters of a component's output tail kept in the result and the notice
# why an operation ended without running (the dispatcher's and the cabinet's own reasons)
GRANT_REVOKED = "grant_revoked"
GRANT_UNUSABLE = "grant_unusable"
REVISION_MOVED = "revision_moved"
UNKNOWN_RESULT = "unknown_result"
HOST_REFUSED = "host_refused"


def _invalid(message):
    return api.ApiError("invalid", message)


# --- rows -------------------------------------------------------------------------------------
def row(s, operation_id: str) -> Optional[dict]:
    found = s._conn.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
    if found is None:
        return None
    out = dict(found)
    out["agents"], out["result"] = json.loads(out["agents"]), json.loads(out["result"] or "{}")
    out["params"] = json.loads(out.get("params") or "{}")
    out["reconcile"] = bool(out["reconcile"])
    return out


def request_row(s, key: str) -> Optional[dict]:
    """An accepted request key: the operation it created or joined, its card, requester, payload."""
    found = s._conn.execute("SELECT * FROM operation_requests WHERE key=?", (key,)).fetchone()
    return dict(found) if found else None


def _remember(s, key: str, operation_id: str, card: str, requester: str, sha: str) -> None:
    s._conn.execute("INSERT INTO operation_requests (key, operation, card, requester, payload_sha, created)"
                    " VALUES (?,?,?,?,?,?)", (key, operation_id, card, requester, sha, s._clock()))


def linked(s, operation_id: str, *, card: str = "", requester: str = "") -> bool:
    """A request of this card (or this requester) created or joined the operation."""
    column, value = ("card", card) if card else ("requester", requester)
    return s._conn.execute(f"SELECT 1 FROM operation_requests WHERE operation=? AND {column}=? LIMIT 1",
                           (operation_id, value)).fetchone() is not None


def view(op: Mapping) -> dict:
    return {k: op[k] for k in ("id", "key", "kind", "card", "requested_by", "agents", "params", "revision",
                               "grant_id", "state", "phase", "result", "error", "reconcile", "superseded_by",
                               "created", "updated")}


def latest(s, operation_id: str) -> Optional[dict]:
    """The operation, or the one that superseded it (and so on): what a wait on it waits for now."""
    op, seen = row(s, operation_id), set()
    while op is not None and op["state"] == "superseded" and op["superseded_by"] and op["id"] not in seen:
        seen.add(op["id"])
        op = row(s, op["superseded_by"])
    return op


def _set(s, operation_id: str, **fields) -> dict:
    if "result" in fields:
        fields["result"] = json.dumps(fields["result"], ensure_ascii=False, sort_keys=True)
    if "reconcile" in fields:
        fields["reconcile"] = int(fields["reconcile"])
    fields["updated"] = s._clock()
    sets = ", ".join(f"{k}=?" for k in fields)
    s._conn.execute(f"UPDATE operations SET {sets} WHERE id=?", (*fields.values(), operation_id))
    return row(s, operation_id)


def _event(s, actor: str, kind: str, op: Mapping, **extra):
    thread = None
    if op["card"]:
        try:
            thread = s.card(op["card"]).thread
        except store.NotFound:
            thread = None
    s._event(actor, kind, op["card"], {"operation": op["id"], "kind": op["kind"], "agents": op["agents"],
                                        "state": op["state"], **extra}, thread)


# --- the grant --------------------------------------------------------------------------------
def _grants(ctx):
    if getattr(ctx, "admin", None) is None:
        raise api.ApiError("unavailable", "the grant store is not served here")
    return ctx.admin.store


def deployed(ctx) -> str:
    """The revision the deployed tree runs (``REVISION`` beside it, release_switch.sh); ctx may set its own."""
    own = getattr(ctx, "deployed_revision", None)
    if own is not None:
        return own()
    try:
        return (Path(ctx.repo_root) / "REVISION").read_text(encoding="utf-8").strip()
    except (OSError, TypeError) as exc:
        raise api.ApiError("unavailable", f"the deployed revision cannot be read ({exc.__class__.__name__})")


def _covering_grant(ctx, card_id: str, requester: str, kind: str, agents, catalog, params=None,
                    revision: str = "", release: bool = False) -> str:
    """The id of the card's factory grant that covers the request now; ``refused`` otherwise."""
    record = api._card(ctx, card_id)
    try:
        grant = _grants(ctx).load(card_id, policies=(admin_grant.FACTORY,), factory=catalog.scope)
    except admin_grant.GrantError as exc:
        raise api.ApiError("refused", f"{card_id}: no usable factory grant ({exc})")
    if grant is None:
        raise api.ApiError("refused", f"{card_id}: the owner approved no factory operations on this card — "
                                      f"kd-factory plan {card_id} first")
    try:
        card = admin_task.from_record(record.to_dict())
        factory_ops.check_request(grant, card=card, requester=requester or grant.assignee, kind=kind,
                                  agents=agents, catalog=catalog, now=_moment(ctx), revision=revision,
                                  release=release, **_named(params))
    except (admin_grant.GrantError, admin_task.CardError) as exc:
        raise api.ApiError("refused", str(exc))
    return grant.grant_id


def release_steps(ctx, card_id: str) -> list:
    """The component steps of the card's usable factory grant on revision ``release`` — the release of
    the card's own PR requests them; ``[]`` without a grant or steps."""
    record = api._card(ctx, card_id)
    catalog = ctx.factory()
    try:
        grant = _grants(ctx).load(card_id, policies=(admin_grant.FACTORY,), factory=catalog.scope)
        if grant is None:
            return []
        admin_grant.check_usable(grant, admin_task.from_record(record.to_dict()), _moment(ctx),
                                 policy=admin_grant.FACTORY, factory=catalog.scope)
    except (admin_grant.GrantError, admin_task.CardError):
        return []
    return [{"component": str(op.get("component") or ""), "mode": str(op.get("mode") or "")}
            for op in grant.operations
            if op.get("verb") == COMPONENT and op.get("revision") == factory_ops.RELEASE_REVISION]


def _named(params) -> dict:
    """A component operation's component and mode for ``check_request``."""
    params = params or {}
    return {"component": params.get("component", ""), "mode": params.get("mode", "")}


def _moment(ctx):
    from datetime import timezone

    from kd.cabinet import users

    return users.parse_time(ctx.now()).replace(tzinfo=timezone.utc)


def grant_usable(ctx, op: Mapping) -> Optional[str]:
    """None while the operation's grant still covers it (or it needs none); the reason otherwise."""
    if not op["grant_id"]:
        return None
    catalog = ctx.factory()
    try:
        grant = _grants(ctx).load(op["card"], policies=(admin_grant.FACTORY,), factory=catalog.scope)
    except (admin_grant.GrantError, api.ApiError):
        return GRANT_UNUSABLE
    if grant is None or grant.grant_id != op["grant_id"]:
        return GRANT_UNUSABLE
    if grant.state == "revoked":
        return GRANT_REVOKED
    try:
        record = ctx.store.card(op["card"])
        card = admin_task.from_record(record.to_dict())
        factory_ops.check_request(grant, card=card, requester=grant.assignee, kind=op["kind"], agents=op["agents"],
                                  catalog=catalog, now=_moment(ctx), revision=op["revision"],
                                  release=op["requested_by"] == "release", **_named(op.get("params")))
    except (store.NotFound, admin_grant.GrantError, admin_task.CardError):
        return GRANT_UNUSABLE
    return None


# --- a request --------------------------------------------------------------------------------
def _agents(ctx, value) -> list:
    if not isinstance(value, list) or not value or len(value) > 16:
        raise _invalid("agents is a non-empty list of generated agents")
    known = ctx.agent_names()
    for name in value:
        if not isinstance(name, str) or name not in known:
            raise _invalid(f"{name!r} is not a generated agent of this host")
    if len(set(value)) != len(value):
        raise _invalid("an agent is listed twice")
    return sorted(value)


def payload_sha(kind: str, card: str, agents, revision: str, params=None) -> str:
    value = {"kind": kind, "card": card, "agents": list(agents), "revision": revision}
    if params:  # a reinstall's sha stays what it was before params (keys repeated across the upgrade)
        value["params"] = dict(params)
    data = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _component(catalog, agents, component, mode) -> dict:
    """A component request's params: the catalog's own component, mode and drain; no roles travel with it."""
    if agents not in (None, []):
        raise _invalid(f"{COMPONENT} names a component and a mode, not agents")
    if not isinstance(component, str) or not isinstance(mode, str):
        raise _invalid(f"{COMPONENT} needs component and mode")
    try:
        spec = catalog.mode(component, mode)
    except factory_ops.ProposalError as exc:
        raise api.ApiError("refused", str(exc))
    return {"component": component, "mode": mode, "drain": spec.drain}


def request(ctx, *, card: str, requester: str, kind: str, agents, key: str, revision: str = "",
            actor: str = "", component=None, mode=None) -> dict:
    """One logical request (see the module): ``{"operation": view, "created": bool}``."""
    catalog = ctx.factory()
    if kind not in catalog.kinds:
        raise _invalid(f"kind must be one of {', '.join(sorted(catalog.kinds))}")
    if not isinstance(key, str) or not KEY.fullmatch(key):
        raise _invalid("key must be 1 to 96 letters, digits or :._- ")
    if kind == COMPONENT:
        params, agents = _component(catalog, agents, component, mode), []
    elif component is not None or mode is not None:
        raise _invalid(f"{kind} takes no component or mode")
    else:
        params, agents = {}, _agents(ctx, agents)
    current = deployed(ctx)
    if revision and revision != current:
        raise api.ApiError("refused", f"{REVISION_MOVED}: the deployed revision is {current[:12]}, not "
                                      f"{revision[:12]} — an older tree is never installed")
    s, grant_id = ctx.store, ""
    with s.transaction():
        if requester != "root":
            if not card:
                raise _invalid("a request names its card")
            grant_id = _covering_grant(ctx, card, "" if requester == "release" else requester, kind, agents,
                                       catalog, params, current, release=requester == "release")
        elif card:
            api._card(ctx, card)
        sha = payload_sha(kind, card, agents, current, params)
        known = request_row(s, key)
        if known is not None:  # the same request again, even after its operation ended
            if known["payload_sha"] != sha:
                raise api.ApiError("conflict", f"the key {key} names another request ({known['operation']})")
            return {"operation": view(row(s, known["operation"])), "created": False}
        for (other,) in s._conn.execute(
                "SELECT id FROM operations WHERE kind=? AND revision=? AND state IN ('pending','running')"
                " ORDER BY created", (kind, current)).fetchall():
            active = row(s, other)
            same = active["params"] == params if kind == COMPONENT else set(agents) <= set(active["agents"])
            if same:  # the same work: one installation, this card linked to it
                _remember(s, key, active["id"], card, actor or requester, sha)
                return {"operation": view(active), "created": False}
        now, operation_id = s._clock(), secrets.token_hex(16)
        s._conn.execute(
            "INSERT INTO operations (id, key, kind, card, requested_by, agents, params, revision, grant_id,"
            " payload_sha, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (operation_id, key, kind, card, actor or requester, json.dumps(agents),
             json.dumps(params, sort_keys=True), current, grant_id, sha, now, now))
        _remember(s, key, operation_id, card, actor or requester, sha)
        op = row(s, operation_id)
        for (other,) in s._conn.execute(
                "SELECT id FROM operations WHERE kind=? AND state='pending' AND phase='' AND id!=? ORDER BY created",
                (kind, operation_id)).fetchall():
            if kind == COMPONENT:
                break  # a component's run is never replaced by another one
            older = row(s, other)
            if set(older["agents"]) <= set(agents):  # a newer request of at least the same roles takes its place
                _event(s, HOST, "operation.superseded", _set(s, other, state="superseded",
                                                             superseded_by=operation_id), by=operation_id)
        _event(s, actor or requester, "operation.queued", op, revision=current, key=key, **params)
        return {"operation": view(op), "created": True}


# --- the host's reports -----------------------------------------------------------------------
def _reason(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > REASON_MAX or "\n" in value:
        raise _invalid(f"reason is one line of at most {REASON_MAX} characters")
    return value


def _per_agent(value) -> dict:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or len(value) > 16:
        raise _invalid("agents is an object of agent -> {state, stage, error}")
    out = {}
    for name, item in value.items():
        if not isinstance(name, str) or not isinstance(item, Mapping) or item.get("state") not in AGENT_STATES:
            raise _invalid(f"agents.{name}: state is one of {', '.join(AGENT_STATES)}")
        out[name] = {"state": item["state"], "stage": _reason(item.get("stage")), "error": _reason(item.get("error"))}
        tail, batch = _tail(item.get("tail")), item.get("batch")
        if tail:  # a failed stage's masked output, as the runner kept it
            out[name]["tail"] = tail
        if batch is not None:
            if not isinstance(batch, int) or isinstance(batch, bool) or not 0 < batch <= 16:  # a role per batch
                raise _invalid(f"agents.{name}: batch is a batch number")
            out[name]["batch"] = batch
    return out


def report_item(ctx, item):
    """``{"type": "operation", "operation", "state": running|succeeded|failed, "phase"?, "agents"?, "reason"?}``.
    A final state is never moved again; a repeated report changes nothing."""
    operation_id = item.get("operation")
    if not isinstance(operation_id, str) or not OPERATION_ID.fullmatch(operation_id):
        raise _invalid("invalid operation id")
    op = row(ctx.store, operation_id)
    if op is None:
        raise api.ApiError("not_found", "unknown operation")
    state, phase = item.get("state"), item.get("phase", "") or ""
    if state not in ("running", "succeeded", "failed") or phase not in PHASES:
        raise _invalid("state is running, succeeded or failed; phase one of " + ", ".join(p for p in PHASES if p))
    reason, per_agent = _reason(item.get("reason")), _per_agent(item.get("agents"))
    kept, until = _names(item.get("kept"), op["agents"]), _until(item.get("until"))
    ran = _ran(item.get("component")) if op["kind"] == COMPONENT else None
    if op["state"] in FINAL:
        return
    if state == "running":
        progress = {"agents": per_agent, "phase": phase, **({"until": until} if until else {})}
        if op["state"] == "running" and op["phase"] == phase and op["result"] == progress:
            return
        moved = _set(ctx.store, operation_id, state="running", phase=phase, result=progress)
        if op["state"] != "running":
            _event(ctx.store, HOST, "operation.running", moved, phase=phase)
        return
    if state == "failed" and reason == REVISION_MOVED and op["kind"] != COMPONENT and _supersede(ctx, op):
        return
    finish(ctx, op, state, reason=reason, agents=per_agent, phase=phase, kept=kept, ran=ran)


def _code(value) -> Optional[int]:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not -255 <= value <= 255:
        raise _invalid("an exit code is an integer")
    return value


def _tail(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _invalid("a tail is text")
    return value[-TAIL_MAX:]


def _ran(value) -> Optional[dict]:
    """A component's run as the runner saw it: the script's exit code, its check's (None: not run) and the
    masked tail of their output."""
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) - {"code", "tail", "check", "check_tail"}:
        raise _invalid("component is {code, tail, check, check_tail}")
    return {"code": _code(value.get("code")), "tail": _tail(value.get("tail")),
            "check": _code(value.get("check")), "check_tail": _tail(value.get("check_tail"))}


def _names(value, agents) -> Optional[list]:
    """The roles a host's result keeps paused: names of the operation's own roles, or None."""
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) and v in agents for v in value):
        raise _invalid("kept is a list of the operation's agents")
    return sorted(set(value))


def _until(value) -> str:
    """The drain's deadline, epoch seconds from the host, as the cabinet's time."""
    if value is None:
        return ""
    if not isinstance(value, int) or isinstance(value, bool) or not 0 < value < 2 ** 33:
        raise _invalid("until is epoch seconds")
    return clock.iso(value)


def _supersede(ctx, op: Mapping) -> bool:
    """A runner that found the tree moved under it ran behind a newer release: when a newer active operation of
    the same kind covers its roles, that one does the work — this one is superseded (a wait on it follows), not a
    failure the owner must hear of."""
    s = ctx.store
    with s.transaction():
        for (other,) in s._conn.execute(
                "SELECT id FROM operations WHERE kind=? AND state IN ('pending','running') AND id!=? AND created>=?"
                " ORDER BY created DESC", (op["kind"], op["id"], op["created"])).fetchall():
            newer = row(s, other)
            if set(op["agents"]) <= set(newer["agents"]):
                _event(s, HOST, "operation.superseded",
                       _set(s, op["id"], state="superseded", superseded_by=other, error=REVISION_MOVED), by=other)
                return True
    return False


# a failure keeps a role paused on the host (session_host.operation.Operations._kept): the roles its result names
# (``kept``); else its install failed, a role the result does not name, every role when the effect is
# unknown or the host's check failed; anything else ended before an install
_UNKNOWN_EFFECT = ("runner_lost", "host_check_failed", UNKNOWN_RESULT)


def _kept(reason: str, agents: Mapping, names, kept: Optional[list] = None) -> list:
    if kept is not None:
        return sorted(kept)
    if reason in _UNKNOWN_EFFECT:
        return sorted(names)
    if not agents:
        return []
    return sorted(n for n in names if n not in agents or agents[n]["state"] == "failed")


def held(op: Mapping) -> list:
    """The roles a failed operation still keeps paused on the host: its result's ``kept`` (less what root released
    since), or for a result written before ``kept`` what the host held then."""
    if op["state"] != "failed":
        return []
    kept = op["result"].get("kept")
    if isinstance(kept, list):
        return sorted(kept)
    return _kept(op["error"] or "", op["result"].get("agents") or {}, op["agents"])


def release_item(ctx, item):
    """``{"type": "operation_release", "operation", "agent"}``: root resumed a role a failed operation kept paused
    (``kd-session-host resume``) — it is not held any more. A repeated report changes nothing."""
    operation_id, agent = item.get("operation"), item.get("agent")
    if not isinstance(operation_id, str) or not OPERATION_ID.fullmatch(operation_id):
        raise _invalid("invalid operation id")
    if not isinstance(agent, str):
        raise _invalid("agent is a role's name")
    op = row(ctx.store, operation_id)
    if op is None or agent not in op["agents"]:
        return  # a pause the cabinet never recorded: nothing it shows to lift
    kept = held(op)
    if agent not in kept:
        return
    s = ctx.store
    with s.transaction():
        done = _set(s, operation_id, result={**op["result"], "kept": [a for a in kept if a != agent]})
        _event(s, HOST, "operation.released", done, agent=agent)


def _fence(text: str) -> str:
    """Output as the notice shows it: plain text, line by line (a notice is not markdown); nothing — a dash."""
    return text.rstrip() or "—"


def _component_notice(s, done: Mapping, state: str, reason: str, ran: Optional[dict]) -> None:
    """A component's end in the card's thread: the codes and the masked tails the runner saw."""
    ran = ran or {}
    params = {"component": done["params"].get("component", ""), "mode": done["params"].get("mode", ""),
              "revision": done["revision"][:12], "operation": done["id"][:12],
              "code": "-" if ran.get("code") is None else str(ran["code"]),
              "check": "-" if ran.get("check") is None else str(ran["check"]),
              "reason": reason or "-", "tail": _fence(ran.get("tail") or ""),
              "check_tail": _fence(ran.get("check_tail") or "")}
    if state == "failed":
        key = "notice.factory.component_unknown" if done["reconcile"] else "notice.factory.component_failed"
        level = "critical"
    else:
        key, level = "notice.factory.component_done", "info"
    notices.post_in_tx(s, source=SOURCE, level=level, key=key, card=done["card"], params=params)


def unit(operation_id: str, batch: Optional[int] = None) -> str:
    """The runner's unit of a batch on the session host (``session_host.operation.unit``)."""
    return f"digipaddock-operation-{operation_id[:12]}" + (f"-{batch}" if batch else "")


def _failure(op: Mapping, agents: Mapping, kept: list) -> dict:
    """A failed reinstall's notice: the failed role's stage, batch unit and masked output, the roles left on
    the old version, and the retry of the failed and skipped roles — a new key, so it is a new request under the
    card's grant (kd-factory checks it as always)."""
    # the failed role; a failed host check fails none — the first role it keeps names the batch's unit
    item = next((a for _, a in sorted(agents.items()) if a["state"] == "failed"), None) or next(
        (agents[n] for n in kept if n in agents), {})
    skipped = sorted(n for n in op["agents"] if n not in agents or agents[n]["state"] == "skipped")
    retry = sorted(set(kept) | set(skipped))
    command = f"--agents {','.join(retry)}" if retry else ""
    return {"unit": unit(op["id"], item.get("batch")), "batch": str(item.get("batch") or "-"),
            "tail": _fence(item.get("tail") or ""), "old": ", ".join(skipped) or "—",
            "retry_agents": ",".join(retry) or "-",
            "retry": (f"kd-factory reinstall {op['card']} {command} --key retry-{op['id'][:12]}"
                      if op["card"] and retry else "—")}


def finish(ctx, op: Mapping, state: str, *, reason: str = "", agents=None, phase: str = "",
           reconcile: bool = False, kept: Optional[list] = None, ran: Optional[dict] = None) -> dict:
    """The operation's end: its row, the event and, for a failure, the owner's critical notice — one write."""
    s = ctx.store
    with s.transaction():
        kept = _kept(reason, agents or {}, op["agents"], kept) if state == "failed" else []
        result = {"agents": agents or {}, "phase": phase, "kept": kept}
        if ran is not None:
            result["component"] = ran
        done = _set(s, op["id"], state=state, phase=phase, result=result, error=reason, reconcile=reconcile)
        _event(s, HOST, f"operation.{state}", done, reason=reason)
        if done["kind"] == COMPONENT:
            _component_notice(s, done, state, reason, ran)
            return done
        names = ", ".join(sorted(done["agents"]))
        stage = next((a["stage"] for a in (agents or {}).values() if a["state"] == "failed"), "") or phase
        if state == "failed":
            if reconcile:  # the host lost it: the roles' install is unknown — the owner checks them
                key = "notice.factory.unknown"
            else:
                key = "notice.factory.failed" if kept else "notice.factory.failed_resumed"
            params = {"agents": ",".join(sorted(done["agents"])), "failed": ",".join(kept),
                      "stage": stage or "-", "reason": reason or "-", "operation": done["id"][:12]}
            if not reconcile:
                params.update(_failure(done, agents or {}, kept))
                if not done["card"]:
                    key += "_root"  # no card, no grant: only root asks again
            notices.post_in_tx(s, source=SOURCE, level="critical", key=key, card=done["card"], params=params)
        else:
            notices.post_in_tx(s, source=SOURCE, level="info", key="notice.factory.done", card=done["card"],
                               params={"agents": names, "revision": done["revision"][:12],
                                       "operation": done["id"][:12]})
        return done


def on_revoked(ctx, card_id: str) -> int:
    """The card's factory grant was revoked: what has not reached the host yet ends ``grant_revoked``."""
    s, n = ctx.store, 0
    with s.transaction():
        for (operation_id,) in s._conn.execute(
                "SELECT id FROM operations WHERE card=? AND state='pending' AND phase='' AND grant_id!=''",
                (card_id,)).fetchall():
            finish(ctx, row(s, operation_id), "failed", reason=GRANT_REVOKED)
            n += 1
    return n


# --- the dispatcher's part --------------------------------------------------------------------
DEVCHECK = "dev.check"  # a contour's check (``devchecks``): its own lane per contour, never this one


def in_flight(s) -> Optional[dict]:
    """The operation the host has now (delivered or running): one at a time."""
    found = s._conn.execute(
        "SELECT id FROM operations WHERE (state='running' OR (state='pending' AND phase='delivered')) AND kind!=?"
        " ORDER BY created LIMIT 1", (DEVCHECK,)).fetchone()
    return row(s, found[0]) if found else None


def next_due(s, now: str) -> Optional[dict]:
    found = s._conn.execute(
        "SELECT id FROM operations WHERE state='pending' AND phase='' AND next_try<=? AND kind!=? ORDER BY created"
        " LIMIT 1", (now, DEVCHECK)).fetchone()
    return row(s, found[0]) if found else None


def host_request(op: Mapping, timeout: int, start: bool = True) -> dict:
    """The host's ``operation``: ``start`` for a pending one; a poll of one in flight asks only (the host answers
    ``unknown`` rather than start again what it lost)."""
    request = {"op": "operation", "operation": op["id"], "kind": op["kind"], "agents": list(op["agents"]),
               "revision": op["revision"], "timeout": timeout, "start": start}
    if op["kind"] == COMPONENT:  # the host names them in the runner's request; the runner maps the catalog
        params = op.get("params") or {}
        request.update(component=params.get("component", ""), mode=params.get("mode", ""),
                       drain=params.get("drain", ""))
    return request


def host_cancel(op: Mapping, reason: str) -> dict:
    """The host's ``operation_cancel``: nothing is installed while the roles drain."""
    return {"op": "operation_cancel", "operation": op["id"], "reason": reason}


def cancel_reason(ctx, op: Mapping) -> Optional[str]:
    """Why the operation the host has must stop now: its grant no longer covers it while nothing is installed yet
    (delivered or draining); None otherwise — an install under way is never cut."""
    if op["phase"] not in ("delivered", "draining"):
        return None
    return grant_usable(ctx, op)


def prepare(ctx, operation_id: str) -> Optional[dict]:
    """A due operation's checks right before the host gets it: its grant still covers it, the deployed tree is
    still its revision. None when it ended here."""
    op = row(ctx.store, operation_id)
    if op is None or op["state"] != "pending" or op["phase"]:
        return None
    why = grant_usable(ctx, op)
    if why is None and op["revision"] != deployed(ctx):
        why = REVISION_MOVED
    if why:
        finish(ctx, op, "failed", reason=why)
        return None
    return op


def delivered(ctx, operation_id: str, answer: Mapping) -> None:
    op = row(ctx.store, operation_id)
    if op is not None and op["state"] == "pending" and not op["phase"]:
        _set(ctx.store, operation_id, phase="delivered", attempts=op["attempts"] + 1)
    answered(ctx, operation_id, answer)


def answered(ctx, operation_id: str, answer: Mapping) -> None:
    """The host's answer to ``operation``: its report, or ``unknown`` — it holds no such operation and has no
    result for it (its state was lost): failed with ``reconcile``, never a success."""
    if answer.get("state") == "unknown":
        op = row(ctx.store, operation_id)
        if op is not None and (op["state"] == "running" or (op["state"] == "pending" and op["phase"] == "delivered")):
            finish(ctx, op, "failed", reason=UNKNOWN_RESULT, reconcile=True)
        return
    item = answer.get("report")
    if isinstance(item, Mapping):
        report_item(ctx, {**item, "operation": operation_id})


def retry(ctx, operation_id: str, next_try: str) -> None:
    op = row(ctx.store, operation_id)
    if op is not None and op["state"] == "pending" and not op["phase"]:
        _set(ctx.store, operation_id, next_try=next_try, attempts=op["attempts"] + 1)


def refused(ctx, operation_id: str, message: str) -> None:
    op = row(ctx.store, operation_id)
    if op is not None and op["state"] == "pending":
        finish(ctx, op, "failed", reason=f"{HOST_REFUSED}: {message}"[:REASON_MAX])


# --- ops --------------------------------------------------------------------------------------
def _request_op(ctx, principal, req):
    extra = set(req) - REQUEST_FIELDS
    if extra:
        raise _invalid(f"unknown field: {', '.join(sorted(extra))}")
    card = req.get("card") or ""
    if card and (not isinstance(card, str) or not api.CARD.fullmatch(card)):
        raise _invalid("card is KD-<n>")
    revision = req.get("revision") or ""
    if revision and (not isinstance(revision, str) or not REVISION.fullmatch(revision)):
        raise _invalid("revision is a commit sha")
    if principal.kind == "root":
        requester = req.get("requester") or "root"
        if requester not in ("root", "release"):
            raise _invalid("root asks as root or release")
        actor = requester
    else:
        agent = api._executor_agent(ctx, principal, req, coordinator=False)
        if agent not in ctx.factory().requesters:
            raise api.ApiError("refused", f"@{agent} asks the factory for nothing")
        requester, actor = agent, f"agent:{agent}"
    return request(ctx, card=card, requester=requester, kind=req.get("kind"), agents=req.get("agents"),
                   key=req.get("key"), revision=revision, actor=actor, component=req.get("component"),
                   mode=req.get("mode"))


def _release_steps_op(ctx, principal, req):
    """``operation.release_steps {card}`` (root) → ``{"steps": [{"component", "mode"}]}``."""
    card = req.get("card")
    if not isinstance(card, str) or not api.CARD.fullmatch(card):
        raise _invalid("card is KD-<n>")
    return {"steps": release_steps(ctx, card)}


def _get_op(ctx, principal, req):
    operation_id = req.get("id")
    if not isinstance(operation_id, str) or not OPERATION_ID.fullmatch(operation_id):
        raise _invalid("invalid operation id")
    op = row(ctx.store, operation_id)
    if op is None:
        raise api.ApiError("not_found", "unknown operation")
    if principal.kind == "executor":
        agent = api._executor_agent(ctx, principal, req, coordinator=False)
        mine = op["requested_by"] == f"agent:{agent}" or linked(ctx.store, op["id"], requester=f"agent:{agent}")
        if not (mine or leads(ctx.store, op, agent)):
            raise api.ApiError("not_found", "unknown operation")
    elif principal.kind == "user":
        if not op["card"] or not api.visible(ctx, principal, ctx.store.card(op["card"]).project):
            raise api.ApiError("not_found", "unknown operation")
    current = latest(ctx.store, operation_id)
    return {"operation": view(op), "current": view(current) if current and current["id"] != op["id"] else None}


def _authorize_op(ctx, principal, req):
    """The session host's question right before a batch of the operation starts — is it still
    allowed: the operation active, the roles its own, its grant still covering it, the tree still its revision."""
    operation_id = req.get("operation")
    if not isinstance(operation_id, str) or not OPERATION_ID.fullmatch(operation_id):
        raise _invalid("invalid operation id")
    op = row(ctx.store, operation_id)
    if op is None:
        return {"allowed": False, "reason": "unknown_operation"}
    agents = req.get("agents")
    if op["kind"] == COMPONENT:  # no roles of its own: the batch is the component's one run
        if agents != []:
            raise _invalid(f"agents of a {COMPONENT} batch is an empty list")
    elif not isinstance(agents, list) or not agents or not all(isinstance(a, str) and a in op["agents"]
                                                                for a in agents):
        raise _invalid("agents is a non-empty list of the operation's agents")
    if op["state"] not in ACTIVE:
        return {"allowed": False, "reason": f"operation_{op['state']}"}
    why = grant_usable(ctx, op)
    if why is None and op["revision"] != deployed(ctx):
        why = REVISION_MOVED
    return {"allowed": why is None, "reason": why or ""}


OPERATION_PREFIX = re.compile(r"[0-9a-f]{12,32}")


def leads(s, op: Mapping, agent: str) -> bool:
    """``agent`` now leads a card of the operation: its own or one whose request joined it. Having asked
    for it is not enough."""
    cards = {op["card"]} | {r[0] for r in s._conn.execute(
        "SELECT card FROM operation_requests WHERE operation=?", (op["id"],)).fetchall()}
    for card_id in sorted(c for c in cards if c):
        try:
            if s.card(card_id).assignee == f"@{agent}":
                return True
        except store.NotFound:
            continue
    return False


def _reader_op(ctx, principal, req):
    """``operation.reader {operation, agent}`` (the host reader): may ``agent`` read the journal of the
    operation's runner units — it leads the operation's card now. ``operation`` is its id or a prefix of at least 12
    hex digits (the units' own); an unknown or ambiguous one is not allowed."""
    prefix, agent = req.get("operation"), req.get("agent")
    if not isinstance(prefix, str) or not OPERATION_PREFIX.fullmatch(prefix):
        raise _invalid("operation is an id or its first 12 hex digits")
    if not isinstance(agent, str) or agent not in ctx.agent_names():
        raise _invalid("agent is an agent of this host")
    found = ctx.store._conn.execute("SELECT id FROM operations WHERE substr(id, 1, ?)=? LIMIT 2",
                                    (len(prefix), prefix)).fetchall()
    if len(found) != 1:
        return {"allowed": False, "reason": "unknown_operation" if not found else "ambiguous_operation"}
    allowed = leads(ctx.store, row(ctx.store, found[0][0]), agent)
    return {"allowed": allowed, "reason": "" if allowed else "not_your_card", "operation": found[0][0]}


def _list_op(ctx, principal, req):
    card = api._visible_card(ctx, principal, req.get("card"))
    ids = [r[0] for r in ctx.store._conn.execute(  # the factory's steps: a card's checks show as notices
        "SELECT id FROM operations WHERE card=? AND kind!=? ORDER BY created DESC LIMIT 20",
        (card.id, DEVCHECK)).fetchall()]
    return {"operations": [view(row(ctx.store, i)) for i in ids]}


OPS = {
    "operation.request": (frozenset({"executor", "root"}), _request_op),
    "operation.release_steps": (frozenset({"root"}), _release_steps_op),
    "operation.get": (frozenset({"executor", "root", "user", "cabinet"}), _get_op),
    "operation.list": (frozenset({"user"}), _list_op),
    "operation.authorize": (frozenset({"root"}), _authorize_op),
    "operation.reader": (frozenset({"observe", "root"}), _reader_op),
}
api.OPS.update(OPS)  # the board API serves them (api imports this module at its end)
