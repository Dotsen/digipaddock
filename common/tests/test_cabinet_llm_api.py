"""the cabinet's models of other providers — ``llm.*`` operations, the model and effort of a card, and the
effective pair a session runs on."""

import pytest

from kd import llm_models
from kd.cabinet import api, llm_api, usage, users
from cabinet_env import CabinetEnv

MODEL = {"op": "llm.create", "id": "or-gpt5", "provider": "openrouter", "upstream": "openai/gpt-5",
         "title": "GPT-5 via OpenRouter", "price_in": 1.25, "price_out": 10, "price_cache_read": 0.125,
         "price_cache_write": 1.25, "context": 400000, "base_url": ""}


def person(user):
    return api.Principal("user", -1, user)


@pytest.fixture
def env(tmp_path):
    cab = CabinetEnv(tmp_path)
    people = users.Users(cab.store)
    people.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    people.create("anna", "Анна", roles=[("partner", ["web"])], actor="user:owner")
    people.create("bob", "Боб", roles=[("partner", ["web", "digipaddock"])], actor="user:owner")
    cab.ctx.llm_env_dir = str(tmp_path / "llm")  # the host's key files: none here
    return cab


def call(env, user, request):
    return api.dispatch(env.ctx, person(user), request)


def refused(env, user, request):
    with pytest.raises(api.ApiError) as exc:
        call(env, user, request)
    return exc.value


def listed(env, user):
    return {m["id"]: m for m in call(env, user, {"op": "llm.list"})["models"]}


def own(models):
    """The cabinet's own models of a list: the subscription's come first in every list."""
    return [m for m in models if not m["builtin"]]


# --- llm.list / create ---------------------------------------------------------------------------------------
def test_the_owner_creates_a_model_and_everyone_lists_it(env, tmp_path):
    made = call(env, "owner", MODEL)["model"]
    assert made["id"] == "or-gpt5" and made["enabled"] == 1 and made["key"] is False and made["experimental"] is True
    mine = listed(env, "owner")["or-gpt5"]
    assert mine["price_in"] == 1.25 and mine["upstream"] == "openai/gpt-5" and mine["probe"] == {}
    # a partner sees that the model exists, not its prices or upstream
    assert listed(env, "anna")["or-gpt5"] == {"id": "or-gpt5", "title": "GPT-5 via OpenRouter",
                                              "provider": "openrouter", "enabled": 1, "key": False,
                                              "experimental": True, "builtin": False, "version": ""}
    assert set(listed(env, "anna")["or-gpt5"]) == set(llm_api.READ_FIELDS)
    # the key file of the kind makes key true
    (tmp_path / "llm").mkdir()
    (tmp_path / "llm" / "openrouter.env").write_text("ANTHROPIC_AUTH_TOKEN=x\n", encoding="utf-8")
    assert listed(env, "owner")["or-gpt5"]["key"] is True
    # the executor and root read the full row too
    assert own(env.call("digi", {"op": "llm.list"})["models"])[0]["upstream"] == "openai/gpt-5"


def test_only_settings_creates_updates_or_deletes(env):
    assert refused(env, "anna", MODEL).kind == "refused"
    call(env, "owner", MODEL)
    assert refused(env, "bob", {"op": "llm.update", "id": "or-gpt5", "title": "x"}).kind == "refused"
    assert refused(env, "bob", {"op": "llm.delete", "id": "or-gpt5"}).kind == "refused"
    assert refused(env, "bob", {"op": "llm.probe.request", "id": "or-gpt5"}).kind == "refused"


@pytest.mark.parametrize("bad, why", [
    ({"id": "claude-sonnet-5-5"}, "Claude"),
    ({"id": "Or GPT"}, "model id"),
    ({"provider": "together"}, "unknown provider"),
    ({"provider": "custom"}, "base_url"),
    ({"upstream": "a b"}, "provider's model id"),
    ({"context": 999}, "context"),
    ({"context": 10_000_001}, "context"),
    ({"price_out": -1}, "price_out"),
    ({"price_out": 10_001}, "price_out"),
    ({"title": ""}, "title"),
    ({"extra": 1}, "unknown field"),
])
def test_create_refuses_a_bad_model(env, bad, why):
    error = refused(env, "owner", {**MODEL, **bad})
    assert error.kind == "invalid" and why in str(error)
    assert own(call(env, "owner", {"op": "llm.list"})["models"]) == []


def test_create_refuses_a_missing_field_and_a_duplicate(env):
    error = refused(env, "owner", {k: v for k, v in MODEL.items() if k != "title"})
    assert error.kind == "invalid"
    call(env, "owner", MODEL)
    assert "exists" in str(refused(env, "owner", MODEL))


def test_a_custom_model_names_its_endpoint(env):
    made = call(env, "owner", {**MODEL, "id": "gw", "provider": "custom", "base_url": "https://gw.local/v1"})["model"]
    assert made["base_url"] == "https://gw.local/v1"
    assert llm_api.spec_of(made) == {"id": "gw", "provider": "custom", "upstream": "openai/gpt-5",
                                     "base_url": "https://gw.local/v1"}


# --- llm.update / delete -------------------------------------------------------------------------------------
def test_update_changes_the_fields_but_never_the_id_or_the_kind(env):
    call(env, "owner", MODEL)
    after = call(env, "owner", {"op": "llm.update", "id": "or-gpt5", "title": "GPT-5", "price_in": 2,
                                "context": 1_000_000})["model"]
    assert (after["title"], after["price_in"], after["context"]) == ("GPT-5", 2.0, 1_000_000)
    for bad in ({"provider": "fireworks"}, {"new_id": "x"}, {"probe": {}}, {"enabled": 2}, {}):
        assert refused(env, "owner", {"op": "llm.update", "id": "or-gpt5", **bad}).kind == "invalid"
    assert refused(env, "owner", {"op": "llm.update", "id": "none", "title": "x"}).kind == "not_found"


def test_a_referenced_model_is_disabled_not_deleted(env):
    call(env, "owner", MODEL)
    card = env.card()
    call(env, "owner", {"op": "user.card.edit", "card": card.id, "revision": card.revision,
                        "changes": {"model": "or-gpt5"}})
    call(env, "owner", {"op": "project.update", "id": "digipaddock", "model": "or-gpt5"})
    error = refused(env, "owner", {"op": "llm.delete", "id": "or-gpt5"})
    assert error.kind == "refused" and error.reason == "model_referenced"
    assert "KD-" in str(error) or card.id in str(error)
    disabled = call(env, "owner", {"op": "llm.update", "id": "or-gpt5", "enabled": 0})["model"]
    assert disabled["enabled"] == 0 and listed(env, "owner")["or-gpt5"]["enabled"] == 0
    # the references gone, the delete goes through
    card = env.store.card(card.id)
    call(env, "owner", {"op": "user.card.edit", "card": card.id, "revision": card.revision,
                        "changes": {"model": ""}})
    call(env, "owner", {"op": "project.update", "id": "digipaddock", "model": ""})
    assert call(env, "owner", {"op": "llm.delete", "id": "or-gpt5"}) == {"ok": True, "deleted": "or-gpt5"}
    assert own(call(env, "owner", {"op": "llm.list"})["models"]) == []
    assert refused(env, "owner", {"op": "llm.delete", "id": "or-gpt5"}).kind == "not_found"


# --- the card's model and effort -------------------------------------------------------------------------------
def test_a_card_model_and_effort_are_a_settings_decision(env):
    call(env, "owner", MODEL)
    card = env.card()
    out = call(env, "owner", {"op": "user.card.edit", "card": card.id, "revision": card.revision,
                              "changes": {"model": "or-gpt5", "effort": "xhigh"}})
    assert (out["card"]["model"], out["card"]["effort"]) == ("or-gpt5", "xhigh")
    assert env.store.card(card.id).model == "or-gpt5"
    # bob edits cards of the project, but the model is the settings' (a decision about spend)
    revision = env.store.card(card.id).revision
    error = refused(env, "bob", {"op": "user.card.edit", "card": card.id, "revision": revision,
                                 "changes": {"effort": "low"}})
    assert error.kind == "refused" and "settings" in str(error)
    assert env.store.card(card.id).effort == "xhigh"


@pytest.mark.parametrize("changes, why", [
    ({"model": "nope"}, "model"), ({"effort": "ultra"}, "effort"), ({"model": 3}, "model"), ({"effort": None}, "effort"),
])
def test_a_card_refuses_an_unknown_model_or_effort(env, changes, why):
    call(env, "owner", MODEL)
    card = env.card()
    error = refused(env, "owner", {"op": "user.card.edit", "card": card.id, "revision": card.revision,
                                   "changes": changes})
    assert error.kind == "invalid" and why in str(error)


def test_a_card_takes_only_an_enabled_model(env):
    call(env, "owner", MODEL)
    call(env, "owner", {"op": "llm.update", "id": "or-gpt5", "enabled": 0})
    card = env.card()
    error = refused(env, "owner", {"op": "user.card.edit", "card": card.id, "revision": card.revision,
                                   "changes": {"model": "or-gpt5"}})
    assert error.kind == "invalid" and "disabled" in str(error)
    assert refused(env, "owner", {"op": "project.update", "id": "digipaddock", "model": "or-gpt5"}).kind == "invalid"


def test_an_agent_never_sets_a_model_or_effort(env):
    call(env, "owner", MODEL)
    card = env.card(status="In Progress")
    for field in ("model", "effort"):
        with pytest.raises(api.ApiError) as exc:
            env.call("digi", {"op": "worker.edit", "agent": "dev", "card": card.id, field: "x"})
        assert exc.value.kind == "refused" and field in str(exc.value)
    assert env.store.card(card.id).model == "" and env.store.card(card.id).effort == ""


# --- the effective pair ----------------------------------------------------------------------------------------
def test_effective_is_the_cards_else_the_projects_else_the_subscription(env):
    call(env, "owner", MODEL)
    card = env.card()
    assert llm_api.effective(env.store, env.store.card(card.id)) == (None, "")
    call(env, "owner", {"op": "project.update", "id": "digipaddock", "model": "or-gpt5", "effort": "low"})
    row, effort = llm_api.effective(env.store, env.store.card(card.id))
    assert row["id"] == "or-gpt5" and effort == "low"
    card = env.store.card(card.id)
    call(env, "owner", {"op": "user.card.edit", "card": card.id, "revision": card.revision,
                        "changes": {"effort": "max"}})
    row, effort = llm_api.effective(env.store, env.store.card(card.id))
    assert row["id"] == "or-gpt5" and effort == "max"
    # a disabled model is still the effective one: the caller decides what a disabled model means
    call(env, "owner", {"op": "llm.update", "id": "or-gpt5", "enabled": 0})
    row, _ = llm_api.effective(env.store, env.store.card(card.id))
    assert row["id"] == "or-gpt5" and row["enabled"] == 0


# --- the probe's bookkeeping (the run itself is the host's, Task 10) -------------------------------------------
def test_a_probe_request_marks_the_model_and_a_result_replaces_it(env):
    call(env, "owner", MODEL)
    asked = call(env, "owner", {"op": "llm.probe.request", "id": "or-gpt5"})["model"]
    assert asked["probe"] == {"state": "requested", "at": env.now}
    env.now = "2026-10-14T10:01:00Z"
    done = env.call("root", {"op": "llm.probe.result", "id": "or-gpt5", "ok": True, "detail": "212 ms"})["model"]
    assert done["probe"] == {"at": env.now, "ok": True, "model": "", "ms": 0, "detail": "212 ms"}
    done = env.call("root", {"op": "llm.probe.result", "id": "or-gpt5", "ok": False, "model": "openai/gpt-5",
                             "ms": 1200, "detail": "exit 1: x" * 100})["model"]
    assert done["probe"]["model"] == "openai/gpt-5" and done["probe"]["ms"] == 1200
    assert len(done["probe"]["detail"]) == llm_api.DETAIL_MAX
    assert refused(env, "owner", {"op": "llm.probe.result", "id": "or-gpt5", "ok": True, "detail": ""}).kind == "refused"
    for bad in ({"ok": "yes", "detail": ""}, {"ok": True, "detail": 1}, {"ok": True, "model": "x y"},
                {"ok": True, "ms": -1}, {"ok": True, "ms": 1.5}):
        with pytest.raises(api.ApiError) as exc:
            env.call("root", {"op": "llm.probe.result", "id": "or-gpt5", **bad})
        assert exc.value.kind == "invalid"


def test_a_probe_report_of_the_host_ends_the_running_probe(env):
    """The host reports ``{type: probe, probe, ok, model, ms, detail}``: the model whose running probe has that id
    gets the result; a report of a probe no model waits for is ignored."""
    call(env, "owner", MODEL)
    env.store.update_llm_model("or-gpt5", {"probe": {"state": "running", "at": env.now, "id": "ab" * 16}},
                               actor="system")
    env.call("root", {"op": "host.report", "items": [
        {"type": "probe", "probe": "cd" * 16, "ok": True, "model": "", "ms": 1, "detail": ""},  # nobody's
        {"type": "probe", "probe": "ab" * 16, "ok": True, "model": "openai/gpt-5", "ms": 900, "detail": "OK"},
    ]})
    assert env.store.llm_model("or-gpt5")["probe"] == {"at": env.now, "ok": True, "model": "openai/gpt-5", "ms": 900,
                                                       "detail": "OK"}
    with pytest.raises(api.ApiError) as exc:
        env.call("root", {"op": "host.report", "items": [{"type": "probe", "probe": "zz", "ok": True}]})
    assert exc.value.kind == "invalid"


# --- the models of the Claude subscription, listed with the cabinet's own ------------------------------
CLAUDE = ["claude:opus", "claude:sonnet", "claude:haiku", "claude:fable"]


def test_subscription_models_are_listed_first_and_builtin(env, tmp_path):
    call(env, "owner", MODEL)
    env.ctx.claude_env = str(tmp_path / "claude.env")
    models = call(env, "owner", {"op": "llm.list"})["models"]
    assert [m["id"] for m in models] == CLAUDE + ["or-gpt5"]
    opus = models[0]
    assert (opus["builtin"], opus["enabled"], opus["provider"], opus["upstream"]) == (True, 1, "subscription", "opus")
    assert opus["title"] == "Claude Opus" and opus["key"] is False and opus["experimental"] is False
    assert opus["probe"] == {} and models[-1]["builtin"] is False
    (tmp_path / "claude.env").write_text("CLAUDE_CODE_OAUTH_TOKEN=x\n", encoding="utf-8")
    assert listed(env, "owner")["claude:opus"]["key"] is True  # the host's token file is the subscription's key
    assert set(listed(env, "anna")["claude:opus"]) == set(llm_api.READ_FIELDS) and "builtin" in llm_api.READ_FIELDS


def test_a_builtin_model_is_not_changed_or_deleted(env):
    for op in ({"op": "llm.update", "id": "claude:opus", "title": "X"}, {"op": "llm.update", "id": "claude:opus",
               "enabled": 0}, {"op": "llm.delete", "id": "claude:opus"}):
        error = refused(env, "owner", op)
        assert error.kind == "refused" and error.reason == "model_builtin"
    assert refused(env, "owner", {**MODEL, "id": "claude:mine"}).kind == "invalid"  # a reference is no slug
    assert refused(env, "owner", {"op": "llm.delete", "id": "claude:nope"}).kind == "not_found"


def test_a_card_and_a_project_take_a_subscription_model(env):
    card = env.card()
    call(env, "owner", {"op": "user.card.edit", "card": card.id, "revision": card.revision,
                        "changes": {"model": "claude:sonnet"}})
    row, effort = llm_api.effective(env.store, env.store.card(card.id))
    assert (row["id"], row["provider"], row["enabled"], effort) == ("claude:sonnet", "subscription", 1, "")
    assert llm_api.spec_of(row) == llm_models.subscription_spec("claude:sonnet")
    done = call(env, "owner", {"op": "project.update", "id": "web", "model": "claude:opus"})["project"]
    assert done["model"] == "claude:opus"
    card = env.store.card(card.id)
    for bad in ("claude:nope", "claude:"):
        error = refused(env, "owner", {"op": "user.card.edit", "card": card.id, "revision": card.revision,
                                       "changes": {"model": bad}})
        assert error.kind == "invalid"


def test_an_alias_gone_from_the_catalog_runs_as_the_agent(env):
    """A card kept the reference of an alias a later catalog dropped: its sessions run as without a model."""
    card = env.card()
    env.store._conn.execute("UPDATE cards SET model='claude:old' WHERE id=?", (card.id,))
    assert llm_api.effective(env.store, env.store.card(card.id)) == (None, "")
    assert call(env, "owner", {"op": "card.get", "card": card.id})["card"]["model"] == "claude:old"


def test_a_subscription_model_is_probed(env):
    asked = call(env, "owner", {"op": "llm.probe.request", "id": "claude:haiku"})["model"]
    assert asked["probe"] == {"state": "requested", "at": env.now} and asked["builtin"] is True
    assert listed(env, "owner")["claude:haiku"]["probe"]["state"] == "requested"
    assert listed(env, "owner")["claude:opus"]["probe"] == {}
    running = {"state": "running", "at": env.now, "id": "ab" * 16}
    assert llm_api.set_probe(env.store, "claude:haiku", running, actor="system")["probe"] == running
    assert llm_api.probe_waiting(env.store, "ab" * 16)["id"] == "claude:haiku"
    env.call("root", {"op": "host.report", "items": [
        {"type": "probe", "probe": "ab" * 16, "ok": True, "model": "claude-haiku-5-5", "ms": 800, "detail": "OK"}]})
    assert listed(env, "owner")["claude:haiku"]["probe"] == {"at": env.now, "ok": True, "model": "claude-haiku-5-5",
                                                            "ms": 800, "detail": "OK"}
    assert refused(env, "bob", {"op": "llm.probe.request", "id": "claude:haiku"}).kind == "refused"


def test_a_subscription_models_probe_is_a_model_event(env):
    """the probe of a builtin model lives in the cabinet's state, not in a row — its change is still the
    ``llm_model.changed`` event the web reads its list again on."""
    call(env, "owner", {"op": "llm.probe.request", "id": "claude:opus"})
    event = [e for e in env.store.events_since(0, 1000) if e["kind"] == "llm_model.changed"][-1]
    assert event["payload"]["model"] == "claude:opus" and event["payload"]["probe"]["to"]["state"] == "requested"
    assert event["payload"]["probe"]["from"] == {}


# --- the version a subscription model's alias served last, in the list ----------------------------------
def test_the_list_gives_the_version_a_subscription_model_served(env):
    call(env, "owner", MODEL)
    assert [listed(env, "owner")[m]["version"] for m in ("claude:opus", "or-gpt5")] == ["", ""]
    with env.store.transaction():
        usage.saw(env.store, "claude:opus", ["claude-opus-5-5"], env.now)
    assert listed(env, "owner")["claude:opus"]["version"] == "claude-opus-5-5"
    assert listed(env, "anna")["claude:opus"]["version"] == "claude-opus-5-5"  # without settings too
    assert listed(env, "owner")["claude:sonnet"]["version"] == ""


def test_a_successful_probe_of_a_subscription_model_keeps_its_version(env):
    env.call("root", {"op": "llm.probe.result", "id": "claude:opus", "ok": False, "model": "claude-opus-5-5",
                      "ms": 10, "detail": "exit 1"})
    assert listed(env, "owner")["claude:opus"]["version"] == ""  # a failed probe proves nothing
    env.call("root", {"op": "llm.probe.result", "id": "claude:opus", "ok": True,
                      "model": "claude-haiku-4-5,claude-opus-5-5", "ms": 900, "detail": "OK"})
    assert listed(env, "owner")["claude:opus"]["version"] == "claude-opus-5-5"
