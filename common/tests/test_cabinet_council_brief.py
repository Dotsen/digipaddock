"""a brief under its council — its state, the owner's early approval, the coordinator held back, the
card closing or leaving under the critics."""

import council_env as ce
import pytest

from kd import board, watch
from kd.cabinet import api, briefs, commands, councils, flow, jobs


@pytest.fixture
def env(tmp_path):
    return ce.make(tmp_path)


def owner():
    return api.Principal("user", -1, "owner")


def _reading(env, **over):
    """A card whose brief the critics read: round 1 runs."""
    card = ce.draft(env, **over)
    ce.handin(env, card.id)
    return card.id, ce.council_of(env, card.id)["id"]


def _state(env, card_id):
    return briefs.state(env.store, env.store.card(card_id))


def _action(env, card_id, name="start"):
    record = env.store.card(card_id)
    return next(a for a in flow.actions(env.ctx, owner(), record) if a["id"] == name)


def _act(env, card_id, name="start", **more):
    record = env.store.card(card_id)
    item = _action(env, card_id, name)
    request = {
        "op": "user.card.act",
        "card": card_id,
        "action": name,
        "revision": record.revision,
        "brief_version": item["brief_version"],
        **more,
    }
    return api.dispatch(env.ctx, owner(), request)


def _close(env, card_id, status):
    changes = {
        "status": status,
        **({"outcome": "cancelled"} if status == "Cancelled" else {}),
    }
    env.store.edit_card(card_id, None, changes, actor="user:owner")


def _brief_wake(env, card_id):
    text = board.BRIEF_WAKE.format(id=card_id)
    return next(
        c for c in env.store.commands(agent="boss", limit=100) if c["text"] == text
    )


def test_the_brief_is_in_council_while_critics_read(env):
    card_id, council_id = _reading(env)

    seen = _state(env, card_id)

    assert (seen["state"], seen["version"], seen["approved"]) == (
        "in_council",
        1,
        False,
    )
    assert seen["council"]["id"] == council_id and seen["council"]["round"] == 1
    ce.play(env, card_id, ce.block(), ce.block())
    after = _state(env, card_id)
    assert (after["state"], after["council"]["state"]) == ("ready", "clean")


def test_a_card_without_a_council_keeps_its_brief_state_shape(env):
    card = ce.draft(env, council=None)
    ce.handin(env, card.id)

    seen = _state(env, card.id)

    assert seen["state"] == "ready" and "council" not in seen


def test_the_writers_turn_shows_as_the_brief_being_written(env):
    card_id, _ = _reading(env)
    ce.play(env, card_id, ce.block("needs_changes", [ce.finding()]), ce.block())

    seen = _state(env, card_id)

    assert (
        seen["state"] == "queued"
    )  # the analyst is woken: the brief is being reworked
    assert (
        seen["council"]["state"] == "needs_writer"
        and seen["council"]["open_findings"] == 1
    )


def test_start_is_offered_with_a_warning_and_cancels_the_council(env):
    card_id, council_id = _reading(env)
    running = [r["job"] for r in ce.runs_of(env, card_id)]

    item = _action(env, card_id)
    assert (item["label"], item["enabled"], item["brief_version"]) == (
        "action.start_brief",
        True,
        1,
    )
    assert item["council"] == {"round": 1, "rounds": 3}
    _act(env, card_id)

    record = env.store.card(card_id)
    assert record.status == "To Do"
    found = councils.row(env.store, council_id)
    assert (found["state"], found["end_reason"]) == ("cancelled", "approved_early")
    assert all(jobs.row(env.store, job)["state"] == "cancelled" for job in running)
    assert briefs.latest(env.store, card_id)["approved_by"] == "user:owner"
    kinds = [e for e in env.store.card_events(card_id) if e["kind"] == "council.ended"]
    assert kinds[-1]["actor"] == "user:owner"


def test_a_start_after_the_council_has_no_warning(env):
    card_id, _ = _reading(env)
    ce.play(env, card_id, ce.block(), ce.block())

    assert "council" not in _action(env, card_id)


def test_boss_cannot_propose_while_in_council(env):
    card_id, _ = _reading(env)
    record = env.store.card(card_id)
    request = {
        "op": "decision.open",
        "type": "promote",
        "agent": "boss",
        "card": card_id,
        "payload": {
            "task_id": card_id,
            "revision": record.revision,
            "reason": "the brief is ready",
        },
    }

    with pytest.raises(api.ApiError) as err:
        env.call("digi", request)

    assert (err.value.kind, err.value.reason) == ("refused", "in_council")
    assert "round 1 of 3" in str(err.value)
    ce.play(env, card_id, ce.block(), ce.block())
    with pytest.raises(
        api.ApiError
    ) as later:  # the council over, the proposal is read as any other
        env.call("digi", request)
    assert later.value.reason != "in_council"


def test_the_brief_wake_is_dropped_while_in_council(env):
    card_id, _ = _reading(env)
    hosted = lambda agent: True  # noqa: E731
    wake = _brief_wake(env, card_id)

    assert commands.stale_reason(env.store, wake, hosted) == "in_council"

    ce.play(env, card_id, ce.block(), ce.block())
    assert commands.stale_reason(env.store, _brief_wake(env, card_id), hosted) is None


def test_the_card_list_names_open_councils(env):
    card_id, _ = _reading(env)
    plain = env.card()

    def listed():
        cards = env.call("digi-cabinet", {"op": "card.list"})["cards"]
        return {c["id"]: c.get("councils") for c in cards}

    assert listed() == {card_id: ["brief"], plain.id: None}
    assert watch.card_snapshot(env.call("digi-cabinet", {"op": "card.list"})["cards"])[
        card_id
    ]["in_council"]
    ce.play(env, card_id, ce.block(), ce.block())
    assert listed()[card_id] is None
    snapshot = watch.card_snapshot(
        env.call("digi-cabinet", {"op": "card.list"})["cards"]
    )
    assert "in_council" not in snapshot[card_id]


def test_an_s_brief_with_a_council_waits_for_the_owner(env):
    card = ce.draft(env)

    ce.handin(env, card.id, labels_add=["spec:implementer", "brief-ready", "size:S"])

    record = env.store.card(card.id)
    assert (
        record.status == "Inbox" and ce.council_of(env, card.id)["state"] == "running"
    )
    ce.play(env, card.id, ce.block(), ce.block())
    assert (
        env.store.card(card.id).status == "Inbox"
    )  # the owner's road, not the automatic start


def test_an_s_brief_whose_council_ran_out_of_rounds_waits_for_the_owner(env):
    card = ce.draft(env, council={"brief": {"critics": ["claude:opus"], "rounds": 1}})
    ce.handin(env, card.id)
    ce.play(env, card.id, ce.block("needs_changes", [ce.finding()]))

    ce.handin(
        env,
        card.id,
        description="Goal: a report, with a timeout.",
        labels_add=["spec:implementer", "brief-ready", "size:S"],
    )

    found = ce.council_of(env, card.id)
    assert (found["state"], found["end_reason"]) == ("limited", "rounds")
    assert env.store.card(card.id).status == "Inbox"


@pytest.mark.parametrize("status", ["Done", "Cancelled"])
def test_closing_the_card_cancels_the_council(env, status):
    card_id, council_id = _reading(env)
    running = [r["job"] for r in ce.runs_of(env, card_id)]

    _close(env, card_id, status)

    found = councils.row(env.store, council_id)
    assert (found["state"], found["end_reason"]) == ("cancelled", "card_closed")
    assert all(jobs.row(env.store, job)["state"] == "cancelled" for job in running)
    assert ce.wakes(env, "analyst") == []


def test_closing_a_card_that_waits_for_its_writer_wakes_nobody(env):
    card_id, council_id = _reading(env)
    ce.play(env, card_id, ce.block("needs_changes", [ce.finding()]), ce.block())
    before = len(env.store.commands(agent="analyst", limit=100))

    env.store.edit_card(card_id, None, {"assignee": "@owner"}, actor="user:owner")
    _close(env, card_id, "Cancelled")

    assert councils.row(env.store, council_id)["state"] == "cancelled"
    assert len(env.store.commands(agent="analyst", limit=100)) == before


def _writer_wake(env, council_id, n=1):
    key = f"council:{council_id}:round:{n}:writer"
    return next(
        c for c in env.store.commands(agent="analyst", limit=100) if c["key"] == key
    )


def _waits_for_writer(env):
    card_id, council_id = _reading(env)
    ce.play(env, card_id, ce.block("needs_changes", [ce.finding()]), ce.block())
    return card_id, council_id


def _stale(env, command):
    return commands.stale_reason(env.store, command, lambda agent: True)


def test_the_writers_wake_holds_while_its_round_waits_for_the_writer(env):
    _, council_id = _waits_for_writer(env)

    assert _stale(env, _writer_wake(env, council_id)) is None


@pytest.mark.parametrize("status", ["Done", "Cancelled"])
def test_a_queued_writers_wake_is_dropped_when_the_card_closes(env, status):
    card_id, council_id = _waits_for_writer(env)

    _close(env, card_id, status)

    assert _stale(env, _writer_wake(env, council_id)) == "council_moved"


def test_a_queued_writers_wake_is_dropped_when_the_council_is_cancelled(env):
    card_id, council_id = _waits_for_writer(env)

    api.dispatch(env.ctx, owner(), {"op": "council.cancel", "council": council_id})

    assert councils.row(env.store, council_id)["state"] == "cancelled"
    assert _stale(env, _writer_wake(env, council_id)) == "council_moved"


def test_a_queued_writers_wake_is_dropped_when_the_owner_starts_the_work(env):
    card_id, council_id = _waits_for_writer(env)

    _act(env, card_id, confirm=True)

    assert councils.row(env.store, council_id)["end_reason"] == "approved_early"
    assert _stale(env, _writer_wake(env, council_id)) == "council_moved"


def test_the_writers_wake_of_an_earlier_round_is_dropped(env):
    card_id, council_id = _waits_for_writer(env)
    ce.started(env, card_id)
    ce.reply(env, card_id, {"r1.f1": "fixed"})

    ce.handin(env, card_id, description="Goal: a report, with a timeout.")

    found = councils.row(env.store, council_id)
    assert (found["state"], found["round"]) == ("running", 2)
    assert _stale(env, _writer_wake(env, council_id)) == "council_moved"


def test_a_card_moved_out_of_inbox_loses_its_brief_council(env):
    card_id, council_id = _reading(env)

    env.store.edit_card(card_id, None, {"status": "To Do"}, actor="user:owner")

    found = councils.row(env.store, council_id)
    assert (found["state"], found["end_reason"]) == ("cancelled", "moved")
    late = ce.runs_of(env, card_id)[0]["job"]
    assert jobs.row(env.store, late)["state"] == "cancelled"
    assert env.store.card(card_id).status == "To Do"  # nothing pulls it back


def test_removing_the_council_cancels_it(env):
    card_id, council_id = _reading(env)

    env.store.edit_card(
        card_id,
        None,
        {"council": {"brief": {"critics": [], "rounds": 3}}},
        actor="user:owner",
    )

    found = councils.row(env.store, council_id)
    assert (found["state"], found["end_reason"]) == ("cancelled", "removed")
    assert _state(env, card_id)["state"] == "ready"


def test_changing_the_critics_keeps_the_council_until_its_next_round(env):
    card_id, council_id = _reading(env)

    env.store.edit_card(
        card_id,
        None,
        {"council": {"brief": {"critics": ["claude:opus"], "rounds": 2}}},
        actor="user:owner",
    )

    found = councils.row(env.store, council_id)
    assert (found["state"], found["critics"]) == ("running", ["claude:opus", "or-gpt5"])
    ce.play(env, card_id, ce.block("needs_changes", [ce.finding()]), ce.block())
    ce.handin(env, card_id, description="Goal: a report, with a timeout.")
    after = councils.row(env.store, council_id)
    assert (after["round"], after["critics"], after["rounds_max"]) == (
        2,
        ["claude:opus"],
        2,
    )
