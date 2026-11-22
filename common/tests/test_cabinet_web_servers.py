"""Servers and incidents P-n over HTTP — reading, ack and mute, "Check now"."""

import pytest
from web_env import WebEnv

AT = "2026-10-14T13:00:00+03:00"


@pytest.fixture
def w(tmp_path):
    w = WebEnv(tmp_path)
    w.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    w.users.create("anna", "Анна", roles=[("partner", "*")], actor="root")
    w.env.store._conn.execute("INSERT INTO roles (id, perms) VALUES ('watch', '[\"servers.read\", \"cards.read\"]')")
    w.users.create("ops", "Дежурный", roles=[("watch", "*")], actor="root")
    w.env.call("digi-sysadmin", {"op": "monitor.push", "items": [
        {"key": "s", "kind": "sync", "at": AT, "servers": [{"alias": "node-c", "host": "", "unreachable_count": 0}],
         "checks": [{"server": "node-c", "check": "disk", "rows": [{"subject": "/", "level": "crit", "value": "96%",
                                                                 "text": ""}]}], "changes": []},
        {"key": "o", "kind": "open", "problem": {"key": "node-c:disk:/", "server": "node-c", "check": "disk", "subject": "/",
                                                "level": "crit", "max_level": "crit", "value": "96%", "text": "",
                                                "opened_at": AT, "repeats": 0}}]})
    return w


ROUTES = [("get", "/api/servers", None), ("get", "/api/servers/node-c", None), ("post", "/api/servers/node-c/check", {}),
          ("get", "/api/incidents", None), ("get", "/api/incidents/P-1", None),
          ("post", "/api/incidents/P-1/ack", {}), ("post", "/api/incidents/P-1/mute", {"for": "1h"}),
          ("post", "/api/incidents/P-1/unmute", {})]


def test_a_partner_gets_403_on_every_route(w):
    anna = w.other_client()
    me = w.login("anna", client=anna)
    for method, path, body in ROUTES:
        kw = {"headers": w.headers(me["csrf"]), "json": body} if method == "post" else {}
        r = getattr(anna, method)(path, **kw)
        assert r.status_code == 403, (path, r.text)


def _alert(w, level="critical", **params):
    facts = {"host": "stage", "unit": "digipaddock-cabinet.service", "reason": "kd-cabinet: boom", **params}
    return w.env.call("root", {"op": "notice.post", "source": "alert", "level": level, "key": "notice.alert.unit",
                               "params": facts})["event"]


def test_the_owner_reads_the_platforms_alerts_and_pages_them(w):
    """The incidents page's block: where a push about an alert without a card leads."""
    seqs = [_alert(w, reason=f"boom {n}") for n in range(3)]
    w.login("owner")
    page = w.client.get("/api/alerts", params={"limit": 2}).json()
    assert [a["seq"] for a in page["items"]] == [seqs[2], seqs[1]] and page["next"] == seqs[1]
    assert page["items"][0]["key"] == "notice.alert.unit" and page["items"][0]["params"]["reason"] == "boom 2"
    assert page["items"][0]["level"] == "critical" and page["items"][0]["source"] == "alert"
    page = w.client.get("/api/alerts", params={"limit": 2, "before": page["next"]}).json()
    assert [a["seq"] for a in page["items"]] == [seqs[0]] and page["next"] is None
    assert w.client.get("/api/alerts", params={"limit": 0}).status_code == 400


def test_the_alerts_are_the_settings_holders_only(w):
    _alert(w)
    for name in ("anna", "ops"):  # a partner; a reader of the servers without the settings
        other = w.other_client()
        w.login(name, client=other)
        assert other.get("/api/alerts").status_code == 403, name


def test_a_reader_reads_but_does_not_act(w):
    ops = w.other_client()
    me = w.login("ops", client=ops)
    assert [s["alias"] for s in ops.get("/api/servers").json()] == ["node-c"]
    page = ops.get("/api/servers/node-c").json()
    assert page["server"]["worst"] == "crit" and [i["id"] for i in page["incidents"]] == ["P-1"]
    got = ops.get("/api/incidents/P-1").json()
    assert got["incident"]["id"] == "P-1" and got["timeline"][0]["actor_name"] == "system:monitor"
    assert [i["id"] for i in ops.get("/api/incidents", params={"status": "active"}).json()] == ["P-1"]
    for path in ("/api/incidents/P-1/ack", "/api/servers/node-c/check"):
        assert ops.post(path, headers=w.headers(me["csrf"]), json={}).status_code == 403
    assert ops.get("/api/inbox").json()["incidents"][0]["id"] == "P-1"


def test_the_query_narrows_to_a_group_over_http(w):
    w.env.call("digi-sysadmin", {"op": "monitor.push", "items": [
        {"key": f"g{n}", "kind": "open", "problem": {
            "key": f"node-{n}:disk:/", "server": f"node-{n}", "check": "disk", "subject": "/", "level": "warn",
            "max_level": "warn", "value": "90%", "text": "", "opened_at": "2026-10-14T14:00:00+03:00", "repeats": 0}}
        for n in "de"]})
    w.login()
    got = w.client.get("/api/incidents/query", params={"group": "2026-10-14T11:00:00Z|disk"}).json()
    assert sorted(i["id"] for i in got["items"]) == ["P-2", "P-3"]
    assert got["items"][0]["group"]["size"] == 2
    assert w.client.get("/api/incidents/query", params={"group": "nonsense"}).status_code == 400


def test_ack_mute_and_unmute_change_the_status_and_the_timeline(w):
    w.login()
    assert w.client.post("/api/incidents/P-1/ack", json={}).status_code == 403  # no CSRF
    assert w.client.post("/api/incidents/P-1/ack", headers=w.headers(), json={}).json()["status"] == "acknowledged"
    assert w.client.get("/api/inbox").json()["incidents"] == []
    r = w.client.post("/api/incidents/P-1/unmute", headers=w.headers(), json={})
    assert r.json()["status"] == "open"
    assert w.client.post("/api/incidents/P-1/mute", headers=w.headers(), json={"for": "2h"}).status_code == 400
    for span, until in (("1h", "2026-10-14T11:00:00Z"), ("1d", "2026-10-15T10:00:00Z"), ("7d", "2026-10-21T10:00:00Z")):
        r = w.client.post("/api/incidents/P-1/mute", headers=w.headers(), json={"for": span})
        assert r.json()["muted_until"] == until
    kinds = [e["kind"] for e in w.client.get("/api/incidents/P-1").json()["timeline"]]
    assert kinds == ["incident.opened", "incident.acknowledged", "incident.unmuted"] + ["incident.muted"] * 3
    assert w.client.get("/api/incidents/P-9").status_code == 404


def _passkey(w):
    from softauth import SoftAuthenticator

    auth = SoftAuthenticator(w.cfg.rp_id, w.cfg.origin)
    opts = w.client.post("/api/me/passkeys/options", headers=w.headers()).json()
    r = w.client.post("/api/me/passkeys", headers=w.headers(),
                      json={"credential": auth.register(opts["options"]), "name": "iPhone"})
    assert r.status_code == 200, r.text
    return auth


def test_the_owners_button_proposes_and_one_passkey_signs_it(w):
    """The "Add an address" action — a monitor_trust decision and its signature in one step-up; without a signature — nothing."""
    w.login()
    auth = _passkey(w)
    r = w.client.post("/api/monitor-trust", headers=w.headers(),
                      json={"kind": "trust", "cidr": "10.1.2.3/32", "label": "офис"})
    assert r.status_code == 200, r.text
    d = r.json()["decision"]
    assert d["card"] == "" and d["requested_by"] == "user:owner"
    item = w.client.get(f"/api/decisions/{d['id']}").json()
    assert item["view"]["kind"] == "trust" and item["view"]["cidr"] == "10.1.2.3"
    assert item["view"]["proposed_by"] == "user:owner" and item["view"]["apply"]["state"] == ""
    assert [x["decision"]["id"] for x in w.client.get("/api/decisions").json()] == [d["id"]]  # the Inbox lists it
    ch = w.client.post("/api/decisions/challenge", headers=w.headers(), json={"ids": [d["id"]]}).json()
    assert ch["step_up"] is True and ch["revisions"] == {f"decision:{d['id']}": ""}
    bad = w.client.post("/api/decisions/approve", headers=w.headers(), json={"challenge_id": ch["challenge_id"]})
    assert bad.status_code == 403  # no signature: nothing happens
    assert w.env.call("digi-sysadmin", {"op": "monitor_trust.take"})["decisions"] == []
    ch = w.client.post("/api/decisions/challenge", headers=w.headers(), json={"ids": [d["id"]]}).json()
    r = w.client.post("/api/decisions/approve", headers=w.headers(),
                      json={"challenge_id": ch["challenge_id"], "credential": auth.assert_(ch["options"])})
    assert r.status_code == 200, r.text
    assert r.json()["decisions"][0]["apply_state"] == "pending"
    assert [x["id"] for x in w.env.call("digi-sysadmin", {"op": "monitor_trust.take"})["decisions"]] == [d["id"]]
    proposals = w.client.get("/api/servers/node-c").json()["proposals"]
    assert [x["id"] for x in proposals["decisions"]] == [d["id"]]


def test_the_owner_accepts_an_incident_and_removes_an_address_from_the_pages(w):
    w.login()
    r = w.client.post("/api/monitor-trust", headers=w.headers(), json={"kind": "accept", "incident": "P-1"})
    assert r.status_code == 403 and "ack" in r.text  # disk has no baseline: it closes by ack
    r = w.client.post("/api/monitor-trust/remove", headers=w.headers(), json={"cidr": "10.1.2.3"})
    assert r.status_code == 200 and r.json()["removal"]["state"] == "queued"
    assert w.client.get("/api/servers/node-c").json()["proposals"]["removals"][0]["item"] == "10.1.2.3"
    ops = w.other_client()
    me = w.login("ops", client=ops)
    for path, body in (("/api/monitor-trust", {"kind": "baseline", "server": "node-c"}),
                       ("/api/monitor-trust/remove", {"cidr": "10.1.2.3"})):
        assert ops.post(path, headers=w.headers(me["csrf"]), json=body).status_code == 403


def test_the_batch_accept_answers_what_it_proposed_and_what_it_refused(w):
    """The "Accept into the baseline" action for many P-n — one call; a P-n without a baseline is refused, not the call."""
    w.login()
    r = w.client.post("/api/monitor-trust/batch", headers=w.headers(), json={"incidents": ["P-1"]})
    assert r.status_code == 200, r.text
    assert r.json()["decisions"] == [] and r.json()["refused"][0]["incident"] == "P-1"
    assert w.client.post("/api/monitor-trust/batch", headers=w.headers(), json={"incidents": []}).status_code == 400
    ops = w.other_client()
    me = w.login("ops", client=ops)
    assert ops.post("/api/monitor-trust/batch", headers=w.headers(me["csrf"]),
                    json={"incidents": ["P-1"]}).status_code == 403


def test_check_now_answers_the_active_request(w):
    w.login()
    first = w.client.post("/api/servers/node-c/check", headers=w.headers(), json={}).json()
    again = w.client.post("/api/servers/node-c/check", headers=w.headers(), json={}).json()
    assert first["repeat"] is False and again["repeat"] is True
    assert again["request"]["id"] == first["request"]["id"]
    assert w.client.get("/api/servers/node-c").json()["check_now"]["state"] == "queued"
    assert w.client.post("/api/servers/nope/check", headers=w.headers(), json={}).status_code == 404
