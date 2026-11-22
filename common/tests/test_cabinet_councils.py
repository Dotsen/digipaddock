"""the council's composition — the critics (models) of a card's brief and code, set on the card or as its
project's default: its checks and limits, who sets it, how a card's own composition overrides its project's by kind,
and the schema's step that holds it."""

import json
import sqlite3
from pathlib import Path

import pytest

from kd import council
from kd.cabinet import api, councils, schema, store, users
from cabinet_env import CabinetEnv

REPO = Path(__file__).resolve().parents[2]

MODEL = {
    "op": "llm.create",
    "id": "or-gpt5",
    "provider": "openrouter",
    "upstream": "openai/gpt-5",
    "title": "GPT-5 via OpenRouter",
    "price_in": 1.25,
    "price_out": 10,
    "price_cache_read": 0.125,
    "price_cache_write": 1.25,
    "context": 400000,
    "base_url": "",
}
BRIEF = {"critics": ["claude:opus", "or-gpt5"], "rounds": 3}
CODE = {"critics": ["claude:opus"], "rounds": 3, "min_lines": 100}


def person(user):
    return api.Principal("user", -1, user)


@pytest.fixture
def env(tmp_path):
    cab = CabinetEnv(tmp_path)
    people = users.Users(cab.store)
    people.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    people.create(
        "bob", "Боб", roles=[("partner", ["web", "digipaddock"])], actor="user:owner"
    )
    cab.ctx.llm_env_dir = str(tmp_path / "llm")
    cab.ctx.repo_root = str(
        REPO
    )  # the worker pools a code council's critics read in
    _bind(cab, "web", "digipaddock")
    api.dispatch(cab.ctx, person("owner"), MODEL)
    return cab


def _bind(env, *projects):
    """The repository registry (root's file): these projects are bound to a repository."""
    registry = {name: {"repo": f"acme/{name}", "dir": name} for name in projects}
    with open(env.ctx.repos_path, "w", encoding="utf-8") as handle:
        json.dump(registry, handle)


def call(env, user, request):
    return api.dispatch(env.ctx, person(user), request)


def refused(env, user, request):
    with pytest.raises(api.ApiError) as exc:
        call(env, user, request)
    return exc.value


def edit(env, user, card, changes):
    card = env.store.card(card.id)
    return call(
        env,
        user,
        {
            "op": "user.card.edit",
            "card": card.id,
            "revision": card.revision,
            "changes": changes,
        },
    )


# --- the limits and the normal form --------------------------------------------------------------------------
def test_the_limits_are_the_catalogs():
    assert council.limits() == {
        "critics_max": 10,
        "rounds_max": 5,
        "rounds_default": 3,
        "min_lines_default": 100,
        "min_lines_max": 100000,
        "jobs_per_day": 60,
        "head_wait_minutes": 20,
        "stalled_hours": 1,
    }


def test_check_fills_the_defaults_and_keeps_a_repeat(env):
    assert councils.check(
        env.store, {"brief": {"critics": ["claude:opus", "or-gpt5"]}}
    ) == {"brief": BRIEF}
    twice = {
        "code": {"critics": ["claude:opus", "claude:opus"], "rounds": 5, "min_lines": 0}
    }
    assert (
        councils.check(env.store, twice) == twice
    )  # two independent reads by one model; 0 — every PR
    assert councils.check(env.store, {}) == {}
    assert councils.check(env.store, {"brief": {"critics": []}}) == {
        "brief": {"critics": [], "rounds": 3}
    }
    assert councils.check(env.store, {"code": {"critics": ["or-gpt5"]}}) == {
        "code": {"critics": ["or-gpt5"], "rounds": 3, "min_lines": 100}
    }


@pytest.mark.parametrize(
    "bad",
    [
        {"brief": {"critics": ["claude:opus"] * 11}},
        {"brief": {"critics": ["claude:opus"], "rounds": 0}},
        {"brief": {"critics": ["claude:opus"], "rounds": 6}},
        {"brief": {"critics": ["claude:opus"], "rounds": True}},
        {"code": {"critics": ["claude:opus"], "min_lines": -1}},
        {"code": {"critics": ["claude:opus"], "min_lines": True}},
        {"code": {"critics": ["claude:opus"], "min_lines": 100001}},
        {"brief": {"critics": ["claude:opus"], "min_lines": 10}},
        {"plan": {"critics": ["claude:opus"]}},
        {"brief": {"critics": "claude:opus"}},
        {"brief": {"critics": ["claude:nope"]}},
        {"brief": {"critics": ["nope"]}},
        {"brief": {"critics": [""]}},
        {"brief": {"critics": [7]}},
        {"brief": {"rounds": 3}},
        {"brief": {"critics": ["claude:opus"], "extra": 1}},
        {"brief": ["claude:opus"]},
        ["claude:opus"],
        None,
    ],
    ids=repr,
)
def test_check_refuses_a_bad_council(env, bad):
    with pytest.raises(api.ApiError) as exc:
        councils.check(env.store, bad)
    assert exc.value.kind == "invalid"


def test_a_disabled_model_is_no_new_critic(env):
    call(env, "owner", {"op": "llm.update", "id": "or-gpt5", "enabled": 0})
    with pytest.raises(api.ApiError) as exc:
        councils.check(env.store, {"brief": {"critics": ["or-gpt5"]}})
    assert exc.value.kind == "invalid" and "disabled" in str(exc.value)


# --- who sets it ---------------------------------------------------------------------------------------------
def test_the_owner_sets_the_council_of_a_card_and_of_a_project(env):
    card = env.card()
    assert (
        call(env, "owner", {"op": "card.get", "card": card.id})["card"]["council"] == {}
    )
    done = edit(
        env,
        "owner",
        card,
        {"council": {"brief": {"critics": ["claude:opus", "or-gpt5"]}}},
    )
    assert done["card"]["council"] == {"brief": BRIEF}
    # a partner reads the composition and may not change it: it is a decision about spend
    assert call(env, "bob", {"op": "card.get", "card": card.id})["card"]["council"] == {
        "brief": BRIEF
    }
    assert (
        refused(
            env,
            "bob",
            {
                "op": "user.card.edit",
                "card": card.id,
                "revision": env.store.card(card.id).revision,
                "changes": {"council": {}},
            },
        ).kind
        == "refused"
    )
    assert env.store.card(card.id).council == {"brief": BRIEF}
    project = call(
        env,
        "owner",
        {"op": "project.update", "id": "digipaddock", "council": {"code": CODE}},
    )["project"]
    assert project["council"] == {"code": CODE}
    listed = {p["id"]: p for p in call(env, "bob", {"op": "project.list"})["items"]}
    assert (
        listed["digipaddock"]["council"] == {"code": CODE}
        and listed["web"]["council"] == {}
    )
    bad = refused(
        env,
        "owner",
        {
            "op": "project.update",
            "id": "digipaddock",
            "council": {"code": {"critics": [7]}},
        },
    )
    assert bad.kind == "invalid" and "council" in str(bad)
    assert (
        refused(
            env, "bob", {"op": "project.update", "id": "digipaddock", "council": {}}
        ).kind
        == "refused"
    )
    # a project has no «explicitly none»: a kind without critics is simply not there
    cleared = call(
        env,
        "owner",
        {
            "op": "project.update",
            "id": "digipaddock",
            "council": {"code": {"critics": []}},
        },
    )["project"]
    assert cleared["council"] == {}


def test_a_council_change_is_a_card_event_and_a_cleared_one_is_empty(env):
    card = env.card()
    edit(env, "owner", card, {"council": {"brief": BRIEF}})
    event = [e for e in env.store.events_since(0, 1000) if e["kind"] == "card.changed"][
        -1
    ]
    assert event["payload"]["fields"] == ["council"]
    assert edit(env, "owner", card, {"council": {}})["card"]["council"] == {}
    assert (
        env.store._conn.execute(
            "SELECT council FROM cards WHERE id=?", (card.id,)
        ).fetchone()[0]
        == ""
    )


def test_an_agent_cannot_set_the_council(env):
    card = env.card(status="In Progress")
    with pytest.raises(api.ApiError) as exc:
        env.call(
            "digi",
            {
                "op": "worker.edit",
                "agent": "dev",
                "card": card.id,
                "council": {"brief": BRIEF},
            },
        )
    assert exc.value.kind == "refused" and "council" in str(
        exc.value
    )  # the field itself is not an agent's
    assert env.store.card(card.id).council == {}


# --- the effective composition -------------------------------------------------------------------------------
def test_the_card_council_overrides_the_project_by_kind(env):
    call(
        env,
        "owner",
        {
            "op": "project.update",
            "id": "digipaddock",
            "council": {"brief": BRIEF, "code": CODE},
        },
    )
    card = env.card()
    assert councils.effective(env.store, env.store.card(card.id)) == {
        "brief": BRIEF,
        "code": CODE,
    }
    # an empty list of critics on the card is «no council» of that kind, over the project's
    edit(env, "owner", card, {"council": {"brief": {"critics": []}}})
    assert councils.effective(env.store, env.store.card(card.id)) == {"code": CODE}
    own = {"critics": ["or-gpt5"], "rounds": 2}
    edit(env, "owner", card, {"council": {"brief": own}})
    assert councils.effective(env.store, env.store.card(card.id)) == {
        "brief": own,
        "code": CODE,
    }
    plain = env.card(project="web")
    assert councils.effective(env.store, env.store.card(plain.id)) == {}


def test_the_line_names_the_critics_rounds_and_threshold():
    assert council.line({"brief": BRIEF, "code": CODE}) == (
        "brief claude:opus, or-gpt5 ×3 · code claude:opus ×3 ≥100 lines"
    )
    assert (
        council.line({"code": {"critics": ["or-gpt5"], "rounds": 1, "min_lines": 0}})
        == "code or-gpt5 ×1"
    )
    assert council.line({}) == ""


# --- a model a council names ---------------------------------------------------------------------------------
def test_a_model_a_council_names_is_not_deleted(env):
    card = env.card()
    edit(env, "owner", card, {"council": {"brief": BRIEF}})
    assert env.store.llm_model_refs("or-gpt5") == {"cards": [card.id], "projects": []}
    error = refused(env, "owner", {"op": "llm.delete", "id": "or-gpt5"})
    assert error.kind == "refused" and error.reason == "model_referenced"
    edit(env, "owner", card, {"council": {}})
    call(
        env,
        "owner",
        {
            "op": "project.update",
            "id": "web",
            "council": {"code": {"critics": ["or-gpt5"]}},
        },
    )
    assert env.store.llm_model_refs("or-gpt5") == {"cards": [], "projects": ["web"]}
    assert (
        refused(env, "owner", {"op": "llm.delete", "id": "or-gpt5"}).reason
        == "model_referenced"
    )
    call(env, "owner", {"op": "project.update", "id": "web", "council": {}})
    assert (
        call(env, "owner", {"op": "llm.delete", "id": "or-gpt5"})["deleted"]
        == "or-gpt5"
    )


def test_a_model_named_as_a_cards_model_and_critic_is_listed_once(env):
    card = env.card()
    edit(env, "owner", card, {"model": "or-gpt5", "council": {"brief": BRIEF}})
    assert env.store.llm_model_refs("or-gpt5") == {"cards": [card.id], "projects": []}
    assert env.store.llm_model_refs("gpt5") == {
        "cards": [],
        "projects": [],
    }  # a part of a name is no reference


# --- the schema ----------------------------------------------------------------------------------------------
def test_schema_40_adds_the_council(tmp_path):
    path = str(tmp_path / "v39.db")
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(schema.DDL + "".join(schema.MIGRATIONS[v] for v in range(2, 40)))
    conn.execute("INSERT INTO meta VALUES ('schema_version', '39')")
    conn.execute("INSERT INTO meta VALUES ('next_card', '1')")
    conn.execute(
        "INSERT INTO projects (id, name, route, routed) VALUES ('shop', 'Shop', '@dev', '')"
    )
    conn.close()
    db = store.Store.open(path)
    assert db.schema_version() == schema.SCHEMA_VERSION >= 40
    assert db.project("shop")["council"] == {}
    assert "council" in {r[1] for r in db._conn.execute("PRAGMA table_info(cards)")}
    tables = {
        r[0]
        for r in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"councils", "council_runs"} <= tables
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        db._conn.execute(
            "INSERT INTO councils (id, card, kind, rounds_max, state, created, updated)"
            " VALUES ('c1', 'KD-1', 'plan', 3, 'running', '', '')"
        )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        db._conn.execute(
            "INSERT INTO council_runs (council, round, n, model, state, created)"
            " VALUES ('c1', 1, 1, 'claude:opus', 'lost', '')"
        )


# --- a critic the cabinet no longer offers ---------------------------------------------------------------------
def test_a_critic_disabled_since_stays_and_blocks_no_other_edit(env):
    """A model disabled after a council named it stays its critic: the rest of the composition is edited — on the
    project and on a card — and only a new disabled critic is refused."""
    card, other = env.card(), env.card()
    call(
        env,
        "owner",
        {
            "op": "project.update",
            "id": "digipaddock",
            "council": {"code": {"critics": ["or-gpt5"]}},
        },
    )
    edit(env, "owner", card, {"council": {"brief": {"critics": ["or-gpt5"]}}})
    call(env, "owner", {"op": "llm.update", "id": "or-gpt5", "enabled": 0})
    wanted = {
        "code": {"critics": ["or-gpt5"], "rounds": 2},
        "brief": {"critics": ["claude:opus"]},
    }
    project = call(
        env, "owner", {"op": "project.update", "id": "digipaddock", "council": wanted}
    )["project"]
    assert project["council"] == {
        "brief": {"critics": ["claude:opus"], "rounds": 3},
        "code": {"critics": ["or-gpt5"], "rounds": 2, "min_lines": 100},
    }
    done = edit(
        env,
        "owner",
        card,
        {"council": {"brief": {"critics": ["or-gpt5", "claude:opus"], "rounds": 4}}},
    )
    assert done["card"]["council"] == {
        "brief": {"critics": ["or-gpt5", "claude:opus"], "rounds": 4}
    }
    # not a new one: neither on a card nor on a project that never named it
    fresh = {"council": {"brief": {"critics": ["or-gpt5"]}}}
    error = refused(
        env,
        "owner",
        {
            "op": "user.card.edit",
            "card": other.id,
            "revision": env.store.card(other.id).revision,
            "changes": fresh,
        },
    )
    assert error.kind == "invalid" and "disabled" in str(error)
    assert (
        refused(env, "owner", {"op": "project.update", "id": "web", **fresh}).kind
        == "invalid"
    )


def test_a_critic_gone_from_the_catalog_stays_until_removed(env):
    """A council kept the reference of an alias a later catalog dropped: it is still read and edited around; the
    reference is not taken anew."""
    card = env.card()
    env.store._conn.execute(
        "UPDATE cards SET council=? WHERE id=?",
        ('{"brief": {"critics": ["claude:old"], "rounds": 3}}', card.id),
    )
    assert councils.effective(env.store, env.store.card(card.id)) == {
        "brief": {"critics": ["claude:old"], "rounds": 3}
    }
    done = edit(
        env,
        "owner",
        card,
        {"council": {"brief": {"critics": ["claude:old", "claude:opus"]}}},
    )
    assert done["card"]["council"]["brief"]["critics"] == ["claude:old", "claude:opus"]
    edit(env, "owner", card, {"council": {"brief": {"critics": ["claude:opus"]}}})
    back = {"council": {"brief": {"critics": ["claude:old"]}}}
    assert (
        refused(
            env,
            "owner",
            {
                "op": "user.card.edit",
                "card": card.id,
                "revision": env.store.card(card.id).revision,
                "changes": back,
            },
        ).kind
        == "invalid"
    )


# --- who may have a code council --------------------------------------------------------------------
CODE_COUNCIL = {"code": {"critics": ["claude:opus"]}}


def test_a_code_council_needs_a_repository(env):
    card = env.card(project="web")
    _bind(env)

    why = refused(
        env,
        "owner",
        {
            "op": "user.card.edit",
            "card": card.id,
            "revision": card.revision,
            "changes": {"council": CODE_COUNCIL},
        },
    )

    assert (
        why.kind == "invalid"
        and "council: code: project web has no repository" in str(why)
    )
    _bind(env, "web")
    assert edit(env, "owner", card, {"council": CODE_COUNCIL})["card"]["council"][
        "code"
    ]["critics"] == ["claude:opus"]


def test_a_code_council_needs_developer_slots(env, tmp_path):
    card = env.card(project="web")
    _bind(env, "web")
    tree = tmp_path / "tree"
    (tree / "common/worker").mkdir(parents=True)
    slots = json.loads((REPO / "common/worker/worker-slots.json").read_text("utf-8"))
    slots["agents"] = {
        name: value for name, value in slots["agents"].items() if name != "dev"
    }
    (tree / "common/worker/worker-slots.json").write_text(json.dumps(slots), "utf-8")
    env.ctx.repo_root = str(tree)

    why = refused(
        env,
        "owner",
        {
            "op": "user.card.edit",
            "card": card.id,
            "revision": card.revision,
            "changes": {"council": CODE_COUNCIL},
        },
    )

    assert (
        why.kind == "invalid"
        and "council: code: the developer has no worker slots" in str(why)
    )


def test_a_brief_council_needs_neither(env):
    card = env.card(project="web")
    _bind(env)

    out = edit(env, "owner", card, {"council": {"brief": {"critics": ["claude:opus"]}}})

    assert out["card"]["council"]["brief"]["critics"] == ["claude:opus"]


def test_a_code_council_without_critics_needs_no_repository(env):
    card = env.card(project="web")
    _bind(env)

    out = edit(
        env, "owner", card, {"council": {"code": {"critics": []}}}
    )  # «no code council on this card»

    assert out["card"]["council"]["code"]["critics"] == []


def test_a_stored_code_council_is_kept_when_the_repository_is_gone(env):
    card = env.card(project="web")
    edit(env, "owner", card, {"council": CODE_COUNCIL})
    _bind(env)  # the project lost its repository since

    both = {**CODE_COUNCIL, "brief": {"critics": ["claude:opus"]}}
    out = edit(env, "owner", card, {"council": both})

    assert out["card"]["council"]["code"]["critics"] == ["claude:opus"]


def test_a_projects_code_council_needs_its_repository(env):
    _bind(env)
    why = refused(
        env, "owner", {"op": "project.update", "id": "web", "council": CODE_COUNCIL}
    )

    assert why.kind == "invalid" and "project web has no repository" in str(why)
    _bind(env, "web")
    out = call(
        env, "owner", {"op": "project.update", "id": "web", "council": CODE_COUNCIL}
    )
    assert out["project"]["council"]["code"]["critics"] == ["claude:opus"]
