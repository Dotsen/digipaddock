"""usable monitoring requires truthful snapshots and a durable investigation handoff."""

import sys
from pathlib import Path

import pytest

from kd.cabinet import api, monitoring

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cabinet_env import CabinetEnv  # noqa: E402


@pytest.fixture
def env(tmp_path):
    found = CabinetEnv(tmp_path, repo_root=Path(__file__).resolve().parents[2])
    found.users.create("owner", "Owner", roles=[("owner", "*")], actor="root")
    return found


def person(env, request):
    return api.dispatch(env.ctx, api.Principal("user", 0, "owner"), request)


def push(env, *items):
    return env.call("digi-sysadmin", {"op": "monitor.push", "items": list(items)})


def opened(env, n=1):
    push(
        env,
        {
            "kind": "open",
            "key": f"open-{n}",
            "problem": {
                "key": f"node-c:disk:/data{n}",
                "server": "node-c",
                "check": "disk",
                "subject": f"/data{n}",
                "level": "warn",
                "value": "86%",
                "text": "disk warning",
                "opened_at": env.now,
            },
        },
    )


def test_same_severity_observation_updates_snapshot_without_rewriting_history(env):
    opened(env)
    env.now = "2026-10-14T10:05:00Z"
    push(
        env,
        {
            "kind": "sync",
            "key": "sync",
            "at": env.now,
            "checks": [
                {
                    "server": "node-c",
                    "check": "disk",
                    "rows": [{"subject": "/data1", "level": "warn", "value": "89%"}],
                }
            ],
        },
    )
    found = person(env, {"op": "incident.get", "incident": 1})
    assert found["incident"]["value"] == "89%"
    assert found["incident"]["last_seen"] == env.now
    assert found["timeline"][0]["payload"]["value"] == "86%"
    assert len(found["timeline"]) == 1


def test_late_sync_preserves_measurement_time_and_cannot_overwrite_newer_data(env):
    for number, at, value in [
        (1, "2026-10-14T09:00:00Z", "86%"),
        (2, env.now, "89%"),
        (3, "2026-10-14T09:30:00Z", "87%"),
    ]:
        push(
            env,
            {
                "kind": "sync",
                "key": str(number),
                "at": env.now,
                "checks": [
                    {
                        "server": "node-c",
                        "check": "disk",
                        "at": at,
                        "rows": [{"subject": "/", "level": "warn", "value": value}],
                    }
                ],
            },
        )
    row = env.store._conn.execute("SELECT at, value FROM server_checks").fetchone()
    assert tuple(row) == (env.now, "89%")


def test_imported_events_receive_identity_without_replacing_historical_values(env):
    env.call(
        "digi-sysadmin",
        {
            "op": "incident.import",
            "items": [
                {
                    "number": 333,
                    "key": "node-c:sec:units:x",
                    "server": "node-c",
                    "check": "sec:units",
                    "subject": "x",
                    "level": "warn",
                    "value": "new",
                    "opened_at": env.now,
                    "history": [
                        {"kind": "open", "level": "warn", "value": "old", "at": env.now}
                    ],
                }
            ],
        },
    )
    payload = person(env, {"op": "incident.get", "incident": 333})["timeline"][0][
        "payload"
    ]
    assert (
        payload["server"],
        payload["check"],
        payload["subject"],
        payload["value"],
    ) == ("node-c", "sec:units", "x", "old")


def test_handoff_is_atomic_idempotent_and_does_not_approve_a_plan(env):
    opened(env)
    request = {
        "op": "incident.handoff",
        "incident": 1,
        "project": "admin",
        "comment": "investigate",
    }
    first, second = person(env, request), person(env, request)
    assert first["card"]["id"] == second["card"]["id"] and second["repeat"]
    card = env.store.card(first["card"]["id"])
    assert card.status == "Inbox" and card.assignee == "@sysadmin"
    assert card.servers == ["node-c"] or tuple(card.servers) == ("node-c",)
    assert "P-1" in card.labels and "investigate" in card.description
    commands = env.store.commands(card=card.id)
    assert len(commands) == 1 and commands[0]["state"] == "queued"
    assert "read-only" in commands[0]["text"]
    assert (
        person(env, {"op": "incident.get", "incident": 1})["cards"][0]["id"] == card.id
    )
    assert env.store._conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0


def test_handoff_does_not_duplicate_closed_work_or_accept_a_resolved_incident(env):
    opened(env)
    request = {"op": "incident.handoff", "incident": 1, "project": "admin"}
    first = person(env, request)["card"]
    env.store.edit_card(first["id"], None, {"status": "Done"}, actor="root")
    assert person(env, request)["card"]["id"] != first["id"]
    push(
        env,
        {
            "kind": "resolve",
            "key": "close",
            "problem_key": "node-c:disk:/data1",
            "at": env.now,
        },
    )
    with pytest.raises(api.ApiError):
        person(env, request)


def test_query_pages_over_500_and_rejects_cursor_from_another_filter(env):
    for n in range(1, 506):
        opened(env, n)
    request = {"op": "incident.query", "limit": 100}
    seen, cursor = [], ""
    while True:
        answer = person(env, {**request, "cursor": cursor})
        seen.extend(i["id"] for i in answer["items"])
        cursor = answer["next_cursor"]
        assert answer["total"] == 505
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 505
    cursor = person(env, request)["next_cursor"]
    with pytest.raises(api.ApiError):
        person(env, {**request, "cursor": cursor, "level": "crit"})


def test_daily_and_hourly_checks_have_independent_freshness(env):
    cfg = {"every_min": {"reboot": 1440, "ports": 60}}
    row = {"server": "node-c", "subject": "", "at": "2026-10-13T13:00:00Z", "level": "ok"}
    assert (
        monitoring.check_view(env.ctx, {**row, "check": "reboot"}, cfg)["freshness"]
        == "fresh"
    )
    assert (
        monitoring.check_view(env.ctx, {**row, "check": "ports"}, cfg)["freshness"]
        == "stale"
    )


def test_resuming_mute_preserves_acknowledgement_and_unack_preserves_mute(env):
    opened(env)
    person(env, {"op": "incident.ack", "incident": 1})
    person(env, {"op": "incident.mute", "incident": 1, "until": "2026-10-15T10:00:00Z"})
    found = person(env, {"op": "incident.unmute", "incident": 1, "preserve_ack": True})[
        "incident"
    ]
    assert found["status"] == "acknowledged" and not found["muted_until"]
    person(env, {"op": "incident.mute", "incident": 1, "until": "2026-10-15T10:00:00Z"})
    found = person(env, {"op": "incident.unack", "incident": 1})["incident"]
    assert found["status"] == "open" and found["muted_until"]


def test_linking_existing_work_uses_card_permissions_and_revision(env):
    opened(env)
    card = env.card(status="To Do", assignee="@dev")
    request = {
        "op": "incident.link",
        "incident": 1,
        "card": card.id,
        "revision": card.revision,
        "transfer": True,
    }
    result = person(env, request)["card"]
    assert result["status"] == "Inbox" and result["assignee"] == "@sysadmin"
    assert "node-c" in result["servers"] and "P-1" in result["labels"]
    with pytest.raises(api.ApiError) as stale:
        person(env, request)
    assert stale.value.kind == "conflict"


def test_a_new_investigation_carries_the_incident_triage_specialization(env):
    """the sysadmin works an investigation the cabinet opens by its incident-triage procedure."""
    opened(env)
    card = person(env, {"op": "incident.handoff", "incident": 1, "project": "admin"})[
        "card"
    ]
    labels = env.store.card(card["id"]).labels
    assert "monitor:investigation" in labels and "spec:incident-triage" in labels
    assert [v for v in labels if v.startswith(("spec:", "skill:"))] == [
        "spec:incident-triage"
    ]
    assert "incident-triage" in env.ctx.specs()["@sysadmin"]


def test_a_transferred_card_trades_its_selection_for_incident_triage(env):
    """a card handed over to the investigation drops the previous assignee's spec and skills."""
    opened(env)
    card = env.card(
        status="To Do",
        assignee="@dev",
        labels=["spec:implementer", "skill:python-backend", "keep"],
    )
    result = person(
        env,
        {
            "op": "incident.link",
            "incident": 1,
            "card": card.id,
            "revision": card.revision,
            "transfer": True,
        },
    )["card"]
    labels = env.store.card(result["id"]).labels
    assert "monitor:investigation" in labels and "keep" in labels
    assert [v for v in labels if v.startswith(("spec:", "skill:"))] == [
        "spec:incident-triage"
    ]


def test_linking_without_transfer_keeps_the_cards_own_selection(env):
    opened(env)
    card = env.card(status="To Do", assignee="@dev", labels=["spec:implementer"])
    result = person(
        env,
        {
            "op": "incident.link",
            "incident": 1,
            "card": card.id,
            "revision": card.revision,
        },
    )["card"]
    labels = env.store.card(result["id"]).labels
    assert "spec:implementer" in labels and "spec:incident-triage" not in labels


def test_failed_enqueue_rolls_back_card_and_incident_link(env, monkeypatch):
    opened(env)
    before = len(env.store.cards())

    def fail(_spec):
        raise RuntimeError("simulated queue failure")

    monkeypatch.setattr(env.store, "_enqueue_in_tx", fail)
    with pytest.raises(RuntimeError):
        person(env, {"op": "incident.handoff", "incident": 1, "project": "admin"})
    assert len(env.store.cards()) == before
    assert (
        env.store._conn.execute("SELECT COUNT(*) FROM incident_cards").fetchone()[0]
        == 0
    )


def test_monitoring_reader_cannot_create_or_link_investigations(env):
    opened(env)
    env.store._conn.execute(
        "INSERT INTO roles VALUES ('monitor_reader', '[\"servers.read\"]')"
    )
    env.users.create("reader", "Reader", roles=[("monitor_reader", "*")], actor="root")
    who = api.Principal("user", 0, "reader")
    assert api.dispatch(env.ctx, who, {"op": "incident.query"})["total"] == 1
    with pytest.raises(api.ApiError) as refused:
        api.dispatch(
            env.ctx, who, {"op": "incident.handoff", "incident": 1, "project": "admin"}
        )
    assert refused.value.kind == "refused"


def test_late_change_keeps_current_snapshot_and_records_original_event(env):
    opened(env)
    push(
        env,
        {
            "kind": "update",
            "key": "late",
            "problem_key": "node-c:disk:/data1",
            "change": "worse",
            "level": "crit",
            "value": "95%",
            "at": "2026-10-14T09:00:00Z",
        },
    )
    found = person(env, {"op": "incident.get", "incident": 1})
    assert found["incident"]["value"] == "86%"
    assert found["incident"]["level"] == "warn"
    assert any(e["payload"].get("value") == "95%" for e in found["timeline"])


def test_delivery_belongs_to_an_incident_and_latest_retry_wins_same_second(env):
    opened(env)
    opened(env, 2)
    card = person(env, {"op": "incident.handoff", "incident": 1, "project": "admin"})[
        "card"
    ]
    person(
        env,
        {
            "op": "incident.link",
            "incident": 2,
            "card": card["id"],
            "revision": card["revision"],
        },
    )
    assert (
        person(env, {"op": "incident.get", "incident": 2})["incident"]["delivery"]
        is None
    )
    env.store._conn.execute(
        "UPDATE commands SET state='failed' WHERE card=?", (card["id"],)
    )
    person(env, {"op": "incident.retry", "incident": 1})
    assert (
        person(env, {"op": "incident.get", "incident": 1})["incident"]["delivery"][
            "state"
        ]
        == "queued"
    )


@pytest.mark.parametrize(
    "filters",
    [
        {"status": []},
        {"cursor": "W10="},
        {"cursor": {"offset": 1}},
        {"since": 42},
    ],
)
def test_malformed_queue_filters_are_validation_errors(env, filters):
    with pytest.raises(api.ApiError) as refused:
        person(env, {"op": "incident.query", **filters})
    assert refused.value.kind == "invalid"
