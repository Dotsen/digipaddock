"""the model's secret reaches Claude Code through a file descriptor, never through its environment.

Whatever is in a process's initial environment stays readable in ``/proc/<pid>/environ`` for its whole life — by
the agent's own Read tool too, which no rule can keep out of ``/proc`` without breaking the sandbox. So
the last step before ``claude`` takes each secret it can read from a descriptor out of the environment, writes it
into a pipe, closes the pipe's write end and names the read end in the variable Claude Code reads: claude reads the
pipe once at start and closes it (the probe of claude 2.1.289 on the card: login, a turn after idle and ``--resume``
work; the descriptor is gone afterwards, nothing hangs).

What goes by a descriptor (``FD_VARIABLES``): the host's token (``CLAUDE_CODE_OAUTH_TOKEN``) by its own, and a
provider's key by the API key's — ``ANTHROPIC_API_KEY`` of the ``anthropic`` kind, and ``ANTHROPIC_AUTH_TOKEN``
of the others too: Claude Code has no descriptor for that one, and the owner's probe of 07.11.2026 (Fireworks) showed
the key works as the API key's. A provider's key moved there takes the explicitly empty variables of that descriptor
with it (OpenRouter's ``ANTHROPIC_API_KEY=""``); without a key they stay as they are. A variable that is not set gets
no descriptor at all; two keys for one descriptor refuse the start.

``python3 -I secret_fd.py <program> [args…]`` moves them and execs the program (agent-launch, the session host's
probe); the root worker runner calls ``move`` itself and passes the descriptors to its child.

The program is named by its absolute path and must be root's (``root_program``): a program its user could replace
would get the secret it is handed — the reason Claude Code is installed under ``/opt``, never in the home (KD-9474).
"""

import os
import stat
import sys

FD_VARIABLES = {
    "CLAUDE_CODE_OAUTH_TOKEN": "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
    "ANTHROPIC_API_KEY": "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
    "ANTHROPIC_AUTH_TOKEN": "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
}
# a page: even a pipe a user over its pipe-buffer limit gets holds it; the write end is non-blocking anyway, so a full
# pipe is an error here, never a wait for a reader that only comes after exec
MAX_SECRET = 4096


def pipe_of(value):
    """The read end of a pipe that holds ``value`` and whose write end is already closed (not inheritable)."""
    data = value.encode("utf-8")
    if len(data) > MAX_SECRET:
        raise ValueError("the secret is longer than a descriptor takes")
    read, write = os.pipe()
    try:
        os.set_blocking(write, False)
        view = memoryview(data)
        while view:
            view = view[os.write(write, view) :]
    except BaseException:
        os.close(read)
        raise
    finally:
        os.close(write)
    return read


def move(env):
    """``(env, fds)``: ``env`` without the secrets a descriptor can carry, each replaced by its descriptor variable,
    and the read ends to pass to the child (the caller closes them after the start). A descriptor variable already in
    ``env`` is dropped: claude must never read a descriptor this step did not open."""
    out = dict(env)
    for fd_name in FD_VARIABLES.values():
        out.pop(fd_name, None)
    chosen = {}
    for name, fd_name in FD_VARIABLES.items():
        if out.get(name):
            if fd_name in chosen:
                raise ValueError("two secrets for one descriptor")
            chosen[fd_name] = name
    fds = []
    try:
        for fd_name, name in chosen.items():
            fd = pipe_of(out[name])
            fds.append(fd)
            for other, target in FD_VARIABLES.items():
                if target == fd_name:
                    out.pop(
                        other, None
                    )  # the secret, and an empty variable of the same descriptor
            out[fd_name] = str(fd)
    except BaseException:
        for fd in fds:
            os.close(fd)
        raise
    return out, fds


def root_program(path, lstat=os.lstat):
    """Why ``path`` may not be handed a secret, or None: a plain absolute path (no ``.``, ``..`` or empty part,
    so no PATH lookup either), with no symlink anywhere on it, whose every directory and the file itself root owns
    and only root may write (group or other write — a POSIX ACL's too — refuses), the file a regular executable."""
    if not isinstance(path, str) or not path.startswith("/") or path == "/" \
            or any(part in ("", ".", "..") for part in path.split("/")[1:]):
        return f"{path!r} is not a plain absolute path"
    parts = path.split("/")[1:]
    chain = ["/"] + ["/" + "/".join(parts[: i + 1]) for i in range(len(parts))]
    info = None
    for each in chain:
        try:
            info = lstat(each)
        except FileNotFoundError:
            return f"{each} is missing"
        except OSError as exc:
            return f"{each}: {exc.strerror or exc.__class__.__name__}"
        if stat.S_ISLNK(info.st_mode):
            return f"{each} is a symlink"
        if each != path and not stat.S_ISDIR(info.st_mode):
            return f"{each} is not a directory"
        if each == path and not stat.S_ISREG(info.st_mode):
            return f"{each} is not a regular file"
        if info.st_uid != 0:
            return f"{each} is not root's"
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return f"{each} is writable by others than root"
    if not info.st_mode & stat.S_IXUSR:
        return f"{path} is not executable"
    return None


def main(argv):
    if not argv:
        print("usage: secret_fd.py <program> [args…]", file=sys.stderr)
        return 2
    why = root_program(argv[0])
    if why:
        print(f"secret_fd: {argv[0]!r} refused: {why} — a secret goes only to a program root alone may change",
              file=sys.stderr)
        return 127 if why.endswith(" is missing") else 126
    try:
        env, fds = move(os.environ)
    except (OSError, ValueError) as exc:
        print(
            f"secret_fd: the secret could not be passed by a descriptor ({exc.__class__.__name__})",
            file=sys.stderr,
        )
        return 1
    for fd in fds:
        os.set_inheritable(fd, True)
    try:
        os.execve(argv[0], argv, env)
    except OSError as exc:
        print(f"secret_fd: {argv[0]}: {exc.strerror}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
