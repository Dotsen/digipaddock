"""A card's result (remediation spec §8, R26): what the owner is asked to accept.

``view`` gathers what the card already has — its summary, plan and notes, the AC and DoD with their ticks, the
links written in the summary and the notes — and its Review iterations, read from the journal: each entry
into Review is one iteration, with who sent it and the comment written with it; the next move out of Review is
its outcome (accepted into Done, returned into work, with who decided and the note they left). Nothing is
stored beside the card: a move never erases an earlier iteration.
"""

import json
import re
from typing import List

from kd.cabinet import api

REVIEW, DONE = "Review", "Done"
RETURNED_TO = ("In Progress", "To Do")
LINK = re.compile(r"https?://[^\s<>()\[\]\"']+")
LINK_TAIL = ".,;:!?"


def links(*texts: str) -> List[str]:
    """The URLs in ``texts`` in order, each once; a sentence's closing punctuation is not part of one."""
    out: List[str] = []
    for text in texts:
        for found in LINK.findall(text or ""):
            url = found.rstrip(LINK_TAIL)
            if url not in out:
                out.append(url)
    return out


def _moves(store, card_id: str) -> List[dict]:
    """The card's status moves, oldest first, each with the comment written in the same write: the event names
    that comment's message (``comment_seq``); an event written before it did is matched to the ``message.added``
    right before it, as that write made them."""
    rows = store._conn.execute(
        "SELECT e.ts, e.actor, e.payload, COALESCE(m.body, o.body, '') AS note FROM events e "
        "LEFT JOIN messages m ON m.thread = e.thread AND m.seq = json_extract(e.payload, '$.comment_seq') "
        "LEFT JOIN events p ON json_extract(e.payload, '$.comment_seq') IS NULL AND p.seq = e.seq - 1 "
        "AND p.kind = 'message.added' AND json_extract(p.payload, '$.via') = 'card.changed' "
        "LEFT JOIN messages o ON o.thread = e.thread AND o.seq = json_extract(p.payload, '$.seq') "
        "WHERE e.card = ? AND e.kind = 'card.changed' ORDER BY e.seq",
        (card_id,),
    ).fetchall()
    out = []
    for row in rows:
        payload = json.loads(row["payload"])
        before = (payload.get("before") or {}).get("status")
        after = (payload.get("after") or {}).get("status")
        if before and after and before != after:
            out.append({"at": row["ts"], "actor": row["actor"], "from": before, "to": after, "note": row["note"]})
    return out


def reviews(store, card_id: str) -> List[dict]:
    out: List[dict] = []
    for move in _moves(store, card_id):
        if move["to"] == REVIEW:
            out.append(
                {
                    "iteration": len(out) + 1,
                    "entered": move["at"],
                    "by": move["actor"],
                    "entered_note": move["note"],
                    "outcome": "pending",
                    "decided": "",
                    "decided_by": "",
                    "note": "",
                }
            )
        elif move["from"] == REVIEW and out and out[-1]["outcome"] == "pending":
            outcome = "accepted" if move["to"] == DONE else "returned" if move["to"] in RETURNED_TO else "other"
            out[-1].update(outcome=outcome, decided=move["at"], decided_by=move["actor"], note=move["note"])
    return out


def view(store, record) -> dict:
    return {
        "summary": record.summary,
        "plan": record.plan,
        "notes": record.notes,
        "acceptance": [{"text": a.text, "checked": a.checked} for a in record.acceptance],
        "dod": record.dod,
        "links": links(record.summary, record.notes),
        "reviews": reviews(store, record.id),
        "release": _release(store, record.id),  # the release's state from the facts
    }


def _release(store, card_id: str) -> dict:
    from kd.cabinet import releases

    return releases.state(store, card_id)


def _result(ctx, principal, request) -> dict:
    """``card.result {card}`` → ``{result}`` of a card the caller may read."""
    record = api._visible_card(ctx, principal, request.get("card"))
    return {"result": view(ctx.store, record)}


OPS = {"card.result": (api.READERS, _result)}
api.OPS.update(OPS)  # the board API serves it (api imports this module at its end)
