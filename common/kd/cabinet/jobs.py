"""Worker jobs in the cabinet: the job's record, its way to the session host and its outcome.

kd-worker (the coordinator's executor, or root) admits a job — the card, the agent's type, the registry of
specializations (``kd.worker_jobs.admit``) — and submits its whole definition here. The cabinet keeps one active
writer per card and the day limits worker-slots.json names; its dispatcher hands a queued job to the session host,
which runs it in one of the agent's worker slots (a pool apart from the agent's sessions). The host reports the job
running and its end; the cabinet then comments the card as the job's agent, logs the usage and wakes the requester
with one line in the card's thread. A job never runs twice: one the host may have started before it restarted
fails ``host_restart``, one it only held goes back to the queue.

    queued -> delivered -> running -> done | failed | no-result | cancelled
    queued -> cancelled            delivered | running -> cancelling -> cancelled (or its outcome, if it came first)
"""

import json
import re
import sys
from typing import Mapping, Optional

from kd import agents, worker_board, worker_inputs, worker_jobs
from kd.cabinet import api, commands, schema, store, usage, users

JOB_ID = re.compile(r"[0-9a-f]{32}")
SPEC = re.compile(r"[a-z][a-z0-9-]{0,31}")
SLOT = re.compile(r"[a-z][a-z0-9_-]{0,31}/worker[1-9]")
CALLER = re.compile(r"[a-z_][a-z0-9_-]{0,31}")  # the requester's Unix user, or root
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
FIELDS = frozenset({"id", "card", "agent", "type", "repo", "spec", "skills", "prompt", "limits", "brief", "caller",
                    "requester", "writer", "timeout", "model"})  # the input; the stored one adds model_spec, effort
WRITES = ("none", "temp", "repo")
DEFINITION_MAX = 48 * 1024
RESULT_MAX = 64 * 1024
REASON_MAX = 500
SKILLS_MAX = 10
TIMEOUT = (60, 14400)
LIST_MAX = 50
ACTIVE = ("queued", "delivered", "running", "cancelling")
TERMINAL = ("done", "failed", "no-result", "cancelled")
COMMENT_MAX = worker_board.TEXT_MAX - 64  # the signature and a cut leave room
HOST = "system"  # the actor of what the host reports and the dispatcher decides
CANCELLED_NOTE = "Сессия отменена."
RESTART_NOTE = "Рабочая сессия прервана: хост сессий перезапустился."
LOST_NOTE = "Рабочая сессия потеряна: хост сессий не сообщил её итог."
HOST_RESTART, HOST_REFUSED, HOST_LOST, EXPIRED = "host_restart", "host_refused", "host_lost_it", "delivery_expired"
NOT_APPROVED = "card_not_approved"
AT_HOST = ("delivered", "running", "cancelling")
# the caller of a job the cabinet starts itself (a council's critic) — no Unix user, no requester to
# wake, no comment on the card: its start and its end go to the council (``council_rounds``)
CABINET = "cabinet"
HEAD_MISSING = "head_missing"  # how the reason of a failed job starts when the host's copy has not got its pull
assert set(ACTIVE + TERMINAL) == set(schema.JOB_STATES)


def _invalid(message):
    return api.ApiError("invalid", message)


# --- rows -------------------------------------------------------------------------------------
def row(s, job_id: str) -> Optional[dict]:
    found = s._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if found is None:
        return None
    out = dict(found)
    out["data"], out["writer"] = json.loads(out["data"]), bool(out["writer"])
    return out


def view(job: Mapping) -> dict:
    """What kd-worker and the card's page show of a job: never its brief, prompt or caller."""
    definition = job["data"]["definition"]
    return {
        "id": job["id"], "card": job["card"], "agent": job["agent"], "spec": job["spec"],
        "skills": list(definition.get("skills", [])), "write": definition.get("limits", {}).get("write", "none"),
        "state": job["state"], "slot": job["slot"], "reason": job["reason"], "created": job["created"],
        "updated": job["updated"], "started": job["started"], "ended": job["ended"],
        "result": job["data"].get("result"), "usage": job["data"].get("usage"),
    }


def _set(s, job_id: str, **fields) -> dict:
    if "data" in fields:
        fields["data"] = json.dumps(fields["data"], ensure_ascii=False, sort_keys=True)
    fields["updated"] = s._clock()
    sets = ", ".join(f"{k}=?" for k in fields)
    s._conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), job_id))
    return row(s, job_id)


def _event(s, actor: str, kind: str, job: Mapping, **payload):
    try:
        thread = s.card(job["card"]).thread
    except store.NotFound:
        thread = None
    s._event(actor, kind, job["card"], {"job": job["id"], "agent": job["agent"], "state": job["state"], **payload},
             thread)


# --- what the cabinet writes on the card ------------------------------------------------------
def _comment(ctx, job: Mapping, text: str):
    """Best effort: one comment as the job's agent; a refused one is a journal line and never changes the job."""
    author = "@" + job["agent"]
    try:
        with ctx.store.transaction():
            card = ctx.store.card(job["card"])
            body = worker_board.signed(job["agent"], worker_board._text(text[:COMMENT_MAX]))
            ctx.store.edit_card(card.id, card.revision, {"comment": {"text": body, "author": author}},
                                actor=store.actor_of(author))
    except (store.NotFound, store.Conflict, store.Invalid, worker_board.Refused) as exc:
        sys.stderr.write(f"kd-cabinet: job {job['id']}: no comment on {job['card']} ({exc})\n")


def _wake(ctx, job: Mapping):
    """The requester's one line about the outcome: in the card's thread, or its chat when the card is gone."""
    try:
        card = ctx.store.card(job["card"]).id
    except store.NotFound:
        card = ""
    text = (f"[worker] job {job['id']} {job['state']} card {job['card']} spec {job['spec']}:"
            f" run kd-worker result --job {job['id']}")
    commands.enqueue_signal(ctx.store, agent=job["requester"], text=text, card=card, initiator=HOST,
                            key=f"job:{job['id']}:end")


def _kept(data: Mapping) -> dict:
    """A job's data once it ended: what its worker read (``inputs``) is not kept — the brief's version,
    the thread and the council's record it was cut from stay where they are."""
    return {key: value for key, value in data.items() if key != "inputs"}


def _finish(ctx, job: Mapping, state: str, *, reason: str, comment: str = "", used=None, result=None,
            actor: str = HOST) -> dict:
    """The job's end: its record, the comment, the usage, the requester's wake (not for a cancel), the event."""
    s, now = ctx.store, ctx.store._clock()
    with s.transaction():
        data = {**_kept(job["data"]), "result": result, "usage": used}
        done = _set(s, job["id"], state=state, reason=reason[:REASON_MAX], data=data, ended=now, lease_until="")
        own = done["caller"] == CABINET
        text = CANCELLED_NOTE + ("\n" + comment if comment else "") if state == "cancelled" else comment
        if text and not own:
            _comment(ctx, done, text)
        if used is not None:
            spec = done["data"]["definition"].get("model_spec")  # a job on a cabinet model
            prices = s.llm_model(spec["id"]) if isinstance(spec, Mapping) and spec.get("id") else None
            usage.record_job(s, done, used, now, prices=prices,
                             model=spec["id"] if isinstance(spec, Mapping) and spec.get("id") else "")
        if state != "cancelled" and done["requester"]:
            _wake(ctx, done)
        _event(s, actor, "job.finished", done, reason=done["reason"])
        if own:
            _council(ctx, "on_job_end", done)
        return done


def _cancel_here(ctx, job: Mapping, reason: str, actor: str = HOST) -> dict:
    """A job that never ran (queued here, or given back by the host) is cancelled with no comment or wake."""
    with ctx.store.transaction():
        done = _set(ctx.store, job["id"], state="cancelled", reason=reason[:REASON_MAX], ended=ctx.store._clock(),
                    lease_until="", data=_kept(job["data"]))
        _event(ctx.store, actor, "job.cancelled", done, reason=done["reason"])
        if done["caller"] == CABINET:
            _council(ctx, "on_job_end", done)
        return done


def _council(ctx, hook: str, job: Mapping) -> None:
    from kd.cabinet import council_rounds, stage_gate  # lazy: api imports this module before the council's

    getattr(council_rounds, hook)(ctx, job)
    if hook == "on_job_end":  # the cabinet's other own job: the reviewer of a chain plan's stage
        stage_gate.on_job_end(ctx, job)


# --- ops --------------------------------------------------------------------------------------
def _count(value, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _registered(ctx, name) -> str:
    if not isinstance(name, str) or not agents.NAME_RE.fullmatch(name):
        raise _invalid("invalid agent name")
    try:
        names = agents.names(ctx.registry_path)
    except ValueError as exc:
        raise api.ApiError("unavailable", str(exc))
    if name not in names:
        raise _invalid(f"{name} is not a registered agent")
    return name


def _definition(ctx, job) -> dict:
    """The fields the cabinet relies on; the runner checks the rest against the registry again."""
    if not isinstance(job, Mapping) or set(job) != FIELDS:
        raise _invalid("a job definition has exactly the fields " + ", ".join(sorted(FIELDS)))
    if len(json.dumps(job, ensure_ascii=False)) > DEFINITION_MAX:
        raise _invalid(f"a job definition is at most {DEFINITION_MAX} bytes")
    if not isinstance(job["id"], str) or not JOB_ID.fullmatch(job["id"]):
        raise _invalid("invalid job id")
    _registered(ctx, job["agent"])
    if job["requester"] != "":
        _registered(ctx, job["requester"])
    if not isinstance(job["spec"], str) or not SPEC.fullmatch(job["spec"]):
        raise _invalid("invalid specialization")
    skills = job["skills"]
    if not isinstance(skills, list) or len(skills) > SKILLS_MAX or not all(
            isinstance(k, str) and SPEC.fullmatch(k) for k in skills):
        raise _invalid("skills is a list of names")
    if not isinstance(job["writer"], bool) or not _count(job["timeout"], *TIMEOUT):
        raise _invalid(f"writer is true or false, timeout {TIMEOUT[0]} to {TIMEOUT[1]} seconds")
    if not isinstance(job["caller"], str) or not CALLER.fullmatch(job["caller"]):
        raise _invalid("invalid caller")
    if not isinstance(job["limits"], Mapping) or job["limits"].get("write") not in WRITES:
        raise _invalid("limits.write is one of " + ", ".join(WRITES))
    return dict(job)


def _limits(value) -> dict:
    if not isinstance(value, Mapping) or not all(
            _count(value.get(k), 1, 1000) for k in ("jobs_per_day", "attempts_per_card_spec")):
        raise _invalid("limits are {jobs_per_day, attempts_per_card_spec}, each 1 to 1000")
    return dict(value)


def _model_of(ctx, job: dict, card, model: Optional[str] = None) -> None:
    """the card's effective model and effort go into the stored definition — ``model`` (its slug or
    subscription reference, else the pool's), ``model_spec`` (only on a named model) and ``effort``; a disabled model
    or a kind without its key on the host refuses the job here, before a slot is taken. ``model`` names the
    job's own model over the card's (a council's critic); the effort stays the card's."""
    from kd.cabinet import llm_api  # lazy: api imports this module before llm_api

    found, effort = llm_api.effective(ctx.store, card)
    if model is not None:
        found = llm_api.row(ctx.store, model)
        if found is None:
            raise api.ApiError("refused", f"unknown model {str(model)[:64]!r}", reason="model_unknown")
    if found is not None:
        if not found["enabled"]:
            raise api.ApiError("refused", f"the model {found['id']} is disabled", reason="model_disabled")
        if llm_api.missing_key(ctx, found):
            raise api.ApiError("refused", f"no key of the {found['provider']} provider on the host", reason="no_key")
        job["model"], job["model_spec"] = found["id"], llm_api.spec_of(found)
    job["effort"] = effort


def _insert(s, job: Mapping, card_id: str, now: str, inputs: Optional[Mapping] = None) -> dict:
    data = {"definition": job, "result": None, "usage": None}
    if inputs:  # beside the definition, not in it: sent to the host with it (prepare), dropped at the job's end
        data["inputs"] = dict(inputs)
    s._conn.execute(
        "INSERT INTO jobs (id, card, agent, caller, requester, spec, writer, data, created, updated)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (job["id"], card_id, job["agent"], job["caller"], job["requester"], job["spec"], int(job["writer"]),
         json.dumps(data, ensure_ascii=False, sort_keys=True), now, now),
    )
    return row(s, job["id"])


def _submit(ctx, principal, request):
    job, limits = _definition(ctx, request.get("job")), _limits(request.get("limits"))
    if job["caller"] == CABINET:  # the cabinet's own jobs are not submitted: nobody passes for it
        raise _invalid("invalid caller")
    card = api._card(ctx, job["card"])
    _model_of(ctx, job, card)
    s = ctx.store
    with s.transaction():
        now = s._clock()
        if row(s, job["id"]) is not None:
            raise api.ApiError("conflict", "job id collision")
        busy = s._conn.execute(
            "SELECT 1 FROM jobs WHERE card=? AND writer=1 AND state IN (%s)" % ",".join("?" * len(ACTIVE)),
            (card.id, *ACTIVE)).fetchone()
        if job["writer"] and busy:
            raise api.ApiError("conflict", f"{card.id} already has an active writer job; wait for it or cancel it")
        since = users.shift(now, hours=-24)
        # the cabinet's own jobs (a council's critics) have their own day limit: enqueue_own
        (today,) = s._conn.execute("SELECT COUNT(*) FROM jobs WHERE agent=? AND created>=? AND caller!=?",
                                   (job["agent"], since, CABINET)).fetchone()
        if today >= limits["jobs_per_day"]:
            raise api.ApiError("rate", f"rate limit: {limits['jobs_per_day']} jobs per day for {job['agent']}")
        (same,) = s._conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE card=? AND spec=? AND created>=? AND caller!=?",
            (card.id, job["spec"], since, CABINET)).fetchone()
        if same >= limits["attempts_per_card_spec"]:
            raise api.ApiError("rate", f"retry limit: {limits['attempts_per_card_spec']} {job['spec']} jobs per card"
                                       " per day")
        queued = _insert(s, job, card.id, now)
        _event(s, api.card_actor(principal), "job.queued", queued, spec=job["spec"], requester=job["requester"])
    return {"job": view(queued)}


def enqueue_own(ctx, job, *, model: Optional[str], inputs: Optional[Mapping] = None,
                pull: Optional[Mapping] = None) -> dict:
    """a job the cabinet starts itself — a council's critic on ``model``, whoever holds the card. No
    requester, no writer; it counts under the council's own day limit per agent (``council.json``), apart from
    what the coordinator may ask for. ``inputs`` are the files its worker reads instead of the board
    (``kd.worker_inputs``): a worker has no channel to ask for the card. ``pull``
    (``worker_jobs.check_pull``) puts a developer job that writes nothing on a pull request's head; it stays in
    the definition. The job's row; ApiError ``refused`` with
    the reason for a model that cannot run (unknown, disabled, no key), ``rate`` over the limit."""
    from kd import council  # lazy: the catalog is read when a council gathers

    job = _definition(ctx, job)
    if job["caller"] != CABINET or job["requester"] or job["writer"]:
        raise _invalid("a job of the cabinet has the caller cabinet, no requester and writes nothing")
    if inputs is not None:
        try:
            inputs = worker_inputs.check(inputs)
        except ValueError as exc:
            raise _invalid(f"a job's inputs: {exc}") from None
    if pull is not None:
        try:
            job["pull"] = worker_jobs.check_pull(pull)
        except ValueError as exc:
            raise _invalid(f"a job's pull: {exc}") from None
        if not worker_jobs.reads_pull(job):
            raise _invalid("a job's pull: only a developer job that writes nothing is on a pull request")
    card = api._card(ctx, job["card"])
    _model_of(ctx, job, card, model)
    s = ctx.store
    with s.transaction():
        now = s._clock()
        if row(s, job["id"]) is not None:
            raise api.ApiError("conflict", "job id collision")
        per_day = council.limits()["jobs_per_day"]
        # a job that met no head of its pull request ran no session: the council tries again every two minutes
        # until the host's copy has the head, and those tries must not eat the day
        (today,) = s._conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE agent=? AND created>=? AND caller=?"
            " AND NOT (state='failed' AND reason LIKE ?)",
            (job["agent"], users.shift(now, hours=-24), CABINET, HEAD_MISSING + "%")).fetchone()
        if today >= per_day:
            raise api.ApiError("rate", f"rate limit: {per_day} council jobs per day for {job['agent']}")
        queued = _insert(s, job, card.id, now, inputs)
        _event(s, HOST, "job.queued", queued, spec=job["spec"], requester="")
    return queued


def cancel_own(ctx, job_id: str, reason: str) -> Optional[str]:
    """The cabinet takes its own job back: queued here — cancelled at once; at the host — cancelling (the host's
    report ends it). Its state after, or None when it had ended."""
    s = ctx.store
    with s.transaction():
        job = row(s, job_id)
        if job is None or job["state"] in TERMINAL:
            return None
        if job["state"] == "queued" and job["lease_until"] < s._clock():
            return _cancel_here(ctx, job, reason)["state"]
        if job["state"] != "cancelling":
            job = _set(s, job_id, state="cancelling", reason=reason[:REASON_MAX])
            _event(s, HOST, "job.cancelling", job, reason=job["reason"])
        return "cancelling"


def _own(ctx, principal, request) -> dict:
    """A job of the caller kd-worker names (root sees every one); a foreign, missing or bad id looks the same."""
    job_id, caller = request.get("job"), request.get("caller")
    if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
        raise _invalid("invalid job id")
    if principal.kind != "root" and (not isinstance(caller, str) or not CALLER.fullmatch(caller)):
        raise _invalid("invalid caller")
    job = row(ctx.store, job_id)
    if job is None or (principal.kind != "root" and caller not in ("root", job["caller"])):
        raise api.ApiError("not_found", "unknown job")
    return job


def _view_op(ctx, principal, request):
    return {"job": view(_own(ctx, principal, request))}


def _cancel(ctx, principal, request):
    """queued -> cancelled at once; delivered or running -> cancelling (the dispatcher asks the host, the host's
    report ends it). A queued job the dispatcher holds may be at the host already: it goes the host's way too."""
    reason = request.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > REASON_MAX or _CONTROL.search(reason):
        raise _invalid(f"a reason is one line of 1 to {REASON_MAX} characters")
    s = ctx.store
    with s.transaction():
        job = _own(ctx, principal, request)
        previous = job["state"]
        if previous in TERMINAL:
            return {"state": previous, "previous": previous}
        actor = api.card_actor(principal)
        if previous == "queued" and job["lease_until"] < s._clock():
            return {"state": _cancel_here(ctx, job, reason.strip(), actor)["state"], "previous": previous}
        if previous != "cancelling":
            job = _set(s, job["id"], state="cancelling", reason=reason.strip())
            _event(s, actor, "job.cancelling", job, reason=job["reason"])
        return {"state": "cancelling", "previous": previous}


def _list(ctx, principal, request):
    card = api._visible_card(ctx, principal, request.get("card"))
    rows = ctx.store._conn.execute(
        "SELECT id FROM jobs WHERE card=? ORDER BY created DESC, rowid DESC LIMIT ?", (card.id, LIST_MAX)).fetchall()
    return {"jobs": [view(row(ctx.store, r[0])) for r in rows]}


# --- the host's reports -----------------------------------------------------------------------
def _line(value, name: str, limit: int, *, lines: bool = False) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit or (not lines and _CONTROL.search(value)):
        raise _invalid(f"{name} is text of at most {limit} characters")
    return value


def report_item(ctx, item):
    """``{"type": "job", "job": id, "state": S, …}`` from the host: running (``starting``, with its slot), or the
    end (``done``, ``failed``, ``no-result``, ``cancelled`` with reason, comment, usage, result). An end reported
    twice changes nothing."""
    job_id = item.get("job")
    if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
        raise _invalid("invalid job id")
    job = row(ctx.store, job_id)
    if job is None:
        raise api.ApiError("not_found", "unknown job")
    state = item.get("state")
    if state in ("queued", "starting", "running"):
        slot = item.get("slot", "")
        if slot and (not isinstance(slot, str) or not SLOT.fullmatch(slot)):
            raise _invalid("invalid worker slot")
        if state == "queued" or job["state"] not in ("queued", "delivered", "cancelling") or job["started"]:
            return
        fields = {"slot": slot, "started": ctx.store._clock(), "lease_until": ""}
        if job["state"] != "cancelling":
            fields["state"] = "running"
        job = _set(ctx.store, job_id, **fields)
        _event(ctx.store, HOST, "job.started", job, slot=slot)
        if job["caller"] == CABINET:
            _council(ctx, "on_job_started", job)
        return
    if state not in TERMINAL:
        raise _invalid(f"unknown job state {state!r}")
    reason = _line(item.get("reason"), "reason", REASON_MAX)
    comment = _line(item.get("comment"), "comment", worker_board.TEXT_MAX, lines=True)
    used = None
    if item.get("usage") is not None:
        try:
            used = usage.check(item["usage"])
        except ValueError as exc:
            raise _invalid(str(exc))
    result = item.get("result")
    if result is not None and (not isinstance(result, Mapping)
                               or len(json.dumps(result, ensure_ascii=False).encode()) > RESULT_MAX):
        raise _invalid(f"result is an object of at most {RESULT_MAX} bytes")
    if job["state"] in TERMINAL:
        return
    if state == "cancelled" and job["state"] == "cancelling" and job["reason"]:
        reason = job["reason"]  # why it was cancelled, not the host's "stopped"
    if state == "failed" and reason == HOST_RESTART and not comment:
        comment = RESTART_NOTE  # a graceful stop of the host says what its crash says
    _finish(ctx, job, state, reason=reason, comment=comment, used=used, result=result)


def on_host_restart(ctx, inflight):
    """The host restarted: its job units were stopped. A job it ran, or may have started (``inflight``), ends
    ``host_restart`` (a cancelling one is cancelled); one it only held is queued again."""
    s = ctx.store
    ids = [r[0] for r in s._conn.execute(
        "SELECT id FROM jobs WHERE state IN ('delivered','running','cancelling') ORDER BY created").fetchall()]
    for job_id in ids:
        job = row(s, job_id)
        if job["state"] == "delivered" and job_id not in inflight:
            _event(s, HOST, "job.requeued", _set(s, job_id, state="queued", next_try=s._clock(), lease_until=""))
        elif job["state"] == "cancelling" and not job["started"] and job_id not in inflight:
            _cancel_here(ctx, job, job["reason"] or HOST_RESTART)  # it never ran: nothing to say on the card
        elif job["state"] == "cancelling":
            _finish(ctx, job, "cancelled", reason=job["reason"] or HOST_RESTART)
        else:
            _finish(ctx, job, "failed", reason=HOST_RESTART, comment=RESTART_NOTE)


# --- the dispatcher's part --------------------------------------------------------------------
def lease(s, now: str, until: str, limit: int) -> list:
    """Queued jobs that are due and not leased, oldest first, leased to the caller until ``until``."""
    with s.transaction():
        ids = [r[0] for r in s._conn.execute(
            "SELECT id FROM jobs WHERE state='queued' AND next_try<=? AND lease_until<? ORDER BY created, rowid"
            " LIMIT ?", (now, now, limit)).fetchall()]
        for job_id in ids:
            s._conn.execute("UPDATE jobs SET lease_until=?, attempts=attempts+1 WHERE id=?", (until, job_id))
        return [row(s, i) for i in ids]


def stale_reason(s, job: Mapping) -> Optional[str]:
    """Why a job that has not started no longer holds — kd-worker's admission again (remediation §4.2): its card
    gone or done, another assignee, or a status the agent's type may not work in; None while it holds."""
    try:
        card = s.card(job["card"])
    except store.NotFound:
        return commands.CARD_GONE
    if card.terminal:
        return commands.CARD_DONE
    if job["caller"] == CABINET:  # a critic reads a card whoever holds it, in any open status
        return None
    if card.assignee != "@" + job["agent"]:
        return commands.REASSIGNED
    if card.status not in worker_jobs.PRECONDITIONS.get(job["data"]["definition"].get("type"), ()):
        return NOT_APPROVED
    return None


def take_back_stale(ctx) -> int:
    """Delivered jobs (waiting at the host, not started) whose card moved on go cancelling: the host gives them
    back. A running job runs on."""
    s, n = ctx.store, 0
    with s.transaction():
        for (job_id,) in s._conn.execute("SELECT id FROM jobs WHERE state='delivered' ORDER BY created").fetchall():
            reason = stale_reason(s, row(s, job_id))
            if reason:
                _event(s, HOST, "job.cancelling", _set(s, job_id, state="cancelling", reason=reason), reason=reason)
                n += 1
    return n


def at_host(s) -> list:
    """The jobs the host should hold: delivered, running or cancelling."""
    return [r[0] for r in s._conn.execute(
        "SELECT id FROM jobs WHERE state IN (%s) ORDER BY created" % ",".join("?" * len(AT_HOST)), AT_HOST).fetchall()]


def lost(ctx, job_id: str) -> Optional[str]:
    """A job the host neither holds nor is reporting (its end refused or dropped): a cancelling one is cancelled,
    any other fails ``host_lost_it``; its outcome, or None when it ended meanwhile."""
    job = row(ctx.store, job_id)
    if job is None or job["state"] not in AT_HOST:
        return None
    if job["state"] == "cancelling" and not job["started"]:
        return _cancel_here(ctx, job, job["reason"] or HOST_LOST)["state"]
    if job["state"] == "cancelling":
        return _finish(ctx, job, "cancelled", reason=job["reason"] or HOST_LOST)["state"]
    return _finish(ctx, job, "failed", reason=HOST_LOST, comment=LOST_NOTE)["state"]


def prepare(ctx, job_id: str, expired_before: str) -> Optional[dict]:
    """The host request of a leased job, or None (it moved on), or ``{"_outcome": …}`` when it ended here."""
    job = row(ctx.store, job_id)
    if job is None or job["state"] != "queued":
        return None
    reason = stale_reason(ctx.store, job)
    if reason:
        _cancel_here(ctx, job, reason)
        return {"_outcome": "cancelled"}
    if job["created"] < expired_before:
        _finish(ctx, job, "failed", reason=EXPIRED)
        return {"_outcome": "failed"}
    definition = job["data"]["definition"]
    if job["data"].get("inputs"):  # what the job's worker reads goes with it
        definition = {**definition, "inputs": job["data"]["inputs"]}
    from kd.cabinet import context_limits  # lazy: api imports this module before context_limits

    limit = context_limits.effective(ctx.store, job["agent"])  # the role's limit as the job is sent; 0 — none
    if limit:
        definition = {**definition, "context_limit": limit}
    return {"op": "job", "job": job_id, "agent": job["agent"], "definition": definition}


def delivered(ctx, job_id: str):
    job = row(ctx.store, job_id)
    if job is not None and job["state"] == "queued":
        _set(ctx.store, job_id, state="delivered")


def retry(ctx, job_id: str, next_try: str):
    job = row(ctx.store, job_id)
    if job is not None and job["state"] == "queued":
        _set(ctx.store, job_id, next_try=next_try, lease_until="")


def refused(ctx, job_id: str, message: str):
    job = row(ctx.store, job_id)
    if job is not None and job["state"] == "queued":
        sys.stderr.write(f"kd-cabinet: job {job_id} refused by the session host: {message[:300]}\n")
        _finish(ctx, job, "failed", reason=HOST_REFUSED)


def cancelling(s) -> list:
    return [row(s, r[0]) for r in s._conn.execute(
        "SELECT id FROM jobs WHERE state='cancelling' ORDER BY updated, rowid").fetchall()]


def host_cancelled(ctx, job_id: str, answer) -> bool:
    """The host's answer to job_cancel: ``queued`` (it never started: cancelled), ``running`` or ``ended`` (its
    report ends it), ``unknown`` (the host never had it: cancelled ``host_lost_it``)."""
    job = row(ctx.store, job_id)
    if job is None or job["state"] != "cancelling":
        return False
    if answer == "queued":
        _cancel_here(ctx, job, job["reason"])
        return True
    if answer == "unknown":
        _cancel_here(ctx, job, job["reason"] or HOST_LOST)
        return True
    return False


OPS = {
    "job.submit": (frozenset({"executor", "root"}), _submit),
    "job.view": (frozenset({"executor", "root"}), _view_op),
    "job.cancel": (frozenset({"executor", "root"}), _cancel),
    "job.list": (frozenset({"user"}), _list),
}
api.OPS.update(OPS)  # the board API serves them (api imports this module at its end)
