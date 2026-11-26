"""Files and directories root hands to a service user, fixed through a descriptor and never by path.

The installers run as root, but the directory that holds the file belongs to a less trusted service user, who can
swap any entry for a symlink. ``chown``/``chmod``/``[ -f ]`` by path would follow the link and hand over a root file.
Here the entry is opened with ``O_NOFOLLOW`` (a new one with ``O_EXCL`` too), must be what it should be (a regular
file with one link, or a directory), and only then owner and mode are set through the descriptor.

Command line (run as ``python3 -I owned_file.py``): ``file|dir <path> <user> <group> <mode-octal>``. Exit 1 with the
reason on stderr for anything that is not as expected; the entry is then left untouched.
"""

import os
import stat
import sys

_COMMON = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(
    os, "O_NONBLOCK", 0
)  # a FIFO planted in place must not block the open


class Refused(Exception):
    """The entry is not what the installer may touch."""


def _ids(user, group):
    if str(user).isdigit() and str(group).isdigit():
        return int(user), int(group)
    import grp  # noqa: PLC0415 -- Unix only
    import pwd  # noqa: PLC0415

    uid = int(user) if str(user).isdigit() else pwd.getpwnam(user).pw_uid
    gid = int(group) if str(group).isdigit() else grp.getgrnam(group).gr_gid
    return uid, gid


def _open_existing(path, flags):
    try:
        return os.open(path, flags | _COMMON | _NONBLOCK)
    except OSError as exc:
        raise Refused(
            f"{path}: {exc.strerror} (a symlink?): left untouched, check it by hand"
        ) from exc


def _fix(fd, path, uid, gid, mode, kind, links):
    st = os.fstat(fd)
    if kind == "file" and not stat.S_ISREG(st.st_mode):
        raise Refused(f"{path}: not a regular file: left untouched, check it by hand")
    if kind == "dir" and not stat.S_ISDIR(st.st_mode):
        raise Refused(f"{path}: not a directory: left untouched, check it by hand")
    if links and st.st_nlink != 1:
        raise Refused(
            f"{path}: {st.st_nlink} hard links: left untouched, check it by hand"
        )
    os.fchown(fd, uid, gid)
    os.fchmod(fd, mode)


def ensure_file(path, user, group, mode):
    """A regular file with one link owned by ``user:group`` with ``mode``; created empty when missing."""
    uid, gid = _ids(user, group)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _COMMON, 0o600)
    except FileExistsError:
        fd = _open_existing(path, os.O_RDONLY)
    except OSError as exc:
        raise Refused(f"{path}: {exc.strerror}") from exc
    try:
        _fix(fd, path, uid, gid, mode, "file", links=True)
    finally:
        os.close(fd)


def ensure_dir(path, user, group, mode):
    """A directory owned by ``user:group`` with ``mode`` (setgid and the like included); created when missing."""
    uid, gid = _ids(user, group)
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise Refused(f"{path}: {exc.strerror}") from exc
    fd = _open_existing(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        _fix(fd, path, uid, gid, mode, "dir", links=False)
    finally:
        os.close(fd)


def main(argv):
    if len(argv) != 5 or argv[0] not in ("file", "dir"):
        sys.exit("usage: owned_file.py file|dir <path> <user> <group> <mode-octal>")
    kind, path, user, group, mode = argv
    try:
        (ensure_file if kind == "file" else ensure_dir)(path, user, group, int(mode, 8))
    except (Refused, KeyError, ValueError) as exc:
        sys.exit(f"owned_file: {exc}")


if __name__ == "__main__":
    main(sys.argv[1:])
