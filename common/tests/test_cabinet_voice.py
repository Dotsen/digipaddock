"""People's audio in the cabinet to text — the queue, one wake per message, a voice answer to a
question, failures with "Retry" and the author's push, the switch, a loop that never stops the cabinet."""

import asyncio
import io
import threading
import time
from pathlib import Path

import pytest
from web_env import WebEnv

from kd import session_config, transcribe_client
from kd.cabinet import api, view, voice

HOSTED = session_config.HostConfig({"analyst": 1, "dev": 2}, 600)
WEBM = b"\x1aE\xdf\xa3" + b"\0" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


class Executor:
    """kd-transcribe as the tests want it: an answer per call in order (the last repeats), the headers it got."""

    def __init__(self, *answers):
        self.answers = list(answers) or [{"status": "ok", "text": "привет"}]
        self.headers = []

    def __call__(self, audio, header, *, path, timeout):
        self.headers.append(header)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return {"duration": 3.5, **answer}


def ok(text="привет"):
    return {"status": "ok", "text": text}


@pytest.fixture
def w(tmp_path):
    w = WebEnv(tmp_path)
    api.install_host(w.env.ctx, lambda: HOSTED)
    w.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    w.users.create("anna", "Анна", roles=[("partner", ["digipaddock"])], language="en", actor="root")
    w.cfg = w.svc.cfg = type(w.cfg)(**{**w.cfg.__dict__, "files_dir": str(tmp_path / "files")})
    w.env.ctx.files_dir = w.cfg.files_dir
    w.executor = Executor()
    w.env.ctx.voice = voice.Settings(enabled=True, language="ru", ask=w.executor)
    w.login()
    return w


def _upload(w, path, files, text=None, client=None, headers=None):
    data = {} if text is None else {"text": text}
    r = (client or w.client).post(path, headers=headers or w.headers(), data=data,
                                  files=[("file", f) for f in files])
    assert r.status_code == 200, r.text
    return r.json()


def _say(w, card, n=1, text=None, **kw):
    return _upload(w, f"/api/cards/{card.id}/files", [(f"voice-{i}.webm", WEBM, "audio/webm") for i in range(n)],
                   text, **kw)


def _step(w) -> bool:
    return asyncio.run(voice.step(w.svc))


def _drain(w) -> int:
    done = 0
    while _step(w):
        done += 1
    return done


def _wakes(w, card=None, kind="message"):
    return [c for c in w.env.store.commands(limit=200)
            if (card is None or c["card"] == card.id) and c["kind"] == kind]


def _message(w, card, seq):
    return w.env.store._message(w.env.store.card(card.id).thread, seq)


def _voices(message):
    return [f["voice"] for f in message["payload"]["files"]]


def _row(w, file_id):
    return dict(w.env.store._conn.execute("SELECT * FROM transcripts WHERE file=?", (file_id,)).fetchone())


def _card(w, **over):
    return w.env.card(**{"status": "In Progress", "assignee": "@dev", **over})


# --- AC1: the text under the audio, what the agent reads, what is never transcribed ---------------------------
def test_audio_waits_then_shows_its_text_and_wakes_the_agent(w):
    card = _card(w)
    made = _say(w, card)
    assert _voices(made) == [{"state": "queued", "position": 1}]
    assert _wakes(w, card) == []  # nothing to read yet
    w.executor.answers = [ok("Проверь KD-9076 на стейдже")]
    assert _drain(w) == 1
    message = _message(w, card, made["seq"])
    assert _voices(message) == [{"state": "done", "status": "ok", "text": "Проверь KD-9076 на стейдже"}]
    assert len(_wakes(w, card)) == 1
    line = view.file_line(message["payload"]["files"][0])
    assert line.splitlines()[1] == "[голосовое, расшифровка] Проверь KD-9076 на стейдже"
    assert "[голосовое, расшифровка] Проверь" in view.message_text(message)


def test_the_header_carries_the_author_language_and_the_cabinet_vocab(w):
    card = _card(w, title="Голос")
    _say(w, card)
    other = w.other_client()
    w.login("anna", client=other)
    csrf = other.get("/api/me").json()["csrf"]
    _say(w, card, client=other, headers=w.headers(csrf))
    _drain(w)
    first, second = w.executor.headers
    assert first["language"] == "ru" and second["language"] == "en"  # the cabinet's, then anna's own
    assert "dev" in first["vocab"]["agents"] and card.id in first["vocab"]["cards"]
    assert "digipaddock" in first["vocab"]["projects"]


def test_a_file_that_is_not_audio_and_an_agents_file_are_not_transcribed(w):
    card = _card(w)
    made = _upload(w, f"/api/cards/{card.id}/files", [("shot.png", PNG, "image/png")])
    assert "voice" not in made["payload"]["files"][0]
    added = w.env.store.add_file(card.id, author="agent:dev", file={
        "id": "a" * 32, "name": "voice.webm", "mime": "audio/webm", "size": 3, "sha256": "0" * 64})
    assert "voice" not in added["payload"]["files"][0]
    assert w.env.store._conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 0
    assert _drain(w) == 0


def test_audio_sent_before_the_queue_is_never_transcribed(w):
    card = _card(w)
    w.env.ctx.voice = voice.Settings(enabled=False)
    before = _say(w, card)
    w.env.ctx.voice = voice.Settings(enabled=True, ask=w.executor)
    assert "voice" not in before["payload"]["files"][0]
    assert _drain(w) == 0
    assert w.executor.headers == []


# --- AC2: one wake per message, after all its audio ----------------------------------------------------------
def test_text_with_a_failed_audio_wakes_once_and_marks_it(w):
    card = _card(w)
    w.executor.answers = [{"status": "error", "text": ""}]
    made = _say(w, card, text="Посмотри запись")
    assert _wakes(w, card) == []
    _drain(w)
    assert len(_wakes(w, card)) == 1
    [state] = _voices(_message(w, card, made["seq"]))
    assert state == {"state": "done", "status": "error"}
    assert "[голосовое] аудио не расшифровано (error)" in view.file_line(_message(w, card, made["seq"])["payload"]["files"][0])


def test_three_audio_wake_once_after_the_last(w):
    card = _card(w)
    w.executor.answers = [{"status": "empty", "text": ""}, ok("второе"), {"status": "error", "text": ""}]
    made = _say(w, card, n=3)
    assert [v["position"] for v in _voices(made)] == [1, 2, 3]
    assert _step(w) and _step(w)
    assert _wakes(w, card) == []  # the third is still in the line
    assert _step(w) and not _step(w)
    assert len(_wakes(w, card)) == 1
    assert [v["status"] for v in _voices(_message(w, card, made["seq"]))] == ["empty", "ok", "error"]


def test_audio_alone_that_failed_wakes_nobody_until_a_retry_succeeds(w):
    card = _card(w)
    w.executor.answers = [{"status": "unavailable", "text": ""}, {"status": "error", "text": ""}, ok("вот")]
    made = _say(w, card, n=2)
    _drain(w)
    assert _wakes(w, card) == []
    second = made["payload"]["files"][1]["id"]
    r = w.client.post(f"/api/files/{second}/transcribe", headers=w.headers())
    assert r.status_code == 200, r.text
    assert r.json()["payload"]["files"][1]["voice"]["state"] == "queued"
    _drain(w)
    assert len(_wakes(w, card)) == 1


def test_a_retry_after_the_wake_does_not_wake_again(w):
    card = _card(w)
    w.executor.answers = [{"status": "error", "text": ""}, ok("теперь понятно")]
    made = _say(w, card, text="Слушай")
    _drain(w)
    assert len(_wakes(w, card)) == 1
    file_id = made["payload"]["files"][0]["id"]
    assert w.client.post(f"/api/files/{file_id}/transcribe", headers=w.headers()).status_code == 200
    _drain(w)
    assert _voices(_message(w, card, made["seq"]))[0]["text"] == "теперь понятно"
    assert len(_wakes(w, card)) == 1


def test_addressees_are_the_rules_at_the_wake(w):
    card = _card(w)
    _say(w, card, text="@analyst глянь")
    _drain(w)
    assert [c["agent"] for c in _wakes(w, card)] == ["analyst"]
    done = _card(w, status="Done")
    _say(w, done)
    _drain(w)
    assert _wakes(w, done) == []  # nobody on a Done card


def test_chat_audio_wakes_the_agent_with_its_chat_command(w):
    made = _upload(w, "/api/agents/dev/files", [("voice.webm", WEBM, "audio/webm")])
    assert _wakes(w, kind="chat") == []
    _drain(w)
    [command] = _wakes(w, kind="chat")
    assert command["agent"] == "dev" and command["key"] == f"chat:{command['thread']}:{made['seq']}"
    chat = w.env.store.agent_thread("dev")
    assert w.env.store._message(chat, made["seq"])["payload"]["files"][0]["voice"]["text"] == "привет"


def test_monitor_audio_wakes_the_monitors_session(w):
    from kd.cabinet import sessions_api

    made = _upload(w, "/api/agents/dev/monitor/files", [("voice.webm", WEBM, "audio/webm")])
    _drain(w)
    [command] = _wakes(w, kind="chat")
    assert command["text"] == sessions_api.MONITOR_WAKE and command["key"].endswith(f":{made['seq']}")
    w.executor.answers = [{"status": "error", "text": ""}]
    _upload(w, "/api/agents/dev/monitor/files", [("voice.webm", WEBM, "audio/webm")])
    _drain(w)
    w.svc.notifier.catch_up(w.svc.decisions)
    [row] = [r for r in w.env.store.notifications("owner") if r["kind"] == "voice.failed"]
    assert row["url"] == "/agents/dev?tab=monitor"


# --- AC3: the line, a restart, one result per audio -----------------------------------------------------------
def test_the_line_goes_in_order_and_shows_places(w):
    card = _card(w)
    first, second = _say(w, card), _say(w, card)
    assert _voices(second)[0]["position"] == 2
    _step(w)
    page = w.client.get(f"/api/cards/{card.id}/messages?limit=10").json()["messages"]
    shown = {m["seq"]: m for m in page}
    assert _voices(shown[first["seq"]])[0]["status"] == "ok"
    assert _voices(shown[second["seq"]])[0] == {"state": "queued", "position": 1}
    assert len(w.executor.headers) == 1


def test_a_restart_queues_again_what_was_running_and_writes_one_result(w):
    card = _card(w)
    made = _say(w, card)
    item = voice.take(w.env.ctx)
    assert _voices(_message(w, card, made["seq"]))[0] == {"state": "running"}
    assert voice.recover(w.env.ctx) == 1  # the cabinet started again
    assert _voices(_message(w, card, made["seq"]))[0] == {"state": "queued"}
    assert voice.finish(w.env.ctx, item, ok("старый ответ")) is None  # the attempt before the restart
    _drain(w)
    assert _row(w, made["payload"]["files"][0]["id"])["attempts"] == 2
    assert _voices(_message(w, card, made["seq"]))[0]["text"] == "привет"
    voice.recover(w.env.ctx)
    assert _drain(w) == 0
    assert len(_wakes(w, card)) == 1


def test_a_wake_that_fails_leaves_the_result_and_a_retry_possible(w, monkeypatch):
    card = _card(w)
    w.executor.answers = [{"status": "error", "text": ""}, ok("ещё раз")]
    made = _say(w, card, n=2)

    def broken(*args, **kw):
        raise api.ApiError("unavailable", "the agents' registry is unreadable")

    monkeypatch.setattr(api, "_addressees", broken)
    err = io.StringIO()
    while (item := voice.take(w.env.ctx)) is not None:
        voice.finish(w.env.ctx, item, voice.work(w.env.ctx.voice, w.env.ctx.files_dir, item), err=err)
    assert [v["state"] for v in _voices(_message(w, card, made["seq"]))] == ["done", "done"]
    assert "the wake of" in err.getvalue() and _wakes(w, card) == []
    monkeypatch.undo()
    first = made["payload"]["files"][0]["id"]
    assert w.client.post(f"/api/files/{first}/transcribe", headers=w.headers()).status_code == 200
    _drain(w)
    assert len(_wakes(w, card)) == 1  # the retry's result settles the message again


def test_a_result_that_cannot_be_written_is_an_error_to_retry(w, monkeypatch):
    card = _card(w)
    made = _say(w, card)
    real, calls = voice._journal, []

    def once(*args, **kw):  # the first write fails as a broken base would; the error's write then goes through
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("disk")
        return real(*args, **kw)

    monkeypatch.setattr(voice, "_journal", once)
    assert _step(w)
    [state] = _voices(_message(w, card, made["seq"]))
    assert state == {"state": "done", "status": "error"}


def test_the_lines_own_steps_stay_out_of_the_feed(w):
    card = _card(w)
    _say(w, card)
    _drain(w)
    kinds = [e["kind"] for e in w.client.get("/api/feed?limit=100").json()["events"]]
    assert "message.added" in kinds
    assert not [k for k in kinds if k.startswith("voice.") or k == "message.changed"]


def test_a_repeated_send_queues_nothing_twice(w):
    card = _card(w)
    for _ in range(2):
        w.client.post(f"/api/cards/{card.id}/files", headers=w.headers(), data={"client_key": "same-send-0001"},
                      files=[("file", ("voice.webm", WEBM, "audio/webm"))])
    assert w.env.store._conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 1
    assert _drain(w) == 1


# --- AC4: a voice answer to a text question ---------------------------------------------------------------------
def _ask(w, payload, card=None):
    card = card or _card(w)
    q = w.env.call("digi", {"op": "decision.open", "type": "question", "agent": "dev", "card": card.id,
                           "payload": payload})["decision"]
    return card, q


def _answer(w, q, files=None):
    return w.client.post(f"/api/decisions/{q['id']}/files", headers=w.headers(),
                         files=[("file", f) for f in files or [("voice.webm", WEBM, "audio/webm")]])


def _decision(w, q):
    return w.env.call("digi-cabinet", {"op": "decision.get", "id": q["id"], "user": "owner"})["decision"]


def test_a_recording_answers_a_text_question_once_transcribed(w):
    card, q = _ask(w, {"text": "Как назвать раздел?"})
    r = _answer(w, q)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "open"
    assert len(list(Path(w.cfg.files_dir).iterdir())) == 1  # the recording's bytes stay
    assert _wakes(w, card, "answer") == []
    long = "слово " * 1200  # a five-minute message: over the 4000 of a text answer
    w.executor.answers = [ok(long.strip())]
    _drain(w)
    d = _decision(w, q)
    assert d["state"] == "approved"
    assert d["reply"] == "[голосовое, расшифровка] " + long.strip() and len(d["reply"]) > 6000
    assert [f["mime"] for f in d["answer"]["files"]] == ["audio/webm"]
    assert len(_wakes(w, card, "answer")) == 1
    assert _wakes(w, card) == []  # the answer's wake is the only one


def test_a_failed_voice_answer_leaves_the_question_open(w):
    card, q = _ask(w, {"text": "Как назвать раздел?"})
    w.executor.answers = [{"status": "too_long", "text": ""}]
    assert _answer(w, q).status_code == 200
    _drain(w)
    assert _decision(w, q)["state"] == "open"
    assert _wakes(w, card, "answer") == [] and _wakes(w, card) == []


def test_a_voice_answer_sent_again_is_taken_once(w):
    card, q = _ask(w, {"text": "Как назвать раздел?"})
    for _ in range(2):
        r = w.client.post(f"/api/decisions/{q['id']}/files", headers=w.headers(), data={"client_key": "voice-answer-01"},
                          files=[("file", ("voice.webm", WEBM, "audio/webm"))])
        assert r.status_code == 200, r.text
    assert len(list(Path(w.cfg.files_dir).iterdir())) == 1
    _drain(w)
    r = w.client.post(f"/api/decisions/{q['id']}/files", headers=w.headers(), data={"client_key": "voice-answer-01"},
                      files=[("file", ("voice.webm", WEBM, "audio/webm"))])
    assert r.status_code == 200 and r.json()["state"] == "approved"  # answered meanwhile: still the same send
    assert len(_wakes(w, card, "answer")) == 1


def test_a_question_closed_meanwhile_takes_the_recording_as_a_message(w):
    card, q = _ask(w, {"text": "Как назвать раздел?"})
    assert _answer(w, q).status_code == 200
    r = w.client.post(f"/api/decisions/{q['id']}/reply", headers=w.headers(), json={"text": "«Операции»"})
    assert r.status_code == 200, r.text
    _drain(w)
    assert _decision(w, q)["reply"] == "«Операции»"
    assert len(_wakes(w, card, "answer")) == 1
    assert len(_wakes(w, card)) == 1  # the recording, now an ordinary message for the card's agent


def test_a_file_question_takes_a_recording_as_a_sample(w):
    card, q = _ask(w, {"text": "Пришлите запись шума", "expect": "file"})
    r = _answer(w, q)
    assert r.status_code == 200 and r.json()["state"] == "approved"
    assert w.env.store._conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 0


def test_a_form_question_still_needs_its_form(w):
    form = {"schema_version": 1, "text": "Выбор", "questions": [
        {"id": "a", "title": "Какой?", "type": "single_select", "required": True,
         "options": [{"id": "x", "label": "X"}, {"id": "y", "label": "Y"}]}]}
    _, q = _ask(w, form)
    assert _answer(w, q).status_code == 400


# --- AC5: failures ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("status", ["error", "unavailable"])
def test_a_failure_of_the_way_may_be_retried_by_the_author_or_the_owner(w, status):
    card = _card(w)
    w.executor.answers = [transcribe_client.TranscribeError("socket.timeout") if status == "unavailable"
                          else {"status": "error", "text": ""}, ok()]
    other = w.other_client()
    w.login("anna", client=other)
    csrf = other.get("/api/me").json()["csrf"]
    made = _say(w, card, client=other, headers=w.headers(csrf))
    _drain(w)
    assert _voices(_message(w, card, made["seq"]))[0]["status"] == status
    file_id = made["payload"]["files"][0]["id"]
    r = w.client.post(f"/api/files/{file_id}/transcribe", headers=w.headers())  # the owner
    assert r.status_code == 200, r.text
    _drain(w)
    assert _voices(_message(w, card, made["seq"]))[0]["status"] == "ok"


@pytest.mark.parametrize("status", ["too_long", "too_large", "not_audio", "empty"])
def test_a_recording_to_record_again_has_no_retry(w, status):
    card = _card(w)
    w.executor.answers = [{"status": status, "text": ""}]
    made = _say(w, card)
    _drain(w)
    r = w.client.post(f"/api/files/{made['payload']['files'][0]['id']}/transcribe", headers=w.headers())
    assert r.status_code != 200 and r.json().get("reason") == "not_retryable", r.text
    assert _voices(_message(w, card, made["seq"]))[0]["status"] == status


def test_someone_else_may_not_retry(w):
    card = _card(w)
    w.executor.answers = [{"status": "error", "text": ""}]
    made = _say(w, card)  # the owner's
    _drain(w)
    with pytest.raises(api.ApiError) as exc:
        voice.retry(w.env.ctx, api.Principal("user", -1, "anna"), made["payload"]["files"][0]["id"])
    assert exc.value.kind == "refused"


def test_a_failure_pushes_its_author_at_once(w):
    card = _card(w)
    w.executor.answers = [{"status": "empty", "text": ""}]
    _say(w, card)
    _drain(w)
    w.svc.notifier.catch_up(w.svc.decisions)
    rows = [r for r in w.env.store.notifications("owner") if r["kind"] == "voice.failed"]
    assert [(r["title"], r["body"], r["url"]) for r in rows] == [
        (f"Аудио в {card.id} не расшифровано", "речь не распознана — запишите заново", f"/cards/{card.id}")]
    assert rows[0]["due"] == w.env.now


def test_a_failure_in_a_chat_names_the_chat(w):
    w.executor.answers = [{"status": "error", "text": ""}]
    _upload(w, "/api/agents/dev/files", [("voice.webm", WEBM, "audio/webm")])
    _drain(w)
    w.svc.notifier.catch_up(w.svc.decisions)
    [row] = [r for r in w.env.store.notifications("owner") if r["kind"] == "voice.failed"]
    assert (row["title"], row["url"]) == ("Аудио в чате @dev не расшифровано", "/agents/dev")


# --- AC6: the cabinet keeps serving while an audio is transcribed --------------------------------------------
def test_the_cabinet_serves_while_the_executor_thinks(w):
    card = _card(w)
    _say(w, card)
    release, entered = threading.Event(), threading.Event()

    def slow(audio, header, *, path, timeout):
        entered.set()
        release.wait(30)  # the stub that answers in 30 s, let go as soon as the checks are done
        return {"status": "ok", "text": "долго", "duration": 30.0}

    w.env.ctx.voice = voice.Settings(enabled=True, ask=slow)

    async def scenario():
        task = asyncio.create_task(voice.step(w.svc))
        while not entered.is_set():
            await asyncio.sleep(0.01)
        started = time.monotonic()
        found = await w.svc.call(w.env.principal("digi-dev"), {"op": "card.get", "card": card.id})
        other = await w.svc.call(w.env.principal("digi-dev"), {"op": "thread.messages", "card": card.id})
        served = time.monotonic() - started
        release.set()
        assert await task
        return found, other, served

    found, other, served = asyncio.run(scenario())
    assert found["card"]["id"] == card.id and other["messages"]
    assert served < 2  # the lock was free and the loop ran while the executor worked


# --- AC7: the journal --------------------------------------------------------------------------------------------
def test_each_result_is_a_journal_line(w):
    card = _card(w)
    made = _say(w, card)
    item = voice.take(w.env.ctx)
    err = io.StringIO()
    w.env.now = "2026-10-14T10:00:12Z"
    voice.finish(w.env.ctx, item, {"status": "ok", "text": "да", "duration": 63.2, "work": 41.0}, err=err)
    line = err.getvalue().strip()
    assert line == (f"kd-cabinet voice: {card.id} file {made['payload']['files'][0]['id'][:8]} wait 0s work 41.0s "
                    f"length 63.2s status ok")


# --- AC8: the switch ---------------------------------------------------------------------------------------------
def test_off_audio_are_plain_files_again(w):
    w.env.ctx.voice = voice.Settings(enabled=False, ask=w.executor)
    card = _card(w)
    made = _say(w, card)
    assert "voice" not in made["payload"]["files"][0]
    assert _wakes(w, card) == []  # files alone wake nobody, as before
    chat = _upload(w, "/api/agents/dev/files", [("voice.webm", WEBM, "audio/webm")])
    assert "voice" not in chat["payload"]["files"][0] and _wakes(w, kind="chat") == []
    _, q = _ask(w, {"text": "Как назвать?"}, card)
    r = _answer(w, q)
    assert r.status_code == 400 and r.json().get("reason") == "text_needed"
    assert w.env.store._conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 0
    assert w.client.get("/api/me").json()["voice"] is False


def test_turning_voice_off_closes_the_queue_and_wakes(w):
    """What waited in the line when the switch went off ends "transcription is off" at the next start; a
    message with text wakes as one whose audio was not understood, audio alone stays a file the agent reads."""
    card = _card(w)
    with_text = _say(w, card, text="Посмотри запись")
    alone = _say(w, card)
    _upload(w, "/api/agents/dev/files", [("voice.webm", WEBM, "audio/webm")], "Глянь")
    assert voice.take(w.env.ctx)["seq"] == with_text["seq"]  # being transcribed when the cabinet stopped
    w.env.ctx.voice = voice.Settings(enabled=False, ask=w.executor)
    asyncio.run(voice.run(w.svc))  # off: the loop closes the line and ends
    assert _voices(_message(w, card, with_text["seq"])) == [{"state": "done", "status": "off"}]
    assert _voices(_message(w, card, alone["seq"])) == [{"state": "done", "status": "off"}]
    assert len(_wakes(w, card)) == 1 and len(_wakes(w, kind="chat")) == 1  # the texts; audio alone wakes nobody
    line = view.file_line(_message(w, card, alone["seq"])["payload"]["files"][0])
    assert "[голосовое] аудио не расшифровано (off)" in line
    assert w.env.store._conn.execute("SELECT COUNT(*) FROM transcripts WHERE state!='done'").fetchone()[0] == 0
    assert voice.close_off(w.env.ctx) == 0 and len(_wakes(w, card)) == 1  # the next start changes nothing
    r = w.client.post(f"/api/files/{alone['payload']['files'][0]['id']}/transcribe", headers=w.headers())
    assert r.status_code != 200 and r.json().get("reason") == "voice_off", r.text


def test_a_recording_closed_by_the_switch_is_retried_once_it_is_on(w):
    card = _card(w)
    made = _say(w, card)
    w.env.ctx.voice = voice.Settings(enabled=False, ask=w.executor)
    voice.close_off(w.env.ctx)
    w.env.ctx.voice = voice.Settings(enabled=True, ask=w.executor)
    r = w.client.post(f"/api/files/{made['payload']['files'][0]['id']}/transcribe", headers=w.headers())
    assert r.status_code == 200, r.text
    _drain(w)
    assert _voices(_message(w, card, made["seq"])) == [{"state": "done", "status": "ok", "text": "привет"}]
    assert len(_wakes(w, card)) == 1


def test_the_config_switch(tmp_path):
    from kd.cabinet import config

    settings = tmp_path / "cabinet.json"
    env = tmp_path / "cabinet.env"
    env.write_text("CABINET_SECRET=" + "ab" * 32 + "\nVAPID_PUBLIC_KEY=k\nVAPID_SUBJECT=mailto:a@b\n", "utf-8")
    settings.write_text('{"origin": "https://board.example"}', "utf-8")
    assert config.load(str(settings), str(env)).transcribe is True
    settings.write_text('{"origin": "https://board.example", "transcribe": false}', "utf-8")
    assert config.load(str(settings), str(env)).transcribe is False
    settings.write_text('{"origin": "https://board.example", "transcribe": "no"}', "utf-8")
    with pytest.raises(config.ConfigError):
        config.load(str(settings), str(env))
