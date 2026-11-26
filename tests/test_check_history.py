"""the history a push publishes is scanned before the push, the way kd-mirror scans it."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_history.py"
SPEC = importlib.util.spec_from_file_location("check_history", SCRIPT)
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)

LITERAL = "acme-node-7"  # a synthetic closed literal
PUBLIC = ".".join(
    ["91", "0", "2", "3"]
)  # a public IPv4 address, built so this file stays clean


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout


def commit(root, name, text, message):
    (root / name).write_text(text, encoding="utf-8")
    git(root, "add", "--", name)
    git(root, "commit", "-qm", message)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "History Test")
    git(root, "config", "user.email", "history@example.invalid")
    (root / "privacy-scan.json").write_text(
        json.dumps({"forbidden_domains": [], "exclude": ["tmp/**"]}), encoding="utf-8"
    )
    commit(root, "README.md", "base\n", "docs: base")
    git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    monkeypatch.delenv(CHECK.LIST_ENV, raising=False)
    monkeypatch.setattr(CHECK, "RELEASE_LIST", tmp_path / "no-release" / "list.json")
    return root


@pytest.fixture
def listing(tmp_path):
    path = tmp_path / "private_scan.json"
    path.write_text(json.dumps({"literals": [LITERAL]}), encoding="utf-8")
    return path


def run(repo, *argv):
    return CHECK.main(list(argv), root=repo)


def test_a_literal_added_and_removed_again_fails_without_printing_it(
    repo, listing, capsys
):
    commit(repo, "notes.md", f"host {LITERAL}\n", "docs: notes")
    commit(repo, "notes.md", "host example\n", "docs: notes, cleaned")
    assert run(repo, "--private-list", str(listing)) == 1
    out = capsys.readouterr()
    assert "notes.md:1: private literal" in out.err
    assert LITERAL not in out.out + out.err


def test_a_commit_message_and_the_author_are_scanned(repo, listing, capsys):
    commit(repo, "a.md", "a\n", f"docs: about {LITERAL}")
    message = git(repo, "rev-parse", "--short", "HEAD").strip()
    (repo / "b.md").write_text("b\n", encoding="utf-8")
    git(repo, "add", "--", "b.md")
    git(repo, "-c", f"user.name={LITERAL}", "commit", "-qm", "docs: b")
    author = git(repo, "rev-parse", "--short", "HEAD").strip()
    assert run(repo, "--private-list", str(listing)) == 1
    err = capsys.readouterr().err
    assert (
        f"commit {message}:3: private literal" in err
    )  # author, committer, then the subject
    assert f"commit {author}:1: private literal" in err
    assert f"commit {author}:2: private literal" in err


def test_uncommitted_and_untracked_changes_are_scanned(repo, listing, capsys):
    (repo / "README.md").write_text(f"base\n{LITERAL}\n", encoding="utf-8")
    (repo / "new.md").write_text(f"x\n{LITERAL}\n", encoding="utf-8")
    assert run(repo, "--private-list", str(listing)) == 1
    err = capsys.readouterr().err
    assert "README.md:2: private literal" in err
    assert "new.md:2: private literal" in err


def test_the_public_scanner_runs_without_a_list(repo, capsys):
    commit(repo, "a.md", f"server {PUBLIC}\n", "docs: a")
    commit(repo, "a.md", "server\n", "docs: a, cleaned")
    assert run(repo) == 1
    err = capsys.readouterr().err
    assert "a.md:1: ipv4" in err
    assert "closed scanner skipped" in err


def test_clean_history_passes_and_names_what_it_scanned(repo, listing, capsys):
    commit(repo, "a.md", "clean\n", "docs: a")
    assert run(repo, "--private-list", str(listing)) == 0
    out = capsys.readouterr().out
    assert "1 commit" in out and "closed scanner on" in out


def test_commits_already_on_main_are_not_scanned(repo, listing):
    commit(repo, "a.md", f"{LITERAL}\n", "docs: a")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    assert run(repo, "--private-list", str(listing)) == 0


def test_the_list_comes_from_the_environment(repo, listing, monkeypatch):
    monkeypatch.setenv(CHECK.LIST_ENV, str(listing))
    commit(repo, "a.md", f"{LITERAL}\n", "docs: a")
    assert run(repo) == 1


def test_the_running_release_list_is_the_fallback(repo, listing, monkeypatch):
    monkeypatch.setattr(CHECK, "RELEASE_LIST", listing)
    commit(repo, "a.md", f"{LITERAL}\n", "docs: a")
    assert run(repo) == 1


def test_an_excluded_path_is_not_public_but_the_closed_scanner_reads_it(
    repo, listing, capsys
):
    (repo / "tmp").mkdir()
    commit(repo, "tmp/x.md", f"{PUBLIC} {LITERAL}\n", "chore: scratch")
    assert run(repo, "--private-list", str(listing)) == 1
    err = capsys.readouterr().err
    assert "tmp/x.md:1: private literal" in err
    assert "ipv4" not in err


def test_no_base_is_skipped_and_said(repo, capsys):
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    assert run(repo) == 0
    assert "skipped" in capsys.readouterr().out


def test_a_broken_list_is_an_error(repo, tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text("{}", encoding="utf-8")
    assert run(repo, "--private-list", str(broken)) == 2


def test_binary_files_are_scanned_committed_uncommitted_and_untracked(repo, listing, capsys):
    blob = b"\0\1 host " + LITERAL.encode() + b"\n"
    (repo / "c.bin").write_bytes(blob)
    git(repo, "add", "--", "c.bin")
    git(repo, "commit", "-qm", "docs: c")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / "c.bin").write_bytes(blob + b"more")  # an uncommitted change of a tracked binary
    (repo / "u.bin").write_bytes(blob)  # an untracked binary
    assert run(repo, "--private-list", str(listing)) == 1
    err = capsys.readouterr().err
    assert "c.bin:" in err and "u.bin:" in err and "(byte offset)" in err
    assert LITERAL not in err
