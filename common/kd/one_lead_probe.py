"""The one-lead probe: answers with files and the factory chain on a copy
of a cabinet base.

``run(copy)`` opens the copy the way ``kd.pipeline_probe`` does (cards ``probe-L0n …`` for @dev, a person ``probe``
with the owner's role, enforce on its own clock) and drives, through the board API only:

- L01 a question that expects a file: text alone is refused, a recording never stands for a text answer, a file
  answers it and wakes the lead once;
- L02 the lead's chain: question (expect=file) → answer with a file → the lead's turn ends ``wait_dependency
  --operation`` on a root reinstall → the operation's end → exactly one continuation, a repeated report and the
  sweep add none;
- L03 C01: release → reinstall → continuation four times on one card: each resolved wait continues — waiting for
  the factory is not going round, the streak does not count it; the line prints the counts.

One line per scenario, ``ok`` or ``FAIL <why>``; the exit code is 0 only when every line is ok. Nothing is decided
here — every rule is the cabinet's. It writes to the copy only and refuses the live base.
"""

import argparse
import os
import secrets
import sys
import tempfile
from typing import Optional, Sequence

from kd.cabinet import api
from kd.pipeline_probe import CABINET, EXECUTOR, LIVE_BASE, PERSON, ROOT, Failed, Probe

REVISION = "0" * 40
FILE = {"name": "probe.png", "mime": "image/png", "size": 40, "sha256": "e" * 64}
VOICE = {"name": "voice.webm", "mime": "audio/webm", "size": 72, "sha256": "c" * 64}
OK = {"dev": {"state": "ok", "stage": "verify", "error": ""}}


class LeadProbe(Probe):
    def _context(self) -> None:
        super()._context()
        self.ctx.deployed_revision = lambda: REVISION

    def ask_file(self, record, text="Пришлите скриншот") -> str:
        return api.dispatch(self.ctx, EXECUTOR, {"op": "decision.open", "type": "question", "agent": "dev",
                                                 "card": record.id,
                                                 "payload": {"text": text, "expect": "file"}})["decision"]["id"]

    def ask_text(self, record, text="Как назвать?") -> str:
        return api.dispatch(self.ctx, EXECUTOR, {"op": "decision.open", "type": "question", "agent": "dev",
                                                 "card": record.id, "payload": {"text": text}})["decision"]["id"]

    def reply(self, decision_id: str, *, text=None, files=()) -> dict:
        files = [{**f, "id": secrets.token_hex(16)} for f in files]  # as the web stored them: each its own
        request = {"op": "decision.reply", "id": decision_id, "user": PERSON, "files": files}
        if text is not None:
            request["text"] = text
        return api.dispatch(self.ctx, CABINET, request)["decision"]

    def refused_reply(self, decision_id: str, **kw) -> str:
        try:
            self.reply(decision_id, **kw)
        except api.ApiError as exc:
            return exc.reason or exc.kind
        raise Failed(f"the reply to {decision_id[:8]} was not refused")

    def reinstall(self, record, key: str) -> str:
        return api.dispatch(self.ctx, ROOT, {"op": "operation.request", "kind": "agent.reinstall",
                                             "agents": ["dev"], "key": key, "card": record.id})["operation"]["id"]

    def ended(self, operation_id: str) -> None:
        self.report({"type": "operation", "operation": operation_id, "state": "succeeded", "phase": "installing",
                     "agents": OK, "reason": ""})

    def queued(self, record, kind: Optional[str] = None) -> list:
        return [c for c in self.store.commands(card=record.id, states=["queued"]) if kind is None or c["kind"] == kind]

    def settle(self, record) -> None:
        """The card's own wake (its creation) is the host's past: delivered and done."""
        for command in self.queued(record):
            for state in ("delivered", "started", "done"):
                self.store.command_state(command["id"], state)

    def conts(self, record) -> list:
        return [r[0] for r in self.store._conn.execute(
            "SELECT state FROM continuations WHERE card=? ORDER BY created, rowid", (record.id,))]

    def problems(self, record) -> list:
        from kd.cabinet import problems

        return [p["kind"] for p in problems.open_for(self.store, record.id)]


def l01(p: LeadProbe) -> str:
    record = p.card("L01 file question")
    p.settle(record)
    asked = p.ask_file(record)
    why = p.refused_reply(asked, text="вот")
    p.check(why == "file_needed", f"text alone answered a file question ({why})")
    spoken = p.ask_text(record)
    why = p.refused_reply(spoken, files=[VOICE])
    p.check(why == "text_needed", f"a recording answered a text question ({why})")
    done = p.reply(asked, files=[FILE])
    p.check(done["state"] == "approved" and done["answer"]["files"][0]["name"] == FILE["name"],
            f"the file did not answer it ({done['state']})")
    wakes = p.queued(record, "answer")
    p.check(len(wakes) == 1, f"{len(wakes)} answer wakes, not 1")
    return "ok"


def l02(p: LeadProbe) -> str:
    record = p.card("L02 lead chain")
    p.settle(record)
    command, session = p.begin(record)
    asked = p.ask_file(record)
    p.finish(record, outcome="wait_user", note="probe: waits for the screenshot")
    p.end(command, session)
    p.reply(asked, files=[FILE])
    woken = p.queued(record)
    p.check(len(woken) == 1, f"the answer woke {len(woken)} times, not once")
    command, session = p.begin(record, command=woken[0]["id"])
    operation = p.reinstall(record, "root:probe-L02")
    p.finish(record, outcome="wait_dependency", note="probe: waits for the reinstall", operations=[operation])
    p.end(command, session)
    p.check(p.queued(record) == [], "a continuation before the operation ended")
    p.ended(operation)
    p.ended(operation)  # the host's answer repeats its report
    p.sweep()
    after = p.queued(record, "continue")
    p.check(len(after) == 1, f"{len(after)} continuations after the reinstall, not 1")
    return "ok"


def l03(p: LeadProbe) -> str:
    record = p.card("L03 release chain")
    p.settle(record)
    turn = p.begin(record)
    for n in range(1, 5):
        operation = p.reinstall(record, f"release:probe-L03-{n}")
        p.finish(record, outcome="wait_dependency", note=f"probe: release {n}", operations=[operation])
        p.end(*turn)
        p.ended(operation)
        p.ended(operation)
        queued = p.queued(record, "continue")
        if n < 4:
            p.check(len(queued) == 1, f"release {n}: {len(queued)} continuations, not 1")
            turn = p.begin(record, command=queued[0]["id"])
    states = p.conts(record)
    p.check(states == ["queued"] * 4, f"continuations {states}")
    p.check("continuation_limit" not in p.problems(record), "a continuation_limit problem")
    return (f"ok (C01 counts: queued={states.count('queued')} merged={states.count('merged')} "
            f"limited={states.count('limited')})")


SCENARIOS = (("L01", "a question that expects a file", l01), ("L02", "the lead's chain", l02),
             ("L03", "C01 release → reinstall → continuation", l03))


def run(path: str, out=sys.stdout) -> int:
    if os.path.realpath(path) == os.path.realpath(LIVE_BASE):
        print(f"one_lead_probe: {path} is the live base — copy it first", file=sys.stderr)
        return 2
    failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        probe = LeadProbe(path, tmp)
        try:
            for name, title, scenario in SCENARIOS:
                try:
                    line = scenario(probe)
                except (Failed, api.ApiError) as exc:
                    line, failed = f"FAIL {exc}", failed + 1
                out.write(f"{name} {title} ... {line}\n")
        finally:
            probe.close()
    return 1 if failed else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="one_lead_probe.py")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run").add_argument("copy")
    args = parser.parse_args(argv)
    return run(args.copy)
