"""Client of a capability channel.

Linked in /etc/digipaddock/agent-bin/<agent>/ under each self-channel command name; wrappers
call ``kd-cap run <key> <program> …`` to reach their executor. The agent is found by the caller's
user in the host's agent registry; the call carries this process's stdin, stdout and stderr.

on a host whose commands or cards are not the repository's ``kd-``/``KD-``, what the
executor prints for the agent to read (a pipe, not a file it redirected into) passes through
this client with the host's names; data — a checkout's files, a server's output, the host's
journal — goes on as it is.
"""

from __future__ import annotations

import os
import select
import signal
import socket
import stat
import sys
import threading

from kd import agents, cap_proto, naming

CAP_DIR = "/run/digipaddock/cap"
EXEC_DIR = "/run/digipaddock/cap-exec"
CLIENT = "/opt/digipaddock/common/bin/kd-cap"
EXEC_KEYS = ("exec", "forge", "base", "host", "data")
REFUSED = 126
RAW_COMMANDS = ("kd-repo", "ssh-ro")  # their stdout is data: a checkout's files, a server's output
RAW_KEYS = ("exec", "host", "data")  # the servers' output, the host's journal and files, the databases' rows
CHUNK = 65536
SOON = 4096  # a part without its newline goes on once it is this long ...
TAIL = 256  # ... but its last bytes, which may begin a name
IDLE = 0.5  # ... or once the writer has been quiet this long (a prompt waits for its answer)
DRAIN = 5.0  # after the reply, at most: a child the executor left behind may keep writing into the pipe


def agent_of(uid: int, registry: str) -> str:
    import pwd  # Linux only; imported lazily like the rest of kd

    user = pwd.getpwuid(uid).pw_name
    name = agents.agent_of_user(user, registry)
    if name is None:
        raise LookupError(f"user {user} is not the user of an agent on this host")
    return name


def host_names() -> bool:
    """This host names commands or cards otherwise than the repository."""
    return naming.COMMANDS != naming.CANON_COMMANDS or naming.CARDS != naming.CANON_CARDS


def _read_by_agent(fd: int) -> bool:
    try:
        return not stat.S_ISREG(os.fstat(fd).st_mode)
    except OSError:
        return False


def _write(fd: int, data: bytes) -> bool:
    try:
        while data:
            data = data[os.write(fd, data):]
    except OSError:
        return False
    return True


def pump(read_fd: int, out_fd: int, done: threading.Event | None = None) -> None:
    """``read_fd`` to ``out_fd`` until EOF, a line at a time, with the host's names; a part without its newline
    goes on past SOON bytes or after IDLE seconds of quiet. After ``done`` (the reply came) a quiet pipe ends it:
    only a child the executor left behind still holds it. A reader that went away is no reason to stop reading
    (the executor must not block on a full pipe)."""
    buf, open_ = b"", True
    with os.fdopen(read_fd, "rb", buffering=0) as src:
        while True:
            readable, _, _ = select.select([src], [], [], IDLE)
            chunk = src.read(CHUNK) if readable else None
            if chunk:
                buf += chunk
                cut = buf.rfind(b"\n") + 1
                if len(buf) - cut > SOON:
                    cut = len(buf) - TAIL
            else:
                cut = len(buf)  # the end, or the writer is quiet: all of it
            ready, buf = buf[:cut], buf[cut:]
            if ready and open_:
                text = naming.rewrite(ready.decode("utf-8", "surrogateescape"))
                open_ = _write(out_fd, text.encode("utf-8", "surrogateescape"))
            if chunk == b"" or (chunk is None and done is not None and done.is_set()):
                return


def _relays(key: str, name: str, fds: list) -> list:
    """Pipes in place of the agent's stdout and stderr where the host's names apply: (read end, write end)."""
    if not host_names():
        return []
    raw = key in RAW_KEYS or (key == "self" and name in {naming.command(c) for c in RAW_COMMANDS})
    relays = []
    for out in (2,) if raw else (1, 2):
        if _read_by_agent(fds[out]):
            read_end, write_end = os.pipe()
            relays.append((read_end, write_end, fds[out]))
            fds[out] = write_end
    return relays


def main(
    argv0: str,
    args: list,
    *,
    uid: int,
    cwd: str | None = None,
    registry: str = agents.REGISTRY,
) -> int:
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    if cwd is None:
        try:
            cwd = os.getcwd()
        except OSError as exc:
            print(
                f"kd-cap: current directory is unreachable: {exc.strerror or exc}",
                file=sys.stderr,
            )
            return REFUSED
    name = os.path.basename(argv0)
    if name == "kd-cap":
        if (
            len(args) < 3
            or args[0] != "run"
            or args[1] not in EXEC_KEYS
            or not args[2].startswith("/")
        ):
            print(
                "usage: kd-cap run <exec|forge|base|host|data> </absolute/program> [args...]",
                file=sys.stderr,
            )
            return 2
        key, request = (
            args[1],
            {"v": 1, "program": args[2], "argv": list(args[3:]), "cwd": cwd},
        )
        session = os.environ.get("KD_CALLER_SESSION", "")  # set by the self channel this wrapper runs under
        if cap_proto.SESSION_RE.fullmatch(session):
            request["session"] = session
    else:
        key, request = "self", {"v": 1, "command": name, "argv": list(args), "cwd": cwd}
    try:
        agent = agent_of(uid, registry)
    except (LookupError, KeyError, ValueError) as exc:
        print(f"kd-cap: {exc}", file=sys.stderr)
        return REFUSED
    path = os.path.join(CAP_DIR if key == "self" else EXEC_DIR, agent, f"{key}.sock")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(path)
    except OSError as exc:
        print(
            f"kd-cap: channel {agent}/{key} is off: {exc.strerror or exc}",
            file=sys.stderr,
        )
        return REFUSED
    fds = [0, 1, 2]
    relays = _relays(key, name, fds)
    done = threading.Event()
    threads = [threading.Thread(target=pump, args=(r, out, done), daemon=True) for r, _, out in relays]
    for thread in threads:
        thread.start()
    reply = failure = None
    try:
        cap_proto.send_request(sock, request, fds)
        for _, write_end, _ in relays:  # the executor holds its copies; EOF comes when it is done
            os.close(write_end)
        relays = []
        reply = cap_proto.recv_reply(sock)
    except (OSError, cap_proto.ProtocolError) as exc:
        failure = exc
    finally:
        sock.close()
        for _, write_end, _ in relays:
            os.close(write_end)
        done.set()
        for thread in threads:
            thread.join(DRAIN)
    if failure is not None:
        print(f"kd-cap: channel {agent}/{key} failed: {failure}", file=sys.stderr)
        return REFUSED
    if "refused" in reply:
        print(f"kd-cap: refused: {reply['refused']}", file=sys.stderr)
        return REFUSED
    if "signal" in reply:
        return 128 + int(reply["signal"])
    return int(reply.get("exit", 1))
