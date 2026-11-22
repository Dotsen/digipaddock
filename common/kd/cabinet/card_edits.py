"""Everything a card edit sets off, in one place and in order.

``store.edit_card`` is the only writer of a card; what follows from a write is listed here, not spread over its
callers. Three moments:

- ``before_write`` — what the write itself adjusts: a changed scope of a ready brief takes ``brief-ready`` off.
- ``in_write`` — in the edit's transaction, with the store only (it holds in every process that writes):
  ``brief-ready`` set by the write records the brief's version; BOSS's open promotes close — their approval binds
  the revision the write replaced, unless the write is that approval (``promoting``); a card that
  reached Done or Cancelled withdraws its open decisions.
- ``after_event`` — the service's reactions to the ``card.changed`` event, with its context (``waits.hook``): the
  outcomes the change released, an aggregate's stages, the card's dev checks. The brief's
  version the write records starts a brief of size S by itself (``kd.cabinet.autostart``).

The agents' wakes the event asks for are the board's rules (``kd.board``, ``kd.cabinet.commands``), not here.
"""

from typing import Mapping

from kd.cabinet import briefs


def before_write(store, before, changes: Mapping, columns: dict) -> None:
    briefs.adjust_labels(store, before, changes, columns)


def in_write(
    store, before, after, changes: Mapping, actor: str, promoting: bool
) -> None:
    from kd.cabinet import (
        decisions,
    )  # it reads the store: imported here, not at the top

    briefs.after_edit(store, before, after, changes, actor)
    ledger = decisions.Decisions(store)
    if not promoting:
        ledger.close_promotes(
            after.id,
            status=after.status,
            actor=actor,
            now=store._clock(),
            fields=changes,
        )
    if after.terminal and not before.terminal:
        ledger.close_with_card(
            after.id, status=after.status, actor=actor, now=store._clock()
        )


def after_event(ctx, event: Mapping) -> None:
    from kd.cabinet import (
        devchecks,
        outcomes,
        stage_gate,
        stages,
    )  # at call time: they import the store and the API

    from kd.cabinet import defers

    outcomes.released(ctx, event["card"])
    if ((event.get("payload") or {}).get("after") or {}).get("status") in ("Done", "Cancelled"):
        defers.clear(ctx, event["card"], how="closed")  # a closed card is put off no more
    stages.on_card_changed(ctx, event)
    stage_gate.on_card_changed(ctx, event)
    devchecks.on_card_changed(ctx, event)
