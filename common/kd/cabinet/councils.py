"""The council's composition in the cabinet: ``kd.council`` checked against the cabinet's models.

A critic is a model a card may run on — one of the subscription (``claude:<alias>``) or an enabled cabinet model.
A critic the composition already names stays its critic whatever became of the model since (disabled, an alias a
later catalog dropped): the rest of the composition is edited around it, and it is only not taken anew.
The composition is written by ``user.card.edit`` (``changes.council``) and ``project.update`` (``council``), both
with ``settings``: it is a decision about spend. Everyone who sees the card reads it; no agent writes it.
"""

import json
import re
from typing import List, Mapping, Optional

from kd import council
from kd.cabinet import api, llm_api, store


POOLS = {
    "brief": "analyst",
    "code": "dev",
}  # whose worker pool the critics of a kind read in


def check(
    db: store.Store,
    value,
    *,
    keep_empty: bool = True,
    stored=None,
    ctx=None,
    project: str = "",
) -> dict:
    """The composition a person sent, in its one form (``kd.council.normalize``); ApiError ``invalid`` otherwise —
    an unknown or a disabled model among the critics too, unless ``stored`` (the composition this one replaces)
    names it already. With ``ctx``, critics of the code where ``stored`` had none need what their jobs need
    : a repository of ``project`` and worker slots of the developer."""
    kept = council.critics(stored or {})

    def critic(name):
        if name not in kept:
            llm_api.check_model(db, name)

    try:
        out = council.normalize(value, critic, keep_empty)
    except ValueError as exc:
        raise api.ApiError("invalid", f"council: {exc}") from None
    except api.ApiError as exc:
        raise api.ApiError(exc.kind, f"council: {exc}") from None
    had = ((stored or {}).get("code") or {}).get("critics")
    if ctx is not None and (out.get("code") or {}).get("critics") and not had:
        why = code_refusal(ctx, project)
        if why:
            raise api.ApiError("invalid", f"council: code: {why}")
    return out


def code_refusal(ctx, project: str) -> str:
    """Why a card of ``project`` can have no code council, or '': its critics are worker jobs of the developer on
    the project's copy."""
    from kd import worker_slots
    from kd.cabinet import projects_api  # at call time: it imports the API

    if project not in projects_api.bound_repos(ctx.repos_path):
        return f"project {project or '—'} has no repository"
    try:
        config = worker_slots.load_config(
            ctx.tree() / "common" / "worker" / "worker-slots.json"
        )
    except (worker_slots.SlotError, OSError, ValueError):
        return "the worker slots are not readable"
    if POOLS["code"] not in config.agents:
        return "the developer has no worker slots"
    return ""


def effective(db: store.Store, card: store.CardRecord) -> dict:
    """The composition in force on the card: its own per kind, else its project's; ``{}`` — no council."""
    project = db.project(card.project) or {}
    return council.merge(card.council, project.get("council") or {})


# --- a gathering and its runs: the read side; ``council_rounds`` writes them ---------------------------
OPEN = ("running", "needs_writer")
ENDED = ("clean", "settled", "limited", "cancelled", "failed")
RUN_ENDED = ("done", "failed", "cancelled")
COUNCIL_ID = re.compile(r"[0-9a-f]{32}")
WRITERS = {
    "brief": "analyst"
}  # who answers the findings; the code's writer is the card's holder
NOT_WRITERS = (
    api.OWNER,
    "@boss",
)  # a person and the coordinator answer no finding (the code's council)


def _council(found) -> dict:
    out = dict(found)
    out["basis"] = json.loads(out["basis"] or "{}")
    out["critics"] = json.loads(out["critics"] or "[]")
    return out


def _run(found) -> dict:
    out = dict(found)
    for key in ("findings", "questions"):
        out[key] = json.loads(out[key] or "[]")
    out["usage"] = json.loads(out["usage"]) if out["usage"] else None
    return out


def row(db: store.Store, council_id) -> Optional[dict]:
    """One gathering — ``basis`` and ``critics`` parsed — or None."""
    if not isinstance(council_id, str) or not COUNCIL_ID.fullmatch(council_id):
        return None
    found = db._conn.execute(
        "SELECT * FROM councils WHERE id=?", (council_id,)
    ).fetchone()
    return None if found is None else _council(found)


def runs(db: store.Store, council_id: str, round_no: Optional[int] = None) -> list:
    """The critics' runs of the gathering (of one round), by round and number; findings, questions, usage parsed."""
    where, args = "council=?", [council_id]
    if round_no is not None:
        where, args = where + " AND round=?", args + [round_no]
    found = db._conn.execute(
        f"SELECT * FROM council_runs WHERE {where} ORDER BY round, n", args
    ).fetchall()
    return [_run(r) for r in found]


def run_of_job(db: store.Store, job_id: str) -> Optional[dict]:
    found = db._conn.execute(
        "SELECT * FROM council_runs WHERE job=?", (job_id,)
    ).fetchone()
    return None if found is None else _run(found)


def of_card(db: store.Store, card_id: str, kind: Optional[str] = None) -> list:
    """The card's gatherings, the newest first."""
    where, args = "card=?", [card_id]
    if kind is not None:
        where, args = where + " AND kind=?", args + [kind]
    found = db._conn.execute(
        f"SELECT * FROM councils WHERE {where} ORDER BY created DESC, rowid DESC", args
    ).fetchall()
    return [_council(r) for r in found]


def latest(db: store.Store, card_id: str, kind: Optional[str] = None) -> Optional[dict]:
    found = of_card(db, card_id, kind)
    return found[0] if found else None


def open_for(db: store.Store, card_id: str, kind: str) -> Optional[dict]:
    """The card's gathering of the kind that has not ended: at most one."""
    found = latest(db, card_id, kind)
    return found if found is not None and found["state"] in OPEN else None


def open_map(db: store.Store) -> dict:
    """``{card: [{kind, round, rounds, state}]}`` of the gatherings that have not ended, a brief's first — what the
    card list and the overview say of each card."""
    out = {}
    for found in db._conn.execute(
        "SELECT card, kind, round, rounds_max, state FROM councils WHERE state IN (?,?) ORDER BY kind",
        OPEN,
    ):
        out.setdefault(found["card"], []).append(
            {
                "kind": found["kind"],
                "round": found["round"],
                "rounds": found["rounds_max"],
                "state": found["state"],
            }
        )
    return out


def of_pull(db: store.Store, card_id: str, repo: str, number: int) -> list:
    """The card's gatherings over one pull request, the newest first: each pull request of a card has
    its own."""
    return [
        found
        for found in of_card(db, card_id, "code")
        if (found["basis"].get("repo"), found["basis"].get("number")) == (repo, number)
    ]


def open_for_pull(
    db: store.Store, card_id: str, repo: str, number: int
) -> Optional[dict]:
    """The pull request's gathering that has not ended: at most one."""
    found = of_pull(db, card_id, repo, number)
    return found[0] if found and found[0]["state"] in OPEN else None


def for_head(
    db: store.Store, card_id: str, repo: str, number: int, head: str
) -> Optional[dict]:
    """The pull request's last gathering, when the head it stands on is this one; None — nobody gathered over
    this head, or the gathering has moved to another."""
    found = of_pull(db, card_id, repo, number)
    return found[0] if found and found[0]["basis"].get("head") == head else None


def writer(db: store.Store, found: Mapping) -> str:
    """The agent that owes the findings an answer: the analyst for a brief, the card's holder for the code — ''
    while a person or the coordinator holds the card."""
    if found["kind"] in WRITERS:
        return WRITERS[found["kind"]]
    try:
        holder = db.card(found["card"]).assignee or ""
    except store.NotFound:
        return ""
    return "" if holder in NOT_WRITERS else holder.lstrip("@")


def replanned(record) -> bool:
    """An aggregate in work: a card with stages in To Do or In Progress. Its plan is the analyst's to write anew
    without holding the card (``worker_board._replanning``), so its council never takes it from its
    holder."""
    return bool(record.stages) and record.status in ("To Do", "In Progress")


def writers_move(db: store.Store, found: Mapping, record) -> bool:
    """Whether the council's writer can answer on the card as it is held now: it holds the card, or the card is
    an aggregate in work whose brief it re-plans from outside."""
    agent = writer(db, found)
    if not agent:
        return False
    return record.assignee == "@" + agent or (
        found["kind"] == "brief" and replanned(record)
    )


def findings_of(found_runs) -> list:
    return [finding for run in found_runs for finding in run["findings"]]


def facts(db: store.Store, card_id: str, kind: Optional[str] = None) -> Optional[dict]:
    """The card's last gathering in short — for the brief's state, the next step, the snapshot; None without one."""
    found = latest(db, card_id, kind)
    return None if found is None else _facts(db, found)


def open_facts(db: store.Store, card_id: str, kind: str) -> Optional[dict]:
    """The card's gathering of the kind that has not ended, in short — the newest such: a card with two pull
    requests may have a council of the code over each."""
    found = next(
        (one for one in of_card(db, card_id, kind) if one["state"] in OPEN), None
    )
    return None if found is None else _facts(db, found)


def _facts(db: store.Store, found: Mapping) -> dict:
    current = runs(db, found["id"], found["round"])
    basis = found["basis"]
    return {
        "id": found["id"],
        "kind": found["kind"],
        "state": found["state"],
        "round": found["round"],
        "rounds": found["rounds_max"],
        "done": sum(run["state"] in RUN_ENDED for run in current),
        "total": len(current),
        "open_findings": len(council.unanswered(findings_of(current))),
        "end_reason": found["end_reason"],
        "version": basis.get("version"),
        # what a council of the code stands on — the pull request and its head
        **(
            {"number": basis.get("number"), "head": basis.get("head", "")}
            if found["kind"] == "code"
            else {}
        ),
    }


def pull_review(db: store.Store, card_id: str, payload: Mapping) -> Optional[dict]:
    """What the card's council of the code says of a pull request's head — for the decision on that head
    : ``{state, council, lines, min_lines, rejected}``. ``state`` is how the gathering over this head
    stands (``clean``, ``settled``, ``limited``, ``failed``, ``cancelled``; ``running`` or ``needs_writer`` while
    it is open), ``skipped`` — the pull request changes fewer lines of code than ``min_lines``, nobody gathered —
    or ``none``: nobody gathered though the card has critics of the code (they came after the decision, or the
    council could not gather). ``rejected`` — the substantive findings of its last round the writer answered
    «rejected» or «later». None — nothing to say: no critics of the code and no gathering, or the pull request is
    on another head by now."""
    try:
        record = db.card(card_id)
    except store.NotFound:
        return None
    repo, number, head = payload.get("repo"), payload.get("number"), payload.get("head")
    row = db.pr(repo, number, diff=False)
    if row is None or row["head"] != head:
        return None
    part = effective(db, record).get("code")
    found = for_head(db, card_id, repo, number, head)
    if found is None and not part:
        return None
    said = {
        "council": None,
        "lines": council.code_lines(row["files"]),
        "min_lines": int((part or {}).get("min_lines") or 0),
        "rejected": 0,
    }
    if found is None:
        return {
            "state": "skipped" if said["lines"] < said["min_lines"] else "none",
            **said,
        }
    current = findings_of(runs(db, found["id"], found["round"]))
    rejected = sum(
        finding.get("answer") in ("rejected", "later")
        for finding in council.substantive(current)
    )
    return {
        "state": found["state"],
        **said,
        "council": found["id"],
        "rejected": rejected,
    }


# What the usage ledger holds of some jobs, a row per job and model, a job's costliest model first.
RUN_USAGE = ("SELECT source, model, SUM(cost) FROM usage_log"
             " WHERE kind='job' AND source IN (SELECT value FROM json_each(?))"
             " GROUP BY source, model ORDER BY source, SUM(cost) DESC, SUM(output) DESC, model")


def _usage(db: store.Store, found_runs) -> tuple:
    """``({job: cost}, {job: [models]})`` of the critics' jobs in one read of the usage ledger: what it priced each
    at, and the models it names as having served each, the costliest first."""
    ids = [run["job"] for run in found_runs if run["job"]]
    costs, served = {}, {}
    if not ids:
        return costs, served
    for source, model, cost in db._conn.execute(RUN_USAGE, (json.dumps(ids),)).fetchall():
        costs[source] = costs.get(source, 0) + (cost or 0)
        served.setdefault(source, []).append(model)
    return {source: round(cost, 6) for source, cost in costs.items()}, served


def view(db: store.Store, found: Mapping) -> dict:
    """One gathering as its block on the card shows it: its state, then round by round each critic's run — the
    verdict, what it cost, the models that served it (``served``), the findings with the writer's answers in them,
    the questions. ``same_model`` — the critic read on the model the writer wrote on: a second opinion of the same
    mind."""
    all_runs = runs(db, found["id"])
    costs, served = _usage(db, all_runs)
    rounds = []
    for entry in found["basis"].get("rounds", ()):
        rounds.append(
            {
                "round": entry["round"],
                "version": entry.get("version"),  # a brief's round
                "head": entry.get("head", ""),  # a round of the code
                "state": entry["state"],
                "started": entry["started"],
                "ended": entry["ended"],
                "runs": [
                    {
                        "n": run["n"],
                        "model": run["model"],
                        "state": run["state"],
                        "verdict": run["verdict"],
                        "counts": council.counts(run["findings"]),
                        "cost": costs.get(run["job"], 0),
                        "served": served.get(run["job"], []),
                        "fail_reason": run["fail_reason"],
                        "same_model": bool(entry.get("writer_model"))
                        and run["model"] == entry["writer_model"],
                        "findings": run["findings"],
                        "questions": run["questions"],
                    }
                    for run in all_runs
                    if run["round"] == entry["round"]
                ],
            }
        )
    return {
        "id": found["id"],
        "kind": found["kind"],
        "card": found["card"],
        "state": found["state"],
        "end_reason": found["end_reason"],
        "round": found["round"],
        "rounds": found["rounds_max"],
        "created": found["created"],
        "ended": found["ended"],
        "basis": _basis_view(db, found),
        "rounds_list": rounds,
        **_held_view(db, found),
    }


def _held_view(db: store.Store, found: Mapping) -> dict:
    """``pull`` — the pull request an open council of the code holds back, as its decision will show it
    : the owner reads its files, its cage and its diff while the critics do, with nothing to decide yet.
    Absent once the council ended: the decision shows it from there on."""
    payload = (found["basis"].get("held") or {}).get("payload")
    if found["kind"] != "code" or found["state"] not in OPEN or not payload:
        return {}
    from kd.cabinet import prs_api  # at call time: its operations load this module

    return {"pull": prs_api.pull_view(db, payload)}


def _basis_view(db: store.Store, found: Mapping) -> dict:
    """What the gathering stands on: a brief's version, or the pull request at its head — with the
    address the forge has it at."""
    basis = found["basis"]
    if found["kind"] == "code":
        row = db.pr(basis.get("repo"), basis.get("number"), diff=False)
        return {
            **{
                key: basis.get(key)
                for key in ("repo", "project", "number", "head", "target")
            },
            "url": row["url"] if row else "",
        }
    return {"version": basis.get("version"), "scope_sha": basis.get("scope_sha", "")}


def rounds_by_version(db: store.Store, card_id: str) -> dict:
    """``{brief version: the round that read it}`` — ``{council, round, state, runs: [{model, state, verdict}]}``;
    a version two gatherings read names the later one."""
    out = {}
    for found in reversed(of_card(db, card_id, "brief")):
        by_round = {}
        for run in runs(db, found["id"]):
            by_round.setdefault(run["round"], []).append(
                {key: run[key] for key in ("model", "state", "verdict")}
            )
        for entry in found["basis"].get("rounds", ()):
            out[entry["version"]] = {
                "council": found["id"],
                "round": entry["round"],
                "state": entry["state"],
                "runs": by_round.get(entry["round"], []),
            }
    return out


# --- a run as text: the writer's page (``kd-work council``) and the next round's critics read the same lines ----
def answer_line(finding: Mapping) -> str:
    """The writer's answer to a finding as the record has it: the answer, the card it went to, the reason."""
    if not finding.get("answer"):
        return "  answer: none"
    text = finding["answer"] + (f" {finding['card']}" if finding.get("card") else "")
    return f"  answer: {text}" + (
        f" — {finding['note']}" if finding.get("note") else ""
    )


def basis_label(kind: str, entry: Mapping) -> str:
    """What a round read, in a word: a brief's version, a pull request's head."""
    if kind == "code":
        return "head " + str(entry.get("head", ""))[:12]
    return f"v{entry.get('version', '?')}"


def run_head(run: Mapping) -> str:
    """A critic's run in a line: its number, model and state, the verdict with the counts or why it failed."""
    head = f"#{run['n']} {run['model']} {run['state']}"
    if run["state"] == "done":
        counts = council.counts(run["findings"])
        head += f" {run['verdict']} · " + " ".join(
            f"{key} {counts[key]}" for key in council.SEVERITIES
        )
    elif run["fail_reason"]:
        head += f" — {run['fail_reason']}"
    return head


def finding_lines(finding: Mapping) -> List[str]:
    """A finding whole: its line, what the critic said of it, the writer's answer."""
    return [
        f"{finding['id']} {finding['severity']} {finding['location']}",
        f"  scenario: {finding['scenario']}",
        f"  evidence: {finding['evidence']}",
        f"  recommendation: {finding['recommendation']}",
        answer_line(finding),
    ]


def question_lines(run: Mapping) -> List[str]:
    """A critic's questions and the inputs it missed, a line each."""
    lines = []
    for question in run["questions"]:
        line = (
            "missing input: " if question.get("missing") else "question: "
        ) + question["text"]
        if question.get("options"):
            line += " · options: " + " | ".join(question["options"])
        if question.get("recommendation"):
            line += f" · recommendation: {question['recommendation']}"
        lines.append(line)
    return lines


def run_lines(run: Mapping) -> List[str]:
    """One critic's run: its head, each finding with the writer's answer, its questions and missing inputs."""
    lines = [run_head(run)]
    for finding in run["findings"]:
        lines += finding_lines(finding)
    return lines + question_lines(run)
