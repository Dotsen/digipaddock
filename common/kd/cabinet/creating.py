"""BOSS looks before it creates, and hands a brief to the analyst itself.

``boss.prepare-create {agent, intent, project}`` → ``{token, expires, candidates}``: the cards that share words with
the intent (any status — a Done card may be a regression's origin), each with its revision, and a token that holds the
normalized intent, the project and what made each candidate relevant (status, title, description, criteria).

``boss.create-inbox`` then needs ``token``, ``reviewed`` (every candidate's id: BOSS read them) and ``difference``
(why none of them is this card). In one transaction the cabinet checks again: the token is BOSS's, unexpired and of
this project; it was not spent (a repeat returns the card it made); no other token of the same intent made a card
(two creates of one intent make one card); the candidates are what BOSS saw (a changed or a new relevant card needs a
new look — a note or a heartbeat does not count); an open card of the same title is returned instead of a second one.
The token proves the look happened, not that its answer is right: the meaning is BOSS's to judge.

``boss.request-brief {agent, task, revision, comment}`` — an Inbox card goes to ``@analyst`` with ``spec:task-brief``
(the card stays in Inbox; the board's own rule wakes the analyst). When the analyst already holds it, nothing is
restarted: the comment reaches the analyst as an addition, with one wake. The hand-over closes BOSS's open start
proposal of the card (``withdrawn``): its approval bound the revision before it.
"""

import hashlib
import json
import secrets
from typing import Mapping

from kd.cabinet import api, briefs, decisions, store, users

TTL_MINUTES = 30
CANDIDATES_MAX = 20
INTENT_MAX = 500
COMMENT_MAX = 2000
SPEC_PREFIX = "spec:"


def _relevance(record: store.CardRecord) -> str:
    """What makes a card a candidate: a change of these needs a new look; a note, a run or a heartbeat does not."""
    text = json.dumps([record.status, record.title, record.description,
                       [a.text for a in record.acceptance], record.canonical], ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _candidates(ctx, intent: str) -> dict:
    return {r.id: (r, score) for r, score in ctx.store.similar(intent, CANDIDATES_MAX)}


def _project(ctx, value) -> str:
    projects = ctx.routing().projects
    if not isinstance(value, str) or value not in projects:
        raise api.ApiError("invalid", f"unknown project {value!r}")
    return value


def _prepare(ctx, principal, request) -> dict:
    api._executor_agent(ctx, principal, request, coordinator=True)
    intent = api._text(request.get("intent"), "intent", limit=INTENT_MAX).strip()
    project = _project(ctx, request.get("project"))
    found = _candidates(ctx, intent)
    now = ctx.now()
    token = secrets.token_hex(16)
    # looks never spent and long expired go (a spent one stays: it names the card its create made)
    ctx.store._conn.execute("DELETE FROM create_tokens WHERE card='' AND expires < ?", (users.shift(now, hours=-24),))
    ctx.store._conn.execute(
        "INSERT INTO create_tokens (token, intent, project, candidates, created, expires) VALUES (?,?,?,?,?,?)",
        (token, store.norm(intent), project, json.dumps({k: _relevance(r) for k, (r, _) in found.items()}),
         now, users.shift(now, minutes=TTL_MINUTES)),
    )
    return {
        "token": token,
        "expires": users.shift(now, minutes=TTL_MINUTES),
        "candidates": [{"id": r.id, "title": r.title, "status": r.status, "project": r.project,
                        "revision": r.revision, "outcome": r.outcome, "canonical": r.canonical, "score": score}
                       for r, score in found.values()],
    }


def _token(ctx, value) -> Mapping:
    if not isinstance(value, str) or len(value) != 32:
        raise api.ApiError("refused", "create-inbox needs the token of prepare-create: look first")
    row = ctx.store._conn.execute("SELECT * FROM create_tokens WHERE token=?", (value,)).fetchone()
    if row is None:
        raise api.ApiError("refused", "unknown token: run prepare-create")
    return dict(row)


def check(ctx, request) -> dict:
    """The look again, inside the create's transaction: ``{"existing": card}`` to return instead, or ``{}`` to go
    on; ``ApiError`` when the look is missing, stale or not BOSS's answer to it."""
    row = _token(ctx, request.get("token"))
    if row["card"]:
        return {"existing": ctx.store.card(row["card"])}  # a repeat of the same create
    if row["expires"] <= ctx.now():
        raise api.ApiError("refused", "the token expired: run prepare-create again")
    if row["project"] != request.get("project"):
        raise api.ApiError("refused", "the token is of another project: run prepare-create for this one")
    # the same intent created meanwhile (a parallel create: a token of the same window, its card still open) — that
    # card, not a second one; a card of the same words long ago, or closed since, is no twin
    since = users.shift(row["created"], minutes=-TTL_MINUTES)
    for twin in ctx.store._conn.execute(
            "SELECT card FROM create_tokens WHERE intent=? AND project=? AND card != '' AND created >= ?",
            (row["intent"], row["project"], since)).fetchall():
        record = ctx.store.card(twin["card"])
        if not record.terminal:
            return {"existing": record}
    seen = json.loads(row["candidates"])
    reviewed = request.get("reviewed")
    if not isinstance(reviewed, list) or not all(isinstance(c, str) for c in reviewed):
        raise api.ApiError("invalid", "reviewed lists the candidates' ids you read")
    unread = sorted(set(seen) - set(reviewed))
    if unread:
        raise api.ApiError("refused", f"read the candidates first: {', '.join(unread)}")
    difference = request.get("difference")
    if seen and not (isinstance(difference, str) and difference.strip()):
        raise api.ApiError("refused", "say why none of the candidates is this card (difference)")
    now_found = _candidates(ctx, row["intent"])
    changed = sorted(c for c, (r, _) in now_found.items() if seen.get(c) != _relevance(r))
    if changed:
        raise api.ApiError("conflict", f"the candidates changed ({', '.join(changed)}): run prepare-create again")
    title = store.norm(request.get("title"))
    for record in ctx.store.cards(project=row["project"]):
        if not record.terminal and store.norm(record.title) == title:
            return {"existing": record}  # an open card of this very title is this card, whatever the intent said
    return {}


def _create_inbox(original):
    def handler(ctx, principal, request):
        api._executor_agent(ctx, principal, request, coordinator=True)
        with ctx.store.transaction():
            found = check(ctx, request)
            if "existing" in found:
                return {"card": found["existing"].to_dict(), "existing": True}
            answer = original(ctx, principal, request)
            ctx.store._conn.execute("UPDATE create_tokens SET card=? WHERE token=?",
                                    (answer["card"]["id"], request["token"]))
            return answer

    return handler


def _request_brief(ctx, principal, request) -> dict:
    api._executor_agent(ctx, principal, request, coordinator=True)
    card = request.get("task")
    if not isinstance(card, str) or not store.CARD_ID.fullmatch(card):
        raise api.ApiError("invalid", "a card is KD-<n>")
    try:
        record = ctx.store.card(card)
    except store.NotFound:
        raise api.ApiError("not_found", f"{card}: no such card")
    revision = request.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int):
        raise api.ApiError("invalid", "revision is the one you read")
    comment = api._text(request.get("comment"), "comment", limit=COMMENT_MAX).strip()
    if record.status != "Inbox":
        raise api.ApiError("refused", f"{record.id} is {record.status}: a brief is prepared in Inbox only")
    if record.revision != revision:
        raise api.ApiError("conflict", f"{record.id}: revision {revision} is not current ({record.revision}); re-read it")
    from kd import board  # at call time: kd.board loads the cabinet's client, which loads api (commands.derive)

    text = f"[boss] {comment}"
    with ctx.store.transaction():
        if record.assignee == api.ANALYST and briefs.BRIEF_SPEC in record.labels:
            # the analyst is on it: the addition reaches it as one wake, the preparation is not started again
            message = ctx.store.add_message(record.id, author="agent:boss", kind="text", body=text)
            ctx.store.enqueue({"key": f"brief-more:{record.id}:{message['seq']}", "agent": api.ANALYST[1:],
                               "thread": record.thread, "card": record.id, "kind": "message",
                               "text": board.MESSAGE_WAKE.format(id=record.id), "initiator": "agent:boss"})
            return {"card": ctx.store.card(record.id).to_dict(), "started": False}
        # the same rule as the owner's "Write the brief" (flow._brief): another spec and a "ready" brief go
        stale = [label for label in record.labels
                 if (label.startswith(SPEC_PREFIX) and label != briefs.BRIEF_SPEC) or label == briefs.BRIEF_READY]
        changes = {"assignee": api.ANALYST, "labels_add": [briefs.BRIEF_SPEC],
                   "comment": {"text": f"{text}", "author": "@boss"}}
        if stale:
            changes["labels_remove"] = stale
        # the edit closes BOSS's open start proposals: BOSS is told, the start is proposed after the brief
        promotes = [d["id"] for d in decisions.Decisions(ctx.store).list(state="open", card=record.id)
                    if d["type"] == "promote"]
        try:
            after = ctx.store.edit_card(record.id, revision, changes, actor="agent:boss")
        except store.Conflict as exc:
            raise api.ApiError("conflict", f"{exc}; re-read it")
        return {"card": after.to_dict(), "started": True, "changed": True, "withdrawn": promotes}


OPS = {
    "boss.prepare-create": (frozenset({"executor"}), _prepare),
    "boss.request-brief": (frozenset({"executor"}), _request_brief),
}
api.OPS.update(OPS)  # the board API serves them (api imports this module at its end)
_kinds, _handler = api.OPS["boss.create-inbox"]
api.OPS["boss.create-inbox"] = (_kinds, _create_inbox(_handler))
