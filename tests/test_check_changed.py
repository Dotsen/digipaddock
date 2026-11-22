"""Regression tests for the incremental local quality gate."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_changed.py"
SPEC = importlib.util.spec_from_file_location("check_changed", SCRIPT)
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    )


@pytest.fixture
def repository(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Quality Gate Test")
    git(tmp_path, "config", "user.email", "quality@example.invalid")
    (tmp_path / "legacy.py").write_text("import os\n", encoding="utf-8")
    (tmp_path / "deleted.py").write_text("value = 1\n", encoding="utf-8")
    git(tmp_path, "add", "--", "legacy.py", "deleted.py")
    git(tmp_path, "commit", "-qm", "test: baseline")
    return tmp_path


@pytest.mark.parametrize(
    ("name", "source", "expected"),
    [
        ("module.py", "", "python"),
        ("tool", "#!/usr/bin/env python3\n", "python"),
        ("setup.sh", "", "bash"),
        ("tool", "#!/usr/bin/env bash\n", "bash"),
        ("notes.md", "# title\n", None),
    ],
)
def test_kind(name, source, expected):
    assert CHECK.kind(Path(name), source) == expected


def test_git_paths_include_untracked_and_exclude_deleted(repository):
    (repository / "legacy.py").write_text("import os\nvalue = 1\n", encoding="utf-8")
    (repository / "new.py").write_text("value = 2\n", encoding="utf-8")
    (repository / "deleted.py").unlink()
    paths = CHECK.git_paths("HEAD", repository)
    assert {path.name for path in paths} == {"legacy.py", "new.py"}


def test_new_diagnostics_ignore_shifted_legacy_line():
    old_source = "import os\n"
    new_source = "\nimport os\n"
    old = [{"code": "F401", "message": "unused", "location": {"row": 1}}]
    current = [{"code": "F401", "message": "unused", "location": {"row": 2}}]
    assert CHECK.new_diagnostics(current, old, new_source, old_source) == []


def test_new_diagnostics_detect_another_instance():
    source = "import os\nimport sys\n"
    old = [{"code": "F401", "message": "unused", "location": {"row": 1}}]
    current = [*old, {"code": "F401", "message": "unused", "location": {"row": 2}}]
    assert CHECK.new_diagnostics(current, old, source, "import os\n") == current[1:]


def test_new_diagnostics_detect_changed_legacy_line():
    old = [{"code": "F401", "message": "unused", "location": {"row": 1}}]
    current = [{"code": "F401", "message": "unused", "location": {"row": 1}}]
    assert CHECK.new_diagnostics(current, old, "import sys\n", "import os\n") == current


@pytest.mark.skipif(importlib.util.find_spec("ruff") is None, reason="Ruff not installed (requirements-dev.txt)")
def test_existing_ruff_finding_is_grandfathered_but_new_finding_fails(
    repository, capsys
):
    (repository / "legacy.py").write_text(
        "import os\n\ndef answer():\n    return 42\n", encoding="utf-8"
    )
    assert CHECK.check("HEAD", repository) == 0
    (repository / "legacy.py").write_text(
        "import os\nimport sys\n\ndef answer():\n    return 42\n", encoding="utf-8"
    )
    assert CHECK.check("HEAD", repository) == 1
    assert "F401" in capsys.readouterr().err


@pytest.mark.skipif(importlib.util.find_spec("ruff") is None, reason="Ruff not installed (requirements-dev.txt)")
def test_moved_legacy_file_keeps_its_baseline(repository, capsys):
    # A rename is not a new file: its findings stay grandfathered and its format is not checked.
    (repository / "legacy.py").write_text(
        "import os\ndef answer( x ):\n return 42\n", encoding="utf-8"
    )
    git(repository, "commit", "-qam", "test: unformatted legacy")
    git(repository, "mv", "legacy.py", "moved.py")
    (repository / "moved.py").write_text(
        "import os\ndef answer( x ):\n return 43\n", encoding="utf-8"
    )
    assert CHECK.check("HEAD", repository) == 0, capsys.readouterr().err
    (repository / "moved.py").write_text(
        "import os\nimport sys\ndef answer( x ):\n return 43\n", encoding="utf-8"
    )
    assert CHECK.check("HEAD", repository) == 1
    assert "F401" in capsys.readouterr().err


@pytest.mark.skipif(importlib.util.find_spec("ruff") is None, reason="Ruff not installed (requirements-dev.txt)")
def test_new_python_requires_formatting(repository, capsys):
    (repository / "new.py").write_text(
        "def answer( x ):\n return 42\n", encoding="utf-8"
    )
    assert CHECK.check("HEAD", repository) == 1
    assert "Ruff format check failed" in capsys.readouterr().err


def test_binary_asset_is_not_source(repository, capsys):
    (repository / "icon.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xff")
    assert CHECK.check("HEAD", repository) == 0, capsys.readouterr().err


def test_undecodable_python_still_fails(repository, capsys):
    (repository / "broken.py").write_bytes(b"value = '\xff'\n")
    assert CHECK.check("HEAD", repository) == 1
    assert "cannot read UTF-8 source" in capsys.readouterr().err


def test_new_bash_requires_valid_syntax(repository, capsys):
    if CHECK.bash_executable() is None:
        pytest.skip("Bash unavailable")
    (repository / "new.sh").write_text(
        "#!/usr/bin/env bash\nif true; then\n", encoding="utf-8"
    )
    assert CHECK.check("HEAD", repository) == 1
    assert "bash -n failed" in capsys.readouterr().err
