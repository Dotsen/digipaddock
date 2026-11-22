"""The owner edits the monitor's settings in the cabinet over config.json; a weakening needs
his step-up, every save is an event people with servers.read see."""

import json

import pytest
from cabinet_env import MONITOR_CONFIG, CabinetEnv

from kd.cabinet import api


@pytest.fixture
def env(tmp_path):
    env = CabinetEnv(tmp_path)
    env.users.set_role("ops", ["servers.read"], actor="root")
    env.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    env.users.create("anna", "Анна", roles=[("partner", ["web"])], actor="root")
    env.users.create("olga", "Ольга", roles=[("ops", "*")], actor="root")
    return env


def as_user(env, user, request):
    return api.dispatch(env.ctx, api.Principal("user", -1, user), request)


def read(env, user="owner"):
    return as_user(env, user, {"op": "monitor.settings.get"})


def save(env, *changes, signed=False, user="owner", revision=None):
    rev = read(env)["revision"] if revision is None else revision
    return as_user(env, user, {"op": "monitor.settings.save", "revision": rev, "changes": list(changes),
                               "signed": signed})


def edits(env):
    return {(i["key"], i["server"]): i["value"] for i in read(env)["items"]}


def refused(env, kind, *changes, **kw):
    before = (read(env)["revision"], edits(env))
    with pytest.raises(api.ApiError) as exc:
        save(env, *changes, **kw)
    assert exc.value.kind == kind, str(exc.value)
    assert (read(env)["revision"], edits(env)) == before  # nothing changed
    return str(exc.value)


def events(env, user=None):
    if user is None:
        found = env.call("root", {"op": "events.since", "after": 0})["events"]
    else:
        found = as_user(env, user, {"op": "events.since", "after": 0})["events"]
    return [e for e in found if e["kind"] == "monitor.settings"]


# --- AC1: an edit is saved -------------------------------------------------------------------------------------
def test_a_threshold_edit_is_saved_and_moves_the_revision(env):
    assert read(env)["revision"] == 0 and read(env)["items"] == []
    out = save(env, {"key": "disk_pct.warn", "value": 80})
    assert out["revision"] == 1 and out["changes"][0]["before"] == 85 and out["changes"][0]["after"] == 80
    assert edits(env) == {("disk_pct.warn", ""): 80}
    item = read(env)["items"][0]
    assert (item["by"], item["stale"]) == ("user:owner", False)


# --- AC3: each kind of weakening needs the step-up -------------------------------------------------------------
WEAKENING = [
    {"key": "disabled_checks", "server": "example-prod", "value": ["certs"]},  # a check off
    {"key": "units_ignore", "value": ["digipaddock-monitor.service", "digipaddock-alert@*.service", "cron-*"]},  # ignore
    {"key": "units_confirm", "value": ["cron-*", "backup-*"]},  # confirm first
    {"key": "disk_pct.warn", "value": 90},  # a softer threshold
    {"key": "mem_available_mb.warn", "server": "node-l", "value": 80},  # a softer one of a server
    {"key": "every_min.certs", "value": 720},  # a longer interval
    {"key": "unreachable_after", "value": 5},
    {"key": "quiet_hours", "value": {"start": "22:00", "end": "08:00"}},  # wider quiet hours
    {"key": "max_wakes_per_hour", "value": 3},  # fewer wakes
    {"key": "critical_unreachable", "value": ["example-prod"]},  # a server out of the critical list
]


@pytest.mark.parametrize("change", WEAKENING, ids=lambda c: c["key"])
def test_each_weakening_without_a_step_up_changes_nothing_and_with_it_is_saved(env, change):
    refused(env, "stepup_required", change)
    out = save(env, change, signed=True)
    assert out["signed"] is True and out["changes"][0]["weaker"] is True
    assert edits(env)[(change["key"], change.get("server", ""))] == change["value"]


# --- AC4: tightening, a reset that tightens, one signature for a mixed save ----------------------------------------
def test_tightening_and_a_tightening_reset_need_no_step_up_and_a_mixed_save_one_signature(env):
    out = save(env, {"key": "disk_pct.warn", "value": 80}, {"key": "max_wakes_per_hour", "value": 10})
    assert out["signed"] is False and [c["weaker"] for c in out["changes"]] == [False, False]
    save(env, {"key": "every_min.certs", "value": 720}, signed=True)
    out = save(env, {"key": "every_min.certs", "reset": True})  # back to 360: tighter, no step-up
    assert out["changes"][0] == {"key": "every_min.certs", "server": "", "before": 720, "after": 360,
                                 "reset": True, "weaker": False}
    refused(env, "stepup_required", {"key": "disk_pct.warn", "reset": True})  # 80 -> 85: softer
    mixed = ({"key": "disk_pct.crit", "value": 90}, {"key": "every_min.backup", "value": 120})
    refused(env, "stepup_required", *mixed)
    out = save(env, *mixed, signed=True)
    assert [c["weaker"] for c in out["changes"]] == [False, True] and out["signed"] is True
    assert len(events(env)) == 4  # one event per save, the mixed one included


def test_a_save_that_changes_nothing_writes_nothing(env):
    out = save(env, {"key": "disk_pct.warn", "reset": True})
    assert (out["revision"], out["changes"], out["signed"]) == (0, [], False) and events(env) == []


# --- AC5: the event, who reads it ----------------------------------------------------------------------------------
def test_every_save_is_an_event_for_servers_read_only(env):
    save(env, {"key": "disk_pct.warn", "value": 90}, signed=True)
    found = events(env)
    assert len(found) == 1
    event = found[0]
    assert event["actor"] == "user:owner"
    assert event["payload"]["signed"] is True and event["payload"]["revision"] == 1
    assert event["payload"]["changes"] == [{"key": "disk_pct.warn", "server": "", "before": 85, "after": 90,
                                            "reset": False, "weaker": True}]
    assert len(events(env, "owner")) == 1 and len(events(env, "olga")) == 1
    assert events(env, "anna") == []


# --- AC6: bad values and a stale revision ------------------------------------------------------------------------
@pytest.mark.parametrize("change", [
    {"key": "disk_pct.warn", "value": "eighty"},
    {"key": "disk_pct.warn", "value": 96},  # crit 95 is no longer stricter
    {"key": "mem_available_pct.crit", "value": 12},
    {"key": "mem_available_mb.warn", "server": "ghost", "value": 100},
    {"key": "disabled_checks", "server": "node-c", "value": ["ghost"]},
    {"key": "critical_unreachable", "value": ["ghost"]},
    {"key": "parallel", "value": 8},
    {"key": "vpn_nodes", "value": []},
    {"key": "disk_pct.warn", "server": "node-c", "value": 80},
    {"key": "disabled_checks", "server": "node-l", "value": ["heartbeat"]},  # runs on the database server only
    {"key": "disabled_checks", "server": "example-stage", "value": ["mysql"]},
    {"key": "security.updates.critical_warn_days", "value": 7},  # crit_days 7 is no longer stricter
], ids=lambda c: f"{c['key']}={c.get('value')}")
def test_bad_values_are_refused_and_nothing_changes(env, change):
    refused(env, "invalid", change, signed=True)


def test_one_bad_change_saves_none_of_the_save(env):
    refused(env, "invalid", {"key": "disk_pct.warn", "value": 80}, {"key": "tick_budget_s", "value": 10})
    refused(env, "invalid", {"key": "disk_pct.warn", "value": 80}, {"key": "disk_pct.warn", "value": 70})


def test_a_stale_revision_is_refused(env):
    save(env, {"key": "disk_pct.warn", "value": 80})
    refused(env, "conflict", {"key": "disk_pct.crit", "value": 99}, signed=True, revision=0)
    refused(env, "invalid", {"key": "disk_pct.crit", "value": 99}, signed=True, revision="1")


# --- AC8: the defaults, who edits ---------------------------------------------------------------------------------
def test_the_page_reads_defaults_and_a_key_without_an_edit_follows_config_json(env, tmp_path):
    got = read(env)
    assert got["defaults"]["disk_pct.warn"] == 85 and got["defaults"]["quiet_hours"] == {"start": "23:00",
                                                                                       "end": "08:00"}
    assert got["server_defaults"]["node-l"] == {"mem_available_mb.warn": 100, "mem_available_mb.crit": 50}
    assert "example-prod" in got["servers"] and any(e["key"] == "disabled_checks" for e in got["catalog"])
    assert {"heartbeat", "mysql", "certs"} <= set(got["checks"]["example-prod"])
    assert "heartbeat" not in got["checks"]["node-l"] and "wg" in got["checks"]["node-l"]
    save(env, {"key": "disk_pct.crit", "value": 97}, signed=True)
    release = json.loads(MONITOR_CONFIG.read_text(encoding="utf-8"))
    release["disk_pct"] = {"warn": 70, "crit": 90}
    del release["every_min"]["cores"]
    path = tmp_path / "config.json"
    path.write_text(json.dumps(release), encoding="utf-8")
    env.ctx.monitor_config = str(path)
    got = read(env)
    assert got["defaults"]["disk_pct.warn"] == 70  # no edit: the new release's default
    assert edits(env) == {("disk_pct.crit", ""): 97}  # the edit stays until reset
    # an edit of a key the release no longer has: not applied, marked stale; a new one is refused, a reset is not
    env.ctx.monitor_config = str(MONITOR_CONFIG)
    save(env, {"key": "every_min.cores", "value": 2880}, signed=True)
    env.ctx.monitor_config = str(path)
    assert {i["key"]: i["stale"] for i in read(env)["items"]}["every_min.cores"] is True
    refused(env, "invalid", {"key": "every_min.cores", "value": 1440})
    save(env, {"key": "every_min.cores", "reset": True})
    assert ("every_min.cores", "") not in edits(env)


def test_only_settings_edits_and_servers_read_reads(env):
    with pytest.raises(api.ApiError) as exc:
        save(env, {"key": "disk_pct.warn", "value": 80}, user="anna")
    assert exc.value.kind == "refused"
    with pytest.raises(api.ApiError):
        save(env, {"key": "disk_pct.warn", "value": 80}, user="olga")  # reads, does not edit
    with pytest.raises(api.ApiError):
        read(env, "anna")
    assert read(env, "olga")["revision"] == 0
    for agent in ("digi-sysadmin", "digi-dev"):
        with pytest.raises(api.ApiError) as exc:
            env.call(agent, {"op": "monitor.settings.save", "revision": 0,
                             "changes": [{"key": "disk_pct.warn", "value": 80}], "signed": True})
        assert exc.value.kind == "refused"
    assert env.call("digi-sysadmin", {"op": "monitor.settings.get"})["revision"] == 0  # the monitor reads
    with pytest.raises(api.ApiError):
        env.call("digi-dev", {"op": "monitor.settings.get"})
    assert edits(env) == {}


# --- over HTTP: the step-up of purpose monitor.settings ----------------------------------------------------------
def test_the_web_asks_a_step_up_only_for_a_weakening_and_takes_one_of_this_purpose(tmp_path):
    from softauth import SoftAuthenticator
    from web_env import WebEnv

    w = WebEnv(tmp_path)
    w.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    w.users.create("anna", "Анна", roles=[("partner", ["web"])], actor="root")
    w.login()
    auth = SoftAuthenticator(w.cfg.rp_id, w.cfg.origin)
    opts = w.client.post("/api/me/passkeys/options", headers=w.headers()).json()
    r = w.client.post("/api/me/passkeys", headers=w.headers(),
                      json={"credential": auth.register(opts["options"]), "name": "iPhone"})
    assert r.status_code == 200, r.text

    def proof(purpose):
        step = w.client.post("/api/stepup/challenge", headers=w.headers(), json={"purpose": purpose}).json()
        return {"challenge_id": step["challenge_id"], "credential": auth.assert_(step["options"])}

    got = w.client.get("/api/monitor-settings").json()
    assert got["revision"] == 0 and got["defaults"]["disk_pct.warn"] == 85
    tighter = {"revision": 0, "changes": [{"key": "disk_pct.warn", "value": 80}]}
    r = w.client.put("/api/monitor-settings", headers=w.headers(), json=tighter)
    assert r.status_code == 200 and r.json()["signed"] is False, r.text
    softer = {"revision": 1, "changes": [{"key": "disabled_checks", "server": "example-prod", "value": ["certs"]}]}
    r = w.client.put("/api/monitor-settings", headers=w.headers(), json=softer)
    assert r.status_code == 403 and r.json()["error"] == "stepup_required"
    r = w.client.put("/api/monitor-settings", headers=w.headers(),
                     json={**softer, "stepup": proof("projects.delete")})  # another purpose
    assert r.status_code == 403 and r.json()["error"] == "stepup_required"
    r = w.client.put("/api/monitor-settings", headers=w.headers(), json={**softer, "signed": True})
    assert r.status_code == 403  # the page cannot claim a signature
    signature = proof("monitor.settings")
    r = w.client.put("/api/monitor-settings", headers=w.headers(), json={**softer, "revision": 0, "stepup": signature})
    assert r.status_code == 409  # a stale revision answers before the step-up is spent
    r = w.client.put("/api/monitor-settings", headers=w.headers(), json={**softer, "stepup": signature})
    assert r.status_code == 200 and r.json()["signed"] is True, r.text
    assert w.client.get("/api/monitor-settings").json()["revision"] == 2
    anna = w.other_client()
    headers = w.headers(w.login("anna", client=anna)["csrf"])
    assert anna.get("/api/monitor-settings").status_code == 403
    assert anna.put("/api/monitor-settings", headers=headers, json=tighter).status_code == 403


def test_the_monitor_still_gets_the_edits_when_config_json_is_unreadable(env, tmp_path):
    save(env, {"key": "disk_pct.warn", "value": 80})
    env.ctx.monitor_config = str(tmp_path / "missing.json")
    got = env.call("digi-sysadmin", {"op": "monitor.settings.get"})
    assert got["items"][0]["value"] == 80 and "defaults" not in got
    with pytest.raises(api.ApiError) as exc:
        read(env)
    assert exc.value.kind == "unavailable"
