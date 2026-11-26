"""The cabinet's dispatcher: queued commands reach the session host.

A tick leases due commands (a lease keeps two overlapping ticks from sending one twice), re-checks each under
the service lock (a card closed or reassigned meanwhile cancels its command with a reason), sends it to the host
outside the lock and writes the answer back: delivered, a retry after the next backoff step while the host is
away, or failed (refused by the host, still undelivered after a day, or a card's command outside its card's thread —
the session a delivery names is the thread's, its context the command's own fields, never its text). The
host is idempotent by command id, so
a command sent again after a lost answer runs once. Every delivery carries the session's runtime id for the host
to resume, except one to an agent's "Monitor": each of its wakes starts a new Claude conversation (the host still
writes a wake that comes while the process lives into that process). A delivered command that has not started
(it waits at the host for a slot or behind a turn) is checked again each tick, and the host gives it back when its card moved on.
A new ``command.queued`` event wakes the loop at once. An ``urgent`` command (the wake of an approved
critical command) goes with ``urgent`` true, and once more without it to a host that does not know the field yet.
A card's command of a project with a memory bank names it as ``memory``, the same way. A card's command on
a cabinet model (the card's, else its project's) names it as ``model`` and the effort as ``effort`` — those
are never dropped for an older host: a session the owner put on a model is not run on the subscription behind their
back, the command fails ``host_outdated`` until the host restarts. A disabled model or a kind without its key on the
host fails the command before it is sent (``model_disabled``, ``no_key``), with a status line in the card's thread.

Worker jobs take the same way in their own table: a queued job is leased, re-checked (kd-worker's
admission: its card, assignee and status) and sent as ``job``; a delivered one that has not started is checked
again each tick and taken back the cancel's way when its card moved on; a cancelling one is sent ``job_cancel``
each tick until the host gives it back or reports its end. Every ``SWEEP_S`` the host's status is compared with
the jobs it should hold: one it neither holds nor is reporting for ``LOST_S`` ends ``host_lost_it`` (its end
report was refused or dropped), so no job holds its card's writer for ever. A new ``job.queued`` or
``job.cancelling`` event wakes the loop too.

Factory operations go one at a time: a due pending one is re-checked (its grant, the deployed revision)
and sent as ``operation``; the one in flight is asked again every ``SWEEP_S`` and the host's answer applies as its
report (``unknown`` — the host lost it — fails it for reconcile); an absent or busy host is tried again with the
backoff, a refusal fails the operation. One whose grant was revoked while its roles drain is cancelled on the host
instead of polled: nothing is installed yet; an install under way is never cut.

Checks (``dev.check``) keep a lane per contour of the release tree, apart from the factory's: an exclusive
contour takes one at a time, another up to its ``parallel``. A flagged check is sent ``devcheck_cancel`` each tick
until it ends; one the host has is polled every ``SWEEP_S``; one whose contour is gone ends before it is sent.

A model probe the owner requested is sent as ``probe`` under an id the cabinet keeps on the model: a busy
or absent host is asked again next tick (the same id — a host that took it answers ``running``), a refusal is the
probe's result, the host's ack marks it running and its ``probe`` report ends it; one neither taken nor reported in
``PROBE_TIMEOUT`` ends lost. A kind without its key on the host fails before the host is asked.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import re
import sys
import traceback
from typing import Optional
import uuid

from kd import contours, naming
from kd.cabinet import api, commands, devchecks, egress, jobs, llm_api, operations, sessions_api

HOST_SOCKET = "/run/digipaddock-session-host/host.sock"
BACKOFF = (2, 5, 15, 30, 60, 120, 300)
LEASE_S = 60
MAX_AGE = timedelta(hours=24)
BATCH = 20
ANSWER_MAX = 1 << 16
CONTEXT_MISMATCH = "context_mismatch"  # a command whose card is not its thread's: never sent
SWEEP_S = 30  # how often the cabinet's active jobs are compared with the host's
LOST_S = 60  # a job the host neither holds nor reports for this long is lost (two sweeps at least)
OPERATION_TIMEOUT = 3600  # the host's drain of a role at work before it is skipped (the others go on)
PROBE_TIMEOUT = 300  # a model probe the host took and did not report in this long is lost
HEX32 = re.compile(r"[0-9a-f]{32}")
_STAMP = "%Y-%m-%dT%H:%M:%SZ"


class HostUnavailable(Exception):
    pass


class HostRefused(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


class HostClient:
    """The session host's socket: one JSON line each way per request."""

    def __init__(self, path: str = HOST_SOCKET):
        self.path = path

    async def call(self, request: dict, timeout: float = 10.0) -> dict:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(self.path, limit=ANSWER_MAX), timeout
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise HostUnavailable(f"{self.path}: {exc.__class__.__name__}") from None
        try:
            writer.write(json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n")
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout)
            answer = json.loads(line.decode("utf-8"))
        except (OSError, asyncio.TimeoutError, ValueError, asyncio.LimitOverrunError) as exc:
            raise HostUnavailable(f"no answer ({exc.__class__.__name__})") from None
        finally:
            writer.close()
        if not isinstance(answer, dict):
            raise HostUnavailable("the answer is not an object")
        if not answer.get("ok"):
            raise HostRefused(str(answer.get("error", "refused")), str(answer.get("message", "")))
        return answer


def _stamp(when: datetime) -> str:
    return when.strftime(_STAMP)


def _age(command: dict, now: datetime) -> timedelta:
    created = datetime.strptime(command["created"], _STAMP).replace(tzinfo=timezone.utc)
    return now - created


def _prepare(svc, command: dict, now: datetime) -> Optional[dict]:
    """The request for a leased command, or None when it is not to be sent (cancelled, failed, moved on)."""
    store = svc.ctx.store
    command = store.command(command["id"])
    if command["state"] != "queued":
        return None  # the host reported its turn already, or a person cancelled it
    reason = commands.stale_reason(store, command, svc.ctx.host_config().hosted,
                                   commands.grant_check(svc.ctx, now.timestamp()))
    if reason:
        store.command_state(command["id"], "cancelled", reason=reason)
        return {"_outcome": "cancelled"}
    if _age(command, now) > MAX_AGE:
        store.command_state(command["id"], "failed", reason="delivery_expired", error=command["error"])
        return {"_outcome": "failed"}
    if command["card"] != store.thread_info(command["thread"])["card"]:  # a card's work in its thread only
        store.command_state(command["id"], "failed", reason=CONTEXT_MISMATCH,
                            error=f"команда для {command['card'] or 'чата'} в треде {command['thread']} другой карточки")
        return {"_outcome": "failed"}
    session = store.session_for(command["thread"], command["agent"])
    if command["kind"] == "stop":
        return {"op": "stop", "session": session["id"], "command": command["id"]}
    space = store.agent_space(command["thread"])
    request = {
        "op": "deliver",
        "command": command["id"],
        "session": session["id"],
        "agent": command["agent"],
        "thread": command["thread"],
        "card": command["card"],
        "text": command["text"],
        # "Monitor" resumes nothing: a wake after its process closed is a new Claude conversation, whatever
        # its earlier wakes held — they reach it as data (the wake, kd-task chat), not as weeks of context
        "resume": "" if space == "monitor" else session["runtime_session_id"],
    }
    if command["kind"] == "urgent":
        request["urgent"] = True  # the host starts it at once, over the agent's slots
    if space not in ("", "chat"):
        request["space"] = space  # the session of the agent's "Monitor" launches as that thread's
    if command["card"]:
        record = store.card(command["card"])
        project = store.project(record.project) or {}
        if project.get("memory_bank"):
            request["memory"] = project["memory_bank"]  # the card's session on its project's bank
        row, effort = llm_api.effective(store, record)  # the card's session on its model and effort
        if row is not None:
            if not row["enabled"]:
                return _failed_here(svc, command, session, "model_disabled", *_model_disabled(row["id"]))
            if llm_api.missing_key(svc.ctx, row):
                return _failed_here(svc, command, session, "no_key", *_no_key(row["provider"]))
            request["model"] = llm_api.spec_of(row)
        if effort:
            request["effort"] = effort
        exit_name, gone = egress.choose(svc.ctx, record, command["agent"])  # the autonomy design, piece 2
        if exit_name:
            request["egress"] = exit_name
        if gone:  # the card's country has no exit on this host now — the session goes without it
            sessions_api._status_line(svc.ctx, session, *_country_gone(gone))
    return request


def _country_gone(country: str) -> tuple:
    label = egress.config.LABEL + country.lower()
    return ("sys.session.egress_country_gone", {"label": label},
            f"the host has no exit of the card's {label} now: the session runs with the standing exit")


def _model_disabled(model: str) -> tuple:
    return ("sys.session.model_disabled", {"model": model},
            f"the model {model} is disabled: enable it or change the card's model")


def _no_key(provider: str) -> tuple:
    command = naming.command("kd-secret")
    return ("sys.session.no_key", {"provider": provider, "command": command},
            f"no key of the {provider} provider on the host: put it there with {command} put llm-{provider}")


HOST_OUTDATED = ("sys.session.host_outdated", {},
                 "the session host does not know the card's model yet: it restarts after the release")


def _failed_here(svc, command: dict, session: dict, reason: str, key: str, params: dict, body: str) -> dict:
    """A command that ends in the cabinet, with a status line in its thread saying why."""
    svc.ctx.store.command_state(command["id"], "failed", reason=reason, error=body[:300])
    sessions_api._status_line(svc.ctx, session, key, params, body)
    return {"_outcome": "failed"}


NEWER_FIELDS = ("urgent", "space", "memory", "egress")  # fields an older host may not know, in the order they came; ``egress`` came with the
# probe exits
MODEL_FIELDS = ("model", "effort")  # never dropped — see ``_refusal``


def _ordinary(request: dict, exc: Exception) -> Optional[dict]:
    """A host older than ``urgent``, ``space`` or ``memory`` refuses the field — the same
    delivery without the newest of them, so the command still reaches the agent's queue as before (a session without
    ``memory`` keeps the role bank, an ``urgent`` one stays urgent on a host that knows it); None for any other
    refusal. The host does not name the field, hence one field a retry. A delivery with a model or an effort is
    never retried without them: an unknown field there fails it as ``host_outdated``."""
    newer = [k for k in NEWER_FIELDS if k in request]
    unknown = isinstance(exc, HostRefused) and exc.kind == "invalid" and "unknown field" in str(exc)
    if unknown and "egress" in request:  # a host older than probe exits: the session runs without one
        return {k: v for k, v in request.items() if k != "egress"}
    if any(k in request for k in MODEL_FIELDS):
        return None
    if newer and isinstance(exc, HostRefused) and exc.kind == "invalid" and "unknown field" in str(exc):
        return {k: v for k, v in request.items() if k != newer[-1]}
    return None


def _refusal(request: dict, exc: HostRefused) -> tuple:
    """The reason a refused delivery fails with, and the status line for the card's thread (None: no line).
    an unknown field of a delivery with a model or an effort is a host older than them; ``no_key: <kind>`` is the
    host finding the kind's key file gone. a host that refuses the model's spec itself (``model: …`` — it
    knows no subscription models yet) is older than the spec too."""
    text = str(exc)
    if exc.kind == "invalid" and any(k in request for k in MODEL_FIELDS) and (
            "unknown field" in text or ("model" in request and text.startswith("model:"))):
        return "host_outdated", HOST_OUTDATED
    if exc.kind == "refused" and str(exc).startswith("no_key:"):
        return "no_key", _no_key(str(exc).partition(":")[2].strip())
    return "host_refused", None


def _delivered(svc, command: dict, request: dict, answer: dict):
    store = svc.ctx.store
    command = store.command(command["id"])
    if command["state"] == "queued":
        store.command_state(command["id"], "delivered", session=request["session"])
        if command["kind"] == "stop":
            store.command_state(command["id"], "done")
    if answer.get("state") == "queued":
        session = store.session(request["session"])
        if session["state"] not in ("starting", "working", "idle", "queued"):
            position = answer.get("position")  # its place in the agent's queue for a slot
            ok = isinstance(position, int) and not isinstance(position, bool) and 1 <= position <= sessions_api.POSITION_MAX
            fields = {"queue_position": position} if ok else {}
            store.session_update(session["id"], state="queued", queued_since=svc.ctx.now(), **fields)


def _retry(svc, command: dict, now: datetime, error: str):
    store = svc.ctx.store
    command = store.command(command["id"])
    if command["state"] != "queued":
        return
    step = BACKOFF[min(max(command["attempts"], 1), len(BACKOFF)) - 1]
    store.command_state(command["id"], "queued", next_try=_stamp(now + timedelta(seconds=step)), error=error)


def _fail(svc, command: dict, reason: str, error: str, line: Optional[tuple] = None, session: str = ""):
    store = svc.ctx.store
    if store.command(command["id"])["state"] == "queued":
        store.command_state(command["id"], "failed", reason=reason, error=error[:300])
        if line:
            sessions_api._status_line(svc.ctx, store.session(session), *line)


def _stale_delivered(svc, now: datetime) -> list:
    """Delivered commands not started yet whose card moved on (they wait at the host for a slot or a turn)."""
    store, hosted = svc.ctx.store, svc.ctx.host_config().hosted
    grant = commands.grant_check(svc.ctx, now.timestamp())
    out = []
    for command in store.commands(states=("delivered",), limit=200):
        if command["kind"] != "stop":
            reason = commands.stale_reason(store, command, hosted, grant)
            if reason:
                out.append((command, reason))
    return out


def _cancel_delivered(svc, command: dict, reason: str):
    store = svc.ctx.store
    if store.command(command["id"])["state"] == "delivered":
        store.command_state(command["id"], "cancelled", reason=reason)


async def _recheck(svc, host, out: dict, now: datetime):
    """Remediation §4.2: the conditions hold until the run starts — the host gives back what has not started."""
    for command, reason in await svc.locked(lambda: _stale_delivered(svc, now)):
        try:
            answer = await host.call({"op": "cancel", "session": command["session"], "command": command["id"]})
        except (HostUnavailable, HostRefused):
            continue  # the next tick asks again
        if answer.get("cancelled"):
            await svc.locked(lambda: _cancel_delivered(svc, command, reason))
            out["cancelled"] += 1


async def _sweep(svc, host, now: datetime, out: dict):
    """Jobs the host should hold but neither holds nor is reporting, for ``LOST_S``, end as lost; the view of what
    is missing lives in memory (a restart of the cabinet starts it over)."""
    state = svc.job_sweep
    if state.get("at") and now - state["at"] < timedelta(seconds=SWEEP_S):
        return
    active = await svc.locked(lambda: jobs.at_host(svc.ctx.store))
    missing = state.setdefault("missing", {})
    if not active:
        missing.clear()
        return
    try:
        status = await host.call({"op": "status"})
    except (HostUnavailable, HostRefused):
        return  # the next tick asks again
    state["at"] = now
    live = {j.get("job") for j in status.get("jobs", ()) if isinstance(j, dict)} | set(status.get("reporting", ()))
    for job_id in list(missing):
        if job_id not in active or job_id in live:
            del missing[job_id]
    for job_id in active:
        if job_id in live:
            continue
        if now - missing.setdefault(job_id, now) >= timedelta(seconds=LOST_S):
            del missing[job_id]
            outcome = await svc.locked(lambda: jobs.lost(svc.ctx, job_id))
            if outcome:
                out[outcome] += 1


async def _egress_sweep(svc, host, now: datetime):
    """The autonomy design, piece 2: a live process holding a probe exit whose card no longer grants it is cut by
    the host's ``revoke`` (its consent ended mid-turn or while it idled); the session goes on without the exit.
    a session that waits for a slot is in the status with the exit it would start with, and its revoke
    takes that exit off it. Every ``SWEEP_S``."""
    state = svc.egress_sweep
    if state.get("at") and now - state["at"] < timedelta(seconds=SWEEP_S):
        return
    if not egress.configured(svc.ctx):
        return
    try:
        status = await host.call({"op": "status"})
    except (HostUnavailable, HostRefused):
        return  # the next tick asks again
    state["at"] = now
    held = [s for s in status.get("sessions", ()) if isinstance(s, dict) and s.get("egress")]
    revokes = await svc.locked(lambda: egress.ended(svc.ctx, held)) if held else []
    for request in revokes:
        try:
            await host.call(request)
        except (HostUnavailable, HostRefused):
            continue  # an older host: the next delivery without the exit still closes the process; else next sweep


async def _jobs(svc, host, now: datetime, out: dict):
    ctx = svc.ctx
    await svc.locked(lambda: jobs.take_back_stale(ctx))
    for job in await svc.locked(lambda: jobs.cancelling(ctx.store)):
        try:
            answer = await host.call({"op": "job_cancel", "job": job["id"]})
        except (HostUnavailable, HostRefused):
            continue  # the next tick asks again
        if await svc.locked(lambda: jobs.host_cancelled(ctx, job["id"], answer.get("cancelled"))):
            out["cancelled"] += 1
    await _sweep(svc, host, now, out)
    leased = await svc.locked(
        lambda: jobs.lease(ctx.store, _stamp(now), _stamp(now + timedelta(seconds=LEASE_S)), BATCH)
    )
    for job in leased:
        request = await svc.locked(lambda: jobs.prepare(ctx, job["id"], _stamp(now - MAX_AGE)))
        if request is None:
            continue
        if "_outcome" in request:
            out[request["_outcome"]] += 1
            continue
        try:
            await host.call(request)
        except HostUnavailable:
            step = BACKOFF[min(max(job["attempts"], 1), len(BACKOFF)) - 1]
            await svc.locked(lambda: jobs.retry(ctx, job["id"], _stamp(now + timedelta(seconds=step))))
            out["retried"] += 1
            continue
        except HostRefused as exc:
            await svc.locked(lambda: jobs.refused(ctx, job["id"], f"{exc.kind}: {exc}"))
            out["failed"] += 1
            continue
        await svc.locked(lambda: jobs.delivered(ctx, job["id"]))
        out["delivered"] += 1


async def _operations(svc, host, now: datetime, out: dict):
    ctx = svc.ctx
    polled = svc.operation_poll
    current = await svc.locked(lambda: operations.in_flight(ctx.store))
    if current is not None:
        at = polled.get(current["id"])
        if at is not None and now - at < timedelta(seconds=SWEEP_S):
            return
        polled.clear()
        polled[current["id"]] = now
        why = await svc.locked(lambda: operations.cancel_reason(ctx, current))
        request = (operations.host_cancel(current, why) if why
                   else operations.host_request(current, OPERATION_TIMEOUT, start=False))
        try:
            answer = await host.call(request)
        except (HostUnavailable, HostRefused):
            return  # asked again at the next sweep
        await svc.locked(lambda: operations.answered(ctx, current["id"], answer))
        return
    due = await svc.locked(lambda: operations.next_due(ctx.store, _stamp(now)))
    if due is None:
        return
    op = await svc.locked(lambda: operations.prepare(ctx, due["id"]))
    if op is None:
        out["failed"] += 1  # it ended here: its grant or its revision moved
        return
    try:
        answer = await host.call(operations.host_request(op, OPERATION_TIMEOUT))
    except HostUnavailable:
        answer = {"state": "busy"}
    except HostRefused as exc:
        await svc.locked(lambda: operations.refused(ctx, op["id"], f"{exc.kind}: {exc}"))
        out["failed"] += 1
        return
    if answer.get("state") == "busy":  # away, or still on an operation the cabinet no longer counts
        step = BACKOFF[min(op["attempts"] + 1, len(BACKOFF)) - 1]
        await svc.locked(lambda: operations.retry(ctx, op["id"], _stamp(now + timedelta(seconds=step))))
        out["retried"] += 1
        return
    polled.clear()
    polled[op["id"]] = now
    await svc.locked(lambda: operations.delivered(ctx, op["id"], answer))
    out["delivered"] += 1


async def _devchecks(svc, host, now: datetime, out: dict):
    """A lane per contour: cancels first, a poll of each check the host has every SWEEP_S, then the due
    ones while the contour has room (``contours.capacity``). An absent or busy host backs the check off."""
    ctx, polled = svc.ctx, svc.devcheck_poll
    try:
        found = ctx.contours()
    except api.ApiError:
        return  # a broken contours.json: nothing to dispatch to until a release fixes it
    for op in await svc.locked(lambda: devchecks.orphans(ctx.store, found)):
        await svc.locked(lambda: devchecks.refused(ctx, op["id"], f"the contour {op['params']['contour']} is gone"))
        out["failed"] += 1
    for op in await svc.locked(lambda: devchecks.cancelling(ctx.store)):
        try:
            answer = await host.call(devchecks.host_cancel(op))
        except (HostUnavailable, HostRefused):
            continue  # the next tick asks again
        polled[op["id"]] = now
        await svc.locked(lambda: devchecks.answered(ctx, op["id"], answer))
    for name, contour in sorted(found.items()):
        current = await svc.locked(lambda: devchecks.in_flight(ctx.store, name))
        for op in current:
            at = polled.get(op["id"])
            if at is not None and now - at < timedelta(seconds=SWEEP_S):
                continue
            polled[op["id"]] = now
            try:
                answer = await host.call(devchecks.host_request(op, start=False))
            except (HostUnavailable, HostRefused):
                continue  # asked again at the next sweep
            await svc.locked(lambda: devchecks.answered(ctx, op["id"], answer))
        room = contours.capacity(contour) - len(await svc.locked(lambda: devchecks.in_flight(ctx.store, name)))
        while room > 0:
            op = await svc.locked(lambda: devchecks.next_due(ctx.store, name, _stamp(now)))
            if op is None:
                break
            try:
                answer = await host.call(devchecks.host_request(op, start=True))
            except HostUnavailable:
                answer = {"state": "busy"}
            except HostRefused as exc:
                await svc.locked(lambda: devchecks.refused(ctx, op["id"], f"{exc.kind}: {exc}"))
                out["failed"] += 1
                continue
            if answer.get("state") == "busy":
                step = BACKOFF[min(op["attempts"] + 1, len(BACKOFF)) - 1]
                await svc.locked(lambda: devchecks.retry(ctx, op["id"], _stamp(now + timedelta(seconds=step))))
                out["retried"] += 1
                break  # the host is away or full: the rest of the contour waits with it
            polled[op["id"]] = now
            await svc.locked(lambda: devchecks.delivered(ctx, op["id"], answer))
            out["delivered"] += 1
            room -= 1
    live = await svc.locked(lambda: {op["id"] for name in found for op in devchecks.in_flight(ctx.store, name)})
    for check_id in [k for k in polled if k not in live]:  # ended: its poll time goes with it
        del polled[check_id]


def _probe_lost(at: str, now: datetime) -> bool:
    try:
        when = datetime.strptime(at, _STAMP).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return True
    return now - when > timedelta(seconds=PROBE_TIMEOUT)


def _probe_due(svc, now: datetime) -> list:
    """``(model id, spec, probe id)`` of the probes to send: each requested one (given its id once), after the ones
    the host cannot run end here — the kind's key is not on the host, or the probe is older than ``PROBE_TIMEOUT``."""
    ctx, due = svc.ctx, []
    subscription = ctx.store.get_state("subscription_probe") or {}
    if subscription.get("state") in ("requested", "running"):
        if _probe_lost(subscription.get("at"), now):
            llm_api.probe_done(ctx, llm_api.SUBSCRIPTION_PROBE, False, "", 0, "no result from the session host", actor="system")
        elif subscription["state"] == "requested":
            probe_id = subscription.get("id") or uuid.uuid4().hex
            with ctx.store.transaction():
                ctx.store.set_state("subscription_probe", {**subscription, "id": probe_id})
            due.append((llm_api.SUBSCRIPTION_PROBE, None, probe_id))
    for row in llm_api.rows(ctx.store):  # the subscription's models are probed like the cabinet's own
        probe = row["probe"]
        state = probe.get("state")
        if state not in ("requested", "running"):
            continue
        if _probe_lost(probe.get("at"), now):
            llm_api.probe_done(ctx, row["id"], False, "", 0, "no result from the session host", actor="system")
        elif llm_api.missing_key(ctx, row):
            llm_api.probe_done(ctx, row["id"], False, "", 0, llm_api.NO_KEY.format(provider=row["provider"]),
                               actor="system")
        elif state == "requested":
            probe_id = probe.get("id") if isinstance(probe.get("id"), str) and HEX32.fullmatch(probe["id"]) else ""
            if not probe_id:
                probe_id = uuid.uuid4().hex
                llm_api.set_probe(ctx.store, row["id"], {**probe, "id": probe_id}, actor="system")
            due.append((row["id"], llm_api.spec_of(row), probe_id))
    return due


def _probe_running(svc, model_id: str, probe_id: str, now: datetime):
    if model_id == llm_api.SUBSCRIPTION_PROBE:
        held = svc.ctx.store.get_state("subscription_probe") or {}
        if held.get("id") == probe_id and held.get("state") == "requested":
            with svc.ctx.store.transaction():
                svc.ctx.store.set_state("subscription_probe", {**held, "state": "running", "at": _stamp(now)})
        return
    row = llm_api.row(svc.ctx.store, model_id)
    if row is not None and row["probe"].get("state") == "requested" and row["probe"].get("id") == probe_id:
        llm_api.set_probe(svc.ctx.store, model_id, {"state": "running", "at": _stamp(now), "id": probe_id},
                          actor="system")


async def _probes(svc, host, now: datetime):
    for model_id, spec, probe_id in await svc.locked(lambda: _probe_due(svc, now)):
        try:
            await host.call({"op": "probe", "probe": probe_id, "model": spec})
        except HostUnavailable:
            continue  # next tick
        except HostRefused as exc:
            if exc.kind == "refused" and str(exc) == "busy":
                continue
            if exc.kind == "refused" and str(exc).startswith("no_key:"):
                detail = llm_api.NO_KEY.format(provider=str(exc).partition(":")[2].strip())
            elif exc.kind == "invalid" and "unknown operation" in str(exc):
                detail = f"the session host does not know the probe yet: it restarts after the release ({exc})"
            else:
                detail = f"{exc.kind}: {exc}"
            await svc.locked(lambda: llm_api.probe_done(svc.ctx, model_id, False, "", 0, detail, actor="system"))
            continue
        await svc.locked(lambda: _probe_running(svc, model_id, probe_id, now))


async def tick(svc, *, host, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    out = {"delivered": 0, "retried": 0, "cancelled": 0, "failed": 0}
    await _recheck(svc, host, out, now)
    await _jobs(svc, host, now, out)
    await _operations(svc, host, now, out)
    await _devchecks(svc, host, now, out)
    await _probes(svc, host, now)
    await _egress_sweep(svc, host, now)
    leased = await svc.locked(
        lambda: svc.ctx.store.lease_commands(_stamp(now), _stamp(now + timedelta(seconds=LEASE_S)), BATCH)
    )
    for command in leased:
        request = await svc.locked(lambda: _prepare(svc, command, now))
        if request is None:
            continue
        if "_outcome" in request:
            out[request["_outcome"]] += 1
            continue
        try:
            while True:
                try:
                    answer = await host.call(request)
                    break
                except HostRefused as exc:
                    ordinary = _ordinary(request, exc)
                    if ordinary is None:
                        raise
                    request = ordinary
        except HostUnavailable:
            await svc.locked(lambda: _retry(svc, command, now, "host_unavailable"))
            out["retried"] += 1
            continue
        except HostRefused as exc:
            reason, line = _refusal(request, exc)
            await svc.locked(lambda: _fail(svc, command, reason, f"{exc.kind}: {exc}", line, request["session"]))
            out["failed"] += 1
            continue
        await svc.locked(lambda: _delivered(svc, command, request, answer))
        out["delivered"] += 1
    return out


async def run(svc, *, host, interval: float = 2.0, clock=None):
    """Tick every ``interval`` seconds, or at once when a write queued a command (``svc.kick``)."""
    while True:
        svc.kick.clear()
        try:
            await tick(svc, host=host, now=clock() if clock else None)
        except Exception:  # noqa: BLE001 — the loop outlives one bad tick; the journal has the traceback
            traceback.print_exc(file=sys.stderr)
        try:
            await asyncio.wait_for(svc.kick.wait(), interval)
        except asyncio.TimeoutError:
            pass
