"""kd-deps install — a project's locked dependencies in a card's working copy.

Run from a working copy right under the agent's ``work/`` (a clone or a card's worktree), it installs what the lock
files of that directory pin, and nothing else:

- ``composer.lock`` → ``composer install --no-scripts --no-plugins --no-interaction --no-progress --prefer-dist``;
- ``package-lock.json`` → ``npm ci --ignore-scripts --no-audit --no-fund``.

A package's install scripts and composer's plugins never run: the packages' code runs only when the agent runs the
project's tests, inside the same sandbox that already runs the pull request's own code. The dependency folder
(``vendor/``, ``node_modules/``) must be ignored by git, so it never reaches a pull request. The caches live under
``work/.cache`` — the sandbox lets nothing be written in the agent's home. The network is the session's own: the
proxy variables of the standing exit pass to the package manager unchanged.

There is one form, ``kd-deps install``: no package is added, updated or removed here — that is a change of the lock
file, made in the project and reviewed in its pull request.
"""

import os
import subprocess
import sys
from typing import Callable, Dict, List, NamedTuple, Optional

from dev_agent import repos

USAGE = "usage: kd-deps install (в рабочей копии проекта: work/<имя>)"


class Refused(Exception):
    """Nothing is installed; the text says why."""


class Step(NamedTuple):
    lock: str  # the lock file that asks for it
    folder: str  # where the packages land: must be ignored by git
    argv: List[str]
    env: Dict[str, str]
    caches: List[str]  # the tool's own cache folders, made before it runs


class _Tool(NamedTuple):
    lock: str
    folder: str
    argv: List[str]
    caches: Callable[
        [str], Dict[str, str]
    ]  # the cache root -> the variables naming the tool's cache folders
    fixed: Dict[
        str, str
    ]  # variables that hold whatever the project or the session says


TOOLS = (
    _Tool(
        "composer.lock",
        "vendor",
        [
            "composer",
            "install",
            "--no-scripts",
            "--no-plugins",
            "--no-interaction",
            "--no-progress",
            "--prefer-dist",
        ],
        lambda cache: {
            "COMPOSER_HOME": os.path.join(cache, "composer", "home"),
            "COMPOSER_CACHE_DIR": os.path.join(cache, "composer", "cache"),
        },
        # composer.json may name another folder (config.vendor-dir): the one git was asked about is the one used
        {"COMPOSER_VENDOR_DIR": "vendor"},
    ),
    _Tool(
        "package-lock.json",
        "node_modules",
        ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
        lambda cache: {"npm_config_cache": os.path.join(cache, "npm")},
        {},
    ),
)


def _ignored(cwd: str, name: str) -> bool:
    """git ignores the folder ``name`` of the working copy ``cwd`` (asked as a folder: it may not exist yet, and a
    pattern ``vendor/`` names folders only)."""
    done = subprocess.run(
        ["git", "check-ignore", "-q", name + "/"],
        cwd=cwd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return done.returncode == 0


def plan(
    cwd: str, work: str, ignored: Callable[[str, str], bool] = _ignored
) -> List[Step]:
    """The installs the lock files of ``cwd`` ask for; Refused when it is no working copy under ``work`` or a
    dependency folder would be tracked."""
    tree, root = os.path.realpath(cwd), os.path.realpath(work)
    if os.path.dirname(tree) != root:
        raise Refused(
            f"зависимости ставятся в рабочей копии проекта — work/<имя>, а это {cwd}"
        )
    if not os.path.lexists(os.path.join(tree, ".git")):
        raise Refused(f"{cwd} — не рабочая копия git")
    cache = os.path.join(root, ".cache")
    steps = []
    for tool in TOOLS:
        lock = os.path.join(tree, tool.lock)
        if not os.path.lexists(lock):
            continue
        if os.path.islink(lock) or not os.path.isfile(lock):
            raise Refused(f"{tool.lock} — не обычный файл")
        if not ignored(tree, tool.folder):
            raise Refused(
                f"{tool.folder}/ не закрыт от git: сначала добавь его в .gitignore проекта"
            )
        caches = tool.caches(cache)
        steps.append(
            Step(
                tool.lock,
                tool.folder,
                list(tool.argv),
                {**os.environ, **caches, **tool.fixed},
                sorted(caches.values()),
            )
        )
    if not steps:
        raise Refused(
            f"в {cwd} нет lock-файла: {' или '.join(tool.lock for tool in TOOLS)}"
        )
    return steps


def main(
    argv: Optional[List[str]] = None,
    run=subprocess.run,
    cwd: Optional[str] = None,
    work: Optional[str] = None,
    out=None,
    err=None,
) -> int:
    argv = sys.argv[1:] if argv is None else argv
    out, err = out or sys.stdout, err or sys.stderr
    if argv != ["install"]:
        print(USAGE, file=err)
        return 2
    cwd = cwd or os.getcwd()
    work = work or os.path.join(repos.AGENT_DIR, "work")
    try:
        steps = plan(cwd, work)
    except Refused as exc:
        print(f"kd-deps: отказ: {exc}", file=err)
        return 2
    for step in steps:
        for path in step.caches:
            os.makedirs(path, exist_ok=True)
        print(f"kd-deps: {step.lock} → {' '.join(step.argv)}", file=out, flush=True)
        code = run(step.argv, cwd=cwd, env=step.env).returncode
        if code:
            print(f"kd-deps: {step.argv[0]} завершился с кодом {code}", file=err)
            return code
    print("kd-deps: зависимости установлены", file=out)
    return 0
