#!/usr/bin/env python3
"""The closed privacy scanner: a deployment's literal list over a code tree or files.

The scanner is code of the installed release tree; the overlay carries only its data, the list. kd-release
and kd-mirror run this file of the tree that is running, never a file of an incoming overlay.

    python3 -I common/kd/private_scan.py --list host/private_scan.json <tree-or-file> [...]

host/private_scan.json is ``{"literals": [...]}`` — addresses, aliases, domains, accounts and names of this
deployment. A literal counts case-insensitively where no letter, digit or ``_`` touches it (``203.0.113.10`` is not
found inside ``1203.0.113.105``; ``example.test`` is found in ``www.example.test``). Text files only (a NUL in the
first 8 KiB — binary, skipped), ``.git`` skipped. Prints ``<path>:<line>: <literal>``; exit 1 with findings,
0 clean, 2 on a broken list or a missing path. Stdlib only, Python 3.6+, no kd imports: kd-release runs it in its
sandbox (no network, no secrets, read-only input), kd-mirror from the release tree.
"""

import argparse
import json
import os
import re
import sys

BINARY_PROBE = 8192


def load(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    literals = data.get("literals") if isinstance(data, dict) else None
    if (
        not isinstance(literals, list)
        or not literals
        or not all(isinstance(x, str) and x.strip() for x in literals)
    ):
        raise ValueError(
            '{}: expected {{"literals": [non-empty strings]}}'.format(path)
        )
    return [
        (
            x,
            re.compile(
                r"(?<![A-Za-z0-9_])" + re.escape(x) + r"(?![A-Za-z0-9_])", re.IGNORECASE
            ),
        )
        for x in literals
    ]


def files(root):
    if os.path.isfile(root):
        yield root
        return
    if not os.path.isdir(root):
        raise FileNotFoundError(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            if not os.path.islink(path):
                yield path


def scan(path, patterns):
    with open(path, "rb") as f:
        data = f.read()
    if b"\0" in data[:BINARY_PROBE]:
        return []
    found = []
    for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
        for literal, pattern in patterns:
            if pattern.search(line):
                found.append("{}:{}: {}".format(path, number, literal))
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(prog="private_scan")
    parser.add_argument("--list", required=True, dest="listing")
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        patterns = load(args.listing)
        found = []
        for root in args.paths:
            for path in files(root):
                found += scan(path, patterns)
    except (OSError, ValueError) as exc:
        print("private_scan: {}".format(exc), file=sys.stderr)
        return 2
    for line in found:
        print(line)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
