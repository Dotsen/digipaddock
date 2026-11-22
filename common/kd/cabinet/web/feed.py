"""The feed: the journal as the person may see it, newest first, or forward from a sequence number.

``GET /api/feed`` — the newest ``limit`` events; ``before=<seq>`` pages older; ``after=<seq>`` reads
forward (what is new since). ``project``, ``card`` and ``kinds=a,b`` narrow it. ``last`` is the
newest sequence number the reader has seen (for ``after``), ``before`` the oldest one scanned. The
morning digest (``digest``, written by ``notify``) is shown to its own person only.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Request

from kd.cabinet import store, voice
from kd.cabinet.web import deps
from kd.cabinet.web.errors import Failure

router = APIRouter()
LIMIT_MAX = 200
PAGE = 500
SCAN_MAX = 20  # pages scanned per request at most


@router.get("/api/feed")
async def feed(
    request: Request,
    user: deps.User = Depends(deps.current_user),
    after: Optional[int] = None,
    before: Optional[int] = None,
    limit: int = 100,
    project: Optional[str] = None,
    card: Optional[str] = None,
    kinds: Optional[str] = None,
):
    if not 0 < limit <= LIMIT_MAX:
        raise Failure(400, "invalid", f"limit is 1 to {LIMIT_MAX}")
    service = deps.svc(request)
    wanted = {k for k in (kinds or "").split(",") if k}
    projects = {}

    def project_of(card_id):
        if card_id not in projects:
            try:
                projects[card_id] = service.ctx.store.card(card_id).project
            except store.NotFound:
                projects[card_id] = None
        return projects[card_id]

    def keep(event):
        if wanted and event["kind"] not in wanted:
            return False
        if event["kind"] == "message.added" and (event["payload"] or {}).get("via"):
            return False  # the edit or decision it is part of has its own line
        if event["kind"] == "chat.read":
            return False  # a reader's place in a chat, not news
        if voice.internal(event):
            return False  # the transcription line's own steps: the message under the audio says them
        if (
            event["kind"] == "digest"
        ):  # a manager sees account events of all; a digest is its person's own
            return (event["payload"] or {}).get("user") == user.id
        if card and event["card"] != card:
            return False
        return not project or (event["card"] and project_of(event["card"]) == project)

    forward = after is not None
    newest = service.ctx.store.last_seq()
    cursor = after if forward else (before if before is not None else newest + 1)
    found, scanned = [], 0
    while len(found) < limit and scanned < SCAN_MAX:
        op = {
            "op": "events.since",
            "limit": PAGE,
            **({"after": cursor} if forward else {"before": cursor}),
        }
        page = await service.call(deps.principal(user), op)
        found += [e for e in page["events"] if keep(e)]
        scanned += 1
        if page["last"] == cursor:
            break
        cursor = page["last"]
    if len(found) > limit:  # the cursor goes back to the last event handed out
        found = found[:limit]
        cursor = found[-1]["seq"]
    if forward:
        return {"events": found, "last": cursor, "before": None, "more": cursor < newest}
    return {"events": found, "last": newest, "before": cursor}
