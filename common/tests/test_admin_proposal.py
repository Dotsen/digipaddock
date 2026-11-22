"""The agent's proposals are stored apart from grants and stay untrusted data."""
import copy
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from kd import admin_operations as ops
from kd import admin_proposal, admin_task

sys.path.insert(0, str(Path(__file__).resolve().parent))  # admin_samples
from admin_samples import CARD, CATALOG, ENABLED, INVENTORY, explain, md_card, proposal  # noqa: E402

NOW = datetime(2026, 10, 9, 17, 0, tzinfo=timezone.utc)
ONE_SERVER = CARD.replace("  - 'server:node-e'\n", "")


def card(text=ONE_SERVER):
    return md_card(text)


def submit(root, data, *, text=ONE_SERVER, agent="sysadmin", raw=None):
    body = raw if raw is not None else json.dumps(data, ensure_ascii=False).encode("utf-8")
    return admin_proposal.submit(str(root), body, agent=agent, card=card(text),
                                 catalog=ops.Catalog.from_dict(copy.deepcopy(CATALOG)),
                                 inventory=INVENTORY, enabled=ENABLED, now=NOW)


def test_submit_stores_the_normalized_proposal_with_its_source(tmp_path):
    saved = submit(tmp_path, proposal())
    stored = admin_proposal.load(str(tmp_path), "KD-123")
    assert stored.task_id == "KD-123"
    assert stored.proposed_by == "sysadmin"
    assert stored.proposed_at == NOW
    again = ops.validate_proposal(stored.data, ops.Catalog.from_dict(copy.deepcopy(CATALOG)), INVENTORY, ENABLED,
                                  card_servers=("node-c",))
    assert again.sha256 == saved.sha256


def test_a_new_proposal_replaces_the_old_one(tmp_path):
    submit(tmp_path, proposal())
    second = submit(tmp_path, proposal(intent="Только перезапуск"))
    assert admin_proposal.load(str(tmp_path), "KD-123").data["intent"] == "Только перезапуск"
    assert second.sha256 != submit(tmp_path, proposal()).sha256


def test_every_plan_is_kept_under_its_hash(tmp_path):
    # a live grant reads the plan the owner signed, whatever the card's current proposal is
    first = submit(tmp_path, proposal())
    second = submit(tmp_path, proposal(intent="Только перезапуск"))
    old = admin_proposal.load_plan(str(tmp_path), "KD-123", first.sha256)
    new = admin_proposal.load_plan(str(tmp_path), "KD-123", second.sha256)
    assert old.data["intent"] != "Только перезапуск" and new.data["intent"] == "Только перезапуск"
    # a plan stored before the change is only the current one: that is the fallback, its hash is the caller's check
    os.unlink(admin_proposal.plan_path(str(tmp_path), "KD-123", second.sha256))
    assert admin_proposal.load_plan(str(tmp_path), "KD-123", second.sha256).data["intent"] == "Только перезапуск"
    for bad in ("x" * 64, "A" * 64, "a" * 63, "../" + "a" * 61):
        with pytest.raises(admin_proposal.ProposalStoreError):
            admin_proposal.load_plan(str(tmp_path), "KD-123", bad)


def test_no_proposal_is_none(tmp_path):
    assert admin_proposal.load(str(tmp_path), "KD-123") is None
    assert admin_proposal.load_plan(str(tmp_path), "KD-123", "a" * 64) is None


@pytest.mark.parametrize("data,why", [
    (proposal(task_id="KD-124"), "KD-124"),
    (proposal(operations=[explain({"server": "node-e", "verb": "package.refresh"})]), "server"),
    (proposal(operations=[explain({"server": "node-c", "verb": "shell", "command": "id"})]), "unexpected|verb"),
])
def test_invalid_proposals_are_not_stored(tmp_path, data, why):
    with pytest.raises(ValueError, match=why):
        submit(tmp_path, data)
    assert admin_proposal.load(str(tmp_path), "KD-123") is None


@pytest.mark.parametrize("raw,why", [
    (b'{"schema_version": 1, "schema_version": 1}', "duplicate"),
    (b'{"schema_version": NaN}', "JSON"),
    (b"\xff", "JSON"),
    (b"[1]", "object"),
    (b" " * (admin_proposal.MAX_BYTES + 1), "64 KiB"),
], ids=["duplicate", "nan", "utf8", "array", "large"])
def test_broken_json_is_refused(tmp_path, raw, why):
    with pytest.raises(ValueError, match=why):
        submit(tmp_path, None, raw=raw)


def test_only_the_card_assignee_may_propose(tmp_path):
    with pytest.raises(ValueError, match="assignee"):
        submit(tmp_path, proposal(), agent="dev")


@pytest.mark.parametrize("old,new,why", [
    ("project: admin", "project: digipaddock", "project"),
    ("status: To Do", "status: Done", "status"),
    ("status: To Do", "status: Review", "status"),
])
def test_card_must_be_an_open_admin_card(tmp_path, old, new, why):
    with pytest.raises(ValueError, match=why):
        submit(tmp_path, proposal(), text=ONE_SERVER.replace(old, new))


def test_inbox_card_takes_a_proposal(tmp_path):
    submit(tmp_path, proposal(), text=ONE_SERVER.replace("status: To Do", "status: Inbox"))


@pytest.mark.parametrize("mutate,why", [
    (lambda d: d.update(extra=1), "keys"),
    (lambda d: d.update(schema_version=2), "schema"),
    (lambda d: d.update(task_id="KD-124"), "task"),
    (lambda d: d.update(proposed_by="dev"), "proposed_by"),
    (lambda d: d.update(proposed_at="yesterday"), "proposed_at"),
    (lambda d: d.update(proposal=[]), "proposal"),
])
def test_tampered_store_is_refused(tmp_path, mutate, why):
    submit(tmp_path, proposal())
    path = tmp_path / "KD-123.json"
    data = json.loads(path.read_text("utf-8"))
    mutate(data)
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(admin_proposal.ProposalStoreError, match=why):
        admin_proposal.load(str(tmp_path), "KD-123")


def test_oversized_store_file_is_refused(tmp_path):
    (tmp_path / "KD-123.json").write_bytes(b" " * (admin_proposal.MAX_BYTES + 1))
    with pytest.raises(admin_proposal.ProposalStoreError, match="64 KiB"):
        admin_proposal.load(str(tmp_path), "KD-123")


def test_missing_directory_is_refused(tmp_path):
    with pytest.raises(admin_proposal.ProposalStoreError, match="missing"):
        admin_proposal.load(str(tmp_path / "nope"), "KD-123")


@pytest.mark.skipif(os.name != "posix", reason="symlinks and modes")
def test_symlinked_proposal_is_refused(tmp_path):
    (tmp_path / "elsewhere.json").write_text("{}", encoding="utf-8")
    os.symlink(tmp_path / "elsewhere.json", tmp_path / "KD-123.json")
    with pytest.raises(admin_proposal.ProposalStoreError, match="regular"):
        admin_proposal.load(str(tmp_path), "KD-123")


@pytest.mark.skipif(os.name != "posix", reason="owners and modes")
def test_foreign_owner_or_writable_directory_is_refused(tmp_path):
    submit(tmp_path, proposal())
    with pytest.raises(admin_proposal.ProposalStoreError, match="owner"):
        admin_proposal.load(str(tmp_path), "KD-123", owner_uid=os.getuid() + 1)
    os.chmod(tmp_path, 0o777)
    with pytest.raises(admin_proposal.ProposalStoreError, match="writable"):
        admin_proposal.load(str(tmp_path), "KD-123")


@pytest.mark.skipif(os.name != "posix", reason="modes")
def test_stored_file_is_not_writable_by_others(tmp_path):
    submit(tmp_path, proposal())
    assert (tmp_path / "KD-123.json").stat().st_mode & 0o777 == 0o640


def test_bad_task_id_is_refused(tmp_path):
    with pytest.raises(admin_task.CardError, match="task id"):
        admin_proposal.load(str(tmp_path), "../KD-1")


ACTION = {"schema_version": 2, "task_id": "KD-123", "intent": "Убрать пир", "basis": "wg show: пир молчит",
          "servers": ["node-c"], "command": "wg set wg0 peer 'abc=' remove", "effect": "Пир отключён",
          "risk": {"level": "high", "why": "Связь с пиром пропадёт"}, "rollback": "Вернуть пир"}


def submit_action(root, data=ACTION, deny=None, text=ONE_SERVER):
    return admin_proposal.submit_action(str(root), json.dumps(data).encode("utf-8"), agent="sysadmin",
                                        card=card(text), inventory=INVENTORY, enabled=ENABLED, now=NOW, deny=deny)


def test_action_is_stored_beside_the_plan_and_listed(tmp_path):
    a = submit_action(tmp_path)
    assert admin_proposal.action_ids(str(tmp_path), "KD-123") == [a.proposal_id]
    stored = admin_proposal.load_action(str(tmp_path), "KD-123", a.proposal_id)
    assert stored.data["command"] == ACTION["command"] and stored.proposed_by == "sysadmin"
    assert admin_proposal.load(str(tmp_path), "KD-123") is None  # the plan is a separate file


def test_denylisted_action_is_not_stored(tmp_path):
    with pytest.raises(ValueError, match="rule"):
        submit_action(tmp_path, deny=lambda command, alias: ("wg", "no"))
    assert admin_proposal.action_ids(str(tmp_path), "KD-123") == []


def test_action_for_a_closed_card_is_refused(tmp_path):
    with pytest.raises(ValueError, match="status"):
        submit_action(tmp_path, text=ONE_SERVER.replace("status: To Do", "status: Done"))


@pytest.mark.parametrize("pid", ["../KD-1", "x" * 16, "0" * 15])
def test_bad_action_ids_are_refused(tmp_path, pid):
    with pytest.raises(admin_proposal.ProposalStoreError):
        admin_proposal.load_action(str(tmp_path), "KD-123", pid)


@pytest.mark.skipif(os.name != "posix", reason="fifos")
def test_fifo_swapped_in_after_lstat_is_refused_without_blocking(tmp_path, monkeypatch):
    import stat
    import threading

    fifo = tmp_path / "KD-123.json"
    os.mkfifo(fifo, 0o640)
    real = os.stat(fifo)
    fake = os.stat_result((stat.S_IFREG | 0o640,) + tuple(real)[1:])
    monkeypatch.setattr(admin_proposal.os, "lstat", lambda path: fake if str(path) == str(fifo) else os.stat(path))
    outcome = []

    def load():
        try:
            admin_proposal.load(str(tmp_path), "KD-123")
            outcome.append("loaded")
        except admin_proposal.ProposalStoreError as exc:
            outcome.append(str(exc))

    thread = threading.Thread(target=load, daemon=True)
    thread.start()
    thread.join(3)
    assert not thread.is_alive(), "loading a FIFO blocked"
    assert outcome and "regular" in outcome[0]
