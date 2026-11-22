"""Expected calls of an agent's self channel.

The agent's PreToolUse handler, after its guard passed, writes one record per channel call of
the Bash command: the exact argv, the Claude Code session and tool_use id and an expiry. The
self channel runs a call only after consuming a matching live record. A record is NOT the
owner's consent: consent is checked by executors, Forgejo or the owner's page.

Records live in /run/digipaddock/cap-expect/<agent>/ (the agent's, 0700; hidden from the
sandbox). Consuming renames the record away first, so two services never run one record.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import time
from typing import Callable, Optional

from kd import agents, cap_grants, guard_base, shell_calls

EXPECT_DIR = "/run/digipaddock/cap-expect"
CD_WHY = (
    "cd вместе с другими командами Claude Code отправляет владельцу на согласование: выполни cd отдельным "
    "вызовом или используй абсолютный путь"
)
LITERAL_WHY = (
    "аргументы {command} должны быть буквальными: подстановки ($…, `…`, маски, ~, {{…}}) bash раскроет до "
    "запуска; текст с ними передай в одинарных кавычках"
)
TTL = 300
MAX_RECORDS = 256
MAX_RECORD_BYTES = 512 * 1024
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_RECORD = re.compile(r"([0-9]{1,20})-[0-9a-f]{16}\.json\Z")


def _prune(directory: str, now: float) -> list:
    """Live record names, oldest first; expired ones are removed."""
    live = []
    for name in sorted(os.listdir(directory)):
        match = _RECORD.fullmatch(name)
        if not match:
            continue
        if int(match.group(1)) <= now:
            try:
                os.unlink(os.path.join(directory, name))
            except FileNotFoundError:
                pass
        else:
            live.append(name)
    return live


def record(
    agent: str,
    session_id: str,
    tool_use_id: str,
    calls: list,
    *,
    root: str = EXPECT_DIR,
    now: float,
) -> None:
    """Writes one record per call; raises OSError or ValueError, and then the hook refuses the command."""
    if not _ID.fullmatch(session_id) or not _ID.fullmatch(tool_use_id):
        raise ValueError("session and tool ids must be plain tokens")
    directory = os.path.join(root, agent)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    live = _prune(directory, now)
    if len(live) + len(calls) > MAX_RECORDS:
        raise ValueError("too many pending channel calls")
    expires = int(now + TTL)
    for argv in calls:
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(a, str) for a in argv)
        ):
            raise ValueError("a call is a non-empty argv")
        data = json.dumps(
            {
                "session_id": session_id,
                "tool_use_id": tool_use_id,
                "argv": argv,
                "expires": expires,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        name = f"{expires}-{secrets.token_hex(8)}.json"
        tmp = os.path.join(directory, f".{name}.tmp")
        fd = os.open(
            tmp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        os.rename(tmp, os.path.join(directory, name))


def consume(agent: str, argv: list, *, root: str = EXPECT_DIR, now: float) -> bool:
    """True after atomically taking one live record of exactly this argv."""
    directory = os.path.join(root, agent)
    try:
        names = _prune(directory, now)
    except FileNotFoundError:
        return False
    for name in names:
        path = os.path.join(directory, name)
        try:
            with open(path, "rb") as handle:
                data = json.loads(handle.read(MAX_RECORD_BYTES))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("argv") != argv:
            continue
        taken = f"{path}.taken-{os.getpid()}-{secrets.token_hex(4)}"
        try:
            os.rename(path, taken)  # atomic: only one consumer wins
        except FileNotFoundError:
            continue
        os.unlink(taken)
        return True
    return False


def after_bash(
    agent: str,
    *,
    grants_root: str = cap_grants.GRANTS_DIR,
    root: str = EXPECT_DIR,
    now: Callable[[], float] = time.time,
) -> Callable[[dict], Optional[str]]:
    """The second half of the agent's PreToolUse handler, run after its guard passed the Bash call:
    refuses `cd X && …` and channel calls with non-literal words, records the literal ones."""

    def check(event: dict) -> Optional[str]:
        command = event["tool_input"]["command"]
        if shell_calls.cd_compound(command):
            return CD_WHY
        try:
            names = set(cap_grants.load(agent, "self", grants_root).commands)
        except cap_grants.GrantError:
            names = (
                set()
            )  # channel off: nothing to expect, the channel refuses such a call itself
        try:
            calls = shell_calls.calls(command, names)
        except shell_calls.NotLiteral as exc:
            return LITERAL_WHY.format(command=exc.command)
        if not calls:
            return None
        try:
            record(
                agent,
                str(event.get("session_id") or ""),
                str(event.get("tool_use_id") or ""),
                calls,
                root=root,
                now=now(),
            )
        except (OSError, ValueError) as exc:
            return f"ожидание вызова канала не записано ({exc}) — вызов заблокирован"
        return None

    return check


def standalone_hook(
    *,
    stdin=None,
    stderr=None,
    stdout=None,
    user: Optional[str] = None,
    registry: str = agents.REGISTRY,
    grants_root: str = cap_grants.GRANTS_DIR,
    root: str = EXPECT_DIR,
    now: Callable[[], float] = time.time,
) -> int:
    """PreToolUse (matcher Bash) of an agent without a guard, BOSS: the form checks and expected calls only.
    The agent is the one running under this process's user; anything unclear blocks the call."""
    stderr = stderr if stderr is not None else sys.stderr
    try:
        if user is None:
            import pwd  # Linux only; imported lazily like the rest of kd

            user = pwd.getpwuid(os.getuid()).pw_name
        name = agents.agent_of_user(user, registry)
    except (KeyError, ValueError) as exc:
        name, why = None, str(exc)
    else:
        why = "пользователь процесса — не пользователь агента"
    if name is None:
        print(f"⛔ kd-cap-expect: {why} — вызов заблокирован", file=stderr)
        return 2
    return guard_base.run_hook(
        lambda command, cwd: None,
        stdin=stdin,
        stderr=stderr,
        stdout=stdout,
        after_bash=after_bash(name, grants_root=grants_root, root=root, now=now),
    )
