"""trusted auto-investigations, factual counters and direct notification targets."""

from pathlib import Path

import pytest
from cabinet_env import CabinetEnv

from kd.cabinet import api, incident_reports, notify, store


@pytest.fixture
def env(tmp_path):
    found = CabinetEnv(tmp_path, repo_root=Path(__file__).resolve().parents[2])
    found.users.create(
        "owner", "Owner", roles=[("owner", "*")], actor="root", language="ru"
    )
    return found


def incident(env, key="node-c:units:certbot.service"):
    return env.call(
        "digi-sysadmin",
        {
            "op": "monitor.push",
            "items": [
                {
                    "key": key,
                    "kind": "open",
                    "problem": {
                        "key": key,
                        "server": "node-c",
                        "check": "units",
                        "subject": "certbot.service",
                        "level": "warn",
                        "value": "failed",
                        "opened_at": env.now,
                    },
                }
            ],
        },
    )["results"][0]["incident"]


def read(env, n=1):
    return api.dispatch(
        env.ctx,
        api.Principal("user", 0, "owner"),
        {"op": "incident.get", "incident": n},
    )["incident"]


def reply(
    env,
    body="**P-1 · certificate renewal failed.** Certificate is still valid.",
    *,
    author="agent:sysadmin",
    kind="text",
    space="monitor",
):
    return env.store.add_thread_message(
        env.store.agent_thread("sysadmin", space), author=author, kind=kind, body=body
    )


def test_monitor_report_is_visible_on_incident_and_live_event(env):
    incident(env)
    reply(env)
    found = read(env)["investigation"]
    assert found["state"] == "reviewed" and "still valid" in found["report"]["body"]
    assert found["report"]["author"] == "agent:sysadmin"
    assert env.store.events_since(0)[-1]["payload"]["incidents"] == [1]


@pytest.mark.parametrize(
    "extra",
    [
        {"author": "user:owner"},
        {"kind": "tool_result"},
        {"space": "chat"},
        {"author": "agent:dev"},
    ],
)
def test_only_trusted_monitor_texts_publish_a_report(env, extra):
    incident(env)
    reply(env, **extra)
    assert read(env)["investigation"]["report"] is None


def test_reference_boundaries_and_messages_before_the_incident(env):
    reply(env)
    env.now = "2026-10-14T10:05:00Z"
    incident(env)
    reply(env, "P-10, XP-1, P-1x and P-" + "9" * 5000)
    assert read(env)["investigation"]["report"] is None
    assert incident_reports.numbers(
        "P-1 and https://cabinet.example.test/incidents/P-10."
    ) == [1, 10]


def test_confirmation_counts_are_cumulative_idempotent_and_start_a_new_epoch_honestly(
    env,
):
    incident(env)
    assert read(env)["confirmation_count"] == 0

    def sync(key, count, started, at):
        return env.call(
            "digi-sysadmin",
            {
                "op": "monitor.push",
                "items": [
                    {
                        "kind": "sync",
                        "key": key,
                        "at": at,
                        "confirmations": [
                            {
                                "problem_key": "node-c:units:certbot.service",
                                "opened_at": "2026-10-14T10:00:00Z",
                                "count": count,
                                "started_at": started,
                                "at": at,
                            }
                        ],
                    }
                ],
            },
        )

    sync("a", 4, env.now, "2026-10-14T10:15:00Z")
    sync("a", 4, env.now, "2026-10-14T10:15:00Z")
    sync("b", 2, env.now, "2026-10-14T10:05:00Z")
    assert read(env)["confirmation_count"] == 4
    assert read(env)["confirmation_at"] == "2026-10-14T10:15:00Z"
    sync("c", 1, "2026-10-14T10:20:00Z", "2026-10-14T10:20:00Z")
    assert read(env)["confirmation_count"] == 1
    assert read(env)["confirmation_started_at"] == "2026-10-14T10:20:00Z"


def test_report_push_links_to_incident_and_replaces_only_its_pending_fallback(env):
    notifier = notify.Notifier(env.store, env.users, None)
    env.store.set_state(notify.CURSOR, env.store.last_seq())
    incident(env)
    incident(env, "node-c:units:other.service")
    for event in env.store.events_since(0):
        notifier.on_event(event, None)
    reply(env)
    notifier.on_event(env.store.events_since(0)[-1], None)
    rows = env.store.notifications("owner")
    investigation = [r for r in rows if r["kind"] == "agent.message"][-1]
    assert investigation["url"] == "/incidents/P-1"
    assert "still valid" in investigation["body"] and "**" not in investigation["body"]
    pending = [r for r in rows if r["tag"] == "monitor:batch" and not r["sent"]]
    assert (
        len(pending) == 1
        and "P-2" in pending[0]["body"]
        and "P-1" not in pending[0]["body"]
    )
    assert pending[0]["url"] == "/incidents/P-2"


def test_multiple_reported_incidents_link_to_the_queue(env):
    notifier = notify.Notifier(env.store, env.users, None)
    incident(env)
    incident(env, "node-c:units:other.service")
    reply(env, "P-1 and P-2: both checked.")
    ids = notifier.on_event(env.store.events_since(0)[-1], None)
    assert env.store.notification(ids[0])["url"] == "/incidents"


def test_a_batched_report_of_several_incidents_stays_a_line_of_the_monitors_batch(env):
    """The incidents' queue is the monitor's place: its line keeps the report's text, in the monitor's batch."""
    notifier = notify.Notifier(env.store, env.users, None)
    env.users.set_prefs("owner", {"agent.message": "batch"})
    incident(env)
    incident(env, "node-c:units:other.service")
    reply(env, "P-1 and P-2: both checked.")
    ids = notifier.on_event(env.store.events_since(0)[-1], None)
    row = env.store.notification(ids[0])
    assert row["kind"] == "batch" and row["tag"] == "monitor:batch" and row["url"] == "/incidents"
    assert "both checked" in row["body"]


def test_incident_test_push_keeps_its_exact_target_and_is_marked_as_a_test(env):
    incident(env)
    notifier = notify.Notifier(env.store, env.users, None)
    result = env.store.notification(notifier.test("owner", env.now, incident=read(env)))
    assert result["url"] == "/incidents/P-1" and result["tag"] == "test:P-1"
    assert result["title"].startswith("Проверка push")


def test_streamed_progress_is_not_published_or_pushed_as_a_finished_investigation(env):
    incident(env)
    notifier = notify.Notifier(env.store, env.users, None)
    thread = env.store.agent_thread("sysadmin", "monitor")
    command = env.store.enqueue(
        {
            "key": "investigation",
            "agent": "sysadmin",
            "thread": thread,
            "kind": "signal",
            "text": "[монитор] P-1 opened",
        }
    )
    for state in ("delivered", "started"):
        env.store.command_state(command["id"], state)
    env.store.add_thread_message(
        thread,
        author="agent:sysadmin",
        kind="text",
        body="P-1: looking at the logs",
        payload={"command": command["id"]},
    )
    assert notifier.on_event(env.store.events_since(0)[-1], None) == []
    assert read(env)["investigation"]["state"] == "running"
    assert read(env)["investigation"]["report"] is None
    # The final answer inherits the incident from its trusted command even without repeating P-n.
    env.store.add_thread_message(
        thread,
        author="agent:sysadmin",
        kind="text",
        body="Certificate is still valid for 29 days.",
        payload={"command": command["id"]},
    )
    env.store.command_state(command["id"], "done")
    event = env.store.events_since(0)[-1]
    assert event["kind"] == "run.finished"
    ids = notifier.on_event(event, None)
    assert env.store.notification(ids[0])["url"] == "/incidents/P-1"
    assert "29 days" in env.store.notification(ids[0])["body"]
    assert read(env)["investigation"]["state"] == "reviewed"
    assert "29 days" in read(env)["investigation"]["report"]["body"]


def summary(env, head, body="**Сводка:** P-1 ждёт вашего слова."):
    """The monitor's summary wake and the sysadmin's retelling of it; -> (command, message id)."""
    thread = env.store.agent_thread("sysadmin", "monitor")
    command = env.store.enqueue(
        {"key": "summary", "agent": "sysadmin", "thread": thread, "kind": "signal",
         "text": f"{head} ¦ P-1 всё ещё открыта · https://cabinet.example.test/incidents/P-1"}
    )
    for state in ("delivered", "started"):
        env.store.command_state(command["id"], state)
    env.store.add_thread_message(
        thread, author="agent:sysadmin", kind="text", body=body, payload={"command": command["id"]}
    )
    message = env.store._conn.execute(
        "SELECT id FROM messages WHERE thread=? ORDER BY seq DESC LIMIT 1", (thread,)
    ).fetchone()[0]
    return command, message


@pytest.mark.parametrize("head", ["[монитор] сводка за неделю", "[монитор] утренняя сводка"])
def test_the_monitor_summary_is_not_an_investigation(env, head):
    """the summary retells what is open; it investigates none of the incidents it names."""
    incident(env)
    notifier = notify.Notifier(env.store, env.users, None)
    command, _ = summary(env, head)
    ids = notifier.on_event(env.store.events_since(0)[-1], None)
    assert env.store.notification(ids[0])["url"] == "/agents/sysadmin?tab=monitor"
    env.store.command_state(command["id"], "done")
    found = read(env)["investigation"]
    assert found["report"] is None and found["state"] == "waiting"


def _drop_v37(conn):
    """What v37 adds: a replay of the migrations needs it gone (ADD COLUMN has no IF NOT EXISTS)."""
    conn.execute("DROP TABLE llm_models")
    for table in ("cards", "projects", "sessions"):
        for column in ("model", "effort"):
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    for column in ("queue_position", "queued_since"):  # and v39
        conn.execute(f"ALTER TABLE sessions DROP COLUMN {column}")
    for table in ("cards", "projects"):  # and v40
        conn.execute(f"ALTER TABLE {table} DROP COLUMN council")
    _undo_v43(conn)


def _undo_v43(conn):
    """What v43 drops: a replay of the migrations needs it back (DROP COLUMN has no IF EXISTS) — and what v44
    adds gone."""
    conn.execute("ALTER TABLE cards ADD COLUMN milestone TEXT NOT NULL DEFAULT ''")
    conn.execute("ALTER TABLE stages DROP COLUMN chain")


def test_migration_drops_the_summaries_already_attached(env):
    incident(env)
    reply(env)
    command, message = summary(env, "[монитор] утренняя сводка")
    env.store.command_state(command["id"], "done")
    path =env.store._conn.execute("PRAGMA database_list").fetchone()[2]
    with env.store.transaction():  # attached before the change
        env.store._conn.execute("INSERT OR IGNORE INTO incident_reports VALUES (1, ?)", (message,))
        _drop_v37(env.store._conn)  # v37 runs again too
        env.store._conn.execute("UPDATE meta SET value='34' WHERE key='schema_version'")
    assert incident_reports.latest(env.store, 1)["body"].startswith("**Сводка")
    env.store._conn.close()
    reopened = store.Store.open(path, clock=env.clock)
    assert incident_reports.latest(reopened, 1)["body"].startswith("**P-1")
    assert reopened._conn.execute("SELECT count(*) FROM incident_reports").fetchone()[0] == 1


def test_migration_attaches_existing_reports_without_replaying_messages(env):
    incident(env)
    reply(env)
    path = env.store._conn.execute("PRAGMA database_list").fetchone()[2]
    with env.store.transaction():
        env.store._conn.execute("DROP TABLE incident_reports")
        for column in (
            "confirmation_count",
            "confirmation_started_at",
            "confirmation_at",
        ):
            env.store._conn.execute(f"ALTER TABLE incidents DROP COLUMN {column}")
        env.store._conn.execute("ALTER TABLE projects DROP COLUMN memory_bank")  # v32 runs again too
        env.store._conn.execute("ALTER TABLE projects DROP COLUMN done_by")  # and v33
        _drop_v37(env.store._conn)  # and v37
        env.store._conn.execute("UPDATE meta SET value='29' WHERE key='schema_version'")
    env.store._conn.close()
    reopened = store.Store.open(path, clock=env.clock)
    assert incident_reports.latest(reopened, 1)["body"].startswith("**P-1")
    assert (
        reopened._conn.execute("SELECT confirmation_count FROM incidents").fetchone()[0]
        == 0
    )
    assert reopened.notifications("owner") == []


def test_a_range_of_incidents_is_each_of_them():
    """the sysadmin's report on a group names it P-a..P-b."""
    assert incident_reports.numbers("P-517..P-519, P-7") == [7, 517, 518, 519]
    assert incident_reports.numbers("P-9..P-1") == [1, 9]
    assert incident_reports.numbers("P-1..P-900") == [1, 900]


def answered(env, wake, body):
    """The monitor's wake and the sysadmin's final reply to it; -> the message id."""
    thread = env.store.agent_thread("sysadmin", "monitor")
    command = env.store.enqueue({"key": wake[:40], "agent": "sysadmin", "thread": thread, "kind": "signal",
                                 "text": wake})
    for state in ("delivered", "started"):
        env.store.command_state(command["id"], state)
    env.store.add_thread_message(thread, author="agent:sysadmin", kind="text", body=body,
                                 payload={"command": command["id"]})
    env.store.command_state(command["id"], "done")
    return env.store._conn.execute(
        "SELECT id FROM messages WHERE thread=? ORDER BY seq DESC LIMIT 1", (thread,)).fetchone()[0]


def linked(store_, message):
    return [r[0] for r in store_._conn.execute(
        "SELECT incident FROM incident_reports WHERE message=? ORDER BY incident", (message,))]


def test_a_reply_to_an_event_reports_on_its_incidents_not_on_those_it_lists_in_passing(env):
    """The reply to P-2/P-3 that ends "also open: P-1" is no report of P-1 — P-1 keeps its own."""
    for key in ("node-c:units:a.service", "node-c:units:b.service", "node-c:units:c.service"):
        incident(env, key)
    own = answered(env, "[монитор] P-1 открыта · node-c", "**P-1 · a.service упал**, перезапуск по плану.")
    other = answered(env, "[монитор] P-2 открыта ¦ P-3 открыта · node-c",
                     "P-2 и P-3: память, закрылись сами. Заодно остаётся открытым: P-1.")
    assert linked(env.store, own) == [1] and linked(env.store, other) == [2, 3]
    assert read(env, 1)["investigation"]["report"]["body"].startswith("**P-1 · a.service")
    # naming none of its event's incidents, the reply reports on all of them
    silent = answered(env, "[монитор] P-2 открыта", "Память вернулась в норму, P-1 по-прежнему ждёт.")
    assert linked(env.store, silent) == [2]
    # the owner's word in "Monitor" names no P-n: the reply reports on what it names
    free = answered(env, "Что с сервером node-c?", "P-1 и P-3 — разобрал выше.")
    assert linked(env.store, free) == [1, 3]
    # a wake cut at the command's limit lost the incidents past the cut: the reply reports on what it names
    cut = answered(env, ("[монитор] P-1 открыта · node-c · " + "x" * 2000)[:2000], "P-3: c.service, разобрал.")
    assert linked(env.store, cut) == [3]


def test_migration_takes_off_the_links_to_incidents_listed_in_passing(env):
    for key in ("node-c:units:a.service", "node-c:units:b.service", "node-c:units:c.service",
                "node-c:units:d.service"):
        incident(env, key)
    own = answered(env, "[монитор] P-1 открыта", "**P-1 · a.service упал.**")
    other = answered(env, "[монитор] P-2 открыта · https://cabinet.example.test/incidents/P-2",
                     "P-2: память. Остаётся открытым: P-1.")
    group = answered(env, "[монитор] P-3..P-4 открыты · node-c", "P-3..P-4: один и тот же юнит. И P-1.")
    cut = answered(env, ("[монитор] P-2 открыта · " + "x" * 2000)[:2000], "P-1: a.service, разобрал.")
    free = answered(env, "Что с сервером node-c?", "P-1 и P-2 — разобрал выше.")
    path = env.store._conn.execute("PRAGMA database_list").fetchone()[2]
    with env.store.transaction():  # linked by the old rule: every P-n of the text
        env.store._conn.executemany("INSERT OR IGNORE INTO incident_reports VALUES (?, ?)",
                                    [(1, other), (1, group)])
        _undo_v43(env.store._conn)  # v43 runs again too
        env.store._conn.execute("UPDATE meta SET value='40' WHERE key='schema_version'")
    assert incident_reports.latest(env.store, 1)["body"].startswith("P-1 и P-2")
    env.store._conn.close()
    reopened = store.Store.open(path, clock=env.clock)
    assert linked(reopened, own) == [1] and linked(reopened, other) == [2]
    assert linked(reopened, group) == [1, 3, 4]  # a range's wake is left as it is
    assert linked(reopened, free) == [1, 2]  # no P-n in its wake: nothing to judge by
    assert linked(reopened, cut) == [1]  # a wake cut at the limit: what it named past the cut is unknown
