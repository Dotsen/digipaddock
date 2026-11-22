"""owner-only HTTP writes with actual WebAuthn assertions; no secrets in SQLite."""

import json

import pytest
from softauth import SoftAuthenticator
from web_env import WebEnv

from kd import llm_models, model_keys
from kd.cabinet import api
from kd.cabinet.dev_model_keys import DevModelKeys

VALUE = "fixture-model-key-9218"


@pytest.fixture
def w(tmp_path):
    w = WebEnv(tmp_path)
    for name, role in (("owner", "owner"), ("anna", "partner"), ("reader", "viewer")):
        w.users.create(name, name, roles=[(role, "*")], actor="root")
    w.svc.model_keys = DevModelKeys(w.svc, tmp_path)
    w.login()
    auth = SoftAuthenticator(w.cfg.rp_id, w.cfg.origin)
    options = w.client.post("/api/me/passkeys/options", headers=w.headers()).json()
    result = w.client.post(
        "/api/me/passkeys",
        headers=w.headers(),
        json={"name": "Owner", "credential": auth.register(options["options"])},
    )
    assert result.status_code == 200, result.text
    root = api.Principal("root", 0, "root")

    class Cabinet:
        def call(self, op):
            return api.dispatch(w.env.ctx, root, op)

    model_keys.enroll(
        "owner",
        model_keys.b64(auth.credential_id)[:12],
        Cabinet(),
        w.svc.model_keys.signers,
    )
    w.auth = auth
    return w


def signature(w, action="put"):
    challenge = w.client.post(
        "/api/models/providers/openrouter/key/challenge",
        headers=w.headers(),
        json={
            "action": action,
            "digest": model_keys.key_digest(VALUE) if action == "put" else "",
        },
    )
    assert challenge.status_code == 200, challenge.text
    return {
        "action": action,
        "challenge_id": challenge.json()["challenge_id"],
        "credential": json.loads(w.auth.assert_(challenge.json()["options"])),
        **({"key": VALUE} if action == "put" else {}),
    }


def test_put_delete_feed_and_database_never_hold_the_key(w, capsys):
    path = w.svc.model_keys.env / "openrouter.env"
    request = signature(w)
    result = w.client.post(
        "/api/models/providers/openrouter/key", headers=w.headers(), json=request
    )
    assert result.status_code == 200, result.text
    assert result.json() == {"key": True, "audit": True}
    assert llm_models.read_secret("openrouter", str(path.parent)) == {
        llm_models.SECRET_KEY: VALUE
    }
    assert (
        w.client.post(
            "/api/models/providers/openrouter/key", headers=w.headers(), json=request
        ).status_code
        == 403
    )
    event = w.client.get("/api/feed?kinds=llm_key.changed").json()["events"][0]
    assert event["payload"] == {"provider": "openrouter", "action": "put"}
    assert event["actor"] == "user:owner" and event["ts"]
    assert (
        w.client.post(
            "/api/models/providers/openrouter/key",
            headers=w.headers(),
            json=signature(w, "delete"),
        ).status_code
        == 200
    )
    assert not path.exists()
    assert not llm_models.read_secret(
        "openrouter", str(path.parent)
    )  # the runtime's no_key input
    assert VALUE not in "\n".join(w.env.store._conn.iterdump())
    assert VALUE not in capsys.readouterr().err
    assert all(p["manage_key"] for p in w.client.get("/api/models/providers").json())


@pytest.mark.parametrize("user", ["owner", "anna", "reader"])
def test_unsigned_or_nonowner_requests_are_403_with_no_file(w, user):
    client = w.other_client()
    me = w.login(user, client)
    headers = w.headers(me["csrf"])
    assert (
        client.post(
            "/api/models/providers/openrouter/key",
            headers=headers,
            json={"action": "put", "key": VALUE},
        ).status_code
        == 403
    )
    if user != "owner":
        assert (
            client.post(
                "/api/models/providers/openrouter/key/challenge",
                headers=headers,
                json={"action": "delete"},
            ).status_code
            == 403
        )
        assert all(
            not p["manage_key"] for p in client.get("/api/models/providers").json()
        )
    assert not (w.svc.model_keys.env / "openrouter.env").exists()


def test_csrf_totp_substitution_and_arbitrary_audit_values_are_refused(w):
    body = signature(w)
    assert (
        w.client.post("/api/models/providers/openrouter/key", json=body).status_code
        == 403
    )
    body.pop("credential")
    body["totp"] = "123456"
    assert (
        w.client.post(
            "/api/models/providers/openrouter/key", headers=w.headers(), json=body
        ).status_code
        == 400
    )
    with pytest.raises(api.ApiError):
        api.dispatch(
            w.env.ctx,
            api.Principal("root", 0, "root"),
            {
                "op": "llm.key.changed",
                "user": "owner",
                "provider": "openrouter",
                "action": "put",
                "key": VALUE,
            },
        )
    with pytest.raises(api.ApiError):
        api.dispatch(
            w.env.ctx,
            api.Principal("user", -1, "owner"),
            {
                "op": "llm.key.changed",
                "user": "owner",
                "provider": "openrouter",
                "action": "put",
            },
        )
    assert VALUE not in "\n".join(w.env.store._conn.iterdump())
