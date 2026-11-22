"""Closing a card without the work: BOSS's typed operations, never a general edit.

``boss.close-duplicate {agent, task, revision, canonical, canonical_revision, reason, wishes?}`` — an unstarted Inbox
card that repeats a live canonical card: Cancelled with outcome ``duplicate``; its unique wishes go to the canonical
card's thread as BOSS's note (the canonical brief and AC are not rewritten), its queued commands are cancelled and its
waits closed. ``boss.close-cancelled {agent, task, revision, reason, source}`` — an unstarted Inbox card nobody needs:
``source`` is the owner's own word — ``{"card": KD-n, "seq": n}`` or ``{"chat": seq}`` of a person's message that names
the card — or ``"own"``: BOSS's own card made in error, with nobody else's word on it and never approved.

Both refuse when the work has begun (a command delivered or started, a worker job, an open PR, an open decision other
than BOSS's own promote) — a race with a start ends in a refusal and a re-read — and when a revision moved. Cancelled
satisfies no dependency (``waits`` reroutes those who wait for it); the owner restores the card to Inbox
(``flow`` ``restore``), which clears its outcome; the history keeps both. BOSS never restores.
"""

import re
from typing import Mapping

from kd.cabinet import api, jobs, outcomes, store

REASON_MAX = 1000
WISHES_MAX = 4000
COORDINATOR = "boss"


def _text(value, name, limit, *, required=True) -> str:
    if not required and (value is None or (isinstance(value, str) and not value.strip())):
        return ""
    return api._text(value, name, limit=limit).strip()


def _card(ctx, value) -> store.CardRecord:
    if not isinstance(value, str) or not store.CARD_ID.fullmatch(value):
        raise api.ApiError("invalid", "a card is KD-<n>")
    try:
        return ctx.store.card(value)
    except store.NotFound:
        raise api.ApiError("not_found", f"{value}: no such card")


def _revision(request, key: str) -> int:
    value = request.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise api.ApiError("invalid", f"{key} is the revision you read")
    return value


def unstarted(ctx, record: store.CardRecord) -> None:
    """Refused unless the card is an Inbox card no work has begun on (brief §6.1)."""
    if record.status != "Inbox":
        raise api.ApiError("refused", f"{record.id} is {record.status}: only an unstarted Inbox card is closed here")
    conn = ctx.store._conn
    started = conn.execute(
        "SELECT id, state FROM commands WHERE card=? AND state IN ('delivered', 'started') LIMIT 1", (record.id,)
    ).fetchone()
    if started is not None:
        raise api.ApiError("refused", f"{record.id}: work has begun (a command is {started['state']}) — re-read it")
    # a queued command the dispatcher has leased is on its way to the host outside the lock: it counts as begun
    leased = conn.execute(
        "SELECT id FROM commands WHERE card=? AND state='queued' AND lease_until > ? LIMIT 1", (record.id, ctx.now())
    ).fetchone()
    if leased is not None:
        raise api.ApiError("refused", f"{record.id}: a command is being delivered — re-read it in a minute")
    job = conn.execute(
        "SELECT id FROM jobs WHERE card=? AND state IN (%s) LIMIT 1" % ",".join("?" * len(jobs.ACTIVE)),
        (record.id, *jobs.ACTIVE),
    ).fetchone()
    if job is not None:
        raise api.ApiError("refused", f"{record.id}: a worker job runs on it")
    if ctx.store.prs(card=record.id, state="open"):
        raise api.ApiError("refused", f"{record.id}: its pull request is open")
    for decision in api._ledger(ctx).list(state="open", card=record.id):
        if decision["type"] != "promote":  # BOSS's own promote closes with the move
            raise api.ApiError("refused", f"{record.id}: a {decision['type']} decision is open on it")


def _close(ctx, record, revision, changes, guards, note: str) -> store.CardRecord:
    """The move into Cancelled, its comment, the queued commands cancelled and the card's waits closed — one write."""
    s = ctx.store
    with s.transaction():
        try:
            after = s.edit_card(record.id, revision, {**changes, "comment": {"text": note, "author": "@boss"}},
                                actor="agent:boss", guards=guards)
        except store.Conflict as exc:
            raise api.ApiError("conflict", f"{exc}; re-read it")
        except store.Invalid as exc:
            raise api.ApiError("invalid", str(exc))
        for command in s.commands(card=record.id, states=("queued",), limit=1000):
            s.command_state(command["id"], "cancelled", reason="card_cancelled", actor="agent:boss")
        s._conn.execute(
            "UPDATE waits SET state='cancelled', reason='card_cancelled', updated=? WHERE card=? "
            "AND state IN ('pending', 'open')", (s._clock(), record.id))
        outcomes.released(ctx, record.id)
    return after


def _close_duplicate(ctx, principal, request) -> dict:
    api._executor_agent(ctx, principal, request, coordinator=True)
    record = _card(ctx, request.get("task"))
    canonical = _card(ctx, request.get("canonical"))
    revision, canonical_revision = _revision(request, "revision"), _revision(request, "canonical_revision")
    reason = _text(request.get("reason"), "reason", REASON_MAX)
    wishes = _text(request.get("wishes"), "wishes", WISHES_MAX, required=False)
    if canonical.id == record.id:
        raise api.ApiError("refused", "a card is no duplicate of itself")
    if canonical.status == "Cancelled":  # only a live card is canonical: no chain, so no cycle either
        raise api.ApiError("refused", f"{canonical.id} is closed without the work itself: name the live card")
    if record.revision != revision:
        raise api.ApiError("conflict", f"{record.id}: revision {revision} is not current ({record.revision}); re-read it")
    unstarted(ctx, record)
    note = f"[boss] Закрыта как дубль {canonical.id}: {reason}"
    with ctx.store.transaction():
        after = _close(ctx, record, revision, {"status": "Cancelled", "outcome": "duplicate",
                                               "canonical": canonical.id},
                       {canonical.id: canonical_revision}, note)
        text = f"[boss] {record.id} закрыта как дубль этой карточки: {reason}"
        if wishes:
            text += f"\n\nПожелания из {record.id}: {wishes}"
        ctx.store.add_message(canonical.id, author="agent:boss", kind="text", body=text)
    return {"card": after.to_dict(), "changed": True}


def _owner_word(ctx, record, source) -> str:
    """The person's own message that cancels the card: on its thread, or in BOSS's chat naming it (brief §6)."""
    if source == "own":
        if record.created_by != "@boss":
            raise api.ApiError("refused", f"{record.id} is not BOSS's own card: the owner's word is needed")
        others = ctx.store._conn.execute(
            "SELECT 1 FROM messages WHERE thread=? AND author NOT IN ('agent:boss', 'system') "
            "AND author NOT LIKE 'system:%' LIMIT 1", (record.thread,)).fetchone()
        if others is not None:
            raise api.ApiError("refused", f"{record.id}: someone else has written on it — the owner decides")
        approved = ctx.store._conn.execute(
            "SELECT 1 FROM brief_versions WHERE card=? AND approved_by != '' LIMIT 1", (record.id,)).fetchone()
        if approved is not None:
            raise api.ApiError("refused", f"{record.id}: its brief was approved — the owner decides")
        return "own"
    if not isinstance(source, Mapping):
        raise api.ApiError("invalid", 'source is {"card": KD-n, "seq": n}, {"chat": seq} or "own"')
    seq = source.get("seq") if "card" in source else source.get("chat")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise api.ApiError("invalid", "source names a message by its seq")
    if "card" in source:
        thread = _card(ctx, source["card"]).thread
        if source["card"] != record.id:
            raise api.ApiError("refused", "the owner's word is on the card itself or in BOSS's chat")
    else:
        thread = ctx.store.agent_thread(COORDINATOR)
    found = ctx.store._conn.execute(
        "SELECT author, body, kind FROM messages WHERE thread=? AND seq=?", (thread, seq)).fetchone()
    if found is None or found["kind"] != "text" or not found["author"].startswith("user:"):
        raise api.ApiError("refused", "the source is no person's message")
    derived = ctx.store._conn.execute(  # a comment the cabinet wrote for a person's action is no word of theirs
        "SELECT 1 FROM events WHERE kind='message.added' AND thread=? AND json_extract(payload, '$.seq')=? "
        "AND json_extract(payload, '$.via') IS NOT NULL LIMIT 1", (thread, seq)).fetchone()
    if derived is not None:
        raise api.ApiError("refused", "the source is the cabinet's note of an action, not the person's own words")
    person = found["author"][5:]
    if ctx.users is None or not ctx.users.can(person, "cards.transition", record.project):
        raise api.ApiError("refused", f"{person} may not cancel cards of {record.project}")
    if "chat" in source and not re.search(rf"(?<![A-Za-z0-9-]){re.escape(record.id)}(?![0-9])", found["body"]):
        raise api.ApiError("refused", f"the message does not name {record.id}")  # KD-12 is no word about KD-1
    return found["author"]


def _close_cancelled(ctx, principal, request) -> dict:
    api._executor_agent(ctx, principal, request, coordinator=True)
    record = _card(ctx, request.get("task"))
    revision = _revision(request, "revision")
    reason = _text(request.get("reason"), "reason", REASON_MAX)
    if record.revision != revision:
        raise api.ApiError("conflict", f"{record.id}: revision {revision} is not current ({record.revision}); re-read it")
    unstarted(ctx, record)
    word = _owner_word(ctx, record, request.get("source"))
    basis = "своя ошибочная карточка BOSS" if word == "own" else f"слово {word.replace('user:', '@')}"
    after = _close(ctx, record, revision, {"status": "Cancelled", "outcome": "cancelled"}, None,
                   f"[boss] Закрыта без выполнения ({basis}): {reason}")
    return {"card": after.to_dict(), "changed": True}


OPS = {
    "boss.close-duplicate": (frozenset({"executor"}), _close_duplicate),
    "boss.close-cancelled": (frozenset({"executor"}), _close_cancelled),
}
api.OPS.update(OPS)  # the board API serves them (api imports this module at its end)
