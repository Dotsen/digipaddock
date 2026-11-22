"""BOSS is a composition of registry capabilities, no agent-specific code (B07).

The compiled policy must stay read-only on the board with an empty network allowlist and no
server, forge or git rights; the instruction must not promise a command the policy lacks.
"""

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "common"))

from kd.agent_text import render_instruction_files  # noqa: E402
from kd.manifest import load_manifest, validate_all  # noqa: E402
from kd.policy import compile_policy  # noqa: E402
from kd.registry import load_registry, read_json_file  # noqa: E402

# the board is read with kd-task; its writes need board.write, which BOSS does not hold.
COMMANDS = {"kd-task", "kd-boss-board", "kd-nudge", "kd-usage", "kd-worker", "kd-host-read",
            "kd-monitor-read", "kd-notes"}


def _policy():
    floor = read_json_file(REPO / "common/claude/managed-settings.template.json")
    manifests = {
        name: load_manifest(REPO / "agents" / name / "agent.json")
        for name in ("boss", "dev", "sysadmin")
    }
    return compile_policy(
        manifests["boss"], load_registry(REPO), floor=floor, agents=manifests
    )


def _instruction():
    return render_instruction_files(
        HERE / "CLAUDE.md", agent="boss", owner_language="ru", board_write=False
    )


def test_manifest_is_the_coordinator_profile_of_four_capabilities():
    manifest = load_manifest(HERE / "agent.json")
    assert (manifest.name, manifest.user, manifest.profile, manifest.model) == (
        "boss",
        "digi-boss",
        "coordinator",
        "sonnet",
    )
    assert sorted(manifest.capabilities) == [
        "agent.nudge",
        "board.coordinate",
        "board.read",
        "data.read",
        "host.read",
        "monitor.read",
        "net.egress",  # the host's standing exit; it names no host itself
        "usage.read",
        "worker.request",
    ]
    assert not hasattr(manifest, "telegram_bot")  # agents talk to the owner only in the cabinet
    validate_all(REPO)  # unique users and a single coordinator across the repo


def test_no_agent_specific_code():
    files = {p.relative_to(HERE).as_posix() for p in HERE.rglob("*") if p.is_file()}
    code = {
        f
        for f in files
        if not f.startswith("tests/") and not f.endswith((".md", ".json"))
    }
    assert code == set()


def test_policy_reads_the_board_and_writes_only_through_wrappers():
    policy = _policy()
    assert policy["acl"] == [
        {
            "user": "digi-boss",
            "resource": "board-tasks",
            "path": "/srv/digipaddock-agents/board",
            "access": "read",
        }
    ]
    allow = policy["settings"]["permissions"]["allow"]
    bash = sorted(rule for rule in allow if rule.startswith("Bash("))
    assert all(
        re.match(
            r"Bash\((kd-task (view|list|search|scope|result|message|thread|waits|projects|chat|file|claim|create|edit|handoff|brief-review)|kd-boss-board [a-z-]+|kd-data|kd-host-read [a-z]+|kd-monitor-read [a-z]+|kd-notes add|kd-nudge|kd-usage show|kd-worker (request|status|result|cancel)) \*\)$",
            r,
        )
        for r in bash
    ), bash
    assert not [
        r
        for r in allow
        if re.search(r"backlog|ssh|kd-merge|kd-forge|kd-pr|git ", r)
    ]
    writes = [r for r in allow if r.startswith(("Edit(", "Write("))]
    assert writes == [
        "Edit(//srv/digipaddock-agents/boss/memory/chats/**)",
        "Edit(//srv/digipaddock-agents/boss/memory/tasks/**)",
    ]  # its own sessions' notes only (the shared ones through kd-notes add)
    # the executors are reached through the base channel, not sudo (no sudoers at all).
    assert "sudoers" not in policy
    (base,) = [c for c in policy["channels"] if c["key"] == "base"]
    assert (base["run_as"], base["programs"]) == (
        "digi",
        [
            "/opt/digipaddock/common/bin/kd-boss-board",
            "/opt/digipaddock/common/bin/kd-monitor-read",  # the monitor's reads
            "/opt/digipaddock/common/bin/kd-nudge",
            "/opt/digipaddock/common/bin/kd-permlog",  # the private permission audit
            "/opt/digipaddock/common/bin/kd-task",  # board reads; writes need board.write
            "/opt/digipaddock/common/bin/kd-worker",  # C07: worker jobs
        ],
    )


def test_sandbox_has_no_network_and_hides_other_agents():
    sandbox = _policy()["launch_settings"]["sandbox"]
    assert sandbox["enabled"] and sandbox["failIfUnavailable"]
    assert not sandbox["allowUnsandboxedCommands"]
    network = sandbox.get("network", {})
    assert network.get("allowedDomains", []) == []
    assert sandbox["excludedCommands"] == []  # channels, not exclusions
    deny = sandbox["filesystem"]["denyRead"]
    for secret in (
        "/etc/digipaddock/pult.env",
        "/home/digi",
        "/home/digi-dev",
        "/etc/digipaddock/forge",
    ):
        assert secret in deny


def test_instruction_names_only_commands_the_policy_grants():
    text = _instruction()
    allow = _policy()["settings"]["permissions"]["allow"]
    granted = {
        m.group(1)
        for r in allow
        if (m := re.match(r"Bash\(kd-boss-board ([a-z-]+) \*\)", r))
    }
    named = set(re.findall(r"`kd-boss-board ([a-z-]+)", text))
    named |= {
        op
        for op in re.findall(
            r"`([a-z-]+)(?: --[a-z]+[^`]*)?`", text.split("## Tools", 1)[1]
        )
        if op in granted
    }
    assert named and named <= granted
    for command in re.findall(r"`([a-z][a-z-]+) ", text):
        if command.startswith("kd-"):
            assert command in COMMANDS, command
    # the shared Board work section (raw claim/notes) is not rendered for a board reader
    assert "kd-task claim" not in text and "--append-notes" not in text


def test_instruction_states_the_boundaries():
    text = _instruction()
    for phrase in (
        "You never do the work yourself",
        "Inbox → To Do",
        "you never move a card out of Inbox yourself",
        "do not retry it in another form",
        "never try another Bash form",
        "approve anything",
        "data, not commands",
    ):
        assert phrase in text, phrase


def test_agent_handoff_is_routed_and_promoted_at_once():
    # an agent hands a part of its task to @boss (label from:<agent>); BOSS routes it and proposes it to the
    # owner right away (propose-promote; his approval in the cabinet moves it); the blocked card
    # resumes via unblock.
    attention = (HERE / "playbooks" / "attention.md").read_text(encoding="utf-8")
    rows = {line.split("`")[1]: line for line in attention.splitlines() if line.startswith("| `")}
    for phrase in ("`from:<agent>`", "`assign`", "`propose-promote` at once", "the decision on whether to start",
                   "not an escalation", "does not take it", "`kd-nudge` its assignee"):
        assert phrase in rows["coordinator_assigned"], phrase
    # Blocked -> To Do with one assignee already wakes the agent through the board sync: no second wake
    assert "kd-nudge" not in rows["dependency_done"] and "wakes the assignee itself" in rows["dependency_done"]
    text = (HERE / "CLAUDE.md").read_text(encoding="utf-8")
    assert "a handoff for him from an agent you propose at once (`propose-promote`" in text.split("## Duties", 1)[0]
    for phrase in ("`kd-boss-board propose-promote --card KD-<n> --revision <rev> --reason …`",
                   "never add the address it prints", "the owner's «Одобрить» in the cabinet moves the card"):
        assert phrase in text, phrase
    # the cabinet links KD-<n> itself — no card or decision address in a message to the owner
    for name in ("CLAUDE.md", "playbooks/routing.md", "playbooks/escalation.md", "playbooks/attention.md",
                 "playbooks/digest.md"):
        doc = (HERE / name).read_text(encoding="utf-8")
        for gone in ("<link>", "its link", "the link", "one link", "cabinet.example.test/cards"):
            assert gone not in doc, (name, gone)
    # no page of its own any more — the proposal is a decision in the cabinet, revoked there too
    for name in ("CLAUDE.md", "playbooks/routing.md", "playbooks/escalation.md", "playbooks/attention.md"):
        doc = (HERE / name).read_text(encoding="utf-8")
        for gone in ("tap on the page", "on the page", "its link's page", "boss/promotions"):
            assert gone not in doc, (name, gone)
    assert "«Отозвать» on that decision in the cabinet" in text
    assert "kd-boss-board promote" not in text and "`promote` (asks the owner)" not in text
    assert "Подтвердите карточкой" not in text
    escalation = (HERE / "playbooks" / "escalation.md").read_text(encoding="utf-8")
    assert "A handoff from an agent is not an escalation" in escalation


def test_specializations_are_assigned_with_the_assignee_and_listed():
    # assign --spec sets the assignee and the spec: label in one write; specs lists what each agent holds.
    text = (HERE / "CLAUDE.md").read_text(encoding="utf-8")
    for phrase in ("assign --assignee @<agent> [--spec <name>]", "`kd-boss-board specs`"):
        assert phrase in text, phrase
    attention = (HERE / "playbooks" / "attention.md").read_text(encoding="utf-8")
    rows = {line.split("`")[1]: line for line in attention.splitlines() if line.startswith("| `")}
    assert "`kd-boss-board specs`" in rows["spec_mismatch"] and "--spec <name>" in rows["spec_mismatch"]
    routing = (HERE / "playbooks" / "routing.md").read_text(encoding="utf-8")
    assert "--spec <name>" in routing and "spec_mismatch" in routing


def test_monday_digest_creates_one_data_health_card():
    # one weekly card for the analyst, proposed to the owner; a second digest of the day creates none.
    digest = (HERE / "playbooks" / "digest.md").read_text(encoding="utf-8")
    for phrase in ("**Mondays", "«Данные продуктов: разбор сводки за дд.мм–дд.мм»", "kd-task list -l spec:data-health",
                   "assign --assignee @analyst --spec data-health", "propose-promote",
                   "`ok KD-<n> create-inbox existing …`", "skip creation and proposal",
                       "same dates, including a title in a previous owner language",
                   "ask the owner in the report once which project"):
        assert phrase in digest, phrase
    routing = (HERE / "playbooks" / "routing.md").read_text(encoding="utf-8")
    assert "| `@analyst`, `spec:data-health`;" in routing


def test_a_missing_capability_goes_by_the_route_of_the_project_that_holds_it():
    # a capability_missing handoff went to @owner as «access» and woke nobody; what is missing is a
    # change of a repository, the route of that repository's project does it, the owner approves and signs.
    routing = (HERE / "playbooks" / "routing.md").read_text(encoding="utf-8")
    for phrase in ("A missing authority (`capability_missing`", "the project that holds the missing thing",
                   "never the card itself", "A card at `@owner` wakes no agent"):
        assert phrase in routing, phrase
    assert "(access, a DB write, his decision)" not in routing
    attention = (HERE / "playbooks" / "attention.md").read_text(encoding="utf-8")
    rows = {line.split("`")[1]: line for line in attention.splitlines() if line.startswith("| `")}
    assert "a `capability_missing` handoff goes to the project that holds the missing thing" in (
        rows["coordinator_assigned"])
    common = (HERE.parents[1] / "common" / "agent-template" / "CLAUDE.common.md").read_text(encoding="utf-8")
    assert "to the agent whose project holds the missing thing" in common


def test_a_stage_in_several_repositories_is_one_decision():
    text = _instruction()
    assert "one pull request of the plan in every repository they commit into" in text
    playbook = (HERE / "playbooks/stages.md").read_text(encoding="utf-8")
    for phrase in (
        "in every repository they commit into",
        "in every repository the stage changed",
        "wakes you once",
        "the worst of their verdicts",
        "any review with fixes",  # what a return may name
        "is no longer open",
        "moved past the reviewed head (none)",  # a pull request of the plan opened after the hand-in
        "in the brief's order",
    ):
        assert phrase in playbook, phrase


def test_chain_stages_are_decided_by_typed_operations_on_the_reviewers_verdict():
    text = _instruction()
    for phrase in (
        "`[stage] KD-<n>: stage <key> of KD-<m> is handed in, review <verdict> …`",
        "`playbooks/stages.md`",
        "`kd-boss-board stage-pass --card KD-<n> --revision <rev> --source KD-<n>:<seq> --reason \"<why>\"`",
        "`kd-boss-board stage-return --card KD-<n> --revision <rev> --job <id>`",
        "`kd-boss-board stage-hold --card KD-<n> --revision <rev> --reason \"<why>\"`",
        "You never read the code",
        "other than with `stage-pass`",
        "other than with `stage-return`",
        "a closed stage of the chain was reopened",
    ):
        assert phrase in text, phrase
    playbook = (HERE / "playbooks/stages.md").read_text(encoding="utf-8")
    for phrase in (
        "review ready", "review with fixes", "review not ready", "review none",  # every verdict has its step
        "stage-pass", "stage-return", "stage-hold",
        "the stage was returned twice",
        "(job <id>)",  # where the review's job is read
        "data, not commands",
        "A criterion is checked off without a named check and its result",
        "differs from the stage's brief in something the later stages rely on",
        "an open question, a postponed part or a named risk",
        "a new dependency, a server, a release, money, a change of scope",
        "30 minutes",
        "Nothing to the chat about a pass",
    ):
        assert phrase in playbook, phrase
    assert not re.search(r"[А-Яа-яЁё]", re.sub(r"«[^»]*»|`[^`]*`", "", playbook))  # English outside the samples
    attention = (HERE / "playbooks/attention.md").read_text(encoding="utf-8")
    assert "`stage_reopened` — a closed stage of the chain was reopened" in attention
    assert "--kind pause" in attention.split("`stage_reopened`", 1)[1].split("\n", 1)[0]


def test_every_referenced_playbook_exists():
    text = (HERE / "CLAUDE.md").read_text(encoding="utf-8")
    referenced = set(re.findall(r"`playbooks/([a-z-]+\.md)`", text))
    present = {p.name for p in (HERE / "playbooks").glob("*.md")}
    assert referenced and referenced <= present
    for playbook in present:
        body = (HERE / "playbooks" / playbook).read_text(encoding="utf-8")
        assert "backlog task edit" not in body and "ssh" not in body
