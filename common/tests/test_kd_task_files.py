"""an agent's files — ids in kd-task view, thread and chat; kd-task file get|add past the 1 MiB socket."""
import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_kd_task import ANALYST, Env  # noqa: E402

from kd import session_config  # noqa: E402
from kd.cabinet import api, files_api, server  # noqa: E402

MB = 1024 * 1024


@pytest.fixture
def env(tmp_path, kd_task):
    e = Env(tmp_path, kd_task)
    e.files = tmp_path / "files"
    e.files.mkdir()
    e.cab.ctx.files_dir = str(e.files)
    api.install_host(e.cab.ctx, lambda: session_config.HostConfig({"analyst": 1, "dev": 1}, 600))
    e.tmp = e.agents_root / "dev" / "work" / "tmp"
    e.tmp.mkdir(parents=True)
    (e.agents_root / "analyst" / "work" / "tmp").mkdir(parents=True)
    return e


def put(env, data: bytes, *, card=None, chat=None, name="shot.png", mime="image/png", text="", author="user:owner"):
    """A person's message with one file, its bytes where the web keeps them."""
    file_id = hashlib.sha256(data + name.encode()).hexdigest()[:32]
    (env.files / file_id).write_bytes(data)
    file = {"id": file_id, "name": name, "mime": mime, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    fields = api.message_fields(text, [file])
    if chat:
        env.cab.store.add_thread_message(env.cab.store.agent_thread(chat), author=author, **fields, attachments=[file])
    else:
        env.cab.store.add_message(card, author=author, **fields, attachments=[file])
    return file


def get(env, file_id, *, into=None, as_env=None):
    path = into or env.tmp / "copy"
    with open(path, "wb") as handle:
        rc, out, err = run(env, "file", "get", file_id, data_out=handle, as_env=as_env)
    return rc, path, err


def run(env, *argv, data_in=None, data_out=None, as_env=None):
    out, err = io.StringIO(), io.StringIO()
    rc = env.mod.main(list(argv), env=as_env or env.env, client=env.client, registry_path=str(env.cab.registry),
                      repo_root=str(env.repo), require_root_owner=False, agents_root=str(env.agents_root), out=out,
                      err=err, data_in=data_in, data_out=data_out)
    return rc, out.getvalue(), err.getvalue()


def test_view_thread_and_chat_show_each_files_id_type_and_size(env):
    card = env.cab.card(project="digipaddock", assignee="@dev", status="In Progress")
    shot = put(env, b"\x89PNG" + b"\0" * 2044, card=card.id, text="Что на скриншоте?")
    log = put(env, b"x" * 10, card=card.id, name="a.log", mime="text/plain")
    line = f"file {shot['id']} · shot.png · image/png · 2 KB"
    rc, out, _ = run(env, "view", card.id)
    assert rc == 0 and line in out and f"file {log['id']} · a.log · text/plain · 10 B" in out
    assert "Что на скриншоте?" in out
    assert line in run(env, "thread", card.id)[1]
    seq = env.cab.store.messages(card.id)[-2]["seq"]
    assert line in run(env, "message", card.id, str(seq))[1]
    put(env, b"y" * 3000, chat="analyst", name="notes.txt", mime="text/plain", text="прочитай")
    rc, out, _ = run(env, "chat", as_env=ANALYST)
    assert rc == 0 and "прочитай" in out and "· notes.txt · text/plain · 3 KB" in out


def test_old_file_messages_still_show(env):
    card = env.cab.card(project="digipaddock")
    env.cab.store.add_message(card.id, author="user:owner", kind="file", body="old.txt",
                              payload={"id": "c" * 32, "name": "old.txt", "mime": "text/plain", "size": 3})
    rc, out, err = run(env, "view", card.id)
    assert rc == 0, err
    assert f"file {'c' * 32} · old.txt · text/plain · 3 B" in out and "file: old.txt" not in out


def test_get_a_20_mb_file_into_work_tmp(env):
    card = env.cab.card(project="digipaddock")
    data = os.urandom(20 * MB)
    file = put(env, data, card=card.id, name="big.bin", mime="application/octet-stream")
    rc, path, err = get(env, file["id"])
    assert rc == 0, err
    assert hashlib.sha256(path.read_bytes()).hexdigest() == file["sha256"]
    assert f"ok file {file['id']} {20 * MB} bytes" in err
    assert json.loads(run(env, "file", "info", file["id"])[1])["name"] == "big.bin"


def test_get_refuses_another_agents_chat_and_a_copy_outside_work_tmp(env, tmp_path):
    theirs = put(env, b"secret", chat="analyst", name="s.txt", mime="text/plain", text="x")
    rc, _, err = get(env, theirs["id"])
    assert rc != 0 and "no such file" in err
    assert get(env, theirs["id"], as_env=ANALYST, into=env.agents_root / "analyst" / "work" / "tmp" / "s")[0] == 0
    card = env.cab.card(project="digipaddock")
    mine = put(env, b"ok", card=card.id, name="a.txt", mime="text/plain")
    rc, _, err = get(env, mine["id"], into=tmp_path / "elsewhere")
    assert rc == 2 and "work/tmp" in err
    rc, _, err = get(env, mine["id"], into=env.agents_root / "analyst" / "work" / "tmp" / "x")  # not dev's own
    assert rc == 2
    assert run(env, "file", "get", "nothex")[0] == 2


def test_add_a_20_mb_file_from_work_tmp_with_text(env):
    card = env.cab.card(project="digipaddock", assignee="@dev", status="In Progress")
    data = os.urandom(20 * MB)
    (env.tmp / "report.pdf").write_bytes(data)
    with open(env.tmp / "report.pdf", "rb") as handle:
        rc, out, err = run(env, "file", "add", card.id, "--name", "report.pdf", "--text", "Отчёт", data_in=handle)
    assert rc == 0, err
    message = env.cab.store.messages(card.id)[-1]
    [file] = message["payload"]["files"]
    assert out == f"ok {card.id} file {file['id']} #{message['seq']} {20 * MB} bytes\n"
    assert message["author"] == "agent:dev" and message["body"] == "[dev] Отчёт" and file["mime"] == "application/pdf"
    stored = env.cab.store.attachment(file["id"])
    assert stored["sha256"] == hashlib.sha256(data).hexdigest() == hashlib.sha256((env.files / file["id"]).read_bytes()).hexdigest()
    assert list((env.files / files_api.UPLOADS).iterdir()) == []  # the upload became the file
    assert "mentions" not in [e for e in env.cab.store.events_since(0) if e["kind"] == "message.added"][-1]["payload"]
    # and back: the agent reads its own file
    rc, path, _ = get(env, file["id"])
    assert rc == 0 and path.read_bytes() == data


def test_add_refuses_a_file_outside_work_tmp_a_chat_and_the_quota(env, tmp_path, monkeypatch):
    card = env.cab.card(project="digipaddock")
    outside = tmp_path / "x.txt"
    outside.write_bytes(b"x")
    with open(outside, "rb") as handle:
        rc, _, err = run(env, "file", "add", card.id, "--name", "x.txt", data_in=handle)
    assert rc == 2 and "work/tmp" in err
    (env.tmp / "a.txt").write_bytes(b"abc")
    with open(env.tmp / "a.txt", "rb") as handle:
        assert run(env, "file", "add", "chat", "--name", "a.txt", data_in=handle)[0] == 2  # a card, never a chat
    monkeypatch.setattr(api, "FILES_USER_MAX", 2)
    with open(env.tmp / "a.txt", "rb") as handle:
        rc, _, err = run(env, "file", "add", card.id, "--name", "a.txt", data_in=handle)
    assert rc == 2 and "remove some" in err
    assert [m for m in env.cab.store.messages(card.id) if m["author"] == "agent:dev"] == []
    assert [p.name for p in env.files.iterdir()] == [files_api.UPLOADS]
    assert list((env.files / files_api.UPLOADS).iterdir()) == []  # the refused upload is gone, not held an hour


def test_a_refused_add_frees_its_upload_and_bad_text_is_refused_before_upload(env):
    card = env.cab.card(project="digipaddock")
    (env.tmp / "a.txt").write_bytes(b"abc")
    for _ in range(files_api.UPLOADS_PER_AGENT + 1):  # never locked out by its own refusals
        with open(env.tmp / "a.txt", "rb") as handle:
            rc, _, err = run(env, "file", "add", card.id, "--name", "a.txt", "--text", "- итог", data_in=handle)
        assert rc == 2 and "may not start with -" in err
    assert not (env.files / files_api.UPLOADS).exists() or list((env.files / files_api.UPLOADS).iterdir()) == []
    up = env.client.call({"op": "agent.file.chunk", "agent": "dev", "offset": 0, "data": "YWJj"})["upload"]
    with pytest.raises(Exception):
        env.client.call({"op": "agent.file.add", "agent": "dev", "card": card.id, "upload": up, "name": "a.txt",
                         "mime": "text/plain", "size": 3, "sha256": "0" * 64})  # not the upload's digest
    assert list((env.files / files_api.UPLOADS).iterdir()) == []
    with open(env.tmp / "a.txt", "rb") as handle:
        assert run(env, "file", "add", card.id, "--name", "a.txt", "--text", "итог", data_in=handle)[0] == 0


def test_the_coordinator_reads_files_but_never_uploads(env):
    card = env.cab.card(project="digipaddock")
    file = put(env, b"boss can read", card=card.id, name="a.txt", mime="text/plain")
    assert env.client.call({"op": "agent.file.info", "agent": "boss", "id": file["id"]})["file"]["name"] == "a.txt"
    with pytest.raises(Exception) as exc:
        env.client.call({"op": "agent.file.chunk", "agent": "boss", "offset": 0, "data": "YWJj"})
    assert getattr(exc.value, "kind", "") == "refused"


def test_copy_names_and_types(kd_task, env):
    name = kd_task.copy_name("a" * 32, "ж" * 200 + ".png")
    assert len(name.encode("utf-8")) <= 255 and name.startswith("aaaaaaaa-ж")
    assert kd_task.copy_name("b" * 32, "../.hidden") == "bbbbbbbb-hidden"
    card = env.cab.card(project="digipaddock")
    for name, mime in (("a.tar.gz", "application/gzip"), ("shot.png", "image/png"), ("x.weird-ext", "application/octet-stream")):
        (env.tmp / name).write_bytes(b"1")
        with open(env.tmp / name, "rb") as handle:
            assert run(env, "file", "add", card.id, "--name", name, data_in=handle)[0] == 0
        assert env.cab.store.messages(card.id)[-1]["payload"]["files"][0]["mime"] == mime, name


def test_add_refuses_more_than_20_mb(env):
    card = env.cab.card(project="digipaddock")
    (env.tmp / "big.bin").write_bytes(b"\0" * (20 * MB + 1))
    with open(env.tmp / "big.bin", "rb") as handle:
        rc, _, err = run(env, "file", "add", card.id, "--name", "big.bin", data_in=handle)
    assert rc != 0 and "20 MB" in err
    assert [m for m in env.cab.store.messages(card.id) if m["author"] == "agent:dev"] == []


def test_file_ops_need_the_executor_and_a_valid_upload(env):
    owner = api.Principal("user", -1, "owner")
    for op in ("agent.file.info", "agent.file.read", "agent.file.chunk", "agent.file.add"):
        with pytest.raises(api.ApiError):
            api.dispatch(env.cab.ctx, owner, {"op": op, "agent": "dev"})
    first = env.client.call({"op": "agent.file.chunk", "agent": "dev", "offset": 0, "data": "YWJj"})
    with pytest.raises(Exception) as exc:  # the same piece again: the offset moved on
        env.client.call({"op": "agent.file.chunk", "agent": "dev", "upload": first["upload"], "offset": 0, "data": "YWJj"})
    assert getattr(exc.value, "kind", "") == "conflict"
    with pytest.raises(Exception) as exc:  # another agent's upload is not there for it
        env.client.call({"op": "agent.file.chunk", "agent": "analyst", "upload": first["upload"], "offset": 3,
                         "data": "YWJj"})
    assert getattr(exc.value, "kind", "") == "not_found"
    for _ in range(files_api.UPLOADS_PER_AGENT - 1):
        env.client.call({"op": "agent.file.chunk", "agent": "dev", "offset": 0, "data": "YWJj"})
    with pytest.raises(Exception) as exc:
        env.client.call({"op": "agent.file.chunk", "agent": "dev", "offset": 0, "data": "YWJj"})
    assert getattr(exc.value, "kind", "") == "refused"


def test_a_piece_fits_one_socket_request():
    request = {"op": "agent.file.chunk", "agent": "dev", "upload": "a" * 32, "offset": api.FILE_MAX,
               "data": "A" * (4 * ((files_api.CHUNK_MAX + 2) // 3))}
    assert len(json.dumps(request).encode()) + 1 < server.MAX_REQUEST


def test_front_opens_the_copy_in_work_tmp_and_hands_it_over(kd_task, tmp_path):
    tmp = tmp_path / "work" / "tmp"
    tmp.mkdir(parents=True)
    calls = []

    def fake(args, stdout=None, stderr=None, stdin=None):
        assert args[:4] == [kd_task.cap_client.CLIENT, "run", "base", kd_task.EXECUTOR]
        calls.append(args[4:])
        if args[4:6] == ["file", "info"]:
            return subprocess.CompletedProcess(args, 0, json.dumps({"name": "../a b.png", "mime": "image/png",
                                                                     "size": 3}).encode(), b"")
        if args[4:6] == ["file", "get"]:
            stdout.write(b"abc")
            return subprocess.CompletedProcess(args, 0, None, b"")
        return subprocess.CompletedProcess(args, 0, None, None)

    out, err = io.StringIO(), io.StringIO()
    file_id = "f" * 32
    assert kd_task.front_file(["file", "get", file_id], tmp=str(tmp), run=fake, out=out, err=err) == 0
    copy = tmp / f"{'f' * 8}-a b.png"
    assert copy.read_bytes() == b"abc" and str(copy) in out.getvalue()
    (tmp / "r.txt").write_text("r")
    assert kd_task.front_file(["file", "add", "KD-5", "r.txt", "--text", "вот"], tmp=str(tmp), run=fake,
                              cwd=str(tmp), out=out, err=err) == 0
    assert calls[-1] == ["file", "add", "KD-5", "--name", "r.txt", "--text", "вот"]
    outside = tmp_path / "o.txt"
    outside.write_text("o")
    assert kd_task.front_file(["file", "add", "KD-5", str(outside)], tmp=str(tmp), run=fake, out=out, err=err) == 2
    (tmp / "link.txt").symlink_to(outside)
    assert kd_task.front_file(["file", "add", "KD-5", "link.txt"], tmp=str(tmp), run=fake, cwd=str(tmp),
                              out=out, err=err) == 2
