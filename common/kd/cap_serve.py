"""One call of a capability channel, run by kd-cap-serve under the channel's user.

systemd accepts the connection (Accept=yes) and starts one instance per call with the socket as
fd 0. Every check is repeated per call: the peer's uid, the grants file (absent = switched off),
the command or program, and for the self channel a consumed expected call. Nothing of the
caller's environment is used. The caller's three descriptors become the program's 0, 1, 2; when
the caller goes away, the program's process group is terminated.
"""

from __future__ import annotations

import json
import os
import re
import select
import signal
import socket
import struct
import subprocess
import sys
import time
from typing import Callable, Mapping

from kd import cap_expect, cap_grants, cap_proto, ids

TERM_GRACE = 5.0
REQUEST_TIMEOUT = 10.0
PATH = "/usr/local/bin:/usr/bin:/bin"
_CRED = struct.Struct("3i")
# an agent's session unit on the session host: digipaddock-session-<12 hex of the session>-<run>.service
_SESSION_UNIT = re.compile(r"/digipaddock-session-([0-9a-f]{12})-[0-9]+\.service$")
_SELF_UNIT = re.compile(r"/digipaddock-cap-([a-z][a-z0-9-]*)-self@[^/]*\.service$")


def peer(conn: socket.socket) -> tuple:
    """(pid, uid, gid) of the process on the other end, from SO_PEERCRED."""
    return _CRED.unpack(
        conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _CRED.size)
    )


def session_of_cgroup(text: str) -> str:
    """The session whose unit holds a process, from its ``/proc/<pid>/cgroup``; '' outside a session unit."""
    for line in text.splitlines():
        found = _SESSION_UNIT.search(line.rsplit(":", 1)[-1])
        if found:
            return found.group(1)
    return ""


def self_unit_agent(text: str) -> str:
    """The agent whose self-channel unit holds a process, from its ``/proc/<pid>/cgroup``; '' elsewhere."""
    for line in text.splitlines():
        found = _SELF_UNIT.search(line.rsplit(":", 1)[-1])
        if found:
            return found.group(1)
    return ""


def caller_self_unit(pid: int) -> str:
    try:
        with open(f"/proc/{int(pid)}/cgroup", encoding="ascii", errors="replace") as handle:
            return self_unit_agent(handle.read(4096))
    except (OSError, ValueError):
        return ""


def caller_session(pid: int) -> str:
    """The caller's session: what the kernel says of its process, never what the caller passes."""
    try:
        with open(f"/proc/{int(pid)}/cgroup", encoding="ascii", errors="replace") as handle:
            return session_of_cgroup(handle.read(4096))
    except (OSError, ValueError):
        return ""


def _passwd(uid: int):
    import pwd  # Linux only; imported lazily like the rest of kd

    return pwd.getpwuid(uid)


def service_user() -> str:
    return _passwd(os.getuid()).pw_name


def journal(**fields) -> None:
    """One JSON line per call to stderr; the unit sends it to the journal as kd-cap."""
    sys.stderr.write(json.dumps(fields, ensure_ascii=False, sort_keys=True) + "\n")
    sys.stderr.flush()


def _session(agent, key, pid, request, session_of, self_unit_of) -> str:
    """The caller's session: what the kernel says of its process; for an executor channel called by a wrapper in the
    agent's own self-channel unit (its cgroup says so), the session that self channel saw and the wrapper forwards —
    without it a finish or a help request from another session of the agent counted for any of its turns (review
    04.11.2026)."""
    session = session_of(pid)
    if not session and key != "self" and request.get("session") and self_unit_of(pid) == agent:
        return request["session"]
    return session


def build_env(
    grant: cap_grants.Grant, caller_uid: int, caller_user: str, extra: Mapping[str, str], session: str = ""
) -> dict:
    me = _passwd(os.getuid())
    env = {
        "PATH": PATH,
        "HOME": me.pw_dir,
        "USER": me.pw_name,
        "LOGNAME": me.pw_name,
        "LANG": "C.UTF-8",
        "KD_AGENT": grant.agent,
        "KD_CALLER_UID": str(caller_uid),
        "KD_CALLER_USER": caller_user,
        "KD_CALLER_SESSION": session,
    }
    env.update({k: v for k, v in extra.items() if k not in cap_grants.FIXED_ENV})
    return env


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGTERM to the program's process group, SIGKILL to whatever of it is left after TERM_GRACE."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + TERM_GRACE
    while time.monotonic() < deadline:
        proc.poll()  # reap the leader so an empty group reads as gone
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _wait(proc: subprocess.Popen, conn: socket.socket) -> int:
    """The program's status; the client going away first terminates the program's process group."""
    poller = select.poll()
    poller.register(conn.fileno(), select.POLLIN | select.POLLHUP | select.POLLERR)
    while proc.poll() is None:
        events = poller.poll(200)
        if not events:
            continue
        gone = any(e & (select.POLLHUP | select.POLLERR) for _, e in events)
        if not gone:
            try:
                gone = (
                    conn.recv(4096, socket.MSG_DONTWAIT) == b""
                )  # bytes after the request are dropped
            except BlockingIOError:
                pass
            except OSError:
                gone = True
        if gone:
            _kill_group(proc)
            break
    return proc.wait()


def _reply(conn: socket.socket, reply: dict) -> None:
    try:
        cap_proto.send_reply(conn, reply)
    except OSError:
        pass  # the client is gone; its exit status no longer matters


def _close(fds) -> None:
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


def serve(
    conn: socket.socket,
    agent: str,
    key: str,
    *,
    grants_root: str = cap_grants.GRANTS_DIR,
    expect_root: str = cap_expect.EXPECT_DIR,
    now: Callable[[], float] = time.time,
    log: Callable[..., None] = journal,
    identify: Callable = peer,
    service_user: Callable[[], str] = service_user,
    session_of: Callable[[int], str] = caller_session,
    self_unit_of: Callable[[int], str] = caller_self_unit,
) -> int:
    """Serves one connection; 0 when a program ran, 126 when the call was refused."""
    started = time.monotonic()
    pid, uid, _ = identify(conn)
    fds: list = []
    entry = {"agent": agent, "key": key, "peer_pid": pid}
    try:
        conn.settimeout(REQUEST_TIMEOUT)
        request, fds = cap_proto.recv_request(conn)
        conn.settimeout(None)
        cap_proto.check_request(request, "self" if key == "self" else "exec")
        grant = cap_grants.load(agent, key, grants_root)
        if uid != grant.uid:
            raise cap_grants.Refused(f"peer uid {uid} is not the agent's")
        if service_user() != grant.run_as:
            raise cap_grants.Refused(
                f"the service runs as {service_user()}, the channel as {grant.run_as}"
            )
        argv = request["argv"]
        if key == "self":
            name = request["command"]
            entry["command"] = name
            command = cap_grants.command_for(grant, name, argv)
            if not cap_expect.consume(
                agent, [name, *argv], root=expect_root, now=now()
            ):
                raise cap_grants.Refused(
                    f"{name}: call not expected — run it by name in a Bash command of the session, not from a script"
                )
            program, fixed, extra, cwd = (
                command.program,
                list(command.args),
                command.env,
                request["cwd"],
            )
        else:
            program = cap_grants.program_for(grant, request["program"])
            entry["program"] = program
            fixed, extra, cwd = [], {}, "/"
        if not os.path.isdir(cwd) or not os.access(cwd, os.X_OK):
            raise cap_grants.Refused(f"cwd {cwd} is not reachable")
        argv = [ids.canonical(a) for a in argv]  # after the expectation: a card typed with an earlier prefix
        caller = _passwd(uid).pw_name
        proc = subprocess.Popen(
            [program, *fixed, *argv],
            stdin=fds[0],
            stdout=fds[1],
            stderr=fds[2],
            env=build_env(grant, uid, caller, extra, _session(agent, key, pid, request, session_of, self_unit_of)),
            cwd=cwd,
            close_fds=True,
            start_new_session=True,
        )
    except (
        cap_proto.ProtocolError,
        cap_grants.GrantError,
        cap_grants.Refused,
        OSError,
        KeyError,
    ) as exc:
        _close(fds)
        reason = str(exc) or exc.__class__.__name__
        if isinstance(exc, socket.timeout):
            reason = "no request in time"
        _reply(conn, {"refused": reason})
        log(**entry, refused=reason[:300], ms=int((time.monotonic() - started) * 1000))
        return 126
    _close(fds)
    entry["args"] = len(argv)
    status = _wait(proc, conn)
    reply = {"signal": -status} if status < 0 else {"exit": status}
    _reply(conn, reply)
    log(**entry, **reply, ms=int((time.monotonic() - started) * 1000))
    return 0
