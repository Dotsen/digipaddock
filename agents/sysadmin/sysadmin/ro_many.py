"""ssh-ro on several servers at once: in parallel, the output in order, each under a header "=== server"."""
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import List, Tuple

WORKERS = 6
TIMEOUT = 300  # the gateway itself cuts a command off after 60 s; this is a margin for slow SSH


def _one(target, run, timeout) -> Tuple[int, bytes]:
    _, argv = target
    try:
        done = run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        return done.returncode, done.stdout or b""
    except subprocess.TimeoutExpired as exc:
        return 124, (exc.output or b"") + f"\n[ssh-ro: таймаут {timeout} с]".encode()


def run_many(targets: List[Tuple[str, List[str]]], out, run=subprocess.run, timeout=TIMEOUT) -> int:
    """0 — every server answered with code 0, otherwise 1 (each one's code is a line "(code N)" under its output)."""
    with ThreadPoolExecutor(max_workers=min(WORKERS, len(targets))) as pool:
        results = list(pool.map(lambda t: _one(t, run, timeout), targets))
    failed = False
    for (alias, _), (code, data) in zip(targets, results):
        out.write(f"=== {alias}\n".encode())
        out.write(data)
        if data and not data.endswith(b"\n"):
            out.write(b"\n")
        if code != 0:
            out.write(f"(код {code})\n".encode())
            failed = True
    out.flush()
    return 1 if failed else 0
