"""the history a PR adds to the forge, by both scanners — the script devcheck's sandbox runs with -I."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "kd" / "history_scan.py"
LITERAL = "acme-node-7"  # a synthetic closed literal
# a public IPv4 address, built so this file stays clean
PUBLIC = ".".join(["91", "0", "2", "3"])


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout


def commit(root, name, text, message="docs: change"):
    (root / name).write_text(text, encoding="utf-8")
    git(root, "add", "--", name)
    git(root, "commit", "-qm", message)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "History Test")
    git(root, "config", "user.email", "history@example.invalid")
    (root / "privacy-scan.json").write_text(
        json.dumps({"forbidden_domains": [], "exclude": []}), encoding="utf-8"
    )
    commit(root, "README.md", "base\n", "docs: base")
    git(root, "update-ref", "refs/kd/base", "HEAD")
    (tmp_path / "private_scan.json").write_text(
        json.dumps({"literals": [LITERAL]}), encoding="utf-8"
    )
    return root


def scan(repo, base="refs/kd/base"):
    """As devcheck runs it: python -I, the checkout as the working directory, the candidate's config."""
    return subprocess.run(
        [
            sys.executable,
            "-I",
            str(SCRIPT),
            "--config",
            "privacy-scan.json",
            "--list",
            str(repo.parent / "private_scan.json"),
            ".",
            base,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
    )


def test_a_literal_the_pr_added_and_removed_again_is_red_without_printing_it(repo):
    commit(repo, "notes.md", f"host {LITERAL}\n")
    commit(repo, "notes.md", "host example\n", "docs: cleaned")
    p = scan(repo)
    assert p.returncode == 1, p.stderr
    assert "notes.md:1: private literal" in p.stdout
    assert "2 commit(s) after refs/kd/base: 1 finding(s)" in p.stdout
    assert LITERAL not in p.stdout + p.stderr


def test_messages_and_the_public_scanner(repo):
    commit(repo, "a.md", f"server {PUBLIC}\n", f"docs: about {LITERAL}")
    sha = git(repo, "rev-parse", "--short", "HEAD").strip()
    p = scan(repo)
    assert p.returncode == 1
    assert f"commit {sha}:3: private literal" in p.stdout
    assert "a.md:1: ipv4 (allow " in p.stdout


def test_what_the_base_holds_is_not_the_pr(repo):
    commit(repo, "a.md", f"{LITERAL}\n")
    git(repo, "update-ref", "refs/kd/base", "HEAD")
    commit(repo, "b.md", "clean\n")
    p = scan(repo)
    assert p.returncode == 0 and "1 commit(s) after refs/kd/base: clean" in p.stdout


def test_no_base_or_no_list_is_an_error(repo):
    assert scan(repo, base="refs/kd/missing").returncode == 2
    (repo.parent / "private_scan.json").write_text("{}", encoding="utf-8")
    p = scan(repo)
    assert p.returncode == 2 and "literals" in p.stderr


def test_an_option_as_the_base_is_refused(repo):
    assert scan(repo, base="--all").returncode == 2
