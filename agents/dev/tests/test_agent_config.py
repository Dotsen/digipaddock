import json
import re
import sys
from pathlib import Path

AGENT = Path(__file__).resolve().parents[1]
ROOT = AGENT.parents[1]
# the generated policy (the golden snapshot test_policy keeps equal to the compiler).
POLICY = json.loads((ROOT / "common/tests/fixtures/policy/dev.policy.json").read_text(encoding="utf-8"))
SETTINGS = POLICY["settings"]["permissions"]
SANDBOX = POLICY["launch_settings"]["sandbox"]
sys.path.insert(0, str(ROOT / "common"))
from kd import naming  # noqa: E402
from kd.agent_text import MAX_TEXT_BYTES, render_instruction_files, render_playbook  # noqa: E402
from kd.registry import read_json_file  # noqa: E402

FLOOR = read_json_file(ROOT / "common/claude/managed-settings.template.json")["permissions"]

# M01: CLAUDE.md is the role text; the agent reads it rendered with the shared sections.
CLAUDE = render_instruction_files(AGENT / "CLAUDE.md", agent="dev", owner_language="ru", board_write=True, data_read=True)
# KD-9461: the always-loaded instruction is small; occasional procedures are playbooks read on demand. A rule that
# lives in a playbook is looked for in PLAYBOOKS[<file>] (or in ALL, the core and every playbook together).
HOST = naming.load_host()
PLAYBOOKS = {
    path.name: render_playbook(path.read_text(encoding="utf-8"), HOST)
    for path in sorted((AGENT / "playbooks").glob("*.md"))
}
ALL = CLAUDE + "\n" + "\n".join(PLAYBOOKS.values())
# the assembled dev instruction (role text + shared sections) stays under this budget (decimal KB, as the card counts)
CORE_BUDGET_BYTES = 50_000


def test_the_assembled_dev_instruction_stays_under_its_budget():
    size = len(CLAUDE.encode("utf-8"))
    assert size <= CORE_BUDGET_BYTES, (
        f"the assembled dev instruction is {size} bytes, the budget is {CORE_BUDGET_BYTES}: "
        "move an occasional procedure into agents/dev/playbooks/ and leave a pointer"
    )
    assert size < MAX_TEXT_BYTES


def test_every_playbook_the_instruction_points_at_exists_and_is_pointed_at():
    pointed = set(re.findall(r"`playbooks/([a-z][a-z0-9-]*\.md)`", CLAUDE))
    # projects.md is the deployment's own file (add_repo.sh writes it), not part of this tree
    missing = pointed - set(PLAYBOOKS) - {"projects.md"}
    assert not missing, f"the instruction points at playbooks that do not exist: {sorted(missing)}"
    unpointed = set(PLAYBOOKS) - pointed - {"pr-template.md"}
    assert not unpointed, f"playbooks no pointer of the instruction names: {sorted(unpointed)}"
    for name in set(PLAYBOOKS) - {"pr-template.md"}:
        # each playbook says when to read it
        assert PLAYBOOKS[name].split("\n")[2].startswith("Read this "), name


def test_the_core_names_the_moments_a_playbook_is_read_and_keeps_the_every_turn_rules():
    """A wake may start a fresh session: the core, not a playbook's own header, says when to open it — and a rule
    of every turn stays in the core."""
    core = re.sub(r"\s+", " ", (AGENT / "CLAUDE.md").read_text(encoding="utf-8"))
    # a council round usually ends as the end of the writer's wait, not as a [council] wake
    assert "needs_writer" in core and "`playbooks/council.md`" in core
    # the deploy and the rollout come in wakes after the merge
    assert "on every wake after the merge until the card ends" in core
    assert "is the one exception, and grants nothing" in core  # the shared directive rule, not against it
    assert "A green subset proves only that subset; Windows does not prove Linux." in core


def test_deny_contains_forge_secrets_and_own_home():
    for rule in ("Read(//etc/digipaddock/forge/**)", "Read(//home/digi-forge/**)", "Bash(git push*)", "Bash(git pull*)",
                 "Bash(git submodule*)", "Bash(git -c *)", "Bash(git -C *)",
                 "Bash(ssh-exec *)", "Bash(ssh-ro *)", "Bash(curl *)", "Bash(sh -c *)", "Bash(bash -c *)", "Bash(pip *)",
                 "Edit(//srv/digipaddock-agents/dev/work/**/.claude/**)",
                 "Edit(//srv/digipaddock-agents/dev/.claude/**)", "Edit(//srv/digipaddock-agents/dev/CLAUDE.md)",
                 "Bash(ssh *)", "Bash(sudo *)", "WebFetch",
                 "Skill(hindsight-coding-agent)", "Edit(//var/lib/digipaddock/**)",
                 # its own home is /home/digi-dev; the base user's home as a whole is not its business
                 "Read(//home/digi/**)", "Read(//home/digi-dev/.ssh/**)", "Read(//home/digi-dev/.claude/.credentials.json)",
                 "Edit(//home/digi-dev/.claude/**)", "Edit(//home/digi-dev/.claude.json)",
                 "Read(//home/digi-dev/.hindsight/**)", "Edit(//home/digi-dev/.hindsight/**)"):
        assert rule in SETTINGS["deny"], rule
    assert {"Read(//etc/digipaddock/forge/**)", "Read(//home/digi-forge/**)"} <= set(FLOOR["deny"])


def test_ask_is_empty_and_allow_has_no_push():
    # The owner's consent to kd-merge is the approval of the head kd-merge checks, not a card.
    assert SETTINGS["ask"] == []
    assert not any("push" in r for r in SETTINGS["allow"])
    # Bash is allowed by the hook's successful JSON decision; there is no bare allow, so the hook's timeout approves
    # no call silently.
    assert "Bash" not in SETTINGS["allow"]
    for rule in ("Read", "Grep", "Glob", "Edit(//srv/digipaddock-agents/dev/work/**)"):
        assert rule in SETTINGS["allow"], rule
    assert not any(r.startswith("Bash(") for r in SETTINGS["allow"])
    # Claude Code: Write(path) rules do not apply to file checks — only Edit(path)
    assert not any(r.startswith("Write(") for r in SETTINGS["allow"] + SETTINGS["deny"] + SETTINGS["ask"])
    # dontAsk refuses what default would ask the owner
    assert SETTINGS["disableBypassPermissionsMode"] == "disable" and SETTINGS["defaultMode"] == "dontAsk"


def test_additional_directories_for_reads_outside_cwd():
    # the rules floor blocks reads outside the working directories (blockReadsOutsideWorkingDirectories); the floor
    # opens the boards, the docs and the platform's journal, the agent's policy opens the Forgejo journal
    assert SETTINGS["additionalDirectories"] == [
        "/srv/digipaddock-agents/board", "/srv/digipaddock-mirror", "/var/log/digipaddock-forge"]  # + the code mirror
    assert {"/srv/example-docs", "/srv/digipaddock-agents/board"} <= set(FLOOR["additionalDirectories"])


def test_the_guard_is_the_one_pre_tool_hook():
    (entry,) = POLICY["settings"]["hooks"]["PreToolUse"]
    assert entry["matcher"] == "Bash"
    assert entry["hooks"][0]["command"] == "/opt/digipaddock/agents/dev/bin/deny_guard.py || exit 2"


def test_launch_settings_enforce_strict_os_sandbox():
    assert set(POLICY["launch_settings"]) == {"sandbox"}
    sandbox = SANDBOX
    assert sandbox["enabled"] is True and sandbox["failIfUnavailable"] is True
    assert sandbox["allowUnsandboxedCommands"] is False and sandbox["autoAllowBashIfSandboxed"] is False
    # nothing leaves the sandbox; kd-task, kd-forge and kd-merge reach their channels
    assert sandbox["excludedCommands"] == []
    assert sandbox["network"]["allowedDomains"] == ["forge.example.test"]  # the mirror is the executor's (git.mirror)
    assert sandbox["network"]["strictAllowlist"] is True
    fs = sandbox["filesystem"]
    assert "/srv/digipaddock-agents/board" in fs["denyWrite"]
    assert "/home/digi-dev/.ssh" in fs["denyRead"]
    assert {p for p in fs["allowRead"] if "/.ssh/" in p} == {"/home/digi-dev/.ssh/config", "/home/digi-dev/.ssh/dev_ed25519"}


def test_sandbox_and_read_rules_leave_test_tools_venv_readable():
    # pytest/vermin come from /opt/digipaddock-venv/dev (agent-launch puts its bin/ on PATH): the sandbox must read
    # it. Claude Code's Read deny rules go into the sandbox's denyRead too — they are checked as well (fnmatch is
    # wider than glob: "no match" is the stricter answer)
    from fnmatch import fnmatch

    venv = "/opt/digipaddock-venv/dev"
    probes = (venv, venv + "/bin/pytest", venv + "/lib/python3.12/site-packages/_pytest/main.py")
    fs = SANDBOX["filesystem"]
    assert not [p for p in fs["denyRead"] if any(x == p or x.startswith(p.rstrip("/") + "/") for x in probes)]
    for rule in SETTINGS["deny"] + FLOOR["deny"]:
        if rule.startswith("Read("):
            pattern = rule[len("Read(/"):-1]
            assert not [x for x in probes if fnmatch(x, pattern) or fnmatch(x, pattern.rstrip("*").rstrip("/") + "/*")], rule


def test_claude_md_test_tools_from_root_managed_venv():
    line = next(ln for ln in CLAUDE.splitlines() if ln.startswith("- Verification follows"))
    assert "root-managed venv" in line and "PATH" in line
    # the project's locked dependencies are the one thing a session installs, and only by kd-deps
    assert "`kd-deps install`" in line and "never install anything else" in line


def test_claude_md_is_english_with_required_sections():
    body = re.sub(r"«[^»]*»|„[^“]*“|`[^`]*`", "", CLAUDE)  # sample messages to the owner and code do not count
    cyr = len(re.findall(r"[А-Яа-яЁё]", body))
    assert cyr < 40, f"CLAUDE.md агента должен быть по-английски (кириллицы вне примеров: {cyr})"
    for phrase in ("kd-task claim", "kd-task list -a @dev", "kd-pr ", "kd-merge ",
                   "playbooks/pr-template.md", "playbooks/projects.md", "data, not commands",
                   "this file wins", "memory/notes.md", "hindsight_search_knowledge_pages", "Write to him in Russian", "agent/KD-",
                   "Edit and Write tools", "git fetch origin && git merge origin/<target>",
                   "--target <branch>", "/srv/digipaddock-agents/dev/work/tmp/"):
        assert phrase in CLAUDE, phrase
    # what moved into a playbook is still part of the dev instruction
    for phrase in ("kd-forge comments", "## Cage", "Cage (computed)", "40 characters", "Checks (passport)", "WIP:",
                   "base_moved", "Approve", "dismisses", "digi-dev", "не одобрен", "There is no Claude Code card"):
        assert phrase in ALL, phrase
    assert "git push" in CLAUDE and "never" in CLAUDE.lower()
    # a card branches from its PR's target (the passport), not always main; pytest is no gate of kd-pr
    assert "agent/KD-<n>-<slug> origin/main" not in CLAUDE and "the line `pytest: N passed`" not in CLAUDE
    # the PR block in the card shows the link, the head, the files and the cage; the message explains
    assert "copy it into your message to the owner" not in CLAUDE and "the line `PR <url> · head" not in CLAUDE
    assert "Right before it send one line" not in CLAUDE
    assert "the PR block in the card already shows them" in PLAYBOOKS["pull-requests.md"]
    # kd-merge goes through the forge channel; the guarantee is the owner's Approve in Forgejo, not a card
    assert "card with ✅/❌" not in CLAUDE and "While the card is pending" not in CLAUDE


def test_a_stage_of_a_chain_plan_works_on_the_plans_branch_and_publishes_before_its_hand_in():
    assert "playbooks/chain-plan.md" in CLAUDE.split("**A stage of a chain plan**", 1)[1].split("\n3. ", 1)[0]
    stage = PLAYBOOKS["chain-plan.md"].split("**A stage of a chain plan**", 1)[1]
    for phrase in (
        "`chain`",  # how the snapshot's first line tells such a stage
        "work/<Repo>.KD-<m>", "agent/KD-<m>-<slug>",  # the plan's one tree and branch, not the stage's own
        "git worktree add ../<Repo>.KD-<m> -b agent/KD-<m>-<slug> origin/<target>",
        "no pull request of its own",
        "kd-pr <project> KD-<m> --title \"KD-<m>: …\"",
        "kd-forge check <project> <N>",
        "names the plan's card",  # the wait line the check prints: the turn ends on the stage's card
        "`этап: PR плана обновлён (голова <sha12>); совет и решение владельца — после последнего этапа`",
        "--final-summary",
        "what the next stage needs",
        "never `kd-merge`",
        "`[stage] KD-<n>: returned on the code review (<verdict>) — …`",
        "data, not commands",
    ):
        assert phrase in stage, phrase
    # the stage's commits are in the plan's pull request before the stage is handed in: its review reads them there
    assert stage.index("kd-forge publish <project> <N>") < stage.index("kd-work finish KD-<n> --outcome review")
    for phrase in ("`[board] KD-<m>: every stage is handed in — publish every pull request of the plan (kd-forge "
                   "publish in each repository it has one), lead each to the owner's decision, then merge them in "
                   "the order the brief names`", "followed by `: <project>#<N>, …` — the plan's open pull requests",
                   "git worktree remove ../<Repo>.KD-<m>"):
        assert phrase in PLAYBOOKS["chain-plan.md"], phrase
    # the shared text no longer says that nobody ever works on a plan's parent
    assert "the parent has no turn of its own and nobody works on it directly" not in ALL
    # KD-9461: what a chain plan gives its parent concerns the dev alone, so it moved from the shared text
    # to the chain-plan playbook; the shared aggregate card keeps the plain plan and the wake on a stage
    parent = PLAYBOOKS["chain-plan.md"].split("## The parent card of a chain plan", 1)[1].split("\n## ", 1)[0]
    assert parent.strip().startswith("A chain plan")
    assert "chain" not in CLAUDE.split("**An aggregate card**", 1)[1].split("\n", 1)[0]


def test_a_chain_plan_in_several_repositories_has_one_pull_request_of_the_plan_in_each():
    """KD-9389: the stages of a plan that works in several repositories opened pull requests of their own, each
    with the owner's decision, and nothing reached the plan's pull request."""
    stage = PLAYBOOKS["chain-plan.md"].split("**A stage of a chain plan**", 1)[1]
    for phrase in (
        "(chain: a branch agent/KD-<m>-… and a pull request of KD-<m> in each repository)",
        "in every repository the plan's stages commit into",
        "The first stage that touches a repository creates the plan's tree there",
        "in each repository the stage changed",
        "before the hand-in",  # a pull request published after it refuses the coordinator's pass
        "is not the plan's",  # a pull request titled with the stage's card
        "Nothing is merged and nothing is rolled out before the plan's end",
        "never ask the owner whether to merge",
        "in the order the brief names",
        "one decision of the owner per pull request",
        # KD-9408: the release tick is on each pull request, the rollout is one
        "the release is one, after the plan's last merge",
        "`kd-merge` of each without `--release`",
        "`kd-merge --release` on the pull request he signed the release on, merged by then",
        "No release signed on any of them",
        "never offer to roll out on his word",
    ):
        assert phrase in stage, phrase
    parent = PLAYBOOKS["chain-plan.md"].split("## The parent card of a chain plan", 1)[1].split("\n## ", 1)[0]
    assert "in every repository they commit into" in parent


def test_claude_md_runs_the_checks_before_the_owner_sees_the_pr():
    # required checks are the factory's dev.check — asked for, waited for as an operation, read in the
    # snapshot
    for phrase in ("kd-forge check <project> <N> [--profiles a,b]", "--outcome wait_dependency --operation <id>",
                   "`checks:`", "kd-host-read tail -n 200 devcheck/<id>.log", "kd-forge publish` only after",
                   "`devcheck` install|check"):
        assert phrase in ALL, phrase
    assert "tell the owner in one line what waits and end the turn with `--outcome wait_user`" not in ALL
    # the core keeps the ending of the turn on the check's wait; the refusals and outcomes are in the playbook
    assert "never wait in the turn" in CLAUDE and "playbooks/pr-checks.md" in CLAUDE
    assert "kd-host-read tail -n 200 devcheck/<id>.log" in PLAYBOOKS["pr-checks.md"]
    template = (AGENT / "playbooks" / "pr-template.md").read_text(encoding="utf-8")
    assert "devcheck/<profile>" in template and "kd-forge check" in template


def test_claude_md_steers_to_command_forms_the_hook_accepts():
    # the guard refuses $'…', an unquoted heredoc with substitutions, cd together with another command (Claude Code
    # sends such a chain to the owner for confirmation), kd-merge in a chain, a full sha in a comment
    from dev_agent import guard

    for phrase in ("Do not write files from Bash", "$'…'", "kd-merge` alone", "git commit -F",
                   "**`cd` is a call of its own.**", "Right: `cd <dir>` as one Bash call"):
        assert phrase in CLAUDE, phrase
    assert "(7 characters)" in ALL
    assert "&& git …" not in ALL and "only before `&&`" not in ALL  # the former advice cd <clone> && … is gone
    # a write from Bash (cat > …, heredoc) matches no allow — a card for the owner; texts go only through the Write tool
    assert "<<" not in ALL
    a, sha = "/srv/digipaddock-agents/dev", "abcdef0123456789abcdef0123456789abcdef01"
    clone = f"{a}/work/DigiPaddock"
    assert guard.decide(f"cd {clone}", a) is None  # cd as a call of its own; after it the hook event's cwd is the clone
    for command in ("git status", "pytest -q", f"git commit -F {a}/tmp/kd-commit-12.txt", f"kd-merge digipaddock 12 {sha} --release"):
        assert guard.decide(command, clone) is None, command
    for command in (f"cd {clone} && git status", f"cd {clone} && pytest -q"):
        assert "отдельным вызовом" in guard.decide(command, a), command
    for command in (f"cd {clone} && kd-merge digipaddock 12 {sha}", "git commit -m $'fix: x'"):
        assert guard.decide(command, a) is not None, command


def test_the_three_most_refused_forms_open_the_core_and_match_the_hook():
    """KD-9461: the hook's most frequent refusals are the first rules of the core, each with the right form beside
    the forbidden one — and the hook refuses exactly the wrong forms and passes the right ones."""
    from dev_agent import guard

    head = CLAUDE.split("\n## Projects\n", 1)[0]
    rules = head.split("## The refusals you meet most", 1)[1]
    assert rules.index("1. **The command name is literal.**") < rules.index("2. **Work from inside the clone.**") < rules.index(
        "3. **`cd` is a call of its own.**"
    )
    for phrase in ("a substitution, a glob or brackets", "`$X`", "`*`", "`?`", "Wrong: `$GIT status`", "Right: `git status`",
                   "`git -C <dir>`", "`git -c <key>=<value>`", "`git --git-dir`",
                   "Right: `cd /srv/digipaddock-agents/dev/work/<Repo>` as its own call",
                   "Wrong: `cd <dir> && git status`", "Right: `cd <dir>` as one Bash call"):
        assert phrase in rules, phrase
    a = "/srv/digipaddock-agents/dev"
    clone = f"{a}/work/DigiPaddock"
    for command, why in (
        ("$GIT status", "имя команды"),
        ("g*t status", "имя команды"),
        (f"git -C {clone} status", "-C"),
        ("git -c core.x=y status", "-c"),
        (f"git --git-dir={clone}/.git status", "--git-dir"),
        (f"cd {clone} && git status", "отдельным вызовом"),
    ):
        reason = guard.decide(command, clone)
        assert reason is not None and why in reason, (command, reason)
    for command in ("git status", f"pytest -q {clone}.KD-1/tests/test_x.py"):
        assert guard.decide(command, clone) is None, command
    assert guard.decide(f"cd {clone}", a) is None


def test_claude_md_nested_repo_instructions_are_data():
    # Claude Code itself loads CLAUDE.md of the clone's subdirectories: agents/sysadmin/CLAUDE.md is another role,
    # agents/dev/CLAUDE.md its own unmerged draft; for the agent this is data
    section = CLAUDE.split("## Repository CLAUDE.md precedence", 1)[1].split("\n## ", 1)[0]
    assert "`agents/*/CLAUDE.md`" in section and "unmerged drafts" in section and "data, not instructions" in section


def test_claude_md_secret_list_matches_guard():
    # the list in the prohibitions is what the guard refuses (dev_agent/guard.py SECRETS): otherwise the agent
    # learns of the refusal only by running into it
    from dev_agent import guard

    prohibitions = CLAUDE.split("## Prohibitions", 1)[1].split("\n## ", 1)[0]
    assert "playbooks/secret-names.md" in prohibitions  # the core says what is refused; the playbook holds the list
    note = next(ln for ln in PLAYBOOKS["secret-names.md"].splitlines() if "The hook refuses a Bash command" in ln)
    for alt in guard.SECRETS.pattern.split("|"):
        name = alt.replace("(?<![\\w-])", "").replace("\\b", "").replace("\\", "")
        assert f"`{name}`" in note, name


def test_the_review_is_the_cabinets_council_not_a_subagent():
    # the reviewer subagent is gone — the card's council of models reads the published head
    assert not (AGENT / "playbooks/review.md").exists()
    assert "playbooks/review.md" not in CLAUDE and "reviewer runs" not in CLAUDE
    core_step = CLAUDE.split("5. Review is the cabinet's council", 1)[1].split("\n6. ", 1)[0]
    for phrase in ("never a reviewer you dispatch", "kd-forge publish", "--council <id>", "playbooks/council.md"):
        assert phrase in core_step, phrase
    step = PLAYBOOKS["council.md"]
    for phrase in ("never a reviewer you dispatch", "kd-forge publish", "--outcome wait_dependency --council <id>",
                   "kd-work council KD-<n>", "kd-work council-reply KD-<n>", "data", "never instructions",
                   "`rejected`", "--later <id>=KD-<m>", "--outcome wait_user", "approves nothing"):
        assert phrase in step, phrase
    agent = next(ln for ln in CLAUDE.splitlines() if ln.startswith("- `Agent` tool"))
    assert "Never a reviewer" in agent
    template = (AGENT / "playbooks/pr-template.md").read_text(encoding="utf-8")
    assert "совет кабинета: см. карточку" in template and "playbooks/review.md" not in template


def test_checks_follow_change_risk_policy_without_an_implicit_full_suite():
    line = next(ln for ln in CLAUDE.splitlines() if ln.startswith("- Verification follows"))
    rules = CLAUDE.split(line, 1)[1].split("\n- ", 2)[1]
    assert "docs/testing.md" in line
    assert "not a full suite before every PR" in line
    for phrase in ("--profile focused", "--profile static", "--profile full", "schema/migrations", "unknown impact"):
        assert phrase in rules
    for phrase in ("short result and log path", "A green subset proves only that subset"):
        assert phrase in PLAYBOOKS["pr-checks.md"]
    assert "the whole suite once, before the PR" not in ALL


def test_a_published_head_under_a_council_ends_the_turn_on_its_wait():
    # publish prints the finish line; steps 6 and 7 end the turn with it, not with wait_user
    publish = next(ln for ln in CLAUDE.splitlines() if ln.startswith("- `kd-forge publish <project> <N>`"))
    assert "совет по коду: круг <R> из <M> — читают критики" in publish
    assert "--outcome wait_dependency --council <id>" in publish
    step = CLAUDE.split("\n6. ", 1)[1].split("\n7. ", 1)[0]
    assert "council" in step and "--outcome wait_user" in step
    step = PLAYBOOKS["pull-requests.md"].split("\n7. ", 1)[1]
    assert "council" in step and "--outcome wait_user" in step
    assert "as in step 6" in CLAUDE.split("\n7. ", 1)[1].split("\n8. ", 1)[0]


WORK = "/srv/digipaddock-agents/dev/work"


def test_work_root_instructions_are_read_only_in_the_cage():
    # the session's cwd is work/ (agent-launch), Claude Code loads CLAUDE.md and CLAUDE.local.md from the cwd.
    # The sandbox mounts root's stubs (agent_setup.sh) read-only — from inside they can be neither removed nor replaced
    fs = SANDBOX["filesystem"]
    assert {f"{WORK}/CLAUDE.md", f"{WORK}/CLAUDE.local.md"} <= set(fs["denyWrite"])
    assert fs["denyWrite"] == sorted(fs["denyWrite"])


def test_work_root_instructions_and_settings_denied_to_edit():
    # explicit rules for the root of work/: do not rely on ** in work/**/.claude/** matching zero segments
    for rule in (f"Edit(/{WORK}/CLAUDE.md)", f"Edit(/{WORK}/CLAUDE.local.md)", f"Edit(/{WORK}/.claude/**)",
                 f"Edit(/{WORK}/.mcp.json)"):
        assert rule in SETTINGS["deny"], rule
    # the agent edits CLAUDE.md of the clones in a PR — nothing forbids them
    assert not any(r.startswith(f"Edit(/{WORK}/**/CLAUDE") for r in SETTINGS["deny"])


def test_claude_md_texts_and_attachments_go_to_work_tmp():
    # M01: the developer-v1 sandbox writes only work/ — texts, command output and files in work/tmp/
    line = next(ln for ln in CLAUDE.splitlines() if ln.startswith("- Writing is allowed only"))
    assert "/srv/digipaddock-agents/dev/work/tmp/" in line and "work/CLAUDE.md" in line
    assert "/srv/digipaddock-agents/dev/tmp/" not in CLAUDE
    assert not re.search(r"(?<![\w-])/tmp/kd-", CLAUDE)
    assert "Files you create with the Write tool go to the same folder" in line


def test_agreed_acceptance_criteria_go_into_a_note_after_claim():
    # kd-task edits the AC list only on an own draft in Inbox; after claim the card is
    # In Progress, so the agreement with the owner (AC included) is recorded as a note, not with --ac.
    workflow = CLAUDE.split("\n## Workflow\n", 1)[1].split("\n## ", 1)[0]
    assert "agreed acceptance criteria go into that note too" in workflow
    assert "only the owner or BOSS edits the acceptance criteria of a card that left Inbox" in workflow
    assert "new or changed acceptance criteria" not in workflow


def test_board_and_board_work_sections_together_carry_the_card_cycle():
    # M01: the claim, notes and Blocked forms live in the shared Board work section; the role's Tools say Review and
    # Done are the outcome of the turn
    board = CLAUDE.split("\n## Board\n", 1)[1].split("\n## Memory", 1)[0]
    assert "\n## Board work\n" in board
    # every board write is a kd-task form; the executor signs, so no "[dev]" and no --comment-author
    # Review and Done are the turn's outcome (kd-work finish), never --status by hand
    for phrase in ("kd-task claim KD-<n>", 'kd-task edit KD-<n> --append-notes "…"', '--final-summary "…"',
                   '--status Blocked --dep KD-<m> --comment "…"', "kd-work finish KD-<n> --outcome",
                   "kd-task list -a @dev"):
        assert phrase in board, phrase
