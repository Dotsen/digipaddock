"""transcribe_setup.sh lock: ``pip freeze`` output and the downloaded wheels → a hash lock on stdout.

    python -I transcribe_lock.py <frozen.txt> <wheels dir>

Every frozen ``name==version`` gets the sha256 of its wheel (``--hash=sha256:…``); a requirement without its wheel
fails: the lock is all or nothing. Stdlib only — it runs in the scratch venv.
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def lock(frozen: str, wheels: Path) -> str:
    by_name: dict[tuple[str, str], list[Path]] = {}
    for wheel in sorted(wheels.glob("*.whl")):
        name, version = wheel.name.split("-")[:2]
        by_name.setdefault((canonical(name), version.lower()), []).append(wheel)
    lines = []
    for line in frozen.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, version = line.partition("==")
        if not sep:
            raise ValueError(f"not a pinned requirement: {line}")
        found = by_name.get((canonical(name), version.lower()))
        if not found:
            raise ValueError(f"no wheel for {line}")
        hashes = sorted(hashlib.sha256(wheel.read_bytes()).hexdigest() for wheel in found)
        lines.append(f"{canonical(name)}=={version} \\\n" + " \\\n".join(f"    --hash=sha256:{h}" for h in hashes))
    if not lines:
        raise ValueError("nothing frozen")
    return "\n".join(sorted(lines)) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: transcribe_lock.py <frozen.txt> <wheels dir>", file=sys.stderr)
        return 2
    try:
        sys.stdout.write(lock(Path(argv[0]).read_text("utf-8"), Path(argv[1])))
    except (OSError, ValueError) as exc:
        print(f"transcribe_lock: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
