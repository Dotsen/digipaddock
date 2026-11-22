"""The BOSS answer probe — the real snapshot of the audit replicas, a strict grade, every run passes."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kd import boss_probe  # noqa: E402
from kd.cabinet import snapshot  # noqa: E402
from kd.effort_probe import ProbeError  # noqa: E402

IDS = [check for check, _ in boss_probe.CHECKS]


def blocks(text):
    out, current = [], None
    for line in text.splitlines(keepends=True):
        if line.startswith("$ kd-task "):
            current = [line]
            out.append(current)
        elif current is not None:
            current.append(line)
    return ["".join(b) for b in out]


def test_transcript_is_the_real_snapshot(tmp_path):
    text = boss_probe.transcript(tmp_path)
    found = blocks(text)
    assert [b.splitlines()[0] for b in found] == [
        "$ kd-task view KD-9041", "$ kd-task result KD-9041", "$ kd-task view KD-9032", "$ kd-task result KD-9032"]
    assert all(len(b.encode("utf-8")) <= snapshot.LIMIT + 64 for b in found)
    assert "#18 merged" in found[0] and "check failed" in found[0] and "turn_ended" in found[0]
    assert "blocked_unexplained" in found[2] and "## Summary" in found[1]


def grade(**over):
    return json.dumps({**{i: True for i in IDS}, **over})


def test_parse_grade_is_strict():
    assert boss_probe.parse_grade(grade()) == {i: True for i in IDS}
    assert boss_probe.parse_grade("```json\n" + grade(limits_named=False) + "\n```")["limits_named"] is False
    for bad in (grade(extra=True), json.dumps({i: True for i in IDS[1:]}), grade(merged_part="yes"), "no json", "[]"):
        with pytest.raises(ProbeError):
            boss_probe.parse_grade(bad)


def stream(answer):
    return [json.dumps({"type": "system", "subtype": "init", "model": "m", "session_id": "s"}),
            json.dumps({"type": "result", "result": answer, "is_error": False, "total_cost_usd": 0.01,
                        "usage": {"output_tokens": 10}})]


class Fake:
    def __init__(self, grades):
        self.calls, self.grades = [], list(grades)

    def __call__(self, argv, stdin, cwd, env, timeout):
        self.calls.append((argv, stdin, env))
        if argv[1:] == ["--help"]:
            return 0, ["  --tools <tools...>", "  --system-prompt <prompt>"]
        if "--system-prompt" in argv and argv[argv.index("--model") + 1] == "grader":
            return 0, stream(self.grades.pop(0))
        return 0, stream("Подтверждено: PR #18 слит.")


def test_run_with_a_fake_runner(tmp_path):
    instructions = tmp_path / "CLAUDE.md"
    instructions.write_text("# BOSS\nYou coordinate.\n", "utf-8")
    out = tmp_path / "out"
    fake = Fake([grade(), grade(limits_named=False)])
    rows = boss_probe.run(instructions=instructions, out=out, model="claude-sonnet-5-5", grader="grader", repeats=2,
                          runner=fake, home=str(tmp_path))
    boss = fake.calls[1][0]
    assert boss[:2] == ["claude", "-p"] and boss[boss.index("--model") + 1] == "claude-sonnet-5-5"
    assert boss[boss.index("--tools") + 1] == "" and boss[boss.index("--system-prompt") + 1] == "# BOSS\nYou coordinate.\n"
    assert "--strict-mcp-config" in boss and boss_probe.QUESTION in fake.calls[1][1]
    assert "$ kd-task view KD-9041" in fake.calls[1][1]
    assert "Подтверждено: PR #18 слит." in fake.calls[2][1] and fake.calls[2][0][fake.calls[2][0].index("--model") + 1] == "grader"
    assert fake.calls[1][2]["HINDSIGHT_CONFIG"] == str(out / "hindsight-off.json")
    assert [r["passed"] for r in rows] == [True, False]
    lines = (out / "runs.jsonl").read_text("utf-8").splitlines()
    assert len(lines) == 2 and json.loads(lines[1])["grade"]["limits_named"] is False
    with pytest.raises(ProbeError):
        boss_probe.run(instructions=instructions, out=out, model="m", grader="grader", repeats=1, runner=fake,
                       home=str(tmp_path))


def test_run_needs_the_options(tmp_path):
    instructions = tmp_path / "CLAUDE.md"
    instructions.write_text("x", "utf-8")
    with pytest.raises(ProbeError):
        boss_probe.run(instructions=instructions, out=tmp_path / "o", model="m", grader="g", repeats=1,
                       runner=lambda *a: (0, ["  --model"]), home=str(tmp_path))


def test_summary_needs_every_check_in_every_run():
    ok = {"repeat": 1, "grade": {i: True for i in IDS}, "passed": True}
    bad = {"repeat": 2, "grade": {**{i: True for i in IDS}, "nobody_on_9041": False}, "passed": False}
    assert boss_probe.summary([ok, dict(ok, repeat=2)]).rstrip().endswith("A03 passed")
    text = boss_probe.summary([ok, bad])
    assert "A03 failed" in text and "nobody_on_9041" in text
    assert "A03 failed" in boss_probe.summary([ok, {"repeat": 2, "grade": None, "passed": False, "error": "x"}])
    assert "A03 failed" in boss_probe.summary([])
