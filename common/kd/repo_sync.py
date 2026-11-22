"""kd-repo-sync — refresh the read-only checkouts of repo.read.

Runs under the repo service user (digipaddock-repo-sync.service, every 10 minutes): for every project
of /etc/digipaddock/repos.json keep CHECKOUTS/<name> at origin/<base> — the passport's base branch, main
by default: clone once (into <name>.new, renamed when complete), then fetch, reset --hard, clean.
The copy also holds the head of every pull request (refs/pull/<n>/head): a critic of the code reads a
pull request on its head, in a worker slot. The agents only read the result; they hold no network and no key. The Forgejo host is the one kd-release pins; the host
key comes from the repository, never from a first connection.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Callable, Mapping

from kd import naming
from kd.agents import NAME_RE as AGENT_NAME_RE
from kd.repo_read import CHECKOUTS, NAME_RE

REPOS = "/etc/digipaddock/repos.json"
KNOWN_HOSTS = "/opt/digipaddock/common/install/forgejo_known_hosts"
KEY = "~/.ssh/repo_sync_ed25519"  # under the service user's home (HOME of the unit)
BRANCH = "main"  # the base of an entry without one
# the passport's branch pattern (dev_agent.repos.BRANCH_RE): no "..", not ending ".lock"
BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_REPO_PART = "[A-Za-z0-9_.-]+"
_REPO_RE = re.compile(rf"{_REPO_PART}/{_REPO_PART}\Z")


class SyncError(Exception):
    pass


def has_other_readers(agent: str, registry_path: str = "/etc/digipaddock/agents.json",
                      agents_root: str = "/srv/digipaddock-agents") -> bool:
    """Keep the shared timer while another registered, installed policy holds repo.read.

    A disabled agent has no installed policy, even though its registry entry is kept.
    Unreadable or malformed state keeps synchronization running; it grants no agent an ACL.
    The installer serializes role changes and excludes the role currently being disabled.
    """
    try:
        registry = json.loads(Path(registry_path).read_text(encoding="utf-8"))
        if not isinstance(registry, dict):
            return True
        for name in registry:
            if not isinstance(name, str) or not AGENT_NAME_RE.fullmatch(name) or not isinstance(registry[name], dict):
                return True
            if name == agent:
                continue
            path = Path(agents_root) / name / ".claude" / "policy.json"
            if path.is_symlink():
                return True
            try:
                policy = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            if not isinstance(policy, dict) or not isinstance(policy.get("capabilities"), list):
                return True
            if "repo.read" in policy["capabilities"]:
                return True
        return False
    except (OSError, ValueError):
        return True


def forge_url(repo: str) -> str:
    """The SSH clone URL from host/host.json of the release tree — the same pinned host as kd-release."""
    return naming.load_host()["forge_ssh_url"].replace("{repo}", repo)


def load_repos(path: str = REPOS) -> dict[str, tuple[str, str]]:
    """name → (owner/repo, base branch)."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise SyncError(f"{path}: unreadable ({exc.__class__.__name__})") from exc
    if not isinstance(data, dict):
        raise SyncError(f"{path}: expected an object")
    repos = {}
    for name, item in data.items():
        repo = item.get("repo") if isinstance(item, dict) else None
        base = item.get("base", BRANCH) if isinstance(item, dict) else None
        if not (isinstance(name, str) and NAME_RE.fullmatch(name)) or not (
            isinstance(repo, str) and _REPO_RE.fullmatch(repo) and ".." not in repo
        ) or not _branch(base):
            raise SyncError(f"{path}: invalid entry {name!r}")
        repos[name] = (repo, base)
    return repos


def _branch(value) -> bool:
    return (isinstance(value, str) and bool(BRANCH_RE.fullmatch(value)) and ".." not in value
            and not value.endswith(".lock"))


def ssh_command(key: str, known_hosts: str = KNOWN_HOSTS) -> str:
    return (f"ssh -i {key} -o IdentitiesOnly=yes -o BatchMode=yes -o UserKnownHostsFile={known_hosts}"
            " -o StrictHostKeyChecking=yes -o GlobalKnownHostsFile=/dev/null -o UpdateHostKeys=no")


def git_env(base: Mapping[str, str], *, key: str, known_hosts: str = KNOWN_HOSTS) -> dict[str, str]:
    env = {k: v for k, v in base.items() if not k.startswith("GIT_")}
    env.update({"GIT_SSH_COMMAND": ssh_command(key, known_hosts), "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "LANG": "C.UTF-8"})
    return env


def _ok(run: Callable, argv: list[str], env, log: Callable[[str], None], what: str) -> bool:
    proc = run(argv, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        log(f"{what}: git exited {proc.returncode}" + (f": {detail[-1]}" if detail else ""))
        return False
    return True


# what a copy holds of its forge: the branches, and the head of every pull request — a critic of the code reads
# a pull request on its head. A ref the forge dropped leaves the copy too (--prune).
REFSPECS = ("+refs/heads/*:refs/remotes/origin/*", "+refs/pull/*/head:refs/pull/*/head")


def _fetch(target: str) -> list[str]:
    return ["git", "-C", target, "fetch", "--quiet", "--prune", "origin", *REFSPECS]


def sync_one(name: str, repo: str, *, base: str = BRANCH, root: str, run: Callable, env,
             log: Callable[[str], None]) -> bool:
    target = os.path.join(root, name)
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        info = None
    if info is not None and not stat.S_ISDIR(info.st_mode):
        log(f"{name}: {target} is a link or not a directory; left alone")
        return False
    try:
        url = forge_url(repo)
    except naming.NamingError as exc:
        log(f"{name}: no forge address ({exc})")
        return False
    if info is None:
        temp = target + ".new"
        shutil.rmtree(temp, ignore_errors=True)
        # the pull heads come with the first run, or it is not a copy yet: the next run starts over
        if not (_ok(run, ["git", "clone", "--quiet", "--branch", base, url, temp], env, log, f"{name}: clone")
                and _ok(run, _fetch(temp), env, log, f"{name}: fetch")):
            shutil.rmtree(temp, ignore_errors=True)
            return False
        os.rename(temp, target)
        return True
    # origin from the registry every time: a project moved to another repository follows it
    return (
        _ok(run, ["git", "-C", target, "remote", "set-url", "origin", url], env, log, f"{name}: set-url")
        and _ok(run, _fetch(target), env, log, f"{name}: fetch")
        and _ok(run, ["git", "-C", target, "reset", "--quiet", "--hard", f"origin/{base}"], env, log,
                f"{name}: reset")
        and _ok(run, ["git", "-C", target, "clean", "--quiet", "-fd"], env, log, f"{name}: clean")
    )


def sync_all(*, repos_path: str = REPOS, root: str = CHECKOUTS, run: Callable = subprocess.run, env,
             log: Callable[[str], None]) -> int:
    failed = 0
    for name, (repo, base) in sorted(load_repos(repos_path).items()):
        if not sync_one(name, repo, base=base, root=root, run=run, env=env, log=log):
            failed += 1
    return 1 if failed else 0


def main(argv=None, *, repos_path: str = REPOS, root: str = CHECKOUTS, run: Callable = subprocess.run,
         env: Mapping[str, str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv:
        print("usage: kd-repo-sync", file=sys.stderr)
        return 2
    base = dict(os.environ if env is None else env)
    key = os.path.join(base.get("HOME", "/nonexistent"), KEY[2:]) if KEY.startswith("~/") else KEY
    try:
        return sync_all(repos_path=repos_path, root=root, run=run, env=git_env(base, key=key),
                        log=lambda line: print(f"kd-repo-sync: {line}", file=sys.stderr))
    except SyncError as exc:
        print(f"kd-repo-sync: {exc}", file=sys.stderr)
        return 1
