"""kd-mirror: each new main of the forge goes to the GitHub mirror as one snapshot commit — the tree of main on top
of GitHub's main, never the forge's history — and only through both privacy scanners; a red scan blocks that SHA
once (one alert), a new SHA is scanned again."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kd import mirror

AUTHOR = "DigiPaddock <mirror@example.test>"
NOW = "2026-10-26T10:00:00Z"
CLEAN = lambda tree, records: []  # noqa: E731
GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "Owner", "GIT_AUTHOR_EMAIL": "owner@forge.example.test",
           "GIT_COMMITTER_NAME": "Owner", "GIT_COMMITTER_EMAIL": "owner@forge.example.test",
           "GIT_CONFIG_NOSYSTEM": "1"}


def git(*args, cwd=None):
    p = subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *args], cwd=cwd,
                       capture_output=True, text=True, env=GIT_ENV)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


class Forge:
    """The forge: a working copy whose main is pushed to a bare repository kd-mirror clones."""

    def __init__(self, root):
        self.work, self.bare = root / "forge-work", root / "forge.git"
        git("init", "-q", "-b", "main", str(self.work))
        git("init", "-q", "--bare", "-b", "main", str(self.bare))
        git("remote", "add", "origin", str(self.bare), cwd=self.work)

    def commit(self, files, message, allow_empty=False):
        for name, text in files.items():
            path = self.work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        git("add", "-A", cwd=self.work)
        git("commit", "-q", *(["--allow-empty"] if allow_empty else []), "-m", message, cwd=self.work)
        git("push", "-q", "origin", "main", cwd=self.work)
        return git("rev-parse", "HEAD", cwd=self.work)

    def tag(self, name, rev="HEAD"):
        git("tag", "-a", "-m", f"release {name} — internal notes", name, rev, cwd=self.work)
        git("push", "-q", "origin", f"refs/tags/{name}", cwd=self.work)


@pytest.fixture
def forge(tmp_path):
    forge = Forge(tmp_path)
    forge.commit({"README": "digipaddock\n"}, "KD-1: первый коммит с внутренними подробностями")
    return forge


@pytest.fixture
def github(tmp_path):
    bare = tmp_path / "github.git"
    git("init", "-q", "--bare", "-b", "main", str(bare))
    return bare


@pytest.fixture(autouse=True)
def _no_host_state(tmp_path, monkeypatch):
    monkeypatch.setattr(mirror, "STATE_DIR", str(tmp_path / "host-state"))


def go(tmp_path, forge, github, scanners=CLEAN, now=NOW, signers=None, run_git=subprocess.run):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    return mirror.run(state_dir=state, mirror_dir=tmp_path / "mirror" / "digipaddock.git", remote=str(github),
                      forge_url=str(forge.bare), scanners=scanners, now=lambda: now,
                      disabled=tmp_path / "disabled", signers=signers, author=AUTHOR, git=run_git)


def saved(tmp_path):
    return json.loads((tmp_path / "state" / "state.json").read_text())


def published(github):
    """GitHub's main: [(sha, tree, parents, author, message)], newest first; [] while it has none."""
    if subprocess.run(["git", "--git-dir", str(github), "rev-parse", "--verify", "--quiet", "main"],
                      capture_output=True).returncode != 0:
        return []
    out = git("--git-dir", str(github), "log", "--format=%H%x1f%T%x1f%P%x1f%an <%ae>%x1f%cn <%ce>%x1f%B%x1e", "main")
    rows = []
    for chunk in out.split("\x1e"):
        if chunk.strip():
            sha, tree, parents, author, committer, body = chunk.strip("\n").split("\x1f")
            rows.append({"sha": sha, "tree": tree, "parents": parents.split(), "author": author,
                         "committer": committer, "message": body.strip()})
    return rows


def has(bare, sha):
    return subprocess.run(["git", "--git-dir", str(bare), "cat-file", "-e", sha + "^{commit}"],
                          capture_output=True).returncode == 0


def test_the_first_publish_is_one_commit_with_the_forge_tree_and_none_of_its_history(tmp_path, forge, github):
    head = forge.commit({"common/a.py": "x = 1\n"}, "KD-2: ещё один внутренний коммит")
    assert go(tmp_path, forge, github) == 0
    [snap] = published(github)
    assert snap["tree"] == git("rev-parse", "HEAD^{tree}", cwd=forge.work) and snap["parents"] == []
    assert snap["author"] == AUTHOR and snap["committer"] == AUTHOR
    assert snap["message"] == f"Update to upstream {head[:12]}"
    assert not has(github, head) and not has(github, git("rev-parse", "HEAD~1", cwd=forge.work))
    assert saved(tmp_path) == {"pushed": head, "blocked": "", "blocked_at": "", "tags": []}


def test_the_next_publish_is_a_child_of_githubs_main(tmp_path, forge, github):
    assert go(tmp_path, forge, github) == 0
    first = published(github)[0]["sha"]
    head = forge.commit({"README": "digipaddock 2\n"}, "KD-3: правка")
    assert go(tmp_path, forge, github) == 0
    newest, older = published(github)
    assert newest["parents"] == [first] and older["sha"] == first
    assert newest["tree"] == git("rev-parse", "HEAD^{tree}", cwd=forge.work) and not has(github, head)


def test_a_repository_initialized_by_hand_is_built_on(tmp_path, forge, github):
    """The owner recreates the mirror with a snapshot of their own: the mirror goes on from it, no force."""
    seed = tmp_path / "seed"
    git("init", "-q", "-b", "main", str(seed))
    (seed / "README").write_text("seed\n")
    git("add", "-A", cwd=seed)
    git("commit", "-q", "-m", "chore: initialize", cwd=seed)
    git("push", "-q", str(github), "main", cwd=seed)
    assert go(tmp_path, forge, github) == 0
    newest, root = published(github)
    assert root["message"] == "chore: initialize" and newest["parents"] == [root["sha"]]


def test_an_unchanged_forge_main_publishes_nothing_and_scans_nothing(tmp_path, forge, github):
    assert go(tmp_path, forge, github) == 0
    scanned = []
    assert go(tmp_path, forge, github, scanners=lambda tree, records: scanned.append(1) or []) == 0
    assert scanned == [] and len(published(github)) == 1


def test_a_new_main_with_the_same_tree_adds_no_commit(tmp_path, forge, github):
    assert go(tmp_path, forge, github) == 0
    head = forge.commit({}, "KD-4: пустой коммит", allow_empty=True)
    assert go(tmp_path, forge, github) == 0
    assert len(published(github)) == 1 and saved(tmp_path)["pushed"] == head


def test_the_scanners_read_the_tree_its_paths_and_the_published_message_not_the_forge_history(tmp_path, forge,
                                                                                               github):
    head = forge.commit({"common/tree-name.json": "{}\n"}, "KD-5: внутреннее сообщение")
    seen = {}

    def scan(tree, records):
        seen["readme"] = (tree / "README").read_text()
        seen["lines"] = [line for _, _, line in records]
        return []

    assert go(tmp_path, forge, github, scanners=scan) == 0
    assert seen["readme"] == "digipaddock\n"
    assert f"Update to upstream {head[:12]}" in seen["lines"] and "common/tree-name.json" in seen["lines"]
    assert any(AUTHOR in line for line in seen["lines"])
    assert not any("KD-5" in line or "KD-1" in line for line in seen["lines"])


def test_a_red_scanner_blocks_once_and_is_quiet_afterwards(tmp_path, forge, github, capsys):
    red = lambda tree, records: ["common/x.py:3: ipv4"]  # noqa: E731
    assert go(tmp_path, forge, github, scanners=red) == 1
    head = git("rev-parse", "HEAD", cwd=forge.work)
    assert published(github) == [] and saved(tmp_path)["blocked"] == head
    assert "kd-mirror: privacy: common/x.py:3: ipv4; main не отправлен на GitHub" in capsys.readouterr().err
    assert go(tmp_path, forge, github, scanners=red) == 0
    assert f"kd-mirror: still blocked at {head[:12]}" in capsys.readouterr().out


def test_a_block_is_alerted_again_after_a_day_and_a_new_main_is_scanned_again(tmp_path, forge, github, capsys):
    red = lambda tree, records: ["x:1: ipv4"]  # noqa: E731
    assert go(tmp_path, forge, github, scanners=red, now="2026-10-25T09:00:00Z") == 1
    assert go(tmp_path, forge, github, scanners=red, now="2026-10-25T20:00:00Z") == 0
    assert go(tmp_path, forge, github, scanners=red, now="2026-10-26T10:00:00Z") == 1
    assert "since 2026-10-25T09:00:00Z" in capsys.readouterr().err
    head = forge.commit({"README": "fixed\n"}, "KD-6: исправлено")
    assert go(tmp_path, forge, github) == 0
    assert saved(tmp_path) == {"pushed": head, "blocked": "", "blocked_at": "", "tags": []}


def test_github_moved_between_read_and_push_is_left_alone(tmp_path, forge, github, capsys):
    """The push is a fast-forward of the main it read: someone else's push in between rejects it."""
    assert go(tmp_path, forge, github) == 0
    before = saved(tmp_path)
    forge.commit({"README": "next\n"}, "KD-7: дальше")
    other = tmp_path / "other"
    git("clone", "-q", str(github), str(other))
    (other / "OTHER").write_text("x\n")
    git("add", "-A", cwd=other)
    git("commit", "-q", "-m", "someone else", cwd=other)

    def race(cmd, **kw):
        if "push" in cmd:
            git("push", "-q", "origin", "main", cwd=other)
        return subprocess.run(cmd, **kw)

    assert go(tmp_path, forge, github, run_git=race) == 2
    assert published(github)[0]["message"] == "someone else" and saved(tmp_path) == before
    assert "kd-mirror: git push" in capsys.readouterr().err


def test_a_tag_on_the_published_main_goes_out_on_its_snapshot_others_stay(tmp_path, forge, github, capsys):
    forge.tag("v0")  # on the first commit: not the main being published
    forge.commit({"README": "v1\n"}, "KD-8: релиз")
    forge.tag("v1")
    assert go(tmp_path, forge, github) == 0
    snap = published(github)[0]["sha"]
    assert git("--git-dir", str(github), "rev-parse", "refs/tags/v1") == snap  # a plain tag: no forge message
    assert git("--git-dir", str(github), "tag", "--list") == "v1"
    assert "tag v0 not pushed" in capsys.readouterr().err


def test_with_allowed_signers_only_signed_tags_go_out(tmp_path, forge, github, monkeypatch, capsys):
    signers = tmp_path / "allowed_signers"
    signers.write_text("owner@example.test ssh-ed25519 AAAA\n")
    forge.tag("v1")
    forge.tag("v2")
    monkeypatch.setattr(mirror, "_signed", lambda git_run, bare, ref, path: ref == "refs/tags/v1" and path == signers)
    assert go(tmp_path, forge, github, signers=signers) == 0
    assert git("--git-dir", str(github), "tag", "--list") == "v1"


def test_a_tag_put_on_a_main_published_earlier_goes_out_on_the_next_run(tmp_path, forge, github, capsys):
    assert go(tmp_path, forge, github) == 0
    snap = published(github)[0]["sha"]
    forge.tag("v1")
    assert go(tmp_path, forge, github) == 0
    assert len(published(github)) == 1  # no new snapshot: the tag goes on the one that is out
    assert git("--git-dir", str(github), "rev-parse", "refs/tags/v1") == snap
    assert saved(tmp_path)["tags"] == ["refs/tags/v1"]
    assert "1 tags" in capsys.readouterr().out


def test_a_late_tag_follows_the_signature_rule_and_is_decided_once(tmp_path, forge, github, monkeypatch, capsys):
    signers = tmp_path / "allowed_signers"
    signers.write_text("owner@example.test ssh-ed25519 AAAA\n")
    assert go(tmp_path, forge, github, signers=signers) == 0
    forge.tag("v1")
    forge.tag("v2")
    checked = []
    monkeypatch.setattr(mirror, "_signed", lambda git_run, bare, ref, path: checked.append(ref) or ref.endswith("v1"))
    assert go(tmp_path, forge, github, signers=signers) == 0
    assert git("--git-dir", str(github), "tag", "--list") == "v1"
    assert saved(tmp_path)["tags"] == ["refs/tags/v1", "refs/tags/v2"]
    checked.clear()
    assert go(tmp_path, forge, github, signers=signers) == 0
    assert checked == []  # an unsigned tag is not checked again every five minutes


def test_an_unchanged_main_without_a_new_tag_does_not_reach_github(tmp_path, forge, github):
    forge.tag("v1")
    assert go(tmp_path, forge, github) == 0
    calls = []

    def record(cmd, **kw):
        calls.append(cmd)
        return subprocess.run(cmd, **kw)

    scanned = []
    assert go(tmp_path, forge, github, run_git=record, scanners=lambda tree, records: scanned.append(1) or []) == 0
    assert scanned == [] and not any(step in cmd for cmd in calls for step in ("ls-remote", "push"))


def test_a_late_tag_is_scanned_and_a_red_one_stays_on_the_forge_once(tmp_path, forge, github, capsys):
    assert go(tmp_path, forge, github) == 0
    forge.tag("v1")
    seen = []

    def red(tree, records):
        seen.extend(line for _, _, line in records)
        return ["<message>:4: private literal"]

    assert go(tmp_path, forge, github, scanners=red) == 1
    assert "refs/tags/v1" in seen  # the tag name is what this run would publish
    assert git("--git-dir", str(github), "tag", "--list") == ""
    assert "kd-mirror: privacy: <message>:4: private literal; теги не отправлены на GitHub" in capsys.readouterr().err
    assert go(tmp_path, forge, github, scanners=red) == 0
    assert saved(tmp_path)["tags"] == ["refs/tags/v1"]


def test_a_late_tag_is_not_put_on_a_github_main_with_another_tree(tmp_path, forge, github, capsys):
    assert go(tmp_path, forge, github) == 0
    other = tmp_path / "other"
    git("clone", "-q", str(github), str(other))
    (other / "OTHER").write_text("x\n")
    git("add", "-A", cwd=other)
    git("commit", "-q", "-m", "someone else", cwd=other)
    git("push", "-q", "origin", "main", cwd=other)
    forge.tag("v1")
    assert go(tmp_path, forge, github) == 1
    assert git("--git-dir", str(github), "tag", "--list") == ""
    assert "tag v1 not pushed — GitHub's main is not the tree of the published main" in capsys.readouterr().err
    assert go(tmp_path, forge, github) == 0  # decided once: no alert every five minutes


def test_the_disabled_flag_exits_zero_without_a_fetch(tmp_path, forge, github, capsys):
    (tmp_path / "disabled").touch()
    assert go(tmp_path, forge, github) == 0
    assert not (tmp_path / "mirror").exists() and "kd-mirror: disabled" in capsys.readouterr().out


def test_the_kill_switch_is_the_specs_flag():
    assert mirror.DISABLED == "/etc/digipaddock/mirror/disabled"


def test_an_unreachable_forge_is_exit_two_and_nothing_recorded(tmp_path, forge, github, capsys):
    forge.bare = tmp_path / "missing.git"
    assert go(tmp_path, forge, github) == 2
    assert not (tmp_path / "state" / "state.json").exists() and "kd-mirror: git" in capsys.readouterr().err


def test_a_scanner_that_cannot_run_keeps_main_back(tmp_path, forge, github, capsys):
    def broken(tree, records):
        raise mirror.MirrorError("closed scanner: no host/private_scan.json")

    assert go(tmp_path, forge, github, scanners=broken) == 2 and published(github) == []
    assert "kd-mirror: closed scanner" in capsys.readouterr().err


CLOSED = Path(mirror.__file__).with_name("private_scan.py")  # the release tree's own


def release_tree(tmp_path, literals='{"literals": ["the-closed-word"]}'):
    tree = tmp_path / "release"
    (tree / "common" / "kd").mkdir(parents=True)
    (tree / "host").mkdir()
    (tree / "common" / "kd" / "private_scan.py").write_bytes(CLOSED.read_bytes())
    (tree / "host" / "private_scan.json").write_text(literals)
    # the overlay's old script is never run: it would find everything
    (tree / "scripts").mkdir()
    (tree / "scripts" / "private_scan.py").write_text("import sys; print('x:1: y'); sys.exit(1)\n")
    return tree


def main_tree(tmp_path, files):
    tree = tmp_path / "work" / "tree"
    tree.mkdir(parents=True)
    (tree / "privacy-scan.json").write_text(json.dumps({"forbidden_domains": ["example.invalid"],
                                                         "exclude": ["privacy-scan.json"]}))
    for name, text in files.items():
        (tree / name).write_text(text)
    return tree


def test_default_scanners_run_the_public_and_the_closed_scanner_over_the_tree_and_the_message(tmp_path):
    scan = mirror.default_scanners(release_tree(tmp_path), python=sys.executable)
    tree = main_tree(tmp_path, {"a.txt": "ok\nhost.example.invalid\n", "b.txt": "the-closed-word\n"})
    records = [("commit snapshot", 1, "mentions the-closed-word"), ("commit snapshot", 2, "see example.invalid")]
    found = scan(tree, records)
    assert "a.txt:2: domain" in found and any(f.startswith("commit snapshot:2: domain") for f in found)
    assert "b.txt:1: private literal" in found and "<message>:1: private literal" in found
    assert not any("the-closed-word" in f for f in found)  # the closed literal itself stays out


def test_default_scanners_clean_tree_finds_nothing(tmp_path):
    scan = mirror.default_scanners(release_tree(tmp_path), python=sys.executable)
    assert scan(main_tree(tmp_path, {"a.txt": "fine\n"}), [("commit snapshot", 1, "Update to upstream x")]) == []


def test_default_scanners_without_the_closed_scanner_is_an_error(tmp_path):
    scan = mirror.default_scanners(tmp_path / "nothing", python=sys.executable)
    with pytest.raises(mirror.MirrorError, match="closed scanner"):
        scan(main_tree(tmp_path, {"a.txt": "fine\n"}), [])


def test_default_scanners_without_privacy_scan_json_is_an_error(tmp_path):
    scan = mirror.default_scanners(release_tree(tmp_path), python=sys.executable)
    tree = main_tree(tmp_path, {})
    (tree / "privacy-scan.json").unlink()
    with pytest.raises(mirror.MirrorError, match="privacy-scan.json"):
        scan(tree, [])


def test_the_snapshot_author_is_a_host_value_with_a_default():
    from kd import naming

    assert naming.HOST_DEFAULTS["mirror_author"] == "DigiPaddock <mirror@localhost>"
    assert naming._shaped("mirror_author", "DigiPaddock <5484945+someone@users.noreply.example.test>")
    assert not naming._shaped("mirror_author", "no email") and not naming._shaped("mirror_author", "x <a@b>\nz")


def _commit_odd(forge, mode, name, message):
    """A commit whose tree has a 120000 or 160000 entry (git update-index: no symlink rights needed)."""
    blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=forge.work, input="target-text\n",
                          capture_output=True, text=True, env=GIT_ENV, check=True).stdout.strip()
    git("update-index", "--add", "--cacheinfo", f"{mode},{blob},{name}", cwd=forge.work)
    git("commit", "-q", "-m", message, cwd=forge.work)
    git("push", "-q", "origin", "main", cwd=forge.work)
    return git("rev-parse", "HEAD", cwd=forge.work)


@pytest.mark.parametrize("mode", ["120000", "160000"])
def test_a_tree_with_a_symlink_or_a_submodule_is_a_privacy_block_not_published(tmp_path, forge, github, capsys, mode):
    seen = []
    head = _commit_odd(forge, mode, "docs/link", "KD-3: a link")
    assert go(tmp_path, forge, github, scanners=lambda tree, records: seen.append(1) or []) == 1
    assert published(github) == [] and saved(tmp_path)["blocked"] == head and not seen
    assert "kd-mirror: privacy: a symlink or a submodule in the tree: docs/link" in capsys.readouterr().err
    assert go(tmp_path, forge, github) == 0  # blocked once, then quiet until main moves
    assert published(github) == []


def test_a_late_tag_on_a_tree_with_a_symlink_is_not_pushed(tmp_path, forge, github, capsys):
    assert go(tmp_path, forge, github) == 0
    _commit_odd(forge, "120000", "docs/link", "KD-3: a link")
    git("update-ref", "refs/heads/main", "HEAD", cwd=forge.work)
    state = tmp_path / "state" / "state.json"
    head = git("rev-parse", "HEAD", cwd=forge.work)
    state.write_text(json.dumps({"pushed": head, "blocked": "", "blocked_at": "", "tags": []}))
    forge.tag("v1")
    assert go(tmp_path, forge, github) == 1
    assert "a symlink or a submodule in the tree: docs/link" in capsys.readouterr().err
    assert not any("v1" in line for line in git("--git-dir", str(github), "tag").split())


def test_default_scanners_read_a_binary_and_say_byte_offset(tmp_path):
    scan = mirror.default_scanners(release_tree(tmp_path), python=sys.executable)
    tree = main_tree(tmp_path, {})
    (tree / "a.bin").write_bytes(b"\0\0 the-closed-word and host.example.invalid")
    found = scan(tree, [("commit snapshot", 1, "Update to upstream x")])
    assert "a.bin:23: domain (byte offset)" in found
    assert "a.bin:3: private literal (byte offset)" in found
