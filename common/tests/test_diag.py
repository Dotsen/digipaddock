"""R31, kd-diag — a typed health probe of a catalog target for the card's assignee, its result in the thread."""

import io
import json
import socket
import threading
import time

import pytest
from cabinet_env import CabinetEnv

from kd import diag

URL = "http://127.0.0.1:18080/health"


def _catalog(tmp_path, targets=None, **limits):
    path = tmp_path / "diagnostics.json"
    body = {
        "schema_version": 1,
        "limits": {"timeout_s": 3, "per_card_hour": 10, "reuse_s": 600, **limits},
        "targets": targets if targets is not None else {
            "hindsight-api": {"op": "http-health", "url": URL, "area": "sysadmin"},
        },
    }
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


# --- the catalog ---------------------------------------------------------------------------------------------

def test_catalog_loads_targets_and_limits(tmp_path):
    catalog = diag.load_catalog(_catalog(tmp_path))
    assert catalog.limits == diag.Limits(timeout_s=3, per_card_hour=10, reuse_s=600)
    assert catalog.targets["hindsight-api"] == diag.Target("hindsight-api", "http-health", URL, "sysadmin")


@pytest.mark.parametrize("url", [
    "http://10.0.0.5:18080/health",            # not loopback
    "http://example.org:80/health",           # a name
    "http://user:pw@127.0.0.1:18080/health",   # userinfo
    "http://127.0.0.1:18080/health?x=1",       # a query
    "http://127.0.0.1:18080/health#top",       # a fragment
    "https://127.0.0.1:18080/health",          # https
    "http://127.0.0.1/health",                # no port
    "http://127.0.0.1:18080",                  # no path
    "http://127.0.0.1:18080/he alth",          # a space
], ids=["remote", "name", "userinfo", "query", "fragment", "https", "no-port", "no-path", "space"])
def test_catalog_refuses_a_non_loopback_url(tmp_path, url):
    path = _catalog(tmp_path, {"t": {"op": "http-health", "url": url, "area": "sysadmin"}})
    with pytest.raises(diag.DiagError):
        diag.load_catalog(path)


@pytest.mark.parametrize("target", [
    {"op": "shell", "url": URL, "area": "sysadmin"},
    {"op": "http-health", "url": URL, "area": "Sys admin"},
    {"op": "http-health", "url": URL, "area": "sysadmin", "command": "curl"},
], ids=["op", "area", "extra"])
def test_catalog_refuses_an_unknown_op_or_field(tmp_path, target):
    with pytest.raises(diag.DiagError):
        diag.load_catalog(_catalog(tmp_path, {"t": target}))


def test_repository_catalog_loads():
    from pathlib import Path

    catalog = diag.load_catalog(Path(diag.__file__).resolve().parents[1] / "coordination" / "diagnostics.json")
    assert {"hindsight-api", "hindsight-worker", "cabinet-push"} <= set(catalog.targets)
    from kd.cabinet import diag_api

    queries = [t.query for t in catalog.targets.values() if t.op == "cabinet-query"]
    assert queries and set(queries) <= set(diag_api.QUERIES)  # every catalog query is one the cabinet runs


def test_catalog_loads_a_cabinet_query(tmp_path):
    catalog = diag.load_catalog(_catalog(tmp_path, {"cabinet-push": {"op": "cabinet-query", "query": "push",
                                                                     "area": "dev"}}))
    assert catalog.targets["cabinet-push"] == diag.Target("cabinet-push", "cabinet-query", "", "dev", "push")


@pytest.mark.parametrize("target", [
    {"op": "cabinet-query", "query": "SELECT 1", "area": "dev"},
    {"op": "cabinet-query", "query": "push", "url": URL, "area": "dev"},
    {"op": "cabinet-query", "area": "dev"},
    {"op": "http-health", "query": "push", "area": "dev"},
], ids=["sql", "url-too", "no-query", "http-with-query"])
def test_catalog_refuses_a_bad_query_target(tmp_path, target):
    with pytest.raises(diag.DiagError):
        diag.load_catalog(_catalog(tmp_path, {"t": target}))


# --- the probe -----------------------------------------------------------------------------------------------

class Server:
    """A loopback TCP server answering each connection with ``answer(conn)``; counts the requests it read."""

    def __init__(self, answer):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port, self.requests, self.answer = self.sock.getsockname()[1], 0, answer
        threading.Thread(target=self._serve, daemon=True).start()

    def url(self, path="/health"):
        return f"http://127.0.0.1:{self.port}{path}"

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    def _one(self, conn):
        with conn:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            self.requests += 1
            self.answer(conn)

    def close(self):
        self.sock.close()


def test_probe_reports_the_status():
    server = Server(lambda c: c.sendall(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n"))
    try:
        found = diag.probe(server.url(), 3)
    finally:
        server.close()
    assert (found.status, found.category) == (204, "http") and found.ms >= 0


def test_probe_does_not_follow_redirects():
    server = Server(lambda c: c.sendall(
        b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:1/elsewhere\r\nContent-Length: 0\r\n\r\n"))
    try:
        found = diag.probe(server.url(), 3)
        time.sleep(0.2)
        assert (found.status, found.category, server.requests) == (302, "http", 1)
    finally:
        server.close()


def test_probe_never_reads_the_body():
    def slow(conn):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n")
        time.sleep(3)

    server = Server(slow)
    try:
        started = time.monotonic()
        found = diag.probe(server.url(), 3)
        assert time.monotonic() - started < 1 and (found.status, found.category) == (200, "http")
    finally:
        server.close()


def test_probe_timeout_and_refused_categories():
    server = Server(lambda c: time.sleep(3))
    try:
        found = diag.probe(server.url(), 0.3)
    finally:
        server.close()
    assert (found.status, found.category) == (None, "timeout")
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()
    found = diag.probe(f"http://127.0.0.1:{port}/health", 5)
    assert (found.status, found.category) == (None, "refused")


def test_probe_reset_category():
    def reset(conn):
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
        conn.close()

    server = Server(reset)
    try:
        found = diag.probe(server.url(), 3)
    finally:
        server.close()
    assert (found.status, found.category) in ((None, "reset"), (None, "error"))


# --- kd-diag check -------------------------------------------------------------------------------------------

class Probes:
    def __init__(self, answer=diag.Probe(200, "http", 12)):
        self.answer, self.urls = answer, []

    def __call__(self, url, timeout):
        self.urls.append((url, timeout))
        return self.answer


@pytest.fixture
def env(tmp_path):
    return CabinetEnv(tmp_path / "cab")


def run(env, tmp_path, argv, *, caller=("dev", "digi-dev"), probe=None, catalog=None):
    out, err = io.StringIO(), io.StringIO()
    code = diag.main(argv, caller=caller, client=env.client("digi"), catalog_path=catalog or _catalog(tmp_path),
                     probe=probe or Probes(), now=env.when, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def _diagnostics(env, card):
    return [m for m in env.store.thread_messages(env.store.card(card).thread, 0) if m["kind"] == "diagnostic"]


def test_check_probes_and_prints_one_line(env, tmp_path):
    card, probe = env.card(), Probes()
    code, out, err = run(env, tmp_path, ["check", "hindsight-api", "--card", card.id], probe=probe)
    assert (code, err) == (0, "")
    assert out == "diag hindsight-api: HTTP 200 at 2026-10-14T10:00:00Z (checked by kd-diag for @dev)\n"
    assert probe.urls == [(URL, 3)]
    assert len(_diagnostics(env, card.id)) == 1


def test_check_needs_the_cards_assignee_and_an_approved_card(env, tmp_path):
    probe = Probes()
    for card in (env.card(assignee="@sysadmin"), env.card(status="Inbox"), env.card(status="Review"),
                 env.card(assignee="@dev, @analyst")):
        code, out, err = run(env, tmp_path, ["check", "hindsight-api", "--card", card.id], probe=probe)
        assert (code, out) == (2, "") and "not your approved card" in err
    card = env.card(status="In Progress")
    assert run(env, tmp_path, ["check", "hindsight-api", "--card", card.id], caller=None, probe=probe)[0] == 2
    assert probe.urls == []
    assert run(env, tmp_path, ["check", "hindsight-api", "--card", card.id], probe=probe)[0] == 0


@pytest.mark.parametrize("argv", [
    ["check", "hindsight-api"], ["check", "hindsight-api", "--card", "KD-x"], ["check", "Bad Target", "--card", "KD-1"],
    ["check", "hindsight-api", "--card", "KD-1", "--key", "a b"], ["run", "x"], ["check", "--url", "http://x"],
], ids=["no-card", "bad-card", "bad-target", "bad-key", "unknown-op", "url-option"])
def test_usage_errors_are_refused(env, tmp_path, argv):
    code, _, err = run(env, tmp_path, argv)
    assert code == 2 and err.startswith("kd-diag: ")


def test_unknown_target_is_capability_missing_in_the_thread(env, tmp_path):
    card, probe = env.card(), Probes()
    code, out, _ = run(env, tmp_path, ["check", "mysql", "--card", card.id], probe=probe)
    assert code == 4 and probe.urls == []
    assert out.startswith("capability_missing: ") and "ask @boss for platform work" in out
    assert "do not ask the owner to run it" in out
    (message,) = _diagnostics(env, card.id)
    assert message["payload"]["key"] == "diag.missing"
    assert message["payload"]["diag"]["category"] == "capability_missing"


def test_same_key_within_reuse_returns_the_stored_result(env, tmp_path):
    card, probe = env.card(), Probes()
    argv = ["check", "hindsight-api", "--card", card.id]
    first = run(env, tmp_path, argv, probe=probe)[1]
    code, again, _ = run(env, tmp_path, argv, probe=probe)
    assert code == 0 and again == first.rstrip("\n") + " (reused)\n" and len(probe.urls) == 1
    assert run(env, tmp_path, argv + ["--key", "after-restart"], probe=probe)[0] == 0 and len(probe.urls) == 2
    env.now = "2026-10-14T10:10:01Z"  # past reuse_s
    run(env, tmp_path, argv, probe=probe)
    assert len(probe.urls) == 3 and len(_diagnostics(env, card.id)) == 3


def test_rate_per_card_hour(env, tmp_path):
    card, probe = env.card(), Probes()
    catalog = _catalog(tmp_path, per_card_hour=2)
    for key in ("a", "b"):
        assert run(env, tmp_path, ["check", "hindsight-api", "--card", card.id, "--key", key], probe=probe,
                   catalog=catalog)[0] == 0
    code, _, err = run(env, tmp_path, ["check", "hindsight-api", "--card", card.id, "--key", "c"], probe=probe,
                       catalog=catalog)
    assert code == 5 and "2 diagnostics per card per hour" in err and len(probe.urls) == 2
    assert run(env, tmp_path, ["check", "hindsight-api", "--card", card.id, "--key", "a"], probe=probe,
               catalog=catalog)[0] == 0  # a reuse costs nothing
    env.now = "2026-10-14T11:00:01Z"
    assert run(env, tmp_path, ["check", "hindsight-api", "--card", card.id, "--key", "c"], probe=probe,
               catalog=catalog)[0] == 0


def test_a_failed_probe_is_its_category(env, tmp_path):
    card = env.card()
    code, out, _ = run(env, tmp_path, ["check", "hindsight-api", "--card", card.id],
                       probe=Probes(diag.Probe(None, "refused", 1)))
    assert code == 0 and out.startswith("diag hindsight-api: refused at 2026-10-14T10:00:00Z")


def test_list_prints_targets_and_ops(env, tmp_path):
    code, out, _ = run(env, tmp_path, ["list"])
    assert (code, out) == (0, "hindsight-api http-health\n")


QUERY_TARGETS = {"hindsight-api": {"op": "http-health", "url": URL, "area": "sysadmin"},
                 "cabinet-push": {"op": "cabinet-query", "query": "push", "area": "dev"}}


def test_a_cabinet_query_prints_its_rows_and_the_thread_keeps_the_count(env, tmp_path):
    card, probe = env.card(), Probes()
    env.users.create("owner", "owner", roles=[("owner", "*")], actor="root", language="ru", timezone="UTC")
    env.store.subscribe("owner", "https://fcm.googleapis.com/fcm/send/tok", "k" * 20, "a" * 8, "phone",
                        actor="user:owner")
    catalog = _catalog(tmp_path, QUERY_TARGETS)
    code, out, err = run(env, tmp_path, ["check", "cabinet-push", "--card", card.id], probe=probe, catalog=catalog)
    assert (code, err, probe.urls) == (0, "", [])
    head, *rows = out.splitlines()
    assert head == "diag cabinet-push: 1 rows at 2026-10-14T10:00:00Z (checked by kd-diag for @dev)"
    assert len(rows) == 1 and rows[0].startswith("sub\t1\tuser=owner host=fcm.googleapis.com ")
    (message,) = _diagnostics(env, card.id)
    assert message["payload"]["diag"]["rows"] == 1 and "tok" not in json.dumps(message["payload"])


def test_a_cabinet_query_is_read_anew_and_counts_against_the_hour(env, tmp_path):
    card = env.card()
    catalog = _catalog(tmp_path, QUERY_TARGETS, per_card_hour=2)
    argv = ["check", "cabinet-push", "--card", card.id]
    for _ in range(2):
        code, out, _ = run(env, tmp_path, argv, catalog=catalog)
        assert code == 0 and "(reused)" not in out
    assert len(_diagnostics(env, card.id)) == 2
    code, _, err = run(env, tmp_path, argv, catalog=catalog)
    assert code == 5 and "per card per hour" in err


def test_a_cabinet_query_needs_the_cards_assignee(env, tmp_path):
    card = env.card(assignee="@sysadmin")
    code, out, err = run(env, tmp_path, ["check", "cabinet-push", "--card", card.id],
                         catalog=_catalog(tmp_path, QUERY_TARGETS))
    assert (code, out) == (2, "") and "not your approved card" in err
    assert run(env, tmp_path, ["check", "cabinet-push", "--card", card.id], caller=("sysadmin", "digi-sysadmin"),
               catalog=_catalog(tmp_path, QUERY_TARGETS))[0] == 0


def test_cabinet_away_is_an_error(tmp_path):
    class Away:
        def call(self, request):
            raise OSError("no socket")

    out, err = io.StringIO(), io.StringIO()
    code = diag.main(["check", "hindsight-api", "--card", "KD-1"], caller=("dev", "digi-dev"), client=Away(),
                     catalog_path=_catalog(tmp_path), probe=Probes(), now=lambda: None, out=out, err=err)
    assert code == 1 and "cabinet" in err.getvalue()


def test_probe_has_one_deadline_for_a_server_that_trickles(tmp_path):
    """Final review: the timeout bounds the whole probe, not each read."""
    def trickle(conn):
        try:
            for byte in b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n":
                conn.sendall(bytes([byte]))
                time.sleep(0.1)
        except OSError:  # the probe gave up and closed it
            pass

    server = Server(trickle)
    try:
        started = time.monotonic()
        found = diag.probe(server.url(), 0.5)
        assert time.monotonic() - started < 1.2 and (found.status, found.category) == (None, "timeout")
    finally:
        server.close()
