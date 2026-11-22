"""independent signatures, finite filesystem effects and secret-free outcomes."""

import io
import json
import os
import socket
import stat
import threading
from pathlib import Path

import pytest
from softauth import SoftAuthenticator

from kd import host_secrets, install_catalog, llm_models, model_keys
from kd.cabinet import config, passkeys

VALUE = "fixture-provider-value-9218"


@pytest.fixture
def signing(tmp_path):
    cfg = config.load("/none", "/none", dev=True)
    auth = SoftAuthenticator(cfg.rp_id, cfg.origin)
    options, challenge = passkeys.registration_options(
        cfg, user_id="owner", user_name="Owner", exclude=[]
    )
    reg = passkeys.verify_registration(cfg, auth.register(options), challenge)
    (tmp_path / "owner.json").write_text(
        json.dumps(
            {
                "user": "owner",
                "owner": True,
                "keys": [
                    {
                        "credential_id": model_keys.b64(reg.credential_id),
                        "public_key": model_keys.b64(reg.public_key),
                    }
                ],
            }
        )
    )
    effects = []
    clock = [1000.0]
    svc = model_keys.KeyService(
        cfg,
        signers_dir=tmp_path,
        clock=lambda: clock[0],
        write=lambda p, v: effects.append(("put", p, v)),
        remove=lambda p: effects.append(("delete", p)),
        audit=lambda u, p, a: effects.append(("audit", u, p, a)),
    )
    return svc, auth, effects, clock


def signed(signing, action="put", value=VALUE, provider="openrouter"):
    svc, auth, _, _ = signing
    op = {"user": "owner", "provider": provider, "action": action}
    challenge = svc.handle(
        {
            "op": "challenge",
            **op,
            "digest": model_keys.key_digest(value) if action == "put" else "",
        }
    )
    return {
        "op": "apply",
        **op,
        "challenge_id": challenge["challenge_id"],
        "credential": auth.assert_(challenge["options"]),
        **({"key": value} if action == "put" else {}),
    }


def test_signed_put_and_delete_are_single_use_and_do_not_expose_the_value(
    signing, capsys
):
    svc, _, effects, _ = signing
    request = signed(signing)
    assert svc.handle(request) == {"key": True, "audit": True}
    assert effects == [
        ("put", "openrouter", VALUE),
        ("audit", "owner", "openrouter", "put"),
    ]
    with pytest.raises(model_keys.Refused):
        svc.handle(request)
    assert svc.handle(signed(signing, "delete")) == {"key": False, "audit": True}
    assert effects[-2:] == [
        ("delete", "openrouter"),
        ("audit", "owner", "openrouter", "delete"),
    ]
    assert VALUE not in capsys.readouterr().err


@pytest.mark.parametrize(
    "change",
    [
        "provider",
        "action",
        "user",
        "key",
        "credential",
        "expired",
        "restart",
        "revoked",
    ],
)
def test_a_signature_cannot_be_rebound_or_used_after_expiry_restart_or_revocation(
    signing, change
):
    svc, _, effects, clock = signing
    request = signed(signing)
    if change == "expired":
        clock[0] += model_keys.TTL
    elif change == "restart":
        svc.pending.clear()
    elif change == "revoked":
        (Path(svc.signers_dir) / "owner.json").unlink()
    else:
        request[change] = {
            "provider": "deepseek",
            "action": "delete",
            "user": "anna",
            "key": "another-value",
            "credential": {},
        }[change]
    with pytest.raises(model_keys.Refused):
        svc.handle(request)
    assert effects == []


@pytest.mark.parametrize("value", ["", "a\nb", "a\n", "a b", "'quoted'", "x" * 4097])
def test_invalid_key_shapes_have_no_effect(signing, value):
    svc, _, effects, _ = signing
    request = signed(signing, value=value)
    with pytest.raises(model_keys.Refused):
        svc.handle(request)
    assert effects == []


def test_unknown_paths_fields_and_other_peers_fail_closed(signing, capsys):
    svc, _, effects, _ = signing
    request = signed(signing)
    for extra in ({"provider": "../cabinet"}, {"path": "/tmp/key"}, {"op": "read"}):
        result = json.loads(
            model_keys.answer(svc, json.dumps({**request, **extra}).encode())
        )
        assert result["ok"] is False and VALUE not in json.dumps(result)
    assert (
        json.loads(model_keys.answer(svc, json.dumps(request).encode(), False))["ok"]
        is False
    )
    assert (
        json.loads(model_keys.answer(svc, b"x" * (model_keys.LINE_MAX + 1)))["ok"]
        is False
    )
    assert effects == [] and VALUE not in capsys.readouterr().err


def test_a_feed_failure_reports_the_filesystem_effect_explicitly(signing):
    svc, _, effects, _ = signing

    def failed(*args):
        raise OSError(VALUE)

    svc.audit = failed
    assert svc.handle(signed(signing)) == {"key": True, "audit": False}
    assert effects == [("put", "openrouter", VALUE)]


def test_the_web_writer_calls_the_same_catalog_writer_as_kd_secret(monkeypatch):
    calls = []
    monkeypatch.setattr(
        model_keys.host_secrets,
        "main",
        lambda argv, **kw: calls.append((argv, kw)) or 0,
    )
    model_keys.write_key("openrouter", VALUE)
    argv, kwargs = calls[0]
    assert argv == ["put", "llm-openrouter"]
    assert kwargs["stdin"].read() == f"{llm_models.SECRET_KEY}={VALUE}\n".encode()
    assert isinstance(kwargs["out"], io.StringIO) and isinstance(
        kwargs["err"], io.StringIO
    )


@pytest.mark.skipif(not hasattr(socket, "SO_PEERCRED"), reason="Linux peer credentials")
def test_actual_socket_accepts_only_the_cabinet_uid(signing):
    svc, _, effects, _ = signing

    def ask(request, allowed_uid):
        server, client = socket.socketpair()
        worker = threading.Thread(
            target=model_keys.serve_one, args=(svc, server, allowed_uid)
        )
        worker.start()
        with client:
            client.settimeout(3)
            if allowed_uid == os.geteuid():
                client.sendall(json.dumps(request).encode() + b"\n")
            result = json.loads(client.recv(model_keys.LINE_MAX))
        worker.join(3)
        assert not worker.is_alive()
        return result

    request = signed(signing)
    assert ask(request, os.geteuid() + 1)["ok"] is False
    assert effects == []
    assert ask(request, os.geteuid())["ok"] is True
    assert effects[0] == ("put", "openrouter", VALUE)


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() != 0,
    reason="root-only disposable writer probe",
)
def test_shared_writer_really_creates_root_0600_file_without_leaking_value(tmp_path):
    path = tmp_path / "provider.env"
    secret = install_catalog.Secret(
        "llm-openrouter",
        str(path),
        "root",
        "root",
        0o600,
        "common/install/cabinet_setup.sh",
        (),
        "ANTHROPIC_AUTH_TOKEN=<key>",
        "fixture",
    )
    out, err = io.StringIO(), io.StringIO()
    result = host_secrets.main(
        ["put", "llm-openrouter"],
        catalog=[secret],
        stdin=io.BytesIO(f"{llm_models.SECRET_KEY}={VALUE}\n".encode()),
        out=out,
        err=err,
    )
    assert result == 0, err.getvalue()
    assert path.stat().st_uid == 0 and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text() == f"{llm_models.SECRET_KEY}={VALUE}\n"
    assert host_secrets.status([secret])[0]["state"] == "ok"
    assert VALUE not in out.getvalue() + err.getvalue()
