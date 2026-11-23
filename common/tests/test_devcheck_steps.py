"""The steps of a dev.check profile, run in the sandboxed child unit — the order, the first failure
stops the rest, the overlay's tests find the code checkout."""

import importlib.util
import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "kd" / "devcheck_steps.py"
spec = importlib.util.spec_from_file_location("devcheck_steps", PATH)
steps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(steps)


class Runner:
    def __init__(self, codes=None, out="3 passed in 1.0s"):
        self.calls, self.codes, self.out = [], dict(codes or {}), out

    def __call__(self, argv, *, cwd, env):
        self.calls.append({"argv": list(argv), "cwd": cwd, "env": dict(env)})
        return self.codes.get(len(self.calls), 0), self.out


def test_the_script_imports_nothing_of_kd():
    text = PATH.read_text(encoding="utf-8")
    assert "from kd" not in text and "import kd" not in text


@pytest.mark.parametrize("argv", [["nope", "unit", "/w/src"], ["digipaddock", "e2e", "/w/src"], ["digipaddock"],
                                  ["digipaddock-deploy", "unit", "/w/src"], ["digipaddock", "unit", "/w/src", "bad"]])
def test_unknown_project_or_profile_is_refused(argv, capsys):
    assert steps.main(argv, runner=Runner()) == 2


def test_the_unit_runs_the_six_steps_in_order_and_stops_at_the_first_failure(capsys):
    runner = Runner()
    assert steps.main(["digipaddock", "unit", "/w/src", "digipaddock-deploy=/w/sets/digipaddock-deploy"], runner=runner) == 0
    names = [line[3:] for line in capsys.readouterr().out.splitlines() if line.startswith("== ")]
    assert names[:6] == ["privacy", "history", "pytest", "web check", "web test", "vulns"]
    privacy, history, pytest_, check, test, vulns = runner.calls
    # last: a red one leaves the tests' result in the output; the deployed scanner, against the runner's base
    assert vulns["argv"][1:] == ["-I", str(PATH.parent / "vuln_scan.py"), "check", ".", "refs/kd/base", "--venv"]
    assert vulns["argv"][0] == sys.executable and vulns["cwd"] == "/w/src"
    assert privacy["argv"][1:] == ["-I", str(PATH.parent / "privacy_scan.py"), "--config", "privacy-scan.json",
                                   "--tracked", "."]
    # what the PR adds to the target the runner put at refs/kd/base, the running release's closed list
    assert history["argv"][1:] == ["-I", str(PATH.parent / "history_scan.py"), "--config", "privacy-scan.json",
                                   "--list", str(PATH.parents[2] / "host" / "private_scan.json"), ".", "refs/kd/base"]
    assert history["cwd"] == "/w/src"
    # a test that hangs dumps its threads' stacks into the log before the unit's limit
    assert pytest_["argv"][1:] == ["-m", "pytest", "-q", "-m", "not host", "-n", "4", "-o", "faulthandler_timeout=300"]
    assert pytest_["env"]["KD_HOST_JSON"] == "host/host.example.json" and pytest_["cwd"] == "/w/src"
    assert check["argv"] == ["npm", "run", "check"] and check["cwd"].replace("\\", "/") == "/w/src/common/cabinet/web"
    assert test["argv"] == ["npm", "test"]
    failing = Runner(codes={2: 1})
    assert steps.main(["digipaddock", "unit", "/w/src"], runner=failing) == 1
    assert len(failing.calls) == 2


def test_overlay_unit_sets_digipaddock(capsys):
    runner = Runner()
    assert steps.main(["digipaddock-deploy", "unit", "/w/src", "digipaddock=/w/sets/digipaddock"], runner=runner) == 0
    (call,) = runner.calls
    assert call["argv"][1:] == ["-m", "pytest", "-q", "tests"]
    assert call["env"]["DIGIPADDOCK"] == "/w/sets/digipaddock" and "KD_HOST_JSON" not in call["env"]


def test_counts_are_summed_and_printed_last(capsys):
    runner = Runner(out=" Test Files  5 passed (5)\n 1 skipped\n      Tests  7 passed (7)\n")  # the last line wins
    steps.main(["digipaddock", "unit", "/w/src"], runner=runner)
    last = capsys.readouterr().out.splitlines()[-1]
    assert last.startswith(steps.COUNTS)
    counts = json.loads(last[len(steps.COUNTS):])
    assert counts["passed"] == 6 * 7 and counts["skipped"] == 6


@pytest.mark.skipif(sys.platform == "win32", reason="the unit's limit is a SIGTERM")
def test_a_step_stopped_by_the_units_limit_keeps_what_it_printed():
    # the unit's RuntimeMaxSec ends in a SIGTERM; the log keeps how far the step got, not an empty section
    child = ("import os, signal, time; print('collected 5 items', flush=True); "
             "os.kill(os.getppid(), signal.SIGTERM); time.sleep(30)")
    old = signal.getsignal(signal.SIGTERM)
    steps.stop_on_term()
    try:
        start = time.monotonic()
        rc, out = steps._run([sys.executable, "-c", child], cwd=".", env=dict(os.environ))
    finally:
        signal.signal(signal.SIGTERM, old)
    assert time.monotonic() - start < 20
    assert rc != 0 and out.splitlines() == ["collected 5 items", steps.STOPPED]


def test_counts_of_a_summary():
    assert steps.counts_of("=== 2 failed, 10 passed, 3 skipped, 1 error in 9.1s ===") == {
        "failed": 2, "passed": 10, "skipped": 3, "error": 1}
    assert steps.counts_of("Tests  557 passed (557)") == {"passed": 557}
