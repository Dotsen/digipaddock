"""kd-data: one read-only SQL statement on a product database of an enabled server.

    kd-data <database> "<SQL>"

The executor runs as the data user (``naming.service_user("data")``), the one platform user with the data key,
behind the agent's ``data`` capability channel; the channel names the agent (``KD_AGENT``). The server is the one
whose inventory entry lists the database in ``data_databases``; it must be enabled (its gate installed). Before SSH
the executor checks the exact remote command with the gate's own rules (one SELECT, SHOW, EXPLAIN or DESCRIBE, no
comments, no second statement, no client commands), so a refusal costs no connection and says why. On the server
the key's ``authorized_keys`` line forces ``agent-ro-gate --data``: mysql only, as the MySQL user without the
secret columns, 30 s per statement, 1 MB of masked output. Every call — refused or run — is a line in the journal
(agent, time, server, database, text, exit code, bytes); no journal, no call.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict

_SYSADMIN = str(Path(__file__).resolve().parents[2] / "agents" / "sysadmin")
if _SYSADMIN not in sys.path:
    sys.path.insert(0, _SYSADMIN)
from gate import agent_ro_gate as gate  # noqa: E402 — the server's own rules, one copy

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_DENIED = 0, 1, 2, 3
INVENTORY = "/opt/digipaddock/agents/sysadmin/inventory.json"
ENABLED = "/etc/digipaddock/servers.enabled"
JOURNAL = "/var/log/digipaddock-data/data.jsonl"
KEY_FILE = ".ssh/agent_data_ed25519"
KNOWN_HOSTS_FILE = ".ssh/known_hosts"
TARGET_USER = "agent-ro"
SSH = "/usr/bin/ssh"
TIMEOUT = (
    gate.DATA_TIMEOUT + 25
)  # the gate stops the statement first; this only bounds a hung connection
LIMIT = gate.LIMIT + 4096  # the gate's 1 MB and its own marks
AGENT_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
ALIAS_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}\Z")
DB_RE = re.compile(r"[A-Za-z0-9_]{1,64}\Z")
HOST_RE = re.compile(r"[0-9]{1,3}(?:\.[0-9]{1,3}){3}\Z")
SHELL_CHARS = "`$|&\n\r"
SHELL_NAMES = {
    "`": "backquotes",
    "$": "$",
    "|": "|",
    "&": "&",
    "\n": "line breaks",
    "\r": "line breaks",
}
USAGE = 'usage: kd-data <database> "<SQL>" — one SELECT, SHOW, EXPLAIN or DESCRIBE\n'


class Refused(Exception):
    """The call is not run; the text says why, the code is the exit code."""

    def __init__(self, code: int, text: str):
        super().__init__(text)
        self.code = code


def databases(inventory_path: str = INVENTORY) -> Dict[str, tuple]:
    """{database: (alias, host)} from the servers' ``data_databases``; a broken entry or a database on two servers
    is an error, never a guess."""
    try:
        servers = json.loads(Path(inventory_path).read_text(encoding="utf-8"))[
            "servers"
        ]
        if not isinstance(servers, dict):
            raise ValueError("servers is not an object")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise Refused(
            EXIT_ERROR,
            f"kd-data: the inventory is unreadable ({exc.__class__.__name__})\n",
        ) from None
    found: Dict[str, tuple] = {}
    for alias, entry in servers.items():
        names = entry.get("data_databases", []) if isinstance(entry, dict) else None
        if names == []:
            continue
        host = entry.get("host") if isinstance(entry, dict) else None
        if (
            not isinstance(names, list)
            or not ALIAS_RE.fullmatch(str(alias))
            or not isinstance(host, str)
            or not HOST_RE.fullmatch(host)
        ):
            raise Refused(
                EXIT_ERROR, f"kd-data: the inventory entry of {alias} is broken\n"
            )
        for name in names:
            if not isinstance(name, str) or not DB_RE.fullmatch(name) or name in found:
                raise Refused(
                    EXIT_ERROR,
                    f"kd-data: the inventory lists database {name!r} of {alias} badly or twice\n",
                )
            found[name] = (alias, host)
    return found


def enabled(path: str = ENABLED) -> set:
    try:
        return {
            line.strip()
            for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
    except FileNotFoundError:
        return set()
    except (OSError, ValueError) as exc:
        raise Refused(
            EXIT_ERROR,
            f"kd-data: the list of connected servers is unreadable ({exc.__class__.__name__})\n",
        )


def remote_command(database: str, sql: str) -> str:
    """What the server's gate receives as SSH_ORIGINAL_COMMAND."""
    return f"mysql -D {database} -e {shlex.quote(sql)}"


def check(database: str, sql: str) -> str:
    """The remote command, checked by the gate's own rules; Refused with the gate's reason otherwise."""
    if not sql.strip():
        raise Refused(EXIT_USAGE, USAGE)
    shell = sorted({c for c in sql if c in SHELL_CHARS}, key=SHELL_CHARS.index)
    if shell:  # the gate refuses them as shell characters; say it in the SQL's terms
        names = ", ".join(dict.fromkeys(SHELL_NAMES[c] for c in shell))
        raise Refused(
            EXIT_DENIED,
            f"kd-data: refused: the statement is one line without {names} "
            "(the server's gate refuses them); name identifiers without backquotes\n",
        )
    command = remote_command(database, sql)
    try:
        gate.check_data(command)
    except gate.Deny as exc:
        raise Refused(EXIT_DENIED, f"kd-data: refused: {exc}\n") from None
    return command


def ssh_argv(home: str, host: str, remote: str) -> list:
    """ssh with nothing of anyone else's: this key only, the pinned host key, no agent, no prompt."""
    return [
        SSH,
        "-F",
        "/dev/null",
        "-i",
        f"{home}/{KEY_FILE}",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={home}/{KNOWN_HOSTS_FILE}",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "UpdateHostKeys=no",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "ServerAliveInterval=30",
        "-T",
        f"{TARGET_USER}@{host}",
        "--",
        remote,
    ]


def journal(path: str, **record) -> None:
    record = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **record}
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
    with os.fdopen(fd, "a", encoding="utf-8", errors="backslashreplace") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def main(
    argv: list,
    *,
    agent: str,
    home: str,
    inventory_path: str = INVENTORY,
    enabled_path: str = ENABLED,
    journal_path: str = JOURNAL,
    runner: Callable = gate.run_limited,
    out=None,
    err=None,
    clock: Callable = time.monotonic,
) -> int:
    out = out if out is not None else sys.stdout.buffer
    err = err if err is not None else sys.stderr
    if not AGENT_RE.fullmatch(agent or ""):
        err.write(
            "kd-data: no calling agent (run it as yourself, not through another channel)\n"
        )
        return EXIT_DENIED
    database, sql, alias = (argv + ["", ""])[0], (argv + ["", ""])[1], None
    try:
        if len(argv) != 2:
            raise Refused(EXIT_USAGE, USAGE)
        known = databases(inventory_path)
        if database not in known:
            names = ", ".join(sorted(known)) or "none"
            raise Refused(
                EXIT_DENIED,
                f"kd-data: unknown database {database[:64]!r}; readable: {names}\n",
            )
        alias, host = known[database]
        if alias not in enabled(enabled_path):
            raise Refused(
                EXIT_DENIED,
                f"kd-data: server {alias} is not connected yet (no gate on it)\n",
            )
        command = check(database, sql)
    except Refused as exc:
        try:  # a refused call is a call: it is journaled too
            journal(
                journal_path,
                event="refused",
                agent=agent,
                server=alias,
                database=database[:64],
                sql=sql[:4096],
                rc=exc.code,
                reason=str(exc).strip()[:500],
            )
        except OSError as journal_exc:
            err.write(
                f"kd-data: the journal is not writable ({journal_exc.__class__.__name__}); nothing ran\n"
            )
            return EXIT_ERROR
        err.write(str(exc))
        return exc.code

    run_id = uuid.uuid4().hex[:8]
    try:
        journal(
            journal_path,
            event="start",
            id=run_id,
            agent=agent,
            server=alias,
            database=database,
            sql=sql,
        )
    except OSError as exc:
        err.write(
            f"kd-data: the journal is not writable ({exc.__class__.__name__}); nothing ran\n"
        )
        return EXIT_ERROR
    started = clock()
    result = runner(ssh_argv(home, host, command), timeout=TIMEOUT, limit=LIMIT)
    rc = 124 if result.timed_out else result.returncode
    try:
        journal(
            journal_path,
            event="result",
            id=run_id,
            agent=agent,
            server=alias,
            database=database,
            rc=rc,
            bytes=len(result.output),
            duration_s=round(clock() - started, 1),
            truncated=result.truncated,
            timed_out=result.timed_out,
        )
    except OSError as exc:
        err.write(f"kd-data: the result is not journaled ({exc.__class__.__name__})\n")
        rc = rc or EXIT_ERROR
    out.write(result.output)
    if result.truncated:
        out.write(f"\n[kd-data: output cut at {LIMIT // 1024} KB]\n".encode("utf-8"))
    if result.timed_out:
        out.write(f"\n[kd-data: stopped after {TIMEOUT} s]\n".encode("utf-8"))
    out.flush()
    return rc
