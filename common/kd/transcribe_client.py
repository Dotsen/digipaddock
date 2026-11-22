"""The caller's side of kd-transcribe: one voice message over the socket, one answer back.

``ask()`` is what the cabinet (voice 2) calls: it sends the header line and the recording, half-closes its
side (the executor reads to EOF) and reads the one JSON line. ``smoke`` (transcribe_setup.sh check, as the cabinet's
user) sends a second of silence as a probe and expects ``probe_ok``: the socket's group and mode, the unit's user,
sandbox and no network, the venv and the model loaded and decoding are all on that path.
"""

from __future__ import annotations

import io
import json
import socket
import sys
import wave
from collections.abc import Mapping
from typing import Any

SOCKET = "/run/digipaddock-transcribe/transcribe.sock"
TIMEOUT = 900  # the unit's RuntimeMaxSec
ANSWER_MAX = 1024 * 1024


class TranscribeError(RuntimeError):
    """No answer: the socket is closed, the executor died or answered garbage — the caller offers "Retry"."""


def ask(audio: bytes, header: Mapping[str, Any], *, path: str = SOCKET, timeout: float = TIMEOUT) -> dict:
    line = json.dumps(dict(header), ensure_ascii=False).encode("utf-8") + b"\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(timeout)
            conn.connect(path)
            conn.sendall(line + audio)
            conn.shutdown(socket.SHUT_WR)
            chunks, size = [], 0
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > ANSWER_MAX:
                    raise TranscribeError("the answer is over 1 MiB")
    except OSError as exc:
        raise TranscribeError(f"{type(exc).__name__}: {exc}") from None
    lines = b"".join(chunks).decode("utf-8", "replace").splitlines()
    try:
        answer = json.loads(lines[-1]) if lines else None
    except ValueError:
        answer = None
    if not isinstance(answer, dict) or "status" not in answer:
        raise TranscribeError("no answer line")
    return answer


def silence(seconds: float = 1.0, rate: int = 16000) -> bytes:
    """A WAV of silence: the smoke test's recording."""
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * int(seconds * rate))
    return out.getvalue()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] != ["smoke"] or len(args) > 2:
        print("usage: transcribe_client.py smoke [<socket>]", file=sys.stderr)
        return 2
    try:
        answer = ask(silence(), {"language": "ru", "probe": True}, path=args[1] if len(args) > 1 else SOCKET,
                     timeout=300)
    except TranscribeError as exc:
        print(f"kd-transcribe smoke: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(answer, ensure_ascii=False))
    if answer.get("status") != "probe_ok":
        print(f"kd-transcribe smoke: expected probe_ok, got {answer.get('status')}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
