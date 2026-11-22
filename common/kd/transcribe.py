"""One voice message to text: the one-shot transcription executor.

A process per voice message: the cabinet connects to a socket unit, systemd starts this process for that one
connection under its own user without network, and the process exits when the message is done — no standing
service. The unit runs ``common/bin/kd-transcribe`` with the transcription venv's python and sets
``StandardError=journal`` (with ``StandardInput=socket`` stderr would otherwise go back to the caller).
stdin: one line of JSON, the request header (``{"language": "ru", "vocab": {"agents": [...], "servers": [...],
"projects": [...], "cards": [...]}}``), then the recording's bytes to EOF. stdout: one line of JSON —
``{"status", "text", "duration", "seconds", "model"}`` (``duration`` — the recording's length, ``null`` when it was
refused before decoding; ``seconds`` — the process's own time); nothing else leaves the process: diagnostics go
to stderr (the unit's journal), and ``main`` points fd 1 at stderr so no library print reaches the answer. The
caller half-closes its side after the bytes (``shutdown(SHUT_WR)``): the recording is read to EOF. The recording is decoded in memory; the process keeps its own temporary folder (``TMPDIR``)
and removes it before it exits, whatever happens.

Statuses: ``ok``; ``empty`` (no speech recognized: silence or nothing left after the filters); ``too_long`` (over
``--max-seconds``); ``too_large``; ``not_audio`` (no decodable sound); ``bad_request``; ``error`` (the model failed);
``probe_ok`` — the answer to the installer's check (``"probe": true`` in the header): the model loaded and decoded
the recording without the VAD, inside the unit's sandbox.

Quality, from the probe of 21.10.2026: the cabinet's dictionary goes to the model as a hint
(``initial_prompt``, whole terms within the prompt tokens whisper keeps), the text is corrected by the synonyms
(misheard forms of the cabinet's words, ``--synonyms``) keeping the rest of its spelling, and the hallucinated tail
after the end of speech is cut: no conditioning on the previous text, whisper's own silence check, no segment
after the last speech the VAD found, no segment in another script (from the second one on, nothing at all), no
trailing segment the model itself doubts.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, BinaryIO

from kd.whisper_probe import PROMPT_TOKENS, load_synonyms, normalize, prompt

HEADER_MAX = 64 * 1024
AUDIO_MAX = 20 * 1024 * 1024  # a 5-minute browser recording is a few MB
SECONDS_MAX = 300  # the cabinet's recording stops at 5 minutes
PROBE_SECONDS = 5  # the installer's probe sends a second of silence
LANGUAGES = ("ru", "en")
VOCAB_KEYS = ("agents", "servers", "projects", "cards")
SAMPLE_RATE = 16000
# Hallucination filters (the tail of 21.10: Greek, Korean and Latin mixed into Russian after the end of speech).
SCRIPTS = ("LATIN", "CYRILLIC")  # the letters a Russian or English message is written in
DOUBT_LOGPROB = -1.0  # a trailing segment the model is this unsure of is dropped
NO_SPEECH = 0.6  # with a doubtful logprob: the model itself says there was no speech
SPEECH_SLACK = 0.3  # seconds: a segment starting this late after the last speech is not speech
TAIL_SPAN = 30.0  # seconds: two garbled segments this close are one hallucinated tail


class Rejected(Exception):
    """A request the executor answers with a status instead of a transcription."""

    def __init__(self, status: str, detail: str = ""):
        super().__init__(detail or status)
        self.status = status


# --- request ---------------------------------------------------------------------------------------------------


def read_request(stream: BinaryIO, header_max: int = HEADER_MAX, audio_max: int = AUDIO_MAX) -> tuple[dict, bytes]:
    """The header (language, dictionary) and the recording's bytes from one stream."""
    line = stream.readline(header_max + 1)
    if len(line) > header_max or not line.endswith(b"\n"):
        raise Rejected("bad_request", "the header is one line of JSON up to 64 KiB")
    try:
        header = json.loads(line)
    except ValueError:
        raise Rejected("bad_request", "the header is not JSON") from None
    if not isinstance(header, dict):
        raise Rejected("bad_request", "the header is not an object")
    language = header.get("language", "ru")
    if language not in LANGUAGES:
        raise Rejected("bad_request", f"language is one of {', '.join(LANGUAGES)}")
    vocab = header.get("vocab") or {}
    if not isinstance(vocab, dict) or not all(
        key in VOCAB_KEYS and isinstance(terms, list) and all(isinstance(term, str) for term in terms)
        for key, terms in vocab.items()
    ):
        raise Rejected("bad_request", f"vocab maps {', '.join(VOCAB_KEYS)} to lists of terms")
    probe = header.get("probe", False)
    if not isinstance(probe, bool):
        raise Rejected("bad_request", "probe is true or false")
    audio = stream.read(audio_max + 1)
    if len(audio) > audio_max:
        raise Rejected("too_large")
    if not audio:
        raise Rejected("not_audio", "no recording after the header")
    return {"language": language, "vocab": vocab, "probe": probe}, audio


# --- text ------------------------------------------------------------------------------------------------------


def garbled(text: str) -> bool:
    """A hallucination's mark (the tail of 21.10): a letter of another script — Greek, Hangul… Words mixing Latin
    and Cyrillic ("PRы", "commitы") are ordinary speech of this team and do not count; only letters do ("²", "½"
    are not)."""
    return any(
        unicodedata.category(char)[0] == "L" and not unicodedata.name(char, "").startswith(SCRIPTS) for char in text
    )


def keep_speech(segments: Iterable[Any], speech_end: float | None) -> list[str]:
    """The segments' texts without the hallucinated tail. Segments arrive in order (whisper decodes them lazily:
    iterating is the work). A garbled segment is dropped; the second one ends the message — a hallucination does
    not stop at one sentence, and what came after the first is its tail too. A trailing segment the model doubts
    goes: a short real "thank you" at the very end may go with it, and a message of one such segment is empty.
    Every cut is logged to stderr (the unit's journal) to tune the limits."""
    kept, first_garbled = [], None
    for segment in segments:
        if speech_end is not None and segment.start >= speech_end + SPEECH_SLACK:
            _cut("after the speech", segment)
            continue
        if garbled(segment.text):
            _cut("garbled", segment)
            if first_garbled is not None and segment.start - first_garbled[1] <= TAIL_SPAN:
                del kept[first_garbled[0]:]
                break
            first_garbled = (len(kept), segment.start)  # a lone one long ago ("β-version") is not the tail's start
            continue
        if segment.no_speech_prob > NO_SPEECH and segment.avg_logprob < DOUBT_LOGPROB:
            _cut("no speech", segment)
            continue
        kept.append(segment)
    while kept and kept[-1].avg_logprob < DOUBT_LOGPROB:
        _cut("doubtful end", kept.pop())
    return [text for text in (segment.text.strip() for segment in kept) if text]


def _cut(why: str, segment: Any) -> None:
    print(f"kd-transcribe: cut ({why}) at {segment.start:.1f} s: {len(segment.text)} chars", file=sys.stderr)


def _form(form: str) -> re.Pattern[str]:
    def token(word: str) -> str:
        return "".join("[еёэ]" if char == "е" else re.escape(char) for char in word)

    words = normalize(form).split()
    # Between a form's words only spaces and dashes: a dot or a comma is a phrase's border ("test. API" is not testapi).
    return re.compile(r"(?<!\w)" + r"[\s\-_]*".join(token(word) for word in words) + r"(?!\w)", re.IGNORECASE)


def apply_synonyms(text: str, synonyms: Mapping[str, Sequence[str]]) -> str:
    """The text with every misheard form replaced by its word as the dictionary writes it; the rest of the text
    (case, punctuation, other words) stays as the model wrote it. Longer forms first."""
    pairs = sorted(((form, word) for word, forms in synonyms.items() for form in forms), key=lambda pair: -len(pair[0]))
    for form, word in pairs:
        text = _form(form).sub(lambda _: word, text)
    return text


# --- transcription ---------------------------------------------------------------------------------------------


def transcribe(
    request: Mapping[str, Any], audio: bytes, *, engine: Any, model: str, models_dir: str, threads: int,
    beam: int, synonyms: Mapping[str, Sequence[str]], max_seconds: int = SECONDS_MAX,
) -> dict:
    """One recording to the answer's fields. ``engine`` is faster-whisper's surface: ``probe_seconds(bytes) ->
    seconds | None`` (the container's declared length; browser recordings often have none — then the decoded
    length decides), ``decode_audio``, ``WhisperModel``, ``speech_end(samples) -> seconds | None``."""
    import io

    try:
        declared = engine.probe_seconds(audio)  # the container's own length, before decoding it all
    except Exception as exc:
        raise Rejected("not_audio", f"{type(exc).__name__}: {exc}") from None
    if declared is not None and declared > max_seconds:
        raise Rejected("too_long", f"{declared:.0f} s declared")
    try:
        samples = engine.decode_audio(io.BytesIO(audio), sampling_rate=SAMPLE_RATE)
    except Exception as exc:  # av raises its own errors for anything that is not a recording
        raise Rejected("not_audio", f"{type(exc).__name__}: {exc}") from None
    duration = len(samples) / SAMPLE_RATE
    if duration > max_seconds:
        raise Rejected("too_long", f"{duration:.0f} s")
    if request.get("probe") and duration > PROBE_SECONDS:
        raise Rejected("bad_request", f"a probe is at most {PROBE_SECONDS} s")  # never a real message without filters
    speech_end = engine.speech_end(samples)
    if speech_end is None and not request.get("probe"):
        return {"status": "empty", "text": "", "duration": round(duration, 2)}
    whisper = engine.WhisperModel(
        model, device="cpu", compute_type="int8", cpu_threads=threads, download_root=models_dir,
        local_files_only=True,
    )

    def fits(text: str) -> bool:  # as faster-whisper encodes an initial prompt
        return len(whisper.hf_tokenizer.encode(" " + text.strip(), add_special_tokens=False).ids) <= PROMPT_TOKENS

    if request.get("probe"):  # the installer's check: the model loads and decodes inside the unit's sandbox
        segments, _ = whisper.transcribe(samples, language=request["language"], beam_size=1, vad_filter=False)
        for _ in segments:
            pass
        return {"status": "probe_ok", "text": "", "duration": round(duration, 2)}
    hint = prompt(request["vocab"], fits) if request["vocab"] else ""
    segments, _ = whisper.transcribe(
        samples, language=request["language"], beam_size=beam, vad_filter=True, initial_prompt=hint or None,
        condition_on_previous_text=False, word_timestamps=True, hallucination_silence_threshold=2.0,
    )
    text = apply_synonyms(" ".join(keep_speech(segments, speech_end)), synonyms)
    if not text.strip():
        return {"status": "empty", "text": "", "duration": round(duration, 2)}
    return {"status": "ok", "text": text, "duration": round(duration, 2)}


class FasterWhisper:
    """The real engine, imported only in the executor's own venv."""

    def __init__(self):
        import faster_whisper
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        self.decode_audio = faster_whisper.decode_audio
        self.WhisperModel = faster_whisper.WhisperModel
        self._vad, self._options = get_speech_timestamps, VadOptions

    def probe_seconds(self, data: bytes) -> float | None:
        import io

        import av

        with av.open(io.BytesIO(data), metadata_errors="ignore") as container:  # as decode_audio opens it
            if not container.streams.audio:
                raise ValueError("no audio stream")
            if container.duration:
                return container.duration / 1_000_000  # av.time_base
            stream = container.streams.audio[0]
            if stream.duration and stream.time_base:
                return float(stream.duration * stream.time_base)
        return None

    def speech_end(self, samples) -> float | None:
        stamps = self._vad(samples, self._options())
        return stamps[-1]["end"] / SAMPLE_RATE if stamps else None


# --- entry -----------------------------------------------------------------------------------------------------


def serve(
    stdin: BinaryIO, stdout, *, model: str, models_dir: str, threads: int, beam: int, synonyms_path: str | None,
    max_seconds: int, engine_factory: Callable[[], Any] = FasterWhisper,
) -> int:
    """Answer one request with one JSON line; the process's temporary folder goes away whatever happens."""
    started = time.monotonic()
    scratch = tempfile.mkdtemp(prefix="kd-transcribe-")
    previous = os.environ.get("TMPDIR"), tempfile.tempdir
    os.environ["TMPDIR"], tempfile.tempdir = scratch, scratch
    try:
        try:
            request, audio = read_request(stdin)
            # the vocabulary is the deployment's overlay file: a deployment may have none
            synonyms = load_synonyms(Path(synonyms_path)) if synonyms_path and Path(synonyms_path).is_file() else {}
            answer = transcribe(
                request, audio, engine=engine_factory(), model=model, models_dir=models_dir, threads=threads,
                beam=beam, synonyms=synonyms, max_seconds=max_seconds,
            )
        except Rejected as exc:
            print(f"kd-transcribe: {exc.status}: {exc}", file=sys.stderr)
            answer = {"status": exc.status, "text": "", "duration": None}
        except Exception as exc:  # the model, the engine, the synonyms file: the caller offers "Retry"
            print(f"kd-transcribe: error: {type(exc).__name__}: {exc}", file=sys.stderr)
            answer = {"status": "error", "text": "", "duration": None}
        answer.update(seconds=round(time.monotonic() - started, 2), model=model)
        stdout.write(json.dumps(answer, ensure_ascii=False) + "\n")
        stdout.flush()
        return 0
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        if previous[0] is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = previous[0]
        tempfile.tempdir = previous[1]


def main(
    argv: Sequence[str] | None = None, *, engine_factory: Callable[[], Any] = FasterWhisper, guard_stdout: bool = True,
) -> int:
    parser = argparse.ArgumentParser(prog="kd-transcribe", description="one voice message on stdin → JSON on stdout")
    parser.add_argument("--model", required=True)
    parser.add_argument("--models-dir", required=True)
    parser.add_argument("--synonyms", help="misheard forms of the cabinet's words (JSON)")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--beam", type=int, default=5)
    parser.add_argument("--max-seconds", type=int, default=SECONDS_MAX)
    args = parser.parse_args(argv)
    out, python_stdout = sys.stdout, sys.stdout
    if guard_stdout:  # the answer gets its own copy of fd 1; fd 1 (C libraries) and print() go to stderr
        sys.stdout.flush()
        out = os.fdopen(os.dup(1), "w", encoding="utf-8")
        os.dup2(2, 1)
        sys.stdout = sys.stderr
    try:
        return serve(
            sys.stdin.buffer, out, model=args.model, models_dir=args.models_dir, threads=args.threads,
            beam=args.beam, synonyms_path=args.synonyms, max_seconds=args.max_seconds, engine_factory=engine_factory,
        )
    finally:
        sys.stdout = python_stdout
        if out is not python_stdout:
            out.close()
