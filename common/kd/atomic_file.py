"""Small journals rewritten whole: a stable lock file beside them and an atomic, fsynced replace.

The lock lives in ``<path>.lock``, never on the data file itself: ``os.replace`` swaps the data
inode, so a lock held on the old one would no longer exclude the next writer. A crash at any
point leaves either the old or the new complete file, never an empty or partial one.
"""

from contextlib import contextmanager
import os
import tempfile

try:
    import fcntl
except ImportError:  # Windows: tests only; callers needing the lock check it
    fcntl = None

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


@contextmanager
def locked(path: str, mode: int = 0o600):
    """Exclusive lock on ``path + ".lock"`` for the whole read-modify-replace."""
    fd = os.open(path + ".lock", os.O_RDWR | os.O_CREAT | _NOFOLLOW, mode)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def read(path: str, limit: int) -> bytes:
    """Up to ``limit + 1`` bytes of a regular file (no symlink); b"" when it does not exist."""
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except FileNotFoundError:
        return b""
    with os.fdopen(fd, "rb") as handle:
        return handle.read(limit + 1)


def replace(path: str, data: bytes, mode: int) -> None:
    """Write ``data`` to a temp file in the same directory, fsync, rename over ``path``.

    The mode goes on the descriptor ``mkstemp`` opened, never through the temp file's name: in a directory another
    user owns (root's release request in the forge user's directory) that name can be swapped for a link to a file
    of root's between the write and the rename, and a chmod by name would follow it."""
    directory = os.path.dirname(path) or "."
    fd, temp = tempfile.mkstemp(
        prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if not hasattr(os, "fchmod"):  # Windows (tests): no other user owns the directory there
            os.chmod(temp, mode)
        os.replace(temp, path)
        if os.name != "nt":
            dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
