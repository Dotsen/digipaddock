"""a card put off until a time — by its agent (wait_until), the owner or BOSS; it wakes by itself."""

from datetime import datetime, timedelta, timezone

import pytest
from cabinet_env import CabinetEnv

from kd import attention_rules, pipeline_config, session_config, watch
from kd.cabinet import api, continuations, defers, snapshot
from test_cabinet_outcomes import begin, end, finish, question
from test_cabinet_outcomes import card as new_card

HOSTED = session_config.HostConfig({"boss": 1, "dev": 2, "analyst": 1})
START = "2026-10-14T10:00:00Z"
TWO_HOURS_MSK = "2026-10-14T15:00+03:00"  # 12:00 UTC


@pytest.fixture
def cab(tmp_path):
    env = CabinetEnv(tmp_path)
    env.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    api.install_host(env.ctx, lambda: HOSTED)
    env.ctx.pipeline = lambda: pipeline_config.Config(mode="enforce")
    return env


def card(cab, **kw):
    """A card whose wake on creation already ran: only what the test does is queued."""
    record = new_card(cab, **kw)
    for command in cab.store.commands(card=record.id, states=["queued"]):
        for state in ("delivered", "started", "done"):
            cab.store.command_state(command["id"], state)
    return record


def owner(cab, op, **request):
    return api.dispatch(
        cab.ctx, api.Principal("user", -1, "owner"), {"op": op, **request}
    )


def boss(cab, **request):
    return cab.call("digi", {"op": "boss.defer", "agent": "boss", **request})


def sweep(cab):
    return cab.call("digi-cabinet", {"op": "pipeline.sweep"})


def at(cab, stamp):
    cab.now = stamp


def due(cab, record, agent="dev"):
    """The agent's queued commands of the card the dispatcher would deliver now."""
    return [
        c
        for c in cab.store.commands(card=record.id, states=["queued"])
        if c["agent"] == agent and c["next_try"] <= cab.now
    ]


def held(cab, record, agent="dev"):
    return [
        c
        for c in cab.store.commands(card=record.id, states=["queued"])
        if c["agent"] == agent and c["next_try"] > cab.now
    ]


def deferred(cab, record, until=TWO_HOURS_MSK, **extra):
    """A turn of @dev that ended putting its card off until ``until``; its command."""
    command, session = begin(cab, record)
    finish(
        cab,
        record,
        outcome="wait_until",
        note="checking after the release",
        until=until,
        **extra,
    )
    end(cab, command, session)
    return command


def refusal(cab, record, until, kind):
    command, session = begin(cab, record)
    with pytest.raises(api.ApiError) as exc:
        finish(cab, record, outcome="wait_until", note="later", until=until)
    end(cab, command, session)
    assert exc.value.kind == kind, exc.value
    return str(exc.value)


# --- AC1: the agent's wait_until --------------------------------------------------------------------------------
def test_wait_until_puts_the_card_off_and_the_snapshot_says_so(cab):
    record = card(cab)
    command = deferred(cab, record)
    row = defers.active(cab.store, record.id)
    assert (row["until"], row["source"], row["by"], row["command"]) == (
        "2026-10-14T12:00:00Z",
        "agent",
        "agent:dev",
        command,
    )
    text = cab.call("digi", {"op": "card.snapshot", "card": record.id})["text"]
    assert "deferred: until 2026-10-14T12:00:00Z by @dev" in text
    assert "time 2026-10-14T12:00:00Z open" in text
    assert (
        snapshot.build(cab.ctx, cab.store.card(record.id))["next"]["what"] == "deferred"
    )
    assert (
        due(cab, record) == [] and held(cab, record) == []
    )  # nothing queued: the time wakes it
    assert cab.store.card(record.id).status == "In Progress"  # its column stays


@pytest.mark.parametrize(
    "until, kind, words",
    [
        ("2026-10-14T15:00", "invalid", "no time zone"),
        ("tomorrow", "invalid", "time zone"),
        ("2026-10-14T13:05+03:00", "refused", "less than 10 minutes"),
        ("2026-10-21T13:01+03:00", "refused", "more than 7 days"),
    ],
)
def test_wait_until_refuses_a_moment_out_of_its_bounds(cab, until, kind, words):
    record = card(cab)
    assert words in refusal(cab, record, until, kind)
    assert defers.active(cab.store, record.id) is None


def test_wait_until_bounds_are_inclusive(cab):
    record = card(cab)
    deferred(cab, record, until="2026-10-14T10:10:00Z")
    deferred(cab, record, until="2026-10-21T10:00:00+00:00")
    assert defers.active(cab.store, record.id)["until"] == "2026-10-21T10:00:00Z"


def test_wait_until_on_another_agents_card_is_refused(cab):
    record = card(cab, assignee="@analyst")
    begin(cab, record, agent="dev")
    with pytest.raises(api.ApiError) as exc:
        finish(cab, record, outcome="wait_until", note="later", until=TWO_HOURS_MSK)
    assert "finish is for the card's assignee" in str(exc.value)


def test_until_goes_only_with_wait_until(cab):
    record = card(cab)
    begin(cab, record)
    with pytest.raises(api.ApiError) as exc:
        finish(cab, record, outcome="continue", until=TWO_HOURS_MSK)
    assert exc.value.kind == "invalid"
    with pytest.raises(api.ApiError) as exc:
        finish(cab, record, outcome="wait_until", note="later")
    assert exc.value.kind == "invalid"


# --- AC2: the time comes ----------------------------------------------------------------------------------------
def test_the_time_wakes_the_agent_once_on_the_next_sweep(cab):
    record = card(cab)
    deferred(cab, record)
    at(cab, "2026-10-14T11:59:59Z")
    sweep(cab)
    assert due(cab, record) == [] and defers.active(cab.store, record.id) is not None
    at(cab, "2026-10-14T12:00:20Z")
    sweep(cab)
    sweep(cab)
    [wake] = due(cab, record)
    assert wake["kind"] == "continue" and wake["text"] == continuations.TIME.format(
        id=record.id
    )
    assert defers.active(cab.store, record.id) is None
    assert defers.woken_map(cab.store)[record.id] == "2026-10-14T12:00:20Z"


def test_a_time_passed_while_the_cabinet_was_down_fires_on_the_first_sweep(cab):
    record = card(cab)
    deferred(cab, record)
    at(cab, "2026-10-16T08:00:00Z")  # two days later, no sweep in between
    assert sweep(cab)["deferred"] == 1
    assert len(due(cab, record)) == 1


def test_the_agents_next_outcome_ends_its_defer(cab):
    record = card(cab)
    deferred(cab, record)
    command, session = begin(
        cab, record, kind="message"
    )  # woken by something else meanwhile
    finish(cab, record, outcome="continue")
    end(cab, command, session)
    assert defers.active(cab.store, record.id) is None
    at(cab, "2026-10-14T12:01:00Z")
    sweep(cab)
    assert [c["key"] for c in due(cab, record)] == [
        f"cont:{command}"
    ]  # no second wake from the old time


# --- AC3: the owner and BOSS ------------------------------------------------------------------------------------
def test_the_owner_defers_any_open_card_up_to_90_days(cab):
    record = card(cab, assignee="@owner", status="Inbox")
    found = owner(
        cab,
        "user.card.defer",
        card=record.id,
        until="2027-01-11T10:00:00+03:00",
        reason="после отпуска",
    )
    assert (
        found["defer"]["until"] == "2027-01-11T07:00:00Z"
        and found["defer"]["by"] == "user:owner"
    )
    with pytest.raises(api.ApiError) as exc:
        owner(cab, "user.card.defer", card=record.id, until="2027-01-13T10:00:00Z")
    assert "more than 90 days" in str(exc.value)
    done = card(cab, status="Done")
    with pytest.raises(api.ApiError) as exc:
        owner(cab, "user.card.defer", card=done.id, until=TWO_HOURS_MSK)
    assert "closed card" in str(exc.value)


def test_at_its_time_the_owner_hears_of_a_card_no_agent_holds_in_work(cab):
    mine = card(cab, assignee="@owner", status="In Progress")
    inbox = card(cab, assignee="@dev", status="Inbox")
    for record in (mine, inbox):
        owner(cab, "user.card.defer", card=record.id, until=TWO_HOURS_MSK)
    at(cab, "2026-10-14T12:00:30Z")
    sweep(cab)
    for record in (mine, inbox):
        notices = [m for m in cab.store.messages(record.id) if m["kind"] == "notice"]
        assert [m["payload"]["key"] for m in notices] == ["notice.defer.due"]
        assert due(cab, record) == []


def test_at_its_time_the_owners_defer_wakes_the_agent_once(cab):
    record = card(cab)
    owner(cab, "user.card.defer", card=record.id, until=TWO_HOURS_MSK)
    at(cab, "2026-10-14T12:00:30Z")
    sweep(cab)
    sweep(cab)
    [wake] = due(cab, record)
    assert wake["text"] == continuations.TIME.format(id=record.id)


@pytest.mark.parametrize("status", ["Done", "Cancelled"])
def test_closing_the_card_clears_its_defer(cab, status):
    record = card(cab)
    owner(cab, "user.card.defer", card=record.id, until=TWO_HOURS_MSK)
    changes = {
        "status": status,
        **({"outcome": "cancelled"} if status == "Cancelled" else {}),
    }
    cab.store.edit_card(record.id, None, changes, actor="user:owner")
    assert defers.active(cab.store, record.id) is None


def test_boss_defers_with_a_reason_up_to_7_days_and_the_owner_is_told(cab):
    record = card(cab)
    with pytest.raises(api.ApiError) as exc:
        boss(cab, task=record.id, until=TWO_HOURS_MSK)
    assert "--reason is required" in str(exc.value)
    with pytest.raises(api.ApiError) as exc:
        boss(cab, task=record.id, until="2026-10-21T10:01:00Z", reason="ждём релиза")
    assert "more than 7 days" in str(exc.value)
    found = boss(cab, task=record.id, until=TWO_HOURS_MSK, reason="ждём релиза")
    assert (
        found["defer"]["by"] == "agent:boss"
        and found["defer"]["reason"] == "ждём релиза"
    )
    [notice] = [m for m in cab.store.messages(record.id) if m["kind"] == "notice"]
    assert notice["payload"]["key"] == "notice.defer.boss"
    assert notice["payload"]["params"] == {
        "until": "14.10 12:00",
        "reason": "ждём релиза",
    }  # the host's zone: UTC


def test_only_the_coordinator_defers_through_boss_defer(cab):
    record = card(cab)
    with pytest.raises(api.ApiError) as exc:
        cab.call(
            "digi",
            {
                "op": "boss.defer",
                "agent": "dev",
                "task": record.id,
                "until": TWO_HOURS_MSK,
                "reason": "x",
            },
        )
    assert exc.value.kind == "refused"


# --- AC4: the wakes an owner's defer holds ----------------------------------------------------------------------
def _waiting_for_an_answer(cab, record):
    command, session = begin(cab, record)
    asked = question(cab, record)
    finish(cab, record, outcome="wait_user", note="waiting for the owner")
    end(cab, command, session)
    return asked


def _answer(cab, decision_id):
    cab.call(
        "digi-cabinet",
        {"op": "decision.reply", "id": decision_id, "user": "owner", "text": "Да"},
    )


def test_an_answer_under_the_owners_defer_waits_for_its_time(cab):
    record = card(cab)
    asked = _waiting_for_an_answer(cab, record)
    owner(cab, "user.card.defer", card=record.id, until=TWO_HOURS_MSK)
    _answer(cab, asked)
    assert due(cab, record) == [] and len(held(cab, record)) == 1
    at(cab, "2026-10-14T12:00:30Z")
    sweep(cab)
    assert (
        len(due(cab, record)) == 1
    )  # the held wake, the time's continuation merged into it


def test_a_dependency_closed_under_bosss_defer_waits_too(cab):
    dep = card(cab, assignee="@analyst")
    record = card(cab)
    cab.store.edit_card(record.id, None, {"dependencies": [dep.id]}, actor="user:owner")
    command, session = begin(cab, record)
    finish(cab, record, outcome="wait_dependency", note="waiting for the dependency")
    end(cab, command, session)
    boss(cab, task=record.id, until=TWO_HOURS_MSK, reason="ждём окна")
    cab.store.edit_card(dep.id, None, {"status": "Done"}, actor="user:owner")
    assert due(cab, record) == [] and len(held(cab, record)) == 1


def test_wake_now_delivers_the_held_wake_at_once_and_once(cab):
    record = card(cab)
    asked = _waiting_for_an_answer(cab, record)
    owner(cab, "user.card.defer", card=record.id, until=TWO_HOURS_MSK)
    _answer(cab, asked)
    owner(cab, "user.card.undefer", card=record.id)
    assert len(due(cab, record)) == 1 and defers.active(cab.store, record.id) is None
    at(cab, "2026-10-14T12:00:30Z")
    sweep(cab)
    assert len(due(cab, record)) == 1
    with pytest.raises(api.ApiError) as exc:
        owner(cab, "user.card.undefer", card=record.id)
    assert "not put off" in str(exc.value)


def test_wake_now_on_the_agents_own_defer_wakes_it(cab):
    record = card(cab)
    deferred(cab, record)
    owner(cab, "user.card.undefer", card=record.id)
    [wake] = due(cab, record)
    assert wake["text"] == continuations.TIME.format(id=record.id)


@pytest.mark.parametrize("source", ["owner", "agent"])
def test_a_persons_message_wakes_the_agent_now_and_clears_any_defer(cab, source):
    record = card(cab)
    if source == "agent":
        deferred(cab, record)
    else:
        owner(cab, "user.card.defer", card=record.id, until=TWO_HOURS_MSK)
    owner(cab, "user.card.comment", card=record.id, text="@dev посмотри сейчас")
    [wake] = due(cab, record)
    assert wake["kind"] == "message" and defers.active(cab.store, record.id) is None
    at(cab, "2026-10-14T12:00:30Z")
    sweep(cab)
    assert len(due(cab, record)) == 1  # the old time wakes nobody again


# --- AC5: the inbox and the board -------------------------------------------------------------------------------
def test_the_inbox_and_the_card_list_leave_a_deferred_card_out_until_its_time(cab):
    principal = api.Principal("user", -1, "owner")
    record = card(cab)
    question(cab, record)
    review = card(cab, status="Review")
    before = api.inbox_count(cab.ctx, principal)
    for one in (record, review):
        owner(cab, "user.card.defer", card=one.id, until=TWO_HOURS_MSK)
    assert api.inbox_count(cab.ctx, principal) == before - 2
    listed = {c["id"]: c for c in owner(cab, "card.list")["cards"]}
    assert (
        listed[review.id]["defer"]["until"] == "2026-10-14T12:00:00Z"
        and listed[review.id]["status"] == "Review"
    )
    page = owner(cab, "card.get", card=record.id)
    assert page["pipeline"]["defer"]["until"] == "2026-10-14T12:00:00Z"
    at(cab, "2026-10-14T12:00:30Z")
    sweep(cab)
    assert api.inbox_count(cab.ctx, principal) == before
    assert {c["id"]: c for c in owner(cab, "card.list")["cards"]}[review.id][
        "woken"
    ] == "2026-10-14T12:00:30Z"


# --- AC8: going round in defers ---------------------------------------------------------------------------------
def _limits(cab, record):
    return [
        r
        for r in cab.store._conn.execute(
            "SELECT kind, detail FROM problems WHERE card=? AND kind='continuation_limit'",
            (record.id,),
        ).fetchall()
    ]


def test_the_third_defer_in_a_row_without_progress_asks_boss(cab):
    record = card(cab)
    deferred(cab, record)
    deferred(cab, record)
    assert _limits(cab, record) == []
    deferred(cab, record)
    [problem] = _limits(cab, record)
    assert '"streak": 3' in problem["detail"]


def test_progress_between_defers_starts_the_count_again(cab):
    record = card(cab)
    deferred(cab, record)
    deferred(cab, record)
    cab.call("digi", {"op": "turn.progress", "agent": "dev", "card": record.id})
    deferred(cab, record)
    deferred(cab, record)
    assert _limits(cab, record) == []
    assert defers.active(cab.store, record.id)["streak"] == 2


def test_progress_is_the_assignees_only(cab):
    record = card(cab, assignee="@analyst")
    with pytest.raises(api.ApiError):
        cab.call("digi", {"op": "turn.progress", "agent": "dev", "card": record.id})


# --- AC6: the attention rules -----------------------------------------------------------------------------------
CONFIG = {
    "schemaVersion": 1,
    "utcOffsetMinutes": 180,
    "clockSkewSeconds": 300,
    "trustedWorkers": ["@dev"],
    "rules": {
        "blocked": {"enabled": True},
        "dependency_done": {"enabled": True},
        "in_progress_idle": {
            "enabled": True,
            "afterHours": 24,
            "escalateAfterHours": 24,
        },
        "review_waiting": {"enabled": True, "afterHours": 24},
        "inbox_waiting": {"enabled": True, "afterHours": 24},
        "due_tomorrow": {"enabled": True},
        "due_today": {"enabled": True},
        "overdue": {"enabled": True},
        "route_mismatch": {"enabled": False, "statuses": ["To Do"]},
        "coordinator_assigned": {"enabled": True},
        "spec_mismatch": {"enabled": True},
        "brief_ready": {"enabled": True},
        "inbox_unassigned": {"enabled": True},
    },
}
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


def _raw(status, *, deferred="", woken="", due="", created="2026-11-01T09:00:00Z"):
    return watch._raw_card(
        {
            "id": "KD-1",
            "revision": 3,
            "status": status,
            "assignee": "@dev",
            "project": "web",
            "dependencies": [],
            "created": created,
            "due": due,
            "labels": [],
            "defer": {"until": deferred} if deferred else None,
            "woken": woken,
        }
    )


def _rules(raw, records=()):
    report = attention_rules.evaluate(
        attention_rules.load_config(CONFIG),
        {"KD-1": raw},
        {"web": "@dev"},
        list(records),
        NOW,
    )
    return sorted(e.rule for e in report.events)


def _record(hours_ago):
    at_ = (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "schemaVersion": 1,
        "cardId": "KD-1",
        "worker": "@dev",
        "action": "progress",
        "at": at_,
        "cardRevision": 3,
    }


LATER = "2026-11-09T12:00:00Z"


@pytest.mark.parametrize(
    "status, rule, records",
    [
        ("In Progress", "in_progress_idle", [_record(30)]),
        ("Review", "review_waiting", [{**_record(30), "action": "review_submission"}]),
        ("Inbox", "inbox_waiting", []),
        ("Blocked", "blocked", []),
    ],
)
def test_a_deferred_card_keeps_the_idle_rules_silent(status, rule, records):
    assert rule in _rules(_raw(status), records)
    assert rule not in _rules(_raw(status, deferred=LATER), records)


def test_a_deferred_card_still_hears_its_due_and_overdue():
    assert "due_today" in _rules(
        _raw("In Progress", deferred=LATER, due="2026-11-08"), [_record(1)]
    )
    assert "overdue" in _rules(
        _raw("In Progress", deferred=LATER, due="2026-11-01"), [_record(1)]
    )


def test_in_progress_idle_counts_from_the_wake():
    assert "in_progress_idle" not in _rules(
        _raw("In Progress", woken="2026-11-08T10:00:00Z"), [_record(30)]
    )
    assert "in_progress_idle" in _rules(
        _raw("In Progress", woken="2026-11-07T11:00:00Z"), [_record(30)]
    )


def test_a_card_without_the_new_fields_is_still_read():
    raw = _raw("In Progress")
    del raw["deferred"], raw["woken"]
    assert "in_progress_idle" in _rules(raw, [_record(30)])
    assert "in_progress_idle" not in _rules(
        {**raw, "deferred": "soon"}, [_record(30)]
    )  # an unreadable card


# --- the page's moment ------------------------------------------------------------------------------------------
def test_the_pages_local_moment_takes_the_persons_zone(cab):
    from kd.cabinet.web import cards as web_cards

    assert (
        web_cards.local_until("2026-10-14T15:00", "Etc/GMT-3")
        == "2026-10-14T15:00:00+03:00"
    )
    assert (
        web_cards.local_until("2026-10-14T15:00", "Nowhere/Zone") == "2026-10-14T15:00"
    )
    record = card(cab)
    until = web_cards.local_until("2026-10-14T15:00", "Etc/GMT-3")
    found = owner(cab, "user.card.defer", card=record.id, until=until)
    assert found["defer"]["until"] == "2026-10-14T12:00:00Z"


# --- a new defer in place of one that holds wakes (review) ------------------------------------------------------
@pytest.mark.parametrize("later", [False, True])
def test_a_new_owners_time_moves_the_held_wake_with_it(cab, later):
    record = card(cab)
    asked = _waiting_for_an_answer(cab, record)
    owner(cab, "user.card.defer", card=record.id, until=TWO_HOURS_MSK)
    _answer(cab, asked)
    new = "2026-10-14T14:00:00Z" if later else "2026-10-14T10:40:00Z"
    owner(cab, "user.card.defer", card=record.id, until=new)
    [wake] = held(cab, record)
    assert wake["next_try"] == new
    at(cab, new.replace(":00Z", ":30Z"))
    sweep(cab)
    assert len(due(cab, record)) == 1


def test_wake_now_after_a_shorter_time_still_delivers_the_held_wake(cab):
    record = card(cab)
    asked = _waiting_for_an_answer(cab, record)
    owner(cab, "user.card.defer", card=record.id, until=TWO_HOURS_MSK)
    _answer(cab, asked)
    owner(cab, "user.card.defer", card=record.id, until="2026-10-14T10:40:00Z")
    owner(cab, "user.card.undefer", card=record.id)
    assert len(due(cab, record)) == 1 and held(cab, record) == []


def test_the_agents_defer_does_not_replace_the_owners_or_release_what_his_held(cab):
    """The owner's (or BOSS's) defer is stronger than the agent's own (KD-9479)."""
    record = card(cab)
    asked = _waiting_for_an_answer(cab, record)
    owner(cab, "user.card.defer", card=record.id, until=TWO_HOURS_MSK)
    _answer(cab, asked)
    kept = defers.place(
        cab.ctx,
        cab.store.card(record.id),
        until="2026-10-14T11:00:00Z",
        source="agent",
        by="agent:dev",
    )
    assert (kept["source"], kept["until"]) == ("owner", "2026-10-14T12:00:00Z")
    assert defers.active(cab.store, record.id)["id"] == kept["id"]
    assert due(cab, record) == [] and len(held(cab, record)) == 1


@pytest.mark.parametrize("by", ["owner", "boss"])
def test_an_agents_wait_until_under_a_stronger_defer_keeps_it_and_wakes_once_at_its_time(cab, by):
    """The owner put the card off; a turn of its agent (woken by something else) ends with wait_until ten minutes
    ahead: his defer stands, the held answer stays held, and the agent wakes once, at his time (KD-9479)."""
    record = card(cab)
    asked = _waiting_for_an_answer(cab, record)
    if by == "owner":
        owner(cab, "user.card.defer", card=record.id, until=TWO_HOURS_MSK)
    else:
        boss(cab, task=record.id, until=TWO_HOURS_MSK, reason="ждём окна")
    _answer(cab, asked)
    command = deferred(cab, record, until="2026-10-14T10:30:00Z")
    row = defers.active(cab.store, record.id)
    assert (row["source"], row["until"]) == (by, "2026-10-14T12:00:00Z")
    state = cab.store._conn.execute("SELECT state FROM turn_outcomes WHERE command=?", (command,)).fetchone()[0]
    assert state == "final" and due(cab, record) == [] and len(held(cab, record)) == 1
    at(cab, "2026-10-14T10:31:00Z")  # the agent's own moment: its wake waits for the stronger defer's time
    sweep(cab)
    assert due(cab, record) == []
    at(cab, "2026-10-14T12:00:30Z")
    sweep(cab)
    sweep(cab)
    assert len(due(cab, record)) == 1 and defers.active(cab.store, record.id) is None


def test_the_owners_refusal_names_the_moment_not_the_flag(cab):
    record = card(cab)
    with pytest.raises(api.ApiError) as exc:
        owner(cab, "user.card.defer", card=record.id, until="2026-10-14T15:00")
    assert "--until" not in str(exc.value) and "the moment" in str(exc.value)
