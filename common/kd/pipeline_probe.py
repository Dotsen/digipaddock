"""The pipeline probe: turn outcomes, waits and continuations on a copy of a cabinet base.

``run(copy)`` opens the copy (``Store.open`` migrates it), adds cards ``probe-B0n …`` to its project for @dev, a
person ``probe`` with the owner's role, and drives them through the board API as the session host (``host.report``),
the executor (``turn.finish``) and the cabinet (answers, the sweep) do — in enforce, on its own clock. It prints one
line per scenario, ``B0n <title> ... ok`` or ``... FAIL <why>``; the exit code is 0 only when every line is ok. Nothing
here decides: every rule is the cabinet's own. It writes to the copy only and refuses the live base.
"""

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from kd import naming, pipeline_config, session_config
from kd.cabinet import api, commands, continuations, decisions, problems, store, users

LIVE_BASE = "/var/lib/digipaddock/cabinet/cabinet.db"
STAMP = "%Y-%m-%dT%H:%M:%SZ"
PERSON = "probe"
AGENTS = ("boss", "dev", "analyst", "sysadmin")
HOSTED = session_config.HostConfig({"boss": 1, "dev": 2, "analyst": 1, "sysadmin": 1})
ROOT = api.Principal("root", 0, "root")
EXECUTOR = api.Principal("executor", 1000, "executor")
CABINET = api.Principal("cabinet", 1001, "cabinet")


class Failed(Exception):
    pass


class Probe:
    """The copy, its context and the moves of the host, the executor and the owner on it."""

    def __init__(self, path: str, tmp: str):
        self.path, self.cards = path, set()
        self.now = (datetime.now(timezone.utc) + timedelta(minutes=2)).strftime(STAMP)
        self._open()
        known = self.store.projects()
        self.project = next((p for p, route in known.items() if route == "@dev"), "") or next(iter(known), "probe")
        if not known:  # the projects table is the board's only source
            self.store.seed_projects([{"id": self.project, "name": self.project, "route": "@dev"}])
        self.store.update_project(self.project, actor="root", done_by="dev")  # dev closes its Done
        registry = os.path.join(tmp, "agents.json")
        with open(registry, "w", encoding="utf-8") as handle:
            json.dump({a: {"user": f"{naming.base_user()}-{a}"} for a in AGENTS}, handle)
        self.registry = registry
        self._context()
        if self.ctx.users.get(PERSON) is None:
            self.ctx.users.create(PERSON, "Проба", roles=[("owner", "*")], actor="root")

    # --- the base ---------------------------------------------------------------------------------------------
    def clock(self) -> str:
        return self.now

    def advance(self, minutes: int) -> None:
        when = datetime.strptime(self.now, STAMP).replace(tzinfo=timezone.utc) + timedelta(minutes=minutes)
        self.now = when.strftime(STAMP)

    def _open(self) -> None:
        self.store = store.Store.open(self.path, clock=self.clock)

    def _context(self) -> None:
        self.ctx = api.Context(self.store, self.registry, None, require_root_owner=False,
                               now=self.clock, users=users.Users(self.store))
        self.ctx.pipeline = lambda: pipeline_config.Config(mode="enforce")
        api.install_host(self.ctx, lambda: HOSTED)

    def reopen(self) -> None:
        """As a restart of the cabinet: the base closed and opened again, the context built anew."""
        self.store.close()
        self._open()
        self._context()

    def close(self) -> None:
        self.store.close()

    def check(self, condition, why: str) -> None:
        if not condition:
            raise Failed(why)

    # --- cards, turns, answers --------------------------------------------------------------------------------
    def card(self, title: str, *, assignee="@dev", status="In Progress", **extra):
        record = self.store.create_card({"title": f"probe-{title}", "project": self.project, "status": status,
                                         "assignee": assignee, "created_by": "@owner", **extra},
                                        actor=f"user:{PERSON}")
        self.cards.add(record.id)
        return record

    def edit(self, record, changes: dict, actor=f"user:{PERSON}"):
        return self.store.edit_card(record.id, None, changes, actor=actor)

    def begin(self, record, *, agent="dev", kind="assigned", thread=None, command=None):
        """The host starts a turn of ``agent``: on ``command``, or on its oldest queued command in the thread (the
        card's wake), or on a new one."""
        thread = thread or record.thread
        if command is None:
            waiting = self.store.commands(thread=thread, agent=agent, states=["queued"])
            command = waiting[-1]["id"] if waiting else self.store.enqueue(
                {"key": f"probe:{thread}:{agent}:{self.store.last_seq()}", "agent": agent, "thread": thread,
                 "card": record.id if thread == record.thread else "", "kind": kind,
                 "text": f"[board] {record.id}: probe turn"})["id"]
        session = self.store.session_for(thread, agent)["id"]
        self.store.command_state(command, "delivered", session=session)
        self.report({"type": "turn", "session": session, "command": command, "phase": "started"})
        return command, session

    def end(self, command: str, session: str, ok=True) -> None:
        item = {"type": "turn", "session": session, "command": command, "phase": "ended", "ok": ok}
        self.report(item if ok else {**item, "error": "probe"})

    def report(self, *items) -> dict:
        return api.dispatch(self.ctx, ROOT, {"op": "host.report", "items": list(items)})

    def finish(self, record, outcome="continue", note="probe: the next step", agent="dev", **extra) -> dict:
        return api.dispatch(self.ctx, EXECUTOR, {"op": "turn.finish", "agent": agent, "card": record.id,
                                                 "outcome": outcome, "note": note, **extra})

    def refusal(self, record, **kw) -> str:
        try:
            self.finish(record, **kw)
        except api.ApiError as exc:
            return str(exc)
        raise Failed(f"finish {kw.get('outcome', 'continue')} on {record.id} was not refused")

    def question(self, record, text="?", agent="dev") -> str:
        return decisions.Decisions(self.store).open("question", card=record.id, payload={"text": text},
                                                    requested_by=f"agent:{agent}", now=self.now)["id"]

    def answer(self, decision_id: str, text="Да") -> None:
        api.dispatch(self.ctx, CABINET, {"op": "decision.reply", "id": decision_id, "user": PERSON, "text": text})

    def reject(self, decision_id: str, text="Нет") -> None:
        api.dispatch(self.ctx, CABINET, {"op": "decision.reject", "id": decision_id, "user": PERSON, "text": text})

    def sweep(self) -> dict:
        return api.dispatch(self.ctx, CABINET, {"op": "pipeline.sweep"})

    # --- what the base holds ----------------------------------------------------------------------------------
    def queued(self, record, agent=None, kind=None) -> list:
        return [c for c in self.store.commands(card=record.id, states=["queued"])
                if (agent is None or c["agent"] == agent) and (kind is None or c["kind"] == kind)]

    def outcome(self, command: str) -> dict:
        row = self.store._conn.execute("SELECT * FROM turn_outcomes WHERE command=?", (command,)).fetchone()
        return dict(row) if row is not None else {}

    def wait_states(self, command: str) -> list:
        return sorted(r[0] for r in self.store._conn.execute("SELECT state FROM waits WHERE command=?", (command,)))

    def continuations(self, record) -> list:
        return [dict(r) for r in self.store._conn.execute(
            "SELECT * FROM continuations WHERE card=? ORDER BY created, rowid", (record.id,))]

    def problem_kinds(self, record) -> list:
        return [p["kind"] for p in problems.open_for(self.store, record.id)]

    def stale(self, command_id: str) -> Optional[str]:
        """Why the dispatcher would cancel the queued command now, as it checks it before delivery."""
        when = datetime.strptime(self.now, STAMP).replace(tzinfo=timezone.utc).timestamp()
        return commands.stale_reason(self.store, self.store.command(command_id), HOSTED.hosted,
                                     commands.grant_check(self.ctx, when))

    def continuing(self, title: str):
        """A card whose turn recorded ``continue``: the card and its queued continuation."""
        record = self.card(title)
        command, session = self.begin(record)
        self.finish(record)
        self.end(command, session)
        queued = self.queued(record, kind="continue")
        self.check(len(queued) == 1, f"{record.id}: {len(queued)} continuations queued after continue")
        return self.store.card(record.id), queued[0]["id"]


# --- the scenarios ---------------------------------------------------------------------------------------------
def b01(p: Probe) -> None:
    record = p.card("B01 continue")
    command, session = p.begin(record)
    p.finish(record)
    p.finish(record)  # the same finish again in the same turn
    p.sweep()
    p.end(command, session)
    p.end(command, session)  # the host reports the end again
    p.sweep()
    p.reopen()
    p.end(command, session)
    p.sweep()
    keys = [c["key"] for c in p.queued(record, kind="continue")]
    p.check(keys == [f"cont:{command}"], f"continuations queued: {keys}")
    p.check(p.outcome(command).get("state") == "final", f"the outcome is {p.outcome(command).get('state')}")


def b02(p: Probe) -> None:
    before = {r[0] for r in p.store._conn.execute("SELECT id FROM problems WHERE state='open'")}
    p.sweep()  # the copy's own cards: their turns ended before the pipeline began to count
    raised = [r for r in p.store._conn.execute("SELECT id, card FROM problems WHERE state='open'")
              if r[0] not in before and r[1] not in p.cards]
    p.check(not raised, f"the sweep raised {len(raised)} problems on the copy's cards: "
                        f"{', '.join(sorted({r[1] for r in raised})[:10])}")
    record = p.card("B02 no outcome")
    command, session = p.begin(record)
    p.end(command, session)
    found = problems.open_for(p.store, record.id)
    p.check([x["kind"] for x in found] == ["missing_next_step"], f"problems after one report: {p.problem_kinds(record)}")
    p.end(command, session)
    p.sweep()
    p.check(len(problems.open_for(p.store, record.id)) == 1, "the repeated report or the sweep raised it again")
    boss = [c["key"] for c in p.queued(record, agent="boss")]
    p.check(boss == [f"problem:{found[0]['id']}"], f"BOSS commands: {boss}")


def b03(p: Probe) -> None:
    record = p.card("B03 two questions")
    command, session = p.begin(record)
    first, second = p.question(record, "q1"), p.question(record, "q2")
    p.finish(record, outcome="wait_user", note="probe: waiting for the owner")
    p.end(command, session)
    p.answer(second)
    p.check(p.queued(record, agent="dev") == [], "the second answer woke @dev before the first")
    p.answer(first)
    wakes = p.queued(record, agent="dev")
    p.check(len(wakes) == 1 and wakes[0]["kind"] == "answer" and first in wakes[0]["text"],
            f"wakes after both answers: {[w['kind'] for w in wakes]}")
    states = [(c["state"], c["command"]) for c in p.continuations(record)]
    p.check(states == [("merged", wakes[0]["id"])], f"continuations: {states}")

    record = p.card("B03 answer in the turn")
    command, session = p.begin(record)
    asked = p.question(record)
    p.finish(record, outcome="wait_user", note="probe: waiting for the owner")
    p.answer(asked)
    p.end(command, session)
    p.check(p.wait_states(command) == ["resolved"], f"waits: {p.wait_states(command)}")
    p.check(len(p.queued(record, agent="dev")) == 1, "not one wake after an answer in the turn")
    states = [(c["key"], c["state"]) for c in p.continuations(record)]
    p.check(states == [(f"wait:{command}", "merged")], f"continuations: {states}")

    for state in ("rejected", "expired"):
        record = p.card(f"B03 {state}")
        command, session = p.begin(record)
        asked = p.question(record)
        p.finish(record, outcome="wait_user", note="probe: waiting for the owner")
        p.end(command, session)
        if state == "rejected":
            p.reject(asked)
        else:
            decisions.Decisions(p.store).expire_one(asked, now=p.now)
        p.check(p.wait_states(command) == [state], f"{state}: waits {p.wait_states(command)}")
        texts = [c["text"] for c in p.queued(record, agent="dev")]
        p.check(texts == [continuations.ENDED.format(id=record.id, state=state)], f"{state}: wakes {texts}")


def b04(p: Probe) -> None:
    for title, change, expected in (
        ("B04 scope", {"description": "probe: a new scope"}, commands.SCOPE_CHANGED),
        ("B04 review", {"status": "Review"}, commands.STATUS_CHANGED),
        ("B04 assignee", {"assignee": "@owner"}, commands.REASSIGNED),
        ("B04 note", {"notes_append": "probe: a note", "comment": {"author": "@dev", "text": "probe"}}, None),
    ):
        record, queued = p.continuing(title)
        p.check(p.stale(queued) is None, f"{title}: the fresh continuation is already stale")
        p.edit(record, change)
        p.check(p.stale(queued) == expected, f"{title}: {p.stale(queued)!r}, expected {expected!r}")
    record, queued = p.continuing("B04 revoke")
    ledger = decisions.Decisions(p.store)
    grant = os.urandom(16).hex()
    delegate = ledger.open("delegate", card=record.id, payload={"goal": record.id}, requested_by="agent:boss",
                           now=p.now)["id"]
    ledger.approve(delegate, user=PERSON, now=p.now, grant_id=grant)
    basis = {**p.store.command(queued)["basis"], "grant": grant}  # as for a subtask made under the grant
    p.store._conn.execute("UPDATE commands SET basis=? WHERE id=?", (json.dumps(basis), queued))
    p.check(p.stale(queued) is None, "a continuation under a held grant is stale")
    ledger.revoke(delegate, user=PERSON, now=p.now)
    p.check(p.stale(queued) == commands.GRANT_REVOKED, f"after «Отозвать»: {p.stale(queued)!r}")


def _run_queued(p: Probe, record, command: str, *, diagnostic: bool):
    """The continuation's own turn: started, a diagnostic when asked (a verified result), continue, ended."""
    command, session = p.begin(record, command=command)
    if diagnostic:
        p.store.add_thread_message(record.thread, author="@dev", kind="diagnostic", body="probe", payload={})
    p.finish(record)
    p.end(command, session)


def b05(p: Probe) -> None:
    record = p.card("B05 streak")
    command, session = p.begin(record)
    p.finish(record)
    p.end(command, session)
    for n in range(3):
        [queued] = p.queued(record, kind="continue")
        if n == 2:
            p.reopen()
        _run_queued(p, record, queued["id"], diagnostic=False)
    states = [c["state"] for c in p.continuations(record)]
    p.check(states == ["queued"] * 3 + ["limited"], f"streak: continuations {states}")
    p.check(p.problem_kinds(record) == ["continuation_limit"], f"streak: problems {p.problem_kinds(record)}")

    record = p.card("B05 window")
    command, session = p.begin(record)
    p.store.add_thread_message(record.thread, author="@dev", kind="diagnostic", body="probe", payload={})
    p.finish(record)
    p.end(command, session)
    for n in range(6):
        p.advance(1)
        [queued] = p.queued(record, kind="continue")
        if n == 5:
            p.reopen()
        _run_queued(p, record, queued["id"], diagnostic=True)
    rows = p.continuations(record)
    states = [c["state"] for c in rows]
    p.check(states == ["queued"] * 7 and rows[-1]["reason"] == "window", f"window: continuations {states}")
    p.check(p.store.command(rows[-1]["command"])["next_try"] > p.ctx.now(),
            "window: the 7th is not held to the window's end")
    p.check(continuations.limit_reason(p.ctx, p.store.card(record.id)) == "window", "window: not the window limit")
    p.advance(61)
    command, session = p.begin(record)
    p.store.add_thread_message(record.thread, author="@dev", kind="diagnostic", body="probe", payload={})
    p.finish(record)
    p.end(command, session)
    p.check(len(p.queued(record, kind="continue")) == 1, "window: no continuation an hour later")

    before = p.store._conn.execute("SELECT COUNT(*) FROM problems WHERE state='open'").fetchone()[0]
    brief = p.card("B05 brief", assignee="@analyst", status="Inbox", labels=["spec:task-brief"])
    command, session = p.begin(brief, agent="analyst")
    p.edit(brief, {"assignee": "@dev", "labels_remove": ["spec:task-brief"]}, actor="agent:analyst")
    p.end(command, session)
    chat = p.card("B05 chat")
    command, session = p.begin(chat, kind="chat", thread=p.store.agent_thread("dev"))
    p.end(command, session)
    job_card = p.card("B05 job")
    command, session = p.begin(job_card)
    p.question(job_card)
    p.finish(job_card, outcome="wait_user", note="probe: waiting for the owner")
    p.end(command, session)
    job = os.urandom(16).hex()
    p.store._conn.execute(
        "INSERT INTO jobs (id, card, agent, caller, requester, spec, writer, state, data, created, updated)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (job, job_card.id, "dev", "boss", "", "tester", 0, "delivered",
         json.dumps({"definition": {}, "result": None, "usage": None}), p.now, p.now))
    p.report({"type": "job", "job": job, "state": "running", "slot": "dev/worker1"})
    p.report({"type": "job", "job": job, "state": "done", "reason": "probe"})
    p.sweep()
    after = p.store._conn.execute("SELECT COUNT(*) FROM problems WHERE state='open'").fetchone()[0]
    p.check(after == before, f"the brief's handoff, a chat turn or a job raised {after - before} problems")


def b06(p: Probe) -> None:
    def depending(title: str):
        dep = p.card(f"{title} (dependency)", assignee="@owner", status="To Do")
        record = p.card(title, dependencies=[dep.id])
        command, session = p.begin(record)
        p.finish(record, outcome="wait_dependency", note="probe: waiting for the dependency")
        p.end(command, session)
        return dep, record, command

    dep, record, command = depending("B06 dependency")
    p.edit(dep, {"status": "Done"})
    p.check(p.wait_states(command) == ["resolved"], f"dependency Done: waits {p.wait_states(command)}")
    texts = [c["text"] for c in p.queued(record, agent="dev")]
    p.check(texts == [continuations.RESOLVED.format(id=record.id)], f"dependency Done: wakes {texts}")

    dep, record, command = depending("B06 duplicate")
    p.edit(dep, {"status": "Done", "labels_add": [f"duplicate-of:{record.id}"]})
    p.check(p.wait_states(command) == ["needs_routing"], f"duplicate: waits {p.wait_states(command)}")
    p.check(p.problem_kinds(record) == ["wait_needs_routing"], f"duplicate: problems {p.problem_kinds(record)}")
    p.check(p.queued(record, agent="dev") == [], "duplicate: @dev was woken")

    parent = p.card("B06 parent")
    part = p.card("B06 child", assignee="@owner")
    p.edit(part, {"parent": parent.id})
    p.begin(parent)
    why = p.refusal(parent, outcome="done", note="probe: all done")
    p.check(f"{parent.id} has open conditions" in why and f"children {part.id}" in why, f"Done refusal: {why}")
    p.check(p.store.card(parent.id).status == "In Progress", "the parent moved")


SCENARIOS = (
    ("B01", "continue: one continuation through repeats, sweeps and a restart", b01),
    ("B02", "a turn without an outcome: one problem, one request to BOSS; the copy's cards: none", b02),
    ("B03", "waits: answers out of order, an answer in the turn, rejected and expired", b03),
    ("B04", "a continuation against its basis: scope, Review, assignee, a revoked grant, a note", b04),
    ("B05", "limits 6/60 and 3 in a row across a restart; a brief, a chat, a job: no problems", b05),
    ("B06", "a dependency Done, a duplicate, a parent with an open child", b06),
)


def run(db_copy: str, *, out=sys.stdout) -> int:
    if os.path.realpath(db_copy) == os.path.realpath(LIVE_BASE):
        out.write(f"refused: {db_copy} is the live base; run the probe on a copy\n")
        return 2
    failed = False
    with tempfile.TemporaryDirectory() as tmp:
        probe = Probe(db_copy, tmp)
        try:
            for code, title, scenario in SCENARIOS:
                try:
                    scenario(probe)
                    verdict = "ok"
                except Failed as exc:
                    verdict, failed = f"FAIL {exc}", True
                except Exception as exc:  # noqa: BLE001 — a broken scenario is a failed line, the rest still run
                    verdict, failed = f"FAIL {type(exc).__name__}: {exc}", True
                out.write(f"{code} {title} ... {verdict}\n")
        finally:
            probe.close()
    return 1 if failed else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="pipeline_probe", description="The pipeline probe B01–B06 on a base copy")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run every scenario on a copy of the cabinet base").add_argument("copy")
    args = parser.parse_args(argv)
    return run(args.copy)
