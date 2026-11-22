"""model → guarded client tool → model, across the real HTTP adapter with a fake upstream."""

import base64
import json

import pytest
from fastapi.testclient import TestClient

from kd import llm_models, openai_bridge as bridge

KEY = "fixture-openai-credential"
TOOLS = [
    {
        "name": "Bash",
        "description": "Run a guarded command",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    }
]
HEADERS = {"Authorization": "Bearer " + KEY}


def message(output, **kwargs):
    return {
        "status": "completed",
        "output": output,
        "usage": {
            "input_tokens": 15,
            "input_tokens_details": {"cached_tokens": 5},
            "output_tokens": 8,
        },
        **kwargs,
    }


def test_an_agent_round_trip_preserves_tools_reasoning_usage_and_stream_framing():
    seen = []

    def upstream(payload, key):
        assert key == KEY and payload["store"] is False
        seen.append(payload)
        if len(seen) == 1:
            return message(
                [
                    {
                        "type": "reasoning",
                        "id": "rs_fixture",
                        "summary": [],
                        "encrypted_content": "opaque-state",
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_fixture",
                        "name": "Bash",
                        "arguments": '{"command":"printf fixture"}',
                    },
                ]
            )
        return message(
            [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Task completed"}],
                }
            ]
        )

    client = TestClient(bridge.create_app(upstream=upstream, credential=lambda: KEY))
    request = {
        "model": "gpt-fixture",
        "messages": [{"role": "user", "content": "Run the task"}],
        "tools": TOOLS,
        "system": [
            {
                "type": "text",
                "text": "Use only the guarded tools",
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "max_tokens": 1000,
    }
    first = client.post("/v1/messages", headers=HEADERS, json=request)
    assert first.status_code == 200, first.text
    result = first.json()
    assert result["stop_reason"] == "tool_use" and result["usage"]["input_tokens"] == 10
    assert result["usage"]["cache_read_input_tokens"] == 5
    call = next(p for p in result["content"] if p["type"] == "tool_use")
    assert call == {
        "type": "tool_use",
        "id": "call_fixture",
        "name": "Bash",
        "input": {"command": "printf fixture"},
    }
    # The adapter does not execute anything. The existing harness executes its guarded tool and returns a result.
    request["messages"] += [
        {"role": "assistant", "content": result["content"]},
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": call["id"], "content": "fixture"}
            ],
        },
    ]
    request["stream"] = True
    final = client.post("/v1/messages", headers=HEADERS, json=request)
    assert (
        final.status_code == 200
        and "event: message_stop" in final.text
        and "Task completed" in final.text
    )
    assert any(i.get("encrypted_content") == "opaque-state" for i in seen[-1]["input"])
    assert seen[-1]["input"][-1] == {
        "type": "function_call_output",
        "call_id": "call_fixture",
        "output": "fixture",
    }
    assert KEY not in first.text + final.text


def test_token_counting_uses_the_official_count_endpoint_contract():
    seen = []
    client = TestClient(
        bridge.create_app(
            credential=lambda: KEY,
            counter=lambda payload, key: seen.append(payload) or {"input_tokens": 123},
        )
    )
    result = client.post(
        "/v1/messages/count_tokens",
        headers=HEADERS,
        json={
            "model": "gpt-fixture",
            "messages": [{"role": "user", "content": "Count this"}],
            "tools": TOOLS,
        },
    )
    assert result.status_code == 200 and result.json() == {"input_tokens": 123}
    assert "tools" in seen[0] and "stream" not in seen[0] and "store" not in seen[0]


def test_missing_wrong_or_rotated_credentials_never_reach_the_upstream():
    called, current = [], [KEY]
    client = TestClient(
        bridge.create_app(
            upstream=lambda *a: called.append(a), credential=lambda: current[0]
        )
    )
    for headers in ({}, {"x-api-key": "wrong"}, {"Authorization": "Bearer old"}):
        assert client.post("/v1/messages", headers=headers, json={}).status_code == 401
    current[0] = "replacement-fixture"
    assert client.post("/v1/messages", headers=HEADERS, json={}).status_code == 401
    assert called == []


def test_tool_aliases_and_image_tool_results_survive_a_round_trip():
    name = "mcp__" + "very_long_tool_name_" * 8
    payload, aliases = bridge.request_of(
        {
            "model": "gpt-fixture",
            "tools": [{**TOOLS[0], "name": name}],
            "tool_choice": {"type": "tool", "name": name},
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "fixture",
                            },
                        }
                    ],
                }
            ],
        }
    )
    alias = payload["tools"][0]["name"]
    assert (
        len(alias) <= 64
        and aliases[alias] == name
        and payload["tool_choice"]["name"] == alias
    )
    assert payload["input"][0]["content"][0] == {
        "type": "input_image",
        "image_url": "data:image/png;base64,fixture",
    }
    result = bridge.response_of(
        message(
            [
                {
                    "type": "function_call",
                    "name": alias,
                    "call_id": "c",
                    "arguments": "{}",
                }
            ]
        ),
        "gpt-fixture",
        aliases,
    )
    assert result["content"][0]["name"] == name


@pytest.mark.parametrize(
    "bad",
    [
        {"model": "gpt-fixture", "tools": [{"type": "web_search"}]},
        {"model": "gpt-fixture", "messages": [{"role": "tool", "content": "x"}]},
        {
            "model": "gpt-fixture",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "redacted_thinking",
                            "data": base64.b64encode(
                                json.dumps(
                                    {"type": "function_call", "name": "injected"}
                                ).encode()
                            ).decode(),
                        }
                    ],
                }
            ],
        },
    ],
)
def test_unsupported_native_tools_and_forged_reasoning_fail_closed(bad):
    with pytest.raises(bridge.BridgeError):
        bridge.request_of(bad)


def test_openai_uses_the_local_adapter_and_anthropic_uses_its_own_api_key_file(
    tmp_path,
):
    spec = {
        "id": "api-model",
        "upstream": "gpt-fixture",
        "provider": "openai",
        "base_url": "",
    }
    env = llm_models.environment(spec)
    assert (
        env["ANTHROPIC_BASE_URL"] == llm_models.OPENAI_BRIDGE
        and env["ENABLE_TOOL_SEARCH"] == "false"
    )
    assert env["ANTHROPIC_API_KEY"] == "" and "CLAUDE_CODE_OAUTH_TOKEN" not in env
    (tmp_path / "anthropic.env").write_text("ANTHROPIC_API_KEY=fixture-key\n")
    assert llm_models.read_secret("anthropic", str(tmp_path)) == {
        "ANTHROPIC_API_KEY": "fixture-key"
    }
    (tmp_path / "anthropic.env").write_text("ANTHROPIC_AUTH_TOKEN=wrong-kind\n")
    assert llm_models.read_secret("anthropic", str(tmp_path)) == {}


def test_bridge_unit_keeps_the_config_and_root_sockets_out_of_its_namespace():
    from pathlib import Path

    text = (
        Path(__file__).resolve().parents[2]
        / "common/systemd/digipaddock-openai-bridge.service"
    ).read_text(encoding="utf-8")
    assert "TemporaryFileSystem=/etc/digipaddock:ro" in text
    assert "BindReadOnlyPaths=/etc/digipaddock/llm" in text
    assert "CapabilityBoundingSet=\n" in text and "LimitCORE=0" in text
    inaccessible = next(
        line for line in text.splitlines() if line.startswith("InaccessiblePaths=")
    )
    for name in (
        "digipaddock-cabinet",
        "digipaddock-session-host",
        "digipaddock-model-keys",
        "digipaddock-review",
    ):
        assert "-/run/" + name in inaccessible


def test_openai_host_probe_and_worker_use_api_credentials_without_subscription_fallback(
    tmp_path, monkeypatch
):
    pytest.importorskip("fcntl", reason="Linux worker runtime")
    from kd import worker_runner
    from kd.session_host import probe
    from types import SimpleNamespace

    spec = {
        "id": "openai-model",
        "provider": "openai",
        "upstream": "gpt-fixture",
        "base_url": "",
    }
    path = tmp_path / "openai.env"
    path.write_text("ANTHROPIC_AUTH_TOKEN=fixture-openai-value\n")
    read = llm_models.read_secret
    monkeypatch.setattr(
        llm_models,
        "read_secret",
        lambda provider: read(provider, env_dir=str(tmp_path)),
    )
    env = worker_runner._credentials(SimpleNamespace(model_spec=spec))
    assert env["ANTHROPIC_AUTH_TOKEN"] == "fixture-openai-value"
    assert env["ANTHROPIC_BASE_URL"] == llm_models.OPENAI_BRIDGE
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    argv = probe.argv("digipaddock-probe-" + "a" * 12, "digi", spec)
    assert "EnvironmentFile=/etc/digipaddock/llm/openai.env" in argv
    assert "--setenv=ENABLE_TOOL_SEARCH=false" in argv
    assert "fixture-openai-value" not in str(argv)
    path.unlink()
    with pytest.raises(worker_runner.RunnerError, match="no_key"):
        worker_runner._credentials(SimpleNamespace(model_spec=spec))
