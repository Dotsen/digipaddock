"""The stages probe on a copy of the cabinet base.

``run(copy)`` opens the copy (``Store.open`` migrates it), adds a parent card ``probe-stages …`` with an approved
brief of two stages (``a``, ``b`` after ``a``) and checks the materialization against failures:

- B01 — a failure between the two child creations rolls the whole write back: no child, no ``stages`` row;
- B02 — the repeated approval creates exactly one child per key (``a`` To Do, ``b`` Blocked on ``a``);
- B03 — a third approval of the same version creates nothing;
- B04 — a half-made plan (a row with ``a``'s child only, as an older version would leave it) is completed: ``b``
  created once, ``a`` reused.

Every line ends with "ok" and the exit code is 0 — B passed; a "FAIL" line says why. The copy is written to; the
live base is refused.
"""

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from kd import naming, pipeline_config, session_config
from kd.cabinet import api, briefs, stages, store, users

LIVE = "/var/lib/digipaddock/cabinet/cabinet.db"
STAMP = "%Y-%m-%dT%H:%M:%SZ"
PERSON = "probe-owner"
AGENTS = ("boss", "dev", "analyst", "sysadmin")
HOSTED = session_config.HostConfig({"boss": 1, "dev": 2, "analyst": 1, "sysadmin": 1}, 600)
STAGES = [
    {"key": "a", "title": "probe-stage a", "role": "@dev", "spec": "implementer", "depends": [],
     "description": "Этап a.", "acceptance": ["a сделано"]},
    {"key": "b", "title": "probe-stage b", "role": "@dev", "spec": "implementer", "depends": ["a"],
     "description": "Этап b.", "acceptance": ["b сделано"]},
]


class Failed(Exception):
    pass


class Boom(Exception):
    pass


class Probe:
    def __init__(self, path: str):
        if os.path.realpath(path) == os.path.realpath(LIVE):
            raise Failed("the live base is refused: probe a copy")
        self.path = path
        self.now = (datetime.now(timezone.utc) + timedelta(minutes=2)).strftime(STAMP)
        self.store = store.Store.open(self.path, clock=lambda: self.now)
        known = self.store.projects()
        self.project = next((p for p, route in known.items() if route == "@dev"), "") or next(iter(known), "probe")
        if not known:
            self.store.seed_projects([{"id": self.project, "name": self.project, "route": "@dev"}])
        self.store.update_project(self.project, actor="root", done_by="dev")  # dev closes its Done
        tmp = tempfile.mkdtemp(prefix="kd-stages-probe.")
        self.registry = os.path.join(tmp, "agents.json")
        with open(self.registry, "w", encoding="utf-8") as handle:
            json.dump({a: {"user": f"{naming.base_user()}-{a}"} for a in AGENTS}, handle)
        self.ctx = api.Context(self.store, self.registry, None, require_root_owner=False,
                               now=lambda: self.now, users=users.Users(self.store))
        self.ctx.pipeline = lambda: pipeline_config.Config(mode="enforce")
        api.install_host(self.ctx, lambda: HOSTED)
        if self.ctx.users.get(PERSON) is None:
            self.ctx.users.create(PERSON, "Проба", roles=[("owner", "*")], actor="root")

    def check(self, condition, why: str) -> None:
        if not condition:
            raise Failed(why)

    def counts(self):
        conn = self.store._conn
        return (conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM stages").fetchone()[0])

    def parent(self):
        record = self.store.create_card({"title": "probe-stages parent", "project": self.project,
                                         "status": "Inbox", "assignee": "@dev", "created_by": "@owner"},
                                        actor=f"user:{PERSON}")
        record = self.store.edit_card(record.id, None, {
            "description": "Цель пробы.", "acceptance": [{"text": "Всё вместе", "checked": False}],
            "stages": [dict(s) for s in STAGES]}, actor="agent:analyst")
        briefs.add_version(self.store, record, origin="agent", author="agent:analyst")
        briefs.approve(self.store, record, by=f"user:{PERSON}")
        return self.store.card(record.id), briefs.latest(self.store, record.id)

    def materialize(self, record, version, *, fail_after: Optional[int] = None):
        original, calls = self.store.create_card, [0]

        def wrapped(values, *, actor):
            calls[0] += 1
            if fail_after is not None and calls[0] > fail_after:
                raise Boom("the cabinet died between two child creations")
            return original(values, actor=actor)

        self.store.create_card = wrapped  # type: ignore[method-assign]
        try:
            with self.store.transaction():
                return stages.materialize(self.ctx, record, version, by=f"user:{PERSON}")
        finally:
            self.store.create_card = original  # type: ignore[method-assign]

    def run(self, out) -> None:
        record, version = self.parent()
        self.check(version is not None and version["approved_by"] and len(version["stages"]) == 2,
                   "the approved version has two stages")
        before = self.counts()
        try:
            self.materialize(record, version, fail_after=1)
            raise Failed("B01: the failure did not propagate")
        except Boom:
            pass
        self.check(self.counts() == before, f"B01: the failed write left rows: {self.counts()} != {before}")
        self.check(stages.rows(self.store, record.id) == [], "B01: no stage rows after the rollback")
        out.write("B01 a failure between the two children rolls the write back ok\n")

        rows = self.materialize(record, version)
        kids = {r["key"]: r["child"] for r in rows if r["child"]}
        self.check(set(kids) == {"a", "b"}, f"B02: children {kids}")
        a, b = self.store.card(kids["a"]), self.store.card(kids["b"])
        self.check(a.status == "To Do" and a.parent == record.id and a.assignee == "@dev", f"B02: a {a.status}")
        self.check(b.status == "Blocked" and b.dependencies == (a.id,), f"B02: b {b.status} {b.dependencies}")
        self.check(self.counts() == (before[0] + 2, before[1] + 2), f"B02: counts {self.counts()}")
        out.write("B02 the repeated approval creates one child per key ok\n")

        again = self.materialize(record, version)
        self.check({r["key"]: r["child"] for r in again} == kids, "B03: a third approval changed the children")
        self.check(self.counts() == (before[0] + 2, before[1] + 2), f"B03: counts {self.counts()}")
        out.write("B03 a third approval creates nothing ok\n")

        # a half-made plan: b's row and card gone (as if the write of b never happened), a's kept
        with self.store.transaction():
            self.store._conn.execute("DELETE FROM stages WHERE card=? AND key='b'", (record.id,))
            self.store._conn.execute("DELETE FROM cards WHERE id=?", (b.id,))
        half = self.counts()
        done = self.materialize(record, version)
        made = {r["key"]: r["child"] for r in done if r["child"]}
        self.check(made["a"] == a.id and made["b"] and made["b"] != b.id, f"B04: {made}")
        self.check(self.counts() == (half[0] + 1, half[1] + 1), f"B04: counts {self.counts()} from {half}")
        self.check(self.store.card(made["b"]).dependencies == (a.id,), "B04: b depends on the reused a")
        out.write("B04 a half-made plan is completed, a reused, b created once ok\n")

    def close(self) -> None:
        self.store.close()


def run(copy: str, out=sys.stdout) -> int:
    probe = None
    try:
        probe = Probe(copy)
        probe.run(out)
    except Failed as exc:
        out.write(f"FAIL {exc}\n")
        return 1
    finally:
        if probe is not None:
            probe.close()
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="stages_probe", description="The stages probe B01–B04 on a base copy")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run the scenarios on a copy of the cabinet base").add_argument("copy")
    args = parser.parse_args(argv)
    return run(args.copy)
