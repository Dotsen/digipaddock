"""Signing in (one-time code, password + TOTP, passkey), sessions, CSRF and the first factors."""

from softauth import SoftAuthenticator
from web_env import WebEnv
from cabinet_env import totp_codes


def _owner(tmp_path):
    w = WebEnv(tmp_path)
    w.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    return w


def _passkey(w, name="iPhone"):
    auth = SoftAuthenticator(w.cfg.rp_id, w.cfg.origin)
    opts = w.client.post("/api/me/passkeys/options", headers=w.headers()).json()
    r = w.client.post(
        "/api/me/passkeys",
        headers=w.headers(),
        json={"credential": auth.register(opts["options"]), "name": name},
    )
    assert r.status_code == 200, r.text
    return auth


def test_health_answers_without_a_session(tmp_path):
    # cabinet_setup.sh check asks it over the http socket: the web is up and its base answers
    w = WebEnv(tmp_path)
    r = w.client.get("/api/health")
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_code_login_sets_a_cookie_and_me_reports_setup_needed(tmp_path):
    w = _owner(tmp_path)
    me = w.login()
    assert (
        me["id"] == "owner" and me["must_setup"] and me["permissions"] == {"*": ["*"]}
    )
    assert "kd_session" in w.client.cookies
    assert w.client.get("/api/me").status_code == 200
    assert me["cards"] == "KD"  # the host's card prefix: the pages find card IDs in texts by it
    assert me["commands"] == "kd"  # and the host's command prefix: the catalogs' kd-<word> are its names
    assert me["card_aliases"] == ["OLD"]  # an old link /cards/OLD-5 opens KD-5


def test_me_tells_the_browser_what_it_needs(tmp_path):
    w = _owner(tmp_path)
    me = w.login()
    assert set(me) >= {
        "id",
        "name",
        "language",
        "timezone",
        "permissions",
        "projects",
        "csrf",
        "push_public_key",
        "second_factor",
        "must_setup",
    }
    assert me["push_public_key"] == w.cfg.vapid_public and me["language"] == "ru"
    assert me["projects"] == sorted(["admin", "alert", "digipaddock", "web"])
    assert me["second_factor"] == {"passkey": False, "totp": False, "password": False}


def test_the_session_cookie_is_strict_and_http_only(tmp_path):
    w = _owner(tmp_path)
    code = w.users.issue_code("owner", w.env.now)
    r = w.client.post("/api/auth/code", json={"user": "owner", "code": code})
    cookie = r.headers["set-cookie"].lower()
    for part in (
        "kd_session=",
        "httponly",
        "samesite=strict",
        "path=/",
        "max-age=2592000",
    ):
        assert part in cookie, part
    assert "secure" not in cookie  # the dev origin is http; https adds Secure


def test_wrong_code_is_401_and_the_sixth_try_is_429(tmp_path):
    w = _owner(tmp_path)
    for _ in range(5):
        assert (
            w.client.post(
                "/api/auth/code", json={"user": "owner", "code": "bad"}
            ).status_code
            == 401
        )
    assert (
        w.client.post(
            "/api/auth/code", json={"user": "owner", "code": "bad"}
        ).status_code
        == 429
    )
    assert w.client.get("/api/me").status_code == 401
    other = {"X-Forwarded-For": "203.0.113.9"}  # another address has its own count
    assert (
        w.client.post(
            "/api/auth/code", json={"user": "owner", "code": "bad"}, headers=other
        ).status_code
        == 401
    )


def test_unknown_user_and_bad_body_say_nothing_more(tmp_path):
    w = _owner(tmp_path)
    r = w.client.post("/api/auth/code", json={"user": "ghost", "code": "123456"})
    assert r.status_code == 401 and r.json() == {
        "error": "unauthorized",
        "message": "wrong code",
    }
    assert w.client.post("/api/auth/code", json=["x"]).status_code == 400
    assert (
        w.client.post(
            "/api/auth/code", content=b"{", headers={"content-type": "application/json"}
        ).status_code
        == 400
    )


def test_writes_need_csrf_and_origin(tmp_path):
    w = _owner(tmp_path)
    w.login()
    assert w.client.post("/api/auth/logout").status_code == 403
    assert (
        w.client.post(
            "/api/auth/logout",
            headers={"X-CSRF-Token": w.csrf, "Origin": "https://evil.example"},
        ).status_code
        == 403
    )
    assert w.client.post("/api/auth/logout", headers=w.headers()).status_code == 200
    assert w.client.get("/api/me").status_code == 401


def test_sign_in_needs_the_pages_origin(tmp_path):
    """Global review 15.10.2026: a cross-site form must not sign the browser into an account it holds a code for."""
    from fastapi.testclient import TestClient

    w = _owner(tmp_path)
    code = w.users.issue_code("owner", w.env.now)
    foreign = TestClient(w.client.app, base_url=w.cfg.origin)  # no Origin at all
    assert foreign.post("/api/auth/code", json={"user": "owner", "code": code}).status_code == 403
    r = w.client.post(
        "/api/auth/code", json={"user": "owner", "code": code}, headers={"Origin": "https://evil.example"}
    )
    assert r.status_code == 403
    for path, body in (
        ("/api/auth/password", {"user": "owner", "password": "x" * 12}),
        ("/api/auth/passkey", {"login_id": "x"}),
    ):
        assert foreign.post(path, json=body).status_code == 403, path
    assert w.client.post("/api/auth/code", json={"user": "owner", "code": code}).status_code == 200


def test_logout_all_closes_every_session_of_the_person(tmp_path):
    w = _owner(tmp_path)
    phone = w.other_client()
    w.login(client=phone)
    w.login()
    assert w.client.post("/api/auth/logout-all", headers=w.headers()).status_code == 200
    assert (
        phone.get("/api/me").status_code == 401
        and w.client.get("/api/me").status_code == 401
    )


def test_passkey_registration_then_discoverable_login(tmp_path):
    w = _owner(tmp_path)
    w.login()
    auth = _passkey(w)
    assert not w.client.get("/api/me").json()["must_setup"]
    w.client.post("/api/auth/logout", headers=w.headers())
    opts = w.client.get("/api/auth/passkey/options").json()
    r = w.client.post(
        "/api/auth/passkey",
        json={
            "login_id": opts["login_id"],
            "credential": auth.assert_(opts["options"]),
        },
    )
    assert r.status_code == 200 and r.json()["id"] == "owner"
    r = w.client.post(
        "/api/auth/passkey",
        json={
            "login_id": opts["login_id"],
            "credential": auth.assert_(opts["options"]),
        },
    )
    assert r.status_code == 401  # a challenge is single use


def test_a_passkey_nobody_registered_does_not_sign_in(tmp_path):
    w = _owner(tmp_path)
    stranger = SoftAuthenticator(w.cfg.rp_id, w.cfg.origin)
    stranger.user_handle = b"x" * 16
    opts = w.client.get("/api/auth/passkey/options").json()
    r = w.client.post(
        "/api/auth/passkey",
        json={
            "login_id": opts["login_id"],
            "credential": stranger.assert_(opts["options"]),
        },
    )
    assert r.status_code == 401


def test_a_second_passkey_needs_a_step_up_with_the_first(tmp_path):
    w = _owner(tmp_path)
    w.login()
    first = _passkey(w)
    second = SoftAuthenticator(w.cfg.rp_id, w.cfg.origin)
    opts = w.client.post("/api/me/passkeys/options", headers=w.headers()).json()
    r = w.client.post(
        "/api/me/passkeys",
        headers=w.headers(),
        json={"credential": second.register(opts["options"])},
    )
    assert (
        r.status_code == 403 and r.json()["error"] == "stepup_required"
    )  # a stolen session adds no key
    step = w.client.post(
        "/api/stepup/challenge", headers=w.headers(), json={"purpose": "passkey.add"}
    ).json()
    proof = {
        "challenge_id": step["challenge_id"],
        "credential": first.assert_(step["options"]),
    }
    opts = w.client.post("/api/me/passkeys/options", headers=w.headers()).json()
    r = w.client.post(
        "/api/me/passkeys",
        headers=w.headers(),
        json={
            "credential": second.register(opts["options"]),
            "name": "Mac",
            "stepup": proof,
        },
    )
    assert r.status_code == 200, r.text
    assert sorted(p["name"] for p in r.json()["passkeys"]) == ["Mac", "iPhone"]
    step = w.client.post(
        "/api/stepup/challenge", headers=w.headers(), json={"purpose": "totp.setup"}
    ).json()
    proof = {
        "challenge_id": step["challenge_id"],
        "credential": first.assert_(step["options"]),
    }
    r = w.client.post(
        "/api/me/password",
        headers=w.headers(),
        json={"password": "x" * 12, "stepup": proof},
    )
    assert r.status_code == 403  # a proof for another purpose
    step = w.client.post(
        "/api/stepup/challenge", headers=w.headers(), json={"purpose": "password.set"}
    ).json()
    proof = {
        "challenge_id": step["challenge_id"],
        "credential": first.assert_(step["options"]),
    }
    r = w.client.post(
        "/api/me/password",
        headers=w.headers(),
        json={"password": "x" * 12, "stepup": proof},
    )
    assert r.status_code == 200 and r.json()["second_factor"]["password"]
    r = w.client.post(
        "/api/me/password",
        headers=w.headers(),
        json={"password": "y" * 12, "stepup": proof},
    )
    assert r.status_code == 403  # one proof, one change


def test_password_login_requires_totp_once_enabled(tmp_path):
    w = _owner(tmp_path)
    w.login()
    w.client.post(
        "/api/me/password",
        headers=w.headers(),
        json={"password": "correct horse battery"},
    )
    b32 = w.client.post("/api/me/totp/setup", headers=w.headers()).json()["secret"]
    codes = totp_codes(b32)
    r = w.client.post(
        "/api/me/totp/confirm",
        headers=w.headers(),
        json={"code": codes[0]},
    )
    assert r.status_code == 200
    w.client.post("/api/auth/logout", headers=w.headers())
    r = w.client.post(
        "/api/auth/password",
        json={"user": "owner", "password": "correct horse battery"},
    )
    assert r.status_code == 401
    r = w.client.post(
        "/api/auth/password",
        json={
            "user": "owner",
            "password": "correct horse battery",
            "totp": codes[1],
        },
    )
    assert r.status_code == 200
    r = w.client.post(
        "/api/me/totp/setup",
        headers={"X-CSRF-Token": r.json()["csrf"], "Origin": w.cfg.origin},
    )
    assert (
        r.status_code == 403
    )  # TOTP is a signing factor now: replacing it needs a step-up
    # global review 15.10.2026: a code seen once is refused for the rest of its window; the next step signs in
    for code, status in ((codes[1], 401), (codes[0], 401), (codes[2], 200)):
        r = w.client.post(
            "/api/auth/password",
            json={"user": "owner", "password": "correct horse battery", "totp": code},
        )
        assert r.status_code == status, code


def test_password_without_it_set_or_wrong_is_401(tmp_path):
    w = _owner(tmp_path)
    assert (
        w.client.post(
            "/api/auth/password", json={"user": "owner", "password": "x" * 12}
        ).status_code
        == 401
    )
    w.login()
    w.client.post(
        "/api/me/password",
        headers=w.headers(),
        json={"password": "correct horse battery"},
    )
    w.client.post("/api/auth/logout", headers=w.headers())
    assert (
        w.client.post(
            "/api/auth/password", json={"user": "owner", "password": "wrong horse"}
        ).status_code
        == 401
    )
    r = w.client.post(
        "/api/auth/password",
        json={"user": "owner", "password": "correct horse battery"},
    )
    # no TOTP enabled: a password alone never signs in (spec 6: passkey, or password + TOTP),
    # and the answer is the wrong password's, so it tells nothing about the account
    assert r.status_code == 401
    assert r.json() == (
        w.client.post(
            "/api/auth/password", json={"user": "owner", "password": "wrong horse"}
        ).json()
    )


def test_a_blocked_person_loses_the_session(tmp_path):
    w = _owner(tmp_path)
    w.login()
    w.users.set_status("owner", "blocked", actor="root")
    assert w.client.get("/api/me").status_code == 401


def test_totp_step_up_and_its_lockout(tmp_path):
    w = _owner(tmp_path)
    w.login()
    b32 = w.client.post("/api/me/totp/setup", headers=w.headers()).json()["secret"]
    codes = totp_codes(b32)
    w.client.post(
        "/api/me/totp/confirm",
        headers=w.headers(),
        json={"code": codes[0]},
    )

    def proof(code):
        step = w.client.post(
            "/api/stepup/challenge",
            headers=w.headers(),
            json={"purpose": "password.set"},
        ).json()
        assert (
            step["options"] is None and step["totp"] is True
        )  # no passkey yet: TOTP only
        return {"challenge_id": step["challenge_id"], "totp": code}

    ok = w.client.post(
        "/api/me/password",
        headers=w.headers(),
        json={"password": "z" * 12, "stepup": proof(codes[1])},
    )
    assert ok.status_code == 200
    for _ in range(5):
        r = w.client.post(
            "/api/me/password",
            headers=w.headers(),
            json={"password": "z" * 12, "stepup": proof("000000")},
        )
        assert r.status_code == 403
    r = w.client.post(
        "/api/me/password",
        headers=w.headers(),
        json={"password": "z" * 12, "stepup": proof(codes[2])},
    )
    assert r.status_code == 429  # even the right code waits now


def test_a_person_without_a_signing_factor_gets_no_step_up(tmp_path):
    w = _owner(tmp_path)
    w.login()
    r = w.client.post(
        "/api/stepup/challenge", headers=w.headers(), json={"purpose": "users.manage"}
    )
    assert r.status_code == 403 and r.json()["error"] == "setup_required"
    r = w.client.post(
        "/api/stepup/challenge", headers=w.headers(), json={"purpose": "everything"}
    )
    assert r.status_code == 400


def test_dev_mode_serves_the_built_pwa_with_a_fallback(tmp_path):
    from fastapi.testclient import TestClient

    from kd.cabinet.web import app as web_app

    w = WebEnv(tmp_path)
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><title>cab</title>", encoding="utf-8"
    )
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("no", encoding="utf-8")
    client = TestClient(web_app.create_app(w.svc, dist=dist), base_url=w.cfg.origin)
    assert client.get("/assets/app.js").text == "console.log(1)"
    assert "<title>cab</title>" in client.get("/cards/KD-1").text
    assert (
        "<title>cab</title>" in client.get("/%2e%2e/secret.txt").text
    )  # never a file above dist
    r = client.get("/api/nothing")
    assert r.status_code == 404 and r.json()["error"] == "not_found"


def test_challenges_are_single_use_and_expire():
    from kd.cabinet import service

    now = [0.0]
    challenges = service.Challenges(ttl=300, clock=lambda: now[0])
    challenges.put("login", "a", b"x")
    challenges.put("login", "b", b"y")
    assert challenges.pop("login", "a") == b"x" and challenges.pop("login", "a") is None
    now[0] = 301
    assert challenges.pop("login", "b") is None


def test_challenges_with_a_limit_evict_the_oldest():
    from kd.cabinet import service

    challenges = service.Challenges(limit=3, clock=lambda: 0.0)
    for n in range(5):
        challenges.put("login", str(n), n)
    assert len(challenges._items) == 3
    assert [challenges.pop("login", str(n)) for n in range(5)] == [None, None, 2, 3, 4]


def test_passkey_options_without_a_login_keep_a_bounded_store(tmp_path, monkeypatch):
    from kd.cabinet import service

    monkeypatch.setattr(service, "LOGIN_CHALLENGES_MAX", 4)
    w = _owner(tmp_path)
    ids = [w.client.get("/api/auth/passkey/options").json()["login_id"] for _ in range(6)]
    assert len(w.svc.login_challenges._items) == 4
    assert w.svc.challenges._items == {}  # the signed-in people's challenges are not in it
    assert [w.svc.login_challenges.pop("login", i) is not None for i in ids] == [False, False, True, True, True, True]


def test_the_second_factor_of_a_password_login_is_limited_per_account(tmp_path):
    # review 04.11.2026: misses were counted per (user, address) only — a known password and many addresses could try
    # every code; now they count for the account, as step-up's, from every address
    w = _owner(tmp_path)
    w.login()
    w.client.post("/api/me/password", headers=w.headers(), json={"password": "correct horse battery"})
    b32 = w.client.post("/api/me/totp/setup", headers=w.headers()).json()["secret"]
    codes = totp_codes(b32)
    assert w.client.post("/api/me/totp/confirm", headers=w.headers(), json={"code": codes[0]}).status_code == 200
    w.client.post("/api/auth/logout", headers=w.headers())
    for n in range(5):  # five addresses, one wrong code each
        r = w.client.post("/api/auth/password", headers={"X-Forwarded-For": f"198.51.100.{n}"},
                          json={"user": "owner", "password": "correct horse battery", "totp": "000000"})
        assert r.status_code == 401
    r = w.client.post("/api/auth/password", headers={"X-Forwarded-For": "198.51.100.99"},
                      json={"user": "owner", "password": "correct horse battery", "totp": codes[1]})
    assert r.status_code == 401  # even the right code: the account's second factor waits out the window
    # a wrong password is the address's miss, not the account's: nobody locks the owner out without the password
    assert w.users.too_many("totp|owner", w.svc.now())


def test_the_second_factor_lock_does_not_tell_a_right_password_from_a_wrong_one(tmp_path):
    # the lock is checked after the password and answers as a wrong password does: no 429 that only a right
    # password reaches
    w = _owner(tmp_path)
    w.login()
    w.client.post("/api/me/password", headers=w.headers(), json={"password": "correct horse battery"})
    b32 = w.client.post("/api/me/totp/setup", headers=w.headers()).json()["secret"]
    codes = totp_codes(b32)
    assert w.client.post("/api/me/totp/confirm", headers=w.headers(), json={"code": codes[0]}).status_code == 200
    w.client.post("/api/auth/logout", headers=w.headers())
    for _ in range(5):
        w.users.failed_attempt("totp|owner", w.svc.now())
    answers = []
    for n, password in enumerate(("correct horse battery", "wrong password here")):
        r = w.client.post("/api/auth/password", headers={"X-Forwarded-For": f"198.51.100.{n}"},
                          json={"user": "owner", "password": password, "totp": codes[1]})
        answers.append((r.status_code, r.json()))
    assert answers[0] == answers[1] and answers[0][0] == 401


def test_an_unknown_user_costs_the_same_argon2_work(tmp_path, monkeypatch):
    from argon2 import PasswordHasher

    w = _owner(tmp_path)
    seen = []
    real = PasswordHasher.verify
    monkeypatch.setattr(PasswordHasher, "verify", lambda self, h, p: seen.append(h) or real(self, h, p))
    assert w.client.post("/api/auth/password", json={"user": "ghost", "password": "x" * 12}).status_code == 401
    assert len(seen) == 1  # verified against a dummy hash: the time does not say who has a password
