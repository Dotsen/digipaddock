"""kd-operation-run dev.check <id> — the root runner of one dev.check.

The session host starts it as ``digipaddock-operation-<id12>`` once ``<id>.request.json`` is written. The runner reads the PR
live in Forgejo with the ci account's token (statuses only), sets ``devcheck/<profile>`` pending on its head, checks
the head and the set's components out as root with the read key of the repo user, hands the directory to the ci user,
prepares the dependencies and runs each profile in its own child unit ``digipaddock-devcheck-<id12>``: the ci user, no
network, a read-only system with only the check's directory writable, the platform's sockets and secrets out of
sight, the deployed ``devcheck_steps.py`` (never the candidate's runner). The first red profile stops the rest. The
PR's head is read again — moved: ``superseded``. The statuses end success or failure (error for what did not run)
and link the card. The masked log is ``LOG_DIR/<id>.log``; the result ``<id>.result.json`` is what the host reports
to the cabinet. Nothing of the request becomes a unit property: the limits are this file's.
"""

import contextlib
import hashlib
import http.client
import json
import os
import posixpath
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

from kd import devcheck_steps as steps
from kd import naming, repo_sync
from kd.envfile import load_env
from kd.session_host import devcheck as host_devcheck
from kd.session_host import protocol
from kd.session_host.operation import STATE, read_json, write_json

WORK = "/var/lib/digipaddock/devcheck"  # <id>/ per check; cache/ — the prepared dependencies
LOG_DIR = "/var/log/digipaddock-devcheck"  # <id>.log, root:<observe> 0640 — kd-host-read's root «devcheck»
TOKEN = "/etc/digipaddock/devcheck/digipaddock-ci.env"  # FORGE_TOKEN= of the ci account: statuses, nothing else
DEPLOYED = "/opt/digipaddock"
CHILD_MAX_S = 1200  # one profile's limit, whatever the passport asks
MIN_CHILD_S = 60  # less left of the check's limit: the profile is not started
DESCRIPTION_MAX = 140
TAIL_MAX = 1500
OUTPUT_MAX = 1 << 20  # bytes of a child's or git's output kept (masked whole, then cut)
GIT_TIMEOUT = 600
RETRY_S = 5  # one retry of Forgejo after this pause
TIMEOUT = 30  # one API call
STATUS_PREFIX = "devcheck/"
INACCESSIBLE = ("/run/digipaddock-cabinet", "/run/digipaddock-cabinet-http", "/run/digipaddock-session-host", "/run/digipaddock-review",
                "/run/digipaddock-model-keys",
                "/run/digipaddock-transcribe", "/run/digipaddock", "/etc/digipaddock", "/srv/digipaddock-agents",
                "/var/lib/digipaddock/cabinet")
UNMASKED = "(вывод скрыт: маски шлюза недоступны)"
COUNT_KINDS = ("passed", "failed", "skipped", "error", "xfailed", "xpassed")
DEPS_MAX_S = 900  # one build step of a prepared dependency
MIN_DEPS_S = 30  # less left of the dependencies' budget: the build step is not started
STOP_S = 120  # of the runner's unit limit, kept for the end: statuses, cleanup, the result
PRUNE_S = 7 * 86400  # a cache of another lock unused this long is removed after a build
LOCK_MAX = 4 << 20
VENV_LOCKS = ("requirements-dev.txt", "common/cabinet/requirements-build.txt", "common/cabinet/requirements.txt")
WEB = "common/cabinet/web"
NODE_LOCK = f"{WEB}/package-lock.json"
_PIP = ("--quiet", "--disable-pip-version-check", "--no-input")
# The candidate's locks decide what is built, so nothing of theirs runs while the network is open (review 04.11.2026):
# a downloader venv of its own fetches the files — wheels are only fetched, never installed there — and the venv
# itself is installed from them with no index, in a unit with no network. The one source package (http-ece, no
# wheel) is built by its maintainers' code even to be fetched: its lines and the build backend's lock
# (requirements-build.txt) must be the deployed tree's, else the check is dependency_update_required.
INDEX = "https://pypi.org/simple"
NO_BINARY = ("http-ece",)
NPM_REGISTRY = "https://registry.npmjs.org/"
_PIP_HASH = re.compile(r"--hash=sha256:[0-9a-f]{64}")
_PIP_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{16,128}")
_SHA = re.compile(r"[0-9a-f]{40}")
_STEP_FAILED = re.compile(r"^== ([a-z ]{1,30}) failed \(exit -?[0-9]+\)$", re.M)
_ARMED = [False]  # SIGTERM interrupts the stages only; the finish (statuses, cleanup, result) runs to its end


class RunnerError(Exception):
    """No valid request for this check: nothing to report."""


class ForgeUnavailable(Exception):
    pass


class ForgeRefused(Exception):
    """Forgejo refused the ci account's token (401/403) or its address is not https."""


class DepsFailed(Exception):
    pass


class DepsSlow(DepsFailed):
    """The build did not fit its budget: the contour is slow, the locks are not to blame."""


class Terminated(BaseException):
    """The host stopped the runner (SIGTERM) while it ran the stages."""


class _End(Exception):
    def __init__(self, cls: str, tail: str = "", *, why: str = "", maintenance: str = ""):
        super().__init__(cls)
        self.cls, self.tail, self.why, self.maintenance = cls, tail, why, maintenance


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib would carry the token to the new address


_OPENER = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({})).open


class Forgejo:
    """The two calls the runner makes: the PR as it is now, and a commit status."""

    def __init__(self, token: str, *, base: str, opener: Optional[Callable] = None):
        if not base.startswith("https://"):
            raise ForgeRefused("Forgejo API is https only")
        self._token, self._base, self._open = token, base, opener or _OPENER

    def _call(self, method: str, path: str, body: Optional[dict] = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self._base + path, data=data, method=method, headers={
            "Authorization": f"token {self._token}", "Accept": "application/json",
            **({"Content-Type": "application/json"} if data else {})})
        try:
            with self._open(req, timeout=TIMEOUT) as resp:
                raw = resp.read(OUTPUT_MAX)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise ForgeRefused(f"Forgejo {exc.code} {path}") from None
            if exc.code == 404:
                return None
            raise ForgeUnavailable(f"Forgejo {exc.code} {path}") from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise ForgeUnavailable(f"Forgejo unreachable ({exc.__class__.__name__})") from None
        try:
            return json.loads(raw) if raw.strip() else {}
        except ValueError:
            raise ForgeUnavailable(f"Forgejo answered {path} not with JSON") from None

    def pr(self, repo: str, number: int) -> Optional[dict]:
        """``{state: open|closed|merged, head, base}``; None — no such PR."""
        data = self._call("GET", f"/repos/{repo}/pulls/{number}")
        if data is None:
            return None
        if not isinstance(data, dict) or data.get("number") != number:
            raise ForgeUnavailable(f"PR #{number}: unexpected answer")
        head, base = data.get("head"), data.get("base")
        state = "merged" if data.get("merged") is True else "open" if data.get("state") == "open" else "closed"
        return {"state": state, "head": str((head or {}).get("sha") or "").lower() if isinstance(head, dict) else "",
                "base": str(base.get("ref") or "") if isinstance(base, dict) else ""}

    def status(self, repo: str, sha: str, *, context: str, state: str, description: str, url: str) -> None:
        self._call("POST", f"/repos/{repo}/statuses/{sha}", {"state": state, "context": context,
                                                               "description": description, "target_url": url})


def set_hash(components: dict) -> str:
    """The set's mark in the statuses: the other components' SHAs the check ran against."""
    text = json.dumps(dict(sorted(components.items())), separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def counts_of(out: str) -> dict:
    """The steps' last ``== counts`` line: informational, only the known kinds (the output is the candidate's)."""
    for line in reversed(out.splitlines()):
        if line.startswith(steps.COUNTS):
            try:
                data = json.loads(line[len(steps.COUNTS):])
            except ValueError:
                return {}
            if not isinstance(data, dict):
                return {}
            return {k: v for k, v in data.items() if k in COUNT_KINDS and isinstance(v, int)
                    and not isinstance(v, bool) and 0 <= v < 10 ** 7}
    return {}


def _counts_text(counts: dict) -> str:
    return ", ".join(f"{counts[k]} {k}" for k in COUNT_KINDS if counts.get(k))


class _Log:
    """The check's masked log, written as it goes (kd-host-read follows it); a log that cannot be written is no
    reason to fail the check — the result then names none."""

    def __init__(self, log_dir: str, check_id: str, mask: Callable, own: Callable, group: str):
        self.path, self.rel, self._mask = os.path.join(log_dir, f"{check_id}.log"), f"devcheck/{check_id}.log", mask
        try:
            os.makedirs(log_dir, mode=0o750, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o640)
            os.close(fd)
            own(self.path, group=group)
        except (OSError, KeyError) as exc:
            print(f"kd-operation-run: the log {self.path} is not writable ({exc.__class__.__name__})", file=sys.stderr)
            self.rel = ""

    def masked(self, text: str) -> str:
        masked = self._mask(text)
        return UNMASKED if masked is None else masked

    def write(self, text: str) -> None:
        if not self.rel:
            return
        try:
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(self.masked(text).rstrip("\n") + "\n")
        except OSError:
            pass


class _Check:
    def __init__(self, check_id: str, record: dict, *, request_path: str, forge, git, units, clock, sleep, work,
                 deps, own, deployed, host, repos, remove, ci_user, log: _Log, token: str):
        self.id, self.params, self.request_path = check_id, record["params"], request_path
        self.forge, self.git, self.units, self.clock, self.sleep = forge, git, units, clock, sleep
        self.deps, self.own, self.deployed, self.host, self.repos = deps, own, deployed, host, repos
        self.remove, self.ci_user, self.log, self.token = remove, ci_user, log, token
        # the runner's own unit stops at the request's timeout: the tests and the end fit before it
        self.deadline = clock() + (record.get("timeout") or self.params["timeout_s"] + 600) - STOP_S
        self.dir = os.path.join(work, check_id)
        self.src = os.path.join(self.dir, "src")
        self.sets = {name: os.path.join(self.dir, "sets", name) for name in sorted(self.params["set"])}
        self.mark = set_hash(self.params["set"])
        self.api = None
        self.url = ""
        self.pending = False  # the statuses are pending: each must end
        self.ended: set = set()  # the profiles whose final status is written
        self.profiles: list = []  # the results of the profiles that ran
        self.red = None  # (profile, its output) of the first red

    # --- the stages -------------------------------------------------------------------------------
    def stages(self) -> tuple:
        p = self.params
        known = steps.STEPS.get(p["project"], {})
        missing = [name for name in p["profiles"] if name not in known]
        missing += [f"{name} checkout" for name in steps.NEEDS.get(p["project"], ()) if name not in p["set"]]
        if missing:
            raise _End("environment_not_ready", f"{p['project']}: no steps on this tree for {', '.join(missing)}")
        token = self._token()
        self.url = f"{self.host['cabinet_origin']}/cards/{p['card']}"
        self.api = self._forge(lambda: self.forge(token))
        self._live()
        for profile in p["profiles"]:
            self._forge(lambda profile=profile: self._status(profile, "pending", f"идёт проверка · набор {self.mark}"))
        self.pending = True
        self._checkout()
        self.own(self.dir, user=self.ci_user, recursive=True)
        try:  # the dependencies get what the tests' own limit leaves of the runner's
            prepared = self.deps(self.id, p["project"], self.src, dict(self.sets),
                                 budget_s=max(0, self.deadline - self.clock() - p["timeout_s"]))
        except DepsSlow as exc:
            self.log.write(f"== dependencies\n{exc}")
            raise _End("environment_not_ready", str(exc), why="не запускался: зависимости не успели подготовиться")
        except DepsFailed as exc:
            self.log.write(f"== dependencies\n{exc}")
            raise _End("dependency_update_required", str(exc), why="не запускался: зависимости не подготовлены")
        self._profiles(prepared)
        self._live()
        self._cancelled()
        return self._final()

    def _token(self) -> str:
        try:
            token = load_env(self.token).get("FORGE_TOKEN", "")
        except (OSError, UnicodeDecodeError):
            token = ""
        if not _TOKEN_RE.fullmatch(token):
            raise _End("environment_not_ready", f"no ci token in {self.token}",
                       maintenance=f"нет токена kd-ci ({self.token})")
        return token

    def _forge(self, call: Callable):
        """One Forgejo call, retried once; a refused token is the contour's maintenance."""
        for attempt in (1, 2):
            try:
                return call()
            except ForgeRefused as exc:
                raise _End("environment_not_ready", str(exc), maintenance=f"токен kd-ci отклонён Forgejo: {exc}"[:200])
            except ForgeUnavailable as exc:
                if attempt == 2:
                    raise _End("external_unavailable", str(exc), why="не запускался: Forgejo недоступен")
                self.log.write(f"== Forgejo: {exc}; one retry in {RETRY_S} s")
                self.sleep(RETRY_S)

    def _status(self, profile: str, state: str, description: str) -> None:
        self.api.status(self.params["repo"], self.params["head"], context=STATUS_PREFIX + profile, state=state,
                        description=description[:DESCRIPTION_MAX], url=self.url)

    def _live(self) -> None:
        p = self.params
        pr = self._forge(lambda: self.api.pr(p["repo"], p["pr"]))
        if pr is None or pr["state"] != "open" or pr["head"] != p["head"] or pr["base"] != p["target"]:
            seen = "not found" if pr is None else f"{pr['state']}, head {pr['head'][:12] or '?'}, into {pr['base'] or '?'}"
            raise _End("superseded", f"PR #{p['pr']} is {seen}; the check is for {p['head'][:12]} into {p['target']}",
                       why="устарело: PR изменился")

    def _checkout(self) -> None:
        p = self.params
        if os.path.lexists(self.dir):
            self.remove(self.dir)  # a crashed run's leftover
        os.makedirs(os.path.join(self.dir, "home"), mode=0o700)
        self._fetch(self.src, p["repo"], f"refs/pull/{p['pr']}/head", p["head"], f"PR #{p['pr']}")
        self._base()
        for name, path in self.sets.items():
            if name not in self.repos:
                raise _End("environment_not_ready", f"{name} is not a project of repos.json")
            repo, base = self.repos[name]
            self._fetch(path, repo, f"refs/heads/{base}", p["set"][name], name)

    def _git(self, argv: list) -> str:
        rc, out = self.git(argv)
        if rc != 0:
            self.log.write(f"== git {argv[0] if argv[0] != '-C' else argv[2]}: exit {rc}\n{out}")
            raise _End("environment_not_ready", out, why="не запускался: копия не подготовлена")
        return out

    def _pull(self, path: str, repo: str, ref: str) -> None:
        """git fetch of one ref into FETCH_HEAD, retried once; Forgejo unreachable twice ends the check."""
        url = self.host["forge_ssh_url"].replace("{repo}", repo)
        for attempt in (1, 2):
            rc, out = self.git(["-C", path, "fetch", "-q", "--no-tags", url, ref])
            if rc == 0:
                return
            self.log.write(f"== fetch {repo} {ref}: exit {rc}\n{out}")
            if attempt == 2:
                raise _End("external_unavailable", out, why="не запускался: Forgejo недоступен")
            self.sleep(RETRY_S)

    def _base(self) -> None:
        """the target's commit the check is about (``base_sha``) as ``refs/kd/base`` of the candidate's
        checkout — the history step reads what the PR adds to it. The commit comes with the PR's own history or the
        target's; a checkout that holds it from neither (a PR not based on it, a target rewritten since) is
        superseded."""
        p = self.params
        self._pull(self.src, p["repo"], f"refs/heads/{p['target']}")
        rc, _ = self.git(["-C", self.src, "cat-file", "-e", f"{p['base_sha']}^{{commit}}"])
        if rc != 0:
            raise _End("superseded", f"{p['target']} no longer holds {p['base_sha'][:12]}",
                       why="устарело: изменилась база PR")
        self._git(["-C", self.src, "update-ref", steps.BASE_REF, p["base_sha"]])
        self.log.write(f"== base: {p['repo']} {p['target']} at {p['base_sha'][:12]}")

    def _fetch(self, path: str, repo: str, ref: str, sha: str, what: str) -> None:
        self._git(["init", "-q", path])
        self._pull(path, repo, ref)
        got = self._git(["-C", path, "rev-parse", "FETCH_HEAD"]).strip().lower()
        if got != sha:
            raise _End("superseded", f"{what} is {got[:12] or '?'} now; the check is for {sha[:12]}",
                       why="устарело: изменился код набора")
        self._git(["-C", path, "checkout", "-q", "--detach", "FETCH_HEAD"])
        self.log.write(f"== {what}: {repo} {ref} at {sha[:12]}")

    def _child(self, project: str, profile: str, prepared: dict, limit: int) -> list:
        props = [f"User={self.ci_user}", f"Group={self.ci_user}", "PrivateNetwork=yes", "PrivateTmp=yes",
                 "ProtectSystem=strict", "ProtectHome=yes", f"ReadWritePaths={self.dir}", "NoNewPrivileges=yes",
                 "CPUQuota=200%", "MemoryMax=3G", f"RuntimeMaxSec={limit}",
                 "InaccessiblePaths=" + " ".join("-" + path for path in INACCESSIBLE),
                 f"WorkingDirectory={self.src}"]
        argv = ["systemd-run", "--unit", host_devcheck.child_unit(self.id), "--wait", "--pipe", "--collect",
                "--quiet"]
        for prop in props:
            argv += ["-p", prop]
        argv += [f"--setenv=HOME={os.path.join(self.dir, 'home')}", "--setenv=CI=1", "--",
                 posixpath.join(prepared["venv"], "bin", "python"), "-I",
                 posixpath.join(self.deployed, "common", "kd", "devcheck_steps.py"), project, profile, self.src]
        return argv + [f"{name}={path}" for name, path in self.sets.items()]

    def _profiles(self, prepared: dict) -> None:
        p = self.params
        spent = 0.0
        for profile in p["profiles"]:
            self._cancelled()
            limit = int(min(p["timeout_s"] - spent, CHILD_MAX_S, self.deadline - self.clock()))
            if limit < MIN_CHILD_S and p["timeout_s"] - spent < MIN_CHILD_S:
                raise _End("checks_failed", f"the check's limit of {p['timeout_s']} s is spent before {profile}",
                           why="не запускался: исчерпан предел проверки")
            if limit < MIN_CHILD_S:  # the runner's own: the contour was slow, the candidate is not red
                raise _End("environment_not_ready", f"the runner's limit is spent before {profile}",
                           why="не запускался: исчерпан предел раннера")
            start = self.clock()
            rc, out = self.units(self._child(p["project"], profile, prepared, limit))
            duration = max(0.0, self.clock() - start)
            spent += duration
            if rc != 0 and duration >= limit:
                out += f"\n== {profile}: the limit of {limit} s is reached"
            self.log.write(f"== profile {profile}: exit {rc} in {round(duration)} s\n{out}")
            self.profiles.append({"profile": profile, "status": "success" if rc == 0 else "failure",
                                  "counts": counts_of(out), "duration_s": round(duration, 1)})
            if rc != 0:
                self._cancelled()  # a stop by the host is no red of the candidate
                self.red = (profile, out)
                return

    def _cancelled(self) -> None:
        why = self._cancel_reason()
        if why:
            raise _End("superseded" if why == "superseded" else "cancelled", f"the host cancelled the check ({why})",
                       why="не запускался: проверка снята")

    def _cancel_reason(self) -> str:
        record = read_json(self.request_path)
        why = record.get("cancel") if isinstance(record, dict) else ""
        return why if isinstance(why, str) else ""

    def _final(self) -> tuple:
        for result in self.profiles:
            name = result["profile"]
            counts = _counts_text(result["counts"])
            if result["status"] == "success":
                text = f"пройдено{' · ' + counts if counts else ''} · {round(result['duration_s'])} с · набор {self.mark}"
                self._end_status(name, "success", text)
            else:
                found = _STEP_FAILED.findall(self.red[1])
                step = f": {found[-1]}" if found else ""
                self._end_status(name, "failure", f"не пройдено{step}{' · ' + counts if counts else ''} · набор "
                                                  f"{self.mark}")
        if self.red is None:
            return "passed", ""
        self._close(f"не запускался: не пройден профиль {self.red[0]}")
        return "checks_failed", self.red[1]

    def _end_status(self, profile: str, state: str, description: str) -> None:
        self._forge(lambda: self._status(profile, state, description))
        self.ended.add(profile)

    def _close(self, why: str) -> None:
        """Every pending status ends: error, with why it did not run (best effort — Forgejo may be the reason)."""
        if not self.pending:
            return
        for profile in self.params["profiles"]:
            if profile in self.ended:
                continue
            try:
                self._status(profile, "error", why)
                self.ended.add(profile)
            except (ForgeUnavailable, ForgeRefused) as exc:
                self.log.write(f"== status {profile}: not written ({exc})")


def run(check_id: str, *, state_dir: str, forge, git, units, clock, deps, own, sleep=time.sleep, work: str = WORK,
        log_dir: str = LOG_DIR, token: str = TOKEN, deployed: str = DEPLOYED, host: Optional[dict] = None,
        repos: Optional[dict] = None, mask: Optional[Callable] = None, remove=shutil.rmtree, ci_user: str = "",
        observe_group: str = "", stop: Optional[Callable] = None) -> dict:
    """The check's stages, its result file written last. ``forge(token)`` → a client (``Forgejo``); ``git(argv)``
    and ``units(argv)`` → (exit code, output); ``deps(id, project, checkout, sets, budget_s=…)`` → ``{"venv",
    "node"}``; ``own(path, user=…, recursive=…)`` or ``own(path, group=…)``; ``stop(unit names)`` — the tests'
    units, which outlive a stopped or failed runner (each is a unit of its own)."""
    if not protocol.HEX32.fullmatch(check_id or ""):
        raise RunnerError("bad check id")
    request_path = os.path.join(state_dir, host_devcheck.DIR, f"{check_id}.request.json")
    record = read_json(request_path)
    if not isinstance(record, dict) or record.get("check") != check_id:
        raise RunnerError(f"no request for check {check_id}")
    try:
        protocol._check_params(record.get("params"))
    except protocol.ProtocolError as exc:
        raise RunnerError(f"check {check_id}: invalid request ({exc})") from None
    log = _Log(log_dir, check_id, mask or _mask, own, observe_group)
    check = _Check(check_id, record, request_path=request_path, forge=forge, git=git, units=units, clock=clock,
                   sleep=sleep, work=work, deps=deps, own=own, deployed=deployed, host=host, repos=repos,
                   remove=remove, ci_user=ci_user, log=log, token=token)
    maintenance = ""
    _ARMED[0] = True
    try:
        cls, tail = check.stages()
    except _End as end:
        _ARMED[0] = False
        cls, tail, maintenance = end.cls, end.tail, end.maintenance
        check._close(end.why or "не запускался: проверка прервана")
    except Terminated:
        _ARMED[0] = False
        _stop_tests(stop, check_id, log)
        why = check._cancel_reason()
        cls = "superseded" if why == "superseded" else "cancelled" if why else "host_lost"
        tail = f"the runner was stopped ({why or 'no cancel'})"
        check._close("не запускался: проверка снята")
    except Exception as exc:  # noqa: BLE001 — a runner's own fault still ends the statuses and leaves a result
        _ARMED[0] = False
        _stop_tests(stop, check_id, log)
        cls, tail = "environment_not_ready", f"the runner failed: {exc.__class__.__name__}: {exc}"
        check._close("не запускался: сбой раннера")
    finally:
        _ARMED[0] = False
    log.write(f"== {cls}" + (f": {maintenance}" if maintenance else ""))
    if os.path.lexists(check.dir):
        try:
            remove(check.dir)
        except OSError as exc:
            note = f"очистка каталога проверки не удалась: {exc.__class__.__name__}"
            maintenance = f"{maintenance}; {note}" if maintenance else note
            log.write(f"== {note}")
    result = {"check": check_id, "class": cls, "profiles": check.profiles, "deployed": {}, "log": log.rel,
              "error_short": log.masked(tail)[-TAIL_MAX:] if tail else ""}
    if maintenance:
        result["maintenance"] = maintenance[:200]
    write_json(os.path.join(state_dir, host_devcheck.DIR, f"{check_id}.result.json"), result)
    return result


def _stop_tests(stop: Optional[Callable], check_id: str, log: _Log) -> None:
    """Before the cleanup: a test still running in the check's directory is stopped, not orphaned."""
    if stop is None:
        return
    try:
        stop([host_devcheck.child_unit(check_id), host_devcheck.deps_unit(check_id)])
    except Exception as exc:  # noqa: BLE001 — the end still runs; the host's stop of the units follows
        log.write(f"== stop of the tests' units failed: {exc.__class__.__name__}: {exc}")


def _mask(text: str) -> Optional[str]:
    from kd import redact

    return redact.text(text)


def _capture(argv: list, *, env=None, timeout=None) -> tuple:
    try:
        proc = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"{argv[0]}: timed out after {timeout} s"
    except OSError as exc:
        return 127, f"{argv[0]}: {exc.__class__.__name__}"
    return proc.returncode, proc.stdout[-OUTPUT_MAX:].decode("utf-8", "replace")


def _own(path: str, *, user: str = "", group: str = "", recursive: bool = False) -> None:
    """chown without following a link: the checkout is the candidate's, a link in it may point anywhere."""
    import grp
    import pwd

    if group:
        os.chown(path, 0, grp.getgrnam(group).gr_gid, follow_symlinks=False)
        os.chmod(path, 0o640)
        return
    entry = pwd.getpwnam(user)
    os.chown(path, entry.pw_uid, entry.pw_gid, follow_symlinks=False)
    if recursive:
        for top, dirs, files in os.walk(path):
            for name in dirs + files:
                os.chown(os.path.join(top, name), entry.pw_uid, entry.pw_gid, follow_symlinks=False)


def _lock_bytes(checkout: str, rel: str) -> bytes:
    """A lock file of the candidate's checkout, read by root: inside the checkout, a regular file, never a link."""
    path = os.path.join(checkout, *rel.split("/"))
    if not os.path.realpath(path).startswith(os.path.realpath(checkout) + os.sep):
        raise DepsFailed(f"{rel}: outside the checkout")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    except OSError as exc:
        raise DepsFailed(f"{rel}: not readable ({exc.__class__.__name__})") from None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise DepsFailed(f"{rel}: not a file")
        data = handle.read(LOCK_MAX + 1)
    if len(data) > LOCK_MAX:
        raise DepsFailed(f"{rel}: larger than {LOCK_MAX} bytes")
    return data


def lock_hashes(checkout: str) -> dict:
    """What a prepared dependency is keyed by: the venv by develop_setup.sh's three files, node by the web's lock."""
    venv = b"".join(_lock_bytes(checkout, rel) for rel in VENV_LOCKS)
    return {"venv": hashlib.sha256(venv).hexdigest()[:12],
            "node": hashlib.sha256(_lock_bytes(checkout, NODE_LOCK)).hexdigest()[:12]}


def _pip_lines(rel: str, data: bytes) -> list:
    """The requirement lines of a candidate's pip lock (continuations joined, comments dropped): a requirement with
    its version, markers and ``--hash=sha256:`` options only — no index, link, URL, path or other file (``-r``,
    ``-c``, ``-e``, ``--index-url`` …), which would let a lock choose where the open network fetches from."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise DepsFailed(f"{rel}: not UTF-8") from None
    lines, current = [], ""
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0] if not raw.lstrip().startswith("#") else ""
        current += line.rstrip("\\").strip() + " "
        if line.rstrip().endswith("\\"):
            continue
        if current.strip():
            lines.append(current.strip())
        current = ""
    if current.strip():
        lines.append(current.strip())
    for line in lines:
        words = line.split()
        if words[0].startswith("-"):
            raise DepsFailed(f"{rel}: only --hash=sha256:… options, not {words[0][:40]}")
        if not _PIP_NAME.match(words[0]) or "://" in line or " @ " in f" {line} " or words[0].startswith((".", "/")):
            raise DepsFailed(f"{rel}: not a plain requirement: {line[:120]}")
        for word in words[1:]:
            if word.startswith("-") and not _PIP_HASH.fullmatch(word):
                raise DepsFailed(f"{rel}: only --hash=sha256:… options, not {word[:40]}")
    return lines


def _pip_name(line: str) -> str:
    return re.split(r"[ <>=!~;\[]", line, 1)[0].lower().replace("_", "-")


def _check_npm_lock(data: bytes) -> None:
    """Every package of the candidate's npm lock comes from the public registry (``resolved``): no git, file or other
    host — npm ci fetches with network (its scripts never run: --ignore-scripts)."""
    try:
        lock = json.loads(data.decode("utf-8"))
        packages = lock.get("packages") if isinstance(lock, dict) else None
        if not isinstance(packages, dict):
            raise ValueError("packages")
    except (UnicodeDecodeError, ValueError, AttributeError):
        raise DepsFailed(f"{NODE_LOCK}: not an npm lock (lockfileVersion 2 or 3)") from None
    for name, entry in packages.items():
        resolved = entry.get("resolved") if isinstance(entry, dict) else None
        if resolved is not None and not (isinstance(resolved, str) and resolved.startswith(NPM_REGISTRY)):
            raise DepsFailed(f"{NODE_LOCK}: {name or '(root)'} comes from {str(resolved)[:80]}, not {NPM_REGISTRY}")


def check_locks(checkout: str, trusted: str) -> None:
    """The candidate's locks may build: plain pip requirements, npm from the registry, and the build backend's lock
    and the source packages' lines exactly the deployed tree's (``trusted``)."""
    for rel in VENV_LOCKS:
        _pip_lines(rel, _lock_bytes(checkout, rel))
    if os.path.exists(os.path.join(checkout, *NODE_LOCK.split("/"))):
        _check_npm_lock(_lock_bytes(checkout, NODE_LOCK))
    build = "common/cabinet/requirements-build.txt"
    if _pip_lines(build, _lock_bytes(checkout, build)) != _pip_lines(build, _lock_bytes(trusted, build)):
        raise DepsFailed(f"{build} differs from the deployed one: the build backend changes with a release first")
    cabinet = "common/cabinet/requirements.txt"
    mine, theirs = (
        [line for line in _pip_lines(cabinet, _lock_bytes(tree, cabinet)) if _pip_name(line) in NO_BINARY]
        for tree in (checkout, trusted)
    )
    if mine != theirs:
        raise DepsFailed(f"{cabinet}: {', '.join(NO_BINARY)} differs from the deployed lock (a source package is "
                         "built with network; it changes with a release first)")


class Deps:
    """The prepared dependencies of a check (plan R2): ``cache/<kind>-<h12>``, keyed by the hash of their locks,
    built once and reused while the locks stay. A build is ``cache/.new-<kind>-<h12>-<id12>``: the lock files copied
    in by root, the ci user's to write, filled by the deps units — the files fetched with network by a downloader
    venv that installs nothing of the candidate's, the venv installed from them with none (``_commands``) — sealed root's
    (no group or other write) and renamed into place; a ``.new-*`` a crash left is removed, never renamed, and a
    cache that is not root's is no finished build. All of it under ``cache/.lock``: a second check of the same locks
    waits and reuses. The checkout gets a copy of node_modules, the ci user's (a link breaks svelte-check and
    vitest)."""

    def __init__(self, *, cache: str, units, own, seal, owner, locker, ci_user: str, remove=shutil.rmtree,
                 copy=None, clock=time.time, timer=time.monotonic, trusted: str = DEPLOYED):
        self.cache, self.units, self.own, self.seal, self.owner = cache, units, own, seal, owner
        self.trusted = trusted
        self.locker, self.ci_user, self.remove, self.clock, self.timer = locker, ci_user, remove, clock, timer
        self.copy = copy or (lambda src, dst: shutil.copytree(src, dst, symlinks=True))
        self.deadline: Optional[float] = None

    def __call__(self, check_id: str, project: str, checkout: str, sets: dict,
                 budget_s: Optional[float] = None) -> dict:
        """``budget_s`` — the build's share of the runner's limit, the wait for the lock included."""
        needs = steps.NEEDS.get(project, ())
        code = sets[needs[0]] if needs else checkout
        hashes = lock_hashes(code)
        check_locks(code, self.trusted)
        self.deadline = None if budget_s is None else self.timer() + budget_s
        os.makedirs(self.cache, exist_ok=True)
        os.chmod(self.cache, 0o755)  # the ci user's tests read the prepared caches; root's umask is 077
        out = {kind: self.ensure(kind, hashes[kind], code, check_id) for kind in steps.DEPS.get(project, ("venv",))}
        if "node" in out:
            self._node_modules(out["node"], code)
        return out

    def ensure(self, kind: str, h12: str, checkout: str, check_id: str) -> str:
        final = os.path.join(self.cache, f"{kind}-{h12}")
        with self.locker(os.path.join(self.cache, ".lock")):
            for name in os.listdir(self.cache):
                if name.startswith(".new-"):
                    self._clear(os.path.join(self.cache, name))  # under the lock no build runs: a crash's leftover
            if os.path.isdir(final) and not os.path.islink(final) and self.owner(final) == "root":
                os.utime(final)  # in use: the pruning spares it
                return final
            if os.path.lexists(final):
                self._clear(final)
            new = os.path.join(self.cache, f".new-{kind}-{h12}-{check_id[:12]}")
            os.mkdir(new)
            os.chmod(new, 0o755)
            os.mkdir(os.path.join(new, ".home"), 0o700)
            for command, network in self._commands(kind, new, checkout):
                what = f"{kind}: {' '.join(os.path.basename(a) for a in command[:5])} …"
                limit = DEPS_MAX_S if self.deadline is None else int(min(DEPS_MAX_S, self.deadline - self.timer()))
                if limit < MIN_DEPS_S:
                    self._clear(new)
                    raise DepsSlow(f"{what} not started: {max(limit, 0)} s left of the check's limit")
                start = self.timer()
                rc, out = self.units(self._unit(check_id, new, command, limit, network=network))
                if rc != 0:
                    self._clear(new)
                    if self.timer() - start >= limit:
                        raise DepsSlow(f"{what} cut at {limit} s\n{out[-TAIL_MAX:]}")
                    raise DepsFailed(f"{what} exit {rc}\n{out[-TAIL_MAX:]}")
            for scratch in (".home", ".dl", ".wheels"):  # the downloader and its files never reach the cache
                if os.path.lexists(os.path.join(new, scratch)):
                    self._clear(os.path.join(new, scratch))
            self.seal(new)
            os.replace(new, final)
            self._prune(kind, final)
        return final

    def _commands(self, kind: str, new: str, checkout: str) -> list:
        """The lock files copied in (by root, before the directory is the ci user's), then the build's
        (argv, network) steps: the venv's files fetched by a downloader venv of their own with network, the venv
        installed from them with none; npm ci with network and no scripts."""
        rels = VENV_LOCKS if kind == "venv" else (NODE_LOCK, f"{WEB}/package.json")
        into = os.path.join(new, ".locks") if kind == "venv" else new
        os.makedirs(into, exist_ok=True)
        for rel in rels:
            with open(os.path.join(into, rel.rsplit("/", 1)[-1]), "wb") as handle:
                handle.write(_lock_bytes(checkout, rel))
        self.own(new, user=self.ci_user, recursive=True)
        if kind == "node":
            return [(["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"], True)]
        locks, dl, wheels = (os.path.join(new, name) for name in (".locks", ".dl", ".wheels"))
        dev, build, cabinet = (os.path.join(locks, name)
                               for name in ("requirements-dev.txt", "requirements-build.txt", "requirements.txt"))
        source = [f"--no-binary={name}" for name in NO_BINARY] + ["--no-build-isolation"]
        fetch = [os.path.join(dl, "bin", "python"), "-m", "pip", "download", *_PIP, "--isolated", "--index-url", INDEX,
                 "--dest", wheels]
        install = [os.path.join(new, "bin", "python"), "-m", "pip", "install", *_PIP, "--isolated", "--no-index",
                   "--find-links", wheels]
        return [
            (["/usr/bin/python3", "-m", "venv", dl], True),
            # the build backend for the source package's metadata: the deployed lock (check_locks), hash-checked
            ([os.path.join(dl, "bin", "python"), "-m", "pip", "install", *_PIP, "--isolated", "--index-url", INDEX,
              "--require-hashes", "--only-binary=:all:", "-r", build], True),
            (fetch + ["--only-binary=:all:", "-r", dev], True),
            (fetch + ["--require-hashes", "--only-binary=:all:", "-r", build], True),
            (fetch + ["--require-hashes", "--only-binary=:all:", *source, "-r", cabinet], True),
            (["/usr/bin/python3", "-m", "venv", "--system-site-packages", new], False),
            (install + ["--upgrade", "--only-binary=:all:", "-r", dev], False),
            (install + ["--require-hashes", "--only-binary=:all:", "-r", build], False),
            (install + ["--require-hashes", "--only-binary=:all:", *source, "-r", cabinet], False),
        ]

    def _unit(self, check_id: str, new: str, command: list, limit: int = DEPS_MAX_S, *, network: bool = False) -> list:
        """A deps unit: the ci user, only its build directory writable; with network only for a step that runs
        nothing of the candidate's (a fetch, npm ci with no scripts)."""
        props = [f"User={self.ci_user}", f"Group={self.ci_user}", *(() if network else ("PrivateNetwork=yes",)),
                 "PrivateTmp=yes", "ProtectSystem=strict",
                 "ProtectHome=yes", f"ReadWritePaths={new}", "NoNewPrivileges=yes", "CPUQuota=200%", "MemoryMax=3G",
                 f"RuntimeMaxSec={limit}", "InaccessiblePaths=" + " ".join("-" + path for path in INACCESSIBLE),
                 f"WorkingDirectory={new}"]
        argv = ["systemd-run", "--unit", host_devcheck.deps_unit(check_id), "--wait", "--pipe", "--collect",
                "--quiet"]
        for prop in props:
            argv += ["-p", prop]
        return argv + [f"--setenv=HOME={os.path.join(new, '.home')}", "--", *command]

    def _node_modules(self, node: str, code: str) -> None:
        web = os.path.join(code, *WEB.split("/"))
        if not os.path.realpath(web).startswith(os.path.realpath(code) + os.sep) or not os.path.isdir(web):
            raise DepsFailed(f"{WEB}: not a directory of the checkout")
        target = os.path.join(web, "node_modules")
        if os.path.lexists(target):
            self._clear(target)
        self.copy(os.path.join(node, "node_modules"), target)
        self.own(target, user=self.ci_user, recursive=True)

    def _prune(self, kind: str, keep: str) -> None:
        """The other caches of this kind unused for PRUNE_S (a reuse touches its directory)."""
        horizon = self.clock() - PRUNE_S
        for name in os.listdir(self.cache):
            path = os.path.join(self.cache, name)
            try:
                if name.startswith(f"{kind}-") and path != keep and os.lstat(path).st_mtime < horizon:
                    self._clear(path)
            except OSError:
                continue

    def _clear(self, path: str) -> None:
        if os.path.islink(path) or not os.path.isdir(path):
            os.unlink(path)
        else:
            self.remove(path)


def _sealed_mode(mode: int) -> int:
    """Read (and traverse, and run what runs) for all, write for root only: the ci user's tests use the cache."""
    bits = stat.S_IMODE(mode)
    if stat.S_ISDIR(mode):
        bits |= 0o555
    elif stat.S_ISREG(mode):
        bits |= 0o444 | (0o111 if bits & 0o111 else 0)
    return bits & ~0o022


def _seal(path: str) -> None:
    """root:root, readable for all and no group or other write, never through a link."""
    for top, dirs, files in os.walk(path):
        for item in ([top] if top == path else []) + [os.path.join(top, name) for name in dirs + files]:
            os.chown(item, 0, 0, follow_symlinks=False)
            mode = os.lstat(item).st_mode
            if not stat.S_ISLNK(mode):
                os.chmod(item, _sealed_mode(mode))


def _owner(path: str) -> str:
    import pwd

    try:
        return pwd.getpwuid(os.lstat(path).st_uid).pw_name
    except (KeyError, OSError):
        return ""


@contextlib.contextmanager
def _flock(path: str):
    import fcntl

    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _on_term(signum, frame):
    if _ARMED[0]:
        _ARMED[0] = False
        raise Terminated()


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] != "dev.check" or not protocol.HEX32.fullmatch(args[1]):
        print("usage: kd-operation-run dev.check <check-id>", file=sys.stderr)
        return 2
    import pwd

    try:
        host, repos = naming.load_host(), repo_sync.load_repos()
        key = os.path.join(pwd.getpwnam(naming.service_user("repo")).pw_dir, ".ssh", "repo_sync_ed25519")
        ci_user, observe = naming.service_user("ci"), naming.service_user("observe")
    except (naming.NamingError, repo_sync.SyncError, KeyError, ValueError) as exc:
        print(f"kd-operation-run: dev.check {args[1]}: {exc}", file=sys.stderr)
        return 1
    env = repo_sync.git_env(os.environ, key=key)
    deps = Deps(cache=os.path.join(WORK, "cache"), units=_capture, own=_own, seal=_seal, owner=_owner, locker=_flock,
                ci_user=ci_user)
    signal.signal(signal.SIGTERM, _on_term)
    try:
        result = run(args[1], state_dir=STATE, forge=lambda token: Forgejo(token, base=host["forge_api"]),
                     git=lambda a: _capture(["git", *a], env=env, timeout=GIT_TIMEOUT), units=_capture,
                     clock=time.monotonic, deps=deps, own=_own, host=host, repos=repos, ci_user=ci_user,
                     observe_group=observe,
                     stop=lambda names: _capture(["systemctl", "stop", "--", *names], timeout=STOP_S // 2))
    except (RunnerError, OSError) as exc:
        print(f"kd-operation-run: dev.check {args[1]}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"check": result["check"], "class": result["class"]}))
    return 0
