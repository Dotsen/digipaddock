"""the cabinet's scheduler — expiring decisions, the notification tick and the nightly backup."""

import asyncio
import os
import sqlite3
from contextlib import closing

from web_env import WebEnv

from kd.cabinet import scheduler


def _tick(w, backups):
    return asyncio.run(scheduler.tick(w.svc, backup_dir=str(backups)))


def test_an_expired_decision_is_closed_with_an_event_and_a_message(
    tmp_path, admin_proposal
):
    w = WebEnv(tmp_path)
    w.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    w.env.put("KD-193", project="admin", assignee="@sysadmin", status="Inbox")
    d = w.env.call(
        "digi-sysadmin",
        {
            "op": "decision.open",
            "type": "admin_plan",
            "card": "KD-193",
            "payload": admin_proposal(w.env, "KD-193"),
        },
    )["decision"]
    w.env.now = "2026-10-17T11:00:00Z"  # 73 hours later
    out = _tick(w, tmp_path / "backups")
    assert [x["id"] for x in out["expired"]] == [d["id"]]
    assert w.svc.decisions.get(d["id"])["state"] == "expired"
    kinds = [e["kind"] for e in w.env.store.events_since(0)]
    assert "decision.expired" in kinds
    said = [m for m in w.env.store.messages("KD-193") if m["kind"] == "decision"][-1]
    assert said["payload"]["state"] == "expired"
    assert "tick" in out
    assert (out["monitor"]["history"], out["monitor"]["applied"]) == (0, 0)  # the checks' history is purged on the same tick


def test_the_backup_is_made_once_a_day_after_half_past_three_and_seven_are_kept(
    tmp_path,
):
    w = WebEnv(tmp_path)
    w.env.card(title="Первая")
    backups = tmp_path / "backups"
    w.env.now = "2026-10-14T03:29:00Z"  # dev config: UTC
    assert _tick(w, backups)["backup"] is None and not backups.exists()
    w.env.now = "2026-10-14T03:31:00Z"
    made = _tick(w, backups)["backup"]
    assert os.path.basename(made) == "cabinet-20261014.db"
    with closing(sqlite3.connect(made)) as copy:
        assert copy.execute("SELECT title FROM cards").fetchone()[0] == "Первая"
    before = os.stat(made).st_mtime_ns
    w.env.now = "2026-10-14T20:00:00Z"
    assert _tick(w, backups)["backup"] is None
    assert os.stat(made).st_mtime_ns == before
    for day in range(15, 24):
        w.env.now = f"2026-10-{day}T04:00:00Z"
        _tick(w, backups)
    names = sorted(os.listdir(backups))
    assert len(names) == 7 and names[0] == "cabinet-20261017.db"
    assert names[-1] == "cabinet-20261023.db"


def test_the_sweep_runs_after_the_backup_and_never_stops_the_tick(tmp_path, monkeypatch):
    """The pipeline sweep is the tick's last step, and its failure is a journal line."""
    w = WebEnv(tmp_path)
    order = []
    original = w.svc.call

    async def call(principal, request):
        order.append(request["op"])
        if request["op"] == "pipeline.sweep":
            raise RuntimeError("boom")
        return await original(principal, request)

    monkeypatch.setattr(w.svc, "call", call)
    monkeypatch.setattr(scheduler, "backup", lambda *a, **k: order.append("backup"))
    out = _tick(w, tmp_path / "backups")
    assert order.index("backup") < order.index("pipeline.sweep") and out["pipeline"] is None
