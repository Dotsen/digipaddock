"""Contracts for repository agent manifests before runtime installation exists."""

import json
import os
import re
from pathlib import Path

import pytest

from kd import naming
from kd.agents import USER_RE
from kd.manifest import (
    SERVICE_USERS,
    AgentManifest,
    ManifestError,
    check_identity,
    check_model,
    load_manifest,
    validate_all,
)
from kd.registry import read_json_file

ROOT = Path(__file__).resolve().parents[2]


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _agent(name: str = "boss", **changes: object) -> dict:
    data = {
        "schema_version": 2,
        "name": name,
        "model": "sonnet",
        "profile": "coordinator",
        "capabilities": ["board.read", "board.coordinate", "agent.nudge"],
        "runtime": {"sandbox_profile": "coordinator-v1", "working_directory": "work"},
    }
    data.update(changes)
    return data


@pytest.fixture
def repo(registry_root: Path) -> Path:
    # The tracked registries (kd.registry validates them inside validate_all) plus two agents.
    root = registry_root
    _write(root / "agents/boss/agent.json", _agent())
    _write(
        root / "agents/dev/agent.json",
        _agent(
            "dev",
            model="claude-opus-4-8",
            profile="developer",
            capabilities=["git.develop"],
            runtime={"sandbox_profile": "developer-v1", "working_directory": "work"},
        ),
    )
    return root


def test_load_manifest_normalizes_one_agent(repo: Path) -> None:
    manifest = load_manifest(repo / "agents/boss/agent.json")
    assert manifest == AgentManifest(
        name="boss",
        user=naming.agent_user("boss"),
        model="sonnet",
        profile="coordinator",
        capabilities=("board.read", "board.coordinate", "agent.nudge"),
        sandbox_profile="coordinator-v1",
        working_directory="work",
    )
    assert not hasattr(manifest, "telegram_bot")


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": True},
        {"schema_version": 3},
        {"name": "Boss"},
        {"name": "other"},
        {"model": "sonnet --danger"},
        {"model": "unknown"},
        {"profile": "../coordinator"},
        {"capabilities": ["board.read", "board.read"]},
        {"capabilities": ["board.*"]},
        {"capabilities": ["/etc/passwd"]},
        {
            "runtime": {
                "sandbox_profile": "coordinator-v1",
                "working_directory": "../home",
            }
        },
        {"runtime": {"sandbox_profile": "/tmp/open", "working_directory": "work"}},
        {
            "runtime": {
                "sandbox_profile": "coordinator-v1",
                "working_directory": "work",
                "network": "all",
            }
        },
        {"unexpected": True},
    ],
)
def test_load_manifest_rejects_bad_schema(repo: Path, change: dict) -> None:
    path = repo / "agents/boss/agent.json"
    _write(path, _agent(**change))
    with pytest.raises(ValueError):
        load_manifest(path)


@pytest.mark.parametrize(
    "missing", ["schema_version", "name", "profile", "capabilities", "runtime"]
)
def test_required_field_cannot_be_missing(repo: Path, missing: str) -> None:
    path = repo / "agents/boss/agent.json"
    value = _agent()
    del value[missing]
    _write(path, value)
    with pytest.raises(ValueError):
        load_manifest(path)


def test_optional_model_is_absent_without_new_rights(repo: Path) -> None:
    path = repo / "agents/boss/agent.json"
    _write(path, _agent(model=""))
    assert load_manifest(path).model is None


def test_optional_effort_is_a_claude_code_level(repo: Path) -> None:
    # absent — Claude Code's default; set — one of its levels, never free text.
    path = repo / "agents/boss/agent.json"
    assert load_manifest(path).effort is None
    for effort in ("low", "medium", "high", "xhigh", "max"):
        _write(path, _agent(effort=effort))
        assert load_manifest(path).effort == effort
    for bad in ("", "High", "ultra", "high --verbose", 2, None, ["high"]):
        _write(path, _agent(effort=bad))
        with pytest.raises(ManifestError, match="invalid effort"):
            load_manifest(path)


def test_optional_thread_models_name_a_known_thread_and_model(repo: Path) -> None:
    # absent — every session on the agent's model; set — "Monitor" on its own, nothing else accepted.
    path = repo / "agents/boss/agent.json"
    assert load_manifest(path).thread_models == ()
    _write(path, _agent(threads={"monitor": {"model": "claude-sonnet-5-5"}}))
    assert load_manifest(path).thread_models == (("monitor", "claude-sonnet-5-5"),)
    for bad in ({}, {"chat": {"model": "sonnet"}}, {"monitor": {"model": "--x"}}, {"monitor": "sonnet"},
                {"monitor": {"model": "sonnet", "effort": "low"}}, ["monitor"], None):
        _write(path, _agent(threads=bad))
        with pytest.raises(ManifestError, match="threads"):
            load_manifest(path)


@pytest.mark.parametrize("telegram", [{"bot": "@good_bot"}, None])
def test_telegram_field_is_unknown(repo: Path, telegram: object) -> None:
    # agents talk only in the cabinet; a manifest that still names a bot fails closed.
    path = repo / "agents/boss/agent.json"
    _write(path, _agent(telegram=telegram))
    with pytest.raises(ManifestError, match="invalid manifest fields"):
        load_manifest(path)


def test_duplicate_json_keys_invalid_utf8_and_large_file_fail(repo: Path) -> None:
    path = repo / "agents/boss/agent.json"
    path.write_text('{"schema_version": 1, "schema_version": 1}', encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSON"):
        load_manifest(path)
    path.write_bytes(b"\xff")
    with pytest.raises(ValueError, match="invalid JSON"):
        load_manifest(path)
    path.write_bytes(b"{" + b" " * (64 * 1024))
    with pytest.raises(ValueError, match="bounded regular file"):
        load_manifest(path)


def test_bytes_after_ctrl_z_are_read_not_dropped(repo: Path) -> None:
    # Windows text-mode descriptors stop at Ctrl-Z and fold CRLF; the reader opens O_BINARY,
    # so trailing garbage a reviewer cannot see in an editor still fails the parse.
    path = repo / "agents/boss/agent.json"
    path.write_bytes(json.dumps(_agent()).encode("utf-8") + b"\x1a{}")
    with pytest.raises(ManifestError, match="invalid JSON"):
        load_manifest(path)


def test_manifest_error_stays_a_value_error() -> None:
    assert issubclass(ManifestError, ValueError)


def test_every_installed_service_account_is_reserved(repo: Path) -> None:
    # A new executor account in an installer or in the capability registry must be reserved
    # in the same change; otherwise a manifest could claim its uid and its keys.
    scripts = [
        *(ROOT / "common/install").rglob("*.sh"),
        *(ROOT / "agents").glob("*/install/*.sh"),
    ]
    created = set()
    for script in scripts:
        for line in script.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("#") or not re.search(r"\b(?:useradd|adduser)\b", line):
                continue
            if script.name == "stage_setup.sh":
                # the base user, named by naming.sh: not a service account
                assert line.rstrip().endswith('useradd --create-home --shell /bin/bash "$base"'), line
                continue
            if script.name == "cabinet_setup.sh":
                # the board service user and the push sender (global review 15.10.2026), named by
                # naming.sh: cabinet=$(kd_user cabinet), push=$(kd_user push), created in one loop over "$u"
                assert line.rstrip().endswith('--shell /usr/sbin/nologin "$u"'), line
                text = script.read_text(encoding="utf-8")
                assert 'cabinet=$(kd_user cabinet)' in text and 'push=$(kd_user push)' in text
                assert 'for u in "$cabinet" "$push"; do' in text
                created.add(naming.service_user("cabinet"))
                created.add(naming.service_user("push"))
                continue
            if script.name == "review_setup.sh":
                # the approver of the owner's signed PR approvals, named by naming.sh: review=$(kd_user
                # review)
                assert line.rstrip().endswith('--shell /usr/sbin/nologin "$review"'), line
                assert 'review=$(kd_user review)' in script.read_text(encoding="utf-8")
                created.add(naming.service_user("review"))
                continue
            if script.name == "transcribe_setup.sh":
                # the one-shot voice transcription, named by naming.sh: transcribe=$(kd_user transcribe)
                assert line.rstrip().endswith('--shell /usr/sbin/nologin "$transcribe"'), line
                assert 'transcribe=$(kd_user transcribe)' in script.read_text(encoding="utf-8")
                created.add(naming.service_user("transcribe"))
                continue
            if script.name == "devcheck_setup.sh":
                # the dev.check tests of a candidate, named by naming.sh: ci=$(kd_user ci)
                assert line.rstrip().endswith('--shell /usr/sbin/nologin "$ci"'), line
                assert 'ci=$(kd_user ci)' in script.read_text(encoding="utf-8")
                created.add(naming.service_user("ci"))
                continue
            if script.name == "repo_setup.sh":
                # the repository service user, named by naming.sh: repo=$(kd_user repo)
                assert line.rstrip().endswith('--shell /usr/sbin/nologin "$repo"'), line
                assert 'repo=$(kd_user repo)' in script.read_text(encoding="utf-8")
                created.add(naming.service_user("repo"))
                continue
            if script.name != "add_user.sh":
                assert not re.search(r"\b(?:useradd|adduser)\b[^#]*\$", line), line
            created.update(re.findall(r"\bagent-[a-z0-9-]+\b", line))
    executors = {
        wrapper["run_as"]
        for entry in (ROOT / "common/agent-capabilities").glob("*.json")
        for wrapper in read_json_file(entry)["wrappers"]
    } - {"self", naming.base_user()}
    # agent-ro comes from target_setup.sh; the executor accounts from the generated install
    # (kd.install_apply) of the capability that runs as them.
    assert created | executors == SERVICE_USERS
    # No agent name derives a service user: the roles are reserved agent names.
    for role in naming.SERVICE_ROLES:
        with pytest.raises(ManifestError):
            check_identity(role, naming.service_user(role), None)


def test_validate_all_cross_references_without_writes(repo: Path) -> None:
    before = {
        str(path.relative_to(repo)): path.read_bytes()
        for path in repo.rglob("*")
        if path.is_file()
    }
    manifests = validate_all(repo)
    after = {
        str(path.relative_to(repo)): path.read_bytes()
        for path in repo.rglob("*")
        if path.is_file()
    }
    assert set(manifests) == {"boss", "dev"}
    assert manifests["boss"].capabilities == (
        "board.read",
        "board.coordinate",
        "agent.nudge",
    )
    assert before == after


@pytest.mark.parametrize(
    "resource",
    [
        "common/agent-profiles/coordinator.json",
        "common/agent-sandboxes/coordinator-v1.json",
        "common/agent-capabilities/board.read.json",
    ],
)
def test_missing_registered_resource_fails(repo: Path, resource: str) -> None:
    (repo / resource).unlink()
    with pytest.raises(ValueError):
        validate_all(repo)


def test_duplicate_coordinator_fails(repo: Path) -> None:
    _write(repo / "agents/helper/agent.json", _agent("helper"))
    with pytest.raises(ValueError, match="multiple coordinators"):
        validate_all(repo)


def test_capability_dependencies_and_profile_fail(repo: Path) -> None:
    path = repo / "agents/dev/agent.json"
    developer = {"sandbox_profile": "developer-v1", "working_directory": "work"}
    _write(
        path,
        _agent(
            "dev",
            profile="developer",
            capabilities=["board.write"],
            runtime=developer,
        ),
    )
    with pytest.raises(ManifestError, match="missing capability dependency"):
        validate_all(repo)
    _write(
        path,
        _agent(
            "dev",
            profile="developer",
            capabilities=["git.develop", "servers.read"],
            runtime=developer,
        ),
    )
    with pytest.raises(ManifestError, match="outside the developer profile ceiling"):
        validate_all(repo)
    _write(
        path,
        _agent(
            "dev",
            profile="developer",
            capabilities=["git.develop"],
        ),
    )
    with pytest.raises(ManifestError, match="not allowed for profile developer"):
        validate_all(repo)
    path = repo / "agents/boss/agent.json"
    _write(path, _agent(capabilities=["board.read", "board.coordinate", "git.develop"]))
    with pytest.raises(ValueError, match="coordinator capability conflict"):
        validate_all(repo)
    _write(
        path,
        _agent(profile="developer", capabilities=["board.read", "board.coordinate"]),
    )
    with pytest.raises(ValueError, match="board.coordinate requires"):
        validate_all(repo)


@pytest.mark.parametrize(
    "capability", ["git.develop", "board.write", "monitor.manage", "servers.read"]
)
def test_coordinator_refuses_executor_capabilities(repo: Path, capability: str) -> None:
    # Even with an empty registry entry (no declared conflict) BOSS never gets executor rights.
    _write(repo / f"common/agent-capabilities/{capability}.json", {})
    _write(
        repo / "agents/boss/agent.json",
        _agent(capabilities=["board.read", "board.coordinate", capability]),
    )
    with pytest.raises(ValueError, match="coordinator capability conflict"):
        validate_all(repo)


def test_coordinator_may_read_the_monitor_only(repo: Path) -> None:
    # monitor.read is a read of the cabinet's incidents; monitor.manage stays refused (above)
    _write(
        repo / "agents/boss/agent.json",
        _agent(capabilities=["board.read", "board.coordinate", "monitor.read"]),
    )
    validate_all(repo)


def test_unknown_capability_and_malformed_registry_fail(repo: Path) -> None:
    path = repo / "agents/boss/agent.json"
    _write(
        path, _agent(capabilities=["board.read", "board.coordinate", "unknown.effect"])
    )
    with pytest.raises(ValueError):
        validate_all(repo)
    _write(path, _agent())
    entry = repo / "common/agent-capabilities/board.read.json"
    data = json.loads(entry.read_text(encoding="utf-8"))
    data["dependencies"] = "agent.nudge"
    _write(entry, data)
    with pytest.raises(ManifestError, match="dependencies: must be a list"):
        validate_all(repo)


def test_manifests_validate_without_any_routes_file(repo: Path) -> None:
    # the board's projects are the cabinet's table; the installer checks their routes (install_plan)
    assert not (repo / "common/coordination/routes.json").exists()
    assert set(validate_all(repo)) == {"boss", "dev"}


def test_zero_manifests_is_valid_before_migration(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    (root / "agents/sysadmin").mkdir(parents=True)
    assert validate_all(root) == {}


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs Windows privileges")
def test_manifest_and_registry_symlinks_are_rejected(
    repo: Path, tmp_path: Path
) -> None:
    manifest = repo / "agents/boss/agent.json"
    target = tmp_path / "outside.json"
    _write(target, _agent())
    manifest.unlink()
    manifest.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        load_manifest(manifest)
    manifest.unlink()
    _write(manifest, _agent())
    entry = repo / "common/agent-capabilities/board.read.json"
    entry.unlink()
    entry.symlink_to(target)
    with pytest.raises(ValueError, match="only regular .json"):
        validate_all(repo)


@pytest.mark.parametrize("name", ["boss", "b2", "a" * 23])
def test_accepted_users_fit_the_runtime_registry(repo: Path, name: str) -> None:
    # The installer registers the same uid in agents.json; a manifest must not accept wider names.
    path = repo / f"agents/{name}/agent.json"
    _write(path, _agent(name))
    user = load_manifest(path).user
    assert user == naming.agent_user(name) and USER_RE.fullmatch(user)


@pytest.mark.parametrize("name", ["exec", "forge", "ro", "worker", "a" * 24])
def test_agent_names_that_cannot_own_a_user_are_refused(repo: Path, name: str) -> None:
    path = repo / f"agents/{name}/agent.json"
    _write(path, _agent(name))
    with pytest.raises(ManifestError):
        load_manifest(path)


@pytest.mark.parametrize(
    ("change", "why"),
    [({"user": "digi-boss"}, "fields"), ({"schema_version": 1}, "schema")],
)
def test_a_manifest_naming_its_user_or_of_schema_1_is_refused(
    repo: Path, change: dict, why: str
) -> None:
    path = repo / "agents/boss/agent.json"
    _write(path, _agent(**change))
    with pytest.raises(ManifestError, match=why):
        load_manifest(path)


def test_the_user_is_derived_from_the_name(repo: Path) -> None:
    assert load_manifest(repo / "agents/boss/agent.json").user == naming.agent_user("boss")


def test_check_identity_requires_the_derived_user() -> None:
    check_identity("dev", naming.agent_user("dev"), None)
    for user in (
        naming.base_user(),
        naming.agent_user("boss"),
        "root",
        naming.service_user("exec"),
    ):
        with pytest.raises(ManifestError):
            check_identity("dev", user, None)


@pytest.mark.parametrize("model", ["fable", "claude-fable-5-2", "opus", "claude-sonnet-5-5"])
def test_the_fable_family_is_a_model_like_the_others(model: str) -> None:  # an alias of the subscription
    assert check_model(model) == model
