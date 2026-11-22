import io
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "common/install/release_switch.sh"
BASH = shutil.which("bash")
# real symlinks, flock and permissions exist only on POSIX: on a PC (Windows, Git Bash copies instead of ln -s) — a skip,
# the run is on example-stage
POSIX = pytest.mark.skipif(BASH is None or os.name == "nt", reason="нужен POSIX bash; прогон на example-stage, Task 11 Step 1")


def _tar(files: dict, links: dict = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            t.addfile(info)
    return buf.getvalue()


def _env(tmp_path):
    base, link = tmp_path / "rel", tmp_path / "opt"
    return base, link, {"KD_RELEASE_BASE": str(base), "KD_RELEASE_LINK": str(link), "PATH": "/usr/bin:/bin"}


def _releases(base):
    return [p for p in base.iterdir() if p.is_dir()]  # .lock is not a release


@POSIX
def test_switch_creates_release_sets_modes_and_keeps_three(tmp_path):
    base, link, env = _env(tmp_path)
    for i in range(4):
        rev = f"{i:07d}"
        tar = _tar({"common/bin/kd-x": b"#!/bin/sh\n", "common/install/a.sh": b"", "README": b"r"})
        p = subprocess.run([BASH, str(SCRIPT), rev], input=tar, env=env, capture_output=True)
        assert p.returncode == 0, p.stderr
        assert f"REVISION {rev}" in p.stdout.decode()
        assert link.is_symlink() and (link / "REVISION").read_text().strip() == rev
        assert (link / "common/bin/kd-x").stat().st_mode & 0o111
    assert len(_releases(base)) == 3


@POSIX
def test_broken_tar_leaves_no_release_and_old_link(tmp_path):
    base, link, env = _env(tmp_path)
    subprocess.run([BASH, str(SCRIPT), "aaaaaaa"], input=_tar({"REV": b"1"}), env=env, capture_output=True, check=True)
    before = link.resolve()
    p = subprocess.run([BASH, str(SCRIPT), "bbbbbbb"], input=b"not a tar", env=env, capture_output=True)
    assert p.returncode != 0 and link.resolve() == before and len(_releases(base)) == 1


@POSIX
def test_symlink_in_archive_is_refused(tmp_path):
    base, link, env = _env(tmp_path)
    subprocess.run([BASH, str(SCRIPT), "aaaaaaa"], input=_tar({"README": b"r"}), env=env, capture_output=True, check=True)
    before = link.resolve()
    tar = _tar({"README": b"r"}, links={"common/bin/x": "/home/digi/.ssh"})
    p = subprocess.run([BASH, str(SCRIPT), "bbbbbbb"], input=tar, env=env, capture_output=True)
    assert p.returncode == 3 and "symlink" in p.stderr.decode()
    assert link.resolve() == before and len(_releases(base)) == 1


def test_script_contract():
    text = SCRIPT.read_text(encoding="utf-8")
    body = text.split("set -euo pipefail", 1)[1]
    assert '^[0-9a-f]{7,40}$' in text and "trap 'rm -rf" in text and "mv -T" in text and "&&" not in body
    assert 'exec 9>"$base/.lock"' in text and "flock -w 300 9" in text
    assert "--no-same-owner --no-same-permissions" in text and "chown -hR root:root" in text
    assert 'find "$new" -type l | head -1' in text and "exit 3" in text
    assert "warning: old releases not removed" in text


# --- rollback to a kept release ----------------------------------------------------------------------
def _switch(env, rev, files=None):
    return subprocess.run([BASH, str(SCRIPT), rev], input=_tar(files or {"README": rev.encode()}), env=env,
                          capture_output=True)


@POSIX
def test_switch_writes_previous(tmp_path):
    base, link, env = _env(tmp_path)
    assert _switch(env, "aaaaaaa").returncode == 0
    assert not (base / "PREVIOUS").exists()  # nothing was current before the first release
    first = link.resolve()
    assert _switch(env, "bbbbbbb").returncode == 0
    assert (base / "PREVIOUS").read_text().strip() == str(first)


@POSIX
def test_rollback_switches_to_existing_dir_and_keeps_releases(tmp_path):
    base, link, env = _env(tmp_path)
    _switch(env, "aaaaaaa")
    first = link.resolve()
    _switch(env, "bbbbbbb")
    second = link.resolve()
    p = subprocess.run([BASH, str(SCRIPT), "--rollback", str(first)], env=env, capture_output=True)
    assert p.returncode == 0, p.stderr
    assert "REVISION aaaaaaa (rollback)" in p.stdout.decode() and link.resolve() == first
    assert (base / "PREVIOUS").read_text().strip() == str(second)
    assert sorted(_releases(base)) == sorted([first, second])  # nothing removed


@POSIX
@pytest.mark.parametrize("where", ["outside", "no_revision", "missing", "base"])
def test_rollback_refuses_dir_outside_base_or_without_revision(tmp_path, where):
    base, link, env = _env(tmp_path)
    _switch(env, "aaaaaaa")
    before = link.resolve()
    target = {"outside": tmp_path / "elsewhere", "no_revision": base / "empty", "missing": base / "nope",
              "base": base}[where]
    if where in ("outside", "no_revision"):
        target.mkdir()
    if where == "outside":
        (target / "REVISION").write_text("ccccccc\n")
    p = subprocess.run([BASH, str(SCRIPT), "--rollback", str(target)], env=env, capture_output=True)
    assert p.returncode == 5 and link.resolve() == before


@POSIX
def test_rollback_needs_one_dir(tmp_path):
    _, _, env = _env(tmp_path)
    assert subprocess.run([BASH, str(SCRIPT), "--rollback"], env=env, capture_output=True).returncode == 2


def test_rollback_contract():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "--rollback" in text and 'realpath -e' in text and '"$base/PREVIOUS"' in text
    assert "(rollback) on" in text and "exit 5" in text


# --- the session host's release is never pruned under it ------------------------------------------
@POSIX
def test_the_release_the_host_pinned_is_kept_past_three(tmp_path):
    base, link, env = _env(tmp_path)
    assert _switch(env, "aaaaaaa").returncode == 0
    pinned = link.resolve()
    (base / "HOST").write_text(f"{pinned}\n")
    for rev in ("bbbbbbb", "ccccccc", "ddddddd", "eeeeeee"):
        assert _switch(env, rev).returncode == 0
    assert pinned.is_dir() and len(_releases(base)) == 4  # the three newest and the host's


def test_script_keeps_the_pinned_release():
    text = SCRIPT.read_text(encoding="utf-8")
    assert '"$base/HOST"' in text and 'tail -n +4' in text
