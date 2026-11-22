"""A council's rounds: the critics' jobs, their ends, the writer's turn.

A gathering starts when the writer hands the work in — a brief: the analyst's new version (``brief.version``) on a
card whose composition names critics for it; the code: a head of the card's pull request the forge
executor published (``pr.publish``) with enough changed lines of code. A round is one job per critic, started by the cabinet itself
(``jobs.enqueue_own``) on the critic's model in the writer agent's worker pool; each job's final block becomes its
run's verdict, findings and questions and a ``review`` message in the card's thread. When every run has ended the
round ends: ``clean`` closes the council, a substantive finding or a question hands the card back to its writer
with one command of a fixed text — never a word of a finding — and the next version is the next round, until the
council's rounds run out (``limited``). A round no critic finished fails the council; the work goes on without it.

    running -> needs_writer -> running ... -> clean | settled | limited | cancelled | failed

The council approves nothing and writes nothing into the card's text. Everything here that runs inside someone
else's write — the analyst's hand-in, the host's report of a job — runs in its own savepoint and never fails that
write: a broken council costs the review, not the work.
"""

import json
import secrets
import sys
import traceback
from typing import Callable, Mapping, Optional, Sequence

from kd import (
    council,
    llm_models,
    manifest,
    worker_board,
    worker_inputs,
    worker_jobs,
    worker_packs,
    worker_slots,
)
from kd.cabinet import (
    api,
    briefs,
    commands,
    continuations,
    council_inputs,
    councils,
    jobs,
    llm_api,
    notices,
    problems,
)

ACTOR = "system:council"
SYSTEM = "system"
BOSS = "boss"
AGENTS = councils.POOLS  # whose worker pool the critics of a kind read in
SPECS = {"brief": "brief-critic", "code": "code-critic"}
WAKE = "[council] {id}: round {round} of the {kind} council ended — read kd-work council {id} and answer each finding"
NO_SLOTS, NO_CRITIC, BAD_RESULT, CONFIG = (
    "no_slots",
    "no_critic",
    "bad_result",
    "config",
)
SUPERSEDED = "superseded"
PR_CLOSED = "pr_closed"  # a council's end: its pull request was merged or closed
PULL_FIELDS = (
    "repo",
    "project",
    "number",
    "head",
    "target",
)  # the basis of a round of the code
ROUND_FIELDS = (
    "version",
    "scope_sha",
    "head",
    "target",
)  # what of a basis each round keeps as its own
HEAD_RETRY_S = 120  # a critic whose copy had not got the pull request's head is given a new job this often
UNANSWERED = "unanswered"  # a council's end: the writer handed the same text in past its findings (observe only)
TELLS_BOSS = (
    "clean",
    "settled",
    "limited",
    "failed",
)  # how a brief's council ends with the brief still to start
BRIEF_BUDGET = worker_packs.MAX_BRIEF - 200  # the pointer to the rest always fits
ROUND_PROBLEMS = (
    "council_run_failed",
    "council_stalled",
    "council_unanswered",
)  # what a council's end settles
PROBLEMS = ROUND_PROBLEMS + ("council_failed",)
SWEEP_COUNCILS = 200  # open councils one sweep looks at


class _NoRun(Exception):
    """Why no critic of the round can start."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


NoRun = _NoRun  # for the cabinet's other own jobs (a stage's reviewer, ``stage_gate``)


def _guarded(ctx, what: str, body: Callable, failed=None):
    """``body`` in its own savepoint; what it returned, or ``failed`` when it failed (said on stderr)."""
    try:
        with ctx.store.transaction():
            return body()
    except Exception:  # noqa: BLE001 — the write this runs in must stand whatever the council does
        sys.stderr.write(
            f"kd-cabinet: council: {what} failed\n{traceback.format_exc()}"
        )
        return failed


def _settle(s, found: Mapping, kinds) -> None:
    """The card's open council problems of these kinds are over: the council moved on."""
    problems.resolve(s, found["card"], kinds, by=f"council:{found['id']}")


# --- rows ---------------------------------------------------------------------------------------------------
def _update(s, council_id: str, **fields) -> dict:
    for key in ("basis", "critics"):
        if key in fields:
            fields[key] = json.dumps(fields[key], ensure_ascii=False, sort_keys=True)
    fields["updated"] = s._clock()
    sets = ", ".join(f"{key}=?" for key in fields)
    s._conn.execute(
        f"UPDATE councils SET {sets} WHERE id=?", (*fields.values(), council_id)
    )
    return councils.row(s, council_id)


def _set_run(s, run: Mapping, **fields) -> None:
    for key in ("findings", "questions", "usage"):
        if key in fields:
            fields[key] = json.dumps(fields[key], ensure_ascii=False, sort_keys=True)
    sets = ", ".join(f"{key}=?" for key in fields)
    s._conn.execute(
        f"UPDATE council_runs SET {sets} WHERE council=? AND round=? AND n=?",
        (*fields.values(), run["council"], run["round"], run["n"]),
    )


def _event(s, record, name: str, actor: str = ACTOR, **payload) -> None:
    s._event(actor, name, record.id, payload, record.thread)


def _current(basis: Mapping) -> dict:
    return basis["rounds"][-1]


# --- a critic's job -----------------------------------------------------------------------------------------
def own_plan(ctx, agent: str) -> tuple:
    """What the cabinet's own jobs in ``agent``'s worker pool share: the agent, its type, its pool's config, the
    registry of specializations. ``NoRun`` with the reason when no such job can start."""
    root = ctx.tree()
    try:
        config = worker_slots.load_config(
            root / "common" / "worker" / "worker-slots.json"
        )
        if agent not in config.agents:
            raise _NoRun(NO_SLOTS)
        agent_type = manifest.load_manifest(
            root / "agents" / agent / "agent.json"
        ).profile
        registry = worker_packs.load_packs(
            root / "common" / "specializations",
            require_root_owner=ctx.require_root_owner,
        )
    except (
        worker_slots.SlotError,
        manifest.ManifestError,
        worker_packs.PackError,
        OSError,
        ValueError,
    ) as exc:
        raise _NoRun(f"{CONFIG}: {exc.__class__.__name__}") from None
    return agent, agent_type, config, registry


def _plan(ctx, kind: str) -> tuple:
    """What the critics' jobs of a round share (``own_plan`` of the kind's pool)."""
    return own_plan(ctx, AGENTS[kind])


def own_definition(
    plan: tuple, card_id: str, spec: str, text: str, repo: Optional[str]
) -> dict:
    """A job the cabinet starts itself, as it keeps it: specialization ``spec`` over ``card_id`` with the task
    ``text``, writing nothing, no requester; ``repo`` — the project whose copy it reads, None for none. ``NoRun``
    when the specialization cannot be built."""
    agent, agent_type, config, registry = plan
    try:
        prompt = worker_packs.build_prompt(
            registry, spec, (), card=card_id, brief=text, agent_type=agent_type
        )
    except worker_packs.PackError as exc:
        raise _NoRun(f"{CONFIG}: {worker_jobs.clean(exc, 120)}") from None
    entry = registry.specs[prompt.spec].limits
    limits = {
        "write": "none",
        "capabilities": list(entry.capabilities),
        "network": entry.network,
    }
    admission = worker_jobs.Admission(agent, agent_type, repo, False, limits, prompt)
    return worker_jobs.definition(
        worker_jobs.Request(card_id, spec, (), text),
        admission,
        job_id=worker_jobs.new_job_id(),
        caller=jobs.CABINET,
        requester="",
        config=config,
    )


def _earlier(s, found: Mapping, round_no: int) -> list:
    """The substantive findings of the rounds before ``round_no``, one line each: its id, severity and the
    writer's answer."""
    before = [run for run in councils.runs(s, found["id"]) if run["round"] < round_no]
    return [
        f"{finding['id']} {finding['severity']} {finding.get('answer') or 'unanswered'}"
        for finding in council.substantive(councils.findings_of(before))
    ]


READS = {
    council_inputs.BRIEF: "the brief, whole",
    council_inputs.THREAD: "the card's thread",
    council_inputs.CARDS: "the cards the brief names",
    council_inputs.CARD: "the card: its description and acceptance criteria",
    council_inputs.COUNCIL: "the earlier rounds and the author's answers, the latest round first",
}


def _pull(found: Mapping) -> Optional[dict]:
    """The pull request a critic of ``found`` is put on (``worker_jobs.check_pull``); None for a brief's."""
    if found["kind"] != "code":
        return None
    basis = found["basis"]
    return {"number": basis["number"], "head": basis["head"], "target": basis["target"]}


def _task(found: Mapping, record, n: int, earlier: list, files=()) -> str:
    """The job's task message: facts only — which council, which round, which files to read. The rules are the
    specialization's; no word of a finding or of the card travels here. A worker has no board: what it reads is
    in ``files`` (``council_inputs``), the names of its slot's ``inputs/``."""
    basis, card = found["basis"], record.id
    reads = [
        f"{worker_inputs.DIR}/{name} — {what}"
        for name, what in READS.items()
        if name in files
    ]
    if found["kind"] == "code":
        about = (
            f"card {card}: pull request {basis['project']}#{basis['number']}, head {basis['head'][:12]}"
            f" into {basis['target']}"
        )
        reads.insert(
            0,
            f"repo/ — the project on that head (branch pr-head; pr-base is the tip of {basis['target']})",
        )
    else:
        about = f"card {card}: the brief v{basis['version']} (scope {basis['scope_sha'][:12]})"
    lines = [
        f"council {found['id']} · {found['kind']} · round {found['round']} of {found['rounds_max']}"
        f" · critic {n} of {len(found['critics'])}",
        about,
        "read in your working directory: " + " · ".join(reads),
    ]
    if found["round"] > 1:
        for line in earlier:
            if sum(len(v) + 1 for v in lines) + len(line) > BRIEF_BUDGET:
                lines.append(f"more: {worker_inputs.DIR}/{council_inputs.COUNCIL}")
                break
            lines.append(line)
    return "\n".join(lines)


def _definition(
    ctx, plan: tuple, found: Mapping, record, n: int, earlier: list, files=()
) -> dict:
    spec, text = SPECS[found["kind"]], _task(found, record, n, earlier, files)
    # a critic of the code reads the copy that holds the pull request's heads: the pull request's project
    repo = found["basis"]["project"] if found["kind"] == "code" else None
    return own_definition(plan, record.id, spec, text, repo)


def _writer_model(ctx, record, agent: str) -> str:
    """The model the writer works on, as a critic's reference would name it: the card's, else the agent's own."""
    found, _ = llm_api.effective(ctx.store, record)
    if found is not None:
        return found["id"]
    try:
        own = manifest.load_manifest(ctx.tree() / "agents" / agent / "agent.json").model
        return next(
            (
                ref
                for ref, entry in llm_models.claude_models().items()
                if entry["upstream"] == own
            ),
            "",
        )
    except (manifest.ManifestError, OSError, ValueError):
        return ""


# --- a round ------------------------------------------------------------------------------------------------
def _create(s, record, kind: str, part: Mapping) -> dict:
    council_id, now = secrets.token_hex(16), s._clock()
    s._conn.execute(
        "INSERT INTO councils (id, card, kind, basis, critics, rounds_max, round, state, created, updated)"
        " VALUES (?,?,?,?,?,?,0,'running',?,?)",
        (
            council_id,
            record.id,
            kind,
            json.dumps({"rounds": []}),
            json.dumps(list(part["critics"]), ensure_ascii=False),
            part["rounds"],
            now,
            now,
        ),
    )
    return councils.row(s, council_id)


def _round_plan(ctx, found: Mapping, record) -> tuple:
    """``(plan, inputs, why)`` of the critics' jobs of ``found``'s round: what they share and the files each
    reads; no plan and the reason when none of them can start."""
    try:
        return (
            _plan(ctx, found["kind"]),
            council_inputs.build(
                ctx.store, found, record
            ),  # the same files for every critic of the round
            "",
        )
    except _NoRun as exc:
        return None, {}, exc.reason
    except (
        ValueError
    ) as exc:  # the brief over its part: no critic could honestly read it
        return None, {}, f"{CONFIG}: {worker_jobs.clean(exc, 120)}"


def _critic_job(
    ctx, plan: tuple, found: Mapping, record, n: int, model: str, inputs: Mapping
) -> tuple:
    """``(job id, "")`` of critic ``n``'s job on ``model``, or ``("", why)`` when it cannot start."""
    try:
        earlier = _earlier(ctx.store, found, found["round"])
        definition = _definition(ctx, plan, found, record, n, earlier, inputs)
        job = jobs.enqueue_own(
            ctx, definition, model=model, inputs=inputs, pull=_pull(found)
        )
        return job["id"], ""
    except _NoRun as exc:
        return "", exc.reason
    except api.ApiError as exc:
        return "", exc.reason or exc.kind


def _start_round(ctx, found: Mapping, record, fields: Mapping, round_no: int) -> None:
    """Round ``round_no`` over a new basis — ``fields``: a brief's ``version`` and ``scope_sha``, a pull request's
    PULL_FIELDS: a run per critic, each with its job — or failed at once, with the reason, when its model cannot
    run or the writer's agent has no worker pool."""
    s, now = ctx.store, ctx.store._clock()
    writer = councils.writer(s, found)
    basis = dict(found["basis"])
    basis.update(fields)
    entry = {key: fields[key] for key in ROUND_FIELDS if key in fields}
    basis["rounds"] = list(basis["rounds"]) + [
        {
            "round": round_no,
            **entry,
            "writer_model": _writer_model(ctx, record, writer),
            "started": now,
            "ended": "",
            "state": "running",
        }
    ]
    found = _update(s, found["id"], state="running", round=round_no, basis=basis)
    _settle(s, found, ("council_failed", "council_run_failed", "council_stalled"))
    _event(
        s,
        record,
        "council.started",
        council=found["id"],
        kind=found["kind"],
        round=round_no,
    )
    plan, inputs, why = _round_plan(ctx, found, record)
    for n, model in enumerate(found["critics"], 1):
        job_id, reason = "", why
        if plan is not None:
            job_id, reason = _critic_job(ctx, plan, found, record, n, model, inputs)
        s._conn.execute(
            "INSERT INTO council_runs (council, round, n, model, job, state, fail_reason, created, ended)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                found["id"],
                round_no,
                n,
                model,
                job_id,
                "queued" if job_id else "failed",
                "" if job_id else reason,
                now,
                "" if job_id else now,
            ),
        )
        if not job_id:
            _posted(ctx, found, record, councils.runs(s, found["id"], round_no)[-1])
    _round_end(
        ctx, found["id"]
    )  # no critic could start: the round is over where it began


def _review_line(found: Mapping, run: Mapping) -> str:
    head = f"{found['kind']} council round {run['round']} · {run['model']} · "
    if run["state"] != "done":
        return head + f"failed: {run['fail_reason']}"
    counts = council.counts(run["findings"])
    return (
        head
        + f"{run['verdict']} · "
        + ", ".join(f"{key} {counts[key]}" for key in council.SEVERITIES)
    )


def _posted(ctx, found: Mapping, record, run: Mapping) -> None:
    """A run that ended — done or failed — is one ``review`` message of the council in the card's thread, its
    event, and for a failed one the problem the coordinator hears of."""
    s = ctx.store
    payload = {
        key: run[key]
        for key in (
            "council",
            "round",
            "n",
            "model",
            "state",
            "verdict",
            "findings",
            "questions",
            "usage",
            "fail_reason",
        )
    }
    s._add_message(
        record.thread,
        ACTOR,
        "review",
        _review_line(found, run),
        payload,
        "",
        via="council",
    )
    _event(
        s,
        record,
        "council.run",
        council=found["id"],
        round=run["round"],
        n=run["n"],
        state=run["state"],
    )
    if run["state"] == "failed":
        problems.raise_problem(
            ctx,
            key=f"council-run:{found['id']}:{run['round']}:{run['n']}",
            record=record,
            kind="council_run_failed",
            agent=councils.writer(s, found),
            detail={
                "council": found["id"],
                "round": run["round"],
                "n": run["n"],
                "model": run["model"],
                "reason": run["fail_reason"],
            },
        )


def _round_end(ctx, council_id: str) -> None:
    """The round is over once every run has ended: by its findings the council closes clean, hands the card
    back to its writer, or — no critic having finished — fails."""
    s = ctx.store
    found = councils.row(s, council_id)
    if found is None or found["state"] != "running":
        return
    outcome = council.round_outcome(councils.runs(s, council_id, found["round"]))
    if outcome is None:
        return
    record = s.card(found["card"])
    basis = found["basis"]
    _current(basis).update(ended=s._clock(), state="ended")
    if outcome != "needs_writer":
        _end(
            ctx,
            found,
            record,
            outcome,
            NO_CRITIC if outcome == "failed" else "",
            basis=basis,
        )
        return
    awaited = _awaited(ctx, found)  # asked before the round's event closes that wait
    found = _update(s, council_id, state="needs_writer", basis=basis)
    _event(
        s,
        record,
        "council.round",
        council=council_id,
        round=found["round"],
        state="needs_writer",
    )
    _wake_writer(ctx, found, record, awaited)


def _end(
    ctx,
    found: Mapping,
    record,
    state: str,
    reason: str,
    *,
    basis: Optional[Mapping] = None,
    actor: str = ACTOR,
    keep: Sequence[str] = (),
) -> dict:
    """The council is over. Its rounds' problems are over with it, but for ``keep``: what the end itself leaves
    for someone to decide."""
    s = ctx.store
    fields = {"state": state, "ended": s._clock(), "end_reason": reason}
    if basis is not None:
        fields["basis"] = basis
    ended = _update(s, found["id"], **fields)
    _event(
        s,
        record,
        "council.ended",
        actor,
        council=found["id"],
        state=state,
        reason=reason,
    )
    _settle(s, found, tuple(kind for kind in ROUND_PROBLEMS if kind not in keep))
    if state == "failed":
        problems.raise_problem(
            ctx,
            key=f"council-failed:{found['id']}",
            record=record,
            kind="council_failed",
            agent=councils.writer(s, found),
            detail={"council": found["id"]},
        )
        notices.post_in_tx(  # the owner hears of it whoever routes the problem
            s,
            source="pipeline",
            level="warn",
            key="notice.council.failed",
            card=record.id,
            params={"round": found["round"]},
        )
    if state in TELLS_BOSS and found["kind"] == "brief":
        _tell_boss(ctx, ended)
    if found["kind"] == "code" and reason != "card_closed":
        _pull_decision(ctx, ended)
    return ended


def _pull_decision(ctx, found: Mapping) -> None:
    """The council of a pull request is over, however it ended: the decision it held back is the owner's now
    (``prs_api.council_ended`` — while the pull request is still open on the head the council stood on). In its
    own savepoint: a decision that cannot open never undoes the council's end — the next publish opens it, and
    the owner is told so: nothing else would say that a pull request waits without its decision."""
    from kd.cabinet import (
        prs_api,
    )  # at call time: it loads the API, which loads this module's readers

    opened = _guarded(
        ctx,
        f"council {found['id']}: the pull request's decision",
        lambda: prs_api.council_ended(ctx, found),
        failed=_guarded,
    )
    if opened is _guarded:
        notices.post_in_tx(
            ctx.store,
            source="pipeline",
            level="warn",
            key="notice.council.decision_failed",
            card=found["card"],
            params={"number": found["basis"].get("number")},
        )


def _tell_boss(ctx, found: Mapping) -> None:
    """The brief is ready for the coordinator's look now that its council is over: the wake the board made at the
    hand-in was held back while the critics read."""
    from kd import (
        board,
    )  # at call time: kd.board loads the cabinet's client, which loads the API

    s = ctx.store
    record = s.card(found["card"])
    if (
        record.status != "Inbox"
        or briefs.BRIEF_READY not in record.labels
        or record.assignee == "@" + BOSS
        or not ctx.host_config().hosted(BOSS)
    ):
        return
    commands.enqueue_signal(
        s,
        agent=BOSS,
        card=record.id,
        initiator=SYSTEM,
        key=f"council:{found['id']}:ended",
        text=board.BRIEF_WAKE.format(id=record.id),
    )


def _awaited(ctx, found: Mapping) -> bool:
    """Whether a turn of the council's writer waits for this council (``finish --outcome wait_dependency
    --council``): the end of that wait is the writer's wake then — one continuation, within the continuations'
    limits. Only where the pipeline launches continuations: one that observes wakes nobody by a wait."""
    s = ctx.store
    writer = councils.writer(s, found)
    if not writer or not ctx.pipeline().enforcing:
        return False
    row = s._conn.execute(
        "SELECT 1 FROM waits WHERE kind='council' AND ref=? AND card=? AND agent=? AND blocking=1"
        " AND state IN ('pending','open') LIMIT 1",
        (found["id"], found["card"], writer),
    ).fetchone()
    return row is not None


def _wake_writer(ctx, found: Mapping, record, awaited: bool = False) -> None:
    """The card goes back to its writer with one command of a fixed text. A brief: to the analyst with
    ``spec:task-brief``, as "Rework the brief" hands it over, in the status it has — and the board's own wake of
    that move is left out. An aggregate in work is not handed over: its holder works on, the analyst re-plans it
    without holding it, so only the hand-in is undone (``brief-ready``) and the analyst woken. The code:
    the card stays as its holder has it. A writer whose turn waits for this council (``awaited``) is woken by
    that wait's end, not by a second command."""
    s = ctx.store
    agent = councils.writer(s, found)
    if record.terminal or not agent:
        return
    if found["kind"] == "brief":
        kept = councils.replanned(record)
        labels = [
            v
            for v in record.labels
            if v != briefs.BRIEF_READY
            and (kept or not v.startswith((worker_board.SPEC_PREFIX, briefs.SIZE)))
        ]
        if not kept:
            labels.append(briefs.BRIEF_SPEC)
        changes = {}
        if not kept and record.assignee != briefs.ANALYST:
            changes["assignee"] = briefs.ANALYST
        if set(labels) != set(record.labels):
            changes["labels"] = labels
        if changes:
            s.edit_card(
                record.id, None, changes, actor=SYSTEM, event_extra={"wake": False}
            )
    if ctx.host_config().hosted(agent) and not awaited:
        s._enqueue_in_tx(
            {
                "key": f"council:{found['id']}:round:{found['round']}:writer",
                "agent": agent,
                "thread": record.thread,
                "card": record.id,
                "kind": "signal",
                "initiator": SYSTEM,
                "text": WAKE.format(
                    id=record.id, round=found["round"], kind=found["kind"]
                ),
            }
        )


def _take_back(ctx, found: Mapping, reason: str) -> None:
    """The open round's runs that still read are cancelled, their jobs taken back."""
    s, now = ctx.store, ctx.store._clock()
    for run in councils.runs(s, found["id"], found["round"]):
        if run["state"] in ("queued", "running"):
            _set_run(
                s, run, state="cancelled", ended=now
            )  # first: the job's end must find the run closed
            if run["job"]:
                jobs.cancel_own(ctx, run["job"], reason)


# --- the entries --------------------------------------------------------------------------------------------
def on_brief_version(ctx, event: Mapping) -> None:
    """The analyst handed in a new version of a brief: its council gathers, or its open council takes the next
    round. A person's version gathers nobody: the owner writes what the owner wants."""
    payload = event.get("payload") or {}
    if (
        payload.get("origin") != "agent"
        or event.get("actor") != "agent:" + AGENTS["brief"]
    ):
        return
    _guarded(
        ctx,
        f"{event['card']}: a brief version",
        lambda: _brief_version(ctx, event["card"]),
    )


def _brief_version(ctx, card_id: str) -> None:
    s = ctx.store
    record, last = s.card(card_id), briefs.latest(s, card_id)
    part = councils.effective(s, record).get("brief")
    found = councils.open_for(s, card_id, "brief")
    if last is None or (found is None and not part):
        return
    fields = {"version": last["version"], "scope_sha": last["scope_sha"]}
    if found is None:
        _start_round(ctx, _create(s, record, "brief", part), record, fields, 1)
        return
    _new_basis(ctx, found, record, part, fields)


def _new_basis(
    ctx, found: Mapping, record, part: Optional[Mapping], fields: Mapping
) -> dict:
    """An open council meets a new basis — the brief's next version, the pull request's next head: the round its
    critics still read does not count, a council past its last round is over (``limited``), else its next round
    starts on the new basis. The council's row after."""
    s = ctx.store
    if not part:  # the composition was taken off the card since: no next round
        return cancel(ctx, found, "removed")
    if (
        found["state"] == "running"
    ):  # a new basis while the critics read: their round does not count
        _take_back(ctx, found, SUPERSEDED)
        basis = found["basis"]
        _current(basis).update(ended=s._clock(), state=SUPERSEDED)
        found = _update(s, found["id"], basis=basis)
    if found["round"] >= part["rounds"]:
        # the code: the end hands the owner the head that came, not the one the last round read — its decision
        # opens on it and says «limited» (``councils.pull_review``)
        moved = {**found["basis"], **fields} if found["kind"] == "code" else None
        return _end(ctx, found, record, "limited", "rounds", basis=moved)
    # the composition is read again at each round: what the owner changed since holds from here
    found = _update(
        s, found["id"], critics=list(part["critics"]), rounds_max=part["rounds"]
    )
    _start_round(ctx, found, record, fields, found["round"] + 1)
    return councils.row(s, found["id"])


def on_pull_head(
    ctx, record, pr: Mapping, held: Optional[Mapping] = None
) -> Optional[dict]:
    """A pull request of the card is published on a head (``pr.publish``, inside its write): the card's council
    of the code gathers over it, takes its next round, or — the head it already stands on — stays as it is. The
    council's row; None when nobody gathers: no critics of the code on the card, or fewer changed lines of code
    than the composition's ``min_lines``. None as well when the council itself broke: the pull request goes on
    without it. ``held`` is what the publish keeps back while the council is open — the decision of this head
    (``prs_api``); it stays in the basis and is handed back at the council's end."""
    return _guarded(
        ctx,
        f"{record.id}: a pull request's head",
        lambda: _pull_head(ctx, record, pr, held),
    )


def _pull_head(
    ctx, record, pr: Mapping, held: Optional[Mapping] = None
) -> Optional[dict]:
    s = ctx.store
    part = councils.effective(s, record).get("code")
    fields = {**{key: pr[key] for key in PULL_FIELDS}, "held": dict(held or {})}
    found = councils.open_for_pull(s, record.id, pr["repo"], pr["number"])
    if found is not None:
        basis = found["basis"]
        if (basis["head"], basis["target"]) == (pr["head"], pr["target"]):
            # the same head again is the same round; what is held back is the newest publish of it (its checks,
            # its effect): the decision opens with what the owner is to sign
            if held and basis.get("held") != fields["held"]:
                found = _update(s, found["id"], basis={**basis, "held": fields["held"]})
            return found
        return _new_basis(ctx, found, record, part, fields)
    read = councils.for_head(s, record.id, pr["repo"], pr["number"], pr["head"])
    if read is not None and read["basis"].get("target") == pr["target"]:
        return (
            read  # a council that ended stood on this very head: nobody reads it twice
        )
    if not part or council.code_lines(pr["files"]) < part["min_lines"]:
        return None
    found = _create(s, record, "code", part)
    _start_round(ctx, found, record, fields, 1)
    return councils.row(s, found["id"])


def on_pull_ended(ctx, card_id: str, repo: str, number: int) -> None:
    """The pull request was merged or closed: its open council is over (``cancelled``/``pr_closed``)."""

    def body():
        found = councils.open_for_pull(ctx.store, card_id, repo, number)
        if found is not None:
            cancel(ctx, found, PR_CLOSED)

    _guarded(ctx, f"{card_id}: a pull request ended", body)


def on_job_started(ctx, job: Mapping) -> None:
    """A critic's job runs."""

    def body():
        run = councils.run_of_job(ctx.store, job["id"])
        if run is not None and run["state"] == "queued":
            _set_run(ctx.store, run, state="running")

    _guarded(ctx, f"job {job['id']}: started", body)


def on_job_end(ctx, job: Mapping) -> None:
    """A critic's job ended: its final block is the run's verdict, findings and questions — or, of any other
    shape, the run's failure. A job whose run is closed already (a round a new basis superseded, a cancelled
    council) changes nothing."""
    _guarded(ctx, f"job {job['id']}: ended", lambda: _job_end(ctx, job))


def _waits_for_head(ctx, found: Mapping, job: Mapping) -> bool:
    """Whether the critic of this failed job is still to be tried again: the host's copy had not got the pull
    request's head (kd-repo-sync brings it within minutes), and the round is younger than ``head_wait_minutes``."""
    if (
        found["kind"] != "code"
        or job["state"] != "failed"
        or not str(job["reason"] or "").startswith(jobs.HEAD_MISSING)
    ):
        return False
    wait = int(council.limits()["head_wait_minutes"]) * 60
    return _current(found["basis"])["started"] > continuations._since(ctx, wait)


def _job_end(ctx, job: Mapping) -> None:
    s = ctx.store
    run = councils.run_of_job(s, job["id"])
    if run is None or run["state"] not in ("queued", "running"):
        return
    found = councils.row(s, run["council"])
    if _waits_for_head(ctx, found, job):
        # the run keeps its ended job and goes back to the queue: the sweep gives it a new one (``_sweep_one``)
        _set_run(s, run, state="queued")
        return
    fields = {"state": "failed", "ended": s._clock(), "usage": job["data"].get("usage")}
    if job["state"] == "done":
        try:
            first = 1 + len(
                councils.findings_of(councils.runs(s, found["id"], run["round"]))
            )
            result = council.critic_result(
                found["kind"], job["data"].get("result"), run["round"], first
            )
            fields.update(
                state="done",
                verdict=result["verdict"],
                findings=result["findings"],
                questions=result["questions"],
            )
        except ValueError as exc:
            fields["fail_reason"] = f"{BAD_RESULT}: {exc}"
    else:  # failed, no-result, or cancelled by someone else than the council: the round still has to end
        fields["fail_reason"] = job["reason"] or job["state"]
    _set_run(s, run, **fields)
    record = s.card(found["card"])
    _posted(ctx, found, record, councils.run_of_job(s, job["id"]))
    _round_end(ctx, found["id"])


# --- the writer's turn ---------------------------------------------------------------------------------------
GATE_SHOWN = 10  # finding ids in a refusal; the rest is counted
SCOPE_CHANGES = (
    "description",
    "ac_add",
    "ac_remove",
    "operations",
    "stages",
)  # worker_board's scope changes


def answered(ctx, found: Mapping, record, agent: str, turn: str, answers) -> None:
    """The writer's answers of one call, checked by ``council.reply``: written into the findings and said once in
    the card's thread."""
    s, now = ctx.store, ctx.store._clock()
    by_id = {answer["finding"]: answer for answer in answers}
    for run in councils.runs(s, found["id"], found["round"]):
        if not any(finding["id"] in by_id for finding in run["findings"]):
            continue
        findings = []
        for finding in run["findings"]:
            answer = by_id.get(finding["id"])
            if answer is not None:
                finding = {
                    k: v
                    for k, v in finding.items()
                    if k not in ("answer", "note", "card", "answered")
                }
                finding.update(
                    {k: answer[k] for k in ("answer", "note", "card") if k in answer}
                )
                finding["answered"] = {"at": now, "turn": turn}
            findings.append(finding)
        _set_run(s, run, findings=findings)
    lines = [
        f"{a['finding']} {a['answer']}"
        + (f" {a['card']}" if a.get("card") else "")
        + (f" — {a['note']}" if a.get("note") else "")
        for a in answers
    ]
    payload = {
        "council": found["id"],
        "round": found["round"],
        "answers": list(answers),
    }
    s._add_message(
        record.thread,
        f"agent:{agent}",
        "review_reply",
        "\n".join(lines),
        payload,
        "",
        via="council",
    )
    left = council.unanswered(
        councils.findings_of(councils.runs(s, found["id"], found["round"]))
    )
    _event(
        s,
        record,
        "council.reply",
        f"agent:{agent}",
        council=found["id"],
        round=found["round"],
        answered=len(answers),
        unanswered=len(left),
    )
    _update(s, found["id"])  # the writer moved: the stall clock starts again
    _settle(s, found, ("council_stalled",))


def _listed(ids: list) -> str:
    more = len(ids) - GATE_SHOWN
    return ", ".join(ids[:GATE_SHOWN]) + (f" and {more} more" if more > 0 else "")


def _same_basis(s, found: Mapping, record, changes: Optional[Mapping] = None) -> bool:
    """Whether what the writer hands in is the text the council read."""
    if any(key in (changes or {}) for key in SCOPE_CHANGES):
        return False
    return briefs.scope_sha(briefs.scope(record)) == found["basis"].get("scope_sha")


def _owed(
    s, found: Mapping, record, what: str, changes: Optional[Mapping] = None
) -> tuple:
    """``(finding ids, why)`` that keep the writer from handing the work in: the substantive findings of the last
    round without an answer, or — for the very basis the council read — the ones answered «fixed», a minor one
    too (the writer answers those as well, and «fixed» is a claim whatever the finding's weight). A
    council of the code that waits for its writer stands on the head it read: a new head is its next round
    already."""
    current = councils.findings_of(councils.runs(s, found["id"], found["round"]))
    left = council.unanswered(current)
    if left:
        return left, (
            f"{record.id} has unanswered council findings: {_listed(left)} — answer each with"
            f" kd-work council-reply (read them: kd-work council {record.id})"
        )
    code = found["kind"] == "code"
    if code or (what == "handin" and _same_basis(s, found, record, changes)):
        fixed = [f["id"] for f in current if f.get("answer") == "fixed"]
        stands = (
            "the pull request is on the head the council read — publish the fix (kd-forge publish)"
            if code
            else "the brief is the version the council read — change it"
        )
        if fixed:
            return fixed, (
                f"{record.id}: {_listed(fixed)} answered fixed, but {stands},"
                " or answer rejected or later with the reason"
            )
    return [], ""


def gate(ctx, record, agent: str, what: str, changes: Optional[Mapping] = None) -> None:
    """The writer does not hand the work in (``handin``: ``brief-ready``) or end its turn with it (``finish``)
    past the findings of its council's last round — of each council of the card that waits for it: refused in
    ``enforce`` with the ids, a problem in ``observe``."""
    s = ctx.store
    for found in _owing(s, record, agent):
        ids, why = _owed(s, found, record, what, changes)
        if not ids:
            continue
        if ctx.pipeline().enforcing:
            raise api.ApiError("refused", why)
        problems.raise_problem(
            ctx,
            key=f"council-unanswered:{found['id']}:{found['round']}",
            record=record,
            kind="council_unanswered",
            agent=agent,
            detail={"council": found["id"], "round": found["round"], "findings": ids},
        )


def _owing(s, record, agent: str, kind: Optional[str] = None) -> list:
    """The card's councils that wait for ``agent`` as their writer."""
    return [
        found
        for found in councils.of_card(s, record.id, kind)
        if found["state"] == "needs_writer" and councils.writer(s, found) == agent
    ]


def on_finish(ctx, record, agent: str, outcome: str, waits_council: bool) -> None:
    """The writer ends its turn (``turn.finish``, inside its write) while a council of the card waits for it. To
    Review or Done, or to wait for the council again, it does not go past the findings (``gate``). A developer
    that answered every substantive finding, no finding of the round «fixed», and asks the owner nothing has handed in the
    head the council read: the council is settled, the rejections are the owner's to weigh — the pull request's
    decision opens (``_end``). «Fixed» with no new head is not settled: the council waits for the head."""
    if outcome in ("review", "done") or waits_council:
        gate(ctx, record, agent, "finish")
    elif outcome != "wait_user":
        return
    s = ctx.store
    for found in _owing(s, record, agent, "code"):
        current = councils.findings_of(councils.runs(s, found["id"], found["round"]))
        answers = {finding.get("answer") for finding in council.substantive(current)}
        if None in answers or any(f.get("answer") == "fixed" for f in current):
            continue  # «fixed» on a minor finding is a claim too: the council waits for the head
        if _silent_since(s, found, agent) is None:
            continue  # its question to the owner is open: what it does with the findings comes after the answer
        _end(ctx, found, record, "settled", "rejected")


def on_card_changed(ctx, event: Mapping) -> None:
    """The card changed under its council. The writer handing in the very text the council read, every
    substantive finding answered and none «fixed», settles it: the rejections are the owner's to weigh."""
    payload = event.get("payload") or {}
    before, after = payload.get("before") or {}, payload.get("after") or {}
    handed = briefs.BRIEF_READY in (
        after.get("labels") or ()
    ) and briefs.BRIEF_READY not in (before.get("labels") or ())
    if handed and event.get("actor") == "agent:" + AGENTS["brief"]:
        _guarded(
            ctx, f"{event['card']}: a hand-in", lambda: _same_handin(ctx, event["card"])
        )
    _guarded(
        ctx,
        f"{event['card']}: the card changed",
        lambda: _follow(ctx, event["card"], payload),
    )


def _follow(ctx, card_id: str, payload: Mapping) -> None:
    """An open council ends with what it stood on: the card closed (``card_closed``), its composition taken off
    the card (``removed``), a brief's card taken out of Inbox past its critics (``moved``). A card that closed or
    left Inbox has no council problem left to look at."""
    s = ctx.store
    before, after = payload.get("before") or {}, payload.get("after") or {}
    moved = before.get("status") != after.get("status")
    left = moved and before.get("status") == "Inbox"
    record = s.card(card_id)
    recomposed = "council" in (payload.get("fields") or ())
    for one in councils.of_card(s, card_id):
        if one["state"] not in councils.OPEN:
            continue
        if record.terminal:
            cancel(ctx, one, "card_closed")
        elif recomposed and not councils.effective(s, record).get(one["kind"]):
            cancel(ctx, one, "removed")
        elif one["kind"] == "brief" and left:
            cancel(ctx, one, "moved")
    if left or (moved and record.terminal):
        problems.resolve(s, card_id, PROBLEMS, by=f"card:{record.status}")


def _same_handin(ctx, card_id: str) -> None:
    s = ctx.store
    found = councils.open_for(s, card_id, "brief")
    record = s.card(card_id)
    if (
        found is None
        or found["state"] != "needs_writer"
        or not _same_basis(s, found, record)
    ):
        return  # another text is a new version: the next round starts on its event
    ids, _ = _owed(s, found, record, "handin")
    if not ids:
        _end(ctx, found, record, "settled", "rejected")
        return
    # Only where the pipeline observes — an enforcing one refused this hand-in. The writer went past the findings
    # and the hand-in stands: the card is the implementer's again and nobody is left for the council to wait for.
    # It ends with the findings open, and ``council_unanswered`` stays for the coordinator and the owner.
    _end(ctx, found, record, "limited", UNANSWERED, keep=("council_unanswered",))


def cancel(ctx, found: Mapping, reason: str, actor: str = ACTOR) -> dict:
    """The council is over without a verdict — the card closed, the composition taken off, the owner's word: its
    jobs are taken back, nobody is woken."""
    s = ctx.store
    with s.transaction():
        found = councils.row(s, found["id"])
        if found["state"] not in councils.OPEN:
            return found
        basis = found["basis"]
        if found["state"] == "running":
            _take_back(ctx, found, reason)
            _current(basis).update(ended=s._clock(), state="ended")
        return _end(
            ctx,
            found,
            s.card(found["card"]),
            "cancelled",
            reason,
            basis=basis,
            actor=actor,
        )


def approved_early(ctx, record, actor: str) -> None:
    """The work starts while the brief's council is open — the owner's "Approve", the approval of the
    coordinator's proposal: the owner's word stands, the council is over (``cancelled``/``approved_early``)."""

    def body():
        cancel_for_card(ctx, record.id, "approved_early", "brief", actor)
        problems.resolve(ctx.store, record.id, PROBLEMS, by=f"approved:{actor}")

    _guarded(ctx, f"{record.id}: an approval before the council's end", body)


def cancel_for_card(
    ctx, card_id: str, reason: str, kind: Optional[str] = None, actor: str = ACTOR
) -> int:
    """Every open council of the card (of one kind) is cancelled; how many."""
    found = [
        c
        for c in councils.of_card(ctx.store, card_id, kind)
        if c["state"] in councils.OPEN
    ]
    for one in found:
        cancel(ctx, one, reason, actor)
    return len(found)


# --- what time decides ----------------------------------------------------------------------------------------
def sweep(ctx) -> dict:
    """What the events may have missed (inside ``pipeline.sweep``): a critic's job that ended with its report never
    reaching the council, or that the host lost — its run fails and the round is played out (``lost``); a critic
    that waits for its pull request's head in the host's copy — a new job every HEAD_RETRY_S, until the round is
    ``head_wait_minutes`` old; a writer
    holding the card and silent on the findings past ``stalled_hours`` — a problem, once a round (``stalled``). A
    writer whose question to the owner is open is not silent: the hours count from the owner's answer."""
    s = ctx.store
    since = continuations._since(ctx, int(council.limits()["stalled_hours"] * 3600))
    rows = s._conn.execute(
        "SELECT id FROM councils WHERE state IN (?,?) ORDER BY created, rowid LIMIT ?",
        (*councils.OPEN, SWEEP_COUNCILS),
    ).fetchall()
    lost = stalled = 0
    for (council_id,) in rows:
        seen = _guarded(
            ctx,
            f"council {council_id}: the sweep",
            lambda council_id=council_id: _sweep_one(ctx, council_id, since),
        )
        lost, stalled = lost + (seen or (0, 0))[0], stalled + (seen or (0, 0))[1]
    return {"lost": lost, "stalled": stalled}


def _silent_since(s, found: Mapping, writer: str) -> Optional[str]:
    """Since when the writer owes the council a word: the council's last move, or the owner's answer to the
    writer's question on the card if that came later; None while such a question is open — the owner's move."""
    asked, answered = s._conn.execute(
        "SELECT COALESCE(SUM(state='open'), 0), COALESCE(MAX(decided_at), '') FROM decisions"
        " WHERE card=? AND type='question' AND requested_by=?",
        (found["card"], f"agent:{writer}"),
    ).fetchone()
    return None if asked else max(found["updated"], answered)


def _retry(ctx, found: Mapping, record, run: Mapping) -> None:
    """The critic that waited for the head gets a new job; one that cannot start any more fails its run."""
    s = ctx.store
    plan, inputs, why = _round_plan(ctx, found, record)
    job_id, reason = "", why
    if plan is not None:
        job_id, reason = _critic_job(
            ctx, plan, found, record, run["n"], run["model"], inputs
        )
    if job_id:
        _set_run(s, run, job=job_id)
        return
    _set_run(s, run, state="failed", ended=s._clock(), fail_reason=reason)
    _posted(
        ctx, found, record, councils.runs(s, found["id"], run["round"])[run["n"] - 1]
    )
    _round_end(ctx, found["id"])


def _sweep_one(ctx, council_id: str, since: str) -> tuple:
    s = ctx.store
    found = councils.row(s, council_id)
    record = s.card(found["card"])
    if found["state"] == "needs_writer":
        writer = holder = councils.writer(s, found)
        if found["kind"] == "code" and not writer:
            # a person or the coordinator holds the card: nobody on it answers a finding, and the pull request's
            # decision is held back meanwhile — the silence is said of the holder
            holder = (record.assignee or "").lstrip("@")
        elif not councils.writers_move(s, found, record):
            return 0, 0  # a card someone took from the writer waits for that someone
        silent = _silent_since(s, found, writer)
        if silent is None or silent >= since:
            return 0, 0
        raised = problems.raise_problem(
            ctx,
            key=f"council-stalled:{found['id']}:{found['round']}",
            record=record,
            kind="council_stalled",
            agent=holder,
            detail={"council": found["id"], "round": found["round"]},
        )
        return 0, int(raised is not None)
    lost = 0
    for run in councils.runs(s, found["id"], found["round"]):
        if run["state"] not in ("queued", "running"):
            continue
        job = jobs.row(s, run["job"]) if run["job"] else None
        if job is not None and job["state"] not in jobs.TERMINAL:
            continue
        if job is not None and _waits_for_head(ctx, found, job):
            if job["ended"] <= continuations._since(ctx, HEAD_RETRY_S):
                _retry(ctx, found, record, run)
            continue
        if job is not None:
            _job_end(ctx, job)  # the end its hook never told the council
        else:
            _set_run(
                s, run, state="failed", ended=s._clock(), fail_reason=jobs.HOST_LOST
            )
            ended = councils.runs(s, found["id"], run["round"])[run["n"] - 1]
            _posted(ctx, found, record, ended)
        lost += 1
    if lost:
        _round_end(ctx, found["id"])
    return lost, 0
