# agents/dev/tests/test_pr.py
import base64
import io
import subprocess
import pytest
from dev_agent import pr, repos

REPO = repos.Repo("digipaddock", "example/digipaddock", "/srv/digipaddock-agents/dev/work/DigiPaddock", "digipaddock")
BODY = "KD-12 https://cabinet.example.test/board/KD-12\n\n## Checks\npytest: 2048 passed\n## Review\nok\n## Risks\n-\n## Cage\nнет\n"
MAIN = "0123456789abcdef0123456789abcdef01234567"  # Forgejo's live main (ls-remote), the base of the cage diff


def fake_run(answers):
    """answers: {a substring of the command: (rc, stdout, stderr)}; origin's main answers unless the test says otherwise."""
    answers = dict(answers)
    answers.setdefault("ls-remote --exit-code origin refs/heads/main", (0, f"{MAIN}\trefs/heads/main\n", ""))
    calls = []
    def run(cmd, **kw):
        calls.append(cmd)
        for key, (rc, out, err) in answers.items():
            if key in " ".join(cmd):
                return subprocess.CompletedProcess(cmd, rc, out, err)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    run.calls = calls
    return run


def test_check_requires_clean_tree_and_task_branch():
    run = fake_run({"status --porcelain": (0, "", ""), "rev-parse --abbrev-ref": (0, "agent/KD-12-fix-x\n", "")})
    assert pr.check(REPO, "KD-12", "KD-12: fix", BODY, run) == ("agent/KD-12-fix-x", [])
    dirty = fake_run({"status --porcelain": (0, " M a.py\n", "")})
    with pytest.raises(pr.PrError, match="не чист"):
        pr.check(REPO, "KD-12", "KD-12: fix", BODY, dirty)
    other = fake_run({"rev-parse --abbrev-ref": (0, "agent/KD-13-x\n", "")})
    with pytest.raises(pr.PrError, match="ветка"):
        pr.check(REPO, "KD-12", "KD-12: fix", BODY, other)
    main = fake_run({"rev-parse --abbrev-ref": (0, "main\n", "")})
    with pytest.raises(pr.PrError, match="ветка"):
        pr.check(REPO, "KD-12", "t", BODY, main)


def test_check_body_sections_and_sizes():
    run = fake_run({"rev-parse --abbrev-ref": (0, "agent/KD-12-x\n", "")})
    with pytest.raises(pr.PrError, match="## Checks"):
        pr.check(REPO, "KD-12", "t", "no sections", run)
    with pytest.raises(pr.PrError, match="## Cage"):
        pr.check(REPO, "KD-12", "t", "## Checks\npytest: 1 passed\n", run)
    with pytest.raises(pr.PrError, match="60"):
        pr.check(REPO, "KD-12", "t", BODY + "x" * 62_000, run)  # BODY_MAX = 61 440 bytes
    with pytest.raises(pr.PrError, match="заголов"):
        pr.check(REPO, "KD-12", "x" * 201, BODY, run)
    with pytest.raises(pr.PrError, match="заголов"):
        pr.check(REPO, "KD-12", "bad\ntitle", BODY, run)


def test_cage_files_is_everything_but_docs_tasks_tests_and_plain_md():
    names = ["docs/a.md", "tasks/todo.md", "agents/dev/tests/test_x.py", "tests/test_shell_syntax.py", "README.md",
             "common/kd/shell.py", "agents/dev/CLAUDE.md", "agents/sysadmin/playbooks/security.md",
             "agents/dev/dev_agent/forge.py", "common/systemd/digipaddock-release.service", ""]
    assert pr.cage_files(names) == ["common/kd/shell.py", "agents/dev/CLAUDE.md", "agents/sysadmin/playbooks/security.md",
                                    "agents/dev/dev_agent/forge.py", "common/systemd/digipaddock-release.service"]
    assert pr.cage_line([]) == "Cage (computed): нет"
    assert pr.cage_line(["a", "b"]) == "Cage (computed): да — a, b"


def test_check_computes_cage_from_diff_and_refuses_false_no():
    run = fake_run({"rev-parse --abbrev-ref": (0, "agent/KD-12-x\n", ""),
                    f"diff --name-only {MAIN}...HEAD": (0, "common/kd/shell.py\ndocs/a.md\n", "")})
    with pytest.raises(pr.PrError, match="клетк"):
        pr.check(REPO, "KD-12", "t", BODY, run)  # BODY says "## Cage\nнет"
    body_yes = BODY.replace("## Cage\nнет", "## Cage\nда, файлы: common/kd/shell.py")
    assert pr.check(REPO, "KD-12", "t", body_yes, run) == ("agent/KD-12-x", ["common/kd/shell.py"])
    broken = fake_run({"rev-parse --abbrev-ref": (0, "agent/KD-12-x\n", ""), "diff --name-only": (128, "", "bad revision")})
    with pytest.raises(pr.PrError, match="git diff"):
        pr.check(REPO, "KD-12", "t", body_yes, broken)


def test_cage_base_is_forgejos_main_not_the_clones_ref():
    """Global review 15.10.2026: the clone's origin/main can be moved by the agent; the diff is against the live sha."""
    run = fake_run({**BRANCH, f"diff --name-only {MAIN}...HEAD": (0, "common/kd/shell.py\n", "")})
    with pytest.raises(pr.PrError, match="клетк"):
        pr.check(REPO, "KD-12", "t", BODY, run)
    diff = [c for c in run.calls if "diff" in c][0]
    assert f"{MAIN}...HEAD" in diff and "origin/main...HEAD" not in diff and "--no-replace-objects" in diff
    gone = fake_run({**BRANCH, "ls-remote": (2, "", "")})
    with pytest.raises(pr.PrError, match="refs/heads/main"):
        pr.check(REPO, "KD-12", "t", BODY, gone)
    odd = fake_run({**BRANCH, "ls-remote": (0, "deadbeef\trefs/heads/main\n", "")})
    with pytest.raises(pr.PrError, match="refs/heads/main"):
        pr.check(REPO, "KD-12", "t", BODY, odd)
    stale = fake_run({**BRANCH, "diff --name-only": (128, "", "bad object")})  # the sha is not in the clone yet
    with pytest.raises(pr.PrError, match="git fetch origin"):
        pr.check(REPO, "KD-12", "t", BODY, stale)


def test_push_command_is_agit_without_force():
    cmd = pr.push_command("agent/KD-12-x", "KD-12: заголовок", "тело\nстрока 2")
    assert cmd[:4] == ["git", "push", "origin", "HEAD:refs/for/main"]
    assert "-o" in cmd and "topic=agent/KD-12-x" in cmd and "title=KD-12: заголовок" in cmd
    desc = [a for a in cmd if a.startswith("description=")][0]
    assert desc.startswith("description={base64}")
    assert base64.b64decode(desc[len("description={base64}"):]).decode() == "тело\nстрока 2"
    assert not any("force" in a for a in cmd)


def test_pr_url_from_remote_output():
    out = "remote: Processing changes: new: 1, done\nremote: \nremote:   https://forge.example.test/example/digipaddock/pulls/12\n"
    assert pr.pr_url(out) == "https://forge.example.test/example/digipaddock/pulls/12"
    assert pr.pr_url("nothing") is None


def test_main_flow_and_exit_codes(tmp_path, monkeypatch):
    body = tmp_path / "b.md"; body.write_text(BODY, encoding="utf-8")
    reg = tmp_path / "repos.json"
    reg.write_text('{"digipaddock": {"repo": "example/digipaddock", "dir": "work/DigiPaddock"}}', encoding="utf-8")
    monkeypatch.setattr(pr, "REPOS", str(reg))
    run = fake_run({"rev-parse --abbrev-ref": (0, "agent/KD-12-x\n", ""),
                    "push origin": (0, "", "remote:   https://forge.example.test/example/digipaddock/pulls/12\n")})
    out, err = io.StringIO(), io.StringIO()
    assert pr.main(["digipaddock", "KD-12", "--title", "KD-12: x", "--body-file", str(body)], run=run, out=out, err=err) == 0
    assert "pulls/12" in out.getvalue() and "Cage (computed): нет" in out.getvalue()
    assert "Дальше: kd-forge publish digipaddock 12 — PR уйдёт владельцу в кабинет" in out.getvalue()
    assert any("-C" in c and "/work/DigiPaddock" in " ".join(c) for c in run.calls)  # git always runs in the project's clone
    push = [c for c in run.calls if "push" in c][0]
    desc = [a for a in push if a.startswith("description={base64}")][0]
    assert base64.b64decode(desc[len("description={base64}"):]).decode().rstrip().endswith(
        "Cage (computed): нет\nChecks (passport): нет обязательных")
    cage_run = fake_run({"rev-parse --abbrev-ref": (0, "agent/KD-12-x\n", ""), "diff --name-only": (0, "common/bin/kd-usage\n", ""),
                         "push origin": (0, "", "remote:   https://forge.example.test/example/digipaddock/pulls/12\n")})
    yes = tmp_path / "yes.md"; yes.write_text(BODY.replace("## Cage\nнет", "## Cage\nда: common/bin/kd-usage"), encoding="utf-8")
    out = io.StringIO()
    assert pr.main(["digipaddock", "KD-12", "--title", "KD-12: x", "--body-file", str(yes)], run=cage_run, out=out, err=io.StringIO()) == 0
    assert "Cage (computed): да — common/bin/kd-usage" in out.getvalue()
    rejected = fake_run({"rev-parse --abbrev-ref": (0, "agent/KD-12-x\n", ""), "push origin": (1, "", "! [remote rejected]")})
    assert pr.main(["digipaddock", "KD-12", "--title", "t", "--body-file", str(body)], run=rejected, out=io.StringIO(), err=io.StringIO()) == 3
    assert pr.main(["digipaddock", "12", "--title", "t", "--body-file", str(body)], run=run, out=io.StringIO(), err=io.StringIO()) == 2
    assert pr.main(["nope", "KD-12", "--title", "t", "--body-file", str(body)], run=run, out=io.StringIO(), err=io.StringIO()) == 1


# ---- beyond the brief: edges found during the implementation ----

BRANCH = {"rev-parse --abbrev-ref": (0, "agent/KD-12-x\n", "")}


def _registry(tmp_path, monkeypatch):
    reg = tmp_path / "repos.json"
    reg.write_text('{"digipaddock": {"repo": "example/digipaddock", "dir": "work/DigiPaddock"}}', encoding="utf-8")
    monkeypatch.setattr(pr, "REPOS", str(reg))


def test_check_diff_without_rename_detection():
    """With rename detection git would show a cage file moved to docs/ only as docs/… — the cage would read "no"."""
    run = fake_run(BRANCH)
    pr.check(REPO, "KD-12", "t", BODY, run)
    diff = [c for c in run.calls if "diff" in c][0]
    assert diff[:3] == ["git", "-C", REPO.dir] and "--no-renames" in diff


def test_check_title_without_control_chars():
    run = fake_run(BRANCH)
    for title in ("a\rb", "a\x00b", "a\tb", "   "):
        with pytest.raises(pr.PrError, match="заголов"):
            pr.check(REPO, "KD-12", title, BODY, run)


def test_check_git_timeout_is_refusal():
    def run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 120)
    with pytest.raises(pr.PrError, match="git status"):
        pr.check(REPO, "KD-12", "t", BODY, run)


def test_main_refuses_body_too_long_for_one_push_option(tmp_path, monkeypatch):
    """git sends a push option as one pkt-line (≤ 65 516 bytes): with base64 the body is up to ~48 KB, though BODY_MAX is 60 KB."""
    _registry(tmp_path, monkeypatch)
    big = tmp_path / "big.md"; big.write_text(BODY + "x" * 50_000, encoding="utf-8")
    run, err = fake_run(BRANCH), io.StringIO()
    assert pr.main(["digipaddock", "KD-12", "--title", "t", "--body-file", str(big)], run=run, out=io.StringIO(), err=err) == 1
    assert "48" in err.getvalue() and not any("push" in c for c in run.calls)
    ok = tmp_path / "ok.md"; ok.write_text(BODY + "x" * 48_000, encoding="utf-8")
    run = fake_run(BRANCH)
    assert pr.main(["digipaddock", "KD-12", "--title", "t", "--body-file", str(ok)], run=run, out=io.StringIO(), err=io.StringIO()) == 0
    assert any("push" in c for c in run.calls)


def test_main_push_timeout_and_missing_url(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch)
    body = tmp_path / "b.md"; body.write_text(BODY, encoding="utf-8")
    def slow(cmd, **kw):
        if "push" in cmd:
            raise subprocess.TimeoutExpired(cmd, 300)
        return fake_run(BRANCH)(cmd, **kw)
    err = io.StringIO()
    assert pr.main(["digipaddock", "KD-12", "--title", "t", "--body-file", str(body)], run=slow, out=io.StringIO(), err=err) == 3
    assert "kd-forge prs" in err.getvalue()
    out = io.StringIO()  # the server sent no link — a hint about kd-forge prs, exit 0
    assert pr.main(["digipaddock", "KD-12", "--title", "t", "--body-file", str(body)], run=fake_run(BRANCH), out=out, err=io.StringIO()) == 0
    assert "kd-forge prs digipaddock" in out.getvalue() and "Cage (computed): нет" in out.getvalue()


def test_main_missing_body_file_and_help(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch)
    err = io.StringIO()
    assert pr.main(["digipaddock", "KD-12", "--title", "t", "--body-file", str(tmp_path / "none.md")],
                   run=fake_run(BRANCH), out=io.StringIO(), err=err) == 1
    assert err.getvalue().startswith("kd-pr: ")
    assert pr.main(["--help"], run=fake_run(BRANCH), out=io.StringIO(), err=io.StringIO()) == 0
    assert pr.main(["digipaddock"], run=fake_run(BRANCH), out=io.StringIO(), err=io.StringIO()) == 2


# ---- two dev sessions, a worktree per card ------------------------------------------------------------------

W = "/srv/digipaddock-agents/dev/work"


def _trees(*pairs):
    """`git worktree list --porcelain` of the main clone on main plus (path, branch) pairs."""
    blocks = [f"worktree {W}/DigiPaddock\nHEAD {MAIN}\nbranch refs/heads/main\n"]
    blocks += [f"worktree {path}\nHEAD {MAIN}\nbranch refs/heads/{branch}\n" for path, branch in pairs]
    return (0, "\n".join(blocks), "")


def test_pr_uses_the_card_worktree(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch)
    body = tmp_path / "b.md"; body.write_text(BODY, encoding="utf-8")
    tree = f"{W}/DigiPaddock.KD-12"
    run = fake_run({"worktree list --porcelain": _trees((tree, "agent/KD-12-x"), (f"{W}/DigiPaddock.KD-13", "agent/KD-13-y")),
                    "rev-parse --abbrev-ref": (0, "agent/KD-12-x\n", ""),
                    "push origin": (0, "", "remote:   https://forge.example/x/y/pulls/12\n")})
    assert pr.main(["digipaddock", "KD-12", "--title", "KD-12: x", "--body-file", str(body)], run=run, out=io.StringIO(),
                   err=io.StringIO()) == 0
    after = [c for c in run.calls if "worktree" not in c]
    assert after and all(c[c.index("-C") + 1] == tree for c in after)


def test_a_branch_of_an_earlier_card_prefix_is_the_cards(tmp_path, monkeypatch):
    # A PR opened before the move keeps its branch, so its topic updates the same PR
    tree = f"{W}/DigiPaddock.KD-12"
    run = fake_run({"worktree list --porcelain": _trees((tree, "agent/OLD-12-x")),
                    "rev-parse --abbrev-ref": (0, "agent/OLD-12-x\n", "")})
    assert pr.tree_of(REPO, "KD-12", run) == tree
    assert pr.check(REPO, "KD-12", "KD-12: fix", BODY, run) == ("agent/OLD-12-x", [])
    with pytest.raises(pr.PrError, match="agent/KD-13"):
        pr.check(REPO, "KD-13", "KD-13: fix", BODY, run)


def test_pr_without_worktree_uses_the_clone():
    run = fake_run({"worktree list --porcelain": _trees()})
    assert pr.tree_of(REPO, "KD-12", run) == REPO.dir


def test_pr_refuses_a_worktree_outside_work(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch)
    body = tmp_path / "b.md"; body.write_text(BODY, encoding="utf-8")
    for outside in ("/tmp/KD-12", f"{W}/../x", f"{W}/a/b"):
        run = fake_run({"worktree list --porcelain": _trees((outside, "agent/KD-12-x")), **BRANCH})
        err = io.StringIO()
        assert pr.main(["digipaddock", "KD-12", "--title", "t", "--body-file", str(body)], run=run, out=io.StringIO(),
                       err=err) == 1
        assert outside in err.getvalue()
        assert not any("push" in c for c in run.calls)


def test_pr_refuses_two_trees_of_one_card():
    run = fake_run({"worktree list --porcelain": _trees((f"{W}/a", "agent/KD-12-a"), (f"{W}/b", "agent/KD-12-b"))})
    with pytest.raises(pr.PrError, match="KD-12"):
        pr.tree_of(REPO, "KD-12", run)


def test_pr_worktree_list_failure_is_a_refusal():
    run = fake_run({"worktree list --porcelain": (128, "", "fatal: not a git repository")})
    with pytest.raises(pr.PrError, match="worktree"):
        pr.tree_of(REPO, "KD-12", run)


# ---- the passport's target, informational checks, drafts --------------------------------------------------

DEV = "fedcba9876543210fedcba9876543210fedcba98"


def _passport(tmp_path, monkeypatch, **entry):
    reg = tmp_path / "repos.json"
    import json
    reg.write_text(json.dumps({"digipaddock": {"repo": "example/digipaddock", "dir": "work/DigiPaddock", **entry}}),
                   encoding="utf-8")
    monkeypatch.setattr(pr, "REPOS", str(reg))
    body = tmp_path / "b.md"
    body.write_text(BODY, encoding="utf-8")
    return str(body)


def _go(body, *extra, run=None):
    run = run or fake_run({**BRANCH, "push origin": (0, "", "remote:   https://forge.example.test/x/y/pulls/12\n")})
    out, err = io.StringIO(), io.StringIO()
    rc = pr.main(["digipaddock", "KD-12", "--title", "KD-12: x", "--body-file", body, *extra], run=run, out=out, err=err)
    return rc, out.getvalue(), err.getvalue(), run


def test_target_must_be_in_the_passport(tmp_path, monkeypatch):
    body = _passport(tmp_path, monkeypatch, base="dev", targets=["dev"])
    rc, _, err, run = _go(body, "--target", "main")
    assert rc == 1 and "ветка main не в targets проекта: dev" in err and run.calls == []


def test_default_target_is_the_only_one(tmp_path, monkeypatch):
    body = _passport(tmp_path, monkeypatch, base="dev", targets=["dev"])
    run = fake_run({**BRANCH, "ls-remote --exit-code origin refs/heads/dev": (0, f"{DEV}\trefs/heads/dev\n", ""),
                    "push origin": (0, "", "")})
    rc, _, err, run = _go(body, run=run)
    assert rc == 0, err
    push = [c for c in run.calls if "push" in c][0]
    assert "HEAD:refs/for/dev" in push


def test_two_targets_need_one_named(tmp_path, monkeypatch):
    body = _passport(tmp_path, monkeypatch, targets=["main", "dev"])
    rc, _, err, run = _go(body)
    assert rc == 1 and "--target" in err and "main, dev" in err and run.calls == []


def test_cage_base_is_the_live_target():
    repo = REPO._replace(base="dev", targets=("dev",))
    run = fake_run({**BRANCH, "ls-remote --exit-code origin refs/heads/dev": (0, f"{DEV}\trefs/heads/dev\n", "")})
    pr.check(repo, "KD-12", "t", BODY, run, target="dev")
    assert any(f"{DEV}...HEAD" in c for c in run.calls)
    assert not any("refs/heads/main" in " ".join(c) for c in run.calls)
    odd = fake_run({**BRANCH, "ls-remote": (0, f"{DEV}\trefs/heads/main\n", "")})  # the answer names another ref
    with pytest.raises(pr.PrError, match="refs/heads/dev"):
        pr.check(repo, "KD-12", "t", BODY, odd, target="dev")


def test_push_goes_to_the_target():
    assert pr.push_command("agent/KD-12-x", "t", "b", "dev")[:4] == ["git", "push", "origin", "HEAD:refs/for/dev"]


def test_checks_section_needs_no_pytest_line():
    run = fake_run(BRANCH)
    assert pr.check(REPO, "KD-12", "t", "## Checks\nran the cabinet tests by hand\n## Cage\nнет\n", run)[1] == []
    with pytest.raises(pr.PrError, match="## Checks"):
        pr.check(REPO, "KD-12", "t", "## Cage\nнет\n", run)


def test_required_profiles_make_a_draft(tmp_path, monkeypatch):
    body = _passport(tmp_path, monkeypatch, checks={"profiles": {"unit": {"required": True}, "static": {"required": True},
                                                                  "smoke": {}}})
    rc, out, err, run = _go(body)
    assert rc == 0, err
    push = [c for c in run.calls if "push" in c][0]
    assert "title=WIP: KD-12: x" in push
    sent = base64.b64decode([a for a in push if a.startswith("description=")][0][len(pr.DESC_PREFIX):]).decode()
    assert sent.rstrip().endswith("Cage (computed): нет\nChecks (passport): обязательны devcheck/unit, devcheck/static "
                                  "— PR черновик до зелёных статусов")
    assert "черновик" in out and "kd-forge publish digipaddock 12" in out


def test_without_required_profiles_no_draft(tmp_path, monkeypatch):
    body = _passport(tmp_path, monkeypatch)
    rc, out, err, run = _go(body)
    push = [c for c in run.calls if "push" in c][0]
    assert rc == 0 and "title=KD-12: x" in push and "черновик" not in out
    sent = base64.b64decode([a for a in push if a.startswith("description=")][0][len(pr.DESC_PREFIX):]).decode()
    assert sent.rstrip().endswith("Checks (passport): нет обязательных")


@pytest.mark.parametrize("given", ["WIP: KD-1: x", "[WIP] KD-1: x", "wip:KD-1: x", "  [wip]  KD-1: x", "KD-1: x",
                                   "WIP: [WIP] KD-1: x", "[WIP] wip: WIP: KD-1: x"])
def test_wip_prefix_is_never_doubled(given):
    assert pr.draft_title(given, ["unit"]) == "WIP: KD-1: x"
    assert pr.draft_title(given, []) == "KD-1: x"
