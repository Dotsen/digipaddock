"""Fixed smoke suite in a private bwrap namespace.

The base executor has no production data mounted inside the namespace. Runtime
preparation is a trusted deployment step, not an agent request or package install.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile

from kd import agents, e2e_seccomp, ids
from kd.manifest import load_manifest

RUNTIME = Path("/var/lib/digipaddock/e2e/runtime")
RUNS = Path("/var/lib/digipaddock/e2e/runs")
REPO = Path("/opt/digipaddock")
SUITES = ("cabinet-smoke",)
TIMEOUT = 180
MAX_USER_TASKS = 512
# What a run may keep for diagnosis: a bounded count and size per run, and the newest runs only.
MAX_ARTIFACTS = 50
MAX_ARTIFACT_BYTES = 64 << 20
KEEP_ARTIFACTS = 10
CARD = ids.CARD
SHA = re.compile(r"[0-9a-f]{40}")


class Unavailable(ValueError):
    pass


def runtime_info(root=RUNTIME, *, require_root_owner=True):
    root = Path(root)
    # No writable dependency may become an executor program. Symlinks are allowed
    # only to read-only contents of this bundle or the fixed system runtime mounts.
    if not root.is_dir() or root.is_symlink():
        raise Unavailable("prepared test runtime is missing")
    for directory, dirs, files in os.walk(root, followlinks=False):
        for path in [
            Path(directory),
            *(Path(directory) / name for name in dirs + files),
        ]:
            info = path.lstat()
            if require_root_owner and (
                info.st_uid != 0 or (not path.is_symlink() and info.st_mode & 0o022)
            ):
                raise Unavailable("test runtime is not root-owned and read-only")
            if not (
                stat.S_ISREG(info.st_mode)
                or stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
            ):
                raise Unavailable("test runtime contains a special file")
            if path.is_symlink():
                resolved = path.resolve()
                if not (
                    resolved.is_relative_to(root.resolve())
                    or any(
                        resolved.is_relative_to(p)
                        for p in (Path("/usr"), Path("/lib"), Path("/lib64"))
                    )
                ):
                    raise Unavailable(
                        "test runtime link escapes permitted read-only mounts"
                    )
    try:
        info = json.loads((root / "revision.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Unavailable("test runtime revision is missing or invalid") from exc
    if (
        not isinstance(info, dict)
        or set(info) != {"commit"}
        or not isinstance(info["commit"], str)
        or not SHA.fullmatch(info["commit"])
    ):
        raise Unavailable("test runtime needs a full observed commit SHA")
    for name in (
        "app/common/cabinet/web/node_modules/@playwright/test/cli.js",
        "app/common/cabinet/web/e2e/isolated.config.ts",
        "app/common/cabinet/web/e2e/hosts",
        "app/common/cabinet/web/e2e/nsswitch.conf",
        "app/common/cabinet/web/dist/index.html",
        "app/common/bin/kd-cabinet",
        "app/tmp",
        "browsers",
        "python/bin/python",
    ):
        if not (root / name).exists():
            raise Unavailable("prepared test runtime is incomplete: " + name)
    return info


def namespace_argv(runtime, run_dir, seccomp_fd=None):
    """No host home, credentials, board sockets or live databases are mounted. ``seccomp_fd`` — a descriptor bwrap
    reads the filter of kd.e2e_seccomp from before it starts the suite."""
    argv = [
        "/usr/bin/bwrap",
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
    ]
    if seccomp_fd is not None:
        argv += ["--seccomp", str(int(seccomp_fd))]
    argv += [
        "--clearenv",
    ]
    for path in ("/usr", "/bin", "/lib", "/lib64", "/etc/fonts", "/etc/ld.so.cache"):
        if Path(path).exists():
            argv += ["--ro-bind", path, path]
    argv += [
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--ro-bind",
        str(runtime),
        "/runtime",
        "--ro-bind",
        str(runtime / "app/common/cabinet/web/e2e/hosts"),
        "/etc/hosts",
        "--ro-bind",
        str(runtime / "app/common/cabinet/web/e2e/nsswitch.conf"),
        "/etc/nsswitch.conf",
        "--bind",
        str(run_dir),
        "/runtime/app/tmp",
        "--dir",
        "/home/test",
        "--chdir",
        "/runtime/app/common/cabinet/web",
    ]
    for name, value in {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/test",
        "LANG": "C.UTF-8",
        "PLAYWRIGHT_BROWSERS_PATH": "/runtime/browsers",
        "KD_PYTHON": "/runtime/python/bin/python",
    }.items():
        argv += ["--setenv", name, value]
    return argv + [
        "/usr/bin/node",
        "/runtime/app/common/cabinet/web/node_modules/@playwright/test/cli.js",
        "test",
        "--config",
        "e2e/isolated.config.ts",
    ]


def _seccomp_pipe() -> int:
    """The read end of a pipe holding the whole filter (a few hundred bytes: within one pipe buffer)."""
    data = e2e_seccomp.program()
    read, write = os.pipe()
    try:
        os.write(write, data)
    finally:
        os.close(write)
    return read


def _limits():
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (TIMEOUT, TIMEOUT))
    resource.setrlimit(resource.RLIMIT_FSIZE, (32 << 20, 32 << 20))
    resource.setrlimit(resource.RLIMIT_NOFILE, (512, 512))
    # Linux counts every thread of the real UID, including the base account's
    # other services. A 128 ceiling can refuse bwrap before this run even starts.
    resource.setrlimit(resource.RLIMIT_NPROC, (MAX_USER_TASKS, MAX_USER_TASKS))


def run_suite(
    suite,
    *,
    runtime=RUNTIME,
    runs=RUNS,
    popen=subprocess.Popen,
    require_root_owner=True,
):
    if suite not in SUITES:
        raise ValueError("unknown test suite; URL and shell input are not accepted")
    info = runtime_info(runtime, require_root_owner=require_root_owner)
    if sys.platform != "linux" or not Path("/usr/bin/bwrap").is_file():
        raise Unavailable(
            "Linux bubblewrap runtime is unavailable; no unsandboxed fallback"
        )
    try:
        e2e_seccomp.program()
    except e2e_seccomp.NoFilter as exc:
        raise Unavailable(f"{exc}; no unfiltered fallback") from None
    runs = Path(runs)
    if runs.is_symlink():
        raise Unavailable("artifact directory must not be a link")
    runs.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="e2e-", dir=runs) as folder:
        os.chmod(folder, 0o700)
        result = dict(suite=suite, commit=info["commit"], state="failed")
        # The log is read back from this handle, never by its path: the folder is the namespace's writable tmp, and
        # what ran there may have put a link or a FIFO in the log's place (review 04.11.2026).
        with (Path(folder) / "executor.log").open("w+b") as log:
            filter_fd = _seccomp_pipe()
            try:
                proc = popen(
                    namespace_argv(runtime, folder, seccomp_fd=filter_fd),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                    start_new_session=True,
                    preexec_fn=_limits,
                    pass_fds=(filter_fd,),
                )
            finally:
                os.close(filter_fd)
            try:
                code = proc.wait(timeout=TIMEOUT)
                result.update(state="passed" if code == 0 else "failed", exit_code=code)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                result.update(state="failed", reason="timeout")
            log.flush()
            log.seek(0)
            log_text = log.read(16384).decode("utf-8", errors="replace")
        if result["state"] == "failed" and any(
            reason in log_text
            for reason in ("bwrap:", "Executable doesn't exist", "No usable sandbox")
        ):
            result.update(
                state="blocked",
                reason="browser or namespace runtime unavailable; inspect private artifacts",
            )
        # Only metadata leaves the namespace; logs/traces can contain synthetic login
        # codes. Copying or publishing those files is deliberately not an agent API.
        artifacts, total, dropped = [], 0, 0
        for path in sorted(Path(folder).rglob("*")):
            if (
                path.is_file()
                and not path.is_symlink()
                and path.suffix in (".zip", ".png", ".log")
            ):
                size = path.stat().st_size
                if len(artifacts) >= MAX_ARTIFACTS or total + size > MAX_ARTIFACT_BYTES:
                    dropped += 1
                    continue
                total += size
                artifacts.append(
                    dict(
                        name=str(path.relative_to(folder)),
                        size=size,
                        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    )
                )
        result["artifacts"] = artifacts
        if dropped:
            result["artifacts_dropped"] = dropped
        # Keep diagnostic artifacts private after TemporaryDirectory removes the run
        # data and synthetic credentials. A caller cannot choose the destination.
        if artifacts:
            import shutil

            saved = Path(tempfile.mkdtemp(prefix="artifacts-", dir=runs))
            os.chmod(saved, 0o700)
            for i, entry in enumerate(artifacts):
                shutil.copyfile(
                    Path(folder) / entry["name"],
                    saved / (str(i) + Path(entry["name"]).suffix),
                    follow_symlinks=False,
                )
            result["artifact_id"] = saved.name
            _prune(runs)
        return result


def _prune(runs):
    """The newest KEEP_ARTIFACTS saved runs stay; older ones go (the runs folder is on the board's disk)."""
    import shutil

    saved = sorted(
        (p for p in Path(runs).glob("artifacts-*") if p.is_dir() and not p.is_symlink()),
        key=lambda p: p.stat().st_mtime,
    )
    for old in saved[:-KEEP_ARTIFACTS]:
        shutil.rmtree(old, ignore_errors=True)


def main(
    argv=None,
    *,
    caller,
    client,
    registry_path=agents.REGISTRY,
    repo_root=REPO,
    out=sys.stdout,
):
    parser = argparse.ArgumentParser(description=__doc__)
    operations = parser.add_subparsers(dest="op", required=True)
    operations.add_parser("list")
    run = operations.add_parser("run")
    run.add_argument("suite", choices=SUITES)
    run.add_argument("--card", required=True)
    args = parser.parse_args(argv)
    if (
        caller is None
        or not caller[0]
        or agents.user_of(caller[0], registry_path) != caller[1]
    ):
        raise ValueError("no capability channel names the caller")
    manifest = load_manifest(Path(repo_root) / "agents" / caller[0] / "agent.json")
    if "e2e.request" not in manifest.capabilities:
        raise ValueError("e2e.request capability is missing")
    if args.op == "list":
        out.write("\n".join(SUITES) + "\n")
        return 0
    if not CARD.fullmatch(args.card):
        raise ValueError("invalid card id")
    card = client.call({"op": "card.get", "card": args.card})["card"]
    if card["assignee"] != "@" + caller[0] or card["status"] != "In Progress":
        raise ValueError("only the assignee of an approved card can request a test")
    try:
        result = run_suite(args.suite)
    except (Unavailable, OSError) as exc:
        result = dict(suite=args.suite, state="blocked", reason=str(exc))
    client.call(
        {
            "op": "worker.edit",
            "agent": caller[0],
            "card": args.card,
            "append_notes": ["e2e: " + json.dumps(result, ensure_ascii=True)],
        }
    )
    out.write(json.dumps(result) + "\n")
    return (
        0 if result["state"] == "passed" else 4 if result["state"] == "blocked" else 1
    )
