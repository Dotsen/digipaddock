"""``kd-cabinet user signing <id> --credential <prefix> on|off`` and ``kd-cabinet user signers`` — the
owner's signing passkeys as the registry a target's exec gate reads (root; the installer fetches it)."""

import base64
import io
import json
import runpy
from pathlib import Path

import pytest

from kd.cabinet import api, config, passkeys
from kd.cabinet import client as cabinet_client
from cabinet_env import CabinetEnv

ORIGIN = "https://cabinet.example.test"


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@pytest.fixture
def cli(tmp_path, monkeypatch):
    env = CabinetEnv(tmp_path)
    env.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    env.users.create("anna", "Анна", roles=[("partner", ["web"])], actor="root")
    monkeypatch.setattr(config, "origin_of", lambda *args, **kwargs: ORIGIN)
    mod = runpy.run_path(str(Path(__file__).resolve().parents[1] / "bin" / "kd-cabinet"), run_name="kd_cabinet")

    class Client:
        def call(self, request):
            try:
                return api.dispatch(env.ctx, api.Principal("root", 0, "root"), request)
            except api.ApiError as exc:
                raise cabinet_client.CabinetError(exc.kind, str(exc))

    def run(argv):
        out, err = io.StringIO(), io.StringIO()
        code = mod["main"](argv, client=Client(), out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def key(user, seed: bytes, name):
        registered = passkeys.Registered(credential_id=seed * 4, public_key=seed * 20, sign_count=0, transports=[])
        return env.users.add_credential(user, registered, name, env.now)

    run.env, run.key = env, key
    return run


def test_user_signers_prints_registry_for_owners(cli):
    cli.key("owner", b"aaaa-1", "Chrome")
    cli.key("owner", b"bbbb-2", "Phone")
    cli.key("anna", b"cccc-3", "Anna's")  # a partner signs nothing on a server
    code, out, err = cli(["user", "signers"])
    assert code == 0 and err == ""
    data = json.loads(out)
    assert data["schema_version"] == 1 and data["rp_id"] == "cabinet.example.test" and data["origin"] == ORIGIN
    assert [s["user"] for s in data["signers"]] == ["owner", "owner"]
    assert set(data["signers"][0]) == {"user", "credential_id", "public_key", "name"}
    assert data["signers"][0] == {"user": "owner", "credential_id": b64(b"aaaa-1" * 4),
                                  "public_key": b64(b"aaaa-1" * 20), "name": "Chrome"}
    # named people: a partner's keys come when asked for, a person without keys is an error, nothing is printed
    code, out, err = cli(["user", "signers", "anna"])
    assert code == 0 and [s["user"] for s in json.loads(out)["signers"]] == ["anna"]
    cli.env.users.create("bob", "Боб", roles=[("owner", "*")], actor="root")
    code, out, err = cli(["user", "signers", "owner", "bob"])
    assert code == 1 and out == "" and "bob" in err and "no signing passkey" in err


def test_user_signers_skips_owners_who_cannot_sign_yet_or_are_blocked(cli):
    cli.key("owner", b"aaaa-1", "Chrome")
    cli.env.users.create("bob", "Боб", roles=[("owner", "*")], actor="root")  # no passkey yet
    cli.env.users.create("eve", "Ева", roles=[("owner", "*")], actor="root")
    cli.key("eve", b"eeee-5", "Eve's")
    cli.env.users.set_status("eve", "blocked", actor="root")
    code, out, err = cli(["user", "signers"])
    assert code == 0 and [s["user"] for s in json.loads(out)["signers"]] == ["owner"]
    assert "bob" in err and "no signing passkey" in err and "eve" not in err


def test_user_signers_needs_the_cabinet_origin(cli, monkeypatch):
    cli.key("owner", b"aaaa-1", "Chrome")
    monkeypatch.setattr(config, "origin_of", lambda *args, **kwargs: None)
    code, out, err = cli(["user", "signers"])
    assert code == 2 and out == "" and "origin" in err


def test_user_signing_cli_turns_a_key_off_and_on(cli):
    a = cli.key("owner", b"aaaa-1", "Chrome")
    cli.key("owner", b"bbbb-2", "Phone")
    prefix = b64(b"aaaa-1" * 4)[:8]
    code, out, err = cli(["user", "signing", "owner", "--credential", prefix, "off"])
    assert code == 0 and out == f"signing off: Chrome {b64(b'aaaa-1' * 4)[:8]}\n", (out, err)
    assert [c["signing_enabled"] for c in cli.env.users.credentials("owner")] == [False, True]
    code, out, _ = cli(["user", "signers"])
    assert [s["name"] for s in json.loads(out)["signers"]] == ["Phone"]
    code, out, err = cli(["user", "signing", "owner", "--credential", prefix, "on"])
    assert code == 0 and out.startswith("signing on: Chrome")
    assert cli.env.users.credentials("owner")[0]["signing_enabled"] is True and a["id"]
    code, out, err = cli(["user", "signing", "owner", "--credential", "", "off"])
    assert code == 2 and "kd-cabinet:" in err  # ambiguous
    code, out, err = cli(["user", "signing", "owner", "--credential", prefix[:1], "off"])
    assert code == 2 and "at least 8" in err  # a one-letter prefix names no key
    code, out, err = cli(["user", "signing", "anna", "--credential", prefix, "off"])
    assert code == 2  # not hers
    [event] = [e for e in cli.env.store.events_since(0) if e["kind"] == "user.passkey_signing"
               and e["payload"]["enabled"] is False]
    assert event["payload"] == {"user": "owner", "credential": a["id"], "enabled": False}


def test_user_signing_is_root_only(cli):
    cli.key("owner", b"aaaa-1", "Chrome")
    for principal in (api.Principal("user", 0, "owner"), api.Principal("executor", 1000, "digi"),
                      api.Principal("agent", 1001, "digi-dev")):
        with pytest.raises(api.ApiError) as exc:
            api.dispatch(cli.env.ctx, principal,
                         {"op": "user.signing", "id": "owner", "credential": "a", "enabled": False})
        assert exc.value.kind == "refused"
    assert cli.env.users.credentials("owner")[0]["signing_enabled"] is True
