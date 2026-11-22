"""A worker job on the session host: its unit's command line and the line that ends it.

The host runs the root runner (``kd-worker-run --slot <agent>/workerN``, ``kd.worker_runner``) as a transient unit
in the sessions' slice and writes the job's definition to its stdin. The runner prepares the slot, runs the
specialization's Claude session under the agent's user and prints exactly one line on stdout, its end: the
outcome, the reason, the card's comment, the usage and the result. Nothing of the definition reaches the command
line but the checked slot.
"""

import json
import re
from typing import Optional

from kd import worker_board
from kd.cabinet import usage
from kd.session_host import claude

PYTHON = "/usr/bin/python3"
RUNNER = "/opt/digipaddock/common/bin/kd-worker-run"
UNIT = re.compile(r"digipaddock-session-job-[0-9a-f]{12}")
SLOT = re.compile(r"[a-z][a-z0-9_-]{0,31}/worker[1-9]")
TIMEOUT = (60, 14400)
GRACE_S = 600  # past the job's own timeout before systemd stops the unit (the runner stops claude first)
LINE_MAX = 128 * 1024  # the end line: the final block (RESULT_MAX), the comment, the usage
OUTCOMES = ("done", "failed", "no-result")
REASON_MAX = 500
RESULT_MAX = 64 * 1024
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def timeout_ok(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and TIMEOUT[0] <= value <= TIMEOUT[1]


def argv(unit: str, slot_ref: str, timeout_s: int) -> list:
    if not UNIT.fullmatch(unit or ""):
        raise ValueError(f"bad job unit {unit!r}")
    if not SLOT.fullmatch(slot_ref or ""):
        raise ValueError(f"bad worker slot {slot_ref!r}")
    if not timeout_ok(timeout_s):
        raise ValueError(f"bad job timeout {timeout_s!r}")
    return [
        "systemd-run", f"--unit={unit}", f"--slice={claude.SLICE}", "--pipe", "--wait", "--quiet", "--collect",
        "--service-type=exec", "-p", "KillMode=control-group", "-p", "TimeoutStopSec=20",
        "-p", "NoNewPrivileges=yes",  # the root runner drops to the agent: nothing below may regain privileges
        "-p", f"RuntimeMaxSec={timeout_s + GRACE_S}", PYTHON, "-I", RUNNER, "--slot", slot_ref,
    ]


def end_line(raw: bytes) -> Optional[dict]:
    """The runner's end, or None for any other line. A usage of another shape is dropped, the outcome kept."""
    try:
        item = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(item, dict) or item.get("type") != "job" or item.get("phase") != "ended":
        return None
    state, reason, comment = item.get("state"), item.get("reason", ""), item.get("comment", "")
    result, used = item.get("result"), item.get("usage")
    if state not in OUTCOMES or not isinstance(reason, str) or len(reason) > REASON_MAX or _CTRL.search(reason):
        return None
    if not isinstance(comment, str) or len(comment) > worker_board.TEXT_MAX:
        return None
    if result is not None and (not isinstance(result, dict)
                               or len(json.dumps(result, ensure_ascii=False).encode()) > RESULT_MAX):
        return None
    try:
        used = usage.check(used) if used is not None else None
    except ValueError:
        used = None
    return {"state": state, "reason": reason, "comment": comment, "usage": used, "result": result}
