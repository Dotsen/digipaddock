"""The execution core of BOSS board operations, shared by ``kd-boss-board`` and the owner's promote page.

Moved out of the CLI: the approval service runs the owner's ``promote`` with
the same routing check, card read, rate limits and audit journal (intent before the write, result
after) as every other BOSS operation. the board is the cabinet. The request is decided here
on the card read through the cabinet (a refusal or a stale revision writes no audit and spends no
rate), then sent as one typed call that the cabinet decides again under its lock: a card changed
meanwhile is a conflict.
"""

import json
import os
import re
import uuid
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: tests only
    fcntl = None

from kd import agents, boss_board, ids, specializations
from kd.cabinet import client as cabinet_client
from kd.manifest import ManifestError

AUDIT = "/var/log/digipaddock-board/boss.jsonl"
PER_HOUR = 60
CREATES_PER_DAY = 20
CREATES = ("create-inbox", "create-child")  # one daily limit for every new card
AUDIT_TAIL = 512 * 1024

EXIT_ERROR, EXIT_REFUSED, EXIT_CONFLICT, EXIT_RATE = 1, 2, 3, 5
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")
_CARD = ids.CARD
_CODES = {"conflict": EXIT_CONFLICT, "refused": EXIT_REFUSED, "invalid": EXIT_REFUSED, "not_found": EXIT_REFUSED}


class Failure(Exception):
    """``applied``: the board edit ran, only its audit result was lost (never re-run it)."""

    def __init__(self, code: int, message: str, applied: bool = False):
        super().__init__(message)
        self.code = code
        self.applied = applied


def clean(text: object, limit: int = 300) -> str:
    return _CONTROL.sub(" ", str(text)).strip()[:limit]


def load_card(client, task_id: str) -> boss_board.Card:
    """The card as the cabinet holds it now; an unknown id is a refusal, a dead board an error."""
    if not isinstance(task_id, str) or not _CARD.fullmatch(task_id):
        raise Failure(EXIT_REFUSED, "invalid card")
    try:
        return cabinet_client.boss_card(client, task_id)
    except cabinet_client.CabinetError as exc:
        if exc.kind == "not_found":
            raise Failure(EXIT_REFUSED, f"{task_id}: card not found") from exc
        raise Failure(_CODES.get(exc.kind, EXIT_ERROR), f"{task_id}: {exc}") from exc
    except boss_board.Refused as exc:
        raise Failure(EXIT_REFUSED, f"{task_id}: {exc}") from exc


def load_routing(client, registry_path: str) -> boss_board.Routing:
    """The board's projects as the cabinet holds them now (its projects table is the only source) over
    the registered agents; an absent cabinet or an unreadable registry is an error, never an empty board."""
    try:
        routes = cabinet_client.routes(client)
    except cabinet_client.CabinetError as exc:
        raise Failure(EXIT_ERROR, f"board projects are not readable ({exc})") from exc
    try:
        registered = set(agents._load(registry_path))
    except (OSError, ValueError) as exc:
        raise Failure(EXIT_ERROR, "agent registry is not readable") from exc
    if not isinstance(routes, dict):
        raise Failure(EXIT_ERROR, "board projects are invalid")
    targets = {
        t[1:]
        for t in routes.values()
        if isinstance(t, str) and t.startswith("@") and t != boss_board.OWNER
    }
    try:
        return boss_board.routing_from(routes, registered | targets)
    except boss_board.Refused as exc:
        raise Failure(EXIT_ERROR, f"board projects are invalid: {exc}") from exc


class Audit:
    """Append-only JSON lines under an exclusive lock held for the whole operation."""

    def __init__(self, path: str):
        flags = (
            os.O_RDWR
            | os.O_APPEND
            | os.O_CREAT
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            # 0660: under the directory's default ACL the group bits are the mask, keeping the approval
            # service's inherited append right on a recreated journal; no ACL — umask
            self.fd = os.open(path, flags, 0o660)
            if fcntl is not None:
                fcntl.flock(self.fd, fcntl.LOCK_EX)
        except OSError as exc:
            raise Failure(
                EXIT_ERROR, "audit journal is unavailable; nothing was changed"
            ) from exc

    def close(self):
        os.close(self.fd)

    def recent(self, now: float):
        size = os.fstat(self.fd).st_size
        os.lseek(self.fd, max(0, size - AUDIT_TAIL), os.SEEK_SET)
        data = os.read(self.fd, min(size, AUDIT_TAIL))
        for line in data.decode("utf-8", "replace").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if (
                isinstance(record, dict)
                and record.get("phase") == "intent"
                and isinstance(record.get("at"), (int, float))
            ):
                if now - record["at"] < 86400:
                    yield record

    def append(self, record: dict) -> None:
        data = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode()
        try:
            os.write(self.fd, data)
            os.fsync(self.fd)
        except OSError as exc:
            raise Failure(
                EXIT_ERROR, "audit journal is unavailable; nothing was changed"
            ) from exc


def rate_limit(audit: Audit, op: str, now: float) -> None:
    recent = list(audit.recent(now))
    if sum(1 for r in recent if now - r["at"] < 3600) >= PER_HOUR:
        raise Failure(EXIT_RATE, f"rate limit: {PER_HOUR} board changes per hour")
    if (
        op in CREATES
        and sum(1 for r in recent if r.get("op") in CREATES) >= CREATES_PER_DAY
    ):
        raise Failure(EXIT_RATE, f"rate limit: {CREATES_PER_DAY} new cards per day")


def load_specs(repo_root=specializations.REPO_ROOT, *, require_root_owner=True) -> dict:
    """``@agent`` -> installed specializations from the deployed tree; unreadable is an error."""
    root = Path(repo_root)
    try:
        registry = specializations.load_registry(root / "common" / "specializations",
                                                 require_root_owner=require_root_owner)
        return specializations.agent_specs(registry, root)
    except (specializations.SpecError, ManifestError, OSError) as exc:
        raise Failure(EXIT_ERROR, f"specialization registry is not readable ({exc.__class__.__name__}: {exc})")


def cabinet_request(request, grant=None, by=None) -> dict:
    """The typed call of one BOSS request: ``boss.<op>`` as the coordinator; a person's promote (no
    grant: a decision in the cabinet) is ``card.promote`` naming that person (``by``, ``user:<id>``) and
    BOSS as the proposer whose reason it carries; a subtask's promote carries the owner's grant."""
    if request["op"] == "promote" and grant is None:
        call = {"op": "card.promote", "card": request["task"], "revision": request["revision"],
                "reason": request["reason"], "proposed_by": boss_board.AUTHOR}
        if by is not None:
            call["by"] = by
        return call
    call = {k: v for k, v in request.items() if k != "actor"}
    call.update(op=f"boss.{request['op']}", agent=boss_board.ACTOR)
    if grant is not None:
        call["grant"] = grant
    return call


def execute(
    request,
    *,
    client,
    registry_path,
    audit_path,
    caller,
    now,
    audit_extra=None,
    specs=None,
    specs_loader=None,
    grant=None,
    written=None,
):
    """Run one BOSS request; (card id, new revision) on success, ``Failure`` otherwise. ``written`` (a dict) gets the
    cabinet's answer — a create that returned an existing card says so.

    ``audit_extra`` adds fields to both audit records (the promote page: proposal id and decision);
    ``specs`` is the ``@agent`` -> specializations map, or ``specs_loader`` produces it only when the request
    involves a specialization (``boss_board.needs_specs``).
    ``grant``: a subtask's promote under the owner's grant (``kd.delegation``).
    """
    routing = load_routing(client, registry_path)
    card, deps = None, None
    if request["op"] not in CREATES:
        card = load_card(client, request["task"])
        if request["op"] == "unblock":
            deps = {d: load_card(client, d) for d in card.dependencies if _CARD.fullmatch(d)}
    if specs is None and specs_loader is not None and boss_board.needs_specs(request, card):
        specs = specs_loader()
    try:
        boss_board.plan(request, routing, card, deps, specs=specs)
    except boss_board.StaleRevision as exc:
        raise Failure(
            EXIT_CONFLICT,
            f"{request['task']}: {exc}; re-read the card and decide again",
        ) from exc
    except boss_board.Refused as exc:
        raise Failure(EXIT_REFUSED, str(exc)) from exc
    audit = Audit(audit_path)
    try:
        stamp = now()
        rate_limit(audit, request["op"], stamp)
        op_id = uuid.uuid4().hex
        base = {
            "id": op_id,
            "op": request["op"],
            "caller": caller,
            "card": request.get("task", ""),
            **(audit_extra or {}),
        }
        look = {}
        if request["op"] == "create-inbox":  # the duplicate check lives here, not in the card
            look = {
                "reviewed": list(request["reviewed"]),
                "difference": clean(request["difference"], boss_board.REASON_MAX),
            }
        audit.append(
            {
                **base,
                "phase": "intent",
                "at": stamp,
                "revision": request.get("revision", ""),
                "reason": clean(request["reason"], boss_board.REASON_MAX),
                **look,
            }
        )
        try:
            person = caller if isinstance(caller, str) and caller.startswith("user:") else None
            answer = client.call(cabinet_request(request, grant, by=person))
            if written is not None:
                written.update(answer)
            card_id, revision, message, code = answer["card"]["id"], answer["card"]["revision"], "", 0
        except cabinet_client.CabinetError as exc:
            card_id, revision, message = request.get("task", ""), None, str(exc)
            code = _CODES.get(exc.kind, EXIT_ERROR)
        try:
            audit.append(
                {
                    **base,
                    "phase": "result",
                    "at": now(),
                    "card": card_id,
                    "status": code,
                    "newRevision": revision or "",
                    "message": clean(message),
                }
            )
        except Failure as exc:
            if code != 0:
                raise
            raise Failure(
                EXIT_ERROR,
                f"{card_id}: changed (revision {revision}), but the audit result was not written",
                applied=True,
            ) from exc
    finally:
        audit.close()
    if code == 0:
        return card_id, revision
    hints = {EXIT_CONFLICT: "the card changed; re-read it and decide again"}
    raise Failure(
        code,
        f"{card_id or request['op']}: {hints.get(code) or clean(message) or 'board write failed'}",
    )
