import json
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kd import activity_journal as aj
from kd import attention_rules
from kd.cabinet import client as cabinet_client

sys.path.insert(0, str(Path(__file__).resolve().parent))  # cabinet_env
from cabinet_env import CabinetEnv  # noqa: E402 — the card lives in the cabinet

NOW = datetime(2026, 10, 8, 9, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _flock(portable_flock):
    pass


@pytest.fixture
def env(tmp_path):
    registry = tmp_path / "agents.json"
    registry.write_text(
        json.dumps({"dev": {"user": "digi-dev"}, "sysadmin": {}}),
        encoding="utf-8",
    )
    journal = tmp_path / "activity.jsonl"
    cab = CabinetEnv(tmp_path / "cabinet")
    lock = threading.Lock()  # the cabinet service serializes its calls

    class Base:  # kd-work runs as the base user: the executor principal
        def call(self, request):
            with lock:
                return cab.client("digi").call(request)

    def write_card(assignee="@dev", body="text", status="In Progress"):
        with lock:
            return cab.put("KD-5", status=status, assignee=assignee, description=body)

    def record(worker="@dev", caller="digi-dev", action="progress", now=NOW, client=None):
        return aj.record(
            str(journal), client or Base(), "KD-5", worker, action, caller, now, str(registry)
        )

    return type(
        "Env",
        (),
        {
            "journal": journal,
            "cab": cab,
            "write_card": staticmethod(write_card),
            "record": staticmethod(record),
        },
    )


def test_verified_action_is_appended_with_the_card_revision_as_evidence(env):
    rev = env.write_card()
    entry = env.record()
    assert entry == {
        "schemaVersion": 1,
        "cardId": "KD-5",
        "worker": "@dev",
        "action": "progress",
        "at": "2026-10-08T09:00:00Z",
        "cardRevision": rev,
    }
    assert aj.read(str(env.journal)) == [entry]


@pytest.mark.parametrize(
    "worker, caller, reason",
    [
        (
            "@sysadmin",
            "digi",
            "shares the agent user",
        ),  # legacy shared uid: no attribution
        ("@dev", "digi", "not the worker's user"),
        ("@dev", "digi-boss", "not the worker's user"),
        ("@unknown", "agent-unknown", "shares the agent user"),
        ("dev", "digi-dev", "unknown worker"),
        ("@boss", "digi-boss", "shares the agent user"),
    ],
)
def test_attribution_is_by_unix_user_never_by_name(env, worker, caller, reason):
    env.write_card(assignee=worker if worker.startswith("@") else "")
    with pytest.raises(aj.Rejected, match=reason):
        env.record(worker=worker, caller=caller)
    assert not env.journal.exists()


def test_worker_must_be_the_single_assignee(env):
    env.write_card(assignee="@sysadmin")
    with pytest.raises(aj.Rejected, match="single assignee"):
        env.record()


def test_keepalive_without_card_change_is_refused_and_does_not_move_the_timer(env):
    env.write_card()
    env.record()
    with pytest.raises(aj.Rejected, match="no change"):
        env.record(action="claim", now=NOW + timedelta(hours=23))
    assert [r["at"] for r in aj.read(str(env.journal))] == ["2026-10-08T09:00:00Z"]
    env.write_card(body="progress made")
    env.record(now=NOW + timedelta(hours=1))
    assert len(aj.read(str(env.journal))) == 2


class _Down:
    def call(self, request):
        raise cabinet_client.CabinetError("unavailable", "board unavailable")


def test_unknown_action_a_missing_card_and_a_dead_board_are_refused(env, tmp_path):
    with pytest.raises(aj.Rejected, match="KD-5: card not found"):
        env.record()
    env.write_card()
    with pytest.raises(aj.Rejected):
        env.record(action="keepalive")
    with pytest.raises(aj.Rejected, match="not readable"):
        env.record(client=_Down())
    assert not env.journal.exists()


def test_missing_journal_is_unknown_and_a_broken_line_distrusts_the_whole_journal(env):
    assert aj.read(str(env.journal)) is None
    env.write_card()
    env.record()
    with open(env.journal, "ab") as f:
        f.write(b'{"truncated": \n')
    records = aj.read(str(env.journal))
    cfg = attention_rules.load_config(
        json.loads(open("common/coordination/watch.json", encoding="utf-8").read())
    )
    report = attention_rules.evaluate(cfg, {}, {}, records, NOW)
    assert ("activity_invalid", "journal") in [
        (d.kind, d.subject) for d in report.diagnostics
    ]


def test_journal_is_bounded_keeping_the_newest_whole_lines(env, monkeypatch):
    monkeypatch.setattr(aj, "MAX_BYTES", 900)
    monkeypatch.setattr(aj, "KEEP_BYTES", 450)
    for i in range(8):
        env.write_card(body=f"step {i}")
        env.record(now=NOW + timedelta(minutes=i))
    data = env.journal.read_bytes()
    assert len(data) <= 900 and data.endswith(b"\n")
    records = aj.read(str(env.journal))
    assert all(isinstance(r, dict) for r in records)
    assert records[-1]["at"] == "2026-10-08T09:07:00Z"


def test_crash_during_compaction_leaves_the_old_journal_whole(env, monkeypatch):
    monkeypatch.setattr(aj, "MAX_BYTES", 900)
    monkeypatch.setattr(aj, "KEEP_BYTES", 450)
    for i in range(4):
        env.write_card(body=f"step {i}")
        env.record(now=NOW + timedelta(minutes=i))
    before = env.journal.read_bytes()

    def crash(src, dst):
        raise OSError("power cut")

    monkeypatch.setattr(aj.atomic_file.os, "replace", crash)
    for i in range(4, 8):  # one of these needs compaction
        env.write_card(body=f"step {i}")
        with pytest.raises(OSError):
            env.record(now=NOW + timedelta(minutes=i))
        assert env.journal.read_bytes() == before
    assert [p.name for p in env.journal.parent.iterdir() if p.suffix == ".tmp"] == []


def test_a_line_longer_than_the_kept_tail_is_dropped_whole(env):
    assert aj._newest(b"x" * (aj.KEEP_BYTES + 10)) == b""


@pytest.mark.skipif(aj.fcntl is None, reason="real flock: Linux (stage) only")
def test_concurrent_producers_never_interleave_lines(env):
    env.write_card()
    errors = []

    def produce(i):
        try:
            env.write_card(body=f"thread {i}")
            env.record(now=NOW + timedelta(seconds=i))
        except aj.Rejected:
            pass  # two threads saw the same card bytes: one record is a keepalive
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=produce, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert all(isinstance(r, dict) for r in aj.read(str(env.journal)))


def test_auto_action_is_claim_first_then_progress_then_review_submission(env):
    env.write_card()
    assert env.record(action="auto")["action"] == "claim"
    env.write_card(body="notes")
    assert env.record(action="auto", now=NOW + timedelta(minutes=1))["action"] == "progress"
    env.write_card(body="done", status="Review")
    entry = env.record(action="auto", now=NOW + timedelta(minutes=2))
    assert entry["action"] == "review_submission"


def test_auto_claim_belongs_to_the_worker_not_to_an_earlier_one(env):
    env.write_card(assignee="@dev")
    env.record(action="auto")
    # a record of another worker on the same card does not count as this worker's claim
    data = json.loads(env.journal.read_text("utf-8").splitlines()[0])
    data["worker"] = "@sysadmin"
    env.journal.write_text(json.dumps(data) + "\n", encoding="utf-8")
    env.write_card(body="again")
    assert env.record(action="auto", now=NOW + timedelta(minutes=1))["action"] == "claim"


def test_journal_lives_with_the_base_user_board_logs():
    assert aj.JOURNAL == "/var/log/digipaddock-board/activity.jsonl"


def test_a_journal_of_another_owner_is_untrusted(env):
    env.write_card()
    env.record()
    mine = os.stat(env.journal).st_uid
    assert aj.read(str(env.journal), owner_uid=mine) == aj.read(str(env.journal))
    assert aj.read(str(env.journal), owner_uid=mine + 1) == ["untrusted journal"]


@pytest.mark.skipif(os.name == "nt", reason="symlinks: Linux (stage) only")
def test_a_symlinked_journal_is_untrusted(env, tmp_path):
    env.write_card()
    env.record()
    link = tmp_path / "link.jsonl"
    link.symlink_to(env.journal)
    assert aj.read(str(link)) == ["unreadable journal"]
