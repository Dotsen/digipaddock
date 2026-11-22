"""People's audio in the cabinet to text: the queue, its loop and what a result wakes.

An audio file (``AUDIO_TYPES``) a person attaches to a message in a card's thread or an agent's chat — a recording
made with the button or a file — is a row of ``transcripts`` written with the message: such a message wakes nobody
when it is written. The loop (``run``, one per service, like the dispatcher) takes the rows one at a time in the
order they came, sends each to the executor kd-transcribe (``transcribe_client.ask``, blocking) in a thread — never
on the loop, never under the service lock — and writes the result once: the row's state and the text into the
message's file entry (``payload.files[i].voice``), with ``message.changed`` for the open pages. A row that was
running when the cabinet stopped is queued again at the next start.

When every audio of a message has its result the message wakes, once (``transcript_wakes``): one with text always
(an audio that failed is marked so under itself), one of audio alone only when at least one audio was understood;
whom — ``api._addressees`` at that moment, in a chat — the agent (its ``chat`` command). A recording sent as the
answer to an agent's text question (``answers``) answers it with the text, when the question is still open: the
question stays open while it waits and after a failure; closed meanwhile, the recording is an ordinary message.

A failure: ``error``, a broken socket or a timeout (``unavailable``) may be retried ("Retry": the author or the
owner, ``user.voice.retry``); ``too_long``, ``too_large``, ``not_audio`` and ``empty`` are recorded again by the
person. Each failure tells the author at once (``voice.failed`` → a push). Every result is one line in the service's
journal: the wait in the queue, the work, the recording's length and the status.

``ctx.voice`` (``Settings``) switches it on (``cabinet.json`` ``"transcribe"``); off — audio are plain files again,
and what still waited in the line ends ``off`` at the start (``close_off``): its messages wake as ones whose audio
was not understood.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
import json
import os
import sys
import time
import traceback
from typing import Callable, List, Mapping, Optional

from kd import transcribe_client
from kd.cabinet import perms, store as store_mod

# what the thread plays is what is transcribed: what MediaRecorder writes and the common audio files
AUDIO_TYPES = frozenset(
    {"audio/webm", "audio/ogg", "audio/mp4", "audio/mpeg", "audio/wav", "audio/aac", "audio/x-m4a"})
# "Retry": the executor or the way to it failed — bad_request too: the cabinet's own header, not the recording;
# off — the switch closed the line before its turn: taken again once the switch is on
RETRY = ("error", "unavailable", "bad_request", "off")
RERECORD = ("too_long", "too_large", "not_audio", "empty")  # the recording itself: record it again
STATUSES = ("ok",) + RETRY + RERECORD + ("deleted",)  # deleted: the file went before its turn
PREFIX = "[голосовое, расшифровка]"
FAILED = "[голосовое] аудио не расшифровано"
INTERVAL = 30.0  # seconds between looks at the queue when nothing kicks the loop
STAMP = "%Y-%m-%dT%H:%M:%SZ"


@dataclass
class Settings:
    """The queue's switch and the executor's way: ``ask(audio, header, path=, timeout=)`` (tests stub it)."""

    enabled: bool = False
    language: str = "ru"
    socket: str = transcribe_client.SOCKET
    timeout: float = transcribe_client.TIMEOUT
    ask: Callable = field(default=transcribe_client.ask)


def on(ctx) -> bool:
    found = getattr(ctx, "voice", None)
    return bool(found is not None and found.enabled)


def audio_ids(ctx, author: str, files: list) -> List[str]:
    """The files of a person's new message that are to be transcribed (none when the queue is off)."""
    if not on(ctx) or not author.startswith("user:"):
        return []
    return [f["id"] for f in files if f.get("mime") in AUDIO_TYPES]


def language_of(ctx, author: str) -> str:
    """The author's language (users.language), else the cabinet's."""
    person = ctx.users.get(author[len("user:"):]) if ctx.users is not None else None
    language = (person or {}).get("language") or ctx.voice.language
    return language if language in ("ru", "en") else "ru"


# --- the queue (in the caller's transaction) ---------------------------------------------------------------------
def enqueue(ctx, message: Mapping, thread: int, author: str) -> int:
    """Rows for the message's files marked ``queued`` (a send repeated after a lost answer adds nothing)."""
    s = ctx.store
    now = s._clock()
    added = 0
    for f in _voiced(message):
        if f["voice"].get("state") != "queued":
            continue
        cursor = s._conn.execute(
            "INSERT OR IGNORE INTO transcripts (file, thread, seq, author, language, state, queued, created)"
            " VALUES (?,?,?,?,?,'queued',?,?)",
            (f["id"], thread, message["seq"], author, language_of(ctx, author), now, now))
        added += cursor.rowcount
    if added:
        _event(s, author, "voice.queued", thread, {"seq": message["seq"]})
    return added


def internal(event: Mapping) -> bool:
    """An event of the line itself — queued, ready, failed, a message's audio state — not news for the feed or a
    card's history: the message under the audio says it, the push tells its author."""
    kind = str(event.get("kind", ""))
    return kind.startswith("voice.") or (kind == "message.changed" and bool((event.get("payload") or {}).get("voice")))


def _voiced(message: Mapping) -> list:
    return [f for f in (message.get("payload") or {}).get("files") or []
            if isinstance(f, dict) and isinstance(f.get("voice"), dict)]


def _event(s, actor: str, kind: str, thread: int, payload: dict) -> int:
    info = s.thread_info(thread)
    space = store_mod.SPACE_OF.get(info["kind"])
    if space:  # who may see it: an agent's chat or "Monitor" has no card (event_filter), as store's own events
        payload = {**payload, "agent": info["agent"], **({"space": space} if space != "chat" else {})}
    return s._event(actor, kind, info["card"], payload, thread)


def _set_voice(s, thread: int, seq: int, file_id: str, voice: dict) -> dict:
    """The file's ``voice`` in its message and ``message.changed`` for the open pages."""
    message = s._message(thread, seq)
    payload = dict(message["payload"])
    payload["files"] = [{**f, "voice": voice} if f.get("id") == file_id and "voice" in f else f
                        for f in payload.get("files") or []]
    s._conn.execute("UPDATE messages SET payload=? WHERE thread=? AND seq=?",
                    (json.dumps(payload, ensure_ascii=False), thread, seq))
    _event(s, "system:voice", "message.changed", thread, {"seq": seq, "file": file_id, "voice": True})
    return s._message(thread, seq)


def positions(s) -> dict:
    """``file -> its place in the line`` (1 — next) for every queued audio; one being transcribed goes first."""
    running = s._conn.execute("SELECT COUNT(*) FROM transcripts WHERE state='running'").fetchone()[0]
    rows = s._conn.execute("SELECT file FROM transcripts WHERE state='queued' ORDER BY queued, rowid").fetchall()
    return {row[0]: running + i for i, row in enumerate(rows, 1)}


def placed(s, messages: list) -> list:
    """The messages with each queued audio's place in the line (``voice.position``) as it is now."""
    if not any(f["voice"].get("state") == "queued" for m in messages for f in _voiced(m)):
        return messages
    places = positions(s)
    out = []
    for m in messages:
        files = (m.get("payload") or {}).get("files")
        if any(f["voice"].get("state") == "queued" for f in _voiced(m)):
            files = [{**f, "voice": {**f["voice"], "position": places.get(f["id"], 1)}}
                     if isinstance(f, dict) and isinstance(f.get("voice"), dict) and f["voice"].get("state") == "queued"
                     else f for f in files]
            m = {**m, "payload": {**m["payload"], "files": files}}
        out.append(m)
    return out


def recover(ctx) -> int:
    """At the service's start: what was being transcribed when it stopped waits in the line again."""
    s = ctx.store
    with s.transaction():
        rows = s._conn.execute("SELECT file, thread, seq FROM transcripts WHERE state='running'").fetchall()
        for row in rows:
            s._conn.execute("UPDATE transcripts SET state='queued', started='' WHERE file=?", (row["file"],))
            _set_voice(s, row["thread"], row["seq"], row["file"], {"state": "queued"})
    return len(rows)


def close_off(ctx, err=None) -> int:
    """The switch is off: every audio still queued or running ends ``off`` — no push, the switch is no
    failure of the author's — and each of its messages settles: one with text wakes, audio alone stays a file."""
    s = ctx.store
    with s.transaction():
        rows = s._conn.execute("SELECT * FROM transcripts WHERE state IN ('queued','running')"
                               " ORDER BY queued, rowid").fetchall()
        now, messages = s._clock(), []
        for row in rows:
            s._conn.execute("UPDATE transcripts SET state='done', status='off', finished=? WHERE file=?",
                            (now, row["file"]))
            _set_voice(s, row["thread"], row["seq"], row["file"], {"state": "done", "status": "off"})
            if (row["thread"], row["seq"]) not in messages:
                messages.append((row["thread"], row["seq"]))
        for thread, seq in messages:
            try:
                with s.transaction():  # a savepoint, as in ``finish``: a wake that fails leaves the rest closed
                    settle(ctx, thread, seq)
            except Exception:  # noqa: BLE001 — the journal has it
                print(f"kd-cabinet voice: the wake of {thread}:{seq} failed", file=err or sys.stderr)
                traceback.print_exc(file=err or sys.stderr)
    if rows:
        print(f"kd-cabinet voice: off — {len(rows)} audio closed", file=err or sys.stderr, flush=True)
    return len(rows)


def take(ctx) -> Optional[dict]:
    """The next audio in the line, now ``running``: its file, message, attempt and the executor's header."""
    from kd.whisper_probe import vocab_from

    s = ctx.store
    with s.transaction():
        row = s._conn.execute(
            "SELECT * FROM transcripts WHERE state='queued' ORDER BY queued, rowid LIMIT 1").fetchone()
        if row is None:
            return None
        now = s._clock()
        s._conn.execute("UPDATE transcripts SET state='running', started=?, attempts=attempts+1 WHERE file=?",
                        (now, row["file"]))
        _set_voice(s, row["thread"], row["seq"], row["file"], {"state": "running"})
        waiting = {(r[0], r[1]) for r in s._conn.execute(
            "SELECT DISTINCT thread, seq FROM transcripts WHERE state='queued'").fetchall()}
        for thread, seq in sorted(waiting):  # the line moved: their places are new
            _event(s, "system:voice", "message.changed", thread, {"seq": seq, "voice": True})
    agents = sorted(name.lstrip("@") for name in _agents(ctx))
    return {
        "file": row["file"], "thread": row["thread"], "seq": row["seq"], "attempt": row["attempts"] + 1,
        "queued": row["queued"], "started": now,
        "header": {"language": row["language"], "vocab": vocab_from(s._conn, agents)},
    }


def _agents(ctx) -> frozenset:
    from kd.cabinet import api  # at call time: api imports this module

    return api._agents(ctx)


def work(settings: Settings, files_dir: str, item: Mapping) -> dict:
    """The executor's answer for one audio (blocking: run it in a thread); a broken way is ``unavailable``."""
    started = time.monotonic()
    try:
        with open(os.path.join(files_dir or "", item["file"]), "rb") as handle:
            audio = handle.read()
    except OSError:
        return {"status": "deleted", "text": "", "duration": None, "work": 0.0}
    try:
        answer = settings.ask(audio, item["header"], path=settings.socket, timeout=settings.timeout)
    except transcribe_client.TranscribeError as exc:
        answer = {"status": "unavailable", "text": "", "duration": None, "error": str(exc)[:300]}
    return {**answer, "work": round(time.monotonic() - started, 1)}


def finish(ctx, item: Mapping, answer: Mapping, err=None) -> Optional[dict]:
    """Write one audio's result, once: a result for an attempt that is no longer the row's changes nothing."""
    s = ctx.store
    with s.transaction():
        row = s._conn.execute("SELECT * FROM transcripts WHERE file=?", (item["file"],)).fetchone()
        if row is None or row["state"] != "running" or row["attempts"] != item["attempt"]:
            return None
        status = answer.get("status") if answer.get("status") in STATUSES else "error"
        text = str(answer.get("text") or "").strip() if status == "ok" else ""
        if status == "ok" and not text:
            status = "empty"
        duration = answer.get("duration")
        duration = float(duration) if isinstance(duration, (int, float)) and not isinstance(duration, bool) else None
        now = s._clock()
        s._conn.execute("UPDATE transcripts SET state='done', status=?, duration=?, finished=? WHERE file=?",
                        (status, duration, now, row["file"]))
        voice = {"state": "done", "status": status}
        if text:
            voice["text"] = text
        message = _set_voice(s, row["thread"], row["seq"], row["file"], voice)
        if status not in ("ok", "deleted"):
            _event(s, "system:voice", "voice.failed", row["thread"],
                   {"seq": row["seq"], "file": row["file"], "author": row["author"], "status": status})
        _journal(s, row, status, duration, answer, now, err)
        try:
            with s.transaction():  # a savepoint: a wake that fails (the registry unreadable, the card gone) leaves
                settle(ctx, row["thread"], row["seq"], message)  # the result standing — never «transcribing» for ever
        except Exception:  # noqa: BLE001 — the journal has it; a retry of another audio of the message settles again
            print(f"kd-cabinet voice: the wake of {row['thread']}:{row['seq']} failed", file=err or sys.stderr)
            traceback.print_exc(file=err or sys.stderr)
    return message


def _journal(s, row, status, duration, answer, now, err):
    info = s.thread_info(row["thread"])
    where = info["card"] or f"chat @{info['agent']}"
    wait = _seconds(row["queued"], row["started"] or now)
    length = "-" if duration is None else f"{duration:.1f}s"
    line = (f"kd-cabinet voice: {where} file {row['file'][:8]} wait {wait}s work {answer.get('work', 0)}s "
            f"length {length} status {status}")
    if answer.get("error"):
        line += f" ({answer['error']})"
    print(line, file=err or sys.stderr, flush=True)


def _seconds(start: str, end: str) -> int:
    try:
        return int((datetime.strptime(end, STAMP) - datetime.strptime(start, STAMP)).total_seconds())
    except (TypeError, ValueError):
        return 0


# --- what a message's results wake -------------------------------------------------------------------------------
def settle(ctx, thread: int, seq: int, message: Optional[Mapping] = None) -> Optional[str]:
    """Once every audio of the message has its result: the one wake it gives (``answer``, ``message``, ``chat``)
    or None. In the caller's transaction."""
    s = ctx.store
    message = message or s._message(thread, seq)
    voiced = _voiced(message)
    if not voiced or any(f["voice"].get("state") != "done" for f in voiced):
        return None
    if s._conn.execute("SELECT 1 FROM transcript_wakes WHERE thread=? AND seq=?", (thread, seq)).fetchone():
        return None
    understood = [f["voice"]["text"] for f in voiced if f["voice"].get("status") == "ok" and f["voice"].get("text")]
    text = message["body"] if message["kind"] == "text" else ""
    author = message["author"]
    decision = (message.get("payload") or {}).get("answers")
    if decision:
        found = _ledger(ctx).get(decision)
        if found is not None and found["state"] == "open":
            if not understood:
                return None  # the question stays open: "Retry" or another answer
            return _answer(ctx, found, author, message, understood, thread, seq)
    if not text and not understood:
        return None  # nothing to read: a retry that succeeds wakes then
    info = s.thread_info(thread)
    s._conn.execute("INSERT INTO transcript_wakes (thread, seq, at) VALUES (?,?,?)", (thread, seq, s._clock()))
    if info["kind"] in store_mod.SPACE_OF:  # an agent's chat or "Monitor": its own wake
        from kd.cabinet import sessions_api  # at call time: it imports api, which imports this module

        sessions_api.chat_wake(ctx, info["agent"], thread, seq, author, store_mod.SPACE_OF[info["kind"]])
        return "chat"
    from kd.cabinet import api

    record = s.card(info["card"])
    mentions = api._addressees(ctx, record, text, author)
    if mentions:
        s._event(author, "voice.ready", record.id,
                 {"seq": seq, "mentions": mentions, "wake_key": f"voice:{thread}:{seq}", "title": record.title},
                 thread)
    return "message"


def _ledger(ctx):
    from kd.cabinet import decisions

    return decisions.Decisions(ctx.store)


def _answer(ctx, found, author, message, understood, thread, seq) -> str:
    """The recording's text answers the open question: ``PREFIX …`` with its files shown."""
    from kd.cabinet import questions

    files = [{k: f[k] for k in ("id", "name", "mime", "size")} for f in _voiced(message)]
    text = (f"{PREFIX} " + "\n\n".join(understood))[:questions.READABLE_MAX]  # an answer with files: 32 KB
    s = ctx.store
    s._conn.execute("INSERT INTO transcript_wakes (thread, seq, at) VALUES (?,?,?)", (thread, seq, s._clock()))
    _ledger(ctx).reply(found["id"], user=author[len("user:"):], text=text, now=ctx.now(), answer={"files": files})
    return "answer"


# --- "Retry" -------------------------------------------------------------------------------------------------------
def retry(ctx, principal, file_id) -> dict:
    """The author or the owner puts a failed audio back in the line; only a failure of the way may be retried."""
    from kd.cabinet import api

    row = api._readable_file(ctx, principal, file_id)
    found = ctx.store._conn.execute("SELECT * FROM transcripts WHERE file=?", (row["id"],)).fetchone()
    if found is None:
        raise api.ApiError("not_found", "this file is not transcribed")
    if found["author"] != api.card_actor(principal) and not ctx.users.can(principal.user, perms.ALL):
        raise api.ApiError("refused", "only the author or the owner retries a transcription")
    if found["state"] != "done" or found["status"] not in RETRY:
        raise api.ApiError("refused", f"the transcription is {found['state']} ({found['status'] or '-'})",
                           reason="not_retryable")
    if not on(ctx):  # nobody takes the line while the switch is off
        raise api.ApiError("refused", "transcription is off in this cabinet", reason="voice_off")
    s = ctx.store
    with s.transaction():
        now = s._clock()
        s._conn.execute("UPDATE transcripts SET state='queued', status='', queued=?, started='', finished=''"
                        " WHERE file=?", (now, found["file"]))
        message = _set_voice(s, found["thread"], found["seq"], found["file"], {"state": "queued"})
        _event(s, api.card_actor(principal), "voice.queued", found["thread"], {"seq": found["seq"]})
    return message


# --- the loop ------------------------------------------------------------------------------------------------------
async def step(svc) -> bool:
    """One audio: taken under the lock, transcribed in a thread outside it, its result written under the lock."""
    ctx = svc.ctx
    item = await svc.locked(lambda: take(ctx))
    if item is None:
        return False
    answer = await asyncio.to_thread(work, ctx.voice, ctx.files_dir, item)
    try:
        await svc.locked(lambda: finish(ctx, item, answer))
    except Exception:  # noqa: BLE001 — the answer could not be written: an error "Retry" takes, not a row
        traceback.print_exc(file=sys.stderr)  # running until the next start
        failed = {"status": "error", "text": "", "duration": None, "work": answer.get("work", 0),
                  "error": "the result could not be written"}
        await svc.locked(lambda: finish(ctx, item, failed))
    return True


async def run(svc, *, interval: float = INTERVAL):
    """The queue's loop: what was running at the last stop is queued again, then one audio at a time, as long as
    there are any; a new one (``voice.queued``) kicks the loop at once. With the switch off it closes what is left
    in the line and ends."""
    if not on(svc.ctx):
        await svc.locked(lambda: close_off(svc.ctx))
        return
    await svc.locked(lambda: recover(svc.ctx))
    while True:
        svc.voice_kick.clear()
        try:
            while await step(svc):
                pass
        except Exception:  # noqa: BLE001 — the loop outlives one bad audio; the journal has the traceback
            traceback.print_exc(file=sys.stderr)
        try:
            await asyncio.wait_for(svc.voice_kick.wait(), interval)
        except asyncio.TimeoutError:
            pass


def _retry_op(ctx, principal, request):
    return {"message": retry(ctx, principal, request.get("file")), "changed": True}


def file_text(file: Mapping) -> str:
    """What an agent reads under an audio file's line (kd-task view/thread/message/chat): its transcription, or
    why there is none (yet)."""
    voice = file.get("voice") if isinstance(file.get("voice"), dict) else None
    if voice is None:
        return ""
    state, status = voice.get("state"), voice.get("status")
    if state != "done":
        return "[голосовое] расшифровывается"
    if status == "ok":
        return f"{PREFIX} {voice.get('text', '')}"
    return f"{FAILED} ({status})"


OPS = {"user.voice.retry": (frozenset({"user"}), _retry_op)}
