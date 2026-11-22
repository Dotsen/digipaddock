"""kd-repo-attach: the root unit that binds and unbinds a project's repository on the owner's request in the
cabinet. digipaddock-repo-attach.path (PathExists) starts it while the cabinet's doorbell
``/var/lib/digipaddock/repo-attach/request`` is there; the requests themselves are the cabinet's
(``project.repo.queue``, root only), the doorbell's content is never read.

For each request it runs add_repo.sh and nothing else, with a fixed argv: ``add_repo.sh <project> <owner/repo>
[--release <the registry's own>]`` to bind (never ``--allow-hooks``; the release flag only stays where root set it
for the same repository) or ``add_repo.sh --remove <project>`` to unbind (the clones stay). The project and
``owner/repo`` are checked again here before any run. Its exit code is the answer to the cabinet
(``project.repo.result``): 0 connected, 4 hooks, 5 no key of dev's account, 6 no access for dev, anything else an
error with add_repo.sh's last words. A run that hangs is killed with its whole process group (git under runuser).

One start works at most ``BUDGET`` seconds; what is left rings the doorbell again for the next start. A cabinet that
is away is asked again (``ASK_TRIES``); an answer it never took rings again too, a queue it never gave fails the unit
(kd-alert) and the cabinet rings again at its own next start (``kd.cabinet.server.build``)."""
import json
import os
import re
import signal
import subprocess
import sys
import time

try:
    import fcntl
except ImportError:  # Windows (the owner's tests): the lock is the server's
    fcntl = None

from dev_agent import repos

REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")  # = projects_api.REPO
ADD_REPO = "/opt/digipaddock/agents/dev/install/add_repo.sh"
DOORBELL = "/var/lib/digipaddock/repo-attach/request"
SOCKET = "/run/digipaddock-cabinet/local.sock"
LOCK = "/run/digipaddock-repo-attach/lock"  # RuntimeDirectory=digipaddock-repo-attach
T_ADD = 300  # a clone of a big repository over ssh
BUDGET = 600  # one start: BUDGET + T_ADD + three calls with their retries (a queue, a result, the next queue) < 1800
ASK_TRIES, ASK_PAUSE = 6, 10  # a cabinet restarting after a release comes back within seconds
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "HOME": "/root"}
BIND_CODES = {0: "connected", 4: "hooks", 5: "no_key", 6: "no_access"}
DETAIL_MAX = 300
ROUNDS = 5  # doorbells answered in one start; the rest — the next start by PathExists


class Refused(ValueError):
    """A request this unit does not run (a bad name, a release project)."""


class Done:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def spawn(argv, timeout=T_ADD) -> Done:
    """Run add_repo.sh in its own process group; past ``timeout`` the whole group is killed (``TimeoutExpired``)."""
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=ENV, cwd="/",
                         start_new_session=True)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        for pipe in (p.stdout, p.stderr):  # a straggler outside the group may hold them: never wait on a read
            pipe.close()
        p.wait()
        raise
    return Done(p.returncode, out, err)


def _registry(path) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise Refused(f"repos.json не читается: {exc.__class__.__name__}")
    if not isinstance(data, dict):
        raise Refused("repos.json: ожидается объект проект → описание")
    return data


def command(item, registry_path=repos.REPOS, add_repo=ADD_REPO) -> list:
    """add_repo.sh's argv for one request of the cabinet; ``Refused`` — nothing is run."""
    project, repo, action = item.get("id"), item.get("repo"), item.get("action")
    if not isinstance(project, str) or not repos.PROJECT_RE.fullmatch(project):
        raise Refused(f"неверный проект {project!r}")
    if action not in ("bind", "unbind"):
        raise Refused(f"неизвестное действие {action!r}")
    entry = _registry(registry_path).get(project)
    entry = entry if isinstance(entry, dict) else {}
    release = entry.get("release")
    if action == "unbind":  # the project alone: a repository root bound by hand goes too
        if release:
            raise Refused(f"проект {project} выкатывается kd-merge --release: отвязывает только root")
        return ["bash", add_repo, "--remove", project]
    if not isinstance(repo, str) or not REPO_RE.fullmatch(repo) or ".." in repo:
        raise Refused(f"неверный репозиторий {repo!r}: owner/repo")
    argv = ["bash", add_repo, project, repo]
    if release:
        if entry.get("repo") != repo:
            raise Refused(f"проект {project} выкатывается из {entry.get('repo')}: репозиторий меняет только root")
        if not isinstance(release, str) or release not in repos.RELEASES:
            raise Refused(f"проект {project}: неизвестный release в repos.json")
        argv += ["--release", release]  # root's own flag stays; the cabinet never sets it
    return argv


def _last_words(text) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return "; ".join(lines[-3:])[-DETAIL_MAX:]


def answer(item, run=spawn, registry_path=repos.REPOS, add_repo=ADD_REPO) -> dict:
    """The ``project.repo.result`` for one request."""
    action = item.get("action")
    result = {"op": "project.repo.result", "id": item.get("id"), "action": action, "repo": item.get("repo")}
    try:
        argv = command(item, registry_path, add_repo)
    except Refused as exc:
        return {**result, "state": "error", "detail": f"отказ: {exc}"[:DETAIL_MAX]}
    try:
        p = run(argv)
        code, words = p.returncode, _last_words(p.stderr) or _last_words(p.stdout)
    except subprocess.TimeoutExpired:
        code, words = None, f"add_repo.sh не уложился в {T_ADD} с — остановлен"
    except (OSError, ValueError) as exc:
        code, words = None, f"add_repo.sh не запустился: {exc.__class__.__name__}"
    if action == "bind":
        state = BIND_CODES.get(code, "error")
    else:
        state = "" if code == 0 else "error"
    return {**result, "state": state, "detail": "" if state in ("connected", "") else words}


def _transient(exc) -> bool:
    """The cabinet away (restarting, busy past the client's timeout), not a refusal."""
    return isinstance(exc, OSError) or getattr(exc, "kind", None) == "unavailable"


def _ask(client, request, sleep, err):
    """One call; a cabinet that is away is asked again (a restart after a release), a refusal is final."""
    for attempt in range(ASK_TRIES):
        try:
            return client.call(request)
        except Exception as exc:  # the cabinet's client error or a socket error
            if not _transient(exc) or attempt == ASK_TRIES - 1:
                raise
            print(f"kd-repo-attach: кабинет не ответил ({exc.__class__.__name__}), повтор", file=err)
            sleep(ASK_PAUSE)
    raise AssertionError("unreachable")


def queue(client, sleep=time.sleep, err=sys.stderr) -> list:
    """The waiting requests."""
    return _ask(client, {"op": "project.repo.queue"}, sleep, err).get("items", [])


def take(client, run=spawn, registry_path=repos.REPOS, add_repo=ADD_REPO, err=sys.stderr, deadline=None,
         clock=time.monotonic, sleep=time.sleep):
    """Every waiting request once, until ``deadline``: ``(answered, left)`` — ``left``: requests not run or not
    answered (the cabinet away); they wait in the cabinet for the next start (add_repo.sh is idempotent)."""
    done = 0
    for item in queue(client, sleep, err):
        if deadline is not None and clock() >= deadline:
            return done, True
        out = answer(item, run, registry_path, add_repo)
        print(f"kd-repo-attach: {out['id']} {out['action']} {out['repo']}: {out['state'] or 'отвязан'}"
              f"{' — ' + out['detail'] if out.get('detail') else ''}", file=err)
        try:
            _ask(client, out, sleep, err)
            done += 1
        except Exception as exc:
            print(f"kd-repo-attach: итог {out['id']} не записан: {exc.__class__.__name__}: {exc}", file=err)
            if _transient(exc):
                return done, True
            # refused: a stale answer (the owner moved on) — the next request goes on
    return done, False


def _ring_off(path) -> bool:
    """Take the doorbell off before the work (a request after it rings again). A doorbell that cannot go (a
    directory in its place — only the cabinet's user can make one) stops the start: PathExists would loop on it
    until its trigger limit; kd-alert tells the owner, root clears the place."""
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return True
    except OSError as exc:
        print(f"kd-repo-attach: не снять {path}: {exc.__class__.__name__}", file=sys.stderr)
        return False


def _ring_again(path):
    """Ring for the next start (requests left past the budget), with the directory's group: the cabinet may touch it."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o660)
    except FileExistsError:
        return
    try:
        os.fchown(fd, -1, os.stat(os.path.dirname(path)).st_gid)  # the owner stays root's
        os.fchmod(fd, 0o660)
    finally:
        os.close(fd)


def main(argv=None, *, doorbell=DOORBELL, client=None, lock_path=LOCK, run=spawn, registry_path=repos.REPOS,
         add_repo=ADD_REPO, clock=time.monotonic, sleep=time.sleep) -> int:
    if client is None:
        sys.path.insert(0, "/opt/digipaddock/common")
        from kd.cabinet import client as cabinet

        client = cabinet.Client(SOCKET)
    lock = None
    if fcntl is not None and lock_path:
        os.makedirs(os.path.dirname(lock_path), mode=0o755, exist_ok=True)
        lock = open(lock_path, "w")
        fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        deadline = clock() + BUDGET
        for _ in range(ROUNDS):
            if clock() >= deadline:
                return 0  # the doorbell that brought this round is still there: PathExists starts the next
            if not _ring_off(doorbell):
                return 1
            _, left = take(client, run, registry_path, add_repo, deadline=deadline, clock=clock, sleep=sleep)
            if left:
                _ring_again(doorbell)
                return 0
            if not os.path.lexists(doorbell):
                return 0
        return 0
    finally:
        if lock is not None:
            lock.close()
