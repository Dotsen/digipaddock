"""signed subscription replacement, age, refusal of deletion and explicit probe state."""

import json
import os
import subprocess
from types import SimpleNamespace

import pytest
from softauth import SoftAuthenticator
from web_env import WebEnv

from kd import claude_token, model_credentials, model_keys
from kd.cabinet import llm_api
from kd.cabinet.dev_model_keys import DevModelKeys
from kd.session_host import probe, protocol


@pytest.fixture
def owner(tmp_path):
    w = WebEnv(tmp_path)
    w.users.create("owner", "Owner", roles=[("owner", "*")], actor="root")
    w.users.create("partner", "Partner", roles=[("partner", "*")], actor="root")
    w.login()
    auth = SoftAuthenticator(w.cfg.rp_id, w.cfg.origin)
    opts = w.client.post("/api/me/passkeys/options", headers=w.headers()).json()
    assert (
        w.client.post(
            "/api/me/passkeys",
            headers=w.headers(),
            json={"name": "Signing", "credential": auth.register(opts["options"])},
        ).status_code
        == 200
    )
    keys = w.users.signing_keys("owner")
    w.svc.model_keys = DevModelKeys(w.svc, tmp_path)
    (w.svc.model_keys.signers / "owner.json").write_text(
        json.dumps(
            {
                "user": "owner",
                "owner": True,
                "keys": [
                    {
                        "credential_id": model_keys.b64(keys[0]["credential_id"]),
                        "public_key": model_keys.b64(keys[0]["public_key"]),
                    }
                ],
            }
        )
    )
    return w, auth


def test_signed_subscription_save_status_probe_and_no_delete(owner):
    w, auth = owner
    url = "/api/models/providers/claude-subscription/key"
    value = "fixture-subscription-token"
    challenge = w.client.post(
        url + "/challenge",
        headers=w.headers(),
        json={"action": "put", "digest": model_keys.key_digest(value)},
    )
    assert challenge.status_code == 200
    result = w.client.post(
        url,
        headers=w.headers(),
        json={
            "action": "put",
            "key": value,
            "challenge_id": challenge.json()["challenge_id"],
            "credential": json.loads(auth.assert_(challenge.json()["options"])),
        },
    )
    assert result.status_code == 200 and result.json()["restart"] is True
    saved = w.svc.model_keys.root / "claude.env"
    assert saved.read_text() == claude_token.KEY + "=" + value + "\n"
    assert w.client.get("/api/model-credentials/subscription").json()["age_days"] == 0
    assert (
        w.client.post(
            url + "/challenge", headers=w.headers(), json={"action": "delete"}
        ).status_code
        == 403
    )
    assert saved.exists() and value not in "\n".join(w.env.store._conn.iterdump())
    asked = w.client.post(
        "/api/model-credentials/subscription/probe", headers=w.headers()
    ).json()
    assert asked["state"] == "requested"
    with w.env.store.transaction():
        w.env.store.set_state("subscription_probe", {**asked, "id": "a" * 32})
    llm_api.report_item(
        w.env.ctx,
        {
            "probe": "a" * 32,
            "ok": True,
            "model": "claude-fixture",
            "ms": 10,
            "detail": "OK",
        },
    )
    assert (
        w.client.get("/api/model-credentials/subscription").json()["probe"]["ok"]
        is True
    )
    other = w.other_client()
    me = w.login("partner", other)
    assert other.get("/api/model-credentials/subscription").status_code == 403
    assert (
        other.post(
            "/api/model-credentials/subscription/probe", headers=w.headers(me["csrf"])
        ).status_code
        == 403
    )


def test_estimated_expiry_is_metadata_with_advance_warning(tmp_path):
    path = tmp_path / "claude.env"
    now = 2_000_000_000
    assert model_credentials.subscription_status(path)["warning"] == "missing"
    path.write_text("fixture")
    for age, warning in ((0, ""), (335, "month"), (358, "week"), (365, "expired")):
        os.utime(path, (now - age * 86400, now - age * 86400))
        status = model_credentials.subscription_status(path, now=lambda: now)
        assert (
            status["age_days"] == age
            and status["warning"] == warning
            and status["estimated"] is True
        )
        assert "fixture" not in json.dumps(status)


@pytest.mark.parametrize("timeout", [False, True])
def test_root_writer_reports_failed_hindsight_restart_after_successful_write(
    monkeypatch, timeout
):
    called = []

    def run(argv, **kwargs):
        called.append(argv)
        if timeout and argv[0] == "systemctl":
            raise subprocess.TimeoutExpired(argv, 20)
        return SimpleNamespace(returncode=0 if argv[0] == "bash" else 1)

    def put(argv, **kwargs):
        assert argv == ["put", "claude-token"]
        assert kwargs["stdin"].read().startswith(b"CLAUDE_CODE_OAUTH_TOKEN=")
        assert kwargs["run"](["bash", "writer"], b"fixture") == 0
        return kwargs["run"](
            [
                "systemctl",
                "try-restart",
                "hindsight-api.service",
                "hindsight-worker.service",
            ],
            None,
        )

    monkeypatch.setattr(model_keys.subprocess, "run", run)
    monkeypatch.setattr(model_keys.host_secrets, "main", put)
    assert model_keys.write_key("claude-subscription", "fixture") == {"restart": False}
    assert called[-1] == [
        "systemctl",
        "try-restart",
        "hindsight-api.service",
        "hindsight-worker.service",
    ]


def test_subscription_probe_has_only_the_fixed_root_token_path():
    request = {"op": "probe", "probe": "a" * 32, "model": None}
    assert protocol._probe(request) == request
    argv = probe.argv("digipaddock-probe-" + "a" * 12, "digi", None)
    assert "EnvironmentFile=" + claude_token.PATH in argv
    assert all("ANTHROPIC_AUTH_TOKEN=" not in a for a in argv)
