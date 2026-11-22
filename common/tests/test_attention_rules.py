import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kd import attention_rules as ar

WATCH = Path(__file__).resolve().parents[1] / "coordination" / "watch.json"
SHIPPED = json.loads(WATCH.read_text(encoding="utf-8"))
ROUTES = {
    "digipaddock": "@dev",
    "admin": "@sysadmin",
    "web": "@owner",
    "alert": "@owner",
}
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)  # 15:00 UTC3
REV = 3  # the cabinet's change counter of a card
REV2 = 4


def config(**changes):
    data = copy.deepcopy(SHIPPED)
    data["trustedWorkers"] = ["@dev", "@sysadmin"]
    for key, value in changes.items():
        data[key] = value
    return ar.load_config(data)


CFG = config()


def card(
    status="To Do",
    assignees=("@dev",),
    project="digipaddock",
    deps=(),
    created="2026-10-07T11:00:00Z",
    due="",
    revision=REV,
    labels=(),
):
    return {
        "revision": revision,
        "status": status,
        "assignees": list(assignees),
        "project": project,
        "dependencies": list(deps),
        "created": created,
        "due": due,
        "labels": list(labels),
    }


SPECS = {"@dev": frozenset({"implementer", "reviewer"}), "@sysadmin": frozenset()}


def test_spec_mismatch_for_a_label_the_assignee_does_not_have():
    cards = {
        "KD-1": card("To Do", labels=["spec:reviewer"]),  # fine
        "KD-2": card("To Do", labels=["spec:task-brief"]),  # another type's
        "KD-3": card("In Progress", labels=["spec:bogus", "server:node-c"]),
        "KD-4": card("Inbox", labels=["spec:reviewer", "spec:docs"]),  # two labels
        "KD-5": card("Done", labels=["spec:bogus"]),  # finished: nobody acts on it
        "KD-6": card("To Do", assignees=["@sysadmin"], labels=["spec:implementer"]),
        "KD-7": card("To Do", assignees=(), labels=["spec:reviewer"]),  # no assignee: route_mismatch's case
        "KD-8": card("To Do", labels=[]),  # no label: the default applies
    }
    report = ar.evaluate(CFG, cards, ROUTES, None, NOW, specs=SPECS)
    assert [e.subject for e in report.events if e.rule == "spec_mismatch"] == ["KD-2", "KD-3", "KD-4", "KD-6"]
    assert all(e.notify == "boss" for e in report.events if e.rule == "spec_mismatch")
    # Without a registry (specs=None) the rule is silent: it never guesses.
    assert not [e for e in ar.evaluate(CFG, cards, ROUTES, None, NOW).events if e.rule == "spec_mismatch"]


def test_brief_ready_only_for_an_inbox_card_of_a_worker():
    # the analyst's label brief-ready on an Inbox card of one worker asks BOSS to propose the start.
    cards = {
        "KD-1": card("Inbox", assignees=["@dev"], labels=["brief-ready", "spec:implementer"]),  # handed over
        "KD-2": card("To Do", labels=["brief-ready"]),  # already started: nothing to propose
        "KD-3": card("Inbox", assignees=["@boss"], labels=["brief-ready"]),  # coordinator_assigned's case
        "KD-4": card("Inbox", assignees=["@owner"], labels=["brief-ready"]),  # the owner's own draft
        "KD-5": card("Inbox", assignees=(), labels=["brief-ready"]),  # nobody to start
        "KD-6": card("Inbox", assignees=["@dev"], labels=[]),  # a plain draft
        "KD-7": card("Inbox", assignees=["@analyst"], labels=["brief-ready"]),  # a brief the analyst kept: BOSS decides
    }
    report = ar.evaluate(CFG, cards, ROUTES, None, NOW)
    events = [e for e in report.events if e.rule == "brief_ready"]
    assert [(e.subject, e.notify) for e in events] == [("KD-1", "boss"), ("KD-7", "boss")]
    assert events[0].revision == str(cards["KD-1"]["revision"])  # the automaton key is text


def test_brief_ready_is_silent_in_council():
    # while the brief's council has not ended, the coordinator is not asked to propose the start.
    reading = card("Inbox", assignees=["@dev"], labels=["brief-ready", "spec:implementer"])
    reading["in_council"] = True
    cards = {"KD-1": reading, "KD-2": card("Inbox", assignees=["@dev"], labels=["brief-ready"])}
    report = ar.evaluate(CFG, cards, ROUTES, None, NOW)
    assert [e.subject for e in report.events if e.rule == "brief_ready"] == ["KD-2"]
    assert not report.diagnostics
    reading["in_council"] = "yes"  # a flag, nothing else
    assert [d.kind for d in ar.evaluate(CFG, cards, ROUTES, None, NOW).diagnostics] == ["invalid_card"]


def test_labels_must_be_a_list_of_strings():
    bad = card("To Do")
    bad["labels"] = "spec:reviewer"
    report = ar.evaluate(CFG, {"KD-1": bad}, ROUTES, None, NOW, specs=SPECS)
    assert [d.kind for d in report.diagnostics] == ["invalid_card"]


def record(
    card_id="KD-1", worker="@dev", action="progress", ago=timedelta(hours=1)
):
    at = (NOW - ago).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "schemaVersion": 1,
        "cardId": card_id,
        "worker": worker,
        "action": action,
        "at": at,
        "cardRevision": 2,  # the card's cabinet revision
    }


def run(cards=None, records=(), cfg=CFG, now=NOW, last_tick=None):
    return ar.evaluate(cfg, cards or {}, ROUTES, records, now, last_tick)


def keys(report, rule=None):
    return [
        (e.rule, e.subject, e.notify, e.level)
        for e in report.events
        if rule in (None, e.rule)
    ]


def diags(report):
    return [(d.kind, d.subject) for d in report.diagnostics]


def scoped(report):
    return [(d.kind, d.subject, d.rule) for d in report.diagnostics]


# --- watch.json -----------------------------------------------------------------------------


def test_shipped_watch_json_covers_every_rule_and_trusts_the_kd_work_producers():
    cfg = ar.load_config(SHIPPED)
    assert set(cfg.rules) == set(ar.RULES)
    # kd-work is the trusted producer, enabled after the stage probe of 11.10.2026
    assert cfg.trusted_workers == frozenset({"@dev", "@sysadmin", "@analyst"})  # the analyst reports with kd-work too
    assert cfg.rules["in_progress_idle"]["afterHours"] == 24
    assert cfg.rules["in_progress_idle"]["escalateAfterHours"] == 12


def test_rules_have_no_panel_rules():
    """agents run in the cabinet's sessions; the session states are the cabinet's (spec §15)."""
    assert not {"panel_waiting", "panel_absent"} & set(ar.RULES)
    assert "maintenance" not in SHIPPED and not any(rule.startswith("panel_") for rule in SHIPPED["rules"])
    for rule in ("panel_waiting", "panel_absent"):
        with pytest.raises(ar.ConfigError):
            ar.load_config(_broken(("rules", rule), {"enabled": True, "afterMinutes": 30}))


def _broken(path, value):
    data = copy.deepcopy(SHIPPED)
    target = data
    for key in path[:-1]:
        target = target[key]
    if value is KeyError:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return data


@pytest.mark.parametrize(
    "path,value",
    [
        (("schemaVersion",), 2),
        (("schemaVersion",), True),
        (("extra",), 1),
        (("rules", "blocked"), KeyError),
        (("rules", "new_rule"), {"enabled": True}),
        (("rules", "blocked", "afterHours"), 1),
        (("rules", "in_progress_idle", "afterHours"), True),
        (("rules", "in_progress_idle", "afterHours"), 0),
        (("rules", "blocked", "enabled"), "yes"),
        (("rules", "route_mismatch", "statuses"), ["Done"]),
        (("rules", "route_mismatch", "statuses"), ["Inbox"]),
        (("rules", "route_mismatch", "statuses"), []),
        (("maintenance",), [{"start": "04:30", "minutes": 20}]),
        (("trustedWorkers",), ["@boss"]),
        (("trustedWorkers",), ["dev"]),
        (("utcOffsetMinutes",), "180"),
        (("parkedMilestones",), []),  # milestones are gone — the key is refused, not read
        (("parkedMilestones",), ["m-9"]),
    ],
)
def test_watch_json_is_validated_strictly(path, value):
    with pytest.raises(ar.ConfigError):
        ar.load_config(_broken(path, value))


def test_shipped_watch_json_parks_no_milestone():
    # milestones are gone, and with them the cards that raised nothing for being an idea
    assert "parkedMilestones" not in SHIPPED and not hasattr(ar.load_config(SHIPPED), "parked")


# --- board rules ----------------------------------------------------------------------------


def test_a_card_without_a_milestone_is_valid_and_one_naming_it_is_not():
    # the field left the card; its set is exact, so a card that still names one is not read
    report = run({"KD-1": card("Blocked"), "KD-2": {**card("Blocked"), "milestone": "m-9"}})
    assert keys(report) == [("blocked", "KD-1", "boss", "attention")]
    assert diags(report) == [("invalid_card", "KD-2")]


def test_blocked_fires_immediately():
    report = run({"KD-1": card("Blocked")})
    assert keys(report, "blocked") == [("blocked", "KD-1", "boss", "attention")]
    assert report.events[0].revision == str(REV)


def test_dependency_done_only_when_every_structured_dependency_is_done():
    cards = {
        "KD-1": card("Blocked", deps=["KD-2", "KD-3"]),
        "KD-2": card("Done"),
        "KD-3": card("Done"),
        "KD-4": card("Blocked", deps=["KD-2", "KD-5"]),
        "KD-5": card("Review"),
        "KD-6": card("Blocked", deps=["KD-99"]),
        "KD-7": card("To Do", deps=["KD-2"]),
        "KD-8": card("Blocked"),
    }
    report = run(cards)
    assert keys(report, "dependency_done") == [
        ("dependency_done", "KD-1", "assignee", "attention")
    ]
    # a missing dependency is not "not Done": the blocked card's rule is unknown, not resolved
    assert [d for d in scoped(report) if d[0] == "dependency_unknown"] == [
        ("dependency_unknown", "KD-6", "dependency_done")
    ]


def test_invalid_dependency_card_makes_dependency_done_unknown():
    cards = {
        "KD-1": card("Blocked", deps=["KD-2", "KD-3"]),
        "KD-2": card("Done"),
        "KD-3": {"status": "Done"},  # fails validation
    }
    report = run(cards)
    assert keys(report, "dependency_done") == []
    assert ("dependency_unknown", "KD-1", "dependency_done") in scoped(report)
    assert ("invalid_card", "KD-3", "") in scoped(report)


@pytest.mark.parametrize(
    "ago,expected",
    [
        (timedelta(hours=24) - timedelta(seconds=1), []),
        (timedelta(hours=24), [("in_progress_idle", "KD-1", "assignee", "attention")]),
        (
            timedelta(hours=36) - timedelta(seconds=1),
            [("in_progress_idle", "KD-1", "assignee", "attention")],
        ),
        (timedelta(hours=36), [("in_progress_idle", "KD-1", "owner", "escalate")]),
    ],
)
def test_in_progress_idle_24h_and_12h_escalation_boundaries(ago, expected):
    report = run({"KD-1": card("In Progress")}, [record(ago=ago)])
    assert keys(report, "in_progress_idle") == expected
    assert diags(report) == []


def test_latest_worker_action_counts_not_the_first():
    records = [
        record(ago=timedelta(hours=40)),
        record(action="claim", ago=timedelta(hours=2)),
    ]
    assert keys(run({"KD-1": card("In Progress")}, records)) == []


def test_without_trusted_journal_the_24h_rule_is_unknown_not_fired():
    report = run({"KD-1": card("In Progress")}, None)
    assert keys(report) == []
    assert diags(report) == [("activity_unknown", "KD-1")]


def test_shipped_config_without_a_record_still_means_unknown():
    # A card nobody reported on through kd-work stays unknown, no false alarm
    report = run(
        {"KD-1": card("In Progress")},
        [],
        cfg=ar.load_config(SHIPPED),
    )
    assert keys(report) == []
    assert diags(report) == [("activity_unknown", "KD-1")]


def test_no_record_for_current_worker_is_unknown_even_if_former_worker_was_active():
    # Reassignment does not inherit the former worker's timer.
    report = run(
        {"KD-1": card("In Progress", assignees=["@sysadmin"])},
        [record(worker="@dev")],
    )
    assert keys(report) == []
    assert diags(report) == [("activity_unknown", "KD-1")]


@pytest.mark.parametrize(
    "assignees", [(), ("@dev", "@sysadmin"), ("@owner",), ("@boss",)]
)
def test_activity_needs_exactly_one_worker(assignees):
    report = run(
        {"KD-1": card("In Progress", assignees=assignees)},
        [record(ago=timedelta(days=3))],
    )
    assert keys(report, "in_progress_idle") == []
    assert diags(report) == [("activity_unknown", "KD-1")]


def test_boss_comment_and_sync_cannot_reset_the_timer():
    # Card files carry no activity input: updated_date is not even accepted as a card field.
    stale = record(ago=timedelta(hours=30))
    cards = {"KD-1": card("In Progress")}
    assert keys(run(cards, [stale]), "in_progress_idle") == [
        ("in_progress_idle", "KD-1", "assignee", "attention")
    ]
    with_updated = {"KD-1": {**card("In Progress"), "updated": "2026-10-07T11:59:00Z"}}
    assert diags(run(with_updated, [stale])) == [("invalid_card", "KD-1")]
    # A BOSS-attributed "activity" record poisons the journal instead of refreshing the card.
    report = run(cards, [stale, record(worker="@boss", ago=timedelta(minutes=1))])
    assert keys(report) == []
    assert ("activity_invalid", "journal") in diags(report)


@pytest.mark.parametrize(
    "bad",
    [
        {**record(), "extra": 1},
        {**record(), "at": "2026-10-07 11:00:00"},
        {**record(), "at": "2026-02-30T11:00:00Z"},
        {**record(), "schemaVersion": 2},
        "not a record",
    ],
)
def test_one_malformed_record_makes_the_journal_untrusted(bad):
    report = run({"KD-1": card("In Progress")}, [record(ago=timedelta(hours=30)), bad])
    assert keys(report) == []
    assert diags(report) == [
        ("activity_invalid", "journal"),
        ("activity_unknown", "KD-1"),
    ]


def test_future_activity_is_a_clock_problem_not_activity():
    report = run({"KD-1": card("In Progress")}, [record(ago=-timedelta(hours=2))])
    assert keys(report) == []
    assert diags(report) == [("future_date", "KD-1")]


def test_review_waiting_counts_from_review_submission_only():
    cards = {"KD-1": card("Review")}
    submitted = record(action="review_submission", ago=timedelta(hours=24))
    assert keys(run(cards, [submitted]), "review_waiting") == [
        ("review_waiting", "KD-1", "owner", "attention")
    ]
    almost = record(
        action="review_submission", ago=timedelta(hours=24) - timedelta(seconds=1)
    )
    assert keys(run(cards, [almost])) == []
    report = run(cards, [record(action="progress", ago=timedelta(hours=30))])
    assert keys(report) == [] and diags(report) == [("activity_unknown", "KD-1")]


@pytest.mark.parametrize(
    "created,expected,diag",
    [
        ("2026-10-06T12:00:00Z", ["KD-1"], []),
        ("2026-10-06T12:01:00Z", [], []),
        ("2026-10-6 12:00", [], [("invalid_date", "KD-1")]),
        ("2026-13-01T12:00:00Z", [], [("invalid_date", "KD-1")]),
        ("2026-10-06 12:00", [], [("invalid_date", "KD-1")]),  # the md board's form: not a cabinet stamp
        ("", [], [("invalid_date", "KD-1")]),
        ("2026-10-08T12:00:00Z", [], [("future_date", "KD-1")]),
    ],
)
def test_inbox_waiting_24h_from_created_date(created, expected, diag):
    report = run({"KD-1": card("Inbox", assignees=["@dev"], created=created)})  # held: the owner's rule alone
    assert [e.subject for e in report.events] == expected
    assert diags(report) == diag


@pytest.mark.parametrize(
    "now,due,rule",
    [
        (NOW, "2026-10-08", "due_tomorrow"),
        (NOW, "2026-10-07", "due_today"),
        (NOW, "2026-10-06", "overdue"),
        (NOW, "2026-10-09", None),
        # 21:30 UTC is already 00:30 on 8 October in UTC+3.
        (datetime(2026, 10, 7, 21, 30, tzinfo=timezone.utc), "2026-10-08", "due_today"),
        (
            datetime(2026, 10, 7, 20, 59, tzinfo=timezone.utc),
            "2026-10-08",
            "due_tomorrow",
        ),
        (NOW, "2026-10-08 18:00", "due_tomorrow"),
    ],
)
def test_due_rules_by_local_day(now, due, rule):
    report = run({"KD-1": card("In Progress", due=due)}, None, now=now)
    due_events = [
        e for e in report.events if e.rule in ("due_tomorrow", "due_today", "overdue")
    ]
    assert [e.rule for e in due_events] == ([rule] if rule else [])
    if rule:
        assert due_events[0].notify == ("owner" if rule == "overdue" else "assignee")


def test_due_ignores_done_and_scopes_bad_dates_to_due_rules():
    assert keys(run({"KD-1": card("Done", due="2026-10-01")})) == []
    report = run({"KD-1": card("To Do", due="2026-10-32")})
    assert keys(report) == [] and scoped(report) == [
        ("invalid_date", "KD-1", rule)
        for rule in ("due_tomorrow", "due_today", "overdue")
    ]


def test_activity_diagnostics_name_only_the_activity_rule():
    report = run({"KD-1": card("In Progress"), "KD-2": card("Review")}, None)
    assert scoped(report) == [
        ("activity_unknown", "KD-1", "in_progress_idle"),
        ("activity_unknown", "KD-2", "review_waiting"),
    ]


def test_inbox_unassigned_wakes_the_coordinator_for_cards_nobody_holds():
    cards = {
        "KD-1": card("Inbox", assignees=()),  # the case: nobody holds it
        "KD-2": card("Inbox", assignees=["@dev"]),  # held: the owner's inbox_waiting only
        "KD-3": card("To Do", assignees=()),  # route_mismatch's case
        "KD-5": card("Inbox", assignees=["@boss"]),  # coordinator_assigned's case
    }
    report = run(cards, None)
    assert keys(report, "inbox_unassigned") == [
        ("inbox_unassigned", "KD-1", "boss", "attention")
    ]
    assert [e.subject for e in report.events if e.rule == "route_mismatch"] == ["KD-3"]


def test_route_mismatch_for_unrouted_to_do_cards_only():
    cards = {
        "KD-1": card("To Do", assignees=()),
        "KD-2": card("To Do", assignees=["@sysadmin"]),
        "KD-3": card("To Do"),
        "KD-4": card("To Do", project="unknown"),
        "KD-5": card("In Progress", assignees=()),
        "KD-6": card("To Do", assignees=["@owner"], project="web"),
        # the owner's part of an agent's task stays in the agent's project — on the owner it is no mistake
        "KD-7": card("To Do", assignees=["@owner"], project="admin"),
        "KD-8": card("To Do", assignees=["@owner"], project="unknown"),
    }
    assert [
        e.subject for e in run(cards, None).events if e.rule == "route_mismatch"
    ] == ["KD-1", "KD-2", "KD-4", "KD-8"]


def test_a_card_assigned_before_its_project_was_rerouted_keeps_its_assignee():
    """a new route counts for new cards; one made before the route changed is no mismatch, unless nobody
    holds it."""
    cards = {
        "KD-1": card("To Do", assignees=["@sysadmin"], created="2026-10-07T10:00:00Z"),  # before the reroute
        "KD-2": card("To Do", assignees=["@sysadmin"], created="2026-10-07T12:00:00Z"),  # after it
        "KD-3": card("To Do", assignees=(), created="2026-10-07T10:00:00Z"),
    }
    report = ar.evaluate(CFG, cards, ROUTES, (), NOW, None, rerouted={"digipaddock": "2026-10-07T11:00:00Z"})
    assert [e.subject for e in report.events if e.rule == "route_mismatch"] == ["KD-2", "KD-3"]


# --- time and input hygiene -----------------------------------------------------------------


def test_clock_going_back_suppresses_timed_rules_but_not_blocked():
    cards = {"KD-1": card("Blocked"), "KD-2": card("In Progress", due="2026-10-01")}
    report = run(
        cards,
        [record("KD-2", ago=timedelta(days=3))],
        last_tick=NOW + timedelta(hours=1),
    )
    assert keys(report) == [("blocked", "KD-1", "boss", "attention")]
    assert diags(report) == [("time_jump", "clock")]


def test_small_clock_skew_is_tolerated():
    report = run({"KD-1": card("Blocked")}, last_tick=NOW + timedelta(seconds=30))
    assert diags(report) == []


@pytest.mark.parametrize(
    "card_id,raw",
    [
        ("KD-1\n", card()),
        ("kd-1", card()),
        ("KD-1", card(revision="x")),
        ("KD-1", card(revision="a" * 64)),
        ("KD-1", card(revision=0)),
        ("KD-1", card(revision=True)),
        ("KD-1", card(status="Archived")),
        ("KD-1", card(assignees=["dev"])),
        ("KD-1", card(deps=["KD-2,KD-3"])),
        ("KD-1", {k: v for k, v in card().items() if k != "due"}),
    ],
)
def test_invalid_cards_are_reported_and_skipped(card_id, raw):
    report = run({card_id: raw, "KD-9": card("Blocked")})
    assert [e.subject for e in report.events] == ["KD-9"]
    assert len(report.diagnostics) == 1 and report.diagnostics[0].kind == "invalid_card"
    assert report.diagnostics[0].subject in ("KD-1", "card")


def test_now_must_be_timezone_aware():
    with pytest.raises(ValueError):
        run(now=datetime(2026, 10, 7, 12, 0))


def test_events_are_deterministic_and_sorted_by_card_number():
    cards = {"KD-10": card("Blocked"), "KD-2": card("Blocked"), "KD-1": card("Blocked")}
    assert [e.subject for e in run(cards).events] == ["KD-1", "KD-2", "KD-10"]
    assert run(cards) == run(dict(reversed(list(cards.items()))))


def test_disabled_rule_is_silent():
    data = copy.deepcopy(SHIPPED)
    data["rules"]["blocked"]["enabled"] = False
    assert run({"KD-1": card("Blocked")}, cfg=ar.load_config(data)).events == ()


def test_card_on_the_coordinator_is_its_event_until_done():
    report = run(
        {
            "KD-1": card("In Progress", assignees=["@boss"]),
            "KD-2": card("Done", assignees=["@boss"]),
        }
    )
    assert keys(report, "coordinator_assigned") == [
        ("coordinator_assigned", "KD-1", "boss", "attention")
    ]
