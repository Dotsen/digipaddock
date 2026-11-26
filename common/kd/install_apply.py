"""Fail-closed, repeatable application of a generated agent install plan.

Only ``--install`` and ``--disable`` mutate a host; ``--verify`` only tells whether the installed agent is what
this code would install (exit 0 matches, 3 differs). Tests inject an operations object;
the production operations object uses fixed argv, never shell interpolation. A failed
install revokes every agent-specific grant and leaves the generated runtime without a
policy, so ``agent-launch`` refuses to start the agent.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import io
import json
import os
import re
import stat
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Protocol, Sequence

from kd import agents, cap_grants, cap_units, claude_code, launch, session_config, specializations
from kd.board_grant import BACKLOG_DIR, BOARD_RESOURCE, FLOOR as FLOOR_TEMPLATE
from kd.manifest import SERVICE_USERS, ManifestError, load_manifest
from kd.install_plan import (
    DEFAULT_USER,
    GENERATED_RUNTIME,
    HostView,
    Plan,
    PlanError,
    _board_path,
    _load_inputs,
    _registry_path,
    build_plan,
)
from kd.policy import PolicyError, compile_policy
from kd.registry import RegistryError, load_registry, read_json_object

# Root-owned record of the named ACLs actually granted per agent. revoke() reads it, so a
# resource later removed, renamed or moved in the registry is still revoked.
GRANTS_DIR = "/var/lib/digipaddock/grants"
GRANTS_SCHEMA = 1
_SUDO_RULE = re.compile(r"^\s+\(([a-z][a-z0-9-]{0,31})\) NOPASSWD: (.+)$")


class ApplyError(RuntimeError):
    """The requested transition did not reach its verified target state."""


class OwnerStepError(ApplyError):
    """A module setup stopped at a step of the owner's (exit 3: a key to add in the forge) and printed which;
    new_agent.sh exits 3 for it, as bootstrap.sh does."""


class Operations(Protocol):
    def runtime_entry(self, plan: Plan) -> object: ...
    def matches(self, plan: Plan) -> bool: ...
    def quiesce(self, plan: Plan) -> None: ...
    def revoke(self, plan: Plan) -> None: ...
    def prepare(self, plan: Plan) -> None: ...
    def publish(self, plan: Plan) -> None: ...
    def grant_acl(self, plan: Plan) -> None: ...
    def grant_channels(self, plan: Plan) -> None: ...
    def verify(self, plan: Plan) -> None: ...
    def disable_runtime(self, plan: Plan) -> None: ...
    def unhost(self, plan: Plan) -> None: ...


def install(plan: Plan, ops: Operations) -> bool:
    """Apply once; return False only when the effective install already matches.

    The rollback deliberately does not remove the user, home, auth or memory. It may
    disable an earlier installation, but never restores an older grant.
    """
    _check_install_arguments(plan)
    _refuse_foreign_entry(plan, ops)
    if ops.matches(plan):
        return False
    _apply(plan, ops)
    return True


def _check_install_arguments(plan: Plan) -> None:
    if plan.manifest.user == DEFAULT_USER:
        raise ApplyError("new-agent install cannot mutate the shared base user")


def _apply(plan: Plan, ops: Operations) -> None:
    try:
        ops.quiesce(plan)
        ops.revoke(plan)
        ops.prepare(plan)
        ops.publish(plan)
        ops.grant_acl(plan)
        ops.grant_channels(plan)
        ops.verify(plan)
    except Exception as exc:
        failures = []
        for action in (ops.quiesce, ops.revoke, ops.disable_runtime):
            try:
                action(plan)
            except Exception as rollback_exc:
                failures.append(type(rollback_exc).__name__)
        detail = f"; rollback errors: {', '.join(failures)}" if failures else ""
        error = OwnerStepError if isinstance(exc, OwnerStepError) else ApplyError
        raise error(f"install failed: {type(exc).__name__}: {exc}{detail}") from exc


def disable(plan: Plan, ops: Operations) -> None:
    """Stop recurrence and revoke grants before invalidating the runtime policy; an agent on the session host
    leaves it first, so no session starts in between."""
    if plan.manifest.user == DEFAULT_USER:
        raise ApplyError("new-agent disable cannot mutate the shared base user")
    _refuse_foreign_entry(plan, ops)
    for _ in range(2):
        errors = []
        for action in (ops.unhost, ops.quiesce, ops.revoke, ops.disable_runtime):
            try:
                action(plan)
            except Exception as exc:
                errors.append(f"{action.__name__}: {type(exc).__name__}")
        if not errors:
            return
    raise ApplyError("disable incomplete: " + ", ".join(errors))


def _refuse_foreign_entry(plan: Plan, ops: Operations) -> None:
    # Only this installer writes agents.json, always with the generated marker: an entry
    # without it is not ours. Quiesce and revoke would take that agent down before
    # disable_runtime refused, so refuse up front.
    entry = ops.runtime_entry(plan)
    if entry is not None and (
        not isinstance(entry, dict) or entry.get("runtime") != GENERATED_RUNTIME
    ):
        raise ApplyError(
            f"{plan.manifest.name}: its agents.json entry is not the generated runtime;"
            " nothing changed"
        )


def disable_plan(repo: Path, name: str) -> Plan:
    """Build a revocation target without requiring install dependencies to be healthy."""
    inputs = _load_inputs(repo, name)
    floor = read_json_object(repo / FLOOR_TEMPLATE)
    try:
        policy = compile_policy(
            inputs.manifest, inputs.registry, floor=floor, agents=inputs.manifests
        )
    except PolicyError as exc:
        raise PlanError(f"disable policy: {exc}") from exc
    return Plan(inputs.manifest, policy, (), (), ())


def _uid_of(user: str) -> int:
    import pwd

    return pwd.getpwnam(user).pw_uid


def installed_spec_checker(
    repo: Path, *, require_root_owner: bool = True
) -> Callable[[str, str], list[str]]:
    """A check that an agent's installed ``playbooks/spec/`` is this code's registry.

    The texts the agent reads are the ones ``kd-task claim`` records; a release that changed
    a text without a reinstall is reported here instead of refused at the next claim.
    """
    try:
        registry = specializations.load_registry(
            repo / "common" / "specializations", require_root_owner=require_root_owner
        )
    except (specializations.SpecError, OSError) as exc:
        raise ApplyError(f"specialization registry: {exc}") from exc

    def check(name: str, folder: str) -> list[str]:
        manifest = load_manifest(repo / "agents" / name / "agent.json")
        hint = f"(new_agent.sh --install {name})"
        problems = []
        texts = [("spec", s) for s in specializations.installable(registry, manifest.profile, manifest.capabilities)]
        texts += [("skill", s) for s in specializations.installable_skills(registry, manifest.profile, manifest.capabilities)]
        for kind, spec in texts:
            relative = f"playbooks/{kind}/{spec.name}.md"
            path = Path(folder) / relative
            try:
                if not stat.S_ISREG(os.lstat(path).st_mode):
                    raise FileNotFoundError(relative)
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                problems.append(f"{relative} is missing {hint}")
                continue
            if digest != spec.sha256:
                problems.append(f"{relative} is not the registry text {hint}")
        return problems

    return check


def _runtime_registry(path: str) -> dict:
    """The runtime agent registry: none yet (a new host before its first agent) is empty; a damaged one,
    a link or a non-object refuses, never reads as empty."""
    if not os.path.lexists(path):
        return {}
    data = read_json_object(path)
    if not isinstance(data, dict):
        raise ApplyError("invalid runtime registry")
    return data


def generated_runtime_problems(
    repo: Path,
    *,
    uid_of: Callable[[str], int] = _uid_of,
    fs: launch.Filesystem | None = None,
    spec_checker: Callable[[Path], Callable[[str, str], list[str]]] = installed_spec_checker,
) -> list[str]:
    """Every installed generated agent this code's launcher would refuse, as "name: why",
    and every one whose installed specialization texts are not this code's registry.

    A release that changes the policy schema stops such agents at their next restart;
    kd-release runs this over the whole host so that never happens silently, and
    ``new_agent.sh --check`` refuses on its own role's problems (``generated_runtime_findings``).
    """
    return [
        f"{name}: {why}"
        for name, why in generated_runtime_findings(
            repo, uid_of=uid_of, fs=fs, spec_checker=spec_checker
        )
    ]


def generated_runtime_findings(
    repo: Path,
    *,
    uid_of: Callable[[str], int] = _uid_of,
    fs: launch.Filesystem | None = None,
    spec_checker: Callable[[Path], Callable[[str, str], list[str]]] = installed_spec_checker,
) -> list[tuple[str, str]]:
    """``generated_runtime_problems`` as (agent, why) pairs."""
    registry = load_registry(repo)
    runtime_path = _registry_path(registry)
    data = _runtime_registry(runtime_path)
    root = registry.resources.layout["agents"]
    check_specs = spec_checker(repo)
    problems = []
    for name in sorted(data):
        try:
            if agents.runtime_of(name, runtime_path) != GENERATED_RUNTIME:
                continue
            user = agents.user_of(name, runtime_path)
            try:
                uid = uid_of(user)
            except KeyError:
                raise ApplyError(f"user {user} is missing") from None
            launch.plan_generated(
                name,
                f"{root}/{name}",
                uid=uid,
                user=user,
                agents_root=root,
                fs=fs or launch.HostFilesystem(),
                registry=runtime_path,
            )
            problems += [(name, problem) for problem in check_specs(name, f"{root}/{name}")]
        except (ApplyError, ManifestError, OSError, ValueError) as exc:
            problems.append((name, str(exc)))
    return problems


def outdated_agents(
    repo: Path,
    *,
    ops: Operations | None = None,
    plan_of: Callable[[str], Plan] | None = None,
    registry_path: str | None = None,
) -> list[str]:
    """The installed generated agents whose install differs from what this code would install (the roles
    a release asks the factory to reinstall); one whose plan cannot be built differs too."""
    system = ops or SystemOperations(repo)
    plan_of = plan_of or (lambda name: build_plan(repo, HostView("/"), name))
    path = registry_path or _registry_path(load_registry(repo))
    data = _runtime_registry(path)
    out = []
    for name in sorted(data):
        try:
            if agents.runtime_of(name, path) != GENERATED_RUNTIME:
                continue
        except ValueError:
            continue
        try:
            if system.matches(plan_of(name)):
                continue
        except (ApplyError, PlanError, ManifestError, OSError, ValueError):
            pass
        out.append(name)
    return out


def verify_main(name: str, repo: Path | None = None) -> int:
    """``--verify``: 0 the installed agent matches this code, 3 it differs, 1 it cannot be told."""
    repo = repo or Path(__file__).resolve().parents[2]
    try:
        matches = SystemOperations(repo).matches(build_plan(repo, HostView("/"), name))
    except (ApplyError, PlanError, OSError, ValueError) as exc:
        print(f"new_agent.sh: {exc}", file=sys.stderr)
        return 1
    print(f"{name}: {'matches' if matches else 'differs'}")
    return 0 if matches else 3


def report_generated_runtime(repo: str) -> int:
    """kd-release postflight: 0 when every generated agent can start with this code."""
    try:
        problems = generated_runtime_problems(Path(repo))
    except (ApplyError, OSError, ValueError) as exc:
        problems = [f"registry: {exc}"]
    for problem in problems:
        print(f"generated runtime refused: {problem}", file=sys.stderr)
    return 1 if problems else 0


def _run(*argv: str, allow_absent: bool = False, pass_fds: Sequence[int] = ()) -> None:
    result = subprocess.run(
        argv, check=False, capture_output=True, text=True, pass_fds=tuple(pass_fds)
    )
    if result.returncode and not (allow_absent and result.returncode == 1):
        # the last line of stderr says why («runuser failed (1)» alone told the owner nothing)
        why = next((line.strip() for line in reversed((result.stderr or "").splitlines()) if line.strip()), "")
        raise ApplyError(f"{argv[0]} failed ({result.returncode})" + (f": {why[-200:]}" if why else ""))


def _clear_dir(path: str, *, remove: bool) -> None:
    """Unlink every entry of a root-owned directory, never following a link; ``remove``
    drops the directory too. An absent directory is already clear."""
    if not os.path.lexists(path):
        return
    directory = _safe_path(path)
    if not directory.is_dir():
        raise ApplyError(f"not a directory: {path}")
    for entry in sorted(directory.iterdir()):
        if entry.is_dir() and not entry.is_symlink():
            raise ApplyError(f"unexpected directory: {entry}")
        entry.unlink()
    if remove:
        directory.rmdir()


def _link(path: str, target: str) -> None:
    link = _safe_path(str(Path(path).parent)) / Path(path).name
    if link.is_symlink() and os.readlink(link) == target:
        return
    if link.is_symlink() or link.exists():
        raise ApplyError(f"link is occupied: {path}")
    link.symlink_to(target)


def _safe_path(path: str) -> Path:
    if not path.startswith("/") or path.startswith("//") or ".." in Path(path).parts:
        raise ApplyError("non-absolute or traversing install path")
    current = Path("/")
    for part in Path(path).parts[1:]:
        current /= part
        if current.is_symlink():
            raise ApplyError(f"symlink in install path: {current}")
    return Path(path)


def _root_parent(path: str) -> None:
    parent = _safe_path(str(Path(path).parent))
    info = parent.stat()
    if info.st_uid != 0 or info.st_gid != 0 or info.st_mode & 0o022:
        raise ApplyError(f"privileged file parent is writable: {parent}")


def _ensure_dir(path: str, user: str = "root", mode: int = 0o755) -> None:
    import pwd

    target = _safe_path(path)
    if target.exists() and not target.is_dir():
        raise ApplyError(f"not a directory: {path}")
    if not target.exists():
        target.mkdir(mode=mode)
    account = pwd.getpwnam(user)
    os.chown(target, account.pw_uid, account.pw_gid)
    os.chmod(target, mode)


def _atomic(path: str, content: bytes, mode: int, gid: int = 0) -> None:
    """Root's file (group ``gid``) at ``path``, written whole or not at all; an existing target is
    compared through its own descriptor, O_NOFOLLOW — the agent owns some of these parents (work/)."""
    target = _safe_path(path)
    try:
        fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        fd = -1
    except OSError as exc:  # ELOOP: a link planted after the path check
        raise ApplyError(f"not a regular file: {path}") from exc
    if fd >= 0:
        with os.fdopen(fd, "rb") as current:
            info = os.fstat(current.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ApplyError(f"not a regular file: {path}")
            if (
                stat.S_IMODE(info.st_mode) == mode
                and (info.st_uid, info.st_gid) == (0, gid)
                and info.st_size == len(content)
                and current.read(len(content) + 1) == content
            ):
                return
    fd, temp = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), mode)
            os.fchown(output.fileno(), 0, gid)
        os.replace(temp, target)
        directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


# Named ACLs are changed through descriptors, never by name: the trees may be writable by the
# agent, which could otherwise swap a checked name for a symlink to a root file before setfacl.
# getfacl/setfacl receive /proc/self/fd/N of an O_PATH descriptor; that magic link resolves to
# the opened inode itself, not to whatever the name points at by then.
_O_PATH = getattr(os, "O_PATH", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
ACL_TREE_MAX = 100_000


def _open_path(path: str) -> int | None:
    """O_PATH descriptor of an absolute path, opened one component at a time.

    No directory component may be a symlink; the last component may be one (callers see
    S_ISLNK and refuse or skip it). None means the path does not exist.
    """
    parts = Path(path).parts
    if (
        not path.startswith("/")
        or path.startswith("//")
        or ".." in parts
        or len(parts) < 2
    ):
        raise ApplyError("non-absolute or traversing install path")
    if not _O_PATH:
        raise ApplyError("descriptor-safe ACL changes need Linux O_PATH")
    fd = os.open("/", _O_PATH | os.O_DIRECTORY)
    try:
        for part in parts[1:-1]:
            child = os.open(part, _O_PATH | os.O_DIRECTORY | _O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return os.open(parts[-1], _O_PATH | _O_NOFOLLOW, dir_fd=fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ApplyError(f"symlink or non-directory in install path: {path}") from exc
    finally:
        os.close(fd)


def _fd_name(fd: int) -> str:
    return f"/proc/self/fd/{fd}"


def _acl_text(fd: int) -> str:
    return subprocess.run(
        ("getfacl", "-cp", _fd_name(fd)),
        check=True,
        capture_output=True,
        text=True,
        pass_fds=(fd,),
    ).stdout


def _walk(fd: int, directory: bool, visit, seen: list) -> None:
    """Visit a real file/directory and, for a directory, its real files and directories.

    Children are opened relative to the opened parent with O_NOFOLLOW; symlinks, sockets and
    devices are skipped, never followed. ``visit`` returns False to stop descending.
    """
    seen[0] += 1
    if seen[0] > ACL_TREE_MAX:
        raise ApplyError("ACL tree is too large")
    if not visit(fd, directory) or not directory:
        return
    listing = os.open(".", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
    try:
        with os.scandir(listing) as entries:
            names = sorted(entry.name for entry in entries)
        for name in names:
            child = os.open(name, _O_PATH | _O_NOFOLLOW, dir_fd=listing)
            try:
                mode = os.fstat(child).st_mode
                if stat.S_ISDIR(mode) or stat.S_ISREG(mode):
                    _walk(child, stat.S_ISDIR(mode), visit, seen)
            finally:
                os.close(child)
    finally:
        os.close(listing)


def _tree(path: str, visit) -> None:
    fd = _open_path(path)
    if fd is None:
        return
    try:
        mode = os.fstat(fd).st_mode
        if stat.S_ISLNK(mode):
            raise ApplyError(f"ACL target is a symlink: {path}")
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise ApplyError(f"ACL target is not a file or directory: {path}")
        _walk(fd, stat.S_ISDIR(mode), visit, [0])
    finally:
        os.close(fd)


def _acl_tree(path: str, user: str, *, access: str | None) -> None:
    """Change only the user's named entries on real files/directories, never symlink targets.

    ``access`` None removes them; "read" or "write" grants.
    """

    def visit(fd: int, directory: bool) -> bool:
        _acl_set(fd, user, access, directory=directory)
        return True

    _tree(path, visit)


_ACL_XATTRS = ("system.posix_acl_access", "system.posix_acl_default")
_ACL_USER_TAG = 0x02  # ACL_USER in the kernel's xattr form: version 2, then (tag u16, perm u16, id u32) entries


def _named_in_xattrs(fd: int, uid: int, *, directory: bool) -> bool:
    """A named entry for ``uid`` in the access ACL, or in the default ACL of a directory — read from the xattrs,
    one system call each, not one getfacl process per file (--verify walked ~24 000 files)."""
    for attr in _ACL_XATTRS[: 2 if directory else 1]:
        try:
            data = os.getxattr(_fd_name(fd), attr)
        except OSError as exc:
            if exc.errno in (errno.ENODATA, errno.EOPNOTSUPP):
                continue
            raise ApplyError(f"ACL not read: {exc.strerror}") from None
        if len(data) < 4 or (len(data) - 4) % 8 or struct.unpack_from("<I", data)[0] != 2:
            raise ApplyError("ACL xattr has an unknown form")
        for offset in range(4, len(data), 8):
            tag, _perm, ident = struct.unpack_from("<HHI", data, offset)
            if tag == _ACL_USER_TAG and ident == uid:
                return True
    return False


def _acl_tree_clean(path: str, user: str) -> bool:
    """True when no file or directory of the tree carries a named entry for ``user``."""
    import pwd

    found = []
    try:
        uid = pwd.getpwnam(user).pw_uid
    except KeyError:
        return True  # no such account: no entry names it

    def visit(fd: int, directory: bool) -> bool:
        if _named_in_xattrs(fd, uid, directory=directory):
            found.append(True)
        return not found

    _tree(path, visit)
    return not found


def _acl_set(fd: int, user: str, access: str | None, *, directory: bool) -> None:
    text = _acl_text(fd)
    if access is None:
        names = [(f"user:{user}", f"u:{user}")]
        if directory:
            names.append((f"default:user:{user}", f"d:u:{user}"))
        entries = ",".join(
            item for name, item in names if _acl_entry_in(text, name) is not None
        )
        if entries:
            _run("setfacl", "-x", entries, _fd_name(fd), pass_fds=(fd,))
        return
    if _acl_text_matches(text, user, access, directory=directory):
        return
    permissions = "rwX" if access == "write" else "rX"
    entries = (
        f"u:{user}:{permissions},d:u:{user}:{permissions}"
        if directory
        else f"u:{user}:{permissions}"
    )
    _run("setfacl", "-m", entries, _fd_name(fd), pass_fds=(fd,))


def _acl_entry_in(text: str, entry: str) -> str | None:
    for line in text.splitlines():
        if line.startswith(entry + ":"):
            permission = line[len(entry) + 1 :].split("\t", 1)[0]
            effective = line.split("#effective:", 1)
            return effective[1].strip() if len(effective) == 2 else permission
    return None


def _acl_text_matches(text: str, user: str, access: str, *, directory: bool) -> bool:
    name = f"user:{user}"
    actual = _acl_entry_in(text, name)
    default = _acl_entry_in(text, f"default:{name}") if directory else None
    if access == "none":
        return actual is None and default is None
    if actual is None or (directory and default is None):
        return False
    entries = (actual, default) if directory else (actual,)
    return all(
        "r" in value
        and ("w" in value) == (access == "write")
        and (not directory or "x" in value)
        for value in entries
    )


def _acl_of(path: str) -> str:
    """ACL text of a real file or directory, read through a descriptor."""
    fd = _open_path(path)
    if fd is None:
        raise ApplyError(f"ACL target is missing: {path}")
    try:
        mode = os.fstat(fd).st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise ApplyError(f"ACL target is not a file or directory: {path}")
        return _acl_text(fd)
    finally:
        os.close(fd)


def _acl_matches(path: str, user: str, access: str, *, directory: bool) -> bool:
    return _acl_text_matches(_acl_of(path), user, access, directory=directory)


def _parse_sudo_listing(output: str) -> set[tuple[str, str]]:
    """Read the complete NOPASSWD command set, refusing unrecognized sudo syntax."""
    lines = output.splitlines()
    start = next(
        (
            index + 1
            for index, line in enumerate(lines)
            if "may run the following commands" in line
        ),
        None,
    )
    if start is None:
        raise ApplyError("sudo -l output has no command section")
    found: set[tuple[str, str]] = set()
    for line in lines[start:]:
        if not line.strip():
            continue
        match = _SUDO_RULE.fullmatch(line)
        if match is None:
            raise ApplyError("sudo -l contains an unrecognized grant")
        for command in match.group(2).split(", "):
            if not command.startswith("/") or "\n" in command:
                raise ApplyError("sudo -l contains a non-absolute grant")
            found.add((match.group(1), command))
    return found


def _sudo_effective(user: str) -> set[tuple[str, str]]:
    result = subprocess.run(
        ("sudo", "-n", "-l", "-U", user),
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C"},
    )
    # sudo versions differ in the exit code of an empty listing; the text is the same.
    text = result.stdout + result.stderr
    if (
        result.returncode in (0, 1)
        and "is not allowed to run sudo" in text
        and "may run the following commands" not in text
    ):
        return set()
    if result.returncode:
        raise ApplyError(f"sudo -l failed for {user}")
    return _parse_sudo_listing(result.stdout)


class SystemOperations:
    """Linux root implementation; file and command targets come only from a checked plan."""

    session_host_config = session_config.CONFIG

    def __init__(self, repo: Path, grants_dir: str = GRANTS_DIR) -> None:
        self.repo = repo
        self.registry = load_registry(repo)
        self.grants_dir = grants_dir

    def _folder(self, plan: Plan) -> str:
        return f"{self.registry.resources.layout['agents']}/{plan.manifest.name}"

    def _policy_path(self, plan: Plan) -> str:
        return f"{self._folder(plan)}/.claude/policy.json"

    def _registry_path(self) -> str:
        return _registry_path(self.registry)

    def runtime_entry(self, plan: Plan) -> object:
        return _runtime_registry(self._registry_path()).get(plan.manifest.name)

    def _channel_sockets(self, plan: Plan) -> list[str]:
        """The agent's socket units on the host, whatever the current policy names."""
        name = plan.manifest.name
        return [
            unit
            for unit in (cap_units.socket_name(name, key) for key in cap_grants.KEYS)
            if os.path.lexists(f"{cap_units.UNIT_DIR}/{unit}")
        ]

    def _user_script(self, plan: Plan, mode: str) -> None:
        if plan.manifest.user != DEFAULT_USER:
            _run(
                "/bin/bash",
                str(self.repo / "common/install/add_user.sh"),
                plan.manifest.user,
                mode,
            )

    def _setup(self, plan: Plan, phase: str) -> None:
        """Run the held modules' setup scripts for one lifecycle phase, in its order."""
        for step in plan.policy["install_steps"][phase]:
            capability, _, kind = step.partition(":")
            if kind != "setup":
                continue
            script = self.registry.capabilities[capability]["setup"]
            # Output stays on the terminal: keys, token hints and check reasons are for the owner.
            result = subprocess.run(
                (
                    "/bin/bash",
                    str(self.repo / script),
                    phase,
                    plan.manifest.name,
                    plan.manifest.user,
                ),
                check=False,
            )
            if result.returncode == 3:
                raise OwnerStepError(f"setup {capability} {phase}: the owner's step (see above)")
            if result.returncode:
                raise ApplyError(
                    f"setup {capability} {phase} failed ({result.returncode})"
                )

    def matches(self, plan: Plan) -> bool:
        try:
            self.verify(plan)
            return True
        except (
            ApplyError,
            OSError,
            ValueError,
            KeyError,
            subprocess.CalledProcessError,
        ):
            return False

    def quiesce(self, plan: Plan) -> None:
        """Every process of the agent's user stops, its session-host sessions included."""
        user = plan.manifest.user
        if user == DEFAULT_USER:
            raise ApplyError("shared base-user agents need a targeted stop operation")
        if subprocess.run(("id", user), capture_output=True).returncode == 0:
            _run("pkill", "-TERM", "-u", user, allow_absent=True)
            for _ in range(50):
                if subprocess.run(
                    ("pgrep", "-u", user), capture_output=True
                ).returncode:
                    return
                time.sleep(0.2)
            _run("pkill", "-KILL", "-u", user, allow_absent=True)
            for _ in range(10):
                if subprocess.run(
                    ("pgrep", "-u", user), capture_output=True
                ).returncode:
                    return
                time.sleep(0.1)
            raise ApplyError("agent user still has running processes")

    def _grants_path(self, plan: Plan) -> str:
        return f"{self.grants_dir}/{plan.manifest.name}.json"

    def _read_grants(self, plan: Plan) -> set[tuple[str, str]]:
        """(path, user) pairs from the grant record; a damaged record refuses, never reads as empty."""
        path = self._grants_path(plan)
        if not os.path.lexists(path):
            return set()
        try:
            data = read_json_object(path)
        except (RegistryError, OSError) as exc:
            raise ApplyError(f"grant record is damaged: {path}") from exc
        if (
            set(data) != {"schema_version", "grants"}
            or data["schema_version"] != GRANTS_SCHEMA
            or not isinstance(data["grants"], list)
        ):
            raise ApplyError(f"grant record is damaged: {path}")
        grants = set()
        for item in data["grants"]:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or not all(isinstance(v, str) for v in item)
                or not item[0].startswith("/")
                or not agents.USER_RE.fullmatch(item[1])
            ):
                raise ApplyError(f"grant record is damaged: {path}")
            grants.add((item[0], item[1]))
        return grants

    def _write_grants(self, plan: Plan, grants: set[tuple[str, str]]) -> None:
        for directory in (str(Path(self.grants_dir).parent), self.grants_dir):
            if not os.path.lexists(directory):
                _ensure_dir(
                    directory, "root", 0o755 if directory != self.grants_dir else 0o700
                )
        _root_parent(self._grants_path(plan))
        content = {
            "schema_version": GRANTS_SCHEMA,
            "grants": [list(item) for item in sorted(grants)],
        }
        _atomic(
            self._grants_path(plan),
            (json.dumps(content, indent=2) + "\n").encode(),
            0o600,
        )

    def _policy_acl(self, plan: Plan) -> set[tuple[str, str]]:
        return {
            (entry["path"], entry["user"])
            for entry in plan.policy["acl"]
            if entry["resource"] != BOARD_RESOURCE
        }

    def _other_acl(self, plan: Plan) -> list[tuple[str, str]]:
        # Revoke from every public resource, not just the *current* policy, and from every
        # path the grant record says was granted: a resource since removed, renamed or moved
        # in the registry is no longer listed there, but its ACL would still be in effect.
        # Code resources are skipped: release files are world-readable, every release is a
        # new directory, and the code path itself is a symlink the safe walk refuses.
        user = plan.manifest.user
        code = self.registry.resources.layout["code"].rstrip("/") + "/"
        public = {
            (resource.path, user)
            for name, resource in self.registry.resources.resources.items()
            if name != BOARD_RESOURCE
            and not resource.secret
            and resource.kind in ("file", "dir")
            and resource.path
            and not resource.path.startswith(code)
        }
        return sorted(public | self._read_grants(plan))

    def _revoke_channels(self, plan: Plan) -> None:
        """The grants files go first: without them every channel call is refused (the
        switch); then the sockets, running calls and agent-bin links.
        Units and tmpfiles stay for the audit."""
        name = plan.manifest.name
        _clear_dir(f"{cap_grants.GRANTS_DIR}/{name}", remove=False)
        sockets = self._channel_sockets(plan)
        if sockets:
            _run("systemctl", "disable", "--now", *sockets)
        _run(
            "systemctl",
            "stop",
            *(f"digipaddock-cap-{name}-{key}@*.service" for key in cap_grants.KEYS),
        )
        _clear_dir(f"{cap_units.AGENT_BIN}/{name}", remove=True)

    def revoke(self, plan: Plan) -> None:
        self._revoke_channels(plan)
        self._user_script(plan, "--revoke")
        for path, user in self._other_acl(plan):
            _acl_tree(path, user, access=None)
        # Cleared only after every recorded grant is gone; a failure above keeps the record.
        if os.path.lexists(self._grants_path(plan)):
            self._write_grants(plan, set())
        # The modules' own services go off last; their data stays.
        self._setup(plan, "disable")

    def prepare(self, plan: Plan) -> None:
        for action in plan.actions:
            if action.kind == "package":
                _run("apt-get", "install", "-y", action.target)
            elif action.kind == "service-user":
                if action.target not in SERVICE_USERS:
                    raise ApplyError("service user is not reserved")
                if subprocess.run(
                    ("id", action.target), capture_output=True
                ).returncode:
                    _run(
                        "useradd",
                        "--system",
                        "--create-home",
                        "--home-dir",
                        f"/home/{action.target}",
                        "--shell",
                        "/usr/sbin/nologin",
                        action.target,
                    )
                _ensure_dir(f"/home/{action.target}", action.target, 0o700)
        self._user_script(plan, "--prepare")

    def publish(self, plan: Plan) -> None:
        folder = self._folder(plan)
        generated = {item.path: item for item in plan.files}
        user_data_done = setup_done = False
        for action in plan.actions:
            if action.kind == "board-grant":
                break
            if action.kind == "registry":
                path = self._registry_path()
                _root_parent(path)
                agents.users(path)
                data = _runtime_registry(path)
                data[plan.manifest.name] = _registry_entry(plan)
                _atomic(
                    path,
                    (
                        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
                        + "\n"
                    ).encode(),
                    0o644,
                )
            elif action.kind == "dir" and (
                action.target == folder or action.target.startswith(f"{folder}/")
            ):
                if action.target in (f"{folder}/work", f"{folder}/memory"):
                    _ensure_dir(action.target, plan.manifest.user, 0o750)  # the agent's own data
                else:
                    _ensure_dir(action.target, "root")
            elif action.kind == "file" and action.target in generated:
                item = generated[action.target]
                _atomic(item.path, item.content, int(item.mode, 8))
            elif action.kind == "reload" and action.target == "apparmor":
                _run("systemctl", "reload", "apparmor")
            elif action.kind == "link":
                link = (
                    _safe_path(str(Path(action.target).parent))
                    / Path(action.target).name
                )
                target = action.detail.split(" ", 2)[1]
                if link.is_symlink():
                    if os.readlink(link) != target:
                        raise ApplyError(f"unexpected wrapper link: {link}")
                elif link.exists():
                    raise ApplyError(f"wrapper link is occupied: {link}")
                else:
                    link.symlink_to(target)
            elif action.kind == "trust":
                self._prepare_user_data(plan)
                user_data_done = True
            elif action.kind == "setup" and not setup_done:
                if not user_data_done:
                    raise ApplyError("module setup comes before the user's data")
                self._setup(plan, "install")
                setup_done = True
        if not user_data_done:
            raise ApplyError("plan has no trust preparation step")

    def _prepare_user_data(self, plan: Plan) -> None:
        folder, user = self._folder(plan), plan.manifest.user
        # This helper runs as the home owner; it never gives root a pathname under home.
        helper = """
import json, os, pathlib, sys, tempfile
folder, home = sys.argv[1:]
note = pathlib.Path(folder) / 'memory/notes.md'
if note.is_symlink(): raise ValueError('memory note is a symlink')
if not note.exists():
    with open(note, 'x', encoding='utf-8') as output: pass
note.chmod(0o640)
# work/tmp/: the agent's attachments and texts (its guard allows reply files from there)
texts = pathlib.Path(folder) / 'work/tmp'
if texts.is_symlink(): raise ValueError('work/tmp is a symlink')
texts.mkdir(mode=0o750, exist_ok=True)
texts.chmod(0o750)
trust = pathlib.Path(home) / '.claude.json'
if trust.is_symlink(): raise ValueError('Claude trust path is a symlink')
data = json.loads(trust.read_text('utf-8')) if trust.exists() else {}
if not isinstance(data, dict): raise ValueError('invalid Claude trust file')
projects = data.setdefault('projects', {})
if not isinstance(projects, dict): raise ValueError('invalid Claude projects')
changed = False
for path in (folder, folder + '/work'):
    item = projects.setdefault(path, {})
    if not isinstance(item, dict): raise ValueError('invalid Claude project')
    if item.get('hasTrustDialogAccepted') is not True:
        item['hasTrustDialogAccepted'] = True
        changed = True
if changed:
    fd, temp = tempfile.mkstemp(prefix='.claude-trust.', dir=home)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(data, output, ensure_ascii=False)
            output.write('\\n')
            output.flush(); os.fsync(output.fileno())
        os.chmod(temp, 0o600); os.replace(temp, trust)
    finally:
        if os.path.exists(temp): os.unlink(temp)
trust.chmod(0o600)
"""
        _run("runuser", "-u", user, "--", "/usr/bin/python3", "-I", "-c", helper, folder, f"/home/{user}")
        # the sessions' own notes and temp folders, and the one-time split of the notes of before (its
        # original kept in memory/legacy/, done once: a reinstall finds the map and changes nothing) — as the agent
        workspace = f"{self.registry.resources.layout['code'].rstrip('/')}/common/kd/workspace.py"
        _run("runuser", "-u", user, "--", "/usr/bin/python3", "-I", workspace, "migrate", folder)

    def grant_acl(self, plan: Plan) -> None:
        self._user_script(plan, "--grant")
        entries = [e for e in plan.policy["acl"] if e["resource"] != BOARD_RESOURCE]
        for entry in entries:
            resource = self.registry.resources.resources[entry["resource"]]
            if resource.kind not in ("file", "dir"):
                raise ApplyError("unsupported ACL resource")
        # Recorded before any setfacl: a crash mid-grant still leaves every grant revocable.
        self._write_grants(plan, self._read_grants(plan) | self._policy_acl(plan))
        for entry in entries:
            _acl_tree(entry["path"], entry["user"], access=entry["access"])

    def grant_channels(self, plan: Plan) -> None:
        """Units, tmpfiles and agent-bin links from the checked plan, then the grants files
        rendered with the agent's uid: the switch goes on last."""
        channels = plan.policy["channels"]
        if not channels:
            return
        name, user = plan.manifest.name, plan.manifest.user
        code = self.registry.resources.layout["code"]
        generated = {item.path: item for item in plan.files}
        units = [path for path, _ in cap_units.unit_files(name, user, code, channels)]
        for path in (cap_units.TARGET_PATH, *units):
            item = generated.get(path)
            if item is None:
                raise ApplyError(f"the plan lacks the channel file {path}")
            _root_parent(path)
            _atomic(path, item.content, 0o644)
        _run("systemd-tmpfiles", "--create", cap_units.tmpfiles(name, user)[0])
        _run("systemctl", "daemon-reload")
        sockets = [cap_units.socket_name(name, c["key"]) for c in channels]
        _run("systemctl", "enable", cap_units.TARGET, *sockets)
        _run("systemctl", "start", cap_units.TARGET)
        # A restarted Accept=yes socket leaves running calls alone.
        _run("systemctl", "restart", *sockets)
        for directory in (cap_units.AGENT_BIN, f"{cap_units.AGENT_BIN}/{name}"):
            _root_parent(directory)
            _ensure_dir(directory, "root", 0o755)
        for link, target in cap_units.links(name, code, channels):
            _link(link, target)
        uid = _uid_of(user)
        for directory in (cap_grants.GRANTS_DIR, f"{cap_grants.GRANTS_DIR}/{name}"):
            _root_parent(directory)
            _ensure_dir(directory, "root", 0o755)
        for channel in channels:
            _atomic(
                cap_units.grants_path(name, channel["key"]),
                cap_grants.render(name, uid, channel),
                0o644,
            )

    def _verify_channels(self, plan: Plan) -> None:
        name, user = plan.manifest.name, plan.manifest.user
        channels = plan.policy["channels"]
        uid = _uid_of(user)
        expected = {
            cap_units.grants_path(name, c["key"]): cap_grants.render(name, uid, c)
            for c in channels
        }
        grants = f"{cap_grants.GRANTS_DIR}/{name}"
        present = (
            {str(p).replace(os.sep, "/") for p in _safe_path(grants).iterdir()}
            if os.path.lexists(grants)
            else set()
        )
        if present != set(expected):
            raise ApplyError(f"channel grants files differ from the policy: {grants}")
        for path, content in expected.items():
            target = _safe_path(path)
            info = target.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or target.read_bytes() != content
                or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (0, 0, 0o644)
            ):
                raise ApplyError(f"channel grants file mismatch: {path}")
        for channel in channels:
            _run("systemctl", "is-active", "--quiet", cap_units.socket_name(name, channel["key"]))
        links = {link for link, _ in cap_units.links(name, self.registry.resources.layout["code"], channels)}
        folder = f"{cap_units.AGENT_BIN}/{name}"
        found = (
            {f"{folder}/{entry.name}" for entry in _safe_path(folder).iterdir()}
            if os.path.lexists(folder)
            else set()
        )
        if found != links:
            raise ApplyError(f"agent-bin differs from the self channel: {folder}")

    def _verify_sudo(self, plan: Plan) -> None:
        """No sudo at all: the agent user holds none, the base user none as it."""
        user = plan.manifest.user
        if _sudo_effective(user):
            raise ApplyError(f"effective sudo grants differ for {user}")
        actual = _sudo_effective(DEFAULT_USER)
        if any(run_as == "ALL" for run_as, _ in actual):
            raise ApplyError(f"ambiguous broad sudo grant of {DEFAULT_USER}")
        if any(run_as == user for run_as, _ in actual):
            raise ApplyError(f"effective sudo grants differ for {DEFAULT_USER}")

    def verify(self, plan: Plan) -> None:
        import pwd

        registry_path = self._registry_path()
        _root_parent(registry_path)
        info = _safe_path(registry_path).stat()
        if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (0, 0, 0o644):
            raise ApplyError("runtime registry ownership or mode mismatch")
        entry = _runtime_registry(registry_path).get(plan.manifest.name)
        if entry != _registry_entry(plan):
            raise ApplyError("runtime registry mismatch")
        for item in plan.files:
            target = _safe_path(item.path)
            info = target.stat()
            if (
                not target.is_file()
                or target.read_bytes() != item.content
                or stat.S_IMODE(info.st_mode) != int(item.mode, 8)
                or info.st_uid != 0
                or info.st_gid != 0
            ):
                raise ApplyError(f"installed file mismatch: {item.path}")
        for action in plan.actions:
            if action.kind == "link":
                link = (
                    _safe_path(str(Path(action.target).parent))
                    / Path(action.target).name
                )
                if (
                    not link.is_symlink()
                    or os.readlink(link) != action.detail.split(" ", 2)[1]
                ):
                    raise ApplyError(f"wrapper link mismatch: {link}")
        user = plan.manifest.user
        board = f"{_board_path(self.registry)}/{BACKLOG_DIR}"
        board_entries = [
            entry for entry in plan.policy["acl"] if entry["resource"] == BOARD_RESOURCE
        ]
        board_access = board_entries[0]["access"] if board_entries else "none"
        if not _acl_matches(board, user, board_access, directory=True):
            raise ApplyError("board named ACL mismatch")
        result = subprocess.run(
            ("runuser", "-u", user, "--", "find", board, "-writable", "-print", "-quit"),
            check=False,
            capture_output=True,
            text=True,
        )
        if result.stdout.strip():
            raise ApplyError("board is writable without board.write")
        for entry in plan.policy["acl"]:
            if entry["resource"] == BOARD_RESOURCE:
                continue
            resource = self.registry.resources.resources[entry["resource"]]
            if not _acl_matches(
                entry["path"], user, entry["access"], directory=resource.kind == "dir"
            ):
                raise ApplyError(f"resource ACL mismatch: {entry['resource']}")
        granted = self._policy_acl(plan)
        if not granted <= self._read_grants(plan):
            raise ApplyError("grant record does not cover the policy ACLs")
        for path, other in self._other_acl(plan):
            # A tree holding a granted path is covered by the policy check above.
            if any(
                user_ == other and (granted_path + "/").startswith(path + "/")
                for granted_path, user_ in granted
            ):
                continue
            if not _acl_tree_clean(path, other):
                raise ApplyError(f"named ACL outside the policy: {path}")
        # the Claude Code the agent runs is root's one install of the pinned build (KD-9474); a copy an older
        # install left in the home differs too, so the release's reinstall of the role removes it
        claude = claude_code.problems()
        if claude:
            raise ApplyError("; ".join(claude))
        for left in (".local/bin/claude", ".local/share/claude"):
            if os.path.lexists(f"/home/{user}/{left}"):
                raise ApplyError(f"/home/{user}/{left}: Claude Code of an older install — add_user.sh removes it")
        account = pwd.getpwnam(user)
        home_info = _safe_path(f"/home/{user}").stat()
        if (
            home_info.st_uid != account.pw_uid
            or stat.S_IMODE(home_info.st_mode) != 0o750
        ):
            raise ApplyError("agent home ownership or mode mismatch")
        for sub in ("memory", "work"):  # the agent's data: closed to other users (global review 15.10.2026)
            info = _safe_path(f"{self._folder(plan)}/{sub}").stat()
            if info.st_uid != account.pw_uid or stat.S_IMODE(info.st_mode) != 0o750:
                raise ApplyError(f"agent {sub}/ ownership or mode mismatch")
        for action in plan.actions:
            if action.kind == "service-user":
                service = pwd.getpwnam(action.target)
                info = _safe_path(f"/home/{action.target}").stat()
                if info.st_uid != service.pw_uid or stat.S_IMODE(info.st_mode) != 0o700:
                    raise ApplyError(f"service home mode mismatch: {action.target}")
        launch.plan_generated(
            plan.manifest.name,
            self._folder(plan),
            uid=account.pw_uid,
            user=user,
            agents_root=self.registry.resources.layout["agents"],
            fs=launch.HostFilesystem(),
            registry=registry_path,
        )
        # The trust file in the home is checked as its owner, not by root.
        user_check = """
import json, os, pathlib, stat, sys
folder, home = sys.argv[1:]
note = pathlib.Path(folder) / 'memory/notes.md'
assert note.is_file() and not note.is_symlink() and stat.S_IMODE(note.stat().st_mode) == 0o640
texts = pathlib.Path(folder) / 'work/tmp'
assert texts.is_dir() and not texts.is_symlink() and texts.stat().st_uid == os.getuid()
assert stat.S_IMODE(texts.stat().st_mode) == 0o750
for sub in ('memory/tasks', 'memory/chats', 'memory/legacy', 'work/tmp/cards', 'work/tmp/chats'):
    item = pathlib.Path(folder) / sub
    assert item.is_dir() and not item.is_symlink() and item.stat().st_uid == os.getuid(), sub
assert (pathlib.Path(folder) / 'memory/legacy/migration.json').is_file()
trust = pathlib.Path(home) / '.claude.json'
assert trust.is_file() and not trust.is_symlink()
assert stat.S_IMODE(trust.stat().st_mode) == 0o600
projects = json.loads(trust.read_text('utf-8'))['projects']
assert all(projects[path]['hasTrustDialogAccepted'] is True
           for path in (folder, folder + '/work'))
"""
        _run("runuser", "-u", user, "--", "/usr/bin/python3", "-I", "-c", user_check, self._folder(plan), f"/home/{user}")
        # These checks exercise the exact effective sudo, channels and sandbox configuration.
        self._verify_channels(plan)
        self._verify_sudo(plan)
        _run(
            "runuser",
            "-u",
            user,
            "--",
            "bwrap",
            "--ro-bind",
            "/",
            "/",
            "--dev-bind",
            "/dev",
            "/dev",
            "--proc",
            "/proc",
            "--unshare-all",
            "--die-with-parent",
            "--",
            "/usr/bin/true",
        )
        self._setup(plan, "check")

    def disable_runtime(self, plan: Plan) -> None:
        self._fail_closed(plan)

    def unhost(self, plan: Plan) -> None:
        """--disable: an agent on the session host leaves its list (the edit kd.session_config makes for
        session_host_setup.sh release); the cabinet then cancels its queued commands as not hosted."""
        path = self.session_host_config
        if not os.path.lexists(path):
            return
        err = io.StringIO()
        if session_config.main(["remove", plan.manifest.name], path=path, out=io.StringIO(), err=err) != 0:
            raise ApplyError(f"session-host: {err.getvalue().strip() or path}")

    def _fail_closed(self, plan: Plan) -> None:
        """A generated registry entry without a policy: the launcher refuses the agent."""
        path = self._registry_path()
        _root_parent(path)
        data = _runtime_registry(path)
        entry = data.get(plan.manifest.name)
        generated = (
            isinstance(entry, dict) and entry.get("runtime") == GENERATED_RUNTIME
        )
        if not generated and entry is not None:
            raise ApplyError("refusing to disable an entry that is not the generated runtime")
        if not generated:
            data[plan.manifest.name] = _registry_entry(plan)
            _atomic(
                path,
                (json.dumps(data, sort_keys=True, indent=2) + "\n").encode(),
                0o644,
            )
        policy = _safe_path(self._policy_path(plan))
        if policy.exists():
            if not policy.is_file():
                raise ApplyError("generated policy is not a file")
            policy.unlink()


def _registry_entry(plan: Plan) -> dict[str, str]:
    """The agent's runtime registry entry."""
    return {"runtime": GENERATED_RUNTIME, "user": plan.manifest.user}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="new_agent.sh", allow_abbrev=False)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--install", metavar="AGENT")
    mode.add_argument("--disable", metavar="AGENT")
    mode.add_argument("--verify", metavar="AGENT")
    args = parser.parse_args(argv)
    name = args.install or args.disable or args.verify
    if os.name != "posix" or os.geteuid() != 0:
        print("new_agent.sh: Linux root required", file=sys.stderr)
        return 1
    if not agents.NAME_RE.fullmatch(name):
        print("new_agent.sh: invalid agent name", file=sys.stderr)
        return 2
    if args.verify:
        return verify_main(name)
    repo = Path(__file__).resolve().parents[2]
    try:
        import fcntl

        # One lock for every agent: agents.json is shared across installations.
        lock_path = "/run/lock/kd-new-agent.lock"
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        plan = (
            disable_plan(repo, name)
            if args.disable
            else build_plan(repo, HostView("/"), name)
        )
        ops = SystemOperations(repo)
        if args.install:
            changed = install(plan, ops)
            print(f"{name}: {'installed' if changed else 'unchanged'}")
        else:
            disable(plan, ops)
            print(f"{name}: disabled; home, auth and secrets preserved")
        return 0
    except OwnerStepError as exc:
        print(f"new_agent.sh: {exc}", file=sys.stderr)
        return 3
    except (ApplyError, PlanError, OSError, ValueError) as exc:
        print(f"new_agent.sh: {exc}", file=sys.stderr)
        return 1
