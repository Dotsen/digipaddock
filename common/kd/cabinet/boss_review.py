"""BOSS hands a card whose work is checked to the owner's acceptance: Review, never Done.

``boss.review {agent, task, revision, reason, source}`` moves the card from In Progress (or Blocked with every
dependency Done) to Review, with BOSS's note in its thread naming the reason and the source. ``source`` is
``{"card": KD-n, "seq": n}`` — a text message on that card's own thread that shows the work checked (anyone's,
BOSS's own too); the cabinet reads it, never BOSS's retelling.

The card is BOSS's own (``@boss``), or nobody leads it: no assignee, or an agent with no turn queued or running, no
open wait or decision and the card not put off (``outcomes.next_state`` is ``missing``). A card of the owner or of a
person is theirs. Refused also with an open pull request, open help or an open decision on the card, an acceptance
criterion not checked, and an aggregate whose stages are open (its stages move it). Done stays the owner's.
"""

from kd.cabinet import api, closing, flow, jobs, outcomes, stages, store

REASON_MAX = 1000
COORDINATOR = "@boss"
FROM = (outcomes.IN_PROGRESS, outcomes.BLOCKED)


def _source(ctx, record: store.CardRecord, source) -> dict:
    """The message on the card's own thread that shows the work checked."""
    if not isinstance(source, dict) or set(source) != {"card", "seq"}:
        raise api.ApiError(
            "invalid", 'source is {"card": KD-n, "seq": n}: a message on the card'
        )
    if source["card"] != record.id:
        raise api.ApiError("refused", f"the source is a message on {record.id} itself")
    seq = source["seq"]
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise api.ApiError("invalid", "source names a message by its seq")
    found = ctx.store._conn.execute(
        "SELECT author, kind FROM messages WHERE thread=? AND seq=?",
        (record.thread, seq),
    ).fetchone()
    if found is None:
        raise api.ApiError("refused", f"{record.id} has no message #{seq}")
    if (
        found["kind"] != "text"
        or found["author"] == "system"
        or found["author"].startswith("system:")
    ):
        raise api.ApiError(
            "refused",
            f"{record.id} #{seq} is a notice of the cabinet, not a report of the work",
        )
    return {"seq": seq, "author": found["author"]}


def _led(ctx, record: store.CardRecord) -> None:
    """Refused unless the card is BOSS's own or nobody leads it now."""
    assignee = record.assignee
    if assignee and (not assignee.startswith("@") or assignee == api.OWNER):
        raise api.ApiError(
            "refused", f"{record.id} is held by {assignee}: its Review is theirs"
        )
    _busy(ctx, record)
    if assignee and assignee != COORDINATOR:
        state = outcomes.next_state(ctx, record)
        if state != "missing":
            raise api.ApiError(
                "refused",
                f"{record.id}: {assignee} leads it ({state}); its Review is the agent's",
            )


def _busy(ctx, record: store.CardRecord) -> None:
    """Refused while another agent has a turn on the card or a worker job runs on it."""
    conn = ctx.store._conn
    live = conn.execute(
        f"SELECT agent, state FROM commands WHERE card=? AND agent!='boss' AND state IN "
        f"({','.join('?' * len(outcomes.LIVE))}) LIMIT 1",
        (record.id, *outcomes.LIVE),
    ).fetchone()
    if live is not None:
        raise api.ApiError(
            "refused",
            f"{record.id}: @{live['agent']} works on it (a turn is {live['state']})",
        )
    job = conn.execute(
        "SELECT id FROM jobs WHERE card=? AND state IN (%s) LIMIT 1"
        % ",".join("?" * len(jobs.ACTIVE)),
        (record.id, *jobs.ACTIVE),
    ).fetchone()
    if job is not None:
        raise api.ApiError("refused", f"{record.id}: a worker job runs on it")


def _guards(ctx, record: store.CardRecord) -> None:
    """What holds a card from Review even with its work checked."""
    if record.status not in FROM:
        raise api.ApiError(
            "refused",
            f"Review is from In Progress or Blocked; {record.id} is {record.status}",
        )
    if record.status == outcomes.BLOCKED:
        deps = flow.open_dependencies(ctx, record)
        if deps:
            raise api.ApiError(
                "refused", f"{record.id} waits for {', '.join(deps)} (not Done yet)"
            )
    pr = ctx.store.prs(card=record.id, state="open")
    if pr:
        raise api.ApiError(
            "refused", f"{record.id}: PR #{pr[-1]['number']} is not merged"
        )
    _unfinished(ctx, record)
    staged = stages.open_children(ctx.store, record.id)
    if staged:
        raise api.ApiError(
            "refused",
            f"{record.id} has open stages {', '.join(staged)}: they move it to Review",
        )


def _unfinished(ctx, record: store.CardRecord) -> None:
    """Refused with open help or an open decision on the card, or an acceptance criterion not checked."""
    from kd.cabinet import (
        assists,
    )  # at call time: it loads the API this module registers on

    helped = assists.open_for(ctx.store, record.id)
    if helped:
        raise api.ApiError(
            "refused",
            f"{record.id} has open help {', '.join(h['id'][:8] for h in helped)}",
        )
    decided = api._ledger(ctx).list(state="open", card=record.id)
    if decided:
        raise api.ApiError(
            "refused", f"{record.id}: a {decided[0]['type']} decision is open on it"
        )
    unchecked = [str(i) for i, a in enumerate(record.acceptance, 1) if not a.checked]
    if unchecked:
        raise api.ApiError(
            "refused",
            f"{record.id}: acceptance criteria {', '.join(unchecked)} are not checked",
        )


def _review(ctx, principal, request) -> dict:
    api._executor_agent(ctx, principal, request, coordinator=True)
    record = closing._card(ctx, request.get("task"))
    revision = closing._revision(request, "revision")
    reason = closing._text(request.get("reason"), "reason", REASON_MAX)
    if record.revision != revision:
        raise api.ApiError(
            "conflict",
            f"{record.id}: revision {revision} is not current ({record.revision}); re-read it",
        )
    _guards(ctx, record)
    _led(ctx, record)
    found = _source(ctx, record, request.get("source"))
    author = found["author"].replace("user:", "@").replace("agent:", "@")
    note = f"[boss] На проверку (Review): {reason}. Источник: сообщение #{found['seq']} ({author})"
    s = ctx.store
    with s.transaction():
        try:
            after = s.edit_card(
                record.id,
                revision,
                {
                    "status": outcomes.REVIEW,
                    "comment": {"text": note, "author": COORDINATOR},
                },
                actor="agent:boss",
            )
        except store.Conflict as exc:
            raise api.ApiError("conflict", f"{exc}; re-read it")
        except store.Invalid as exc:
            raise api.ApiError("invalid", str(exc))
        if (
            record.assignee == COORDINATOR
        ):  # BOSS's own waits on it end with its move: the owner's turn now
            s._conn.execute(
                "UPDATE waits SET state='cancelled', reason='review', updated=? WHERE card=? "
                "AND agent='boss' AND state IN ('pending', 'open')",
                (s._clock(), record.id),
            )
    return {"card": after.to_dict(), "changed": True}


OPS = {
    "boss.review": (frozenset({"executor"}), _review),
}
api.OPS.update(OPS)  # the board API serves them (api imports this module at its end)
