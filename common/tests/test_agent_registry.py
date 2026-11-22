"""Registry consistency (BOSS spec sections 4.2 and 5.1-5.2b), table-driven over kd.registry.

kd.registry is the one validator of the agent registries; these tests feed it the tracked
data and broken copies of it, so a green run is not a vacuous pass. Two kinds of checks
stay here because they pin repository facts rather than registry rules: no routes file (the
board's projects are the cabinet's) and the policy baseline (the registry may not grant sysadmin and dev more
than their generated policy of 29.10.2026, policy_baseline). Manifest fields are validated by kd.manifest (A01), not here.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import re
from pathlib import Path
from typing import Any, Callable

import policy_baseline
import pytest

from kd import naming
from kd.manifest import load_manifest
from kd.policy import PolicyError, compile_policy
from kd.registry import (
    CAPABILITIES,
    CAPABILITY_MODULES,
    FAIL_CLOSED_SANDBOX,
    MAX_JSON_BYTES,
    PROFILES,
    REGISTRY_PATHS,
    RESOURCES,
    SANDBOXES,
    Entry,
    Registry,
    RegistryError,
    board_routes,
    capability_union,
    load_registry,
    read_json_file,
    read_json_object,
    validate_registry,
    validate_routes,
    validate_selection,
)

ROOT = Path(__file__).resolve().parents[2]
MANAGED_SETTINGS = "common/claude/managed-settings.template.json"

# Spec section 5.2: the finite capability table; the module comes from section 5.1.
SPEC_CAPABILITIES = {
    "net.egress": "net",
    "board.read": "board",
    "board.write": "board",
    "board.coordinate": "coordinator",
    "agent.nudge": "coordinator",
    "usage.read": "usage",
    "git.develop": "forge",
    "forge.review": "forge",
    "git.mirror": "forge",
    "servers.read": "servers",
    "servers.execute": "servers",
    "monitor.manage": "servers",
    "docs.read": "docs",  # M02 amendment
    "repo.read": "repo",  # read-only checkouts
    "worker.request": "worker",  # C07: worker jobs through the base-user executor kd-worker
    "diag.request": "diag",  # R31: catalog health probes through the base-user executor kd-diag
    "factory.request": "factory",  # the factory catalog's operations through kd-factory
    "host.read": "host",  # the platform host through the host reader kd-host-read
    "e2e.request": "e2e",  # fixed prepared smoke suite
    "monitor.read": "monitor",  # the monitor's incidents and servers through kd-monitor-read
    "data.read": "data",  # read-only SQL on the servers' product databases through kd-data
}
# net.egress is every agent's — it names no host itself, the launch adds the host's standing exit.
SPEC_BOSS_CAPABILITIES = {"board.read", "board.coordinate", "agent.nudge", "data.read", "host.read", "monitor.read",
                          "net.egress", "usage.read", "worker.request"}
# Spec section 4.6: no generic edit, status choice, title, description or AC.
SPEC_BOSS_BOARD = (
    "comment",
    "assign",
    "set-project",
    "set-priority",
    "set-labels",
    "set-dependency",
    "create-inbox",
    "unblock",
    "attention-list",  # spec 4.6: the automaton's events, read and ack through the same executor
    "attention-ack",
    "assist-list",  # the leads' help requests, and who does them
    "assist-assign",
    "revision",  # read only: the SHA-256 the mutations require
    "specs",  # read only: the installed specializations per agent
    "propose-promote",  # the owner's page moves the card, BOSS only proposes
    "propose-delegate",  # the owner's grant to split a goal, asked on the same page
    "create-child",  # a subtask of a delegated goal, created and started only under that grant
    "finish",  # how BOSS's own turn on its own card ends (kd-work finish of the others)
    "prepare-create",  # the look before a create (create-inbox needs its token)
    "request-brief",  # an Inbox card to the analyst, the card stays in Inbox
    "close-duplicate",  # an unstarted Inbox duplicate closed without the work
    "close-cancelled",  # on the owner's own word, or BOSS's own card made in error
    "review",  # checked work to the owner's acceptance, never Done
    "stage-pass",  # a handed-in stage of a chain plan with a ready code review: Done, the next one starts
    "stage-return",  # back to its agent on that review's verdict, twice a stage at most
    "stage-hold",  # left to the owner with a reason
    "defer",  # a card put off until a moment, with a reason the owner is told
    "council",  # read only — a card's council (the writer's kd-work council needs board.write)
)
# Modules whose executors run as their own service users since before the manifests (digi-exec, digi-forge); any
# other module may sudo only to the base user.
LEGACY_MODULES = frozenset({"docs", "forge", "servers"})
# A new module whose executor has its own service user, reviewed with its spec: the host reader, the one
# platform user with the journal ; the data user, the one holder of the data key.
NEW_EXECUTORS = {"host": "digi-observe", "data": "digi-data"}
SYSADMIN_V1_CHANNELS = {"kd-admin-critical", "kd-admin-propose", "kd-admin-propose-action", "kd-admin-run", "kd-diag", "kd-host-read", "kd-monitor", "kd-notes", "kd-task", "kd-work", "ssh-exec", "ssh-ro"}
# The owner's consent for these moved from a Claude Code card into the
# executor (the task grant for ssh-exec, the Forgejo Approve for kd-merge).
CONSENT_IN_EXECUTOR = frozenset({"kd-merge", "ssh-exec"})


def _bash_rules(rules: list[str]) -> list[str]:
    return [
        rule[5:-1] for rule in rules if rule.startswith("Bash(") and rule.endswith(")")
    ]


def _matches(rule: str, argv: str) -> bool:
    # Claude Code Bash rules: a trailing * is a prefix match, anything else is exact.
    return argv.startswith(rule[:-1]) if rule.endswith("*") else argv == rule


def _samples(wrapper: Entry) -> list[str]:
    command = wrapper["command"]
    return [f"{command} {sub} x" for sub in wrapper["subcommands"]] or [f"{command} x"]


def _hits(rules: list[str], wrapper: Entry) -> bool:
    return any(_matches(rule, argv) for rule in rules for argv in _samples(wrapper))


def _read(repo_root: Path, relative: str) -> Entry:
    return read_json_object(repo_root / relative)


def _snapshot(name: str) -> Entry:
    return _read(ROOT, f"common/tests/fixtures/policy/{name}.policy.json")


def _baseline_errors(registry: Registry, repo_root: Path = ROOT) -> list[str]:
    """Registry data must not grant more than the floor and the policy baseline (policy_baseline) already do."""
    floor = read_json_file(repo_root / MANAGED_SETTINGS)
    floor_deny = _bash_rules(floor["permissions"]["deny"])
    floor_ask = _bash_rules(floor["permissions"]["ask"])
    frozen = policy_baseline.load()["agents"]
    frozen_allow = [rule for name in frozen for rule in _bash_rules(frozen[name]["view"]["allow"])]
    errors = []
    for name, entry in sorted(registry.capabilities.items()):
        label = f"capability {name}"
        for wrapper in entry["wrappers"]:
            command = wrapper["command"]
            if _hits(floor_deny, wrapper):
                errors.append(f"{label}: {command} is denied by the managed floor")
            if _hits(floor_ask, wrapper) and wrapper["approval"] != "ask" and command not in CONSENT_IN_EXECUTOR:
                errors.append(f"{label}: {command} must keep the managed ask")
            if wrapper["source"] == "platform" and not all(
                any(_matches(rule, argv) for rule in frozen_allow) for argv in _samples(wrapper)
            ):
                errors.append(f"{label}: {command} forms exceed the baseline allow rules")
            if wrapper["run_as"] not in ("self", "digi") and entry["module"] not in LEGACY_MODULES and (
                NEW_EXECUTORS.get(entry["module"]) != wrapper["run_as"]
            ):
                errors.append(f"{label}: a new module may sudo only to digi")
    # The widest agent each baseline profile allows: its whole ceiling, in each of its sandboxes.
    manifests = {name: load_manifest(repo_root / "agents" / name / "agent.json") for name in policy_baseline.AGENTS}
    for name, manifest in manifests.items():
        label = f"profile {manifest.profile}"
        profile = registry.profiles.get(manifest.profile)
        if profile is None:
            errors.append(f"{label}: missing baseline profile")
            continue
        for sandbox in profile["sandbox_profiles"]:
            widest = dataclasses.replace(
                manifest, capabilities=tuple(profile["capability_ceiling"]), sandbox_profile=sandbox
            )
            try:
                policy = compile_policy(widest, registry, floor=floor, agents={**manifests, name: widest})
            except PolicyError as exc:
                errors += [f"{label}: {error}" for error in exc.errors]
                continue
            listed = policy_baseline.load()["widenings"].get(name, {})  # reviewed, each with its reason
            errors += [
                f"{label}: {item} widens the policy baseline"
                for item in policy_baseline.widenings(name, policy, floor) if item not in listed
            ]
    return errors


def _repo_agents() -> list[str]:
    return [path.name for path in (ROOT / "agents").iterdir() if path.is_dir()]


def registry_errors(data_root: Path) -> list[str]:
    """Every error kd.registry reports for data_root, then the baseline pins."""
    try:
        registry = load_registry(data_root)
        validate_registry(registry)
    except RegistryError as exc:
        return list(exc.errors)
    return _baseline_errors(registry)


def _tracked() -> Registry:
    return load_registry(ROOT)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def test_tracked_registries_are_consistent() -> None:
    assert registry_errors(ROOT) == []


def test_host_values_decide_how_the_network_domains_sort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a host value that sorted before another refused the whole registry on the host. Since
    # review 04.11.2026 the developer's sandbox reaches the forge only: the mirror's host is the executor's
    host = json.loads((ROOT / "common/tests/fixtures/host/host.json").read_text("utf-8"))
    host.update(forge_host="zz.example.test", mirror_remote="git@aa.example.test:example/digipaddock.git")
    path = tmp_path / "host.json"
    path.write_text(json.dumps(host), encoding="utf-8")
    monkeypatch.setenv("KD_HOST_JSON", str(path))
    ceiling = _tracked().sandboxes["developer-v1"]["ceiling"]  # loads and validates; the baseline is the example's
    assert ceiling["network_domains"] == ["zz.example.test"]


def test_capability_table_matches_spec() -> None:
    capabilities = _tracked().capabilities
    assert CAPABILITY_MODULES == SPEC_CAPABILITIES
    assert {n: e["module"] for n, e in capabilities.items()} == SPEC_CAPABILITIES


class _Cabinet:
    """The cabinet's answer to project.list, or its absence (``items`` None: the socket is not there)."""

    def __init__(self, items):
        self.items, self.calls = items, []

    def call(self, request):
        from kd.cabinet import client

        self.calls.append(request)
        if self.items is None:
            raise client.CabinetError("unavailable", "board unavailable (FileNotFoundError)")
        return {"projects": {p["id"]: p["route"] for p in self.items}, "items": self.items}


def _items(**routes) -> list:
    return [{"id": p, "name": p, "route": r, "repo": ""} for p, r in routes.items()]


def test_no_routes_file_in_the_repository() -> None:
    # the board's projects are the cabinet's table; the registry reads no routes file
    assert not (ROOT / "common/coordination/routes.json").exists()
    assert not any("routes" in path for path in REGISTRY_PATHS)
    assert "board-routes" not in read_json_object(ROOT / RESOURCES)["resources"]


def test_board_routes_come_from_the_cabinet() -> None:
    cabinet = _Cabinet(_items(digipaddock="@dev", admin="@sysadmin", alert="@owner"))
    assert board_routes(cabinet) == {"digipaddock": "@dev", "admin": "@sysadmin", "alert": "@owner"}
    assert cabinet.calls == [{"op": "project.list"}]
    validate_routes(board_routes(cabinet), _repo_agents())


def test_board_routes_fail_loudly_without_the_cabinet() -> None:
    # AC3: no cabinet is an explicit error naming it, never an empty list of projects
    with pytest.raises(RegistryError, match="the cabinet did not answer"):
        board_routes(_Cabinet(None))


@pytest.mark.parametrize(
    "items, needle",
    [
        (_items(digipaddock="owner"), "route target for digipaddock must be an assignee"),
        (_items(**{"Prod Line": "@owner"}), "invalid project"),
        ([{"id": "digipaddock", "name": "h", "route": ["@dev"], "repo": ""}], "must be an assignee"),
    ],
)
def test_board_routes_refuse_a_malformed_answer(items, needle) -> None:
    with pytest.raises(RegistryError, match=needle):
        board_routes(_Cabinet(items))


def test_validate_routes_refuses_a_route_to_an_unknown_agent() -> None:
    validate_routes({"digipaddock": "@dev", "alert": "@owner"}, ["dev"])
    with pytest.raises(RegistryError, match="project digipaddock routes to unknown agent @ghost"):
        validate_routes({"digipaddock": "@ghost"}, _repo_agents())


def test_profile_defaults_grant_no_rights() -> None:
    capabilities = _tracked().capabilities
    commands = {w["command"] for e in capabilities.values() for w in e["wrappers"]}
    for name, profile in _tracked().profiles.items():
        assert set(profile["defaults"]) == {"role"}, name
        words = set(re.findall(r"[a-z][a-z0-9.-]*", profile["defaults"]["role"]))
        assert not words & (set(capabilities) | commands), name


def test_coordinator_profile_matches_boss_manifest_example() -> None:
    profile = read_json_object(ROOT / PROFILES / "coordinator.json")
    assert set(profile["capability_ceiling"]) == SPEC_BOSS_CAPABILITIES
    assert profile["sandbox_profiles"] == ["coordinator-v1"]
    sandbox = read_json_object(ROOT / SANDBOXES / "coordinator-v1.json")
    assert sandbox["ceiling"]["network_domains"] == []


def test_boss_capabilities_add_no_network_secret_or_raw_write() -> None:
    capabilities = _tracked().capabilities
    for name in sorted(SPEC_BOSS_CAPABILITIES):
        entry = capabilities[name]
        assert entry["shell"] == "none", name
        assert entry["sandbox_ceiling"]["network_domains"] == [], name
        # the host reader and the data user are the service users a boss wrapper reaches besides
        # the base user
        assert {w["run_as"] for w in entry["wrappers"]} <= {"self", "digi", "digi-observe", "digi-data"}, name
        table = _tracked().resources.resources
        assert not [r for r in entry["resources"] if table[r["name"]].secret], name
        assert "write-grant" not in {r["access"] for r in entry["resources"]}, name
    coordinate = capabilities["board.coordinate"]
    assert {r["access"] for r in coordinate["resources"]} == {"via-wrapper"}
    assert "read-grants" not in coordinate["lifecycle"]["install"]
    assert {"board.write", "monitor.manage"} <= set(coordinate["conflicts"])
    (wrapper,) = coordinate["wrappers"]
    assert (wrapper["command"], tuple(wrapper["subcommands"])) == ("kd-boss-board", SPEC_BOSS_BOARD)
    assert wrapper["approval"] == "allow"
    # The one status move out of Inbox is the owner's decision on its page;
    # BOSS proposes it, the actual promote is never one of its forms.
    assert "promote" not in wrapper["subcommands"] and "propose-promote" in wrapper["subcommands"]
    # the board is read through the base executor; it writes only for a board.write manifest.
    reader, notes = capabilities["board.read"]["wrappers"]
    # kd-notes add — the agent's own shared notes, as the agent itself, nothing of the board
    assert (notes["command"], notes["subcommands"], notes["run_as"], notes["executor"]) == (
        "kd-notes", ["add"], "self", None)
    assert (reader["command"], set(reader["subcommands"]), reader["run_as"]) == (
        "kd-task",
        {"view", "list", "search", "scope", "result", "message", "thread", "waits", "projects", "chat", "file",
         "claim", "create", "edit", "handoff", "brief-review"},
        "digi",
    )


def test_developer_sandbox_is_developer_v1() -> None:
    """nothing leaves the developer's sandbox; its tools reach channels.

    kd-task comes from board.read (its executor runs as the base user), kd-forge
    and kd-merge from forge.review (their executor runs as digi-forge).
    """
    launch = _snapshot("dev")["launch_settings"]["sandbox"]
    capabilities = _tracked().capabilities
    ceiling = set(
        read_json_object(ROOT / PROFILES / "developer.json")["capability_ceiling"]
    )
    excluded = capability_union(capabilities, ceiling, "channel_commands")
    assert launch["excludedCommands"] == []
    assert capability_union(capabilities, ceiling, "network_domains") == set(
        launch["network"]["allowedDomains"]
    )
    sandbox = read_json_object(ROOT / SANDBOXES / "developer-v1.json")["ceiling"]
    assert set(sandbox["channel_commands"]) == excluded
    assert capabilities["board.read"]["sandbox_ceiling"]["channel_commands"] == ["kd-notes", "kd-task"]
    assert capabilities["board.write"]["sandbox_ceiling"]["channel_commands"] == ["kd-work"]


def test_sysadmin_target_sandbox_is_sysadmin_v1() -> None:
    """Spec 5.2b, no network; ssh-ro, ssh-exec, kd-monitor and kd-task reach their
    channels (their executors are outside the sandbox)."""
    registry = _tracked()
    assert registry.profiles["sysadmin"]["sandbox_profiles"] == ["sysadmin-v1"]
    sandbox = registry.sandboxes["sysadmin-v1"]
    assert _canonical(sandbox["settings"]) == _canonical(FAIL_CLOSED_SANDBOX)
    assert sandbox["settings"]["network"]["allowUnixSockets"] == []
    assert sandbox["ceiling"]["network_domains"] == []
    assert set(sandbox["ceiling"]["channel_commands"]) == SYSADMIN_V1_CHANNELS
    ceiling = registry.profiles["sysadmin"]["capability_ceiling"]
    assert capability_union(registry.capabilities, ceiling, "channel_commands") == (
        SYSADMIN_V1_CHANNELS
    )
    assert not (ROOT / "agents/sysadmin/launch-settings.json").exists()
    no_sandbox = [
        name
        for name, entry in registry.sandboxes.items()
        if entry["settings"]["enabled"] is not True
    ]
    assert no_sandbox == []


def test_board_write_is_the_base_executor_and_no_more() -> None:
    capabilities = _tracked().capabilities
    entry = capabilities["board.write"]
    forms = {w["command"]: set(w["subcommands"]) for w in entry["wrappers"]}
    # the kd-task wrapper lives in board.read; board.write opens its writes in the executor.
    # ask/answer: the owner's answer on the card; the template documents them (global review 15.10.2026)
    # finish: how the agent's turn on its own card ends
    # council, council-reply: the writer reads its card's council of models and answers its findings
    assert forms == {"kd-work": {"progress", "directive", "reply", "ask", "answer", "finish", "assist", "council",
                                 "council-reply"}}
    # every board write leaves the agent's user, to the base user's executors.
    assert {w["run_as"] for w in entry["wrappers"]} == {naming.base_user()}
    assert entry["resources"] == [
        {"name": "board-tasks", "access": "via-wrapper"},
        {"name": "board-activity", "access": "via-wrapper"},
        {"name": "board-directives", "access": "via-wrapper"},
    ]
    assert "write-grants" not in entry["lifecycle"]["install"]
    assert (entry["dependencies"], entry["conflicts"]) == (
        ["board.read"],
        ["board.coordinate", "worker.request"],  # C07: a coordinator orders workers, never writes the board
    )
    read = capabilities["board.read"]
    assert {w["command"]: set(w["subcommands"]) for w in read["wrappers"]} == {
        "kd-task": {"view", "list", "search", "scope", "result", "message", "thread", "waits", "projects", "chat",
                    "file", "claim", "create", "edit", "handoff", "brief-review"},
        "kd-notes": {"add"},
    }
    writers = {
        name
        for name, profile in _tracked().profiles.items()
        if "board.write" in profile["capability_ceiling"]
    }
    assert writers == {"analyst", "developer", "sysadmin"}  # the analyst writes briefs into cards


def test_monitor_manage_is_exactly_the_baseline_forms() -> None:
    capabilities = _tracked().capabilities
    (wrapper,) = [
        w for w in capabilities["monitor.manage"]["wrappers"] if w["command"] == "kd-monitor"
    ]
    frozen = policy_baseline.load()["agents"]["sysadmin"]["channels"]
    listed = {item.split(":", 1)[1] for item in policy_baseline.load()["widenings"].get("sysadmin", {})}
    # Not wider and not narrower than the baseline and its reviewed widenings; kd-monitor tick stays with the timer.
    assert {item for item in [*frozen, *listed] if ":kd-monitor " in item} == {
        f"self:digi-sysadmin:kd-monitor {sub}" for sub in wrapper["subcommands"]
    }
    assert "tick" not in wrapper["subcommands"]
    assert capabilities["monitor.manage"]["dependencies"] == ["servers.read"]


def test_module_setup_scripts_are_lifecycle_steps() -> None:
    capabilities = _tracked().capabilities
    scripts = {n: e["setup"] for n, e in capabilities.items() if e["setup"] is not None}
    assert scripts == {
        "forge.review": "agents/dev/install/forge_setup.sh",
        "git.develop": "agents/dev/install/develop_setup.sh",
        "git.mirror": "agents/dev/install/mirror_read_setup.sh",  # the mirror's Host block and remote
        "monitor.manage": "agents/sysadmin/install/monitor_setup.sh",
        "servers.execute": "agents/sysadmin/install/exec_setup.sh",
        "servers.read": "agents/sysadmin/install/servers_setup.sh",
        "repo.read": "common/install/repo_setup.sh",  # a common module, no agent of its own
        "host.read": "common/install/host_read_setup.sh",  # the host reader's groups and ACLs
        "data.read": "common/install/data_read_setup.sh",  # the data key and its journal
    }
    assert capabilities["repo.read"]["lifecycle"] == {
        "install": ["setup", "wrappers", "read-grants"],
        "check": ["setup", "wrappers", "read-grants"],
        "disable": ["read-grants", "setup"],
    }
    assert capabilities["forge.review"]["lifecycle"]["install"][:2] == ["executor-user", "setup"]
    assert capabilities["forge.review"]["lifecycle"]["disable"][-1] == "setup"
    assert capabilities["git.develop"]["lifecycle"]["disable"] == ["setup"]


@pytest.fixture
def registry_copy(registry_root: Path) -> Path:
    return registry_root


Mutation = Callable[[Path], object]


def _edit(relative: str, change: Callable[[Entry], object]) -> Mutation:
    def apply(root: Path) -> None:
        path = root / relative
        data = json.loads(path.read_text(encoding="utf-8"))
        change(data)
        path.write_text(json.dumps(data), encoding="utf-8")

    return apply


def _set(relative: str, keys: tuple[str | int, ...], value: object) -> Mutation:
    def change(data: Any) -> None:
        for key in keys[:-1]:
            data = data[key]
        data[keys[-1]] = value

    return _edit(relative, change)


def _raw(relative: str, change: Callable[[bytes], bytes]) -> Mutation:
    def apply(root: Path) -> None:
        path = root / relative
        path.write_bytes(change(path.read_bytes()))

    return apply


def _clone_capability(name: str, template: str) -> Mutation:
    def apply(root: Path) -> None:
        data = json.loads((root / CAPABILITIES / f"{template}.json").read_text("utf-8"))
        data["name"] = name
        (root / CAPABILITIES / f"{name}.json").write_text(json.dumps(data), "utf-8")

    return apply


def _cap(name: str, keys: tuple[str | int, ...], value: object) -> Mutation:
    return _set(f"{CAPABILITIES}/{name}.json", keys, value)


def _pro(name: str, keys: tuple[str | int, ...], value: object) -> Mutation:
    return _set(f"{PROFILES}/{name}.json", keys, value)


def _box(name: str, keys: tuple[str | int, ...], value: object) -> Mutation:
    return _set(f"{SANDBOXES}/{name}.json", keys, value)


def _all(*mutations: Mutation) -> Mutation:
    def apply(root: Path) -> None:
        for mutation in mutations:
            mutation(root)

    return apply


def _split_boss_board(**changes: object) -> Mutation:
    """A second declaration of kd-boss-board in board.coordinate, changed as given."""
    def change(data: Entry) -> None:
        data["wrappers"].append({**data["wrappers"][0], "subcommands": ["promote"], **changes})

    return _edit(f"{CAPABILITIES}/board.coordinate.json", change)


def _renamed_exec(command: str, approval: str, source: str | None = None) -> Mutation:
    """servers.execute published under another name, with the sandbox exclusions moved
    along, so that every other registry rule still passes."""
    changes = [
        _cap("servers.execute", ("wrappers", 0, "command"), command),
        _cap("servers.execute", ("wrappers", 0, "approval"), approval),
        _cap("servers.execute", ("sandbox_ceiling", "channel_commands"), [command]),
        _box(
            "sysadmin-v1",
            ("ceiling", "channel_commands"),
            sorted([command, "kd-monitor", "kd-task", "ssh-ro"]),
        ),
    ]
    if source is not None:
        changes.append(_cap("servers.execute", ("wrappers", 0, "source"), source))
    return _all(*changes)


def _renamed_merge(command: str, approval: str, source: str | None = None) -> Mutation:
    changes = [
        _cap("forge.review", ("wrappers", 1, "command"), command),
        _cap("forge.review", ("wrappers", 1, "approval"), approval),
        _cap(
            "forge.review",
            ("sandbox_ceiling", "channel_commands"),
            sorted(["kd-forge", command]),
        ),
        _box(
            "developer-v1",
            ("ceiling", "channel_commands"),
            sorted(["kd-forge", command, "kd-task"]),
        ),
    ]
    if source is not None:
        changes.append(_cap("forge.review", ("wrappers", 1, "source"), source))
    return _all(*changes)


def _res(keys: tuple[str | int, ...], value: object) -> Mutation:
    return _set(RESOURCES, keys, value)


def _add_resource(name: str, entry: Entry) -> Mutation:
    return _edit(RESOURCES, lambda data: data["resources"].__setitem__(name, entry))


_PLANNED = {"kind": "file", "planned": "B05", "secret": False}


def _both(first: Mutation, second: Mutation) -> Mutation:
    def apply(root: Path) -> None:
        first(root)
        second(root)

    return apply


def _usage_bytes(change: Callable[[bytes], bytes]) -> Mutation:
    return _raw(f"{CAPABILITIES}/usage.read.json", change)


def _in_capabilities(action: Callable[[Path], object]) -> Mutation:
    return lambda root: action(root / CAPABILITIES)


# Each row breaks one rule in a clean copy and names the error it must produce.
# fmt: off
MUTATIONS = [
    # duplicates
    ("dup-dependency", _cap("board.coordinate", ("dependencies",), ["board.read"] * 2), "duplicate"),
    ("dup-ceiling", _pro("developer", ("capability_ceiling",), ["board.read", "board.read", "git.develop"]), "duplicate"),
    ("dup-command", _cap("usage.read", ("wrappers", 0, "command"), "kd-task"), "declared twice"),
    ("dup-json-key", _usage_bytes(lambda b: b.replace(b"{", b'{"name": "usage.read", ', 1)), "duplicate JSON key"),
    ("extra-capability", _clone_capability("board.delete", "usage.read"), "not in the spec"),
    ("missing-capability", _in_capabilities(lambda d: (d / "usage.read.json").unlink()), "missing spec capability"),
    ("name-mismatch", _cap("usage.read", ("name",), "board.read"), "name does not match"),
    # wildcards, absolute paths and other unsafe strings
    ("wildcard-subcommand", _cap("board.read", ("wrappers", 0, "subcommands"), ["task *"]), "wildcard"),
    ("wildcard-capability", _cap("board.coordinate", ("conflicts",), ["git.*"]), "wildcard"),
    ("wildcard-domain", _box("developer-v1", ("ceiling", "network_domains"), ["*.example.test"]), "wildcard"),
    ("absolute-source", _cap("usage.read", ("wrappers", 0, "source"), "/opt/digipaddock/common/bin/kd-usage"), "absolute path"),
    ("home-validator", _cap("git.develop", ("validator",), "~/guard.py"), "absolute path"),
    ("traversal", _cap("forge.review", ("wrappers", 0, "executor"), "agents/dev/../sysadmin/bin/ssh_exec.py"), "traversal"),
    ("path-in-text", _cap("usage.read", ("description",), "reads var/lib/digipaddock"), "path outside"),
    ("control-char", _cap("usage.read", ("description",), "usage\u0007table"), "control character"),
    # file hygiene
    ("oversized", _usage_bytes(lambda b: b + b" " * MAX_JSON_BYTES), "bounded"),
    ("not-object", _usage_bytes(lambda b: b"[]"), "JSON object"),
    ("nan", _usage_bytes(lambda b: b.replace(b'"schema_version": 1', b'"schema_version": NaN')), "non-finite"),
    ("bad-utf8", _usage_bytes(lambda b: b"\xff" + b), "invalid JSON"),
    ("stray-file", _in_capabilities(lambda d: (d / "README.md").write_text("x", "utf-8")), "only regular .json"),
    ("nested-dir", _in_capabilities(lambda d: (d / "nested.json").mkdir()), "only regular .json"),
    ("schema-bool", _cap("usage.read", ("schema_version",), True), "schema_version"),
    ("unknown-field", _cap("usage.read", ("permissions",), {"allow": ["Bash"]}), "fields must be exactly"),
    # unresolved references
    ("unknown-dependency", _cap("agent.nudge", ("dependencies",), ["board.unknown"]), "unknown capability board.unknown"),
    ("unknown-sandbox", _pro("coordinator", ("sandbox_profiles",), ["missing-v1"]), "unknown sandbox profile"),
    ("unknown-resource", _cap("usage.read", ("resources", 0, "name"), "usage-raw"), "unknown resource"),
    ("missing-source", _cap("usage.read", ("wrappers", 0, "source"), "common/bin/kd-missing"), "does not exist"),
    ("missing-validator", _cap("git.develop", ("validator",), "agents/dev/dev_agent/nope.py"), "does not exist"),
    ("validator-not-guard", _cap("git.develop", ("validator",), "agents/dev/bin/kd-pr"), "is not a guard module"),
    ("validator-hook-missing", lambda root: (root / "agents/dev/bin/deny_guard.py").unlink(), "validator hook agents/dev/bin/deny_guard.py does not exist"),
    ("shell-without-validator", _cap("git.develop", ("validator",), None), "a sandboxed shell needs a validator"),
    # review 04.11.2026: no capability without a tool of its own only to open a host to a sandboxed shell
    ("no-tool", _cap("git.mirror", ("wrappers",), []), "wrappers must be a non-empty list"),
    # profile defaults never grant rights
    ("default-grants", _pro("coordinator", ("defaults", "capabilities"), ["servers.execute"]), "defaults may only"),
    ("profile-capabilities", _pro("developer", ("capabilities",), ["servers.execute"]), "fields must be exactly"),
    ("profile-conflict", _pro("developer", ("capability_ceiling",), ["board.read", "forge.review", "git.develop", "servers.read"]), "conflicting"),
    ("profile-dependency", _pro("coordinator", ("capability_ceiling",), ["agent.nudge", "board.coordinate", "usage.read"]), "closed under dependencies"),
    ("coordinate-elsewhere", _pro("developer", ("capability_ceiling",), ["board.coordinate", "board.read", "git.develop"]), "coordinator profile only"),
    ("legacy-new-capability", _pro("sysadmin", ("capability_ceiling",), ["board.read", "board.write", "diag.request", "host.read", "monitor.manage", "servers.execute", "servers.read", "usage.read"]), "widens the policy baseline"),
    # sandbox stays fail-closed and tight
    ("sandbox-optional", _box("coordinator-v1", ("settings", "failIfUnavailable"), False), "fail-closed"),
    ("sandbox-unsandboxed", _box("developer-v1", ("settings", "allowUnsandboxedCommands"), True), "fail-closed"),
    ("sandbox-sockets", _box("coordinator-v1", ("settings", "network", "allowAllUnixSockets"), True), "fail-closed"),
    ("sandbox-int-bool", _box("coordinator-v1", ("settings", "enabled"), 1), "fail-closed"),
    ("sandbox-writable", _box("developer-v1", ("ceiling", "writable"), ["work", "tmp"]), "writable ceiling"),
    ("coordinator-network", _box("coordinator-v1", ("ceiling", "network_domains"), ["forge.example.test"]), "ceiling must equal"),
    ("developer-extra-domain", _box("developer-v1", ("ceiling", "network_domains"), ["example.com", "forge.example.test", "github.example.test"]), "ceiling must equal"),
    ("capability-wider", _cap("git.develop", ("sandbox_ceiling", "network_domains"), ["example.com"]), "exceeds sandbox profile"),
    ("network-without-shell", _cap("usage.read", ("sandbox_ceiling", "network_domains"), ["forge.example.test"]), "sandboxed shell"),
    ("sudo-inside-sandbox", _cap("agent.nudge", ("sandbox_ceiling", "channel_commands"), []), "must be a channel command"),
    ("executor-not-channel", _cap("servers.execute", ("sandbox_ceiling", "channel_commands"), []), "must be a channel command"),
    ("exclude-foreign", _cap("usage.read", ("sandbox_ceiling", "channel_commands"), ["backlog"]), "may be channel commands"),
    # sudo, secrets and approvals
    ("sudo-root", _cap("agent.nudge", ("wrappers", 0, "run_as"), "root"), "run_as must be"),
    ("executor-without-sudo", _cap("usage.read", ("wrappers", 0, "executor"), "common/bin/kd-usage"), "executor is required"),
    ("foreign-service-user", _cap("forge.review", ("wrappers", 0, "run_as"), "digi-exec"), "service user"),
    ("new-module-service-user", _cap("agent.nudge", ("wrappers", 0, "run_as"), "digi-forge"), "service user"),
    ("sudo-beyond-baseline", _cap("forge.review", ("wrappers", 0, "executor"), "agents/dev/bin/kd-pr"), "added channel:forge:digi-forge:kd-pr widens"),
    ("merge-to-ask", _cap("forge.review", ("wrappers", 1, "approval"), "ask"), "approval of kd-merge must be allow"),
    ("exec-to-ask", _cap("servers.execute", ("wrappers", 0, "approval"), "ask"), "approval of ssh-exec must be allow"),
    ("platform-git", _cap("usage.read", ("wrappers", 0), {"command": "git", "source": "platform", "subcommands": ["log", "show"], "approval": "allow", "run_as": "self", "executor": None}), "platform command git is not one of"),
    # owner approval is pinned to the wrapper source, its name and its executor
    ("exec-renamed", _renamed_exec("kd-exec", "allow"), "agents/sysadmin/bin/ssh-exec must be published as ssh-exec"),
    ("exec-renamed-ask", _renamed_exec("kd-exec", "ask"), "agents/sysadmin/bin/ssh-exec must be published as ssh-exec"),
    ("exec-other-source", _renamed_exec("kd-exec", "allow", "agents/sysadmin/bin/kd-monitor"), "agents/sysadmin/bin/ssh_exec.py is reached only from"),
    ("merge-renamed", _renamed_merge("kd-mrg", "allow"), "agents/dev/bin/kd-merge must be published as kd-merge"),
    ("merge-renamed-ask", _renamed_merge("kd-mrg", "ask"), "agents/dev/bin/kd-merge must be published as kd-merge"),
    ("merge-other-source", _renamed_merge("kd-mrg", "allow", "agents/dev/bin/kd-pr"), "agents/dev/bin/forge_exec.py is reached only from"),
    ("name-squat", _cap("usage.read", ("wrappers", 0, "command"), "ssh-exec"), "the name ssh-exec is reserved for agents/sysadmin/bin/ssh-exec"),
    ("platform-shell", _cap("usage.read", ("wrappers", 0), {"command": "sh", "source": "platform", "subcommands": ["exec"], "approval": "allow", "run_as": "self", "executor": None}), "platform command sh is not one of"),
    ("secret-grant", _cap("forge.review", ("resources", 1, "access"), "read-grant"), "secret resource"),
    ("platform-free-argv", _cap("usage.read", ("wrappers", 0), {"command": "git", "source": "platform", "subcommands": [], "approval": "allow", "run_as": "self", "executor": None}), "finite subcommand"),
    ("platform-sudo", _cap("usage.read", ("wrappers", 0), {"command": "git", "source": "platform", "subcommands": ["log"], "approval": "allow", "run_as": "digi", "executor": None}), "runs as the agent itself"),
    # the Backlog.md CLI left the policy; the board is read and written through kd-task only
    ("backlog-again", _cap("board.read", ("wrappers", 0), {"command": "backlog", "source": "platform", "subcommands": ["task list"], "approval": "allow", "run_as": "self", "executor": None}), "platform command backlog is not one of"),
    ("kd-task-overlap", _cap("board.write", ("wrappers", 0), {"command": "kd-task", "source": "common/bin/kd-task", "subcommands": ["claim"], "approval": "allow", "run_as": "digi", "executor": "common/bin/kd-task"}), "declared twice"),
    # a repository wrapper splits only inside its capability, by disjoint finite forms
    ("split-overlap", _split_boss_board(subcommands=["promote", "comment"]), "declared twice"),
    ("split-free-argv", _split_boss_board(subcommands=[]), "declared twice"),
    ("split-other-executor", _split_boss_board(executor="common/bin/kd-nudge"), "declared twice"),
    ("promote-elsewhere", _cap("agent.nudge", ("wrappers", 0), {"command": "kd-boss-board", "source": "common/bin/kd-boss-board", "subcommands": ["close"], "approval": "allow", "run_as": "digi", "executor": "common/bin/kd-boss-board"}), "declared twice"),
    ("write-grant-elsewhere", _cap("usage.read", ("resources", 0, "access"), "write-grant"), "write grant on usage-ledger"),
    ("coordinate-raw-write", _cap("board.coordinate", ("resources", 0, "access"), "write-grant"), "write grant on board-tasks"),
    ("platform-excluded-elsewhere", _cap("board.read", ("sandbox_ceiling", "channel_commands"), ["backlog"]), "may be channel commands"),
    ("write-without-read", _pro("developer", ("capability_ceiling",), ["board.write", "forge.review", "git.develop"]), "closed under dependencies"),
    ("coordinator-board-write", _pro("coordinator", ("capability_ceiling",), ["agent.nudge", "board.coordinate", "board.read", "board.write", "usage.read"]), "conflicting"),
    ("write-coordinate-asymmetric", _cap("board.write", ("conflicts",), []), "conflict with board.write must be symmetric"),
    ("write-grant-kept", _cap("board.write", ("lifecycle", "disable"), []), "lifecycle disable"),
    ("board-read-not-channel", _cap("board.read", ("sandbox_ceiling", "channel_commands"), []), "must be a channel command"),
    # monitor.manage: the sysadmin profile only, today's forms only
    ("monitor-developer", _pro("developer", ("capability_ceiling",), ["board.read", "board.write", "forge.review", "git.develop", "monitor.manage"]), "monitor.manage belongs to the sysadmin profile only"),
    ("monitor-coordinator", _pro("coordinator", ("capability_ceiling",), ["agent.nudge", "board.coordinate", "board.read", "monitor.manage", "servers.read", "usage.read"]), "monitor.manage belongs to the sysadmin profile only"),
    ("monitor-dropped", _pro("sysadmin", ("capability_ceiling",), ["board.read", "board.write", "servers.execute", "servers.read"]), "monitor.manage belongs to the sysadmin profile only"),
    ("monitor-tick", _cap("monitor.manage", ("wrappers", 0, "subcommands"), ["list", "tick"]), "added channel:self:digi-sysadmin:kd-monitor tick widens"),
    ("monitor-option", _cap("monitor.manage", ("wrappers", 0, "subcommands"), ["list --state x"]), "invalid names"),
    # conflicts and dependencies
    ("asymmetric-conflict", _cap("servers.read", ("conflicts",), ["board.coordinate", "forge.review"]), "symmetric"),
    ("dependency-conflict", _cap("board.coordinate", ("conflicts",), ["board.read", "forge.review", "git.develop", "servers.execute", "servers.read"]), "both a dependency"),
    ("dependency-cycle", _cap("board.read", ("dependencies",), ["board.coordinate"]), "dependency cycle"),
    # lifecycle: grants installed last, removed first, data never purged
    ("channels-not-last", _cap("forge.review", ("lifecycle", "install"), ["executor-user", "channels", "wrappers", "read-grants"]), "lifecycle install"),
    ("unchecked-step", _cap("servers.execute", ("lifecycle", "check"), ["wrappers", "channels"]), "lifecycle check"),
    ("disable-keeps-channels", _cap("agent.nudge", ("lifecycle", "disable"), []), "lifecycle disable"),
    ("sudoers-step", _cap("agent.nudge", ("lifecycle", "disable"), ["sudoers"]), "unknown lifecycle step"),
    ("disable-purges", _cap("usage.read", ("lifecycle", "disable"), ["purge"]), "unknown lifecycle step"),
    # module setup scripts: a named script in an install directory, run as a lifecycle step
    ("setup-not-a-script", _cap("forge.review", ("setup",), "agents/dev/bin/kd-forge"), "setup script must be"),
    ("setup-traversal", _cap("forge.review", ("setup",), "agents/dev/install/../x_setup.sh"), "setup script must be"),
    ("setup-missing", _cap("forge.review", ("setup",), "agents/dev/install/missing_setup.sh"), "does not exist"),
    ("setup-without-step", _cap("forge.review", ("lifecycle", "install"), ["executor-user", "wrappers", "read-grants", "channels"]), "lifecycle install"),
    ("setup-not-disabled", _cap("git.develop", ("lifecycle", "disable"), []), "lifecycle disable"),
    ("step-without-setup", _cap("usage.read", ("lifecycle", "install"), ["setup", "wrappers"]), "lifecycle install"),
    # the resource table: the only absolute paths, below known system prefixes only
    ("resource-outside-prefix", _res(("resources", "exec-key", "path"), "/root/pult.env"), "normalized absolute path below"),
    ("resource-tmp", _res(("resources", "usage-ledger", "path"), "/tmp/usage.jsonl"), "normalized absolute path below"),
    ("resource-is-prefix", _res(("resources", "forge-tokens", "path"), "/etc/"), "normalized absolute path below"),
    ("resource-relative", _res(("resources", "exec-key", "path"), "etc/digipaddock/pult.env"), "normalized absolute path below"),
    ("resource-traversal", _res(("resources", "exec-key", "path"), "/etc/digipaddock/../shadow"), "normalized absolute path below"),
    ("resource-dot", _res(("resources", "board-tasks", "path"), "/srv/digipaddock-agents/./board"), "normalized absolute path below"),
    ("resource-double-slash", _res(("resources", "board-tasks", "path"), "/srv//digipaddock-agents/board"), "normalized absolute path below"),
    ("resource-trailing-slash", _res(("resources", "board-tasks", "path"), "/srv/digipaddock-agents/board/"), "normalized absolute path below"),
    ("resource-wildcard", _res(("resources", "forge-tokens", "path"), "/etc/digipaddock/*"), "normalized absolute path below"),
    ("resource-control-char", _res(("resources", "exec-key", "path"), "/etc/digipaddock/pult\n.env"), "normalized absolute path below"),
    ("resource-unknown-field", _res(("resources", "exec-key", "mode"), "0640"), "fields must be exactly"),
    ("resource-kind", _res(("resources", "board-tasks", "kind"), "socket"), "kind must be dir or file"),
    ("resource-secret-int", _res(("resources", "exec-key", "secret"), 1), "secret must be true or false"),
    ("resource-unused", _add_resource("spare-file", {"kind": "file", "path": "/etc/digipaddock/spare", "secret": False}), "resource spare-file is not used"),
    ("resource-bad-name", _add_resource("Spare", {"kind": "file", "path": "/etc/digipaddock/spare", "secret": False}), "invalid resource name"),
    ("layout-outside-prefix", _res(("layout", "code"), "/usr/lib/agents"), "layout: code must be"),
    ("layout-unknown-field", _res(("layout", "bin"), "/opt/digipaddock/bin"), "layout: fields must be exactly"),
    ("table-schema", _res(("schema_version",), 2), "unsupported schema_version"),
    ("table-unknown-field", _res(("paths",), {}), "fields must be exactly"),
    ("sysadmin-foreign-sandbox", _pro("sysadmin", ("sandbox_profiles",), ["developer-v1", "sysadmin-v1"]), "exceeds sandbox profile developer-v1"),
    ("protected-wildcard-dir", _res(("protected", 0, "path"), "/etc/agent-*"), "invalid dir path"),
    ("protected-bad-pattern", _res(("protected", 8, "path"), "/opt/**/.git/conf?g"), "invalid pattern path"),
    ("protected-pattern-traversal", _res(("protected", 8, "path"), "/opt/**/../.git/config"), "invalid pattern path"),
    ("protected-unknown-deny", _res(("protected", 0, "deny"), ["write"]), "deny must be"),
    ("protected-duplicate", _res(("protected", 0, "path"), "/etc/digipaddock/cabinet.env"), "protected paths must be unique"),
    # secrets: own-file only for the agent's own home file, everything else via a wrapper
    ("home-not-secret", _res(("resources", "forge-ssh-key", "secret"), False), "home resource must be a secret file"),
    ("home-absolute", _res(("resources", "forge-ssh-key", "home"), ["/home/digi-dev/.ssh/config"]), "relative paths in the agent's home"),
    ("home-traversal", _res(("resources", "server-ro-key", "home"), ["../digi-exec/.ssh/agent_exec_ed25519"]), "relative paths in the agent's home"),
    ("home-placeholder", _res(("resources", "forge-ssh-key", "home"), [".ssh/config", ".ssh/{user}_ed25519"]), "relative paths in the agent's home"),
    ("own-file-credentials", _res(("resources", "forge-ssh-key", "home"), [".claude/.credentials.json"]), "own files must lie in the agent's .ssh/"),
    ("own-file-memory-key", _res(("resources", "server-ro-key", "home"), [".hindsight/config.json"]), "own files must lie in the agent's .ssh/"),
    ("own-file-ssh-prefix", _res(("resources", "server-ro-key", "home"), [".sshx/agent_ro_ed25519"]), "own files must lie in the agent's .ssh/"),
    ("home-via-wrapper", _cap("git.develop", ("resources", 1, "access"), "via-wrapper"), "home resource forge-ssh-key must be own-file"),
    ("home-read-grant", _cap("servers.read", ("resources", 2, "access"), "read-grant"), "home resource server-ro-key must be own-file"),
    ("own-file-absolute", _cap("forge.review", ("resources", 1, "access"), "own-file"), "own-file needs a home resource"),
    ("own-file-public", _cap("usage.read", ("resources", 0, "access"), "own-file"), "own-file needs a home resource"),
    ("grant-exposes-secret", _res(("resources", "exec-journal", "path"), "/etc/digipaddock"), "would expose secret"),
    ("grant-over-protected", _res(("resources", "exec-journal", "path"), "/etc/digipaddock"), "would expose protected path"),
    ("grant-inside-protected", _res(("resources", "exec-journal", "path"), "/var/lib/digipaddock/journal"), "lies inside protected path"),
    ("grant-inside-secret", _res(("resources", "forge-journal", "path"), "/etc/digipaddock/forge/log"), "lies inside secret forge-tokens"),
    ("public-inside-secret", _res(("resources", "forge-repos", "path"), "/etc/digipaddock/forge/digipaddock-merge.env"), "read-public lies inside secret forge-tokens"),
    # a planned resource: no path yet, never a secret, only behind a planned wrapper
    ("planned-secret", _add_resource("board-later", {"kind": "file", "planned": "B05", "secret": True}), "planned resource has no path to deny a secret"),
    ("planned-task", _add_resource("board-later", {"kind": "file", "planned": "later", "secret": False}), "planned must name a plan task"),
    ("planned-with-path", _add_resource("board-later", {"kind": "file", "planned": "B05", "path": "/var/lib/digipaddock/board/later.jsonl", "secret": False}), "fields must be exactly"),
    ("planned-granted", _both(_add_resource("board-later", _PLANNED), _cap("board.coordinate", ("resources", 2), {"name": "board-later", "access": "read-grant"})), "planned resource board-later is reached only through a planned wrapper"),
    ("planned-elsewhere", _both(_add_resource("board-later", _PLANNED), _cap("usage.read", ("resources",), [{"name": "usage-ledger", "access": "read-public"}, {"name": "board-later", "access": "via-wrapper"}])), "planned resource board-later is reached only through a planned wrapper"),
    ("grant-into-home", _res(("resources", "exec-journal", "path"), "/home/digi/logs"), "read-grant reaches into a user home"),
    ("public-into-home", _res(("resources", "usage-ledger", "path"), "/home/digi-dev/usage.jsonl"), "read-public reaches into a user home"),
    # read-direct: the Read tool opens it, no ACL; never over or inside a read-protected path, never in a home
    ("direct-over-read-protected", _res(("resources", "project-docs", "path"), "/etc/digi-exec"), "read-direct would expose protected path /etc/digi-exec"),
    ("direct-inside-read-protected", _res(("resources", "project-docs", "path"), "/var/lib/digipaddock/docs"), "read-direct lies inside protected path /var/lib/digipaddock"),
    ("direct-into-home", _res(("resources", "project-docs", "path"), "/home/digi/docs"), "read-direct reaches into a user home"),
]
# fmt: on


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [pytest.param(mutate, expected, id=name) for name, mutate, expected in MUTATIONS],
)
def test_registry_rejects_broken_copy(
    registry_copy: Path, mutate: Mutation, expected: str
) -> None:
    assert registry_errors(registry_copy) == []
    mutate(registry_copy)
    errors = registry_errors(registry_copy)
    assert any(expected in error for error in errors), errors


@pytest.mark.parametrize("target", [f"{CAPABILITIES}/usage.read.json", PROFILES])
def test_registry_rejects_symlinks(registry_copy: Path, target: str) -> None:
    link = registry_copy / target
    moved = registry_copy / "moved"
    link.rename(moved)
    try:
        link.symlink_to(moved, target_is_directory=moved.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks needs extra privileges on this platform")
    errors = registry_errors(registry_copy)
    assert any("only regular .json" in e or "linked registry" in e for e in errors)


# One agent's choice: (profile, capabilities, sandbox profile) and the refusal it must produce.
# fmt: off
SELECTIONS = [
    ("unknown-profile", ("auditor", ["board.read"], "coordinator-v1"), "unknown profile"),
    ("unknown-capability", ("developer", ["board.read", "board.delete"], "developer-v1"), "unknown capability board.delete"),
    ("wildcard-capability", ("developer", ["board.*"], "developer-v1"), "unknown capability board.*"),
    ("outside-ceiling", ("developer", ["board.read", "servers.read"], "developer-v1"), "outside the developer profile ceiling"),
    ("coordinator-writes", ("coordinator", ["board.read", "board.coordinate", "board.write"], "coordinator-v1"), "outside the coordinator profile ceiling"),
    ("foreign-sandbox", ("coordinator", ["board.read", "board.coordinate"], "developer-v1"), "not allowed for profile coordinator"),
    ("unknown-sandbox", ("developer", ["git.develop"], "open-v1"), "not allowed for profile developer"),
    ("missing-dependency", ("developer", ["board.write"], "developer-v1"), "missing capability dependency of board.write"),
]
# fmt: on


@pytest.mark.parametrize(
    ("selection", "expected"),
    [
        pytest.param(selection, expected, id=name)
        for name, selection, expected in SELECTIONS
    ],
)
def test_selection_stays_inside_the_profile(
    selection: tuple[str, list[str], str], expected: str
) -> None:
    profile, capabilities, sandbox = selection
    with pytest.raises(RegistryError, match=re.escape(expected)):
        validate_selection(_tracked(), profile, capabilities, sandbox)


@pytest.mark.parametrize(
    ("profile", "capabilities", "sandbox"),
    [
        ("coordinator", sorted(SPEC_BOSS_CAPABILITIES), "coordinator-v1"),
        (
            "developer",
            ["board.read", "board.write", "forge.review", "git.develop"],
            "developer-v1",
        ),
        ("developer", ["git.develop"], "developer-v1"),
    ],
)
def test_selection_inside_the_profile_is_accepted(
    profile: str, capabilities: list[str], sandbox: str
) -> None:
    validate_selection(_tracked(), profile, capabilities, sandbox)


def test_selection_refuses_conflicts_even_if_a_ceiling_held_them() -> None:
    # validate_registry keeps ceilings conflict-free; the selection check does not rely on it.
    registry = _tracked()
    profiles = dict(registry.profiles)
    profiles["developer"] = {
        **profiles["developer"],
        "capability_ceiling": ["board.coordinate", "board.read", "board.write"],
    }
    broken = dataclasses.replace(registry, profiles=profiles)
    with pytest.raises(RegistryError, match="incompatible capabilities"):
        validate_selection(
            broken,
            "developer",
            ["board.coordinate", "board.read", "board.write"],
            "developer-v1",
        )


def test_resource_table_is_the_only_place_for_deployment_paths() -> None:
    # The registry code is deployment-neutral: the deployment's roots live in the data table.
    table = _tracked().resources
    assert set(table.layout) == {"agents", "code"}
    for name in ("board-tasks", "exec-key", "forge-tokens"):
        assert table.resources[name].path.startswith("/"), name


def test_own_file_is_only_the_holders_home_secret() -> None:
    registry = _tracked()
    table = registry.resources.resources
    own = {
        (name, r["name"])
        for name, entry in registry.capabilities.items()
        for r in entry["resources"]
        if r["access"] == "own-file"
    }
    assert own == {("git.develop", "forge-ssh-key"), ("servers.read", "server-ro-key")}
    for _, resource in own:
        assert table[resource].secret and table[resource].path is None
    # The dev sandbox reads exactly these two files of its own .ssh.
    launch = _snapshot("dev")["launch_settings"]["sandbox"]
    key = table["forge-ssh-key"].paths("/home/digi-dev", "dev")
    assert list(key) == [p for p in launch["filesystem"]["allowRead"] if "/.ssh/" in p]
    # ssh-ro reads the same key the sysadmin inventory names, in its own user's home (M02).
    inventory = (ROOT / "agents/sysadmin/sysadmin/inventory.py").read_text("utf-8")
    (ro_key,) = table["server-ro-key"].paths("/home/digi-sysadmin", "sysadmin")
    assert f'RO_KEY_FILE = "{ro_key.removeprefix("/home/digi-sysadmin/")}"' in inventory


def test_public_data_may_sit_in_a_protected_directory() -> None:
    # usage.read's ledger lies in the protected state root: allowed for read-public
    # (the compiler opens that file alone); a secret directory has no public files.
    registry = _tracked()
    ledger = registry.resources.resources["usage-ledger"]
    protected = [p.path for p in registry.resources.protected if "read" in p.deny]
    assert any(ledger.path.startswith(path + "/") for path in protected)
    validate_registry(registry)


def test_resource_paths_match_the_code_that_uses_them() -> None:
    table = _tracked().resources
    resources, code = table.resources, table.layout["code"]
    # The release (release_switch.sh) installs this repository as the code root.
    inventory = (ROOT / "agents/sysadmin/sysadmin/inventory.py").read_text("utf-8")
    assert resources["server-inventory"].path == f"{code}/agents/sysadmin/inventory.json"
    # the inventory itself comes from the overlay; the code repository ships its example
    assert (ROOT / "agents/sysadmin/inventory.json").is_file() or (ROOT / "agents/sysadmin/inventory.example.json").is_file()
    assert 'INVENTORY = Path(__file__).resolve().parent.parent / "inventory.json"' in (
        inventory
    )
    assert f'ENABLED = Path("{resources["servers-enabled"].path}")' in inventory
    deliver = (ROOT / "agents/sysadmin/sysadmin/monitor/deliver.py").read_text("utf-8")
    # the static monitor pages are gone with their directory
    assert "monitor-web" not in resources and not (ROOT / "agents/sysadmin/sysadmin/monitor/web.py").exists()
    assert f'DEFAULT_STATE = "{resources["monitor-state"].path}/state.json"' in deliver
    # kd-boss-board runs as the board owner and writes its audit outside the root-only
    # state directory; stage_setup.sh creates that directory for the base user. The path lives in
    # the execution core it shares with the owner's promote page.
    audit = resources["board-audit"]
    executor = (ROOT / "common/kd/boss_exec.py").read_text("utf-8")
    assert (audit.planned, audit.secret) == (None, False)
    assert f'AUDIT = "{audit.path}"' in executor
    stage = (ROOT / "common/install/stage_setup.sh").read_text("utf-8")
    assert f'install -d -o "$base" -g "$base" -m 0750 {audit.path.rsplit("/", 1)[0]}' in stage


InMemory = Callable[[Registry], object]


def _capability_field(name: str, keys: tuple[str | int, ...], value: object) -> InMemory:
    def change(registry: Registry) -> None:
        target: Any = registry.capabilities[name]
        for key in keys[:-1]:
            target = target[key]
        target[keys[-1]] = value

    return change


# Each row changes a loaded registry in memory, past the file checks of load_registry.
# fmt: off
IN_MEMORY = [
    ("subcommand-injection", _capability_field("usage.read", ("wrappers", 0, "subcommands"), ["show; curl"]), "subcommands: invalid names"),
    ("unknown-capability-field", _capability_field("usage.read", ("permissions",), {"allow": ["Bash"]}), "capability usage.read: fields must be exactly"),
    ("wildcard-form", _capability_field("board.read", ("wrappers", 0, "subcommands"), ["task *"]), "wildcard or expansion character"),
    ("absolute-source", _capability_field("usage.read", ("wrappers", 0, "source"), "/opt/kd/kd-usage"), "absolute path in source"),
    ("name-key-mismatch", _capability_field("usage.read", ("name",), "board.read"), "capability usage.read: name does not match its key"),
    ("profile-default-rights", lambda r: r.profiles["coordinator"]["defaults"].__setitem__("capabilities", ["servers.execute"]), "defaults may only hold the role text"),
    ("sandbox-weakened", lambda r: r.sandboxes["coordinator-v1"]["settings"].__setitem__("allowUnsandboxedCommands", True), "settings must equal the fail-closed sandbox"),
    ("sandbox-unknown-field", lambda r: r.sandboxes["developer-v1"].__setitem__("filesystem", {"allowWrite": ["/"]}), "sandbox profile developer-v1: fields must be exactly"),
]
# fmt: on


@pytest.mark.parametrize(
    ("change", "expected"),
    [pytest.param(c, e, id=name) for name, c, e in IN_MEMORY],
)
def test_validate_registry_rechecks_entries_in_memory(
    change: InMemory, expected: str
) -> None:
    registry = copy.deepcopy(_tracked())
    validate_registry(registry)
    change(registry)
    with pytest.raises(RegistryError) as caught:
        validate_registry(registry)
    assert any(expected in error for error in caught.value.errors), caught.value.errors


def test_docs_read_is_a_direct_read_of_sysadmin_and_analyst() -> None:
    # M02: the sysadmin instructions read the project docs directly; the analyst too.
    registry = _tracked()
    entry = registry.capabilities["docs.read"]
    assert entry["resources"] == [{"name": "project-docs", "access": "read-direct"}]
    assert entry["wrappers"] == [] and entry["setup"] is None
    docs = registry.resources.resources["project-docs"]
    assert (docs.kind, docs.path, docs.secret) == ("dir", "/srv/example-docs", False)
    holders = {
        name
        for name in ("analyst", "coordinator", "developer", "sysadmin")
        if "docs.read" in read_json_object(ROOT / PROFILES / f"{name}.json")["capability_ceiling"]
    }
    assert holders == {"analyst", "sysadmin"}


def test_repo_read_is_a_grant_and_a_wrapper_without_a_shell() -> None:
    # the agent reads the checkouts by ACL and asks history through kd-repo; no network, no key, no guard.
    registry = _tracked()
    entry = registry.capabilities["repo.read"]
    assert entry["shell"] == "none" and entry["validator"] is None
    assert entry["setup"] == "common/install/repo_setup.sh"
    assert entry["resources"] == [{"name": "repo-checkouts", "access": "read-grant"}]
    [wrapper] = entry["wrappers"]
    assert (wrapper["command"], wrapper["run_as"], wrapper["executor"]) == ("kd-repo", "self", None)
    assert wrapper["subcommands"] == ["list", "log", "show", "diff", "blame"]
    assert entry["sandbox_ceiling"] == {"network_domains": [], "channel_commands": []}
    checkouts = registry.resources.resources["repo-checkouts"]
    assert (checkouts.kind, checkouts.path, checkouts.secret) == ("dir", "/srv/digipaddock-repos", False)
    profile = read_json_object(ROOT / PROFILES / "analyst.json")
    assert profile["capability_ceiling"] == ["board.read", "board.write", "data.read", "docs.read", "host.read",
                                             "monitor.read", "net.egress", "repo.read", "usage.read"]
    assert profile["sandbox_profiles"] == ["analyst-v1"]
    sandbox = read_json_object(ROOT / "common/agent-sandboxes/analyst-v1.json")
    assert sandbox["ceiling"] == {"network_domains": [], "channel_commands": ["kd-data", "kd-host-read",
                                                                             "kd-monitor-read", "kd-notes",
                                                                             "kd-task", "kd-work"],
                                  "writable": ["work"]}


def test_monitor_read_is_a_base_executor_for_boss_and_analyst_only() -> None:
    # The monitor's reads through kd-monitor-read — coordinator and analyst, not dev or sysadmin
    registry = _tracked()
    entry = registry.capabilities["monitor.read"]
    assert entry["shell"] == "none" and entry["conflicts"] == [] and entry["resources"] == []
    assert entry["sandbox_ceiling"] == {"network_domains": [], "channel_commands": ["kd-monitor-read"]}
    [wrapper] = entry["wrappers"]
    assert (wrapper["command"], wrapper["run_as"], wrapper["executor"]) == (
        "kd-monitor-read", "digi", "common/bin/kd-monitor-read")
    assert wrapper["subcommands"] == ["incident", "incidents", "server", "servers"]
    holders = {name for name, profile in registry.profiles.items() if "monitor.read" in profile["capability_ceiling"]}
    assert holders == {"coordinator", "analyst"}
    for agent, holds in (("boss", True), ("analyst", True), ("dev", False), ("sysadmin", False)):
        manifest = read_json_object(ROOT / "agents" / agent / "agent.json")
        assert ("monitor.read" in manifest["capabilities"]) is holds, agent


def test_data_read_is_the_data_users_executor_for_analyst_dev_and_boss_only() -> None:
    # kd-data under its own service user, the one holder of the data key; analyst, dev and boss hold it
    registry = _tracked()
    entry = registry.capabilities["data.read"]
    assert entry["module"] == "data" and entry["shell"] == "none" and entry["validator"] is None
    assert entry["setup"] == "common/install/data_read_setup.sh"
    assert entry["sandbox_ceiling"] == {"network_domains": [], "channel_commands": ["kd-data"]}
    assert all(r["access"] == "via-wrapper" for r in entry["resources"])
    [wrapper] = entry["wrappers"]
    assert (wrapper["command"], wrapper["run_as"], wrapper["executor"], wrapper["source"]) == (
        "kd-data", "digi-data", "common/bin/kd-data", "common/bin/kd-data")
    assert entry["lifecycle"]["install"] == ["executor-user", "setup", "wrappers", "channels"]
    holders = {name for name, profile in registry.profiles.items() if "data.read" in profile["capability_ceiling"]}
    assert holders == {"analyst", "coordinator", "developer"}
    for agent, holds in (("analyst", True), ("dev", True), ("boss", True), ("sysadmin", False)):
        manifest = read_json_object(ROOT / "agents" / agent / "agent.json")
        assert ("data.read" in manifest["capabilities"]) is holds, agent


def test_servers_read_runs_in_the_sandbox_behind_the_guard() -> None:
    # ssh-ro and local reads run sandboxed without a card; the guard validates them.
    entry = _tracked().capabilities["servers.read"]
    assert entry["shell"] == "sandboxed" and entry["validator"]


def test_monitor_manage_has_no_wake_self() -> None:
    # the monitor's tick queues a cabinet command for its own agent; no digipaddock pane to reach.
    entry = _tracked().capabilities["monitor.manage"]
    assert [w["command"] for w in entry["wrappers"]] == ["kd-monitor"]
    assert entry["sandbox_ceiling"]["channel_commands"] == ["kd-monitor"]
    assert "channels" in entry["lifecycle"]["install"] and "channels" in entry["lifecycle"]["disable"]
    assert "kd-wake-self" not in json.dumps(entry)


def test_worker_request_is_a_coordinator_capability_through_the_base_channel(registry_root: Path) -> None:
    reg = load_registry(registry_root)
    cap = reg.capabilities["worker.request"]
    (wrapper,) = cap["wrappers"]
    assert wrapper["command"] == "kd-worker" and wrapper["subcommands"] == ["request", "status", "result", "cancel"]
    assert wrapper["run_as"] == naming.base_user() and wrapper["executor"] == "common/bin/kd-worker"
    assert "worker.request" in reg.profiles["coordinator"]["capability_ceiling"]
    assert "kd-worker" in reg.sandboxes["coordinator-v1"]["ceiling"]["channel_commands"]
    assert "worker.request" not in reg.profiles["developer"]["capability_ceiling"]
    assert "board.read" in cap["dependencies"] and {"board.write", "git.develop", "servers.execute"} <= set(cap["conflicts"])
    assert reg.resources.resources["worker-jobs"].path == "/var/lib/digipaddock/worker"


def test_a_deployments_own_protected_paths_come_from_its_overlay_file(
    tmp_path, monkeypatch
):
    """Review 04.11.2026: no deployment's secret file names in agent-resources.json; host/protected.json adds them."""
    from kd import registry as reg

    host = tmp_path / "host.json"
    host.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("KD_HOST_JSON", str(host))
    assert reg._deployment_protected() == ([], [])  # no file: the public table only
    (tmp_path / "protected.json").write_text(
        '{"schema_version": 1, "protected": [{"kind": "pattern", "path": "/var/www/**/k.json", "deny": ["read"]}]}'
    )
    found, errors = reg._deployment_protected()
    assert errors == [] and [(p.kind, p.path, p.deny) for p in found] == [
        ("pattern", "/var/www/**/k.json", ("read",))
    ]
    (tmp_path / "protected.json").write_text(
        '{"schema_version": 1, "protected": [{"kind": "pattern"}]}'
    )
    assert reg._deployment_protected()[1]


def test_net_egress_is_every_agents_capability() -> None:
    """The autonomy design, piece 2: an exit is added at launch from host/egress.json — a card's or the
    host's standing one; the capability itself opens no name and runs no wrapper, it only lets the launch add an
    exit. Which agents an exit serves is the deployment's choice, in that file."""
    registry = _tracked()
    entry = registry.capabilities["net.egress"]
    assert entry["module"] == "net" and entry["wrappers"] == []
    assert entry["sandbox_ceiling"] == {"network_domains": [], "channel_commands": []}
    assert entry["resources"] == [{"name": "egress-config", "access": "read-direct"}]
    holders = {name for name, profile in registry.profiles.items() if "net.egress" in profile["capability_ceiling"]}
    assert holders == set(registry.profiles)
    for agent in ("dev", "analyst", "boss", "sysadmin"):
        manifest = read_json_object(ROOT / "agents" / agent / "agent.json")
        assert "net.egress" in manifest["capabilities"], agent
