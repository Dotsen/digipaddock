"""A server's page data, "Check now", the alarms in the inbox and the overview."""

import sys
from pathlib import Path

import pytest

from kd.cabinet import api, incidents

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cabinet_env import CabinetEnv  # noqa: E402

AT = "2026-10-14T13:00:00+03:00"  # 10:00Z, the cabinet's now


@pytest.fixture
def env(tmp_path):
    e = CabinetEnv(tmp_path)
    e.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    e.users.create("partner", "Партнёр", roles=[("partner", "*")], actor="root")
    e.store._conn.execute("INSERT INTO roles (id, perms) VALUES ('watch', '[\"servers.read\", \"cards.read\"]')")
    e.users.create("watcher", "Наблюдатель", roles=[("watch", "*")], actor="root")
    return e


def push(env, *items):
    return env.call("digi-sysadmin", {"op": "monitor.push", "items": list(items)})["results"]


def person(user):
    return api.Principal("user", 0, user)


def as_user(env, user, request):
    return api.dispatch(env.ctx, person(user), request)


SERVERS = [{"alias": "node-c", "host": "10.0.0.1", "unreachable_count": 0},
           {"alias": "node-d", "host": "10.0.0.2", "unreachable_count": 0},
           {"alias": "node-e", "host": "10.0.0.3", "unreachable_count": 3}]


def sync(key, servers=SERVERS, checks=(), changes=(), **extra):
    return {"key": key, "kind": "sync", "at": AT, "servers": list(servers), "checks": list(checks),
            "changes": list(changes), **extra}


def check(server, name, level, subject="-", value="v"):
    return {"server": server, "check": name, "rows": [{"subject": subject, "level": level, "value": value,
                                                      "text": ""}]}


def problem(key, level="crit", opened_at=AT):
    server, name, subject = key.split(":", 2)
    return {"key": key, "server": server, "check": name, "subject": subject, "level": level, "max_level": level,
            "value": "96%", "text": "", "opened_at": opened_at, "repeats": 0}


# --- AC2: a server's page --------------------------------------------------------------------------
def test_a_server_brings_the_whole_week_of_changes_not_the_newest_500(env):
    changes = [{"server": "node-c", "check": "disk", "subject": "/", "level": "warn" if i % 2 else "ok",
                "prev": "ok" if i % 2 else "warn", "value": "", "text": "",
                "at": f"2026-10-{8 + i // 1000:02d}T{(i // 60) % 24:02d}:{i % 60:02d}:00Z"} for i in range(1200)]
    push(env, sync("s1", changes=changes))
    old = dict(changes[0], at="2026-10-06T09:00:00Z")  # past the week: the purge's, not the page's
    push(env, sync("s2", changes=[old]))
    got = env.call("digi-sysadmin", {"op": "server.get", "alias": "node-c"})
    assert len(got["history"]) == 1200


def test_a_server_brings_its_levels_incidents_cards_and_trusted_lists(env):
    trust = {"trusted": {"203.0.113.10/32": "stage", "10.1.0.0/16": "офис"},
             "servers": {"node-c": {"identities": {"SHA256:abc (ED25519)": "owner"}, "ports": {"tcp:22": "sshd"},
                                 "wgpeers": []},
                         "node-d": {"identities": {}, "ports": {"udp:51820": "wg"}, "wgpeers": ["peerkey="]}}}
    push(env, sync("s1", checks=[check("node-c", "disk", "warn", "/"), check("node-c", "mem", "ok")], trust=trust),
         {"key": "o1", "kind": "open", "problem": problem("node-c:disk:/", "warn")})
    mine = env.card(title="Диск node-c", labels=["server:node-c"])
    env.card(title="Чужой", labels=["server:node-d"])
    got = as_user(env, "watcher", {"op": "server.get", "alias": "node-c"})
    assert got["server"]["worst"] == "warn"
    assert [(c["check"], c["level"]) for c in got["checks"]] == [("disk", "warn"), ("mem", "ok")]
    assert [i["id"] for i in got["incidents"]] == ["P-1"]
    assert [c["id"] for c in got["cards"]] == [mine.id]
    t = got["trust"]
    assert {r["item"] for r in t["trusted"]} == {"203.0.113.10/32", "10.1.0.0/16"}
    assert t["identities"] == [{"item": "SHA256:abc (ED25519)", "label": "owner"}]
    assert t["ports"] == [{"item": "tcp:22", "label": "sshd"}] and t["wgpeers"] == []
    # the next sync replaces a listed server's lists and keeps an unlisted one's
    push(env, sync("s2", trust={"trusted": {}, "servers": {"node-c": {"identities": {}, "ports": {}, "wgpeers": []}}}))
    assert env.call("digi-sysadmin", {"op": "server.get", "alias": "node-c"})["trust"]["ports"] == []
    vpn = env.call("digi-sysadmin", {"op": "server.get", "alias": "node-d"})["trust"]
    assert vpn["wgpeers"] == [{"item": "peerkey=", "label": ""}] and vpn["trusted"] == []


def test_a_partial_sync_never_switches_the_other_servers_off(env):
    push(env, sync("s1"))
    push(env, sync("s2", servers=[dict(SERVERS[0], unreachable_count=1)], partial=True))
    listed = {s["alias"]: s for s in env.call("digi-sysadmin", {"op": "server.list"})["items"]}
    assert {a: s["enabled"] for a, s in listed.items()} == {"node-c": 1, "node-d": 1, "node-e": 1}
    assert listed["node-c"]["reachable"] == 0
    push(env, sync("s3", servers=SERVERS[:1]))  # a full tick does
    listed = {s["alias"]: s["enabled"] for s in env.call("digi-sysadmin", {"op": "server.list"})["items"]}
    assert listed == {"node-c": 1, "node-d": 0, "node-e": 0}


# --- AC3: an incident's cards ----------------------------------------------------------------------
def test_an_incident_brings_its_labelled_and_fixed_cards(env):
    push(env, {"key": "o1", "kind": "open", "problem": problem("node-c:disk:/")})
    labelled = env.card(title="P-1 диск", labels=["P-1", "server:node-c"], project="admin")
    fixed = env.card(title="Почистить", project="admin")
    env.card(title="Другая", labels=["P-2"])
    push(env, {"key": "f", "kind": "update", "problem_key": "node-c:disk:/", "opened_at": AT, "change": "fixed",
               "card": fixed.id, "at": AT})
    got = as_user(env, "watcher", {"op": "incident.get", "incident": "P-1"})
    assert [c["id"] for c in got["cards"]] == [fixed.id, labelled.id]


# --- "Check now" -----------------------------------------------------------------------
def test_check_now_has_one_active_request_per_server_and_the_monitor_takes_and_reports_it(env):
    push(env, sync("s1"))
    first = as_user(env, "owner", {"op": "server.check.request", "alias": "node-c"})
    assert first["repeat"] is False and first["request"]["state"] == "queued"
    again = as_user(env, "owner", {"op": "server.check.request", "alias": "node-c"})
    assert again["repeat"] is True and again["request"]["id"] == first["request"]["id"]
    other = as_user(env, "owner", {"op": "server.check.request", "alias": "node-d"})
    taken = env.call("digi-sysadmin", {"op": "server.check.take"})["items"]
    assert {t["server"] for t in taken} == {"node-c", "node-d"}
    assert env.call("digi-sysadmin", {"op": "server.check.take"})["items"] == []  # running: not taken twice
    still = as_user(env, "owner", {"op": "server.check.request", "alias": "node-c"})
    assert still["repeat"] is True and still["request"]["state"] == "running"
    env.call("digi-sysadmin", {"op": "server.check.done", "id": first["request"]["id"], "ok": True})
    env.call("digi-sysadmin", {"op": "server.check.done", "id": other["request"]["id"], "ok": False,
                              "detail": "tick failed"})
    rows = {s["alias"]: s["check_now"] for s in env.call("digi-sysadmin", {"op": "server.list"})["items"]}
    assert rows["node-c"]["state"] == "done" and rows["node-d"]["state"] == "failed"
    assert rows["node-e"] is None
    states = [e["payload"]["state"] for e in env.store.events_since(0) if e["kind"] == "server.check"
              and e["payload"]["server"] == "node-c"]
    assert states == ["queued", "running", "done"]
    fresh = as_user(env, "owner", {"op": "server.check.request", "alias": "node-c"})  # finished: a new one
    assert fresh["repeat"] is False and fresh["request"]["id"] != first["request"]["id"]


def test_a_request_nobody_finished_in_ten_minutes_is_failed(env):
    push(env, sync("s1"))
    req = as_user(env, "owner", {"op": "server.check.request", "alias": "node-c"})["request"]
    env.now = "2026-10-14T10:09:59Z"
    assert env.call("digi-sysadmin", {"op": "server.get", "alias": "node-c"})["check_now"]["state"] == "queued"
    env.now = "2026-10-14T10:10:01Z"
    assert env.call("digi-sysadmin", {"op": "server.get", "alias": "node-c"})["check_now"]["state"] == "failed"
    assert env.call("root", {"op": "monitor.purge"})["checks_expired"] == 1
    late = env.call("digi-sysadmin", {"op": "server.check.done", "id": req["id"], "ok": True})
    assert late["request"]["state"] == "failed"
    assert as_user(env, "owner", {"op": "server.check.request", "alias": "node-c"})["repeat"] is False


def test_check_now_needs_incidents_ack_and_a_checked_server(env):
    push(env, sync("s1"), sync("s2", servers=SERVERS[:1]))
    for user in ("partner", "watcher"):
        with pytest.raises(api.ApiError) as err:
            as_user(env, user, {"op": "server.check.request", "alias": "node-c"})
        assert err.value.kind == "refused"
    with pytest.raises(api.ApiError) as err:
        as_user(env, "owner", {"op": "server.check.request", "alias": "node-d"})  # switched off
    assert err.value.kind == "invalid"
    with pytest.raises(api.ApiError) as err:
        as_user(env, "owner", {"op": "server.check.request", "alias": "nope"})
    assert err.value.kind == "not_found"
    for op in ({"op": "server.check.take"}, {"op": "server.check.done", "id": "x", "ok": True}):
        with pytest.raises(api.ApiError):
            as_user(env, "owner", op)
        with pytest.raises(api.ApiError):
            env.call("digi-dev", op)


def test_check_events_reach_only_who_reads_the_servers(env):
    push(env, sync("s1"))
    as_user(env, "owner", {"op": "server.check.request", "alias": "node-c"})
    events = [e for e in env.store.events_since(0) if e["kind"] == "server.check"]
    assert api.event_filter(env.ctx, person("watcher"))(events) == events
    assert api.event_filter(env.ctx, person("partner"))(events) == []


# --- AC6: alarms in the inbox and the overview -------------------------------------------------------
def test_open_crit_nobody_acknowledged_or_muted_count_in_the_inbox_until_ack_mute_or_resolve(env):
    push(env, sync("s1"), *[{"key": f"o{i}", "kind": "open", "problem": problem(k, level)} for i, (k, level) in
                           enumerate([("node-c:disk:/", "crit"), ("node-c:mem:ram", "warn"), ("node-d:reach:-", "crit"),
                                      ("node-e:disk:/", "crit")])])
    owner = person("owner")
    assert [a["id"] for a in incidents.alarms(env.ctx, owner)] == ["P-4", "P-3", "P-1"]
    base = api.inbox_count(env.ctx, owner)
    # P-1 and P-4: one tick's disk on two servers — one group row
    assert base == 2 and api.inbox_count(env.ctx, person("partner")) == 0
    as_user(env, "owner", {"op": "incident.ack", "incident": "P-1"})
    as_user(env, "owner", {"op": "incident.mute", "incident": "P-3", "until": "2026-10-14T11:00:00Z"})
    push(env, {"key": "r", "kind": "resolve", "problem_key": "node-e:disk:/", "opened_at": AT, "at": AT})
    assert api.inbox_count(env.ctx, owner) == 0
    env.now = "2026-10-14T11:00:01Z"  # the mute ran out
    assert [a["id"] for a in incidents.alarms(env.ctx, owner)] == ["P-3"]


def test_the_overview_puts_the_alarms_first_and_counts_the_servers(env):
    push(env, sync("s1", checks=[check("node-c", "disk", "crit", "/"), check("node-d", "mem", "warn"),
                                 check("node-e", "disk", "crit")]),
         {"key": "o", "kind": "open", "problem": problem("node-c:disk:/")})
    found = as_user(env, "owner", {"op": "overview.get"})
    assert found["problems"][0]["kind"] == "incident" and found["problems"][0]["incident"] == "P-1"
    assert found["servers"] == {"servers": 3, "crit": 1, "warn": 1, "unreachable": 1}
    assert found["counts"]["problems"] == 1
    assert as_user(env, "owner", {"op": "overview.get", "project": "digipaddock"})["problems"] == []
    blind = as_user(env, "partner", {"op": "overview.get"})
    assert blind["servers"] is None and blind["problems"] == []
