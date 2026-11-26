"""The units the ci user runs in — a candidate's tests, the dependency builds, the daily vulnerability scan — each in a
pid namespace and a /proc of its own (KD-9473).

Processes of one uid open each other's ``/proc/<pid>/cwd`` and ``/proc/<pid>/root``, and the path resolves inside
the other unit's mount namespace, where its work folder is writable: with ``User=`` two checks at once could plant a
file in each other's dependency build (some of whose steps have the network) or checkout. ``ProtectProc=invisible``
hides other users' processes only, and ``PrivatePIDs=`` needs systemd 257. So a ci unit starts as root, holding only
the capabilities the wrapper needs, and:

- ``unshare --pid --fork --mount-proc`` gives it a pid namespace and a ``/proc`` in which no other unit's process
  exists; ``--kill-child`` takes the namespace down with the unit's main process, and unshare passes the command's
  exit code (or its signal) on, so ``systemd-run --wait`` returns the command's;
- ``setpriv`` drops to the ci user (its own group and supplementary groups, as ``User=`` had), no inheritable
  capabilities, an empty bounding set and no_new_privs, before anything of the unit's command runs; the exec that
  follows leaves it no capability at all;
- ``env --chdir`` enters the working directory as the ci user: root's chdir (``WorkingDirectory=``, ``unshare
  --wd``) would follow a link the candidate left in its checkout, through the host's ``/proc/1/root``, out of the
  unit's read-only mounts;
- ``PrivateIPC=`` gives the unit an IPC namespace of its own (SysV shared memory, semaphores and message queues,
  POSIX message queues) and ``TemporaryFileSystem=`` a ``/dev/shm`` of its own (POSIX shared memory is that
  directory, which no IPC namespace covers and ``ProtectSystem=`` leaves writable): systemd sets both up before
  unshare starts, unshare makes only a pid and a mount namespace (a copy of the unit's mounts) and setpriv none, so
  the command runs in the unit's — two checks at once share no IPC object.

The memory, CPU and time limits are the unit's cgroup's and hold for all of it. The command is its namespace's
init: a signal it has no handler for never reaches it (pip, npm and a plain Python script ignore the stop's SIGTERM),
so the stop's SIGKILL follows after STOP_S, not systemd's 90 s; devcheck_steps.py handles SIGTERM and prints its
tail within it. The tools are util-linux's and coreutils'; the installer and the runners check that they are there.
"""

import os
import re
from typing import Callable, List, Sequence

UNSHARE = "/usr/bin/unshare"
SETPRIV = "/usr/bin/setpriv"
ENV = "/usr/bin/env"
TOOLS = (UNSHARE, SETPRIV, ENV)
# unshare: the namespaces and the mount of /proc; setpriv: the uid, the gid and groups, the bounding set
CAPABILITIES = "CAP_SYS_ADMIN CAP_SETUID CAP_SETGID CAP_SETPCAP"
STOP_S = 15  # from the stop's SIGTERM to its SIGKILL
SHM = "TemporaryFileSystem=/dev/shm:mode=1777,nosuid,nodev"  # as the host's /dev/shm, but the unit's own
# identity, privilege and the namespaces are the wrapper's: a caller's User= would put the unit back in the host's pid
# namespace, its IPC settings in a namespace another unit shares
_WRAPPERS = (
    "PrivateIPC=",
    "IPCNamespacePath=",
    "JoinsNamespaceOf=",
    "TemporaryFileSystem=",
    "User=",
    "Group=",
    "DynamicUser=",
    "SupplementaryGroups=",
    "WorkingDirectory=",
    "CapabilityBoundingSet=",
    "AmbientCapabilities=",
    "NoNewPrivileges=",
    "SecureBits=",
)
_USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}")


def wrapper(user: str, workdir: str = "") -> List[str]:
    """What the unit runs before the command: its own pid namespace, then the ci user, then its directory."""
    return [
        UNSHARE,
        "--pid",
        "--fork",
        "--kill-child",
        "--mount-proc",
        "--",
        SETPRIV,
        f"--reuid={user}",
        f"--regid={user}",
        "--init-groups",
        "--inh-caps=-all",
        "--bounding-set=-all",
        "--no-new-privs",
        "--",
        *((ENV, f"--chdir={workdir}", "--") if workdir else ()),
    ]


def argv(
    unit: str,
    props: Sequence[str],
    command: Sequence[str],
    *,
    user: str,
    env: Sequence[str] = (),
    workdir: str = "",
) -> List[str]:
    """``systemd-run --wait --pipe`` of ``command`` as ``user`` in a pid namespace of its own; ``props`` are the
    unit's sandbox (no identity or privilege of their own), ``env`` its ``KEY=VALUE`` settings."""
    if not _USER.fullmatch(user or "") or user == "root":
        raise ValueError(f"not a service user: {user!r}")
    if (
        not command or "=" in command[0]
    ):  # env would take NAME=VALUE for a setting, not the command
        raise ValueError(f"not a command: {list(command)[:1]!r}")
    for prop in props:
        if prop.startswith(_WRAPPERS):
            raise ValueError(f"the wrapper's property: {prop}")
    out = ["systemd-run", "--unit", unit, "--wait", "--pipe", "--collect", "--quiet"]
    for prop in (
        f"CapabilityBoundingSet={CAPABILITIES}",
        "NoNewPrivileges=yes",
        f"TimeoutStopSec={STOP_S}",
        "PrivateIPC=yes",
        SHM,
        *props,
    ):
        out += ["-p", prop]
    out += [f"--setenv={item}" for item in (f"USER={user}", f"LOGNAME={user}", *env)]
    return out + ["--", *wrapper(user, workdir), *command]


def command_of(unit_argv: Sequence[str]) -> List[str]:
    """The command a ci unit's argv runs, after the wrapper."""
    rest = list(unit_argv[unit_argv.index(SETPRIV) :])
    rest = rest[rest.index("--") + 1 :]
    return rest[rest.index("--") + 1 :] if rest[:1] == [ENV] else rest


def missing(access: Callable = os.access) -> List[str]:
    """The wrapper's tools that are not there: no ci unit can start without them."""
    return [tool for tool in TOOLS if not access(tool, os.X_OK)]
