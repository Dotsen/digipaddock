"""A capability channel's grants file: what one channel may run.

Generated from the compiled policy by the installer, root:root 0644, read on every call; a
missing file is the owner's switch: the channel is off.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
from dataclasses import dataclass, field
from typing import Mapping

GRANTS_DIR = "/etc/digipaddock/cap"
SCHEMA = 1
MAX_BYTES = 64 * 1024
KEYS = ("self", "exec", "forge", "base", "host", "data")
_USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}\Z")
_NAME = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
# Set by the service for every call; a grants file cannot override them.
FIXED_ENV = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "KD_AGENT",
        "KD_CALLER_UID",
        "KD_CALLER_USER",
        "KD_CALLER_SESSION",
    }
)
_FILE_KEYS = frozenset(
    {"schema_version", "agent", "key", "uid", "run_as", "commands", "programs"}
)
_COMMAND_KEYS = frozenset({"program", "args", "subcommands", "env"})


class GrantError(ValueError):
    """The grants file is missing (channel off) or malformed."""


class Refused(ValueError):
    """The call is outside what the channel grants."""


@dataclass(frozen=True)
class Command:
    name: str
    program: str
    args: tuple
    subcommands: tuple
    env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Grant:
    agent: str
    key: str
    uid: int
    run_as: str
    commands: Mapping[str, Command]
    programs: frozenset


def _absolute(path: object) -> bool:
    return (
        isinstance(path, str)
        and path.startswith("/")
        and "\0" not in path
        and posixpath.normpath(path) == path
    )


def _command(name: str, entry: object) -> Command:
    if not isinstance(entry, dict) or set(entry) != _COMMAND_KEYS:
        raise GrantError(f"command {name}: keys must be {sorted(_COMMAND_KEYS)}")
    if not _absolute(entry["program"]):
        raise GrantError(f"command {name}: program must be an absolute path")
    args, forms, env = entry["args"], entry["subcommands"], entry["env"]
    if not isinstance(args, list) or not all(
        isinstance(a, str) and "\0" not in a for a in args
    ):
        raise GrantError(f"command {name}: args must be strings")
    if not isinstance(forms, list) or not all(
        isinstance(f, str) and f and f == " ".join(f.split()) for f in forms
    ):
        raise GrantError(
            f"command {name}: subcommands must be words separated by single spaces"
        )
    if not isinstance(env, dict) or not all(
        isinstance(k, str)
        and _ENV_NAME.fullmatch(k)
        and k not in FIXED_ENV
        and isinstance(v, str)
        and "\0" not in v
        for k, v in env.items()
    ):
        raise GrantError(
            f"command {name}: env must map new upper-case names to strings"
        )
    return Command(
        name,
        entry["program"],
        tuple(args),
        tuple(tuple(f.split(" ")) for f in forms),
        dict(env),
    )


def parse(data: object, agent: str, key: str) -> Grant:
    if not isinstance(data, dict) or set(data) != _FILE_KEYS:
        raise GrantError(f"grants keys must be {sorted(_FILE_KEYS)}")
    if data["schema_version"] != SCHEMA:
        raise GrantError("unknown grants schema_version")
    if data["agent"] != agent or not _NAME.fullmatch(agent):
        raise GrantError("grants file names another agent")
    if data["key"] != key or key not in KEYS:
        raise GrantError("grants file names another key")
    uid = data["uid"]
    if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
        raise GrantError("uid must be a positive integer")
    run_as = data["run_as"]
    if not isinstance(run_as, str) or not _USER.fullmatch(run_as) or run_as == "root":
        raise GrantError("run_as must be a non-root user name")
    commands, programs = data["commands"], data["programs"]
    if not isinstance(commands, dict) or not all(
        isinstance(n, str) and _NAME.fullmatch(n) for n in commands
    ):
        raise GrantError("commands must be keyed by command names")
    if not isinstance(programs, list) or not all(_absolute(p) for p in programs):
        raise GrantError("programs must be absolute paths")
    if (key == "self") != bool(commands) or (key == "self") == bool(programs):
        raise GrantError(
            "a self channel lists commands, an executor channel lists programs"
        )
    return Grant(
        agent,
        key,
        uid,
        run_as,
        {n: _command(n, e) for n, e in sorted(commands.items())},
        frozenset(programs),
    )


def load(agent: str, key: str, root: str = GRANTS_DIR) -> Grant:
    path = os.path.join(root, agent, f"{key}.json")
    try:
        fd = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
    except FileNotFoundError as exc:
        raise GrantError(f"channel {agent}/{key} is switched off (no {path})") from exc
    except OSError as exc:
        raise GrantError(
            f"grants file {path} cannot be opened: {exc.__class__.__name__}"
        ) from exc
    with os.fdopen(fd, "rb") as handle:
        if os.name == "posix":  # the installer's file: root's, or the service's own; nobody else writes it
            st = os.fstat(handle.fileno())
            if st.st_uid not in (0, os.geteuid()) or st.st_mode & 0o022:
                raise GrantError(f"grants file {path} is not root's or is writable by others")
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise GrantError("grants file is too large")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise GrantError("grants file is not JSON") from exc
    return parse(data, agent, key)


def command_for(grant: Grant, name: str, argv: list) -> Command:
    """The granted command for a self-channel call, or Refused."""
    command = grant.commands.get(name)
    if command is None:
        raise Refused(f"{name} is not a command of this channel")
    if command.subcommands and not any(
        tuple(argv[: len(f)]) == f for f in command.subcommands
    ):
        allowed = ", ".join(" ".join(f) for f in command.subcommands)
        raise Refused(f"{name}: subcommand not allowed here (allowed: {allowed})")
    return command


def program_for(grant: Grant, program: str) -> str:
    """The granted executor program, or Refused."""
    if program not in grant.programs:
        raise Refused(f"{program} is not a program of this channel")
    return program


def render(agent: str, uid: int, channel: Mapping) -> bytes:
    """The grants file of one compiled policy channel; refuses what load() would refuse."""
    data = {
        "schema_version": SCHEMA,
        "agent": agent,
        "key": channel["key"],
        "uid": uid,
        "run_as": channel["run_as"],
        "commands": channel["commands"],
        "programs": channel["programs"],
    }
    parse(json.loads(json.dumps(data)), agent, channel["key"])
    return (
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
