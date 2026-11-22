"""The council's pure rules: a critic's final block, finding ids, a round's outcome, the writer's notes."""

import pytest

from kd import council


def _finding(severity="major", **over):
    out = {
        "severity": severity,
        "location": "AC 2",
        "evidence": "the brief names no timeout",
        "scenario": "a request hangs and the owner waits forever",
        "recommendation": "name the timeout",
    }
    out.update(over)
    return out


def _block(verdict="needs_changes", findings=(), **over):
    out = {"verdict": verdict, "findings": list(findings), "questions": []}
    out.update(over)
    return out


def _run(state="done", verdict="ready", findings=(), questions=()):
    return {
        "state": state,
        "verdict": verdict,
        "findings": list(findings),
        "questions": list(questions),
    }


def test_a_good_block_gets_round_ids():
    result = council.critic_result(
        "brief", _block(findings=[_finding(), _finding("minor")]), 2, first=3
    )

    assert result["verdict"] == "needs_changes"
    assert [f["id"] for f in result["findings"]] == ["r2.f3", "r2.f4"]
    assert result["findings"][0]["severity"] == "major"
    assert set(result["findings"][0]) == {
        "id",
        "severity",
        "location",
        "evidence",
        "scenario",
        "recommendation",
    }
    assert result["questions"] == []


def test_missing_inputs_become_questions():
    block = _block(
        "blocked",
        questions=[
            {
                "text": "which currency?",
                "options": ["RUB", "EUR"],
                "recommendation": "RUB",
            },
            "a bare question",
        ],
        missing_inputs=["no schema of the export"],
    )

    result = council.critic_result("brief", block, 1)

    assert result["questions"] == [
        {"text": "which currency?", "options": ["RUB", "EUR"], "recommendation": "RUB"},
        {"text": "a bare question"},
        {"text": "no schema of the export", "missing": True},
    ]


def test_questions_and_missing_inputs_may_be_absent():
    result = council.critic_result("code", {"verdict": "ready", "findings": []}, 1)

    assert result == {"verdict": "ready", "findings": [], "questions": []}


@pytest.mark.parametrize(
    "block",
    [
        "ready",
        _block("ok"),
        {"verdict": "ready"},
        _block(findings=["the brief is vague"]),
        _block(findings=[{k: v for k, v in _finding().items() if k != "scenario"}]),
        _block(findings=[_finding(scenario="  ")]),
        _block(findings=[_finding("blocker")]),
        _block(findings=[_finding(evidence=7)]),
        _block(findings=[_finding() for _ in range(council.FINDINGS_MAX + 1)]),
        _block(findings={"0": _finding()}),
        _block(questions=[{"options": ["a"]}]),
        _block(questions=[{"text": "q", "options": "a"}]),
        _block(questions="why?"),
        _block(questions=[{"text": "q"}] * (council.QUESTIONS_MAX + 1)),
        _block(missing_inputs=[1]),
        _block("with_fixes"),  # a code verdict in a brief council
    ],
)
def test_bad_blocks_are_refused(block):
    with pytest.raises(ValueError):
        council.critic_result("brief", block, 1)


def test_extra_keys_and_where_are_tolerated():
    finding = _finding(id="x9", confidence=0.9, evidence="e" * 5000)
    finding["where"] = "  section 3  "
    del finding["location"]

    result = council.critic_result("brief", _block(findings=[finding]), 1)

    (got,) = result["findings"]
    assert got["id"] == "r1.f1"
    assert got["location"] == "section 3"
    assert len(got["evidence"]) == council.FIELD_MAX
    assert "confidence" not in got


@pytest.mark.parametrize(
    "runs, outcome",
    [
        ([_run()], "clean"),
        ([_run(findings=[_finding("minor")])], "clean"),
        ([_run(findings=[_finding("major")])], "needs_writer"),
        ([_run(findings=[_finding("critical")]), _run()], "needs_writer"),
        ([_run(questions=[{"text": "q"}])], "needs_writer"),
        ([_run(verdict="blocked")], "needs_writer"),
        ([_run("failed"), _run()], "clean"),
        ([_run("failed"), _run(findings=[_finding()])], "needs_writer"),
        ([_run("failed"), _run("failed")], "failed"),
        ([_run("failed"), _run("cancelled")], "failed"),
        ([_run("running"), _run()], None),
        ([_run("queued"), _run("failed")], None),
        ([_run("cancelled"), _run("cancelled")], None),
        ([], None),
    ],
)
def test_round_outcome(runs, outcome):
    assert council.round_outcome(runs) == outcome


def test_unanswered_lists_only_substantive():
    findings = [
        dict(_finding("major"), id="r1.f1"),
        dict(_finding("minor"), id="r1.f2"),
        dict(_finding("critical"), id="r1.f3", answer="rejected"),
    ]

    assert council.unanswered(findings) == ["r1.f1"]
    assert [f["id"] for f in council.substantive(findings)] == ["r1.f1", "r1.f3"]
    assert council.counts(findings) == {"critical": 1, "major": 1, "minor": 1}


def test_minor_findings_left_without_an_answer_are_listed_apart():
    # they hold nothing, yet the writer is asked to say what became of each
    findings = [
        dict(_finding("major"), id="r1.f1"),
        dict(_finding("minor"), id="r1.f2"),
        dict(_finding("minor"), id="r1.f3", answer="rejected"),
        dict(_finding("minor"), id="r1.f4"),
    ]

    assert council.unanswered_minor(findings) == ["r1.f2", "r1.f4"]
    assert council.unanswered(findings) == ["r1.f1"]


def test_notes_of():
    notes = council.notes_of(
        "r2.f1: the gateway already limits it; r2.f3: a later card, see the note"
    )

    assert notes == {
        "r2.f1": "the gateway already limits it",
        "r2.f3": "a later card, see the note",
    }
    assert council.notes_of("") == {}
    assert council.notes_of(None) == {}


@pytest.mark.parametrize(
    "text",
    [
        "a note without an id",
        "r1.f1: one; r1.f1: again",
        "r1.f1:",
        "r1.f1: " + "x" * (council.NOTE_MAX + 1),
        "r0.f1: no such round",
    ],
)
def test_bad_notes_are_refused(text):
    with pytest.raises(ValueError):
        council.notes_of(text)


def test_a_note_keeps_a_semicolon_that_starts_no_finding():
    assert council.notes_of("r1.f2: first; then second") == {
        "r1.f2": "first; then second"
    }


# --- the code lines of a pull request ---------------------------------------------------------------
def _file(path, additions=10, deletions=5):
    return {
        "path": path,
        "status": "modified",
        "additions": additions,
        "deletions": deletions,
    }


def test_code_lines_skip_tests_docs_and_catalogs():
    files = [
        _file("common/kd/council.py", 40, 2),
        _file("common/cabinet/web/src/lib/council.ts", 7, 1),
        _file("common/tests/test_council_logic.py", 300, 0),
        _file("agents/dev/tests/conftest.py", 20, 0),
        _file("common/kd/test_inline.py", 9, 9),
        _file("pkg/thing_test.py", 9, 9),
        _file("common/cabinet/web/src/lib/council.test.ts", 80, 0),
        _file("common/cabinet/web/e2e/council.spec.ts", 60, 0),
        _file("docs/cabinet.md", 50, 10),
        _file("README.md", 5, 5),
        _file("agents/dev/CLAUDE.md", 5, 5),
        _file("common/cabinet/web/messages/ru.json", 30, 0),
    ]

    assert council.code_lines(files) == 50


def test_code_lines_count_what_only_looks_excluded():
    files = [
        _file("common/kd/docs.py", 1, 1),  # not under docs/
        _file("common/kd/contest.py", 1, 1),  # not a test_ file
        _file("web/messages.json", 1, 1),  # not a catalog of messages/
        _file("latest/thing.py", 1, 1),  # «tests» inside a word
    ]

    assert council.code_lines(files) == 8


def test_code_lines_of_no_files_is_zero():
    assert council.code_lines([]) == 0
