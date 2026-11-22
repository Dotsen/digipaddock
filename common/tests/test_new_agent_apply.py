"""A08 transition tests: local state machine only; no server installation."""

from __future__ import annotations

import inspect
import json
import os
import shutil
import subprocess
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kd import install_apply
from kd.install_apply import ApplyError, disable, install
from kd.install_plan import GENERATED_RUNTIME, Action, Plan


def _plan() -> Plan:
    manifest = SimpleNamespace(name="boss", user="digi-boss")
    return Plan(manifest, {"acl": []}, (), (), ())


class FakeOperations:
    """A separate root-owned policy, grants and durable user data."""

    def __init__(self, fail_at: str | None = None) -> None:
        self.fail_at = fail_at
        self.failed = False
        self.calls: list[str] = []
        self.registry = False
        self.policy = False
        self.acl = False
        self.channels = False
        self.running = False
        self.home = {"auth": "login", "secret": "token", "memory": "notes"}
        self.entry: object = None  # the agent's runtime registry entry before the run

    def runtime_entry(self, plan: Plan) -> object:
        return self.entry  # a read: not a call that changes state

    def _call(self, method: str) -> None:
        self.calls.append(method)
        if method == self.fail_at and not self.failed:
            self.failed = True
            raise OSError("injected failure")

    def matches(self, plan: Plan) -> bool:
        self._call("matches")
        return self.registry and self.policy and self.acl and self.channels

    def quiesce(self, plan: Plan) -> None:
        self._call("quiesce")
        self.running = False

    def revoke(self, plan: Plan) -> None:
        self._call("revoke")
        self.channels = False
        self.acl = False

    def prepare(self, plan: Plan) -> None:
        self._call("prepare")

    def publish(self, plan: Plan) -> None:
        self._call("publish")
        self.registry = True
        self.policy = True

    def grant_acl(self, plan: Plan) -> None:
        self._call("grant_acl")
        self.acl = True

    def grant_channels(self, plan: Plan) -> None:
        self._call("grant_channels")
        self.channels = True

    def verify(self, plan: Plan) -> None:
        self._call("verify")
        if not self.matches(plan):
            raise OSError("postflight mismatch")

    def disable_runtime(self, plan: Plan) -> None:
        self._call("disable_runtime")
        self.registry = True  # generated marker remains, the policy goes
        self.policy = False

    def unhost(self, plan: Plan) -> None:
        self._call("unhost")


@pytest.mark.parametrize(
    "step",
    [
        "quiesce",
        "revoke",
        "prepare",
        "publish",
        "grant_acl",
        "grant_channels",
        "verify",
    ],
)
def test_failure_after_each_step_revokes_all_rights(step: str) -> None:
    ops = FakeOperations(step)
    with pytest.raises(ApplyError, match="install failed"):
        install(_plan(), ops)
    assert ops.registry and not ops.policy  # generated runtime without policy
    assert not ops.acl and not ops.channels and not ops.running
    assert ops.home == {"auth": "login", "secret": "token", "memory": "notes"}
    assert ops.calls[-3:] == ["quiesce", "revoke", "disable_runtime"]


def test_second_install_is_no_op_and_preserves_data() -> None:
    ops = FakeOperations()
    plan = _plan()
    assert install(plan, ops) is True
    assert ops.calls.index("publish") < ops.calls.index("grant_acl")
    assert ops.calls.index("grant_acl") < ops.calls.index("grant_channels")
    assert ops.calls.index("grant_channels") < ops.calls.index("verify")
    before = ops.calls[:]
    assert install(plan, ops) is False
    assert ops.calls == before + ["matches"]
    assert ops.home["secret"] == "token"


def test_disable_revokes_first_and_preserves_home_and_registry_marker() -> None:
    ops = FakeOperations()
    plan = _plan()
    install(plan, ops)
    ops.running = True
    start = len(ops.calls)
    disable(plan, ops)
    # off the session host first — no session starts between the stop and the policy's removal
    assert ops.calls[start:] == ["unhost", "quiesce", "revoke", "disable_runtime"]
    assert ops.registry and not ops.policy
    assert not ops.running and not ops.acl and not ops.channels
    assert ops.home == {"auth": "login", "secret": "token", "memory": "notes"}
    disable(plan, ops)
    assert ops.registry and not ops.policy


def test_disable_continues_revoking_after_stop_failure() -> None:
    ops = FakeOperations("quiesce")
    ops.registry = ops.policy = ops.acl = ops.channels = True
    disable(_plan(), ops)
    assert not ops.policy and not ops.acl and not ops.channels


@pytest.mark.parametrize("step", ["unhost", "quiesce", "revoke", "disable_runtime"])
def test_disable_retries_each_transient_failure(step: str) -> None:
    ops = FakeOperations(step)
    ops.registry = ops.policy = ops.acl = ops.channels = True
    disable(_plan(), ops)
    assert ops.registry and not ops.policy
    assert not ops.acl and not ops.channels


def test_disable_reports_permanent_revoke_failure() -> None:
    class BrokenRevoke(FakeOperations):
        def revoke(self, plan: Plan) -> None:
            raise OSError("permanent failure")

    ops = BrokenRevoke()
    ops.registry = ops.policy = ops.acl = ops.channels = True
    with pytest.raises(ApplyError, match="revoke"):
        disable(_plan(), ops)
    assert ops.registry and not ops.policy
    assert ops.acl and ops.channels  # reported, never silently called success


def test_acl_checker_uses_effective_permissions() -> None:
    output = "user:digi-boss:rwx\t#effective:r-x\ndefault:user:digi-boss:r-x\n"
    match = install_apply._acl_text_matches
    assert match(output, "digi-boss", "read", directory=True)
    assert not match(output, "digi-boss", "write", directory=True)
    assert not match(output, "digi-boss", "none", directory=True)
    assert match("user::rw-\n", "digi-boss", "none", directory=False)


# --- descriptor-safe ACL changes (Linux) ----------------------------------------------------


def _root_mapped() -> bool:
    """False in a user namespace without root, like the dev agent's sandbox: setfacl u:root fails there."""
    try:
        maps = [Path(f"/proc/self/{kind}_map").read_text() for kind in ("uid", "gid")]
    except OSError:
        return False
    return all(
        any(line.split()[0] == "0" for line in text.splitlines()) for text in maps
    )


_LINUX_ACL = (
    hasattr(install_apply.os, "O_PATH")
    and Path("/proc/self/fd").is_dir()
    and shutil.which("setfacl") is not None
    and _root_mapped()
)
linux_acl = pytest.mark.skipif(not _LINUX_ACL, reason="Linux setfacl and O_PATH")
ACL_USER = "root"  # any existing account works as a named entry on files the test owns


def _named(path: Path) -> bool:
    out = subprocess.run(
        ("getfacl", "-cp", str(path)), capture_output=True, text=True, check=True
    )
    return any(line.startswith(f"user:{ACL_USER}:") for line in out.stdout.splitlines())


@linux_acl
def test_acl_tree_grants_real_entries_and_skips_symlinks(tmp_path: Path) -> None:
    tree, outside = tmp_path / "tree", tmp_path / "outside"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "file").write_text("x")
    outside.write_text("root file")
    (tree / "link").symlink_to(outside)
    install_apply._acl_tree(str(tree), ACL_USER, access="read")
    assert _named(tree) and _named(tree / "sub") and _named(tree / "sub" / "file")
    assert not _named(outside)
    assert not install_apply._acl_tree_clean(str(tree), ACL_USER)
    install_apply._acl_tree(str(tree), ACL_USER, access=None)
    assert not _named(tree / "sub" / "file") and install_apply._acl_tree_clean(
        str(tree), ACL_USER
    )


@linux_acl
def test_acl_tree_clean_reads_the_acls_without_a_process_per_file(tmp_path: Path, monkeypatch) -> None:
    """--verify walked ~24 000 files of the agents' trees with one getfacl each (75 s a role, kd-release's
    30 s ran out); the named entries come from the ACL xattrs — access and, on a directory, default."""
    tree = tmp_path / "tree"
    for n in range(30):
        (tree / f"d{n % 3}").mkdir(parents=True, exist_ok=True)
        (tree / f"d{n % 3}" / f"f{n}").write_text("x")
    subprocess.run(("setfacl", "-m", "u:nobody:r", str(tree / "d0" / "f0")), check=True)  # another user's entry
    calls = []
    real_run = install_apply.subprocess.run
    monkeypatch.setattr(install_apply.subprocess, "run", lambda *a, **kw: calls.append(a) or real_run(*a, **kw))
    assert install_apply._acl_tree_clean(str(tree), ACL_USER) and calls == []
    monkeypatch.undo()
    subprocess.run(("setfacl", "-d", "-m", f"u:{ACL_USER}:r", str(tree / "d2")), check=True)
    assert not install_apply._acl_tree_clean(str(tree), ACL_USER)
    subprocess.run(("setfacl", "-k", str(tree / "d2")), check=True)
    subprocess.run(("setfacl", "-m", f"u:{ACL_USER}:r", str(tree / "d1" / "f1")), check=True)
    assert not install_apply._acl_tree_clean(str(tree), ACL_USER)
    assert install_apply._acl_tree_clean(str(tree), "no-such-user-kd")


@linux_acl
def test_acl_tree_refuses_symlinked_target_or_parent(tmp_path: Path) -> None:
    real = tmp_path / "real"
    (real / "dir").mkdir(parents=True)
    (tmp_path / "alias").symlink_to(real)
    with pytest.raises(ApplyError, match="symlink"):
        install_apply._acl_tree(str(tmp_path / "alias"), ACL_USER, access="read")
    with pytest.raises(ApplyError, match="symlink"):
        install_apply._acl_tree(
            str(tmp_path / "alias" / "dir"), ACL_USER, access="read"
        )
    assert not _named(real) and not _named(real / "dir")
    install_apply._acl_tree(
        str(tmp_path / "missing"), ACL_USER, access="read"
    )  # absent: no-op


@linux_acl
def test_name_swapped_for_symlink_after_open_cannot_redirect_setfacl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree, outside = tmp_path / "tree", tmp_path / "root-file"
    tree.mkdir()
    (tree / "file").write_text("agent data")
    outside.write_text("root data")
    real_run = install_apply._run

    victim = tree / "file"
    swapped = []

    def swap_then_run(*argv: str, **kw: object) -> None:
        # setfacl is about to change the descriptor opened for "file": the agent swaps the
        # name for a symlink to a root file right now, after every check.
        if argv[0] == "setfacl" and install_apply.os.readlink(argv[-1]) == str(victim):
            victim.rename(tree / "moved")
            victim.symlink_to(outside)
            swapped.append(True)
        real_run(*argv, **kw)

    monkeypatch.setattr(install_apply, "_run", swap_then_run)
    install_apply._acl_tree(str(tree), ACL_USER, access="write")
    assert swapped and victim.is_symlink()
    assert not _named(outside)  # the root file is untouched
    assert _named(tree / "moved")  # the entry landed on the inode that was checked


# --- grant record ---------------------------------------------------------------------------


def _ops(tmp_path: Path) -> install_apply.SystemOperations:
    ops = object.__new__(install_apply.SystemOperations)
    ops.registry = install_apply.load_registry(Path(__file__).resolve().parents[2])
    ops.grants_dir = str(tmp_path / "grants")
    return ops


def test_disable_takes_a_hosted_agent_off_the_session_host(tmp_path: Path) -> None:
    """Final review: --disable of an agent on the session host unlists it (the cabinet then cancels its queued
    commands) instead of leaving it listed with commands that fail at launch."""
    ops = _ops(tmp_path)
    config = tmp_path / "session-host.json"
    config.write_text(json.dumps({"schema_version": 1, "agents": {"dev": {"slots": 1}, "analyst": {"slots": 1}}}),
                      encoding="utf-8")
    ops.session_host_config = str(config)
    plan = install_apply.disable_plan(REPO, "dev")
    ops.unhost(plan)
    assert json.loads(config.read_text(encoding="utf-8"))["agents"] == {"analyst": {"slots": 1}}
    ops.unhost(plan)  # not listed: nothing to do
    config.unlink()
    ops.unhost(plan)  # no session host on this host
    config.write_text("{", encoding="utf-8")
    with pytest.raises(ApplyError, match="session-host"):
        ops.unhost(plan)


def test_revoke_targets_include_recorded_grants_of_removed_resources(
    tmp_path: Path,
) -> None:
    ops = _ops(tmp_path)
    (tmp_path / "grants").mkdir()
    record = {"schema_version": 1, "grants": [["/srv/retired-resource", "digi-boss"]]}
    (tmp_path / "grants" / "boss.json").write_text(json.dumps(record))
    targets = ops._other_acl(_plan())
    assert ("/srv/retired-resource", "digi-boss") in targets
    assert ("/etc/digipaddock/agents.json", "digi-boss") in targets
    assert all(path != "/srv/digipaddock-agents/board" for path, _ in targets)


def test_revoke_sweep_skips_code_resources(tmp_path: Path) -> None:
    """Code is world-readable release files under a symlink; it never carries an ACL."""
    ops = _ops(tmp_path)
    targets = ops._other_acl(_plan())
    assert not [p for p, _ in targets if p.startswith("/opt/digipaddock/")]


@pytest.mark.parametrize(
    "content",
    [
        "{",
        "[]",
        json.dumps({"schema_version": 2, "grants": []}),
        json.dumps({"schema_version": 1, "grants": [["relative", "digi-boss"]]}),
        json.dumps({"schema_version": 1, "grants": [["/x", "root"]]}),
        json.dumps({"schema_version": 1, "grants": [["/x"]]}),
        json.dumps({"schema_version": 1}),
    ],
)
def test_damaged_grant_record_refuses_instead_of_reading_empty(
    tmp_path: Path, content: str
) -> None:
    ops = _ops(tmp_path)
    (tmp_path / "grants").mkdir()
    (tmp_path / "grants" / "boss.json").write_text(content)
    with pytest.raises(ApplyError, match="grant record"):
        ops._other_acl(_plan())


@pytest.mark.skipif(
    not hasattr(install_apply.os, "geteuid") or install_apply.os.geteuid() != 0,
    reason="root",
)
def test_grant_record_round_trip_is_root_only(tmp_path: Path) -> None:
    tmp_path.chmod(0o755)
    ops = _ops(tmp_path)
    grants = {("/var/lib/digipaddock/usage.jsonl", "digi-boss")}
    ops._write_grants(_plan(), grants)
    record = tmp_path / "grants" / "boss.json"
    assert ops._read_grants(_plan()) == grants
    assert stat.S_IMODE(record.stat().st_mode) == 0o600 and record.stat().st_uid == 0
    assert stat.S_IMODE((tmp_path / "grants").stat().st_mode) == 0o700


def test_grant_is_recorded_before_any_setfacl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ops = _ops(tmp_path)
    events: list[object] = []
    monkeypatch.setattr(ops, "_user_script", lambda plan, mode: None, raising=False)
    monkeypatch.setattr(
        ops,
        "_write_grants",
        lambda plan, grants: events.append(sorted(grants)),
        raising=False,
    )
    monkeypatch.setattr(
        install_apply,
        "_acl_tree",
        lambda path, user, access: events.append(("acl", path, user, access)),
    )
    plan = Plan(
        SimpleNamespace(name="boss", user="digi-boss"),
        {
            "acl": [
                {
                    "resource": "usage-ledger",
                    "path": "/var/lib/digipaddock/usage.jsonl",
                    "user": "digi-boss",
                    "access": "read",
                },
                {
                    "resource": "board-tasks",
                    "path": "/srv/digipaddock-agents/board",
                    "user": "digi-boss",
                    "access": "read",
                },
            ],
        },
        (),
        (),
        (),
    )
    ops.grant_acl(plan)
    assert events == [
        [("/var/lib/digipaddock/usage.jsonl", "digi-boss")],
        ("acl", "/var/lib/digipaddock/usage.jsonl", "digi-boss", "read"),
    ]


@pytest.mark.parametrize("argv", [["--install", "boss", "--owner-id", "738"], ["--migrate", "boss"]])
def test_cli_refuses_an_owner_id_and_migrate(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    # no agent has a Telegram chat, so there is no owner chat id to install;
    # no legacy runtime is left to migrate.
    with pytest.raises(SystemExit) as caught:
        install_apply.main(argv)
    assert caught.value.code == 2


def test_shared_base_user_is_refused_before_any_mutation() -> None:
    ops = FakeOperations()
    plan = _plan()
    plan.manifest.user = "digi"
    with pytest.raises(ApplyError, match="shared base user"):
        install(plan, ops)
    with pytest.raises(ApplyError, match="shared base user"):
        disable(plan, ops)
    assert ops.calls == []


@pytest.mark.parametrize(
    "entry",
    [
        {"user": "digi-dev"},
        {"runtime": "legacy", "user": "digi-dev"},
        "not an object",
    ],
)
def test_an_entry_not_of_this_installer_is_refused_before_any_mutation(entry: object) -> None:
    ops = FakeOperations()
    ops.entry = entry
    with pytest.raises(ApplyError, match="not the generated runtime"):
        install(_plan(), ops)
    with pytest.raises(ApplyError, match="not the generated runtime"):
        disable(_plan(), ops)
    assert ops.calls == []


def test_runtime_entry_reads_the_installed_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ops = install_apply.SystemOperations.__new__(install_apply.SystemOperations)
    path = tmp_path / "agents.json"
    monkeypatch.setattr(ops, "_registry_path", lambda: str(path))
    assert ops.runtime_entry(_plan()) is None  # no registry yet: a first install
    path.write_text(json.dumps({"dev": {"user": "digi-dev"}}), "utf-8")
    assert ops.runtime_entry(_plan()) is None
    path.write_text(json.dumps({"boss": {"user": "digi-boss"}}), "utf-8")
    assert ops.runtime_entry(_plan()) == {"user": "digi-boss"}


def test_the_runtime_registry_of_a_host_with_no_agent_is_empty(tmp_path: Path) -> None:
    """The empty-host run: the first agent's install failed writing its registry entry (and the rollback
    too) on the missing agents.json. Every read of it goes through one helper: missing is empty, broken refuses."""
    path = tmp_path / "agents.json"
    assert install_apply._runtime_registry(str(path)) == {}
    path.write_text(json.dumps({"boss": {"user": "digi-boss"}}), "utf-8")
    assert install_apply._runtime_registry(str(path)) == {"boss": {"user": "digi-boss"}}
    path.write_text("[]", "utf-8")
    with pytest.raises((ApplyError, ValueError)):
        install_apply._runtime_registry(str(path))
    source = inspect.getsource(install_apply)
    assert "read_json_object(path) or {}" not in source and "read_json_object(runtime_path)" not in source


def test_a_failed_command_says_why(monkeypatch: pytest.MonkeyPatch) -> None:
    """The empty-host run: «runuser failed (1)» alone left the owner nothing to fix; the error carries the
    command's last stderr line, bounded and on one line."""
    monkeypatch.setattr(
        install_apply.subprocess,
        "run",
        lambda argv, **_: subprocess.CompletedProcess(argv, 1, "", "Traceback ...\n" + "x" * 500 + "\nPermissionError: [Errno 13] /home/u\n"),
    )
    with pytest.raises(ApplyError) as caught:
        install_apply._run("runuser", "-u", "u", "--", "true")
    assert str(caught.value) == "runuser failed (1): PermissionError: [Errno 13] /home/u"
    monkeypatch.setattr(
        install_apply.subprocess, "run", lambda argv, **_: subprocess.CompletedProcess(argv, 2, "", "y" * 900)
    )
    with pytest.raises(ApplyError) as caught:
        install_apply._run("setfacl")
    assert len(str(caught.value)) < 260 and str(caught.value).startswith("setfacl failed (2): ")


def test_generated_runtime_entry_is_reinstalled_and_disabled() -> None:
    ops = FakeOperations()
    ops.entry = {"runtime": GENERATED_RUNTIME, "user": "digi-boss"}
    assert install(_plan(), ops) is True
    disable(_plan(), ops)
    assert ops.registry and not ops.policy


def test_sudo_listing_is_exact_and_refuses_unknown_syntax() -> None:
    listing = (
        "Matching Defaults entries for digi-boss on host:\n"
        "    env_reset\n"
        "User digi-boss may run the following commands on host:\n"
        "    (digi) NOPASSWD: /opt/a, /opt/b\n"
    )
    assert install_apply._parse_sudo_listing(listing) == {
        ("digi", "/opt/a"),
        ("digi", "/opt/b"),
    }
    with pytest.raises(ApplyError, match="unrecognized grant"):
        install_apply._parse_sudo_listing(listing + "    (ALL) ALL\n")


def test_verify_requires_no_sudo_of_the_agent_or_as_the_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the session host starts the agent; the base user's panel launch is gone.
    plan = _plan()
    current = {
        "digi-boss": set(),
        "digi": {("digi-dev", "/usr/local/bin/agent-launch dev")},  # another agent's line, not this one's
    }
    monkeypatch.setattr(install_apply, "_sudo_effective", lambda user: current[user])
    ops = object.__new__(install_apply.SystemOperations)
    ops._verify_sudo(plan)
    current["digi"].add(("digi-boss", "/usr/local/bin/agent-launch boss"))
    with pytest.raises(ApplyError, match="differ for digi$"):
        ops._verify_sudo(plan)
    current["digi"] = {("ALL", "/usr/bin/true")}
    with pytest.raises(ApplyError, match="ambiguous broad sudo grant of digi"):
        ops._verify_sudo(plan)
    current["digi"] = set()
    current["digi-boss"] = {("digi", "/opt/a")}
    with pytest.raises(ApplyError, match="differ for digi-boss"):
        ops._verify_sudo(plan)


@pytest.mark.parametrize("rc", [0, 1])
def test_no_sudo_rights_is_an_empty_set_whatever_the_exit_code(
    monkeypatch: pytest.MonkeyPatch, rc: int
) -> None:
    def sudo(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
        assert argv == ("sudo", "-n", "-l", "-U", "digi-boss")
        return subprocess.CompletedProcess(
            argv, rc, "User digi-boss is not allowed to run sudo on host.\n", ""
        )

    monkeypatch.setattr(install_apply.subprocess, "run", sudo)
    assert install_apply._sudo_effective("digi-boss") == set()


def test_verify_refuses_an_executor_sudo_line_beside_the_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the agent's capabilities are channels; any sudo line of the agent is an old grant.
    plan = install_apply.disable_plan(REPO, "sysadmin")
    assert "sudoers" not in plan.policy
    current: dict[str, set[tuple[str, str]]] = {"digi-sysadmin": set(), "digi": set()}
    monkeypatch.setattr(install_apply, "_sudo_effective", lambda user: current[user])
    ops = object.__new__(install_apply.SystemOperations)
    ops._verify_sudo(plan)
    current["digi-sysadmin"].add(("digi-exec", "/opt/digipaddock/agents/sysadmin/bin/ssh_exec.py"))
    with pytest.raises(ApplyError, match="effective sudo grants differ for digi-sysadmin"):
        ops._verify_sudo(plan)


def test_failed_sudo_listing_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        install_apply.subprocess,
        "run",
        lambda argv, **_: subprocess.CompletedProcess(
            argv, 1, "", "sudo: unknown user digi-boss\n"
        ),
    )
    with pytest.raises(ApplyError, match="sudo -l failed"):
        install_apply._sudo_effective("digi-boss")


def test_generated_runtime_problems_name_every_refused_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "agents.json"
    registry.write_text(
        json.dumps(
            {
                "boss": {"runtime": "generated", "user": "digi-boss"},
                "gone": {"runtime": "generated", "user": "digi-gone"},
                "ok": {"runtime": "generated", "user": "digi-ok"},
                "sysadmin": {"user": "digi"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        install_apply,
        "load_registry",
        lambda repo: SimpleNamespace(
            resources=SimpleNamespace(layout={"agents": "/srv/agents"})
        ),
    )
    monkeypatch.setattr(install_apply, "_registry_path", lambda _: str(registry))
    launched = []

    def plan_generated(name: str, folder: str, **kw: object) -> None:
        assert "channel" not in kw
        launched.append((name, folder, kw["uid"]))
        if name == "boss":
            raise install_apply.launch.LaunchError(
                "compiled policy: unsupported schema_version"
            )

    def uid_of(user: str) -> int:
        if user == "digi-gone":
            raise KeyError(f"getpwnam(): name not found: {user!r}")
        return 1001

    monkeypatch.setattr(install_apply.launch, "plan_generated", plan_generated)
    checked = []

    def spec_checker(repo: Path):
        def check(name: str, folder: str) -> list[str]:
            checked.append((name, folder))
            return ["playbooks/spec/docs.md is missing"] if name == "ok" else []

        return check

    problems = install_apply.generated_runtime_problems(
        Path("/repo"), uid_of=uid_of, fs=object(), spec_checker=spec_checker
    )
    assert problems == [
        "boss: compiled policy: unsupported schema_version",
        "gone: user digi-gone is missing",
        "ok: playbooks/spec/docs.md is missing",
    ]
    assert launched == [
        ("boss", "/srv/agents/boss", 1001),
        ("ok", "/srv/agents/ok", 1001),
    ]
    assert checked == [("ok", "/srv/agents/ok")]  # an agent the launcher refuses is reported once


def test_a_host_with_no_agent_yet_has_no_runtime_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The empty-host run: kd-release before the first agent reported «registry: unreadable file» and a
    degraded release. No agents.json is no agent; a link in its place is still refused."""
    registry = tmp_path / "agents.json"
    monkeypatch.setattr(
        install_apply,
        "load_registry",
        lambda repo: SimpleNamespace(resources=SimpleNamespace(layout={"agents": "/srv/agents"})),
    )
    monkeypatch.setattr(install_apply, "_registry_path", lambda _: str(registry))
    none = lambda repo: lambda name, folder: []  # noqa: E731
    assert install_apply.generated_runtime_problems(Path("/repo"), fs=object(), spec_checker=none) == []
    try:
        registry.symlink_to(tmp_path / "elsewhere.json")
    except OSError:
        pytest.skip("no symlinks here")
    with pytest.raises(ValueError):
        install_apply.generated_runtime_problems(Path("/repo"), fs=object(), spec_checker=none)


def test_installed_spec_checker_compares_playbooks_with_the_registry(tmp_path: Path) -> None:
    # Review minor: kd-release's postflight and --check report an agent whose installed
    # texts are not the registry's, instead of claim refusing it later.
    from kd.specializations import SPECS_ROOT, load_registry

    fixtures = Path(__file__).resolve().parent / "fixtures" / "policy" / "agents"
    repo = tmp_path / "repo"
    for name in ("dev", "boss"):
        (repo / "agents" / name).mkdir(parents=True)
        shutil.copyfile(fixtures / name / "agent.json", repo / "agents" / name / "agent.json")
    shutil.copytree(SPECS_ROOT, repo / "common" / "specializations")
    registry = load_registry(SPECS_ROOT, require_root_owner=False)
    folder = tmp_path / "srv" / "dev"
    (folder / "playbooks" / "spec").mkdir(parents=True)
    for spec in registry.specs.values():
        if spec.type == "developer":
            shutil.copyfile(SPECS_ROOT / spec.file, folder / "playbooks" / "spec" / f"{spec.name}.md")
    (folder / 'playbooks' / 'skill').mkdir()
    for skill in registry.skills.values():
        shutil.copyfile(SPECS_ROOT / skill.file, folder / 'playbooks' / 'skill' / f'{skill.name}.md')
    check = install_apply.installed_spec_checker(repo, require_root_owner=False)
    assert check("dev", str(folder)) == []
    assert check("boss", str(tmp_path / "srv" / "boss")) == []  # nothing installable, nothing to compare
    frontend = folder / "playbooks" / "skill" / "frontend.md"
    frontend.write_text("modified instruction", encoding="utf-8")
    assert check("dev", str(folder)) == [
        "playbooks/skill/frontend.md is not the registry text (new_agent.sh --install dev)"
    ]
    frontend.unlink()
    assert check("dev", str(folder)) == [
        "playbooks/skill/frontend.md is missing (new_agent.sh --install dev)"
    ]
    shutil.copyfile(SPECS_ROOT / registry.skills["frontend"].file, frontend)
    assert check("dev", str(folder)) == []
    (folder / "playbooks" / "spec" / "docs.md").write_bytes(b"# Specialization: docs (v1)\nold\n")
    (folder / "playbooks" / "spec" / "tester.md").unlink()
    assert check("dev", str(folder)) == [
        "playbooks/spec/docs.md is not the registry text (new_agent.sh --install dev)",
        "playbooks/spec/tester.md is missing (new_agent.sh --install dev)",
    ]
    with pytest.raises(ApplyError, match="specialization registry"):
        (repo / "common" / "specializations" / "specializations.json").write_text("{", encoding="utf-8")
        install_apply.installed_spec_checker(repo, require_root_owner=False)


def test_unreserved_service_user_is_refused_before_effects() -> None:
    plan = Plan(
        _plan().manifest, {}, (Action("service-user", "digi-intruder"),), (), ()
    )
    ops = object.__new__(install_apply.SystemOperations)
    with pytest.raises(ApplyError, match="not reserved"):
        ops.prepare(plan)


# --- module setup scripts (M01) ---------------------------------------------------------

REPO = Path(__file__).resolve().parents[2]


def _setup_ops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returncode: int = 0
) -> tuple:
    ops = _ops(tmp_path)
    ops.repo = REPO
    calls: list[tuple[str, ...]] = []
    options: list[dict] = []

    def run(argv, **kwargs):
        calls.append(tuple(argv))
        options.append(kwargs)
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(install_apply.subprocess, "run", run)
    ops.setup_options = options
    return ops, install_apply.disable_plan(REPO, "dev"), calls


def _scripts(calls: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    return [
        (Path(c[1]).name, *c[2:])
        for c in calls
        if c[0] == "/bin/bash" and "_setup" in c[1]
    ]


@pytest.mark.parametrize("phase", ["install", "check", "disable"])
def test_setup_scripts_run_in_lifecycle_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    ops, plan, calls = _setup_ops(tmp_path, monkeypatch)
    ops._setup(plan, phase)
    # data.read sorts before forge.review, git.develop and host.read; each runs as root with the agent and its user.
    assert _scripts(calls) == [
        ("data_read_setup.sh", phase, "dev", "digi-dev"),
        ("forge_setup.sh", phase, "dev", "digi-dev"),
        ("develop_setup.sh", phase, "dev", "digi-dev"),
        ("mirror_read_setup.sh", phase, "dev", "digi-dev"),  # after the key's git.develop
        ("host_read_setup.sh", phase, "dev", "digi-dev"),
    ]
    assert all(c[1].startswith(str(REPO)) for c in calls)


def test_quiesce_stops_the_agent_users_processes_and_no_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # no panel unit to stop or restart; the session host starts the next session.
    ops = _ops(tmp_path)
    calls: list[tuple[str, ...]] = []

    def run(argv, **kwargs):
        calls.append(tuple(argv))
        return SimpleNamespace(returncode=0 if argv[0] == "id" else 1)

    monkeypatch.setattr(install_apply.subprocess, "run", run)
    ops.quiesce(install_apply.disable_plan(REPO, "dev"))
    assert calls[:2] == [("id", "digi-dev"), ("pkill", "-TERM", "-u", "digi-dev")]
    assert not [c for c in calls if c[0] == "systemctl"]


def test_installer_grants_no_sudoers_and_enables_no_unit() -> None:
    for name in ("grant_sudoers", "enable", "_verify_units", "_move_channel"):
        assert not hasattr(install_apply.SystemOperations, name), name
    assert not hasattr(install_apply, "OWNER_CHAT") and not hasattr(install_apply, "_token_id")


def test_coordinator_runs_only_the_data_and_host_read_setups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # host.read and data.read are the coordinator's capabilities with a module setup
    ops, _, calls = _setup_ops(tmp_path, monkeypatch)
    ops._setup(install_apply.disable_plan(REPO, "boss"), "install")
    assert _scripts(calls) == [("data_read_setup.sh", "install", "boss", "digi-boss"),
                               ("host_read_setup.sh", "install", "boss", "digi-boss")]


def test_revoke_switches_module_setup_off_after_the_grants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ops, plan, calls = _setup_ops(tmp_path, monkeypatch)
    order: list[str] = []
    monkeypatch.setattr(ops, "_revoke_channels", lambda plan: order.append("channels"))
    monkeypatch.setattr(ops, "_user_script", lambda plan, mode: order.append(mode))
    monkeypatch.setattr(ops, "_other_acl", lambda plan: [("/x", "digi-dev")])
    monkeypatch.setattr(
        install_apply, "_acl_tree", lambda path, user, access: order.append("acl")
    )
    monkeypatch.setattr(ops, "_setup", lambda plan, phase: order.append(phase))
    ops.revoke(plan)
    assert order == ["channels", "--revoke", "acl", "disable"]


def _channel_ops(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple:
    """SystemOperations whose file and command effects are recorded in order."""
    ops = _ops(tmp_path)
    order: list[tuple] = []
    monkeypatch.setattr(install_apply, "_run", lambda *argv, **kw: order.append(("run", *argv)))
    monkeypatch.setattr(install_apply, "_clear_dir", lambda path, remove: order.append(("clear", path, remove)))
    monkeypatch.setattr(install_apply, "_root_parent", lambda path: None)
    monkeypatch.setattr(install_apply, "_ensure_dir", lambda path, user, mode: order.append(("dir", path)))
    monkeypatch.setattr(install_apply, "_atomic", lambda path, content, mode: order.append(("write", path, mode)))
    monkeypatch.setattr(install_apply, "_link", lambda path, target: order.append(("link", path, target)))
    monkeypatch.setattr(install_apply, "_uid_of", lambda user: 1003)
    return ops, order


def test_disable_turns_the_grants_files_off_before_the_sockets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A missing grants file is the switch; sockets and calls stop after it.
    ops, order = _channel_ops(monkeypatch, tmp_path)
    monkeypatch.setattr(
        ops, "_channel_sockets", lambda plan: ["digipaddock-cap-sysadmin-exec.socket", "digipaddock-cap-sysadmin-self.socket"]
    )
    ops._revoke_channels(install_apply.disable_plan(REPO, "sysadmin"))
    assert order == [
        ("clear", "/etc/digipaddock/cap/sysadmin", False),
        ("run", "systemctl", "disable", "--now", "digipaddock-cap-sysadmin-exec.socket", "digipaddock-cap-sysadmin-self.socket"),
        ("run", "systemctl", "stop", *(f"digipaddock-cap-sysadmin-{k}@*.service" for k in ("self", "exec", "forge", "base", "host", "data"))),
        ("clear", "/etc/digipaddock/agent-bin/sysadmin", True),
    ]


def _channel_plan(name: str) -> Plan:
    from kd import cap_units
    from kd.install_plan import GeneratedFile

    policy = install_apply.disable_plan(REPO, name).policy
    user = f"digi-{name}"
    files = [(cap_units.TARGET_PATH, (REPO / cap_units.TARGET_SOURCE).read_bytes())]
    files += cap_units.unit_files(name, user, "/opt/digipaddock", policy["channels"])
    manifest = SimpleNamespace(name=name, user=user)
    return Plan(manifest, policy, (), tuple(GeneratedFile(p, "root:root", "0644", c, False) for p, c in files), ())


def test_grant_channels_turns_the_grants_on_last(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ops, order = _channel_ops(monkeypatch, tmp_path)
    plan = _channel_plan("boss")
    ops.grant_channels(plan)
    runs = [step[1:] for step in order if step[0] == "run"]
    assert runs == [
        ("systemd-tmpfiles", "--create", "/etc/tmpfiles.d/digipaddock-cap-boss.conf"),
        ("systemctl", "daemon-reload"),
        ("systemctl", "enable", "digipaddock-cap.target", "digipaddock-cap-boss-base.socket", "digipaddock-cap-boss-data.socket",
         "digipaddock-cap-boss-host.socket", "digipaddock-cap-boss-self.socket"),
        ("systemctl", "start", "digipaddock-cap.target"),
        ("systemctl", "restart", "digipaddock-cap-boss-base.socket", "digipaddock-cap-boss-data.socket",
         "digipaddock-cap-boss-host.socket", "digipaddock-cap-boss-self.socket"),
    ]
    links = [step[1:] for step in order if step[0] == "link"]
    assert links == [
        ("/etc/digipaddock/agent-bin/boss/kd-boss-board", "/opt/digipaddock/common/bin/kd-cap"),
        ("/etc/digipaddock/agent-bin/boss/kd-data", "/opt/digipaddock/common/bin/kd-cap"),
        ("/etc/digipaddock/agent-bin/boss/kd-host-read", "/opt/digipaddock/common/bin/kd-cap"),
        ("/etc/digipaddock/agent-bin/boss/kd-monitor-read", "/opt/digipaddock/common/bin/kd-cap"),
        ("/etc/digipaddock/agent-bin/boss/kd-notes", "/opt/digipaddock/common/bin/kd-cap"),
        ("/etc/digipaddock/agent-bin/boss/kd-nudge", "/opt/digipaddock/common/bin/kd-cap"),
        ("/etc/digipaddock/agent-bin/boss/kd-task", "/opt/digipaddock/common/bin/kd-cap"),
        ("/etc/digipaddock/agent-bin/boss/kd-worker", "/opt/digipaddock/common/bin/kd-cap"),  # C07
    ]
    writes = [step[1] for step in order if step[0] == "write"]
    grants = ["/etc/digipaddock/cap/boss/base.json", "/etc/digipaddock/cap/boss/data.json",
              "/etc/digipaddock/cap/boss/host.json", "/etc/digipaddock/cap/boss/self.json"]
    assert writes[-4:] == grants
    assert order[-4:] == [("write", w, 0o644) for w in grants]  # the switch goes on last
    assert all(not w.startswith("/etc/sudoers.d") for w in writes)
    assert set(writes[:-4]) == {f.path for f in plan.files}


def test_grant_channels_refuses_a_plan_without_its_units(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ops, order = _channel_ops(monkeypatch, tmp_path)
    plan = _channel_plan("boss")
    plan = Plan(plan.manifest, plan.policy, (), plan.files[:-1], ())
    with pytest.raises(ApplyError, match="the plan lacks the channel file /etc/tmpfiles.d/digipaddock-cap-boss.conf"):
        ops.grant_channels(plan)
    assert not any(step[0] == "write" and "/cap/" in step[1] for step in order)


def test_failed_setup_script_is_an_apply_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ops, plan, _ = _setup_ops(tmp_path, monkeypatch, returncode=1)
    with pytest.raises(ApplyError, match=r"setup data.read install failed \(1\)"):
        ops._setup(plan, "install")


def test_a_setup_script_at_the_owners_step_stays_the_owners_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A module setup that exits 3 stopped at the owner's step (repo_setup.sh: the
    copies' deploy key not in the forge yet); bootstrap takes new_agent.sh's 3 for that, not for a failure."""
    ops, plan, _ = _setup_ops(tmp_path, monkeypatch, returncode=3)
    with pytest.raises(install_apply.OwnerStepError, match=r"setup data.read install: the owner's step"):
        ops._setup(plan, "install")


def test_the_owners_step_survives_the_rollback() -> None:
    class Owner(FakeOperations):
        def prepare(self, plan: Plan) -> None:
            raise install_apply.OwnerStepError("setup repo.read install: the owner's step")

    with pytest.raises(install_apply.OwnerStepError, match="install failed: OwnerStepError: setup repo.read"):
        install(_plan(), Owner())


@pytest.mark.skipif(os.name != "posix", reason="new_agent.sh runs on Linux")
def test_new_agent_exits_3_at_the_owners_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    real_open = os.open
    monkeypatch.setattr(install_apply.os, "geteuid", lambda: 0)
    monkeypatch.setattr(install_apply.os, "open", lambda path, *a: real_open(str(tmp_path / "lock"), *a))
    monkeypatch.setattr(install_apply, "build_plan", lambda *a: None)
    monkeypatch.setattr(install_apply, "SystemOperations", lambda repo: None)

    def owner(plan: object, ops: object) -> bool:
        raise install_apply.OwnerStepError("install failed: OwnerStepError: setup repo.read install: the owner's step")

    monkeypatch.setattr(install_apply, "install", owner)
    assert install_apply.main(["--install", "analyst"]) == 3
    assert "the owner's step" in capsys.readouterr().err


# --- final review fixes (M01) -----------------------------------------------------------


def test_setup_scripts_show_their_output_to_the_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The Forgejo key, token hints, the selfcheck warning and check reasons reach the terminal.
    ops, plan, _ = _setup_ops(tmp_path, monkeypatch)
    ops._setup(plan, "check")
    assert ops.setup_options
    for kwargs in ops.setup_options:
        assert not {"capture_output", "stdout", "stderr"} & kwargs.keys()


def test_install_failure_names_the_reason() -> None:
    ops = FakeOperations("prepare")
    with pytest.raises(ApplyError, match="install failed: OSError: injected failure"):
        install(_plan(), ops)


# --- Task 9 (stage): every agent gets work/tmp/ for its attachments ----------------------


def _run_user_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ops = _ops(tmp_path)
    folder, home = tmp_path / "srv/agent", tmp_path / "home"
    (folder / "memory").mkdir(parents=True)
    (folder / "work").mkdir()
    home.mkdir()
    monkeypatch.setattr(ops, "_folder", lambda plan: str(folder))
    seen: list[tuple] = []

    def run(*argv, **kwargs):
        seen.append(argv)
        if argv[6] != "-c":  # the installed kd.workspace, run from this tree
            script = Path(install_apply.__file__).with_name("workspace.py")
            assert argv[6].endswith("/common/kd/workspace.py") and argv[7] == "migrate", argv
            proc = subprocess.run([sys.executable, "-I", str(script), *argv[7:]], capture_output=True, text=True)
            if proc.returncode:
                raise ApplyError(proc.stderr.strip().splitlines()[-1])
            return
        code, args = argv[7], argv[8:]
        args = (args[0], str(home), *args[2:])
        proc = subprocess.run([sys.executable, "-I", "-c", code, *args], capture_output=True, text=True)
        if proc.returncode:
            raise ApplyError(proc.stderr.strip().splitlines()[-1])

    monkeypatch.setattr(install_apply, "_run", run)
    return ops, folder, seen


def test_user_data_makes_work_tmp_as_the_agent_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ops, folder, seen = _run_user_data(tmp_path, monkeypatch)
    ops._prepare_user_data(_plan())
    assert (folder / "work/tmp").is_dir()
    assert seen[0][:4] == ("runuser", "-u", "digi-boss", "--")


def test_user_data_splits_the_notes_once_as_the_agent_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # the session folders and the one-time split run as the agent, after its own helper
    ops, folder, seen = _run_user_data(tmp_path, monkeypatch)
    (folder / "memory/notes.md").write_text("- 2026-11-01: KD-5 merged\n- Pitfall: x\n", encoding="utf-8")
    ops._prepare_user_data(_plan())
    assert seen[1][:4] == ("runuser", "-u", "digi-boss", "--") and seen[1][-2:] == ("migrate", str(folder))
    for sub in ("memory/tasks", "memory/chats", "memory/legacy", "work/tmp/cards", "work/tmp/chats"):
        assert (folder / sub).is_dir(), sub
    assert (folder / "memory/tasks/KD-5.md").read_text(encoding="utf-8") == "- 2026-11-01: KD-5 merged\n"
    first = (folder / "memory/notes.md").read_bytes()
    ops._prepare_user_data(_plan())  # a reinstall: the map is there, nothing moves again
    assert (folder / "memory/notes.md").read_bytes() == first
    assert len(list((folder / "memory/legacy").glob("notes-*.md"))) == 1


def test_user_data_writes_nothing_as_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # no owner chat file and no Telegram access list; the user's own helper does the rest.
    ops, _, _ = _run_user_data(tmp_path, monkeypatch)
    written: list[tuple] = []
    monkeypatch.setattr(install_apply, "_atomic", lambda *args, **kw: written.append(args))
    ops._prepare_user_data(_plan())
    assert written == []


def test_install_check_reads_no_channel_or_owner_chat() -> None:
    source = Path(install_apply.__file__).read_text(encoding="utf-8")
    for helper in ('user_check = """', 'helper = """'):
        code = source.split(helper, 1)[1].split('"""', 1)[0]
        code = code.replace("'memory/chats'", "").replace("'work/tmp/chats'", "")  # the sessions' notes
        assert "channel" not in code and "chat" not in code and "telegram" not in code.lower()


@pytest.mark.skipif(os.name != "posix", reason="symlinks")
def test_user_data_refuses_a_linked_work_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ops, folder, _ = _run_user_data(tmp_path, monkeypatch)
    (folder / "work/tmp").symlink_to(tmp_path)
    with pytest.raises(ApplyError, match="work/tmp is a symlink"):
        ops._prepare_user_data(_plan())
