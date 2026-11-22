"""pipeline problems — raised once by key, silent in observe, BOSS's in enforce, the owner's on BOSS's card."""

import pytest
from cabinet_env import CabinetEnv

from kd import pipeline_config, session_config
from kd.cabinet import problems


@pytest.fixture
def cab(tmp_path):
    env = CabinetEnv(tmp_path)
    env.ctx.host_config = lambda: session_config.HostConfig({"boss": 1, "dev": 2})
    return env


def enforce(cab):
    cab.ctx.pipeline = lambda: pipeline_config.Config(mode="enforce")


def card(cab, assignee="@dev"):
    return cab.store.create_card({"title": "Work", "project": "digipaddock", "status": "In Progress", "assignee": assignee,
                                  "created_by": "@owner"}, actor="user:owner")


def kinds(cab, after):
    return [e["kind"] for e in cab.store.events_since(after)]


def raise_missing(cab, record, key="missing:c1"):
    return problems.raise_problem(cab.ctx, key=key, record=record, kind="missing_next_step", agent="dev", command="c1")


def test_problem_raised_once_by_key(cab):
    record = card(cab)
    seq = cab.store.last_seq()
    first = raise_missing(cab, record)
    assert first["kind"] == "missing_next_step" and first["state"] == "open" and first["agent"] == "dev"
    assert raise_missing(cab, record) is None
    assert [p["id"] for p in problems.open_for(cab.store, record.id)] == [first["id"]]
    assert kinds(cab, seq) == ["problem.raised"]


def test_problem_in_observe_wakes_nobody(cab):
    record = card(cab)
    seq = cab.store.last_seq()
    raise_missing(cab, record)
    assert cab.store.commands(card=record.id) == []
    assert "notice" not in kinds(cab, seq)


def test_problem_in_enforce_asks_boss(cab):
    enforce(cab)
    record = card(cab)
    found = raise_missing(cab, record)
    [command] = cab.store.commands(card=record.id)
    assert (command["agent"], command["kind"], command["thread"]) == ("boss", "signal", record.thread)
    assert command["key"] == f"problem:{found['id']}"
    assert command["text"] == problems.PROBLEM_WAKE["missing_next_step"].format(id=record.id, agent="dev")


def test_problem_on_boss_card_goes_to_the_owner(cab):
    enforce(cab)
    record = card(cab, assignee="@boss")
    seq = cab.store.last_seq()
    problems.raise_problem(cab.ctx, key="missing:b1", record=record, kind="missing_next_step", agent="boss")
    assert cab.store.commands(card=record.id) == []
    [notice] = [m for m in cab.store.thread_messages(record.thread) if m["kind"] == "notice"]
    assert notice["author"] == "system:pipeline" and notice["payload"]["key"] == "notice.pipeline.missing_next_step"
    assert "notice" in kinds(cab, seq)


def test_problem_without_boss_on_the_host_goes_to_the_owner(cab):
    enforce(cab)
    cab.ctx.host_config = lambda: session_config.HostConfig({"dev": 2})
    record = card(cab)
    problems.raise_problem(cab.ctx, key="limit:x", record=record, kind="continuation_limit", agent="dev")
    assert cab.store.commands(card=record.id) == []
    assert [m["payload"]["key"] for m in cab.store.thread_messages(record.thread) if m["kind"] == "notice"] == [
        "notice.pipeline.continuation_limit"]


def test_resolve_closes_open_problems_of_the_kinds(cab):
    record, other = card(cab), card(cab)
    raise_missing(cab, record)
    problems.raise_problem(cab.ctx, key="limit:y", record=record, kind="continuation_limit", agent="dev")
    raise_missing(cab, other, key="missing:c2")
    seq = cab.store.last_seq()
    assert problems.resolve(cab.store, record.id, ("missing_next_step",), by="turn:c9") == 1
    assert [p["kind"] for p in problems.open_for(cab.store, record.id)] == ["continuation_limit"]
    assert kinds(cab, seq) == ["problem.resolved"]
    assert {p["card"] for p in problems.open_rows(cab.store, [record.id, other.id])} == {record.id, other.id}
    assert problems.open_rows(cab.store, []) == []


def test_every_problem_kind_has_a_wake_and_a_notice():
    from kd.cabinet import i18n
    from kd.cabinet.schema import PROBLEM_KINDS

    assert set(problems.PROBLEM_WAKE) == set(PROBLEM_KINDS)
    for language in ("ru", "en"):
        catalog = i18n.load()[language]
        assert all(f"notice.pipeline.{kind}" in catalog for kind in PROBLEM_KINDS)
