"""The session host's state machine, without I/O: the server feeds it requests and process events and
carries out the actions it returns.

A session (one cabinet thread of one agent) has at most one process and runs one turn at a time; inputs that
arrive meanwhile wait in its FIFO. An agent has ``slots`` processes at most: a session that needs one while all
are taken waits in the agent's FIFO, and an idle process of the same agent is closed at once to free its slot (a
working one never is). The quantum is one turn: a session whose turn ends with more inputs while another
session of the agent waits for a slot and none is free gives its slot up — its process closes between turns and its
exit puts it at the tail of the agent's FIFO, so ready sessions take one turn each in turn; with no one waiting the
next input goes to the same process. A waiting session reports its place in that FIFO whenever it changes. A process
idle for ``idle_s`` is closed; its next input resumes it (``--resume``). A process
that exits on its own during a turn fails its session and the commands it held; the cabinet offers "Continue".
A resumed one that exits before its first line is retried once without ``--resume`` — the Claude session it resumed
is gone. One that ends between turns (a reinstall's signal) just ends: the next input resumes it.

A turn is reported started at the process's first output line after its input (``output``), not when the input is
written: a process that hangs before it answers reads «start delayed» in the cabinet. A session with no process and
no input is forgotten — the cabinet sends the resume id with every delivery.

A session's agent, thread and card (none: the agent's chat) are fixed by its first delivery: a later one
that names another is refused before it queues, and the process is spawned with them as its context.

A worker job runs in one of its agent's worker slots (``job_slots``: the enabled slots of
worker-slots.json), a pool apart from the sessions': a job never waits for a session, nor a session for a job.
Jobs beyond the free slots wait in the agent's FIFO. The runner's end line is the job's outcome; a job without one
failed with its exit, a stopped one is cancelled, one cut by the host's stop failed ``host_restart``.

A paused agent (a factory operation reinstalls it) starts nothing: its idle processes close at once, a turn
under way ends as usual and its process closes after it, its running jobs finish; new inputs and jobs wait in its
queues for the resume. The agent is drained when none of its processes and jobs is left.

A card's session runs on the model and effort its latest delivery names (the cabinet's resolution of the
card's and the project's settings; none — the subscription and the agent's effort). The pair is the process's for
its life: a delivery with another pair closes an idle process at once and a working one after its turn, and the
next input starts a process on the new pair. A delivery of a model whose key the host does not hold is refused
(``no_key``). A process that resumes a history another model wrote and fails its first turn without an answer runs
that input once more fresh (no ``--resume``) and says so in the thread; a second failure is the turn's. Every
session's process runs on its latest delivery's context limit (the role's, ``kd.context_limit``) the same way: a
delivery with another limit closes an idle process at once and a working one after its turn, and the next process —
with ``--resume``, the same conversation — compacts at the new limit.

An urgent input (the wake of an approved critical command, whose approval lives 15 minutes) never waits for
a slot: its session starts at once over the agent's limit — no working turn is cut, an ordinary session still waits
for one of the agent's own slots — and goes through a pause while ``urgent_in_pause`` says the role only drains (the
drain then waits for its turn as for any other); while the role's install runs it waits like any input, and the
resume starts it over the slots too. Behind a turn of its own session it waits for that turn, then runs before the
session's older inputs. Each approved critical command may add one process over the limit; an idle one over the
limit is closed as soon as an ordinary session waits.
"""

from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Mapping, Optional, Tuple

from kd import llm_models
from kd.session_host.protocol import AUTH_FAILED, ProtocolError

EGRESS_ENDED = "egress_ended"  # a turn cut because its card no longer grants the process's probe exit (revoke)

SEEN_MAX = 10000
# the one status line a switch of model leaves in the thread when the new model refused the old context
HISTORY_DROPPED = "sys.session.history_dropped"


@dataclass
class Spawn:
    session: str
    agent: str
    user: str
    unit: str
    resume: str
    thread: int = 0  # the session's thread and card ("" — the agent's chat) for the launch's context
    card: str = ""
    space: str = ""  # "monitor" — the agent's "Monitor" thread, not its chat
    memory: str = ""  # the Hindsight bank of the card's project ("" — the role bank)
    model: Optional[dict] = None  # the cabinet model's spec {id, provider, upstream, base_url}; None — the subscription
    effort: str = ""  # the effort level; "" — the agent's own
    egress: str = ""  # the card's probe exit ("" — none)
    context_limit: int = 0  # the role's session context limit in tokens (0 — none: Claude Code's own window)


@dataclass
class Write:
    session: str
    text: str
    command: str


@dataclass
class Close:
    session: str


@dataclass
class Kill:
    session: str


@dataclass
class Report:
    item: dict


@dataclass
class SpawnJob:
    job: str
    agent: str
    slot: str
    unit: str
    definition: dict
    timeout: int


@dataclass
class KillJob:
    job: str


@dataclass
class _Session:
    id: str
    agent: str
    thread: int
    card: str
    space: str = ""  # fixed by the first delivery, as the thread
    memory: str = ""  # the latest delivery's bank, for the next process (a live one keeps its own)
    resume: str = ""
    inputs: Deque[Tuple[str, str]] = field(default_factory=deque)
    proc: Optional[str] = None  # None, working, idle, closing
    current: str = ""
    idle_since: float = 0.0
    slot: str = ""
    stopping: bool = False
    started: bool = False  # the current turn's first output line was seen
    text: str = ""  # the current turn's input, written again by a fresh retry
    urgent: bool = False  # the current turn's input was urgent, so is its fresh retry
    fresh: bool = False  # the resume could not start and the next process runs without it
    failed: str = ""  # why the closing process can run no turn (AUTH_FAILED); its exit fails the session
    # the latest delivery's model and effort (for the next process) and the running process's own; a
    # process
    # whose pair is not the latest closes after its turn (an idle one at once) and the next input starts a new one
    model: Optional[dict] = None
    effort: str = ""
    proc_model: Optional[dict] = None
    proc_effort: str = ""
    egress: str = ""  # the latest delivery's probe exit (for the next process) and the running process's own
    proc_egress: str = ""
    context_limit: int = 0  # the latest delivery's context limit (for the next process) and the running process's own
    proc_context_limit: int = 0
    revoked: bool = False  # the cabinet cut the working process: its card no longer grants the exit (revoke)
    last_slug: str = ""  # the cabinet model the previous process ran on ("" — the subscription)
    switched: bool = False  # the running process resumes a history another model wrote: its first failure without
    # an answer drops the history once (a fresh process, no --resume) instead of failing the turn


def _slug(model: Optional[Mapping]) -> str:
    """The usage ledger's slug of the model a process runs on: a cabinet model's id; ``""`` on the subscription —
    the agent's own model, or one of the subscription's named by reference: its usage is counted under
    the names Claude Code reports, and a change among them is no change of provider."""
    return "" if model is None or llm_models.is_subscription(model) else model["id"]


def _model_name(model: Optional[Mapping]) -> str:
    """What the cabinet shows the process runs on: the model's slug or subscription reference, ``""`` for none."""
    return model["id"] if model else ""


@dataclass
class _Job:
    id: str
    agent: str
    definition: dict
    slot: str = ""  # empty while it waits
    stopping: bool = False
    end: Optional[dict] = None


NO_END = "Рабочая сессия завершилась без результата (exit {rc})."
NO_START = "Рабочая сессия не запустилась."


def _key(s: "_Session") -> tuple:
    """What the next process runs on: the latest delivery's model, effort, probe exit and context limit; a process
    whose own differ closes after its turn (an idle one at once)."""
    return s.model, s.effort, s.egress, s.context_limit


def _proc_key(s: "_Session") -> tuple:
    return s.proc_model, s.proc_effort, s.proc_egress, s.proc_context_limit


def _place(card: str) -> str:
    return f"карточке {card}" if card else "личному чату агента"


def _same_context(s: _Session, req: Mapping) -> None:
    """a delivery to a session the host holds is of its agent, thread and card (or chat), or refused before
    it queues — never a turn of one card in another's history."""
    if req["agent"] != s.agent:
        raise ProtocolError("refused", f"сессия {s.id[:12]} принадлежит агенту {s.agent}, а доставка — для {req['agent']}")
    if req["thread"] != s.thread:
        raise ProtocolError("refused", f"сессия {s.id[:12]} привязана к треду {s.thread}, а доставка — для треда"
                                       f" {req['thread']}")
    if req["card"] != s.card:
        raise ProtocolError("refused", f"сессия {s.id[:12]} привязана к {_place(s.card)}, а доставка — к"
                                       f" {_place(req['card'])}")


class Host:
    def __init__(self, config, users: Mapping[str, str], now: Callable[[], float],
                 job_slots: Callable[[str], List[str]] = lambda agent: []):
        self.config, self.users, self.now = config, dict(users), now
        self.job_slots = job_slots
        self._sessions: Dict[str, _Session] = {}
        self._waiting: Dict[str, Deque[str]] = {}
        self._seen: "OrderedDict[str, dict]" = OrderedDict()
        self._jobs: "OrderedDict[str, _Job]" = OrderedDict()
        self._job_waiting: Dict[str, Deque[str]] = {}
        self._jobs_ended: "OrderedDict[str, str]" = OrderedDict()
        self._runs = 0
        self._down = False
        self.paused: Dict[str, str] = {}  # agent -> the operation that holds it
        self._urgent: set = set()  # the queued inputs (command ids) that start at once
        # may an urgent input start while the role is paused: only while its operation drains it (the operations set it)
        self.urgent_in_pause: Callable[[str], bool] = lambda agent: False

    # --- requests -------------------------------------------------------------------------------
    def deliver(self, req: Mapping) -> Tuple[dict, list]:
        if req["command"] in self._seen:
            return self._seen[req["command"]], []
        agent = req["agent"]
        if not self.config.hosted(agent) or agent not in self.users:
            raise ProtocolError("refused", f"{agent} is not on this session host")
        model = req.get("model")
        paid = model is not None and not llm_models.is_subscription(model)  # the subscription has no kind
        if paid and not llm_models.key_present(model["provider"]):
            # no key of the kind on this host — the cabinet says so on the card; nothing queues
            raise ProtocolError("refused", f"no_key: {model['provider']}")
        before = self._positions()
        s = self._sessions.get(req["session"])
        if s is None:
            s = self._sessions[req["session"]] = _Session(req["session"], agent, req["thread"], req["card"],
                                                          req.get("space", ""))
        else:
            _same_context(s, req)
        if not s.fresh:  # a fresh retry waiting for its start keeps off the resume that could not start
            s.resume = req["resume"] or s.resume
        s.memory = req.get("memory", "")
        s.model, s.effort, s.egress = model, req.get("effort", ""), req.get("egress", "")
        s.context_limit = req.get("context_limit", 0)
        s.inputs.append((req["command"], req["text"]))
        urgent = bool(req.get("urgent"))
        if urgent:
            self._urgent.add(req["command"])
        actions: list = []
        changed = s.proc is not None and _key(s) != _proc_key(s)
        if urgent and s.proc is None and self._may_start_urgent(agent):
            if s.id in self._waiting.get(agent, ()):
                self._waiting[agent].remove(s.id)
            actions += self._spawn(s)  # over the agent's slots: a 15-minute approval never waits for one
            ack = {"state": "starting", "urgent": True}
        elif agent in self.paused and not (urgent and self._may_start_urgent(agent)):
            ack = {"state": "queued", "paused": True}
            if s.proc == "idle":
                actions += self._close(s)  # its exit queues it
            elif s.proc is None and s.id not in self._waiting.get(agent, ()):
                self._waiting.setdefault(agent, deque()).append(s.id)
        elif changed and s.proc == "idle":
            # the process runs on another model or effort — it closes now, its exit starts the next one on
            # the new pair (a working one closes after its turn: turn_ended)
            actions += self._close(s)
            ack = {"state": "starting"}
        elif s.proc == "idle":
            actions += self._next_input(s)
            ack = {"state": "running"}
        elif s.proc in ("working", "closing"):
            ack = {"state": "running", "position": len(s.inputs)} if s.proc == "working" else {"state": "starting"}
        elif s.id in self._waiting.get(agent, ()):
            ack = {"state": "queued", "position": list(self._waiting[agent]).index(s.id) + 1}
        elif self._used(agent) < self.config.slots(agent):
            actions += self._spawn(s)
            ack = {"state": "starting"}
        else:
            waiting = self._waiting.setdefault(agent, deque())
            waiting.append(s.id)
            ack = {"state": "queued", "position": len(waiting)}
            actions += self._preempt(agent)
        actions += self._moved(before)
        self._seen[req["command"]] = ack
        while len(self._seen) > SEEN_MAX:
            self._seen.popitem(last=False)
        return ack, actions

    def stop(self, session: str) -> list:
        s = self._sessions.get(session)
        if s is None:
            return [Report({"type": "session", "session": session, "state": "stopped"})]
        if s.proc is not None:
            s.stopping = True
            return [Kill(session)]
        before = self._positions()
        if s.id in self._waiting.get(s.agent, ()):
            self._waiting[s.agent].remove(s.id)
        actions = self._drop_inputs(s, "stopped")
        del self._sessions[session]
        return actions + [Report({"type": "session", "session": session, "state": "stopped"})] + self._moved(before)

    def revoke(self, session: str, egress: str) -> list:
        """The autonomy design, piece 2: the cabinet's sweep found the card no longer grants the process's exit.
        Unlike a person's stop the session lives on: an idle process closes, a working one is cut now (its turn ends
        ``egress_ended``), and the inputs that wait start the next process without the exit. a session with
        no process yet (it waits for a slot) loses the exit its delivery named, so its first process starts without
        it."""
        s = self._sessions.get(session)
        if s is not None and s.proc is None:
            if s.egress == egress:
                s.egress = ""
            return []
        if s is None or s.proc_egress != egress:
            return []
        if s.egress == egress:
            s.egress = ""
        if s.proc == "idle":
            return self._close(s)
        if s.proc == "working":
            s.revoked = True
            return [Kill(session)]
        return []  # closing: its exit starts the next process without the exit

    def cancel(self, command: str) -> Tuple[dict, list]:
        """Take a delivered command out of the host's queues before it starts (its card moved on); a command that
        runs or ran is not touched (``cancelled`` false)."""
        for s in self._sessions.values():
            for i, (queued, _) in enumerate(s.inputs):
                if queued != command:
                    continue
                del s.inputs[i]
                self._urgent.discard(command)
                actions: list = []
                if not s.inputs and s.proc is None:
                    before = self._positions()
                    if s.id in self._waiting.get(s.agent, ()):
                        self._waiting[s.agent].remove(s.id)
                    actions.append(Report({"type": "session", "session": s.id, "state": "exited"}))
                    actions += self._moved(before)
                return {"cancelled": True}, actions
        return {"cancelled": False}, []

    def shutdown(self) -> list:
        """The host stops (a restart, a release, a disable): nothing new spawns and no new input is written. Inputs
        that never reached a process are dropped unreported — the cabinet keeps them delivered and the next host's
        start queues them again; each process gets EOF, a turn it finishes ends as usual, an idle one exits clean."""
        self._down = True
        self._waiting.clear()
        for queue in self._job_waiting.values():  # jobs waiting for a slot go the inputs' way; running ones the stop's
            for job_id in queue:
                self._jobs.pop(job_id, None)
        self._job_waiting.clear()
        actions: list = []
        self._urgent.clear()
        for s in self._sessions.values():
            s.inputs.clear()
            if s.proc == "working":
                actions.append(Close(s.id))
            elif s.proc == "idle":
                actions += self._close(s)
        return actions

    def output(self, session: str) -> list:
        """A stdout line of the session's process: the current turn's first one reports the turn started."""
        s = self._sessions.get(session)
        if s is None or not s.current or s.started:
            return []
        s.started, s.fresh = True, False
        return [Report({"type": "turn", "session": s.id, "command": s.current, "phase": "started"})]

    def inflight(self) -> list:
        """The commands written to a process whose turn has not ended, and the jobs running (the host journals
        them: a crash in between fails them instead of running them twice)."""
        return [s.current for s in self._sessions.values() if s.current] + [
            j.id for j in self._jobs.values() if j.slot]

    def current(self, session: str) -> str:
        """The command of the turn written to the session's process, "" between turns (its messages)."""
        s = self._sessions.get(session)
        return s.current if s is not None else ""

    def runtime(self, session: str, runtime_session_id: str) -> None:
        """The Claude session id the process reported: a later process of the session resumes it."""
        s = self._sessions.get(session)
        if s is not None:
            s.resume = runtime_session_id

    # --- process events -------------------------------------------------------------------------
    def turn_ended(self, session: str, ok: bool, error: str = "", usage: Optional[dict] = None,
                   assistant: bool = True, rejected: bool = False) -> list:
        """The turn's result; ``usage`` (Claude's cumulative model usage) rides on its report; ``assistant`` — the
        model answered at least once; ``rejected`` — the API refused a request as invalid (a rejection
        without an answer right after a switch of model is the history the new model would not take, not the
        turn's; any other failure — a 500, an overload, a timeout — is the turn's and keeps the history)."""
        s = self._sessions.get(session)
        if s is None or s.proc != "working" or not s.current:
            return []
        command, s.current = s.current, ""
        if (not ok and error != AUTH_FAILED and rejected and not assistant and s.switched and s.resume
                and not s.fresh):
            # the same input once more on a fresh process (no --resume), as on a failed resume; the turn has not
            # ended for the cabinet — its next «started» is the retry's
            if s.urgent:
                self._urgent.add(command)
            s.inputs.appendleft((command, s.text))
            s.resume, s.fresh, s.switched = "", True, False
            slug = _slug(s.proc_model) or "claude"
            note = Report({"type": "message", "session": s.id, "command": command, "kind": "status",
                           "body": f"history not carried over: {slug} did not accept the previous context",
                           "payload": {"key": HISTORY_DROPPED, "params": {"model": slug}}})
            return [note] + self._close(s)
        if ok or assistant:
            s.switched = False  # the new model took the history: a later failure is its own
        item = {"type": "turn", "session": s.id, "command": command, "phase": "ended", "ok": bool(ok)}
        if not ok:
            item["error"] = error or "turn failed"
        if usage is not None:
            item["usage"] = usage
        actions = [Report(item)]
        if not ok and error == AUTH_FAILED:  # no turn of this process can authenticate
            s.failed = AUTH_FAILED
            return actions + self._drop_inputs(s, AUTH_FAILED) + self._close(s)
        if _key(s) != _proc_key(s):
            # a delivery meanwhile moved the session to another model or effort: this process ends here,
            # its exit starts the next one on the new pair with the inputs that wait
            s.proc, s.idle_since = "idle", self.now()
            return actions + self._close(s)
        if s.inputs and (s.agent not in self.paused or (self._has_urgent(s) and self._may_start_urgent(s.agent))):
            if self._yields(s):
                # another session of the agent is ready and no slot is free — this one gives its slot up
                # between turns; its exit puts it at the tail of the agent's queue with its inputs and resume id
                return actions + self._close(s)
            return actions + self._next_input(s, report_working=False)
        s.proc, s.idle_since = "idle", self.now()
        actions.append(Report({"type": "session", "session": s.id, "state": "idle"}))
        if self._waiting.get(s.agent) or s.agent in self.paused:
            actions += self._close(s)
        return actions

    def exited(self, session: str, rc: int, error: str = "") -> list:
        """The session's process ended with ``rc``; ``error`` names a failure that is not an exit code (the
        process never started: ``spawn_failed``)."""
        s = self._sessions.get(session)
        if s is None or s.proc is None:
            return []
        actions: list = []
        agent = s.agent
        before = self._positions()
        if s.failed:  # closed for a failure — what came meanwhile ends with it, the session fails once,
            # though a stop or the host's shutdown came while it closed: the failure is what the owner must see
            actions += self._end_current(s, s.failed) + self._drop_inputs(s, s.failed)
            actions.append(Report({"type": "session", "session": s.id, "state": "failed", "error": s.failed}))
            del self._sessions[s.id]
        elif s.stopping:
            actions += self._end_current(s, "stopped") + self._drop_inputs(s, "stopped")
            actions.append(Report({"type": "session", "session": s.id, "state": "stopped"}))
            del self._sessions[s.id]
        elif self._down:
            cut = self._end_current(s, "host_restart")
            s.proc, s.slot = None, ""
            if cut:  # the stop's grace ran out mid-turn
                actions += cut
                actions.append(Report({"type": "session", "session": s.id, "state": "failed", "error": "host_restart"}))
            else:
                actions.append(Report({"type": "session", "session": s.id, "state": "exited"}))
            del self._sessions[s.id]
        elif s.revoked:  # the exit's consent ended mid-turn: that turn is cut, the session goes on without it
            s.revoked = False
            actions += self._end_current(s, EGRESS_ENDED)
            s.proc, s.slot = None, ""
            if s.inputs:
                self._waiting.setdefault(agent, deque()).append(s.id)
            else:
                actions.append(Report({"type": "session", "session": s.id, "state": "exited"}))
                del self._sessions[s.id]
        elif not s.current and not error:  # closed, or ended between turns (a signal): nothing was lost
            s.proc, s.slot = None, ""
            if s.inputs:  # it gave its slot up, or input came while it closed: run again, from the tail
                waiting = self._waiting.setdefault(agent, deque())
                if s.id not in waiting:
                    waiting.append(s.id)
            else:
                actions.append(Report({"type": "session", "session": s.id, "state": "exited"}))
                del self._sessions[s.id]
        elif s.current and not s.started and s.resume and not s.fresh and error != "spawn_failed":
            # A resumed process died before its first line — the Claude session it resumes is gone (or
            # cannot load): the same input once more, without --resume; the turn has not started, nothing ran twice
            if s.urgent:
                self._urgent.add(s.current)
            s.inputs.appendleft((s.current, s.text))
            s.current, s.resume, s.fresh, s.proc, s.slot = "", "", True, None, ""
            if agent in self.paused and not (s.urgent and self._may_start_urgent(agent)):
                self._waiting.setdefault(agent, deque()).append(s.id)  # a paused role starts nothing: at the resume
            else:
                actions += self._spawn(s)
        else:
            error = error or f"exit {rc}"
            actions += self._end_current(s, error) + self._drop_inputs(s, "session_failed")
            actions.append(Report({"type": "session", "session": s.id, "state": "failed", "error": error}))
            del self._sessions[s.id]
        actions += self._fill(agent) + self._fill_urgent(agent)
        return actions + self._moved(before)

    def tick(self) -> list:
        """Close every process idle past the limit; a turn under way beats: the cabinet confirms work only
        by a fresh heartbeat, and a long turn reports nothing else. One not answered yet does not — its start may be
        late — nor one on its way out."""
        actions: list = []
        for s in list(self._sessions.values()):
            if s.proc == "idle" and self.now() - s.idle_since >= self.config.idle_s:
                actions += self._close(s)
            elif s.proc == "working" and s.started and not s.stopping:
                actions.append(Report({"type": "session", "session": s.id, "state": "working"}))
        return actions

    def status(self) -> dict:
        sessions = [
            {"session": s.id, "agent": s.agent, "state": s.proc or "waiting", "slot": s.slot,
             "queued_inputs": len(s.inputs), "card": s.card,
             "egress": s.proc_egress if s.proc else s.egress}  # a waiting one — the exit it will start with
            for s in self._sessions.values()
        ]
        slots = {
            agent: {"used": self._used(agent), "max": n, "waiting": len(self._waiting.get(agent, ()))}
            for agent, n in self.config.agents.items()
        }
        jobs = [{"job": j.id, "agent": j.agent, "state": "running" if j.slot else "queued", "slot": j.slot}
                for j in self._jobs.values()]
        worker_slots = {}
        for agent in list(self.config.agents) + [j.agent for j in self._jobs.values()]:
            refs = self.job_slots(agent)
            if refs and agent not in worker_slots:
                worker_slots[agent] = {"used": sum(1 for j in self._jobs.values() if j.agent == agent and j.slot),
                                       "max": len(refs), "waiting": len(self._job_waiting.get(agent, ()))}
        return {"sessions": sessions, "slots": slots, "jobs": jobs, "worker_slots": worker_slots,
                "paused": dict(self.paused)}

    # --- the factory's pause ----------------------------------------------------------
    def pause(self, agent: str, operation: str) -> list:
        """Hold ``agent`` for ``operation``: nothing of it starts, its idle processes close now."""
        self.paused[agent] = operation
        actions: list = []
        for s in self._sessions.values():
            if s.agent == agent and s.proc == "idle":
                actions += self._close(s)
        return actions

    def resume(self, agent: str) -> list:
        """Lift the pause: the sessions and jobs that waited start as slots allow."""
        if self.paused.pop(agent, None) is None:
            return []
        before = self._positions()
        return self._fill(agent) + self._fill_urgent(agent) + self._fill_jobs(agent) + self._moved(before)

    def drained(self, agent: str) -> bool:
        """No process and no running job of ``agent`` is left."""
        return not any(s.agent == agent and s.proc is not None for s in self._sessions.values()) and not any(
            j.agent == agent and j.slot for j in self._jobs.values())

    # --- worker jobs ----------------------------------------------------------------------------
    def job(self, req: Mapping) -> Tuple[dict, list]:
        """A job of the cabinet; idempotent by its id (one the host still runs or ran answers so)."""
        job_id, agent = req["job"], req["agent"]
        known = self._jobs.get(job_id)
        if known is not None:
            if known.slot:
                return {"state": "running"}, []
            return {"state": "queued", "position": list(self._job_waiting[agent]).index(job_id) + 1}, []
        if job_id in self._jobs_ended:
            return {"state": "ended"}, []
        if not self.job_slots(agent):
            raise ProtocolError("refused", f"{agent} has no enabled worker slots on this host")
        job = self._jobs[job_id] = _Job(job_id, agent, dict(req["definition"]))
        slot = "" if agent in self.paused else self._free_job_slot(agent)
        if slot:
            return {"state": "starting"}, self._start_job(job, slot)
        waiting = self._job_waiting.setdefault(agent, deque())
        waiting.append(job_id)
        return {"state": "queued", "position": len(waiting)}, []

    def job_cancel(self, job_id: str) -> Tuple[dict, list]:
        """A waiting job is taken back (``queued``: it never ran); a running one is stopped (``running``: its exit
        reports it cancelled); ``ended``: its end is reported; ``unknown``: the host never had it."""
        job = self._jobs.get(job_id)
        if job is None:
            return {"cancelled": "ended" if job_id in self._jobs_ended else "unknown"}, []
        if not job.slot:
            self._job_waiting[job.agent].remove(job_id)
            del self._jobs[job_id]
            return {"cancelled": "queued"}, []
        job.stopping = True
        return {"cancelled": "running"}, [KillJob(job_id)]

    def job_line(self, job_id: str, end: dict) -> None:
        """The runner's end line (``worker.end_line``); the first one counts."""
        job = self._jobs.get(job_id)
        if job is not None and job.slot and job.end is None:
            job.end = dict(end)

    def job_exited(self, job_id: str, rc: int, error: str = "") -> list:
        job = self._jobs.get(job_id)
        if job is None or not job.slot:
            return []
        del self._jobs[job_id]
        item = {"type": "job", "job": job_id}
        if job.end is not None:
            item.update(job.end)
        elif job.stopping:
            item.update(state="cancelled", reason="stopped", comment="", usage=None, result=None)
        elif self._down:
            item.update(state="failed", reason="host_restart", comment="", usage=None, result=None)
        else:
            reason = error or f"exit {rc}"
            comment = NO_START if error == "spawn_failed" else NO_END.format(rc=error or rc)
            item.update(state="failed", reason=reason, comment=comment, usage=None, result=None)
        self._jobs_ended[job_id] = item["state"]
        while len(self._jobs_ended) > SEEN_MAX:
            self._jobs_ended.popitem(last=False)
        return [Report(item)] + self._fill_jobs(job.agent)

    # --- helpers --------------------------------------------------------------------------------
    def _used(self, agent: str) -> int:
        return sum(1 for s in self._sessions.values() if s.agent == agent and s.proc is not None)

    def _slot_name(self, agent: str) -> str:
        taken = {s.slot for s in self._sessions.values() if s.agent == agent and s.proc is not None}
        n = 1
        while f"{agent}/{n}" in taken:
            n += 1
        return f"{agent}/{n}"

    def _spawn(self, s: _Session) -> list:
        self._runs += 1
        s.slot, s.proc = self._slot_name(s.agent), "working"
        unit = f"digipaddock-session-{s.id[:12]}-{self._runs}"
        # the process runs on the latest delivery's pair; one that resumes a history another model wrote
        # (the previous process's, or the subscription's before this host) may find it refused once
        slug = _slug(s.model)
        s.proc_model, s.proc_effort, s.proc_egress = s.model, s.effort, s.egress
        s.proc_context_limit = s.context_limit
        s.switched, s.last_slug = bool(s.resume) and slug != s.last_slug, slug
        actions = [
            Report({"type": "session", "session": s.id, "state": "starting", "slot": s.slot,
                    "model": _model_name(s.model), "effort": s.effort}),
            Spawn(s.id, s.agent, self.users[s.agent], unit, s.resume, s.thread, s.card, s.space, s.memory,
                  s.model, s.effort, s.egress, s.context_limit),
        ]
        return actions + self._next_input(s, report_working=False)

    def _yields(self, s: _Session) -> bool:
        """a session whose turn ended with inputs left gives its slot up when another session of the agent
        is ready for one and none is free (one turn each, round robin); an urgent input never waits. Only a close
        that gives a waiting session a slot counts: processes already closing free theirs for the first ones, and
        one over the limit (an urgent start) frees none."""
        if self._has_urgent(s):
            return False
        slots = self.config.slots(s.agent)
        closing = sum(1 for o in self._sessions.values() if o.agent == s.agent and o.proc == "closing")
        staying = self._used(s.agent) - closing  # this one among them
        ready = sum(1 for w in self._waiting.get(s.agent, ()) if w != s.id and self._ready(w))
        return staying - 1 < slots and ready > max(0, slots - staying)

    def _positions(self) -> Dict[str, int]:
        return {session: n for queue in self._waiting.values() for n, session in enumerate(queue, 1)}

    def _moved(self, before: Mapping[str, int]) -> list:
        """a waiting session whose place in its agent's queue changed reports it — the cabinet shows the
        place, never a time it would mean."""
        return [Report({"type": "session", "session": session, "state": "queued", "position": n})
                for session, n in self._positions().items() if before.get(session) != n and session in self._sessions]

    def _ready(self, session_id: str) -> bool:
        other = self._sessions.get(session_id)
        return other is not None and other.proc is None and bool(other.inputs)

    def _has_urgent(self, s: _Session) -> bool:
        return any(command in self._urgent for command, _ in s.inputs)

    def _may_start_urgent(self, agent: str) -> bool:
        return not self._down and (agent not in self.paused or self.urgent_in_pause(agent))

    def _fill_urgent(self, agent: str) -> list:
        """The waiting sessions of ``agent`` with an urgent input start now, over its slots."""
        actions: list = []
        waiting = self._waiting.get(agent)
        if not waiting or not self._may_start_urgent(agent):
            return actions
        for session_id in list(waiting):
            s = self._sessions.get(session_id)
            if s is not None and s.proc is None and self._has_urgent(s):
                waiting.remove(session_id)
                actions += self._spawn(s)
        return actions

    def _next_input(self, s: _Session, report_working: bool = True) -> list:
        """The session's next input: an urgent one first (it is why the session runs now), else the oldest."""
        index = next((i for i, (command, _) in enumerate(s.inputs) if command in self._urgent), 0)
        command, text = s.inputs[index]
        del s.inputs[index]
        s.urgent = command in self._urgent
        self._urgent.discard(command)
        s.proc, s.current, s.started, s.text = "working", command, False, text
        actions = [Report({"type": "session", "session": s.id, "state": "working"})] if report_working else []
        return actions + [Write(s.id, text, command)]

    def _close(self, s: _Session) -> list:
        s.proc = "closing"
        return [Close(s.id)]

    def _preempt(self, agent: str) -> list:
        for s in self._sessions.values():
            if s.agent == agent and s.proc == "idle":
                return self._close(s)
        return []

    def _fill(self, agent: str) -> list:
        actions: list = []
        waiting = self._waiting.get(agent)
        while not self._down and agent not in self.paused and waiting and self._used(agent) < self.config.slots(agent):
            s = self._sessions.get(waiting.popleft())
            if s is not None and s.inputs:
                actions += self._spawn(s)
        if not self._down and agent not in self.paused and waiting and any(
                not self._has_urgent(self._sessions[w]) for w in waiting if w in self._sessions):
            # an urgent process over the limit may still hold the slot idle: free one more for an ordinary
            # session, its exit fills (an urgent one starts over the slots without it)
            actions += self._preempt(agent)
        return actions

    def _free_job_slot(self, agent: str) -> str:
        taken = {j.slot for j in self._jobs.values() if j.slot}
        return next((ref for ref in self.job_slots(agent) if ref not in taken), "")

    def _start_job(self, job: _Job, slot: str) -> list:
        job.slot = slot
        unit = f"digipaddock-session-job-{job.id[:12]}"
        return [
            Report({"type": "job", "job": job.id, "state": "starting", "slot": slot}),
            SpawnJob(job.id, job.agent, slot, unit, job.definition, int(job.definition["timeout"])),
        ]

    def _fill_jobs(self, agent: str) -> list:
        actions: list = []
        waiting = self._job_waiting.get(agent)
        while not self._down and agent not in self.paused and waiting:
            slot = self._free_job_slot(agent)
            if not slot:
                break
            job = self._jobs.get(waiting.popleft())
            if job is not None:
                actions += self._start_job(job, slot)
        return actions

    def _end_current(self, s: _Session, error: str) -> list:
        if not s.current:
            return []
        item = {"type": "turn", "session": s.id, "command": s.current, "phase": "ended", "ok": False, "error": error}
        s.current = ""
        return [Report(item)]

    def _drop_inputs(self, s: _Session, error: str) -> List[Report]:
        out = [
            Report({"type": "turn", "session": s.id, "command": command, "phase": "ended", "ok": False,
                    "error": error})
            for command, _ in s.inputs
        ]
        for command, _ in s.inputs:
            self._urgent.discard(command)
        s.inputs.clear()
        return out
