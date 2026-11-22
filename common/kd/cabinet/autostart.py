"""A brief of size S starts by itself.

The analyst sizes every brief it hands over: a ``size:S``, ``size:M`` or ``size:L`` label beside ``brief-ready``.
An S brief needs no "Approve": when the analyst's handoff sets ``brief-ready`` on an S card in Inbox, the cabinet
approves that version and moves the card to To Do on its implementer in the same transaction, and tells the owner
in the card's thread; the owner may still send it back (a message in the thread, or the card to Inbox).

The analyst never sizes as S what touches servers, writes to production, secrets, security or money; the cabinet
checks what it can see on its own: a brief with "Operations" (a grant) or "Stages", a card for the servers' agent,
its project or with servers named, a card not yet on its implementer, open dependencies or a specialization the
taker lacks keep the usual road — the coordinator proposes the start and the owner approves it — and the thread
says why. Only the size the handoff itself gave counts (the ``brief.version`` event carries it): a ``size:S``
left on the card from an earlier version never starts a new one.
"""

from typing import Mapping, Optional

from kd.cabinet import briefs, notices

SIZES = ("S", "M", "L")
SMALL = briefs.SIZE + "S"
SERVERS = (
    "@sysadmin"  # its cards change managed servers: never without the owner's signature
)
ACTOR = "system"
SOURCE = "pipeline"
STARTED = "notice.brief.size_s_started"
REASONS = ("operations", "stages", "servers", "blocked")  # notice.brief.size_s_<reason>


def why_not(ctx, record) -> Optional[str]:
    """Why the S brief of ``record`` keeps the owner's "Approve" (one of ``REASONS``), or None."""
    from kd.cabinet import flow  # at call time: flow imports the API

    if record.operations:
        return "operations"
    if record.stages:
        return "stages"
    agent = flow.target(ctx, record)
    if (
        SERVERS in (agent, record.assignee, ctx.routing().projects.get(record.project))
        or record.servers
    ):
        return "servers"
    # the start only moves the card: the analyst's handoff must already have put it on its implementer
    if agent != record.assignee or flow.check_start(ctx, record, agent) is not None:
        return "blocked"
    return None


def on_brief_version(ctx, event: Mapping) -> bool:
    """``brief.version`` the analyst's handoff recorded (inside the write's transaction, the card already written):
    an S brief in Inbox starts on its implementer. True when it started."""
    payload = event.get("payload") or {}
    if (
        event.get("kind") != "brief.version"
        or not event.get("card")
        or payload.get("origin") != "agent"
        or payload.get("size")
        != "S"  # the size this very handoff gave, never a label left from before
        or str(event.get("actor", "")) != "agent:" + briefs.ANALYST[1:]
    ):
        return False
    s = ctx.store
    record = s.card(event["card"])
    sizes = [v for v in record.labels if v.startswith(briefs.SIZE)]
    if (
        sizes != [SMALL]
        or record.status != "Inbox"
        or briefs.BRIEF_READY not in record.labels
    ):
        return False
    seen = briefs.state(s, record)
    if seen["state"] != "ready" or seen["approved"] or not seen["version"]:
        return False
    from kd.cabinet import councils  # at call time: it loads the API

    if councils.effective(s, record).get("brief"):
        return False  # a brief with a council is the owner's to start, whatever its critics said
    try:
        reason = why_not(ctx, record)
    except Exception:  # noqa: BLE001 — the routes or the registry unreadable: the owner's "Approve" decides
        reason = "blocked"
    if reason is not None:
        notices.post_in_tx(
            s,
            source=SOURCE,
            level="info",
            key=f"notice.brief.size_s_{reason}",
            card=record.id,
            params={"version": seen["version"]},
        )
        return False
    briefs.approve(s, record, by=ACTOR)
    after = s.edit_card(
        record.id,
        None,
        {
            "status": "To Do",
            "comment": {
                "text": f"[{ACTOR}] To Do: ТЗ v{seen['version']} размера S — в работу без «Одобрить»",
                "author": ACTOR,
            },
        },
        actor=ACTOR,
    )
    notices.post_in_tx(
        s,
        source=SOURCE,
        level="info",
        key=STARTED,
        card=record.id,
        params={"version": seen["version"], "agent": after.assignee},
    )
    return True
