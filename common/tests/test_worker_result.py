"""C06: kd.worker_result — the record of one worker job, its outcome, summary and card comment."""

import json

from kd import worker_jobs as wj
from kd import worker_result as wrs

RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "duration_ms": 1200,
    "num_turns": 3,
    "result": 'Done.\n```json\n{"status": "done", "commit": "abc", "tests": "pytest -q: 3 passed", "notes": "ok"}\n```\n',
    "total_cost_usd": 0.0123,
    "usage": {
        "input_tokens": 10,
        "output_tokens": 20,
        "cache_creation_input_tokens": 5,
        "cache_read_input_tokens": 7,
    },
    "modelUsage": {"claude-sonnet-5-5": {"inputTokens": 10, "outputTokens": 20}},
}
JOB = {
    "id": "a" * 32,
    "card": "KD-121",
    "agent": "dev",
    "type": "developer",
    "spec": "implementer",
    "skills": [],
    "prompt": {"spec": "implementer", "version": 1, "sha256": "f" * 64, "skills": {}},
    "limits": {"write": "repo"},
    "caller": "digi-boss",
}
FACTS = {
    "base": "b" * 40,
    "branch": "worker/" + "a" * 32,
    "slot": "dev/worker1",
    "started": 1.0,
    "ended": 3.0,
}
NONE = {
    "base": None,
    "branch": None,
    "slot": "dev/worker1",
    "started": 1.0,
    "ended": 2.0,
}


def test_final_block_is_the_last_json_block_with_a_status():
    assert wrs.final_block(RESULT["result"]) == {
        "status": "done",
        "commit": "abc",
        "tests": "pytest -q: 3 passed",
        "notes": "ok",
    }
    assert wrs.final_block("no block") is None
    assert wrs.final_block("```json\n[1]\n```") is None
    assert wrs.final_block('```json\n{"status": "maybe"}\n```') is None
    two = '```json\n{"status": "blocked", "why": "x"}\n```\ntext\n```json\n{"status": "done"}\n```\n'
    assert wrs.final_block(two) == {"status": "done"}
    assert wrs.final_block(
        '```json\n{"status": "done", "x": "{"}\n```\n```json\nnot json\n```'
    ) == {"status": "done", "x": "{"}


def test_usage_comes_from_the_result_event():
    assert wrs.usage_of(RESULT) == {
        "model": "claude-sonnet-5-5",
        "input": 10,
        "output": 20,
        "cache_write": 5,
        "cache_read": 7,
        "requests": 3,
        "cost_usd": 0.0123,
    }
    assert wrs.usage_of({})["model"] == "?" and wrs.usage_of(None)["cost_usd"] == 0.0


def test_build_binds_the_record_to_job_base_spec_and_commit():
    rec = wrs.build(
        JOB,
        FACTS,
        {"exit": 0, "timed_out": False, "result_event": RESULT, "output_bytes": 10},
        head="c" * 40,
        stat=" README.md | 2 +-",
        patch="/srv/digipaddock-agents/dev/work/tmp/worker-aaa.patch",
    )
    assert (
        rec["schema_version"] == 1
        and rec["job"] == JOB["id"]
        and rec["card"] == "KD-121"
    )
    assert rec["provenance"] == {
        "spec": "implementer",
        "version": 1,
        "sha256": "f" * 64,
        "skills": {},
        "base": "b" * 40,
        "commit": "c" * 40,
    }
    assert (
        rec["result"]["status"] == "done"
        and rec["usage"]["cost_usd"] == 0.0123
        and rec["duration_s"] == 2.0
    )
    assert wrs.outcome_of(rec) == ("done", "")
    assert "transcript" not in json.dumps(rec) and "credentials" not in json.dumps(rec)
    assert json.loads(json.dumps(rec)) == rec  # a plain JSON record


def test_outcomes_follow_exit_timeout_and_block():
    rec = wrs.build(
        JOB,
        NONE,
        {"exit": 1, "timed_out": False, "result_event": None, "output_bytes": 0},
        head=None,
        stat="",
        patch=None,
    )
    assert wrs.outcome_of(rec) == ("failed", "claude exited 1 without a result event")
    rec = wrs.build(
        JOB,
        NONE,
        {"exit": -9, "timed_out": True, "result_event": None, "output_bytes": 0},
        head=None,
        stat="",
        patch=None,
    )
    assert wrs.outcome_of(rec)[0] == "failed" and "timeout" in wrs.outcome_of(rec)[1]
    rec = wrs.build(
        JOB,
        NONE,
        {
            "exit": 0,
            "timed_out": False,
            "result_event": {**RESULT, "result": "no block"},
            "output_bytes": 0,
        },
        head=None,
        stat="",
        patch=None,
    )
    assert wrs.outcome_of(rec) == ("no-result", wj.NO_BLOCK)
    rec = wrs.build(
        JOB,
        NONE,
        {
            "exit": 1,
            "timed_out": False,
            "result_event": {**RESULT, "is_error": True, "subtype": "error_max_turns"},
            "output_bytes": 0,
        },
        head=None,
        stat="",
        patch=None,
    )
    assert wrs.outcome_of(rec) == ("failed", "claude reported error_max_turns")
    blocked = {
        **RESULT,
        "result": '```json\n{"status": "blocked", "notes": "needs the owner"}\n```',
    }
    rec = wrs.build(
        JOB,
        NONE,
        {"exit": 0, "timed_out": False, "result_event": blocked, "output_bytes": 0},
        head=None,
        stat="",
        patch=None,
    )
    assert wrs.outcome_of(rec) == ("done", "blocked: needs the owner")
    rec = wrs.build(
        JOB,
        NONE,
        {
            "exit": None,
            "timed_out": False,
            "result_event": None,
            "output_bytes": 0,
            "reason": "git: boom",
        },
        head=None,
        stat="",
        patch=None,
    )
    assert wrs.outcome_of(rec) == ("failed", "git: boom") and rec["exit"] is None


def test_a_runner_failure_after_the_session_is_a_failure_not_done():
    # git failed after claude ended (rev-parse, diff, the patch write): the commits were never
    # handed over — the record says so, whatever exit and block the session left
    rec = wrs.build(
        JOB,
        FACTS,
        {
            "exit": 0,
            "timed_out": False,
            "result_event": RESULT,
            "output_bytes": 10,
            "reason": "git: boom",
        },
        head=None,
        stat="",
        patch=None,
    )
    assert wrs.outcome_of(rec) == ("failed", "git: boom")
    assert "сбой" in wrs.comment(rec) and "git: boom" in wrs.comment(rec)
    rec = wrs.build(
        JOB,
        FACTS,
        {
            "exit": -9,
            "timed_out": True,
            "result_event": None,
            "output_bytes": 0,
            "reason": "git: boom",
        },
        head=None,
        stat="",
        patch=None,
    )
    assert wrs.outcome_of(rec)[0] == "failed" and "timeout" in wrs.outcome_of(rec)[1]


AUTH_EVENT = {**RESULT, "subtype": "success", "is_error": True, "result": "Invalid API key · Please run /login"}


def _auth(**outcome):
    return wrs.build(JOB, NONE, {"exit": 1, "timed_out": False, "result_event": AUTH_EVENT, "output_bytes": 0,
                                 "auth_failed": True, **outcome}, head=None, stat="", patch=None)


def test_a_refused_sign_in_is_auth_failed():
    """Claude's «success» with is_error after a refused sign-in is auth_failed, not «claude reported»."""
    rec = _auth()
    assert rec["auth_failed"] is True and wrs.outcome_of(rec) == ("failed", "auth_failed")
    # without the result event too: the mark is above «exited without a result event»
    assert wrs.outcome_of(_auth(result_event=None)) == ("failed", "auth_failed")
    # a timeout and the runner's own reason still come first
    assert "timeout" in wrs.outcome_of(_auth(timed_out=True))[1]
    assert wrs.outcome_of(_auth(reason="git: boom")) == ("failed", "git: boom")
    # a record without the mark keeps its old reasons
    plain = wrs.build(JOB, NONE, {"exit": 1, "timed_out": False, "result_event": AUTH_EVENT, "output_bytes": 0},
                      head=None, stat="", patch=None)
    assert plain["auth_failed"] is False and wrs.outcome_of(plain) == ("failed", "claude reported success")
    old = {k: v for k, v in plain.items() if k != "auth_failed"}  # a record written before the change
    assert wrs.outcome_of(old) == ("failed", "claude reported success")
    assert "auth_failed" in wrs.summary(rec)  # BOSS's line keeps the code


def test_the_comment_on_a_refused_sign_in_says_the_remedy_in_words():
    text = wrs.comment(_auth())
    assert ": сбой" in text and "нет входа в Claude: токен недействителен или истёк" in text
    assert "claude setup-token" in text and "kd-secret put claude-token" in text
    assert "auth_failed" not in text and "claude reported" not in text


def test_summary_and_comment_carry_no_secrets_and_the_comment_is_russian():
    rec = wrs.build(
        JOB,
        {**FACTS, "branch": "x"},
        {"exit": 0, "timed_out": False, "result_event": RESULT, "output_bytes": 10},
        head="c" * 40,
        stat=" README.md | 2 +-\n 1 file changed",
        patch="/p/worker.patch",
    )
    assert (
        "implementer" in wrs.summary(rec)
        and "$0.01" in wrs.summary(rec)
        and "b" * 12 in wrs.summary(rec)
    )
    assert "\n" not in wrs.summary(rec)
    text = wrs.comment(rec)
    assert (
        "Рабочая сессия" in text
        and "worker.patch" in text
        and "pytest -q: 3 passed" in text
    )
    assert "\x1b" not in wrs.comment(
        wrs.build(
            JOB,
            FACTS,
            {
                "exit": 0,
                "timed_out": False,
                "output_bytes": 0,
                "result_event": {
                    **RESULT,
                    "result": '```json\n{"status": "done", "notes": "a\\u001b[31mred"}\n```',
                },
            },
            head=None,
            stat="",
            patch=None,
        )
    )
