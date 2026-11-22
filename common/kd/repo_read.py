"""kd-repo — read-only git views of the host's repository checkouts (capability repo.read).

The checkouts live in CHECKOUTS/<name>, owned by the repo service user and refreshed by
kd-repo-sync; the agent reads files with its own tools and asks git history through this
wrapper, which runs as the agent inside its sandbox. Only the listed subcommands and options
pass: no configuration overrides, no external programs, no output files, no other git
directory, no path outside the checkout (two outside paths would make ``git diff`` imply
``--no-index`` and print any readable file). Everything after ``--`` is a pathspec. The sandbox
denies writes anyway; this is the second wall.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Sequence

CHECKOUTS = "/srv/digipaddock-repos"
NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")  # the keys of repos.json (kd-release)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
USAGE = (
    "usage: kd-repo list | kd-repo log|show|diff|blame <name> [options] [revisions] [-- paths]\n"
    "  read-only git of the checkouts under /srv/digipaddock-repos; options from the allowlist only"
)

# Flags that pass alone (or as --flag=value for the valued ones).
_COMMON = {"--stat", "--name-only", "--name-status", "--format", "--pretty", "--date", "--abbrev-commit",
           "-p", "--patch", "--no-color"}
OPTIONS: dict[str, frozenset[str]] = {
    "log": frozenset(_COMMON | {"--oneline", "--no-merges", "--first-parent", "--follow", "--all", "--reverse",
                                "--date-order", "--topo-order", "-n", "--max-count", "--since", "--until",
                                "--after", "--before", "--author", "--grep", "--skip", "-S", "-G", "--decorate",
                                "--shortstat", "--numstat"}),
    "show": frozenset(_COMMON | {"--no-patch", "-s", "--shortstat", "--numstat"}),
    "diff": frozenset(_COMMON | {"-w", "--ignore-all-space", "-b", "--ignore-space-change", "-U", "--unified",
                                 "-M", "--find-renames", "--no-renames", "--diff-filter", "--shortstat", "--numstat"}),
    "blame": frozenset({"-L", "-w", "--date", "-M", "-C", "--show-stats", "-s", "-e", "-l", "-t"}),
}
# Options whose value may follow as the next token (``-n 20``, ``-L 10,20``, ``--since 2026-10-01``).
VALUED = frozenset({"-n", "--max-count", "--since", "--until", "--after", "--before", "--author", "--grep",
                    "--format", "--pretty", "--date", "--skip", "-S", "-G", "-U", "--unified", "--diff-filter",
                    "-L", "-M", "-C"})
_SHORT_WITH_VALUE = frozenset({"-n", "-S", "-G", "-U", "-L", "-M", "-C"})  # ``-n20``, ``-L10,20``
ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_PAGER": "cat",
    "LANG": "C.UTF-8",
}


class Refused(Exception):
    pass


@dataclass(frozen=True)
class Request:
    subcommand: str
    name: str
    args: tuple[str, ...]


def _clean(token: object, what: str) -> str:
    if not isinstance(token, str) or _CONTROL.search(token):
        raise Refused(f"{what}: control characters")
    return token


def _positional(token: str, what: str) -> str:
    """A revision, range or pathspec: relative, inside the work tree (git normalizes textually, so
    a ``..`` component is the only way out), never empty."""
    if not token or token[0] in "/~" or ".." in token.split("/"):
        raise Refused(f"{what}: {token!r} is not a revision or a path inside the checkout")
    return token


def _split(token: str) -> tuple[str, str | None]:
    """``--flag=value`` / ``-n20`` -> (flag, value); a bare flag -> (flag, None)."""
    if token.startswith("--"):
        flag, sep, value = token.partition("=")
        return flag, (value if sep else None)
    if len(token) > 2 and token[:2] in _SHORT_WITH_VALUE:
        return token[:2], token[2:]
    return token, None


def plan(argv: Sequence[str]) -> Request:
    argv = [_clean(a, "argument") for a in argv]
    if argv == ["list"]:
        return Request("list", "", ())
    if len(argv) < 2 or argv[0] not in OPTIONS:
        raise Refused(USAGE)
    subcommand, name, rest = argv[0], argv[1], argv[2:]
    if not NAME_RE.fullmatch(name):
        raise Refused(f"invalid checkout name {name!r}")
    allowed = OPTIONS[subcommand]
    out: list[str] = []
    i = 0
    while i < len(rest):
        token = rest[i]
        if token == "--":
            paths = rest[i + 1:]
            if "--" in paths:
                raise Refused("one -- separator")
            out += [token, *(_positional(p, subcommand) for p in paths)]
            break
        if token.startswith("-"):
            flag, value = _split(token)
            if flag not in allowed:
                raise Refused(f"{subcommand}: option {flag} is not allowed")
            out.append(token)
            if value is None and flag in VALUED:
                if i + 1 >= len(rest) or rest[i + 1].startswith("-"):
                    raise Refused(f"{subcommand}: {flag} needs a value")
                out.append(rest[i + 1])
                i += 1
        else:
            out.append(_positional(token, subcommand))  # a revision or a path: never starts with -
        i += 1
    return Request(subcommand, name, tuple(out))


def checkout_dir(name: str, root: str = CHECKOUTS) -> str:
    """The checkout of ``name``: a real directory holding a real .git, never a link."""
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise Refused(f"invalid checkout name {name!r}")
    path = os.path.join(root, name)
    try:
        info, git = os.lstat(path), os.lstat(os.path.join(path, ".git"))
    except OSError:
        raise Refused(f"no checkout {name}") from None
    if not (stat.S_ISDIR(info.st_mode) and stat.S_ISDIR(git.st_mode)):
        raise Refused(f"no checkout {name}")
    return path


def git_argv(request: Request) -> list[str]:
    return ["git", "--no-pager", request.subcommand, *request.args]


def _env(checkout: str) -> dict[str, str]:
    """The checkouts belong to the repo user: git refuses another user's repository ("dubious
    ownership") unless safe.directory names it in protected config. Command-scope config is
    protected, and it is the only scope this wrapper leaves open; the exact path, never ``*``."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(ENV)
    env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": checkout})
    return env


def list_checkouts(root: str = CHECKOUTS, run: Callable = subprocess.run) -> list[str]:
    lines = []
    for name in sorted(os.listdir(root)):
        try:
            path = checkout_dir(name, root)
        except Refused:
            continue
        head = run(["git", "--no-pager", "log", "-1", "--format=%h %cs %s"], cwd=path, env=_env(path),
                   capture_output=True, text=True)
        summary = head.stdout.strip() if head.returncode == 0 else "(no commits)"
        lines.append(f"{name}  {summary}")
    return lines


def main(argv: Sequence[str] | None = None, *, root: str = CHECKOUTS, run: Callable = subprocess.run) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        request = plan(argv)
        if request.subcommand == "list":
            for line in list_checkouts(root, run):
                print(line)
            return 0
        cwd = checkout_dir(request.name, root)
    except Refused as exc:
        print(f"kd-repo: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"kd-repo: {exc.__class__.__name__}", file=sys.stderr)
        return 2
    return run(git_argv(request), cwd=cwd, env=_env(cwd)).returncode
