"""Worker runner (BOSS spec 5.7, 5.8.6, C05): one worker job of the session host, as root.

The host runs ``kd-worker-run --slot <agent>/workerN`` as a transient unit and writes the job's
definition (what kd-worker submitted to the cabinet) to its stdin. Trusted code prepares what the
job needs and never trusts the definition beyond what the executor wrote: the prompt is rebuilt
from the registry and must hash to the job's provenance, the policy is compiled again from the
agent's installed policy and the registry entry, the working copy is a fresh clone of the host's
read-only copy on a pinned base commit with its own branch and no remote. Then claude runs as the
agent's user in the slot with the isolation arguments of kd.worker_packs; the runner keeps the
stream-json events, writes the result record and prints exactly one line on stdout, the job's end
(``kd.session_host.worker.end_line``): the outcome, the reason, the card's comment, the usage and
the result. The cabinet records it.
"""

from __future__ import annotations

import json
import os
import posixpath
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from kd import (
    agents,
    claude_token,
    context_limit,
    llm_models,
    secret_fd,
    worker_board,
    worker_inputs,
    worker_jobs,
    worker_packs,
    worker_policy,
    worker_result,
    worker_slots,
    workspace,
)
from kd.registry import RegistryError, read_json_file
from kd.session_host import claude as session_claude
from kd.worker_packs import ISOLATION_ARGS
from kd.worker_policy import WORKERS_ROOT, Slot, WorkerPolicyError
from kd.worker_result import OUTPUT_CAP

COPIES = "/srv/digipaddock-repos"  # read-only host copies, kd-repo-sync
FLOOR = "/etc/claude-code/managed-settings.json"
POLICY_FILE = ".claude/policy.json"
CLAUDE = ".local/bin/claude"  # under the agent's home, the install of common/install
PROJECT = worker_jobs.PROJECT
T_GIT = 120
# The definition kd-worker submits (kd.worker_jobs.definition) and the bounds the host's end_line checks.
DEFINITION_FIELDS = frozenset({"id", "card", "agent", "type", "repo", "spec", "skills", "prompt", "limits", "brief",
                               "caller", "requester", "writer", "timeout", "model"})
# the cabinet model of the card and its effort; the files a job of the cabinet reads; the role's session context
# limit as the cabinet sent the job (kd.context_limit)
DEFINITION_OPTIONAL = frozenset({"model_spec", "effort", "inputs", "pull", "context_limit"})
HEAD_MISSING = "head_missing"  # the host's copy has not got the pull request's head (yet): the council waits
DEFINITION_MAX = 48 * 1024  # the definition without its inputs
STDIN_MAX = DEFINITION_MAX + worker_inputs.MAX
TIMEOUT = (60, 14400)
RESULT_MAX = 64 * 1024
COMMENT_MAX = worker_board.TEXT_MAX - 64  # the cabinet signs the comment
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_DIR = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW
_DIR_FD = os.open in os.supports_dir_fd  # Windows (tests only) has no dir_fd


class RunnerError(Exception):
    """Why the job cannot run; becomes the job's failure reason."""


@dataclass(frozen=True)
class Prepared:
    job: dict
    slot: Slot
    user: str
    uid: int
    gid: int
    home: str
    settings_path: str
    system_path: str
    task: str
    base: Optional[str]
    branch: Optional[str]
    model: Optional[str]
    work: str = ""  # the session's cwd: <slot dir>/work
    model_slug: str = ""  # the cabinet model the job runs on ("" — the pool's model on the subscription)
    model_spec: Optional[dict] = None  # its {id, provider, upstream, base_url}
    effort: str = ""
    context_limit: int = 0  # the role's session context limit in tokens (0 — none: Claude Code's own window)


def usage_slug(job: Mapping[str, Any]) -> str:
    """The usage ledger's slug of the job's model: a cabinet model's id; ``""`` for the pool's model and for a model
    of the subscription — their usage is counted under the names Claude Code reports."""
    spec = job.get("model_spec")
    return "" if not spec or llm_models.is_subscription(spec) else spec["id"]


def slot_dir(slot: Slot, root: str = WORKERS_ROOT) -> str:
    """``slot.root`` under another pool root (tests); the policy keeps the real paths."""
    return f"{root}/{slot.agent}/{slot.name}"


def check_definition(obj: Any, slot_ref: str) -> dict:
    """The host's job for the slot it gave: the definition's fields exactly, its id, its timeout and a slot of
    its own agent; the job with ``slot``. The registry, the prompt hashes and the policy are prepare's."""
    try:
        slot = Slot.from_ref(slot_ref)
    except WorkerPolicyError as exc:
        raise RunnerError(f"bad slot: {exc}") from exc
    if not isinstance(obj, dict) or set(obj) - DEFINITION_OPTIONAL != DEFINITION_FIELDS:
        raise RunnerError("a job definition has exactly the fields " + ", ".join(sorted(DEFINITION_FIELDS))
                          + " (and " + ", ".join(sorted(DEFINITION_OPTIONAL)) + ")")
    if not isinstance(obj["id"], str) or not worker_jobs.JOB_ID.fullmatch(obj["id"]):
        raise RunnerError("bad job id")
    own = {key: value for key, value in obj.items() if key != "inputs"}
    if len(json.dumps(own, ensure_ascii=False).encode("utf-8", "replace")) > DEFINITION_MAX:
        raise RunnerError(f"a job definition is over {DEFINITION_MAX} bytes")
    try:
        if "model_spec" in obj:
            llm_models.check_spec(obj["model_spec"])
        if "effort" in obj:
            llm_models.check_effort(obj["effort"])
        if "inputs" in obj:
            worker_inputs.check(obj["inputs"])
        if "pull" in obj:
            worker_jobs.check_pull(obj["pull"])
        if "context_limit" in obj:
            context_limit.check(obj["context_limit"])
    except ValueError as exc:
        raise RunnerError(f"job definition: {exc}") from exc
    if "pull" in obj and not worker_jobs.reads_pull(obj):
        raise RunnerError("only a developer job that writes nothing is on a pull request")
    timeout = obj["timeout"]
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not TIMEOUT[0] <= timeout <= TIMEOUT[1]:
        raise RunnerError(f"timeout must be {TIMEOUT[0]}..{TIMEOUT[1]} seconds")
    if not isinstance(obj["agent"], str) or slot.agent != obj["agent"]:
        raise RunnerError(f"slot {slot.ref} is not a slot of {worker_jobs.clean(obj['agent'], 40)}")
    return {**obj, "slot": slot.ref}


def verify_prompt(job: Mapping[str, Any], packs) -> worker_packs.WorkerPrompt:
    """The prompt rebuilt from the registry; its hashes must be the ones the request recorded."""
    try:
        prompt = worker_packs.build_prompt(
            packs,
            job["spec"],
            tuple(job["skills"]),
            card=job["card"],
            brief=job["brief"],
            agent_type=job["type"],
        )
    except worker_packs.PackError as exc:
        raise RunnerError(f"registry changed: {exc}") from exc
    if prompt.provenance() != job["prompt"]:
        raise RunnerError(
            "registry changed since the request: the specialization or skill texts differ"
        )
    return prompt


def source_copy(project: str, *, copies_root: str = COPIES) -> str:
    """The host's read-only copy of ``project``: a git work tree, never a link."""
    if not isinstance(project, str) or not PROJECT.fullmatch(project):
        raise RunnerError("bad project name")
    path = posixpath.join(copies_root, project)
    if os.path.islink(path) or not os.path.isdir(os.path.join(path, ".git")):
        raise RunnerError(f"{path} is not a git work tree (kd-repo-sync, KD-209)")
    return path


def _empty(dfd: int) -> None:
    with os.scandir(dfd) as entries:
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                child = os.open(entry.name, _DIR, dir_fd=dfd)
                try:
                    _empty(child)
                finally:
                    os.close(child)
                os.rmdir(entry.name, dir_fd=dfd)
            else:
                os.unlink(entry.name, dir_fd=dfd)


def _open_child(dfd: int, name: str, mode: int) -> int:
    """``name`` under ``dfd`` as a fresh, empty directory of ``mode`` opened on a descriptor: whatever the agent
    left there — a link, a file, a directory — goes first, never followed (the folder is the agent's). The mode
    is set, not left to the umask: the runner works under 077, and root's own folder (the job's inputs) would be
    closed to the worker's user."""
    try:
        fd = os.open(name, _DIR, dir_fd=dfd)
    except FileNotFoundError:
        fd = -1
    except OSError:  # ELOOP (a link), ENOTDIR (a file)
        os.unlink(name, dir_fd=dfd)
        fd = -1
    if fd < 0:
        os.mkdir(name, mode, dir_fd=dfd)
        fd = os.open(name, _DIR, dir_fd=dfd)
    try:
        os.fchmod(fd, mode)
        _empty(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd


def fresh_dir(parent: str, name: str, mode: int = 0o755) -> str:
    """``parent/name`` as an empty directory — made, or emptied — through descriptors of ``parent``:
    root never follows what the agent planted under its own work/ (global review 15.10.2026)."""
    path = f"{parent}/{name}"
    try:
        if _DIR_FD:
            pfd = os.open(parent, _DIR)
            try:
                os.close(_open_child(pfd, name, mode))
            finally:
                os.close(pfd)
            return path
        os.makedirs(path, mode, exist_ok=True)  # Windows: tests only
        clear(path)
        return path
    except OSError as exc:
        raise RunnerError(f"{path}: cannot prepare ({exc.__class__.__name__})") from exc


def move_in(parent: str, name: str, source: str) -> None:
    """``source`` (a root-owned directory) becomes ``parent/name``; what was there goes first,
    through descriptors of ``parent`` — a link planted as ``name`` is unlinked, never followed."""
    path = f"{parent}/{name}"
    try:
        if _DIR_FD:
            pfd = os.open(parent, _DIR)
            try:
                try:
                    fd = os.open(name, _DIR, dir_fd=pfd)
                except FileNotFoundError:
                    pass
                except OSError:  # ELOOP (a link), ENOTDIR (a file)
                    os.unlink(name, dir_fd=pfd)
                else:
                    try:
                        _empty(fd)
                    finally:
                        os.close(fd)
                    os.rmdir(name, dir_fd=pfd)
                os.rename(source, name, dst_dir_fd=pfd)
            finally:
                os.close(pfd)
            return
        if os.path.lexists(path):  # Windows: tests only
            shutil.rmtree(path)
        os.replace(source, path)
    except OSError as exc:
        raise RunnerError(f"{path}: cannot move the clone in ({exc.__class__.__name__})") from exc


def clear(path: str) -> None:
    """Empty directory ``path`` and keep it; a link inside is unlinked, never followed."""
    try:
        if _DIR_FD:
            dfd = os.open(path, _DIR)
            try:
                _empty(dfd)
            finally:
                os.close(dfd)
            return
        for entry in os.scandir(path):  # Windows: tests only
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path)
            else:
                os.unlink(entry.path)
    except OSError as exc:
        raise RunnerError(f"{path}: cannot clear ({exc.__class__.__name__})") from exc


def _git(
    run: Callable[..., Any],
    args: list[str],
    *,
    cwd: str = "/",
    env: Optional[dict] = None,
    as_user: Optional[tuple[int, int]] = None,
) -> str:
    drop = {"user": as_user[0], "group": as_user[1]} if as_user else {}
    if as_user and os.geteuid() == 0:  # only root may setgroups; a runner that is not root keeps no other groups
        drop["extra_groups"] = []
    try:
        proc = run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=T_GIT,
            stdin=subprocess.DEVNULL,
            env=env,
            **drop,
        )
    except subprocess.CalledProcessError as exc:
        raise RunnerError(
            f"git: {worker_jobs.clean(exc.stderr or exc.stdout or exc.returncode)}"
        ) from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RunnerError(f"git: {exc.__class__.__name__}") from exc
    return (proc.stdout or "").strip()


def _config_value(value: str) -> str:
    """A git config value as a double-quoted string (backslashes and quotes escaped)."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def trust_file(slot_root: str) -> str:
    """Path of the slot's root-owned git config naming the repositories root may touch: the source copy and the
    fresh clone before the job, never the job's copy after it (``_agent_git``)."""
    return f"{slot_root}/gitconfig"


def git_env(slot_root: str) -> dict[str, str]:
    """The runner's environment for git: the host's plus the slot's trust file as global config.
    Root runs git on the repo user's copy and on the fresh clone it stages: without safe.directory git refuses
    them («dubious ownership»). The agent's work/repo is never in it — after the job git runs there as the agent
    (``_agent_git``); git's local transport strips GIT_CONFIG_PARAMETERS
    from its upload-pack, so ``-c`` never reaches a clone — GIT_CONFIG_GLOBAL does."""
    return {**os.environ, "GIT_CONFIG_GLOBAL": trust_file(slot_root)}


def write_trust(slot_root: str, paths: list[str]) -> None:
    lines = "".join(f"\tdirectory = {_config_value(path)}\n" for path in paths)
    root_file(trust_file(slot_root), ("[safe]\n" + lines).encode(), 0o644)


def prepare_repo(
    slot: Slot,
    source: str,
    *,
    job_id: str,
    run: Callable[..., Any],
    chown: Callable[[str, int, int], None],
    root: str = WORKERS_ROOT,
    uid: int = 0,
    gid: int = 0,
    pull: Optional[Mapping[str, Any]] = None,
) -> tuple[str, str]:
    """A fresh local clone of ``source`` on its HEAD in the slot's work/repo: no remote, its own
    branch worker/<job id>, a commit identity; then owned by the agent. Returns (base sha, branch).
    Every git call carries the slot's trust file (``git_env``). With ``pull`` the clone is on the pull
    request's head instead: branch ``pr-head`` checked out, ``pr-base`` on the target's tip — (that tip, "pr-head");
    a copy without this head or target is RunnerError ``head_missing: …``."""
    slot_root = slot_dir(slot, root)
    work = f"{slot_root}/work"
    # the clone is made and handed to the agent in a staging folder of the root-owned slot root, then moved
    # into the agent's work/ through descriptors: between two steps the agent could swap work/repo for a
    # link, and root must neither clone through it nor chown what it points at (global review 15.10.2026)
    staging = f"{slot_root}/repo.{job_id}"
    try:
        os.makedirs(work, 0o700, exist_ok=True)  # a host has it (worker_slots.sh); tests build the slot here
        if os.path.lexists(staging):
            shutil.rmtree(staging)
        os.mkdir(staging, 0o755)
    except OSError as exc:
        raise RunnerError(f"{staging}: cannot prepare ({exc.__class__.__name__})") from exc
    write_trust(slot_root, [source, f"{source}/.git", staging])
    env = git_env(slot_root)
    _git(run, ["clone", "--quiet", "--no-hardlinks", source, staging], env=env)
    if pull is None:
        base, at = _git(run, ["rev-parse", "HEAD"], cwd=staging, env=env), "HEAD"
        branch = f"worker/{job_id}"
    else:
        base, at = _pull_refs(run, source, staging, pull, env)
        branch = "pr-head"
        _git(run, ["branch", "-q", "pr-base", base], cwd=staging, env=env)
    _git(run, ["remote", "remove", "origin"], cwd=staging, env=env)
    _git(run, ["checkout", "-q", "-b", branch, at], cwd=staging, env=env)
    _git(run, ["config", "user.name", "worker"], cwd=staging, env=env)
    _git(run, ["config", "user.email", "worker@localhost"], cwd=staging, env=env)
    chown(staging, uid, gid)
    move_in(work, "repo", staging)
    return base, branch


def _pull_refs(run: Callable[..., Any], source: str, staging: str, pull: Mapping[str, Any],
               env: dict[str, str]) -> tuple[str, str]:
    """(the target's tip, the pull request's head) fetched from the host's copy into the clone. The copy holds
    the forge's branches as ``refs/remotes/origin/*`` and the heads as ``refs/pull/<n>/head`` (kd-repo-sync); a
    ref it has not got, or a head other than the job's, is ``head_missing`` — the next sync may bring it."""

    def fetched(ref: str) -> str:
        try:
            _git(run, ["fetch", "--quiet", "--no-tags", source, ref], cwd=staging, env=env)
        except RunnerError as exc:
            raise RunnerError(f"{HEAD_MISSING}: the copy has no {ref}") from exc
        return _git(run, ["rev-parse", "FETCH_HEAD"], cwd=staging, env=env)

    base = fetched(f"refs/remotes/origin/{pull['target']}")
    head = fetched(f"refs/pull/{pull['number']}/head")
    if head != pull["head"]:
        raise RunnerError(f"{HEAD_MISSING}: pull {pull['number']} is at {head[:12]} in the copy, "
                          f"not at {pull['head'][:12]}")
    return base, head


def chown_tree(path: str, uid: int, gid: int) -> None:
    """Every entry under ``path`` to the agent — by descriptors of each folder, links themselves and
    never their targets; a link in place of ``path`` is refused, not walked (global review 15.10.2026)."""
    try:
        if not _DIR_FD:  # Windows: tests only
            os.chown(path, uid, gid, follow_symlinks=False)
            for folder, dirs, files in os.walk(path):
                for name in dirs + files:
                    os.chown(os.path.join(folder, name), uid, gid, follow_symlinks=False)
            return
        top = os.open(path, _DIR)  # ELOOP when a link stands where the folder should be
        try:
            for _folder, dirs, files, dfd in os.fwalk(".", dir_fd=top, follow_symlinks=False):
                os.fchown(dfd, uid, gid)
                for name in dirs + files:
                    os.chown(name, uid, gid, dir_fd=dfd, follow_symlinks=False)
        finally:
            os.close(top)
    except OSError as exc:
        raise RunnerError(f"{path}: cannot hand over ({exc.__class__.__name__})") from exc


def root_file(path: str, data: bytes, mode: int, uid: int = -1, gid: int = -1) -> None:
    """A regular file of ``data`` written whole and renamed into place, owned by uid:gid (-1:
    root's own). Every directory on the way is opened without following links: the patch lands
    in the agent's work/tmp and the record in the base user's results folder — a link planted
    there must not steer where root writes or what it hands over."""
    if _DIR_FD:
        try:
            worker_slots.write_file(path, data, mode, uid, gid)
        except worker_slots.SlotError as exc:
            raise RunnerError(str(exc)) from exc
        return
    # Windows: tests only (no dir_fd, no owners)
    fd, temp = tempfile.mkstemp(prefix=".", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def agent_dir(path: str, uid: int, gid: int) -> None:
    """A folder of the agent's own (0750, uid:gid), made when missing; never through a link (the card's
    temp folder a worker's patch goes to)."""
    if _DIR_FD:
        try:
            worker_slots._ensure_dir(path, uid, gid, 0o750)
        except (worker_slots.SlotError, OSError) as exc:
            raise RunnerError(str(exc)) from exc
        return
    os.makedirs(path, exist_ok=True)  # Windows: tests only (no dir_fd, no owners)


def write_slot_files(
    slot: Slot,
    policy: Mapping[str, Any],
    prompt: worker_packs.WorkerPrompt,
    *,
    root: str = WORKERS_ROOT,
    write: Callable[[str, bytes, int], None] = root_file,
) -> tuple[str, str]:
    """The worker's ``--settings`` file (policy settings plus launch settings) and its system
    prompt, root-owned in the slot root, outside work/ (the sandbox never binds them writable)."""
    base = slot_dir(slot, root)
    settings = f"{base}/settings.json"
    system = f"{base}/system.md"
    merged = {**policy["settings"], **policy["launch_settings"]}
    write(
        settings,
        json.dumps(merged, ensure_ascii=False, indent=2, sort_keys=True).encode()
        + b"\n",
        0o644,
    )
    write(system, prompt.system.encode(), 0o644)
    return settings, system


def check_work(
    work: str, uid: int, *, look: worker_slots.Look = worker_slots.probe
) -> None:
    """work/ must be the agent's private directory, as kd.launch demands of a persistent session;
    the installer made it so and nothing of the agent's can change that — root checks anyway."""
    entry = look(work)
    if entry is None or entry.kind != "dir" or entry.uid != uid or entry.mode != 0o700:
        raise RunnerError(
            f"{work} is not the agent's private directory (worker_slots.sh --install)"
        )


def restore_placeholders(work: str) -> None:
    """work/CLAUDE.md and work/CLAUDE.local.md root-owned again before every job: the folder is
    the agent's, so the last job could have left its own instructions there."""
    for name in worker_slots.WORK_PLACEHOLDERS:
        text = worker_slots.PLACEHOLDER if name == "CLAUDE.md" else ""
        root_file(f"{work}/{name}", text.encode(), 0o644)


def _policy(path: str) -> dict[str, Any]:
    try:
        data = read_json_file(path)
    except RegistryError as exc:
        raise RunnerError(f"policy {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RunnerError(f"policy {path}: not an object")
    return data


def prepare(
    job: dict,
    *,
    config_path=worker_slots.CONFIG,
    registry_path: str = agents.REGISTRY,
    packs_root=worker_packs.PACKS_ROOT,
    agents_root: str,
    floor_path: str = FLOOR,
    policy_root: str,
    copies_root: str = COPIES,
    slots_root: str = WORKERS_ROOT,
    require_root_owner: bool = True,
    run: Callable[..., Any] = subprocess.run,
    chown: Callable[[str, int, int], None] = chown_tree,
    getpw: Callable[[str], Any],
    login: Callable[[str, int, str], list[str]] = worker_slots.credential_problems,
    look: worker_slots.Look = worker_slots.probe,
) -> Prepared:
    """Everything one job (``check_definition``) needs in its slot, or RunnerError with the reason it cannot run.

    ``login(home, uid, agent)`` lists the problems of the agent's Claude Code login (by
    metadata only); the check is the runner's because the base-user executor cannot see the
    agent's home. ``look`` is the lstat of the slot's work/ (tests)."""
    try:
        config = worker_slots.load_config(config_path)
        user = worker_slots.agent_user(config, job["agent"], registry_path)
    except worker_slots.SlotError as exc:
        raise RunnerError(str(exc)) from exc
    try:
        account = getpw(user)
    except KeyError as exc:
        raise RunnerError(f"user {user} of {job['agent']} does not exist") from exc
    problems = login(account.pw_dir, account.pw_uid, job["agent"])
    if problems:
        raise RunnerError(f"login of {job['agent']}: {'; '.join(problems)}")
    try:
        slot = Slot.from_ref(job["slot"])
    except WorkerPolicyError as exc:
        raise RunnerError(str(exc)) from exc
    if slot.agent != job["agent"]:
        raise RunnerError(f"slot {slot.ref} is not a slot of {job['agent']}")
    work = f"{slot_dir(slot, slots_root)}/work"
    check_work(work, account.pw_uid, look=look)
    os.makedirs(work, 0o700, exist_ok=True)  # a host has it (checked above by lstat); tests build the slot here
    try:
        packs = worker_packs.load_packs(
            packs_root, require_root_owner=require_root_owner
        )
    except worker_packs.PackError as exc:
        raise RunnerError(f"specialization registry: {exc}") from exc
    prompt = verify_prompt(job, packs)
    policy = _policy(f"{policy_root}/{job['agent']}/{POLICY_FILE}")
    floor = _policy(floor_path)
    try:
        worker = worker_policy.compile_worker_policy(
            policy,
            packs.specs[job["spec"]],
            slot,
            agents_root=agents_root,
            floor=floor,
            other_agents=[a for a in config.agents if a != job["agent"]],
        )
    except WorkerPolicyError as exc:
        raise RunnerError(f"worker policy: {exc}") from exc
    if worker["user"] != user:
        raise RunnerError(f"policy user {worker['user']} is not {user}")
    base = branch = None
    if job["type"] == "developer":
        source = source_copy(job["repo"], copies_root=copies_root)
        base, branch = prepare_repo(
            slot,
            source,
            job_id=job["id"],
            run=run,
            chown=chown,
            root=slots_root,
            uid=account.pw_uid,
            gid=account.pw_gid,
            pull=job.get("pull"),
        )
    tmp = fresh_dir(work, "tmp")
    chown(tmp, account.pw_uid, account.pw_gid)
    # what the job reads instead of the board — root's files in root's folder, emptied for every job, so
    # no job reads the last one's card and none rewrites its own
    inputs = fresh_dir(work, worker_inputs.DIR)
    for name, text in sorted((job.get("inputs") or {}).items()):
        root_file(f"{inputs}/{name}", text.encode("utf-8"), 0o644)
    restore_placeholders(work)
    settings_path, system_path = write_slot_files(slot, worker, prompt, root=slots_root)
    spec = job.get("model_spec")  # the card's cabinet model beats the pool's
    return Prepared(
        job=job,
        slot=slot,
        user=user,
        uid=account.pw_uid,
        gid=account.pw_gid,
        home=account.pw_dir,
        settings_path=settings_path,
        system_path=system_path,
        task=prompt.task,
        base=base,
        branch=branch,
        model=spec["upstream"] if spec else config.model,
        work=work,
        model_slug=usage_slug(job),
        model_spec=dict(spec) if spec else None,
        effort=job.get("effort", ""),
        context_limit=job.get("context_limit", 0),
    )


# --- part 2: launch, facts, record, settle ----------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    exit: Optional[int]
    timed_out: bool
    result_event: Optional[dict]
    output_bytes: int
    started: float
    ended: float
    auth_failed: bool = False  # Claude refused the credentials (an assistant event's error)


def argv(p: Prepared) -> list[str]:
    """claude in print mode with the slot's system prompt and settings, every setting source
    and skill discovery off (kd.worker_packs.ISOLATION_ARGS), one stream of JSON events."""
    out = [
        f"{p.home}/{CLAUDE}",
        "-p",
        p.task,
        "--system-prompt-file",
        p.system_path,
        "--settings",
        p.settings_path,
        *ISOLATION_ARGS,
        "--no-session-persistence",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    if p.model:
        out += ["--model", p.model]
    if p.effort:
        out += ["--effort", p.effort]
    return out


def _credentials(p: Prepared) -> dict[str, str]:
    """the host's Claude token, read here as root, is the job's only credential. a job on a
    cabinet model gets the kind's key and the kind's non-secret variables instead, never the Claude token; a key
    file gone since the request is the job's failure, not a session without credentials. a job on a model
    of the subscription keeps the host's token — its spec only names the alias."""
    if not p.model_spec or llm_models.is_subscription(p.model_spec):
        return claude_token.env()
    secret = llm_models.read_secret(p.model_spec["provider"])
    if not secret:
        raise RunnerError(f"no_key: {p.model_spec['provider']}")
    return {**secret, **llm_models.environment(p.model_spec)}


def env(p: Prepared) -> dict[str, str]:
    """The session's whole environment: nothing of the unit's reaches claude; the slot and
    the job are named for the agent's Bash guard (slot mode) and the typing hook. The
    credential (``_credentials``) is here as a variable; ``launch`` moves it into a pipe
    claude reads at start (``kd.secret_fd``), so it is never in claude's environ. The role's context limit is
    Claude Code's auto-compact window (``kd.context_limit``)."""
    return {
        **_credentials(p),
        **context_limit.environment(p.context_limit),
        "PATH": f"{p.home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": p.home,
        "USER": p.user,
        "LOGNAME": p.user,
        "LANG": "C.UTF-8",
        "KD_AGENT": p.slot.agent,
        "KD_WORKER_JOB": p.job["id"],
        "KD_WORKER_SLOT": p.slot.root,
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_ENABLE_AWAY_SUMMARY": "0",
        "CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION": "false",
        "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",  # the job ends with the reply: nothing may outlive it
    }


def _private(path: str, mode: int = 0o600) -> int:
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, mode)


def _kill(proc: Any) -> None:
    pid = getattr(proc, "pid", None)
    try:
        if pid and hasattr(os, "killpg"):
            os.killpg(
                pid, signal.SIGKILL
            )  # the whole session: claude and what it spawned
        else:
            proc.kill()
    except ProcessLookupError:
        pass


def launch(
    p: Prepared,
    *,
    popen: Callable[..., Any],
    events_path: str,
    stderr_path: str,
    timeout: float,
    now: Callable[[], float] = time.time,
) -> Outcome:
    """claude as the agent's user in the slot's work/: the JSON events go to ``events_path``
    (root only), stderr to ``stderr_path``; the result event is kept with its text capped;
    ``timeout`` seconds end the session with SIGKILL. An assistant event with the error
    authentication_failed (as kd.session_host.claude.Normalizer reads it) marks the outcome."""
    started = now()
    result_event: Optional[dict] = None
    total = 0
    timed_out = False
    auth_failed = False
    events = os.fdopen(_private(events_path), "wb")
    errors = _private(stderr_path)
    secret_fds: list[int] = []
    try:
        # the token (or an API key) reaches claude by a pipe, not in its environ
        child_env, secret_fds = secret_fd.move(env(p))
        proc = popen(
            argv(p),
            cwd=p.work,
            env=child_env,
            pass_fds=tuple(secret_fds),
            user=p.uid,
            group=p.gid,
            extra_groups=[],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=errors,
            start_new_session=True,
        )
    finally:
        os.close(errors)
        for fd in secret_fds:
            os.close(fd)

    def on_timeout() -> None:
        nonlocal timed_out
        timed_out = True
        _kill(proc)

    deadline = time.monotonic() + timeout
    timer = threading.Timer(timeout, on_timeout)
    timer.daemon = True
    timer.start()
    try:
        for raw in proc.stdout:
            total += len(raw)
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            events.write(raw.rstrip(b"\r\n") + b"\n")
            if event.get("type") == "assistant" and event.get("error") == "authentication_failed":
                auth_failed = True
            if event.get("type") == "result":
                text = event.get("result")
                if isinstance(text, str) and len(text) > OUTPUT_CAP:
                    event = {**event, "result": text[:OUTPUT_CAP]}
                result_event = event
        try:
            rc = proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill(proc)
            rc = proc.wait()
    finally:
        timer.cancel()
        events.close()
    return Outcome(rc, timed_out, result_event, total, started, now(), auth_failed)


# After the job the copy is the job's writing, .git included: its config, info/attributes, commondir. git run
# there by root would run what that config names (diff.external, a textconv driver, fsmonitor) as root, so
# every read of it after the session runs as the agent, with no system or global config (no slot trust file:
# the copy is the agent's own), and the diff takes no external diff and no text conversion (review 04.11.2026).
AGENT_GIT_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "HOME": "/nonexistent",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
}
DIFF_PLAIN = ["diff", "--no-ext-diff", "--no-textconv"]


def _agent_git(p: Prepared, run: Callable[..., Any], args: list[str]) -> str:
    """git in the job's copy as the agent (``p.uid``/``p.gid``), never as root."""
    return _git(run, args, cwd=f"{p.work}/repo", env=dict(AGENT_GIT_ENV), as_user=(p.uid, p.gid))


def repo_facts(p: Prepared, *, run: Callable[..., Any]) -> tuple[Optional[str], str]:
    """(HEAD, diff --stat against the base) of a writer's copy; (None, "") for the rest."""
    if p.base is None or p.job["limits"].get("write") != "repo":
        return None, ""
    head = _agent_git(p, run, ["rev-parse", "HEAD"])
    stat = _agent_git(p, run, [*DIFF_PLAIN, "--stat", f"{p.base}..HEAD"])
    return head, stat


def hand_over_patch(
    p: Prepared, *, run: Callable[..., Any], agent_tmp: str
) -> Optional[str]:
    """The worker's commits as one patch in the temp folder of the job's card in the agent's work/tmp
    (``cards/<card>/worker-<id>.patch`` — the card's session finds it beside its own files,
    another card's session never), owned by the agent; the persistent agent applies it in its own
    clone and opens the PR. None when there is nothing to hand over. The folders are the agent's:
    neither they nor root_file follow a link on the way."""
    if p.base is None or p.job["limits"].get("write") != "repo":
        return None
    patch = _agent_git(p, run, [*DIFF_PLAIN, "--binary", f"{p.base}..HEAD"])
    if not patch:
        return None
    card = p.job["card"]
    if not workspace.CARD.fullmatch(card):
        raise RunnerError(f"the job's card is not a card ID: {card!r}")
    folder = agent_tmp
    for name in ("cards", card):
        folder = f"{folder}/{name}"
        agent_dir(folder, p.uid, p.gid)
    path = f"{folder}/worker-{p.job['id']}.patch"
    root_file(path, patch.encode() + b"\n", 0o644, p.uid, p.gid)
    return path


def end_line(record: Mapping[str, Any], event: Optional[dict], slug: str = "") -> dict:
    """The job's end for the session host: the outcome of the record, its Russian comment, the session's usage per
    model (Claude's ``modelUsage``, as for a hosted turn; under ``slug`` when the job ran on a cabinet model)
    and the specialization's final block. A block the cabinet cannot keep fails the job."""
    result = record.get("result")
    if result is not None and len(json.dumps(result, ensure_ascii=False).encode()) > RESULT_MAX:
        # the comment says what the state says: the block is not handed over
        record = {**record, "result": None, "reason": f"the final block is over {RESULT_MAX // 1024} KiB"}
    state, reason = worker_result.outcome_of(record)
    result = record.get("result")
    used = session_claude.model_usage(event, slug) if isinstance(event, dict) else {"models": {}, "requests": 0}
    return {
        "type": "job",
        "phase": "ended",
        "state": state,
        "reason": worker_jobs.clean(reason, 500),
        "comment": worker_result.comment(record)[:COMMENT_MAX],
        "usage": used,
        "result": result,
    }


def failed_end(reason: str) -> dict:
    """The end of a job the runner could not even read."""
    return {"type": "job", "phase": "ended", "state": "failed", "reason": worker_jobs.clean(reason, 500),
            "comment": "", "usage": {"models": {}, "requests": 0}, "result": None}


def run(
    job: dict,
    *,
    config_path=worker_slots.CONFIG,
    registry_path: str = agents.REGISTRY,
    packs_root=worker_packs.PACKS_ROOT,
    agents_root: str,
    policy_root: str,
    floor_path: str = FLOOR,
    copies_root: str = COPIES,
    slots_root: str = WORKERS_ROOT,
    results_root: str = worker_slots.RESULTS,
    events_root: str = worker_slots.EVENTS,
    agent_tmp: Optional[str] = None,
    require_root_owner: bool = True,
    run: Callable[..., Any] = subprocess.run,
    chown: Callable[[str, int, int], None] = chown_tree,
    getpw: Callable[[str], Any],
    login: Callable[[str, int, str], list[str]] = worker_slots.credential_problems,
    look: worker_slots.Look = worker_slots.probe,
    popen: Callable[..., Any] = subprocess.Popen,
    now: Callable[[], float] = time.time,
) -> tuple[int, dict]:
    """The job (``check_definition``): prepare, launch, record; returns the exit code and the end line. Every
    failure of the runner itself becomes a failed result record; only a record that cannot be written returns 1
    (the end line still says what happened)."""
    job_id = job["id"]
    started = now()
    prepared: Optional[Prepared] = None
    outcome: dict[str, Any] = {
        "exit": None,
        "timed_out": False,
        "result_event": None,
        "output_bytes": 0,
    }
    head, stat, patch = None, "", None
    try:
        prepared = prepare(
            job,
            config_path=config_path,
            registry_path=registry_path,
            packs_root=packs_root,
            agents_root=agents_root,
            floor_path=floor_path,
            policy_root=policy_root,
            copies_root=copies_root,
            slots_root=slots_root,
            require_root_owner=require_root_owner,
            run=run,
            chown=chown,
            getpw=getpw,
            login=login,
            look=look,
        )
        os.makedirs(events_root, 0o700, exist_ok=True)
        done = launch(
            prepared,
            popen=popen,
            events_path=f"{events_root}/{job_id}.jsonl",
            stderr_path=f"{events_root}/{job_id}.stderr",
            timeout=float(job["timeout"]),
            now=now,
        )
        outcome = {
            "exit": done.exit,
            "timed_out": done.timed_out,
            "result_event": done.result_event,
            "output_bytes": done.output_bytes,
            "auth_failed": done.auth_failed,
        }
        head, stat = repo_facts(prepared, run=run)
        patch = hand_over_patch(
            prepared,
            run=run,
            agent_tmp=agent_tmp or f"{policy_root}/{job['agent']}/work/tmp",
        )
    except RunnerError as exc:
        outcome["reason"] = str(exc)
    except Exception as exc:  # noqa: BLE001 — the record says the runner broke; the job never hangs
        outcome["reason"] = f"internal: {exc.__class__.__name__}"
    facts = {
        "base": prepared.base if prepared else None,
        "branch": prepared.branch if prepared else None,
        "slot": job["slot"],
        "started": started,
        "ended": now(),
    }
    record = worker_result.build(job, facts, outcome, head=head, stat=stat, patch=patch)
    end = end_line(record, outcome["result_event"], usage_slug(job))
    try:
        os.makedirs(results_root, 0o700, exist_ok=True)
        root_file(
            f"{results_root}/{job_id}.json",
            json.dumps(record, ensure_ascii=False, sort_keys=True).encode() + b"\n",
            0o644,
        )
    except (OSError, RunnerError) as exc:
        print(f"kd-worker-run: result record of {job_id}: {exc}", file=sys.stderr)
        return 1, end
    return 0, end


def main(argv_: list[str], *, stdin=None, out=None, **options) -> int:
    """kd-worker-run --slot <agent>/workerN: the job's definition on stdin (the session host), its end on stdout,
    exactly one line. ``options`` are run's (tests); the host's defaults otherwise, as root."""
    stdin = stdin if stdin is not None else sys.stdin.buffer
    out = out if out is not None else sys.stdout
    try:
        if len(argv_) != 2 or argv_[0] != "--slot":
            raise WorkerPolicyError("one --slot")
        Slot.from_ref(argv_[1])
    except WorkerPolicyError:
        print("usage: kd-worker-run --slot <agent>/workerN (the job's definition on stdin)", file=sys.stderr)
        return 2
    raw = stdin.read(STDIN_MAX + 1)
    try:
        if len(raw) > STDIN_MAX:
            raise RunnerError(f"over {STDIN_MAX} bytes")
        job = check_definition(json.loads(raw.decode("utf-8")), argv_[1])
    except (UnicodeDecodeError, ValueError, RunnerError) as exc:
        code, end = 1, failed_end(f"bad job definition: {exc}")
    else:
        if "agents_root" not in options:
            from kd import launch as kd_launch

            options["agents_root"] = options["policy_root"] = kd_launch.configured_agents_root()
        if "getpw" not in options:
            import pwd

            options["getpw"] = pwd.getpwnam
        code, end = run(job, **options)
    out.write(json.dumps(end, ensure_ascii=False) + "\n")
    out.flush()
    return code
