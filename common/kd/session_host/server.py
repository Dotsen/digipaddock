"""kd-session-host's I/O: the socket for the cabinet, the session processes, the reports, the journals.

Root runs it (``digipaddock-session-host.service``). Its socket takes requests from the cabinet's service user only (the
peer's uid, ``SO_PEERCRED``); root may ask ``status``. Each session process is ``systemd-run --pipe`` of the
agent's checked launch under the agent's user (``claude.argv``): the host writes its input, reads its stream-json
output line by line (a raw copy per session in a root-only file), and reports what happened to the cabinet's local
socket as root, in order, in small batches, again and again while the cabinet is away. The host keeps no texts of
its own: its journal names sessions, agents, commands and states only.

Before it hands a process a turn's input the host writes the commands in flight to a root-only state file; the
next host's start reports them, so a crash between the write and the turn's first line fails the command instead
of delivering it twice. Raw streams older than ``EVENTS_KEEP_DAYS`` are removed.

A worker job is ``systemd-run --pipe`` of the root runner (``worker.argv``): the host writes the job's
definition to its stdin and closes it, keeps its stdout in the job's raw file and takes the runner's end line as
the outcome. A running job is journaled like a command in flight, until the cabinet heard its end.

A factory operation (``operation``) pauses and drains its roles and runs the root runner's unit; each tick
moves it on (``kd.session_host.operation``). Root may ask ``status`` and ``resume`` a role's pause.

A dev.check (``devcheck``) runs the same root runner as its own unit, beside any operation and with no pause;
each tick reports a runner's result or ends a runner gone without one (``kd.session_host.devcheck``).

A model probe (``probe``) is one short ``claude -p`` under the base user on a cabinet model's kind
(``kd.session_host.probe``), one at a time: the host answers ``running`` (``busy`` for another id meanwhile,
``no_key: <kind>`` without the kind's key file) and reports its outcome as a ``probe`` item.
"""

import asyncio
from dataclasses import dataclass, field
import functools
import json
import os
import sys
import time
from typing import Callable, Dict, Optional

from kd import claude_token, llm_models, naming
from kd.cabinet import client as cabinet_client
from kd.cabinet.client import CabinetError
from kd.session_host import claude, core, devcheck, operation, probe, protocol, worker

SOCKET = "/run/digipaddock-session-host/host.sock"
LOGS = "/var/log/digipaddock-session"
JOURNAL = LOGS + "/host.jsonl"
EVENTS = LOGS + "/events"
STATE = operation.STATE
INFLIGHT = "inflight.json"
EVENTS_KEEP_DAYS = 14
PRUNE_EVERY = 3600.0
BATCH = 50
REPORT_BYTES = 768 * 1024  # a report's line, well under the cabinet's 1 MiB (output excerpts)
RETRY = (1.0, 2.0, 5.0, 10.0)
TICK = 15.0
GRACE = 20.0
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _utf8(item: dict) -> dict:
    """``item`` as UTF-8 can carry it: a lone surrogate from Claude's JSON (a cut emoji) becomes «?» — it would stop
    the report task at its encoding."""
    text = json.dumps(item, ensure_ascii=False)
    try:
        text.encode("utf-8")
        return item
    except UnicodeEncodeError:
        return json.loads(text.encode("utf-8", "replace").decode("utf-8"))


def _open_append(path: str, mode: int = 0o600) -> int:
    """An append-only fd of a root file: never through a link."""
    return os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | _NOFOLLOW, mode)


@dataclass
class _Proc:
    process: object
    unit: str
    normalizer: claude.Normalizer
    events_fd: int
    # card → the instruction selection this process was given in full: resent only on a change
    instructions: dict = field(default_factory=dict)


@dataclass
class _JobProc:
    process: object
    unit: str
    events_fd: int


async def _lines(stream, limit: int):
    """Lines of ``stream``; a line longer than ``limit`` is dropped whole, the next one read as usual."""
    skipping = False
    while True:
        try:
            line = await stream.readuntil(b"\n")
        except asyncio.IncompleteReadError as exc:
            if exc.partial and not skipping:
                yield exc.partial
            return
        except asyncio.LimitOverrunError as exc:
            await stream.readexactly(exc.consumed)
            skipping = True
            continue
        if skipping:
            skipping = False
            continue
        yield line


async def systemctl_active(unit: str) -> bool:
    """The unit still runs (or starts, or stops): its ActiveState is not inactive or failed."""
    proc = await asyncio.create_subprocess_exec(
        "systemctl", "show", "-p", "ActiveState", "--value", "--", unit, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return out.decode("utf-8", "replace").strip() in ("active", "activating", "deactivating", "reloading")


async def systemctl_stop(unit: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "systemctl", "stop", "--", unit, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()


class HostServer:
    def __init__(self, *, config, users: Callable[[str], str], cabinet, allowed_uid: int,
                 spawn=None, stop_unit=None, events_dir: str = EVENTS, journal_path: str = JOURNAL,
                 state_dir: str = STATE, clock=time.monotonic, batch_interval: float = 0.5, retry_delays=RETRY, tick_interval: float = TICK,
                 grace: float = GRACE, err=None, job_slots: Optional[Callable[[str], list]] = None,
                 unit_active=None, wall=time.time, instruction_check=None, base_user: str = ""):
        self.config, self.users_of, self.cabinet = config, users, cabinet
        self.allowed_uid = allowed_uid
        self.base_user = base_user or naming.base_user()  # whose ``claude`` a model probe runs
        self._probe: Optional[dict] = None  # the probe under way: {probe, task}
        self.instruction_check = instruction_check
        self.spawn = spawn or asyncio.create_subprocess_exec
        self.stop_unit = stop_unit or systemctl_stop
        self.unit_active, self.wall = unit_active or systemctl_active, wall
        self.events_dir, self.journal_path = events_dir, journal_path
        self.inflight_path = os.path.join(state_dir, INFLIGHT)
        self._inflight_saved: Optional[list] = None
        self._pruned = 0.0
        self.batch_interval, self.retry_delays = batch_interval, tuple(retry_delays)
        self.tick_interval, self.grace, self.err = tick_interval, grace, err or sys.stderr
        self.core = core.Host(config.get(), self._users(config.get()), clock,
                              job_slots=job_slots or (lambda agent: []))
        self.procs: Dict[str, _Proc] = {}
        self.job_procs: Dict[str, _JobProc] = {}
        self._pending: list = []
        self._wake = asyncio.Event()
        self._tasks: list = []
        self._checks: set = set()  # instruction checks under way: one per turn, gone when done
        self._lock = asyncio.Lock()
        os.makedirs(self.events_dir, mode=0o700, exist_ok=True)
        os.makedirs(state_dir, mode=0o700, exist_ok=True)
        self.ops = operation.Operations(state_dir, self.core)
        self.checks = devcheck.Checks(state_dir)
        self._stops: set = set()  # the stops of checks' units under way (``_stop_check``)

    # --- config ---------------------------------------------------------------------------------
    def _users(self, config) -> dict:
        users = {}
        for agent in config.agents:
            try:
                users[agent] = self.users_of(agent)
            except (KeyError, ValueError, OSError) as exc:
                self.err.write(f"kd-session-host: {agent}: no user ({exc})\n")
        return users

    def _refresh(self):
        config = self.config.get()
        if config is not self.core.config:
            self.core.config, self.core.users = config, self._users(config)

    # --- the commands in flight, the raw streams -------------------------------------------------
    def _read_inflight(self) -> list:
        try:
            with open(self.inflight_path, encoding="utf-8") as handle:
                value = json.load(handle)
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            self.err.write(f"kd-session-host: {self.inflight_path}: {exc}\n")
            return []
        if not isinstance(value, list):
            return []
        return [c for c in value if isinstance(c, str) and protocol.HEX32.fullmatch(c)]

    def _journaled(self) -> list:
        """The commands a crash must fail rather than repeat: those written to a process whose turn has not ended,
        and those whose turn the cabinet has not heard of yet (a report still pending; a previous host's, while its
        «host started» report waits)."""
        ids = list(self.core.inflight())
        for item in self._pending:
            if item.get("type") == "turn":
                found = [item.get("command")]
            elif item.get("type") == "job" and item.get("state") not in ("starting", "running"):
                found = [item.get("job")]
            elif item.get("type") == "host":
                found = list(item.get("inflight") or ())
            else:
                continue
            ids += [c for c in found if c and c not in ids]
        return ids

    def _save_inflight(self) -> bool:
        """Write the journaled commands when they changed: atomically, root-only, never through a link.
        False — the journal could not be written."""
        current = self._journaled()
        if current == self._inflight_saved:
            return True
        tmp = f"{self.inflight_path}.{os.getpid()}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _NOFOLLOW, 0o600)
            try:
                os.write(fd, json.dumps(current).encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, self.inflight_path)
            self._inflight_saved = current
            return True
        except OSError as exc:
            self.err.write(f"kd-session-host: {self.inflight_path}: {exc}\n")
            return False

    def prune_events(self) -> int:
        """Remove raw streams older than EVENTS_KEEP_DAYS, never a running session's; returns how many."""
        horizon = time.time() - EVENTS_KEEP_DAYS * 86400
        removed = 0
        try:
            names = os.listdir(self.events_dir)
        except OSError:
            return 0
        for name in names:
            session, _, kind = name.partition(".")
            if kind not in ("jsonl", "stderr") or session in self.procs or session in self.job_procs:
                continue
            path = os.path.join(self.events_dir, name)
            try:
                if os.lstat(path).st_mtime < horizon:
                    os.unlink(path)
                    removed += 1
            except OSError:
                pass
        return removed

    # --- the journal and the reports ------------------------------------------------------------
    def journal(self, op: str, **fields):
        line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "op": op, **fields}
        try:
            fd = _open_append(self.journal_path)
            try:
                os.write(fd, (json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
            finally:
                os.close(fd)
        except OSError as exc:
            self.err.write(f"kd-session-host: journal: {exc}\n")

    def queue(self, item: dict):
        self._pending.append(_utf8(item))
        self._wake.set()

    async def _send(self, items: list) -> bool:
        """True when the cabinet took the items (or refused them for good); False to try again later."""
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self.cabinet.call, {"op": "host.report", "items": items})
            return True
        except CabinetError as exc:
            if exc.kind == "unavailable":
                return False
            if len(items) == 1:
                self.err.write(f"kd-session-host: the cabinet refused a report item, dropped ({exc.kind}: {exc}):"
                               f" {json.dumps(items[0])[:300]}\n")
                return True
            for item in items:  # find the one it refuses; the others still go, in order
                if not await self._send([item]):
                    return False
            return True
        except OSError:
            return False

    def _batch(self) -> list:
        """The oldest pending items, at most BATCH, whose report line stays within REPORT_BYTES; always one."""
        size = len(cabinet_client.encode({"op": "host.report", "items": []}))
        batch = []
        for item in self._pending[:BATCH]:
            size += len(json.dumps(item, ensure_ascii=False).encode("utf-8")) + (2 if batch else 0)  # ", "
            if batch and size > REPORT_BYTES:
                break
            batch.append(item)
        return batch

    async def _reporter(self):
        failures = 0
        while True:
            if not self._pending:
                self._wake.clear()
                await self._wake.wait()
            await asyncio.sleep(self.batch_interval)
            batch = self._batch()
            if await self._send(batch):
                del self._pending[: len(batch)]
                self._save_inflight()  # the cabinet knows of these turns now: a crash no longer needs to fail them
                failures = 0
            else:
                await asyncio.sleep(self.retry_delays[min(failures, len(self.retry_delays) - 1)])
                failures += 1

    async def flush(self, timeout: float = 5.0):
        end = asyncio.get_running_loop().time() + timeout
        while self._pending and asyncio.get_running_loop().time() < end:
            await asyncio.sleep(0.02)

    # --- requests -------------------------------------------------------------------------------
    async def request(self, line: bytes, uid: int) -> bytes:
        try:
            req = protocol.parse(line)
        except protocol.ProtocolError as exc:
            return protocol.error(exc.kind, str(exc))
        if uid != self.allowed_uid and not (uid == 0 and req["op"] in ("status", "resume")):
            self.journal("refused", uid=uid, request=req["op"])
            return protocol.error("refused", "the session host takes requests from the cabinet only")
        async with self._lock:
            self._refresh()
            if req["op"] == "status":
                # the jobs whose end is still on its way: the cabinet's sweep must not take them for lost ones
                reporting = [i["job"] for i in self._pending
                             if i.get("type") == "job" and i.get("state") not in ("queued", "starting", "running")]
                return protocol.ok(**self.core.status(), reporting=reporting, checks=self.checks.status())
            if req["op"] == "stop":
                self.journal("stop", session=req["session"])
                await self._run(self.core.stop(req["session"]))
                return protocol.ok()
            if req["op"] == "revoke":
                self.journal("revoke", session=req["session"], egress=req["egress"])
                await self._run(self.core.revoke(req["session"], req["egress"]))
                return protocol.ok()
            if req["op"] == "job":
                try:
                    ack, actions = self.core.job(req)
                except protocol.ProtocolError as exc:
                    self.journal("job_refused", job=req["job"], agent=req["agent"])
                    return protocol.error(exc.kind, str(exc))
                self.journal("job", job=req["job"], agent=req["agent"], state=ack["state"])
                await self._run(actions)
                return protocol.ok(**ack)
            if req["op"] == "job_cancel":
                ack, actions = self.core.job_cancel(req["job"])
                self.journal("job_cancel", job=req["job"], cancelled=ack["cancelled"])
                await self._run(actions)
                return protocol.ok(**ack)
            if req["op"] == "operation":
                try:
                    ack, actions = self.ops.request(req, self._hosted, self.wall(), self._hosted_all)
                except protocol.ProtocolError as exc:
                    self.journal("operation_refused", operation=req["operation"], agents=req["agents"])
                    return protocol.error(exc.kind, str(exc))
                if actions:
                    self.journal("operation", operation=req["operation"], agents=req["agents"],
                                 revision=req["revision"])
                await self._run(actions)
                return protocol.ok(**ack)
            if req["op"] == "operation_cancel":
                ack, actions = self.ops.cancel(req["operation"], req["reason"])
                self.journal("operation_cancel", operation=req["operation"], reason=req["reason"], state=ack["state"])
                await self._run(actions)
                return protocol.ok(**ack)
            if req["op"] == "devcheck":
                try:
                    ack, actions = self.checks.request(req, self.wall())
                except protocol.ProtocolError as exc:
                    return protocol.error(exc.kind, str(exc))
                if actions:
                    self.journal("devcheck", check=req["check"], project=req["params"]["project"],
                                 contour=req["params"]["contour"])
                await self._run(actions)
                return protocol.ok(**ack)
            if req["op"] == "devcheck_cancel":
                ack, actions = self.checks.cancel(req["check"], req["reason"])
                self.journal("devcheck_cancel", check=req["check"], reason=req["reason"], state=ack["state"])
                await self._run(actions)
                return protocol.ok(**ack)
            if req["op"] == "probe":
                return self._probe_request(req)
            if req["op"] == "resume":
                try:
                    resumed, actions = self.ops.unpause(req["agent"])
                except protocol.ProtocolError as exc:
                    return protocol.error(exc.kind, str(exc))
                self.journal("resume", agent=req["agent"], uid=uid, resumed=resumed)
                await self._run(actions)
                return protocol.ok(resumed=resumed)
            if req["op"] == "cancel":
                ack, actions = self.core.cancel(req["command"])
                self.journal("cancel", session=req["session"], command=req["command"], cancelled=ack["cancelled"])
                await self._run(actions)
                return protocol.ok(**ack)
            try:
                ack, actions = self.core.deliver(req)
            except protocol.ProtocolError as exc:
                self.journal("refused", session=req["session"], agent=req["agent"], command=req["command"])
                return protocol.error(exc.kind, str(exc))
            # the exit the delivery named ("" — none; the standing one, if any, is the launch's)
            self.journal("deliver", session=req["session"], agent=req["agent"], command=req["command"],
                         state=ack["state"], egress=req.get("egress", ""),
                         **({"urgent": True} if req.get("urgent") else {}))
            await self._run(actions)
            return protocol.ok(**ack)

    def _hosted(self, agent: str) -> bool:
        return self.config.get().hosted(agent) and agent in self.core.users

    def _hosted_all(self) -> list:
        """Every role this host serves now: what a component's ``drain: all`` pauses."""
        return sorted(a for a in self.core.users if self._hosted(a))

    # --- factory operations ---------------------------------------------------------------------
    async def operation_tick(self):
        """Move the operation under way on: a batch of the roles drained by now starts once the cabinet
        authorizes it, a batch's result is adopted (or a runner gone without one), a drain past the timeout ends it."""
        async with self._lock:
            ops = self.ops
            step = ops.step(self.wall(), self.core.drained)
            if step in ("idle", "wait"):
                return
            operation_id = ops.active["operation"]
            if step != "launch":
                actions = await self._operation_step(step, operation_id)
                await self._run(actions)
                return
            ready = ops.ready(self.core.drained)
        # the cabinet's word on this very batch, asked outside the lock: the host serves on meanwhile
        verdict = await self._authorize(operation_id, ready)
        async with self._lock:
            ops = self.ops
            if ops.active is None or ops.active["operation"] != operation_id or ops.active["phase"] != operation.DRAINING:
                return
            if ops.component():  # one run with no roles, still only once every paused role is drained
                if ops.step(self.wall(), self.core.drained) != "launch":
                    return
                ready = []
            else:
                ready = [a for a in ops.ready(self.core.drained) if a in ready]
                if not ready:
                    return
            if verdict is None:
                return  # the cabinet did not answer: nothing starts, the drain's timeout still runs
            if not verdict["allowed"]:
                reason = str(verdict.get("reason") or "")
                reason = reason if protocol.REASON.fullmatch(reason) else "refused"
                self.journal("operation_refused", operation=operation_id, agents=ready, reason=reason)
                actions = ops.finish({"state": "failed", "reason": reason, "phase": operation.DRAINING})
            else:
                try:
                    n, actions = ops.launch(ready)
                except OSError as exc:
                    self.err.write(f"kd-session-host: operation {operation_id}: {exc}\n")
                    actions = ops.finish({"state": "failed", "reason": operation.LAUNCH_FAILED,
                                          "phase": operation.DRAINING})
                else:
                    if not await self._launch(operation_id, n):
                        actions += ops.finish({"state": "failed", "reason": operation.LAUNCH_FAILED,
                                               "phase": operation.INSTALLING})
            await self._run(actions)

    async def _operation_step(self, step: str, operation_id: str) -> list:
        ops = self.ops
        if step == "timeout":
            self.journal("operation_drain_timeout", operation=operation_id, agents=ops.remaining())
            return ops.finish({"state": "failed", "reason": operation.DRAIN_TIMEOUT, "phase": operation.DRAINING})
        if step == "complete":
            return ops.complete()
        batch = ops.batch()
        read = (lambda: ops.result(operation_id)) if batch is None else (lambda: ops.batch_result(batch["n"]))
        done = read()
        if done is None and not await self.unit_active(operation.unit(operation_id, batch and batch["n"])):
            done = read() or {"state": "failed", "reason": operation.RUNNER_LOST}
        if done is None:
            return []
        self.journal("operation_batch" if batch else "operation_end", operation=operation_id,
                     batch=batch["n"] if batch else 0, state=done["state"], reason=done.get("reason") or "")
        return ops.finish(done) if batch is None else ops.adopt(done)

    async def _authorize(self, operation_id: str, agents) -> Optional[dict]:
        """``operation.authorize``: ``{"allowed", "reason"}``, or None when the cabinet did not answer."""
        loop = asyncio.get_running_loop()
        try:
            answer = await loop.run_in_executor(None, self.cabinet.call, {
                "op": "operation.authorize", "operation": operation_id, "agents": list(agents)})
        except (CabinetError, OSError, ValueError) as exc:
            self.err.write(f"kd-session-host: operation {operation_id}: no authorization ({exc})\n")
            return None
        if not isinstance(answer, dict) or not isinstance(answer.get("allowed"), bool):
            return None
        return answer

    async def _launch(self, operation_id: str, batch: int) -> bool:
        try:
            proc = await self.spawn(*operation.argv(operation_id, batch), stdin=asyncio.subprocess.DEVNULL,
                                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            rc = await proc.wait()
        except (OSError, ValueError) as exc:
            self.err.write(f"kd-session-host: operation {operation_id}: launch failed ({exc})\n")
            rc = -1
        self.journal("operation_launch", operation=operation_id, unit=operation.unit(operation_id, batch), rc=rc)
        return rc == 0

    # --- model probes ------------------------------------------------------------------
    def _probe_request(self, req: dict) -> bytes:
        """One at a time: the same id again answers ``running``, another ``busy``; the kind's key must be on the
        host (the unit would start without it and fail on the provider, telling nothing)."""
        if self._probe is not None:
            if self._probe["probe"] == req["probe"]:
                return protocol.ok(state="running")
            return protocol.error("refused", "busy")
        spec = req["model"]
        # a subscription model's probe runs on the host's Claude token, like the credential's own probe
        paid = spec is not None and not llm_models.is_subscription(spec)
        name = spec["id"] if spec else "claude-subscription"
        provider = spec["provider"] if paid else "claude-subscription"
        if not (llm_models.key_present(provider) if paid else claude_token.read()):
            self.journal("probe_refused", probe=req["probe"], model=name, reason="no_key")
            return protocol.error("refused", f"no_key: {provider}")
        self.journal("probe", probe=req["probe"], model=name, provider=provider)
        task = asyncio.ensure_future(self._run_probe(req["probe"], spec))
        self._probe = {"probe": req["probe"], "task": task}
        self._tasks.append(task)
        return protocol.ok(state="running")

    async def _run_probe(self, probe_id: str, spec: dict):
        started = self.wall()
        rc, out, err = -1, b"", b""
        paid = spec is not None and not llm_models.is_subscription(spec)
        provider = spec["provider"] if paid else "claude-subscription"
        if not (llm_models.read_secret(provider) if paid else claude_token.read()):
            # the file was there at the request; root reads it once more right before the spawn — gone or
            # malformed by now (the runbook's kill switch), the probe says so and claude never starts
            result = {"ok": False, "model": "", "ms": 0, "detail": f"no_key: {provider}"}
        else:
            try:
                argv = probe.argv(probe.unit(probe_id), self.base_user, spec)
                proc = await self.spawn(*argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                        stderr=asyncio.subprocess.PIPE)
                out, err = await proc.communicate()
                rc = proc.returncode
            except (OSError, ValueError) as exc:
                err = f"launch failed: {exc.__class__.__name__}".encode("utf-8")
            result = probe.outcome(rc, out[:probe.DETAIL_MAX * 64].decode("utf-8", "replace"),
                                   err[:probe.DETAIL_MAX * 16].decode("utf-8", "replace"), started, self.wall())
        async with self._lock:
            self._probe = None
            self.journal("probe_end", probe=probe_id, model=spec["id"] if spec else "claude-subscription", ok=result["ok"], ms=result["ms"], rc=rc)
            self.queue({"type": "probe", "probe": probe_id, **result})

    # --- dev.check ---------------------------------------------------------------------
    async def devcheck_tick(self):
        """A runner's result is reported; a runner gone without one is stopped with its children and ends."""
        watched = sorted(self.checks.watched)
        if not watched:
            return
        active = {}
        for check_id in watched:  # read before the results: a runner gone by now has written what it would
            active[devcheck.unit(check_id)] = await self.unit_active(devcheck.unit(check_id))
        async with self._lock:
            await self._run(self.checks.tick(lambda name: active.get(name, True)))

    async def _launch_check(self, action: "devcheck.Launch") -> None:
        try:
            proc = await self.spawn(*action.argv, stdin=asyncio.subprocess.DEVNULL,
                                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            rc = await proc.wait()
        except (OSError, ValueError) as exc:
            self.err.write(f"kd-session-host: devcheck {action.check}: launch failed ({exc})\n")
            rc = -1
        self.journal("devcheck_launch", check=action.check, unit=devcheck.unit(action.check), rc=rc)
        if rc != 0:
            await self._run(self.checks.launch_failed(action.check))

    async def _stop_check(self, action: "devcheck.Stop") -> None:
        """The tests' unit, the dependencies' and the runner, in that order; then the check ends (``stopped``)."""
        for name in action.units:
            await self.stop_unit(name)
        still = [name for name in action.units if await self.unit_active(name)]
        self.journal("devcheck_stopped", check=action.check, still=still)
        async with self._lock:
            await self._run(self.checks.stopped(action.check, still, self.wall()))

    # --- actions --------------------------------------------------------------------------------
    async def _run(self, actions):
        failed = set()  # sessions whose spawn failed: the rest of this batch is not theirs any more
        for action in actions:
            if isinstance(action, core.Report):
                self.queue(action.item)
            elif isinstance(action, core.Spawn):
                if not await self._spawn(action):
                    failed.add(action.session)
            elif isinstance(action, core.Write):
                if action.session in failed:
                    continue
                # before the input: a crash after it fails the command, never repeats it; without the journal the
                # input is not written — the unit stops and its exit fails the turn
                if self._save_inflight():
                    await self._write(action)
                else:
                    proc = self.procs.get(action.session)
                    self.journal("inflight_failed", session=action.session, unit=proc.unit if proc else "")
                    if proc is not None:
                        asyncio.ensure_future(self.stop_unit(proc.unit))
            elif isinstance(action, core.Close):
                proc = self.procs.get(action.session)
                if proc is not None and proc.process.stdin is not None:
                    proc.process.stdin.close()
            elif isinstance(action, core.Kill):
                proc = self.procs.get(action.session)
                if proc is not None:
                    self.journal("kill", session=action.session, unit=proc.unit)
                    asyncio.ensure_future(self.stop_unit(proc.unit))
            elif isinstance(action, devcheck.Launch):
                await self._launch_check(action)
            elif isinstance(action, devcheck.Stop):
                task = asyncio.ensure_future(self._stop_check(action))  # outside the lock: a stop may take a while
                self._stops.add(task)
                task.add_done_callback(self._stops.discard)
            elif isinstance(action, core.SpawnJob):
                await self._spawn_job(action)
            elif isinstance(action, core.KillJob):
                job = self.job_procs.get(action.job)
                if job is not None:
                    self.journal("job_kill", job=action.job, unit=job.unit)
                    asyncio.ensure_future(self.stop_unit(job.unit))
        self._save_inflight()

    async def _spawn_job(self, action: core.SpawnJob):
        """The runner's unit with the definition on its stdin; the job is journaled in flight before it starts."""
        if not self._save_inflight():
            self.journal("inflight_failed", job=action.job, unit=action.unit)
            await self._run(self.core.job_exited(action.job, -1, error="inflight_failed"))
            return
        events_fd = -1
        try:
            argv = worker.argv(action.unit, action.slot, action.timeout)
            events_fd = _open_append(os.path.join(self.events_dir, f"{action.job}.jsonl"))
            stderr_fd = _open_append(os.path.join(self.events_dir, f"{action.job}.stderr"))
            try:
                process = await self.spawn(*argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                           stderr=stderr_fd, limit=worker.LINE_MAX)
            finally:
                os.close(stderr_fd)
        except (OSError, ValueError) as exc:
            if events_fd >= 0:
                os.close(events_fd)
            self.journal("job_spawn_failed", job=action.job, agent=action.agent, error=exc.__class__.__name__)
            await self._run(self.core.job_exited(action.job, -1, error="spawn_failed"))
            return
        job = self.job_procs[action.job] = _JobProc(process, action.unit, events_fd)
        self.journal("job_spawn", job=action.job, agent=action.agent, unit=action.unit, slot=action.slot)
        try:
            process.stdin.write(json.dumps(action.definition, ensure_ascii=False).encode("utf-8") + b"\n")
            await process.stdin.drain()
            process.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the runner is gone: its exit fails the job
        self._tasks.append(asyncio.ensure_future(self._read_job(action.job, job)))

    async def _read_job(self, job_id: str, job: _JobProc):
        try:
            async for raw in _lines(job.process.stdout, worker.LINE_MAX):
                try:
                    os.write(job.events_fd, raw if raw.endswith(b"\n") else raw + b"\n")
                except OSError:
                    pass
                end = worker.end_line(raw)
                if end is not None:
                    async with self._lock:
                        self.core.job_line(job_id, end)
        except asyncio.CancelledError:
            os.close(job.events_fd)
            raise
        except Exception as exc:  # noqa: BLE001 — a bug here must still end the job below
            self.err.write(f"kd-session-host: job {job_id}: reader failed ({exc.__class__.__name__}: {exc})\n")
        rc = await job.process.wait()
        os.close(job.events_fd)
        async with self._lock:
            self.job_procs.pop(job_id, None)
            self.journal("job_exit", job=job_id, rc=rc)
            await self._run(self.core.job_exited(job_id, rc))

    async def _spawn(self, action: core.Spawn) -> bool:
        events_fd = -1
        # a cabinet model — the kind's key file instead of the host's Claude token, the provider's plain
        # variables, the model at the provider and the effort on the launch's command line. a model of the
        # subscription is only --model <alias> on the host's own token — no key file, no provider variables, and
        # its usage under the names Claude Code reports
        slug, env_file, setenv = "", claude_token.PATH, {}
        try:
            if action.model is not None and not llm_models.is_subscription(action.model):
                slug = action.model["id"]
                env_file = llm_models.env_file(action.model["provider"])
                setenv = llm_models.environment(action.model)
                if not llm_models.read_secret(action.model["provider"]):
                    # the key file was there at the delivery; gone or malformed by the spawn (the runbook's kill
                    # switch), the unit would start on the user's stored login with ANTHROPIC_BASE_URL pointing
                    # at the provider — the session fails instead, telling why
                    self.journal("spawn_failed", session=action.session, agent=action.agent, error="no_key")
                    await self._run(self.core.exited(action.session, -1,
                                                     error=f"no_key: {action.model['provider']}"))
                    return False
            argv = claude.argv(action.agent, action.user, action.unit, action.resume,
                               claude.context_args(action.session, action.thread, action.card, action.space,
                                                   action.memory, action.model, action.effort,
                                                   action.egress),
                               env_file=env_file, setenv=setenv)
            events_fd = _open_append(os.path.join(self.events_dir, f"{action.session}.jsonl"))
            stderr_fd = _open_append(os.path.join(self.events_dir, f"{action.session}.stderr"))
            try:
                process = await self.spawn(*argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                           stderr=stderr_fd, limit=claude.MAX_LINE)
            finally:
                os.close(stderr_fd)
        except (OSError, ValueError) as exc:
            if events_fd >= 0:
                os.close(events_fd)
            self.journal("spawn_failed", session=action.session, agent=action.agent, error=exc.__class__.__name__)
            await self._run(self.core.exited(action.session, -1, error="spawn_failed"))
            return False
        proc = _Proc(process, action.unit, claude.Normalizer(slug), events_fd)
        self.procs[action.session] = proc
        self.journal("spawn", session=action.session, agent=action.agent, unit=action.unit,
                     resume=bool(action.resume), model=action.model["id"] if action.model else "",
                     effort=action.effort)
        self._tasks.append(asyncio.ensure_future(self._read(action.session, proc)))
        return True

    async def _write(self, action: core.Write):
        proc = self.procs.get(action.session)
        if proc is None or proc.process.stdin is None:
            return
        if self.instruction_check is None:
            await self._input(proc, action.text)
            return
        session = self.core._sessions[action.session]
        check = functools.partial(self.instruction_check, self.cabinet, session.agent, session.card,
                                  delivered=proc.instructions)
        # the registry, the installed files and the cabinet are blocking reads: neither on the loop nor under the
        # host's lock, which every other session's output, ticks and the cabinet's requests wait for (review M-2)
        task = asyncio.ensure_future(self._checked_write(action, proc, check))
        self._checks.add(task)
        task.add_done_callback(self._checks.discard)

    async def _checked_write(self, action: core.Write, proc: _Proc, check):
        previous = dict(proc.instructions)
        try:
            preamble = await asyncio.get_running_loop().run_in_executor(None, check)
        except Exception as exc:  # noqa: BLE001 — a refusal or the check's own bug: the turn fails, never hangs
            async with self._lock:
                if self._turn_of(action, proc):
                    self.journal('instruction_check_failed', session=action.session, reason=str(exc))
                    await self._run(self.core.turn_ended(action.session, False,
                                                         error='instruction check failed: ' + str(exc)))
            return
        async with self._lock:
            if self._turn_of(action, proc):
                if await self._input(proc, preamble + action.text):
                    card = self.core._sessions[action.session].card
                    current = proc.instructions.get(card)
                    if current and current != "foreign" and current != previous.get(card):
                        chosen = json.loads(current)
                        self.queue({"type": "message", "session": action.session, "command": action.command,
                                    "kind": "status", "body": "",
                                    "payload": {"key": "sys.session.instructions_loaded", "params": {},
                                                "activity": {"type": "instructions", "state": "loaded", **chosen}}})
                else:
                    proc.instructions.clear()
                    proc.instructions.update(previous)

    def _turn_of(self, action: core.Write, proc: _Proc) -> bool:
        """The checked turn is still the one running in the process it was given to; otherwise it already ended
        (the process exited, the session was stopped) and the session may be running the next one."""
        return self.procs.get(action.session) is proc and self.core.current(action.session) == action.command

    async def _input(self, proc: _Proc, text: str):
        if proc.process.stdin is None:
            return False
        try:
            proc.process.stdin.write(claude.input_line(text))
            await proc.process.stdin.drain()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False  # the process is gone: its exit fails the turn

    async def _read(self, session: str, proc: _Proc):
        try:
            async for raw in _lines(proc.process.stdout, claude.MAX_LINE):
                try:
                    os.write(proc.events_fd, raw if raw.endswith(b"\n") else raw + b"\n")
                except OSError:
                    pass
                async with self._lock:
                    await self._run(self.core.output(session))
                event = claude.parse_line(raw)
                if event is None:
                    continue
                for item in proc.normalizer.feed(event):
                    async with self._lock:
                        await self._item(session, proc, item)
        except asyncio.CancelledError:
            os.close(proc.events_fd)  # the host stops: the process is the unit's to end
            raise
        except Exception as exc:  # noqa: BLE001 — a bug here must still end the session below
            self.err.write(f"kd-session-host: {session}: reader failed ({exc.__class__.__name__}: {exc})\n")
        rc = await proc.process.wait()
        os.close(proc.events_fd)
        async with self._lock:
            self.procs.pop(session, None)
            self.journal("exit", session=session, rc=rc)
            await self._run(self.core.exited(session, rc))

    async def _item(self, session: str, proc: _Proc, item: dict):
        if item["type"] == "session":
            self.core.runtime(session, item["runtime_session_id"])
            self.queue({**item, "session": session})
        elif item["type"] == "message":  # of the turn running now: the cabinet groups a turn's steps by it
            self.queue({**item, "session": session, "command": self.core.current(session)})
        elif item["type"] == "turn_end":
            actions = self.core.turn_ended(session, item["ok"], item.get("error", ""), usage=item["usage"],
                                           assistant=item.get("assistant", True),
                                           rejected=item.get("rejected", False))
            # heartbeat — not of a process that closes (an auth failure; its exit reports the session)
            if not any(isinstance(a, core.Close) or (isinstance(a, core.Report) and a.item.get("type") == "session")
                       for a in actions):
                actions.append(core.Report({"type": "session", "session": session, "state": "working"}))
            await self._run(actions)

    # --- life -----------------------------------------------------------------------------------
    async def _ticker(self):
        while True:
            await asyncio.sleep(self.tick_interval)
            try:
                async with self._lock:
                    self._refresh()
                    await self._run(self.core.tick())
                await self.operation_tick()
                await self.devcheck_tick()
                if time.monotonic() - self._pruned >= PRUNE_EVERY:
                    self._pruned = time.monotonic()
                    self.prune_events()
                    self.checks.prune(self.wall())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # one bad tick is logged; the host keeps ticking
                self.err.write(f"kd-session-host: tick: {exc!r}\n")
                self.journal("tick_failed", error=repr(exc)[:300])

    async def start(self, report_start: bool = True):
        """The reporter and the idle ticker; first of all the cabinet hears the host (re)started."""
        if report_start:
            inflight = self._read_inflight()
            self._pending.insert(0, {"type": "host", "phase": "started", **({"inflight": inflight} if inflight else {})})
            self._save_inflight()
        self._tasks += [asyncio.ensure_future(self._reporter()), asyncio.ensure_future(self._ticker())]
        self._wake.set()
        self.journal("start")

    async def close(self):
        """Every process gets EOF (a turn in flight finishes, an idle one exits clean; inputs that never started
        stay with the cabinet), then a unit still running after the grace time is stopped."""
        loop = asyncio.get_running_loop()
        end = loop.time() + self.grace  # one budget for the checks and the processes: TimeoutStopSec holds
        while self._checks and loop.time() < end:  # an input still being checked goes in before the EOF, also one
            await asyncio.wait(set(self._checks), timeout=end - loop.time())  # whose check began during the wait
        async with self._lock:
            await self._run(self.core.shutdown())
        while self.procs and loop.time() < end:
            await asyncio.sleep(0.05)
        for unit in [p.unit for p in self.procs.values()] + [j.unit for j in self.job_procs.values()]:
            await self.stop_unit(unit)
        end = loop.time() + 5.0
        while (self.procs or self.job_procs) and loop.time() < end:  # their exits reach the reports; the next
            await asyncio.sleep(0.05)  # host's start covers the rest
        await self.flush()
        for task in self._tasks:
            task.cancel()
        self.journal("stop_host")


async def serve(*, config, users, cabinet, allowed_uid: int, sock=None, path: str = SOCKET,
                cleanup: Optional[Callable] = None, job_slots: Optional[Callable[[str], list]] = None):
    """The unit's main: stop session units a former host left behind, report the start, take requests."""
    import signal

    from kd import cap_serve

    if cleanup is not None:
        await cleanup()
    from kd.persistent_skills import turn_instructions

    host = HostServer(config=config, users=users, cabinet=cabinet, allowed_uid=allowed_uid, job_slots=job_slots,
                      instruction_check=turn_instructions)
    await host.start()

    async def handle(reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), 30)
            _, uid, _ = cap_serve.peer(writer.get_extra_info("socket"))
            writer.write(await host.request(line, uid))
            await writer.drain()
        except (OSError, asyncio.TimeoutError, ValueError, asyncio.LimitOverrunError):
            pass
        finally:
            writer.close()

    if sock is not None:
        srv = await asyncio.start_unix_server(handle, sock=sock, limit=protocol.REQUEST_MAX)
    else:
        if os.path.exists(path):
            os.unlink(path)
        srv = await asyncio.start_unix_server(handle, path=path, limit=protocol.REQUEST_MAX)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stopping.set)
    async with srv:
        await stopping.wait()
    await host.close()
