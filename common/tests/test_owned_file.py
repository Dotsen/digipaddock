"""owned_file: root fixes a service user's file through a descriptor; a planted link or odd file is refused."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from kd import owned_file

HELPER = Path(owned_file.__file__)
POSIX = pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "mkfifo"),
    reason="links and FIFOs: Linux",
)
UID = os.getuid() if hasattr(os, "getuid") else 0
GID = os.getgid() if hasattr(os, "getgid") else 0


@pytest.fixture
def chowned(monkeypatch):
    """fchown/fchmod recorded, not done: the suite does not run as root (and Windows has neither)."""
    calls = []
    monkeypatch.setattr(
        os,
        "fchown",
        lambda fd, uid, gid: calls.append(("chown", uid, gid)),
        raising=False,
    )
    monkeypatch.setattr(
        os, "fchmod", lambda fd, mode: calls.append(("chmod", mode)), raising=False
    )
    return calls


def test_a_missing_journal_is_created_and_a_regular_one_is_fixed(tmp_path, chowned):
    new = tmp_path / "new.jsonl"
    owned_file.ensure_file(str(new), UID, GID, 0o640)
    assert new.is_file() and chowned == [("chown", UID, GID), ("chmod", 0o640)]
    old = tmp_path / "old.jsonl"
    old.write_text("kept\n")
    chowned.clear()
    owned_file.ensure_file(str(old), UID, GID, 0o640)
    assert old.read_text() == "kept\n" and chowned == [
        ("chown", UID, GID),
        ("chmod", 0o640),
    ]


def test_a_directory_in_place_of_the_journal_is_refused(tmp_path, chowned):
    (tmp_path / "j").mkdir()
    with pytest.raises(owned_file.Refused):
        owned_file.ensure_file(str(tmp_path / "j"), UID, GID, 0o640)
    assert chowned == []


@POSIX
def test_a_symlink_in_place_of_the_journal_is_refused_and_its_target_untouched(
    tmp_path, chowned
):
    target = tmp_path / "root-file"
    target.write_text("secret\n")
    target.chmod(0o600)
    (tmp_path / "j.jsonl").symlink_to(target)
    with pytest.raises(owned_file.Refused):
        owned_file.ensure_file(str(tmp_path / "j.jsonl"), UID, GID, 0o644)
    assert (
        chowned == []
        and (target.stat().st_mode & 0o777) == 0o600
        and target.read_text() == "secret\n"
    )


@POSIX
def test_a_dangling_symlink_is_not_created_through(tmp_path, chowned):
    (tmp_path / "j.jsonl").symlink_to(tmp_path / "nowhere")
    with pytest.raises(owned_file.Refused):
        owned_file.ensure_file(str(tmp_path / "j.jsonl"), UID, GID, 0o640)
    assert not (tmp_path / "nowhere").exists() and chowned == []


@POSIX
def test_a_fifo_and_a_hard_linked_file_are_refused(tmp_path, chowned):
    os.mkfifo(tmp_path / "fifo")
    with pytest.raises(owned_file.Refused):
        owned_file.ensure_file(str(tmp_path / "fifo"), UID, GID, 0o640)
    (tmp_path / "a").write_text("x")
    os.link(tmp_path / "a", tmp_path / "b")
    with pytest.raises(owned_file.Refused):
        owned_file.ensure_file(str(tmp_path / "b"), UID, GID, 0o640)
    assert chowned == []


@POSIX
def test_a_directory_is_made_or_fixed_but_a_link_in_its_place_is_refused(
    tmp_path, chowned
):
    owned_file.ensure_dir(str(tmp_path / "ssh"), UID, GID, 0o700)
    assert (tmp_path / "ssh").is_dir() and ("chmod", 0o700) in chowned
    chowned.clear()
    (tmp_path / "system").mkdir()
    (tmp_path / ".ssh").symlink_to(tmp_path / "system")
    with pytest.raises(owned_file.Refused):
        owned_file.ensure_dir(str(tmp_path / ".ssh"), UID, GID, 0o700)
    (tmp_path / "plain").write_text("x")
    with pytest.raises(owned_file.Refused):
        owned_file.ensure_dir(str(tmp_path / "plain"), UID, GID, 0o700)
    assert chowned == []


@POSIX
def test_the_command_line_refuses_with_a_message_and_a_non_zero_exit(tmp_path):
    target = tmp_path / "t"
    target.write_text("x")
    (tmp_path / "j").symlink_to(target)
    done = subprocess.run(
        [
            sys.executable,
            "-I",
            str(HELPER),
            "file",
            str(tmp_path / "j"),
            str(UID),
            str(GID),
            "640",
        ],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 1 and "symlink" in done.stderr
    ok = subprocess.run(
        [
            sys.executable,
            "-I",
            str(HELPER),
            "file",
            str(tmp_path / "fresh"),
            str(UID),
            str(GID),
            "640",
        ],
        capture_output=True,
        text=True,
    )
    assert ok.returncode == 0 and (tmp_path / "fresh").is_file()
    assert ((tmp_path / "fresh").stat().st_mode & 0o777) == 0o640
