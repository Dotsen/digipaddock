"""The board API's part for the session host: its reports, commands, sessions, the agent chat.

The host (root) reports what its sessions do in one ``host.report`` call per batch, applied in one transaction
in order: a session's state, a turn that started or ended for a command, a message of the session (the agent's
text, a tool call with its one-line summary, a tool result's size; with the turn's command, and a tool's
masked output kept apart from the thread — ``session.turns`` and ``session.output`` show them to whoever controls
the agent). Commands come from the
board's own events (``kd.cabinet.commands``), from an executor or an agent for itself (``command.enqueue``), and
from people: "Continue", "Stop", a message in the agent's chat.
"""

import json
import re
import sys
import traceback
from typing import Mapping

from kd import agents, llm_models, naming
from kd.cabinet import api, commands, jobs, outcomes, schema, store, usage, voice, waits

KEY = re.compile(r"[A-Za-z0-9:._-]{1,64}")
SESSION_ID = COMMAND_ID = re.compile(r"[0-9a-f]{32}")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SLOT = re.compile(r"[a-z0-9_/-]{1,64}")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
COMMAND_TEXT_MAX = 2000
ERROR_MAX = 300
PAYLOAD_MAX = 4096
REPORT_MAX = 200
LIST_MAX = 20
OUTPUT_MAX = 4000  # the host's excerpt of a tool's output (kd.session_host.claude.OUTPUT_MAX)
TURNS_MAX = 20
STEPS_MAX = 300
TURN_TEXT_MAX = 300
STEP_BODY_MAX = 500
POSITION_MAX = 10000  # a place in an agent's queue for a slot
LIVE = ("queued", "starting", "working", "idle")
ENDED = ("exited", "failed", "stopped")
REPORTED = LIVE + ENDED
RESUMABLE = ENDED
MESSAGE_KINDS = ("text", "tool_call", "tool_result", "status")
AUTH_FAILED = "auth_failed"  # kd.session_host.protocol.AUTH_FAILED: the host's session error when Claude refused it
HOST = "system"  # the actor of what the host reports
# The session runs in that thread, so its turn's text is the answer; a chat --post there is refused.
CHAT_WAKE = ("[chat] a message for you in your cabinet chat — read it with kd-task chat; your turn's text is the answer, "
             "posted there")
MONITOR_WAKE = ("[monitor] a message for you in your «Монитор» thread — read it with kd-task chat; your turn's text is "
                "the answer, posted there")
# Each wake of "Monitor" is a new conversation: the agent's own message the person most likely answers comes as data
MONITOR_LAST = ". Your last message there before it: #{seq} — kd-task chat --message {seq} reads it whole"
_PATH = ("queued", "delivered", "started")


def install(ctx, config) -> None:
    """``config()`` is the host's current ``HostConfig``: the board's events queue commands for its agents."""
    ctx.host_config = config
    commands.install(ctx.store, lambda agent: config().hosted(agent), pipeline=waits.hook(ctx))


def _hosted(ctx, agent: str) -> bool:
    return ctx.host_config().hosted(agent)


# --- validation -------------------------------------------------------------------------------
def _invalid(message):
    return api.ApiError("invalid", message)


def _line_text(value, name, limit) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or _CTRL.search(value):
        raise _invalid(f"{name} must be one line of 1 to {limit} characters")
    return value.strip()


def _agent(ctx, value) -> str:
    if not isinstance(value, str) or not agents.NAME_RE.fullmatch(value):
        raise _invalid("invalid agent name")
    try:
        names = agents.names(ctx.registry_path)
    except ValueError as exc:
        raise api.ApiError("unavailable", str(exc))
    if value not in names:
        raise api.ApiError("not_found", f"{value}: no such agent")
    return value


def _session(ctx, value) -> dict:
    if not isinstance(value, str) or not SESSION_ID.fullmatch(value):
        raise _invalid("invalid session id")
    try:
        return ctx.store.session(value)
    except store.NotFound as exc:
        raise api.ApiError("not_found", str(exc))


def _command(ctx, value) -> dict:
    if not isinstance(value, str) or not SESSION_ID.fullmatch(value):
        raise _invalid("invalid command id")
    try:
        return ctx.store.command(value)
    except store.NotFound as exc:
        raise api.ApiError("not_found", str(exc))


def _thread_card(ctx, thread: int):
    """The card behind a thread, or None for an agent chat."""
    card = ctx.store.thread_info(thread)["card"]
    return ctx.store.card(card) if card else None


# --- host.report ------------------------------------------------------------------------------
def _status_line(ctx, session: Mapping, key: str, params: Mapping, body: str):
    ctx.store.add_thread_message(
        session["thread"],
        author=HOST,
        kind="status",
        body=body,
        payload={"key": key, "params": dict(params)},
        session=session["id"],
        via="session",
    )


def _item_host(ctx, item):
    """A (re)started host: sessions with a turn under way failed, the others lost nothing and exit quietly;
    commands it had written to a process (``inflight``, its journal) may have run and fail; the rest it held go
    back to the queue; started ones fail — none runs twice."""
    if item.get("phase") != "started":
        raise _invalid("a host item is {type: host, phase: started}")
    inflight = item.get("inflight", [])
    if not isinstance(inflight, list) or not all(isinstance(c, str) and COMMAND_ID.fullmatch(c) for c in inflight):
        raise _invalid("inflight is a list of command ids")
    now = ctx.now()
    for session in ctx.store.sessions(states=LIVE):
        if session["state"] not in ("starting", "working"):
            ctx.store.session_update(session["id"], state="exited", ended=now, actor=HOST)
            continue
        ctx.store.session_update(session["id"], state="failed", error="host_restart", ended=now, actor=HOST)
        _status_line(
            ctx, session, "sys.session.failed_restart", {"agent": "@" + session["agent"]},
            "the session stopped: the session host restarted",
        )
    for command in ctx.store.commands(states=("delivered",), limit=10000):
        if command["id"] in inflight:
            ctx.store.command_state(command["id"], "failed", reason="host_restart", actor=HOST)
        else:
            ctx.store.command_state(command["id"], "queued", next_try=now, actor=HOST)
    for command in ctx.store.commands(states=("started",), limit=10000):
        ctx.store.command_state(command["id"], "failed", reason="host_restart", actor=HOST)
        outcomes.void(ctx, command["id"], "host_restart")  # the turn was cut: its outcome never applies
    jobs.on_host_restart(ctx, inflight)


def _item_session(ctx, item):
    session = _session(ctx, item.get("session"))
    state = item.get("state")
    if state not in REPORTED:
        raise _invalid(f"unknown session state {state!r}")
    now = ctx.now()
    fields = {"state": state, "heartbeat": now}
    if "slot" in item:
        if not isinstance(item["slot"], str) or not SLOT.fullmatch(item["slot"]):
            raise _invalid("invalid slot")
        fields["slot"] = item["slot"]
    if "runtime_session_id" in item:
        if not isinstance(item["runtime_session_id"], str) or not UUID.fullmatch(item["runtime_session_id"]):
            raise _invalid("invalid runtime session id")
        fields["runtime_session_id"] = item["runtime_session_id"]
        if item["runtime_session_id"] != session["runtime_session_id"]:
            # a new Claude conversation (a resume keeps its id): its cumulative usage starts from zero, so its
            # first total counts whole — never as a difference from another conversation's (usage.py)
            fields["usage_total"] = {}
    # the cabinet model the process runs on ("" — the agent's own); or a subscription model's ref
    if "model" in item:
        model = item["model"]
        named = isinstance(model, str) and (llm_models.SLUG.fullmatch(model) or llm_models.is_ref(model))
        if not isinstance(model, str) or (model and not named):
            raise _invalid("invalid model")
        fields["model"] = model
    if "effort" in item:
        try:
            fields["effort"] = llm_models.check_effort(item["effort"])
        except ValueError as exc:
            raise _invalid(str(exc))
    if state == "queued":  # its place in the agent's queue for a slot (none: an older host), since when
        if "position" in item:
            position = item["position"]
            if not isinstance(position, int) or isinstance(position, bool) or not 1 <= position <= POSITION_MAX:
                raise _invalid("invalid position")
            fields["queue_position"] = position
        if session["state"] != "queued" or not session["queued_since"]:
            fields["queued_since"] = now
    if "error" in item:
        fields["error"] = _line_text(item["error"], "error", ERROR_MAX)
    elif state != "failed":
        fields["error"] = ""  # a session that runs again leaves its last failure behind
    if state in ENDED:
        fields["ended"] = now
    else:
        fields["ended"] = ""
        if state in ("starting", "working") and not session["started"]:
            fields["started"] = now
    after = ctx.store.session_update(session["id"], actor=HOST, **fields)
    if (state == "starting" and ("model" in fields or "effort" in fields) and (after["model"] or after["effort"])
            and ((after["model"], after["effort"]) != (session["model"], session["effort"])
                 or not session["started"])):
        # the thread says what a process starts on — once per change of the pair; a process
        # on the subscription with the agent's own effort says nothing
        _status_line(
            ctx, after, "sys.session.model", {"model": after["model"], "effort": after["effort"]},
            f"model: {after['model'] or 'subscription'}, effort: {after['effort'] or 'agent'}",
        )
    if state == "failed" and session["state"] != "failed" and after["error"] == AUTH_FAILED:
        _status_line(  # Claude refused the credentials — the remedy, not the code
            ctx, after, "sys.session.auth_failed",
            {"agent": "@" + after["agent"], "command": naming.command("kd-secret")},
            "the session failed: Claude refused the agent's credentials (renew the host's Claude token)",
        )
    elif state == "failed" and session["state"] != "failed":
        _status_line(
            ctx, after, "sys.session.failed", {"agent": "@" + after["agent"], "error": after["error"]},
            f"the session failed: {after['error']}",
        )


def _advance(ctx, command: Mapping, target: str, session: str, error: str = ""):
    """Move ``command`` along queued → delivered → started → done|failed up to ``target``; a command already
    there or past it (the host restarted, a report repeated) is left as it is."""
    state = command["state"]
    if state not in _PATH:
        return
    if target == "failed" and state != "started":  # it never ran: no run.started for it
        ctx.store.command_state(command["id"], "failed", session=session, error=error, actor=HOST)
        return
    steps = list(_PATH[_PATH.index(state) + 1 :]) if target != state else []
    if target == "started":
        steps = steps[: steps.index("started") + 1] if "started" in steps else []
    else:
        steps.append(target)
    for step in steps:
        ctx.store.command_state(
            command["id"], step, session=session, error=error if step == "failed" else "", actor=HOST
        )


def _waiting_user(ctx, command: Mapping) -> bool:
    if not command["card"]:
        return False
    return any(
        d["type"] == "question" and d["requested_by"] == f"agent:{command['agent']}"
        for d in api._ledger(ctx).list(state="open", card=command["card"])
    )


def _item_turn(ctx, item):
    session = _session(ctx, item.get("session"))
    command = _command(ctx, item.get("command"))
    if command["agent"] != session["agent"] or command["thread"] != session["thread"]:
        raise _invalid("the command is not of this session")
    phase = item.get("phase")
    if phase == "started":
        _advance(ctx, command, "started", session["id"])
        outcomes.started(ctx, ctx.store.command(command["id"]))
    elif phase == "ended":
        ok = item.get("ok") is True
        error = "" if ok else _line_text(item.get("error") or "turn failed", "error", ERROR_MAX)
        if "usage" in item:
            try:
                used = usage.check(item["usage"])
            except ValueError as exc:
                raise _invalid(str(exc))
            # a session on a cabinet model is costed by the model's table, not by what the process said
            prices = ctx.store.llm_model(session["model"]) if session.get("model") else None
            usage.record_turn(ctx.store, session, command, used, ctx.now(), prices=prices)
        _advance(ctx, command, "done" if ok else "failed", session["id"], error)
        after = ctx.store.command(command["id"])
        if after["state"] == "done" and _waiting_user(ctx, after):
            ctx.store._command_event(after, "run.waiting_user", HOST)
        if after["state"] in ("done", "failed"):
            try:  # the outcome, in this report; its failure never takes the turn's end with it
                with ctx.store.transaction():
                    outcomes.finalize(ctx, after, after["state"] == "done")
            except Exception:  # noqa: BLE001 — the sweep repairs what it left; the journal has the traceback
                traceback.print_exc(file=sys.stderr)
    else:
        raise _invalid("a turn's phase is started or ended")


def _excerpt(value) -> dict:
    if not isinstance(value, Mapping) or set(value) != {"text", "size", "truncated"}:
        raise _invalid("output is {text, size, truncated}")
    text, size, truncated = value["text"], value["size"], value["truncated"]
    if not isinstance(text, str) or len(text) > OUTPUT_MAX:
        raise _invalid(f"output text is up to {OUTPUT_MAX} characters")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0 or not isinstance(truncated, bool):
        raise _invalid("output size is a count and truncated a flag")
    return {"text": text, "size": size, "truncated": truncated}


def _item_message(ctx, item):
    session = _session(ctx, item.get("session"))
    kind = item.get("kind")
    if kind not in MESSAGE_KINDS:
        raise _invalid(f"unknown message kind {kind!r}")
    body = item.get("body", "")
    if not isinstance(body, str) or len(body) > api.TEXT_MAX:
        raise _invalid(f"body must be text up to {api.TEXT_MAX} characters")
    payload = item.get("payload", {})
    if not isinstance(payload, Mapping) or len(json.dumps(payload, ensure_ascii=False)) > PAYLOAD_MAX:
        raise _invalid("payload must be a small object")
    if kind == "status" and not str(payload.get("key", "")).startswith("sys.session."):
        raise _invalid("a status line of a session has a sys.session.* key")
    payload = {k: v for k, v in payload.items() if k not in ("command", "output", "session")}  # the cabinet's own marks
    if kind == "status" and payload.get("key") in ("sys.session.instructions_loaded", "sys.session.memory"):
        params = payload.get("params", {})
        payload["params"] = {**(params if isinstance(params, Mapping) else {}), "agent": "@" + session["agent"]}
    command = item.get("command") or ""
    if command:  # the turn the message belongs to; none from a host before it, or between turns
        found = _command(ctx, command)
        if found["agent"] != session["agent"] or found["thread"] != session["thread"]:
            raise _invalid("the command is not of this session")
        payload["command"] = command
    output = None
    if item.get("output") is not None:
        if kind != "tool_result":
            raise _invalid("only a tool result has output")
        output = _excerpt(item["output"])
        payload["output"] = True  # the text itself stays out of the thread
        payload["session"] = session["id"]  # where the thread reads it from
    message = ctx.store.add_thread_message(
        session["thread"],
        author=HOST if kind == "status" else f"agent:{session['agent']}",
        kind=kind,
        body=body,
        payload=payload,
        session=session["id"],
        via="session",
    )
    if output is not None:
        ctx.store.tool_output_put(session["thread"], message["seq"], session=session["id"], command=command,
                                  output=output)


_ITEMS = {"host": _item_host, "session": _item_session, "turn": _item_turn, "message": _item_message,
          "job": lambda ctx, item: jobs.report_item(ctx, item),  # late: jobs may still be loading (api's end)
          "operation": lambda ctx, item: _operations().report_item(ctx, item),
          "operation_release": lambda ctx, item: _operations().release_item(ctx, item),
          "devcheck": lambda ctx, item: _devchecks().report_item(ctx, item),
          "probe": lambda ctx, item: _llm().report_item(ctx, item)}  # a model probe's outcome


def _llm():
    from kd.cabinet import llm_api  # the cabinet's models, loaded at api's end

    return llm_api


def _operations():
    from kd.cabinet import operations  # the factory's operations, loaded at api's end

    return operations


def _devchecks():
    from kd.cabinet import devchecks  # the contours' checks, loaded at api's end

    return devchecks


def _host_report(ctx, principal, request):
    items = request.get("items")
    if not isinstance(items, list) or len(items) > REPORT_MAX:
        raise _invalid(f"items must be a list of up to {REPORT_MAX}")
    # all or nothing: a refused report is sent again item by item by the host, which drops the refused one
    with ctx.store.transaction():
        for index, item in enumerate(items):
            handler = _ITEMS.get(item.get("type")) if isinstance(item, Mapping) else None
            if handler is None:
                raise _invalid(f"item {index}: unknown report item")
            try:
                handler(ctx, item)
            except api.ApiError as exc:
                raise api.ApiError(exc.kind, f"item {index}: {exc}")
            except (store.Invalid, store.NotFound) as exc:
                raise _invalid(f"item {index}: {exc}")
            except Exception as exc:  # noqa: BLE001 — "unavailable" would make the host send it for ever
                traceback.print_exc(file=sys.stderr)
                raise _invalid(f"item {index}: {type(exc).__name__}")
    return {"applied": len(items)}


# --- commands, sessions -----------------------------------------------------------------------
def _command_enqueue(ctx, principal, request):
    agent = _agent(ctx, request.get("agent"))
    if principal.kind == "agent" and principal.agent != agent:
        raise api.ApiError("refused", "an agent queues commands only for itself")
    if not _hosted(ctx, agent):
        raise api.ApiError("refused", f"{agent} is not on the session host")
    text = _line_text(request.get("text"), "text", COMMAND_TEXT_MAX)
    key = request.get("key")
    if not isinstance(key, str) or not KEY.fullmatch(key):
        raise _invalid("key must be 1 to 64 letters, digits or :._-")
    card = request.get("card", "")
    if card:
        card = api._card(ctx, card).id
    space = request.get("space", "chat")
    if not isinstance(space, str) or space not in schema.AGENT_SPACES or (card and space != "chat"):
        raise _invalid(f"space is one of {', '.join(schema.AGENT_SPACES)}, and only without a card")
    command = commands.enqueue_signal(
        ctx.store, agent=agent, text=text, card=card, initiator=api.card_actor(principal), key=key, space=space
    )
    return {"command": command}


def _scope(ctx, principal, request):
    """(card, agent) a list is for: a card the caller sees, or an agent (people need ``agents.chat``)."""
    if "card" in request:
        return api._visible_card(ctx, principal, request.get("card")), None
    agent = _agent(ctx, request.get("agent"))
    api._require(ctx, principal, "agents.chat")
    return None, agent


def _visible_thread(ctx, principal, thread: int) -> bool:
    card = _thread_card(ctx, thread)
    return card is None or api.visible(ctx, principal, card.project)


def _command_list(ctx, principal, request):
    card, agent = _scope(ctx, principal, request)
    if card is not None:
        rows = ctx.store.commands(card=card.id, limit=LIST_MAX)
    else:
        rows = [c for c in ctx.store.commands(agent=agent, limit=LIST_MAX) if _visible_thread(ctx, principal, c["thread"])]
    return {"commands": rows}


def _session_list(ctx, principal, request):
    card, agent = _scope(ctx, principal, request)
    if card is not None:
        rows = ctx.store.sessions(thread=card.thread)
    else:
        rows = [s for s in ctx.store.sessions(agent=agent) if _visible_thread(ctx, principal, s["thread"])]
    for row in rows:  # the card behind the thread, "" for the agent's chat (kd-nudge shows BOSS which)
        card = _thread_card(ctx, row["thread"])
        row["card"] = card.id if card else ""
    return {"sessions": rows}


def _controlled_session(ctx, principal, request) -> tuple:
    session = _session(ctx, request.get("session"))
    card = _thread_card(ctx, session["thread"])
    if card is not None:
        api._visible_card(ctx, principal, card.id)
    api._require(ctx, principal, "agents.control", card.project if card else None)
    return session, card


def _session_continue(ctx, principal, request):
    session, card = _controlled_session(ctx, principal, request)
    last = ctx.store.commands(thread=session["thread"], agent=session["agent"], limit=1)
    turn_failed = session["state"] in LIVE and bool(last) and last[0]["state"] == "failed"
    if session["state"] not in RESUMABLE and not turn_failed:
        raise api.ApiError("refused", f"the session is {session['state']}, nothing to continue")
    ended = "your previous turn failed" if turn_failed else f"your previous run ended ({session['state']})"
    if card is not None:
        text = (f"[session] {card.id}: {ended}; continue where you left off"
                f" — re-read the card with kd-task view {card.id} and its thread")
    elif ctx.store.agent_space(session["thread"]) == "monitor":
        # "Monitor" resumes nothing: this wake starts a new conversation, so the wake that did not finish comes
        # with it as data (a continue that failed hands its own text on — the same wake, not one inside another)
        if turn_failed and last[0]["kind"] == "continue":
            text = last[0]["text"]
        else:
            unfinished = f"; the wake it did not finish: {last[0]['text']}" if turn_failed else ""
            text = (f"[session] monitor: {ended}{unfinished} — this is a new conversation: re-read this thread "
                    "with kd-task chat and handle it")
    else:
        text = f"[session] chat: {ended}; continue where you left off — re-read your chat with kd-task chat"
    command = ctx.store.enqueue(
        {"key": f"continue:{session['id']}:{session['updated']}", "agent": session["agent"],
         "thread": session["thread"], "card": card.id if card else "", "kind": "continue", "text": text,
         "initiator": api.card_actor(principal)}
    )
    return {"command": command}


def _session_stop(ctx, principal, request):
    session, card = _controlled_session(ctx, principal, request)
    if session["state"] not in LIVE:
        raise api.ApiError("refused", f"the session is {session['state']}, nothing to stop")
    command = ctx.store.enqueue(
        {"key": f"stop:{session['id']}:{session['updated']}", "agent": session["agent"],
         "thread": session["thread"], "card": card.id if card else "", "kind": "stop", "text": "[session] stop",
         "initiator": api.card_actor(principal)}
    )
    return {"command": command}


def _limit(request, default: int, top: int) -> int:
    value = request.get("limit", default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= top:
        raise _invalid(f"limit is 1 to {top}")
    return value


def _step(row: Mapping, calls: Mapping) -> dict:
    """One message of a turn as its step: a tool result names the tool of the call it answers; ``id`` (the
    tool's call id) pairs them."""
    kind, payload = row["kind"], row["payload"]
    result = kind == "tool_result"
    tool = payload.get("tool", "") if kind == "tool_call" else calls.get(payload.get("id"), "") if result else ""
    status = {"key": payload.get("key", ""), "params": payload.get("params", {})} if kind == "status" else None
    call = str(payload.get("id", "")) if kind in ("tool_call", "tool_result") else ""
    return {"seq": row["seq"], "kind": kind, "body": row["body"][:STEP_BODY_MAX], "tool": tool, "id": call,
            "ok": payload.get("ok") if result else None, "size": payload.get("size") if result else None,
            "output": payload.get("output") is True, "status": status}


def _session_turns(ctx, principal, request):
    """The session's turns, newest first: what started each (its command), how it went, its cost, the models
    that served it (``served``, as the usage ledger names them) and its steps (the session's messages of
    that command; a message from before the change has none)."""
    session, _ = _controlled_session(ctx, principal, request)
    turns = []
    for command in ctx.store.commands(session=session["id"], limit=_limit(request, TURNS_MAX, TURNS_MAX)):
        rows = ctx.store.turn_messages(session["thread"], session["id"], command["id"], STEPS_MAX)
        calls = {r["payload"].get("id"): r["payload"].get("tool", "") for r in rows if r["kind"] == "tool_call"}
        turns.append({
            "command": command["id"], "kind": command["kind"], "text": command["text"][:TURN_TEXT_MAX],
            "initiator": command["initiator"], "state": command["state"], "error": command["error"],
            "created": command["created"], "updated": command["updated"],
            "cost": ctx.store.turn_cost(command["id"]), "served": ctx.store.turn_models(command["id"]),
            "steps": [_step(r, calls) for r in rows],
        })
    return {"turns": turns}


def _session_output(ctx, principal, request):
    """A tool result's masked output, by its message's seq in the session's thread."""
    session, _ = _controlled_session(ctx, principal, request)
    seq = request.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise _invalid("seq is a message number")
    found = ctx.store.tool_output(session["thread"], seq, session=session["id"])
    if found is None:
        raise api.ApiError("not_found", f"no output at {seq}")
    return {"output": found}


# --- the agent chat ---------------------------------------------------------------------------
CALLER_SESSION = re.compile(r"[0-9a-f]{12}")  # the caller's session unit, as the channel read it
WAKES = {"chat": CHAT_WAKE, "monitor": MONITOR_WAKE}


def space_of(request) -> str:
    """The agent thread a person's request is for: ``chat`` (the default) or ``monitor``."""
    space = request.get("space", "chat")
    if not isinstance(space, str) or space not in schema.AGENT_SPACES:
        raise _invalid(f"space is one of {', '.join(schema.AGENT_SPACES)}")
    return space


def caller_space(ctx, agent: str, request) -> str:
    """The agent's own thread its call is about: the thread of the session it runs in (``session`` — the channel's
    12 hex characters of its unit, never the caller's word), so a "Monitor" session reads "Monitor";
    anything else — its chat. ``space: "chat"`` — the chat whatever the session (what the owner needs of
    the coordinator's attention tick in its "Monitor")."""
    if "space" in request:
        if request["space"] != "chat":
            raise _invalid("space: only chat — the session's own thread is the default")
        return "chat"
    thread = session_thread(ctx, agent, request)
    return (ctx.store.agent_space(thread) or "chat") if thread is not None else "chat"


def session_thread(ctx, agent: str, request):
    """The thread of the session the call came from (``session``: the channel's 12 hex characters of its unit);
    None — no such session of this agent."""
    session = request.get("session", "")
    if not isinstance(session, str) or not CALLER_SESSION.fullmatch(session):
        return None
    row = ctx.store._conn.execute(
        "SELECT thread FROM sessions WHERE agent=? AND substr(id, 1, 12)=?", (agent, session)
    ).fetchone()
    return row[0] if row is not None else None


def _chat_open(ctx, principal, request) -> str:
    """The agent whose chat this person may write to now: ``agents.chat`` and the agent on the session host."""
    agent = _agent(ctx, request.get("agent"))
    api._require(ctx, principal, "agents.chat")
    if not _hosted(ctx, agent):
        raise api.ApiError("refused", f"{agent} is not on the session host: its chat is not open yet")
    return agent


def _agent_check(ctx, principal, request):
    """The checks of ``user.agent.message`` alone: the web asks before it reads an upload's body."""
    return {"agent": _chat_open(ctx, principal, request)}


def _agent_message(ctx, principal, request):
    agent = _chat_open(ctx, principal, request)
    space = space_of(request)
    actor = api.card_actor(principal)
    files = api.message_files(ctx, actor, request.get("files", []))
    text = api.message_text(request.get("text"), files)
    key = request.get("client_key", "")
    if key != "" and not (isinstance(key, str) and api.CLIENT_KEY.fullmatch(key)):
        raise _invalid("client_key must be 8 to 64 letters, digits or dashes")
    command = None
    voiced = voice.audio_ids(ctx, actor, files)
    with ctx.store.transaction():
        thread = ctx.store.agent_thread(agent, space)
        message = ctx.store.add_thread_message(
            thread, author=actor, **api.message_fields(text, files, voiced), client_key=key, attachments=files
        )
        if voiced:  # the transcriptions wake the agent once they are done
            voice.enqueue(ctx, message, thread, actor)
        elif text:  # files alone wake nobody; in "Monitor" — its own session
            command = chat_wake(ctx, agent, thread, message["seq"], actor, space)
    return {"message": voice.placed(ctx.store, [message])[0], "command": command, "changed": True}


def chat_wake(ctx, agent: str, thread: int, seq: int, actor: str, space: str = "chat") -> dict:
    """The agent's wake for a person's message in its chat or "Monitor": one per message (its key). "Monitor"
    resumes no conversation, so its wake names the agent's own last text there before the message."""
    text = WAKES[space]
    if space == "monitor":
        row = ctx.store._conn.execute(
            "SELECT MAX(seq) FROM messages WHERE thread=? AND seq<? AND author=? AND kind='text'",
            (thread, seq, f"agent:{agent}"),
        ).fetchone()
        if row[0] is not None:
            text += MONITOR_LAST.format(seq=row[0])
    return ctx.store.enqueue(
        {"key": f"chat:{thread}:{seq}", "agent": agent, "thread": thread, "card": "",
         "kind": "chat", "text": text, "initiator": actor}
    )


def chat_messages(ctx, principal, request):
    """``thread.messages`` of an agent's chat or "Monitor" (``{agent, space}``): people need ``agents.chat``; an
    agent reads its own threads only (the owner's chat with one agent is not another's)."""
    agent = _agent(ctx, request.get("agent"))
    space = space_of(request)
    api._require(ctx, principal, "agents.chat")
    if principal.kind == "agent" and principal.agent != agent:
        raise api.ApiError("refused", "an agent reads its own chat only")
    thread = ctx.store.agent_thread(agent, space)
    found = api.thread_page(ctx.store, thread, request)
    return {**found, "watermark": ctx.store.last_seq(), "thread": thread}


def _own_chat(ctx, principal, request) -> str:
    agent = request.get("agent")
    return api._executor_agent(ctx, principal, request, coordinator=agent == api.worker_board.COORDINATOR[1:])


def _chat_read(ctx, principal, request):
    """An agent reads its own chat (kd-task chat, the base channel): the agent is the channel's; a "Monitor"
    session reads "Monitor"."""
    agent = _own_chat(ctx, principal, request)
    after = request.get("after", 0)
    if not isinstance(after, int) or isinstance(after, bool) or after < 0:
        raise _invalid("after must be a message seq")
    thread = ctx.store.agent_thread(agent, caller_space(ctx, agent, request))
    return {"messages": ctx.store.thread_messages(thread, after)}


def _chat_post(ctx, principal, request):
    """One message into the agent's chat (or "Monitor": its own session's) from another thread. Into the session's
    own thread never: the turn's text is posted there already, so a post would be a second message for
    one answer."""
    agent = _own_chat(ctx, principal, request)
    text = api._text(request.get("text"), "text")
    thread = ctx.store.agent_thread(agent, caller_space(ctx, agent, request))
    if session_thread(ctx, agent, request) == thread:
        raise api.ApiError("refused", "this is your session's own thread: your turn's text is posted here already — "
                                      "write the answer as your turn's text, not with kd-task chat --post")
    api._agent_rate_limit(ctx, agent, "chat")
    with ctx.store.transaction():
        message = ctx.store.add_thread_message(thread, author=f"agent:{agent}", kind="text", body=text)
    return {"message": message}


def _usage_day(ctx, principal, request):
    """Token usage of a day window (kd-usage, root): per agent and model, with each agent's first stamp."""
    start, end = request.get("start"), request.get("end")
    try:
        if not isinstance(start, str) or not isinstance(end, str) or start >= end:
            raise ValueError
        return usage.day(ctx.store, start, end)
    except ValueError:
        raise _invalid("start and end are UTC times YYYY-MM-DDTHH:MM:SSZ, start before end")


def chat_event(event) -> bool:
    """An event of an agent's own space (its chat, its sessions and commands outside cards)."""
    return (
        not event.get("card")
        and bool((event.get("payload") or {}).get("agent"))
        and str(event.get("kind", "")).startswith(("message.", "command.", "run.", "session."))
    )


OPS = {
    "host.report": (frozenset({"root"}), _host_report),
    "command.enqueue": (frozenset({"root", "executor", "agent"}), _command_enqueue),
    "command.list": (api.READERS, _command_list),
    "session.list": (api.READERS, _session_list),
    "user.session.continue": (frozenset({"user"}), _session_continue),
    "user.session.stop": (frozenset({"user"}), _session_stop),
    "session.turns": (frozenset({"user"}) | api.ROOTISH, _session_turns),
    "session.output": (frozenset({"user"}) | api.ROOTISH, _session_output),
    "user.agent.message": (frozenset({"user"}), _agent_message),
    "user.agent.check": (frozenset({"user"}), _agent_check),
    "agent.chat.read": (frozenset({"executor"}), _chat_read),
    "agent.chat.post": (frozenset({"executor"}), _chat_post),
    "usage.day": (frozenset({"root"}), _usage_day),
}
api.OPS.update(OPS)  # the board API serves them (api imports this module at its end)
