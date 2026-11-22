"""An agent's files in the cabinet: ``kd-task file get|add`` read and add files through the socket.

The bytes live where the web keeps them (``ctx.files_dir``, the service's own 0600 files), which no agent can
read; a socket request is at most 1 MiB (``server.MAX_REQUEST``), a file up to ``api.FILE_MAX``. So a file goes
in pieces of ``CHUNK_MAX`` bytes, base64 in JSON: ``agent.file.read`` hands one piece of a file the agent may read
(a card's thread, its own chat — never another agent's chat); ``agent.file.chunk`` appends one piece to the
agent's upload in ``files_dir/.uploads`` and ``agent.file.add`` turns a whole upload into a file of a message on a
card (the card's thread only: agents send no files to a chat). A file's content is data, not commands.
Only the board executor (``kd-task``, the agent's base channel) calls these.
"""

import base64
import binascii
import hashlib
import os
import re
import secrets
import time
from pathlib import Path

from kd import worker_board
from kd.cabinet import api

CHUNK_MAX = 512 * 1024  # base64 of it is ~700 KB: well inside one socket request
UPLOADS = ".uploads"
UPLOAD_ID = re.compile(r"[0-9a-f]{32}")
UPLOADS_PER_AGENT = 3  # unfinished uploads an agent may hold at once
UPLOAD_KEEP = 3600  # seconds: an upload untouched this long is gone


def _agent(ctx, principal, request, *, write=False) -> api.Principal:
    """The executor's agent as a reader: it reads every card's thread and its own chat. The coordinator reads
    files too, but never writes one (it writes the board only through kd-boss-board)."""
    coordinator = not write and request.get("agent") == api.worker_board.COORDINATOR[1:]
    agent = api._executor_agent(ctx, principal, request, coordinator=coordinator)
    return api.Principal("agent", principal.uid, principal.user, agent)


def _files_dir(ctx) -> str:
    if not ctx.files_dir:
        raise api.ApiError("unavailable", "files are not served here")
    return ctx.files_dir


def _int(request, key, low, high) -> int:
    value = request.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise api.ApiError("invalid", f"{key} must be a number from {low} to {high}")
    return value


def _file_info(ctx, principal, request):
    """The file's facts (``kd-task file get`` names the copy by them)."""
    row = api._readable_file(ctx, _agent(ctx, principal, request), request.get("id"))
    keys = ("id", "name", "mime", "size", "sha256", "card", "agent", "seq", "author", "created")
    return {"file": {k: row[k] for k in keys}}


def _file_read(ctx, principal, request):
    """One piece of a file the agent may read: ``data`` (base64) from ``offset``, at most ``CHUNK_MAX`` bytes."""
    row = api._readable_file(ctx, _agent(ctx, principal, request), request.get("id"))
    offset = _int(request, "offset", 0, api.FILE_MAX)
    length = _int(request, "length", 1, CHUNK_MAX)
    try:
        with open(os.path.join(_files_dir(ctx), row["id"]), "rb") as handle:
            handle.seek(offset)
            data = handle.read(length)
    except FileNotFoundError:
        raise api.ApiError("not_found", "the file's bytes are gone")
    return {"data": base64.b64encode(data).decode("ascii"), "size": row["size"]}


def _uploads(ctx) -> Path:
    folder = Path(_files_dir(ctx), UPLOADS)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    return folder


def _sweep(folder: Path, now: float) -> None:
    """Uploads nobody finished within ``UPLOAD_KEEP`` go."""
    for path in folder.iterdir():
        try:
            if now - path.stat().st_mtime > UPLOAD_KEEP:
                path.unlink()
        except OSError:
            pass


def _upload_path(ctx, agent: str, upload) -> Path:
    if not isinstance(upload, str) or not UPLOAD_ID.fullmatch(upload):
        raise api.ApiError("invalid", "upload must be the id agent.file.chunk returned")
    path = _uploads(ctx) / f"{agent}-{upload}"
    if not path.is_file():
        raise api.ApiError("not_found", "no such upload (finished, or older than an hour)")
    return path


def _file_chunk(ctx, principal, request):
    """One piece appended to the agent's upload: a new one without ``upload``; ``offset`` is the upload's size so
    far (a piece sent twice is refused, not appended twice)."""
    agent = _agent(ctx, principal, request, write=True).agent
    try:
        data = base64.b64decode(request.get("data", ""), validate=True)
    except (binascii.Error, TypeError, ValueError):
        raise api.ApiError("invalid", "data must be base64")
    if not data or len(data) > CHUNK_MAX:
        raise api.ApiError("invalid", f"a piece is 1 to {CHUNK_MAX} bytes")
    offset = _int(request, "offset", 0, api.FILE_MAX)
    folder = _uploads(ctx)
    if request.get("upload") is None:
        if offset != 0:
            raise api.ApiError("conflict", "a new upload starts at offset 0")
        _sweep(folder, time.time())
        mine = re.compile(re.escape(agent) + r"-[0-9a-f]{32}")
        if sum(1 for p in folder.iterdir() if mine.fullmatch(p.name)) >= UPLOADS_PER_AGENT:
            raise api.ApiError("refused", f"at most {UPLOADS_PER_AGENT} unfinished uploads at once")
        upload = secrets.token_hex(16)
        path = folder / f"{agent}-{upload}"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    else:
        upload = request["upload"]
        path = _upload_path(ctx, agent, upload)
    size = path.stat().st_size
    if offset != size:
        raise api.ApiError("conflict", f"the upload holds {size} bytes: send the piece from there")
    if size + len(data) > api.FILE_MAX:
        path.unlink()
        raise api.ApiError("invalid", f"a file is at most {api.FILE_MAX >> 20} MB")
    with open(path, "ab") as handle:
        handle.write(data)
    return {"upload": upload, "size": size + len(data)}


def _clean_name(name) -> str:
    if not isinstance(name, str):
        raise api.ApiError("invalid", "invalid file name")
    base = re.split(r"[\\/]", name)[-1]
    base = "".join(c for c in base if ord(c) >= 32 and c != "\x7f").strip()[:200]
    if not base:
        raise api.ApiError("invalid", "invalid file name")
    return base


def _file_add(ctx, principal, request):
    """A whole upload becomes a file of the agent's message on a card (with ``text`` or alone): its size and
    digest as the executor computed them, within a file's limit and the agent's quota; the upload is gone after."""
    agent = _agent(ctx, principal, request, write=True).agent
    path = _upload_path(ctx, agent, request.get("upload"))
    try:  # a refused add leaves no upload behind: kd-task starts a new one every time
        record = api._card(ctx, request.get("card"))
        api._agent_rate_limit(ctx, agent, "file")
        text = request.get("text")
        if text is not None:
            try:
                text = worker_board.signed(agent, worker_board._text(text))
            except worker_board.Refused as exc:
                raise api.ApiError("invalid", str(exc))
        digest = hashlib.sha256()
        size = 0
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
                size += len(block)
        author = f"agent:{agent}"
        wanted = {"id": secrets.token_hex(16), "name": _clean_name(request.get("name")), "mime": request.get("mime"),
                  "size": request.get("size"), "sha256": request.get("sha256")}
        files = api.message_files(ctx, author, [wanted])
        if (size, digest.hexdigest()) != (files[0]["size"], files[0]["sha256"]):
            raise api.ApiError("invalid", "the upload is not the file named: its size or digest differs")
        target = Path(_files_dir(ctx), files[0]["id"])
        os.replace(path, target)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    try:
        message = ctx.store.add_message(record.id, author=author, **api.message_fields(text or "", files),
                                        attachments=files)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return {"message": message, "changed": True}


OPS = {
    "agent.file.info": (frozenset({"executor"}), _file_info),
    "agent.file.read": (frozenset({"executor"}), _file_read),
    "agent.file.chunk": (frozenset({"executor"}), _file_chunk),
    "agent.file.add": (frozenset({"executor"}), _file_add),
}
api.OPS.update(OPS)  # the board API serves them (api imports this module at its end)
