"""The monitor's servers, checks and incidents P-n in the cabinet."""

import sqlite3
import sys
from pathlib import Path

import pytest

from kd.cabinet import api, incidents, schema, store

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cabinet_env import CabinetEnv  # noqa: E402

AT = "2026-10-14T13:00:00+03:00"  # the monitor's clock (UTC3) — 10:00Z, the cabinet's now


@pytest.fixture
def env(tmp_path):
    e = CabinetEnv(tmp_path)
    e.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    e.users.create("partner", "Партнёр", roles=[("partner", "*")], actor="root")
    e.users.create("ops", "Дежурный", roles=[("viewer", "*")], actor="root")
    e.store._conn.execute("INSERT INTO roles (id, perms) VALUES ('watch', '[\"servers.read\"]')")
    e.users.create("watcher", "Наблюдатель", roles=[("watch", "*")], actor="root")
    return e


def push(env, *items, user="digi-sysadmin"):
    return env.call(user, {"op": "monitor.push", "items": list(items)})["results"]


def problem(key="node-c:disk:/", level="crit", value="96%", opened_at=AT, **over):
    server, check, subject = key.split(":", 2)
    return {"key": key, "server": server, "check": check, "subject": subject, "level": level, "max_level": level,
            "value": value, "text": f"{subject}: {value}", "opened_at": opened_at, "repeats": 0, **over}


def opened(item="k-open-1", **over):
    return {"key": item, "kind": "open", "problem": problem(**over)}


# --- AC1: the migration -----------------------------------------------------------------------------
def test_a_v18_base_migrates_to_the_monitor_tables_and_leaves_the_pipelines_problems_as_they_were(tmp_path):
    path = str(tmp_path / "v18.db")
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(schema.DDL + "".join(schema.MIGRATIONS[v] for v in range(2, 19)))
    conn.execute("INSERT INTO meta VALUES ('schema_version', '18')")
    conn.execute("INSERT INTO meta VALUES ('next_card', '1')")
    conn.execute("INSERT INTO problems (key, card, kind, created, updated) VALUES ('p1', 'KD-1', 'missing_next_step',"
                 " '2026-10-21T00:00:00Z', '2026-10-21T00:00:00Z')")
    before = conn.execute("SELECT sql FROM sqlite_master WHERE name='problems'").fetchone()[0]
    conn.close()
    db = store.Store.open(path)
    assert db.schema_version() == schema.SCHEMA_VERSION  # 19 and every later step
    tables = {r[0] for r in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"servers", "server_checks", "check_history", "incidents", "monitor_applied"} <= tables
    assert db._conn.execute("SELECT sql FROM sqlite_master WHERE name='problems'").fetchone()[0] == before
    assert [tuple(r) for r in db._conn.execute("SELECT key, card, kind FROM problems")] == [
        ("p1", "KD-1", "missing_next_step")]
    assert db._conn.execute("SELECT value FROM meta WHERE key='next_incident'").fetchone()[0] == "1"


# --- AC3: numbers -----------------------------------------------------------------------------------
def test_numbers_continue_the_imported_ones_and_a_repeated_open_of_an_open_key_keeps_its_number(env):
    out = env.call("digi-sysadmin", {"op": "incident.import", "next_id": 42, "items": [
        {"number": 41, **problem("node-c:mem:ram", "warn"), "closed_at": "2026-10-13T10:00:00+03:00"}]})
    assert out["added"] == [41] and out["next"] == 42
    first = push(env, opened("a"))
    assert first == [{"key": "a", "incident": 42, "new": True}]
    again = push(env, opened("b", opened_at="2026-10-14T13:05:00+03:00"))
    assert again == [{"key": "b", "incident": 42, "new": False}]
    other = push(env, opened("c", key="node-c:disk:/var"))
    assert other[0]["incident"] == 43


def test_a_resent_item_answers_its_first_result_and_changes_nothing(env):
    assert push(env, opened("a"))[0]["incident"] == 1
    push(env, {"key": "r", "kind": "resolve", "problem_key": "node-c:disk:/", "opened_at": AT, "at": AT})
    # the answer was lost: the monitor sends the same items again
    again = push(env, opened("a"), {"key": "r", "kind": "resolve", "problem_key": "node-c:disk:/", "opened_at": AT,
                                    "at": AT})
    assert [r.get("repeat") for r in again] == [True, True] and again[0]["incident"] == 1
    assert env.store._conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 1
    kinds = [e["kind"] for e in incidents.timeline(env.store, 1)]
    assert kinds == ["incident.opened", "incident.resolved"]


def test_a_bad_item_answers_its_error_and_the_rest_of_the_batch_is_applied(env):
    out = push(env, {"key": "x", "kind": "open", "problem": {"key": "node-c:disk:/"}}, opened("a"))
    assert "error" in out[0] and out[1]["incident"] == 1
    assert push(env, {"key": "x", "kind": "open", "problem": problem()})[0]["new"] is False  # not marked applied


# --- AC4: who --------------------------------------------------------------------------------------
@pytest.mark.parametrize("user", ["digi-dev", "digi-boss", "digi", "digi-cabinet"])
def test_writes_from_another_agent_an_executor_or_the_web_are_refused(env, user):
    with pytest.raises(api.ApiError) as err:
        push(env, opened("a"), user=user)
    assert err.value.kind == "refused"
    with pytest.raises(api.ApiError):
        env.call(user, {"op": "incident.import", "next_id": 1, "items": [{"number": 1, **problem()}]})


def test_a_person_never_writes_and_reads_only_with_servers_read(env):
    push(env, opened("a"))
    person = api.Principal("user", 0, "owner")
    with pytest.raises(api.ApiError) as err:
        api.dispatch(env.ctx, person, {"op": "monitor.push", "items": [opened("b")]})
    assert err.value.kind == "refused"
    for user in ("partner", "ops"):
        who = api.Principal("user", 0, user)
        for op in ({"op": "incident.list"}, {"op": "incident.get", "incident": 1}, {"op": "server.list"}):
            with pytest.raises(api.ApiError) as err:
                api.dispatch(env.ctx, who, op)
            assert err.value.kind == "refused"
    watcher = api.Principal("user", 0, "watcher")
    assert api.dispatch(env.ctx, watcher, {"op": "incident.list"})["items"][0]["id"] == "P-1"
    with pytest.raises(api.ApiError) as err:  # reading is not acknowledging
        api.dispatch(env.ctx, watcher, {"op": "incident.ack", "incident": 1})
    assert err.value.kind == "refused"
    for op in ({"op": "incident.mute", "incident": 1, "until": "2026-10-15T10:00:00Z"},
               {"op": "incident.unmute", "incident": 1}):
        with pytest.raises(api.ApiError):
            api.dispatch(env.ctx, api.Principal("user", 0, "partner"), op)
    owner = api.Principal("user", 0, "owner")
    assert api.dispatch(env.ctx, owner, {"op": "incident.ack", "incident": "P-1"})["incident"]["status"] == (
        "acknowledged")


def test_other_agents_do_not_read_the_servers(env):
    with pytest.raises(api.ApiError):
        env.call("digi-dev", {"op": "server.list"})
    assert env.call("digi-sysadmin", {"op": "server.list"})["items"] == []


def test_the_feed_shows_incident_events_only_to_who_reads_the_servers(env):
    push(env, opened("a"))
    events = [e for e in env.store.events_since(0) if not e["kind"].startswith("user.")]
    seen = api.event_filter(env.ctx, api.Principal("user", 0, "watcher"))(events)
    assert [e["kind"] for e in seen] == ["incident.opened"]
    assert api.event_filter(env.ctx, api.Principal("user", 0, "partner"))(events) == []


def test_monitor_writes_are_not_the_agents_board_writes(env):
    push(env, *[opened(f"k{i}", key=f"node-c:disk:/d{i}") for i in range(70)])
    # the rate limit of an agent's board writes counts its own actor's events
    assert env.store.count_events("agent:sysadmin", "2026-10-14T00:00:00Z") == 0


# --- AC6: checks and their history ---------------------------------------------------------------------
def sync(key, at, rows, changes=(), servers=None):
    item = {"key": key, "kind": "sync", "at": at,
            "checks": [{"server": "node-c", "check": "disk", "rows": rows}], "changes": list(changes)}
    if servers is not None:
        item["servers"] = servers
    return item


def test_a_sync_keeps_the_last_result_of_each_check_and_the_status_changes(env):
    servers = [{"alias": "node-c", "host": "10.0.0.1", "unreachable_count": 0},
               {"alias": "node-d", "host": "10.0.0.2", "unreachable_count": 2}]
    push(env, sync("s1", AT, [{"subject": "/", "level": "ok", "value": "50%", "text": ""}], servers=servers))
    push(env, sync("s2", "2026-10-14T13:05:00+03:00", [{"subject": "/", "level": "warn", "value": "91%", "text": ""}],
                   changes=[{"server": "node-c", "check": "disk", "subject": "/", "level": "warn", "prev": "ok",
                             "value": "91%", "text": "", "at": "2026-10-14T13:05:00+03:00"}]))
    push(env, sync("s3", "2026-10-14T13:10:00+03:00", [{"subject": "/", "level": "ok", "value": "60%", "text": ""}],
                   changes=[{"server": "node-c", "check": "disk", "subject": "/", "level": "ok", "prev": "warn",
                             "value": "60%", "text": "", "at": "2026-10-14T13:10:00+03:00"}],
                   servers=servers[:1]))
    got = env.call("digi-sysadmin", {"op": "server.get", "alias": "node-c"})
    assert [(c["subject"], c["level"], c["value"], c["at"]) for c in got["checks"]] == [
        ("/", "ok", "60%", "2026-10-14T10:10:00Z")]
    assert [(h["prev"], h["level"]) for h in got["history"]] == [("warn", "ok"), ("ok", "warn")]
    listed = {s["alias"]: s for s in env.call("digi-sysadmin", {"op": "server.list"})["items"]}
    assert listed["node-c"]["enabled"] == 1 and listed["node-c"]["last_ok"] == "2026-10-14T10:10:00Z"
    assert listed["node-d"]["enabled"] == 0 and listed["node-d"]["reachable"] == 0  # switched off: kept, inactive


def test_the_scheduler_drops_history_older_than_seven_days(env):
    old = {"server": "node-c", "check": "disk", "subject": "/", "level": "warn", "prev": "ok", "value": "", "text": "",
           "at": "2026-10-07T09:00:00Z"}
    new = dict(old, at="2026-10-08T11:00:00Z")
    push(env, sync("s1", AT, [], changes=[old, new]))
    out = env.call("root", {"op": "monitor.purge"})
    assert out["history"] == 1
    left = env.store._conn.execute("SELECT at FROM check_history").fetchall()
    assert [r[0] for r in left] == ["2026-10-08T11:00:00Z"]


# --- AC7 (cabinet part): ack, mute and the timeline -----------------------------------------------------
def test_the_timeline_of_an_incident_has_its_open_ack_mute_and_resolve(env):
    push(env, opened("a"))
    env.call("digi-sysadmin", {"op": "incident.ack", "incident": 1})
    env.call("digi-sysadmin", {"op": "incident.unmute", "incident": 1})
    env.call("digi-sysadmin", {"op": "incident.mute", "incident": 1, "until": "2026-10-15T09:00:00+03:00"})
    push(env, {"key": "u", "kind": "update", "problem_key": "node-c:disk:/", "opened_at": AT, "change": "fixed",
               "card": "KD-5", "at": AT},
         {"key": "r", "kind": "resolve", "problem_key": "node-c:disk:/", "opened_at": AT, "at": AT})
    got = env.call("digi-sysadmin", {"op": "incident.get", "incident": "P-1"})
    assert [e["kind"] for e in got["timeline"]] == [
        "incident.opened", "incident.acknowledged", "incident.unmuted", "incident.muted", "incident.fixed",
        "incident.resolved"]
    assert got["timeline"][1]["actor"] == "agent:sysadmin" and got["timeline"][0]["actor"] == "system:monitor"
    i = got["incident"]
    assert (i["status"], i["muted_until"], i["fixed_card"]) == ("resolved", "2026-10-15T06:00:00Z", "KD-5")
    with pytest.raises(api.ApiError):
        env.call("digi-sysadmin", {"op": "incident.ack", "incident": 1})


def test_an_update_moves_the_level_and_keeps_the_worst(env):
    push(env, opened("a", level="warn"))
    push(env, {"key": "w", "kind": "update", "problem_key": "node-c:disk:/", "opened_at": AT, "change": "worse",
               "level": "crit", "value": "97%", "at": AT},
         {"key": "b", "kind": "update", "problem_key": "node-c:disk:/", "opened_at": AT, "change": "better",
          "level": "warn", "value": "92%", "at": AT})
    i = env.call("digi-sysadmin", {"op": "incident.list", "status": "active"})["items"][0]
    assert (i["level"], i["max_level"], i["value"]) == ("warn", "crit", "92%")


def test_a_drift_value_changed_after_opening_is_an_event_and_a_threshold_value_is_not(env):
    """The owner accepts what is there now — he sees that a file changed again after the opening; a
    threshold's value moving within its level is no news."""
    cron = "node-c:sec:cron:/var/spool/cron/crontabs/u"
    H1, H2 = "1" * 64, "2" * 64
    push(env, opened("o1", key=cron, check="sec:cron", subject="/var/spool/cron/crontabs/u", level="warn",
                     value="aaa", value_hash=H1),
         opened("o2", level="warn", value="91%"))

    def tick(key, at, value, value_hash):
        return {"key": key, "kind": "sync", "at": at, "checks": [
            {"server": "node-c", "check": "sec:cron", "rows": [
                {"subject": "/var/spool/cron/crontabs/u", "level": "warn", "value": value, "text": "",
                 "value_hash": value_hash}]},
            {"server": "node-c", "check": "disk", "rows": [
                {"subject": "/", "level": "warn", "value": value[:2] + "%", "text": "", "value_hash": value_hash}]}]}

    push(env, tick("t1", "2026-10-14T13:05:00+03:00", "aaa", H1),  # the same content: nothing new
         tick("t2", "2026-10-14T13:10:00+03:00", "bbb", H2),
         tick("t3", "2026-10-14T13:15:00+03:00", "bbb", H2))
    def kinds(n):
        return [e["kind"] for e in env.call("digi-sysadmin", {"op": "incident.get", "incident": n})["timeline"]]

    assert kinds(1) == ["incident.opened", "incident.changed"] and kinds(2) == ["incident.opened"]
    got = env.call("digi-sysadmin", {"op": "incident.get", "incident": 1})
    assert got["timeline"][1]["payload"]["value"] == "bbb"
    from kd.cabinet import notify

    notify.Notifier(env.store, env.users, None).catch_up()  # silent: the timeline and the feed only
    assert env.store._conn.execute("SELECT COUNT(*) FROM notifications WHERE body LIKE '%bbb%'").fetchone()[0] == 0
    assert got["incident"]["changes"] == {"count": 1, "last_at": "2026-10-14T10:10:00Z"}
    assert env.call("digi-sysadmin", {"op": "incident.get", "incident": 2})["incident"]["changes"] == {
        "count": 0, "last_at": ""}


def test_the_incident_shows_the_last_recheck_of_its_server(env):
    """The "Recheck the server" action on the incident's page shows where the request is."""
    push(env, sync("s1", AT, [{"subject": "/", "level": "crit", "value": "96%", "text": ""}],
                   servers=[{"alias": "node-c", "host": "10.0.0.1", "unreachable_count": 0}]), opened("a"))
    assert env.call("digi-sysadmin", {"op": "incident.get", "incident": 1})["incident"]["recheck"] is None
    env.call("digi-sysadmin", {"op": "server.check.request", "alias": "node-c"})
    assert env.call("digi-sysadmin", {"op": "incident.get", "incident": 1})["incident"]["recheck"]["state"] == "queued"


# --- AC2 (cabinet part): the import -------------------------------------------------------------------------
def test_the_import_keeps_numbers_statuses_mutes_and_history_and_a_repeat_adds_nothing(env):
    items = [
        {"number": 7, **problem("node-c:disk:/"), "acked_until": "close", "last_seen": AT,
         "history": [{"at": AT, "kind": "open", "level": "crit", "value": "96%"}]},
        {"number": 9, **problem("node-c:mem:ram", "warn"), "acked_until": "2026-10-15T10:00:00+03:00",
         "fixed_card": "KD-12", "history": [{"at": AT, "kind": "open", "level": "warn", "value": "x"},
                                            {"at": AT, "kind": "fixed", "level": "warn", "value": "KD-12"}]},
        {"number": 3, **problem("node-d:reach:-"), "closed_at": "2026-10-10T10:00:00+03:00",
         "history": [{"at": AT, "kind": "open", "level": "crit", "value": ""},
                     {"at": AT, "kind": "close", "level": "ok", "value": ""}]},
    ]
    out = env.call("digi-sysadmin", {"op": "incident.import", "next_id": 10, "items": items})
    assert sorted(out["added"]) == [3, 7, 9] and out["next"] == 10
    rows = {r["n"]: r for r in env.call("digi-sysadmin", {"op": "incident.list"})["items"]}
    assert rows[7]["status"] == "acknowledged"
    assert (rows[9]["status"], rows[9]["muted_until"], rows[9]["fixed_card"]) == ("open", "2026-10-15T07:00:00Z",
                                                                                  "KD-12")
    assert (rows[3]["status"], rows[3]["resolved_at"]) == ("resolved", "2026-10-10T07:00:00Z")
    assert [e["kind"] for e in incidents.timeline(env.store, 3)] == ["incident.opened", "incident.resolved"]
    events = env.store._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    again = env.call("digi-sysadmin", {"op": "incident.import", "next_id": 10, "items": items})
    assert again["added"] == [] and sorted(again["kept"]) == [3, 7, 9]
    assert env.store._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events
    assert push(env, opened("n", key="node-c:load:-"))[0]["incident"] == 10


# --- incidents of one tick and check are a group --------------------------------------------------------
def tick_of(env, servers, check="sec:units", subject="x.service", level="warn", opened_at=AT, prefix="g"):
    return push(env, *[opened(f"{prefix}-{s}", key=f"{s}:{check}:{subject}", check=check, subject=subject,
                              level=level, opened_at=opened_at)
                       for s in servers])


SERVERS = [f"node-{c}" for c in "abcdefghijklm"]


def test_incidents_of_one_tick_and_check_are_a_group(env):
    tick_of(env, SERVERS)
    push(env, opened("lone", key="node-a:disk:/"))
    items = env.call("root", {"op": "incident.query", "limit": 100})["items"]
    grouped = [i for i in items if i["check"] == "sec:units"]
    assert len(grouped) == 13
    assert {i["group"]["size"] for i in grouped} == {13} and {i["group"]["servers"] for i in grouped} == {13}
    assert {i["group"]["subjects"] for i in grouped} == {1}
    assert grouped[0]["group"]["id"] == "2026-10-14T10:00:00Z|sec:units"


def test_a_single_incident_has_no_group(env):
    tick_of(env, SERVERS[:2])
    push(env, opened("lone", key="node-a:disk:/"))
    (lone,) = [i for i in env.call("root", {"op": "incident.query"})["items"] if i["check"] == "disk"]
    assert "group" not in lone


def test_the_group_filter_returns_only_its_members(env):
    tick_of(env, SERVERS[:3])
    tick_of(env, SERVERS[:2], opened_at="2026-10-14T13:05:00+03:00", prefix="h")
    got = env.call("root", {"op": "incident.query", "group": "2026-10-14T10:00:00Z|sec:units"})
    assert got["total"] == 3 and {i["server"] for i in got["items"]} == set(SERVERS[:3])
    with pytest.raises(api.ApiError) as bad:
        env.call("root", {"op": "incident.query", "group": "yesterday|sec:units"})
    assert bad.value.kind == "invalid"


def test_alarms_of_one_group_are_one_row(env):
    tick_of(env, SERVERS[:3], check="sec:keys", subject="/root/.ssh/authorized_keys", level="crit")
    push(env, opened("lone", key="node-a:disk:/"))
    rows = incidents.alarms(env.ctx, api.Principal("root", 0, ""))
    assert len(rows) == 4
    folded = incidents.collapse(rows)
    assert len(folded) == 2
    (group,) = [r for r in folded if r.get("members")]
    assert sorted(group["members"]) == ["P-1", "P-2", "P-3"] and group["servers"] == sorted(SERVERS[:3])
    assert group["group"] == {"id": "2026-10-14T10:00:00Z|sec:keys", "size": 3, "servers": 3, "subjects": 1}  # as the query's
