"""the session host's model probe — one short claude run on a cabinet model under the base user: its unit's
command line, its outcome, the host's one-at-a-time run and the report the cabinet hears."""

import asyncio
import json
import sys

import pytest

from kd.session_host import claude, probe, protocol, server
from test_session_host_server import CABINET_UID, Cabinet, _host, _until  # noqa: E402

SPEC = {"id": "or-x", "provider": "openrouter", "upstream": "anthropic/claude-sonnet-4.5", "base_url": ""}
PID = "ab" * 16
RESULT = {"type": "result", "subtype": "success", "is_error": False, "result": "OK",
          "modelUsage": {"anthropic/claude-sonnet-4.5": {"inputTokens": 10, "outputTokens": 1}}}


def test_argv_runs_claude_once_on_the_model_with_the_kinds_key():
    a = probe.argv(probe.unit(PID), "digi", SPEC)
    assert a[:2] == ["systemd-run", "--unit=digipaddock-probe-abababababab"]
    assert "--uid=digi" in a and f"--slice={claude.SLICE}" in a and "--pipe" in a and "--wait" in a
    assert "EnvironmentFile=/etc/digipaddock/llm/openrouter.env" in a and "NoNewPrivileges=yes" in a
    assert "RuntimeMaxSec=90" in a
    assert "--setenv=ANTHROPIC_BASE_URL=https://openrouter.ai/api" in a and "--setenv=ANTHROPIC_API_KEY=" in a
    assert "--setenv=ANTHROPIC_MODEL=anthropic/claude-sonnet-4.5" in a
    assert not any("claude.env" in part or "CLAUDE_CODE_OAUTH" in part for part in a)
    tail = a[a.index("/opt/digipaddock-claude/claude"):]  # root's binary, never a home's (KD-9474)
    assert not any(".local" in part for part in a)
    assert tail[1:3] == ["-p", probe.PROMPT]
    assert tail[tail.index("--model") + 1] == "anthropic/claude-sonnet-4.5"
    assert tail[tail.index("--max-turns") + 1] == "1" and tail[tail.index("--output-format") + 1] == "json"
    assert "--strict-mcp-config" in tail and "--no-session-persistence" in tail
    assert tail[tail.index("--setting-sources") + 1] == ""
    # claude starts through kd.secret_fd, which hands it the key by a pipe where claude reads one
    assert a[a.index(tail[0]) - 3:a.index(tail[0])] == ["/usr/bin/python3", "-I", probe.SECRET_FD]
    assert probe.SECRET_FD.endswith("/common/kd/secret_fd.py")


def test_argv_of_a_custom_kind_uses_the_models_base_url():
    spec = {**SPEC, "provider": "custom", "base_url": "https://llm.example.net/v1"}
    a = probe.argv(probe.unit(PID), "digi", spec, timeout_s=30)
    assert "--setenv=ANTHROPIC_BASE_URL=https://llm.example.net/v1" in a and "RuntimeMaxSec=30" in a
    assert "EnvironmentFile=/etc/digipaddock/llm/custom.env" in a


@pytest.mark.parametrize("unit, user, spec", [
    ("digipaddock-session-abababababab", "digi", SPEC), (probe.unit(PID), "root", SPEC), (probe.unit(PID), "Digi", SPEC),
    (probe.unit(PID), "digi", {**SPEC, "provider": "nope"}), (probe.unit(PID), "digi", {**SPEC, "upstream": "x y"}),
])
def test_argv_refuses_unchecked_parts(unit, user, spec):
    with pytest.raises(ValueError):
        probe.argv(unit, user, spec)


def test_outcome_of_a_good_run_names_the_model_and_the_time():
    out = probe.outcome(0, json.dumps(RESULT), "", 10.0, 11.2)
    assert out == {"ok": True, "model": "anthropic/claude-sonnet-4.5", "ms": 1200, "detail": "OK"}


def test_outcome_of_a_failed_run_masks_secrets():
    out = probe.outcome(1, "", "401 Unauthorized token=abc Bearer xyz", 10.0, 10.5)
    assert out["ok"] is False and out["model"] == "" and out["ms"] == 500
    assert "token=***" in out["detail"] and "abc" not in out["detail"] and "xyz" not in out["detail"]
    assert out["detail"].startswith("exit 1")


def test_outcome_of_an_error_result_or_no_json_is_not_ok():
    error = {**RESULT, "is_error": True, "result": "API Error: 402 insufficient credits"}
    out = probe.outcome(0, json.dumps(error), "", 0, 0.1)
    assert out["ok"] is False and out["detail"] == "API Error: 402 insufficient credits" and out["model"] == SPEC["upstream"]
    out = probe.outcome(0, "not json at all", "", 0, 0)
    assert out["ok"] is False and "not json at all" in out["detail"]
    out = probe.outcome(-1, "", "", 0, 0)
    assert out["ok"] is False and out["detail"] == "exit -1"


def test_outcome_detail_is_one_clean_line_of_at_most_500():
    out = probe.outcome(1, "", "\x1b[31mred\x1b[0m\n" + "x" * 900, 0, 0)
    assert len(out["detail"]) <= probe.DETAIL_MAX and "\x1b" not in out["detail"] and "\n" not in out["detail"]


# --- the host ----------------------------------------------------------------------------------------------------
def _probe_request(pid=PID, spec=SPEC):
    return json.dumps({"op": "probe", "probe": pid, "model": spec}).encode() + b"\n"


def test_protocol_probe_fields_checked():
    assert protocol.parse(_probe_request())["model"] == SPEC
    for bad in (_probe_request(pid="xyz"), _probe_request(spec={**SPEC, "id": "Bad/Name"}),
                _probe_request(spec={**SPEC, "provider": "nope"}), _probe_request(spec="or-x"),
                json.dumps({"op": "probe", "probe": PID}).encode() + b"\n",
                json.dumps({"op": "probe", "probe": PID, "model": SPEC, "extra": 1}).encode() + b"\n"):
        with pytest.raises(protocol.ProtocolError):
            protocol.parse(bad)


def _claude(delay=0.0, rc=0, seen=None):
    async def fake(*argv, **kwargs):
        if seen is not None:
            seen.append(list(argv))
        return await asyncio.create_subprocess_exec(
            sys.executable, "-c",
            f"import sys, time; time.sleep({delay}); print({json.dumps(RESULT)!r}); sys.exit({rc})",
            **kwargs)
    return fake


def test_the_host_runs_one_probe_at_a_time_and_reports_its_outcome(tmp_path, monkeypatch):
    monkeypatch.setattr(server.llm_models, "key_present", lambda provider, *a, **k: True)
    monkeypatch.setattr(server.llm_models, "read_secret", lambda provider, *a, **k: {"ANTHROPIC_AUTH_TOKEN": "k"})
    cabinet, seen = Cabinet(), []

    async def scenario():
        host = _host(tmp_path, cabinet, spawn=_claude(delay=0.6, seen=seen))
        await host.start()
        first = json.loads(await host.request(_probe_request(), CABINET_UID))
        same = json.loads(await host.request(_probe_request(), CABINET_UID))
        other = json.loads(await host.request(_probe_request(pid="cd" * 16), CABINET_UID))
        await _until(lambda: any(i.get("type") == "probe" for i in cabinet.items()))
        after = json.loads(await host.request(_probe_request(pid="ef" * 16), CABINET_UID))
        await _until(lambda: len([i for i in cabinet.items() if i.get("type") == "probe"]) == 2)
        await host.close()
        return host, first, same, other, after

    host, first, same, other, after = asyncio.run(scenario())
    assert first == {"ok": True, "state": "running"} and same == first
    assert other == {"ok": False, "error": "refused", "message": "busy"}
    assert after == {"ok": True, "state": "running"}
    assert seen[0] == probe.argv(probe.unit(PID), "digi", SPEC) and len(seen) == 2
    reports = [i for i in cabinet.items() if i.get("type") == "probe"]
    assert reports[0]["probe"] == PID and reports[0]["ok"] is True and reports[0]["detail"] == "OK"
    assert reports[0]["model"] == "anthropic/claude-sonnet-4.5" and isinstance(reports[0]["ms"], int)
    assert reports[0]["ms"] >= 500 and reports[1]["probe"] == "ef" * 16
    journal = (tmp_path / "host.jsonl").read_text(encoding="utf-8")
    assert '"op": "probe"' in journal and "OK" not in journal.replace('"ok"', "")  # no texts in the journal


def test_a_failed_probe_is_reported_with_its_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(server.llm_models, "key_present", lambda provider, *a, **k: True)
    monkeypatch.setattr(server.llm_models, "read_secret", lambda provider, *a, **k: {"ANTHROPIC_AUTH_TOKEN": "k"})
    cabinet = Cabinet()

    async def scenario():
        host = _host(tmp_path, cabinet, spawn=_claude(rc=2))
        await host.start()
        await host.request(_probe_request(), CABINET_UID)
        await _until(lambda: any(i.get("type") == "probe" for i in cabinet.items()))
        await host.close()

    asyncio.run(scenario())
    [report] = [i for i in cabinet.items() if i.get("type") == "probe"]
    assert report["ok"] is False and report["detail"].startswith("exit 2")


def test_a_key_gone_or_broken_before_the_probes_spawn_is_reported_not_run(tmp_path, monkeypatch):
    """Final review (Critical 1): the file was there when the probe was accepted; root reads it again right before
    the spawn — gone or malformed, the probe reports no_key and claude never starts."""
    monkeypatch.setattr(server.llm_models, "key_present", lambda provider, *a, **k: True)
    monkeypatch.setattr(server.llm_models, "read_secret", lambda provider, *a, **k: {})
    cabinet, seen = Cabinet(), []

    async def scenario():
        host = _host(tmp_path, cabinet, spawn=_claude(seen=seen))
        await host.start()
        accepted = json.loads(await host.request(_probe_request(), CABINET_UID))
        await _until(lambda: any(i.get("type") == "probe" for i in cabinet.items()))
        await host.close()
        return accepted

    assert asyncio.run(scenario()) == {"ok": True, "state": "running"}
    [report] = [i for i in cabinet.items() if i.get("type") == "probe"]
    assert report["ok"] is False and report["detail"] == "no_key: openrouter" and report["model"] == ""
    assert seen == []


def test_a_probe_without_the_kinds_key_or_from_another_peer_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(server.llm_models, "key_present", lambda provider, *a, **k: False)
    cabinet, seen = Cabinet(), []

    async def scenario():
        host = _host(tmp_path, cabinet, spawn=_claude(seen=seen))
        await host.start()
        no_key = json.loads(await host.request(_probe_request(), CABINET_UID))
        peer = json.loads(await host.request(_probe_request(), CABINET_UID + 1))
        await host.close()
        return no_key, peer

    no_key, peer = asyncio.run(scenario())
    assert no_key == {"ok": False, "error": "refused", "message": "no_key: openrouter"}
    assert peer["ok"] is False and peer["error"] == "refused"
    assert seen == []


def test_a_subscription_spec_probes_that_alias_on_the_host_token():
    spec = {"id": "claude:opus", "provider": "subscription", "upstream": "opus", "base_url": ""}
    a = probe.argv(probe.unit(PID), "digi", spec)
    assert "EnvironmentFile=/etc/digipaddock/claude.env" in a
    assert not any(part.startswith("--setenv=ANTHROPIC_") for part in a)
    tail = a[a.index("/opt/digipaddock-claude/claude"):]  # root's binary, never a home's (KD-9474)
    assert not any(".local" in part for part in a)
    assert tail[tail.index("--model") + 1] == "opus"


def test_the_host_probes_a_subscription_model_on_its_own_token(tmp_path, monkeypatch):
    """no kind's key is read for a subscription model; the host's Claude token is what it needs."""

    def no_secret(*a, **k):
        raise AssertionError("a subscription model reads no kind's key")

    spec = {"id": "claude:opus", "provider": "subscription", "upstream": "opus", "base_url": ""}
    monkeypatch.setattr(server.llm_models, "key_present", lambda provider, *a, **k: False)
    monkeypatch.setattr(server.llm_models, "read_secret", no_secret)
    token = ["tok"]
    monkeypatch.setattr(server.claude_token, "read", lambda *a, **k: token[0])
    cabinet, seen = Cabinet(), []

    async def scenario():
        host = _host(tmp_path, cabinet, spawn=_claude(seen=seen))
        await host.start()
        first = json.loads(await host.request(_probe_request(spec=spec), CABINET_UID))
        await _until(lambda: any(i.get("type") == "probe" for i in cabinet.items()))
        token[0] = ""
        second = json.loads(await host.request(_probe_request(pid="cd" * 16, spec=spec), CABINET_UID))
        await host.close()
        return first, second

    first, second = asyncio.run(scenario())
    assert first == {"ok": True, "state": "running"} and seen == [probe.argv(probe.unit(PID), "digi", spec)]
    assert second == {"ok": False, "error": "refused", "message": "no_key: claude-subscription"}
    [report] = [i for i in cabinet.items() if i.get("type") == "probe"]
    assert report["ok"] is True
