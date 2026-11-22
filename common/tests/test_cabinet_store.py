import json

import pytest
from kd.cabinet import schema, store


@pytest.fixture
def db(tmp_path):
    return store.Store.open(
        str(tmp_path / "cabinet.db"), prefix="KD", clock=lambda: "2026-10-14T10:00:00Z"
    )


def test_open_creates_schema_and_is_idempotent(tmp_path, db):
    assert (
        db.get_state("schema_version") is None
    )  # state is for root documents, not meta
    again = store.Store.open(str(tmp_path / "cabinet.db"))
    assert again.schema_version() == schema.SCHEMA_VERSION
    assert again.cards() == [] and again.last_seq() == 0 and again.projects() == {}
    with pytest.raises(store.NotFound):
        again.card("KD-1")


def test_open_refuses_a_newer_schema(tmp_path, db):
    db._conn.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    db._conn.commit()
    with pytest.raises(store.StoreError):
        store.Store.open(str(tmp_path / "cabinet.db"))


def test_create_assigns_ids_and_events(db):
    a = db.create_card(
        {
            "title": "First",
            "project": "digipaddock",
            "status": "Inbox",
            "created_by": "@owner",
        },
        actor="user:owner",
    )
    b = db.create_card(
        {
            "title": "Second",
            "project": "admin",
            "status": "Inbox",
            "assignee": "@sysadmin",
            "acceptance": ["one", "two"],
            "labels": ["server:node-c"],
            "servers": ["node-c"],
            "created_by": "@sysadmin",
        },
        actor="agent:sysadmin",
    )
    assert (a.id, a.n, a.revision) == ("KD-1", 1, 1) and b.id == "KD-2"
    assert b.acceptance == (
        store.Acceptance("one"),
        store.Acceptance("two"),
    ) and b.servers == ("node-c",)
    events = db.events_since(0)
    assert [e["kind"] for e in events] == ["card.created", "card.created"]
    assert (
        events[1]["actor"] == "agent:sysadmin"
        and events[1]["payload"]["assignee"] == "@sysadmin"
    )


def test_edit_is_compare_and_set(db):
    card = db.create_card(
        {
            "title": "T",
            "project": "digipaddock",
            "status": "To Do",
            "assignee": "@dev",
            "created_by": "@owner",
        },
        actor="user:owner",
    )
    moved = db.edit_card(
        card.id,
        card.revision,
        {"status": "In Progress", "notes_append": "[dev] spec implementer v1"},
        actor="agent:dev",
    )
    assert (moved.status, moved.revision, moved.notes) == (
        "In Progress",
        2,
        "[dev] spec implementer v1",
    )
    with pytest.raises(store.Conflict):
        db.edit_card(
            card.id, card.revision, {"status": "Review"}, actor="agent:dev"
        )  # stale
    assert db.card(card.id).status == "In Progress"  # nothing written
    event = db.events_since(0)[-1]
    assert (
        event["kind"] == "card.changed"
        and event["payload"]["before"]["status"] == "To Do"
    )
    assert event["payload"]["fields"] == ["notes_append", "status"]


def test_comment_lands_in_the_thread_in_the_same_write(db):
    card = db.create_card(
        {"title": "T", "project": "digipaddock", "status": "Inbox", "created_by": "@owner"},
        actor="user:owner",
    )
    db.edit_card(
        card.id,
        card.revision,
        {"comment": {"text": "[boss] hello", "author": "@boss"}},
        actor="agent:boss",
    )
    msgs = db.messages(card.id)
    assert [(m["seq"], m["author"], m["body"]) for m in msgs] == [
        (1, "agent:boss", "[boss] hello")
    ]
    assert (
        db.card(card.id).revision == 2
        and db.events_since(0)[-1]["payload"]["comment"] is True
    )


def test_guards_pin_dependencies(db):
    dep = db.create_card(
        {"title": "D", "project": "digipaddock", "status": "Done", "created_by": "@owner"},
        actor="user:owner",
    )
    card = db.create_card(
        {
            "title": "C",
            "project": "digipaddock",
            "status": "Blocked",
            "dependencies": [dep.id],
            "created_by": "@owner",
        },
        actor="user:owner",
    )
    with pytest.raises(store.Conflict):
        db.edit_card(
            card.id,
            card.revision,
            {"status": "To Do"},
            actor="agent:boss",
            guards={dep.id: dep.revision + 1},
        )
    assert (
        db.edit_card(
            card.id,
            card.revision,
            {"status": "To Do"},
            actor="agent:boss",
            guards={dep.id: dep.revision},
        ).status
        == "To Do"
    )


def test_acceptance_and_labels_and_invalid_keys(db):
    card = db.create_card(
        {
            "title": "T",
            "project": "digipaddock",
            "status": "Inbox",
            "acceptance": ["a", "b"],
            "labels": ["x"],
            "created_by": "@owner",
        },
        actor="user:owner",
    )
    card = db.edit_card(
        card.id,
        None,
        {
            "ac_check": [2],
            "ac_add": ["c"],
            "labels_add": ["spec:implementer"],
            "labels_remove": ["x"],
        },
        actor="agent:dev",
    )
    assert [(a.text, a.checked) for a in card.acceptance] == [("a", False), ("b", True), ("c", False)] and card.labels == (
        "spec:implementer",
    )
    # title is a whole-field write of people; an empty one is still invalid, an unknown key too
    for bad in ({"status": "Later"}, {"title": " "}, {"ac_check": [9]}, {"name": "no"}):
        with pytest.raises(store.Invalid):
            db.edit_card(card.id, None, bad, actor="agent:dev")


def test_import_keeps_ids_and_is_refused_twice(db):
    rec = {
        "id": "KD-40",
        "n": 40,
        "project": "admin",
        "title": "Old",
        "status": "Done",
        "assignee": "@sysadmin",
        "revision": 1,
        "created_by": "@owner",
        "created": "2026-10-01T00:00:00Z",
        "updated": "2026-10-02T00:00:00Z",
        "labels": ["server:node-c"],
        "servers": ["node-c"],
        "dependencies": [],
        "acceptance": [{"text": "x", "checked": True}],
    }
    card = db.import_card(
        rec,
        [{"author": "@boss", "created": "2026-10-01T10:00:00Z", "body": "[boss] hi"}],
    )
    assert card.id == "KD-40" and db.messages("KD-40")[0]["author"] == "agent:boss"
    assert (
        db.create_card(
            {
                "title": "Next",
                "project": "digipaddock",
                "status": "Inbox",
                "created_by": "@owner",
            },
            actor="user:owner",
        ).id
        == "KD-41"
    )
    with pytest.raises(store.Conflict):
        db.import_card(rec, [])


def test_state_and_acks(db):
    assert db.get_state("sync") is None
    db.set_state("sync", {"seq": 7})
    assert db.get_state("sync") == {"seq": 7}
    for event in ("a" * 32, "a" * 32, "b" * 32):
        db.add_ack(event, "owner", 30, "asked")
    assert [a["event"] for a in db.take_acks()] == ["a" * 32, "b" * 32] and db.take_acks() == []


def test_server_labels_are_stored_as_card_servers(db):
    # A server: label of the old board is a card_servers row; every writer still speaks labels.
    card = db.create_card(
        {
            "title": "T",
            "project": "admin",
            "status": "Inbox",
            "labels": ["server:node-c", "x"],
            "created_by": "@sysadmin",
        },
        actor="agent:sysadmin",
    )
    assert card.labels == ("x",) and card.servers == ("node-c",)
    card = db.edit_card(
        card.id,
        card.revision,
        {"labels_add": ["server:node-l"], "labels_remove": ["server:node-c"]},
        actor="agent:sysadmin",
    )
    assert card.labels == ("x",) and card.servers == ("node-l",)
    card = db.edit_card(
        card.id,
        card.revision,
        {"labels_add": ["server:node-l", "y"]},
        actor="agent:sysadmin",
    )
    assert card.labels == ("x", "y") and card.servers == ("node-l",)


def test_v1_base_migrates_to_v2(tmp_path):
    import sqlite3

    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(schema.DDL)
    conn.execute("INSERT INTO meta VALUES ('schema_version', '1')")
    conn.execute("INSERT INTO meta VALUES ('next_card', '2')")
    conn.execute(
        "INSERT INTO cards (id, n, project, title, status, created_by, revision, created, updated)"
        " VALUES ('KD-1', 1, 'digipaddock', 'Old', 'To Do', '@owner', 3, 't', 't')"
    )
    conn.execute("INSERT INTO threads (kind, card) VALUES ('card', 'KD-1')")
    conn.close()
    db = store.Store.open(path)
    assert db.schema_version() == schema.SCHEMA_VERSION
    assert db.card("KD-1").revision == 3
    tables = {r[0] for r in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"users", "roles", "decisions", "web_sessions", "notifications", "attachments"} <= tables
    assert {r[0] for r in db._conn.execute("SELECT id FROM roles")} == {"owner", "partner", "viewer"}
    db._conn.execute("UPDATE roles SET perms='[\"cards.read\"]' WHERE id='owner'")
    again = store.Store.open(path)  # the owner's own role set is not overwritten
    assert again._conn.execute("SELECT perms FROM roles WHERE id='owner'").fetchone()[0] == '["cards.read"]'


def test_v4_base_migrates_to_v5_keeping_answers(tmp_path):
    """An answered text question and a brief-ready card of a v4 base stay as they were; no
    version of the brief is made up from the label."""
    import sqlite3

    path = str(tmp_path / "v4.db")
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(schema.DDL + schema.DDL_V2 + schema.DDL_V3 + schema.DDL_V4)
    conn.execute("INSERT INTO meta VALUES ('schema_version', '4')")
    conn.execute("INSERT INTO meta VALUES ('next_card', '40')")
    conn.execute(
        "INSERT INTO cards (id, n, project, title, labels, status, assignee, created_by, revision, created, updated)"
        " VALUES ('KD-39', 39, 'digipaddock', 'Brief', '[\"brief-ready\",\"spec:implementer\"]', 'Inbox', '@dev',"
        " '@owner', 7, 't', 't')"
    )
    conn.execute("INSERT INTO threads (id, kind, card) VALUES (1, 'card', 'KD-39')")
    conn.execute(
        "INSERT INTO messages (thread, seq, author, kind, body, created) VALUES (1, 1, 'agent:analyst', 'decision',"
        " '1. Как? А) так; Б) иначе.', 't'), (1, 2, 'user:owner', 'decision', 'А', 't')"
    )
    conn.execute(
        "INSERT INTO decisions (id, type, card, thread, message, payload, payload_sha, requested_by,"
        " decide_permission, step_up, state, created, decided_by, decided_at, reply)"
        " VALUES (?, 'question', 'KD-39', 1, 1, '{\"text\":\"1. Как? А) так; Б) иначе.\"}', 'x', 'agent:analyst',"
        " 'decide.question', 0, 'approved', 't', 'user:owner', 't', 'А')",
        ("d" * 32,),
    )
    conn.close()
    db = store.Store.open(path)
    assert db.schema_version() == schema.SCHEMA_VERSION
    row = db._conn.execute("SELECT reply, answer, state FROM decisions").fetchone()
    assert tuple(row) == ("А", "", "approved")
    assert [m["seq"] for m in db.messages("KD-39")] == [1, 2]
    assert db.card("KD-39").labels == ("brief-ready", "spec:implementer")
    assert db._conn.execute("SELECT COUNT(*) FROM brief_versions").fetchone()[0] == 0


# --- every thread message is a message.added event --------------------------
def _card(db):
    return db.create_card(
        {"title": "T", "project": "digipaddock", "created_by": "@owner"}, actor="user:owner"
    )


def test_a_comment_of_an_edit_is_a_message_added_event(db):
    card = _card(db)
    db.edit_card(
        card.id, 1, {"comment": {"text": "[dev] шаг", "author": "@dev"}}, actor="agent:dev"
    )
    events = db.events_since(0)[-2:]
    assert [(e["kind"], e["actor"]) for e in events] == [
        ("message.added", "agent:dev"),
        ("card.changed", "agent:dev"),
    ]
    assert events[0]["payload"] == {
        "seq": 1,
        "kind": "text",
        "title": "T",
        "via": "card.changed",
    }
    assert events[0]["card"] == card.id


def test_a_plain_message_keeps_its_mentions_and_has_no_via(db):
    card = _card(db)
    db.add_message(card.id, author="user:owner", kind="text", body="@dev", mentions=["@dev"])
    event = db.events_since(0)[-1]
    assert event["kind"] == "message.added"
    assert event["payload"] == {"seq": 1, "kind": "text", "title": "T", "mentions": ["@dev"]}


def test_an_import_writes_no_message_events(db):
    db.import_card(
        {
            "id": "KD-7",
            "n": 7,
            "title": "Old",
            "project": "digipaddock",
            "status": "Done",
            "created_by": "@owner",
            "created": "2026-10-01T10:00:00Z",
            "updated": "2026-10-01T10:00:00Z",
        },
        [
            {"author": "@dev", "body": "one", "created": "2026-10-01T10:00:00Z"},
            {"author": "@owner", "body": "two", "created": "2026-10-01T11:00:00Z"},
        ],
    )
    assert [e["kind"] for e in db.events_since(0)] == ["card.imported"]
    assert len(db.messages("KD-7")) == 2


def test_a_derived_message_does_not_count_against_the_agent(db):
    card = _card(db)
    db.edit_card(
        card.id, 1, {"comment": {"text": "[dev] шаг", "author": "@dev"}}, actor="agent:dev"
    )
    assert db.count_events("agent:dev", "2026-10-14T00:00:00Z") == 1
    db.add_message(card.id, author="agent:dev", kind="progress", body="шаг 2")
    assert db.count_events("agent:dev", "2026-10-14T00:00:00Z") == 2


def test_v6_base_migrates_to_7_keeping_sessions(tmp_path):
    """schema 7 adds the usage ledger, jobs and a session's cumulative usage; v6 rows stay."""
    import sqlite3

    path = str(tmp_path / "v6.db")
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(schema.DDL + schema.DDL_V2 + schema.DDL_V3 + schema.DDL_V4 + schema.DDL_V5 + schema.DDL_V6)
    conn.execute("INSERT INTO meta VALUES ('schema_version', '6')")
    conn.execute("INSERT INTO meta VALUES ('next_card', '1')")
    conn.execute("INSERT INTO threads (id, kind, agent) VALUES (1, 'agent_chat', 'dev')")
    conn.execute("INSERT INTO sessions (id, thread, agent, usage, created, updated) VALUES (?, 1, 'dev',"
                 " '{\"cost\": 1.5}', 't', 't')", ("a" * 32,))
    conn.execute("INSERT INTO threads (id, kind, agent) VALUES (2, 'agent_chat', 'boss')")
    conn.execute("INSERT INTO sessions (id, thread, agent, runtime_session_id, created, updated) VALUES (?, 2, 'boss',"
                 " '0b9c3f1e-1111-2222-3333-444455556666', 't', 't')", ("b" * 32,))
    conn.close()
    db = store.Store.open(path)
    assert db.schema_version() == schema.SCHEMA_VERSION  # 7 and every later step
    session = db.session("a" * 32)
    assert session["usage"] == {"cost": 1.5} and session["usage_total"] == {}
    assert db.session("b" * 32)["usage_total"] is None  # its first total is a baseline: the files counted it
    assert db._conn.execute("SELECT COUNT(*) FROM usage_log").fetchone()[0] == 0
    assert db._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_migration_8_adds_tool_outputs_and_usage_command(tmp_path):
    """a tool's masked output apart from the thread; a usage row knows its turn's command."""
    import sqlite3

    path = str(tmp_path / "v7.db")
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(schema.DDL + schema.DDL_V2 + schema.DDL_V3 + schema.DDL_V4 + schema.DDL_V5 + schema.DDL_V6
                       + schema.DDL_V7)
    conn.execute("INSERT INTO meta VALUES ('schema_version', '7')")
    conn.execute("INSERT INTO meta VALUES ('next_card', '1')")
    conn.execute("INSERT INTO usage_log (at, agent, kind, source, model, requests, input, output, cache_read,"
                 " cache_write, cost) VALUES ('t', 'dev', 'turn', ?, 'm', 1, 1, 1, 0, 0, 0.5)", ("a" * 32,))
    conn.close()
    db = store.Store.open(path)
    assert db.schema_version() == schema.SCHEMA_VERSION  # 8 and every later step
    assert [tuple(r) for r in db._conn.execute("SELECT command, cost FROM usage_log")] == [("", 0.5)]
    thread = db.agent_thread("dev")
    db.tool_output_put(thread, 3, session="b" * 32, command="c" * 32,
                       output={"text": "вывод", "size": 10, "truncated": True})
    assert db.tool_output(thread, 3) == {"text": "вывод", "size": 10, "truncated": True}
    assert db.tool_output(thread, 4) is None


def test_migration_9_adds_prs(tmp_path):
    """dev's pull requests, the latest of each, keyed by repo and number."""
    import sqlite3

    path = str(tmp_path / "v8.db")
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(schema.DDL + schema.DDL_V2 + schema.DDL_V3 + schema.DDL_V4 + schema.DDL_V5 + schema.DDL_V6
                       + schema.DDL_V7 + schema.DDL_V8)
    conn.execute("INSERT INTO meta VALUES ('schema_version', '8')")
    conn.execute("INSERT INTO meta VALUES ('next_card', '1')")
    conn.close()
    db = store.Store.open(path)
    assert db.schema_version() == schema.SCHEMA_VERSION
    assert db.pr("owner/repo", 1) is None and db.prs() == []


# no repository request yet; no memory bank (the role bank); the owner accepts Done;
# no council
NO_REPO = {"repo_state": "", "repo_detail": "", "repo_updated": "", "memory_bank": "", "done_by": "", "model": "",
           "effort": "", "council": {}}


def test_migration_12_names_the_mirrored_projects_once(tmp_path):
    """the table the old start mirrored routes.json into (name = id) gets its names once and a repo column;
    a reopen changes nothing, a name the owner gave stays, ops stays until its cards move."""
    import sqlite3

    path = str(tmp_path / "v11.db")
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(schema.DDL + "".join(schema.MIGRATIONS[v] for v in range(2, 12)))
    conn.execute("INSERT INTO meta VALUES ('schema_version', '11')")
    conn.execute("INSERT INTO meta VALUES ('next_card', '1')")
    conn.executemany("INSERT INTO projects VALUES (?,?,?)", [
        ("digipaddock", "digipaddock", "@dev"), ("admin", "Сервера", "@sysadmin"), ("ops", "ops", "@owner"), ("alert", "alert", "@owner"),
    ])
    conn.close()
    want = [
        {"id": "admin", "name": "Сервера", "route": "@sysadmin", "repo": "", "routed": "", **NO_REPO},
        {"id": "alert", "name": "alert", "route": "@owner", "repo": "", "routed": "", **NO_REPO},
        {"id": "digipaddock", "name": "DIGIPADDOCK", "route": "@dev", "repo": "", "routed": "", **NO_REPO},
        {"id": "ops", "name": "ops", "route": "@owner", "repo": "", "routed": "", **NO_REPO},
    ]
    db = store.Store.open(path)
    assert db.schema_version() == schema.SCHEMA_VERSION
    assert db.project_rows() == want
    assert db.projects() == {"admin": "@sysadmin", "alert": "@owner", "digipaddock": "@dev", "ops": "@owner"}
    db.close()
    assert store.Store.open(path).project_rows() == want


def test_a_repository_request_and_its_answer_are_events(tmp_path):
    """the request and the root unit's answer, each with its event; only open requests are queued."""
    db = store.Store.open(str(tmp_path / "c.db"), clock=lambda: "2026-10-22T09:00:00Z")
    db.seed_projects([{"id": "web", "name": "Сайт", "route": "@owner"}, {"id": "digipaddock", "name": "D", "route": "@dev"}])
    asked = db.set_project_repo("web", actor="user:owner", repo="o/Site", state="pending")
    assert (asked["repo"], asked["repo_state"], asked["repo_updated"]) == ("o/Site", "pending", "2026-10-22T09:00:00Z")
    assert [r["id"] for r in db.repo_requests()] == ["web"]
    db.set_project_repo("web", actor="root", repo="o/Site", state="no_access", detail="clone")
    assert db.repo_requests() == [] and db.project("web")["repo_detail"] == "clone"
    with pytest.raises(store.Invalid):
        db.set_project_repo("web", actor="root", repo="o/Site", state="maybe")
    with pytest.raises(store.NotFound):
        db.set_project_repo("nope", actor="root", repo="", state="")
    payloads = [e["payload"] for e in db.events_since(0) if e["kind"] == "project.repo"]
    assert payloads == [{"project": "web", "repo": "o/Site", "state": "pending", "from": ""},
                        {"project": "web", "repo": "o/Site", "state": "no_access", "detail": "clone"}]


def test_seed_projects_keeps_what_is_there(tmp_path):
    db = store.Store.open(str(tmp_path / "c.db"))
    assert db.projects() == {} and db.project_rows() == []
    db.seed_projects([{"id": "digipaddock", "name": "DIGIPADDOCK", "route": "@dev"}])
    db.seed_projects([{"id": "digipaddock", "name": "x", "route": "@owner"}, {"id": "web", "name": "Сайт", "route": "@owner"}])
    assert db.project_rows() == [
        {"id": "digipaddock", "name": "DIGIPADDOCK", "route": "@dev", "repo": "", "routed": "", **NO_REPO},
        {"id": "web", "name": "Сайт", "route": "@owner", "repo": "", "routed": "", **NO_REPO},
    ]


def test_project_changes_are_events_and_a_reroute_is_stamped(tmp_path):
    """the owner's create, rename, reroute, each with its event; ``routed`` — when the route last moved."""
    clock = iter(["2026-10-21T09:00:00Z", "2026-10-21T09:05:00Z", "2026-10-21T09:10:00Z"])
    db = store.Store.open(str(tmp_path / "c.db"), clock=lambda: next(clock, "2026-10-21T10:00:00Z"))
    made = db.create_project("site", "Сайт", "@dev", actor="user:owner")
    assert made == {"id": "site", "name": "Сайт", "route": "@dev", "repo": "", "routed": "2026-10-21T09:00:00Z",
                    **NO_REPO}
    with pytest.raises(store.Conflict):
        db.create_project("site", "Другой", "@owner", actor="user:owner")
    renamed = db.update_project("site", name="Сайт и лендинги", actor="user:owner")
    assert renamed["routed"] == made["routed"]
    moved = db.update_project("site", route="@owner", actor="user:owner")
    assert moved["route"] == "@owner" and moved["routed"] > made["routed"]
    kinds = [(e["kind"], e["payload"]) for e in db.events_since(0) if e["kind"].startswith("project.")]
    assert kinds == [
        ("project.created", {"project": "site", "name": "Сайт", "route": "@dev"}),
        ("project.changed", {"project": "site", "name": {"from": "Сайт", "to": "Сайт и лендинги"}}),
        ("project.changed", {"project": "site", "route": {"from": "@dev", "to": "@owner"}}),
    ]
    with pytest.raises(store.NotFound):
        db.update_project("nope", name="x", actor="user:owner")


def test_pipeline_migration_adds_its_tables_and_basis(tmp_path):
    """turn outcomes, waits, continuations, problems; a command's basis; the moment it began."""
    import sqlite3

    last = 10  # the base before the pipeline's step
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(schema.DDL + "".join(schema.MIGRATIONS[v] for v in range(2, last + 1)))
    conn.execute(f"INSERT INTO meta VALUES ('schema_version', '{last}')")
    conn.execute("INSERT INTO meta VALUES ('next_card', '1')")
    conn.execute("INSERT INTO threads (id, kind, card, agent) VALUES (1, 'agent_chat', NULL, 'dev')")
    conn.execute("INSERT INTO commands (id, key, agent, thread, kind, text, created, updated)"
                 " VALUES ('c1', 'k1', 'dev', 1, 'chat', 'hi', 'x', 'x')")
    conn.close()
    db = store.Store.open(path)
    assert db.schema_version() == schema.SCHEMA_VERSION
    tables = {r[0] for r in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"turn_outcomes", "waits", "continuations", "problems"} <= tables
    assert db.command("c1")["basis"] == {}
    assert db.meta("pipeline_since")
    assert schema.OUTCOMES == ("continue", "wait_user", "wait_dependency", "wait_until", "review", "done")


# --- the cards' prefix of a base ----------------------------------------------------------------------
def _old_base(path):
    old = store.Store.open(path, prefix="OLD", aliases=(), clock=lambda: "2026-10-14T10:00:00Z")
    first = old.create_card({"title": "First", "project": "digipaddock", "status": "Done", "created_by": "@owner"},
                            actor="user:owner")
    second = old.create_card({"title": "After OLD-1, not XOLD-1", "project": "digipaddock", "status": "Blocked",
                              "dependencies": [first.id], "created_by": "@owner"}, actor="user:owner")
    old.edit_card(second.id, second.revision, {"comment": {"text": "see OLD-1 (OLD-12345678)", "author": "@boss"}},
                  actor="agent:boss")
    old.close()


def test_a_base_of_an_alias_takes_the_hosts_prefix_at_open(tmp_path):
    path = str(tmp_path / "c.db")
    _old_base(path)
    new = store.Store.open(path, prefix="KD", aliases=("OLD",))
    # the code before the move (schema 33, its cards' prefix fixed) refuses the base instead of adding OLD- cards
    assert new.schema_version() == schema.SCHEMA_VERSION > 33 and new.meta("card_prefix") == "KD"
    assert [c.id for c in new.cards()] == ["KD-1", "KD-2"]
    second = new.card("KD-2")
    assert second.title == "After KD-1, not XOLD-1" and second.dependencies == ("KD-1",) and second.revision == 2
    assert [m["body"] for m in new.messages("KD-2")] == ["see KD-1 (OLD-12345678)"]
    assert {e["card"] for e in new.events_since(0)} == {"KD-1", "KD-2"}
    assert "OLD-" not in json.dumps([e["payload"] for e in new.events_since(0)]).replace("XOLD-1", "")
    nxt = new.create_card({"title": "Third", "project": "digipaddock", "status": "Inbox", "created_by": "@owner"},
                          actor="user:owner")
    assert nxt.id == "KD-3"
    new.close()
    assert store.Store.open(path, prefix="KD", aliases=("OLD",)).card("KD-2").title == "After KD-1, not XOLD-1"


def test_a_brief_that_names_a_card_stays_ready_across_the_prefix_move(tmp_path):
    # the version's hash covers its text: rewritten with the text, or a ready brief reads stale (review I3)
    from kd.cabinet import briefs

    path = str(tmp_path / "c.db")
    _old_base(path)
    old = store.Store.open(path, prefix="OLD", aliases=())
    card = old.edit_card("OLD-2", old.card("OLD-2").revision,
                         {"description": "Builds on OLD-1.", "acceptance": [{"text": "OLD-1 still works"}]},
                         actor="agent:analyst")
    assert briefs.add_version(old, card, origin="agent", author="@analyst")["version"] == 1
    untouched = briefs.latest(old, "OLD-2")["scope_sha"]
    old.close()
    new = store.Store.open(path, prefix="KD", aliases=("OLD",))
    record = new.card("KD-2")
    assert record.description == "Builds on KD-1." and briefs.state(new, record)["state"] == "ready"
    assert briefs.latest(new, "KD-2")["scope_sha"] != untouched


def test_a_base_of_another_prefix_is_refused(tmp_path):
    path = str(tmp_path / "c.db")
    _old_base(path)
    with pytest.raises(store.StoreError, match="card_aliases"):
        store.Store.open(path, prefix="KD", aliases=())
    assert [c.id for c in store.Store.open(path, prefix="OLD", aliases=()).cards()] == ["OLD-1", "OLD-2"]


def test_a_new_base_keeps_the_hosts_prefix(tmp_path):
    path = str(tmp_path / "c.db")
    store.Store.open(path, prefix="KD", aliases=("OLD",)).close()
    with pytest.raises(store.StoreError, match="card_aliases"):
        store.Store.open(path, prefix="AC", aliases=())


def test_schema_43_drops_the_milestones(tmp_path):
    # the table and the card's column go; a card that carried a milestone stays as it was without it
    import sqlite3

    path = str(tmp_path / "v42.db")
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(schema.DDL + "".join(schema.MIGRATIONS[v] for v in range(2, 43)))
    conn.execute("INSERT INTO meta VALUES ('schema_version', '42')")
    conn.execute("INSERT INTO meta VALUES ('next_card', '3')")
    conn.execute(  # the list as a base made before schema 43 has it
        "CREATE TABLE IF NOT EXISTS milestones (id TEXT PRIMARY KEY, n INTEGER NOT NULL UNIQUE, title TEXT NOT NULL,"
        " due TEXT NOT NULL DEFAULT '', parked INTEGER NOT NULL DEFAULT 0)")
    conn.execute("INSERT INTO milestones (id, n, title, parked) VALUES ('m-9', 9, 'Ideas', 1)")
    for n, milestone in ((1, "m-9"), (2, "")):
        conn.execute(
            "INSERT INTO cards (id, n, project, title, status, milestone, created_by, revision, created, updated)"
            " VALUES (?, ?, 'digipaddock', 'Old', 'Done', ?, '@owner', 3, 't', 't')", (f"KD-{n}", n, milestone))
        conn.execute("INSERT INTO threads (kind, card) VALUES ('card', ?)", (f"KD-{n}",))
    conn.close()
    db = store.Store.open(path)
    assert db.schema_version() == schema.SCHEMA_VERSION >= 43
    assert "milestone" not in {r[1] for r in db._conn.execute("PRAGMA table_info(cards)")}
    assert db._conn.execute("SELECT name FROM sqlite_master WHERE name='milestones'").fetchone() is None
    assert [(c.id, c.title, c.status, c.revision) for c in db.cards()] == [("KD-1", "Old", "Done", 3), ("KD-2", "Old", "Done", 3)]
    assert "milestone" not in db.card("KD-1").to_dict()
    made = db.create_card({"title": "New", "project": "digipaddock", "status": "Inbox", "created_by": "@owner"},
                          actor="user:owner")
    assert made.id == "KD-3"  # the table still takes a card after its column went
    db.close()
    again = store.Store.open(path)  # every start runs the base DDL: it must not bring the list back
    assert again._conn.execute("SELECT name FROM sqlite_master WHERE name='milestones'").fetchone() is None
    again.close()
    new = store.Store.open(str(tmp_path / "new.db"))  # and a base made now never has it
    assert new._conn.execute("SELECT name FROM sqlite_master WHERE name='milestones'").fetchone() is None
    assert "milestone" not in {r[1] for r in new._conn.execute("PRAGMA table_info(cards)")}
