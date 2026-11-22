"""a worker job on the session host — its unit's command line, its request, the line that ends it."""

import json

import pytest

from kd.session_host import protocol, worker

JOB = "ab" * 16
DEFINITION = {"id": JOB, "agent": "dev", "card": "KD-7", "spec": "implementer", "timeout": 3600}
USED = {"models": {"claude-sonnet-5-5": {"in": 1, "out": 2, "cache_read": 3, "cache_write": 4, "cost": 0.5}},
        "requests": 3}


def test_worker_argv_is_exact():
    assert worker.argv("digipaddock-session-job-abababababab", "dev/worker2", 3600) == [
        "systemd-run", "--unit=digipaddock-session-job-abababababab", "--slice=digipaddock-sessions.slice", "--pipe", "--wait",
        "--quiet", "--collect", "--service-type=exec", "-p", "KillMode=control-group", "-p", "TimeoutStopSec=20",
        "-p", "NoNewPrivileges=yes", "-p", "RuntimeMaxSec=4200", "/usr/bin/python3", "-I", "/opt/digipaddock/common/bin/kd-worker-run",
        "--slot", "dev/worker2",
    ]


@pytest.mark.parametrize("unit, slot, timeout", [
    ("digipaddock-session-abc", "dev/worker1", 3600), ("digipaddock-session-job-ABABABABABAB", "dev/worker1", 3600),
    ("digipaddock-session-job-abababababab", "dev/worker0", 3600), ("digipaddock-session-job-abababababab", "../x/worker1", 3600),
    ("digipaddock-session-job-abababababab", "dev/worker1", 30), ("digipaddock-session-job-abababababab", "dev/worker1", True),
])
def test_worker_argv_refuses_unchecked_parts(unit, slot, timeout):
    with pytest.raises(ValueError):
        worker.argv(unit, slot, timeout)


def _line(**over):
    item = {"type": "job", "phase": "ended", "state": "done", "reason": "", "comment": "Готово.\nТри файла.",
            "usage": USED, "result": {"summary": "ok"}}
    item.update(over)
    return (json.dumps(item, ensure_ascii=False) + "\n").encode()


def test_end_line_is_the_runners_outcome():
    assert worker.end_line(_line()) == {"state": "done", "reason": "", "comment": "Готово.\nТри файла.",
                                        "usage": USED, "result": {"summary": "ok"}}
    assert worker.end_line(_line(state="no-result", result=None, usage=None))["usage"] is None
    assert worker.end_line(_line(usage={"models": {"Bad!": {}}, "requests": 1}))["usage"] is None  # dropped, kept


@pytest.mark.parametrize("raw", [
    b"not json\n", b"[]\n", _line(type="message"), _line(phase="started"), _line(state="cancelled"),
    _line(state="running"), _line(reason="x" * 501), _line(comment="x" * 5000), _line(result=[1]),
    pytest.param(_line(result={"x": "y" * (worker.RESULT_MAX + 1)}), id="a result over the limit"),
    _line(reason="a\nb"),
])
def test_end_line_refuses_other_lines(raw):
    assert worker.end_line(raw) is None


def test_end_line_keeps_a_thorough_critics_block():
    import council_env

    block = council_env.thorough()
    assert len(json.dumps(block, ensure_ascii=False).encode()) > 32 * 1024

    assert worker.end_line(_line(result=block))["result"] == block


def test_the_longest_end_line_is_within_what_the_host_reads():
    """A line over LINE_MAX is dropped whole: the job would end with no outcome at all."""
    full = _line(result={"x": "я" * ((worker.RESULT_MAX - 16) // 2)}, comment="я" * 4000, reason="x" * 500)

    assert worker.end_line(full) is not None and len(full) <= worker.LINE_MAX


def _request(**over):
    request = {"op": "job", "job": JOB, "agent": "dev", "definition": dict(DEFINITION)}
    request.update(over)
    return json.dumps(request).encode() + b"\n"


SPEC = {"id": "or-x", "provider": "openrouter", "upstream": "anthropic/claude-sonnet-4.5", "base_url": ""}


def test_protocol_job_fields_checked():
    assert protocol.parse(_request())["definition"]["card"] == "KD-7"
    assert protocol.parse(b'{"op": "job_cancel", "job": "' + JOB.encode() + b'"}') == {"op": "job_cancel", "job": JOB}
    # a job on a cabinet model carries the model's spec and an effort
    on_model = protocol.parse(_request(definition={**DEFINITION, "model_spec": SPEC, "effort": "high"}))
    assert on_model["definition"]["model_spec"] == SPEC and on_model["definition"]["effort"] == "high"
    assert protocol.parse(_request(definition={**DEFINITION, "effort": ""}))["definition"]["effort"] == ""
    for bad in (
        _request(job="xyz"), _request(agent="Dev"), _request(definition=[]),
        _request(definition={**DEFINITION, "id": "cd" * 16}), _request(definition={**DEFINITION, "agent": "boss"}),
        _request(definition={**DEFINITION, "timeout": 5}), _request(definition={**DEFINITION, "pad": "x" * 50000}),
        _request(definition={**DEFINITION, "model_spec": "or-x"}),
        _request(definition={**DEFINITION, "model_spec": {**SPEC, "id": "Bad/Name"}}),
        _request(definition={**DEFINITION, "model_spec": {**SPEC, "provider": "nope"}}),
        _request(definition={**DEFINITION, "effort": "ultra"}), _request(definition={**DEFINITION, "effort": None}),
        _request(extra=1), b'{"op": "job_cancel", "job": "../x"}', b'{"op": "job_cancel"}',
    ):
        with pytest.raises(protocol.ProtocolError):
            protocol.parse(bad)


# --- a job of the cabinet carries what its worker reads --------------------------------------------
def test_a_job_carries_its_inputs_past_the_definitions_limit():
    inputs = {"brief.md": "the brief\n" * 20000, "thread.md": "#1 @owner: go\n"}  # 200 KB: over the old request
    request = _request(definition={**DEFINITION, "inputs": inputs})

    assert len(request) > 1 << 16
    assert protocol.parse(request)["definition"]["inputs"] == inputs


@pytest.mark.parametrize("inputs", [
    {}, [], "brief", {"brief.txt": "x"}, {"../x.md": "x"}, {"brief.md": ""}, {"brief.md": 1},
    {"brief.md": "x" * (256 * 1024 + 1)},
])
def test_bad_inputs_refuse_the_job(inputs):
    with pytest.raises(protocol.ProtocolError):
        protocol.parse(_request(definition={**DEFINITION, "inputs": inputs}))


def test_inputs_do_not_widen_the_definition_itself():
    with pytest.raises(protocol.ProtocolError):
        protocol.parse(_request(definition={**DEFINITION, "pad": "x" * 50000, "inputs": {"brief.md": "x"}}))


# --- a job on a pull request's head ---------------------------------------------------------------
PULL = {"number": 7, "head": "a" * 40, "target": "main"}


def test_job_request_carries_a_pull():
    request = _request(definition={**DEFINITION, "pull": PULL})

    assert protocol.parse(request)["definition"]["pull"] == PULL


@pytest.mark.parametrize("pull", [
    "7", {**PULL, "extra": 1}, {**PULL, "head": "a" * 39}, {**PULL, "number": 0}, {**PULL, "target": "a..b"},
])
def test_job_request_refuses_a_bad_pull(pull):
    with pytest.raises(protocol.ProtocolError, match="job definition: "):
        protocol.parse(_request(definition={**DEFINITION, "pull": pull}))
