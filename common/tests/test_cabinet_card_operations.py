"""Schema 16 — a card's "Operations" are part of its brief's scope, typed and checked
against the factory catalog; a brief without them keeps its old hash."""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from kd.cabinet import api, briefs, commands, schema, store

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cabinet_env import CabinetEnv  # noqa: E402

NOW = "2026-10-21T10:00:00Z"
STEP = {"verb": "agent.reinstall", "target": "factory", "agents": ["dev"], "what": "Переустановить dev",
        "effect": "Новые инструкции dev", "risk": {"level": "low", "why": "Очередь сохраняется"},
        "rollback": "Предыдущая выкатка и переустановка"}


@pytest.fixture
def db(tmp_path):
    s = store.Store.open(str(tmp_path / "cabinet.db"), prefix="KD", clock=lambda: NOW)
    commands.install(s, lambda agent: agent == "analyst")
    return s


def _draft(db):
    return db.create_card({"title": "Роли", "project": "digipaddock", "status": "Inbox", "assignee": "@analyst",
                           "labels": ["spec:task-brief"], "created_by": "@owner"}, actor="user:owner")


def _edit(db, card, changes, actor="agent:analyst"):
    return db.edit_card(card.id, db.card(card.id).revision, changes, actor=actor)


def test_migration_16_from_15(tmp_path):
    path = str(tmp_path / "v15.db")
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(schema.DDL + "".join(schema.MIGRATIONS[v] for v in range(2, 16)))
    conn.execute("INSERT INTO meta VALUES ('schema_version', '15')")
    conn.execute("INSERT INTO meta VALUES ('next_card', '2')")
    conn.execute("INSERT INTO cards (id, n, project, title, status, created_by, revision, created, updated)"
                 " VALUES ('KD-1', 1, 'digipaddock', 'Old', 'Done', '@owner', 3, 't', 't')")
    conn.execute("INSERT INTO threads (kind, card) VALUES ('card', 'KD-1')")
    conn.close()
    db = store.Store.open(path)
    assert db.schema_version() == schema.SCHEMA_VERSION
    assert db.card("KD-1").operations == ()
    columns = {r[1] for r in db._conn.execute("PRAGMA table_info(operations)")}
    assert {"id", "key", "kind", "card", "requested_by", "agents", "revision", "grant_id", "state", "phase",
            "result", "error", "reconcile", "superseded_by", "payload_sha", "created", "updated"} <= columns
    assert "operation" in schema.WAIT_KINDS
    assert schema.OPERATION_STATES == ("pending", "running", "succeeded", "failed", "superseded")


def test_brief_scope_without_operations_unchanged(db):
    card = _edit(db, _draft(db), {"description": "Цель.", "ac_add": ["Готово"]})
    assert briefs.scope(card) == {"description": "Цель.", "acceptance": ["Готово"]}  # the old hash stays


def test_brief_version_keeps_operations(db):
    card = _edit(db, _draft(db), {"description": "Цель.", "operations": [STEP]})
    assert card.operations == (STEP,)
    assert briefs.scope(card)["operations"] == [STEP]
    _edit(db, card, {"labels_add": ["brief-ready"]})
    [v1] = briefs.versions(db, card.id)
    assert v1["operations"] == [STEP]
    assert v1["scope_sha"] == briefs.scope_sha(briefs.scope(db.card(card.id)))
    assert "agent.reinstall" in briefs.render(briefs.scope(db.card(card.id)))


def test_operations_edit_marks_brief_changed(db):
    card = _edit(db, _draft(db), {"description": "Цель.", "labels_add": ["brief-ready"]})
    assert len(briefs.versions(db, card.id)) == 1
    after = _edit(db, card, {"operations": [STEP]}, actor="user:owner")
    assert "brief-ready" not in after.labels  # a changed scope takes the label off in the same write


@pytest.fixture
def env(tmp_path):
    return CabinetEnv(tmp_path)


def _inbox_card(env, assignee="@analyst"):
    return env.store.create_card({"title": "Роли", "project": "digipaddock", "status": "Inbox", "assignee": assignee,
                                  "labels": [], "created_by": "@owner"}, actor="user:owner")


def test_task_edit_operations_validates(env):
    card = _inbox_card(env)
    edit = {"op": "worker.edit", "agent": "analyst", "card": card.id, "revision": card.revision}
    for bad, why in (([{**STEP, "agents": ["ghost"]}], "generated agent"),
                     ([{**STEP, "verb": "shell"}], "unknown factory kind"),
                     ([{**STEP, "command": "rm -rf /"}], "unexpected"),
                     ("agent.reinstall", "list")):
        with pytest.raises(api.ApiError) as exc:
            env.call("digi", {**edit, "operations": bad})
        assert why in str(exc.value)
    with pytest.raises(api.ApiError) as exc:  # the scope is written over the revision the agent read
        env.call("digi", {"op": "worker.edit", "agent": "analyst", "card": card.id, "operations": [STEP]})
    assert "revision" in str(exc.value)
    env.call("digi", {**edit, "operations": [{**STEP, "agents": ["dev", "boss"]}]})
    assert env.store.card(card.id).operations[0]["agents"] == ["boss", "dev"]


def test_task_edit_operations_only_own_draft(env):
    card = _inbox_card(env, assignee="@dev")
    with pytest.raises(api.ApiError) as exc:
        env.call("digi", {"op": "worker.edit", "agent": "analyst", "card": card.id, "revision": card.revision,
                         "operations": [STEP]})
    assert "not your card" in str(exc.value)
    moved = env.store.edit_card(card.id, card.revision, {"status": "To Do"}, actor="user:owner")
    with pytest.raises(api.ApiError) as exc:
        env.call("digi", {"op": "worker.edit", "agent": "dev", "card": card.id, "revision": moved.revision,
                         "operations": [STEP]})
    assert "draft in Inbox" in str(exc.value)


def test_kd_task_edit_operations_flag():
    import importlib.machinery
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "bin" / "kd-task"
    loader = importlib.machinery.SourceFileLoader("kd_task_ops", str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("kd_task_ops", loader))
    loader.exec_module(module)
    name, card, request = module.parse(["edit", "KD-1", "--operations", json.dumps([STEP]), "--revision", "4"])
    assert (name, card, request) == ("edit", "KD-1", {"operations": [STEP], "revision": 4})
    with pytest.raises(module.Refused):
        module.parse(["edit", "KD-1", "--operations", "{not json"])


def test_task_edit_clears_operations(env):
    """An empty list takes the brief's "Operations" off (a new draft without factory steps)."""
    card = _inbox_card(env)
    env.call("digi", {"op": "worker.edit", "agent": "analyst", "card": card.id, "revision": card.revision,
                     "operations": [STEP]})
    card = env.store.card(card.id)
    assert card.operations
    env.call("digi", {"op": "worker.edit", "agent": "analyst", "card": card.id, "revision": card.revision,
                     "operations": []})
    assert env.store.card(card.id).operations == ()


def test_the_brief_diff_shows_every_field_of_an_operation():
    """What the owner approves — kind, target, roles, effect, risk and why, rollback — is in the text
    the diff compares, so a change of any of them shows."""
    text = briefs.render({"description": "Цель.", "acceptance": [], "operations": [STEP]})
    for part in ("agent.reinstall", "factory", "dev", "Переустановить dev", "Новые инструкции dev", "low",
                 "Очередь сохраняется", "Предыдущая выкатка и переустановка"):
        assert part in text, part
    changed = {**STEP, "rollback": "Нет"}
    lines = briefs.diff({"description": "Цель.", "acceptance": [], "operations": [STEP]},
                        {"description": "Цель.", "acceptance": [], "operations": [changed]})
    assert any(line["op"] == "+" and "Нет" in line["text"] for line in lines)
