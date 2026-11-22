"""the cabinet models of other providers (``llm_models``) and the model and effort of cards, projects
and sessions — the store's rows and their events."""

import sqlite3

import pytest

from kd.cabinet import schema, store
from cabinet_env import CabinetEnv

VALUES = {"id": "or-x", "provider": "openrouter", "base_url": "", "upstream": "anthropic/claude-sonnet-4.5",
          "title": "Sonnet via OpenRouter", "price_in": 3, "price_out": 15, "price_cache_read": 0.3,
          "price_cache_write": 3.75, "context": 200000}


@pytest.fixture
def env(tmp_path):
    return CabinetEnv(tmp_path)


def events(env, kind):
    return [e for e in env.store.events_since(0) if e["kind"] == kind]


def test_a_created_model_is_enabled_and_unprobed(env):
    made = env.store.create_llm_model(VALUES, actor="user:owner")
    assert made == env.store.llm_model("or-x")
    assert made["enabled"] == 1 and made["probe"] == {} and made["created"] == made["updated"] == env.now
    assert [m["id"] for m in env.store.llm_models()] == ["or-x"]
    assert events(env, "llm_model.created")[-1]["payload"] == {"model": "or-x", "provider": "openrouter",
                                                               "upstream": "anthropic/claude-sonnet-4.5"}
    assert env.store.llm_model("none") is None


@pytest.mark.parametrize("bad", [
    {**VALUES, "id": "sonnet"},  # a Claude alias
    {**VALUES, "provider": "together"},
    {**VALUES, "base_url": "https://x"},  # the kind has its own
    {k: v for k, v in VALUES.items() if k != "title"},
    {**VALUES, "extra": 1},
    {**VALUES, "title": ""},
    {**VALUES, "price_in": -1},
    {**VALUES, "context": 0},
    {**VALUES, "context": "x"},
])
def test_create_refuses_a_bad_model(env, bad):
    with pytest.raises(store.Invalid):
        env.store.create_llm_model(bad, actor="user:owner")
    assert env.store.llm_models() == []


def test_create_refuses_a_duplicate(env):
    env.store.create_llm_model(VALUES, actor="user:owner")
    with pytest.raises(store.Conflict):
        env.store.create_llm_model(VALUES, actor="user:owner")


def test_update_changes_fields_and_records_from_to(env):
    env.store.create_llm_model(VALUES, actor="user:owner")
    env.now = "2026-10-14T10:05:00Z"
    after = env.store.update_llm_model("or-x", {"enabled": 0, "title": "T2"}, actor="user:owner")
    assert after["enabled"] == 0 and after["title"] == "T2" and after["updated"] == env.now
    assert env.store.llm_models(enabled_only=True) == []
    payload = events(env, "llm_model.changed")[-1]["payload"]
    assert payload == {"model": "or-x", "enabled": {"from": 1, "to": 0}, "title": {"from": VALUES["title"], "to": "T2"}}
    probe = {"at": env.now, "ok": True, "detail": "212 ms"}
    assert env.store.update_llm_model("or-x", {"probe": probe}, actor="user:owner")["probe"] == probe
    # nothing to change — no event
    before = len(events(env, "llm_model.changed"))
    env.store.update_llm_model("or-x", {"title": "T2"}, actor="user:owner")
    assert len(events(env, "llm_model.changed")) == before


@pytest.mark.parametrize("changes", [
    {"id": "y"}, {"provider": "fireworks"}, {"upstream": "a b"}, {"enabled": 2}, {"base_url": "https://x"},
    {"probe": "x"}, {"price_out": "x"}, {}, {"nope": 1},
])
def test_update_refuses_the_id_the_kind_and_bad_values(env, changes):
    env.store.create_llm_model(VALUES, actor="user:owner")
    with pytest.raises(store.Invalid):
        env.store.update_llm_model("or-x", changes, actor="user:owner")
    with pytest.raises(store.NotFound):
        env.store.update_llm_model("none", {"title": "x"}, actor="user:owner")


def test_a_custom_model_may_change_its_base_url(env):
    custom = {**VALUES, "id": "gw", "provider": "custom", "base_url": "https://gw.local/v1"}
    env.store.create_llm_model(custom, actor="user:owner")
    after = env.store.update_llm_model("gw", {"base_url": "https://gw2.local/v1"}, actor="user:owner")
    assert after["base_url"] == "https://gw2.local/v1"


def test_a_card_takes_a_model_and_an_effort(env):
    env.store.create_llm_model(VALUES, actor="user:owner")
    card = env.card()
    assert (card.model, card.effort) == ("", "")
    after = env.store.edit_card(card.id, card.revision, {"model": "or-x", "effort": "high"}, actor="user:owner")
    assert (after.model, after.effort) == ("or-x", "high")
    assert after.to_dict()["model"] == "or-x" and store.CardRecord.from_dict(after.to_dict()) == after
    assert env.store.llm_model_refs("or-x") == {"cards": [card.id], "projects": []}
    cleared = env.store.edit_card(card.id, after.revision, {"model": "", "effort": ""}, actor="user:owner")
    assert (cleared.model, cleared.effort) == ("", "")
    assert env.store.llm_model_refs("or-x") == {"cards": [], "projects": []}


@pytest.mark.parametrize("changes", [{"model": "none"}, {"effort": "ultra"}, {"model": 3}, {"effort": None}])
def test_a_card_refuses_an_unknown_model_or_effort(env, changes):
    env.store.create_llm_model(VALUES, actor="user:owner")
    card = env.card()
    with pytest.raises(store.Invalid):
        env.store.edit_card(card.id, card.revision, changes, actor="user:owner")


def test_a_project_takes_a_model_and_an_effort(env):
    env.store.create_llm_model(VALUES, actor="user:owner")
    assert env.store.project("digipaddock")["model"] == "" and env.store.project("digipaddock")["effort"] == ""
    after = env.store.update_project("digipaddock", actor="user:owner", model="or-x", effort="low")
    assert (after["model"], after["effort"]) == ("or-x", "low")
    payload = events(env, "project.changed")[-1]["payload"]
    assert payload["model"] == {"from": "", "to": "or-x"} and payload["effort"] == {"from": "", "to": "low"}
    assert env.store.llm_model_refs("or-x") == {"cards": [], "projects": ["digipaddock"]}
    assert env.store.project_rows()[0]["model"] == ""  # admin, by id
    for bad in ({"model": "none"}, {"effort": "ultra"}):
        with pytest.raises(store.Invalid):
            env.store.update_project("digipaddock", actor="user:owner", **bad)


def test_delete_removes_the_row_and_its_event_names_it(env):
    env.store.create_llm_model(VALUES, actor="user:owner")
    env.store.delete_llm_model("or-x", actor="user:owner")
    assert env.store.llm_model("or-x") is None and env.store.llm_models() == []
    assert events(env, "llm_model.deleted")[-1]["payload"] == {"model": "or-x", "provider": "openrouter",
                                                               "upstream": "anthropic/claude-sonnet-4.5"}
    with pytest.raises(store.NotFound):
        env.store.delete_llm_model("or-x", actor="user:owner")


def test_delete_refuses_a_referenced_model(env):
    env.store.create_llm_model(VALUES, actor="user:owner")
    card = env.card()
    env.store.edit_card(card.id, card.revision, {"model": "or-x"}, actor="user:owner")
    with pytest.raises(store.Invalid):
        env.store.delete_llm_model("or-x", actor="user:owner")
    assert env.store.llm_model("or-x") is not None


def test_a_session_records_its_model_and_effort(env):
    env.store.create_llm_model(VALUES, actor="user:owner")
    card = env.card()
    session = env.store.session_for(card.thread, "dev")
    assert (session["model"], session["effort"]) == ("", "")
    after = env.store.session_update(session["id"], model="or-x", effort="high")
    assert (after["model"], after["effort"]) == ("or-x", "high")


def test_schema_37_adds_the_models_and_the_columns(tmp_path):
    path = str(tmp_path / "v36.db")
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(schema.DDL + "".join(schema.MIGRATIONS[v] for v in range(2, 37)))
    conn.execute("INSERT INTO meta VALUES ('schema_version', '36')")
    conn.execute("INSERT INTO meta VALUES ('next_card', '1')")
    conn.execute("INSERT INTO projects (id, name, route, routed) VALUES ('shop', 'Shop', '@dev', '')")
    conn.close()
    db = store.Store.open(path)
    assert db.schema_version() == schema.SCHEMA_VERSION  # 37 and every later step
    assert db.project("shop")["model"] == "" and db.project("shop")["effort"] == ""
    assert db.llm_models() == []
    columns = {r[1] for r in db._conn.execute("PRAGMA table_info(cards)")}
    assert {"model", "effort"} <= columns
    columns = {r[1] for r in db._conn.execute("PRAGMA table_info(sessions)")}
    assert {"model", "effort"} <= columns


def test_a_card_and_a_project_take_a_subscription_reference(env):
    card = env.card()
    after = env.store.edit_card(card.id, card.revision, {"model": "claude:opus"}, actor="user:owner")
    assert after.model == "claude:opus"
    assert env.store.update_project("web", actor="user:owner", model="claude:sonnet")["model"] == "claude:sonnet"
    for bad in ("claude:nope", "claude:"):
        with pytest.raises(store.Invalid):
            env.store.edit_card(card.id, after.revision, {"model": bad}, actor="user:owner")
