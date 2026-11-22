"""A card's result — summary, plan, notes, AC and DoD, links and its Review iterations."""

import sys
from pathlib import Path

import pytest

from kd.cabinet import api

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cabinet_env import CabinetEnv  # noqa: E402


@pytest.fixture
def env(tmp_path):
    return CabinetEnv(tmp_path)


def move(env, card, status, actor, comment=""):
    changes = {"status": status}
    if comment:
        changes["comment"] = {"text": comment, "author": actor}
    return env.store.edit_card(card.id, None, changes, actor=actor)


def result(env, card, user="root"):
    return env.call(user, {"op": "card.result", "card": card.id})["result"]


def test_a_card_without_a_result(env):
    card = env.card()
    found = result(env, card)
    assert found["summary"] == "" and found["plan"] == "" and found["notes"] == ""
    assert found["links"] == [] and found["reviews"] == []


def test_two_review_iterations_with_their_outcomes(env):
    card = env.card()
    move(env, card, "In Progress", "agent:dev")
    move(env, card, "Review", "agent:dev", "PR #12 готов")
    move(env, card, "In Progress", "user:owner", "Нет теста на пустой ввод")
    move(env, card, "Review", "agent:dev")
    move(env, card, "Done", "user:owner")
    reviews = result(env, card)["reviews"]
    assert [(r["iteration"], r["outcome"]) for r in reviews] == [(1, "returned"), (2, "accepted")]
    first, second = reviews
    assert first["by"] == "agent:dev" and first["entered_note"] == "PR #12 готов"
    assert first["decided_by"] == "user:owner" and first["note"] == "Нет теста на пустой ввод"
    assert first["entered"] and first["decided"]
    assert second["entered_note"] == "" and second["note"] == "" and second["decided_by"] == "user:owner"


def test_an_iteration_still_under_review_is_pending(env):
    card = env.card()
    move(env, card, "Review", "agent:dev")
    (only,) = result(env, card)["reviews"]
    assert only["outcome"] == "pending" and only["decided"] == "" and only["decided_by"] == ""


def test_links_come_from_the_summary_and_the_notes_once_each(env):
    card = env.card()
    env.put(
        card.id,
        summary="Готово: https://forge.example/pr/12. Релиз — https://forge.example/r/3",
        notes="См. https://forge.example/pr/12 и (https://docs.example/a)",
    )
    found = result(env, card)
    assert found["links"] == [
        "https://forge.example/pr/12",
        "https://forge.example/r/3",
        "https://docs.example/a",
    ]
    assert found["summary"].startswith("Готово")


def test_the_result_of_a_card_out_of_sight(env):
    env.users.create("anna", "Анна", roles=[("partner", ["web"])], actor="root")
    card = env.card(project="digipaddock")
    with pytest.raises(api.ApiError) as exc:
        api.dispatch(env.ctx, api.Principal("user", -1, "anna"), {"op": "card.result", "card": card.id})
    assert exc.value.kind == "not_found"


def test_the_comment_of_a_move_is_found_even_with_an_event_between(env, monkeypatch):
    card = env.card()
    real = type(env.store)._event

    def wedged(self, actor, kind, *args, **kwargs):
        if kind == "card.changed":
            real(self, actor, "card.wake", card.id, {})  # some other write's event lands between the two
        return real(self, actor, kind, *args, **kwargs)

    monkeypatch.setattr(type(env.store), "_event", wedged)
    move(env, card, "Review", "agent:dev", "PR #7 готов")
    (only,) = result(env, card)["reviews"]
    assert only["entered_note"] == "PR #7 готов"
