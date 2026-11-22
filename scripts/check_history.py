#!/usr/bin/env python3
"""Scan the history a push adds to the forge, before the push.

The forge's history cannot be edited after the merge: a line stays in every clone for good, even one a later commit
removed (a literal added in one commit and removed in the next counts). This check reads every commit message with its
author and committer, every path and every added line of the commits main has not got yet (``origin/main..HEAD``),
plus the uncommitted and untracked changes, with the public scanner (the checkout's privacy-scan.json) and, given a
deployment's literal list, the closed one. A closed finding names its
commit or path and line, never the literal.

    python scripts/check_history.py [--base origin/main] [--private-list <host/private_scan.json>]

The list: --private-list, else $KD_PRIVATE_SCAN_LIST, else the running release's own when there is one; none — the
closed scanner is skipped and said so. No base to compare with — skipped and said so. Exit 1 on a finding, 2 on an
error.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "common"))
from kd import history_scan, mirror, privacy_scan, private_scan  # noqa: E402

LIST_ENV = "KD_PRIVATE_SCAN_LIST"
RELEASE_LIST = Path(mirror.TREE) / "host" / "private_scan.json"


def git(root: Path, *args: str) -> str:
    return history_scan.git_text(root, *args)


def untracked(root: Path) -> list:
    """(path, number, line) of every line of an untracked text file, as an added line."""
    found = []
    for name in git(root, "ls-files", "--others", "--exclude-standard", "-z").split(
        "\0"
    ):
        path = root / name
        if not name or path.is_symlink() or not path.is_file():
            continue
        data = path.read_bytes()
        found.append(("path", 0, name))
        if b"\0" not in data[: privacy_scan.BINARY_PROBE]:
            text = data.decode("utf-8", "replace")
            found += [
                (name, number, line) for number, line in enumerate(text.splitlines(), 1)
            ]
    return found


def records(root: Path, base: str) -> list:
    """The span as the mirror reads it, then the uncommitted and the untracked changes as added lines."""
    work = git(root, "diff", "--no-renames", "HEAD", "--")
    return (
        history_scan.span(root, base) + history_scan.history("", work) + untracked(root)
    )


def private_list(value: str | None) -> Path | None:
    chosen = value or os.environ.get(LIST_ENV)
    if chosen:
        return Path(chosen)
    return RELEASE_LIST if RELEASE_LIST.is_file() else None


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--base",
        default="origin/main",
        help="what is published already (default: origin/main)",
    )
    parser.add_argument(
        "--private-list",
        help=f"the closed scanner's literal list (default: ${LIST_ENV})",
    )
    args = parser.parse_args(argv)
    if args.base.startswith("-"):
        print("history: the base must be a Git revision", file=sys.stderr)
        return 2
    known = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{args.base}^{{commit}}"],
        cwd=root,
        capture_output=True,
    )
    if known.returncode:
        print(f"history: no {args.base} to compare with; skipped")
        return 0
    listing = private_list(args.private_list)
    try:
        cfg = privacy_scan.load_config(root / "privacy-scan.json")
        patterns = private_scan.load(listing) if listing else []
        recs = records(root, args.base)
        count = int(git(root, "rev-list", "--count", f"{args.base}..HEAD"))
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"history: {exc}", file=sys.stderr)
        return 2
    closed = (
        "closed scanner on"
        if listing
        else f"closed scanner skipped: no list (--private-list or ${LIST_ENV})"
    )
    found = history_scan.scan(recs, cfg, patterns)
    commits = f"{count} commit{'' if count == 1 else 's'} since {args.base} and the uncommitted changes"
    if found:
        for line in found:
            print(line, file=sys.stderr)
        print(
            f"history: {len(found)} finding(s) in {commits}; {closed}. Rewrite those commits before the push: "
            "the mirror blocks main on such a line for good.",
            file=sys.stderr,
        )
        return 1
    print(f"history: {commits} clean; {closed}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
