"""``kd-signers status | sync [--apply]`` — the cabinet's signing passkeys against kd-review, kd-model-keys and
every enabled server's exec gate; sync writes only the two local copies and prints the owner's line for servers."""

import base64
import io
import json
import os
import stat
from pathlib import Path

import pytest

from kd import signers
from kd.cabinet import api, passkeys
from kd.cabinet import client as cabinet_client
from cabinet_env import CabinetEnv

ORIGIN, RP = "https://cabinet.example.test", "cabinet.example.test"
PROD = "prod-a"


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class Client:
    def __init__(self, env):
        self.env = env

    def call(self, request):
        try:
            return api.dispatch(self.env.ctx, api.Principal("root", 0, "root"), request)
        except api.ApiError as exc:
            raise cabinet_client.CabinetError(exc.kind, str(exc))


class World:
    """A cabinet, the two local registries in tmp and servers whose registry ``read`` answers from ``self.remote``."""

    def __init__(self, tmp_path):
        self.env = CabinetEnv(tmp_path)
        self.env.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
        self.env.users.create(
            "anna", "Анна", roles=[("partner", ["web"])], actor="root"
        )
        self.review_dir, self.model_dir = tmp_path / "review", tmp_path / "model"
        self.review_dir.mkdir(mode=0o700)
        self.model_dir.mkdir(mode=0o700)
        me = (os.getuid(), os.getgid())
        self.registries = (
            signers.Review("kd-review", str(self.review_dir), lambda: me),
            signers.ModelKeys("kd-model-keys", str(self.model_dir), lambda: me),
        )
        self.remote = {}  # alias -> the text its read gate prints, or an Unread reason
        self.reads = []

    def key(self, user, seed: bytes, name):
        registered = passkeys.Registered(
            credential_id=seed * 4, public_key=seed * 20, sign_count=0, transports=[]
        )
        self.env.users.add_credential(user, registered, name, self.env.now)
        return b64(seed * 4)

    def read(self, argv):
        self.reads.append(argv)
        answer = self.remote[argv[0]]
        if isinstance(answer, signers.Unread):
            raise answer
        return answer

    def servers(self):
        return [signers.Server(alias, [alias]) for alias in self.remote]

    def run(self, *argv, euid=0):
        out, err = io.StringIO(), io.StringIO()
        env = signers.Env(
            client=Client(self.env),
            registries=self.registries,
            servers=self.servers,
            read=self.read,
            origin=lambda: ORIGIN,
            clock=lambda: 1_800_000_000,
        )
        code = signers.main(list(argv), env=env, euid=euid, out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    # registries as the services hold them
    def cabinet_keys(self, user="owner"):
        return Client(self.env).call({"op": "signer.keys", "user": user})["keys"]

    def review_file(self, user, keys):
        entries = [
            {
                "credential_id": k["credential_id"],
                "public_key": k["public_key"],
                "name": k["name"],
                "types": ["pr_approve"],
                "added": "2026-11-01T10:00:00Z",
                "added_by": "root",
            }
            for k in keys
        ]
        path = self.review_dir / f"{user}.json"
        path.write_text(json.dumps({"user": user, "keys": entries}), encoding="utf-8")
        path.chmod(0o640)  # not the default: sync keeps a file's own mode
        return path

    def model_file(self, user, keys):
        path = self.model_dir / f"{user}.json"
        path.write_text(
            json.dumps({"user": user, "owner": True, "keys": keys}), encoding="utf-8"
        )
        path.chmod(0o600)
        return path

    def target(self, keys, *, masked=True, rp_id=RP, origin=ORIGIN):
        """The server's registry as its read gate prints it: public keys masked, its note after the JSON."""
        entries = [
            {
                "user": "owner",
                "credential_id": k["credential_id"],
                "public_key": "***" if masked else k["public_key"],
                "name": k["name"],
            }
            for k in keys
        ]
        text = json.dumps(
            {"schema_version": 1, "rp_id": rp_id, "origin": origin, "signers": entries},
            indent=2,
        )
        return text + (
            "\n\n[agent-ro-gate: скрыто секретов: %d]\n" % len(keys)
            if masked and keys
            else "\n"
        )

    def files(self):
        return {
            p: (p.read_bytes(), p.stat().st_mtime_ns)
            for d in (self.review_dir, self.model_dir)
            for p in sorted(d.iterdir())
        }


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def matching(world):
    """Two signing keys of the owner, every registry in step with the cabinet."""
    world.key("owner", b"aaaa-1", "Chrome")
    world.key("owner", b"bbbb-2", "Phone")
    world.key("anna", b"cccc-3", "Anna's")
    keys = world.cabinet_keys()
    world.review_file("owner", keys)
    world.model_file("owner", keys)
    world.remote = {"node-a": world.target(keys), PROD: world.target(keys)}
    return keys


def test_status_ok_everywhere_exit_0(world):
    matching(world)
    code, out, err = world.run("status")
    assert code == 0, out + err
    lines = out.splitlines()
    for where in (
        "kd-review owner",
        "kd-model-keys owner",
        "server node-a",
        f"server {PROD}",
    ):
        assert f"{where}: ok" in lines
    assert "anna" not in out  # a partner signs nothing here
    assert all(argv in (["node-a"], [PROD]) for argv in world.reads)


def test_a_switched_off_key_is_extra_everywhere_and_sync_without_apply_writes_nothing(
    world,
):
    keys = matching(world)
    world.env.users.set_signing(
        "owner", keys[1]["credential_id"][:8], False
    )  # kd-cabinet user signing … off
    before = world.files()
    code, out, err = world.run("status")
    assert code == 1, out + err
    gone = f"Phone {keys[1]['credential_id'][:8]}"
    for where in (
        "kd-review owner",
        "kd-model-keys owner",
        "server node-a",
        f"server {PROD}",
    ):
        assert f"{where}: extra {gone}" in out.splitlines()
    code, out, err = world.run("sync")
    assert code == 1
    assert (
        f"plan kd-review owner: remove {gone}" in out
        and f"plan kd-model-keys owner: remove {gone}" in out
    )
    assert "nothing written" in out
    assert world.files() == before  # not one registry file touched


def test_sync_prints_replace_signers_for_differing_servers_prod_last_and_never_execs(
    world,
):
    keys = matching(world)
    other = dict(keys[0], credential_id=b64(b"zzzz-9" * 4), name="Old")
    world.remote = {
        PROD: world.target([other]),
        "node-a": world.target(keys),
        "node-b": world.target(keys[:1]),
    }
    world.servers = lambda: [
        signers.Server(a, [a]) for a in ("node-a", "node-b", PROD)
    ]  # prod last, as servers()
    code, out, _ = world.run("sync")
    assert code == 1
    lines = [line for line in out.splitlines() if "--replace-signers" in line]
    assert lines == [
        "server node-b: on the owner's machine: deploy_target.sh node-b --replace-signers",
        f"server {PROD}: on the owner's machine: deploy_target.sh {PROD} --replace-signers",
    ]
    assert "server node-a: ok" in out
    # the servers were only read: one argv each, the read command and nothing else
    assert sorted(a[0] for a in world.reads) == sorted(["node-a", "node-b", PROD])
    code, out, _ = world.run("sync", "--apply")
    assert sorted(a[0] for a in world.reads) == sorted(
        ["node-a", "node-b", PROD] * 2
    )  # status after: no re-read
    assert "deploy_target.sh node-b --replace-signers" in out


def test_servers_are_read_through_the_ro_gate_with_cat_only_prod_last(tmp_path):
    inv = tmp_path / "inventory.json"
    inv.write_text(
        json.dumps(
            {
                "home": "stage-a",
                "prod": PROD,
                "servers": {
                    PROD: {"host": "203.0.113.20"},
                    "stage-a": {"host": "203.0.113.10"},
                    "node-a": {"host": "203.0.113.30"},
                    "node-off": {"host": "203.0.113.40"},
                },
            }
        ),
        encoding="utf-8",
    )
    enabled = tmp_path / "servers.enabled"
    enabled.write_text(f"{PROD}\nstage-a\nnode-a\n", encoding="utf-8")
    found = signers.servers(str(inv), str(enabled), home="/home/x-sysadmin")
    assert [s.alias for s in found] == ["stage-a", "node-a", PROD]
    for server in found:
        assert (
            server.argv[0] == "/usr/bin/ssh"
            and server.argv[-1] == "cat /etc/agent-exec-gate/signers.json"
        )
        assert (
            server.argv[-3].startswith("agent-ro@") and "BatchMode=yes" in server.argv
        )


def test_an_unreadable_server_says_why_the_others_are_checked_exit_2(world):
    keys = matching(world)
    world.remote = {
        "node-a": signers.Unread("exit 255: Connection timed out"),
        "node-b": "not json",
        PROD: world.target(keys),
    }
    code, out, _ = world.run("status")
    assert code == 2
    assert "server node-a: unread: exit 255: Connection timed out" in out.splitlines()
    assert (
        "server node-b: unread: /etc/agent-exec-gate/signers.json is not JSON"
        in out.splitlines()
    )
    assert f"server {PROD}: ok" in out.splitlines() and "kd-review owner: ok" in out


def test_the_read_gates_own_masking_still_parses(world):
    """The server's gate masks ``"public_key": …`` and adds its note: kd-signers compares by credential id there."""
    from gate import agent_ro_gate

    keys = matching(world)
    registry = json.dumps(
        {
            "schema_version": 1,
            "rp_id": RP,
            "origin": ORIGIN,
            "signers": [dict(k, user="owner") for k in keys],
        },
        indent=2,
    ).encode()
    printed, hidden = agent_ro_gate.redact_counted(registry)
    assert hidden == len(keys) and keys[0]["public_key"].encode() not in printed
    world.remote = {
        "node-a": (
            printed
            + b"\n[agent-ro-gate: \xd1\x81\xd0\xba\xd1\x80\xd1\x8b\xd1\x82\xd0\xbe "
            b"\xd1\x81\xd0\xb5\xd0\xba\xd1\x80\xd0\xb5\xd1\x82\xd0\xbe\xd0\xb2: 2]\n"
        ).decode("utf-8")
    }
    assert world.run("status")[0] == 0


def test_a_real_ssh_failure_is_unread(tmp_path):
    with pytest.raises(signers.Unread, match="exit 3"):
        signers.ssh_read(["/bin/sh", "-c", "echo nope >&2; exit 3"])


def test_a_server_trusting_another_cabinet_differs(world):
    keys = matching(world)
    world.remote = {
        "node-a": world.target(
            keys, rp_id="evil.example.test", origin="https://evil.example.test"
        )
    }
    code, out, _ = world.run("status")
    assert (
        code == 1
        and "server node-a: trusts the cabinet evil.example.test https://evil.example.test"
        in out
    )


def test_an_unmasked_public_key_is_compared_too(world):
    keys = matching(world)
    world.remote = {
        "node-a": world.target(
            [dict(keys[0], public_key=b64(b"x" * 77)), keys[1]], masked=False
        )
    }
    code, out, _ = world.run("status")
    assert (
        code == 1
        and f"server node-a: extra Chrome {keys[0]['credential_id'][:8]}" in out
    )


def test_apply_refuses_an_owner_without_a_signing_key_and_writes_nothing(world):
    keys = matching(world)
    for k in keys:
        world.env.users.set_signing("owner", k["credential_id"][:8], False)
    before = world.files()
    code, out, err = world.run("sync", "--apply")
    assert (
        code == 3
        and "owner has no passkey that signs" in err
        and "nothing written" in err
    )
    assert world.files() == before


def test_not_root_is_refused(world):
    matching(world)
    code, out, err = world.run("status", euid=1000)
    assert code == 3 and out == "" and "root only" in err


def test_usage(world):
    assert world.run("sync", "--force")[0] == 3


def test_apply_brings_both_local_registries_to_the_cabinet_and_keeps_owner_and_mode(
    world,
):
    keys = matching(world)
    world.env.users.set_signing(
        "owner", keys[1]["credential_id"][:8], False
    )  # switched off
    new_id = world.key("owner", b"dddd-4", "Laptop")  # a new passkey signs by default
    bob = world.review_file("bob", [keys[0]])  # not an owner's registry: never touched
    bob_before = bob.read_bytes()
    review, model = world.review_dir / "owner.json", world.model_dir / "owner.json"
    old_entry = json.loads(review.read_text())["keys"][0]
    code, out, err = world.run("sync", "--apply")
    assert code == 1, (
        out + err
    )  # the servers still hold the old set: theirs is the owner's --replace-signers
    assert "status after sync:" in out
    after = out.split("status after sync:")[1].splitlines()
    assert "kd-review owner: ok" in after and "kd-model-keys owner: ok" in after
    data = json.loads(review.read_text())
    assert [k["credential_id"] for k in data["keys"]] == [
        keys[0]["credential_id"],
        new_id,
    ]
    assert data["keys"][0] == old_entry  # a kept key keeps its entry
    assert (
        data["keys"][1]["types"] == ["pr_approve"]
        and data["keys"][1]["added_by"] == "kd-signers"
    )
    assert (
        stat.S_IMODE(review.stat().st_mode) == 0o640
        and review.stat().st_uid == os.getuid()
    )
    model_data = json.loads(model.read_text())
    assert model_data["owner"] is True and [
        k["credential_id"] for k in model_data["keys"]
    ] == [keys[0]["credential_id"], new_id]
    assert set(model_data["keys"][1]) == {
        "credential_id",
        "public_key",
        "name",
        "created",
    }
    assert stat.S_IMODE(model.stat().st_mode) == 0o600
    assert bob.read_bytes() == bob_before
    assert not [
        p for p in world.review_dir.iterdir() if p.name.startswith(".")
    ]  # no temporary left
    world.remote = {}
    assert world.run("status")[0] == 0


def test_apply_creates_a_missing_registry_0600(world):
    world.key("owner", b"aaaa-1", "Chrome")
    code, out, err = world.run("sync", "--apply")
    assert code == 0, out + err
    for path in (world.review_dir / "owner.json", world.model_dir / "owner.json"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_apply_refuses_an_unreadable_registry_before_writing(world):
    keys = matching(world)
    world.env.users.set_signing("owner", keys[1]["credential_id"][:8], False)
    (world.model_dir / "owner.json").write_text("{broken", encoding="utf-8")
    before = world.files()
    code, out, err = world.run("sync", "--apply")
    assert code == 3 and "kd-model-keys" in err and "nothing written" in err
    assert world.files() == before
    code, out, _ = world.run("status")
    assert code == 2 and "kd-model-keys owner: unread:" in out


def test_review_entries_of_other_types_stay(world):
    world.key("owner", b"aaaa-1", "Chrome")
    path = world.review_dir / "owner.json"
    other = {
        "credential_id": "x" * 22,
        "public_key": "y",
        "types": ["future"],
        "name": "?",
    }
    path.write_text(json.dumps({"user": "owner", "keys": [other]}), encoding="utf-8")
    assert world.run("sync", "--apply")[0] == 0
    assert json.loads(path.read_text())["keys"][0] == other


def test_the_command_file_runs_main():
    text = (Path(__file__).resolve().parents[1] / "bin" / "kd-signers").read_text(
        encoding="utf-8"
    )
    assert (
        text.startswith("#!/usr/bin/python3 -I\n")
        and "from kd.signers import main" in text
    )
