import os
import shutil
import socket
import tempfile
import threading
from pathlib import Path

import pytest

from kd import cap_client, cap_proto, naming

pytestmark = pytest.mark.skipif(not hasattr(socket, "send_fds"), reason="Linux only")


@pytest.fixture
def agentx(monkeypatch):
    # a short directory, not tmp_path: under xdist tmp_path/cap-exec/agentx/exec.sock outgrows AF_UNIX's 108 bytes
    root = Path(tempfile.mkdtemp(prefix="cap"))
    monkeypatch.setattr(cap_client, "agent_of", lambda uid, registry: "agentx")
    monkeypatch.setattr(cap_client, "CAP_DIR", str(root / "cap"))
    monkeypatch.setattr(cap_client, "EXEC_DIR", str(root / "cap-exec"))
    yield root
    shutil.rmtree(root, ignore_errors=True)


def _server(path, reply, seen, out=b""):
    path.parent.mkdir(parents=True, exist_ok=True)
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(str(path))
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        request, fds = cap_proto.recv_request(conn)
        seen.append(request)
        if out:
            os.write(fds[1], out)
        for fd in fds:
            os.close(fd)
        cap_proto.send_reply(conn, reply)
        conn.close()

    threading.Thread(target=run, daemon=True).start()


def test_self_call_by_command_name(agentx):
    seen = []
    _server(agentx / "cap" / "agentx" / "self.sock", {"exit": 4}, seen)
    rc = cap_client.main(
        "/etc/digipaddock/agent-bin/agentx/ssh-ro",
        ["node-c", "uptime"],
        uid=os.getuid(),
        cwd="/srv",
    )
    assert rc == 4 and seen == [
        {"v": 1, "command": "ssh-ro", "argv": ["node-c", "uptime"], "cwd": "/srv"}
    ]


def test_run_reaches_executor_channel(agentx, monkeypatch):
    monkeypatch.delenv("KD_CALLER_SESSION", raising=False)
    seen = []
    _server(agentx / "cap-exec" / "agentx" / "exec.sock", {"signal": 15}, seen)
    rc = cap_client.main(
        "kd-cap",
        ["run", "exec", "/opt/x/ssh_exec.py", "--agent", "agentx"],
        uid=os.getuid(),
        cwd="/",
    )
    assert rc == 143 and seen == [
        {
            "v": 1,
            "program": "/opt/x/ssh_exec.py",
            "argv": ["--agent", "agentx"],
            "cwd": "/",
        }
    ]


def test_run_forwards_the_self_channels_session(agentx, monkeypatch):
    """Review 04.11.2026 (L2): a wrapper running under the agent's self channel passes on the session it was given;
    anything else in the variable is not sent."""
    for value, sent in (("0123456789ab", {"session": "0123456789ab"}), ("../x", {}), ("", {})):
        monkeypatch.setenv("KD_CALLER_SESSION", value)
        seen = []
        _server(agentx / "cap-exec" / "agentx" / "exec.sock", {"exit": 0}, seen)
        assert cap_client.main("kd-cap", ["run", "exec", "/opt/x/p"], uid=os.getuid(), cwd="/") == 0
        assert seen == [{"v": 1, "program": "/opt/x/p", "argv": [], "cwd": "/", **sent}]
        (agentx / "cap-exec" / "agentx" / "exec.sock").unlink()


def test_missing_socket_reports_switched_off(agentx, capsys):
    rc = cap_client.main("ssh-ro", ["node-c"], uid=os.getuid(), cwd="/")
    assert rc == 126 and "channel agentx/self is off" in capsys.readouterr().err


def test_refusal_is_printed(agentx, capsys):
    _server(
        agentx / "cap" / "agentx" / "self.sock", {"refused": "call not expected"}, []
    )
    assert cap_client.main("ssh-ro", [], uid=os.getuid(), cwd="/") == 126
    assert "kd-cap: refused: call not expected" in capsys.readouterr().err


def test_dropped_connection_is_an_error(agentx, capsys):
    path = agentx / "cap" / "agentx" / "self.sock"
    path.parent.mkdir(parents=True)
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(str(path))
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        conn.recv(1)
        conn.close()

    threading.Thread(target=run, daemon=True).start()
    assert cap_client.main("ssh-ro", [], uid=os.getuid(), cwd="/") == 126
    assert "failed" in capsys.readouterr().err


@pytest.mark.parametrize(
    "args",
    [
        ["run"],
        ["run", "root", "/x"],
        ["run", "exec", "rel"],
        [],
        ["exec", "exec", "/x"],
    ],
)
def test_bad_usage(agentx, args):
    assert cap_client.main("kd-cap", args, uid=os.getuid(), cwd="/") == 2


def test_deleted_cwd_is_a_refusal_not_a_traceback(agentx, monkeypatch, capsys):
    # The entry point resolves the cwd inside main, so a removed directory is a plain refusal
    gone = agentx / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    assert cap_client.main("ssh-ro", ["node-c"], uid=os.getuid()) == 126
    assert "current directory is unreachable" in capsys.readouterr().err


def test_unknown_user_is_refused(tmp_path, capsys):
    registry = tmp_path / "agents.json"
    registry.write_text('{"sysadmin": {"user": "digi-sysadmin"}}')
    assert (
        cap_client.main("ssh-ro", [], uid=os.getuid(), cwd="/", registry=str(registry))
        == 126
    )
    assert "not the user of an agent" in capsys.readouterr().err


@pytest.fixture
def acme(monkeypatch):
    """A host that names commands acme-<word> and cards AC-<n>."""
    monkeypatch.setattr(naming, "COMMANDS", "acme")
    monkeypatch.setattr(naming, "CARDS", "AC")


def _pumped(data):
    read_end, write_end = os.pipe()
    out_r, out_w = os.pipe()
    thread = threading.Thread(target=cap_client.pump, args=(read_end, out_w))
    thread.start()
    for piece in data:
        os.write(write_end, piece)
    os.close(write_end)
    thread.join(10)
    os.close(out_w)
    with os.fdopen(out_r, "rb") as got:
        return got.read()


def test_pump_gives_the_hosts_names_a_line_at_a_time(acme):
    # a name split across two writes is still one name; bytes that are not UTF-8 pass as they are
    assert _pumped([b"run kd-ta", b"sk view KD-<n> \xff\n", b"no newline kd-work"]) == (
        b"run acme-task view AC-<n> \xff\nno newline acme-work"
    )


def test_pump_is_identity_on_a_host_with_the_repository_names():
    data = b"run kd-task view KD-<n>\n\x00\xfe"
    assert _pumped([data]) == data


def test_pump_reads_to_the_end_after_its_reader_left(acme):
    read_end, write_end = os.pipe()
    out_r, out_w = os.pipe()
    os.close(out_r)  # the agent stopped reading: the executor must still not block on a full pipe
    thread = threading.Thread(target=cap_client.pump, args=(read_end, out_w))
    thread.start()
    for _ in range(64):
        os.write(write_end, b"kd-task line\n" * 1024)
    os.close(write_end)
    thread.join(10)
    assert not thread.is_alive()
    os.close(out_w)


def _read_within(fd, seconds, size=65536):
    import select

    ready, _, _ = select.select([fd], [], [], seconds)
    return os.read(fd, size) if ready else b""


def test_pump_passes_a_part_without_its_newline_once_the_writer_is_quiet(acme):
    """A prompt (no newline yet) reaches the agent while the executor waits, not at its end (10.6)."""
    read_end, write_end = os.pipe()
    out_r, out_w = os.pipe()
    thread = threading.Thread(target=cap_client.pump, args=(read_end, out_w))
    thread.start()
    os.write(write_end, b"answer for kd-task: ")
    try:
        assert _read_within(out_r, 3) == b"answer for acme-task: "
    finally:
        os.close(write_end)
        thread.join(5)
        os.close(out_w)
        os.close(out_r)


def test_pump_passes_a_long_part_before_its_newline(acme, monkeypatch):
    monkeypatch.setattr(cap_client, "IDLE", 30.0)  # only the size lets it go
    read_end, write_end = os.pipe()
    out_r, out_w = os.pipe()
    thread = threading.Thread(target=cap_client.pump, args=(read_end, out_w))
    thread.start()
    os.write(write_end, b"x" * 10000)
    try:
        got = b""
        while len(got) < 10000 - cap_client.TAIL:
            part = _read_within(out_r, 5)
            assert part, len(got)
            got += part
    finally:
        os.close(write_end)
        thread.join(5)
        os.close(out_w)
        os.close(out_r)


def test_pump_ends_after_the_reply_when_a_child_holds_the_pipe(acme):
    read_end, write_end = os.pipe()  # write_end stays open: a child the executor left behind
    out_r, out_w = os.pipe()
    done = threading.Event()
    thread = threading.Thread(target=cap_client.pump, args=(read_end, out_w, done))
    thread.start()
    os.write(write_end, b"kd-task done\n")
    done.set()
    thread.join(3)
    try:
        assert not thread.is_alive()
        assert _read_within(out_r, 1) == b"acme-task done\n"
    finally:
        os.close(write_end)
        os.close(out_w)
        os.close(out_r)


def test_an_executors_prompt_comes_before_what_it_prints_after(agentx, acme):
    """stdout and stderr of one call into one pipe (2>&1): a part the executor paused at is not overtaken."""
    import time

    path = agentx / "cap" / "agentx" / "self.sock"
    path.parent.mkdir(parents=True)
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(str(path))
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        _, fds = cap_proto.recv_request(conn)
        os.write(fds[1], b"step kd-task ")
        time.sleep(1.5)
        os.write(fds[2], b"warning\n")
        os.write(fds[1], b"done\n")
        for fd in fds:
            os.close(fd)
        cap_proto.send_reply(conn, {"exit": 0})
        conn.close()

    threading.Thread(target=run, daemon=True).start()
    saved = os.dup(1), os.dup(2)
    out_r, out_w = os.pipe()
    os.dup2(out_w, 1)
    os.dup2(out_w, 2)
    try:
        rc = cap_client.main("/etc/digipaddock/agent-bin/agentx/acme-task", [], uid=os.getuid(), cwd="/")
    finally:
        os.dup2(saved[0], 1)
        os.dup2(saved[1], 2)
        for fd in (*saved, out_w):
            os.close(fd)
    with os.fdopen(out_r, "rb") as got:
        text = got.read()
    # two pipes keep no order between writes a moment apart (warning, done): only the paused part is first
    assert text.startswith(b"step acme-task ") and sorted(text[15:].splitlines()) == [b"done", b"warning"]
    assert rc == 0


@pytest.fixture
def streams(tmp_path):
    """The agent's stdout and stderr as pipes it reads, and a file it redirected into."""
    out, err = os.pipe(), os.pipe()
    with open(tmp_path / "redirected", "wb") as file:
        yield {"out": out[1], "err": err[1], "file": file.fileno()}
    for fd in (*out, *err):
        os.close(fd)


def _relayed(key, name, streams, stdout="out"):
    fds = [0, streams[stdout], streams["err"]]
    relays = cap_client._relays(key, name, fds)
    for read_end, write_end, _ in relays:
        os.close(read_end)
        os.close(write_end)
    return [{v: k for k, v in streams.items()}[out] for _, _, out in relays]


def test_relays_turn_names_only_where_the_agent_reads_text(acme, streams):
    assert _relayed("self", "acme-task", streams) == ["out", "err"]
    assert _relayed("base", "kd-cap", streams) == ["out", "err"]
    # data goes on as it is: a checkout's files, a server's output, the host's journal and files
    assert _relayed("self", "acme-repo", streams) == ["err"]
    assert _relayed("self", "ssh-ro", streams) == ["err"]
    assert _relayed("exec", "kd-cap", streams) == ["err"]
    assert _relayed("host", "kd-cap", streams) == ["err"]
    assert _relayed("self", "acme-task", streams, stdout="file") == ["err"]


def test_no_relays_on_a_host_with_the_repository_names(streams):
    assert _relayed("self", "kd-task", streams) == []


def test_an_executors_output_reaches_the_agent_with_the_hosts_names(agentx, acme):
    seen = []
    _server(agentx / "cap" / "agentx" / "self.sock", {"exit": 0}, seen, out=b"next: kd-task view KD-<n>\n")
    saved, (out_r, out_w) = os.dup(1), os.pipe()
    os.dup2(out_w, 1)
    try:
        rc = cap_client.main("/etc/digipaddock/agent-bin/agentx/acme-task", ["view"], uid=os.getuid(), cwd="/")
    finally:
        os.dup2(saved, 1)
        os.close(saved)
        os.close(out_w)
    with os.fdopen(out_r, "rb") as got:
        assert got.read() == b"next: acme-task view AC-<n>\n"
    assert rc == 0 and seen[0]["command"] == "acme-task"
