#!/usr/bin/env python3
"""Run explicitly selected checks and keep verbose output outside the agent's context."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parent.parent
WEB = "common/cabinet/web"
PROFILES = ("docs", "static", "focused", "frontend", "full", "ci")
# The ci profile's overlay lives in the run's private scratch, never in the checkout.
SCRATCH = "{scratch}"
LINUX_ONLY = ("full", "ci")


def test_path(value: str, root: Path, *, web: bool = False) -> str:
    """Keep explicit selections inside the checkout; options and whole-suite paths are not tests."""
    filename = value.split("::", 1)[0]
    base = root / WEB if web else root
    path = (base / filename).resolve()
    if (
        filename.startswith("-")
        or not path.is_relative_to(base.resolve())
        or not path.is_file()
        or (web and not path.name.endswith((".test.ts", ".spec.ts")))
        or (
            not web
            and not (
                path.suffix == ".py"
                and (path.name.startswith("test_") or path.name.endswith("_test.py"))
            )
        )
    ):
        raise ValueError(f"Choose an existing test file inside {base}: {value}")
    return value


def plan(
    profiles: list[str],
    tests: list[str],
    web_tests: list[str],
    base: str,
    root: Path,
    workers: int = 1,
) -> list[dict]:
    if not profiles or any(profile not in PROFILES for profile in profiles):
        raise ValueError("Choose an explicit check profile")
    if base.startswith("-"):
        raise ValueError("The base must be a Git revision, not an option")
    if workers not in (1, 2, 4):
        raise ValueError("Choose 1, 2 or 4 workers")
    if workers != 1 and not {"focused", "full", "ci"}.intersection(profiles):
        raise ValueError("--workers requires a Python check profile")
    if "focused" in profiles and not tests:
        raise ValueError(
            "The focused profile requires at least one --test file or node ID"
        )
    if tests and "focused" not in profiles:
        raise ValueError("--test requires the focused profile")
    if web_tests and "frontend" not in profiles:
        raise ValueError("--web-test requires the frontend profile")
    for value in tests:
        test_path(value, root)
    for value in web_tests:
        test_path(value, root, web=True)

    npm = "npm"
    if {"frontend", "ci"}.intersection(profiles):
        npm = shutil.which("npm.cmd" if os.name == "nt" else "npm") or npm
    static = [sys.executable, "scripts/check_changed.py", "--base", base]
    # what a push would publish (origin/main..HEAD and the uncommitted changes), scanned as kd-mirror scans
    # it
    history = [sys.executable, "scripts/check_history.py"]
    parallel = ["-n", str(workers), "--dist=loadfile"] if workers > 1 else []
    overlay = f"{SCRATCH}/overlay"
    web = [
        [npm, "--prefix", WEB, "run", "check"],
        [npm, "--prefix", WEB, "test"],
        [npm, "--prefix", WEB, "run", "build"],
    ]
    commands = {
        "docs": [["git", "diff", "--check", base, "--"]],
        "static": [static, history],
        "focused": [
            [
                sys.executable,
                "-m",
                "pytest",
                *parallel,
                "-x",
                "--tb=short",
                "-rs",
                *tests,
            ]
        ],
        "frontend": [
            web[0],
            [*web[1], *(["--", *web_tests] if web_tests else [])],
            web[2],
        ],
        "full": [
            static,
            history,
            [sys.executable, "-m", "pytest", *parallel, "-rfs", "--durations=20"],
        ],
        # GitHub CI (.github/workflows/ci.yml) runs exactly this: the checks devcheck/unit runs,
        # minus the tests that need the factory host itself.
        "ci": [
            [
                sys.executable,
                "-I",
                "common/kd/privacy_scan.py",
                "--tracked",
                ".",
                "--config",
                "privacy-scan.json",
            ],
            static,
            (
                [sys.executable, "-m", "pytest", *parallel, "-m", "not host", "-rfs"],
                {"KD_HOST_JSON": "host/host.example.json"},
            ),
            [sys.executable, "-I", "common/kd/overlay.py", "init", overlay],
            [sys.executable, "-I", "scripts/overlay_stand_ins.py", overlay],
            [sys.executable, "-I", "common/kd/overlay.py", "check", overlay],
            *web,
        ],
    }
    result = []
    seen = set()
    for profile in dict.fromkeys(profiles):
        for item in commands[profile]:
            argv, env = item if isinstance(item, tuple) else (item, {})
            key = (tuple(argv), tuple(sorted(env.items())))
            if key not in seen:
                result.append(
                    {"profile": profile, "argv": argv, **({"env": env} if env else {})}
                )
                seen.add(key)
    return result


def tail(path: Path, lines: int = 20) -> list[str]:
    with path.open("rb") as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - 65536))
        return stream.read().decode("utf-8", errors="replace").splitlines()[-lines:]


def source_info(root: Path) -> dict:
    result = {}
    for name, args in (
        ("revision", ["rev-parse", "HEAD"]),
        ("dirty", ["status", "--porcelain"]),
    ):
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=10,
            )
            value = proc.stdout.strip() if proc.returncode == 0 else None
            result[name] = (
                bool(value) if name == "dirty" and value is not None else value
            )
        except (OSError, subprocess.TimeoutExpired):
            result[name] = None
    return result


def execute(
    commands: list[dict],
    profiles: list[str],
    root: Path,
    output: Path,
    scratch_root: Path | None = None,
) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    # The agent's sandbox only writes in its checkout. An external Linux test
    # environment can explicitly choose a shorter root for AF_UNIX socket names.
    with tempfile.TemporaryDirectory(
        prefix="kdc-", dir=scratch_root if scratch_root is not None else output
    ) as temporary:
        return _execute(commands, profiles, root, output, Path(temporary))


def _execute(
    commands: list[dict], profiles: list[str], root: Path, output: Path, temporary: Path
) -> dict:
    report = {
        "profiles": profiles,
        "status": "passed",
        "platform": sys.platform,
        "source": source_info(root),
        "planned_commands": len(commands),
        "commands": [],
    }
    env = dict(os.environ, NO_COLOR="1", PYTHONUNBUFFERED="1")
    env.pop("FORCE_COLOR", None)
    env.update(TMPDIR=str(temporary), TMP=str(temporary), TEMP=str(temporary))
    started = time.monotonic()
    print(f"Checks: {', '.join(profiles)} | logs: {output}", flush=True)
    for index, command in enumerate(commands, 1):
        command = {
            **command,
            "argv": [arg.replace(SCRATCH, str(temporary)) for arg in command["argv"]],
        }
        if command["argv"][1:3] == ["-m", "pytest"]:
            command["argv"][3:3] = ["--basetemp", str(temporary / str(index))]
        log = output / f"{index:02d}-{command['profile']}.log"
        before = time.monotonic()
        with log.open("wb") as stream:
            try:
                proc = subprocess.run(
                    command["argv"],
                    cwd=root,
                    env={**env, **command.get("env", {})},
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                )
                code = (
                    proc.returncode if proc.returncode >= 0 else 128 - proc.returncode
                )
            except OSError as exc:
                stream.write(f"Cannot start check: {exc}\n".encode())
                code = 127
            except KeyboardInterrupt:
                stream.write(b"Check interrupted\n")
                code = 130
        summaries = [
            line.strip("= ")
            for line in tail(log, 80)
            if re.search(
                r"\b\d+ (?:passed|failed|skipped|errors?)\b|^\s*(?:Test Files|Tests)\s+\d",
                line,
            )
        ][-3:]
        report["commands"].append(
            {
                **command,
                "returncode": code,
                "duration_s": round(time.monotonic() - before, 2),
                "log": str(log),
                "summary": summaries,
            }
        )
        if code:
            report["status"] = "failed"
            # The failed command and its log's tail: enough to diagnose a CI job without artifacts.
            print(f"Failed ({code}): {shlex.join(command['argv'])} | log: {log}")
            print("\n".join(tail(log))[-4000:])
            break
    report["duration_s"] = round(time.monotonic() - started, 2)
    report["exit_code"] = report["commands"][-1]["returncode"]
    (output / "summary.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"{report['status'].upper()} | {report['duration_s']}s | report: {output / 'summary.json'}"
    )
    for command in report["commands"]:
        for summary in command["summary"]:
            print(summary)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", action="append", choices=PROFILES, required=True)
    parser.add_argument(
        "--scratch-root",
        type=Path,
        help="Existing writable parent for private scratch (default: the report directory); external Linux checks can use /tmp",
    )
    parser.add_argument(
        "--workers",
        type=int,
        choices=(1, 2, 4),
        default=1,
        help="Python processes: 1 (default, sequential), 2 or 4 (xdist loadfile)",
    )
    parser.add_argument(
        "--test",
        action="append",
        default=[],
        help="Python test file or node ID; repeat for more",
    )
    parser.add_argument(
        "--web-test",
        action="append",
        default=[],
        help="Test file relative to common/cabinet/web",
    )
    parser.add_argument(
        "--base", default="HEAD", help="Base revision for docs/static/ci checks"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan without running checks or writing logs",
    )
    args = parser.parse_args(argv)
    if args.scratch_root is not None:
        args.scratch_root = args.scratch_root.resolve()
        if not args.scratch_root.is_dir():
            parser.error("--scratch-root must be an existing directory")
    try:
        commands = plan(
            args.profile, args.test, args.web_test, args.base, ROOT, args.workers
        )
    except ValueError as exc:
        parser.error(str(exc))
    if args.dry_run:
        print(json.dumps({"status": "planned", "commands": commands}, indent=2))
        return 0
    linux_only = [p for p in LINUX_ONLY if p in args.profile]
    if linux_only and not sys.platform.startswith("linux"):
        parser.error(
            f"The {linux_only[0]} profile requires Linux; use focused checks here or --dry-run to inspect its plan"
        )
    output = ROOT / "tmp" / "checks" / f"{time.time_ns()}-{uuid.uuid4().hex[:8]}"
    return execute(commands, args.profile, ROOT, output, args.scratch_root)["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
