"""The session host's factory operations: ``agent.reinstall`` of generated roles.

The cabinet asks for one operation at a time. The host pauses its roles (``core.Host.pause``: nothing of them
starts, their queues are kept) and installs them in batches as they drain: the roles drained by now —
every session process and running job of the role ended on its own, nothing is killed — go to the root runner's unit
(``digipaddock-operation-<id12>-<n>``, outside ``digipaddock-sessions.slice``: stopping the sessions never stops it) as batch ``n``;
a role still at work stays paused until its own turn ends, and never holds up the others. Right before a batch
starts the cabinet authorizes it (``operation.authorize``: the grant still covers it, the tree is still the
revision): a refusal ends the operation, an unreachable cabinet starts nothing. The runner reads
``<id>.request.json`` and writes ``<id>.batch<n>.json`` atomically; the host adopts it, lifts the pause of the
batch's roles that installed (or were unchanged) and goes on with the rest. The operation's end — every role done,
a batch that failed, a drain past the request's timeout, a cancel — is the host's ``<id>.result.json``: each role's
state and the roles it keeps paused (``kept``).

A ``component.install`` has no roles of its own: it is one batch with no roles, started at once — or,
for ``drain: all`` (the session host's own restart), once every role hosted when it arrived is paused and drained.
Its result carries the runner's ``component`` (exit codes and masked tails), and its end lifts every pause it took:
nothing of a role was installed. The host restarted by such a batch reads the batch's result as after any restart.

A role whose install failed stays paused with its queue, for a new reinstall after the fix; so does every role still
paused when the runner left no result (its effect is unknown) or the host's own check failed after an install. A
drain past the timeout skips the roles still at work and lifts their pauses: nothing was installed for them.

Every state is a root-only file in the host's state directory — the pauses (``paused.json``) and each operation's
request, batch results and result — so a host restarted mid-way keeps its pauses, never starts a runner twice (the
request file names the batch before its unit is started) and adopts a result the runner wrote meanwhile.
"""

import json
import os
from typing import Callable, Dict, List, Optional, Tuple

from kd import atomic_file
from kd.session_host import core, protocol

STATE = "/var/lib/digipaddock-session"  # the host's root-only state directory
OPERATIONS = "operations"
PAUSED = "paused.json"
RUNNER = "/opt/digipaddock/common/bin/kd-operation-run"
PYTHON = "/usr/bin/python3"
DRAINING, INSTALLING = "draining", "installing"
DRAIN_TIMEOUT, LAUNCH_FAILED, RUNNER_LOST = "drain_timeout", "launch_failed", "runner_lost"
HOST_CHECK_FAILED = "host_check_failed"
HOLD_ALL = (RUNNER_LOST, HOST_CHECK_FAILED)  # the host's state is unknown or unsafe: no paused role is lifted
RESULT_STATES = ("succeeded", "failed")
FINE = ("ok", "unchanged")
RUNNER_MAX_S = 3600  # the runner unit's limit: a hung install ends as a lost runner, its roles kept paused
TAIL_MAX = 1500  # characters of a failed stage's masked output a role's result keeps (the runner's own limit)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def unit(operation_id: str, batch: Optional[int] = None) -> str:
    return f"digipaddock-operation-{operation_id[:12]}" + (f"-{batch}" if batch else "")


def argv(operation_id: str, batch: int) -> List[str]:
    """The runner's transient unit for one batch: root, its own cgroup, collected when it ends."""
    if not protocol.HEX32.fullmatch(operation_id or "") or not isinstance(batch, int) or batch < 1:
        raise ValueError("bad operation id or batch")
    return ["systemd-run", "--unit", unit(operation_id, batch), "--collect", "--quiet",
            "-p", f"RuntimeMaxSec={RUNNER_MAX_S}", PYTHON, "-I", RUNNER, operation_id, str(batch)]


def write_json(path: str, value) -> None:
    """Atomically, root-only; the rename replaces a link at the name, never follows it."""
    atomic_file.replace(path, json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"), 0o600)


def read_json(path: str):
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except OSError:
        return None
    try:
        with os.fdopen(fd, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _final(record: dict) -> dict:
    agents = record.get("agents") or {}
    kept = record.get("kept")
    if not isinstance(kept, list):  # a result written before ``kept``: the roles it holds, as the host reads them
        kept = sorted(Operations._kept({"reason": "", "agents": agents, **record}, list(agents)))
    final = {"type": "operation", "operation": record["operation"], "state": record["state"],
             "phase": record.get("phase") or INSTALLING, "agents": agents,
             "reason": record.get("reason") or "", "kept": kept}
    if isinstance(record.get("component"), dict):
        final["component"] = record["component"]
    return final


def _role(state: str, error: str = "", stage: str = "") -> dict:
    return {"state": state, "stage": stage, "error": error}


def _adopted(item: dict) -> dict:
    """A role's state from a batch result; a failure keeps the runner's masked tail (the host adds the batch),
    kept on disk with the request and the result, so a restarted host reports them too."""
    out = _role(item.get("state"), str(item.get("error") or ""), str(item.get("stage") or ""))
    if isinstance(item.get("tail"), str) and item["tail"]:
        out["tail"] = item["tail"][-TAIL_MAX:]
    return out


class Operations:
    def __init__(self, state_dir: str, host: core.Host):
        self.dir = os.path.join(state_dir, OPERATIONS)
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        self.paused_path = os.path.join(state_dir, PAUSED)
        self.host = host
        host.urgent_in_pause = self.drains  # an urgent wake goes through a drain, never an install
        for agent, operation_id in self._load_paused().items():
            host.pause(agent, operation_id)  # a host that just started has no process to close
        self.active: Optional[dict] = self._load_active()
        self._adopt_finished()

    # --- files ----------------------------------------------------------------------------------
    def path(self, operation_id: str, kind: str) -> str:
        return os.path.join(self.dir, f"{operation_id}.{kind}.json")

    def _load_paused(self) -> Dict[str, str]:
        value = read_json(self.paused_path)
        if not isinstance(value, dict):
            return {}
        return {a: o for a, o in value.items() if isinstance(a, str) and protocol.AGENT.fullmatch(a)
                and isinstance(o, str) and protocol.HEX32.fullmatch(o)}

    def save_paused(self) -> None:
        write_json(self.paused_path, dict(self.host.paused))

    def _save(self) -> None:
        write_json(self.path(self.active["operation"], "request"), self.active)

    def _load_active(self) -> Optional[dict]:
        """The request without a result: at most one (the host takes one at a time)."""
        found = []
        for name in os.listdir(self.dir):
            operation_id, _, kind = name.partition(".")
            if kind != "request.json" or not protocol.HEX32.fullmatch(operation_id):
                continue
            if os.path.exists(self.path(operation_id, "result")):
                continue
            record = read_json(self.path(operation_id, "request"))
            if (isinstance(record, dict) and record.get("operation") == operation_id
                    and isinstance(record.get("batches"), list)):
                found.append(record)
        if not found:
            return None
        record = min(found, key=lambda r: r.get("started", 0))
        record["done"] = {a: v for a, v in (record.get("done") or {}).items() if a in record["agents"]}
        return record

    def _adopt_finished(self) -> None:
        """A result written while no host ran (or before the pauses were saved): lift the pauses it allows, as
        ``finish`` would have."""
        by: Dict[str, List[str]] = {}
        for agent, operation_id in self.host.paused.items():
            if self.active is None or self.active["operation"] != operation_id:
                by.setdefault(operation_id, []).append(agent)
        changed = False
        for operation_id, agents in by.items():
            record = self.result(operation_id)
            if record is None:
                continue  # no result and no request under way: kept for root's ``resume``
            record = {"reason": "", "agents": {}, **record}
            for agent in agents:
                if agent not in self._kept(record, agents):
                    self.host.resume(agent)  # a host that just started has nothing waiting to fill
                    changed = True
        if changed:
            self.save_paused()

    def result(self, operation_id: str) -> Optional[dict]:
        record = read_json(self.path(operation_id, "result"))
        if (not isinstance(record, dict) or record.get("operation") != operation_id
                or record.get("state") not in RESULT_STATES or not isinstance(record.get("agents", {}), dict)):
            return None
        return record

    def batch_result(self, n: int) -> Optional[dict]:
        operation_id = self.active["operation"]
        record = read_json(self.path(operation_id, f"batch{n}"))
        if (not isinstance(record, dict) or record.get("operation") != operation_id or record.get("batch") != n
                or record.get("state") not in RESULT_STATES or not isinstance(record.get("agents", {}), dict)):
            return None
        return record

    # --- requests -------------------------------------------------------------------------------
    def component(self) -> bool:
        """The operation under way runs a platform component, not a role's install."""
        return self.active is not None and self.active.get("kind") == protocol.COMPONENT

    def drains(self, agent: str) -> bool:
        """Is ``agent`` paused only to drain: the operation under way holds it and has not started its install (a
        batch of its own runs). A role kept paused after an operation (its install failed, its effect unknown) is
        not."""
        active = self.active
        if active is None or self.host.paused.get(agent) != active["operation"] or agent not in self.remaining():
            return False
        if self.component():  # every role waits out the component's one run (the host's restart)
            return active.get("batch") is None
        batch = active.get("batch")
        return not (batch and agent in batch.get("agents", ()))

    def remaining(self) -> List[str]:
        done = self.active.get("done") or {}
        return [a for a in self.active["agents"] if a not in done]

    def _running(self) -> dict:
        report = {"type": "operation", "operation": self.active["operation"], "state": "running",
                  "phase": self.active["phase"], "agents": dict(self.active["done"]),
                  "until": int(float(self.active.get("started", 0)) + int(self.active.get("timeout", 0)))}
        return report

    def request(self, req: dict, hosted: Callable[[str], bool], now: float,
                hosted_all: Callable[[], List[str]] = list) -> Tuple[dict, list]:
        """The cabinet's ``operation``: idempotent by id — a known one answers its state or its result. A component
        names no roles: ``drain: all`` pauses every role hosted now (``hosted_all``)."""
        operation_id = req["operation"]
        done = self.result(operation_id)
        if done is not None:
            return {"state": done["state"], "report": _final(done)}, []
        if self.active is not None and self.active["operation"] == operation_id:
            return {"state": "running", "report": self._running()}, []
        if not req["start"]:
            return {"state": "unknown"}, []
        if self.active is not None:
            return {"state": "busy", "operation": self.active["operation"]}, []
        missing = [a for a in req["agents"] if not hosted(a)]
        if missing:
            raise protocol.ProtocolError("refused", f"not on this session host: {', '.join(missing)}")
        agents = list(req["agents"])
        if req["kind"] == protocol.COMPONENT:
            # a role another operation keeps paused (its install failed) has no process to drain and stays held
            # by that operation: the component's end must never lift it
            agents = sorted(a for a in hosted_all() if a not in self.host.paused) if req["drain"] == "all" else []
        record = {"operation": operation_id, "kind": req["kind"], "agents": agents,
                  "revision": req["revision"], "timeout": req["timeout"], "started": now, "phase": DRAINING,
                  "done": {}, "batch": None, "batches": []}
        if req["kind"] == protocol.COMPONENT:
            record.update(component=req["component"], mode=req["mode"], drain=req["drain"])
        try:
            write_json(self.path(operation_id, "request"), record)
        except OSError as exc:
            raise protocol.ProtocolError("unavailable", f"the operation's state is not writable ({exc})")
        self.active = record
        actions: list = []
        for agent in record["agents"]:
            actions += self.host.pause(agent, operation_id)
        self.save_paused()
        report = self._running()
        return {"state": "running", "report": report}, actions + [core.Report(report)]

    def cancel(self, operation_id: str, reason: str) -> Tuple[dict, list]:
        """The cabinet's ``operation_cancel`` (its grant was revoked): while no batch runs nothing more is
        installed — the operation fails with ``reason`` and the remaining pauses are lifted; a batch under way is
        never cut (``running``; the next one is refused at its authorization); a finished one answers its result, an
        unknown one ``unknown``."""
        done = self.result(operation_id)
        if done is not None:
            return {"state": done["state"], "report": _final(done)}, []
        if self.active is None or self.active["operation"] != operation_id:
            return {"state": "unknown"}, []
        if self.active["phase"] != DRAINING:
            return {"state": "running", "report": self._running()}, []
        actions = self.finish({"state": "failed", "reason": reason, "phase": DRAINING})
        return {"state": "failed", "report": _final(self.result(operation_id))}, actions

    def unpause(self, agent: str) -> Tuple[bool, list]:
        """``resume``: lift a role's pause, never one the operation under way holds."""
        if self.active is not None and agent in self.active["agents"] and self.host.paused.get(agent) == \
                self.active["operation"]:
            raise protocol.ProtocolError("refused", f"operation {self.active['operation']} holds {agent}")
        operation_id = self.host.paused.get(agent)
        actions = self.host.resume(agent)
        if operation_id is not None:
            self.save_paused()
            # the cabinet shows the role held after its failed operation until it hears this
            actions.append(core.Report({"type": "operation_release", "operation": operation_id, "agent": agent}))
        return operation_id is not None, actions

    # --- the tick -------------------------------------------------------------------------------
    def step(self, now: float, drained: Callable[[str], bool]) -> str:
        """What the active operation needs: idle, wait, launch, timeout, installing or complete."""
        if self.active is None:
            return "idle"
        if self.active["phase"] != DRAINING:
            return "installing"
        remaining = self.remaining()
        if self.component():  # one run, once every paused role is drained (none to drain: at once)
            if all(drained(a) for a in remaining):
                return "launch"
        elif not remaining:
            return "complete"  # every role done, the end not written yet (a host stopped right then)
        if now - float(self.active.get("started", 0)) >= int(self.active.get("timeout", 0)):
            return "timeout"
        if not self.component() and any(drained(a) for a in remaining):
            return "launch"
        return "wait"

    def ready(self, drained: Callable[[str], bool]) -> List[str]:
        """The roles of the next batch; a component's batch has none (``step`` says when it may start)."""
        if self.component():
            return []
        return [a for a in self.remaining() if drained(a)]

    def launch(self, agents: List[str]) -> Tuple[int, list]:
        """Batch ``n`` of these drained roles is about to start: said on disk first, so a restarted host never
        starts it again."""
        n = len(self.active["batches"]) + 1
        self.active.update(batch={"n": n, "agents": list(agents)}, phase=INSTALLING)
        self.active["batches"].append(list(agents))
        self._save()
        return n, [core.Report(self._running())]

    def batch(self) -> Optional[dict]:
        return self.active.get("batch")

    def adopt(self, outcome: dict) -> list:
        """A batch's result: its roles' states; the fine ones work again. A failed batch ends the operation. A
        component's one batch is its end."""
        batch, named = self.active["batch"], outcome.get("agents") or {}
        reason = outcome.get("reason") or ""
        if self.component():
            ran = outcome.get("component")
            return self.finish({"state": outcome["state"], "phase": INSTALLING,
                                "reason": reason or ("" if outcome["state"] == "succeeded" else "install_failed"),
                                **({"component": ran} if isinstance(ran, dict) else {})})
        for agent in batch["agents"]:
            item = named.get(agent)
            self.active["done"][agent] = {
                **(_adopted(item) if isinstance(item, dict) and item.get("state") in ("ok", "unchanged", "failed",
                                                                                       "skipped")
                   else _role("failed", reason or "no result for the role")),
                "batch": batch["n"]}  # every role of the batch: a failed host check names its unit too
        if outcome["state"] != "succeeded":
            return self.finish({"state": "failed", "reason": reason or "install_failed", "phase": INSTALLING})
        operation_id, actions = self.active["operation"], []
        for agent in batch["agents"]:
            if self.active["done"][agent]["state"] in FINE and self.host.paused.get(agent) == operation_id:
                actions += self.host.resume(agent)
        self.active.update(batch=None, phase=DRAINING)
        self._save()
        self.save_paused()
        if not self.remaining():
            return actions + self.complete()
        return actions + [core.Report(self._running())]

    def complete(self) -> list:
        fine = all(v["state"] in FINE for v in self.active["done"].values())
        return self.finish({"state": "succeeded" if fine else "failed", "reason": "" if fine else "install_failed",
                            "phase": INSTALLING})

    def finish(self, outcome: dict) -> list:
        """The operation's end: every role's state, its result on disk, the pauses lifted but of the roles it must
        keep (named in the result), the final report."""
        operation_id, agents = self.active["operation"], list(self.active["agents"])
        reason = outcome.get("reason") or ""
        done = dict(self.active.get("done") or {})
        done.update(outcome.get("agents") or {})
        component = self.component()
        if not component:
            for agent in agents:
                done.setdefault(agent, _role("skipped", reason))
        record = {"operation": operation_id, "state": outcome["state"], "reason": reason, "agents": done,
                  "phase": outcome.get("phase") or INSTALLING}
        if component:  # nothing of a role was installed: every pause it took is lifted
            record["kept"] = []
            if isinstance(outcome.get("component"), dict):
                record["component"] = outcome["component"]
        keep = self._kept(record, agents)
        record["kept"] = sorted(a for a in keep if self.host.paused.get(a) == operation_id)
        if self.result(operation_id) is None:
            write_json(self.path(operation_id, "result"), record)
        actions: list = []
        for agent in agents:
            if agent not in keep and self.host.paused.get(agent) == operation_id:
                actions += self.host.resume(agent)
        self.save_paused()
        self.active = None
        return actions + [core.Report(_final(record))]

    @staticmethod
    def _kept(record: dict, agents: list) -> set:
        """The roles a result keeps paused: the ones it names (a result of this host), else a failed install's role,
        a role the result does not name, and every role when the effect is unknown or the host's check failed.
        Nothing installed — nothing kept."""
        if isinstance(record.get("kept"), list):
            return {a for a in record["kept"] if a in agents}
        if record["state"] != "failed":
            return set()
        if record["reason"] in HOLD_ALL:
            return set(agents)
        named = record["agents"]
        if not named:
            return set()
        return {a for a in agents if a not in named or named[a].get("state") == "failed"}
