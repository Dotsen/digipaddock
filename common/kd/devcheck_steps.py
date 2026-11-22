"""The steps of a dev.check profile, run in the sandboxed child unit.

``<venv>/bin/python -I devcheck_steps.py <project> <profile> <checkout> [<component>=<checkout>...]``: the deployed
script (never the candidate's) runs each step of the profile in order in the candidate's checkout, prints
``== <step>`` and the tail of its output, and stops at the first step that fails (exit 1). The last line is the
counts of the summaries it saw (``passed``, ``failed`` …), informational only. Stdlib only and free of kd imports:
``-I`` puts nothing of the tree on the path.
"""

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from collections import namedtuple

HERE = os.path.dirname(os.path.abspath(__file__))
BASE_REF = "refs/kd/base"  # the runner's: the target's commit the check is about
# the closed scanner's literal list of the running release (the deployed tree's overlay data), never the candidate's
PRIVATE_LIST = os.path.join(os.path.dirname(os.path.dirname(HERE)), "host", "private_scan.json")
COUNTS = "== counts "
STOPPED = "== stopped: the unit's limit is reached"
TAIL_LINES = 60
_COUNT = re.compile(r"(\d+) (passed|failed|skipped|errors?|xfailed|xpassed)\b")

Step = namedtuple("Step", "name argv cwd env")


def privacy(checkout, sets):
    """The deployed public scanner with the candidate's own config, over the files git tracks."""
    return Step("privacy", [sys.executable, "-I", os.path.join(HERE, "privacy_scan.py"), "--config",
                            "privacy-scan.json", "--tracked", "."], checkout, {})


def history(checkout, sets):
    """what the PR adds to its target's history — every commit message, author, path and added line, by the
    public scanner (the candidate's config) and the closed one (the release's list): a line added and removed again
    inside the PR passes the tree's scan but, merged, stays in the forge's history for good."""
    return Step("history", [sys.executable, "-I", os.path.join(HERE, "history_scan.py"), "--config",
                            "privacy-scan.json", "--list", PRIVATE_LIST, ".", BASE_REF], checkout, {})


def pytest_unit(checkout, sets):
    # a test running past 300 s dumps every thread's stack into the step's output
    return Step("pytest", [sys.executable, "-m", "pytest", "-q", "-m", "not host", "-n", "4", "-o",
                           "faulthandler_timeout=300"], checkout, {"KD_HOST_JSON": "host/host.example.json"})


def _web(checkout):
    return os.path.join(checkout, "common", "cabinet", "web")


def web_check(checkout, sets):
    return Step("web check", ["npm", "run", "check"], _web(checkout), {})


def web_test(checkout, sets):
    return Step("web test", ["npm", "test"], _web(checkout), {})


def overlay_pytest(checkout, sets):
    """The overlay's tests over the code checkout of the set (they find it by ``DIGIPADDOCK``)."""
    return Step("overlay pytest", [sys.executable, "-m", "pytest", "-q", "tests"], checkout,
                {"DIGIPADDOCK": sets["digipaddock"], "KD_HOST_JSON": None})  # the overlay's own host.json, not an example


# project -> profile -> steps; a step needs the set's components it names
STEPS = {"digipaddock": {"unit": (privacy, history, pytest_unit, web_check, web_test)},
         "digipaddock-deploy": {"unit": (overlay_pytest,)}}
NEEDS = {"digipaddock-deploy": ("digipaddock",)}
# the prepared dependencies a project's steps run with, built from the locks of its code checkout (NEEDS[0] or its own)
DEPS = {"digipaddock": ("venv", "node"), "digipaddock-deploy": ("venv",)}


def counts_of(text):
    """The counts of pytest's and vitest's summary lines; the last of each kind wins within one step (vitest's
    ``Tests`` line comes after its ``Test Files``)."""
    found = {}
    for number, kind in _COUNT.findall(text):
        found[kind.rstrip("s") if kind.startswith("error") else kind] = int(number)
    return found


class _Stopped(Exception):
    pass


def _stop(signum, frame):
    raise _Stopped()


def stop_on_term():
    """the unit's limit (RuntimeMaxSec) is a SIGTERM; the running step ends with what it printed so far."""
    signal.signal(signal.SIGTERM, _stop)


def _run(argv, *, cwd, env):
    """The step's output goes to a file, not a pipe: a step the unit's limit stops still has what it printed."""
    with tempfile.TemporaryFile() as sink:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=sink,
                                stderr=subprocess.STDOUT)
        try:
            code = proc.wait()
            stopped = ""
        except _Stopped:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)  # once: the tail is printed before the unit's SIGKILL
            proc.kill()
            code = proc.wait()
            stopped = "\n" + STOPPED
        sink.seek(0)
        return code, sink.read().decode("utf-8", "replace").rstrip("\n") + stopped


def main(argv=None, runner=_run):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3 or args[0] not in STEPS or args[1] not in STEPS[args[0]]:
        print("usage: devcheck_steps.py <project> <profile> <checkout> [<component>=<checkout>...]", file=sys.stderr)
        return 2
    project, profile, checkout = args[:3]
    sets = {}
    for item in args[3:]:
        name, sep, path = item.partition("=")
        if not sep or not name or not path:
            print(f"devcheck_steps: not a component checkout: {item}", file=sys.stderr)
            return 2
        sets[name] = path
    missing = [n for n in NEEDS.get(project, ()) if n not in sets]
    if missing:
        print(f"devcheck_steps: {project} needs the checkout of {', '.join(missing)}", file=sys.stderr)
        return 2
    total, code = {}, 0
    for make in STEPS[project][profile]:
        step = make(checkout, sets)
        print(f"== {step.name}", flush=True)
        env = {k: v for k, v in {**os.environ, **step.env}.items() if v is not None}  # None: not in the step's env
        rc, out = runner(step.argv, cwd=step.cwd, env=env)
        print("\n".join(out.splitlines()[-TAIL_LINES:]), flush=True)
        for kind, number in counts_of(out).items():
            total[kind] = total.get(kind, 0) + number
        if rc != 0:
            print(f"== {step.name} failed (exit {rc})", flush=True)
            code = 1
            break
    print(COUNTS + json.dumps(total, sort_keys=True), flush=True)
    return code


if __name__ == "__main__":
    stop_on_term()
    sys.exit(main())
