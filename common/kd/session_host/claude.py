"""The session host's adapter for Claude Code: how a session starts, what goes in, what comes out.

A session is one ``agent-launch <agent> --session --context <session> <thread> <card|chat> [<resume id>]`` process
under the agent's user (the context names its place to the model), started by root
through ``systemd-run --pipe`` (its own unit, the agent's checked launch, the login of the agent's user). Input is
one stream-json user message per line; output is stream-json events, which ``Normalizer`` turns into the host's
report items. What reaches the cabinet: the agent's text, each tool call as its tool name and a masked one-line
summary, each tool result as ok and size with a masked excerpt of its output (the cabinet keeps it apart
from the thread, for whoever controls the agent). Thinking and anything unknown stay in the host's raw file.
"""

import json
import re
import sys
from typing import List, Mapping, Optional

from kd import claude_token, llm_models, memory_bank, naming, redact
from kd.egress import EXIT as EGRESS
from kd.cabinet import usage
from kd.session_host.protocol import AUTH_FAILED

LAUNCH = "/usr/local/bin/agent-launch"
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
AGENT = re.compile(r"[a-z][a-z0-9_-]{0,31}")
USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
UNIT = re.compile(r"digipaddock-session-[a-z0-9-]{1,64}")
SESSION = re.compile(r"[0-9a-f]{32}")
CARD = re.compile(r"[A-Z][A-Z0-9]{0,7}-[1-9][0-9]{0,5}")
# Every session unit lives in this slice: stopping it stops them all and never the host (digipaddock-session-host.service).
SLICE = "digipaddock-sessions.slice"
CLEANUP = ["systemctl", "stop", SLICE]
MAX_LINE = 8 << 20
TEXT_MAX = 20000
SUMMARY_MAX = 300
OUTPUT_MAX = 4000
MEMORY_TOOL = re.compile(r"(?:^|__)hindsight_(search_knowledge_pages|read_knowledge_page|list_knowledge_pages|reflect|"
                         r"ingest_document|capture_initiative|sync_status|diagnose)$")
MEMORY_WRITES = frozenset({"ingest_document", "capture_initiative"})

_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])")
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]+")
_OUTPUT_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]+")  # an excerpt keeps its lines and tabs
_MASKS = (
    (re.compile(r"(://)[^/\s:@]+:[^/\s@]+@"), r"\1***@"),
    (re.compile(r"(?i)(bearer\s+)\S+"), r"\1***"),
    (re.compile(r"(-u\s+)[^\s:]+:\S+"), r"\1***"),
    (re.compile(r"(?i)\b(mysql|mysqldump|mariadb|mariadb-dump)\b([^|;&]*?\s)-p(\S+)"), r"\1\2-p***"),
    (
        re.compile(r"(?i)\b([a-z0-9_]*(?:password|passwd|token|secret|apikey|api_key|key))(\s*[=:]\s*)"
                   r"(\"[^\"]*\"|'[^']*'|\S+)"),
        r"\1\2***",
    ),
)


def context_args(session: str, thread: int, card: str, space: str = "", memory: str = "",
                 model: Optional[Mapping] = None, effort: str = "", egress: str = "") -> List[str]:
    """``--context <session> <thread> <card|chat|monitor>`` — the session's place as the cabinet routed it,
    from checked ids only (``agent-launch`` checks them again and names them to the model); ``monitor`` — the
    agent's "Monitor" thread; ``--memory <bank>`` — a card's session on its project's Hindsight bank;
    ``--model <upstream>`` and ``--effort <level>`` — a card's session on a cabinet model (its id at the
    provider) and an effort, over the agent's plan."""
    if not SESSION.fullmatch(session or ""):
        raise ValueError("bad session id")
    if not isinstance(thread, int) or isinstance(thread, bool) or thread <= 0:
        raise ValueError("bad thread")
    if card and not CARD.fullmatch(card):
        raise ValueError("bad card")
    if space not in ("", "monitor") or (space and card):
        raise ValueError("bad space")
    if memory and not (card and memory_bank.BANK.fullmatch(memory)):
        raise ValueError("bad memory bank")
    if (model is not None or effort) and not card:
        raise ValueError("a model or an effort is a card session's")
    if egress and not (card and EGRESS.fullmatch(egress)):
        raise ValueError("a probe exit is a card session's")
    extra = []
    if model is not None:
        extra += ["--model", llm_models.check_upstream(llm_models.check_spec(model)["upstream"])]
    if effort:
        extra += ["--effort", llm_models.check_effort(effort)]
    if egress:
        extra += ["--egress", egress]  # the autonomy design, piece 2: agent-launch, then kd.launch, add the exit
    return ["--context", session, str(thread), card or space or "chat", *(["--memory", memory] if memory else []),
            *extra]


_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_ENV_VALUE = re.compile(r"[A-Za-z0-9._:/\[\]-]*")
_ENV_FILE = re.compile(r"/[A-Za-z0-9._/-]{1,200}")


def setenv_args(setenv: Optional[Mapping]) -> List[str]:
    """``--setenv=NAME=value`` of plain variables only: names and values of the checked shapes, never a secret
    (``systemctl show`` prints them to anyone)."""
    plain = []
    for name, value in (setenv or {}).items():
        if not isinstance(name, str) or not _ENV_NAME.fullmatch(name) or not isinstance(value, str) \
                or not _ENV_VALUE.fullmatch(value):
            raise ValueError(f"bad environment variable {str(name)[:40]!r}")
        plain.append(f"--setenv={name}={value}")
    return plain


def argv(agent: str, user: str, unit: str, resume: str, context: List[str], *, env_file: str = claude_token.PATH,
         setenv: Optional[Mapping] = None) -> List[str]:
    """``context`` — ``context_args`` of the session; ``env_file`` — the root-only file systemd reads the process's
    credential from (the host's Claude token, or a provider kind's key); ``setenv`` — plain variables of
    the provider (the base URL, the model aliases: ``kd.llm_models.environment``), never a secret."""
    if not AGENT.fullmatch(agent or ""):
        raise ValueError(f"bad agent name {agent!r}")
    if not USER.fullmatch(user or "") or user == "root":
        raise ValueError(f"bad agent user {user!r}")
    if not UNIT.fullmatch(unit or ""):
        raise ValueError(f"bad unit name {unit!r}")
    if resume and not UUID.fullmatch(resume):
        raise ValueError("bad resume id")
    if not _ENV_FILE.fullmatch(env_file or ""):
        raise ValueError("bad environment file")
    plain = setenv_args(setenv)
    return [
        "systemd-run", f"--unit={unit}", f"--slice={SLICE}", f"--uid={user}", "--pipe", "--wait", "--quiet",
        "--collect", "--service-type=exec", "-p", "KillMode=control-group", "-p", "TimeoutStopSec=20",
        # systemd reads the credential as root; never --setenv for it, which systemctl show prints to
        # anyone — only the provider's plain variables ride that way. The host's token file is optional
        # (no file: the user's stored login); a kind's file is required — without it the unit must not start on the
        # stored login with ANTHROPIC_BASE_URL pointing at the provider. agent-launch hands the credential
        # to claude by a pipe (kd.secret_fd), so it is not in claude's own environ
        "-p", f"EnvironmentFile={'-' if env_file == claude_token.PATH else ''}{env_file}",
        "-p", "NoNewPrivileges=yes",  # no setuid or file caps under the agent's UID
        *plain,
        LAUNCH, agent, "--session", *context, *([resume] if resume else []),
    ]


def input_line(text: str) -> bytes:
    """One prompt of the session; the repository's command and card names in it are the host's."""
    message = {"type": "user", "message": {"role": "user", "content": naming.rewrite(text)}}
    return json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"


def parse_line(raw: bytes) -> Optional[dict]:
    """One output line as an event, or None (too long, not JSON, not an object)."""
    if len(raw) > MAX_LINE:
        return None
    try:
        event = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return event if isinstance(event, dict) else None


def _clean(text: str) -> str:
    return " ".join(_CTRL.sub(" ", _ANSI.sub("", text)).split())


def mask(text: str) -> str:
    for pattern, replacement in _MASKS:
        text = pattern.sub(replacement, text)
    return text


def _detail(name: str, data: Mapping) -> str:
    def get(key):
        value = data.get(key)
        return value if isinstance(value, str) else ""

    if name == "Bash":
        return get("command")
    if name in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit"):
        return get("file_path") or get("notebook_path")
    if name in ("Grep", "Glob"):
        return get("pattern") + (f" in {get('path')}" if get("path") else "")
    if name == "WebFetch":
        return get("url")
    if name == "WebSearch":
        return get("query")
    if name in ("Task", "Agent"):
        return get("description")
    if name == "TodoWrite" and isinstance(data.get("todos"), list):
        return f"{len(data['todos'])} items"
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def summary(name: str, data) -> str:
    """One masked line of at most SUMMARY_MAX characters: the tool and what it was asked."""
    name = _clean(str(name))[:64] or "tool"
    detail = _detail(name, data if isinstance(data, Mapping) else {})
    line = mask(_clean(f"{name}: {detail}"))
    return line if len(line) <= SUMMARY_MAX else line[: SUMMARY_MAX - 1] + "…"


def output_excerpt(content) -> Optional[dict]:
    """A tool result's output as the cabinet may show it: text blocks joined (any other block as ``[<type>]``),
    cleaned of terminal codes, masked by the sysadmin gate's rules and then this adapter's line by line, the first
    OUTPUT_MAX characters. ``size`` is the whole output's; None when the gate's rules are unavailable."""
    size = len(json.dumps(content, ensure_ascii=False))
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
            else:
                kind = block.get("type") if isinstance(block, Mapping) else None
                parts.append(f"[{_clean(str(kind or 'block'))[:32]}]")
        raw = "\n".join(parts)
    else:
        raw = content if isinstance(content, str) else ""
    # masked before the cut, so a secret across the edge is not left half; four times the excerpt bounds the work
    cleaned = _OUTPUT_CTRL.sub("", _ANSI.sub("", raw[: OUTPUT_MAX * 4].replace("\r\n", "\n")))
    masked = redact.text(cleaned)
    if masked is None:
        return None
    text = "\n".join(mask(line) for line in masked.split("\n"))
    return {"text": text[:OUTPUT_MAX], "size": size, "truncated": len(text) > OUTPUT_MAX or len(raw) > OUTPUT_MAX * 4}


def _message(kind: str, body: str, payload: Mapping) -> dict:
    return {"type": "message", "kind": kind, "body": body, "payload": dict(payload)}


_MODEL_FIELDS = (("in", "inputTokens"), ("out", "outputTokens"), ("cache_read", "cacheReadInputTokens"),
                 ("cache_write", "cacheCreationInputTokens"), ("cost", "costUSD"))


def model_usage(event: Mapping, slug: str = "") -> dict:
    """Claude's model usage of the whole session so far (``modelUsage`` is cumulative across resumes; the cabinet
    takes the turn's share) and this turn's API calls (``num_turns``). A model with an odd name or number is
    dropped with a journal line: the cabinet would refuse the report, and that spend is counted nowhere.

    a process on a cabinet model (``slug``) reports every model it used summed under the slug, with no
    cost — the provider's names would not pass the ledger, and the cabinet prices the slug by its own table."""
    raw = event.get("modelUsage")
    models = {}
    total = {key: 0 for key, _ in _MODEL_FIELDS}
    for name, data in raw.items() if isinstance(raw, Mapping) else ():
        if not slug and len(models) == usage.MODELS_MAX:
            break
        try:
            if not isinstance(data, Mapping) or (not slug and not (isinstance(name, str) and usage.MODEL.fullmatch(name))):
                raise ValueError("an odd model name")
            fields = usage.check_model({key: data.get(source, 0) for key, source in _MODEL_FIELDS})
        except ValueError as exc:
            sys.stderr.write(f"usage of model {str(name)[:80]!r} dropped: {exc}\n")
            continue
        if slug:
            for key in total:
                total[key] += fields[key]
            models[slug] = {**total, "cost": 0}
        else:
            models[name] = fields
    requests = event.get("num_turns")
    return {"models": models, "requests": requests if usage._count(requests) else 0}


class Normalizer:
    """stream-json events of one process → the host's items: ``session`` (a new Claude session id),
    ``message`` (text, tool_call, tool_result, status) and ``turn_end`` (ok, usage, error, assistant — the model
    answered at least once this turn; rejected — the API refused a request as invalid, a 400). ``slug`` — the
    cabinet model the process runs on, for its usage."""

    def __init__(self, slug: str = ""):
        self.slug = slug
        self.runtime_session_id = ""
        self.auth_failed = False  # this turn's synthetic message said Claude refused the credentials
        self.saw_assistant = False  # the model answered this turn (a synthetic message is not an answer)
        self.rejected = False  # the API answered a request with invalid_request (the history, mostly)
        self.memory_calls = {}

    def feed(self, event: Mapping) -> List[dict]:
        kind, subtype = event.get("type"), event.get("subtype")
        if kind == "system" and subtype == "init":
            sid = event.get("session_id")
            if isinstance(sid, str) and UUID.fullmatch(sid) and sid != self.runtime_session_id:
                self.runtime_session_id = sid
                return [{"type": "session", "state": "working", "runtime_session_id": sid}]
            return []
        if kind == "system" and subtype == "permission_denied":
            tool = _clean(str(event.get("tool_name") or event.get("tool") or "tool"))[:64]
            return [_message("status", f"denied: {tool}", {"key": "sys.session.denied", "params": {"tool": tool}})]
        if kind == "assistant":
            # Claude Code reports an API failure as an assistant event with an error and the model <synthetic>
            error, message = event.get("error"), event.get("message")
            synthetic = bool(error) or (isinstance(message, Mapping) and message.get("model") == "<synthetic>")
            self.auth_failed = self.auth_failed or error == "authentication_failed"
            self.rejected = self.rejected or error == "invalid_request"
            self.saw_assistant = self.saw_assistant or not synthetic
            return self._assistant(event)
        if kind == "user":
            return self._results(event)
        if kind == "result":
            ok = event.get("subtype") == "success" and event.get("is_error") is not True
            item = {"type": "turn_end", "ok": ok, "usage": model_usage(event, self.slug),
                    "assistant": self.saw_assistant, "rejected": self.rejected}
            if not ok:
                # the result's own text says why (a «success» with is_error has no other); else the subtype
                text = event.get("result")
                why = text if isinstance(text, str) and text.strip() else str(event.get("subtype") or "error")
                item["error"] = AUTH_FAILED if self.auth_failed else _clean(why)[:100]
            self.auth_failed = self.saw_assistant = self.rejected = False
            pending = [self._memory(operation, "interrupted", call) for call, operation in self.memory_calls.items()]
            self.memory_calls.clear()
            return [*pending, item]
        return []

    @staticmethod
    def _content(event: Mapping) -> list:
        message = event.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        return [b for b in content if isinstance(b, Mapping)] if isinstance(content, list) else []

    def _assistant(self, event) -> List[dict]:
        out = []
        for block in self._content(event):
            if block.get("type") == "text" and isinstance(block.get("text"), str) and block["text"].strip():
                out.append(_message("text", block["text"][:TEXT_MAX], {}))
            elif block.get("type") == "tool_use":
                tool = _clean(str(block.get("name") or "tool"))[:64]
                out.append(
                    _message("tool_call", summary(tool, block.get("input")), {"tool": tool, "id": str(block.get("id", ""))[:64]})
                )
                match = MEMORY_TOOL.search(tool)
                call = str(block.get("id", ""))[:64]
                if match and call and len(self.memory_calls) < 256:
                    operation = match.group(1)
                    self.memory_calls[call] = operation
                    out.append(self._memory(operation, "started", call))
        return out

    @staticmethod
    def _memory(operation, state, call):
        # No query, source text or inferred source count enters the public thread. A returned write call is
        # not proof of completed indexing, and a returned read is not proof that the agent used its results.
        return _message("status", "", {"key": "sys.session.memory", "params": {}, "activity": {
            "type": "memory", "operation": operation, "state": state, "call": call,
            "mode": "write" if operation in MEMORY_WRITES else "read",
        }})

    def _results(self, event) -> List[dict]:
        out = []
        for block in self._content(event):
            if block.get("type") == "tool_result":
                size = len(json.dumps(block.get("content"), ensure_ascii=False))
                payload = {"id": str(block.get("tool_use_id", ""))[:64], "ok": block.get("is_error") is not True,
                           "size": size}
                item = _message("tool_result", "", payload)
                excerpt = output_excerpt(block.get("content"))
                if excerpt is not None:
                    item["output"] = excerpt
                out.append(item)
                call = str(block.get("tool_use_id", ""))[:64]
                operation = self.memory_calls.pop(call, None)
                if operation:
                    out.append(self._memory(operation, "failed" if block.get("is_error") is True else "returned", call))
        return out
