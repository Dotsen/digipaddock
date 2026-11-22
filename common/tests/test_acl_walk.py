"""acl_walk: root grants the host reader read ACLs on a tree without following links."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

from kd import acl_walk

LINUX = pytest.mark.skipif(sys.platform == "win32" or not hasattr(os, "fwalk") or not shutil.which("setfacl"),
                           reason="Linux ACLs")


def _acl(path) -> str:
    return subprocess.run(["getfacl", "-cp", str(path)], capture_output=True, text=True, check=True).stdout


@LINUX
def test_the_tree_gets_the_acl_and_a_link_out_is_left_alone(tmp_path) -> None:
    top = tmp_path / "admin-proposals"
    (top / "sub").mkdir(parents=True)
    (top / "KD-1.json").write_text("{}")
    (top / "sub" / "x.json").write_text("{}")
    secret = tmp_path / "push.env"
    secret.write_text("KEY=1\n")
    (top / "link").symlink_to(secret)
    (top / "sub" / "dirlink").symlink_to(tmp_path)

    assert acl_walk.grant(str(top), "nobody") == 0

    for path in (top, top / "sub", top / "KD-1.json", top / "sub" / "x.json"):
        assert "user:nobody:r" in _acl(path), path
    assert "default:user:nobody:r-x" in _acl(top) and "default:user:nobody:r-x" in _acl(top / "sub")
    assert "nobody" not in _acl(secret) and "nobody" not in _acl(tmp_path)


@LINUX
def test_a_top_that_is_a_link_is_refused(tmp_path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "top").symlink_to(real)
    assert acl_walk.grant(str(tmp_path / "top"), "nobody") == 1
    assert "nobody" not in _acl(real)


def test_setfacl_gets_the_opened_descriptor_not_a_path(monkeypatch, tmp_path) -> None:
    if not hasattr(os, "fwalk"):
        pytest.skip("os.fwalk is POSIX only")
    (tmp_path / "f").write_text("x")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    assert acl_walk.grant(str(tmp_path), "nobody", run=run) == 0
    for argv, kwargs in calls:
        assert argv[0] == "setfacl" and argv[-1].startswith("/proc/self/fd/")
        assert kwargs["pass_fds"] == (int(argv[-1].rsplit("/", 1)[1]),)
