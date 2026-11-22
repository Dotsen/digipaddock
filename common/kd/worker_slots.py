"""Worker slots (BOSS spec 5.7, 5.8.6, C03): the finite pool of folders where worker jobs run.

A slot is /srv/digipaddock-workers/<agent>/workerN laid out like an agent folder: work/ is the
job's cwd (the agent's, so the sandbox can make its bind targets there), work/repo its own copy
of the repository, work/tmp its temp files; work/CLAUDE.md and work/CLAUDE.local.md are
root-owned placeholders. A slot has no user, home, login, sudoers line or agents.json entry: jobs run as the permanent agent that owns the pool, with its one Claude
Code login (spec 5.8.1).

The enabled file lists the slots kd-worker and the session host may use, one ``agent/workerN`` per
line. ``install`` writes it only when the layout and every pool owner's login are in place;
``disable`` empties it and keeps every folder. The session host runs the jobs; the
C04 job table and journal stay on the host as an archive.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Optional, Sequence

from kd import agents, claude_token, manifest, naming
from kd.policy import WORK_PLACEHOLDERS
from kd.worker_policy import SLOT_RE, WORKERS_ROOT, Slot, WorkerPolicyError

CONFIG = Path(__file__).resolve().parents[1] / "worker" / "worker-slots.json"
ENABLED = "/etc/digipaddock/worker-slots.enabled"
# The runner (root) writes result records the base user's kd-worker reads, and the session events.
STATE_DIR = "/var/lib/digipaddock/worker"
RESULTS = STATE_DIR + "/results"
AUDIT_DIR = "/var/log/digipaddock-worker"
EVENTS = AUDIT_DIR + "/events"
SCHEMA_VERSION = 3  # 3: model of worker sessions (C05)
MAX_SLOTS = 2  # per agent
CREDENTIALS = ".claude/.credentials.json"
PLACEHOLDER = (
    "Root-owned placeholder: a worker job takes its instructions from its system prompt,"
    " not from this folder.\n"
)
SMALL = 4096
_FIELDS = frozenset({"schema_version", "requester", "model", "agents", "limits"})
_LIMITS = {
    "jobs_per_day": (1, 50),
    "attempts_per_card_spec": (1, 5),
    "timeout_minutes": (5, 240),
}
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


class SlotError(ValueError):
    pass


@dataclass(frozen=True)
class SlotConfig:
    requester: str
    agents: tuple[str, ...]
    slots: tuple[Slot, ...]
    jobs_per_day: int
    attempts_per_card_spec: int
    timeout_minutes: int
    model: Optional[str] = None  # the model of every worker session on this host; None: claude's default

    def slots_of(self, agent: str) -> tuple[Slot, ...]:
        return tuple(slot for slot in self.slots if slot.agent == agent)


@dataclass(frozen=True)
class Want:
    path: str
    kind: str  # "dir" or "file"
    owner: str  # "root", "base" or an agent name
    mode: int
    content: Optional[bytes] = None


@dataclass(frozen=True)
class Entry:
    kind: str  # "dir", "file", "link" or "other"
    uid: int
    mode: int
    size: int


Look = Callable[[str], Optional[Entry]]


def load_config(path=CONFIG) -> SlotConfig:
    try:
        data = json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise SlotError(f"worker-slots.json unreadable: {exc.__class__.__name__}") from exc
    if not isinstance(data, dict) or set(data) != _FIELDS:
        raise SlotError("worker-slots.json: fields must be " + ", ".join(sorted(_FIELDS)))
    if data["schema_version"] != SCHEMA_VERSION:
        raise SlotError(f"worker-slots.json: schema_version must be {SCHEMA_VERSION}")
    requester = data["requester"]
    if not isinstance(requester, str) or not agents.NAME_RE.fullmatch(requester):
        raise SlotError("worker-slots.json: bad requester")
    model = data["model"]
    if model is not None:
        try:
            manifest.check_model(model)
        except manifest.ManifestError as exc:
            raise SlotError(f"worker-slots.json: {exc}") from exc
    pools = data["agents"]
    if not isinstance(pools, dict) or not pools:
        raise SlotError("worker-slots.json: agents must be a non-empty object agent -> slots")
    slots: list[Slot] = []
    for agent, names in pools.items():
        if not isinstance(agent, str) or not agents.NAME_RE.fullmatch(agent) or agent == requester:
            raise SlotError(f"worker-slots.json: bad agent {agent!r}")
        if (
            not isinstance(names, list)
            or not 1 <= len(names) <= MAX_SLOTS
            or not all(isinstance(s, str) and SLOT_RE.fullmatch(s) for s in names)
            or len(set(names)) != len(names)
        ):
            raise SlotError(
                f"worker-slots.json: {agent}: slots must be 1..{MAX_SLOTS} distinct names worker1..worker9"
            )
        try:
            slots += [Slot.parse(agent, name) for name in names]
        except WorkerPolicyError as exc:
            raise SlotError(f"worker-slots.json: {exc}") from exc
    limits = data["limits"]
    if not isinstance(limits, dict) or set(limits) != set(_LIMITS):
        raise SlotError("worker-slots.json: limits must be " + ", ".join(sorted(_LIMITS)))
    for name, (low, high) in _LIMITS.items():
        value = limits[name]
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            raise SlotError(f"worker-slots.json: {name} must be {low}..{high}")
    return SlotConfig(requester, tuple(pools), tuple(slots), model=model, **limits)


def agent_user(config: SlotConfig, agent: str, registry=agents.REGISTRY) -> str:
    """The pool owner's Linux user from an installed generated agent, never a default."""
    if agent not in config.agents:
        raise SlotError(f"{agent}: no slots are registered for this agent")
    try:
        entry = agents._load(registry).get(agent)
    except ValueError as exc:
        raise SlotError(str(exc)) from exc
    if (
        not isinstance(entry, dict)
        or entry.get("runtime") != agents.GENERATED_RUNTIME
        or not isinstance(entry.get("user"), str)
    ):
        raise SlotError(f"{agent} is not an installed agent (new_agent.sh --install {agent})")
    return entry["user"]


def layout(config: SlotConfig, root: str = WORKERS_ROOT) -> list[Want]:
    """Every folder and placeholder of the pools, parents first."""
    wants = [Want(root, "dir", "root", 0o755)]
    for agent in config.agents:
        wants.append(Want(f"{root}/{agent}", "dir", "root", 0o755))
        for slot in config.slots_of(agent):
            base = f"{root}/{agent}/{slot.name}"
            work = f"{base}/work"
            wants += [
                Want(base, "dir", "root", 0o755),
                Want(work, "dir", agent, 0o700),
                Want(f"{work}/repo", "dir", agent, 0o700),
                Want(f"{work}/tmp", "dir", agent, 0o700),
            ]
            for name in WORK_PLACEHOLDERS:
                text = PLACEHOLDER if name == "CLAUDE.md" else ""
                wants.append(Want(f"{work}/{name}", "file", "root", 0o644, text.encode()))
    return wants


def probe(path: str) -> Optional[Entry]:
    """lstat only: a link is reported as a link, never followed."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    mode = info.st_mode
    if stat.S_ISLNK(mode):
        kind = "link"
    elif stat.S_ISDIR(mode):
        kind = "dir"
    elif stat.S_ISREG(mode):
        kind = "file"
    else:
        kind = "other"
    return Entry(kind, info.st_uid, stat.S_IMODE(mode), info.st_size)


def read_small(path: str) -> Optional[bytes]:
    """Up to SMALL + 1 bytes of a regular file, never through a link or into a FIFO."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | _NOFOLLOW)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return os.read(fd, SMALL + 1)
    finally:
        os.close(fd)


def _listing(listdir: Callable[[str], list], path: str) -> list[str]:
    try:
        return sorted(listdir(path))
    except FileNotFoundError:
        return []


def problems(
    config: SlotConfig,
    uids: Mapping[str, int],
    *,
    root: str = WORKERS_ROOT,
    look: Look = probe,
    read: Callable[[str], Optional[bytes]] = read_small,
    listdir: Callable[[str], list] = os.listdir,
) -> list[str]:
    """Every difference between the pools on disk and their registered layout."""
    found = []
    for want in layout(config, root):
        entry = look(want.path)
        if entry is None:
            found.append(f"{want.path}: missing")
            continue
        if entry.kind != want.kind:
            noun = "directory" if want.kind == "dir" else "regular file"
            found.append(f"{want.path}: not a {noun}")
            continue
        if entry.uid != uids[want.owner]:
            found.append(f"{want.path}: owner is not {want.owner}")
        if entry.mode != want.mode:
            found.append(f"{want.path}: mode {entry.mode:o}, expected {want.mode:o}")
        if want.content is not None and read(want.path) != want.content:
            found.append(f"{want.path}: placeholder changed")
    for slot in config.slots:
        git = look(f"{root}/{slot.agent}/{slot.name}/work/repo/.git")
        if git is not None and git.kind != "dir":
            found.append(
                f"{root}/{slot.agent}/{slot.name}/work/repo/.git: not a directory (a linked or shared checkout)"
            )
    found += [f"{root}/{name}: not a registered pool" for name in _listing(listdir, root) if name not in config.agents]
    for agent in config.agents:
        names = {slot.name for slot in config.slots_of(agent)}
        found += [
            f"{root}/{agent}/{name}: not a registered slot"
            for name in _listing(listdir, f"{root}/{agent}")
            if name not in names
        ]
    return found


def credential_problems(
    home: str, uid: int, agent: str, *, look: Look = probe, token_problem: Callable[[], str] = claude_token.problem
) -> list[str]:
    """Metadata of the pool owner's Claude Code login; that file is never opened. the host's token (a root
    file, mode 600, one bare value — ``claude_token.problem``, read as root) is the login of every job; without it,
    the user's own. A token file that is there but broken is named: it would still reach the job."""
    token = look(claude_token.PATH)
    if token is not None:
        again = f"put it again: {naming.command('kd-secret')} put claude-token"
        if not (token.kind == "file" and token.uid == 0 and not token.mode & 0o077 and token.size):
            return [f"{claude_token.PATH}: not a regular root file with mode 600 — {again}"]
        reason = token_problem()
        return [f"{claude_token.PATH}: {reason} — {again}"] if reason else []
    path = f"{home}/{CREDENTIALS}"
    entry = look(path)
    if entry is None:
        user = home.rstrip("/").rsplit("/", 1)[-1]
        return [f"{path}: missing; put the host's Claude token ({naming.command('kd-secret')} put claude-token) or log in as {agent}"
                f" (sudo -iu {user} env DISABLE_AUTOUPDATER=1 claude, then /login)"]
    if entry.kind != "file":
        return [f"{path}: not a regular file"]
    found = []
    if entry.uid != uid:
        found.append(f"{path}: not owned by {agent}")
    if entry.mode & 0o077:
        found.append(f"{path}: open to others (mode {entry.mode:o})")
    if entry.size == 0:
        found.append(f"{path}: empty")
    return found


def read_enabled(path: str = ENABLED) -> tuple[str, ...]:
    try:
        text = Path(path).read_text("utf-8")
    except FileNotFoundError:
        return ()
    except (OSError, UnicodeDecodeError) as exc:
        raise SlotError(f"{path}: unreadable") from exc
    refs = tuple(line.strip() for line in text.splitlines() if line.strip())
    for ref in refs:
        try:
            Slot.from_ref(ref)
        except WorkerPolicyError as exc:
            raise SlotError(f"{path}: not a slot reference: {ref[:40]!r}") from exc
    return refs


def enabled(config: SlotConfig, path: str = ENABLED) -> tuple[Slot, ...]:
    """Registered slots the enabled file lists; an unregistered reference there enables nothing."""
    listed = set(read_enabled(path))
    return tuple(slot for slot in config.slots if slot.ref in listed)


@dataclass(frozen=True)
class Paths:
    root: str = WORKERS_ROOT
    enabled: str = ENABLED
    state: str = STATE_DIR
    audit: str = AUDIT_DIR
    results: Optional[str] = None  # default: <state>/results
    events: Optional[str] = None  # default: <audit>/events

    def __post_init__(self) -> None:
        if self.results is None:
            object.__setattr__(self, "results", self.state + "/results")
        if self.events is None:
            object.__setattr__(self, "events", self.audit + "/events")


def private_layout(paths: Paths = Paths()) -> list[Want]:
    """The result records the base user reads and the root-only events folder, parents first."""
    return [
        Want(paths.state, "dir", "base", 0o700),
        Want(paths.results, "dir", "base", 0o700),
        Want(paths.audit, "dir", "base", 0o700),
        Want(paths.events, "dir", "root", 0o700),
    ]


# Changes go through descriptors opened one component at a time without following symlinks:
# work/ belongs to the agent, so a job could swap an entry there for a link to a root file.
_DIR = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW


def _walk(path: str) -> int:
    """Descriptor of directory ``path``, opened from / one component at a time, no symlinks."""
    parts = PurePosixPath(path).parts
    if not path.startswith("/") or ".." in parts:
        raise SlotError(f"not an absolute plain path: {path}")
    fd = os.open("/", _DIR)
    try:
        for part in parts[1:]:
            child = os.open(part, _DIR, dir_fd=fd)
            os.close(fd)
            fd = child
    except OSError as exc:
        os.close(fd)
        raise SlotError(f"{path}: not a plain directory ({exc.strerror})") from exc
    return fd


def _ensure_dir(path: str, uid: int, gid: int, mode: int) -> bool:
    parent, name = os.path.split(path)
    pfd = _walk(parent)
    try:
        changed = False
        try:
            os.mkdir(name, mode, dir_fd=pfd)
            changed = True
        except FileExistsError:
            pass
        try:
            fd = os.open(name, _DIR, dir_fd=pfd)
        except OSError as exc:
            raise SlotError(f"{path}: not a plain directory ({exc.strerror})") from exc
        try:
            info = os.fstat(fd)
            if (info.st_uid, info.st_gid) != (uid, gid):
                os.fchown(fd, uid, gid)
                changed = True
            if stat.S_IMODE(info.st_mode) != mode:
                os.fchmod(fd, mode)
                changed = True
        finally:
            os.close(fd)
        return changed
    finally:
        os.close(pfd)


def write_file(path: str, content: bytes, mode: int, uid: int, gid: int) -> bool:
    """Make ``path`` a regular file of ``content`` owned by uid:gid (-1: the writer's own); a link
    there is replaced, never followed — nor is one on the way (the runner writes into folders of
    the agent and of the base user, C05)."""
    parent, name = os.path.split(path)
    pfd = _walk(parent)
    try:
        try:
            fd = os.open(name, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | _NOFOLLOW, dir_fd=pfd)
        except OSError:
            fd = None
        if fd is not None:
            try:
                info = os.fstat(fd)
                same = (
                    stat.S_ISREG(info.st_mode)
                    and stat.S_IMODE(info.st_mode) == mode
                    and (info.st_uid, info.st_gid) == (uid, gid)
                    and os.read(fd, len(content) + 1) == content
                )
            finally:
                os.close(fd)
            if same:
                return False
        temp = f".{name}.{secrets.token_hex(8)}"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600, dir_fd=pfd)
        try:
            try:
                view = memoryview(content)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
                os.fchown(fd, uid, gid)
                os.fchmod(fd, mode)
            finally:
                os.close(fd)
            os.replace(temp, name, src_dir_fd=pfd, dst_dir_fd=pfd)
        except BaseException:
            try:
                os.unlink(temp, dir_fd=pfd)
            except OSError:
                pass
            raise
        os.fsync(pfd)
        return True
    finally:
        os.close(pfd)


def install(
    config: SlotConfig,
    ids: Mapping[str, tuple[int, int]],
    homes: Mapping[str, str],
    paths: Paths = Paths(),
) -> tuple[int, list[str]]:
    """Create or repair the pools and the base user's result folder (``ids["base"]`` its uid/gid);
    enable the slots only when nothing is left to fix.

    Returns how many entries changed and the problems left. A pool that could not be built
    is disabled before the error is raised.
    """
    changed = 0
    try:
        for want in layout(config, paths.root):
            uid, gid = ids[want.owner]
            if want.kind == "dir":
                changed += _ensure_dir(want.path, uid, gid, want.mode)
            else:
                changed += write_file(want.path, want.content, want.mode, uid, gid)
        for want in private_layout(paths):
            os.makedirs(os.path.dirname(want.path), 0o755, exist_ok=True)
            changed += _ensure_dir(want.path, *ids[want.owner], want.mode)
        uids = {owner: pair[0] for owner, pair in ids.items()}
        left = problems(config, uids, root=paths.root)
        for agent in config.agents:
            left += credential_problems(homes[agent], ids[agent][0], agent)
        listing = "" if left else "".join(f"{slot.ref}\n" for slot in config.slots)
        changed += write_file(paths.enabled, listing.encode(), 0o644, *ids["root"])
    except (OSError, SlotError, KeyError) as exc:
        disable(ids, paths)
        raise SlotError(f"install stopped, slots disabled: {exc}") from exc
    return changed, left


def disable(ids: Mapping[str, tuple[int, int]], paths: Paths = Paths()) -> bool:
    """Empty the enabled list: kd-worker accepts no job. Folders, copies and results stay."""
    try:
        return write_file(paths.enabled, b"", 0o644, *ids["root"])
    except OSError as exc:
        raise SlotError(f"disable stopped: {exc}") from exc


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    euid: Optional[int] = None,
    config_path=CONFIG,
    registry=agents.REGISTRY,
    paths: Paths = Paths(),
    out=sys.stdout,
    err=sys.stderr,
) -> int:
    parser = argparse.ArgumentParser(prog="worker_slots.sh", allow_abbrev=False)
    action = parser.add_mutually_exclusive_group(required=True)
    for flag in ("--check", "--install", "--disable"):
        action.add_argument(flag, action="store_true")
    args = parser.parse_args(argv)
    if (euid if euid is not None else getattr(os, "geteuid", lambda: -1)()) != 0:
        err.write("worker_slots.sh: run as root\n")
        return 2
    import pwd

    from kd import naming

    try:
        config = load_config(config_path)
        base = naming.base_user()
        account = pwd.getpwnam(base)
        ids: dict[str, tuple[int, int]] = {"root": (0, 0), "base": (account.pw_uid, account.pw_gid)}
        homes: dict[str, str] = {}
        for agent in config.agents:
            account = pwd.getpwnam(agent_user(config, agent, registry))
            ids[agent] = (account.pw_uid, account.pw_gid)
            homes[agent] = account.pw_dir
        if args.disable:
            disable(ids, paths)
            out.write("ok: worker slots disabled; folders kept\n")
            return 0
        if args.install:
            changed, left = install(config, ids, homes, paths)
        else:
            changed = 0
            left = problems(config, {k: v[0] for k, v in ids.items()}, root=paths.root)
            for agent in config.agents:
                left += credential_problems(homes[agent], ids[agent][0], agent)
        on = enabled(config, paths.enabled)
    except (SlotError, KeyError) as exc:
        err.write(f"worker_slots.sh: {exc}\n")
        return 1
    for line in left:
        out.write(f"problem: {line}\n")
    out.write(f"changed: {changed}; enabled: {', '.join(s.ref for s in on) or 'none'}\n")
    return 1 if left else 0
