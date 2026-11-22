"""An open question takes files; ``expect`` file|text decides what answers it."""

from pathlib import Path

import pytest
from web_env import WebEnv

from kd import session_config
from kd.cabinet import api, decisions

HOSTED = session_config.HostConfig({"analyst": 1, "dev": 2}, 600)
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
WEBM = b"\x1aE\xdf\xa3" + b"\0" * 64


@pytest.fixture
def w(tmp_path):
    w = WebEnv(tmp_path)
    api.install_host(w.env.ctx, lambda: HOSTED)
    w.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    w.users.create("anna", "Анна", roles=[("partner", ["web"])], actor="root")
    w.cfg = w.svc.cfg = type(w.cfg)(**{**w.cfg.__dict__, "files_dir": str(tmp_path / "files")})
    w.login()
    return w


def _stored(w):
    folder = Path(w.cfg.files_dir)
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


def _ask(w, payload, project="digipaddock"):
    card = w.env.card(status="In Progress", assignee="@dev", project=project)
    q = w.env.call("digi", {"op": "decision.open", "type": "question", "agent": "dev", "card": card.id,
                           "payload": payload})["decision"]
    return card, q


def _send(w, q, files, text=None, key=None, client=None, headers=None):
    data = {} if text is None else {"text": text}
    if key:
        data["client_key"] = key
    return (client or w.client).post(f"/api/decisions/{q['id']}/files", headers=headers or w.headers(), data=data,
                                     files=[("file", f) for f in files])


def _wakes(w, card):
    return [c for c in w.env.store.commands(limit=100) if c["card"] == card.id and c["kind"] == "answer"]


# --- the question's expectation ----------------------------------------------------------------------
def test_open_question_keeps_payload_without_expect(w):
    _, q = _ask(w, {"text": "Пришлите скриншот?"})
    assert q["payload"] == {"text": "Пришлите скриншот?"}
    assert q["payload_sha"] == decisions.payload_sha({"text": "Пришлите скриншот?"})


def test_open_question_expect_file(w):
    _, q = _ask(w, {"text": "Пришлите скриншот", "expect": "file"})
    assert q["payload"] == {"text": "Пришлите скриншот", "expect": "file"}
    _, t = _ask(w, {"text": "Как назвать?", "expect": "text"})
    assert t["payload"] == {"text": "Как назвать?"}  # text is the default and is not written


def test_open_question_rejects_unknown_expect(w):
    with pytest.raises(api.ApiError) as exc:
        _ask(w, {"text": "Голос?", "expect": "audio"})
    assert exc.value.kind == "invalid"


# --- replies with files ----------------------------------------------------------------------------
def test_reply_file_question_with_file_only_approves(w):
    card, q = _ask(w, {"text": "Пришлите скриншот", "expect": "file"})
    r = _send(w, q, [("shot.png", PNG, "image/png")])
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["state"] == "approved"
    [f] = d["answer"]["files"]
    assert (f["name"], f["mime"], f["size"]) == ("shot.png", "image/png", len(PNG))
    assert _stored(w) == [f["id"]]
    message = w.env.store.messages(card.id)[-1]
    assert message["kind"] == "decision" and message["payload"]["files"] == d["answer"]["files"]
    assert w.env.store.attachment(f["id"])["seq"] == message["seq"]
    assert len(_wakes(w, card)) == 1


def test_reply_file_question_without_file_refused(w):
    _, q = _ask(w, {"text": "Пришлите скриншот", "expect": "file"})
    r = w.client.post(f"/api/decisions/{q['id']}/reply", headers=w.headers(), json={"text": "потом пришлю"})
    assert r.status_code == 400 and r.json().get("reason") == "file_needed"
    assert w.env.call("digi-cabinet", {"op": "decision.get", "id": q["id"], "user": "owner"})["decision"]["state"] == "open"


def test_reply_text_question_audio_only_refused(w):
    _, q = _ask(w, {"text": "Как назвать раздел?"})
    r = _send(w, q, [("voice.webm", WEBM, "audio/webm")])
    assert r.status_code == 400 and r.json().get("reason") == "text_needed"
    assert _stored(w) == []


def test_reply_text_question_text_and_audio(w):
    card, q = _ask(w, {"text": "Как назвать раздел?"})
    r = _send(w, q, [("voice.webm", WEBM, "audio/webm")], text="«Операции»")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["state"] == "approved" and d["reply"] == "«Операции»"
    assert [f["mime"] for f in d["answer"]["files"]] == ["audio/webm"]
    assert len(_wakes(w, card)) == 1


def test_reply_with_files_is_idempotent_by_client_key(w):
    card, q = _ask(w, {"text": "Пришлите скриншот", "expect": "file"})
    first = _send(w, q, [("shot.png", PNG, "image/png")], key="retry-key-0001")
    again = _send(w, q, [("shot.png", PNG, "image/png")], key="retry-key-0001")
    assert first.status_code == again.status_code == 200, again.text
    assert again.json()["answer"] == first.json()["answer"]
    assert len(_stored(w)) == 1
    assert sum(1 for m in w.env.store.messages(card.id) if m["kind"] == "decision"
               and m["payload"].get("state") == "approved") == 1
    assert len(_wakes(w, card)) == 1


def test_reply_to_closed_question_removes_files(w):
    _, q = _ask(w, {"text": "Пришлите скриншот", "expect": "file"})
    assert _send(w, q, [("a.png", PNG, "image/png")]).status_code == 200
    late = _send(w, q, [("b.png", PNG, "image/png")], key="another-send-01")
    assert late.status_code in (403, 409) and len(_stored(w)) == 1


def test_reply_other_card_question_refused(w):
    _, q = _ask(w, {"text": "Пришлите скриншот", "expect": "file"}, project="digipaddock")
    browser = w.other_client()
    me = w.login("anna", client=browser)  # a partner of «web» only
    r = _send(w, q, [("a.png", PNG, "image/png")], client=browser, headers=w.headers(me["csrf"]))
    assert r.status_code in (403, 404) and _stored(w) == []


def test_reply_file_quota(w, monkeypatch):
    monkeypatch.setattr(api, "FILES_USER_MAX", 10)
    _, q = _ask(w, {"text": "Пришлите скриншот", "expect": "file"})
    r = _send(w, q, [("a.png", PNG, "image/png")])
    assert r.status_code == 403 and r.json().get("reason") == "files_quota" and _stored(w) == []


def test_web_reply_json_unchanged(w):
    _, q = _ask(w, {"text": "Как назвать раздел?"})
    r = w.client.post(f"/api/decisions/{q['id']}/reply", headers=w.headers(), json={"text": "Операции"})
    assert r.status_code == 200 and r.json()["reply"] == "Операции" and r.json()["answer"] is None


# --- a form answer with files keeps its form fields ----------------------------------------
def _form_answer(q):
    import json

    from kd.cabinet import questions

    return {"answers": json.dumps({"method": {"selected_option_ids": ["ssh"]}}), "comment": "Срок — неделя",
            "form_sha256": questions.form_sha(q["payload"])}


def test_a_form_answer_is_the_same_through_json_and_multipart(w):
    import json

    from question_samples import form

    _, by_json = _ask(w, form())
    body = _form_answer(by_json)
    r = w.client.post(f"/api/decisions/{by_json['id']}/reply", headers=w.headers(),
                      json={**body, "answers": json.loads(body["answers"])})
    assert r.status_code == 200, r.text
    _, by_form = _ask(w, form())
    data = {**_form_answer(by_form), "client_key": "k-form-1"}
    sent = w.client.post(f"/api/decisions/{by_form['id']}/files", headers=w.headers(), data=data,
                         files=[("file", ("shot.png", PNG, "image/png"))])
    assert sent.status_code == 200, sent.text
    answer, plain = sent.json()["answer"], r.json()["answer"]
    assert answer["answers"] == plain["answers"] and answer["comment"] == plain["comment"]
    assert sent.json()["state"] == "approved" and len(answer["files"]) == 1
    again = w.client.post(f"/api/decisions/{by_form['id']}/files", headers=w.headers(), data=data,
                          files=[("file", ("shot.png", PNG, "image/png"))])  # the answer was lost: sent again
    assert again.status_code == 200 and again.json()["answer"]["files"] == answer["files"]
    assert _stored(w) == [answer["files"][0]["id"]]
