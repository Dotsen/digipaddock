"""The executors' reports: ssh-exec, kd-admin-run and kd-admin-critical post each run to the card's thread as
a cabinet notice ``(level, key, params)`` of the catalog's ``notice.exec.*``; the cabinet renders the text.

The command is masked the way the gate masks output (``agent_ro_gate.redact``) and cut to 500 characters; a server
list is cut to 200. Levels: a refusal by the denylist is ``warn``, everything else ``info``."""
from gate.agent_ro_gate import redact

MAX_COMMAND = 500
MAX_SERVERS = 200


def _command(command: str) -> str:
    try:
        raw = command.encode("utf-8", "surrogateescape")  # argv's non-UTF-8 bytes come as surrogates
    except UnicodeEncodeError:
        raw = command.encode("utf-8", "replace")
    text = redact(raw).decode("utf-8", "replace")
    return text if len(text) <= MAX_COMMAND else text[:MAX_COMMAND] + "…"


def _seconds(duration: float) -> str:
    return f"{duration:.1f}".replace(".", ",")


def _servers(aliases, limit: int = MAX_SERVERS) -> str:
    """Join server names with commas up to limit characters. Summarize the remainder and show a dash for an empty list."""
    aliases = list(aliases)
    text = ", ".join(aliases)
    k = len(aliases)
    while len(text) > limit and k > 0:
        k -= 1
        tail = f"… ещё {len(aliases) - k}"
        text = f"{', '.join(aliases[:k])} {tail}" if k else tail
    return text or "—"


def result(agent: str, server: str, command: str, rc: int, duration: float, timed_out: bool = False) -> tuple:
    params = {"agent": f"@{agent}", "server": server, "duration": _seconds(duration), "command": _command(command)}
    if timed_out:
        return "info", "notice.exec.timeout", params
    if rc:
        return "info", "notice.exec.failed", {**params, "rc": rc}
    return "info", "notice.exec.result", params


def denied(agent: str, server: str, command: str, rule: str) -> tuple:
    return "warn", "notice.exec.denied", {"agent": f"@{agent}", "server": server, "rule": rule,
                                          "command": _command(command)}


def interrupted(agent: str, server: str, command: str, duration: float, reason: str) -> tuple:
    return "info", "notice.exec.interrupted", {"agent": f"@{agent}", "server": server, "reason": reason,
                                               "duration": _seconds(duration), "command": _command(command)}


# --- a group ssh-exec: one notice per group ---

def batch_ok(agent: str, command: str, servers) -> tuple:
    return "info", "notice.exec.batch_ok", {"agent": f"@{agent}", "count": len(servers), "command": _command(command),
                                            "servers": _servers(servers)}


def batch_stopped(agent: str, command: str, done, not_run, failed, why: str) -> tuple:
    """failed — the server the group stopped on (why: "code 1", "timeout", "interrupted, SIGTERM", "executor error:
    OSError"); None — it stopped between servers (why: "time is up", "interrupted, SIGTERM", "the journal cannot be written", …)."""
    params = {"agent": f"@{agent}", "why": why, "command": _command(command), "done": _servers(done),
              "not_run": _servers(not_run)}
    if failed:
        return "info", "notice.exec.batch_failed", {**params, "failed": failed}
    return "info", "notice.exec.batch_stopped", params


def batch_denied(agent: str, command: str, servers, server: str, rule: str) -> tuple:
    return "warn", "notice.exec.batch_denied", {"agent": f"@{agent}", "rule": rule, "server": server,
                                                "command": _command(command), "servers": _servers(servers)}
