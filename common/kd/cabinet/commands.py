"""Commands to agents on the session host from the board's own events.

The rules are kd-board sync's (``kd.board.wake_items``): a card that became «to work» for an agent, the owner's
«take it into work», a person's message naming an agent, an answer to an agent's question. For an agent on the
session host the cabinet writes the command in the same transaction as the event (``install``), so a write and
the intent it carries land together; kd-board sync leaves these agents alone. The command text is fixed and never
carries a card title or a person's words: the agent reads the card itself.
"""

from typing import Callable, Mapping, Optional

from kd.cabinet import briefs, store as store_mod

OWN_EVENTS = ("command.", "run.", "session.", "job.", "turn.", "wait.", "problem.", "pipeline.")
CARD_GONE, CARD_DONE, REASSIGNED, NOT_HOSTED = "card_gone", "card_done", "reassigned", "not_hosted"
# a continuation's basis: the card as it was when the cabinet queued it
SCOPE_CHANGED, STATUS_CHANGED, ITERATION_CHANGED = "scope_changed", "status_changed", "iteration_changed"
GRANT_REVOKED, GRANT_EXPIRED = "grant_revoked", "grant_expired"
ASSIST_MOVED = "assist_moved"  # a helper's wake whose help went to another role or ended
BRIEF_LEFT_INBOX = "brief_left_inbox"  # «propose the start» of a card already out of Inbox (an S brief)
IN_COUNCIL = "in_council"  # «propose the start» of a brief its council still reads
COUNCIL_MOVED = "council_moved"  # the writer's wake of a round its council no longer waits for it in
_WORKING = ("To Do", "In Progress", "Blocked")
_ENDS_AT_DONE = ("assigned", "message", "continue", "urgent")


def derive(event: Mapping) -> list:
    """The command specs (without a thread) one journal event asks for, for any agent."""
    if str(event.get("kind", "")).startswith(OWN_EVENTS):
        return []
    out = []
    from kd import board  # at call time: kd.board loads the cabinet's client, which loads api and server

    # a transcribed message wakes by its message's key: a retry of its audio never wakes twice
    wake_key = (event.get("payload") or {}).get("wake_key") if event.get("kind") == "voice.ready" else None
    for item in board.wake_items(board.changes_from([dict(event)])):
        out.append(
            {
                "key": f"{wake_key or 'ev:' + str(event['seq'])}:{item['agent']}",
                "agent": item["agent"],
                "card": item["card"],
                "kind": item["kind"],
                "text": item["text"],
                "initiator": str(event.get("actor") or ""),
                "event": event["seq"],
            }
        )
    return out


def install(store, hosted: Callable[[str], bool], pipeline: Optional[Callable] = None) -> None:
    """Every event of ``store`` from now on queues the commands it asks for the agents ``hosted`` names; with
    ``pipeline`` (``waits.hook``) that hook sees the event first and queues the commands itself."""

    def hook(s, event):
        specs = []
        for spec in derive(event):
            if hosted(spec["agent"]):
                thread = event.get("thread") or s.card(spec["card"]).thread
                specs.append({**spec, "thread": thread})
        if pipeline is None:
            for spec in specs:
                s._enqueue_in_tx(spec)
        else:
            pipeline(s, event, specs, s._enqueue_in_tx)

    store.on_event = hook


def stale_reason(store, command: Mapping, hosted: Callable[[str], bool],
                 grant: Optional[Callable[[str], Optional[str]]] = None) -> Optional[str]:
    """Why a queued command no longer holds, or None (the dispatcher cancels it with this reason). A command with
    a basis (a continuation) holds only while the card is as it was: its holder, in work, its scope, its review
    round and the owner's grant it works under (``grant`` says why that grant no longer holds)."""
    if not hosted(command["agent"]):
        return NOT_HOSTED
    if not command["card"]:
        return None
    try:
        card = store.card(command["card"])
    except store_mod.NotFound:
        return CARD_GONE
    if card.terminal and command["kind"] in _ENDS_AT_DONE:  # Done, or closed without the work
        return CARD_DONE
    if command["kind"] in ("assigned", "continue", "urgent") and card.assignee != "@" + command["agent"]:
        return REASSIGNED
    if _brief_wake(command):
        if card.status != "Inbox":
            return BRIEF_LEFT_INBOX
        from kd.cabinet import councils  # at call time: it loads the API, which loads this module

        if councils.open_for(store, card.id, "brief") is not None:
            return IN_COUNCIL  # the council's end tells the coordinator again
    basis = command.get("basis") or {}
    reason = _basis_reason(store, card, basis, grant) if basis else None
    return reason or _assist_reason(store, command) or _council_reason(store, command)


def _council_reason(store, command: Mapping) -> Optional[str]:
    """A council's wake of its writer (``council:<id>:round:<n>:writer``) holds while that council waits for the
    writer in that round: cancelled, ended or gone to its next round, it wakes nobody."""
    parts = str(command.get("key") or "").split(":")
    if len(parts) != 5 or (parts[0], parts[2], parts[4]) != ("council", "round", "writer"):
        return None
    from kd.cabinet import councils  # at call time, as above

    found = councils.row(store, parts[1])
    if found is None or found["state"] != "needs_writer" or str(found["round"]) != parts[3]:
        return COUNCIL_MOVED
    return None


def _brief_wake(command: Mapping) -> bool:
    from kd import board  # at call time, as in ``derive``

    return command["kind"] == "signal" and command.get("text") == board.BRIEF_WAKE.format(id=command["card"])


def _assist_reason(store, command: Mapping) -> Optional[str]:
    """A helper's wake (``assist:<id>:help:…``) holds while its help is open and assigned to that agent."""
    parts = str(command.get("key") or "").split(":")
    if len(parts) < 3 or parts[0] != "assist" or parts[2] != "help":
        return None
    found = store._conn.execute("SELECT state, helper FROM assists WHERE id=?", (parts[1],)).fetchone()
    if found is None or found["state"] not in ("assigned", "accepted") or found["helper"] != command["agent"]:
        return ASSIST_MOVED
    return None


def _basis_reason(store, card, basis: Mapping, grant) -> Optional[str]:
    from kd.cabinet import result  # at call time: result registers its operation on the API

    if card.assignee != basis.get("assignee"):
        return REASSIGNED
    working = card.status in _WORKING or (card.status == "Inbox" and briefs.BRIEF_SPEC in card.labels)
    if card.status != basis.get("status") and not working:  # moved out of work (a card queued in Review stays)
        return STATUS_CHANGED
    if briefs.scope_sha(briefs.scope(card))[:16] != basis.get("scope"):
        return SCOPE_CHANGED
    if len(result.reviews(store, card.id)) != basis.get("iteration", 0):
        return ITERATION_CHANGED
    if basis.get("grant") and grant is not None:
        return grant(basis["grant"])
    return None


def grant_check(ctx, now: float) -> Callable[[str], Optional[str]]:
    """Why the owner's grant to split a goal no longer holds: revoked in the cabinet or in its file, or past its
    days; None while it holds or cannot be read (nothing then to take back)."""
    from kd import delegation

    def check(grant_id: str) -> Optional[str]:
        row = ctx.store._conn.execute("SELECT state FROM decisions WHERE type='delegate' AND grant_id=?",
                                      (grant_id,)).fetchone()
        if row is not None and row[0] == "revoked":
            return GRANT_REVOKED
        root = getattr(ctx.promote, "grants", None)
        if not root:
            return None
        try:
            found = delegation.load_grant(root, grant_id)
        except delegation.GrantError:
            return None
        if found.state != delegation.ACTIVE:
            return GRANT_REVOKED
        return GRANT_EXPIRED if now >= found.expires else None

    return check


def enqueue_signal(store, *, agent: str, text: str, card: str = "", initiator: str, key: str,
                   space: str = "chat") -> dict:
    """An explicit command (BOSS, the monitor, a worker's result): in the card's thread, or in the agent's own
    ``space`` — its chat, or "Monitor" for the monitor's wakes."""
    with store.transaction():
        thread = store.card(card).thread if card else store.agent_thread(agent, space)
        return store._enqueue_in_tx(
            {"key": key, "agent": agent, "thread": thread, "card": card, "kind": "signal", "text": text,
             "initiator": initiator}
        )
