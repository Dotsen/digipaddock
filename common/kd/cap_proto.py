"""Wire format of a capability channel.

One connection is one call. A frame is a 4-byte big-endian length and a JSON object. The
client's request carries exactly three file descriptors (stdin, stdout, stderr) with the
first byte of its frame; SOCK_STREAM keeps no message boundaries, so both sides read whole
frames by length and never trust one recv.
"""

from __future__ import annotations

import json
import os
import re
import socket
import stat
import struct
from typing import Sequence

MAX_REQUEST = 256 * 1024
MAX_REPLY = 64 * 1024
MAX_ARGS = 1000
MAX_CWD = 4096
_HEADER = struct.Struct("!I")
_SELF_KEYS = frozenset({"v", "command", "argv", "cwd"})
_EXEC_KEYS = frozenset({"v", "program", "argv", "cwd"})
# A call into an executor channel may name the session it serves: kd-cap forwards the KD_CALLER_SESSION its self
# channel set; the server takes it only from the agent's own self-channel unit (cap_serve, review 04.11.2026).
SESSION_RE = re.compile(r"[0-9a-f]{12}")


class ProtocolError(ValueError):
    """A malformed or oversized frame; the connection is dropped."""


def _frame(obj: dict) -> bytes:
    body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _HEADER.pack(len(body)) + body


def _read_exact(sock: socket.socket, size: int, have: bytes = b"") -> bytes:
    data = bytearray(have)
    while len(data) < size:
        chunk = sock.recv(min(65536, size - len(data)))
        if not chunk:
            raise ProtocolError("connection closed inside a frame")
        data += chunk
    return bytes(data)


def _decode(body: bytes) -> dict:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError("frame is not JSON") from exc
    if not isinstance(value, dict):
        raise ProtocolError("frame is not a JSON object")
    return value


def send_request(sock: socket.socket, request: dict, fds: Sequence[int]) -> None:
    frame = _frame(request)
    if len(frame) - _HEADER.size > MAX_REQUEST:
        raise ProtocolError("request too large")
    sent = socket.send_fds(sock, [frame], list(fds))
    if sent < len(frame):
        sock.sendall(frame[sent:])  # the rest without the descriptors


def recv_request(sock: socket.socket) -> tuple:
    """The request and its three descriptors; on any error the received descriptors are closed."""
    msg, fds, flags, _ = socket.recv_fds(sock, _HEADER.size, 4)
    try:
        if not msg and not fds:
            raise ProtocolError("connection closed before the frame")
        if flags & getattr(socket, "MSG_CTRUNC", 0):
            raise ProtocolError("descriptor list truncated")
        if len(fds) != 3:
            raise ProtocolError(
                f"a request carries exactly three descriptors, not {len(fds)}"
            )
        for fd in fds:
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise ProtocolError("a descriptor is a directory")
        (size,) = _HEADER.unpack(_read_exact(sock, _HEADER.size, msg))
        if size > MAX_REQUEST:
            raise ProtocolError("request too large")
        return _decode(_read_exact(sock, size)), list(fds)
    except BaseException:
        for fd in fds:
            os.close(fd)
        raise


def send_reply(sock: socket.socket, reply: dict) -> None:
    sock.sendall(_frame(reply))


def recv_reply(sock: socket.socket) -> dict:
    (size,) = _HEADER.unpack(_read_exact(sock, _HEADER.size))
    if size > MAX_REPLY:
        raise ProtocolError("reply too large")
    return _decode(_read_exact(sock, size))


def check_request(request: dict, kind: str) -> dict:
    """The request with its shape checked for a ``self`` or an executor (``exec``) channel."""
    keys = _SELF_KEYS if kind == "self" else _EXEC_KEYS
    if set(request) != keys and not (kind != "self" and set(request) == keys | {"session"}):
        raise ProtocolError(f"request keys must be {sorted(keys)}")
    if "session" in request and not (isinstance(request["session"], str) and SESSION_RE.fullmatch(request["session"])):
        raise ProtocolError("session is 12 hex characters of a session unit")
    if request["v"] != 1:
        raise ProtocolError("unknown protocol version")
    argv = request["argv"]
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        raise ProtocolError("argv must be a list of strings")
    if len(argv) > MAX_ARGS:
        raise ProtocolError(f"at most {MAX_ARGS} arguments")
    if any("\0" in a for a in argv):
        raise ProtocolError("an argument holds NUL")
    cwd = request["cwd"]
    if (
        not isinstance(cwd, str)
        or not cwd.startswith("/")
        or "\0" in cwd
        or len(cwd.encode("utf-8")) > MAX_CWD
    ):
        raise ProtocolError("cwd must be an absolute path")
    if kind == "self":
        name = request["command"]
        if (
            not isinstance(name, str)
            or not name
            or "/" in name
            or "\0" in name
            or name in (".", "..")
        ):
            raise ProtocolError("command must be a bare name")
    else:
        program = request["program"]
        if (
            not isinstance(program, str)
            or not program.startswith("/")
            or "\0" in program
        ):
            raise ProtocolError("program must be an absolute path")
    return request
