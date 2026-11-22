"""OpenAI Responses transport for the existing Claude Code harness.

The loopback adapter never executes tools: Claude Code and its existing guards do that. A caller must
present the current root-only OpenAI provider credential. Requests go only to OpenAI's fixed endpoint,
with no redirects, storage or request/response logging. Reasoning items survive tool rounds opaquely.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import re
import urllib.error
import urllib.request
import uuid

from kd import llm_models

URL = "https://api.openai.com/v1/responses"
BODY_MAX = 16 * 1024 * 1024
TIMEOUT = 180
TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")


class BridgeError(Exception):
    def __init__(self, message: str, status=400):
        super().__init__(message)
        self.status = status


def tool_name(name: str) -> str:
    return (
        name
        if TOOL_NAME.fullmatch(name)
        else "tool_" + hashlib.sha256(name.encode()).hexdigest()[:48]
    )


def parts(content, *, output=False) -> list:
    if isinstance(content, str):
        return [{"type": "output_text" if output else "input_text", "text": content}]
    if not isinstance(content, list):
        raise BridgeError("content must be text or blocks")
    result = []
    for block in content:
        kind = block.get("type")
        if kind == "text":
            result.append(
                {
                    "type": "output_text" if output else "input_text",
                    "text": block["text"],
                }
            )
        elif kind in ("image", "document") and not output:
            source = block.get("source", {})
            if source.get("type") == "base64":
                media = source.get("media_type", "")
                data = "data:" + media + ";base64," + source["data"]
                if kind == "image" and media in (
                    "image/png",
                    "image/jpeg",
                    "image/webp",
                    "image/gif",
                ):
                    result.append({"type": "input_image", "image_url": data})
                elif kind == "document" and media == "application/pdf":
                    result.append(
                        {
                            "type": "input_file",
                            "filename": "document.pdf",
                            "file_data": data,
                        }
                    )
                else:
                    raise BridgeError("unsupported media type")
            elif (
                source.get("type") == "url"
                and str(source.get("url", "")).startswith("https://")
                and kind == "image"
            ):
                result.append({"type": "input_image", "image_url": source["url"]})
            elif kind == "document" and source.get("type") == "text":
                result.append({"type": "input_text", "text": source["data"]})
            else:
                raise BridgeError("unsupported media source")
        else:
            raise BridgeError("unsupported content block")
    return result


def request_of(body: dict) -> tuple[dict, dict]:
    """Anthropic Messages → stateless Responses input; the second result restores tool aliases."""
    if (
        not isinstance(body, dict)
        or not isinstance(body.get("model"), str)
        or not body["model"]
    ):
        raise BridgeError("model is required")
    tools, aliases = [], {}
    for tool in body.get("tools", []):
        if not isinstance(tool.get("name"), str) or "input_schema" not in tool:
            raise BridgeError("only client-executed function tools are supported")
        name = tool_name(tool["name"])
        aliases[name] = tool["name"]
        tools.append(
            {
                "type": "function",
                "name": name,
                "description": tool.get("description", ""),
                "parameters": tool["input_schema"],
                "strict": False,
            }
        )
    inputs = []
    if body.get("system"):
        inputs.append({"role": "developer", "content": parts(body["system"])})
    for message in body.get("messages", []):
        role = message.get("role")
        if role not in ("user", "assistant", "system", "developer"):
            raise BridgeError("unsupported message role")
        if role == "system":
            role = "developer"
        blocks = message.get("content", [])
        if isinstance(blocks, str):
            blocks = [{"type": "text", "text": blocks}]
        pending = []

        def flush():
            if pending:
                inputs.append(
                    {
                        "role": role,
                        "content": parts(pending, output=role == "assistant"),
                    }
                )
                pending.clear()

        for block in blocks:
            kind = block.get("type")
            if kind == "tool_use" and role == "assistant":
                flush()
                inputs.append(
                    {
                        "type": "function_call",
                        "call_id": block["id"],
                        "name": tool_name(block["name"]),
                        "arguments": json.dumps(block["input"], ensure_ascii=False),
                    }
                )
            elif kind == "tool_result" and role == "user":
                flush()
                result = block.get("content", "")
                if block.get("is_error"):
                    result = [{"type": "text", "text": "[tool error]"}] + (
                        [{"type": "text", "text": result}]
                        if isinstance(result, str)
                        else result
                    )
                inputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": block["tool_use_id"],
                        "output": result if isinstance(result, str) else parts(result),
                    }
                )
            elif kind == "redacted_thinking" and role == "assistant":
                flush()
                try:
                    item = json.loads(base64.b64decode(block["data"], validate=True))
                except (ValueError, TypeError):
                    raise BridgeError("invalid reasoning state") from None
                if (
                    not isinstance(item, dict)
                    or item.get("type") != "reasoning"
                    or set(item)
                    - {"type", "id", "encrypted_content", "summary", "status"}
                ):
                    raise BridgeError("invalid reasoning state")
                inputs.append(item)
            elif kind == "thinking":
                # Anthropic-only visible reasoning has no corresponding OpenAI encrypted state.
                continue
            else:
                pending.append(block)
        flush()
    out = {
        "model": body["model"],
        "input": inputs,
        "store": False,
        "stream": False,
        "include": ["reasoning.encrypted_content"],
    }
    if tools:
        out["tools"] = tools
    limit = body.get("max_tokens")
    if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
        out["max_output_tokens"] = limit
    choice = body.get("tool_choice", {})
    if choice.get("type") in ("auto", "none", "any"):
        out["tool_choice"] = {"any": "required"}.get(choice["type"], choice["type"])
    elif choice.get("type") == "tool":
        out["tool_choice"] = {"type": "function", "name": tool_name(choice["name"])}
    if choice.get("disable_parallel_tool_use"):
        out["parallel_tool_calls"] = False
    effort = body.get("output_config", {}).get("effort")
    if effort in ("low", "medium", "high", "max", "xhigh"):
        out["reasoning"] = {"effort": "high" if effort in ("max", "xhigh") else effort}
    return out, aliases


def response_of(response: dict, model: str, aliases: dict) -> dict:
    content = []
    for item in response.get("output", []):
        if item.get("type") == "reasoning":
            if item.get("encrypted_content"):
                saved = {
                    k: v
                    for k, v in item.items()
                    if k in {"type", "id", "encrypted_content", "summary", "status"}
                }
                content.append(
                    {
                        "type": "redacted_thinking",
                        "data": base64.b64encode(json.dumps(saved).encode()).decode(),
                    }
                )
        elif item.get("type") == "function_call":
            if item.get("name") not in aliases:
                raise BridgeError("OpenAI returned an unknown tool", 502)
            try:
                args = json.loads(item["arguments"])
            except (ValueError, TypeError):
                raise BridgeError(
                    "OpenAI returned invalid tool arguments", 502
                ) from None
            if not isinstance(args, dict):
                raise BridgeError("OpenAI returned non-object tool arguments", 502)
            content.append(
                {
                    "type": "tool_use",
                    "id": item["call_id"],
                    "name": aliases[item["name"]],
                    "input": args,
                }
            )
        elif item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") in ("output_text", "refusal"):
                    content.append(
                        {
                            "type": "text",
                            "text": part.get("text", part.get("refusal", "")),
                        }
                    )
    if response.get("status") not in ("completed", "incomplete"):
        raise BridgeError("OpenAI did not complete the response", 502)
    if not content:
        raise BridgeError("OpenAI returned no usable content", 502)
    usage = response.get("usage") or {}
    cached = (usage.get("input_tokens_details") or {}).get("cached_tokens", 0)
    return {
        "id": "msg_" + uuid.uuid4().hex,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": "max_tokens"
        if response.get("status") == "incomplete"
        else "tool_use"
        if any(p["type"] == "tool_use" for p in content)
        else "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": max(0, usage.get("input_tokens", 0) - cached),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_read_input_tokens": cached,
            "cache_creation_input_tokens": 0,
        },
    }


def stream_of(message: dict):
    """Anthropic SSE framing. Upstream is buffered so partial function JSON is never executed."""

    def event(kind, **data):
        return (
            "event: "
            + kind
            + "\ndata: "
            + json.dumps({"type": kind, **data}, ensure_ascii=False)
            + "\n\n"
        ).encode()

    yield event(
        "message_start",
        message={
            **message,
            "content": [],
            "stop_reason": None,
            "usage": {**message["usage"], "output_tokens": 0},
        },
    )
    for index, part in enumerate(message["content"]):
        if part["type"] == "text":
            yield event(
                "content_block_start",
                index=index,
                content_block={"type": "text", "text": ""},
            )
            yield event(
                "content_block_delta",
                index=index,
                delta={"type": "text_delta", "text": part["text"]},
            )
        elif part["type"] == "tool_use":
            yield event(
                "content_block_start", index=index, content_block={**part, "input": {}}
            )
            yield event(
                "content_block_delta",
                index=index,
                delta={
                    "type": "input_json_delta",
                    "partial_json": json.dumps(part["input"]),
                },
            )
        else:
            yield event("content_block_start", index=index, content_block=part)
        yield event("content_block_stop", index=index)
    yield event(
        "message_delta",
        delta={"stop_reason": message["stop_reason"], "stop_sequence": None},
        usage={"output_tokens": message["usage"]["output_tokens"]},
    )
    yield event("message_stop")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise BridgeError("OpenAI redirects are refused", 502)


def post(payload: dict, key: str, *, count=False) -> dict:
    request = urllib.request.Request(
        URL + ("/input_tokens" if count else ""),
        json.dumps(payload).encode(),
        {"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.build_opener(NoRedirect).open(
            request, timeout=TIMEOUT
        ) as response:
            raw = response.read(BODY_MAX + 1)
    except urllib.error.HTTPError as exc:
        raise BridgeError(
            f"OpenAI request failed (HTTP {exc.code})",
            exc.code if exc.code in (400, 401, 403, 404, 429) else 502,
        ) from None
    except (OSError, urllib.error.URLError):
        raise BridgeError("OpenAI is unavailable", 502) from None
    if len(raw) > BODY_MAX:
        raise BridgeError("OpenAI response too large", 502)
    return json.loads(raw)


def create_app(*, upstream=post, credential=None, counter=None):
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, StreamingResponse

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    slots = asyncio.Semaphore(8)
    credential = credential or (
        lambda: llm_models.read_secret("openai").get(llm_models.SECRET_KEY, "")
    )
    counter = counter or (lambda payload, key: post(payload, key, count=True))

    @app.post("/v1/messages")
    @app.post("/v1/messages/count_tokens")
    async def messages(request: Request):
        try:
            key = credential()
            given = request.headers.get("authorization", "").removeprefix(
                "Bearer "
            ) or request.headers.get("x-api-key", "")
            if not key or not hmac.compare_digest(given.encode(), key.encode()):
                raise BridgeError("OpenAI credential was not accepted", 401)
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > BODY_MAX:
                    raise BridgeError("request too large", 413)
            body = json.loads(raw)
            payload, aliases = request_of(body)
            async with slots:
                if request.url.path.endswith("/count_tokens"):
                    counted = await asyncio.to_thread(
                        counter,
                        {
                            k: v
                            for k, v in payload.items()
                            if k
                            in {
                                "model",
                                "input",
                                "tools",
                                "tool_choice",
                                "reasoning",
                                "parallel_tool_calls",
                            }
                        },
                        key,
                    )
                    return JSONResponse(
                        {"input_tokens": counted["input_tokens"]},
                        headers={"Cache-Control": "no-store"},
                    )
                response = await asyncio.to_thread(upstream, payload, key)
            message = response_of(response, body["model"], aliases)
            if body.get("stream"):
                return StreamingResponse(
                    stream_of(message),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-store"},
                )
            return JSONResponse(message, headers={"Cache-Control": "no-store"})
        except BridgeError as exc:
            return JSONResponse(
                {
                    "type": "error",
                    "error": {
                        "type": "authentication_error"
                        if exc.status == 401
                        else "rate_limit_error"
                        if exc.status == 429
                        else "api_error",
                        "message": str(exc),
                    },
                },
                status_code=exc.status,
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            return JSONResponse(
                {
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": "invalid provider request or response",
                    },
                },
                status_code=400,
            )

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(
        create_app(),
        host="127.0.0.1",
        port=17841,
        access_log=False,
        log_level="error",
        limit_concurrency=16,
    )
