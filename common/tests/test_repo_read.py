"""kd-repo: read-only git views of the repo.read checkouts."""

import os
import subprocess

import pytest

from kd import repo_read as rr


@pytest.fixture
def root(tmp_path):
    for name in ("digipaddock", "app1"):
        (tmp_path / name / ".git").mkdir(parents=True)
    (tmp_path / "board").mkdir()  # no .git: not a checkout
    try:
        os.symlink(tmp_path / "digipaddock", tmp_path / "linked")
    except OSError:  # Windows without the symlink right: the linked case is skipped below
        pass
    (tmp_path / "digipaddock.new" / ".git").mkdir(parents=True)  # a half-made clone of the sync
    return tmp_path


class Run:
    def __init__(self, rc=0, out=""):
        self.calls = []
        self.rc, self.out = rc, out

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw.get("cwd"), kw.get("env")))
        return subprocess.CompletedProcess(argv, self.rc, self.out, "")


@pytest.mark.parametrize("argv, expected", [
    (["log", "digipaddock"], ("log", "digipaddock", ())),
    (["log", "digipaddock", "-n", "20", "--oneline", "--", "common/kd/watch.py"],
     ("log", "digipaddock", ("-n", "20", "--oneline", "--", "common/kd/watch.py"))),
    (["log", "digipaddock", "--since=2026-10-01", "--author=Owner", "-p", "main"],
     ("log", "digipaddock", ("--since=2026-10-01", "--author=Owner", "-p", "main"))),
    (["show", "digipaddock", "9c3f463", "--stat"], ("show", "digipaddock", ("9c3f463", "--stat"))),
    (["diff", "digipaddock", "main~3..main", "--name-only"], ("diff", "digipaddock", ("main~3..main", "--name-only"))),
    (["blame", "digipaddock", "-L", "10,20", "--", "README.md"], ("blame", "digipaddock", ("-L", "10,20", "--", "README.md"))),
    (["blame", "digipaddock", "-L10,20", "README.md"], ("blame", "digipaddock", ("-L10,20", "README.md"))),
    (["log", "digipaddock", "origin/main..HEAD", "HEAD~3", "--", "docs/b.md"],
     ("log", "digipaddock", ("origin/main..HEAD", "HEAD~3", "--", "docs/b.md"))),  # ranges are not path components
])
def test_accepts_read_only_forms(argv, expected):
    request = rr.plan(argv)
    assert (request.subcommand, request.name, request.args) == expected


@pytest.mark.parametrize("argv", [
    ["log", "digipaddock", "--output=/srv/digipaddock-repos/x"],
    ["log", "digipaddock", "--output", "x"],
    ["diff", "digipaddock", "--ext-diff"],
    ["show", "digipaddock", "--textconv"],
    ["log", "digipaddock", "-c", "core.pager=sh"],
    ["log", "digipaddock", "--git-dir=/etc"],
    ["log", "digipaddock", "-C", "/"],
    ["log", "digipaddock", "--exec-path=/tmp"],
    ["diff", "digipaddock", "--no-index", "/etc/passwd", "/etc/shadow"],
    ["log", "digipaddock", "-n"],  # a valued option without its value
    ["log", "digipaddock", "-n", "--oneline"],  # the value would be an option
    ["log", "digipaddock", "--grep=x\ny"],  # control characters
    ["log", "digipaddock", "-x"],
    ["fetch", "digipaddock"],
    ["status", "digipaddock"],
    ["log"],
    [],
    ["log", "digipaddock", "--", "--output=x", "--", "y"],  # a second separator is not a path
    # Final review: two paths outside the work tree make git diff imply --no-index (any readable file)
    ["diff", "digipaddock", "/etc/passwd", "/etc/hostname"],
    ["diff", "digipaddock", "--", "../x", "../y"],
    ["diff", "digipaddock", "a/../../x", "y"],
    ["log", "digipaddock", "~/x"],
    ["show", "digipaddock", "HEAD:../../etc/passwd"],
    ["show", "digipaddock", ""],
])
def test_refuses_options_outside_the_allowlist(argv):
    with pytest.raises(rr.Refused):
        rr.plan(argv)
    run = Run()
    assert rr.main(argv, root="/nonexistent", run=run) == 2 and run.calls == []


@pytest.mark.parametrize("name", ["../board", "board", "linked", "digipaddock.new", "Digipaddock", "x" * 33, "", ".git"])
def test_refuses_a_name_outside_the_checkouts(root, name):
    if name == "linked" and not (root / "linked").is_symlink():
        pytest.skip("symlinks are not available here")
    with pytest.raises(rr.Refused):
        rr.checkout_dir(name, str(root))
    run = Run()
    assert rr.main(["log", name], root=str(root), run=run) == 2
    assert run.calls == []


def test_git_runs_in_the_checkout_without_pager_or_config(root):
    run = Run()
    assert rr.main(["log", "digipaddock", "-n", "3", "--", "README.md"], root=str(root), run=run) == 0
    [(argv, cwd, env)] = run.calls
    assert argv == ["git", "--no-pager", "log", "-n", "3", "--", "README.md"]
    assert cwd == str(root / "digipaddock")
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_OPTIONAL_LOCKS"] == "0" and env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_PAGER"] == "cat"
    assert "GIT_DIR" not in env and "GIT_WORK_TREE" not in env
    # The checkouts belong to the repo user: without safe.directory git refuses them ("dubious ownership").
    # Command-scope config is protected config, so it counts; the exact path, not "*".
    assert (env["GIT_CONFIG_COUNT"], env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"]) == (
        "1", "safe.directory", str(root / "digipaddock"))


def test_git_exit_code_is_passed_through(root):
    run = Run(rc=128)
    assert rr.main(["show", "digipaddock", "nope"], root=str(root), run=run) == 128


def test_list_names_the_checkouts_with_their_head(root, capsys):
    run = Run(out="9c3f463 2026-10-14 docs: KD-210 minors\n")
    assert rr.main(["list"], root=str(root), run=run) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["app1  9c3f463 2026-10-14 docs: KD-210 minors", "digipaddock  9c3f463 2026-10-14 docs: KD-210 minors"]
    assert [c[0][-1] for c in run.calls] == ["--format=%h %cs %s"] * 2  # linked, board, digipaddock.new are skipped
    assert all(c[0][:4] == ["git", "--no-pager", "log", "-1"] for c in run.calls)
    assert all(c[2]["GIT_CONFIG_VALUE_0"] == c[1] for c in run.calls)  # safe.directory per checkout


def test_usage_and_refusals_go_to_stderr(root, capsys):
    assert rr.main(["log", "digipaddock", "--ext-diff"], root=str(root), run=Run()) == 2
    assert "kd-repo:" in capsys.readouterr().err
    assert rr.main(["help"], root=str(root), run=Run()) == 2
