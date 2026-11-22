import json

import pytest

from kd.cabinet import perms, users
from cabinet_env import CabinetEnv, totp_codes

SECRET = b"k" * 32


def _users(tmp_path):
    env = CabinetEnv(tmp_path)
    return env, users.Users(env.store)


def _registered(seed: bytes):
    from kd.cabinet import passkeys

    return passkeys.Registered(credential_id=seed * 4, public_key=seed * 20, sign_count=0, transports=[])


def test_set_signing_by_prefix_flips_only_that_key(tmp_path):
    """root turns one passkey into a signing key or back, naming it by a prefix of its base64url id;
    the event names the key, never its material."""
    import base64

    env, u = _users(tmp_path)
    u.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    a = u.add_credential("owner", _registered(b"aaaa-1"), "login", env.now)
    b = u.add_credential("owner", _registered(b"bbbb-2"), "sign", env.now)
    prefix = base64.urlsafe_b64encode(b"aaaa-1" * 4).rstrip(b"=").decode("ascii")[:8]
    row = u.set_signing("owner", prefix, False)
    assert row["id"] == a["id"] and row["signing_enabled"] is False and row["name"] == "login"
    assert row["credential_id"].startswith(prefix) and "public_key" not in row
    assert [c["signing_enabled"] for c in u.credentials("owner")] == [False, True]
    assert u.credential_ids("owner", signing=True) == [b"bbbb-2" * 4]
    [event] = [e for e in env.store.events_since(0) if e["kind"] == "user.passkey_signing"]
    assert event["payload"] == {"user": "owner", "credential": a["id"], "enabled": False}
    assert u.set_signing("owner", prefix, True)["signing_enabled"] is True
    assert u.credential_ids("owner", signing=True) == [b"aaaa-1" * 4, b"bbbb-2" * 4]
    with pytest.raises(ValueError):
        u.set_signing("owner", "", False)  # empty: no key named
    with pytest.raises(ValueError):
        u.set_signing("owner", "zzzzzzzz", False)  # none
    with pytest.raises(ValueError, match="at least 8"):
        u.set_signing("owner", prefix[:7], False)  # a short prefix names no key, even a unique one
    with pytest.raises(ValueError, match="at least 8"):
        u.set_signing("owner", prefix[:1], False)
    with pytest.raises(ValueError):
        u.set_signing("anna", prefix, False)  # another person's key is not found under this one
    assert b["id"] and [c["signing_enabled"] for c in u.credentials("owner")] == [True, True]


def test_owner_sees_everything_partner_only_own_projects(tmp_path):
    env, u = _users(tmp_path)
    u.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    u.create("anna", "Анна", roles=[("partner", ["web"])], actor="user:owner")
    assert u.can("owner", "decide.admin_plan", "admin") and u.can(
        "owner", "users.manage"
    )
    assert u.can("anna", "cards.read", "web") and not u.can(
        "anna", "cards.read", "digipaddock"
    )
    assert not u.can("anna", "cards.transition", "web") and not u.can(
        "anna", "users.manage"
    )
    assert u.visible_projects("anna", {"digipaddock", "web", "admin"}) == {"web"}
    assert u.visible_projects("owner", {"digipaddock", "web"}) == {"digipaddock", "web"}


def test_login_code_is_single_use_and_expires(tmp_path):
    env, u = _users(tmp_path)
    u.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    code = u.issue_code("owner", env.now)
    assert len(code) >= 10
    assert not u.redeem_code("owner", code + "x", env.now)
    assert not u.redeem_code("anna", code, env.now)
    assert u.redeem_code("owner", code, env.now)
    assert not u.redeem_code("owner", code, env.now)
    late = u.issue_code("owner", env.now)
    assert not u.redeem_code("owner", late, "2026-10-14T10:16:00Z")


def test_a_code_is_an_event_of_who_gave_it_to_whom_without_the_code(tmp_path):
    # the feed tells who issued a code to whom and until when; the code itself is never there
    env, u = _users(tmp_path)
    u.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    code = u.issue_code("owner", env.now, actor="user:owner")
    event = env.store.events_since(0)[-1]
    assert (event["kind"], event["actor"]) == ("user.code_issued", "user:owner")
    assert event["payload"] == {"user": "owner", "expires": "2026-10-14T10:15:00Z"}
    assert code not in json.dumps(event)


def test_a_new_code_cancels_the_previous_one(tmp_path):
    env, u = _users(tmp_path)
    u.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    first = u.issue_code("owner", env.now)
    second = u.issue_code("owner", env.now)
    assert not u.redeem_code("owner", first, env.now) and u.redeem_code(
        "owner", second, env.now
    )


def test_password_totp_and_sessions(tmp_path):
    env, u = _users(tmp_path)
    u.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    u.set_password("owner", "correct horse")
    assert u.verify_password("owner", "correct horse") and not u.verify_password(
        "owner", "wrong"
    )
    b32 = u.totp_begin("owner", SECRET)
    assert not u.has_second_factor("owner")["totp"]
    earlier, current = totp_codes(b32)[:2]
    assert u.totp_confirm("owner", earlier, SECRET, env.now)
    assert u.has_second_factor("owner")["totp"]
    assert u.totp_verify("owner", current, SECRET, env.now)
    assert not u.totp_verify("owner", "not-a-code", SECRET, env.now)
    # global review 15.10.2026: a code is taken once (RFC 6238 §5.2) — the same one, and any older step, is refused
    assert not u.totp_verify("owner", current, SECRET, env.now)
    assert not u.totp_verify("owner", earlier, SECRET, env.now)
    assert u.totp_verify("owner", totp_codes(b32)[2], SECRET, env.now)
    assert "totp_secret" not in str(u.get("owner"))
    token = u.open_session("owner", "iPhone", env.now)
    assert u.session(token, env.now)["user"] == "owner"
    assert u.session("nope", env.now) is None
    u.close_all("owner")
    assert u.session(token, env.now) is None


def test_sessions_slide_and_expire_and_a_blocked_user_has_none(tmp_path):
    env, u = _users(tmp_path)
    u.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    token = u.open_session("owner", "Desktop", env.now)
    assert u.session(token, "2026-11-12T10:00:00Z")  # 29 days later: slides
    assert u.session(token, "2026-12-11T09:00:00Z")  # within 30 days of the last use
    assert u.session(token, "2027-02-01T00:00:00Z") is None
    other = u.open_session("owner", "Phone", env.now)
    assert [s["device"] for s in u.sessions("owner")] == ["Phone"]
    u.set_status("owner", "blocked", actor="root")
    assert u.session(other, env.now) is None
    u.close_session(other)
    assert u.sessions("owner") == []


def test_failed_attempts_are_counted_per_key(tmp_path):
    env, u = _users(tmp_path)
    for _ in range(5):
        u.failed_attempt("owner|198.51.100.2", env.now)
    assert u.too_many("owner|198.51.100.2", env.now) and not u.too_many(
        "owner|198.51.100.5", env.now
    )
    assert not u.too_many("owner|198.51.100.2", "2026-10-14T10:16:00Z")
    u.clear_attempts("owner|198.51.100.2")
    assert not u.too_many("owner|198.51.100.2", env.now)


def test_roles_are_data_and_events_never_carry_secrets(tmp_path):
    env, u = _users(tmp_path)
    assert set(u.roles()) == {"owner", "partner", "viewer"}
    u.set_role("auditor", ["cards.read", "servers.read"], actor="user:owner")
    with pytest.raises(ValueError):
        u.set_role("bad", ["cards.fly"], actor="user:owner")
    u.create("bob", "Bob", roles=[("auditor", "*")], actor="user:owner", language="en")
    assert u.get("bob")["language"] == "en" and u.can("bob", "servers.read", "digipaddock")
    assert u.permissions("bob") == {"*": {"cards.read", "servers.read"}}
    u.assign("bob", [("viewer", ["web"])], actor="user:owner")
    assert u.permissions("bob") == {"web": {"cards.read"}}
    code = u.issue_code("bob", env.now)
    token = u.open_session("bob", "x", env.now)
    kinds = [e["kind"] for e in env.store.events_since(0)]
    assert (
        "user.created" in kinds
        and "user.role_set" in kinds
        and "user.session_opened" in kinds
    )
    dump = str(env.store.events_since(0))
    assert code not in dump and token not in dump
    with pytest.raises(ValueError):
        u.create("bob", "Again", roles=[("viewer", "*")], actor="root")
    with pytest.raises(ValueError):
        u.create("Bad Id", "x", roles=[("viewer", "*")], actor="root")
    with pytest.raises(ValueError):
        u.create("carl", "x", roles=[("nosuch", "*")], actor="root")
    u.update("bob", name="Robert", timezone="Asia/Dubai", actor="user:owner")
    assert u.get("bob")["name"] == "Robert" and [x["id"] for x in u.list()] == ["bob"]


def test_unknown_permission_is_refused():
    with pytest.raises(ValueError):
        perms.check("cards.fly")
    assert perms.allows(["*"], "users.manage") and not perms.allows(
        ["cards.read"], "cards.edit"
    )
