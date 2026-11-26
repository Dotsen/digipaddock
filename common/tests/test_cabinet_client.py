"""the cabinet clients without a real socket (LocalClient, a stand-in transport)."""

import importlib.util
import io
import json
import os
import sys
import types
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

from kd.cabinet import client

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cabinet_env import CabinetEnv  # noqa: E402

BIN = Path(__file__).resolve().parents[1] / "bin"


def test_local_client_strips_ok_and_raises_typed_errors(tmp_path):
    env = CabinetEnv(tmp_path)
    card = env.card()
    local = client.LocalClient(env.ctx, env.principal("root"))
    assert local.call({"op": "card.get", "card": card.id})["card"]["id"] == card.id
    with pytest.raises(client.CabinetError) as exc:
        local.call({"op": "card.get", "card": "KD-9"})
    assert exc.value.kind == "not_found" and client.exit_code(exc.value) == 2


def test_local_client_speaks_json_like_the_socket(tmp_path):
    env = CabinetEnv(tmp_path)
    local = client.LocalClient(env.ctx, env.principal("digi"))
    out = local.call(
        {
            "op": "worker.create",
            "agent": "dev",
            "title": "x",
            "project": "digipaddock",
            "label": ("a",),
        }
    )
    assert out["card"]["labels"] == ["a"] and "ok" not in out


def test_socket_client_maps_a_missing_socket_to_unavailable(tmp_path):
    with pytest.raises(client.CabinetError) as exc:
        client.Client(str(tmp_path / "none.sock")).call({"op": "card.list"})
    assert exc.value.kind == "unavailable" and client.exit_code(exc.value) == 1


def test_socket_client_round_trip(monkeypatch):
    sent = {}

    class Conn:
        def __init__(self, *a, **k):
            pass

        def settimeout(self, t):
            pass

        def connect(self, path):
            sent["path"] = path

        def sendall(self, data):
            sent["data"] = data

        def makefile(self, mode):
            return io.BytesIO(
                b'{"ok": false, "error": "conflict", "message": "stale"}\n'
            )

        def close(self):
            pass

    monkeypatch.setattr(client.socket, "socket", Conn)
    monkeypatch.setattr(
        client.socket, "AF_UNIX", 1, raising=False
    )  # Windows has no unix sockets
    with pytest.raises(client.CabinetError) as exc:
        client.Client("/tmp/x.sock").call({"op": "card.get", "card": "KD-1"})
    assert exc.value.kind == "conflict" and json.loads(sent["data"]) == {
        "op": "card.get",
        "card": "KD-1",
    }
    assert sent["path"] == "/tmp/x.sock"


@pytest.mark.parametrize("line", [b"", b"nonsense\n", b"[1]\n"])
def test_socket_client_treats_a_broken_answer_as_unavailable(line):
    with pytest.raises(client.CabinetError) as exc:
        client._unwrap(line)
    assert exc.value.kind == "unavailable"


def test_boss_card_and_records(tmp_path):
    env = CabinetEnv(tmp_path)
    card = env.card(servers=["node-c"])
    local = client.LocalClient(env.ctx, env.principal("digi-dev"))
    assert client.boss_card(local, card.id).labels == ("server:node-c",)
    assert [r["id"] for r in client.records(local, status="To Do", assignee=None)] == [
        card.id
    ]


def _kd_cabinet():
    loader = SourceFileLoader("kd_cabinet", str(BIN / "kd-cabinet"))
    spec = importlib.util.spec_from_loader("kd_cabinet", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_kd_cabinet_prints_cards_for_the_owner(tmp_path):
    env = CabinetEnv(tmp_path)
    card = env.card(title="Alpha", labels=["x"])
    env.store.edit_card(
        card.id,
        None,
        {"comment": {"text": "[boss] hi", "author": "@boss"}},
        actor="agent:boss",
    )
    cab = _kd_cabinet()
    out, err = io.StringIO(), io.StringIO()
    local = client.LocalClient(env.ctx, env.principal("root"))
    assert cab.main(["cards", "-s", "To Do"], client=local, out=out, err=err) == 0
    assert out.getvalue().splitlines() == ["KD-1 · To Do · @dev · Alpha [x]"]
    for gone in (["cards", "-m", "m-1"], ["edit", "KD-1", "-m", "m-1"]):  # no milestones, no flag
        assert cab.main(gone, client=local, out=io.StringIO(), err=io.StringIO()) == 2, gone
    out = io.StringIO()
    assert cab.main(["card", "KD-1"], client=local, out=out, err=err) == 0
    assert "Alpha" in out.getvalue() and "[boss] hi" in out.getvalue()
    down = type(
        "Down",
        (),
        {
            "call": lambda self, r: (_ for _ in ()).throw(
                client.CabinetError("unavailable", "x")
            )
        },
    )()
    assert (
        cab.main(["cards"], client=down, out=out, err=err) == 1
        and "x" in err.getvalue()
    )


def test_kd_cabinet_creates_and_edits_a_card_as_root(tmp_path):
    env = CabinetEnv(tmp_path)
    cab = _kd_cabinet()
    out, err = io.StringIO(), io.StringIO()
    local = client.LocalClient(env.ctx, env.principal("root"))
    assert cab.main(["create", "Чистка", "-p", "digipaddock", "-d", "зачем"], client=local, out=out, err=err) == 0
    assert out.getvalue() == "KD-1 · Inbox · @dev · Чистка\n"
    assert env.store.card("KD-1").description == "зачем" and env.store.card("KD-1").created_by == "root"
    out = io.StringIO()
    assert cab.main(["edit", "KD-1", "-s", "Done", "-c", "закрыто"], client=local, out=out, err=err) == 0
    assert out.getvalue() == "KD-1 · Done · @dev · Чистка\n"
    assert env.store.messages("KD-1")[-1]["body"] == "закрыто"
    assert cab.main(["edit", "KD-1", "-c", "ещё"], client=local, out=io.StringIO(), err=err) == 0
    assert env.store.card("KD-1").revision == 3
    assert cab.main(["edit", "KD-1"], client=local, out=out, err=err) == 2 and "needs a change" in err.getvalue()
    env.store.edit_card("KD-1", 3, {"acceptance": [{"text": "a", "checked": True}, {"text": "b", "checked": False}]}, actor="root")
    assert cab.main(["edit", "KD-1", "--uncheck-ac", "1", "--check-ac", "2"], client=local, out=io.StringIO(), err=err) == 0
    assert [a.checked for a in env.store.card("KD-1").acceptance] == [False, True]
    assert cab.main(["edit", "KD-1", "--check-ac", "3"], client=local, out=out, err=err) == 2 and "item 3" in err.getvalue()
    digi = client.LocalClient(env.ctx, env.principal("digi"))
    assert cab.main(["create", "x", "-p", "digipaddock"], client=digi, out=out, err=err) == 2  # root only


def test_kd_cabinet_closes_a_card_without_the_work_and_says_why(tmp_path):
    # a close without the work says why — the CLI had no way to say it and Cancelled was refused,
    # so junk cards were closed as Done. ``-s Cancelled`` is a plain cancel (as a person's manual move in the web),
    # ``--duplicate-of KD-m`` names the card that stays.
    env = CabinetEnv(tmp_path)
    junk, twin, kept, done = env.card(title="A"), env.card(title="B"), env.card(title="C"), env.card(status="Done")
    cab = _kd_cabinet()
    out, err = io.StringIO(), io.StringIO()
    local = client.LocalClient(env.ctx, env.principal("root"))
    assert cab.main(["edit", junk.id, "-s", "Cancelled", "-c", "лишняя"], client=local, out=out, err=err) == 0
    closed = env.store.card(junk.id)
    assert (closed.status, closed.outcome, closed.canonical) == ("Cancelled", "cancelled", "")
    assert out.getvalue() == f"{junk.id} · Cancelled · @dev · A\n" and env.store.messages(junk.id)[-1]["body"] == "лишняя"
    assert cab.main(["edit", twin.id, "--duplicate-of", kept.id], client=local, out=io.StringIO(), err=err) == 0
    dup = env.store.card(twin.id)
    assert (dup.status, dup.outcome, dup.canonical) == ("Cancelled", "duplicate", kept.id)
    # a card closed as Done by mistake is re-closed the right way (the board revision of 08.11.2026)
    assert cab.main(["edit", done.id, "-s", "Cancelled"], client=local, out=io.StringIO(), err=err) == 0
    assert (env.store.card(done.id).status, env.store.card(done.id).outcome) == ("Cancelled", "cancelled")
    # the refusals leave the card as it is
    assert cab.main(["edit", kept.id, "--duplicate-of", junk.id, "-s", "Done"], client=local, out=out, err=err) == 2
    assert "Cancelled" in err.getvalue()
    err = io.StringIO()
    assert cab.main(["edit", kept.id, "--duplicate-of", "KD-99"], client=local, out=out, err=err) == 2
    assert "KD-99" in err.getvalue()
    err = io.StringIO()
    assert cab.main(["edit", kept.id, "--duplicate-of", kept.id], client=local, out=out, err=err) == 2
    assert "itself" in err.getvalue()
    assert env.store.card(kept.id).status == "To Do" and env.store.card(kept.id).revision == 1
    # the socket operation itself: a canonical card is written only with the move into Cancelled
    with pytest.raises(client.CabinetError) as exc:
        local.call({"op": "card.edit", "card": kept.id, "revision": 1, "changes": {"status": "Done"}, "canonical": junk.id})
    assert exc.value.kind == "invalid" and "Cancelled" in str(exc.value)


def test_kd_cabinet_puts_a_card_off_until_a_time_and_wakes_it(tmp_path):
    # the owner got "Put off until…"; root had no way to do it for the maintainer's own cards (a step of one of them
    # waits a week) — ``kd-cabinet defer`` is the same operation, signed root
    from kd.cabinet import defers

    env = CabinetEnv(tmp_path)
    mine, done = env.card(title="Шаг E", assignee="@owner"), env.card(status="Done")
    cab = _kd_cabinet()
    out, err = io.StringIO(), io.StringIO()
    local = client.LocalClient(env.ctx, env.principal("root"))
    argv = ["defer", mine.id, "--until", "2026-10-20T09:00+03:00", "--reason", "неделя наблюдения"]
    assert cab.main(argv, client=local, out=out, err=err) == 0
    row = defers.active(env.store, mine.id)
    assert row["until"] == "2026-10-20T06:00:00Z" and row["by"] == "root" and row["reason"] == "неделя наблюдения"
    assert out.getvalue() == f"{mine.id} · To Do · @owner · Шаг E — put off until 2026-10-20T06:00:00Z\n"
    # the refusals: a closed card, a moment without its time zone, too far
    assert cab.main(["defer", done.id, "--until", "2026-10-20T09:00+03:00"], client=local, out=out, err=err) == 2
    assert "closed card" in err.getvalue()
    err = io.StringIO()
    assert cab.main(["defer", mine.id, "--until", "2026-10-20T09:00"], client=local, out=out, err=err) == 2
    assert "time zone" in err.getvalue()
    err = io.StringIO()
    assert cab.main(["defer", mine.id, "--until", "2027-02-01T09:00Z"], client=local, out=out, err=err) == 2
    assert "90 days" in err.getvalue()
    # --wake ends the defer now; a card not put off is refused
    out = io.StringIO()
    assert cab.main(["defer", mine.id, "--wake"], client=local, out=out, err=err) == 0
    assert defers.active(env.store, mine.id) is None and out.getvalue() == f"{mine.id} · To Do · @owner · Шаг E — woken\n"
    err = io.StringIO()
    assert cab.main(["defer", mine.id, "--wake"], client=local, out=out, err=err) == 2 and "not put off" in err.getvalue()
    # the flags go alone: --until or --wake, not both and not neither
    err = io.StringIO()
    assert cab.main(["defer", mine.id], client=local, out=out, err=err) == 2 and "--until" in err.getvalue()
    err = io.StringIO()
    assert cab.main(["defer", mine.id, "--wake", "--until", "2026-10-20T09:00Z"], client=local, out=out, err=err) == 2
    # a person stays a person: the operation keeps its own rights check
    digi = client.LocalClient(env.ctx, env.principal("digi"))
    assert cab.main(["defer", mine.id, "--until", "2026-10-20T09:00Z"], client=digi, out=out, err=io.StringIO()) == 2


def test_kd_cabinet_serve_takes_the_dev_flags():
    cab = _kd_cabinet()
    args = cab.parse(["serve"])
    assert not args.dev and args.db is None and args.listen == "127.0.0.1:8765"
    args = cab.parse(
        [
            "serve",
            "--dev",
            "--seed",
            "--db",
            "x.db",
            "--listen",
            "127.0.0.1:9000",
            "--origin",
            "http://localhost:9000",
            "--state-dir",
            "st",
        ]
    )
    assert (
        args.dev
        and args.seed
        and args.db == "x.db"
        and args.origin == "http://localhost:9000"
    )
    assert args.state_dir == "st"
    assert cab.dev_db(None).replace("\\", "/").endswith("tmp/cabinet-dev/cabinet.db")
    assert cab.dev_db("x.db") == "x.db"


def test_serve_keeps_the_board_up_when_the_web_has_no_settings(monkeypatch):
    # The rollout: units rendered (with the http socket) before cabinet.json/cabinet.env exist must not take the
    # board down for the agents — the local socket is served, the web stays off (cabinet_setup.sh check says so)
    cab = _kd_cabinet()
    seen = {}

    def no_settings():
        raise cab.config.ConfigError("no cabinet.json")

    async def serve_all(svc, **kwargs):
        seen.update(kwargs, svc=svc)

    monkeypatch.setattr(cab.server, "listen_sockets", lambda: {"local": "L", "http": "H"})
    monkeypatch.setattr(cab.config, "load", no_settings)
    monkeypatch.setattr(cab.server, "build", lambda *a, **kw: ("svc", kw["cfg"]))
    monkeypatch.setattr(cab.server, "serve_all", serve_all)
    err = io.StringIO()
    assert cab.main(["serve"], out=io.StringIO(), err=err) == 0
    assert seen["svc"] == ("svc", None) and seen["local_sock"] == "L"
    assert seen["http_sock"] is None and "the web is off" in err.getvalue()


def test_kd_cabinet_user_commands_go_through_the_socket(tmp_path):
    env = CabinetEnv(tmp_path)
    cab = _kd_cabinet()
    local = client.LocalClient(env.ctx, env.principal("root"))
    out, err = io.StringIO(), io.StringIO()
    argv = [
        "user",
        "add",
        "anna",
        "--name",
        "Анна",
        "--role",
        "partner",
        "--projects",
        "web,digipaddock",
    ]
    assert cab.main(argv, client=local, out=out, err=err) == 0, err.getvalue()
    code = out.getvalue().strip().split()[-1]
    assert env.users.redeem_code("anna", code, env.now)
    assert env.users.assignments("anna") == [("partner", ["digipaddock", "web"])]
    out = io.StringIO()
    assert cab.main(["user", "code", "anna"], client=local, out=out, err=err) == 0
    assert env.users.redeem_code("anna", out.getvalue().strip().split()[-1], env.now)
    out = io.StringIO()
    assert cab.main(["user", "list"], client=local, out=out, err=err) == 0
    assert "anna" in out.getvalue() and "partner" in out.getvalue()
    assert (
        cab.main(["user", "block", "anna"], client=local, out=io.StringIO(), err=err)
        == 0
    )
    assert env.users.get("anna")["status"] == "blocked"
    assert (
        cab.main(
            ["user", "add", "Bad Id", "--name", "x", "--role", "viewer"],
            client=local,
            out=io.StringIO(),
            err=err,
        )
        == 2
    )
    assert (
        cab.main(
            ["user", "add", "boris", "--name", "Борис", "--role", "owner"],
            client=local,
            out=io.StringIO(),
            err=err,
        )
        == 0
    )
    assert env.users.assignments("boris") == [("owner", "*")]


def test_kd_cabinet_pipeline_status_prints_the_line_setup_checks(tmp_path):
    env = CabinetEnv(tmp_path)
    cab = _kd_cabinet()
    out, err = io.StringIO(), io.StringIO()
    local = client.LocalClient(env.ctx, env.principal("root"))
    assert cab.main(["pipeline", "status"], client=local, out=out, err=err) == 0
    lines = out.getvalue().splitlines()
    assert "protocol: 1" in lines and "mode: observe" in lines  # pipeline_setup.sh check greps the first


def test_release_subcommand_writes_root_request(tmp_path, monkeypatch):
    """root asks kd-release for main as it is; the repository comes from root's registry."""
    cab = _kd_cabinet()
    release, repos = tmp_path / "release", tmp_path / "repos.json"
    release.mkdir()
    repos.write_text(json.dumps({"digipaddock": {"repo": "example/digipaddock", "dir": "work/x", "release": "digipaddock"},
                                 "api": {"repo": "example/api", "dir": "work/api"}}), encoding="utf-8")
    monkeypatch.setattr(cab, "RELEASE_DIR", str(release))
    monkeypatch.setattr(cab, "REPOS", str(repos))
    out, err = io.StringIO(), io.StringIO()
    assert cab.main(["release", "digipaddock"], out=out, err=err) == 0, err.getvalue()
    request = json.loads((release / "request.json").read_text(encoding="utf-8"))
    assert {k: request[k] for k in ("project", "repo", "requester")} == {
        "project": "digipaddock", "repo": "example/digipaddock", "requester": "root"}
    assert "sha" not in request and "pr" not in request and float(request["ts"]) > 0
    assert "release request main queued" in out.getvalue()
    assert f"result-{request['request_id']}.json" in out.getvalue()
    assert [p.name for p in release.iterdir() if p.name.startswith(".request.")] == []
    assert cab.main(["release", "digipaddock"], out=io.StringIO(), err=err) == 1  # the earlier one is not taken yet
    (release / "request.json").unlink()
    assert cab.main(["release", "api"], out=io.StringIO(), err=err) == 2  # not a released project
    assert cab.main(["release", "nope"], out=io.StringIO(), err=err) == 2


@pytest.mark.parametrize("rev,rc", [("v1.2.0", 0), ("c" * 40, 0), ("abc1234", 0), ("main", 2), ("v1..2", 2),
                                    ("-v1", 2), ("refs/tags/v1", 2)])
def test_cabinet_release_rev_writes_it_into_the_request(tmp_path, monkeypatch, rev, rc):
    """root names a commit or a tag v... of the code repository; kd-release checks it is on main."""
    cab = _kd_cabinet()
    release, repos = tmp_path / "release", tmp_path / "repos.json"
    release.mkdir()
    repos.write_text(json.dumps({"digipaddock": {"repo": "example/digipaddock", "dir": "work/x",
                                                 "release": "digipaddock"}}), encoding="utf-8")
    monkeypatch.setattr(cab, "RELEASE_DIR", str(release))
    monkeypatch.setattr(cab, "REPOS", str(repos))
    out, err = io.StringIO(), io.StringIO()
    assert cab.main(["release", "digipaddock", "--rev", rev], out=out, err=err) == rc, err.getvalue()
    if rc:
        assert not (release / "request.json").exists() and "--rev" in err.getvalue()
        return
    assert json.loads((release / "request.json").read_text(encoding="utf-8"))["rev"] == rev
    assert rev in out.getvalue()


def test_release_lock_root_creates_is_the_directory_owners(tmp_path, monkeypatch):
    """Review I1: the forge executor (kd-merge --release) locks the same release/.lock — created by root's first
    release, it goes to the directory's owner; an existing lock is left as it is."""
    cab = _kd_cabinet()
    release, repos = tmp_path / "release", tmp_path / "repos.json"
    release.mkdir()
    repos.write_text(json.dumps({"digipaddock": {"repo": "example/digipaddock", "dir": "w", "release": "digipaddock"}}),
                     encoding="utf-8")
    monkeypatch.setattr(cab, "RELEASE_DIR", str(release))
    monkeypatch.setattr(cab, "REPOS", str(repos))
    chowned = []
    monkeypatch.setattr(cab.os, "fchown", lambda fd, uid, gid: chowned.append((uid, gid)), raising=False)
    err = io.StringIO()
    assert cab.main(["release", "digipaddock"], out=io.StringIO(), err=err) == 0, err.getvalue()
    assert (release / ".lock").is_file() and chowned == [(os.stat(release).st_uid, -1)]
    (release / "request.json").unlink()
    chowned.clear()
    assert cab.main(["release", "digipaddock"], out=io.StringIO(), err=err) == 0, err.getvalue()
    assert chowned == []
    # re-review M2: a lock someone else owns (root's, from an older kd-cabinet) goes to the directory's owner too
    (release / "request.json").unlink()
    real_stat = os.stat
    monkeypatch.setattr(cab.os, "stat", lambda p, *a, **kw: types.SimpleNamespace(st_uid=1234)
                        if str(p) == str(release) else real_stat(p, *a, **kw))
    assert cab.main(["release", "digipaddock"], out=io.StringIO(), err=err) == 0, err.getvalue()
    assert chowned == [(1234, -1)]


def test_release_lock_that_is_not_a_plain_file_is_refused(tmp_path, monkeypatch):
    """Re-review M4: a directory (or a FIFO, a link) in place of release/.lock — a line, not a hang or a traceback."""
    cab = _kd_cabinet()
    release, repos = tmp_path / "release", tmp_path / "repos.json"
    release.mkdir()
    (release / ".lock").mkdir()
    repos.write_text(json.dumps({"digipaddock": {"repo": "example/digipaddock", "dir": "w", "release": "digipaddock"}}),
                     encoding="utf-8")
    monkeypatch.setattr(cab, "RELEASE_DIR", str(release))
    monkeypatch.setattr(cab, "REPOS", str(repos))
    err = io.StringIO()
    assert cab.main(["release", "digipaddock"], out=io.StringIO(), err=err) == 1
    assert "kd-cabinet: release lock" in err.getvalue() and not (release / "request.json").exists()
    if hasattr(os, "mkfifo"):
        (release / ".lock").rmdir()
        os.mkfifo(release / ".lock")
        err = io.StringIO()
        assert cab.main(["release", "digipaddock"], out=io.StringIO(), err=err) == 1
        assert "kd-cabinet: release lock" in err.getvalue()


def test_release_lock_with_a_second_link_is_refused(tmp_path, monkeypatch):
    """Re-review 3, M2: a hard link of another file in place of release/.lock is never handed to the directory's
    owner — root would give away that file."""
    cab = _kd_cabinet()
    release, repos = tmp_path / "release", tmp_path / "repos.json"
    release.mkdir()
    other = tmp_path / "other"
    other.write_text("root's", encoding="utf-8")
    os.link(other, release / ".lock")
    repos.write_text(json.dumps({"digipaddock": {"repo": "example/digipaddock", "dir": "w", "release": "digipaddock"}}),
                     encoding="utf-8")
    monkeypatch.setattr(cab, "RELEASE_DIR", str(release))
    monkeypatch.setattr(cab, "REPOS", str(repos))
    chowned = []
    monkeypatch.setattr(cab.os, "fchown", lambda fd, uid, gid: chowned.append((uid, gid)), raising=False)
    err = io.StringIO()
    assert cab.main(["release", "digipaddock"], out=io.StringIO(), err=err) == 1
    assert "kd-cabinet: release lock" in err.getvalue() and chowned == [] and not (release / "request.json").exists()


@pytest.mark.skipif(os.name == "nt", reason="a symlink and its mode: POSIX, run on the Linux host")
def test_roots_request_sets_its_mode_on_the_descriptor_not_through_a_swapped_name(tmp_path, monkeypatch):
    """The release directory is the forge user's: it may swap root's temporary request for a link to a root file
    between the write and the rename. Root's mode goes on the file it wrote, never on what the name points to."""
    from kd import atomic_file

    cab = _kd_cabinet()
    release, repos = tmp_path / "release", tmp_path / "repos.json"
    release.mkdir()
    repos.write_text(json.dumps({"digipaddock": {"repo": "example/digipaddock", "dir": "w", "release": "digipaddock"}}),
                     encoding="utf-8")
    victim = tmp_path / "root-secret"
    victim.write_text("secret", encoding="utf-8")
    victim.chmod(0o600)
    monkeypatch.setattr(cab, "RELEASE_DIR", str(release))
    monkeypatch.setattr(cab, "REPOS", str(repos))
    monkeypatch.setattr(cab.os, "fchown", lambda fd, uid, gid: None, raising=False)
    real_fsync = os.fsync

    def swap(fd):  # the directory's owner, between root's write and its rename
        real_fsync(fd)
        for name in os.listdir(release):
            if name.startswith(".request.json.") and name.endswith(".tmp"):
                os.unlink(release / name)
                os.symlink(victim, release / name)

    monkeypatch.setattr(atomic_file.os, "fsync", swap)
    err = io.StringIO()
    cab.main(["release", "digipaddock"], out=io.StringIO(), err=err)
    assert victim.stat().st_mode & 0o777 == 0o600 and victim.read_text(encoding="utf-8") == "secret"


def test_atomic_replace_sets_the_mode_it_was_given(tmp_path):
    from kd import atomic_file

    atomic_file.replace(str(tmp_path / "f.json"), b"{}", 0o640)
    assert (tmp_path / "f.json").read_bytes() == b"{}"
    if os.name != "nt":
        assert (tmp_path / "f.json").stat().st_mode & 0o777 == 0o640


def test_release_rollback_hands_over_to_kd_release(monkeypatch):
    cab = _kd_cabinet()
    seen = []
    monkeypatch.setattr(cab.os, "execv", lambda path, argv: seen.append((path, argv)))
    cab.main(["release", "digipaddock", "--rollback"], out=io.StringIO(), err=io.StringIO())
    cab.main(["release", "digipaddock", "--rollback", "/opt/digipaddock-releases/abc"], out=io.StringIO(),
             err=io.StringIO())
    assert seen == [("/usr/local/bin/kd-release", ["kd-release", "--rollback"]),
                    ("/usr/local/bin/kd-release", ["kd-release", "--rollback", "/opt/digipaddock-releases/abc"])]
