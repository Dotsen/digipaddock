"""A scripted session host for the dev cabinet: ``kd-cabinet serve --dev`` hands its dispatcher this
instead of the root host's socket, so the cabinet's whole run flow works on a developer's machine.

``DevHost(svc)`` answers ``deliver`` with ``accepted`` and then, on the loop, reports through ``host.report`` (as
root, like the real host) a turn: the session working in slot ``<agent>/1``, the turn started, one tool call with
its result and masked output (the Sessions tab's "Turn"), one agent message "Got it: <command text>", all of
that turn's command, the turn ended, the session idle. A person's newest message in the thread containing
``[fail]`` fails that turn and the session once (``continue`` then runs normally); ``stop`` stops the session.
A command it has seen is not run twice (the real host is idempotent by command id too).

It plays a council's critics as well: a ``job`` of the ``brief-critic`` or ``code-critic``
specialization is reported started in ``<agent>/worker1`` and then done with a block that found nothing, so a
council gathered in the dev cabinet ends ``clean`` as a real one would. Any other job is refused, as a host
without such a worker refuses it. ``job_cancel`` answers as the real host does — ``queued`` (never started: it is
never reported), ``running`` (its report ends it ``cancelled``), ``ended``, ``unknown``.

A run on a model of the Claude subscription reports a usage as Claude Code would name what the alias
resolved to — ``claude-<alias>-5-5`` — so the dev cabinet shows the model that served a turn and a critic, and the
version the alias served last. A session's total grows by one step per turn, as Claude's cumulative one does. Any
other run reports no usage, as before.
"""

import asyncio

from kd import llm_models
from kd.cabinet import api, dispatcher

ROOT = api.Principal("root", 0, "root")
STEP_S = 1.0  # long enough to see each state on the page
FAIL = "[fail]"
FAILURE = "error_during_execution"  # the code Claude Code gives a failed turn
# what a played critic hands back, by its specialization (kd.council.critic_result): nothing found
CRITICS = {
    "brief-critic": {"status": "done", "verdict": "ready", "findings": [], "questions": [], "missing_inputs": []},
    "code-critic": {"status": "done", "verdict": "ready", "findings": [], "questions": []},
}
NO_USAGE = {"models": {}, "requests": 0}
STEP_USAGE = {"in": 1200, "out": 300, "cache_read": 0, "cache_write": 0, "cost": 0.02}  # of one played step


def _subscription(spec) -> bool:
    return isinstance(spec, dict) and llm_models.is_subscription(spec)


def played_usage(spec, steps: int = 1) -> dict:
    """What a played run of ``steps`` steps on ``spec`` reports: under the name Claude Code gives the alias's model
    for a subscription model, nothing for any other."""
    if not _subscription(spec):
        return NO_USAGE
    upstream = spec["upstream"]
    name = f"claude-{upstream}-5-5" if upstream.isalpha() else upstream
    used = {key: round(value * steps, 6) if key == "cost" else value * steps for key, value in STEP_USAGE.items()}
    return {"models": {name: used}, "requests": 1}


class DevHost:
    def __init__(self, svc, *, step: float = STEP_S):
        self.svc, self.step = svc, step
        self.seen = set()
        self.failed = set()  # the person's [fail] messages already failed once
        self.tasks = set()
        self.probes = set()
        self.jobs = {}  # a job it was given: queued, running, ended
        self.steps = {}  # a session: the turns it played on a subscription model
        self.cancelled = set()

    async def call(self, request: dict, timeout: float = 10.0) -> dict:
        op = request.get("op")
        if op == "probe":
            if request["probe"] not in self.probes:
                self.probes.add(request["probe"])
                self._spawn(self._probe(request))
            return {"ok": True, "state": "running"}
        if op == "deliver":
            if request["command"] not in self.seen:
                self.seen.add(request["command"])
                self._spawn(self._turn(request))
            return {"ok": True, "state": "accepted"}
        if op == "stop":
            self._spawn(self._report({"type": "session", "session": request["session"], "state": "stopped"}))
            return {"ok": True}
        if op == "job":
            spec = request["definition"].get("spec")
            if spec not in CRITICS:
                raise dispatcher.HostRefused("invalid", f"the dev host plays a council's critics only, not {spec!r}")
            if request["job"] not in self.jobs:
                self.jobs[request["job"]] = "queued"
                self._spawn(self._job(request))
            return {"ok": True, "state": "accepted"}
        if op == "job_cancel":
            state = self.jobs.get(request["job"], "unknown")
            if state in ("queued", "running"):
                self.cancelled.add(request["job"])
            return {"ok": True, "cancelled": state}
        raise dispatcher.HostRefused("invalid", f"unknown op {op!r}")

    def _spawn(self, coro):
        task = asyncio.ensure_future(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _probe(self, request):
        await asyncio.sleep(self.step)
        spec = request["model"]
        await self._report({"type": "probe", "probe": request["probe"], "ok": True,
                            "model": spec["upstream"] if spec else "claude-fixture", "ms": 1000, "detail": "OK"})

    async def _report(self, *items):
        await self.svc.call(ROOT, {"op": "host.report", "items": list(items)})

    async def _job(self, request: dict):
        """A critic's read: started in the agent's first worker slot, then done with its specialization's block.
        A job cancelled before it started is never reported; one cancelled while it reads ends ``cancelled``."""
        job = {"type": "job", "job": request["job"]}
        await asyncio.sleep(self.step)
        if request["job"] in self.cancelled:
            self.jobs[request["job"]] = "ended"
            return
        self.jobs[request["job"]] = "running"
        await self._report(dict(job, state="starting", slot=f"{request['agent']}/worker1"))
        await asyncio.sleep(self.step)
        self.jobs[request["job"]] = "ended"
        if request["job"] in self.cancelled:
            await self._report(dict(job, state="cancelled", reason="stopped"))
            return
        definition = request["definition"]
        await self._report(dict(job, state="done", usage=played_usage(definition.get("model_spec")),
                                result=CRITICS[definition["spec"]]))

    def _fail_marker(self, thread: int):
        """The person's newest message in the thread, when it asks for a failure not yet made."""
        people = [m for m in self.svc.ctx.store.thread_messages(thread) if m["author"].startswith("user:")]
        if people and FAIL in people[-1]["body"] and people[-1]["seq"] not in self.failed:
            return people[-1]["seq"]
        return None

    async def _turn(self, request: dict):
        session, command = request["session"], request["command"]
        turn = {"type": "turn", "session": session, "command": command}
        spec = request.get("model")
        await asyncio.sleep(self.step)
        await self._report(
            {"type": "session", "session": session, "state": "working", "slot": f"{request['agent']}/1",
             "model": spec["id"] if isinstance(spec, dict) else ""},  # the model the process runs on, as the host says
            dict(turn, phase="started"),
        )
        await asyncio.sleep(self.step)
        marker = self._fail_marker(request["thread"])
        if marker is not None:
            self.failed.add(marker)
            await self._report(
                dict(turn, phase="ended", ok=False, error=FAILURE),
                {"type": "session", "session": session, "state": "failed", "error": FAILURE},
            )
            return
        await self._report(
            {"type": "message", "session": session, "command": command, "kind": "tool_call", "body": "Bash: ls work",
             "payload": {"tool": "Bash", "id": "toolu_dev"}},
            {"type": "message", "session": session, "command": command, "kind": "tool_result", "body": "",
             "payload": {"id": "toolu_dev", "ok": True, "size": 19},
             "output": {"text": "brief.md\nnotes.md", "size": 19, "truncated": False}},
            {"type": "message", "session": session, "command": command, "kind": "text",
             "body": f"Got it: {request['text']}"},
            dict(turn, phase="ended", ok=True, **self._turn_usage(session, spec)),
            {"type": "session", "session": session, "state": "idle"},
        )

    def _turn_usage(self, session: str, spec) -> dict:
        """The ended turn's ``usage`` — the session's total so far — on a subscription model; nothing otherwise."""
        if not _subscription(spec):
            return {}
        self.steps[session] = self.steps.get(session, 0) + 1
        return {"usage": played_usage(spec, self.steps[session])}
