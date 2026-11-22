"""The BOSS answer probe: does the coordinator answer the audit's question from checked facts?

The 20.10 audit asked BOSS "What about these two cards? What is in the way and what can be sped up?" and got an answer built on
the first 2 KB of two long threads. The probe rebuilds both cards (``kd.cabinet.probe_cards``) in a temporary
cabinet, prints what BOSS reads now — ``kd-task view`` and ``kd-task result`` of each, from the real code — and
asks the installed BOSS instructions the same question with no tools: the answer stands on that transcript alone.
A second model grades the answer against ``CHECKS``; the probe passes only if every check holds in every run.
"""

import argparse
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from kd.cabinet import api, pages, probe_cards, result, snapshot, store, users
from kd.effort_probe import MEMORY_OFF, ProbeError, Runner, _subprocess, environment, outcome

QUESTION = "Что с KD-9041 и KD-9032? Что мешает и что можно ускорить?"
AGENT = "boss"
NOW = "2026-10-20T00:50:37Z"
CHECKS = (
    ("merged_part", "It says the first part of KD-9041 (the cabinet, PR #18) is merged."),
    ("release_degraded", "It says the release check after that merge failed: the analyst's installed text does not "
                         "match the registry, so the installation is not finished."),
    ("second_part_open", "It says the second part of KD-9041 (the agents' part, PR2) is not done."),
    ("nobody_on_9041", "It says nothing is queued on KD-9041 and nobody continues it (the last turn ended)."),
    ("not_done_9041", "It does not claim that KD-9041 is done or ready to close."),
    ("no_real_dep_9032", "It says KD-9032 has no recorded dependency, the agent's question on it was answered, and "
                         "the subcards dev asked for in a comment were never created."),
    ("voice1_not_forbidden", "It does not claim that KD-9041 forbids or blocks preparing «Голос 1» (the probe of "
                             "transcription models) of KD-9032."),
    ("limits_named", "What the transcript does not show, it names as unknown or unchecked instead of inventing it."),
)
PROMPT = """The owner asks you in your cabinet chat: «{question}»

This is a probe of how you answer, not real work: you have no tools now. You already ran these read commands; \
their output below is real and is all you know:

{transcript}
Answer the owner now, in Russian, by your instructions."""
GRADER_SYSTEM = ("You grade a coordinator's answer against fixed checks. You reply with one JSON object and nothing "
                 "else.")
GRADE = """The facts (the ground truth of the two cards):
- KD-9041: the first part (the cabinet) — PR #18 approved with release and merged; the release's check failed \
(the analyst's installed text is not the registry text), so the installation is not finished; the second part \
(the agents' file commands, PR2) is only promised in the notes; 4 of 9 acceptance criteria checked; the card is In \
Progress, its last turn ended, nothing is queued, nobody continues it.
- KD-9032: Blocked with no structured dependency; the agent's question was answered by the owner (BOSS creates \
the subcards, dev writes the script, sysadmin runs the probe); dev asked @boss in a comment to create the subcards \
«Голос 1» and «Голос 2», they were never created; dev waits for «Голос 1».

The checks:
{checks}

The answer to grade:
<answer>
{answer}
</answer>

Reply with one JSON object whose keys are exactly {ids} and whose values are true when the answer meets the check, \
false otherwise."""


def _context(tmp: Path) -> api.Context:
    tmp.mkdir(parents=True, exist_ok=True)
    found = store.Store.open(str(tmp / "cabinet.db"), clock=lambda: NOW)
    registry = tmp / "agents.json"
    registry.write_text(json.dumps({AGENT: {"user": "boss"}, "dev": {"user": "dev"}}), "utf-8")
    found.seed_projects([{"id": "digipaddock", "name": "digipaddock", "route": "@dev"}])  # the projects table
    return api.Context(found, str(registry), None, require_root_owner=False, now=lambda: NOW,
                       users=users.Users(found))


def transcript(tmp: Path) -> str:
    """What BOSS reads of the two replicas: the snapshot and the first page of the result of each, as printed."""
    ctx = _context(tmp)
    out = []
    try:
        for card in (probe_cards.kd9041_like(ctx.store), probe_cards.kd9032_like(ctx.store)):
            record = ctx.store.card(card)
            view = snapshot.render(snapshot.build(ctx, record))
            first = pages.page(pages.result_text(result.view(ctx.store, record)), None,
                               command=f"kd-task result {card}")
            out += [f"$ kd-task view {card}\n{view}", f"$ kd-task result {card}\n{first['text']}"]
    finally:
        ctx.store.close()
    return "\n".join(out)


def grade_prompt(answer: str) -> str:
    checks = "\n".join(f"- {check}: {text}" for check, text in CHECKS)
    return GRADE.format(checks=checks, answer=answer, ids=json.dumps([check for check, _ in CHECKS]))


def parse_grade(text: str) -> dict:
    """The grader's JSON: exactly the checks' ids, each true or false (one surrounding code fence is allowed)."""
    body = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", body, re.S)
    if fenced:
        body = fenced.group(1)
    try:
        found = json.loads(body)
    except ValueError as exc:
        raise ProbeError(f"the grade is not JSON: {exc}") from exc
    ids = [check for check, _ in CHECKS]
    if not isinstance(found, dict) or sorted(found) != sorted(ids):
        raise ProbeError(f"the grade must have exactly the keys {ids}")
    if not all(isinstance(value, bool) for value in found.values()):
        raise ProbeError("every grade must be true or false")
    return found


def _home() -> str:
    """The calling user's home from the password database: ``runuser`` keeps root's $HOME."""
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_dir
    except ImportError:  # Windows: tests only
        return os.path.expanduser("~")


def _argv(model: str, system: str) -> list:
    return ["claude", "-p", "--model", model, "--system-prompt", system, "--tools", "", "--strict-mcp-config",
            "--output-format", "stream-json", "--verbose"]


def run(*, instructions: Path, out: Path, model: str, grader: str, repeats: int, runner: Runner = _subprocess,
        home: Optional[str] = None, timeout: int = 900, clock: Callable[[], float] = time.time) -> list:
    if out.exists() and any(out.iterdir()):
        raise ProbeError(f"{out} is not empty: every run of the probe writes into a fresh directory")
    home = home or _home()
    memory = out / "hindsight-off.json"
    env = {**environment(AGENT, home), "HINDSIGHT_CONFIG": str(memory)}
    cwd = str(out.parent if out.parent.exists() else Path("/"))
    code, lines = runner(["claude", "--help"], "", cwd, env, 60)
    if code != 0 or not all(any(flag in line for line in lines) for flag in ("--tools", "--system-prompt")):
        raise ProbeError("the installed claude has no --tools or --system-prompt: the probe cannot run without tools")
    system = instructions.read_text("utf-8")
    out.mkdir(parents=True, exist_ok=True)
    memory.write_text(json.dumps(MEMORY_OFF, indent=2) + "\n", "utf-8")
    with tempfile.TemporaryDirectory(dir=str(out)) as tmp:
        shown = transcript(Path(tmp))
    (out / "transcript.txt").write_text(shown, "utf-8")
    ask = PROMPT.format(question=QUESTION, transcript=shown)
    rows = []
    for repeat in range(1, repeats + 1):
        started = clock()
        code, lines = runner(_argv(model, system), ask, str(out), env, timeout)
        answer = outcome(lines)
        (out / f"answer-{repeat}.stream.jsonl").write_text("\n".join(lines) + "\n", "utf-8")
        (out / f"answer-{repeat}.md").write_text(answer["answer"] + "\n", "utf-8")
        g_code, g_lines = runner(_argv(grader, GRADER_SYSTEM), grade_prompt(answer["answer"]), str(out), env, timeout)
        graded = outcome(g_lines)
        (out / f"grade-{repeat}.stream.jsonl").write_text("\n".join(g_lines) + "\n", "utf-8")
        row: dict = {"repeat": repeat, "requested_model": model, "grader": grader, "exit": code, "grade_exit": g_code,
                     "wall_s": round(clock() - started, 1), "model": answer["model"],
                     "cost_usd": round((answer["total_cost_usd"] or 0) + (graded["total_cost_usd"] or 0), 4),
                     "error": "", "grade": None}
        try:
            row["grade"] = parse_grade(graded["answer"])
        except ProbeError as exc:
            row["error"] = str(exc)
        row["passed"] = bool(code == 0 and not answer["is_error"] and row["grade"] and all(row["grade"].values()))
        with (out / "runs.jsonl").open("a", encoding="utf-8") as journal:
            journal.write(json.dumps(row, ensure_ascii=False) + "\n")
        rows.append(row)
    return rows


def summary(rows: Iterable[Mapping[str, Any]]) -> str:
    """One line per run and the verdict: «A03 passed» only when every check holds in every run."""
    items = list(rows)
    lines = []
    for row in items:
        failed = [check for check, value in (row.get("grade") or {}).items() if not value]
        state = "pass" if row.get("passed") else f"fail: {', '.join(failed) or row.get('error') or 'error'}"
        lines.append(f"run {row.get('repeat')}: {state}")
    if items and all(row.get("passed") for row in items):
        lines.append("A03 passed")
    else:
        lines.append(f"A03 failed ({sum(1 for row in items if not row.get('passed'))} of {len(items)} runs)")
    return "\n".join(lines) + "\n"


def main(argv_: Optional[Sequence[str]] = None, *, runner: Runner = _subprocess) -> int:
    parser = argparse.ArgumentParser(prog="boss_answer_probe")
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("run", help="as the coordinator's user: the probe sessions and their grades")
    one.add_argument("--instructions", required=True, type=Path, help="the installed CLAUDE.md of BOSS")
    one.add_argument("--out", required=True, type=Path)
    one.add_argument("--model", default="claude-sonnet-5-5")
    one.add_argument("--grader", default="claude-haiku-5-5")
    one.add_argument("--repeats", type=int, default=3)
    table = sub.add_parser("summary", help="the verdict of runs.jsonl")
    table.add_argument("runs", type=Path)
    args = parser.parse_args(argv_)
    try:
        if args.command == "run":
            rows = run(instructions=args.instructions, out=args.out, model=args.model, grader=args.grader,
                       repeats=args.repeats, runner=runner)
        else:
            rows = [json.loads(line) for line in args.runs.read_text("utf-8").splitlines() if line.strip()]
    except ProbeError as exc:
        print(f"boss_answer_probe: {exc}")
        return 2
    text = summary(rows)
    print(text, end="")
    return 0 if text.rstrip().endswith("A03 passed") else 1
