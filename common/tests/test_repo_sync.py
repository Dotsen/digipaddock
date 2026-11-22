"""kd-repo-sync: the trusted refresher of the repo.read checkouts."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from kd import repo_sync as rs

UNITS = Path(__file__).resolve().parents[1] / "systemd"
FETCH = ["fetch", "--quiet", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*",
         "+refs/pull/*/head:refs/pull/*/head"]  # the branches and the heads of pull requests
REPOS = {"digipaddock": {"dir": "work/DigiPaddock", "release": "digipaddock", "repo": "example/DigiPaddock"},
         "app1": {"dir": "work/App", "repo": "example/App"}}


class Run:
    """Records git calls; ``fail`` names argv prefixes that return 128; clone makes the target."""

    def __init__(self, fail=()):
        self.calls, self.fail = [], tuple(fail)

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if any(argv[:len(f)] == list(f) for f in self.fail):
            return subprocess.CompletedProcess(argv, 128, "", "fatal: nope")
        if argv[:2] == ["git", "clone"]:
            os.makedirs(os.path.join(argv[-1], ".git"))
        return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture
def repos(tmp_path):
    path = tmp_path / "repos.json"
    path.write_text(json.dumps(REPOS), encoding="utf-8")
    return str(path)


def test_load_repos_keeps_only_valid_names_and_repositories(tmp_path):
    path = tmp_path / "repos.json"
    path.write_text(json.dumps({"digipaddock": {"repo": "example/DigiPaddock"}, "Bad": {"repo": "x/y"},
                                "nope": {"repo": "../x"}, "none": {}}), encoding="utf-8")
    with pytest.raises(rs.SyncError, match="Bad"):
        rs.load_repos(str(path))
    path.write_text(json.dumps({"digipaddock": {"repo": "example/DigiPaddock"}}), encoding="utf-8")
    assert rs.load_repos(str(path)) == {"digipaddock": ("example/DigiPaddock", "main")}


@pytest.mark.parametrize("other", [None, [], ["repo.read"], ["board.read"]])
def test_shared_timer_is_needed_only_by_another_installed_reader(tmp_path, other):
    registry = tmp_path / "agents.json"
    registry.write_text(json.dumps({"analyst": {}, "sysadmin": {}}), encoding="utf-8")
    for name, capabilities in (("sysadmin", ["repo.read"]), ("analyst", other)):
        if capabilities is not None:
            policy = tmp_path / name / ".claude" / "policy.json"
            policy.parent.mkdir(parents=True)
            policy.write_text(json.dumps({"capabilities": capabilities}), encoding="utf-8")
    assert rs.has_other_readers("sysadmin", str(registry), str(tmp_path)) is (other == ["repo.read"])


@pytest.mark.parametrize("broken", ["registry", "policy"])
def test_uncertain_reader_state_does_not_stop_the_shared_timer(tmp_path, broken):
    registry = tmp_path / "agents.json"
    registry.write_text("{" if broken == "registry" else '{"analyst": {}}', encoding="utf-8")
    if broken == "policy":
        policy = tmp_path / "analyst" / ".claude" / "policy.json"
        policy.parent.mkdir(parents=True)
        policy.write_text("{", encoding="utf-8")
    assert rs.has_other_readers("sysadmin", str(registry), str(tmp_path))


def test_first_run_clones_into_a_temporary_name_then_renames(tmp_path, repos):
    run, log = Run(), []
    assert rs.sync_all(repos_path=repos, root=str(tmp_path), run=run, env={}, log=log.append) == 0
    assert run.calls == [
        ["git", "clone", "--quiet", "--branch", "main", "ssh://git@forge.example.test/example/App.git",
         str(tmp_path / "app1.new")],
        ["git", "-C", str(tmp_path / "app1.new"), *FETCH],
        ["git", "clone", "--quiet", "--branch", "main", "ssh://git@forge.example.test/example/DigiPaddock.git",
         str(tmp_path / "digipaddock.new")],
        ["git", "-C", str(tmp_path / "digipaddock.new"), *FETCH],
    ]
    assert (tmp_path / "app1" / ".git").is_dir() and (tmp_path / "digipaddock" / ".git").is_dir()
    assert not (tmp_path / "app1.new").exists()


def test_next_runs_fetch_and_reset_to_origin_main(tmp_path, repos):
    for name in REPOS:
        (tmp_path / name / ".git").mkdir(parents=True)
    run = Run()
    assert rs.sync_all(repos_path=repos, root=str(tmp_path), run=run, env={}, log=lambda s: None) == 0
    digipaddock = str(tmp_path / "digipaddock")
    assert run.calls[4:] == [
        ["git", "-C", digipaddock, "remote", "set-url", "origin", "ssh://git@forge.example.test/example/DigiPaddock.git"],
        ["git", "-C", digipaddock, *FETCH],
        ["git", "-C", digipaddock, "reset", "--quiet", "--hard", "origin/main"],
        ["git", "-C", digipaddock, "clean", "--quiet", "-fd"],
    ]


def test_one_failed_repository_does_not_stop_the_others(tmp_path, repos):
    (tmp_path / "digipaddock" / ".git").mkdir(parents=True)
    run, log = Run(fail=[("git", "clone")]), []
    assert rs.sync_all(repos_path=repos, root=str(tmp_path), run=run, env={}, log=log.append) == 1
    assert not (tmp_path / "app1.new").exists() and not (tmp_path / "app1").exists()
    assert any("app1" in line and "clone" in line for line in log)
    assert ["git", "-C", str(tmp_path / "digipaddock"), *FETCH] in run.calls


def test_sync_refuses_a_symlinked_checkout(tmp_path, repos):
    (tmp_path / "elsewhere" / ".git").mkdir(parents=True)
    try:
        os.symlink(tmp_path / "elsewhere", tmp_path / "digipaddock")
    except OSError:
        pytest.skip("symlinks are not available here")
    run, log = Run(), []
    assert rs.sync_all(repos_path=repos, root=str(tmp_path), run=run, env={}, log=log.append) == 1
    assert not any("-C" in c and str(tmp_path / "digipaddock") in c for c in run.calls)
    assert any("digipaddock" in line and "link" in line for line in log)


def test_git_env_pins_the_key_and_the_host_key(tmp_path):
    env = rs.git_env({"HOME": str(tmp_path), "PATH": "/usr/bin", "GIT_DIR": "/x"},
                     key="/home/digi-repo/.ssh/repo_sync_ed25519",
                     known_hosts="/opt/digipaddock/common/install/forgejo_known_hosts")
    ssh = env["GIT_SSH_COMMAND"]
    for part in ("-i /home/digi-repo/.ssh/repo_sync_ed25519", "-o IdentitiesOnly=yes", "-o BatchMode=yes",
                 "-o UserKnownHostsFile=/opt/digipaddock/common/install/forgejo_known_hosts",
                 "-o StrictHostKeyChecking=yes", "-o GlobalKnownHostsFile=/dev/null", "-o UpdateHostKeys=no"):
        assert part in ssh
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null" and "GIT_DIR" not in env


def test_main_fails_closed_on_a_broken_registry(tmp_path, capsys):
    bad = tmp_path / "repos.json"
    bad.write_text("{", encoding="utf-8")
    assert rs.main([], repos_path=str(bad), root=str(tmp_path), run=Run(), env={"HOME": str(tmp_path)}) == 1
    assert "repos.json" in capsys.readouterr().err


def test_main_uses_the_key_of_the_service_home(tmp_path, repos, capsys):
    run = Run()
    assert rs.main([], repos_path=repos, root=str(tmp_path), run=run, env={"HOME": "/home/digi-repo"}) == 0
    assert len(run.calls) == 4  # two clones, each with its fetch of the pull heads; the key comes from HOME
    assert rs.main(["x"], repos_path=repos, root=str(tmp_path), run=run, env={}) == 2


def test_service_runs_as_the_repo_user_with_a_closed_system():
    text = (UNITS / "digipaddock-repo-sync.service").read_text(encoding="utf-8")
    for line in ("User=@repo@", "Group=@repo@", "ProtectSystem=strict", "ReadWritePaths=/srv/digipaddock-repos",
                 "OnFailure=digipaddock-alert@digipaddock-repo-sync.service", "ExecStart=/usr/local/bin/kd-repo-sync", "UMask=0027",
                 "NoNewPrivileges=yes", "CapabilityBoundingSet="):
        assert line in text, line
    timer = (UNITS / "digipaddock-repo-sync.timer").read_text(encoding="utf-8")
    assert "OnUnitActiveSec=10min" in timer and "WantedBy=timers.target" in timer


def test_forge_url_comes_from_host_json():
    assert rs.forge_url("a/b") == "ssh://git@forge.example.test/a/b.git"


def test_the_overlay_entry_of_a_release_gets_its_own_checkout(tmp_path):
    """digipaddock (code) and digipaddock-deploy (role overlay of the same release) — two checkouts, no extra setup."""
    path = tmp_path / "repos.json"
    path.write_text(json.dumps({
        "digipaddock": {"dir": "work/digipaddock", "release": "digipaddock", "repo": "example/digipaddock"},
        "digipaddock-deploy": {"dir": "work/digipaddock-deploy", "release": "digipaddock", "role": "overlay",
                         "repo": "example/digipaddock-deploy"}}), encoding="utf-8")
    assert rs.load_repos(str(path)) == {"digipaddock": ("example/digipaddock", "main"),
                                        "digipaddock-deploy": ("example/digipaddock-deploy", "main")}
    root = tmp_path / "copies"
    root.mkdir()
    run = Run()
    assert rs.sync_all(repos_path=str(path), root=str(root), run=run, env={}, log=lambda s: None) == 0
    clones = [c[-1] for c in run.calls if c[:2] == ["git", "clone"]]
    assert clones == [str(root / "digipaddock.new"), str(root / "digipaddock-deploy.new")]
    assert (root / "digipaddock" / ".git").is_dir() and (root / "digipaddock-deploy" / ".git").is_dir()


def test_base_branch_from_the_passport(tmp_path):
    """the checkout follows the passport's base branch, not main."""
    path = tmp_path / "repos.json"
    path.write_text(json.dumps({"api": {"dir": "work/Api", "repo": "example/Api", "base": "master"}}),
                    encoding="utf-8")
    run = Run()
    assert rs.sync_all(repos_path=str(path), root=str(tmp_path), run=run, env={}, log=lambda s: None) == 0
    assert run.calls == [["git", "clone", "--quiet", "--branch", "master", "ssh://git@forge.example.test/example/Api.git",
                          str(tmp_path / "api.new")],
                         ["git", "-C", str(tmp_path / "api.new"), *FETCH]]
    run = Run()
    assert rs.sync_all(repos_path=str(path), root=str(tmp_path), run=run, env={}, log=lambda s: None) == 0
    assert ["git", "-C", str(tmp_path / "api"), "reset", "--quiet", "--hard", "origin/master"] in run.calls


def test_schema_2_entry_with_extra_keys_loads(tmp_path):
    path = tmp_path / "repos.json"
    path.write_text(json.dumps({"digipaddock": {
        "dir": "work/digipaddock", "repo": "example/digipaddock", "release": "digipaddock", "base": "main",
        "targets": ["main"], "checks": {"profiles": {"unit": {"required": True}}, "contour": "", "timeout_s": 1800},
        "merge_effects": {"main": "deploy to stage"},
        "merge": {"require_up_to_date": False, "up_to_date_paths": ["common/kd/cabinet/schema.py"]}}}),
        encoding="utf-8")
    assert rs.load_repos(str(path)) == {"digipaddock": ("example/digipaddock", "main")}


@pytest.mark.parametrize("base", ["", "-x", "a..b", "a b", "x.lock", 7, None, "a/b"])
def test_a_bad_base_branch_is_a_sync_error(tmp_path, base):
    path = tmp_path / "repos.json"
    path.write_text(json.dumps({"api": {"dir": "work/Api", "repo": "example/Api", "base": base}}), encoding="utf-8")
    with pytest.raises(rs.SyncError, match="api"):
        rs.load_repos(str(path))


# --- the copies carry the heads of pull requests ------------------------------------------------
def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def forge(tmp_path, monkeypatch):
    """A repository as the forge serves it: ``main`` and pull request 7 with its head; ``forge_url`` leads to it."""
    origin = tmp_path / "forge" / "app"
    origin.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=origin)
    _git("config", "user.email", "t@t", cwd=origin)
    _git("config", "user.name", "t", cwd=origin)
    (origin / "a.txt").write_text("one\n")
    _git("add", "a.txt", cwd=origin)
    _git("commit", "-qm", "one", cwd=origin)
    _git("checkout", "-q", "-b", "feature", cwd=origin)
    (origin / "a.txt").write_text("two\n")
    _git("commit", "-qam", "two", cwd=origin)
    _git("update-ref", "refs/pull/7/head", "HEAD", cwd=origin)
    _git("checkout", "-q", "main", cwd=origin)
    _git("branch", "-qD", "feature", cwd=origin)
    monkeypatch.setattr(rs, "forge_url", lambda repo: str(origin))
    return origin


def _sync(tmp_path):
    root = tmp_path / "copies"
    root.mkdir(exist_ok=True)
    log = []
    ok = rs.sync_one("app", "example/App", root=str(root), run=subprocess.run, env=dict(os.environ), log=log.append)
    assert ok, log
    return root / "app"


def test_fetch_brings_the_pull_heads(tmp_path, forge):
    head = _git("rev-parse", "refs/pull/7/head", cwd=forge)

    copy = _sync(tmp_path)  # the first run: a clone, the pull heads with it

    assert _git("rev-parse", "refs/pull/7/head", cwd=copy) == head
    assert _git("rev-parse", "HEAD", cwd=copy) == _git("rev-parse", "main", cwd=forge)
    assert (copy / "a.txt").read_text() == "one\n"  # the work tree stays on the base branch
    (forge / "b.txt").write_text("b\n")
    _git("add", "b.txt", cwd=forge)
    _git("commit", "-qm", "three", cwd=forge)
    _git("update-ref", "refs/pull/8/head", "HEAD", cwd=forge)
    _git("update-ref", "refs/pull/7/head", "HEAD", cwd=forge)  # a push into pull 7, forced or not

    _sync(tmp_path)  # the next runs: a fetch

    moved = _git("rev-parse", "main", cwd=forge)
    assert _git("rev-parse", "refs/pull/8/head", cwd=copy) == moved
    assert _git("rev-parse", "refs/pull/7/head", cwd=copy) == moved
    assert _git("rev-parse", "refs/remotes/origin/main", cwd=copy) == moved == _git("rev-parse", "HEAD", cwd=copy)


def test_a_closed_pull_head_is_pruned(tmp_path, forge):
    copy = _sync(tmp_path)
    assert "refs/pull/7/head" in _git("for-each-ref", "--format=%(refname)", cwd=copy).split()
    _git("update-ref", "-d", "refs/pull/7/head", cwd=forge)
    _git("branch", "-q", "side", cwd=forge)
    _sync(tmp_path)
    assert "refs/remotes/origin/side" in _git("for-each-ref", "--format=%(refname)", cwd=copy).split()
    _git("branch", "-qD", "side", cwd=forge)

    _sync(tmp_path)

    refs = _git("for-each-ref", "--format=%(refname)", cwd=copy).split()
    assert "refs/pull/7/head" not in refs and "refs/remotes/origin/side" not in refs
    assert "refs/remotes/origin/main" in refs


def test_a_clone_whose_pull_heads_did_not_come_is_not_a_copy(tmp_path, repos):
    """The first run is whole or it is not: a copy without the pull heads would be taken for a synced one."""
    run, log = Run(fail=[("git", "-C", str(tmp_path / "app1.new"), "fetch")]), []

    assert rs.sync_all(repos_path=repos, root=str(tmp_path), run=run, env={}, log=log.append) == 1

    assert not (tmp_path / "app1").exists() and not (tmp_path / "app1.new").exists()
    assert (tmp_path / "digipaddock" / ".git").is_dir()
    assert any("app1" in line and "fetch" in line for line in log)
