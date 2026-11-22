import itertools
import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from kd import attention as at
from kd.attention_rules import Diagnostic, Event, Report

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
REV = "a" * 64
REV2 = "b" * 64


@pytest.fixture(autouse=True)
def portable_lock(monkeypatch):
    """flock on Linux; elsewhere a process-local lock so the automaton logic still runs."""
    if at.fcntl is None:
        guard = threading.Lock()

        @contextmanager
        def locked(path):
            with guard:
                yield

        monkeypatch.setattr(at, "_locked", locked)


@pytest.fixture
def state(tmp_path):
    return str(tmp_path / "attention.json")


def ids():
    counter = itertools.count(1)
    return lambda: f"ab{next(counter):030x}"


def ACKED(event_id, action="owner", minutes=30, reason="asked the owner"):
    return {"event": event_id, "action": action, "next_check": minutes, "reason": reason}


def ev(rule="blocked", subject="KD-1", revision=REV, notify="boss", level="attention"):
    return Event(rule, subject, revision, notify, level)


def rep(*events, diags=()):
    return Report(tuple(events), tuple(Diagnostic(*d) for d in diags))


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)["events"]


def by_key(path):
    return {
        (e["rule"], e["subject"], e["revision"]): dict(e, id=i)
        for i, e in load(path).items()
    }


# --- ticks ----------------------------------------------------------------------------------


def test_first_tick_creates_pending_events_with_opaque_ids(state):
    changed = at.observe(state, rep(ev(), ev("route_mismatch", "KD-2")), NOW)
    events = load(state)
    assert sorted(changed) == sorted(events) and len(events) == 2
    assert all(at._ID.fullmatch(i) for i in events)
    assert {e["state"] for e in events.values()} == {"pending"}
    assert oct(os.stat(state).st_mode & 0o777) == (
        "0o640" if os.name != "nt" else oct(os.stat(state).st_mode & 0o777)
    )


def test_repeated_tick_is_idempotent_and_keeps_age(state):
    report = rep(ev(), ev("inbox_waiting", "KD-3", notify="owner"))
    at.observe(state, report, NOW)
    before = open(state, "rb").read()
    assert at.observe(state, report, NOW + timedelta(minutes=15)) == []
    assert (
        open(state, "rb").read() == before
    )  # nothing rewritten, created/updated unchanged


def test_disappeared_condition_resolves_and_return_is_a_new_event(state):
    at.observe(state, rep(ev()), NOW, ids())
    at.observe(state, rep(), NOW + timedelta(minutes=15))
    (only,) = load(state).values()
    assert only["state"] == "resolved"
    at.observe(state, rep(ev()), NOW + timedelta(minutes=30), lambda: "f" * 32)
    assert load(state)["f" * 32]["state"] == "pending"


def test_new_card_revision_is_a_new_event(state):
    at.observe(state, rep(ev("route_mismatch")), NOW, ids())
    at.observe(
        state, rep(ev("route_mismatch", revision=REV2)), NOW + timedelta(minutes=15), lambda: "f" * 32
    )
    events = by_key(state)
    assert events[("route_mismatch", "KD-1", REV)]["state"] == "resolved"
    assert events[("route_mismatch", "KD-1", REV2)]["state"] == "pending"


def test_escalation_is_a_transition_reported_once(state):
    at.observe(state, rep(ev("in_progress_idle", notify="assignee")), NOW, ids())
    (event_id,) = load(state)
    at.mark_dispatched(state, [event_id], NOW)
    at.ack(state, event_id, NOW)
    up = ev("in_progress_idle", notify="owner", level="escalate")
    assert at.observe(state, rep(up), NOW + timedelta(hours=12)) == [event_id]
    event = load(state)[event_id]
    assert (
        event["state"],
        event["level"],
        event["notify"],
        event["ownerNotified"],
    ) == ("escalated", "escalate", "owner", False)
    assert at.observe(state, rep(up), NOW + timedelta(hours=13)) == []
    assert at.mark_owner_notified(state, [event_id], NOW) == [event_id]
    assert at.mark_owner_notified(state, [event_id], NOW) == []
    assert load(state)[event_id]["created"] == "2026-10-07T12:00:00Z"


def test_event_born_escalated(state):
    at.observe(
        state, rep(ev("in_progress_idle", notify="owner", level="escalate")), NOW
    )
    assert [e["state"] for e in load(state).values()] == ["escalated"]


# --- unknown is not resolved ----------------------------------------------------------------


def test_time_jump_keeps_timed_events_but_resolves_board_ones(state):
    at.observe(
        state,
        rep(
            ev(),
            ev("overdue", "KD-2", notify="owner"),
            ev("due_today", "KD-3", notify="assignee"),
        ),
        NOW,
    )
    at.observe(state, rep(diags=[("time_jump", "clock")]), NOW + timedelta(minutes=15))
    states = {k[0]: e["state"] for k, e in by_key(state).items()}
    assert states == {
        "blocked": "resolved",
        "overdue": "pending",
        "due_today": "pending",
    }


def test_untrusted_journal_keeps_activity_events(state):
    at.observe(
        state,
        rep(
            ev("in_progress_idle", notify="assignee"),
            ev("review_waiting", "KD-2", notify="owner"),
        ),
        NOW,
    )
    at.observe(
        state, rep(diags=[("activity_invalid", "journal")]), NOW + timedelta(minutes=15)
    )
    assert {e["state"] for e in load(state).values()} == {"pending"}


def test_card_diagnostics_keep_only_their_subject(state):
    at.observe(state, rep(ev(), ev("blocked", "KD-2")), NOW)
    at.observe(state, rep(diags=[("invalid_card", "KD-1")]), NOW + timedelta(minutes=15))
    states = {k[:2]: e["state"] for k, e in by_key(state).items()}
    assert states == {
        ("blocked", "KD-1"): "pending",
        ("blocked", "KD-2"): "resolved",
    }


def test_rule_scoped_diagnostic_keeps_only_that_rule_of_the_card(state):
    card_events = [
        ev(),
        ev("due_today", notify="assignee"),
        ev("route_mismatch"),
        ev("in_progress_idle", notify="assignee"),
        ev("dependency_done", notify="assignee"),
    ]
    at.observe(state, rep(*card_events), NOW)
    at.observe(
        state,
        rep(
            diags=[
                ("activity_unknown", "KD-1", "in_progress_idle"),
                ("dependency_unknown", "KD-1", "dependency_done"),
            ]
        ),
        NOW + timedelta(minutes=15),
    )
    assert {k[0]: e["state"] for k, e in by_key(state).items()} == {
        "blocked": "resolved",
        "due_today": "resolved",
        "route_mismatch": "resolved",
        "in_progress_idle": "pending",
        "dependency_done": "pending",
    }


def test_a_ruleless_future_date_keeps_every_rule_of_its_card(state):
    at.observe(state, rep(ev("overdue", "KD-3", notify="owner"), ev("due_today", "KD-3", notify="assignee")), NOW)
    at.observe(
        state, rep(diags=[("future_date", "KD-3", "")]), NOW + timedelta(minutes=15)
    )
    assert {e["state"] for e in load(state).values()} == {"pending"}


# --- delivery -------------------------------------------------------------------------------


def test_failed_nudge_leaves_pending_and_retry_dispatches(state):
    at.observe(state, rep(ev()), NOW)
    (event_id,) = load(state)
    assert at.mark_failed(state, [event_id], NOW) == [event_id]
    assert (
        load(state)[event_id]["state"] == "pending"
        and load(state)[event_id]["attempts"] == 1
    )
    assert [e["id"] for e in at.listing(state) if e["state"] == "pending"] == [event_id]
    assert at.mark_dispatched(state, [event_id], NOW + timedelta(minutes=15)) == [
        event_id
    ]
    assert load(state)[event_id]["state"] == "dispatched"
    assert at.mark_dispatched(state, [event_id], NOW) == []  # not pending any more


def test_dispatch_after_condition_disappeared_does_not_reopen(state):
    at.observe(state, rep(ev()), NOW)
    (event_id,) = load(state)
    at.observe(state, rep(), NOW)  # resolved while the nudge was in flight
    assert at.mark_dispatched(state, [event_id], NOW) == []
    assert at.mark_failed(state, [event_id], NOW) == []
    assert load(state)[event_id]["state"] == "resolved"


def test_marking_unknown_ids_is_refused_without_changes(state):
    at.observe(state, rep(ev()), NOW)
    before = open(state, "rb").read()
    with pytest.raises(at.AttentionError):
        at.mark_dispatched(state, [next(iter(load(state))), "0" * 32], NOW)
    assert open(state, "rb").read() == before


# --- ack ------------------------------------------------------------------------------------


def test_ack_needs_the_exact_opaque_id_and_touches_only_that_event(state):
    at.observe(state, rep(ev(), ev("blocked", "KD-2")), NOW, ids())
    first, second = sorted(load(state))
    for bad in [
        first[:8],
        first.upper(),
        " " + first,
        first + "\n",
        "KD-1",
        "",
        None,
        1,
    ]:
        with pytest.raises(at.AttentionError):
            at.ack(state, bad, NOW)
    assert at.ack(state, first, NOW) == "acknowledged"
    assert at.ack(state, first, NOW) == "acknowledged"
    assert load(state)[second]["state"] == "pending"


def test_ack_of_resolved_event_is_refused(state):
    at.observe(state, rep(ev()), NOW)
    (event_id,) = load(state)
    at.observe(state, rep(), NOW)
    with pytest.raises(at.AttentionError, match="resolved"):
        at.ack(state, event_id, NOW)


def test_acknowledged_event_stays_quiet_on_later_ticks(state):
    at.observe(state, rep(ev()), NOW)
    (event_id,) = load(state)
    at.ack(state, event_id, NOW)
    assert at.observe(state, rep(ev()), NOW + timedelta(hours=1)) == []
    assert load(state)[event_id]["state"] == "acknowledged"


def test_listing_shows_open_events_without_resolved(state):
    at.observe(state, rep(ev(), ev("blocked", "KD-2")), NOW, ids())
    at.observe(state, rep(ev()), NOW + timedelta(minutes=1))
    (item,) = at.listing(state)
    assert item["subject"] == "KD-1" and set(item) == {
        "id",
        "rule",
        "subject",
        "revision",
        "notify",
        "level",
        "state",
        "created",
        "updated",
        "attempts",
        "nextCheck",
        "action",
        "reason",
    }


# --- damaged state --------------------------------------------------------------------------


def _valid_event(**over):
    event = {
        "rule": "blocked",
        "subject": "KD-1",
        "revision": REV,
        "notify": "boss",
        "level": "attention",
        "state": "pending",
        "created": "2026-10-07T12:00:00Z",
        "updated": "2026-10-07T12:00:00Z",
        "attempts": 0,
        "ownerNotified": False,
    }
    event.update(over)
    return event


@pytest.mark.parametrize(
    "content",
    [
        "",
        "{",
        "[]",
        json.dumps({"schemaVersion": 3, "events": {}}),
        json.dumps({"schemaVersion": True, "events": {}}),
        json.dumps({"schemaVersion": 1, "events": {}, "extra": 1}),
        json.dumps({"schemaVersion": 1, "events": {"KD-1": _valid_event()}}),
        json.dumps(
            {"schemaVersion": 1, "events": {"0" * 32: _valid_event(state="done")}}
        ),
        json.dumps(
            {"schemaVersion": 1, "events": {"0" * 32: _valid_event(rule="free_text")}}
        ),
        json.dumps(
            {
                "schemaVersion": 1,
                "events": {"0" * 32: _valid_event(created="yesterday")},
            }
        ),
        json.dumps(
            {"schemaVersion": 1, "events": {"0" * 32: _valid_event(attempts=-1)}}
        ),
        json.dumps(
            {"schemaVersion": 1, "events": {"0" * 32: {**_valid_event(), "extra": 1}}}
        ),
        json.dumps(
            {
                "schemaVersion": 1,
                "events": {"0" * 32: _valid_event(), "1" * 32: _valid_event()},
            }
        ),
    ],
)
def test_damaged_state_is_an_error_never_empty(state, content):
    with open(state, "w", encoding="utf-8") as f:
        f.write(content)
    for call in (
        lambda: at.observe(state, rep(ev()), NOW),
        lambda: at.ack(state, "0" * 32, NOW),
        lambda: at.listing(state),
        lambda: at.mark_dispatched(state, [], NOW),
    ):
        with pytest.raises(at.CorruptState):
            call()
    assert open(state, encoding="utf-8").read() == content


def test_recover_moves_damaged_file_aside_and_rebuilds_pending(state):
    with open(state, "w", encoding="utf-8") as f:
        f.write("{")
    aside = at.recover(state, rep(ev(), ev("blocked", "KD-2")), NOW)
    assert open(aside, encoding="utf-8").read() == "{"
    assert {e["state"] for e in load(state).values()} == {"pending"} and len(
        load(state)
    ) == 2


def test_resolved_resolved_twice_is_valid_but_two_open_duplicates_are_not(state):
    resolved = {
        "0" * 32: _valid_event(state="resolved"),
        "1" * 32: _valid_event(state="resolved"),
        "2" * 32: _valid_event(),
    }
    with open(state, "w", encoding="utf-8") as f:
        json.dump({"schemaVersion": 1, "events": resolved}, f)
    assert [e["id"] for e in at.listing(state)] == ["2" * 32]


# --- crash and concurrency ------------------------------------------------------------------


@pytest.mark.parametrize("fail", ["replace", "fsync"])
def test_crash_during_write_keeps_the_previous_state(
    state, monkeypatch, fail, tmp_path
):
    at.observe(state, rep(ev()), NOW)
    before = open(state, "rb").read()
    real = getattr(at.os, fail)

    def boom(*args, **kwargs):
        raise OSError("power loss")

    monkeypatch.setattr(at.os, fail, boom)
    with pytest.raises(OSError):
        at.observe(state, rep(ev("blocked", "KD-2")), NOW)
    monkeypatch.setattr(at.os, fail, real)
    assert open(state, "rb").read() == before
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []
    at.observe(state, rep(ev("blocked", "KD-2")), NOW)  # the next tick works
    assert len(load(state)) == 2


def test_resolved_events_are_pruned_after_retention(state):
    at.observe(state, rep(ev()), NOW)
    at.observe(state, rep(), NOW)
    at.observe(state, rep(), NOW + at.RETAIN - timedelta(seconds=1))
    assert len(load(state)) == 1
    at.observe(state, rep(), NOW + at.RETAIN + timedelta(seconds=1))
    assert load(state) == {}


def test_a_full_journal_drops_the_oldest_resolved_then_pending_events_and_keeps_ticking(state, monkeypatch):
    """Global review 15.10.2026: above the cap the automaton degrades instead of raising on every tick."""
    monkeypatch.setattr(at, "MAX_EVENTS", 4)
    at.observe(state, rep(ev("blocked", "KD-1"), ev("blocked", "KD-2"), ev("blocked", "KD-3")), NOW)
    at.observe(state, rep(ev("blocked", "KD-2"), ev("blocked", "KD-3")), NOW + timedelta(minutes=1))  # KD-1 resolved
    at.observe(
        state,
        rep(ev("blocked", "KD-2"), ev("blocked", "KD-3"), ev("blocked", "KD-4"), ev("blocked", "KD-5")),
        NOW + timedelta(minutes=2),
    )
    events = load(state)
    assert len(events) == 4 and all(e["state"] != "resolved" for e in events.values())  # the resolved one went
    at.observe(
        state,
        rep(*[ev("blocked", f"KD-{n}") for n in (2, 3, 4, 5, 6, 7)]),
        NOW + timedelta(minutes=3),
    )
    events = load(state)
    assert len(events) == 4 and {e["subject"] for e in events.values()} == {"KD-4", "KD-5", "KD-6", "KD-7"}


def test_missing_state_is_the_first_run(state):
    assert at.listing(state) == []
    assert not os.path.exists(state)


def test_invalid_detector_event_writes_nothing(state):
    at.observe(state, rep(ev()), NOW)
    before = open(state, "rb").read()
    with pytest.raises(at.AttentionError):
        at.observe(state, rep(ev(notify="everyone")), NOW)
    assert open(state, "rb").read() == before


def test_concurrent_ack_and_watch_ticks_lose_nothing(state):
    at.observe(state, rep(*[ev("blocked", f"KD-{n}") for n in range(1, 21)]), NOW)
    targets = sorted(load(state))
    errors = []

    def acker(event_id):
        try:
            at.ack(state, event_id, NOW)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    def watcher():
        try:
            for minute in range(10):
                at.observe(
                    state,
                    rep(
                        *[ev("blocked", f"KD-{n}") for n in range(1, 21)],
                        ev("blocked", "KD-99"),
                    ),
                    NOW + timedelta(minutes=minute),
                )
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=acker, args=(i,)) for i in targets] + [
        threading.Thread(target=watcher) for _ in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    events = load(state)
    assert all(events[i]["state"] == "acknowledged" for i in targets)
    assert [e["subject"] for e in events.values() if e["state"] == "pending"] == [
        "KD-99"
    ]


@pytest.mark.skipif(at.fcntl is None, reason="flock: Linux")
def test_lock_file_symlink_is_refused(state, tmp_path):
    (tmp_path / "elsewhere").write_text("x")
    os.symlink(tmp_path / "elsewhere", state + ".lock")
    with pytest.raises(OSError):
        at.observe(state, rep(ev()), NOW)


# --- watch helpers (B05) --------------------------------------------------------------------


def test_apply_acks_skips_unknown_and_resolved_ids(state):
    at.observe(
        state,
        rep(
            ev(),
            ev("blocked", "KD-2"),
            ev("in_progress_idle", "KD-3", notify="owner", level="escalate"),
        ),
        NOW,
        ids(),
    )
    first, second, third = sorted(load(state))
    at.observe(
        state,
        rep(ev(), ev("in_progress_idle", "KD-3", notify="owner", level="escalate")),
        NOW,
    )
    done = at.apply_acks(state, [ACKED(first), ACKED(second), ACKED(third), ACKED("f" * 32), None, first], NOW)
    assert done == [first, third]
    events = load(state)
    assert [events[i]["state"] for i in (first, second, third)] == [
        "acknowledged",
        "resolved",
        "acknowledged",
    ]
    assert at.apply_acks(state, [ACKED(first)], NOW) == []


def test_an_ack_of_an_escalated_event_acknowledges_it(state):
    """BOSS acks an event the owner was told of — it leaves the open list until its next check."""
    up = ev("overdue", "KD-2", notify="owner", level="escalate")
    at.observe(state, rep(up), NOW, ids())
    (due,) = at.owner_due(state)
    at.mark_owner_notified(state, [due["id"]], NOW)
    assert at.apply_acks(state, [ACKED(due["id"], action="card:KD-2", minutes=60)], NOW) == [due["id"]]
    event = load(state)[due["id"]]
    assert (event["state"], event["action"], event["nextCheck"]) == ("acknowledged", "card:KD-2", "2026-10-07T13:00:00Z")
    at.observe(state, rep(up), NOW + timedelta(minutes=5))  # the same level: no new escalation
    assert load(state)[due["id"]]["state"] == "acknowledged" and at.owner_due(state) == []


def test_an_escalation_acked_before_its_owner_line_still_reaches_the_owner(state):
    at.observe(state, rep(ev("overdue", "KD-2", notify="owner", level="escalate")), NOW, ids())
    [event_id] = load(state)
    assert at.apply_acks(state, [ACKED(event_id)], NOW) == [event_id]
    assert [e["id"] for e in at.owner_due(state)] == [event_id]
    assert at.mark_owner_notified(state, [event_id], NOW) == [event_id]
    assert at.owner_due(state) == []


def test_owner_due_lists_unnotified_escalations_once(state):
    at.observe(
        state, rep(ev("overdue", "KD-2", notify="owner", level="escalate")), NOW, ids()
    )
    (due,) = at.owner_due(state)
    assert (due["rule"], due["subject"]) == ("overdue", "KD-2")
    at.mark_owner_notified(state, [due["id"]], NOW)
    assert at.owner_due(state) == []


def test_listing_reads_without_the_lock(state, monkeypatch):
    at.observe(state, rep(ev()), NOW)

    def no_lock(path):
        raise PermissionError("the board owner cannot open the lock file")

    monkeypatch.setattr(at, "_locked", no_lock)
    assert [e["rule"] for e in at.listing(state)] == ["blocked"]


# --- C03: age rules keep their event, an ack comes back when its check is due -------------


def test_age_rule_survives_revision_change(state):
    at.observe(state, rep(ev("in_progress_idle", revision="5", notify="assignee")), NOW, ids())
    later = NOW + timedelta(minutes=15)
    assert at.observe(state, rep(ev("in_progress_idle", revision="6", notify="assignee")), later, ids()) == []
    (only,) = load(state).values()
    assert only["state"] == "pending" and only["created"] == at._stamp(NOW) and only["revision"] == "6"


def test_revision_rules_still_refire(state):
    at.observe(state, rep(ev("brief_ready", revision="5")), NOW, ids())
    changed = at.observe(state, rep(ev("brief_ready", revision="6")), NOW + timedelta(minutes=15), lambda: "f" * 32)
    assert changed == ["f" * 32]
    assert sorted(e["state"] for e in load(state).values()) == ["pending", "resolved"]


def test_recheck_returns_unresolved_event_to_pending(state):
    at.observe(state, rep(ev()), NOW, ids())
    (event_id,) = load(state)
    at.mark_dispatched(state, [event_id], NOW)
    assert at.apply_acks(state, [ACKED(event_id, "card:KD-2", 20, "waits for KD-2")], NOW) == [event_id]
    acked = load(state)[event_id]
    assert (acked["state"], acked["action"], acked["reason"]) == ("acknowledged", "card:KD-2", "waits for KD-2")
    assert acked["nextCheck"] == at._stamp(NOW + timedelta(minutes=20))
    assert at.recheck(state, NOW + timedelta(minutes=19)) == []
    assert at.recheck(state, NOW + timedelta(minutes=20)) == [event_id]
    back = load(state)[event_id]
    assert back["state"] == "pending" and back["created"] == at._stamp(NOW)
    assert (back["action"], back["reason"], back["nextCheck"]) == ("", "", "")


def test_v1_state_reads_with_defaults(state):
    v1 = {"rule": "blocked", "subject": "KD-1", "revision": REV, "notify": "boss", "level": "attention",
          "state": "acknowledged", "created": "2026-10-07T11:00:00Z", "updated": "2026-10-07T11:00:00Z",
          "attempts": 0, "ownerNotified": False}
    with open(state, "w", encoding="utf-8") as f:
        json.dump({"schemaVersion": 1, "events": {"a" * 32: v1}}, f)
    (listed,) = at.listing(state)
    assert (listed["action"], listed["reason"], listed["nextCheck"]) == ("", "", "")
    # an acknowledgement from before C03 has no check: it comes back on the first recheck
    assert at.recheck(state, NOW) == ["a" * 32]
    with open(state, encoding="utf-8") as f:
        assert json.load(f)["schemaVersion"] == at.SCHEMA_VERSION == 2


def test_v1_duplicates_of_an_age_rule_merge_on_upgrade(state):
    """Final review M8: v1 kept one open event per revision; v2 keeps the oldest per card and resolves the rest."""
    def v1(revision, created):
        return {"rule": "blocked", "subject": "KD-1", "revision": revision, "notify": "boss", "level": "attention",
                "state": "pending", "created": created, "updated": created, "attempts": 0, "ownerNotified": False}
    with open(state, "w", encoding="utf-8") as f:
        json.dump({"schemaVersion": 1, "events": {"a" * 32: v1("5", "2026-10-07T09:00:00Z"),
                                                   "b" * 32: v1("6", "2026-10-07T10:00:00Z")}}, f)
    (listed,) = at.listing(state)
    assert listed["id"] == "a" * 32 and listed["created"] == "2026-10-07T09:00:00Z"
