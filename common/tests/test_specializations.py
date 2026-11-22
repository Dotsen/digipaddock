"""Specialization registry: one root-owned set of texts per type."""

import dataclasses
import hashlib
import json
import re
import shutil
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kd import specializations as sp

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "agents/sysadmin") not in sys.path:
    sys.path.insert(0, str(ROOT / "agents/sysadmin"))
from gate import agent_ro_gate as gate  # noqa: E402 — the managed servers' read gate, the one copy
AGENTS = ROOT / "common/tests/fixtures/policy/agents"
DEV = ("code-critic", "docs", "implementer", "reviewer", "site-copy", "test-author", "tester", "ui-design")
ANALYST = ("brief-critic", "brief-review", "commit-review", "copywriting", "cost-report", "data-health", "onboarding",
           "release-check", "support-draft", "task-brief", "ux-review")
SYSADMIN = ("capacity", "drift-audit", "incident-triage", "post-deploy", "vpn-deploy")
ANALYST_CAPS = ("board.read", "data.read", "docs.read", "repo.read", "usage.read")
DEV_CAPS = ("board.read", "board.write", "forge.review", "git.develop")


@pytest.fixture
def reg():
    return sp.load_registry(sp.SPECS_ROOT, require_root_owner=False)


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "specializations"
    shutil.copytree(sp.SPECS_ROOT, root)
    return root


@pytest.fixture
def repo(tmp_path):
    """A repository root with the four fixture manifests and the real registry."""
    for name in ("analyst", "boss", "dev", "sysadmin"):
        (tmp_path / "agents" / name).mkdir(parents=True)
        shutil.copyfile(AGENTS / name / "agent.json", tmp_path / "agents" / name / "agent.json")
    shutil.copytree(sp.SPECS_ROOT, tmp_path / "common" / "specializations")
    return tmp_path


def _edit(root, change):
    path = root / sp.INDEX
    data = json.loads(path.read_text("utf-8"))
    change(data)
    path.write_text(json.dumps(data), "utf-8")


def test_initial_set_is_the_spec_one(reg):
    assert sorted(sp.for_type(reg, "developer")) == list(DEV)
    assert sorted(sp.for_type(reg, "analyst")) == list(ANALYST)
    assert set(sp.for_type(reg, "sysadmin")) == set(SYSADMIN)
    assert sp.for_type(reg, "coordinator") == {}
    assert sorted(reg.skills) == ["e2e-testing", "frontend", "localization", "python-backend", "security-review",
                                  "stateful-testing", "technical-writing"] and reg.max_skills == 2
    defaults = sorted((s.type, s.name) for s in reg.specs.values() if s.default)
    assert ("developer", "implementer") in defaults and ("analyst", "task-brief") in defaults
    assert not [t for t, _ in defaults if t == "sysadmin"]


@pytest.mark.parametrize("name, write", [("reviewer", "none"), ("code-critic", "none"), ("tester", "temp"),
                                         ("implementer", "repo"),
                                         ("test-author", "repo"), ("docs", "repo"), ("task-brief", "none"),
                                         ("copywriting", "none"), ("ux-review", "none"), ("site-copy", "repo"),
                                         ("ui-design", "repo"), ("incident-triage", "none"), ("drift-audit", "none"),
                                         ("capacity", "none"), ("vpn-deploy", "none"), ("cost-report", "none"),
                                         ("commit-review", "none"), ("onboarding", "none"), ("data-health", "none")])
def test_initial_limits(reg, name, write):
    limits = reg.specs[name].limits
    assert (limits.write, limits.capabilities, limits.network) == (write, (), "none")


def test_every_text_has_the_five_sections_and_no_worker_frame(reg):
    for spec in reg.specs.values():
        assert spec.text.startswith(f"# Specialization: {spec.name} (v{spec.version})\n")
        assert [line[3:] for line in spec.text.splitlines() if line.startswith("## ")] == list(sp.SECTIONS)
        # The frame (unattended run, foreground, result JSON) belongs to the worker loader, not the text.
        assert "run_in_background" not in spec.text and "worker session" not in spec.text
        output = spec.text.split("## Output", 1)[1].split("## Limits", 1)[0]
        assert all(f"`{key}`" in output for key in spec.result), spec.name
    for skill in reg.skills.values():
        assert skill.text.startswith(f"# Skill: {skill.name} (v{skill.version})\n")


def test_texts_carry_no_worker_frame_limits(reg):
    # Final review: a persistent agent reads the text as its playbook (spec 5.8.4), so "never push /
    # open a PR / write to the board" belongs to the worker frame, not to the text: the dev agent
    # itself opens the PR and moves the card. The reviewer's text must not ask for a tmp directory
    # its write limit (none) denies.
    for spec in reg.specs.values():
        limits = spec.text.split("## Limits", 1)[1].lower()
        for phrase in ("push", "merge", "release", "board"):
            assert phrase not in limits, (spec.name, phrase)
    assert "tmp directory" not in reg.specs["reviewer"].text


@pytest.mark.parametrize("name", ["reviewer", "code-critic"])
def test_a_read_only_reader_of_a_prepared_copy_is_told_how_to_reach_it(reg, name):
    """The developer's guard refuses `cd repo && git …`, `git -C` and `--git-dir`, and Claude Code itself refuses a
    relative `cd repo` in a session that asks nobody: a reviewer that was not told the one form that works gave up
    and answered a verdict on code it never read (KD-9386, twice)."""
    spec = reg.specs[name]
    assert spec.version == 3
    assert "by its absolute path — your working directory followed by `/repo` — as a Bash call of its own" in spec.text
    assert "a relative `cd repo`, `cd … && …`, `git -C` and `--git-dir` are refused" in spec.text


def test_a_reviewer_that_could_not_read_the_change_gives_no_verdict(reg):
    assert "never grade code you did not read" in reg.specs["reviewer"].text


@pytest.mark.parametrize("name", ["incident-triage", "drift-audit", "capacity", "vpn-deploy", "commit-review",
                                  "onboarding"])
def test_operator_limits_leave_changes_and_sending_to_the_owner(reg, name):
    # a server change runs only by a plan the owner signs; sending to a customer and publishing are his.
    limits = reg.specs[name].text.split("## Limits", 1)[1]
    assert "plan with the owner's signature" in limits
    assert "owner's to send" in limits


def test_cost_report_reads_the_ledger_only_and_leaves_decisions_to_the_owner(reg):
    # the default period, the comparison, and the limits the owner agreed.
    spec = reg.specs["cost-report"]
    assert (spec.type, spec.requires) == ("analyst", ("usage.read",))
    inputs = spec.text.split("## Inputs", 1)[1].split("## Procedure", 1)[0]
    procedure = spec.text.split("## Procedure", 1)[1].split("## Output", 1)[0]
    limits = spec.text.split("## Limits", 1)[1]
    assert "last 7 days" in inputs and "previous period" in procedure
    assert "Read only with `kd-usage show`" in limits and "no session transcripts" in limits
    assert "no sending" in limits and "not a bill" in limits and "cabinet's model prices" in limits
    assert "models, budgets" in limits and "owner's to decide" in limits


def test_commit_review_takes_a_sha_a_branch_or_a_range_and_reports_an_unknown_one(reg):
    # the owner names the project and the revision; a revision that does not resolve is a missing input.
    spec = reg.specs["commit-review"]
    assert (spec.type, spec.requires) == ("analyst", ("repo.read", "board.read"))
    inputs = spec.text.split("## Inputs", 1)[1].split("## Procedure", 1)[0]
    procedure = spec.text.split("## Procedure", 1)[1].split("## Output", 1)[0]
    output = spec.text.split("## Output", 1)[1].split("## Limits", 1)[0]
    assert "commit sha" in inputs and "`origin/<branch>`" in inputs and "`<base>..<head>`" in inputs
    assert "a revision that does not resolve (unknown sha, unknown branch, a range with an unknown end)" in procedure
    assert "goes to `missing_inputs`" in procedure
    for field in ("file", "line", "severity", "what is wrong"):
        assert field in output, field
    for line in procedure.splitlines():
        for call in re.findall(r"`(kd-repo [^`]+)`", line):
            subcommand = call.split()[1]
            assert subcommand in ("list", "log", "show", "diff", "blame"), call


def test_onboarding_names_the_support_draft_boundary_and_keeps_personal_data_out(reg):
    # connecting and the first steps, not an answer to an inquiry; drafts only, the owner sends.
    spec = reg.specs["onboarding"]
    assert (spec.type, spec.requires) == ("analyst", ("board.read", "docs.read"))
    purpose = spec.text.split("## Purpose", 1)[1].split("## Inputs", 1)[0]
    limits = spec.text.split("## Limits", 1)[1]
    assert "The boundary with `support-draft`" in purpose and "connecting and the first steps" in purpose
    assert "an answer to a customer's inquiry" in purpose
    assert "no sending" in limits and "No personal data" in limits


# A server read in an operator text: `ssh-ro <server> '<command>'`, or mysql without the outer quotes.
SSH_RO = re.compile(r"`ssh-ro <server> (?:'([^'`]+)'|(mysql [^`]+))`")


@pytest.mark.parametrize("name", ["drift-audit", "capacity"])
def test_operator_reads_are_the_ones_the_gate_allows(reg, name, monkeypatch):
    # the texts name only reads agent-ro-gate lets through; anything it refuses goes to `unchecked`.
    monkeypatch.setattr(gate, "_extra", ())  # the host's own forbidden list is not this repository's
    text = reg.specs[name].text
    commands = [quoted or bare for quoted, bare in SSH_RO.findall(text)]
    assert len(commands) >= 10
    for command in commands:
        gate.check(command)  # Deny on a refused read
    procedure = text.split("## Procedure", 1)[1].split("## Output", 1)[0]
    assert "goes to `unchecked`" in procedure


def test_vpn_deploy_points_to_the_checklist_and_does_not_copy_it(reg):
    # the deployment's VPN document is the one source of the steps; the text names its section only.
    text = reg.specs["vpn-deploy"].text
    assert "new VPN server checklist section, by that section's name" in text
    assert "Documentation section of your instructions" in text
    assert "```" not in text and not re.search(r"[А-Яа-яЁё]", text)
    for step in ("wg-quick", "wg0", "ufw ", "iptables", "danted", "vnstat", "fail2ban", "INSERT", "SELECT", "10.100."):
        assert step not in text, step
    procedure = text.split("## Procedure", 1)[1].split("## Output", 1)[0]
    assert "final check in full" in procedure and "kd-admin-propose" in procedure
    assert reg.specs["vpn-deploy"].requires == ("host.read", "servers.read", "servers.execute")


def test_hash_is_the_sha256_of_the_installed_bytes(reg):
    for item in (*reg.specs.values(), *reg.skills.values()):
        assert item.sha256 == hashlib.sha256((sp.SPECS_ROOT / item.file).read_bytes()).hexdigest()
    spec = reg.specs["implementer"]
    assert sp.note(spec) == f"spec implementer v1 sha256={spec.sha256}"
    assert sp.provenance(spec, [reg.skills["python-backend"]]) == {
        "spec": "implementer", "version": 1, "sha256": spec.sha256,
        "skills": {"python-backend": {"version": 1, "sha256": reg.skills["python-backend"].sha256}},
    }


def test_requires_fit_the_profile_ceiling(reg):
    # A specialization that needs a capability its type can never hold would never install.
    for spec in reg.specs.values():
        profile = ROOT / "common/agent-profiles" / f"{spec.type}.json"
        if profile.exists():  # a type may have no profile yet
            ceiling = json.loads(profile.read_text("utf-8"))["capability_ceiling"]
            assert set(spec.requires) <= set(ceiling), spec.name


def test_installable_filters_by_type_and_capabilities(reg):
    assert [s.name for s in sp.installable(reg, "developer", DEV_CAPS)] == list(DEV)
    assert sp.installable(reg, "developer", ("board.read", "board.write")) == []
    assert sp.installable(reg, "sysadmin", ("servers.execute",)) == []
    assert [s.name for s in sp.installable(reg, "analyst", ())] == ["copywriting", "task-brief"]
    assert [s.name for s in sp.installable(reg, "analyst", ANALYST_CAPS)] == list(ANALYST)
    # data-health needs the product database reader; an analyst without data.read never gets it.
    without_data = [c for c in ANALYST_CAPS if c != "data.read"]
    assert [s.name for s in sp.installable(reg, "analyst", without_data)] == [n for n in ANALYST if n != "data-health"]


def test_data_health_reads_and_reports_only(reg):
    # the verdict is recomputed from the product's current code; the text names no deployment value
    # (database, table, project) and fixes no threshold.
    spec = reg.specs["data-health"]
    assert (spec.type, spec.version, spec.requires) == ("analyst", 1, ("board.read", "data.read", "docs.read", "repo.read"))
    assert spec.result == ("period", "slices", "causes", "unknowns", "missing_inputs")
    inputs = spec.text.split("## Inputs", 1)[1].split("## Procedure", 1)[0]
    assert "the period" in inputs and "optional filter by product, aggregator" in inputs
    procedure = spec.text.split("## Procedure", 1)[1].split("## Output", 1)[0]
    for phrase in ("Recompute every slice's verdict", "thresholds of the code you read", "only a production database",
                   "`collection`", "`parsing`", "`configuration`", "`real_change`", "`unknown`", "kd-repo log"):
        assert phrase in procedure, phrase
    limits = spec.text.split("## Limits", 1)[1]
    for phrase in ("Only reads", "only on the product's production database", "No personal data",
                   "Only a report on the card"):
        assert phrase in limits, phrase
    assert not re.search(r"\b\d+(\.\d+)?\s*%", spec.text)  # no threshold copied from the product's code


@pytest.mark.parametrize("labels, expected", [
    ([], "implementer"),
    (["server:node-c", "spec:reviewer"], "reviewer"),
    (["spec:test-author"], "test-author"),
])
def test_select_takes_the_label_or_the_default(reg, labels, expected):
    assert sp.select(reg, "developer", DEV_CAPS, labels).name == expected


@pytest.mark.parametrize("type_, caps, labels, why", [
    ("developer", DEV_CAPS, ["spec:reviewer", "spec:docs"], "several"),
    ("developer", DEV_CAPS, ["spec:task-brief"], "not a specialization"),
    ("developer", DEV_CAPS, ["spec:bogus"], "not a specialization"),
    ("developer", ("board.write",), ["spec:implementer"], "not a specialization"),
    ("sysadmin", ("servers.read",), ["spec:implementer"], "not a specialization"),
    ("developer", DEV_CAPS, ["spec:Implementer"], "not a specialization"),
])
def test_select_never_guesses(reg, type_, caps, labels, why):
    with pytest.raises(sp.SpecError, match=why):
        sp.select(reg, type_, caps, labels)


def test_select_is_none_without_a_default(reg):
    assert sp.select(reg, "sysadmin", ("servers.read",), ["server:node-c"]) is None
    assert sp.select(reg, "developer", ("board.write",), []) is None  # the default is not installable


def test_agent_specs_come_from_the_manifests(reg, repo):
    assert sp.agent_specs(reg, repo) == {"@analyst": frozenset(ANALYST), "@boss": frozenset(), "@dev": frozenset(DEV), "@sysadmin": frozenset(SYSADMIN)}


def test_agent_specs_refuse_a_linked_agent_directory(reg, repo):
    try:
        (repo / "agents" / "evil").symlink_to(repo / "agents" / "dev", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are not available here")
    with pytest.raises(sp.SpecError, match="linked"):
        sp.agent_specs(reg, repo)


@pytest.mark.parametrize("change, why", [
    (lambda d: d["specializations"]["implementer"].update({"allowed-tools": ["Bash"]}), "unknown field"),
    (lambda d: d["specializations"]["implementer"].update(permissions={"allow": ["Bash"]}), "unknown field"),
    (lambda d: d.update(model="opus"), "unknown field"),
    (lambda d: d["specializations"]["implementer"].update(type="operator"), "type"),
    (lambda d: d["specializations"]["reviewer"].update(default=True), "more than one default"),
    (lambda d: d["specializations"]["implementer"].update(default="yes"), "default"),
    (lambda d: d["specializations"]["implementer"]["limits"].update(write="all"), "write"),
    (lambda d: d["specializations"]["implementer"]["limits"].update(network="all"), "network"),
    (lambda d: d["specializations"]["implementer"]["limits"].update(capabilities="git.develop"), "capabilities"),
    (lambda d: d["specializations"]["implementer"]["limits"].pop("network"), "missing"),
    (lambda d: d["specializations"]["implementer"].update(result=[]), "result"),
    (lambda d: d["specializations"]["implementer"].update(result=["status"]), "result"),
    (lambda d: d["specializations"]["implementer"].update(result=["commit", "tests", "verdict"]), "Output"),
    (lambda d: d["specializations"]["implementer"].update(requires=["git develop"]), "requires"),
    (lambda d: d["specializations"]["implementer"].update(version=2), "file"),
    (lambda d: d["specializations"]["implementer"].update(file="../../kd/policy.py"), "file"),
    (lambda d: d["specializations"]["implementer"].update(file="analyst/implementer.v1.md"), "file"),
    (lambda d: d["specializations"]["implementer"].update(skills=["rust"]), "unknown skill"),
    (lambda d: d["skills"]["python-backend"].update(conflicts=["rust"]), "unknown skill"),
    (lambda d: d.update(schema_version=2), "schema_version"),
    (lambda d: d.update(max_skills=0), "max_skills"),
    (lambda d: d["specializations"].update({"Bad Name": d["specializations"]["docs"]}), "name"),
])
def test_broken_index_is_refused(tree, change, why):
    _edit(tree, change)
    with pytest.raises(sp.SpecError, match=why):
        sp.load_registry(tree, require_root_owner=False)


@pytest.mark.parametrize("edit, why", [
    (lambda t: t.replace("## Inputs", "## Input"), "sections"),
    (lambda t: t.replace("## Limits\n", "## Limits\n\n## Notes\n"), "sections"),
    (lambda t: t.replace("# Specialization: docs (v1)", "# Specialization: docs (v2)"), "first line"),
    (lambda t: t.replace("`files`", "files"), "Output"),
    (lambda t: t + "x" * sp.MAX_TEXT, "larger"),
])
def test_broken_text_is_refused(tree, edit, why):
    path = tree / "developer" / "docs.v1.md"
    path.write_text(edit(path.read_text("utf-8")), "utf-8", newline="\n")
    with pytest.raises(sp.SpecError, match=why):
        sp.load_registry(tree, require_root_owner=False)


def test_headings_count_only_outside_code_fences_and_only_at_level_two(tree):
    # Review minor: an example inside a fence is prose; '### Output' is not the Output section.
    path = tree / "developer" / "docs.v1.md"
    text = path.read_text("utf-8")
    fenced = text.replace("## Limits\n", "```md\n## Example\n# Title\n```\n\n## Limits\n")
    path.write_text(fenced, "utf-8", newline="\n")
    assert sp.load_registry(tree, require_root_owner=False).specs["docs"].text == fenced
    fake = text.replace("`files`", "files").replace("## Procedure\n", "## Procedure\n\n### Output\n\n`files`\n")
    path.write_text(fake, "utf-8", newline="\n")
    with pytest.raises(sp.SpecError, match="Output does not describe files"):
        sp.load_registry(tree, require_root_owner=False)


def test_names_and_keys_are_matched_whole(tree):
    _edit(tree, lambda d: d["specializations"]["implementer"].update(result=["commit\n", "tests", "notes"]))
    with pytest.raises(sp.SpecError, match="list of keys"):
        sp.load_registry(tree, require_root_owner=False)


def test_registry_is_read_only(reg):
    with pytest.raises((TypeError, AttributeError)):
        reg.specs["x"] = None
    with pytest.raises((TypeError, AttributeError)):
        reg.skills.clear()


def test_missing_text_file_is_refused(tree):
    (tree / "skills" / "security-review.v1.md").unlink()
    with pytest.raises(sp.SpecError, match="security-review"):
        sp.load_registry(tree, require_root_owner=False)


def _fake_lstat(root, *, uid=0, mode=0o644, link=None):
    linked = Path(root) / link if link else None

    def lstat(path):
        st = Path(path).lstat()
        kind = stat.S_IFLNK if linked is not None and Path(path) == linked else stat.S_IFMT(st.st_mode)
        bits = 0o755 if stat.S_ISDIR(st.st_mode) else mode
        return SimpleNamespace(st_uid=uid, st_mode=kind | bits, st_size=st.st_size)

    return lstat


def test_root_owned_registry_passes_the_owner_check(tree):
    assert sp.load_registry(tree, require_root_owner=True, lstat=_fake_lstat(tree))


@pytest.mark.parametrize("kwargs, why", [
    ({"uid": 1000}, "root"),
    ({"mode": 0o664}, "writable"),
    ({"link": "developer/reviewer.v3.md"}, "regular file"),
])
def test_registry_not_root_owned_or_writable_or_linked_is_refused(tree, kwargs, why):
    with pytest.raises(sp.SpecError, match=why):
        sp.load_registry(tree, require_root_owner=True, lstat=_fake_lstat(tree, **kwargs))


def test_owner_check_is_on_by_default(tree):
    with pytest.raises(sp.SpecError, match="root"):
        sp.load_registry(tree, lstat=_fake_lstat(tree, uid=1000))


def test_entries_are_frozen_data(reg):
    with pytest.raises(dataclasses.FrozenInstanceError):
        reg.specs["docs"].default = True


def test_task_brief_is_v8_and_tells_to_ask_with_a_form(reg):
    """Choices are asked as a form; a rework comes in the thread and the next handoff is the
    next version; an L brief that splits is written as stages. The note a claim writes names v7 and the
    hash of the v7 file."""
    spec = reg.specs["task-brief"]
    assert (spec.version, spec.file) == (8, "analyst/task-brief.v8.md")
    assert "kd-work ask KD-<n> --form '" in spec.text and "next version" in spec.text  # the JSON as the argument
    assert "--stages '<JSON list>'" in spec.text and "aggregate" in spec.text and "stages" in spec.result
    assert spec.text.startswith("# Specialization: task-brief (v8)")
    assert sp.note(spec) == f"spec task-brief v8 sha256={hashlib.sha256((sp.SPECS_ROOT / spec.file).read_bytes()).hexdigest()}"
    for old in ("task-brief.v1.md", "task-brief.v2.md", "task-brief.v3.md", "task-brief.v4.md", "task-brief.v5.md",
                "task-brief.v6.md", "task-brief.v7.md"):
        assert not (sp.SPECS_ROOT / "analyst" / old).exists()


def test_task_brief_plans_stages_of_one_role_as_a_chain(reg):
    """Stages of one role in one project run one after another on one branch without the owner's acceptance of
    each: a stage's criteria name what the next one relies on, and a stop for a rollout or the owner's decision is a
    stage of another role or a card of its own."""
    text = reg.specs["task-brief"].text
    chain = text.split("- A chain plan:", 1)[1].split("\n", 1)[0]
    for phrase in ("one role", "one project", "one branch", "one pull request", "without the owner's acceptance of each",
                   "what the next stage relies on", "a rollout or the owner's decision",
                   "a stage of another role or a card of its own"):
        assert phrase in chain, phrase


def test_task_brief_keeps_the_rollout_of_a_chain_plan_for_its_end(reg):
    """A chain plan's stages commit into any repository of the project's work; nothing is merged or rolled out
    before the plan's end, so no stage's criteria may need a stand (KD-9389: every stage waited for a merge)."""
    chain = reg.specs["task-brief"].text.split("- A chain plan:", 1)[1].split("\n", 1)[0]
    for phrase in ("in every repository they commit into", "which repositories it changes",
                   "Nothing is merged and nothing is rolled out before the plan's end",
                   "no stage's criteria may need its change on a stand",
                   "the brief's own acceptance criteria", "the order the repositories merge and roll out in"):
        assert phrase in chain, phrase


def test_task_brief_sizes_the_brief_and_asks_less(reg):
    """no summary for confirmation before the brief; a clear recommendation is decided, not asked; every
    remaining question has a default; the size is a label, and an S brief without exceptions starts by itself."""
    procedure = reg.specs["task-brief"].text.split("## Procedure", 1)[1].split("## Output", 1)[0]
    assert "No separate summary for confirmation" in procedure
    assert "get the owner's confirmation before you write the final brief" not in procedure
    assert "in the owner's language" in procedure
    assert "Defaults chosen section" in procedure and "give every one a default" in procedure
    assert "`size:S`, `size:M` or `size:L`" in procedure and "starts by itself" in procedure
    for exception in ("servers", "production data", "secrets", "security", "money"):
        assert exception in procedure, exception


def test_task_brief_final_check_comes_before_every_handoff(reg):
    """a mandatory final pass over the complete brief before each handoff, not replaced by the owner's
    confirmation of the summary; it covers code, consistency, neighbours, the scenario, limits, errors,
    measurable promises, feasibility and coverage by criteria; it separates the owner's intent from facts and
    routine choices. The summary states the agreement in the affirmative, without "I understood it as"."""
    text = reg.specs["task-brief"].text
    procedure = text.split("## Procedure", 1)[1].split("## Output", 1)[0]
    check = procedure.index("Final check")
    assert procedure.index("Goal and Agreed work") < check < procedure.index("Name the next step")
    assert "in the affirmative" in procedure and "«понял так: …»" not in procedure
    assert "before every handoff" in procedure and "does not replace it" in procedure
    for point in (
        "Code:", "Consistency:", "Neighbours:", "scenario end to end", "Limits and edge cases:",
        "Errors, retries and recovery", "Measurable promises:", "Feasibility:", "Coverage:",
    ):
        assert point in procedure, point
    assert "hypothesis with a preliminary probe" in procedure
    assert "under Open questions with who decides it and when" in procedure


def test_task_brief_is_written_for_reading_in_the_cabinet(reg):
    """The cabinet shows Markdown, so the brief has a heading over each part, technical names in
    backticks, lists and tables; the section about the present state is "Current behavior"."""
    procedure = reg.specs["task-brief"].text.split("## Procedure", 1)[1].split("## Output", 1)[0]
    for phrase in ("which shows Markdown", "each part under its own heading", "in backticks",
                   "several items as a list", "a comparison as a table"):
        assert phrase in procedure, phrase
    assert "every claim under Current behavior" in procedure
    assert "the check runs again" in procedure and "hand over only a brief that passes" in procedure
    assert "Ask the owner only about intent and significant product trade-offs" in procedure
    assert "needs no new confirmation" in procedure
    assert "run the final check again" in procedure  # a rework too


# copywriting and UI/UX roles. Each new text names the inputs of its role,
# both cases of a missing input and, for the writers, who opens the pull request.
NEW = {
    "copywriting": (("drafts", "sources", "missing_inputs"),
                    ("audience", "goal", "channel", "language", "constraints", "key message", "CTA")),
    "ux-review": (("findings", "summary", "missing_inputs"),
                  ("interface", "scenarios", "widths", "screenshots or recordings")),
    "site-copy": (("commit", "files", "notes", "missing_inputs"),
                  ("project", "pages or strings", "audience", "goal", "languages")),
    "ui-design": (("commit", "files", "tokens", "screens", "missing_inputs"),
                  ("screens", "scenarios", "widths", "360, 900 and 1440")),
}


def _section(text, name):
    return text.split(f"## {name}\n", 1)[1].split("\n## ", 1)[0]


def test_new_entries_are_registered_as_agreed(reg):
    assert {n: (reg.specs[n].type, reg.specs[n].version, reg.specs[n].requires) for n in NEW} == {
        "copywriting": ("analyst", 1, ()),
        "ux-review": ("analyst", 1, ("repo.read",)),
        "site-copy": ("developer", 1, ("git.develop",)),
        "ui-design": ("developer", 1, ("git.develop",)),
    }
    assert not any(reg.specs[n].default for n in NEW)
    skill = reg.skills["frontend"]
    assert (skill.version, skill.file, skill.conflicts) == (1, "skills/frontend.v1.md", ())
    assert sorted(n for n, s in reg.specs.items() if "frontend" in s.skills) == [
        "implementer", "reviewer", "tester", "ui-design"]


@pytest.mark.parametrize("name", sorted(NEW))
def test_new_text_output_names_every_result_key_and_limits_leave_the_frame_alone(reg, name):
    spec = reg.specs[name]
    keys, _ = NEW[name]
    assert spec.result == keys
    output, limits = _section(spec.text, "Output"), _section(spec.text, "Limits").lower()
    for key in keys:
        assert f"`{key}`" in output, (name, key)
    for phrase in ("push", "merge", "release", "board"):
        assert phrase not in limits, (name, phrase)


@pytest.mark.parametrize("name", sorted(NEW))
def test_new_text_lists_its_inputs_and_both_cases_of_a_missing_one(reg, name):
    text = reg.specs[name].text
    _, inputs = NEW[name]
    section = _section(text, "Inputs")
    for item in inputs:
        assert item in section, (name, item)
    procedure = _section(text, "Procedure")
    assert "as the persistent agent, ask the owner on the card with `kd-work ask` before" in procedure
    assert 'as a worker' in procedure and 'status "blocked"' in procedure and "`missing_inputs`" in procedure


@pytest.mark.parametrize("name", ["site-copy", "ui-design"])
def test_only_the_persistent_agent_opens_the_pull_request(reg, name):
    procedure = _section(reg.specs[name].text, "Procedure")
    assert "The persistent agent opens the pull request itself" in procedure
    assert "a worker returns its commit and never opens one" in procedure


def test_ui_design_writes_only_its_prototype_folder(reg):
    text = reg.specs["ui-design"].text
    assert "docs/prototypes/KD-<n>/" in _section(text, "Procedure")
    assert "product code, styles and tests stay unchanged" in _section(text, "Limits")
