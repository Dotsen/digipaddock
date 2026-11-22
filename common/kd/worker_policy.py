"""Worker policy (spec 5.7, 5.8.4): the effective agent policy narrowed to one specialization's
limits in one slot.

The agent policy is first rebased into the slot's paths, then intersected with the ceiling
the specialization's limits define (write none/temp/repo; no capability channel and no
network in this runtime). The ceiling never grows with the agent, so a wider agent cannot
widen a worker; and every result must compare to the rebased agent without a single
widening (kd.policy.compare_views), or compilation fails closed.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from kd import worker_inputs
from kd.policy import CHANNEL_SOCKETS, PERMISSION_MODES, compare_views, covers, effective_view
from kd.registry import FAIL_CLOSED_SANDBOX
from kd.specializations import Limits, Specialization

SCHEMA_VERSION = 1
WORKERS_ROOT = "/srv/digipaddock-workers"
SLOT_RE = re.compile(r"^worker[1-9]$")  # SLOT_COUNT names
AGENT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")  # kd.agents.NAME_RE, pinned here: no registry read
PLAIN_MATCHER = re.compile(r"^[A-Za-z0-9_]+$")
SLOT_COUNT = 9  # worker1..worker9: the finite slot names
# Tools and commands no worker gets, whatever the developer has: no channel, memory, board,
# forge, network fetches, skill discovery, subagents, schedules, remote triggers or messages
# (F04 found the latter offered to a worker). Deny is a union with the developer's deny.
WORKER_DENY = (
    "Agent",
    "Bash(curl *)",
    "Bash(git push*)",
    "Bash(kd-boss-board *)",
    "Bash(kd-cap *)",
    "Bash(kd-data *)",
    "Bash(kd-diag *)",
    "Bash(kd-forge *)",
    "Bash(kd-host-read *)",
    "Bash(kd-merge *)",
    "Bash(kd-monitor-read *)",
    "Bash(kd-nudge *)",
    "Bash(kd-pr *)",
    "Bash(kd-task *)",
    "Bash(kd-work *)",
    "Bash(kd-worker *)",
    "Bash(wget *)",
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
    "Skill",
    "Task",
    "WebFetch",
    "WebSearch",
    "Workflow",
    "mcp__hindsight",
)
MODE_FIELDS = tuple(PERMISSION_MODES)
# The read grants a worker of a reader keeps: the capability a specialization requires -> the resource
# its agent reads by grant. A resource's name, never its path: the path is the agent policy's ACL entry.
READ_GRANTS = {"repo.read": "repo-checkouts"}


class WorkerPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Slot:
    """One prepared worker slot of one permanent agent; everything derives from the two finite
    names. A slot is a folder and a unit under the agent's own user (spec 5.8.1): it has no
    user, home or Claude login of its own."""

    agent: str
    name: str

    @classmethod
    def parse(cls, agent: Any, name: Any) -> Slot:
        # fullmatch: "$" alone would let a trailing newline through
        if not isinstance(agent, str) or not AGENT_RE.fullmatch(agent):
            raise WorkerPolicyError(f"bad agent name {agent!r}")
        if not isinstance(name, str) or not SLOT_RE.fullmatch(name):
            raise WorkerPolicyError(f"bad slot name {name!r}")
        return cls(agent, name)

    @classmethod
    def from_ref(cls, ref: Any) -> Slot:
        """``agent/workerN`` — the form of the enabled list and of a job's slot field."""
        if not isinstance(ref, str) or ref.count("/") != 1:
            raise WorkerPolicyError(f"bad slot reference {ref!r}")
        agent, name = ref.split("/")
        return cls.parse(agent, name)

    @property
    def ref(self) -> str:
        return f"{self.agent}/{self.name}"

    @property
    def root(self) -> str:
        return f"{WORKERS_ROOT}/{self.agent}/{self.name}"

    @property
    def work(self) -> str:
        """The job's cwd, like the agent's work/: writable, so the sandbox can make its
        bind targets there (F04), and holding the repo, so no directory is added."""
        return f"{self.root}/work"

    @property
    def repo(self) -> str:
        return f"{self.work}/repo"

    @property
    def tmp(self) -> str:
        return f"{self.work}/tmp"

    @property
    def inputs(self) -> str:
        """What a job of the cabinet reads instead of the board (kd.worker_inputs): root's files, read only."""
        return f"{self.work}/{worker_inputs.DIR}"


def _mapping(
    developer: Mapping[str, Any], slot: Slot, agents_root: str
) -> list[tuple[str, str]]:
    # The slot mirrors the agent folder, so one prefix moves: work/ and work/tmp follow.
    return [(f"{agents_root}/{developer['agent']}", slot.root)]


def _rebase_path(
    path: str, pairs: list[tuple[str, str]], *, directory: bool = False
) -> list[str]:
    """``path`` in slot terms. A pattern that reaches below a remapped prefix (a ``**``
    right after its literal part, or a sandbox directory) is carried to that prefix too:
    a developer deny on ``/srv/digipaddock-agents/**`` binds the slot as well."""
    star = path.find("*")
    base = path if star < 0 else path[:star].rstrip("/")
    rest = path[len(base) :]
    out = path
    for old, new in pairs:
        if base == old or base.startswith(old + "/"):
            out = new + path[len(old) :]
            break
    result = [out]
    if (directory and star < 0) or rest.startswith("/**"):
        for old, new in pairs:
            if old.startswith(base + "/") and new + rest not in result:
                result.append(new + rest)
    return result


def _rebase_rule(rule: str, pairs: list[tuple[str, str]]) -> list[str]:
    tool, sep, rest = rule.partition("(")
    if not sep or tool == "Bash" or not rest.startswith("//"):
        return [rule]
    return [f"{tool}(/{path})" for path in _rebase_path(rest[1:-1], pairs)]


def _rules(rules: Sequence[str], pairs: list[tuple[str, str]]) -> list[str]:
    return sorted({out for rule in rules for out in _rebase_rule(rule, pairs)})


def _paths(paths: Sequence[str], pairs: list[tuple[str, str]]) -> list[str]:
    # Sandbox paths and additional directories name whole directories.
    return sorted(
        {out for path in paths for out in _rebase_path(path, pairs, directory=True)}
    )


def rebase(
    developer: Mapping[str, Any], slot: Slot, *, agents_root: str
) -> dict[str, Any]:
    """The developer policy with its agent folder moved to ``slot``; user and home stay."""
    pairs = _mapping(developer, slot, agents_root)
    out = copy.deepcopy(dict(developer))
    perms = out["settings"]["permissions"]
    for field in ("allow", "ask", "deny"):
        perms[field] = _rules(perms.get(field, []), pairs)
    perms["additionalDirectories"] = _paths(
        perms.get("additionalDirectories", []), pairs
    )
    fs = out["launch_settings"]["sandbox"]["filesystem"]
    for field in list(fs):
        fs[field] = _paths(fs[field], pairs)
    return out


def _view(policy: Mapping[str, Any], floor: Mapping[str, Any]) -> dict[str, Any]:
    acl = [f"{entry['access']}:{entry['resource']}" for entry in policy.get("acl", [])]
    return effective_view(
        floor=floor,
        settings=policy["settings"],
        launch_settings=policy["launch_settings"],
        bash_approval=policy["bash_approval"],
        acl=acl,
    )


def _denied_tool(matcher: str, deny: Sequence[str]) -> bool:
    # Only a plain tool name can be matched against deny rules; a regex matcher such as
    # "mcp__x__.*|Edit" may guard a reachable tool and is never treated as denied.
    if not PLAIN_MATCHER.match(matcher):
        return False
    return any(
        covers(rule, matcher)
        # A bare "mcp__<server>" deny rule covers every tool of that server.
        or (
            rule.startswith("mcp__")
            and "(" not in rule
            and matcher.startswith(rule + "__")
        )
        for rule in deny
    )


def _hooks_kept(policy: Mapping[str, Any], deny: Sequence[str]) -> dict[str, Any]:
    """``policy`` without the hooks a worker drops on purpose: the guards of tools it
    cannot call. Every other hook must survive in the worker."""
    out = copy.deepcopy(dict(policy))
    hooks = out["settings"].get("hooks", {})
    for event, groups in hooks.items():
        hooks[event] = [group for group in groups if not _denied_tool(group["matcher"], deny)]
    return out


def widenings(
    base: Mapping[str, Any], worker: Mapping[str, Any], *, floor: Mapping[str, Any]
) -> list[str]:
    """Every right ``worker`` has that ``base`` does not; empty means worker ⊆ base."""
    deny = worker["settings"]["permissions"].get("deny", [])
    # The managed floor overrides permission modes in the effective view; the worker's own
    # modes must still match, so the policy stays closed without relying on the floor.
    own, theirs = worker["settings"]["permissions"], base["settings"]["permissions"]
    changed = [
        f"changed mode:{key}" for key in MODE_FIELDS if own.get(key) != theirs.get(key)
    ]
    return changed + [
        f"{diff.change} {diff.item}"
        for diff in compare_views(
            _view(_hooks_kept(base, deny), floor), _view(worker, floor)
        )
        if diff.widens
    ]


def _fail_closed(sandbox: Mapping[str, Any]) -> bool:
    # The developer's own channel sockets stay closed to a worker: its sandbox
    # opens no Unix socket and sees nothing under /run.
    network = sandbox.get("network", {})
    return all(
        sandbox.get(k) == v for k, v in FAIL_CLOSED_SANDBOX.items() if k != "network"
    ) and all(
        network.get(k) == v
        for k, v in FAIL_CLOSED_SANDBOX["network"].items()
        if k != CHANNEL_SOCKETS
    )


def _ceiling(limits: Limits, slot: Slot) -> tuple[list[str], list[str]]:
    """Allow rules and write-denied directories of the limits: ``none`` edits nothing (the
    repo is a snapshot of a fixed commit), ``temp`` edits the slot's tmp only, ``repo`` the
    working copy as well. The job's inputs are never written, whatever the limits."""
    allow = ["Glob", "Grep", "Read"]
    deny_write: list[str] = [slot.inputs]
    if limits.write == "none":
        deny_write += [slot.repo, slot.tmp]
    else:
        allow.append(f"Edit(/{slot.tmp}/**)")
        if limits.write == "repo":
            allow.append(f"Edit(/{slot.repo}/**)")
        else:
            deny_write.append(slot.repo)
    return allow, deny_write


def _scoped_reads(
    developer: Mapping[str, Any], perms: Mapping[str, Any], spec: Specialization, slot: Slot, allow: Sequence[str]
) -> tuple[list[str], list[str]]:
    """The by-path read rules and directories a worker of a reader keeps. An agent without a shell holds no bare
    Read/Glob/Grep — it reads by path — so the ceiling's bare tools leave its worker nothing to read with. Such a
    worker keeps the agent's own rules, never new ones, for exactly two places: its slot (the job's inputs) and
    the read grants of the capabilities its specialization requires. An agent that holds the bare tool keeps it
    and gains nothing here."""
    names = {READ_GRANTS[name] for name in spec.requires if name in READ_GRANTS}
    granted = {
        entry["path"]
        for entry in developer.get("acl", [])
        if entry.get("access") == "read" and entry.get("resource") in names
    }
    rules = []
    for rule in perms["allow"]:
        tool, sep, rest = rule.partition("(")
        if not sep or tool in allow or tool not in ("Glob", "Grep", "Read") or not rest.endswith("/**)"):
            continue
        if rest[1 : -len("/**)")] in {slot.root, *granted}:
            rules.append(rule)
    return rules, sorted(path for path in perms.get("additionalDirectories", []) if path in granted)


def compile_worker_policy(
    developer: Mapping[str, Any],
    spec: Specialization,
    slot: Slot,
    *,
    agents_root: str,
    floor: Mapping[str, Any],
    other_agents: Sequence[str] = (),
) -> dict[str, Any]:
    """The policy of ``spec`` in ``slot``; raises WorkerPolicyError instead of widening.

    ``other_agents`` are the other pool owners: their pools are denied like sibling slots."""
    if not isinstance(spec, Specialization):
        raise WorkerPolicyError("a registry specialization is required")
    if developer.get("profile") != spec.type:
        raise WorkerPolicyError(
            f"{spec.name} is a specialization of {spec.type}, the source policy is {developer.get('profile')!r}"
        )
    if developer.get("agent") != slot.agent:
        raise WorkerPolicyError(
            f"slot {slot.ref} belongs to {slot.agent}, the source policy is {developer.get('agent')!r}"
        )
    if spec.limits.capabilities:
        raise WorkerPolicyError(f"{spec.name}: worker capability channels are not available yet")
    if spec.limits.network != "none":
        raise WorkerPolicyError(f"{spec.name}: a worker has no network")
    sandbox = developer["launch_settings"]["sandbox"]
    if not _fail_closed(sandbox):
        raise WorkerPolicyError("the source sandbox is not fail-closed")
    slot = Slot.parse(slot.agent, slot.name)
    # Every sibling slot and every other agent's pool is denied; the caller cannot forget one.
    others = [Slot(slot.agent, f"worker{n}") for n in range(1, SLOT_COUNT + 1)]
    others = [other for other in others if other != slot]
    pools = []
    for agent in other_agents:
        if not isinstance(agent, str) or not AGENT_RE.fullmatch(agent) or agent == slot.agent:
            raise WorkerPolicyError(f"bad other agent {agent!r}")
        pools.append(f"{WORKERS_ROOT}/{agent}")
    base = rebase(developer, slot, agents_root=agents_root)
    perms = base["settings"]["permissions"]
    ceiling_allow, ceiling_deny_write = _ceiling(spec.limits, slot)
    allow = sorted(
        r for r in ceiling_allow if any(covers(o, r) for o in perms["allow"])
    )
    scoped, directories = _scoped_reads(developer, perms, spec, slot, allow)
    allow = sorted({*allow, *scoped})
    fs = base["launch_settings"]["sandbox"]["filesystem"]
    # The same user runs the developer and every slot: only the sandbox and these rules keep a
    # job out of the dev home (channel token, memory hooks, keys), the agents and other slots.
    deny_read = {
        *fs.get("denyRead", []),
        f"/home/{developer['user']}",
        agents_root,
        *(s.root for s in others),
        *pools,
    }
    # The sandbox binds Bash only: Read, Glob, Grep and Edit obey permission rules, so
    # every sandbox read denial gets its pair, and a reader's repo is closed to Edit too.
    paired_deny = {
        f"{tool}(/{path}/**)" for path in deny_read for tool in ("Read", "Edit")
    }
    paired_deny |= {f"Edit(/{path}/**)" for path in ceiling_deny_write}
    if spec.limits.write == "repo":  # the copy's .git is git's own: no Edit tool writes its config (review 04.11.2026)
        paired_deny.add(f"Edit(/{slot.repo}/.git/**)")
    worker_sandbox = {
        **{k: v for k, v in FAIL_CLOSED_SANDBOX.items() if k != "network"},
        "excludedCommands": [],
        "filesystem": {
            "allowRead": [],
            "denyRead": sorted(deny_read),
            "denyWrite": sorted({*fs.get("denyWrite", []), *ceiling_deny_write}),
        },
        "network": {**FAIL_CLOSED_SANDBOX["network"], "allowedDomains": []},
    }
    guards = [
        g
        for g in base["settings"].get("hooks", {}).get("PreToolUse", [])
        if g["matcher"] == "Bash"
    ]
    worker = {
        "schema_version": SCHEMA_VERSION,
        "worker": {
            "spec": spec.name,
            "version": spec.version,
            "sha256": spec.sha256,
            "slot": slot.name,
            "source": developer["agent"],
            "cwd": slot.work,
        },
        "user": developer["user"],
        "bash_approval": developer["bash_approval"],
        "settings": {
            "hooks": {"PreToolUse": guards},
            "permissions": {
                "additionalDirectories": directories,
                "allow": allow,
                "ask": [],
                "deny": sorted({*perms.get("deny", []), *WORKER_DENY, *paired_deny}),
                **{k: perms[k] for k in MODE_FIELDS if k in perms},
            },
        },
        "launch_settings": {"sandbox": worker_sandbox},
    }
    found = widenings(base, worker, floor=floor)
    if found:
        raise WorkerPolicyError(
            "worker policy widens the agent: " + "; ".join(found)
        )
    return worker
