"""The read-only pipeline inventory — one item per open card with something to look at, classed;
it reads the base and writes nothing (no events, no commands)."""

import pytest
from cabinet_env import CabinetEnv

from kd import session_config
from kd.cabinet import api, briefs, inventory, problems, users

HOSTED = session_config.HostConfig({"boss": 1, "dev": 2, "analyst": 1}, 600)
STAGE = {"key": "a", "title": "Этап A", "role": "@dev", "spec": "implementer", "depends": [],
         "description": "", "acceptance": []}


@pytest.fixture
def cab(tmp_path):
    env = CabinetEnv(tmp_path)
    users.Users(env.store).create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    api.install_host(env.ctx, lambda: HOSTED)
    return env


def _counts(cab):
    conn = cab.store._conn
    return tuple(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                 for table in ("events", "commands", "problems", "waits"))


@pytest.fixture
def base(cab):
    """One card per class, plus a clean one and a closed one that must not appear."""
    s, out = cab.store, {}
    out["no_next_step"] = cab.card(status="In Progress", assignee="@dev", title="Без шага")
    out["problem_open"] = cab.card(status="To Do", assignee="@dev", title="Проблема")
    problems.raise_problem(cab.ctx, key="inv:problem", record=out["problem_open"], kind="missing_next_step",
                           agent="dev")
    out["wait_stale"] = cab.card(status="In Progress", assignee="@dev", title="Ожидание")
    s._conn.execute(
        "INSERT INTO waits (id, command, card, agent, kind, ref, blocking, mode, state, created, updated)"
        " VALUES ('w1', 'cmd1', ?, 'dev', 'decision', 'gone', 1, 'enforce', 'open', ?, ?)",
        (out["wait_stale"].id, cab.now, cab.now))
    out["blocked_unexplained"] = cab.card(status="Blocked", assignee="@dev", title="Blocked без причины")
    out["stages_unmaterialized"] = cab.card(status="In Progress", assignee="@dev", title="Этапы без карточек")
    record = s.edit_card(out["stages_unmaterialized"].id, None,
                         {"description": "Цель.", "acceptance": [{"text": "Готово", "checked": False}],
                          "stages": [STAGE]}, actor="agent:analyst")
    briefs.add_version(s, record, origin=briefs.ORIGINS[0], author="agent:analyst")
    s._conn.execute("UPDATE brief_versions SET approved_by='owner' WHERE card=?", (record.id,))
    out["aggregate"] = cab.card(status="In Progress", assignee="@dev", title="Агрегат")
    child = cab.card(status="To Do", assignee="@dev", title="Этап агрегата", parent=out["aggregate"].id)
    s._conn.execute("INSERT INTO stages (card, version, key, child, state, created, updated)"
                    " VALUES (?, 1, 'a', ?, 'created', ?, ?)", (out["aggregate"].id, child.id, cab.now, cab.now))
    out["release_degraded"] = cab.card(status="In Progress", assignee="@dev", title="Выкатка")
    s.add_thread_message(out["release_degraded"].thread, author="system:release", kind="notice", body="",
                         via="notice", payload={"key": "notice.release.degraded", "level": "critical",
                                                "source": "release",
                                                "params": {"rev": "abc1234", "failed": "nginx", "why": "502"}})
    s._conn.commit()
    out["clean"] = cab.card(status="To Do", assignee="@owner", title="Чистая")
    out["done"] = cab.card(status="Done", assignee="@dev", title="Закрытая")
    return out


def _by_card(items):
    return {item["card"]: item for item in items}


def test_collect_classes_each_card_once_and_reads_only(cab, base):
    before = _counts(cab)
    found = _by_card(inventory.collect(cab.ctx))
    assert _counts(cab) == before
    assert base["clean"].id not in found and base["done"].id not in found
    assert found[base["no_next_step"].id]["classes"] == ["no_next_step"]
    assert found[base["problem_open"].id]["classes"] == ["problem_open"]
    assert found[base["problem_open"].id]["detail"]["problems"] == ["missing_next_step"]
    assert "wait_stale" in found[base["wait_stale"].id]["classes"]
    assert found[base["wait_stale"].id]["detail"]["waits"] == [{"id": "w1", "kind": "decision", "ref": "gone",
                                                                 "source": "needs_routing"}]
    assert "blocked_unexplained" in found[base["blocked_unexplained"].id]["classes"]
    assert found[base["stages_unmaterialized"].id]["detail"]["stages_missing"] == ["a"]
    assert "stages_unmaterialized" in found[base["stages_unmaterialized"].id]["classes"]
    assert found[base["aggregate"].id]["classes"] == ["aggregate"]
    assert found[base["aggregate"].id]["detail"]["open_children"] == 1
    item = found[base["release_degraded"].id]
    assert "release_degraded" in item["classes"] and item["detail"]["release"]["rev"] == "abc1234"
    for item in found.values():
        assert set(item) == {"card", "status", "assignee", "project", "title", "classes", "detail"}


def test_render_one_line_per_card(cab, base):
    items = inventory.collect(cab.ctx)
    text = inventory.render(items)
    lines = text.splitlines()
    assert len(lines) == len(items) and all(line.startswith("KD-") for line in lines)
    assert any("release_degraded" in line and "abc1234" in line for line in lines)
    assert inventory.render([]) == "nothing to look at"


def test_op_and_cli(cab, base, capsys):
    out = api.dispatch(cab.ctx, api.Principal("root", 0, "root"), {"op": "pipeline.inventory"})
    assert {i["card"] for i in out["items"]} == {i["card"] for i in inventory.collect(cab.ctx)}
    with pytest.raises(api.ApiError):
        api.dispatch(cab.ctx, api.Principal("executor", 1000, "digi"), {"op": "pipeline.inventory", "agent": "dev"})
    import io
    import json
    import runpy
    from pathlib import Path

    mod = runpy.run_path(str(Path(__file__).resolve().parents[1] / "bin" / "kd-cabinet"), run_name="kd_cabinet")

    class Client:
        def call(self, request):
            return api.dispatch(cab.ctx, api.Principal("root", 0, "root"), request)

    buf = io.StringIO()
    assert mod["main"](["pipeline", "inventory"], client=Client(), out=buf) == 0
    assert len(buf.getvalue().splitlines()) == len(out["items"])
    buf = io.StringIO()
    assert mod["main"](["pipeline", "inventory", "--json"], client=Client(), out=buf) == 0
    assert {i["card"] for i in json.loads(buf.getvalue())["items"]} == {i["card"] for i in out["items"]}


def test_a_done_stage_is_not_an_unmaterialized_one(cab):
    """Review F6: only a key without a child counts, a finished child is still a child."""
    s = cab.store
    parent = cab.card(status="In Progress", assignee="@dev", title="Агрегат с готовым этапом")
    record = s.edit_card(parent.id, None, {"description": "Цель.", "acceptance": [{"text": "Готово", "checked": False}],
                                           "stages": [STAGE]}, actor="agent:analyst")
    briefs.add_version(s, record, origin=briefs.ORIGINS[0], author="agent:analyst")
    s._conn.execute("UPDATE brief_versions SET approved_by='owner' WHERE card=?", (record.id,))
    child = cab.card(status="Done", assignee="@dev", title="Готовый этап", parent=parent.id)
    s._conn.execute("INSERT INTO stages (card, version, key, child, state, created, updated)"
                    " VALUES (?, 1, 'a', ?, 'created', ?, ?)", (parent.id, child.id, cab.now, cab.now))
    s._conn.commit()
    found = _by_card(inventory.collect(cab.ctx))
    assert parent.id not in found or "stages_unmaterialized" not in found[parent.id]["classes"]
