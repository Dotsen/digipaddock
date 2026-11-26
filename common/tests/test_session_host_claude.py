"""the Claude Code adapter of the session host — argv, input line, stream-json normalizer."""

import json
from pathlib import Path

import pytest

from kd.session_host import claude

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "stream_json"
SID = "0b9c3f1e-1111-2222-3333-444455556666"


def test_memory_events_pair_calls_and_failures_without_claiming_sources_were_used():
    norm = claude.Normalizer()
    def call(name, ident):
        return norm.feed({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": ident,
            "name": name, "input": {"query": "private search text"}}]}})
    def result(ident, failed=False):
        return norm.feed({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": ident,
            "is_error": failed, "content": "private source body"}]}})

    events = call("mcp__hindsight__hindsight_search_knowledge_pages", "read")
    assert events[-1]["payload"]["activity"]["state"] == "started"
    assert "private search" not in json.dumps(events[-1])
    call("mcp__hindsight__hindsight_ingest_document", "write")
    returned = result("read")[-1]["payload"]["activity"]
    assert returned == {"type": "memory", "operation": "search_knowledge_pages", "state": "returned",
                        "call": "read", "mode": "read"}
    assert result("write", True)[-1]["payload"]["activity"]["state"] == "failed"
    assert len(result("read")) == 1  # a repeated or orphan result cannot invent another memory event
    assert len(call("mcp__other__search", "other")) == 1
    call("hindsight_reflect", "pending")
    ended = norm.feed({"type": "result", "subtype": "success"})
    assert ended[0]["payload"]["activity"]["state"] == "interrupted"
    assert not norm.memory_calls


def replay(name):
    norm, items = claude.Normalizer(), []
    for raw in (FIXTURES / f"{name}.jsonl").read_bytes().splitlines():
        event = claude.parse_line(raw)
        if event is not None:
            items.extend(norm.feed(event))
    return items


def test_argv_exact_and_rejects_bad_values():
    chat = claude.context_args("a" * 32, 7, "")
    assert claude.argv("analyst", "digi-analyst", "digipaddock-session-ab12", "", chat) == [
        "systemd-run", "--unit=digipaddock-session-ab12", "--slice=digipaddock-sessions.slice", "--uid=digi-analyst", "--pipe", "--wait",
        "--quiet", "--collect", "--service-type=exec", "-p", "KillMode=control-group", "-p", "TimeoutStopSec=20",
        "-p", "EnvironmentFile=-/etc/digipaddock/claude.env",  # systemd reads the host's token as root
        "-p", "NoNewPrivileges=yes", "/usr/local/bin/agent-launch", "analyst", "--session", *chat,
    ]
    assert claude.argv("analyst", "digi-analyst", "digipaddock-session-ab12", SID, chat)[-2:] == ["chat", SID]
    for bad in (("Analyst", "digi-analyst", "u"), ("analyst", "digi analyst", "u"), ("analyst", "digi-analyst", "u x"),
                ("analyst", "root", "u")):
        with pytest.raises(ValueError):
            claude.argv(*bad, "", chat)
    with pytest.raises(ValueError):
        claude.argv("analyst", "digi-analyst", "digipaddock-session-ab12", "--dangerously-skip-permissions", chat)


def test_argv_carries_the_sessions_context_before_the_resume_id():
    """the launch names the session's place — a card's thread, or the agent's chat."""
    card = claude.context_args("a" * 32, 7, "KD-9045")
    assert card == ["--context", "a" * 32, "7", "KD-9045"]
    assert claude.argv("dev", "digi-dev", "digipaddock-session-ab12", SID, card)[-6:] == [
        "--session", "--context", "a" * 32, "7", "KD-9045", SID]
    assert claude.context_args("b" * 32, 9, "") == ["--context", "b" * 32, "9", "chat"]
    # the agent's "Monitor" thread
    assert claude.context_args("b" * 32, 9, "", "monitor") == ["--context", "b" * 32, "9", "monitor"]
    for bad_space in (("b" * 32, 9, "KD-1", "monitor"), ("b" * 32, 9, "", "chat"), ("b" * 32, 9, "", "x")):
        with pytest.raises(ValueError):
            claude.context_args(*bad_space)
    for bad in (("A" * 32, 7, ""), ("a" * 31, 7, ""), ("a" * 32, 0, ""), ("a" * 32, True, ""), ("a" * 32, "7", ""),
                ("a" * 32, 7, "chat"), ("a" * 32, 7, "KD-1 --x"), ("a" * 32, 7, "kd-1")):
        with pytest.raises(ValueError):
            claude.context_args(*bad)


def test_cleanup_stops_the_sessions_slice_never_the_host_itself():
    """A former host's sessions are stopped at start; a unit glob like digipaddock-session-*.service also names the host."""
    import fnmatch

    assert claude.CLEANUP == ["systemctl", "stop", "digipaddock-sessions.slice"]
    for arg in claude.CLEANUP[2:]:
        assert not fnmatch.fnmatch("digipaddock-session-host.service", arg) and not fnmatch.fnmatch("kd-session-host", arg)
    host = (Path(__file__).resolve().parents[1] / "bin" / "kd-session-host").read_text(encoding="utf-8")
    assert "*claude.CLEANUP" in host and "digipaddock-session-*" not in host


def test_input_line_shape():
    line = claude.input_line("[board] KD-1 assigned to you")
    assert line.endswith(b"\n") and line.count(b"\n") == 1
    assert json.loads(line) == {"type": "user", "message": {"role": "user", "content": "[board] KD-1 assigned to you"}}


def test_two_turns_replay():
    items = replay("two_turns")
    assert items[0] == {"type": "session", "state": "working", "runtime_session_id": SID}
    texts = [i["body"] for i in items if i["type"] == "message" and i["kind"] == "text"]
    assert texts == ["ok", "APRICOT"]  # thinking is not shown; the result does not repeat the text
    ends = [i for i in items if i["type"] == "turn_end"]
    assert len(ends) == 2 and all(e["ok"] for e in ends)
    # Claude's cumulative model usage of the session, and this turn's API calls
    model = {"in": 12, "out": 7, "cache_read": 2000, "cache_write": 100, "cost": 0.0123}
    assert ends[0]["usage"] == {"models": {"claude-sonnet-5-5": model}, "requests": 1}
    assert ends[1]["usage"]["models"]["claude-sonnet-5-5"]["out"] == 14
    assert sum(1 for i in items if i["type"] == "session") == 1  # the same id again is not news


def test_tools_replay_keeps_tool_output_out_of_the_message():
    """The output goes as a separate excerpt, never in the body or the payload the thread keeps."""
    items = replay("tools")
    calls = [i for i in items if i.get("kind") == "tool_call"]
    results = [i for i in items if i.get("kind") == "tool_result"]
    assert [c["payload"] for c in calls] == [{"tool": "Bash", "id": "toolu_01"}, {"tool": "Read", "id": "toolu_02"}]
    assert calls[1]["body"] == "Read: /srv/digipaddock-agents/analyst/work/brief.md"
    bash = calls[0]["body"]
    assert bash.startswith("Bash: mysql") and len(bash) <= 300
    for secret in ("S3cret", "abc.def", "u:pw", "zzz"):
        assert secret not in bash
    assert all(r["body"] == "" for r in results)
    assert results[0]["payload"]["ok"] is True and results[0]["payload"]["size"] > 1000
    assert results[0]["payload"]["id"] == "toolu_01"
    assert set(results[0]["payload"]) == {"id", "ok", "size"}
    assert results[0]["output"]["text"].startswith("1\n1\n") and results[0]["output"]["truncated"] is False
    assert results[1]["output"] == {"text": "# Brief", "size": len('[{"type": "text", "text": "# Brief"}]'),
                                    "truncated": False}


def test_excerpt_masks_gate_patterns():
    """The sysadmin gate's rules (multi-line PEM, PHP define, -u user:pass, a bot token) and this adapter's."""
    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAA\nAAAABG5vbmUAAAAEbm9uZQ\n-----END OPENSSH PRIVATE KEY-----"  # privacy-scan: allow (fake sample)
    output = "\n".join([
        "define('DB_PASSWORD', 'Hunter2php');",
        "curl -u admin:Pa55word https://example.org/api",
        pem,
        "bot 1234567890:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawE ok",  # privacy-scan: allow (fake sample)
        "API_KEY=abc123xyz",
        "\x1b[31mred\x1b[0m\tcolumn\r",
    ])
    excerpt = claude.output_excerpt([{"type": "text", "text": output}, {"type": "image", "source": {}}])
    text = excerpt["text"]
    for secret in ("Hunter2php", "Pa55word", "b3BlbnNzaC1rZXktdjEAAAA", "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawE",
                   "abc123xyz", "\x1b", "\r"):
        assert secret not in text
    assert "red\tcolumn" in text and text.endswith("[image]") and text.count("\n") >= 6
    assert excerpt["truncated"] is False


def test_excerpt_is_cut_and_marked():
    content = "строка\n" * 2000
    excerpt = claude.output_excerpt(content)
    assert len(excerpt["text"]) == claude.OUTPUT_MAX == 4000
    assert excerpt["truncated"] is True and excerpt["size"] == len(json.dumps(content, ensure_ascii=False))
    assert claude.output_excerpt(None) == {"text": "", "size": 4, "truncated": False}


def test_no_gate_no_excerpt(monkeypatch, tmp_path):
    """Without the gate's rules nothing is shown: the result keeps its ok and size, no output."""
    from kd import redact

    monkeypatch.setattr(redact, "GATE", tmp_path / "missing.py")
    assert redact.text("password=x") is None
    assert claude.output_excerpt("password=x") is None
    [result] = claude.Normalizer().feed({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_09", "content": "password=x"}]}})
    assert "output" not in result and result["payload"]["ok"] is True


def test_error_result_turn_end_not_ok():
    [end] = [i for i in replay("error_result") if i["type"] == "turn_end"]
    assert end["ok"] is False and end["error"] == "error_during_execution"


AUTH_TEXT = "Failed to authenticate: OAuth session expired and could not be refreshed"


def test_an_authentication_failure_ends_the_turn_auth_failed():
    """Seen 05.11: Claude Code's synthetic message carries error authentication_failed, its result is «success»
    with is_error — the turn's error was «success»; now it is auth_failed."""
    n = claude.Normalizer()
    [text] = n.feed({"type": "assistant", "error": "authentication_failed",
                     "message": {"role": "assistant", "model": "<synthetic>",
                                 "content": [{"type": "text", "text": AUTH_TEXT}]}})
    assert text["kind"] == "text"
    [end] = n.feed({"type": "result", "subtype": "success", "is_error": True, "result": AUTH_TEXT})
    assert end["ok"] is False and end["error"] == claude.AUTH_FAILED == "auth_failed"
    [ok] = n.feed({"type": "result", "subtype": "success", "is_error": False})  # the mark is the turn's only
    assert ok["ok"] is True and "error" not in ok


def test_another_is_error_result_ends_the_turn_with_its_text():
    [end] = claude.Normalizer().feed({"type": "result", "subtype": "success", "is_error": True,
                                      "result": "API Error: 529\n overloaded " + "x" * 200})
    assert end["ok"] is False and end["error"].startswith("API Error: 529 overloaded") and len(end["error"]) == 100


def test_denied_becomes_status():
    items = replay("denied")
    [status] = [i for i in items if i.get("kind") == "status"]
    assert status["payload"] == {"key": "sys.session.denied", "params": {"tool": "Bash"}}
    [result] = [i for i in items if i.get("kind") == "tool_result"]
    assert result["payload"]["ok"] is False


def test_summary_masks_secrets_and_truncates():
    s = claude.summary("Bash", {"command": "export API_KEY=abc123; psql password=hunter2 \x1b[31mred\x1b[0m\nnext"})
    assert "abc123" not in s and "hunter2" not in s and "\x1b" not in s and "\n" not in s
    assert claude.summary("Bash", {"command": "mkdir -p /tmp/x && find . -print"}) == (
        "Bash: mkdir -p /tmp/x && find . -print"
    )
    long = claude.summary("Bash", {"command": "echo " + "x" * 1000})
    assert len(long) == 300 and long.endswith("…")
    assert claude.summary("Grep", {"pattern": "TODO", "path": "src"}) == "Grep: TODO in src"
    assert claude.summary("Mystery", {"a": 1}) == 'Mystery: {"a": 1}'


COMPACTED = "sys.session.compacted"


def test_a_compaction_is_a_status_line_with_the_context_before_and_after():
    """Claude Code reports an auto-compaction as a ``compact_boundary`` system event with the size before; the
    size after is the context of the next request of the main conversation (its input, cache read and cache write)
    — never a subagent's, never a message without usage. The line comes before that request's text."""
    items = replay("compact")
    lines = [(i["kind"], i["body"]) for i in items if i.get("type") == "message"]
    assert lines == [
        ("text", "reading"),
        ("text", "a subagent's own context"),
        ("status", "context compacted (auto): 412345 -> 45005 tokens"),
        ("text", "continuing"),
        ("text", "done"),
    ]
    [status] = [i for i in items if i.get("kind") == "status"]
    assert status["payload"] == {"key": COMPACTED, "params": {"trigger": "auto", "before": 412345, "after": 45005}}


def test_a_compaction_the_turn_ends_after_shows_the_size_before_only():
    norm = claude.Normalizer()
    assert norm.feed({"type": "system", "subtype": "compact_boundary",
                      "compact_metadata": {"trigger": "manual", "pre_tokens": 98765}}) == []
    status, end = norm.feed({"type": "result", "subtype": "success", "num_turns": 1})
    assert status["payload"] == {"key": COMPACTED + "_before", "params": {"trigger": "manual", "before": 98765}}
    assert end["type"] == "turn_end"
    # an event that names the size after itself needs no next request; odd metadata still says it compacted
    [line] = norm.feed({"type": "system", "subtype": "compact_boundary",
                        "compact_metadata": {"trigger": "auto", "pre_tokens": 500000, "post_tokens": 41000}})
    assert line["payload"]["params"] == {"trigger": "auto", "before": 500000, "after": 41000}
    norm.feed({"type": "system", "subtype": "compact_boundary", "compact_metadata": {"pre_tokens": "x", "trigger": 7}})
    [bare, _] = norm.feed({"type": "result", "subtype": "success"})
    assert bare["payload"] == {"key": COMPACTED + "_bare", "params": {}}


def test_a_compaction_event_as_the_pinned_claude_code_sends_it():
    """The event's shape as Claude Code 2.1.289 printed it in a probe with a 100000 window: both sizes and fields
    the line does not use."""
    segment = {"head_uuid": "981a3975-1111-4222-8333-444455556666", "anchor_uuid": "9c8077c2-1111-4222-8333-444455556666",
               "tail_uuid": "bc20b50f-1111-4222-8333-444455556666"}
    [line] = claude.Normalizer().feed({
        "type": "system", "subtype": "compact_boundary", "session_id": "39671b55-1111-4222-8333-444455556666",
        "compact_metadata": {"trigger": "auto", "pre_tokens": 72228, "post_tokens": 12787,
                             "cumulative_dropped_tokens": 59441, "duration_ms": 8437, "preserved_segment": segment,
                             "preserved_messages": {"anchor_uuid": segment["anchor_uuid"], "uuids": [], "all_uuids": []}},
    })
    assert line["payload"]["params"] == {"trigger": "auto", "before": 72228, "after": 12787}


def test_unknown_events_ignored():
    items = replay("garbage")
    assert [i["body"] for i in items if i.get("kind") == "text"] == ["still fine"]
    assert claude.parse_line(b"x" * (claude.MAX_LINE + 1)) is None
    assert claude.parse_line(b"[1, 2]") is None and claude.parse_line(b"\xff\xfe") is None


def test_result_reports_cumulative_models_and_requests(capsys):
    """modelUsage is cumulative for the Claude session; an odd model name or number is dropped — with a
    journal line (final review), or that spend would vanish without a trace."""
    normalizer = claude.Normalizer()
    event = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 6, "total_cost_usd": 0.17,
             "usage": {"input_tokens": 8}, "modelUsage": {
                 "claude-sonnet-5-5": {"inputTokens": 8, "outputTokens": 817, "cacheReadInputTokens": 102123,
                                       "cacheCreationInputTokens": 36355, "costUSD": 0.174, "contextWindow": 1000000},
                 "Bad Model!": {"inputTokens": 1, "outputTokens": 1, "costUSD": 0.1},
                 "claude-haiku-5-5": {"inputTokens": -5, "outputTokens": 2, "costUSD": 0.01}}}
    [end] = normalizer.feed(event)
    assert end["usage"] == {"models": {"claude-sonnet-5-5": {"in": 8, "out": 817, "cache_read": 102123,
                                                             "cache_write": 36355, "cost": 0.174}},
                            "requests": 6}
    err = capsys.readouterr().err
    assert "'Bad Model!'" in err and "'claude-haiku-5-5'" in err and err.count("\n") == 2
    [bare] = claude.Normalizer().feed({"type": "result", "subtype": "success", "num_turns": "x"})
    assert bare["usage"] == {"models": {}, "requests": 0}


# --- a session on another provider's model ----------------------------------------------------------
SPEC = {"id": "or-x", "provider": "openrouter", "upstream": "anthropic/claude-sonnet-4.5", "base_url": ""}


def test_argv_takes_the_providers_key_file_and_its_plain_variables():
    card = claude.context_args("a" * 32, 7, "KD-7")
    argv = claude.argv("dev", "digi-dev", "digipaddock-session-ab12", "", card,
                       env_file="/etc/digipaddock/llm/openrouter.env",
                       setenv={"ANTHROPIC_BASE_URL": "https://openrouter.ai/api", "ANTHROPIC_API_KEY": ""})
    # final review (Critical 1): the provider's file is required — a missing one must not start claude on the
    # stored login with ANTHROPIC_BASE_URL pointing at the provider; only the host's own token file is optional
    assert "EnvironmentFile=/etc/digipaddock/llm/openrouter.env" in argv and not any("claude.env" in a for a in argv)
    assert not any(a.startswith("EnvironmentFile=-") for a in argv)
    i = argv.index("-p", argv.index("EnvironmentFile=/etc/digipaddock/llm/openrouter.env"))
    assert argv[i + 1] == "NoNewPrivileges=yes"
    assert "--setenv=ANTHROPIC_BASE_URL=https://openrouter.ai/api" in argv and "--setenv=ANTHROPIC_API_KEY=" in argv
    assert argv.index("--setenv=ANTHROPIC_API_KEY=") < argv.index("/usr/local/bin/agent-launch")
    for bad in ({"ANTHROPIC_MODEL": "a b"}, {"X=Y": "1"}, {"lower": "x"}, {"ANTHROPIC_MODEL": "x\n"}):
        with pytest.raises(ValueError):
            claude.argv("dev", "digi-dev", "digipaddock-session-ab12", "", card, setenv=bad)
    with pytest.raises(ValueError):
        claude.argv("dev", "digi-dev", "digipaddock-session-ab12", "", card, env_file="/etc/x y")


def test_context_args_name_the_model_and_effort_after_the_place():
    assert claude.context_args("a" * 32, 7, "KD-7", model=SPEC, effort="high")[-4:] == [
        "--model", "anthropic/claude-sonnet-4.5", "--effort", "high"]
    assert claude.context_args("a" * 32, 7, "KD-7", memory="coding-agent::x", model=SPEC) == [
        "--context", "a" * 32, "7", "KD-7", "--memory", "coding-agent::x", "--model", "anthropic/claude-sonnet-4.5"]
    assert claude.context_args("a" * 32, 7, "KD-7", effort="low")[-2:] == ["--effort", "low"]
    for bad in (dict(model=SPEC, card=""), dict(effort="low", card=""), dict(effort="ultra", card="KD-7"),
                dict(model={**SPEC, "upstream": "a b"}, card="KD-7"), dict(model="or-x", card="KD-7")):
        with pytest.raises(ValueError):
            claude.context_args("a" * 32, 7, bad.pop("card"), **bad)


def test_usage_of_a_cabinet_model_is_summed_under_its_slug(capsys):
    event = {"type": "result", "subtype": "success", "num_turns": 3, "modelUsage": {
        "openai/gpt-5": {"inputTokens": 10, "outputTokens": 20, "cacheReadInputTokens": 30,
                         "cacheCreationInputTokens": 40, "costUSD": 0.5},
        "openai/gpt-5-mini": {"inputTokens": 1, "outputTokens": 2, "cacheReadInputTokens": 3,
                              "cacheCreationInputTokens": 4, "costUSD": 0.1},
        "odd": {"inputTokens": -1}}}
    assert claude.model_usage(event, slug="or-x") == {
        "models": {"or-x": {"in": 11, "out": 22, "cache_read": 33, "cache_write": 44, "cost": 0}}, "requests": 3}
    assert "'odd'" in capsys.readouterr().err
    assert claude.model_usage({"type": "result"}, slug="or-x") == {"models": {}, "requests": 0}
    [end] = claude.Normalizer("or-x").feed(event)
    assert set(end["usage"]["models"]) == {"or-x"}


def test_a_synthetic_assistant_message_is_not_an_answer_and_a_400_is_a_rejection():
    """Final review (Important 1): Claude Code reports an API failure as an assistant message with
    model <synthetic> and an error before the result — that is not the model answering; only an
    invalid_request (a 400: the provider refused the request) counts as the history being rejected."""
    norm = claude.Normalizer("or-x")
    norm.feed({"type": "assistant", "error": "invalid_request",
               "message": {"role": "assistant", "model": "<synthetic>",
                           "content": [{"type": "text", "text": "API Error: 400 invalid signature"}]}})
    [end] = norm.feed({"type": "result", "subtype": "success", "is_error": True, "result": "API Error: 400"})
    assert end["assistant"] is False and end["rejected"] is True
    norm.feed({"type": "assistant", "error": "server_error",
               "message": {"role": "assistant", "model": "<synthetic>", "content": []}})
    [end] = norm.feed({"type": "result", "subtype": "success", "is_error": True, "result": "API Error: 500"})
    assert end["assistant"] is False and end["rejected"] is False
    norm.feed({"type": "assistant", "message": {"model": "claude-opus-5-5", "content": [{"type": "text", "text": "hi"}]}})
    [end] = norm.feed({"type": "result", "subtype": "success"})
    assert end["assistant"] is True and end["rejected"] is False


def test_turn_end_says_whether_the_model_answered():
    norm = claude.Normalizer("or-x")
    [end] = norm.feed({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "api 400"})
    assert end["assistant"] is False and end["error"] == "api 400"
    norm.feed({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}})
    [end] = norm.feed({"type": "result", "subtype": "success"})
    assert end["assistant"] is True
    [end] = norm.feed({"type": "result", "subtype": "success"})  # per turn
    assert end["assistant"] is False
