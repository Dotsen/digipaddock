import json

import pytest

from kd.cabinet import api, briefs, users
from cabinet_env import CabinetEnv


def _env(tmp_path):
    env = CabinetEnv(tmp_path)
    u = users.Users(env.store)
    u.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    u.create("anna", "Анна", roles=[("partner", ["web"])], actor="user:owner")
    return env


def person(user):
    return api.Principal("user", -1, user)


def test_projects_are_the_cabinets_table_read_at_every_call(tmp_path):
    """the table is the only source — names come with it, a project added there is a project at once
    (create, its route, the person's view and user.me) without a restart; a person sees only their projects."""
    env = _env(tmp_path)
    items = api.dispatch(env.ctx, person("owner"), {"op": "project.list"})["items"]
    assert [(p["id"], p["name"], p["route"], p["repo"]) for p in items] == [
        ("admin", "ADMIN", "@sysadmin", ""), ("alert", "ALERT", "@owner", ""),
        ("digipaddock", "DIGIPADDOCK", "@dev", ""), ("web", "WEB", "@owner", ""),
    ]
    assert [p["id"] for p in api.dispatch(env.ctx, person("anna"), {"op": "project.list"})["items"]] == ["web"]
    with pytest.raises(api.ApiError) as exc:
        api.dispatch(env.ctx, person("owner"), {"op": "user.card.create", "title": "Форма", "project": "site"})
    assert exc.value.kind == "invalid"
    env.store.seed_projects([{"id": "site", "name": "Сайт", "route": "@sysadmin"}])
    card = api.dispatch(env.ctx, person("owner"), {"op": "user.card.create", "title": "Форма", "project": "site"})
    assert (card["card"]["project"], card["card"]["assignee"]) == ("site", "@sysadmin")
    assert "site" in api.dispatch(env.ctx, person("owner"), {"op": "user.me"})["projects"]
    assert "site" not in api.dispatch(env.ctx, person("anna"), {"op": "user.me"})["projects"]


def test_a_route_to_an_agent_the_registry_lacks_keeps_the_board_up(tmp_path):
    # one stale route never makes the cabinet unavailable: the other projects route as before
    env = _env(tmp_path)
    env.store.seed_projects([{"id": "old", "name": "Old", "route": "@retired"}])
    card = api.dispatch(env.ctx, person("owner"), {"op": "user.card.create", "title": "x", "project": "digipaddock"})
    assert card["card"]["assignee"] == "@dev"
    # ...but a gone agent is no target: not for the stale project's new cards, not for any card's assignee
    assert "@retired" not in api.dispatch(env.ctx, person("owner"), {"op": "assignee.list"})["assignees"]
    with pytest.raises(api.ApiError) as exc:
        api.dispatch(env.ctx, person("owner"), {"op": "user.card.create", "title": "y", "project": "old"})
    assert exc.value.kind == "invalid"
    with pytest.raises(api.ApiError) as exc:
        api.dispatch(env.ctx, person("owner"), {"op": "user.card.edit", "card": card["card"]["id"],
                                                "revision": card["card"]["revision"],
                                                "changes": {"assignee": "@retired"}})
    assert exc.value.kind == "invalid"


def test_owner_creates_moves_and_comments_a_card(tmp_path):
    env = _env(tmp_path)
    out = api.dispatch(
        env.ctx,
        person("owner"),
        {"op": "user.card.create", "title": "Nginx cache", "project": "digipaddock"},
    )
    card = out["card"]
    assert (
        card["status"] == "Inbox"
        and card["assignee"] == "@dev"
        and card["created_by"] == "@owner"
    )
    moved = api.dispatch(
        env.ctx,
        person("owner"),
        {
            "op": "user.card.edit",
            "card": card["id"],
            "revision": card["revision"],
            "changes": {"status": "To Do"},
            "override_reason": "сразу в работу",
        },
    )["card"]
    assert moved["status"] == "To Do" and moved["revision"] == card["revision"] + 1
    with pytest.raises(api.ApiError) as exc:
        api.dispatch(
            env.ctx,
            person("owner"),
            {
                "op": "user.card.edit",
                "card": card["id"],
                "revision": card["revision"],
                "changes": {"title": "x"},
            },
        )
    assert exc.value.kind == "conflict"
    msg = api.dispatch(
        env.ctx,
        person("owner"),
        {"op": "user.card.comment", "card": card["id"], "text": "Берём"},
    )
    assert msg["message"]["author"] == "user:owner" and msg["changed"]
    event = env.store.events_since(0)[-1]
    assert event["kind"] == "message.added" and event["actor"] == "user:owner"
    # a plain message on the agent's card is for that agent
    # (seq 1 is the override's own line)
    assert event["payload"] == {"seq": 2, "kind": "text", "title": "Nginx cache", "mentions": ["@dev"]}
    # the agents a message names, as whole words among the board's targets (sync wakes them)
    api.dispatch(
        env.ctx,
        person("owner"),
        {
            "op": "user.card.comment",
            "card": card["id"],
            "text": "@boss запусти @analyst, @dev-ops и @owner не трогать; mail@dev.io тоже",
        },
    )
    assert env.store.events_since(0)[-1]["payload"]["mentions"] == ["@analyst", "@boss"]


def test_owner_edits_every_field_and_moves_out_of_inbox_and_into_done(tmp_path):
    env = _env(tmp_path)
    env.ctx._specs = {"@dev": frozenset({"implementer"}), "@sysadmin": frozenset()}
    card = env.card(project="digipaddock", status="Inbox")
    out = api.dispatch(
        env.ctx,
        person("owner"),
        {
            "op": "user.card.edit",
            "card": card.id,
            "revision": card.revision,
            "override_reason": "сделано вне доски",
            "changes": {
                "title": "New title",
                "description": "Why",
                "acceptance": [{"text": "one", "checked": True}, {"text": "two"}],
                "dod": "d",
                "notes": "n",
                "plan": "p",
                "labels": ["a", "spec:implementer"],
                "priority": "high",
                "due": "2026-10-20",
                "assignee": "@sysadmin",
                "dependencies": ["KD-9"],
                "servers": ["node-c"],
                "parent": "KD-2",
                "status": "Done",
            },
        },
    )["card"]
    assert out["title"] == "New title" and out["acceptance"] == [
        {"text": "one", "checked": True},
        {"text": "two", "checked": False},
    ]
    # the new assignee does not hold spec:implementer: the label goes in the same write
    assert out["labels"] == ["a"] and out["servers"] == ["node-c"]
    assert out["status"] == "Done" and out["assignee"] == "@sysadmin"
    assert out["dependencies"] == ["KD-9"] and out["parent"] == "KD-2"
    event = env.store.events_since(0)[-1]
    assert event["kind"] == "card.changed" and "title" in event["payload"]["fields"]
    for bad in (
        {"status": "Nowhere"},
        {"title": ""},
        {"assignee": "@stranger"},
        {"acceptance": "x"},
        {"priority": "urgent"},
        {"dependencies": ["nope"]},
        {"secret": 1},
    ):
        with pytest.raises(api.ApiError) as exc:
            api.dispatch(
                env.ctx,
                person("owner"),
                {
                    "op": "user.card.edit",
                    "card": card.id,
                    "revision": out["revision"],
                    "changes": bad,
                },
            )
        assert exc.value.kind == "invalid", bad


def test_partner_sees_and_edits_only_her_projects(tmp_path):
    env = _env(tmp_path)
    digipaddock = env.card(project="digipaddock")
    ops = api.dispatch(
        env.ctx,
        person("anna"),
        {"op": "user.card.create", "title": "Счёт", "project": "web"},
    )["card"]
    assert ops["created_by"] == "@anna" and ops["assignee"] == "@owner"
    with pytest.raises(api.ApiError) as exc:
        api.dispatch(env.ctx, person("anna"), {"op": "card.get", "card": digipaddock.id})
    assert exc.value.kind == "not_found"
    with pytest.raises(api.ApiError) as exc:
        api.dispatch(
            env.ctx, person("anna"), {"op": "thread.messages", "card": digipaddock.id}
        )
    assert exc.value.kind == "not_found"
    ids = {
        c["id"]
        for c in api.dispatch(env.ctx, person("anna"), {"op": "card.list"})["cards"]
    }
    assert ids == {ops["id"]}
    found = api.dispatch(
        env.ctx, person("anna"), {"op": "card.search", "text": "Card"}
    )["cards"]
    assert found == []
    assert api.dispatch(env.ctx, person("anna"), {"op": "project.list"})[
        "projects"
    ] == {"web": "@owner"}
    with pytest.raises(api.ApiError) as exc:
        api.dispatch(
            env.ctx,
            person("anna"),
            {"op": "user.card.create", "title": "x", "project": "digipaddock"},
        )
    assert exc.value.kind == "refused"
    with pytest.raises(api.ApiError) as exc:  # no cards.transition
        api.dispatch(
            env.ctx,
            person("anna"),
            {
                "op": "user.card.edit",
                "card": ops["id"],
                "revision": ops["revision"],
                "changes": {"status": "To Do"},
            },
        )
    assert exc.value.kind == "refused"
    edited = api.dispatch(
        env.ctx,
        person("anna"),
        {
            "op": "user.card.edit",
            "card": ops["id"],
            "revision": ops["revision"],
            "changes": {"description": "Оплатить до пятницы"},
        },
    )["card"]
    assert edited["description"] == "Оплатить до пятницы"
    with pytest.raises(api.ApiError):  # someone else's card: no edit for a partner
        api.dispatch(
            env.ctx,
            person("anna"),
            {
                "op": "user.card.edit",
                "card": env.card(project="web").id,
                "revision": 1,
                "changes": {"title": "y"},
            },
        )
    with pytest.raises(api.ApiError) as exc:
        api.dispatch(
            env.ctx,
            person("anna"),
            {"op": "user.card.comment", "card": digipaddock.id, "text": "hi"},
        )
    assert exc.value.kind == "not_found"


def test_events_for_a_partner_are_filtered_by_project(tmp_path):
    env = _env(tmp_path)
    env.card(project="digipaddock")
    mine = env.card(project="web")
    events = api.dispatch(env.ctx, person("anna"), {"op": "events.since", "after": 0})[
        "events"
    ]
    assert {e["card"] for e in events if e["card"]} == {mine.id}
    assert all(
        e["card"] or e["payload"].get("user") == "anna" for e in events
    )  # user events: only her own
    everything = api.dispatch(
        env.ctx, person("owner"), {"op": "events.since", "after": 0}
    )["events"]
    assert len(everything) > len(events)


def test_files_are_messages_with_an_attachment_row(tmp_path):
    env = _env(tmp_path)
    card = env.card(project="web")
    file = {"id": "a" * 32, "name": "счёт.pdf", "mime": "application/pdf", "size": 1024, "sha256": "b" * 64}
    out = api.dispatch(env.ctx, person("anna"), {"op": "user.card.comment", "card": card.id, "files": [file]})
    assert out["message"]["kind"] == "file" and out["message"]["payload"] == {
        "files": [{"id": "a" * 32, "name": "счёт.pdf", "mime": "application/pdf", "size": 1024}]
    }
    assert env.store.attachment("a" * 32)["card"] == card.id
    with pytest.raises(api.ApiError):
        api.dispatch(
            env.ctx,
            person("anna"),
            {"op": "user.card.comment", "card": card.id, "files": [{**file, "id": "../x"}]},
        )


def test_a_person_takes_their_own_code_but_not_anothers(tmp_path):
    # the own code needs no users.manage (the web asks a step-up); another's still does
    env = _env(tmp_path)
    out = api.dispatch(env.ctx, person("anna"), {"op": "user.code", "id": "anna"})
    assert out["user"] == "anna" and out["code"] and out["expires"] == "2026-10-14T10:15:00Z"
    with pytest.raises(api.ApiError) as exc:
        api.dispatch(env.ctx, person("anna"), {"op": "user.code", "id": "owner"})
    assert exc.value.kind == "refused"
    with pytest.raises(api.ApiError) as exc:  # nobody learns who exists by asking
        api.dispatch(env.ctx, person("anna"), {"op": "user.code", "id": "ghost"})
    assert exc.value.kind == "refused"


def test_user_management_needs_the_permission(tmp_path):
    env = _env(tmp_path)
    with pytest.raises(api.ApiError):
        api.dispatch(
            env.ctx,
            person("anna"),
            {
                "op": "user.create",
                "id": "bob",
                "name": "Bob",
                "roles": [["viewer", "*"]],
            },
        )
    out = api.dispatch(
        env.ctx,
        person("owner"),
        {"op": "user.create", "id": "bob", "name": "Bob", "roles": [["viewer", "*"]]},
    )
    assert len(out["code"]) >= 10 and out["user"]["id"] == "bob"
    assert api.dispatch(
        env.ctx, api.Principal("root", 0, "root"), {"op": "user.code", "id": "bob"}
    )["code"]
    api.dispatch(env.ctx, person("owner"), {"op": "user.block", "id": "bob"})
    with pytest.raises(api.ApiError) as exc:
        api.dispatch(env.ctx, person("bob"), {"op": "card.list"})
    assert exc.value.kind == "refused"
    api.dispatch(
        env.ctx,
        person("owner"),
        {
            "op": "user.update",
            "id": "bob",
            "status": "active",
            "roles": [["partner", ["web"]]],
        },
    )
    assert api.dispatch(env.ctx, person("bob"), {"op": "user.me"})["projects"] == [
        "web"
    ]
    with pytest.raises(api.ApiError):  # roles are the settings permission
        api.dispatch(
            env.ctx,
            person("anna"),
            {"op": "role.set", "id": "x", "perms": ["cards.read"]},
        )
    api.dispatch(
        env.ctx, person("owner"), {"op": "role.set", "id": "x", "perms": ["cards.read"]}
    )
    with pytest.raises(api.ApiError) as exc:
        api.dispatch(
            env.ctx, person("owner"), {"op": "role.set", "id": "y", "perms": ["fly"]}
        )
    assert exc.value.kind == "invalid"


def test_me_reports_permissions_by_project(tmp_path):
    env = _env(tmp_path)
    me = api.dispatch(env.ctx, person("anna"), {"op": "user.me"})
    assert me["user"]["id"] == "anna" and me["projects"] == ["web"]
    assert "cards.create" in me["permissions"]["web"]
    owner = api.dispatch(env.ctx, person("owner"), {"op": "user.me"})
    assert owner["permissions"] == {"*": ["*"]}
    assert owner["projects"] == ["admin", "alert", "digipaddock", "web"]


def _kinds(env):
    return [(e["kind"], e["actor"], e["payload"]) for e in env.store.events_since(0)]


def test_owner_takes_a_card_into_work(tmp_path):
    """Inbox -> To Do under the project's route (sync wakes the agent on the status event); a To Do card
    gets a card.wake for its agent and nothing else changes; the viewer, the owner's own cards and the
    coordinator's are refused."""
    env = _env(tmp_path)
    card = env.card(project="digipaddock", status="Inbox", assignee="")
    out = api.dispatch(
        env.ctx,
        person("owner"),
        {"op": "user.card.start", "card": card.id, "revision": card.revision},
    )["card"]
    assert out["status"] == "To Do" and out["assignee"] == "@dev"
    assert env.store.messages(card.id)[-1]["body"] == "[owner] To Do: в работу"
    again = api.dispatch(
        env.ctx,
        person("owner"),
        {"op": "user.card.start", "card": card.id, "revision": out["revision"]},
    )
    assert again["card"]["revision"] == out["revision"] and again["changed"]
    kind, actor, payload = _kinds(env)[-1]
    assert kind == "card.wake" and actor == "user:owner"
    assert payload == {"agent": "@dev", "title": "Card"}
    for card_, revision, why in (
        (card.id, out["revision"] - 1, "conflict"),
        (env.card(project="web", status="Inbox", assignee="").id, 1, "refused"),  # routed to the owner
        (env.card(status="Inbox", assignee="@boss").id, 1, "refused"),
        (env.card(status="In Progress").id, 1, "refused"),
    ):
        with pytest.raises(api.ApiError) as exc:
            api.dispatch(
                env.ctx,
                person("owner"),
                {"op": "user.card.start", "card": card_, "revision": revision},
            )
        assert exc.value.kind == why, card_
    env.users.create("vera", "Вера", roles=[("viewer", ["digipaddock"])], actor="user:owner")
    with pytest.raises(api.ApiError) as exc:
        api.dispatch(
            env.ctx,
            person("vera"),
            {"op": "user.card.start", "card": card.id, "revision": out["revision"]},
        )
    assert exc.value.kind == "refused"


def test_owner_asks_the_analyst_for_a_brief(tmp_path):
    """The card goes to @analyst in Inbox with spec:task-brief (other spec labels and a stale brief-ready
    dropped), a comment says so and the analyst is woken; without an analyst on the host — unavailable."""
    env = _env(tmp_path)
    card = env.card(status="To Do", labels=["ui", "spec:implementer", "brief-ready"])
    out = api.dispatch(
        env.ctx,
        person("owner"),
        {"op": "user.card.brief", "card": card.id, "revision": card.revision, "comment": "Уточни сроки"},
    )["card"]  # an old brief-ready card is a legacy brief: its rework takes a comment
    assert out["status"] == "Inbox" and out["assignee"] == "@analyst"
    assert out["labels"] == ["ui", "spec:task-brief"]
    assert env.store.messages(card.id)[-1]["body"] == "[owner] ТЗ: на доработку — Уточни сроки"
    assert _kinds(env)[-1][0] == "card.wake" and _kinds(env)[-1][2]["agent"] == "@analyst"
    env.registry.write_text('{"dev": {"user": "digi-dev"}}', encoding="utf-8")
    env.ctx.refresh()
    with pytest.raises(api.ApiError) as exc:
        api.dispatch(
            env.ctx,
            person("owner"),
            {"op": "user.card.brief", "card": card.id, "revision": out["revision"]},
        )
    # no analyst registered: the action is not offered (a route to an agent the registry lacks no longer
    # makes the whole board unavailable)
    assert exc.value.kind == "refused"


def test_root_creates_and_edits_cards_from_the_host(tmp_path):
    """kd-cabinet create|edit: actor root, Inbox for the route, a person's field checks, a signed comment."""
    env = _env(tmp_path)
    card = env.call(
        "root", {"op": "card.create", "title": " Чистка доски ", "project": "digipaddock"}
    )["card"]
    assert card["title"] == "Чистка доски" and card["status"] == "Inbox"
    assert card["assignee"] == "@dev" and card["created_by"] == "root"
    assert _kinds(env)[-1][1] == "root"
    done = env.call(
        "root",
        {
            "op": "card.edit",
            "card": card["id"],
            "revision": card["revision"],
            "changes": {"status": "Done"},
            "comment": "закрыто",
        },
    )["card"]
    assert done["status"] == "Done"
    assert env.store.messages(card["id"])[-1]["author"] == "root"
    for request, why in (
        ({"op": "card.edit", "card": card["id"], "revision": done["revision"]}, "invalid"),
        ({"op": "card.edit", "card": card["id"], "revision": 1, "changes": {"status": "To Do"}}, "conflict"),
        ({"op": "card.create", "title": "x", "project": "nope"}, "invalid"),
    ):
        with pytest.raises(api.ApiError) as exc:
            env.call("root", request)
        assert exc.value.kind == why, request
    with pytest.raises(api.ApiError) as exc:
        api.dispatch(env.ctx, person("owner"), {"op": "card.create", "title": "x", "project": "digipaddock"})
    assert exc.value.kind == "refused"


def test_agents_cannot_use_user_ops(tmp_path):
    env = _env(tmp_path)
    with pytest.raises(api.ApiError) as exc:
        env.call(
            "digi-dev", {"op": "user.card.create", "title": "x", "project": "digipaddock"}
        )
    assert exc.value.kind == "refused"
    with pytest.raises(api.ApiError) as exc:
        api.dispatch(env.ctx, person("ghost"), {"op": "card.list"})
    assert exc.value.kind == "refused"


# --- a person's message reaches the card's agent; a resend is one message --------------
def _say(env, card_id, text, **extra):
    return api.dispatch(
        env.ctx, person("owner"), {"op": "user.card.comment", "card": card_id, "text": text, **extra}
    )


def test_a_resent_message_with_the_same_key_is_one_message(tmp_path):
    env = _env(tmp_path)
    card = env.card(project="digipaddock", assignee="@dev", status="In Progress")
    start = env.store.last_seq()
    first = _say(env, card.id, "x", client_key="k-00000001")
    again = _say(env, card.id, "x", client_key="k-00000001")
    assert first["message"]["seq"] == again["message"]["seq"]
    assert len(env.store.messages(card.id)) == 1
    assert [e["kind"] for e in env.store.events_since(start)] == ["message.added"]
    other = _say(env, card.id, "x", client_key="k-00000002")
    assert other["message"]["seq"] == 2
    for bad in ("short", "has space 123", 7):
        with pytest.raises(api.ApiError) as exc:
            _say(env, card.id, "x", client_key=bad)
        assert exc.value.kind == "invalid"


def test_a_plain_message_goes_to_the_cards_agent(tmp_path):
    env = _env(tmp_path)

    def addressed(text, **card):
        made = env.card(project="digipaddock", **card)
        _say(env, made.id, text)
        return env.store.events_since(0)[-1]["payload"].get("mentions")

    assert addressed("посмотри", assignee="@dev", status="Inbox") == ["@dev"]
    assert addressed("посмотри", assignee="@boss", status="In Progress") == ["@boss"]
    assert addressed("спасибо", assignee="@dev", status="Done") is None
    assert addressed("себе", assignee="@owner", status="In Progress") is None
    assert addressed("@analyst глянь", assignee="@dev", status="In Progress") == ["@analyst"]


# --- the BOSS promote approves the brief it starts (R17, AC18) ------------------------------------------------
def test_a_promote_of_a_stale_brief_is_refused(tmp_path):
    env = _env(tmp_path)
    card = env.briefed()
    changed = env.store.edit_card(card.id, None, {"description": "Цель: другое."}, actor="user:owner")
    with pytest.raises(api.ApiError) as exc:
        env.call("digi-cabinet", {"op": "card.promote", "card": card.id, "revision": changed.revision, "reason": "go"})
    assert (exc.value.kind, exc.value.reason) == ("refused", "brief_stale")
    assert env.store.card(card.id).status == "Inbox"
    assert briefs.latest(env.store, card.id)["approved_by"] == ""


def test_a_promote_approves_the_ready_version(tmp_path):
    env = _env(tmp_path)
    card = env.briefed()
    out = env.call(
        "digi-cabinet", {"op": "card.promote", "card": card.id, "revision": card.revision, "reason": "go", "by": "user:anna"}
    )
    assert out["card"]["status"] == "To Do"
    v1 = briefs.latest(env.store, card.id)
    assert (v1["version"], v1["approved_by"]) == (1, "user:anna")
    plain = env.card(status="Inbox", assignee="@dev")
    env.call("digi-cabinet", {"op": "card.promote", "card": plain.id, "revision": plain.revision, "reason": "go"})
    assert briefs.versions(env.store, plain.id) == []


def test_a_manager_gives_no_more_than_they_hold(tmp_path):
    # review 04.11.2026: users.manage alone made itself the owner through user.update, past role.set's guard
    env = _env(tmp_path)
    owner = person("owner")
    api.dispatch(env.ctx, owner, {"op": "role.set", "id": "mgr", "perms": ["users.manage", "cards.read"]})
    api.dispatch(env.ctx, owner, {"op": "user.create", "id": "mal", "name": "Mal", "roles": [["mgr", "*"]]})
    mal = person("mal")

    def refused(request):
        with pytest.raises(api.ApiError) as exc:
            api.dispatch(env.ctx, mal, request)
        assert exc.value.kind == "refused", request
        return str(exc.value)

    assert "own roles" in refused({"op": "user.update", "id": "mal", "roles": [["owner", "*"]]})
    assert "own roles" in refused({"op": "user.update", "id": "mal", "roles": [["viewer", "*"]]})
    refused({"op": "role.set", "id": "x", "perms": ["*"]})
    refused({"op": "role.set", "id": "partner", "perms": ["cards.read", "servers.read"]})  # every partner widened
    refused({"op": "user.create", "id": "eve", "name": "Eve", "roles": [["owner", "*"]]})
    refused({"op": "user.update", "id": "anna", "roles": [["partner", ["web"]]]})  # partner holds more than cards.read
    # the owner is changed, blocked or given a code only by a holder of every permission
    refused({"op": "user.update", "id": "owner", "roles": [["viewer", "*"]]})
    refused({"op": "user.block", "id": "owner"})
    refused({"op": "user.code", "id": "owner"})
    assert api.dispatch(env.ctx, person("owner"), {"op": "user.me"})["permissions"]["*"] == ["*"]
    # what the manager holds, they give
    api.dispatch(env.ctx, mal, {"op": "user.create", "id": "vic", "name": "Vic", "roles": [["viewer", "*"]]})
    api.dispatch(env.ctx, mal, {"op": "user.update", "id": "vic", "roles": [["viewer", ["web"]]]})
    api.dispatch(env.ctx, mal, {"op": "role.set", "id": "reader", "perms": ["cards.read"]})
    assert api.dispatch(env.ctx, mal, {"op": "user.code", "id": "vic"})["code"]
    api.dispatch(env.ctx, mal, {"op": "user.block", "id": "vic"})
    # the owner keeps every power; root (the host) too
    api.dispatch(env.ctx, owner, {"op": "user.update", "id": "mal", "roles": [["owner", "*"]]})
    api.dispatch(env.ctx, api.Principal("root", 0, "root"), {"op": "user.update", "id": "mal", "roles": [["viewer", "*"]]})


def test_a_partner_reaches_only_the_projects_agents_and_cards(tmp_path):
    # review 04.11.2026: a partner (no servers, no agents.control) had @sysadmin read servers for them, assigned it,
    # put server labels and linked cards of a project they do not see
    env = _env(tmp_path)
    anna, owner = person("anna"), person("owner")
    admin = api.dispatch(env.ctx, owner, {"op": "user.card.create", "title": "Сервер", "project": "admin"})["card"]
    mine = api.dispatch(env.ctx, anna, {"op": "user.card.create", "title": "Моя", "project": "web"})["card"]
    def woken():  # the agents a message is for: its event's mentions (kd-board sync wakes them)
        rows = env.store._conn.execute("SELECT payload FROM events WHERE card=?", (mine["id"],))
        return [m for (payload,) in rows for m in json.loads(payload or "{}").get("mentions", [])]


    api.dispatch(env.ctx, anna, {"op": "user.card.comment", "card": mine["id"], "text": "@sysadmin покажи auth.log на db-1"})
    assert "@sysadmin" not in woken()
    for assignee in ("@sysadmin", "@dev"):
        with pytest.raises(api.ApiError) as exc:
            api.dispatch(env.ctx, anna, {"op": "user.card.create", "title": "x", "project": "web", "assignee": assignee})
        assert exc.value.kind == "refused"

    def edit(who, card, changes):
        card = api.dispatch(env.ctx, who, {"op": "card.get", "card": card["id"]})["card"]
        return api.dispatch(env.ctx, who, {"op": "user.card.edit", "card": card["id"], "revision": card["revision"],
                                           "changes": changes})

    for changes, kind in (({"assignee": "@sysadmin"}, "refused"), ({"servers": ["db-1"]}, "refused"),
                          ({"parent": admin["id"]}, "not_found"), ({"dependencies": [admin["id"]]}, "not_found")):
        with pytest.raises(api.ApiError) as exc:
            edit(anna, mine, changes)
        assert exc.value.kind == kind, changes
    # the owner steers every agent on every project
    edit(owner, mine, {"servers": ["db-1"], "parent": admin["id"]})
    other = api.dispatch(env.ctx, owner, {"op": "user.card.create", "title": "y", "project": "web",
                                          "assignee": "@sysadmin"})["card"]
    assert other["assignee"] == "@sysadmin"
    api.dispatch(env.ctx, owner, {"op": "user.card.comment", "card": mine["id"], "text": "@sysadmin посмотри"})
    assert "@sysadmin" in woken()


# the new card's form — a card is created with what an edit could set on it, in one write


def _create(env, who, **fields):
    return api.dispatch(env.ctx, person(who), {"op": "user.card.create", "title": "Форма", "project": "web", **fields})


def _cards(env):
    return [c["id"] for c in api.dispatch(env.ctx, person("owner"), {"op": "card.list"})["cards"]]


def test_a_new_card_takes_the_optional_fields_in_one_write(tmp_path):
    env = _env(tmp_path)
    goal, first, second = (env.card(project="web").id for _ in range(3))
    council = {"brief": {"critics": ["claude:opus", "claude:sonnet"], "rounds": 2}}
    card = _create(
        env, "owner", description="Зачем", labels=["ui", "ui", " night "],
        acceptance=[{"text": " A "}, {"text": "B", "checked": True}], parent=goal,
        dependencies=[second, first, second], model="claude:opus", effort="high", council=council,
    )["card"]
    assert (card["status"], card["revision"], card["created_by"]) == ("Inbox", 1, "@owner")
    assert card["labels"] == ["ui", "night"]
    assert [(a["text"], a["checked"]) for a in card["acceptance"]] == [("A", False), ("B", True)]
    assert (card["parent"], card["dependencies"]) == (goal, sorted([first, second]))
    assert (card["model"], card["effort"]) == ("claude:opus", "high")
    assert card["council"]["brief"] == {"critics": ["claude:opus", "claude:sonnet"], "rounds": 2}
    kinds = [k for (k,) in env.store._conn.execute("SELECT kind FROM events WHERE card=?", (card["id"],))]
    assert kinds == ["card.created"]
    assert env.store.card(card["id"]).council == card["council"]


@pytest.mark.parametrize("spend", [
    {"model": "claude:opus"}, {"effort": "high"}, {"council": {"brief": {"critics": ["claude:opus"]}}},
    {"model": ""}, {"council": {}},
    {"model": "nope"},  # refused before it is checked: what the cabinet's models are is not asked this way
])
def test_a_new_cards_model_effort_and_council_need_settings(tmp_path, spend):
    env = _env(tmp_path)
    before = _cards(env)
    with pytest.raises(api.ApiError) as exc:
        _create(env, "anna", **spend)
    assert exc.value.kind == "refused"
    assert _cards(env) == before
    assert _create(env, "anna", labels=["ui"])["card"]["labels"] == ["ui"]  # the rest of the form is hers


def test_a_refused_new_card_takes_no_number(tmp_path):
    env = _env(tmp_path)
    first = _create(env, "anna")["card"]["id"]
    with pytest.raises(api.ApiError):
        _create(env, "anna", model="claude:opus")
    with pytest.raises(api.ApiError):
        _create(env, "owner", model="nope")
    second = _create(env, "anna")["card"]["id"]
    assert int(second.rsplit("-", 1)[1]) == int(first.rsplit("-", 1)[1]) + 1


@pytest.mark.parametrize("link", ["parent", "dependencies"])
def test_a_new_cards_links_stay_in_the_persons_reach(tmp_path, link):
    env = _env(tmp_path)
    hidden = env.card(project="admin").id
    seen = env.card(project="web").id
    before = _cards(env)
    with pytest.raises(api.ApiError) as exc:
        _create(env, "anna", **{link: hidden if link == "parent" else [seen, hidden]})
    assert exc.value.kind == "not_found"
    assert _cards(env) == before
    made = _create(env, "anna", **{link: seen if link == "parent" else [seen]})["card"]
    assert made[link] == (seen if link == "parent" else [seen])


@pytest.mark.parametrize("bad", [
    {"labels": ["server:db-1"]}, {"labels": "ui"}, {"acceptance": [{"text": ""}]}, {"parent": "x"},
    {"dependencies": "KD-1"}, {"model": "nope"}, {"effort": "max!"},
    {"council": {"brief": {"critics": ["nope"]}}},
])
def test_a_new_cards_fields_are_checked_as_an_edits(tmp_path, bad):
    env = _env(tmp_path)
    before = _cards(env)
    with pytest.raises(api.ApiError) as exc:
        _create(env, "owner", **bad)
    assert exc.value.kind == "invalid"
    assert _cards(env) == before


def test_a_new_card_without_the_optional_fields_is_as_before(tmp_path):
    env = _env(tmp_path)
    card = _create(env, "owner", parent="", labels=[], dependencies=[], acceptance=[])["card"]
    assert (card["parent"], card["labels"], card["dependencies"]) == ("", [], [])
    assert (card["model"], card["effort"], card["council"]) == ("", "", {})


def test_the_board_has_no_milestones(tmp_path):
    # the field, the filter and the list are gone; a request that still names one gets no milestone
    env = _env(tmp_path)
    made = _create(env, "owner", milestone="m-1")["card"]  # an older page's form: the key is not a card's
    assert "milestone" not in made
    listed = api.dispatch(env.ctx, person("owner"), {"op": "card.list", "milestone": "m-1"})["cards"]
    assert [c["id"] for c in listed] == [made["id"]] and "milestone" not in listed[0]  # nothing to filter by
    with pytest.raises(api.ApiError) as exc:
        api.dispatch(env.ctx, person("owner"), {
            "op": "user.card.edit", "card": made["id"], "revision": made["revision"], "changes": {"milestone": "m-1"}})
    assert exc.value.kind == "invalid" and "milestone" in str(exc.value)
    with pytest.raises(api.ApiError) as exc:
        api.dispatch(env.ctx, person("owner"), {"op": "milestone.list"})
    assert exc.value.kind == "invalid"
