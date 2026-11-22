import json
import os
import socket
import struct
import threading

import pytest

from kd import cap_proto

linux = pytest.mark.skipif(
    not hasattr(socket, "send_fds"), reason="SCM_RIGHTS needs Linux"
)


def _pair():
    return socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)


@linux
def test_request_round_trip_with_three_fds():
    a, b = _pair()
    r, w = os.pipe()
    cap_proto.send_request(
        a,
        {"v": 1, "command": "ssh-ro", "argv": ["node-c", "uptime"], "cwd": "/"},
        [r, w, w],
    )
    request, fds = cap_proto.recv_request(b)
    assert request["argv"] == ["node-c", "uptime"] and len(fds) == 3
    os.write(fds[1], b"x")
    assert os.read(r, 1) == b"x"
    for fd in fds + [r, w]:
        os.close(fd)


@linux
def test_frame_split_into_many_reads_is_reassembled():
    a, b = _pair()
    a.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    request = {"v": 1, "command": "c", "argv": ["x" * 200] * 999, "cwd": "/"}
    sender = threading.Thread(
        target=cap_proto.send_request, args=(a, request, [0, 1, 2])
    )
    sender.start()
    got, fds = cap_proto.recv_request(b)
    sender.join()
    assert got["argv"] == request["argv"]
    for fd in fds:
        os.close(fd)


@linux
def test_wrong_fd_count_is_refused_and_fds_closed():
    a, b = _pair()
    r, w = os.pipe()
    body = json.dumps({"v": 1, "command": "c", "argv": [], "cwd": "/"}).encode()
    socket.send_fds(a, [struct.pack("!I", len(body)) + body], [r])
    with pytest.raises(cap_proto.ProtocolError, match="three"):
        cap_proto.recv_request(b)
    os.close(r)
    os.close(w)


@linux
def test_directory_descriptor_is_refused():
    a, b = _pair()
    d = os.open("/", os.O_RDONLY)
    body = json.dumps({"v": 1, "command": "c", "argv": [], "cwd": "/"}).encode()
    socket.send_fds(a, [struct.pack("!I", len(body)) + body], [0, d, 2])
    with pytest.raises(cap_proto.ProtocolError, match="directory"):
        cap_proto.recv_request(b)
    os.close(d)


@linux
def test_oversized_frame_is_refused_before_reading_body():
    a, b = _pair()
    socket.send_fds(a, [struct.pack("!I", cap_proto.MAX_REQUEST + 1)], [0, 1, 2])
    with pytest.raises(cap_proto.ProtocolError, match="too large"):
        cap_proto.recv_request(b)


@linux
def test_connection_closed_before_frame_is_refused():
    a, b = _pair()
    a.close()
    with pytest.raises(cap_proto.ProtocolError, match="closed"):
        cap_proto.recv_request(b)


@linux
def test_reply_round_trip():
    a, b = _pair()
    cap_proto.send_reply(a, {"exit": 3})
    assert cap_proto.recv_reply(b) == {"exit": 3}


@linux
def test_truncated_reply_is_an_error():
    a, b = _pair()
    a.sendall(struct.pack("!I", 10) + b"{}")
    a.close()
    with pytest.raises(cap_proto.ProtocolError, match="closed"):
        cap_proto.recv_reply(b)


@pytest.mark.parametrize(
    "request_, why",
    [
        ({"v": 2, "command": "c", "argv": [], "cwd": "/"}, "version"),
        ({"v": 1, "command": "c", "argv": ["a\0b"], "cwd": "/"}, "NUL"),
        ({"v": 1, "command": "c", "argv": ["a"] * 1001, "cwd": "/"}, "arguments"),
        ({"v": 1, "command": "c", "argv": "a b", "cwd": "/"}, "list"),
        ({"v": 1, "command": "c", "argv": [], "cwd": "relative"}, "cwd"),
        ({"v": 1, "command": "c", "argv": [], "cwd": "/" + "a" * 4096}, "cwd"),
        ({"v": 1, "command": "c", "argv": [], "cwd": "/", "extra": 1}, "keys"),
        ({"v": 1, "program": "/x", "argv": [], "cwd": "/"}, "keys"),
        ({"v": 1, "command": "../c", "argv": [], "cwd": "/"}, "bare"),
        ({"v": 1, "command": "", "argv": [], "cwd": "/"}, "bare"),
    ],
)
def test_check_request_refusals(request_, why):
    with pytest.raises(cap_proto.ProtocolError, match=why):
        cap_proto.check_request(request_, "self")


def test_check_request_exec_needs_absolute_program():
    with pytest.raises(cap_proto.ProtocolError, match="program"):
        cap_proto.check_request(
            {"v": 1, "program": "rel", "argv": [], "cwd": "/"}, "exec"
        )
    ok = cap_proto.check_request(
        {"v": 1, "program": "/opt/x", "argv": ["a"], "cwd": "/"}, "exec"
    )
    assert ok["program"] == "/opt/x"


def test_an_exec_request_may_name_its_session_and_a_self_request_may_not():
    base = {"v": 1, "program": "/opt/x", "argv": [], "cwd": "/"}
    assert cap_proto.check_request(dict(base, session="0123456789ab"), "exec")["session"] == "0123456789ab"
    for bad in ("0123456789AB", "0123456789a", "x" * 12, 12):
        with pytest.raises(cap_proto.ProtocolError, match="session"):
            cap_proto.check_request(dict(base, session=bad), "exec")
    with pytest.raises(cap_proto.ProtocolError, match="keys"):
        cap_proto.check_request({"v": 1, "command": "c", "argv": [], "cwd": "/", "session": "0123456789ab"}, "self")
