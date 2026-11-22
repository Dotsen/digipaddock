"""Worker policy (C02): the effective dev policy narrowed to one specialization's limits in one slot."""

import copy
import dataclasses
import difflib
import json
from pathlib import Path

import pytest

from kd.policy import render_policy
from kd.registry import read_json_file
from kd.specializations import SPECS_ROOT, for_type, load_registry
from kd.worker_policy import (
    Slot,
    WorkerPolicyError,
    compile_worker_policy,
    rebase,
    widenings,
)

ROOT = Path(__file__).resolve().parents[2]
FLOOR = read_json_file(ROOT / "common/claude/managed-settings.template.json")
DEV = json.loads(
    (ROOT / "common/tests/fixtures/policy/dev.policy.json").read_text("utf-8")
)
SNAPSHOTS = ROOT / "common/tests/fixtures/worker"
AGENTS = "/srv/digipaddock-agents"
W1, W2 = Slot.parse("dev", "worker1"), Slot.parse("dev", "worker2")
REG = load_registry(SPECS_ROOT, require_root_owner=False)
SPECS = sorted(for_type(REG, "developer"))  # docs, implementer, reviewer, test-author, tester
WRITERS = [n for n in SPECS if REG.specs[n].limits.write == "repo"]


def _compile(name, developer=DEV, slot=W1):
    return compile_worker_policy(developer, REG.specs[name], slot, agents_root=AGENTS, floor=FLOOR)


def _perms(policy):
    return policy["settings"]["permissions"]


def _fs(policy):
    return policy["launch_settings"]["sandbox"]["filesystem"]


@pytest.mark.parametrize("role", SPECS)
def test_snapshot(role):
    expected = (SNAPSHOTS / f"{role}.policy.json").read_text("utf-8")
    actual = render_policy(_compile(role)).decode("utf-8")
    assert actual == expected, "".join(
        difflib.unified_diff(expected.splitlines(True), actual.splitlines(True))
    )


@pytest.mark.parametrize("role", SPECS)
def test_every_role_is_a_subset_of_the_rebased_developer(role):
    assert (
        widenings(rebase(DEV, W1, agents_root=AGENTS), _compile(role), floor=FLOOR)
        == []
    )


def test_slot_names_are_finite():
    assert (W1.ref, W1.root, W1.work, W1.repo, W1.tmp) == (
        "dev/worker1",
        "/srv/digipaddock-workers/dev/worker1",
        "/srv/digipaddock-workers/dev/worker1/work",
        "/srv/digipaddock-workers/dev/worker1/work/repo",
        "/srv/digipaddock-workers/dev/worker1/work/tmp",
    )
    assert Slot.from_ref("dev/worker2") == W2
    for agent, name in (
        ("dev", "worker0"), ("dev", "worker10"), ("dev", "worker1\n"), ("dev", "../x"),
        ("Dev", "worker1"), ("../x", "worker1"), ("", "worker1"), ("dev", ""), (None, "worker1"),
    ):
        with pytest.raises(WorkerPolicyError):
            Slot.parse(agent, name)
    for ref in ("worker1", "dev/worker1/x", "/dev/worker1", "dev//worker1", "dev/worker1\n", 7):
        with pytest.raises(WorkerPolicyError):
            Slot.from_ref(ref)


def test_the_policy_must_belong_to_the_slots_agent():
    foreign = Slot.parse("analyst", "worker1")
    with pytest.raises(WorkerPolicyError, match="belongs to analyst"):
        _compile("implementer", slot=foreign)


def test_other_pools_and_sibling_slots_are_denied():
    policy = compile_worker_policy(
        DEV, REG.specs["implementer"], W1, agents_root=AGENTS, floor=FLOOR, other_agents=("analyst", "sysadmin")
    )
    denied = set(_fs(policy)["denyRead"])
    assert {"/srv/digipaddock-workers/analyst", "/srv/digipaddock-workers/sysadmin", W2.root} <= denied
    assert not any(p == W1.root or p.startswith(W1.root + "/") for p in denied)
    rules = set(_perms(policy)["deny"])
    assert {"Read(//srv/digipaddock-workers/analyst/**)", f"Read(/{W2.root}/**)"} <= rules
    for bad in (("dev",), ("Dev",), ("../x",), (7,)):
        with pytest.raises(WorkerPolicyError):
            compile_worker_policy(DEV, REG.specs["implementer"], W1, agents_root=AGENTS, floor=FLOOR, other_agents=bad)


def test_a_name_instead_of_a_registry_entry_is_refused():
    with pytest.raises(WorkerPolicyError, match="registry specialization"):
        compile_worker_policy(DEV, "implementer", W1, agents_root=AGENTS, floor=FLOOR)


def test_worker_block_names_the_spec_version_and_hash():
    spec = REG.specs["test-author"]
    block = _compile("test-author")["worker"]
    assert block == {"spec": "test-author", "version": 1, "sha256": spec.sha256, "slot": "worker1",
                     "source": DEV["agent"], "cwd": W1.work}


@pytest.mark.parametrize("change, why", [
    (lambda s: dataclasses.replace(s, type="analyst"), "analyst, the source policy is 'developer'"),
    (lambda s: dataclasses.replace(s, limits=dataclasses.replace(s.limits, capabilities=("board.write",))),
     "capability channels are not available"),
    (lambda s: dataclasses.replace(s, limits=dataclasses.replace(s.limits, network="all")), "no network"),
])
def test_limits_outside_the_runtime_are_refused(change, why):
    with pytest.raises(WorkerPolicyError, match=why):
        compile_worker_policy(DEV, change(REG.specs["implementer"]), W1, agents_root=AGENTS, floor=FLOOR)


def test_rebase_moves_the_agent_folder_to_the_slot():
    # The slot mirrors the developer: its work/ holds the job's repo and tmp.
    allow = _perms(rebase(DEV, W1, agents_root=AGENTS))["allow"]
    assert "Edit(//srv/digipaddock-workers/dev/worker1/work/**)" in allow
    # the agent folder and the user never leak; "dev" itself names the pool now
    assert not any(f"{AGENTS}/dev" in rule or "digi-dev" in rule for rule in allow)


def test_write_limits_are_the_ceiling():
    # none: not one Edit rule, the repo and the tmp directory are write-denied; temp: Edit(tmp) only;
    # repo: Edit(tmp) and Edit(repo). Nothing else distinguishes the specializations' policies.
    tmp, repo = "/srv/digipaddock-workers/dev/worker1/work/tmp", "/srv/digipaddock-workers/dev/worker1/work/repo"
    none = _compile("reviewer")
    assert not [r for r in _perms(none)["allow"] if r.startswith("Edit(")]
    assert {tmp, repo} <= set(_fs(none)["denyWrite"]) and f"Edit(/{tmp}/**)" in _perms(none)["deny"]
    temp = _compile("tester")
    assert [r for r in _perms(temp)["allow"] if r.startswith("Edit(")] == [f"Edit(/{tmp}/**)"]
    assert repo in _fs(temp)["denyWrite"] and tmp not in _fs(temp)["denyWrite"]
    for name in WRITERS:
        policy = _compile(name)
        assert [r for r in _perms(policy)["allow"] if r.startswith("Edit(")] == [f"Edit(/{repo}/**)", f"Edit(/{tmp}/**)"]
        assert repo not in _fs(policy)["denyWrite"]


@pytest.mark.parametrize("role", SPECS)
def test_the_job_runs_in_a_writable_work_folder_that_holds_its_repo(role):
    # F04: the sandbox makes its bind targets (CLAUDE.local.md) in the cwd, so the cwd is
    # writable even for a reader; the repo inside it needs no additional directory.
    policy = _compile(role)
    assert policy["worker"]["cwd"] == W1.work
    assert W1.repo.startswith(W1.work + "/") and W1.tmp.startswith(W1.work + "/")
    deny_write = _fs(policy)["denyWrite"]
    assert not any(W1.work == p or W1.work.startswith(p + "/") for p in deny_write)
    # The developer's root placeholders keep their place in the slot (C03 writes them).
    assert {f"{W1.work}/CLAUDE.md", f"{W1.work}/CLAUDE.local.md"} <= set(deny_write)


@pytest.mark.parametrize("role", SPECS)
def test_a_worker_gets_no_agents_schedules_remote_triggers_or_messages(role):
    # F04: these tools were offered to a worker; none of them belongs to a job.
    deny = set(_perms(_compile(role))["deny"])
    assert {
        "Agent",
        "CronCreate",
        "CronDelete",
        "CronList",
        "DesignSync",
        "EnterWorktree",
        "ExitWorktree",
        "ListAgents",
        "PushNotification",
        "RemoteTrigger",
        "ScheduleWakeup",
        "SendMessage",
        "Task",
        "Workflow",
    } <= deny


@pytest.mark.parametrize("role", SPECS)
def test_worker_has_no_channel_board_forge_network_or_secrets(role):
    policy = _compile(role)
    perms, sandbox = _perms(policy), policy["launch_settings"]["sandbox"]
    assert perms["ask"] == [] and perms["additionalDirectories"] == []
    assert not any(r.startswith(("mcp__", "Bash(")) for r in perms["allow"])
    assert {
        "Skill",
        "WebFetch",
        "WebSearch",
        "Bash(kd-merge *)",
        "Bash(kd-pr *)",
        "Bash(git push*)",
        # global review 15.10.2026: the channel clients and the board/coordinator executors of its agent
        "Bash(kd-cap *)",
        "Bash(kd-work *)",
        "Bash(kd-task *)",
        "Bash(kd-worker *)",
        "Bash(kd-nudge *)",
        "Bash(kd-boss-board *)",
        "Bash(kd-diag *)",  # a worker posts no diagnostics on the card as its agent
        "Bash(kd-host-read *)",  # the host reads stay with the permanent agent
        "Bash(kd-monitor-read *)",  # the monitor's reads stay with the permanent agent
        "Bash(kd-data *)",  # the databases' reads stay with the permanent agent
    } <= set(perms["deny"])
    assert (
        sandbox["excludedCommands"] == [] and sandbox["network"]["allowedDomains"] == []
    )
    assert _fs(policy)["allowRead"] == []
    assert {
        "/home/digi-dev",
        "/srv/digipaddock-agents",
        "/srv/digipaddock-workers/dev/worker2",
    } <= set(_fs(policy)["denyRead"])
    assert sandbox["allowUnsandboxedCommands"] is False and sandbox["enabled"] is True
    assert perms["disableBypassPermissionsMode"] == "disable"
    hooks = policy["settings"]["hooks"]["PreToolUse"]
    assert {g["matcher"] for g in hooks} == {"Bash"}


def _widened_developer():
    dev = copy.deepcopy(DEV)
    perms, sandbox = dev["settings"]["permissions"], dev["launch_settings"]["sandbox"]
    perms["allow"] += [
        "WebFetch",
        "Bash(curl *)",
        "Edit(//srv/digipaddock-agents/dev/**)",
        "Write",
    ]
    perms["additionalDirectories"].append("/var")
    sandbox["network"]["allowedDomains"].append("pypi.org")
    sandbox["excludedCommands"].append("curl *")
    sandbox["filesystem"]["allowRead"].append("/etc/shadow")
    dev["acl"].append(
        {"access": "write", "path": "/var/lib/x", "resource": "x", "user": "digi-dev"}
    )
    return dev


@pytest.mark.parametrize("role", SPECS)
def test_a_wider_developer_does_not_widen_the_worker(role):
    assert _compile(role, developer=_widened_developer()) == _compile(role)


def _widen(path, value):
    def apply(policy):
        target = policy
        for key in path[:-1]:
            target = target[key]
        if callable(value):
            value(target, path[-1])
        else:
            target[path[-1]] = value

    return apply


P, S = ("settings", "permissions"), ("launch_settings", "sandbox")
WIDENINGS = {
    "allow": _widen((*P, "allow"), lambda t, k: t[k].append("Bash(curl *)")),
    # Widen beyond the developer: what dev itself has is not a widening of the subset.
    "deny": _widen((*P, "deny"), lambda t, k: t[k].remove("Bash(apt*)")),
    "ask-from-deny": _widen((*P, "ask"), lambda t, k: t[k].append("Bash(apt*)")),
    "mode": _widen((*P, "disableBypassPermissionsMode"), "enable"),
    "default-mode": _widen((*P, "defaultMode"), "acceptEdits"),
    "additional-dir": _widen(
        (*P, "additionalDirectories"), lambda t, k: t[k].append("/var")
    ),
    "domain": _widen(
        (*S, "network", "allowedDomains"), lambda t, k: t[k].append("pypi.org")
    ),
    "excluded": _widen((*S, "excludedCommands"), lambda t, k: t[k].append("curl *")),
    "allow-read": _widen(
        (*S, "filesystem", "allowRead"), lambda t, k: t[k].append("/etc/shadow")
    ),
    "deny-read": _widen(
        (*S, "filesystem", "denyRead"),
        lambda t, k: t[k].remove("/etc/hindsight"),
    ),
    "deny-write": _widen(
        (*S, "filesystem", "denyWrite"), lambda t, k: t[k].remove("/opt/digipaddock")
    ),
    "unsandboxed": _widen((*S, "allowUnsandboxedCommands"), True),
    "auto-bash": _widen((*S, "autoAllowBashIfSandboxed"), True),
    "no-sandbox": _widen(("launch_settings", "sandbox", "enabled"), False),
    "bash-approval": _widen(("bash_approval",), "rules"),
    "guard": _widen(("settings", "hooks", "PreToolUse"), lambda t, k: t[k].clear()),
}


@pytest.mark.parametrize("dimension", sorted(WIDENINGS))
def test_every_widened_dimension_is_reported(dimension):
    base = rebase(DEV, W1, agents_root=AGENTS)
    worker = _compile("implementer")
    WIDENINGS[dimension](worker)
    assert widenings(base, worker, floor=FLOOR), dimension


@pytest.mark.parametrize("role", SPECS)
def test_worker_opens_no_channel_socket(role):
    # the developer reaches its channels through Unix sockets; a worker of the same
    # user opens none and sees nothing under /run (not a widening check: dev has them).
    worker = _compile(role)
    sandbox = worker["launch_settings"]["sandbox"]
    assert sandbox["network"]["allowAllUnixSockets"] is False
    assert "/run" in _fs(worker)["denyRead"] and "/var/run" in _fs(worker)["denyRead"]
    assert _fs(worker)["allowRead"] == []


def test_compile_refuses_a_developer_without_a_fail_closed_sandbox():
    dev = copy.deepcopy(DEV)
    dev["launch_settings"]["sandbox"]["allowUnsandboxedCommands"] = True
    with pytest.raises(WorkerPolicyError, match="sandbox"):
        _compile("implementer", developer=dev)


def test_compile_refuses_a_non_developer_source():
    dev = copy.deepcopy(DEV)
    dev["profile"] = "sysadmin"
    with pytest.raises(WorkerPolicyError, match="developer"):
        _compile("implementer", developer=dev)


def test_every_other_slot_is_denied_without_the_caller_naming_them():
    deny_read = set(_fs(_compile("implementer"))["denyRead"])
    for n in range(2, 10):
        assert f"/srv/digipaddock-workers/dev/worker{n}" in deny_read
    assert not any(p.startswith(W1.root) for p in deny_read)


@pytest.mark.parametrize("role", SPECS)
def test_a_worker_runs_as_the_developer_user_with_no_home_of_its_own(role):
    # One user per agent type: a slot is a folder and a unit under the dev user.
    policy = _compile(role)
    assert policy["user"] == DEV["user"] == "digi-dev"
    assert rebase(DEV, W1, agents_root=AGENTS)["user"] == "digi-dev"
    assert "digi-worker" not in json.dumps(policy)
    # The whole dev home stays closed: its channel token, memory hooks and keys.
    assert f"/home/{DEV['user']}" in _fs(policy)["denyRead"]


def test_compile_takes_no_request_paths_or_domains():
    import inspect

    params = set(inspect.signature(compile_worker_policy).parameters)
    assert params == {
        "developer",
        "spec",
        "slot",
        "agents_root",
        "floor",
        "other_agents",  # agent names only, never paths
    }


@pytest.mark.parametrize("role", SPECS)
def test_sandbox_read_denials_are_paired_with_permission_rules(role):
    # The sandbox binds Bash only; Read, Glob, Grep and Edit obey permission rules.
    from kd.policy import covers

    policy = _compile(role)
    deny = _perms(policy)["deny"]
    for path in _fs(policy)["denyRead"]:
        for tool in ("Read", "Edit"):
            assert any(covers(rule, f"{tool}(/{path}/**)") for rule in deny), (
                tool,
                path,
            )
    repo_edit = any(covers(rule, f"Edit(/{W1.repo}/**)") for rule in deny)
    assert repo_edit is (role not in WRITERS)


def test_rebase_keeps_work_rules_whole_over_repo_and_tmp():
    dev = copy.deepcopy(DEV)
    dev["launch_settings"]["sandbox"]["filesystem"]["denyWrite"].append(
        "/srv/digipaddock-agents/dev/work"
    )
    base = rebase(dev, W1, agents_root=AGENTS)
    deny = _perms(base)["deny"]
    # work/** binds both repo/ and tmp/, which live under the slot's work/.
    assert f"Edit(/{W1.work}/**/.claude/**)" in deny
    assert f"Edit(/{W1.work}/**/.mcp.json)" in deny
    assert W1.work in _fs(base)["denyWrite"]
    # exact files keep their one place
    assert f"Edit(/{W1.work}/CLAUDE.md)" in deny
    assert f"Edit(/{W1.repo}/CLAUDE.md)" not in deny


def _with_hook(matcher, command="/opt/digipaddock/common/bin/guard-x || exit 2"):
    dev = copy.deepcopy(DEV)
    dev["settings"]["hooks"]["PreToolUse"].append(
        {
            "hooks": [{"command": command, "timeout": 5, "type": "command"}],
            "matcher": matcher,
        }
    )
    return dev


def test_a_regex_matcher_guard_is_never_dropped_on_purpose():
    # "mcp__hindsight__.*|Edit" also guards Edit: dropping it is a widening.
    with pytest.raises(WorkerPolicyError, match="widens"):
        _compile("implementer", developer=_with_hook("mcp__hindsight__.*|Edit"))


def test_a_guard_of_a_denied_tool_with_pipes_in_its_command_is_dropped():
    policy = _compile(
        "implementer", developer=_with_hook("WebFetch", "/opt/x/g.py || exit 2 | tee")
    )
    assert {g["matcher"] for g in policy["settings"]["hooks"]["PreToolUse"]} == {"Bash"}


def test_a_rule_over_the_agents_folder_binds_the_slot_too():
    dev = copy.deepcopy(DEV)
    dev["settings"]["permissions"]["deny"].append("Edit(//srv/digipaddock-agents/**)")
    deny = _perms(rebase(dev, W1, agents_root=AGENTS))["deny"]
    assert {"Edit(//srv/digipaddock-agents/**)", f"Edit(/{W1.root}/**)"} <= set(deny)


# --- the inputs of a job are read, never written ---------------------------------------------------
@pytest.mark.parametrize("role", SPECS)
def test_no_role_writes_its_inputs(role):
    policy = _compile(role)
    assert W1.inputs == f"{W1.work}/inputs"
    assert W1.inputs in _fs(policy)["denyWrite"]
    assert f"Edit(/{W1.inputs}/**)" in _perms(policy)["deny"]
    assert f"Read(/{W1.inputs}/**)" not in _perms(policy)["deny"]


# --- a worker of a reader (the analyst: a council's critic) -----------------------------------------
# The analyst holds no bare Read/Glob/Grep — its reads are by path — so the ceiling's bare tools leave its worker
# nothing: it keeps the reads a job cannot do without, its own slot and the grants its specialization requires.
A1 = Slot.parse("analyst", "worker1")
REPOS = "/srv/digipaddock-repos"


def _analyst():
    import test_policy

    return json.loads(render_policy(test_policy._compile("analyst")).decode("utf-8"))


def _critic(spec=None, analyst=None):
    return compile_worker_policy(
        analyst or _analyst(),
        spec or REG.specs["brief-critic"],
        A1,
        agents_root=AGENTS,
        floor=FLOOR,
        other_agents=("dev",),
    )


def test_the_analyst_itself_reads_by_path_only():
    allow = _perms(_analyst())["allow"]
    assert not {"Read", "Glob", "Grep"} & set(allow)
    assert f"Read(/{REPOS}/**)" in allow and REPOS in _perms(_analyst())["additionalDirectories"]


def test_a_critic_reads_its_slot_and_the_repositories_and_nothing_else():
    policy = _critic()
    perms = _perms(policy)

    assert perms["allow"] == sorted([f"Read(/{REPOS}/**)", f"Read(/{A1.root}/**)"])
    assert perms["additionalDirectories"] == [REPOS]
    assert perms["ask"] == [] and perms["defaultMode"] == "dontAsk"
    # written by nobody: the inputs, the repositories, anything
    assert f"Edit(/{A1.inputs}/**)" in perms["deny"] and A1.inputs in _fs(policy)["denyWrite"]
    assert not any(rule.startswith(("Bash", "Edit", "Write")) for rule in perms["allow"])
    # no board, no docs, no other slot, no pool of another agent, not the agent's own folder
    for path in ("/srv/digipaddock-agents/board", "/srv/example-docs", "/srv/digipaddock-agents/analyst"):
        assert not any(path in rule for rule in perms["allow"]), path
    denied = set(_fs(policy)["denyRead"])
    assert {"/srv/digipaddock-agents", "/srv/digipaddock-workers/dev", f"{A1.root[:-1]}2"} <= denied


def test_a_critic_is_a_subset_of_the_rebased_analyst():
    analyst = _analyst()
    assert widenings(rebase(analyst, A1, agents_root=AGENTS), _critic(analyst=analyst), floor=FLOOR) == []


def test_a_grant_the_specialization_does_not_require_is_not_kept():
    bare = dataclasses.replace(REG.specs["brief-critic"], requires=())

    perms = _perms(_critic(spec=bare))

    assert perms["allow"] == [f"Read(/{A1.root}/**)"] and perms["additionalDirectories"] == []


def test_a_grant_the_agent_does_not_hold_is_not_made_up():
    analyst = _analyst()
    analyst["acl"] = [entry for entry in analyst["acl"] if entry["resource"] != "repo-checkouts"]
    perms = _perms(analyst)
    perms["allow"] = [rule for rule in perms["allow"] if REPOS not in rule]
    perms["additionalDirectories"] = [path for path in perms["additionalDirectories"] if path != REPOS]

    kept = _perms(_critic(analyst=analyst))

    assert kept["allow"] == [f"Read(/{A1.root}/**)"] and kept["additionalDirectories"] == []


@pytest.mark.parametrize("role", SPECS)
def test_a_developers_worker_keeps_its_bare_tools_and_gains_no_scoped_rule(role):
    perms = _perms(_compile(role))
    assert {"Glob", "Grep", "Read"} <= set(perms["allow"])
    assert not any(rule.startswith("Read(") for rule in perms["allow"]) and perms["additionalDirectories"] == []


def test_a_code_critics_worker_reads_and_runs_git_and_writes_nothing():
    """a critic of the code reads the pull request's head in its slot. The read tools and the shell
    under the agent's own validator (git reads); not one Edit rule, the copy, the tmp folder and the job's inputs
    write-denied, no network and no push."""
    work = "/srv/digipaddock-workers/dev/worker1/work"
    policy = _compile("code-critic")
    perms = _perms(policy)
    assert perms["allow"] == ["Glob", "Grep", "Read"]
    assert {f"{work}/repo", f"{work}/tmp", f"{work}/inputs"} <= set(_fs(policy)["denyWrite"])
    assert {f"Edit(/{work}/repo/**)", f"Edit(/{work}/tmp/**)", f"Edit(/{work}/inputs/**)"} <= set(perms["deny"])
    assert [g["matcher"] for g in policy["settings"]["hooks"]["PreToolUse"]] == ["Bash"]  # the validator stays
    assert "Bash(git push*)" in perms["deny"]
    assert policy["launch_settings"]["sandbox"]["network"]["allowedDomains"] == []
    reviewer = _compile("reviewer")
    assert perms == _perms(reviewer) and policy["launch_settings"] == reviewer["launch_settings"]
