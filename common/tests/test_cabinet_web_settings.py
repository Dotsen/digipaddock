"""The person's settings (profile, passkeys, sessions, notifications, push) and the
owner's (people, roles), each sensitive change behind a step-up."""

import pytest
from softauth import SoftAuthenticator
from web_env import WebEnv


@pytest.fixture
def w(tmp_path):
    w = WebEnv(tmp_path)
    w.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    w.users.create("anna", "Анна", roles=[("partner", ["web"])], actor="root")
    return w


def _passkey(w, name="iPhone"):
    auth = SoftAuthenticator(w.cfg.rp_id, w.cfg.origin)
    opts = w.client.post("/api/me/passkeys/options", headers=w.headers()).json()
    body = {"credential": auth.register(opts["options"]), "name": name}
    if w.users.signs("owner"):
        body["stepup"] = _proof(w, auth_of(w), "passkey.add")
    r = w.client.post("/api/me/passkeys", headers=w.headers(), json=body)
    assert r.status_code == 200, r.text
    w.authenticators.append(auth)
    return auth


def auth_of(w):
    return w.authenticators[0]


def _proof(w, auth, purpose):
    step = w.client.post(
        "/api/stepup/challenge", headers=w.headers(), json={"purpose": purpose}
    ).json()
    return {
        "challenge_id": step["challenge_id"],
        "credential": auth.assert_(step["options"]),
    }


@pytest.fixture
def owner(w):
    w.login()
    w.authenticators = []
    _passkey(w)
    return w


def test_me_says_whether_the_host_runs_the_memory_service(w, tmp_path, monkeypatch):
    # the Hindsight env (root's file, its directory open to read) — the same mark the plugin's install
    # takes
    from kd import hindsight_status

    env = tmp_path / "coding-agent.env"
    monkeypatch.setattr(hindsight_status, "ENV", env)
    w.login()
    assert w.client.get("/api/me").json()["memory"] is False
    env.write_text("KEY=x\n")
    assert w.client.get("/api/me").json()["memory"] is True


def test_profile_changes_and_bad_values_are_refused(owner):
    w = owner
    r = w.client.patch(
        "/api/me",
        headers=w.headers(),
        json={"name": "Константин", "language": "en", "timezone": "Asia/Dubai"},
    )
    assert r.status_code == 200, r.text
    me = w.client.get("/api/me").json()
    assert (me["name"], me["language"], me["timezone"]) == (
        "Константин",
        "en",
        "Asia/Dubai",
    )
    assert (
        w.client.patch(
            "/api/me", headers=w.headers(), json={"timezone": "Mars/Base"}
        ).status_code
        == 400
    )


def test_notification_preferences_round_trip(owner):
    w = owner
    got = w.client.get("/api/me/notifications").json()
    assert got["kinds"]["decision.waiting"] == "now" and got["quiet"] == ""
    r = w.client.put(
        "/api/me/notifications",
        headers=w.headers(),
        json={"kinds": {"card.done": "feed"}, "quiet": "22:00-08:00"},
    )
    assert r.status_code == 200, r.text
    got = w.client.get("/api/me/notifications").json()
    assert got["kinds"]["card.done"] == "feed" and got["quiet"] == "22:00-08:00"
    bad = w.client.put(
        "/api/me/notifications", headers=w.headers(), json={"kinds": {"x": "now"}}
    )
    assert bad.status_code == 400


def test_the_last_passkey_stays_without_a_password_and_totp(owner):
    w = owner
    keys = w.client.get("/api/me/passkeys").json()
    assert [k["name"] for k in keys] == ["iPhone"]
    r = w.client.request(
        "DELETE",
        f"/api/me/passkeys/{keys[0]['id']}",
        headers=w.headers(),
        json={"stepup": _proof(w, auth_of(w), "passkey.remove")},
    )
    assert r.status_code == 409 and r.json()["error"] == "conflict"
    _passkey(w, "Ноутбук")
    keys = w.client.get("/api/me/passkeys").json()
    no_proof = w.client.request(
        "DELETE", f"/api/me/passkeys/{keys[1]['id']}", headers=w.headers(), json={}
    )
    assert no_proof.status_code == 403 and no_proof.json()["error"] == "stepup_required"
    r = w.client.request(
        "DELETE",
        f"/api/me/passkeys/{keys[1]['id']}",
        headers=w.headers(),
        json={"stepup": _proof(w, auth_of(w), "passkey.remove")},
    )
    assert r.status_code == 200, r.text
    assert [k["name"] for k in r.json()] == ["iPhone"]


def test_sessions_list_and_close_another_one(owner):
    w = owner
    phone = w.other_client()
    w.login(client=phone)
    sessions = w.client.get("/api/me/sessions").json()
    assert len(sessions) == 2 and sum(s["current"] for s in sessions) == 1
    other = next(s for s in sessions if not s["current"])
    r = w.client.delete(f"/api/me/sessions/{other['id']}", headers=w.headers())
    assert r.status_code == 200
    assert phone.get("/api/me").status_code == 401
    assert w.client.get("/api/me").status_code == 200
    assert (
        w.client.delete(
            f"/api/me/sessions/{other['id']}", headers=w.headers()
        ).status_code
        == 404
    )


def test_people_are_managed_behind_a_step_up(owner):
    w = owner
    body = {
        "id": "boris",
        "name": "Борис",
        "roles": [{"role": "viewer", "projects": ["web"]}],
    }
    r = w.client.post("/api/users", headers=w.headers(), json=body)
    assert r.status_code == 403 and r.json()["error"] == "stepup_required"
    r = w.client.post(
        "/api/users",
        headers=w.headers(),
        json={**body, "stepup": _proof(w, auth_of(w), "users.manage")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["user"]["id"] == "boris" and len(r.json()["code"]) >= 8
    listed = {u["id"]: u for u in w.client.get("/api/users").json()["users"]}
    assert listed["boris"]["roles"] == [["viewer", ["web"]]]
    anna = w.other_client()
    w.login("anna", client=anna)
    r = w.client.patch(
        "/api/users/anna",
        headers=w.headers(),
        json={"status": "blocked", "stepup": _proof(w, auth_of(w), "users.manage")},
    )
    assert r.status_code == 200 and r.json()["status"] == "blocked"
    assert anna.get("/api/me").status_code == 401
    r = w.client.post(
        "/api/users/boris/code",
        headers=w.headers(),
        json={"stepup": _proof(w, auth_of(w), "users.manage")},
    )
    assert r.status_code == 200 and r.json()["code"]
    # a proof for another purpose does not do
    r = w.client.post(
        "/api/users/boris/code",
        headers=w.headers(),
        json={"stepup": _proof(w, auth_of(w), "roles.set")},
    )
    assert r.status_code == 403


def test_a_signed_in_person_takes_a_code_for_a_new_device(owner):
    # The code is a new sign-in, so a session alone does not get one
    w = owner
    r = w.client.post("/api/me/code", headers=w.headers(), json={})
    assert r.status_code == 403 and r.json()["error"] == "stepup_required"
    wrong = {"stepup": _proof(w, auth_of(w), "users.manage")}
    assert w.client.post("/api/me/code", headers=w.headers(), json=wrong).status_code == 403
    r = w.client.post(
        "/api/me/code",
        headers=w.headers(),
        json={"stepup": _proof(w, auth_of(w), "code.self")},
    )
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["user"] == "owner" and got["expires"] == "2026-10-14T10:15:00Z"
    phone = w.other_client()
    signed = phone.post("/api/auth/code", json={"user": "owner", "code": got["code"]})
    assert signed.status_code == 200 and signed.json()["id"] == "owner"
    event = [e for e in w.env.store.events_since(0) if e["kind"] == "user.code_issued"][-1]
    assert (event["actor"], event["payload"]["user"]) == ("user:owner", "owner")


def test_a_code_for_a_new_device_needs_a_signing_factor(w):
    w.login("anna")
    r = w.client.post("/api/me/code", headers=w.headers(), json={})
    assert r.status_code == 403 and r.json()["error"] == "setup_required"


def test_a_password_session_takes_its_code_with_totp(w):
    # AC2: signed in by password + TOTP (no passkey) — TOTP confirms the code
    from cabinet_env import totp_codes

    w.login("anna")
    b32 = w.client.post("/api/me/totp/setup", headers=w.headers()).json()["secret"]
    codes = totp_codes(b32)
    w.client.post("/api/me/totp/confirm", headers=w.headers(), json={"code": codes[0]})
    step = w.client.post(
        "/api/stepup/challenge", headers=w.headers(), json={"purpose": "code.self"}
    ).json()
    assert step["options"] is None and step["totp"] is True
    r = w.client.post(
        "/api/me/code",
        headers=w.headers(),
        json={"stepup": {"challenge_id": step["challenge_id"], "totp": codes[1]}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["user"] == "anna" and r.json()["code"]


def test_the_owner_gives_a_partner_a_code_with_its_expiry(owner):
    # "Users and roles" → "New code"; the event names who gave it to whom
    w = owner
    r = w.client.post(
        "/api/users/anna/code",
        headers=w.headers(),
        json={"stepup": _proof(w, auth_of(w), "users.manage")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["user"] == "anna" and r.json()["expires"] == "2026-10-14T10:15:00Z"
    event = [e for e in w.env.store.events_since(0) if e["kind"] == "user.code_issued"][-1]
    assert (event["actor"], event["payload"]["user"]) == ("user:owner", "anna")


def test_a_person_without_a_factor_cannot_manage_people(w):
    w.login()
    r = w.client.post(
        "/api/users",
        headers=w.headers(),
        json={
            "id": "boris",
            "name": "Борис",
            "roles": [{"role": "viewer", "projects": ["web"]}],
        },
    )
    assert r.status_code == 403 and r.json()["error"] == "setup_required"


def test_roles_need_settings(owner):
    w = owner
    roles = w.client.get("/api/roles").json()
    assert (
        roles["roles"]["viewer"] == ["cards.read"]
        and "settings" in roles["permissions"]
    )
    r = w.client.put(
        "/api/roles/reviewer",
        headers=w.headers(),
        json={
            "perms": ["cards.read", "cards.comment"],
            "stepup": _proof(w, auth_of(w), "roles.set"),
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["roles"]["reviewer"] == ["cards.comment", "cards.read"]
    # global review 15.10.2026: the owner's role, and any role the caller holds, is never rewritten from the web
    r = w.client.put(
        "/api/roles/owner",
        headers=w.headers(),
        json={"perms": ["cards.read"], "stepup": _proof(w, auth_of(w), "roles.set")},
    )
    assert r.status_code == 403 and w.client.get("/api/roles").json()["roles"]["owner"] != ["cards.read"]
    anna = w.other_client()
    headers = w.headers(w.login("anna", client=anna)["csrf"])
    assert anna.get("/api/roles").status_code == 403
    assert anna.get("/api/users").status_code == 403
    assert (
        anna.put(
            "/api/roles/viewer", headers=headers, json={"perms": ["*"]}
        ).status_code
        == 403
    )


def test_projects_are_managed_with_settings_and_a_delete_behind_a_step_up(owner):
    """create and reroute with ``settings``; a delete moves the cards and needs a passkey or TOTP."""
    w = owner
    r = w.client.post("/api/projects", headers=w.headers(), json={"name": "Сайт", "route": "@dev"})
    assert r.status_code == 200, r.text
    assert r.json() == {"id": "sayt", "name": "Сайт", "route": "@dev", "repo": "", "repo_state": "",
                        "repo_detail": "", "repo_updated": "", "repo_release": False, "memory_bank": "",
                        "done_by": "", "model": "", "effort": "", "council": {}}
    assert w.client.post("/api/projects", headers=w.headers(), json={"name": "Сайт", "route": "@dev"}).status_code == 400
    assert w.client.post("/api/projects", headers=w.headers(), json={"name": "x", "route": "@ghost"}).status_code == 400
    r = w.client.patch("/api/projects/sayt", headers=w.headers(), json={"route": "@owner"})
    assert r.status_code == 200 and r.json()["route"] == "@owner"
    # the project's memory bank, with settings only (a partner's list below has no such key)
    r = w.client.patch("/api/projects/sayt", headers=w.headers(), json={"memory_bank": "coding-agent::site"})
    assert r.status_code == 200 and r.json()["memory_bank"] == "coding-agent::site"
    assert w.client.patch("/api/projects/sayt", headers=w.headers(), json={"memory_bank": "a/b"}).status_code == 400
    # which agent closes the project's cards Done itself ("" — the owner accepts)
    r = w.client.patch("/api/projects/sayt", headers=w.headers(), json={"done_by": "dev"})
    assert r.status_code == 200 and r.json()["done_by"] == "dev"
    assert w.client.patch("/api/projects/sayt", headers=w.headers(), json={"done_by": "ghost"}).status_code == 400
    assert w.client.patch("/api/projects/sayt", headers=w.headers(), json={"id": "site"}).status_code == 400
    card = w.client.post("/api/cards", headers=w.headers(), json={"title": "Форма", "project": "sayt"}).json()
    no_proof = w.client.request("DELETE", "/api/projects/sayt", headers=w.headers(), json={"target": "web"})
    assert no_proof.status_code == 403 and no_proof.json()["error"] == "stepup_required"
    wrong = {"target": "web", "stepup": _proof(w, auth_of(w), "users.manage")}
    assert w.client.request("DELETE", "/api/projects/sayt", headers=w.headers(), json=wrong).status_code == 403
    r = w.client.request("DELETE", "/api/projects/sayt", headers=w.headers(),
                         json={"target": "web", "stepup": _proof(w, auth_of(w), "projects.delete")})
    assert r.status_code == 200 and r.json()["moved"] == 1, r.text
    assert w.client.get(f"/api/cards/{card['id']}").json()["project"] == "web"
    assert "sayt" not in [p["id"] for p in w.client.get("/api/projects").json()]
    anna = w.other_client()
    headers = w.headers(w.login("anna", client=anna)["csrf"])
    assert anna.post("/api/projects", headers=headers, json={"name": "Её", "route": "@dev"}).status_code == 403
    assert anna.request("DELETE", "/api/projects/web", headers=headers, json={}).status_code == 403


def test_the_projects_section_reads_routes_and_a_delete_impact(owner):
    """the form's routes and the delete dialog's cards and people, both behind ``settings``."""
    w = owner
    routes = w.client.get("/api/projects/routes").json()
    assert routes[0] == "@owner" and "@dev" in routes and "@boss" not in routes
    w.client.post("/api/cards", headers=w.headers(), json={"title": "Форма", "project": "web"})
    impact = w.client.get("/api/projects/web/impact").json()
    assert impact == {"id": "web", "cards": 1, "users": [{"id": "anna", "name": "Анна"}], "bound": False}
    assert w.client.get("/api/projects/nope/impact").status_code == 404
    anna = w.other_client()
    w.login("anna", client=anna)
    assert anna.get("/api/projects/routes").status_code == 403
    assert anna.get("/api/projects/web/impact").status_code == 403


def test_a_repository_is_asked_for_and_unbound_with_settings(owner):
    """a bad owner/repo is refused before any request; the request and its state show in the list for the
    owner only; an unbind of a request that never bound needs no unit."""
    w = owner
    bad = w.client.post("/api/projects/web/repo", headers=w.headers(), json={"repo": "o/.claude"})
    assert bad.status_code == 400
    r = w.client.post("/api/projects/web/repo", headers=w.headers(), json={"repo": "example/Site"})
    assert r.status_code == 200, r.text
    assert (r.json()["repo"], r.json()["repo_state"]) == ("example/Site", "pending")
    again = w.client.post("/api/projects/web/repo", headers=w.headers(), json={"repo": "example/Site"})
    assert again.status_code == 403  # a request is waiting
    listed = {p["id"]: p for p in w.client.get("/api/projects").json()}
    assert listed["web"]["repo_state"] == "pending"
    w.env.store.set_project_repo("web", actor="root", repo="example/Site", state="no_access", detail="clone")
    r = w.client.request("DELETE", "/api/projects/web/repo", headers=w.headers())
    assert r.status_code == 200 and (r.json()["repo"], r.json()["repo_state"]) == ("", "")
    anna = w.other_client()
    headers = w.headers(w.login("anna", client=anna)["csrf"])
    assert anna.post("/api/projects/web/repo", headers=headers, json={"repo": "o/r"}).status_code == 403
    assert anna.request("DELETE", "/api/projects/web/repo", headers=headers).status_code == 403
    mine = anna.get("/api/projects").json()
    assert mine == [{"id": "web", "name": mine[0]["name"], "route": mine[0]["route"], "repo": "",
                     "council": {}}]  # no states; the council is everyone's to read


def test_incident_push_test_uses_the_current_person_and_requires_servers_read(owner):
    w = owner
    result = w.env.call("digi-sysadmin", {"op":"monitor.push", "items":[{"key":"test-push-incident", "kind":"open",
        "problem":{"key":"node-c:units:certbot.service", "server":"node-c", "check":"units", "subject":"certbot.service",
                   "level":"warn", "value":"failed", "opened_at":w.env.now}}]})
    pid = f"P-{result['results'][0]['incident']}"
    sent = w.client.post(f"/api/push/test?incident={pid}", headers=w.headers())
    assert sent.status_code == 200, sent.text
    row = w.env.store.notification(sent.json()["notification"])
    assert row["user"] == "owner" and row["url"] == f"/incidents/{pid}"
    w.login("anna")
    denied = w.client.post(f"/api/push/test?incident={pid}", headers=w.headers())
    assert denied.status_code == 403


def test_push_subscriptions_and_a_test_push(owner):
    w = owner
    sub = {
        "endpoint": "https://web.push.apple.com/QGhJ-abc",
        "keys": {"p256dh": "BPk" + "a" * 60, "auth": "c" * 22},
    }
    r = w.client.post(
        "/api/push/subscribe",
        headers=w.headers(),
        json={"subscription": sub, "device": "iPhone"},
    )
    assert r.status_code == 200, r.text
    assert [s["endpoint"] for s in r.json()] == [sub["endpoint"]]
    # global review 15.10.2026: the page asks whether the device's subscription is this person's
    state = lambda endpoint: w.client.post("/api/push/state", headers=w.headers(), json={"endpoint": endpoint})  # noqa: E731
    assert state(sub["endpoint"]).json() == {"mine": True, "last": None}  # no delivery yet
    assert state("https://web.push.apple.com/someone-else").json() == {"mine": False, "last": None}
    assert state("not a url").status_code == 400
    r = w.client.post("/api/push/test", headers=w.headers())
    assert r.status_code == 200
    rows = w.env.store.notifications("owner")
    assert rows and rows[-1]["kind"] == "test" and rows[-1]["title"]
    # Invalid or unreadable incident targets cannot be put into a person's test push.
    r = w.client.post("/api/push/test?incident=https://evil.example/x", headers=w.headers())
    assert r.status_code == 400
    r = w.client.post(
        "/api/push/unsubscribe", headers=w.headers(), json={"endpoint": sub["endpoint"]}
    )
    assert r.status_code == 200 and r.json() == []
    # signing out everywhere drops the subscriptions with the sessions
    w.client.post("/api/push/subscribe", headers=w.headers(), json={"subscription": sub, "device": "iPhone"})
    assert w.env.store.subscriptions("owner")
    assert w.client.post("/api/auth/logout-all", headers=w.headers()).status_code == 200
    assert w.env.store.subscriptions("owner") == []
