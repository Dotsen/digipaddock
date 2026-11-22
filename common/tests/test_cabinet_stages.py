"""Stages of a brief: structured parts of a large brief that the owner's one approval turns into child
cards. Here — the shape and its checks, the stages as part of the brief's versioned scope, and schema 29."""

import pytest

from kd.cabinet import briefs, commands, schema, stages, store

NOW = "2026-10-24T10:00:00Z"
STAGE_A = {"key": "a", "title": "Модель", "role": "@dev", "spec": "implementer", "description": "Схема и модуль.",
           "acceptance": ["Тест модели проходит"], "depends": [], "project": ""}
STAGE_B = {"key": "b", "title": "Веб", "role": "@dev", "spec": "implementer", "description": "Вкладка.",
           "acceptance": ["Vitest проходит"], "depends": ["a"], "project": ""}
ROLES = frozenset({"@dev", "@sysadmin"})
SPECS = {"@dev": ("implementer", "docs"), "@sysadmin": ()}
PROJECTS = frozenset({"digipaddock"})


def _parse(value, keep=()):
    return stages.parse(value, roles=ROLES, specs=SPECS, projects=PROJECTS, limit=5, keep=keep)


@pytest.fixture
def db(tmp_path):
    s = store.Store.open(str(tmp_path / "cabinet.db"), prefix="KD", clock=lambda: NOW)
    commands.install(s, lambda agent: agent in ("analyst", "dev"))
    return s


def _draft(db):
    return db.create_card({"title": "Большая задача", "project": "digipaddock", "status": "Inbox", "assignee": "@analyst",
                           "labels": ["spec:task-brief"], "created_by": "@owner"}, actor="user:owner")


def _edit(db, card, changes, actor="agent:analyst"):
    return db.edit_card(card.id, db.card(card.id).revision, changes, actor=actor)


def _handoff(db, card, stage_list):
    return _edit(db, card, {"description": "Цель.", "acceptance": [{"text": "Всё работает", "checked": False}],
                            "stages": stage_list, "assignee": "@dev", "labels_remove": ["spec:task-brief"],
                            "labels_add": ["spec:implementer", "brief-ready"]})


def test_parse_normalizes_a_good_plan():
    out = _parse([STAGE_A, {**STAGE_B, "spec": "", "project": "digipaddock"}])
    assert [s["key"] for s in out] == ["a", "b"]
    assert out[1] == {**STAGE_B, "spec": "", "project": "digipaddock"}
    assert _parse([]) == []


@pytest.mark.parametrize("bad, text", [
    ([STAGE_A, STAGE_A], "listed twice"),
    ([{**STAGE_A, "key": "A b"}], "key"),
    ([{**STAGE_A, "key": "b", "depends": ["c"]}, {**STAGE_A, "key": "c"}], "earlier stage"),
    ([{**STAGE_A, "depends": ["a"]}], "earlier stage"),
    ([{**STAGE_A, "role": "@boss"}], "role"),
    ([{**STAGE_A, "spec": "tester"}], "specialization"),
    ([{**STAGE_A, "acceptance": []}], "criterion"),
    ([{**STAGE_A, "title": ""}], "title"),
    ([{**STAGE_A, "project": "other"}], "project"),
    ([{**STAGE_A, "key": f"k{i}"} for i in range(6)], "at most 5"),
    ("not a list", "list"),
], ids=["dup", "key", "forward-dep", "self-dep", "role", "spec", "no-ac", "title", "project", "limit", "shape"])
def test_parse_refuses_a_bad_plan(bad, text):
    with pytest.raises(stages.StageError, match=text):
        _parse(bad)


def test_parse_keeps_materialized_keys():
    with pytest.raises(stages.StageError, match="a: materialized"):
        _parse([STAGE_B | {"depends": []}], keep=("a",))
    with pytest.raises(stages.StageError, match="a: materialized"):
        _parse([], keep=("a",))
    assert _parse([STAGE_A, STAGE_B], keep=("a",))[0]["key"] == "a"


def test_current_schema_retains_the_stages_table_and_columns(db):
    assert db.schema_version() == schema.SCHEMA_VERSION
    cols = {r[1] for r in db._conn.execute("PRAGMA table_info(stages)")}
    assert cols == {"card", "version", "key", "child", "state", "created", "updated", "chain"}
    assert "stages" in {r[1] for r in db._conn.execute("PRAGMA table_info(cards)")}
    assert "stages" in {r[1] for r in db._conn.execute("PRAGMA table_info(brief_versions)")}


def test_stages_are_a_card_field_and_part_of_the_versioned_scope(db):
    card = _draft(db)
    assert db.card(card.id).stages == ()
    after = _handoff(db, card, [STAGE_A, STAGE_B])
    assert [dict(s) for s in after.stages] == [STAGE_A, STAGE_B]
    assert after.to_dict()["stages"] == [STAGE_A, STAGE_B]
    v1 = briefs.latest(db, card.id)
    assert v1["stages"] == [STAGE_A, STAGE_B]
    assert v1["scope_sha"] == briefs.scope_sha(briefs.scope(after))
    assert "stages" in briefs.scope(after)
    assert "stages" not in briefs.scope(db.card(_draft(db).id))
    # a scope change of the stages makes the brief stale and drops brief-ready
    changed = _edit(db, card, {"stages": [STAGE_A, {**STAGE_B, "title": "Веб и e2e"}]})
    assert "brief-ready" not in changed.labels
    assert briefs.state(db, changed)["state"] == "stale"
    cleared = _edit(db, card, {"stages": []})
    assert cleared.stages == ()


def test_render_lists_the_stages(db):
    text = briefs.render({"description": "d", "acceptance": ["ok"], "stages": [STAGE_A, STAGE_B]})
    assert "Этапы:" in text
    assert "- a · @dev · implementer · Модель" in text
    assert "- b · @dev · implementer · Веб (после: a)" in text
    assert "Этапы:" not in briefs.render({"description": "d", "acceptance": []})


def test_create_card_accepts_parent_and_stages(db):
    parent = _draft(db)
    child = db.create_card({"title": "Этап", "project": "digipaddock", "created_by": "@owner", "parent": parent.id,
                            "stages": []}, actor="user:owner")
    assert child.parent == parent.id and child.stages == ()


# --- through the API: the analyst's write, the brief view and the start action --------------------------------
from kd import session_config  # noqa: E402
from kd.cabinet import api, flow  # noqa: E402
from cabinet_env import CabinetEnv  # noqa: E402

SPECS_BY_ROLE = {"@dev": frozenset({"implementer", "docs"}), "@analyst": frozenset({"task-brief"}),
                 "@sysadmin": frozenset(), "@boss": frozenset()}


@pytest.fixture
def cab(tmp_path):
    from kd.cabinet import users
    e = CabinetEnv(tmp_path)
    users.Users(e.store).create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    api.install_host(e.ctx, lambda: session_config.HostConfig({"analyst": 1, "dev": 2, "boss": 1}, 600))
    e.ctx._specs = SPECS_BY_ROLE
    return e


def _owner():
    return api.Principal("user", -1, "owner")


def test_the_analyst_writes_stages_over_the_revision_it_read(cab):
    card = cab.card(status="Inbox", assignee="@analyst", labels=["spec:task-brief"])
    out = cab.call("digi", {"op": "worker.edit", "agent": "analyst", "card": card.id, "revision": card.revision,
                                   "stages": [STAGE_A, STAGE_B]})
    assert out["card"]["stages"][1]["depends"] == ["a"]
    with pytest.raises(api.ApiError, match="--revision"):
        cab.call("digi", {"op": "worker.edit", "agent": "analyst", "card": card.id, "stages": [STAGE_A]})
    with pytest.raises(api.ApiError, match="stages: .*role"):
        cab.call("digi", {"op": "worker.edit", "agent": "analyst", "card": card.id, "revision": 2,
                                 "stages": [{**STAGE_A, "role": "@owner"}]})
    with pytest.raises(api.ApiError, match="stages: .*specialization"):
        cab.call("digi", {"op": "worker.edit", "agent": "analyst", "card": card.id, "revision": 2,
                                 "stages": [{**STAGE_A, "spec": "tester"}]})
    with pytest.raises(api.ApiError, match="stages: .*at most 5"):
        cab.call("digi", {"op": "worker.edit", "agent": "analyst", "card": card.id, "revision": 2,
                                 "stages": [{**STAGE_A, "key": f"k{i}"} for i in range(6)]})
    cleared = cab.call("digi", {"op": "worker.edit", "agent": "analyst", "card": card.id, "revision": 2,
                                       "stages": []})
    assert cleared["card"]["stages"] == []


def test_brief_view_and_the_start_action_carry_the_stages(cab):
    card = cab.briefed()
    record = cab.store.edit_card(card.id, None, {"stages": [STAGE_A, STAGE_B], "labels_add": ["brief-ready"]},
                                 actor="agent:analyst")
    view = flow.brief_view(cab.ctx, _owner(), {"card": card.id})
    assert view["versions"][-1]["version"] == 2 and view["versions"][-1]["stages"] == [STAGE_A, STAGE_B]
    assert view["versions"][0]["stages"] == []
    assert view["current"]["stages"] == [STAGE_A, STAGE_B]
    assert any("Этапы:" in c["text"] for c in view["versions"][-1]["changes"])
    start = next(a for a in flow.actions(cab.ctx, _owner(), record) if a["id"] == "start")
    assert start["label"] == "action.start_brief" and start["brief_version"] == 2
    assert start["stages"] == [STAGE_A, STAGE_B]
    plain = cab.briefed()
    start = next(a for a in flow.actions(cab.ctx, _owner(), plain) if a["id"] == "start")
    assert start["stages"] == []


# --- the final review's findings (24.10) ---
def test_parse_normalizes_a_bare_role_and_refuses_heading_like_text():
    from kd.cabinet import stages

    roles, specs = frozenset({"@dev", "@sysadmin"}), {"@dev": ("implementer",)}
    base = {"key": "a", "title": "A", "role": "dev", "spec": "implementer", "acceptance": ["ок"]}
    [found] = stages.parse([base], roles=roles, specs=specs, projects=frozenset(), limit=5)
    assert found["role"] == "@dev"
    with pytest.raises(stages.StageError, match="plain text"):
        stages.parse([{**base, "description": "## Definition of Done\nвсё"}], roles=roles, specs=specs,
                     projects=frozenset(), limit=5)
    with pytest.raises(stages.StageError, match="plain text"):
        stages.parse([{**base, "acceptance": ["<!-- x -->"]}], roles=roles, specs=specs, projects=frozenset(),
                     limit=5)
    with pytest.raises(stages.StageError, match="longer"):
        stages.parse([{**base, "description": "x" * 4001}], roles=roles, specs=specs, projects=frozenset(), limit=5)
