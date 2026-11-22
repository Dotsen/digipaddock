"""The session host's model probe: one short Claude Code run on a cabinet model, under the base user.

The owner's "Check" on a model asks the cabinet, whose dispatcher asks the host (``probe``). The host runs
``claude -p`` once on the model's kind — the kind's key file as the unit's EnvironmentFile (root reads it, the base
user never sees it; ``kd.secret_fd`` hands claude the key by a pipe), the kind's base
URL and model aliases as plain variables — with nobody's settings, skills or
MCP servers, one turn, no session saved, and reports what came back: ok, the model the provider answered as, the
time and one masked line of detail. One probe at a time; the unit's RuntimeMaxSec ends a hanging one.
"""

import json
import re
from typing import List, Mapping

from kd import claude_token, llm_models, redact
from kd.session_host import claude
from kd.worker_packs import ISOLATION_ARGS

PROMPT = "Reply with one word: OK"
TIMEOUT_S = 90
DETAIL_MAX = 500
UNIT = re.compile(r"digipaddock-probe-[0-9a-f]{12}")
WITHHELD = "[output withheld: the gate's masks are unavailable]"
SECRET_FD = "/opt/digipaddock/common/kd/secret_fd.py"


def unit(probe_id: str) -> str:
    return f"digipaddock-probe-{probe_id[:12]}"


def argv(unit_name: str, user: str, spec: Mapping, timeout_s: int = TIMEOUT_S) -> List[str]:
    """The probe's transient unit: the base user's ``claude`` in its home, on the kind's key and the model's
    environment, apart from any agent's settings; nothing of the request reaches the line but the checked spec."""
    if not UNIT.fullmatch(unit_name or ""):
        raise ValueError(f"bad unit name {unit_name!r}")
    if not claude.USER.fullmatch(user or "") or user == "root":
        raise ValueError(f"bad user {user!r}")
    if not isinstance(timeout_s, int) or isinstance(timeout_s, bool) or timeout_s <= 0:
        raise ValueError("bad timeout")
    spec = llm_models.check_spec(spec) if spec is not None else None
    paid = spec is not None and not llm_models.is_subscription(spec)  # else the host's own Claude token
    env_file = llm_models.env_file(spec["provider"]) if paid else claude_token.PATH
    model = spec["upstream"] if spec else "sonnet"
    return [
        "systemd-run", f"--unit={unit_name}", f"--slice={claude.SLICE}", f"--uid={user}", "--pipe", "--wait",
        "--quiet", "--collect", "--service-type=exec", "-p", "KillMode=control-group", "-p", "TimeoutStopSec=20",
        "-p", f"RuntimeMaxSec={timeout_s}", "-p", "WorkingDirectory=~",
        "-p", f"EnvironmentFile={env_file}",  # required: no key, no probe
        "-p", "NoNewPrivileges=yes",
        *claude.setenv_args({**(llm_models.environment(spec) if paid else {}), "DISABLE_AUTOUPDATER": "1"}),
        # the key goes to claude by a pipe (kd.secret_fd), not in its environ
        "/usr/bin/python3", "-I", SECRET_FD, f"/home/{user}/.local/bin/claude", "-p", PROMPT, "--model", model, "--output-format", "json",
        "--max-turns", "1", *ISOLATION_ARGS, "--no-session-persistence",
    ]


def _result(stdout: str):
    """Claude's ``--output-format json`` result (the last JSON object line of stdout), or None."""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("type") == "result":
            return value
    return None


def _masked(text: str) -> str:
    """One clean line: the gate's masks, then the adapter's; nothing of the text when the gate is unavailable."""
    cleaned = claude._clean(text)
    if not cleaned:
        return ""
    masked = redact.text(cleaned)
    return WITHHELD if masked is None else claude.mask(masked)


def _cut(line: str) -> str:
    return line if len(line) <= DETAIL_MAX else line[: DETAIL_MAX - 1] + "…"


def outcome(rc: int, stdout: str, stderr: str, started: float, ended: float) -> dict:
    """``{ok, model, ms, detail}`` of a finished probe: ok is a clean exit with a successful result; ``model`` the
    names the result's usage lists (what the provider served); ``detail`` the result's text, else what the process
    printed, after ``exit N`` when it failed."""
    result = _result(stdout)
    ok = rc == 0 and result is not None and result.get("is_error") is not True \
        and result.get("subtype", "success") == "success"
    model = ""
    if result is not None:
        raw = result.get("modelUsage")
        names = [n for n in raw if isinstance(n, str)] if isinstance(raw, Mapping) else []
        model = ",".join(names)
        text = result.get("result")
        text = text if isinstance(text, str) and text.strip() else ("" if ok else str(result.get("subtype") or ""))
    else:
        text = "\n".join(part for part in (stdout, stderr) if part.strip())
    detail = ": ".join(part for part in ([f"exit {rc}"] if rc != 0 else []) + [_masked(text)] if part)
    return {"ok": ok, "model": claude._clean(model)[:128], "ms": max(0, int(round((ended - started) * 1000))),
            "detail": _cut(detail)}
