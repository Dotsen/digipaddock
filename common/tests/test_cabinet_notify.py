"""Who gets a push for which event, when, in which language; reminders, the morning digest."""

import json

import pytest
from cabinet_env import CabinetEnv

from kd.cabinet import api, decisions, i18n, notify

PLAN = {"proposal_sha256": "a" * 64, "card_scope_sha256": "b" * 64}


def _env(tmp_path):
    env = CabinetEnv(tmp_path)
    env.users.create(
        "owner",
        "Владелец",
        roles=[("owner", "*")],
        actor="root",
        language="ru",
        timezone="Etc/GMT-3",
    )
    env.users.create(
        "anna",
        "Анна",
        roles=[("partner", ["web"])],
        actor="root",
        language="en",
        timezone="Asia/Dubai",
    )
    return env, notify.Notifier(env.store, env.users, None, usage_path=str(tmp_path / "usage.jsonl"))


def person(user):
    return api.Principal("user", -1, user)


def test_a_waiting_decision_notifies_those_who_may_decide_in_their_language(tmp_path):
    env, n = _env(tmp_path)
    d = decisions.Decisions(env.store)
    card = env.card(project="admin")
    opened = d.open(
        "admin_plan",
        card=card.id,
        payload={"task_id": card.id, **PLAN},
        requested_by="agent:sysadmin",
        now=env.now,
    )
    ids = n.on_event(env.store.events_since(0)[-1], d)
    rows = [env.store.notification(i) for i in ids]
    assert {r["user"] for r in rows} == {
        "owner"
    }  # anna has no decide.admin_plan and no admin project
    assert (
        rows[0]["tag"] == card.id
        and rows[0]["due"] == env.now
        and rows[0]["url"].endswith(opened["id"])
    )
    q = d.open(
        "question",
        card=env.card(project="web").id,
        payload={"text": "?"},
        requested_by="agent:dev",
        now=env.now,
    )
    ids = n.on_event(env.store.events_since(0)[-1], d)
    rows = [env.store.notification(i) for i in ids]
    assert {r["user"] for r in rows} == {"owner", "anna"}
    anna = [r for r in rows if r["user"] == "anna"][0]
    assert anna["title"] != [r for r in rows if r["user"] == "owner"][0]["title"]
    assert (
        anna["title"] == i18n.t("en", "push.question", agent="@dev", card=q["card"])
        and anna["body"] == "?"
    )


def test_batch_kinds_wait_for_the_half_hour_and_feed_kinds_are_not_queued(tmp_path):
    env, n = _env(tmp_path)
    card = env.card(project="web", status="In Progress")
    env.store.edit_card(card.id, card.revision, {"status": "Done"}, actor="agent:dev")
    ids = n.on_event(env.store.events_since(0)[-1], None)
    assert (
        all(env.store.notification(i)["due"] == "2026-10-14T10:30:00Z" for i in ids)
        and ids
    )
    env.store.add_message(card.id, author="agent:dev", kind="progress", body="step 1")
    assert n.on_event(env.store.events_since(0)[-1], None) == []


def test_one_batch_per_person_and_half_hour(tmp_path):
    env, n = _env(tmp_path)
    first, second = (
        env.card(project="web", title="One"),
        env.card(project="web", title="Two"),
    )
    for card in (first, second):
        env.store.edit_card(
            card.id, card.revision, {"status": "Done"}, actor="agent:dev"
        )
        n.on_event(env.store.events_since(0)[-1], None)
    (row,) = [r for r in env.store.notifications("owner") if r["kind"] == "batch"]
    assert row["title"] == i18n.t("ru", "push.batch.title", count=2)
    assert (
        "One" in row["body"]
        and "Two" in row["body"]
        and row["due"] == "2026-10-14T10:30:00Z"
    )


def test_a_batch_of_one_card_leads_to_the_card_and_of_several_to_the_board(tmp_path):
    """The feed is the journal, not a place to work in: a push never opens it."""
    env, n = _env(tmp_path)
    first, second = env.card(project="web", title="One"), env.card(project="web", title="Two")
    env.store.edit_card(first.id, first.revision, {"status": "Done"}, actor="agent:dev")
    n.on_event(env.store.events_since(0)[-1], None)
    (row,) = [r for r in env.store.notifications("owner") if r["kind"] == "batch"]
    assert row["url"].endswith(f"/cards/{first.id}")
    env.store.edit_card(second.id, second.revision, {"status": "Done"}, actor="agent:dev")
    n.on_event(env.store.events_since(0)[-1], None)
    (row,) = [r for r in env.store.notifications("owner") if r["kind"] == "batch"]
    assert row["url"] == notify.BOARD_URL == "/board"


def test_the_digest_leads_to_the_dashboard(tmp_path):
    env, n = _env(tmp_path)
    assert n.tick("2026-10-15T06:00:00Z")["digests"] == 1
    (row,) = [r for r in env.store.notifications("owner") if r["kind"] == "digest"]
    assert row["url"] == notify.DASHBOARD_URL == "/"


def test_quiet_hours_hold_everything_but_critical(tmp_path):
    env, n = _env(tmp_path)
    env.users.set_prefs("owner", {"quiet": "22:00-08:00"})
    env.now = "2026-10-14T20:30:00Z"  # 23:30 UTC3
    d = decisions.Decisions(env.store)
    card = env.card(project="admin")
    d.open(
        "admin_plan",
        card=card.id,
        payload={"task_id": card.id, **PLAN},
        requested_by="agent:sysadmin",
        now=env.now,
    )
    held = n.on_event(env.store.events_since(0)[-1], d)
    assert env.store.notification(held[0])["due"] == "2026-10-15T05:00:00Z"
    d.open(
        "admin_critical",
        card=card.id,
        payload={"task_id": card.id, "proposal_id": "c" * 16, **PLAN},
        requested_by="agent:sysadmin",
        now=env.now,
    )
    now_ids = n.on_event(env.store.events_since(0)[-1], d)
    assert env.store.notification(now_ids[0])["due"] == env.now


def test_tick_reminds_at_4h_and_24h_and_builds_the_morning_digest_once(tmp_path):
    env, n = _env(tmp_path)
    d = decisions.Decisions(env.store)
    card = env.card(project="admin")
    d.open(
        "admin_plan",
        card=card.id,
        payload={"task_id": card.id, **PLAN},
        requested_by="agent:sysadmin",
        now=env.now,
    )
    assert n.tick("2026-10-14T13:59:00Z")["reminders"] == 0
    assert (
        n.tick("2026-10-14T14:01:00Z")["reminders"] == 1
        and n.tick("2026-10-14T14:05:00Z")["reminders"] == 0
    )
    assert n.tick("2026-10-15T10:01:00Z")["reminders"] == 1
    assert n.tick("2026-10-15T06:00:00Z")["digests"] == 1  # 09:00 UTC3 for owner
    assert n.tick("2026-10-15T06:30:00Z")["digests"] == 0
    assert n.tick("2026-10-15T05:00:00Z")["digests"] == 1  # 09:00 Dubai for anna


def test_the_digest_is_also_a_journal_event_of_its_person(tmp_path):
    env, n = _env(tmp_path)
    assert n.tick("2026-10-15T06:00:00Z")["digests"] == 1
    digests = [e for e in env.store.events_since(0) if e["kind"] == "digest"]
    assert len(digests) == 1 and digests[0]["card"] == ""
    payload = digests[0]["payload"]
    # no ledger: the agents' spend is 0 k, Hindsight's is unknown — said so, not a zero
    assert payload["user"] == "owner" and payload["key"] == "push.digest.body.spend_nodata"
    assert set(payload["params"]) == {"decisions", "done", "cost"}
    assert set(payload["incidents"]) == {"crit", "warn", "opened", "closed", "list"}  # the owner reads the servers


def test_the_digest_counts_open_decisions_review_done_and_tokens(tmp_path):
    env, _ = _env(tmp_path)
    usage = tmp_path / "usage.jsonl"
    usage.write_text(
        json.dumps(
            {
                "day": "2026-10-14",
                "agent": "dev",
                "model": "m",
                "input": 1000,
                "output": 500,
                "cache_write": 500,
                "cache_read": 90000,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    n = notify.Notifier(env.store, env.users, None, usage_path=str(usage))
    d = decisions.Decisions(env.store)
    card = env.card(project="admin")
    d.open(
        "admin_plan",
        card=card.id,
        payload={"task_id": card.id, **PLAN},
        requested_by="agent:sysadmin",
        now=env.now,
    )
    review = env.card(project="web", status="In Progress")
    env.store.edit_card(
        review.id, review.revision, {"status": "Review"}, actor="agent:dev"
    )
    done = env.card(project="web", status="In Progress")
    env.store.edit_card(done.id, done.revision, {"status": "Done"}, actor="agent:dev")
    assert n.tick("2026-10-15T06:00:00Z")["digests"] == 1
    (row,) = [r for r in env.store.notifications("owner") if r["kind"] == "digest"]
    assert row["title"] == i18n.t("ru", "push.digest.title")
    first, _ = row["body"].split("\n")
    assert first == i18n.t("ru", "push.digest.body.spend_nodata", decisions=1, done=1, cost="2 k")
    assert first.endswith("расход: агенты 2 k · Hindsight: нет данных")


def _digest_of(tmp_path, rows):
    env, _ = _env(tmp_path)
    usage = tmp_path / "usage.jsonl"
    usage.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    n = notify.Notifier(env.store, env.users, None, usage_path=str(usage))
    assert n.tick("2026-10-15T06:00:00Z")["digests"] == 1
    (row,) = [r for r in env.store.notifications("owner") if r["kind"] == "digest"]
    (event,) = [e for e in env.store.events_since(0) if e["kind"] == "digest"]
    return row["body"].split("\n")[0], event["payload"]  # the second line is the servers' part


def _hs(model, coverage="full", **numbers):
    return {"day": "2026-10-14", "agent": "hindsight", "category": "hindsight", "model": model,
            "coverage": coverage, **numbers}


AGENT_ROW = {"day": "2026-10-14", "agent": "dev", "model": "m", "input": 1000, "output": 500,
             "cache_write": 500, "cache_read": 90000, "cost_usd": 1.5}


def test_the_digest_splits_agents_and_hindsight_and_sums_its_models(tmp_path):
    body, payload = _digest_of(tmp_path, [
        AGENT_ROW,
        _hs("claude-haiku-4-5", input=2000, output=400, cache_write=600, cache_read=50000),
        _hs("claude-sonnet-5-5", input=1000, output=0, cache_write=0, cache_read=0),
        _hs("other-day", input=9_000_000) | {"day": "2026-10-13"},
    ])
    assert body.endswith("расход: агенты 2 k · $1.50 · Hindsight 4 k")
    assert payload["key"] == "push.digest.body.spend"
    assert payload["params"]["cost"] == "2 k · $1.50" and payload["params"]["hindsight"] == "4 k"
    assert "запрос" not in body and "кеш" not in body


def test_the_digest_marks_partial_hindsight(tmp_path):
    body, payload = _digest_of(tmp_path, [AGENT_ROW, _hs("claude-haiku-4-5", "partial", input=3000)])
    assert payload["key"] == "push.digest.body.spend_partial"
    assert body.endswith("агенты 2 k · $1.50 · Hindsight ≥3 k (неполные данные)")


def test_the_digest_says_no_data_for_hindsight_without_numbers(tmp_path):
    body, payload = _digest_of(tmp_path, [AGENT_ROW, _hs("-", "none")])
    assert payload["key"] == "push.digest.body.spend_nodata" and "hindsight" not in payload["params"]
    assert body.endswith("агенты 2 k · $1.50 · Hindsight: нет данных")


def test_a_confirmed_zero_of_hindsight_is_a_zero(tmp_path):
    body, _ = _digest_of(tmp_path, [AGENT_ROW, _hs("-", input=0, output=0, cache_write=0, cache_read=0)])
    assert body.endswith("Hindsight 0 k")


def test_the_digest_names_the_spend_only_to_who_may_see_settings(tmp_path):
    env, n = _env(tmp_path)
    assert n.tick("2026-10-15T05:00:00Z")["digests"] == 1  # 09:00 Dubai for anna
    (row,) = [r for r in env.store.notifications("anna") if r["kind"] == "digest"]
    assert row["body"] == i18n.t("en", "push.digest.body.team", decisions=0, done=0)
    (event,) = [e for e in env.store.events_since(0) if e["kind"] == "digest"]
    assert event["payload"]["key"] == "push.digest.body.team"
    assert "cost" not in event["payload"]["params"]


def test_the_actor_is_not_told_about_his_own_change(tmp_path):
    env, n = _env(tmp_path)
    card = env.card(project="web", status="In Progress")
    env.store.edit_card(
        card.id, card.revision, {"status": "Review"}, actor="user:owner"
    )
    ids = n.on_event(env.store.events_since(0)[-1], None)
    assert {env.store.notification(i)["user"] for i in ids} == {"anna"}


def test_a_card_blocked_on_my_card_tells_me(tmp_path):
    env, n = _env(tmp_path)
    mine = env.card(project="web", assignee="@anna", title="Ключи")
    blocked = env.card(project="web", status="In Progress")
    env.store.edit_card(
        blocked.id,
        blocked.revision,
        {"status": "Blocked", "dependencies": [mine.id]},
        actor="agent:sysadmin",
    )
    ids = n.on_event(env.store.events_since(0)[-1], None)
    (row,) = [env.store.notification(i) for i in ids]
    assert (
        row["user"] == "anna"
        and row["kind"] == "card.blocked_on_me"
        and row["tag"] == blocked.id
    )
    assert row["due"] == env.now


def test_feed_pref_silences_a_kind_and_events_are_handled_once(tmp_path):
    env, n = _env(tmp_path)
    env.users.set_prefs("owner", {"card.review": "feed"})
    card = env.card(project="web", status="In Progress")
    env.store.edit_card(card.id, card.revision, {"status": "Review"}, actor="agent:dev")
    event = env.store.events_since(0)[-1]
    ids = n.on_event(event, None)
    assert {env.store.notification(i)["user"] for i in ids} == {"anna"}
    assert (
        n.on_event(event, None) == []
    )  # the service and the scheduler may both see it
    assert n.tick(env.now)["events"] == 0


def test_tick_catches_up_on_events_but_not_on_history(tmp_path):
    env, n = _env(tmp_path)
    old = env.card(project="web", status="In Progress")
    env.store.edit_card(old.id, old.revision, {"status": "Review"}, actor="agent:dev")
    assert (
        n.tick(env.now)["events"] == 0
    )  # the first tick starts at the end of the journal
    card = env.card(project="web", status="In Progress")
    env.store.edit_card(card.id, card.revision, {"status": "Review"}, actor="agent:dev")
    assert n.tick(env.now)["events"] == 2
    assert {r["tag"] for r in env.store.notifications("owner")} == {card.id}


def test_sent_notifications_are_purged_after_30_days(tmp_path):
    env, n = _env(tmp_path)
    card = env.card(project="web", status="In Progress")
    env.store.edit_card(card.id, card.revision, {"status": "Review"}, actor="agent:dev")
    ids = n.on_event(env.store.events_since(0)[-1], None)
    env.store.notifications_sent(
        [{"id": ids[0], "endpoint": "", "ok": True, "status": 201}]
    )
    assert (
        n.tick("2026-11-14T10:00:01Z")["purged"] == 2
    )  # the sent one, and the one nobody could receive


def test_prefs_are_checked_and_returned_with_the_defaults(tmp_path):
    env, _ = _env(tmp_path)
    out = api.dispatch(
        env.ctx,
        person("anna"),
        {"op": "prefs.set", "prefs": {"card.done": "now", "quiet": "23:00-07:30"}},
    )
    assert out["prefs"]["card.done"] == "now" and out["prefs"]["quiet"] == "23:00-07:30"
    assert (
        out["prefs"]["progress"] == "feed" and out["prefs"]["decision.waiting"] == "now"
    )
    for bad in ({"card.done": "loud"}, {"nope": "now"}, {"quiet": "25:00-07:00"}, "x"):
        with pytest.raises(api.ApiError) as exc:
            api.dispatch(env.ctx, person("anna"), {"op": "prefs.set", "prefs": bad})
        assert exc.value.kind == "invalid"
    assert (
        api.dispatch(env.ctx, person("owner"), {"op": "prefs.get"})["prefs"][
            "card.done"
        ]
        == "batch"
    )


def test_subscriptions_belong_to_their_person(tmp_path):
    env, _ = _env(tmp_path)
    sub = {
        "op": "push.subscribe",
        "endpoint": "https://fcm.googleapis.com/fcm/send/a",
        "keys": {"p256dh": "k" * 20, "auth": "a" * 8},
        "device": "iPhone",
    }
    api.dispatch(env.ctx, person("anna"), sub)
    api.dispatch(env.ctx, person("anna"), sub)  # the same device again replaces it
    assert [s["endpoint"] for s in env.store.subscriptions("anna")] == [
        "https://fcm.googleapis.com/fcm/send/a"
    ]
    with pytest.raises(api.ApiError):
        api.dispatch(
            env.ctx,
            person("owner"),
            {**sub, "endpoint": "http://fcm.googleapis.com/fcm/send/b"},
        )  # https only
    api.dispatch(
        env.ctx,
        person("owner"),
        {"op": "push.unsubscribe", "endpoint": "https://fcm.googleapis.com/fcm/send/a"},
    )
    assert env.store.subscriptions("anna")  # not his to remove
    api.dispatch(
        env.ctx,
        person("anna"),
        {"op": "push.unsubscribe", "endpoint": "https://fcm.googleapis.com/fcm/send/a"},
    )
    assert env.store.subscriptions("anna") == []


def test_notify_tick_is_for_the_service_itself(tmp_path):
    env, n = _env(tmp_path)
    env.ctx.notifier = n
    assert set(env.call("digi-cabinet", {"op": "notify.tick"})["tick"]) >= {
        "events",
        "reminders",
        "digests",
        "purged",
    }
    with pytest.raises(api.ApiError):
        api.dispatch(env.ctx, person("owner"), {"op": "notify.tick"})


def test_a_subscription_must_point_at_a_known_push_service(tmp_path):
    # the push sender reaches the network: a page may not aim it at the host's own neighbours
    env, _ = _env(tmp_path)
    keys = {"p256dh": "k" * 20, "auth": "a" * 8}
    for good in (
        "https://fcm.googleapis.com/fcm/send/x",
        "https://fcm.googleapis.com:443/fcm/send/x",
        "https://updates.push.services.mozilla.com/wpush/v2/x",
        "https://web.push.apple.com/QG-x",
        "https://wns2-par02p.notify.windows.com/w/?token=x",
    ):
        api.dispatch(
            env.ctx,
            person("anna"),
            {"op": "push.subscribe", "endpoint": good, "keys": keys},
        )
    for bad in (
        "https://169.254.169.254/latest/meta-data",
        "https://127.0.0.1:9077/x",
        "https://localhost/x",
        "https://evil.example/x",
        "https://fcm.googleapis.com.evil.example/x",
        "https://evilpush.apple.com/x",
        "https://push.apple.com/x",
        "https://user@fcm.googleapis.com/x",
        "https://fcm.googleapis.com:8443/x",
        "https://fcm.googleapis.com:99999/x",
    ):
        with pytest.raises(api.ApiError) as exc:
            api.dispatch(
                env.ctx,
                person("anna"),
                {"op": "push.subscribe", "endpoint": bad, "keys": keys},
            )
        assert exc.value.kind == "invalid", bad


def test_a_failed_session_pushes_once(tmp_path):
    env, n = _env(tmp_path)
    card = env.card(project="web", title="Deploy")
    session = env.store.session_for(card.thread, "dev")
    env.store.session_update(session["id"], state="working")
    assert n.on_event(env.store.events_since(0)[-1], None) == []
    env.store.session_update(session["id"], state="failed", error="turn failed")
    ids = n.on_event(env.store.events_since(0)[-1], None)
    rows = [env.store.notification(i) for i in ids]
    # anna is a partner: she reads web cards but has no agents.control
    assert [r["user"] for r in rows] == ["owner"]
    assert rows[0]["kind"] == "session.failed" and rows[0]["tag"] == "session:dev"
    assert rows[0]["title"] == i18n.t("ru", "push.session.failed", agent="@dev", card=card.id)
    assert rows[0]["due"] == env.now and rows[0]["url"].endswith(card.id)
    env.store.session_update(session["id"], error="turn failed again")  # still failed: no second push
    assert n.on_event(env.store.events_since(0)[-1], None) == []
    chat = env.store.session_for(env.store.agent_thread("analyst"), "analyst")
    env.store.session_update(chat["id"], state="failed", error="exit 1")
    (row,) = [env.store.notification(i) for i in n.on_event(env.store.events_since(0)[-1], None)]
    assert row["user"] == "owner" and row["url"] == "/agents/analyst"
    # final review: the body says why in words, as the card's run state does — not the host's code
    assert row["body"] == "процесс сессии завершился (код 1)"
    assert rows[0]["body"] == "ход не завершился"  # "turn failed"


@pytest.mark.parametrize("code, text", [
    ("host_restart", "хост сессий перезапустился"),
    ("spawn_failed", "сессию не удалось запустить"),
    ("exit 143", "процесс сессии завершился (код 143)"),
    ("host_refused: no policy", "хост сессий отказал"),
    ("exit", "ошибка исполнения"),
    ("", "ошибка исполнения"),
    ("weird thing", "ошибка исполнения"),
])
def test_a_failure_code_reads_in_words(code, text):
    assert i18n.reason("ru", code) == text


def test_an_agents_chat_message_pushes_the_people_who_may_read_the_chat(tmp_path):
    env, n = _env(tmp_path)
    env.users.create("bob", "Bob", roles=[("partner", "*")], actor="root", language="en")
    thread = env.store.agent_thread("analyst")
    env.store.add_thread_message(thread, author="agent:analyst", kind="text", body="x" * 300)
    ids = n.on_event(env.store.events_since(0)[-1], None)
    rows = {r["user"]: r for r in (env.store.notification(i) for i in ids)}
    assert set(rows) == {"owner", "bob"}  # anna's agents.chat is for web only, the chat has no project
    seq = env.store.thread_messages(thread)[-1]["seq"]
    assert rows["owner"]["kind"] == "agent.message" and rows["owner"]["tag"] == f"chat:analyst:seq:{seq}"
    assert rows["bob"]["title"] == i18n.t("en", "push.agent.reply", agent="ANALYST", count=1)
    assert rows["owner"]["body"] == "x" * notify.BODY_MAX and rows["owner"]["url"] == "/agents/analyst"
    assert notify.default_mode("agent.message") == "now"
    env.store.add_thread_message(thread, author="agent:analyst", kind="tool_call", body="kd-task view")
    assert n.on_event(env.store.events_since(0)[-1], None) == []
    card = env.card(project="web")
    env.store.add_message(card.id, author="agent:dev", kind="text", body="done")
    assert n.on_event(env.store.events_since(0)[-1], None) == []  # a card thread's text stays in the feed


def test_a_persons_own_chat_message_pushes_nobody(tmp_path):
    env, n = _env(tmp_path)
    thread = env.store.agent_thread("analyst")
    env.store.add_thread_message(thread, author="user:owner", kind="text", body="hello")
    assert n.on_event(env.store.events_since(0)[-1], None) == []


# --- the monitor's incidents ----------------------------------------------------------------
AT = "2026-10-14T13:00:00+03:00"  # the monitor's clock: 10:00Z, the cabinet's now


def monitor(env, *items):
    return env.call("digi-sysadmin", {"op": "monitor.push", "items": list(items)})["results"]


def problem(key="node-c:disk:/", level="crit", value="96%"):
    server, check, subject = key.split(":", 2)
    return {"key": key, "server": server, "check": check, "subject": subject, "level": level, "max_level": level,
            "value": value, "text": "", "opened_at": AT, "repeats": 0}


def opened(item, **over):
    return {"key": item, "kind": "open", "problem": problem(**over)}


def change(item, what, level, value="97%", key="node-c:disk:/"):
    out = {"key": item, "kind": "update", "problem_key": key, "opened_at": AT, "change": what, "level": level,
           "value": value, "at": AT}
    if what == "fixed":
        out["card"] = "KD-5"
    return out


def resolve(item, key="node-c:disk:/"):
    return {"key": item, "kind": "resolve", "problem_key": key, "opened_at": AT, "at": AT}


def handled(env, n):
    """The rows the incident events not handled yet add (the cursor moves past every event)."""
    rows = []
    for event in env.store.events_since(env.store.get_state(notify.CURSOR) or 0):
        rows += [env.store.notification(i) for i in n.on_event(event, None)]
    return rows


def _incident_env(tmp_path):
    env, n = _env(tmp_path)
    env.store.set_state(notify.CURSOR, env.store.last_seq())
    return env, n


def test_a_new_crit_pushes_at_once_even_in_quiet_hours_to_who_reads_the_servers(tmp_path):
    env, n = _incident_env(tmp_path)
    env.users.set_prefs("owner", {"quiet": "12:00-14:00"})  # 13:00 UTC3: inside
    monitor(env, opened("a"))
    (row,) = handled(env, n)  # anna is a partner: no servers.read
    assert row["user"] == "owner" and row["kind"] == "incident.critical" and row["due"] == env.now
    assert row["tag"] == "P-1" and row["url"] == "/incidents/P-1"  # its page
    assert row["title"] == "Критично · P-1 · node-c · disk /: 96%" and row["body"] == "Новый инцидент"
    assert notify.default_mode("incident.critical") == "now"


def test_warn_to_crit_and_a_failed_fix_push_at_once_and_a_rise_within_crit_does_not(tmp_path):
    env, n = _incident_env(tmp_path)
    monitor(env, opened("a", level="warn", value="91%"))
    handled(env, n)
    monitor(env, change("w", "worse", "crit", "97%"))
    (row,) = handled(env, n)
    assert row["kind"] == "incident.critical" and row["tag"] == "P-1" and row["body"] == "Стало критичным"
    assert "97%" in row["title"]
    monitor(env, change("w2", "worse", "crit", "99%"))
    assert handled(env, n) == []
    monitor(env, change("f", "fix_failed", "crit", "99%"))
    (row,) = handled(env, n)
    assert row["kind"] == "incident.critical" and row["body"] == "Исправление не помогло"


def test_a_new_warn_is_a_line_of_the_half_hour_batch_and_waits_for_quiet_hours(tmp_path):
    env, n = _incident_env(tmp_path)
    monitor(env, opened("a", level="warn", value="91%"))
    (row,) = handled(env, n)
    assert row["kind"] == "batch" and row["due"] == "2026-10-14T10:30:00Z"
    assert row["body"] == "P-1 · node-c · disk /: 91%"
    env.users.set_prefs("owner", {"quiet": "12:00-14:00"})
    monitor(env, opened("b", key="node-c:mem:ram", level="warn", value="88%"))
    (row,) = handled(env, n)
    assert row["kind"] == "batch" and row["due"] == "2026-10-14T11:00:00Z"  # 14:00 UTC3
    for item in (change("w", "worse", "warn", "93%"), change("b2", "better", "warn", "90%"),
                 change("x", "fixed", "warn", "90%")):
        monitor(env, item)
    assert handled(env, n) == []  # a rise within warn, better and fixed stay in the feed


def test_acknowledged_or_muted_is_silent_but_for_crit_and_a_failed_fix(tmp_path):
    env, n = _incident_env(tmp_path)
    monitor(env, opened("a", level="warn"))
    env.call("digi-sysadmin", {"op": "incident.ack", "incident": 1})
    assert handled(env, n) == []  # handled after the ack: the opening is silent
    monitor(env, change("w", "worse", "crit"))
    assert [r["kind"] for r in handled(env, n)] == ["incident.critical"]
    monitor(env, opened("b", key="node-c:mem:ram", level="crit"))
    env.call("digi-sysadmin", {"op": "incident.mute", "incident": 2, "until": "2026-10-14T12:00:00Z"})
    assert handled(env, n) == []
    monitor(env, change("f", "fix_failed", "crit", key="node-c:mem:ram"))
    assert [r["tag"] for r in handled(env, n)] == ["P-2"]


def test_a_closing_an_import_and_an_incident_closed_by_now_push_nothing(tmp_path):
    env, n = _incident_env(tmp_path)
    monitor(env, opened("a"), resolve("r"))  # the monitor's queue after an outage: closed meanwhile
    assert handled(env, n) == []
    env.call("digi-sysadmin", {"op": "incident.import", "next_id": 10, "items": [
        {"number": 7, **problem("node-d:reach:-"), "history": [{"at": AT, "kind": "open", "level": "crit"}]}]})
    assert handled(env, n) == []
    monitor(env, opened("b", key="node-c:mem:ram", level="crit"))
    assert len(handled(env, n)) == 1
    monitor(env, resolve("r2", key="node-c:mem:ram"))
    assert handled(env, n) == []


def test_only_active_people_with_servers_read_are_told_and_a_broken_event_is_skipped(tmp_path):
    env, n = _incident_env(tmp_path)
    env.store._conn.execute("INSERT INTO roles (id, perms) VALUES ('watch', '[\"servers.read\"]')")
    env.users.create("ops", "Ops", roles=[("watch", "*")], actor="root", language="en")
    env.users.create("gone", "Gone", roles=[("watch", "*")], actor="root")
    env.store._conn.execute("UPDATE users SET status='blocked' WHERE id='gone'")
    env.store.set_state(notify.CURSOR, env.store.last_seq())
    monitor(env, opened("a"))
    rows = handled(env, n)
    assert {r["user"] for r in rows} == {"owner", "ops"}
    ops = [r for r in rows if r["user"] == "ops"][0]
    assert ops["title"] == "Critical · P-1 · node-c · disk /: 96%" and ops["body"] == "New incident"
    events = [e for e in env.store.events_since(0) if e["kind"].startswith("incident.")]
    assert api.event_filter(env.ctx, person("anna"))(events) == []  # the partner's feed has no incident lines
    broken = dict(events[-1], seq=env.store.last_seq() + 1, payload={"incident": "x"})
    assert n.on_event(broken, None) == []


def test_the_digest_has_the_servers_part_only_for_who_reads_the_servers(tmp_path):
    env, n = _incident_env(tmp_path)
    monitor(env, opened("a"), opened("b", key="node-c:mem:ram", level="warn"),
            opened("c", key="node:disk:/", level="crit"), opened("d", key="vpn:load:-", level="crit"),
            resolve("r", key="vpn:load:-"))
    env.call("digi-sysadmin", {"op": "incident.import", "next_id": 10, "items": [
        {"number": 7, **problem("old:disk:/"), "opened_at": "2026-10-10T10:00:00Z"}]})
    assert n.tick("2026-10-15T06:00:00Z")["digests"] == 1  # 09:00 UTC3 for the owner
    (row,) = [r for r in env.store.notifications("owner") if r["kind"] == "digest"]
    first, second = row["body"].split("\n")
    assert first == i18n.t("ru", "push.digest.body.spend_nodata", decisions=0, done=0, cost="0 k")
    assert second == "Инциденты: crit 3 · warn 1 · за сутки открыто 4, закрыто 1 · P-3, P-1, P-7"
    (event,) = [e for e in env.store.events_since(0) if e["kind"] == "digest"]
    assert event["payload"]["incidents"] == {"crit": 3, "warn": 1, "opened": 4, "closed": 1,
                                             "list": " · P-3, P-1, P-7"}
    assert n.tick("2026-10-15T05:00:00Z")["digests"] == 1  # anna, 09:00 Dubai
    (row,) = [r for r in env.store.notifications("anna") if r["kind"] == "digest"]
    assert row["body"] == i18n.t("en", "push.digest.body.team", decisions=0, done=0)
    assert "incidents" not in [e for e in env.store.events_since(0) if e["kind"] == "digest"][-1]["payload"]


def test_the_digest_names_at_most_five_open_crit_newest_first(tmp_path):
    env, n = _incident_env(tmp_path)
    monitor(env, *[opened(f"k{i}", key=f"node-c:disk:/d{i}") for i in range(7)])
    n.tick("2026-10-15T06:00:00Z")
    (event,) = [e for e in env.store.events_since(0) if e["kind"] == "digest"]
    assert event["payload"]["incidents"]["list"] == " · P-7, P-6, P-5, P-4, P-3"
