"""everything a card edit sets off runs from one module, ``kd.cabinet.card_edits``."""

from kd.cabinet import card_edits, store, waits

NOW = "2026-11-04T10:00:00Z"


def test_an_edit_runs_its_reactions_from_one_place(tmp_path, monkeypatch):
    s = store.Store.open(str(tmp_path / "cabinet.db"), prefix="KD", clock=lambda: NOW)
    card = s.create_card(
        {
            "title": "Work",
            "project": "digipaddock",
            "status": "Inbox",
            "created_by": "@owner",
        },
        actor="user:owner",
    )
    seen = []
    monkeypatch.setattr(
        card_edits,
        "before_write",
        lambda st, before, changes, columns: seen.append(("before", before.revision)),
    )
    monkeypatch.setattr(
        card_edits,
        "in_write",
        lambda st, before, after, changes, actor, promoting: seen.append(
            ("in", before.revision, after.revision, actor, promoting)
        ),
    )
    s.edit_card(card.id, card.revision, {"priority": "high"}, actor="user:owner")
    assert seen == [("before", 1), ("in", 1, 2, "user:owner", False)]


def test_the_service_hands_a_card_change_to_the_same_module(monkeypatch):
    seen = []
    monkeypatch.setattr(
        card_edits, "after_event", lambda ctx, event: seen.append(event["card"])
    )

    class Ctx:
        @staticmethod
        def pipeline():
            class Mode:
                enforcing = False

            return Mode()

    run = waits.hook(Ctx())
    monkeypatch.setattr(waits, "_touched", lambda ctx, event: [])
    run(
        None,
        {"kind": "card.changed", "card": "KD-1", "payload": {}},
        [],
        lambda spec: spec,
    )
    run(
        None,
        {"kind": "message.added", "card": "KD-1", "payload": {}},
        [],
        lambda spec: spec,
    )
    assert seen == ["KD-1"]
