"""Show a directive and its answer on the card, best effort.

The directive store is the authority; the comment only lets the owner see on the board what the
coordinator asked and what the worker answered. A comment that cannot be written (the card is
gone, the board unavailable) is skipped: the caller's operation stands. one ``card.comment``
through the cabinet as the author's agent; the cabinet takes the card's current revision itself.
"""

import os
from typing import Optional

from kd import boss_board, boss_exec, cap_units, delegation, directives
from kd.cabinet import client as cabinet_client


def comment(card_id, text, author, *, client) -> bool:
    agent = boss_board.ACTOR if author == boss_board.AUTHOR else author[1:]
    try:
        client.call({"op": "card.comment", "agent": agent, "card": card_id, "author": author, "text": text})
    except cabinet_client.CabinetError:
        return False
    return True


SWITCH = cap_units.grants_path("boss", "base")  # the coordinator's channel switch


def grant_problem(card_id: str, *, children: str, grants: str, owner_uid: Optional[int], now: float) -> Optional[str]:
    """A subtask (by the children journal) is directed only while its owner grant is active."""
    try:
        grant_id = delegation.grant_of_child(children, card_id)
    except delegation.Failure:
        return f"{card_id}: the subtask journal is not readable"
    if grant_id is None:
        return None
    try:
        grant = delegation.load_grant(grants, grant_id, owner_uid)
    except delegation.GrantError:
        grant = None
    if grant is None or not grant.active(now):
        return f"{card_id} is a subtask of the owner's grant {grant_id[:8]}, which is revoked or expired"
    return None


def revoked(item, *, children: str, grants: str, grant_owner: Optional[int], switch: str,
            now: float) -> Optional[str]:
    """Why the owner took back what a directive rested on (the coordinator's switch, the grant of a subtask), or None.
    Checked on directives the worker may already have; a card that only moved on is not such a reason (the worker
    moves its card to Review before it answers)."""
    if not os.path.isfile(switch):
        return "the coordinator is switched off"
    if item.kind in directives.GRANT_KINDS:
        return grant_problem(item.card, children=children, grants=grants, owner_uid=grant_owner, now=now)
    return None


def problem(item, *, client, children: str, grants: str, grant_owner: Optional[int], switch: str,
            now: float) -> Optional[str]:
    """Why a directive no longer applies, or None: checked again at delivery and when the worker reads it."""
    if not os.path.isfile(switch):
        return "the coordinator is switched off"
    try:
        card = boss_exec.load_card(client, item.card)
    except boss_exec.Failure as exc:
        if exc.code != boss_exec.EXIT_REFUSED:  # the board did not answer: nothing is decided, nothing withdrawn
            raise directives.DirectiveError(f"{item.card}: {exc}") from exc
        return f"{item.card}: {exc}"
    if card.assignees != (f"@{item.agent}",) or card.status in ("Done", "Cancelled"):
        return f"{item.card} is no longer yours to work on"
    if card.status not in directives.KIND_STATUSES[item.kind]:
        return (f"{item.card} is {card.status or 'without status'} now: a {item.kind} directive needs "
                + " / ".join(directives.KIND_STATUSES[item.kind]))
    if item.kind in directives.GRANT_KINDS:
        return grant_problem(item.card, children=children, grants=grants, owner_uid=grant_owner, now=now)
    return None
