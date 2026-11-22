"""the owner's test critical alert — one request at a time, the monitor takes and reports it."""

import sys
from pathlib import Path

import pytest

from kd.cabinet import api

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cabinet_env import CabinetEnv  # noqa: E402


@pytest.fixture
def env(tmp_path):
    e = CabinetEnv(tmp_path)
    e.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    e.store._conn.execute("INSERT INTO roles (id, perms) VALUES ('watch', '[\"servers.read\", \"cards.read\"]')")
    e.users.create("watcher", "Наблюдатель", roles=[("watch", "*")], actor="root")
    return e


def as_user(env, user, request):
    return api.dispatch(env.ctx, api.Principal("user", 0, user), request)


def test_one_test_at_a_time_the_monitor_takes_and_reports_it(env):
    first = as_user(env, "owner", {"op": "monitor.test.request"})
    assert first["repeat"] is False and first["test"]["state"] == "queued"
    assert first["test"]["requested_by"] == "user:owner"
    again = as_user(env, "owner", {"op": "monitor.test.request"})
    assert again["repeat"] is True and again["test"]["id"] == first["test"]["id"]
    taken = env.call("digi-sysadmin", {"op": "monitor.test.take"})["test"]
    assert taken["id"] == first["test"]["id"] and taken["state"] == "running"
    assert env.call("digi-sysadmin", {"op": "monitor.test.take"})["test"] is None  # running: not taken twice
    assert as_user(env, "owner", {"op": "monitor.test.request"})["repeat"] is True
    done = env.call("digi-sysadmin", {"op": "monitor.test.done", "id": taken["id"], "ok": True})["test"]
    assert done["state"] == "done" and done["finished"]
    assert as_user(env, "watcher", {"op": "monitor.test.get"})["test"]["state"] == "done"
    states = [e["payload"]["state"] for e in env.store.events_since(0) if e["kind"] == "monitor.test"]
    assert states == ["queued", "running", "done"]
    fresh = as_user(env, "owner", {"op": "monitor.test.request"})  # finished: a new one
    assert fresh["repeat"] is False and fresh["test"]["id"] != first["test"]["id"]


def test_a_test_nobody_takes_or_finishes_fails_and_frees_the_button(env):
    first = as_user(env, "owner", {"op": "monitor.test.request"})["test"]
    env.now = "2026-10-14T10:16:00Z"  # past QUEUED_TTL_MIN of CabinetEnv's 10:00Z
    assert as_user(env, "owner", {"op": "monitor.test.get"})["test"]["state"] == "failed"
    assert env.call("digi-sysadmin", {"op": "monitor.test.take"})["test"] is None
    second = as_user(env, "owner", {"op": "monitor.test.request"})
    assert second["repeat"] is False
    assert env.call("digi-sysadmin", {"op": "monitor.test.take"})["test"]["state"] == "running"
    env.now = "2026-10-14T11:17:00Z"  # running past RUNNING_TTL_MIN
    late = env.call("digi-sysadmin", {"op": "monitor.test.done", "id": second["test"]["id"], "ok": True})
    assert late["test"]["state"] == "failed" and late["test"]["detail"] == "timeout"
    with pytest.raises(api.ApiError) as err:
        env.call("digi-sysadmin", {"op": "monitor.test.done", "id": first["id"], "ok": True})
    assert err.value.kind == "not_found"


def test_only_settings_asks_only_the_monitor_takes(env):
    with pytest.raises(api.ApiError) as err:
        as_user(env, "watcher", {"op": "monitor.test.request"})
    assert err.value.kind == "refused"
    for op in ({"op": "monitor.test.take"}, {"op": "monitor.test.done", "id": "x", "ok": True},
               {"op": "monitor.test.request"}):
        with pytest.raises(api.ApiError):
            env.call("digi-dev", op)
    for op in ({"op": "monitor.test.take"}, {"op": "monitor.test.done", "id": "x", "ok": True}):
        with pytest.raises(api.ApiError):
            as_user(env, "owner", op)
    assert env.call("root", {"op": "monitor.test.request"})["test"]["requested_by"] == "root"


def test_the_button_over_http_needs_settings(tmp_path):
    from web_env import WebEnv

    w = WebEnv(tmp_path)
    w.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    w.users.create("anna", "Анна", roles=[("partner", ["web"])], actor="root")
    w.login()
    assert w.client.get("/api/monitor-test").json() == {"test": None}
    r = w.client.post("/api/monitor-test", headers=w.headers())
    assert r.status_code == 200 and r.json()["repeat"] is False and r.json()["test"]["state"] == "queued", r.text
    assert w.client.post("/api/monitor-test", headers=w.headers()).json()["repeat"] is True
    assert w.client.get("/api/monitor-test").json()["test"]["state"] == "queued"
    anna = w.other_client()
    headers = w.headers(w.login("anna", client=anna)["csrf"])
    assert anna.get("/api/monitor-test").status_code == 403
    assert anna.post("/api/monitor-test", headers=headers).status_code == 403


def test_test_events_reach_only_who_reads_the_servers(env):
    env.users.create("partner", "Партнёр", roles=[("partner", "*")], actor="root")
    as_user(env, "owner", {"op": "monitor.test.request"})
    events = [e for e in env.store.events_since(0) if e["kind"] == "monitor.test"]
    assert len(events) == 1
    assert api.event_filter(env.ctx, api.Principal("user", 0, "watcher"))(events) == events
    assert api.event_filter(env.ctx, api.Principal("user", 0, "partner"))(events) == []
