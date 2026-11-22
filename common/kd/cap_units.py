"""systemd units of an agent's capability channels.

One socket per channel, group-only to the agent's user, and a per-connection service that runs
kd-cap-serve as the channel's user; tmpfiles.d creates the socket and expectation directories.
"""

from __future__ import annotations

import posixpath
import re

from kd.cap_client import CAP_DIR, EXEC_DIR
from kd.cap_expect import EXPECT_DIR
from kd.cap_grants import GRANTS_DIR, KEYS

TARGET = "digipaddock-cap.target"
AGENT_BIN = "/etc/digipaddock/agent-bin"
UNIT_DIR = "/etc/systemd/system"
TARGET_PATH = f"{UNIT_DIR}/{TARGET}"
TARGET_SOURCE = "common/systemd/digipaddock-cap.target"  # in the repository
TMPFILES_DIR = "/etc/tmpfiles.d"
RUN_ROOT = posixpath.dirname(CAP_DIR)
_NAME = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
_USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}\Z")


def _check(agent: str, key: str, user: str) -> None:
    if not _NAME.fullmatch(agent):
        raise ValueError(f"unsafe agent name: {agent!r}")
    if key not in KEYS:
        raise ValueError(f"unknown channel key: {key!r}")
    if not _USER.fullmatch(user) or user == "root":
        raise ValueError(f"unsafe channel user: {user!r}")


def socket_path(agent: str, key: str) -> str:
    if not _NAME.fullmatch(agent) or key not in KEYS:
        raise ValueError(f"unsafe channel: {agent!r}/{key!r}")
    return posixpath.join(CAP_DIR if key == "self" else EXEC_DIR, agent, f"{key}.sock")


def socket_name(agent: str, key: str) -> str:
    return f"digipaddock-cap-{agent}-{key}.socket"


def socket_unit(agent: str, key: str, agent_user: str) -> tuple[str, bytes]:
    _check(agent, key, agent_user)
    text = f"""[Unit]
Description=DigiPaddock capability channel {agent}/{key} (KD-194)
PartOf={TARGET}

[Socket]
ListenStream={socket_path(agent, key)}
Accept=yes
SocketUser=root
SocketGroup={agent_user}
SocketMode=0660
MaxConnections=16
RemoveOnStop=yes

[Install]
WantedBy={TARGET}
"""
    return socket_name(agent, key), text.encode()


# One connection is one unit instance (Accept=yes), so these bound one call with everything it starts: a kd-e2e run's
# bubblewrap, node, Chromium and the cabinet under test, their tmpfs pages included (charged to the writer's cgroup) —
# no call can take the host's memory or tasks (review 04.11.2026). Board and forge calls use a small part of this.
MEMORY_MAX = "3G"
TASKS_MAX = 1024


def service_unit(agent: str, key: str, run_as: str, code: str) -> tuple[str, bytes]:
    _check(agent, key, run_as)
    if (
        not code.startswith("/")
        or posixpath.normpath(code) != code
        or any(c.isspace() for c in code)
    ):
        raise ValueError(f"unsafe code path: {code!r}")
    text = f"""[Unit]
Description=DigiPaddock capability call {agent}/{key} (KD-194)
PartOf={TARGET}

[Service]
User={run_as}
ExecStart=/usr/bin/python3 -I {code}/common/bin/kd-cap-serve {agent} {key}
StandardInput=socket
StandardOutput=journal
StandardError=journal
SyslogIdentifier=digipaddock-cap
NoNewPrivileges=yes
KillMode=control-group
RuntimeMaxSec=1h
MemoryMax={MEMORY_MAX}
MemorySwapMax=0
TasksMax={TASKS_MAX}
SuccessExitStatus=126
"""
    return f"digipaddock-cap-{agent}-{key}@.service", text.encode()


def tmpfiles(agent: str, agent_user: str) -> tuple[str, bytes]:
    _check(agent, "self", agent_user)
    lines = [
        f"d {RUN_ROOT} 0755 root root -",
        f"d {CAP_DIR} 0755 root root -",
        f"d {CAP_DIR}/{agent} 0750 root {agent_user} -",
        f"d {EXEC_DIR} 0755 root root -",
        f"d {EXEC_DIR}/{agent} 0750 root {agent_user} -",
        f"d {EXPECT_DIR} 0711 root root -",
        f"d {EXPECT_DIR}/{agent} 0700 {agent_user} {agent_user} -",
    ]
    return f"{TMPFILES_DIR}/digipaddock-cap-{agent}.conf", ("\n".join(lines) + "\n").encode()


def grants_path(agent: str, key: str) -> str:
    socket_path(agent, key)  # checks the names
    return posixpath.join(GRANTS_DIR, agent, f"{key}.json")


def client(code: str) -> str:
    return f"{code}/common/bin/kd-cap"


def unit_files(
    agent: str, agent_user: str, code: str, channels: list
) -> list[tuple[str, bytes]]:
    """The agent's socket and service units, then its tmpfiles.d file: (host path, content)."""
    files = []
    for channel in sorted(channels, key=lambda c: c["key"]):
        for name, text in (
            socket_unit(agent, channel["key"], agent_user),
            service_unit(agent, channel["key"], channel["run_as"], code),
        ):
            files.append((f"{UNIT_DIR}/{name}", text))
    files.append(tmpfiles(agent, agent_user))
    return files


def links(agent: str, code: str, channels: list) -> list[tuple[str, str]]:
    """agent-bin links of the self channel's commands to the client: (link, target)."""
    socket_path(agent, "self")
    return [
        (posixpath.join(AGENT_BIN, agent, command), client(code))
        for channel in channels
        if channel["key"] == "self"
        for command in sorted(channel["commands"])
    ]
