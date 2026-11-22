"""the owner's projects — create, rename, reroute, delete with the cards moved in one transaction."""

import json

import pytest

from kd import delegation
from kd.cabinet import api, decisions, projects_api, store, users
from cabinet_env import CabinetEnv

GRANT = "a" * 32


def person(user):
    return api.Principal("user", -1, user)


@pytest.fixture
def env(tmp_path):
    cab = CabinetEnv(tmp_path)
    people = users.Users(cab.store)
    people.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    people.create("anna", "Анна", roles=[("partner", ["web"])], actor="user:owner")
    people.create("bob", "Боб", roles=[("partner", ["web", "digipaddock"]), ("viewer", ["web"])], actor="user:owner")
    cab.ctx.repos_path = str(tmp_path / "repos.json")
    return cab


def call(env, user, request):
    return api.dispatch(env.ctx, person(user), request)


def refused(env, user, request):
    with pytest.raises(api.ApiError) as exc:
        call(env, user, request)
    return exc.value


# --- create and update (AC4, AC2) ----------------------------------------------------------------------------
@pytest.mark.parametrize("name, want", [
    ("DIGIPADDOCK", "digipaddock"), ("Сайт и лендинги", "sayt-i-lendingi"), ("Example Servers of the platform 2", "example-servers-of-the-platform"),
    ("2026 бюджет", "byudzhet"), ("Alert.Examples.Co", "alert-examples-co"), ("Объём «Ёжик»", "obem-ezhik"),
])  # the web's lib/slug.test.ts checks the same names
def test_the_id_comes_from_the_name(name, want):
    assert projects_api.slug(name) == want and projects_api.ID.fullmatch(want)


def test_a_created_project_is_a_project_at_once(env):
    made = call(env, "owner", {"op": "project.create", "name": "Сайт", "route": "@dev"})["project"]
    assert (made["id"], made["name"], made["route"]) == ("sayt", "Сайт", "@dev")
    # a person's card in it goes to its route; the agents' executor takes it too — no restart
    card = call(env, "owner", {"op": "user.card.create", "title": "Форма", "project": "sayt"})["card"]
    assert card["assignee"] == "@dev"
    created = env.call("digi", {"op": "worker.create", "agent": "dev", "title": "Тесты формы", "project": "sayt"})
    assert created["card"]["project"] == "sayt"
    assert "sayt" in env.call("digi", {"op": "project.list"})["projects"]


@pytest.mark.parametrize("request_, why", [
    ({"name": "Сайт", "route": "@dev", "id": "Site"}, "id"),
    ({"name": "Сайт", "route": "@dev", "id": "a" * 33}, "id"),
    ({"name": "123", "route": "@dev"}, "id"),
    ({"name": "  ", "route": "@dev"}, "name"),
    ({"name": "x" * 61, "route": "@dev"}, "name"),
    ({"name": "Сайт", "route": "@ghost"}, "route"),
    ({"name": "Сайт", "route": "@boss"}, "route"),
    ({"name": "Сайт", "route": "dev"}, "route"),
    ({"name": "DIGIPADDOCK", "route": "@dev"}, "exists"),
])
def test_create_refuses_a_bad_id_name_route_or_a_duplicate(env, request_, why):
    error = refused(env, "owner", {"op": "project.create", **request_})
    assert error.kind == "invalid" and why in str(error)


def test_only_settings_manages_projects_and_a_person_sees_only_theirs(env):
    for request in ({"op": "project.create", "name": "Сайт", "route": "@dev"},
                    {"op": "project.update", "id": "web", "name": "Сайт"},
                    {"op": "project.delete", "id": "web"}):
        error = refused(env, "anna", request)
        assert error.kind == "refused" and "settings" in str(error)
        with pytest.raises(api.ApiError):  # an agent's executor never manages projects
            env.call("digi", {**request, "agent": "dev"})
    assert [p["id"] for p in call(env, "anna", {"op": "project.list"})["items"]] == ["web"]


def test_an_id_never_changes(env):
    error = refused(env, "owner", {"op": "project.update", "id": "web", "new_id": "site"})
    assert error.kind == "invalid" and "never changes" in str(error)
    assert refused(env, "owner", {"op": "project.update", "id": "web"}).kind == "invalid"
    assert refused(env, "owner", {"op": "project.update", "id": "nope", "name": "x"}).kind == "not_found"
    renamed = call(env, "owner", {"op": "project.update", "id": "web", "name": "Сайт"})["project"]
    assert (renamed["id"], renamed["name"]) == ("web", "Сайт")


# --- the project's memory bank ------------------------------------------
def test_update_sets_and_clears_the_memory_bank(env):
    assert env.store.project("web")["memory_bank"] == ""
    bound = call(env, "owner", {"op": "project.update", "id": "web", "memory_bank": "coding-agent::_Shop.v2"})
    assert bound["project"]["memory_bank"] == "coding-agent::_Shop.v2"
    listed = {p["id"]: p for p in call(env, "owner", {"op": "project.list"})["items"]}
    assert listed["web"]["memory_bank"] == "coding-agent::_Shop.v2"
    call(env, "owner", {"op": "project.update", "id": "web", "memory_bank": ""})
    assert env.store.project("web")["memory_bank"] == ""
    changes = [e["payload"] for e in env.store.events_since(0) if e["kind"] == "project.changed"]
    assert changes == [
        {"project": "web", "memory_bank": {"from": "", "to": "coding-agent::_Shop.v2"}},
        {"project": "web", "memory_bank": {"from": "coding-agent::_Shop.v2", "to": ""}},
    ]


@pytest.mark.parametrize("bank", ["a/b", ".x", "-x", "a b", "x" * 101, 7, None])
def test_update_refuses_a_bad_memory_bank(env, bank):
    error = refused(env, "owner", {"op": "project.update", "id": "web", "memory_bank": bank})
    assert error.kind == "invalid" and "memory bank" in str(error)
    assert env.store.project("web")["memory_bank"] == ""


def test_schema_32_gives_every_project_an_empty_memory_bank(tmp_path):
    import sqlite3

    from kd.cabinet import schema

    path = str(tmp_path / "v31.db")
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(schema.DDL + "".join(schema.MIGRATIONS[v] for v in range(2, 32)))
    conn.execute("INSERT INTO meta VALUES ('schema_version', '31')")
    conn.execute("INSERT INTO meta VALUES ('next_card', '1')")
    conn.execute("INSERT INTO projects (id, name, route, routed) VALUES ('shop', 'Shop', '@dev', '')")
    conn.close()
    db = store.Store.open(path)
    assert db.schema_version() == schema.SCHEMA_VERSION  # 32 and every later step
    assert db.project("shop")["memory_bank"] == ""


def test_only_settings_binds_a_memory_bank(env):
    assert refused(env, "anna", {"op": "project.update", "id": "web", "memory_bank": "b"}).kind == "refused"


# --- who closes the project's cards Done ------------------------------------------------------------
def test_update_sets_and_clears_who_closes_done(env):
    assert env.store.project("web")["done_by"] == ""
    bound = call(env, "owner", {"op": "project.update", "id": "web", "done_by": "dev"})
    assert bound["project"]["done_by"] == "dev"
    call(env, "owner", {"op": "project.update", "id": "web", "done_by": ""})
    assert env.store.project("web")["done_by"] == ""
    changes = [e["payload"] for e in env.store.events_since(0) if e["kind"] == "project.changed"]
    assert changes == [
        {"project": "web", "done_by": {"from": "", "to": "dev"}},
        {"project": "web", "done_by": {"from": "dev", "to": ""}},
    ]


@pytest.mark.parametrize("agent", ["ghost", "@dev", "boss", "owner", 7, None])
def test_update_refuses_an_unknown_closer(env, agent):
    error = refused(env, "owner", {"op": "project.update", "id": "web", "done_by": agent})
    assert error.kind == "invalid" and "done_by" in str(error)
    assert env.store.project("web")["done_by"] == ""


def test_schema_33_gives_every_project_the_owners_acceptance(tmp_path):
    import sqlite3

    from kd.cabinet import schema

    path = str(tmp_path / "v32.db")
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(schema.DDL + "".join(schema.MIGRATIONS[v] for v in range(2, 33)))
    conn.execute("INSERT INTO meta VALUES ('schema_version', '32')")
    conn.execute("INSERT INTO meta VALUES ('next_card', '1')")
    conn.execute("INSERT INTO projects (id, name, route, routed) VALUES ('shop', 'Shop', '@dev', '')")
    conn.close()
    db = store.Store.open(path)
    assert db.schema_version() == schema.SCHEMA_VERSION  # 33 and every later step
    assert db.project("shop")["done_by"] == ""


def test_a_partner_hears_a_closer_change_without_the_closer(env):
    call(env, "owner", {"op": "project.update", "id": "web", "done_by": "dev"})
    events = [e for e in env.store.events_since(0) if e["kind"] == "project.changed"]
    [partner] = api.event_filter(env.ctx, person("anna"))(events)
    [owner] = api.event_filter(env.ctx, person("owner"))(events)
    assert partner["payload"] == {"project": "web"}
    assert owner["payload"]["done_by"] == {"from": "", "to": "dev"}


# --- the project's default model and effort ---------------------------------------------------------
MODEL = {"op": "llm.create", "id": "or-gpt5", "provider": "openrouter", "upstream": "openai/gpt-5", "base_url": "",
         "title": "GPT-5", "price_in": 1.25, "price_out": 10, "price_cache_read": 0.125, "price_cache_write": 1.25,
         "context": 400000}


def test_update_sets_and_clears_the_model_and_effort(env):
    call(env, "owner", MODEL)
    assert env.store.project("web")["model"] == "" and env.store.project("web")["effort"] == ""
    bound = call(env, "owner", {"op": "project.update", "id": "web", "model": "or-gpt5", "effort": "low"})["project"]
    assert (bound["model"], bound["effort"]) == ("or-gpt5", "low")
    listed = {p["id"]: p for p in call(env, "owner", {"op": "project.list"})["items"]}
    assert (listed["web"]["model"], listed["web"]["effort"]) == ("or-gpt5", "low")
    call(env, "owner", {"op": "project.update", "id": "web", "model": "", "effort": ""})
    assert env.store.project("web")["model"] == "" and env.store.project("web")["effort"] == ""
    changes = [e["payload"] for e in env.store.events_since(0) if e["kind"] == "project.changed"]
    assert changes == [
        {"project": "web", "model": {"from": "", "to": "or-gpt5"}, "effort": {"from": "", "to": "low"}},
        {"project": "web", "model": {"from": "or-gpt5", "to": ""}, "effort": {"from": "low", "to": ""}},
    ]
    events = [e for e in env.store.events_since(0) if e["kind"] == "project.changed"]
    assert [e["payload"] for e in api.event_filter(env.ctx, person("anna"))(events)] == [{"project": "web"}] * 2


@pytest.mark.parametrize("bad", [{"model": "nope"}, {"effort": "ultra"}, {"model": 7}, {"effort": None}])
def test_update_refuses_an_unknown_model_or_effort(env, bad):
    call(env, "owner", MODEL)
    error = refused(env, "owner", {"op": "project.update", "id": "web", **bad})
    assert error.kind == "invalid" and next(iter(bad)) in str(error)
    assert env.store.project("web")["model"] == "" and env.store.project("web")["effort"] == ""


def test_update_refuses_a_disabled_model(env):
    call(env, "owner", MODEL)
    call(env, "owner", {"op": "llm.update", "id": "or-gpt5", "enabled": 0})
    error = refused(env, "owner", {"op": "project.update", "id": "web", "model": "or-gpt5"})
    assert error.kind == "invalid" and "disabled" in str(error)
    assert refused(env, "anna", {"op": "project.update", "id": "web", "effort": "low"}).kind == "refused"


# --- a new route counts for new cards (AC5) --------------------------------------------------------------------
def test_a_new_route_holds_for_new_cards_only(env):
    old = env.card(project="digipaddock", status="To Do", assignee="@dev")
    call(env, "owner", {"op": "project.update", "id": "digipaddock", "route": "@sysadmin"})
    assert env.store.card(old.id).assignee == "@dev"  # an open card keeps its assignee
    new = call(env, "owner", {"op": "user.card.create", "title": "Новая", "project": "digipaddock"})["card"]
    assert new["assignee"] == "@sysadmin"


# --- delete (AC6) ----------------------------------------------------------------------------------------------
def _busy_project(env):
    """Two cards of web, one with a file, an open question and a PR; bob holds web in two roles."""
    first = env.card(project="web", status="In Progress", assignee="@dev", title="Первая")
    second = env.card(project="web", status="Done", assignee="@dev", title="Вторая")
    env.store.add_message(first.id, author="user:owner", kind="text", body="обсуждение")
    env.store.add_file(first.id, author="user:owner", file={
        "id": "f" * 32, "name": "a.txt", "mime": "text/plain", "size": 1, "sha256": "0" * 64})
    asked = env.call("digi", {"op": "decision.open", "type": "question", "agent": "dev", "card": first.id,
                             "payload": {"text": "Какой домен?"}})["decision"]
    env.store.pr_put({"repo": "owner/repo", "project": "web", "number": 7, "card": first.id, "title": "x",
                      "url": "https://forge.example/owner/repo/pulls/7", "head": "1" * 40, "state": "open",
                      "files": [], "cage": [], "diff": "", "truncated": False, "release": False}, actor="forge")
    return first, second, asked


def test_a_project_with_cards_needs_a_target(env):
    _busy_project(env)
    error = refused(env, "owner", {"op": "project.delete", "id": "web"})
    assert error.kind == "invalid" and "2 cards" in str(error)
    assert refused(env, "owner", {"op": "project.delete", "id": "web", "target": "web"}).kind == "invalid"
    assert refused(env, "owner", {"op": "project.delete", "id": "web", "target": "nope"}).kind == "not_found"
    assert env.store.project("web") is not None


def test_delete_moves_every_card_with_its_thread_files_history_and_prs(env, tmp_path):
    first, second, asked = _busy_project(env)
    before = {c.id: (len(env.store.messages(c.id)), len(env.store.card_events(c.id))) for c in (first, second)}
    grants = tmp_path / "promote" / "grants"
    delegation.write_grant(str(grants), delegation.Grant(
        id=GRANT, goal="KD-99", projects={"web": "@dev", "digipaddock": "@dev"}, max_children=3, max_active=1,
        created=0.0, expires=4e9, state=delegation.ACTIVE, approved_by="owner"))
    out = call(env, "owner", {"op": "project.delete", "id": "web", "target": "digipaddock"})
    assert (out["moved"], out["withdrawn"], out["users"], out["grants"]) == (2, 1, ["anna", "bob"], [GRANT])
    assert env.store.project("web") is None
    for card in (first, second):
        moved = env.store.card(card.id)
        assert (moved.project, moved.assignee) == ("digipaddock", "@dev")
        messages, events = before[card.id]
        thread = env.store.messages(card.id)
        assert len(thread) > messages and len(env.store.card_events(card.id)) > events
        assert any("проект web удалён — карточка перенесена в digipaddock" in m["body"] for m in thread)
    assert "открытые решения отменены: 1" in env.store.messages(first.id)[-1]["body"]
    assert env.store.attachment("f" * 32)["card"] == first.id
    assert env.store.pr("owner/repo", 7)["project"] == "digipaddock"
    assert decisions.Decisions(env.store).get(asked["id"])["state"] == "revoked"
    assert env.users.assignments("anna") == []  # her only project is gone: the role goes with it
    assert env.users.assignments("bob") == [("partner", ["digipaddock"])]
    assert delegation.load_grant(str(grants), GRANT).projects == {"digipaddock": "@dev"}
    (event,) = [e for e in env.store.events_since(0) if e["kind"] == "project.deleted"]
    assert event["payload"] == {"project": "web", "target": "digipaddock", "cards": 2, "users": ["anna", "bob"]}


def test_an_empty_project_goes_without_a_target(env):
    out = call(env, "owner", {"op": "project.delete", "id": "alert"})
    assert out["moved"] == 0 and env.store.project("alert") is None


def test_a_project_bound_to_a_repository_stays(env, tmp_path):
    (tmp_path / "repos.json").write_text(json.dumps({"web": {"repo": "o/r", "dir": "work/R"}}), encoding="utf-8")
    error = refused(env, "owner", {"op": "project.delete", "id": "web"})
    assert error.kind == "refused" and error.reason == "project_bound"
    (tmp_path / "repos.json").write_text("{not json", encoding="utf-8")
    assert refused(env, "owner", {"op": "project.delete", "id": "web"}).kind == "unavailable"
    assert env.store.project("web") is not None


def test_a_failure_in_the_middle_loses_no_card(env, monkeypatch):
    first, second, asked = _busy_project(env)
    real, seen = env.store.edit_card, []

    def flaky(card_id, *args, **kwargs):
        seen.append(card_id)
        if len(seen) == 2:
            raise store.StoreError("disk full")
        return real(card_id, *args, **kwargs)

    monkeypatch.setattr(env.store, "edit_card", flaky)
    with pytest.raises(store.StoreError):
        call(env, "owner", {"op": "project.delete", "id": "web", "target": "digipaddock"})
    monkeypatch.undo()
    assert [env.store.card(c.id).project for c in (first, second)] == ["web", "web"]
    assert env.store.project("web") is not None and env.users.assignments("anna") == [("partner", ["web"])]
    assert decisions.Decisions(env.store).get(asked["id"])["state"] == "open"
    assert env.store.pr("owner/repo", 7)["project"] == "web"


# --- the Projects section ----------------------------------------------------------------------------
def test_the_delete_dialog_hears_the_cards_people_and_binding_first(env, tmp_path):
    _busy_project(env)
    impact = call(env, "owner", {"op": "project.impact", "id": "web"})
    assert {k: impact[k] for k in ("id", "cards", "users", "bound")} == {
        "id": "web", "cards": 2, "users": ["anna", "bob"], "bound": False}
    assert call(env, "owner", {"op": "project.impact", "id": "alert"})["cards"] == 0
    (tmp_path / "repos.json").write_text(json.dumps({"web": {"repo": "o/r"}}), encoding="utf-8")
    assert call(env, "owner", {"op": "project.impact", "id": "web"})["bound"] is True
    assert env.store.project("web") is not None  # a look changes nothing
    assert refused(env, "anna", {"op": "project.impact", "id": "web"}).kind == "refused"
    assert refused(env, "owner", {"op": "project.impact", "id": "nope"}).kind == "not_found"


def test_the_routes_a_project_may_take(env):
    routes = call(env, "owner", {"op": "project.routes"})["routes"]
    assert routes[0] == "@owner" and "@dev" in routes and "@sysadmin" in routes and "@boss" not in routes
    assert routes[1:] == sorted(routes[1:])
    for route in routes:  # every one of them is accepted by create
        assert projects_api._route(env.ctx, route) == route
    assert refused(env, "anna", {"op": "project.routes"}).kind == "refused"


def test_a_project_change_reaches_everyone_who_sees_the_project(env):
    call(env, "owner", {"op": "project.update", "id": "web", "name": "Сайт"})
    call(env, "owner", {"op": "project.update", "id": "digipaddock", "name": "Paddock"})
    call(env, "owner", {"op": "project.delete", "id": "alert", "target": "web"})
    events = [e for e in env.store.events_since(0) if e["kind"].startswith("project.")]
    seen = lambda user: [  # noqa: E731
        (e["kind"], e["payload"]["project"]) for e in api.event_filter(env.ctx, person(user))(events)
    ]
    assert seen("anna") == [("project.changed", "web"), ("project.deleted", "alert")]  # alert's cards came to web
    assert seen("bob") == [("project.changed", "web"), ("project.changed", "digipaddock"), ("project.deleted", "alert")]


def test_a_partner_hears_a_bank_change_without_the_bank(env):
    """a partner's screens refresh on the change, but which memory a project has stays the owner's."""
    call(env, "owner", {"op": "project.update", "id": "web", "memory_bank": "coding-agent::shop"})
    events = [e for e in env.store.events_since(0) if e["kind"] == "project.changed"]
    [partner] = api.event_filter(env.ctx, person("anna"))(events)
    [owner] = api.event_filter(env.ctx, person("owner"))(events)
    assert partner["payload"] == {"project": "web"}
    assert owner["payload"]["memory_bank"] == {"from": "", "to": "coding-agent::shop"}
    assert events[0]["payload"]["memory_bank"]  # the filter copies, the stored event keeps its record


def test_every_page_hears_a_project_change():
    """A card page subscribes to its card only, yet shows its project's name: project events reach it too."""
    from kd.cabinet.web import ws

    conn = ws.Connection.__new__(ws.Connection)
    conn.all, conn.cards, conn.agents = False, {"KD-1"}, set()
    assert conn.wants({"kind": "project.changed", "card": "", "payload": {"project": "web"}})
    assert not conn.wants({"kind": "card.changed", "card": "KD-2", "payload": {}})


def test_root_manages_projects_from_the_host(env):
    root = env.principal("root")
    made = api.dispatch(env.ctx, root, {"op": "project.create", "name": "Ops 2", "route": "@owner", "id": "ops2"})
    assert made["project"]["id"] == "ops2"
    assert api.dispatch(env.ctx, root, {"op": "project.delete", "id": "ops2"})["deleted"] == "ops2"
