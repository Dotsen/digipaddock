"""The session host's dev.check runners: a contour's check of a PR head.

Unlike a factory operation a check pauses no role, drains nothing and needs no authorization (it runs no grant):
the cabinet's lane per contour decides what runs. ``devcheck`` with ``start`` writes ``<id>.request.json`` (its
params and limit) and only then starts the root runner's unit ``digipaddock-operation-<id12>`` (``kd-operation-run dev.check
<id>``), so a restarted host never starts it twice; the runner runs the candidate's tests in its own child unit
``digipaddock-devcheck-<id12>`` (its dependencies in ``digipaddock-devcheck-<id12>-deps``) and writes ``<id>.result.json`` atomically.
The result is the check's final report, read at each tick and at each poll; a runner unit gone without one is
stopped with its children and ends ``host_lost``. ``devcheck_cancel`` marks the request and stops the tests' unit,
the dependencies' and then the runner; the runner's own result wins, else the check ends ``cancelled`` (``superseded``
for a newer head), with ``maintenance`` when a unit would not stop — the contour is not clean.
"""

import os
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from kd.session_host import core, protocol
from kd.session_host.operation import PYTHON, RUNNER, read_json, write_json

DIR = "devchecks"
# the cabinet's classes (kd.cabinet.devchecks.CLASSES): the host imports nothing of the cabinet
CLASSES = ("passed", "checks_failed", "environment_not_ready", "dependency_update_required", "external_unavailable",
           "superseded", "cancelled", "host_lost")
_REPORTED = ("profiles", "deployed", "log", "error_short", "maintenance")
KEEP_S = 7 * 86400  # an ended check's files stay this long: a poll after a lost answer still finds its result
CRASH_S = 300  # a runner gone without a result sooner than this after its start: the host is at fault, not the PR


@dataclass
class Launch:
    check: str
    argv: List[str]


@dataclass
class Stop:
    """Stop these units in order; then ``Checks.stopped`` with the ones still active."""
    check: str
    units: List[str]


def unit(check_id: str) -> str:
    return f"digipaddock-operation-{check_id[:12]}"


def child_unit(check_id: str) -> str:
    return f"digipaddock-devcheck-{check_id[:12]}"


def deps_unit(check_id: str) -> str:
    return f"digipaddock-devcheck-{check_id[:12]}-deps"


def argv(check_id: str, timeout: int) -> List[str]:
    """The runner's transient unit: root, its own cgroup outside the sessions' slice, collected when it ends. The
    cabinet's ``timeout`` already has the runner's grace over the tests' own limit."""
    if not protocol.HEX32.fullmatch(check_id or "") or not isinstance(timeout, int) or timeout <= 0:
        raise ValueError("bad check id or timeout")
    return ["systemd-run", "--unit", unit(check_id), "--collect", "--quiet", "-p", f"RuntimeMaxSec={timeout}",
            PYTHON, "-I", RUNNER, "dev.check", check_id]


def _final(check_id: str, record: dict) -> dict:
    report = {"type": "devcheck", "check": check_id, "state": "succeeded" if record["class"] == "passed" else "failed",
              "class": record["class"]}
    for key in _REPORTED:
        if key in record:
            report[key] = record[key]
    return report


class Checks:
    def __init__(self, state_dir: str):
        self.dir = os.path.join(state_dir, DIR)
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        self.stopping: set = set()  # checks whose units are being stopped now (one stop at a time)
        # checks the ticks watch for a result to report: the ones under way when the host started, and each it
        # started since; a result written while no host ran reaches the cabinet at its next poll
        self.watched: set = set(self.ids())

    def path(self, check_id: str, kind: str) -> str:
        return os.path.join(self.dir, f"{check_id}.{kind}.json")

    def stored(self, check_id: str) -> Optional[dict]:
        record = read_json(self.path(check_id, "request"))
        return record if isinstance(record, dict) and record.get("check") == check_id else None

    def result(self, check_id: str) -> Optional[dict]:
        record = read_json(self.path(check_id, "result"))
        if not isinstance(record, dict) or record.get("check") != check_id or record.get("class") not in CLASSES:
            return None
        return record

    def ids(self) -> List[str]:
        """The checks under way: a request and no result."""
        found = []
        for name in sorted(os.listdir(self.dir)):
            check_id, _, kind = name.partition(".")
            if kind == "request.json" and protocol.HEX32.fullmatch(check_id) and self.result(check_id) is None \
                    and self.stored(check_id) is not None:
                found.append(check_id)
        return found

    def status(self) -> list:
        return [{"check": i, "started": self.stored(i).get("started", 0), "cancel": self.stored(i).get("cancel", "")}
                for i in self.ids()]

    def _running(self, check_id: str) -> dict:
        return {"type": "devcheck", "check": check_id, "state": "running"}

    def _end(self, check_id: str, record: dict) -> list:
        """The host's own result (the runner left none); the runner's, written meanwhile, wins."""
        if self.result(check_id) is None:
            write_json(self.path(check_id, "result"), {"check": check_id, "profiles": [], "log": "",
                                                       "error_short": "", **record})
        self.watched.discard(check_id)
        return [core.Report(_final(check_id, self.result(check_id)))]

    def prune(self, now: float, keep_s: float = KEEP_S) -> int:
        """The files of checks that ended more than ``keep_s`` ago (the cabinet has their results)."""
        removed = 0
        for name in os.listdir(self.dir):
            check_id, _, kind = name.partition(".")
            if kind != "result.json" or not protocol.HEX32.fullmatch(check_id):
                continue
            try:
                if now - os.lstat(self.path(check_id, "result")).st_mtime < keep_s:
                    continue
                for kind in ("result", "request"):
                    if os.path.lexists(self.path(check_id, kind)):
                        os.unlink(self.path(check_id, kind))
                removed += 1
            except OSError:
                continue
        return removed

    # --- requests -------------------------------------------------------------------------------
    def request(self, req: dict, now: float) -> Tuple[dict, list]:
        """The cabinet's ``devcheck``: idempotent by id — a known one answers its state or its result."""
        check_id = req["check"]
        done = self.result(check_id)
        if done is not None:
            final = _final(check_id, done)
            return {"state": final["state"], "report": final}, []
        if self.stored(check_id) is not None:
            return {"state": "running", "report": self._running(check_id)}, []
        if not req["start"]:
            return {"state": "unknown"}, []
        record = {"check": check_id, "params": req["params"], "timeout": req["timeout"], "started": now}
        try:
            write_json(self.path(check_id, "request"), record)
        except OSError as exc:
            raise protocol.ProtocolError("unavailable", f"the check's state is not writable ({exc})")
        self.watched.add(check_id)
        report = self._running(check_id)
        return {"state": "running", "report": report}, [Launch(check_id, argv(check_id, req["timeout"])),
                                                        core.Report(report)]

    def launch_failed(self, check_id: str) -> list:
        """A host fault: every next check of the contour would fail the same way."""
        return self._end(check_id, {"class": "environment_not_ready", "error_short": "the runner did not start",
                                    "maintenance": "раннер проверки не запустился"})

    def _stop(self, check_id: str) -> list:
        if check_id in self.stopping:
            return []
        self.stopping.add(check_id)
        return [Stop(check_id, [child_unit(check_id), deps_unit(check_id), unit(check_id)])]

    def cancel(self, check_id: str, reason: str) -> Tuple[dict, list]:
        """The cabinet's ``devcheck_cancel``: the request marked, its units stopped (``stopped`` ends it)."""
        done = self.result(check_id)
        if done is not None:
            final = _final(check_id, done)
            return {"state": final["state"], "report": final}, []
        record = self.stored(check_id)
        if record is None:
            return {"state": "unknown"}, []
        if not record.get("cancel"):
            write_json(self.path(check_id, "request"), {**record, "cancel": reason})
        return {"state": "stopping"}, self._stop(check_id)

    def stopped(self, check_id: str, still: List[str], now: Optional[float] = None) -> list:
        """Its units were stopped; ``still`` the ones that did not stop. The runner's result wins; else it ended as
        the cancel asked (or ``host_lost``: the runner went on its own), and the contour needs maintenance when a unit
        would not stop or the runner went without a result soon after its start (it crashes, the next one would)."""
        self.stopping.discard(check_id)
        record = self.stored(check_id) or {}
        why = record.get("cancel") or ""
        cls = "superseded" if why == "superseded" else "cancelled" if why else "host_lost"
        end = {"class": cls}
        lived = (time.time() if now is None else now) - float(record.get("started") or 0)
        if still:
            end["maintenance"] = f"units did not stop: {', '.join(still)}"
        elif not why and lived < CRASH_S:
            end["maintenance"] = f"раннер проверки закончился без результата через {max(0, round(lived))} с после старта"
        return self._end(check_id, end)

    # --- the tick -------------------------------------------------------------------------------
    def tick(self, active: Callable[[str], bool]) -> list:
        """``active(unit)`` is the runner unit's state, read before this call: a result is the final report; a runner
        gone without one is stopped with its children and ends."""
        actions: list = []
        for check_id in sorted(self.watched):
            if check_id in self.stopping:
                continue
            running = active(unit(check_id))
            done = self.result(check_id)
            if done is not None:
                self.watched.discard(check_id)
                actions.append(core.Report(_final(check_id, done)))
            elif self.stored(check_id) is None:
                self.watched.discard(check_id)  # its files were removed under the host
            elif not running:
                actions += self._stop(check_id)
        return actions
