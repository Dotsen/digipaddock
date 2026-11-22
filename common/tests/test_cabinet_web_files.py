"""a message's text and files in one upload, all or nothing; picture previews; deletion over HTTP."""

import hashlib
from pathlib import Path

import pytest
from web_env import WebEnv

from kd import session_config
from kd.cabinet import api

HOSTED = session_config.HostConfig({"analyst": 1}, 600)
MB = 1024 * 1024
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


@pytest.fixture
def w(tmp_path):
    w = WebEnv(tmp_path)
    api.install_host(w.env.ctx, lambda: HOSTED)
    w.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    w.users.create("anna", "Анна", roles=[("partner", ["web"])], actor="root")
    w.cfg = w.svc.cfg = type(w.cfg)(**{**w.cfg.__dict__, "files_dir": str(tmp_path / "files")})
    w.login()
    return w


def _as(w, user):
    browser = w.other_client()
    me = w.login(user, client=browser)
    return browser, w.headers(me["csrf"])


def _card(w, project="web"):
    return w.client.post("/api/cards", headers=w.headers(), json={"title": "Скриншоты", "project": project}).json()


def _stored(w):
    folder = Path(w.cfg.files_dir)
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


def _send(w, path, files, text=None, client=None, headers=None, key=None):
    data = {} if text is None else {"text": text}
    if key:
        data["client_key"] = key
    return (client or w.client).post(
        path, headers=headers or w.headers(), data=data, files=[("file", f) for f in files]
    )


def test_text_and_several_files_are_one_message(w):
    card = _card(w)
    r = _send(w, f"/api/cards/{card['id']}/files",
              [("shot.png", PNG, "image/png"), ("notes.txt", b"hello", "text/plain")], text="Что тут не так?")
    assert r.status_code == 200, r.text
    message = r.json()
    assert message["kind"] == "text" and message["body"] == "Что тут не так?" and message["author_name"] == "Владелец"
    assert [(f["name"], f["mime"], f["size"]) for f in message["payload"]["files"]] == [
        ("shot.png", "image/png", len(PNG)), ("notes.txt", "text/plain", 5)]
    assert sorted(f["id"] for f in message["payload"]["files"]) == _stored(w)
    row = w.env.store.attachment(message["payload"]["files"][1]["id"])
    assert row["sha256"] == hashlib.sha256(b"hello").hexdigest()
    [shown] = [m for m in w.client.get(f"/api/cards/{card['id']}/messages").json()["messages"] if m["seq"] == message["seq"]]
    assert shown["payload"]["files"] == message["payload"]["files"]


def test_a_file_of_20_mb_is_taken_and_a_byte_more_is_413(w):
    card = _card(w)
    ok = _send(w, f"/api/cards/{card['id']}/files", [("a.bin", b"\0" * (20 * MB), "application/octet-stream")])
    assert ok.status_code == 200, ok.text
    over = _send(w, f"/api/cards/{card['id']}/files", [("b.bin", b"\0" * (20 * MB + 1), "application/octet-stream")])
    assert over.status_code == 413 and over.json()["error"] == "too_large"
    assert _stored(w) == [ok.json()["payload"]["files"][0]["id"]]


def test_a_message_of_100_mb_is_taken_and_more_is_413(w):
    card = _card(w)
    five = [(f"{n}.bin", b"\0" * (20 * MB), "application/octet-stream") for n in range(5)]
    ok = _send(w, f"/api/cards/{card['id']}/files", five, text="сто мегабайт")
    assert ok.status_code == 200, ok.text
    assert len(_stored(w)) == 5 and w.env.store.files_size() == 100 * MB
    over = _send(w, f"/api/cards/{card['id']}/files", five + [("6.bin", b"\0", "application/octet-stream")], text="x")
    assert over.status_code == 413 and len(_stored(w)) == 5
    assert len(w.env.store.messages(card["id"])) == 1  # the refused one is not posted


def test_a_refusal_posts_nothing_and_removes_every_byte(w, monkeypatch):
    card = _card(w)
    good = ("a.png", PNG, "image/png")
    # the third file is over the limit: the two before it are already on disk and go
    r = _send(w, f"/api/cards/{card['id']}/files", [good, good, ("big.bin", b"\0" * (20 * MB + 1), "x/y")], text="три")
    assert r.status_code == 413 and _stored(w) == []
    # a quota refused after the upload: the same
    monkeypatch.setattr(api, "FILES_USER_MAX", 10)
    r = _send(w, f"/api/cards/{card['id']}/files", [good], text="квота")
    assert r.status_code == 403 and _stored(w) == []
    assert [m for m in w.env.store.messages(card["id"]) if m["author"] == "user:owner"] == []
    # a card out of sight: 404 before anything is read
    anna, headers = _as(w, "anna")
    secret = _card(w, project="digipaddock")
    r = _send(w, f"/api/cards/{secret['id']}/files", [good], text="x", client=anna, headers=headers)
    assert r.status_code == 404 and _stored(w) == []


def test_too_many_files_and_a_broken_body(w):
    card = _card(w)
    many = [("a.txt", b"x", "text/plain")] * (api.FILES_PER_MESSAGE + 1)
    r = _send(w, f"/api/cards/{card['id']}/files", many)
    assert r.status_code == 400 and _stored(w) == []
    r = w.client.post(f"/api/cards/{card['id']}/files", headers={**w.headers(), "Content-Type": "application/json"},
                      content=b"{}")
    assert r.status_code == 400
    r = w.client.post(f"/api/cards/{card['id']}/files", headers=w.headers(), data={"text": " "})
    assert r.status_code == 400  # neither text nor a file


def test_a_repeated_send_keeps_the_first_message_and_its_files(w):
    card = _card(w)
    first = _send(w, f"/api/cards/{card['id']}/files", [("a.txt", b"1", "text/plain")], text="раз", key="send-0001")
    again = _send(w, f"/api/cards/{card['id']}/files", [("a.txt", b"1", "text/plain")], text="раз", key="send-0001")
    assert again.json()["seq"] == first.json()["seq"]
    assert _stored(w) == [first.json()["payload"]["files"][0]["id"]]


def test_files_in_the_agent_chat(w):
    r = _send(w, "/api/agents/analyst/files", [("shot.png", PNG, "image/png")], text="Что на скриншоте?")
    assert r.status_code == 200, r.text
    message = r.json()
    assert message["author"] == "user:owner" and message["payload"]["files"][0]["name"] == "shot.png"
    assert [c["kind"] for c in w.env.store.commands(agent="analyst")] == ["chat"]
    quiet = _send(w, "/api/agents/analyst/files", [("b.png", PNG, "image/png")])
    assert quiet.json()["kind"] == "file" and len(w.env.store.commands(agent="analyst")) == 1
    got = w.client.get(f"/api/files/{message['payload']['files'][0]['id']}?inline=1")
    assert got.status_code == 200 and got.content == PNG
    anna, headers = _as(w, "anna")  # a partner of ops alone: no agents.chat over every project
    assert anna.get(f"/api/files/{message['payload']['files'][0]['id']}").status_code == 404
    r = _send(w, "/api/agents/analyst/files", [("c.png", PNG, "image/png")], text="x", client=anna, headers=headers)
    assert r.status_code == 403 and len(_stored(w)) == 2


def test_a_chat_upload_is_refused_before_its_body_is_read(w, monkeypatch):
    from kd.cabinet.web import files

    def never(*args, **kwargs):
        raise AssertionError("the body was read")

    monkeypatch.setattr(files, "receive", never)
    anna, headers = _as(w, "anna")  # no agents.chat
    assert _send(w, "/api/agents/analyst/files", [("a.png", PNG, "image/png")], client=anna, headers=headers).status_code == 403
    assert _send(w, "/api/agents/dev/files", [("a.png", PNG, "image/png")]).status_code == 403  # not on the host
    assert _send(w, "/api/agents/ghost/files", [("a.png", PNG, "image/png")]).status_code == 404
    assert _stored(w) == []


def test_only_raster_pictures_open_inline(w):
    card = _card(w)
    sent = _send(w, f"/api/cards/{card['id']}/files", [
        ("a.png", PNG, "image/png"), ("b.jpg", b"\xff\xd8\xff", "image/jpeg"), ("c.gif", b"GIF89a", "image/gif"),
        ("d.webp", b"RIFF0000WEBP", "image/webp"),
        ("e.svg", b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>", "image/svg+xml"),
        ("f.html", b"<script>alert(1)</script>", "text/html"), ("g.txt", b"hi", "text/plain"),
    ]).json()["payload"]["files"]
    for f in sent:
        got = w.client.get(f"/api/files/{f['id']}", params={"inline": 1})
        assert got.status_code == 200
        assert got.headers["x-content-type-options"] == "nosniff"
        assert got.headers["content-security-policy"] == "default-src 'none'; sandbox"
        disposition = got.headers["content-disposition"].split(";")[0]
        if f["mime"] in ("image/png", "image/jpeg", "image/gif", "image/webp"):
            assert disposition == "inline" and got.headers["content-type"] == f["mime"], f
        else:
            assert disposition == "attachment", f
        assert w.client.get(f"/api/files/{f['id']}").headers["content-disposition"].startswith("attachment;")
    svg = next(f for f in sent if f["name"] == "e.svg")
    assert w.client.get(f"/api/files/{svg['id']}", params={"inline": 1}).headers["content-type"] == "application/octet-stream"


WEBM = b"\x1aE\xdf\xa3" + bytes(range(256)) * 64  # an EBML header and bytes no transcoding would keep


def test_a_voice_recording_plays_inline_and_comes_back_byte_for_byte(w):
    # the recorder's file as the browser sends it (MediaRecorder: audio/webm;codecs=opus)
    card = _card(w)
    r = _send(w, f"/api/cards/{card['id']}/files", [("voice-20261020-213000.webm", WEBM, "audio/webm;codecs=opus")])
    assert r.status_code == 200, r.text
    [voice] = r.json()["payload"]["files"]
    assert voice["mime"] == "audio/webm" and voice["size"] == len(WEBM)
    assert w.env.store.attachment(voice["id"])["sha256"] == hashlib.sha256(WEBM).hexdigest()
    assert Path(w.cfg.files_dir, voice["id"]).read_bytes() == WEBM  # stored as sent, not transcoded
    got = w.client.get(f"/api/files/{voice['id']}", params={"inline": 1})
    assert got.status_code == 200 and got.content == WEBM
    assert got.headers["content-type"] == "audio/webm"
    assert got.headers["content-disposition"].startswith("inline;")
    assert got.headers["x-content-type-options"] == "nosniff"
    assert got.headers["content-security-policy"] == "default-src 'none'; sandbox"
    # a player seeks with ranges (Safari asks for one before it plays)
    part = w.client.get(f"/api/files/{voice['id']}", params={"inline": 1}, headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and part.content == WEBM[:100]
    assert w.client.get(f"/api/files/{voice['id']}").headers["content-disposition"].startswith("attachment;")


def test_the_whisper_probe_collects_recordings_from_the_thread_as_they_are(w, tmp_path):
    # The owner records thirteen voice messages in a card's thread; the probe's collect
    # (root on the factory host) copies them by their attachment rows from the file store, unchanged
    probe = pytest.importorskip("kd.whisper_probe")
    card = w.env.card(project="digipaddock", assignee="@dev", status="In Progress")
    sent = {}
    for n, item in enumerate(probe.ITEMS):
        body = WEBM + bytes([n])
        name = f"voice-20261021-0900{n:02d}.webm"
        r = _send(w, f"/api/cards/{card.id}/files", [(name, body, "audio/webm;codecs=opus")])
        assert r.status_code == 200, r.text
        sent[name] = (item, body)
    out = tmp_path / "items"
    items = probe.collect(str(tmp_path / "cabinet.db"), w.cfg.files_dir, card.id, out,
                          {name: item for name, (item, _) in sent.items()})
    assert len(items) == len(probe.ITEMS)
    for entry in items:
        item, body = sent[entry["name"]]
        assert entry["item"] == item and entry["file"] == f"{item}.webm"
        assert (out / entry["file"]).read_bytes() == body


def test_only_audio_and_raster_pictures_open_inline(w):
    card = _card(w)
    sent = _send(w, f"/api/cards/{card['id']}/files", [
        ("a.m4a", b"\0\0\0 ftypM4A ", "audio/mp4"), ("b.ogg", b"OggS", "audio/ogg"), ("c.mp3", b"ID3", "audio/mpeg"),
        ("d.wav", b"RIFF0000WAVE", "audio/wav"),
        ("e.mid", b"MThd", "audio/x-midi-weird"), ("f.html", b"<script>alert(1)</script>", "video/webm"),
    ]).json()["payload"]["files"]
    for f in sent:
        got = w.client.get(f"/api/files/{f['id']}", params={"inline": 1})
        disposition = got.headers["content-disposition"].split(";")[0]
        if f["name"] in ("e.mid", "f.html"):
            assert disposition == "attachment" and got.headers["content-type"] == "application/octet-stream", f
        else:
            assert disposition == "inline" and got.headers["content-type"] == f["mime"], f


def test_a_voice_message_alone_wakes_nobody(w):
    # no transcription yet — a voice message is a file without text in the thread and the chat
    card = w.env.card(project="ops", assignee="@dev", status="In Progress")
    before = len(w.env.store.commands(agent="analyst"))
    r = _send(w, f"/api/cards/{card.id}/files", [("voice.webm", WEBM, "audio/webm;codecs=opus")])
    assert r.status_code == 200 and r.json()["kind"] == "file"
    added = [e for e in w.env.store.events_since(0) if e["kind"] == "message.added"][-1]
    assert "mentions" not in added["payload"] and w.env.store.commands(agent="dev") == []
    r = _send(w, "/api/agents/analyst/files", [("voice.webm", WEBM, "audio/webm;codecs=opus")])
    assert r.status_code == 200 and r.json()["kind"] == "file"
    assert len(w.env.store.commands(agent="analyst")) == before
    # the same file with a line of text is a message for the agent, as before
    _send(w, "/api/agents/analyst/files", [("voice.webm", WEBM, "audio/webm")], text="послушай")
    assert len(w.env.store.commands(agent="analyst")) == before + 1


def test_the_author_or_the_owner_deletes_a_file(w):
    card = _card(w)
    anna, headers = _as(w, "anna")
    mine = _send(w, f"/api/cards/{card['id']}/files", [("a.txt", b"anna", "text/plain"), ("b.txt", b"b", "text/plain")],
                 text="мои", client=anna, headers=headers).json()
    owners = _send(w, f"/api/cards/{card['id']}/files", [("c.txt", b"c", "text/plain")]).json()
    a, b = (f["id"] for f in mine["payload"]["files"])
    assert anna.delete(f"/api/files/{owners['payload']['files'][0]['id']}", headers=headers).status_code == 403
    r = anna.delete(f"/api/files/{a}", headers=headers)
    assert r.status_code == 200 and r.json()["payload"]["files"][0] == {"id": a, "name": "a.txt", "deleted": True}
    assert a not in _stored(w) and w.env.store.files_size("user:anna") == 1
    assert anna.get(f"/api/files/{a}").status_code == 404
    assert w.client.delete(f"/api/files/{b}", headers=w.headers()).status_code == 200  # the owner may
    assert w.env.store.files_size("user:anna") == 0 and _stored(w) == [owners["payload"]["files"][0]["id"]]
    assert w.client.delete(f"/api/files/{b}", headers=w.headers()).status_code == 404
    assert w.client.delete("/api/files/zzz", headers=w.headers()).status_code == 404
    assert w.client.delete(f"/api/files/{b}").status_code in (401, 403)  # no CSRF token
