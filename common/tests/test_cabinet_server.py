"""the cabinet's unix socket (Linux: SO_PEERCRED of a real peer)."""

import asyncio
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from kd.cabinet import api, client, server

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cabinet_env import CabinetEnv  # noqa: E402

unix = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX") or not hasattr(socket, "SO_PEERCRED"),
    reason="unix sockets with SO_PEERCRED",
)


def _start(env, path, principal_of, changed):
    thread = threading.Thread(
        target=lambda: asyncio.run(
            server.serve(
                env.ctx,
                path=path,
                principal_of=principal_of,
                on_change=lambda: changed.append(1),
            )
        ),
        daemon=True,
    )
    thread.start()
    for _ in range(100):
        if os.path.exists(path):
            return
        time.sleep(0.05)
    raise AssertionError("the server did not bind")


@unix
def test_server_identifies_the_peer_and_serializes_writes(tmp_path):
    env = CabinetEnv(tmp_path)
    me = os.getuid()

    def principal_of(uid, **k):
        return api.Principal("root", uid, "root") if uid == me else None

    path = str(tmp_path / "s.sock")
    changed = []
    _start(env, path, principal_of, changed)
    c = client.Client(path)
    out = c.call({"op": "card.create", "project": "admin", "title": "Old"})
    assert out["card"]["title"] == "Old" and changed == [1]
    stale = out["card"]["revision"] + 1
    with pytest.raises(client.CabinetError) as exc:
        c.call({"op": "card.edit", "card": out["card"]["id"], "revision": stale, "comment": "x"})
    assert exc.value.kind == "conflict" and changed == [1]
    assert c.call({"op": "state.get", "key": "sync"}) == {
        "value": None
    } and changed == [1]
    # an oversized or non-JSON request is answered, not fatal
    raw = socket.socket(socket.AF_UNIX)
    raw.connect(path)
    raw.sendall(b"not json\n")
    assert json.loads(raw.makefile("rb").readline())["error"] == "invalid"
    raw = socket.socket(socket.AF_UNIX)
    raw.connect(path)
    raw.sendall(b"x" * (server.MAX_REQUEST + 10) + b"\n")
    assert json.loads(raw.makefile("rb").readline())["error"] == "invalid"
    assert c.call({"op": "card.get", "card": out["card"]["id"]})["card"]["title"] == "Old"  # still serving


@unix
def test_an_unknown_peer_is_refused(tmp_path):
    env = CabinetEnv(tmp_path)
    path = str(tmp_path / "s.sock")
    _start(env, path, lambda uid, **k: None, [])
    with pytest.raises(client.CabinetError) as exc:
        client.Client(path).call({"op": "card.list"})
    assert exc.value.kind == "refused"


@unix
def test_an_agent_peer_may_read_but_not_write_for_another_agent(tmp_path):
    # Review focus 2 through the socket: the peer uid is an agent's own user.
    env = CabinetEnv(tmp_path)
    env.card()
    me = os.getuid()

    def principal_of(uid, **k):
        return api.Principal("agent", uid, "digi-dev", "dev") if uid == me else None

    path = str(tmp_path / "s.sock")
    _start(env, path, principal_of, [])
    c = client.Client(path)
    assert [card["id"] for card in c.call({"op": "card.list"})["cards"]] == ["KD-1"]
    with pytest.raises(client.CabinetError) as exc:
        c.call(
            {"op": "worker.claim", "agent": "sysadmin", "card": "KD-1", "revision": 1}
        )
    assert exc.value.kind == "refused"


@unix
def test_an_internal_error_is_a_plain_answer(tmp_path, monkeypatch):
    env = CabinetEnv(tmp_path)
    me = os.getuid()
    path = str(tmp_path / "s.sock")
    monkeypatch.setattr(env.store, "cards", lambda **k: 1 / 0)
    _start(
        env,
        path,
        lambda uid, **k: api.Principal("root", uid, "root") if uid == me else None,
        [],
    )
    with pytest.raises(client.CabinetError) as exc:
        client.Client(path).call({"op": "card.list"})
    assert exc.value.kind == "unavailable" and "Traceback" not in str(exc.value)


class _FakeSocket:
    def __init__(self, *args, fileno=None, **kwargs):
        self.fd = fileno


def test_listen_sockets_are_found_by_their_names(monkeypatch):
    monkeypatch.setattr(server.socket, "socket", _FakeSocket)
    monkeypatch.setenv("LISTEN_PID", str(os.getpid()))
    monkeypatch.setenv("LISTEN_FDS", "2")
    monkeypatch.setenv("LISTEN_FDNAMES", "local:http")
    found = server.listen_sockets()
    assert {name: s.fd for name, s in found.items()} == {"local": 3, "http": 4}
    monkeypatch.setenv("LISTEN_FDNAMES", "http:local")
    assert {name: s.fd for name, s in server.listen_sockets().items()} == {
        "http": 3,
        "local": 4,
    }
    # the older socket unit (one socket, systemd names it after the unit) is the local one
    monkeypatch.setenv("LISTEN_FDS", "1")
    monkeypatch.setenv("LISTEN_FDNAMES", "digipaddock-cabinet.socket")
    assert {name: s.fd for name, s in server.listen_sockets().items()} == {"local": 3}
    monkeypatch.setenv("LISTEN_PID", str(os.getpid() + 1))  # passed to another process
    assert server.listen_sockets() == {}
    monkeypatch.delenv("LISTEN_PID")
    assert server.listen_sockets() == {}


def test_serve_all_answers_http_and_the_local_socket_on_one_loop(tmp_path):
    """The web app on the socket systemd would pass (a TCP one here) and, on Linux, the local socket."""
    pytest.importorskip("fastapi")
    import httpx
    from web_env import WebEnv

    w = WebEnv(tmp_path / "w")
    http_sock = socket.socket()
    http_sock.bind(("127.0.0.1", 0))
    http_sock.listen()
    port = http_sock.getsockname()[1]
    local_sock = local_path = None
    linux = hasattr(socket, "AF_UNIX") and hasattr(socket, "SO_PEERCRED")
    if linux:
        local_path = str(tmp_path / "local.sock")
        local_sock = socket.socket(socket.AF_UNIX)
        local_sock.bind(local_path)
        local_sock.listen()
    me = os.getuid() if linux else -1
    threading.Thread(
        target=lambda: asyncio.run(
            server.serve_all(
                w.svc,
                local_sock=local_sock,
                http_sock=http_sock,
                principal_of=lambda uid, **k: (
                    api.Principal("root", uid, "root") if uid == me else None
                ),
                scheduler_interval=3600,
            )
        ),
        daemon=True,
    ).start()
    status = None
    for _ in range(100):
        try:
            status = httpx.get(f"http://127.0.0.1:{port}/api/me").status_code
            break
        except httpx.TransportError:
            time.sleep(0.05)
    assert status == 401
    if linux:
        out = client.Client(local_path).call({"op": "card.create", "project": "admin", "title": "Old"})
        assert out["card"]["title"] == "Old"


def test_start_backfills_signed_copies_and_survives_a_failure():
    """the start gives live grants their signed copies; a failed backfill only says so."""
    import io

    from kd import admin_grant

    class Admin:
        def __init__(self, outcome):
            self.outcome = outcome

        def backfill(self, now):
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return self.outcome

    err = io.StringIO()
    server.backfill_signed(Admin(["KD-1: signed plan kept"]), err)
    server.backfill_signed(Admin(admin_grant.GrantError("grant directory x is missing")), err)
    assert err.getvalue().splitlines() == [
        "kd-cabinet: signed copies: KD-1: signed plan kept",
        "kd-cabinet: signed copies: not done (GrantError: grant directory x is missing)",
    ]
