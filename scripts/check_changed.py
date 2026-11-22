#!/usr/bin/env python3
"""Check changed code without reformatting or linting untouched legacy files."""

from __future__ import annotations

import argparse
import difflib
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUFF_RULES = "E4,E7,E9,F"


def run(
    args: list[str], *, input_text: str | None = None, cwd: Path = ROOT
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        input=input_text,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def git_paths(base: str, root: Path = ROOT) -> list[Path]:
    """Return changed and untracked paths, excluding deletions and ignored files."""
    changed = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMRT", "-z", base, "--"],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z", "--"],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout
    names = {
        name.decode("utf-8", errors="surrogateescape")
        for name in (changed + untracked).split(b"\0")
        if name
    }
    result = []
    for name in sorted(names):
        path = root / name
        if path.is_symlink():
            raise ValueError(f"Changed path is a symlink: {name}")
        if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
            continue
        result.append(path)
    return result


def renamed_from(base: str, root: Path = ROOT) -> dict[str, str]:
    """Map each path renamed since the base commit to its old path."""
    out = subprocess.run(
        ["git", "diff", "-M", "--name-status", "--diff-filter=R", "-z", base, "--"],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout
    fields = [f.decode("utf-8", errors="surrogateescape") for f in out.split(b"\0")]
    return {fields[i + 2]: fields[i + 1] for i in range(0, len(fields) - 2, 3)}


def kind(path: Path, source: str) -> str | None:
    if path.suffix == ".py":
        return "python"
    if path.suffix == ".sh":
        return "bash"
    first_line = source.partition("\n")[0].lower()
    if first_line.startswith("#!") and "python" in first_line:
        return "python"
    if first_line.startswith("#!") and "bash" in first_line:
        return "bash"
    return None


def baseline(base: str, relative: Path, root: Path = ROOT) -> str | None:
    proc = run(["git", "show", f"{base}:{relative.as_posix()}"], cwd=root)
    return proc.stdout if proc.returncode == 0 else None


def ruff_diagnostics(source: str, filename: str) -> list[dict]:
    proc = run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--select",
            RUFF_RULES,
            "--target-version",
            "py312",
            "--output-format",
            "json",
            "--stdin-filename",
            filename,
            "-",
        ],
        input_text=source,
    )
    if not proc.stdout.strip():
        raise RuntimeError(
            "Ruff unavailable; run python -m pip install -r requirements-dev.txt"
        )
    if proc.returncode not in (0, 1):
        raise RuntimeError(proc.stderr.strip() or "Ruff failed")
    return json.loads(proc.stdout)


def new_diagnostics(
    current: list[dict], old: list[dict], current_source: str, old_source: str
) -> list[dict]:
    """Grandfather findings only on lines unchanged since the base commit."""
    unchanged_rows = {}
    matcher = difflib.SequenceMatcher(
        a=old_source.splitlines(), b=current_source.splitlines(), autojunk=False
    )
    for old_start, current_start, size in matcher.get_matching_blocks():
        for offset in range(size):
            unchanged_rows[old_start + offset + 1] = current_start + offset + 1
    counts = Counter(
        (item["code"], item["message"], unchanged_rows[item["location"]["row"]])
        for item in old
        if item["location"]["row"] in unchanged_rows
    )
    fresh = []
    for item in current:
        key = item["code"], item["message"], item["location"]["row"]
        if counts[key]:
            counts[key] -= 1
        else:
            fresh.append(item)
    return fresh


def bash_executable() -> str | None:
    candidates = [shutil.which("bash")]
    if os.name == "nt":
        candidates.insert(0, r"C:\Program Files\Git\bin\bash.exe")
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            try:
                if run([candidate, "-c", "true"]).returncode == 0:
                    return candidate
            except OSError:
                pass
    return None


def check(base: str, root: Path = ROOT) -> int:
    errors = []
    revision = run(
        ["git", "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}"],
        cwd=root,
    )
    if revision.returncode:
        print(f"Invalid base revision: {base}", file=sys.stderr)
        return 2
    whitespace = run(["git", "diff", "--check", base, "--"], cwd=root)
    if whitespace.returncode:
        errors.append(whitespace.stdout.strip() or whitespace.stderr.strip())
    try:
        paths = git_paths(base, root)
        moved = renamed_from(base, root)
    except (subprocess.CalledProcessError, ValueError) as exc:
        print(f"Cannot list changed files: {exc}", file=sys.stderr)
        return 2
    bash = bash_executable()
    shellcheck = shutil.which("shellcheck")
    checked = {"python": 0, "bash": 0}
    for path in paths:
        relative = path.relative_to(root)
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeError as exc:
            # a binary asset (an icon, an image) is not source; a script never is binary
            if path.suffix in (".py", ".sh"):
                errors.append(f"{relative}: cannot read UTF-8 source: {exc}")
            continue
        except OSError as exc:
            errors.append(f"{relative}: cannot read UTF-8 source: {exc}")
            continue
        language = kind(path, source)
        if language is None:
            continue
        checked[language] += 1
        # A moved file is compared with its old path: a rename is not new code.
        previous = baseline(base, Path(moved.get(relative.as_posix(), relative)), root)
        if language == "python":
            filename = relative.as_posix() + (".py" if not path.suffix else "")
            try:
                current_issues = ruff_diagnostics(source, filename)
                old_issues = (
                    ruff_diagnostics(previous, filename) if previous is not None else []
                )
                for issue in new_diagnostics(
                    current_issues, old_issues, source, previous or ""
                ):
                    errors.append(
                        f"{relative}:{issue['location']['row']}: {issue['code']} {issue['message']}"
                    )
                if previous is None:
                    formatted = run(
                        [
                            sys.executable,
                            "-m",
                            "ruff",
                            "format",
                            "--check",
                            "--target-version",
                            "py312",
                            "--stdin-filename",
                            filename,
                            "-",
                        ],
                        input_text=source,
                    )
                    if formatted.returncode:
                        errors.append(
                            f"{relative}: Ruff format check failed ({formatted.stderr.strip() or formatted.stdout.strip()})"
                        )
            except (RuntimeError, ValueError) as exc:
                errors.append(f"{relative}: {exc}")
        else:
            if bash is None:
                errors.append(
                    f"{relative}: Bash unavailable (install Git Bash or Bash)"
                )
            else:
                syntax = run([bash, "-n", str(path)], cwd=root)
                if syntax.returncode:
                    errors.append(
                        f"{relative}: bash -n failed: {syntax.stderr.strip()}"
                    )
            if previous is None and shellcheck:
                lint = run([shellcheck, "--severity=warning", str(path)], cwd=root)
                if lint.returncode:
                    errors.append(
                        f"{relative}: ShellCheck failed: {lint.stdout.strip() or lint.stderr.strip()}"
                    )
    for error in errors:
        print(error, file=sys.stderr)
    print(
        f"Checked {checked['python']} Python and {checked['bash']} Bash files; ShellCheck {'available' if shellcheck else 'not installed (optional)'}."
    )
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        default="HEAD",
        help="Git commit/ref to compare with the working tree (default: HEAD)",
    )
    args = parser.parse_args(argv)
    return check(args.base)


if __name__ == "__main__":
    sys.exit(main())
