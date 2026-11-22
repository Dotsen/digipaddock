import os
import socket
import threading
import time

import pytest

from kd import cap_expect, cap_grants, cap_proto, cap_serve

pytestmark = pytest.mark.skipif(not hasattr(socket, "send_fds"), reason="Linux only")
# The tests run as root on stage; the caller and the service user are injected (real ones: test_peer_*).
CALLER_UID = 65534
CALLER = "nobody"
ME = "digi-test"


def _write_grants(
    root, key="self", commands=None, programs=None, uid=CALLER_UID, run_as=ME
):
    channel = {
        "key": key,
        "run_as": run_as,
        "commands": commands or {},
        "programs": programs or [],
    }
    os.makedirs(root / "agentx", exist_ok=True)
    (root / "agentx" / f"{key}.json").write_bytes(
        cap_grants.render("agentx", uid, channel)
    )


def _sh(tmp_path, **kwargs):
    _write_grants(
        tmp_path / "g",
        commands={
            "sh": {
                "program": "/bin/sh",
                "args": ["-c"],
                "subcommands": [],
                "env": {"X_KD": "1"},
            }
        },
        **kwargs,
    )


def _expect(tmp_path, *calls):
    cap_expect.record(
        "agentx",
        "s",
        f"t{time.monotonic_ns()}",
        [list(c) for c in calls],
        root=str(tmp_path / "e"),
        now=time.time(),
    )


def _serve_thread(tmp_path, conn, key, logs, **lookups):
    result = {}

    def run():
        result["rc"] = cap_serve.serve(
            conn,
            "agentx",
            key,
            grants_root=str(tmp_path / "g"),
            expect_root=str(tmp_path / "e"),
            log=lambda **f: logs.append(f),
            identify=lambda c: (os.getpid(), CALLER_UID, CALLER_UID),
            service_user=lambda: ME,
            **lookups,
        )

    thread = threading.Thread(target=run)
    thread.start()
    return thread, result


def _call(tmp_path, request, key="self", stdin=b"", **lookups):
    """Runs one call like the client does; returns (reply, stdout bytes, log entries)."""
    a, b = socket.socketpair()
    rin, win = os.pipe()
    rout, wout = os.pipe()
    logs = []
    thread, _ = _serve_thread(tmp_path, b, key, logs, **lookups)

    def feed():
        os.write(win, stdin) if stdin else None
        os.close(win)

    feeder = threading.Thread(target=feed)
    feeder.start()
    cap_proto.send_request(a, request, [rin, wout, wout])
    os.close(rin)
    os.close(wout)
    chunks = []
    while True:
        chunk = os.read(rout, 1 << 16)
        if not chunk:
            break
        chunks.append(chunk)
    os.close(rout)
    reply = cap_proto.recv_reply(a)
    feeder.join(10)
    thread.join(10)
    return reply, b"".join(chunks), logs


def test_expected_call_runs_with_passed_fds_and_rebuilt_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAK_ME", "1")
    _sh(tmp_path)
    script = "cat; echo $X_KD $KD_AGENT $KD_CALLER_USER ${LEAK_ME:-clean} $LANG; pwd"
    _expect(tmp_path, ["sh", script])
    reply, out, logs = _call(
        tmp_path,
        {"v": 1, "command": "sh", "argv": [script], "cwd": str(tmp_path)},
        stdin=b"in\n",
    )
    assert reply == {"exit": 0}
    assert out == f"in\n1 agentx {CALLER} clean C.UTF-8\n{tmp_path}\n".encode()
    assert (
        logs[-1]["command"] == "sh" and logs[-1]["exit"] == 0 and logs[-1]["args"] == 1
    )
    assert script not in repr(logs)  # argument contents are not journaled


def test_an_alias_card_argument_reaches_the_executor_as_the_card(tmp_path):
    # the agent typed an earlier prefix of the host (OLD-7, the fixture's card_aliases); the expectation
    # is what it typed, the executor gets the card
    _sh(tmp_path)
    script = 'echo "$0 $1"'
    _expect(tmp_path, ["sh", script, "OLD-7", "see OLD-7"])
    reply, out, _ = _call(tmp_path, {"v": 1, "command": "sh", "argv": [script, "OLD-7", "see OLD-7"], "cwd": "/"})
    assert reply == {"exit": 0} and out == b"KD-7 see OLD-7\n"


def test_unexpected_call_is_refused(tmp_path):
    _sh(tmp_path)
    reply, out, logs = _call(
        tmp_path, {"v": 1, "command": "sh", "argv": ["true"], "cwd": "/"}
    )
    assert "not expected" in reply["refused"] and out == b""
    assert "refused" in logs[-1]


def test_expectation_is_used_once(tmp_path):
    _sh(tmp_path)
    _expect(tmp_path, ["sh", "true"])
    assert _call(tmp_path, {"v": 1, "command": "sh", "argv": ["true"], "cwd": "/"})[
        0
    ] == {"exit": 0}
    assert (
        "not expected"
        in _call(tmp_path, {"v": 1, "command": "sh", "argv": ["true"], "cwd": "/"})[0][
            "refused"
        ]
    )


def test_switched_off_channel_is_refused(tmp_path):
    reply, _, _ = _call(tmp_path, {"v": 1, "command": "sh", "argv": [], "cwd": "/"})
    assert "switched off" in reply["refused"]


def test_foreign_uid_is_refused(tmp_path):
    _sh(tmp_path, uid=CALLER_UID - 1)
    _expect(tmp_path, ["sh", "true"])
    reply, _, _ = _call(
        tmp_path, {"v": 1, "command": "sh", "argv": ["true"], "cwd": "/"}
    )
    assert "uid" in reply["refused"]


def test_service_user_must_match_grants(tmp_path):
    _sh(tmp_path, run_as="someone-else")
    _expect(tmp_path, ["sh", "true"])
    reply, _, _ = _call(
        tmp_path, {"v": 1, "command": "sh", "argv": ["true"], "cwd": "/"}
    )
    assert "runs as" in reply["refused"]


def test_unreachable_cwd_is_refused(tmp_path):
    _sh(tmp_path)
    _expect(tmp_path, ["sh", "true"])
    reply, _, _ = _call(
        tmp_path,
        {"v": 1, "command": "sh", "argv": ["true"], "cwd": str(tmp_path / "gone")},
    )
    assert "cwd" in reply["refused"]


def test_malformed_request_is_refused(tmp_path):
    _sh(tmp_path)
    reply, _, _ = _call(
        tmp_path, {"v": 1, "program": "/bin/sh", "argv": [], "cwd": "/"}
    )
    assert "keys" in reply["refused"]


def test_exit_code_and_signal_are_reported(tmp_path):
    _sh(tmp_path)
    _expect(tmp_path, ["sh", "exit 7"], ["sh", "kill -TERM $$"])
    assert _call(tmp_path, {"v": 1, "command": "sh", "argv": ["exit 7"], "cwd": "/"})[
        0
    ] == {"exit": 7}
    assert _call(
        tmp_path, {"v": 1, "command": "sh", "argv": ["kill -TERM $$"], "cwd": "/"}
    )[0] == {"signal": 15}


def test_large_binary_output_and_stdin_pass_through(tmp_path):
    _sh(tmp_path)
    blob = os.urandom(3 * 1024 * 1024) + b"\0\0"
    _expect(tmp_path, ["sh", "cat"])
    reply, out, _ = _call(
        tmp_path, {"v": 1, "command": "sh", "argv": ["cat"], "cwd": "/"}, stdin=blob
    )
    assert reply == {"exit": 0} and out == blob


def test_exec_channel_runs_listed_program_only_from_root_dir(tmp_path):
    _write_grants(tmp_path / "g", key="exec", programs=["/bin/pwd"])
    reply, out, _ = _call(
        tmp_path,
        {"v": 1, "program": "/bin/pwd", "argv": [], "cwd": str(tmp_path)},
        key="exec",
    )
    assert reply == {"exit": 0} and out == b"/\n"
    reply, _, _ = _call(
        tmp_path, {"v": 1, "program": "/bin/sh", "argv": [], "cwd": "/"}, key="exec"
    )
    assert "not a program" in reply["refused"]


def test_silent_client_times_out(tmp_path, monkeypatch):
    monkeypatch.setattr(cap_serve, "REQUEST_TIMEOUT", 0.5)
    a, b = socket.socketpair()
    logs = []
    thread, result = _serve_thread(tmp_path, b, "self", logs)
    thread.join(5)
    assert not thread.is_alive() and result["rc"] == 126
    a.close()


def test_client_eof_kills_process_group(tmp_path):
    _sh(tmp_path)
    marker = tmp_path / "pid"
    script = f"sh -c 'echo $$ > {marker}; exec sleep 60' & wait"
    _expect(tmp_path, ["sh", script])
    a, b = socket.socketpair()
    logs = []
    thread, _ = _serve_thread(tmp_path, b, "self", logs)
    devnull = os.open(os.devnull, os.O_RDWR)
    cap_proto.send_request(
        a,
        {"v": 1, "command": "sh", "argv": [script], "cwd": "/"},
        [devnull, devnull, devnull],
    )
    os.close(devnull)
    for _ in range(100):
        if marker.exists() and marker.read_text().strip():
            break
        time.sleep(0.05)
    child = int(marker.read_text())
    a.close()  # Claude Code killed the client
    thread.join(cap_serve.TERM_GRACE + 5)
    assert not thread.is_alive()
    for _ in range(60):
        try:
            with open(f"/proc/{child}/stat") as handle:
                if handle.read().split(") ", 1)[1].startswith("Z"):
                    break
        except FileNotFoundError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("the grandchild survived the client")


def test_client_garbage_after_request_does_not_spin(tmp_path):
    _sh(tmp_path)
    _expect(tmp_path, ["sh", "sleep 0.3"])
    a, b = socket.socketpair()
    logs = []
    thread, result = _serve_thread(tmp_path, b, "self", logs)
    devnull = os.open(os.devnull, os.O_RDWR)
    cap_proto.send_request(
        a,
        {"v": 1, "command": "sh", "argv": ["sleep 0.3"], "cwd": "/"},
        [devnull, devnull, devnull],
    )
    os.close(devnull)
    a.sendall(b"junk")
    assert cap_proto.recv_reply(a) == {"exit": 0}
    thread.join(5)


def test_peer_reads_so_peercred():
    a, b = socket.socketpair()
    assert cap_serve.peer(b) == (os.getpid(), os.getuid(), os.getgid())


def test_service_user_is_the_process_user():
    import pwd

    assert cap_serve.service_user() == pwd.getpwuid(os.getuid()).pw_name


def test_group_that_ignores_term_is_killed_after_grace(tmp_path, monkeypatch):
    monkeypatch.setattr(cap_serve, "TERM_GRACE", 0.5)
    _sh(tmp_path)
    marker = tmp_path / "pid"
    script = f"trap '' TERM; echo $$ > {marker}; while :; do sleep 0.1; done"
    _expect(tmp_path, ["sh", script])
    a, b = socket.socketpair()
    thread, _ = _serve_thread(tmp_path, b, "self", [])
    devnull = os.open(os.devnull, os.O_RDWR)
    cap_proto.send_request(
        a,
        {"v": 1, "command": "sh", "argv": [script], "cwd": "/"},
        [devnull, devnull, devnull],
    )
    os.close(devnull)
    for _ in range(100):
        if marker.exists() and marker.read_text().strip():
            break
        time.sleep(0.05)
    a.close()
    thread.join(5)
    assert not thread.is_alive()
    assert not os.path.exists(f"/proc/{int(marker.read_text())}")


def test_the_callers_session_unit_reaches_the_program(tmp_path):
    """The session unit the caller runs in (its cgroup) is the program's KD_CALLER_SESSION;
    a grants file cannot set it."""
    assert "KD_CALLER_SESSION" in cap_grants.FIXED_ENV
    cgroup = "0::/kd.slice/digipaddock-sessions.slice/digipaddock-session-0123456789ab-3.service\n"
    assert cap_serve.session_of_cgroup(cgroup) == "0123456789ab"
    for other in ("0::/system.slice/digipaddock-session-job-0123456789ab.service\n", "0::/user.slice/session-3.scope\n", ""):
        assert cap_serve.session_of_cgroup(other) == ""
    assert cap_serve.caller_session(2 ** 22 + 7) == ""  # no such process: no session


def test_a_forwarded_session_counts_only_from_the_agents_own_self_unit(tmp_path):
    """Review 04.11.2026 (L2): kd-cap in the agent's self-channel unit forwards the session that channel saw, so a
    finish or a help request reaches the executor bound to its turn; the kernel's session wins, and a forwarded one
    from anywhere else — another agent's self unit, a process outside any unit, a self channel — is not taken."""
    _write_grants(tmp_path / "g", key="exec", programs=["/bin/sh"])
    request = {"v": 1, "program": "/bin/sh", "argv": ["-c", "echo \"[$KD_CALLER_SESSION]\""], "cwd": "/",
               "session": "0123456789ab"}

    def seen(session_of, self_unit_of):
        reply, out, _ = _call(tmp_path, dict(request), key="exec", session_of=session_of, self_unit_of=self_unit_of)
        assert reply == {"exit": 0}
        return out.decode().strip()

    assert seen(lambda pid: "", lambda pid: "agentx") == "[0123456789ab]"
    assert seen(lambda pid: "fedcba987654", lambda pid: "agentx") == "[fedcba987654]"
    assert seen(lambda pid: "", lambda pid: "agenty") == "[]"
    assert seen(lambda pid: "", lambda pid: "") == "[]"
    assert cap_serve._session("agentx", "self", 1, request, lambda pid: "", lambda pid: "agentx") == ""
    cgroup = "0::/kd.slice/digipaddock-cap-agentx-self@3-1000.service\n"
    assert cap_serve.self_unit_agent(cgroup) == "agentx"
    assert cap_serve.self_unit_agent("0::/kd.slice/digipaddock-cap-agentx-exec@3.service\n") == ""


@pytest.mark.parametrize("program", ["kd-work", "kd-task", "kd-boss-board"])
def test_a_board_program_keeps_the_channels_session_past_its_clean_environment(program):
    """Review 04.11.2026 (L2): the channel's KD_CALLER_SESSION survives the program's os.environ.clear(); before,
    every finish and help request reached the cabinet without a session and so counted for any turn of the agent."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", program)
    with open(path, encoding="utf-8") as handle:
        main = handle.read().split('if __name__ == "__main__":', 1)[1]
    assert "KD_CALLER_SESSION" in main.split("os.environ.clear()", 1)[0]
