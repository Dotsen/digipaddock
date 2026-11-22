"""What a span of history holds: every commit message with its author and committer, every path and every added
line, by the public scanner and the closed one.

The forge's history keeps a line for good once merged, even one a later commit removed, in every clone of it.
``devcheck/unit`` scans a PR's own commits (``refs/kd/base..HEAD``) and ``scripts/check_history.py`` the commits main
has not got yet, before the push, so such a line never reaches main. (kd-mirror publishes snapshots of main's tree, not
this history.)

    python3 -I common/kd/history_scan.py --config privacy-scan.json --list host/private_scan.json <repo> <base>

Prints ``<commit h|path|file>:<line>: <kind>``; a closed finding is ``private literal``, never the literal itself.
Exit 1 with findings, 0 clean, 2 on an error (git, a broken list or config). Stdlib and the two scanners only.
"""

import argparse
import fnmatch
import hashlib
import re
import subprocess
import sys
from pathlib import Path

if __package__ in (
    None,
    "",
):  # run as a script: devcheck's sandbox runs python3 -I common/kd/history_scan.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kd import privacy_scan, private_scan  # noqa: E402

# Messages are NUL-separated (git keeps no NUL in a message): the first line is the label, the rest the message;
# git push publishes the names and emails too, so they are lines of the message here.
LOG_FORMAT = "--format=commit %h%nauthor %an <%ae>%ncommitter %cn <%ce>%n%B"
_DIFF = "diff --git a/"
_HUNK = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)")


def _messages(text):
    for chunk in text.split("\0"):
        label, _, body = chunk.strip("\n").partition("\n")
        if label:
            if label.startswith("tag "):
                yield label, 0, label[len("tag ") :]  # the tag's name goes out with it
            yield from (
                (label, number, line) for number, line in enumerate(body.split("\n"), 1)
            )


def _diff_path(line):
    """The path of a ``diff --git a/P b/P`` header (renames are off: both sides are one path); a quoted header
    as it is — its text is scanned all the same."""
    rest = line[len(_DIFF) :]
    half = (len(rest) - len(" b/")) // 2
    return (
        rest[:half]
        if rest[half : half + 3] == " b/" and rest[half + 3 :] == rest[:half]
        else rest
    )


def _added(diff):
    """(path, new line number, line) of every added line, and ("path", 0, path) of every path the history touches;
    removed lines and the content of deleted files are not published."""
    path, number, state = None, 0, "none"
    for line in diff.split("\n"):
        if line.startswith(_DIFF):
            yield "path", 0, _diff_path(line)
            path, state = None, "header"
        elif line.startswith("diff --git "):
            yield "path", 0, line[len("diff --git ") :]
            path, state = None, "header"
        elif state == "header":
            if line.startswith("+++ "):
                target = line[4:]
                path = (
                    None
                    if target == "/dev/null"
                    else (target[2:] if target.startswith("b/") else target)
                )
            elif line.startswith("@@"):
                match = _HUNK.match(line)
                number, state = (int(match.group(1)) if match else 0), "hunk"
        elif state == "hunk":
            if line.startswith("@@"):
                match = _HUNK.match(line)
                number = int(match.group(1)) if match else 0
            elif line.startswith("+"):
                if path is not None:
                    yield path, number, line[1:]
                number += 1
            elif line.startswith(" "):
                number += 1


def history(messages, diff, paths=""):
    """Records (where, line number, line): ``commit <h>`` / ``tag <ref>`` message lines, then the paths of a tree
    (``paths``: ``git ls-tree -r --name-only -z``) and of the history, then added lines."""
    return [
        *_messages(messages),
        *(("path", 0, name) for name in paths.split("\0") if name),
        *_added(diff),
    ]


def _published(records, exclude):
    for where, number, line in records:
        if (
            where.startswith(("commit ", "tag "))
            or where == "path"
            or not any(fnmatch.fnmatchcase(where, glob) for glob in exclude)
        ):
            yield where, number, line


def history_text(records):
    """Message lines, paths and every added line — what the closed scanner reads as one file (the public exclude
    list is the public scanner's: the closed one reads the whole tree, review M2)."""
    return "".join(line + "\n" for _, _, line in records)


def line_digest(line):
    """sha256 of a history line without its indentation: the line's name in a finding."""
    return hashlib.sha256(line.strip().encode("utf-8")).hexdigest()


def public_history_findings(records, cfg, allowed=frozenset()):
    """The public scanner over the history: ["<where>:<line>: <kind> (allow <digest>)", …]; a line whose digest
    is in ``allowed`` is not scanned."""
    domains = privacy_scan._domain_patterns(cfg)
    found = []
    for where, number, line in _published(records, cfg.exclude):
        digest = line_digest(line)
        if digest in allowed:
            continue
        found += [
            f"{where}:{number}: {f.kind} (allow {digest})"
            for f in privacy_scan.scan_text(line, where, cfg, domains)
        ]
    return found


def closed_findings(records, patterns):
    """The closed scanner's literals (``private_scan.load``) over every record, excluded paths too: ["<where>:<line>:
    private literal", …] — the literal itself stays out."""
    return [
        f"{where}:{number}: private literal"
        for where, number, line in records
        if any(pattern.search(line) for _, pattern in patterns)
    ]


def git_text(repo, *args):
    out = subprocess.run(
        ["git", "-c", "core.quotePath=false", *args],
        cwd=str(repo),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout
    return out.decode("utf-8", "replace")


def span(repo, base, head="HEAD"):
    """The records of ``base..head``: its messages, then the paths and added lines — a merge by its first parent,
    as main shows it."""
    revs = f"{base}..{head}"
    messages = git_text(repo, "log", "-z", LOG_FORMAT, revs)
    diff = git_text(
        repo,
        "log",
        "-p",
        "--no-renames",
        "--format=",
        "--diff-merges=first-parent",
        revs,
    )
    return history(messages, diff)


def scan(records, cfg, patterns):
    return public_history_findings(records, cfg) + closed_findings(records, patterns)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="history_scan", description="What a span of history publishes, scanned"
    )
    parser.add_argument(
        "--config", default="privacy-scan.json", help="the public scanner's config"
    )
    parser.add_argument(
        "--list",
        required=True,
        dest="listing",
        help="the closed scanner's literal list",
    )
    parser.add_argument("repo")
    parser.add_argument("base")
    args = parser.parse_args(argv)
    if args.base.startswith("-"):
        print("history_scan: the base is a revision, not an option", file=sys.stderr)
        return 2
    try:
        cfg = privacy_scan.load_config(args.config)
        patterns = private_scan.load(args.listing)
        records = span(args.repo, args.base)
        count = int(git_text(args.repo, "rev-list", "--count", f"{args.base}..HEAD"))
    except subprocess.CalledProcessError as exc:
        print(
            f"history_scan: git {exc.cmd[3]}: {exc.stderr.decode('utf-8', 'replace').strip()[-300:]}",
            file=sys.stderr,
        )
        return 2
    except (OSError, ValueError) as exc:
        print(f"history_scan: {exc}", file=sys.stderr)
        return 2
    found = scan(records, cfg, patterns)
    for line in found:
        print(line)
    print(
        f"history_scan: {count} commit(s) after {args.base}: "
        + (
            f"{len(found)} finding(s) — such a line, merged, blocks the GitHub mirror for good"
            if found
            else "clean"
        )
    )
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
