"""Read ACLs for the host reader on a platform tree, run by root from host_read_setup.sh install.

Some trees belong to an agent's user (admin-proposals — the sysadmin's), so a path-based recursive setfacl by
root could be steered by a link swapped in between its check and its write. Here every entry is opened relative
to its parent's descriptor without following a link (os.fwalk, O_NOFOLLOW), and setfacl gets that descriptor as
/proc/self/fd/N: a link is skipped, never followed. Directories also get the default ACL, so new files inherit it.

    python3 -I acl_walk.py <top> <user>    exit 0 done, 1 refused or failed
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys

FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)


def _set(fd: int, user: str, is_dir: bool, run) -> bool:
    path = f"/proc/self/fd/{fd}"
    specs = [["-m", f"u:{user}:rX"]] + ([["-d", "-m", f"u:{user}:rX"]] if is_dir else [])
    return all(run(["setfacl", *spec, path], pass_fds=(fd,), check=False).returncode == 0 for spec in specs)


def _entry(name: str, dir_fd: int, user: str, run) -> bool:
    try:
        fd = os.open(name, FLAGS, dir_fd=dir_fd)
    except OSError:  # a link (ELOOP), gone, or not openable: nothing to grant
        return True
    try:
        mode = os.fstat(fd).st_mode
        if stat.S_ISDIR(mode) or stat.S_ISREG(mode):
            return _set(fd, user, stat.S_ISDIR(mode), run)
        return True
    finally:
        os.close(fd)


def grant(top: str, user: str, run=subprocess.run) -> int:
    try:
        top_fd = os.open(top, FLAGS | getattr(os, "O_DIRECTORY", 0))
    except OSError as exc:
        print(f"acl_walk: {top}: {exc.strerror}", file=sys.stderr)
        return 1
    ok = True
    try:
        ok = _set(top_fd, user, True, run)
        for _, dirnames, filenames, dir_fd in os.fwalk(".", dir_fd=top_fd, follow_symlinks=False):
            for name in dirnames + filenames:
                ok = _entry(name, dir_fd, user, run) and ok
    finally:
        os.close(top_fd)
    if not ok:
        print(f"acl_walk: {top}: setfacl failed", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: acl_walk.py <top> <user>", file=sys.stderr)
        sys.exit(2)
    sys.exit(grant(sys.argv[1], sys.argv[2]))
