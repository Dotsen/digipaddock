"""Optional installed-Claude integration against a fake OpenAI upstream, never a paid API."""

import json
import os
import socket
import subprocess
import threading

import pytest

from kd import openai_bridge
from kd.worker_packs import ISOLATION_ARGS


@pytest.mark.host
@pytest.mark.skipif(
    not os.environ.get("KD_TEST_CLAUDE_BIN"),
    reason="explicit installed Claude binary required",
)
def test_installed_claude_runs_a_read_tool_round_through_openai_transport(
    tmp_path, monkeypatch
):
    import uvicorn

    target = tmp_path / "read-fixture.txt"
    target.write_text("BRIDGE_READ_PAYLOAD", encoding="utf-8")
    calls = []
    shapes = []
    original = openai_bridge.request_of

    def inspect(body):
        shapes.append([m.get("role") for m in body.get("messages", [])])
        return original(body)

    monkeypatch.setattr(openai_bridge, "request_of", inspect)

    def upstream(payload, key):
        assert key == "fixture-cli-key"
        calls.append(payload)
        results = [
            item
            for item in payload["input"]
            if item.get("type") == "function_call_output"
        ]
        if not results and any(
            tool["name"] == "Read" for tool in payload.get("tools", [])
        ):
            output = [
                {
                    "type": "reasoning",
                    "id": "rs_fixture",
                    "encrypted_content": "fixture-opaque-state",
                    "summary": [],
                },
                {
                    "type": "function_call",
                    "name": "Read",
                    "call_id": "call_fixture",
                    "arguments": json.dumps({"file_path": str(target)}),
                },
            ]
        else:
            output = [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "BRIDGE_FIXTURE_OK"}],
                }
            ]
        return {
            "status": "completed",
            "output": output,
            "usage": {
                "input_tokens": 100,
                "output_tokens": 10,
                "input_tokens_details": {"cached_tokens": 0},
            },
        }

    app = openai_bridge.create_app(
        upstream=upstream,
        credential=lambda: "fixture-cli-key",
        counter=lambda *a: {"input_tokens": 100},
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.listen(128)
    server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="error"))
    thread = threading.Thread(
        target=lambda: server.run(sockets=[listener]), daemon=True
    )
    thread.start()
    home = tmp_path / "home"
    home.mkdir()
    try:
        result = subprocess.run(
            [
                os.environ["KD_TEST_CLAUDE_BIN"],
                "-p",
                "Read the fixture with Read, then report success.",
                "--model",
                "gpt-fixture",
                "--tools",
                "Read",
                "--allowedTools",
                "Read",
                "--max-turns",
                "3",
                "--output-format",
                "json",
                "--no-session-persistence",
                *ISOLATION_ARGS,
            ],
            cwd=tmp_path,
            timeout=90,
            env={
                "HOME": str(home),
                "CLAUDE_CONFIG_DIR": str(home / "config"),
                "PATH": os.environ.get("PATH", os.defpath),
                "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
                "USERPROFILE": str(home),
                "APPDATA": str(home / "Roaming"),
                "LOCALAPPDATA": str(home / "Local"),
                "TMP": str(home),
                "TEMP": str(home),
                "LANG": "C.UTF-8",
                "ANTHROPIC_AUTH_TOKEN": "fixture-cli-key",
                "ANTHROPIC_API_KEY": "",
                "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
                "ENABLE_TOOL_SEARCH": "false",
                "DISABLE_AUTOUPDATER": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "DISABLE_TELEMETRY": "1",
                "DISABLE_ERROR_REPORTING": "1",
            },
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            repr(shapes) + result.stderr[-1000:] + result.stdout[-1000:]
        )
        assert "BRIDGE_FIXTURE_OK" in result.stdout
        outputs = [
            item
            for payload in calls
            for item in payload["input"]
            if item.get("type") == "function_call_output"
        ]
        assert outputs and "BRIDGE_READ_PAYLOAD" in json.dumps(outputs)
        assert any(
            item.get("encrypted_content") == "fixture-opaque-state"
            for payload in calls
            for item in payload["input"]
        )
    finally:
        server.should_exit = True
        thread.join(5)
        listener.close()
