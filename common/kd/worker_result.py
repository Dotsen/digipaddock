"""Worker results (BOSS spec 5.7, 5.8.6, C06): the record of one job, written by the runner as
root next to the job table, read by the executor (finish), BOSS (kd-worker result) and the
usage report. It binds the result to the job, the registry texts the request recorded, the
base commit and the commit the worker reached; it keeps the specialization's final JSON block
and the usage of the session, never the transcript.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Optional

from kd import naming
from kd.worker_jobs import NO_BLOCK, clean

RESULT_SCHEMA = 1
AUTH_FAILED = "auth_failed"  # kd.session_host.protocol.AUTH_FAILED: Claude refused the credentials
OUTPUT_CAP = 128 * 1024  # characters of result text the runner keeps for the block search: twice a block's limit
STAT_CAP = 4096
VALUE_CAP = 300
STATUSES = ("done", "blocked")
_BLOCK = re.compile(r"```json[ \t]*\n(.*?)\n[ \t]*```", re.S)


def final_block(text: Any) -> Optional[dict[str, Any]]:
    """The last fenced ``json`` block that is an object with a known status; None otherwise."""
    if not isinstance(text, str):
        return None
    for body in reversed(_BLOCK.findall(text)):
        try:
            data = json.loads(body)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("status") in STATUSES:
            return data
    return None


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def usage_of(event: Any) -> dict[str, Any]:
    """Model, tokens, turns and cost of the session from the stream-json result event."""
    event = event if isinstance(event, dict) else {}
    usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
    models = (
        event.get("modelUsage") if isinstance(event.get("modelUsage"), dict) else {}
    )
    cost = event.get("total_cost_usd")
    return {
        "model": next(iter(models), "?") if models else "?",
        "input": _int(usage.get("input_tokens")),
        "output": _int(usage.get("output_tokens")),
        "cache_write": _int(usage.get("cache_creation_input_tokens")),
        "cache_read": _int(usage.get("cache_read_input_tokens")),
        "requests": _int(event.get("num_turns")),
        "cost_usd": float(cost)
        if isinstance(cost, (int, float)) and not isinstance(cost, bool)
        else 0.0,
    }


def build(
    job: Mapping[str, Any],
    facts: Mapping[str, Any],
    outcome: Mapping[str, Any],
    *,
    head: Optional[str],
    stat: str,
    patch: Optional[str],
) -> dict[str, Any]:
    """The result record of ``job``: run facts (base, branch, slot, started, ended), the launch
    outcome (exit, timed_out, result_event, output_bytes, auth_failed, reason), the commit and diff
    reached."""
    event = outcome.get("result_event")
    event = event if isinstance(event, dict) else None
    started, ended = facts.get("started"), facts.get("ended")
    return {
        "schema_version": RESULT_SCHEMA,
        "job": job["id"],
        "card": job["card"],
        "agent": job["agent"],
        "type": job["type"],
        "spec": job["spec"],
        "skills": list(job["skills"]),
        "caller": job["caller"],
        "slot": facts.get("slot"),
        "started": started,
        "ended": ended,
        "duration_s": round(ended - started, 3)
        if isinstance(started, float) and isinstance(ended, float)
        else None,
        "exit": outcome.get("exit"),
        "timed_out": bool(outcome.get("timed_out")),
        "output_bytes": _int(outcome.get("output_bytes")),
        # Claude refused the credentials; a record of before has no such field and reads as False
        "auth_failed": bool(outcome.get("auth_failed")),
        "reason": clean(outcome.get("reason") or "", 500),
        "provenance": {**job["prompt"], "base": facts.get("base"), "commit": head},
        "branch": facts.get("branch"),
        "diff_stat": clean(stat or "", STAT_CAP)
        if "\n" not in (stat or "")
        else _lines(stat, STAT_CAP),
        "patch": patch,
        "result": final_block(event.get("result")) if event else None,
        "is_error": bool(event.get("is_error")) if event else None,
        "subtype": clean(event.get("subtype") or "", 60) if event else None,
        "usage": usage_of(event),
    }


def _lines(text: str, cap: int) -> str:
    return "\n".join(clean(line, cap) for line in text[:cap].splitlines())


def outcome_of(record: Mapping[str, Any]) -> tuple[str, str]:
    """The job outcome and its reason: failed (timeout, the runner before or after the session,
    exit, claude error), no-result (no final block) or done — a block that says blocked is done
    with the reason it gives."""
    if record.get("timed_out"):
        return (
            "failed",
            f"timeout after {int(record.get('duration_s') or 0)} s: the session was killed",
        )
    if record.get(
        "reason"
    ):  # the runner broke before claude or after it (git, the patch): nothing was handed over
        return "failed", record["reason"]
    if record.get("auth_failed"):  # Claude's «success» with is_error after a refused sign-in
        return "failed", AUTH_FAILED
    result = record.get("result")
    if record.get("subtype") is None and record.get("is_error") is None:
        return "failed", f"claude exited {record.get('exit')} without a result event"
    if record.get("is_error"):
        return "failed", f"claude reported {record.get('subtype') or 'an error'}"
    if result is None:
        return "no-result", NO_BLOCK
    if result.get("status") == "blocked":
        note = next((str(v) for k, v in result.items() if k != "status" and v), "")
        return "done", clean("blocked: " + note, 300) if note else "blocked"
    return "done", ""


def _short(value: Any, n: int = 12) -> str:
    return value[:n] if isinstance(value, str) and value else "-"


def summary(record: Mapping[str, Any]) -> str:
    """One sanitized English line for BOSS."""
    state, reason = outcome_of(record)
    prov = record["provenance"]
    text = (
        f"{state} · worker {record['spec']} v{prov.get('version')} job {_short(record['job'], 8)} card {record['card']}"
        f" · base {_short(prov.get('base'))} → commit {_short(prov.get('commit'))}"
        f" · ${record['usage']['cost_usd']:.2f} {record['usage']['model']}"
    )
    if record.get("patch"):
        text += f" · patch {record['patch']}"
    if reason:
        text += f" · {reason}"
    return clean(text, 600)


def comment(record: Mapping[str, Any]) -> str:
    """The Russian card comment: the outcome, the commits reached, the patch, the cost and the
    specialization's own keys."""
    state, reason = outcome_of(record)
    if reason == AUTH_FAILED:  # the remedy in words, as sys.session.auth_failed says it for a session
        reason = (
            "нет входа в Claude: токен недействителен или истёк. Выпустите новый (claude setup-token)"
            f" и положите его на сервер: {naming.command('kd-secret')} put claude-token"
        )
    prov = record["provenance"]
    words = {"done": "готово", "failed": "сбой", "no-result": "без итога"}
    stat = (record.get("diff_stat") or "").strip().splitlines()
    lines = [
        f"Рабочая сессия {record['spec']} v{prov.get('version')} (job {_short(record['job'], 8)}): {words.get(state, state)}"
        f" · база {_short(prov.get('base'))} → коммит {_short(prov.get('commit'))}"
        f" · файлы: {clean(stat[-1].strip(), 120) if stat else 'без изменений'}"
        f" · патч: {record.get('patch') or 'нет'}"
        f" · расход ${record['usage']['cost_usd']:.2f}"
        + (f" · {reason}" if reason else "")
    ]
    result = record.get("result") or {}
    for key, value in result.items():
        if key == "status":
            continue
        text = (
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        )
        lines.append(f"{clean(key, 40)}: {clean(text, VALUE_CAP)}")
    return "\n".join(lines)
