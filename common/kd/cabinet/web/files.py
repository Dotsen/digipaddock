"""Files in threads: a message's text and files in one upload, a picture's preview, deletion.

Bytes live in ``cfg.files_dir/<id>`` (the service's user, 0600); the base keeps the name, the type, the size and
the sha256. An upload is read as it streams in, each file straight to its own file in ``files_dir`` (never whole in
memory): a file over ``api.FILE_MAX`` or a message over ``api.MESSAGE_FILES_MAX`` stops it with 413. It is all or
nothing — on any refusal (a limit, a quota, the card) the bytes already written are removed and no message is
posted. A download is ``attachment`` with ``nosniff``; only the four raster picture types (the thread's
thumbnails) and audio (its player) open ``inline``, and every file answers with a CSP that runs nothing.
"""

import hashlib
import json
import os
import re
import secrets
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from python_multipart.multipart import MultipartParser, parse_options_header

from kd.cabinet import api, voice
from kd.cabinet.web import deps
from kd.cabinet.web.cards import call, with_names
from kd.cabinet.web.errors import Failure

router = APIRouter()
NAME_MAX = 200
# played in the thread: what MediaRecorder writes (Chrome and Android webm/opus, Safari mp4) and the
# common audio files; a browser plays them and runs nothing — the same the cabinet transcribes
AUDIO_TYPES = set(voice.AUDIO_TYPES)
SAFE_TYPES = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "application/pdf",
    "text/plain",
} | AUDIO_TYPES
# shown in the thread as pictures: raster types a browser does not run (SVG, HTML and the rest are downloads)
PREVIEW_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
INLINE_TYPES = PREVIEW_TYPES | AUDIO_TYPES
FILE_ID = re.compile(r"[0-9a-f]{32}")
FIELDS = ("text", "client_key")
REPLY_FIELDS = FIELDS + ("answers", "comment", "form_sha256")  # a form's answer with files
FIELD_MAX = 256 * 1024  # a text field's bytes (the API holds the text itself to api.TEXT_MAX characters)
BODY_MAX = api.MESSAGE_FILES_MAX + (1 << 20)  # the files, the text and the multipart framing
FILE_CSP = "default-src 'none'; sandbox"


def clean_name(name: str) -> str:
    """The base name without control characters, at most 200 characters; ``file`` when nothing is left."""
    base = re.split(r"[\\/]", name or "")[-1]
    base = "".join(c for c in base if ord(c) >= 32 and c != "\x7f").strip()
    return base[:NAME_MAX] or "file"


def _mime(value) -> str:
    value = (value or "").split(";")[0].strip().lower()
    return value if api.MIME.fullmatch(value or "-") else "application/octet-stream"


def _open(directory: str):
    os.makedirs(directory, mode=0o700, exist_ok=True)
    file_id = secrets.token_hex(16)
    fd = os.open(
        os.path.join(directory, file_id),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
        0o600,
    )
    return file_id, os.fdopen(fd, "wb")


class Upload:
    """One message's multipart body as it streams in: its fields (``text``, ``client_key``) and its files, each
    written to ``directory`` as it comes; ``discard`` removes what was written."""

    def __init__(self, directory: str, fields=FIELDS):
        self.directory, self.names = directory, tuple(fields)
        self.fields = {}
        self.files = []  # id, name, mime, size, sha256 of every file written whole
        self.total = 0
        self._ids = []  # every file opened, written whole or not
        self._headers = {}
        self._name = self._value = b""
        self._part = None

    def callbacks(self) -> dict:
        return {
            "on_part_begin": self._begin,
            "on_header_field": lambda data, start, end: self._grow("_name", data[start:end]),
            "on_header_value": lambda data, start, end: self._grow("_value", data[start:end]),
            "on_header_end": self._header,
            "on_headers_finished": self._headers_done,
            "on_part_data": self._data,
            "on_part_end": self._end,
        }

    def _grow(self, attr, chunk):
        setattr(self, attr, getattr(self, attr) + chunk)

    def _begin(self):
        self._headers, self._part = {}, None

    def _header(self):
        self._headers[self._name.strip().lower()] = self._value.strip()
        self._name = self._value = b""

    def _headers_done(self):
        _, params = parse_options_header(self._headers.get(b"content-disposition", b""))
        field = params.get(b"name", b"").decode("utf-8", "replace")
        filename = params.get(b"filename")
        if filename is None:
            self._part = {"field": field, "data": bytearray()} if field in self.names else {"skip": True}
            return
        if not filename:
            self._part = {"skip": True}  # an empty file input
            return
        if len(self._ids) >= api.FILES_PER_MESSAGE:
            raise Failure(400, "invalid", f"a message carries at most {api.FILES_PER_MESSAGE} files")
        file_id, handle = _open(self.directory)
        self._ids.append(file_id)
        content_type = self._headers.get(b"content-type", b"").decode("latin-1")
        self._part = {
            "id": file_id,
            "name": clean_name(filename.decode("utf-8", "replace")),
            "mime": _mime(content_type),
            "size": 0,
            "hash": hashlib.sha256(),
            "handle": handle,
        }

    def _data(self, data, start, end):
        part, chunk = self._part, data[start:end]
        if part is None or "skip" in part:
            return
        if "field" in part:
            part["data"] += chunk
            if len(part["data"]) > FIELD_MAX:
                raise Failure(400, "invalid", f"the {part['field']} field is too long")
            return
        part["size"] += len(chunk)
        self.total += len(chunk)
        if part["size"] > api.FILE_MAX:
            raise Failure(413, "too_large", f"a file is at most {api.FILE_MAX >> 20} MB: {part['name']}")
        if self.total > api.MESSAGE_FILES_MAX:
            raise Failure(413, "too_large", f"a message's files are at most {api.MESSAGE_FILES_MAX >> 20} MB together")
        part["handle"].write(chunk)
        part["hash"].update(chunk)

    def _end(self):
        part, self._part = self._part, None
        if part is None or "skip" in part:
            return
        if "field" in part:
            self.fields[part["field"]] = part["data"].decode("utf-8", "replace")
            return
        part["handle"].close()
        self.files.append(
            {"id": part["id"], "name": part["name"], "mime": part["mime"], "size": part["size"],
             "sha256": part["hash"].hexdigest()}
        )

    def discard(self, keep=frozenset()):
        """Remove the files written (all of them, or those not in ``keep``)."""
        if self._part and "handle" in self._part:
            self._part["handle"].close()
        for file_id in self._ids:
            if file_id not in keep:
                Path(self.directory, file_id).unlink(missing_ok=True)


async def receive(request: Request, directory: str, fields=FIELDS) -> Upload:
    """The request's multipart body, streamed into ``directory``; a refusal leaves nothing behind."""
    kind, params = parse_options_header(request.headers.get("content-type", ""))
    if kind != b"multipart/form-data" or not params.get(b"boundary"):
        raise Failure(400, "invalid", "the body must be multipart/form-data")
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > BODY_MAX:
        raise Failure(413, "too_large", f"a message's files are at most {api.MESSAGE_FILES_MAX >> 20} MB together")
    upload = Upload(directory, fields)
    parser = MultipartParser(params[b"boundary"], upload.callbacks())
    seen = 0
    try:
        async for chunk in request.stream():
            seen += len(chunk)
            if seen > BODY_MAX:
                raise Failure(413, "too_large", f"a message's files are at most {api.MESSAGE_FILES_MAX >> 20} MB together")
            parser.write(chunk)
        parser.finalize()
    except Failure:
        upload.discard()
        raise
    except Exception:
        upload.discard()
        raise Failure(400, "invalid", "the upload is broken")
    except BaseException:  # the request cancelled mid-stream (a client gone, the service stopping)
        upload.discard()
        raise
    return upload


async def _post(request: Request, user: deps.User, op: dict):
    service = deps.svc(request)
    upload = await receive(request, service.cfg.files_dir)
    op = {**op, "files": upload.files, "client_key": upload.fields.get("client_key", "")}
    if "text" in upload.fields:
        op["text"] = upload.fields["text"]
    try:
        message = (await call(request, user, op))["message"]
    except BaseException:
        upload.discard()
        raise
    # a send repeated after a lost answer gets the message written the first time: this time's bytes go
    kept = {f.get("id") for f in (message.get("payload") or {}).get("files", [])}
    upload.discard(keep=kept)
    return with_names(service, [message])[0]


@router.post("/api/cards/{card_id}/files")
async def card_upload(card_id: str, request: Request, user: deps.User = Depends(deps.writer)):
    """A message of text and files in a card's thread: ``file`` (repeated), ``text``, ``client_key``."""
    await call(request, user, {"op": "card.get", "card": card_id})  # 404 before reading anything
    return await _post(request, user, {"op": "user.card.comment", "card": card_id})


@router.post("/api/agents/{name}/files")
async def chat_upload(name: str, request: Request, user: deps.User = Depends(deps.writer)):
    """The same in an agent's chat."""
    await call(request, user, {"op": "user.agent.check", "agent": name})  # refused before reading anything
    return await _post(request, user, {"op": "user.agent.message", "agent": name})


@router.post("/api/agents/{name}/monitor/files")
async def monitor_upload(name: str, request: Request, user: deps.User = Depends(deps.writer)):
    """The same in an agent's "Monitor"."""
    await call(request, user, {"op": "user.agent.check", "agent": name})
    return await _post(request, user, {"op": "user.agent.message", "agent": name, "space": "monitor"})


@router.post("/api/decisions/{decision_id}/files")
async def reply_upload(decision_id: str, request: Request, user: deps.User = Depends(deps.writer)):
    """An answer to an agent's question with files: ``file`` (repeated), ``text``, ``client_key``; the
    form fields of a form answer travel as ``answers``/``comment``/``form_sha256`` JSON text."""
    await call(request, user, {"op": "decision.get", "id": decision_id})  # 404 before reading anything
    service = deps.svc(request)
    upload = await receive(request, service.cfg.files_dir, REPLY_FIELDS)
    op = {"op": "decision.reply", "id": decision_id, "files": upload.files,
          "client_key": upload.fields.get("client_key", ""), "text": upload.fields.get("text", "")}
    if "answers" in upload.fields:
        try:
            op.update(answers=json.loads(upload.fields["answers"]), comment=upload.fields.get("comment", ""),
                      form_sha256=upload.fields.get("form_sha256", ""))
        except ValueError:
            upload.discard()
            raise Failure(400, "invalid", "answers must be JSON")
    try:
        found = await call(request, user, op)
    except BaseException:
        upload.discard()
        raise
    decision = found["decision"]
    # a send repeated after a lost answer gets the answer written the first time: this time's bytes go; a voice
    # answer keeps its recording's in the thread message that waits for the transcription
    kept = [*(decision.get("answer") or {}).get("files", []), *((found.get("message") or {}).get("payload") or {})
            .get("files", [])]
    upload.discard(keep={f.get("id") for f in kept})
    return decision


@router.get("/api/files/{file_id}")
async def download(
    file_id: str,
    request: Request,
    user: deps.User = Depends(deps.current_user),
    inline: Optional[int] = None,
):
    if not FILE_ID.fullmatch(file_id):
        raise Failure(404, "not_found", "no such file")
    found = (await call(request, user, {"op": "file.get", "id": file_id}))["file"]
    path = Path(deps.svc(request).cfg.files_dir, file_id)
    if not path.is_file():
        raise Failure(404, "not_found", "the file's bytes are gone")
    mime = found["mime"] if found["mime"] in SAFE_TYPES else "application/octet-stream"
    disposition = "inline" if inline and found["mime"] in INLINE_TYPES else "attachment"
    headers = {
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(found['name'], safe='')}",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": FILE_CSP,
        "Cache-Control": "private, no-store",
    }
    return FileResponse(path, media_type=mime, headers=headers)


@router.post("/api/files/{file_id}/transcribe")
async def retranscribe(file_id: str, request: Request, user: deps.User = Depends(deps.writer)):
    """The "Retry" action: the author or the owner puts an audio whose transcription failed back in the line."""
    message = (await call(request, user, {"op": "user.voice.retry", "file": file_id}))["message"]
    return with_names(deps.svc(request), [message])[0]


@router.delete("/api/files/{file_id}")
async def delete(file_id: str, request: Request, user: deps.User = Depends(deps.writer)):
    """The file's author or the owner deletes it: the base first (the quota is free), then the bytes."""
    message = (await call(request, user, {"op": "user.file.delete", "id": file_id}))["message"]
    Path(deps.svc(request).cfg.files_dir, file_id).unlink(missing_ok=True)  # the op took only a valid id
    return with_names(deps.svc(request), [message])[0]
