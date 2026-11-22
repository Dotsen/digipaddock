"""notices — what root services and executors used to tell the owner in the Pult, now in the cabinet:
a thread message on a card or a feed event, a catalog key with parameters, pushed by level."""

import pytest
from cabinet_env import CabinetEnv

from kd.cabinet import api, i18n, notify, store, view
from kd.cabinet import client as cabinet_client


@pytest.fixture
def env(tmp_path):
    env = CabinetEnv(tmp_path)
    env.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root", language="ru",
                     timezone="Etc/GMT-3")
    env.users.create("vera", "Вера", roles=[("viewer", ["digipaddock"])], actor="root", language="ru",
                     timezone="Etc/GMT-3")
    env.users.create("anna", "Анна", roles=[("partner", ["digipaddock"])], actor="root", language="en",
                     timezone="Asia/Dubai")
    return env


def _notice(**over):
    request = {"op": "notice.post", "source": "watch", "level": "warn", "key": "notice.watch.recovered", "params": {}}
    request.update(over)
    return request


def _exec_result(card):
    params = {"agent": "@sysadmin", "server": "node-a", "duration": "1,5", "command": "uptime"}
    return _notice(source="exec", level="info", key="notice.exec.result", params=params, card=card)


def _code(env, user, request):
    with pytest.raises(api.ApiError) as exc:
        env.call(user, request)
    return exc.value.kind


def test_root_posts_a_cardless_notice_as_a_feed_event(env):
    before = env.store.last_seq()
    out = env.call("root", _notice())
    (event,) = env.store.events_since(before)
    assert event["kind"] == "notice" and event["card"] == "" and event["actor"] == "system:watch"
    assert event["payload"] == {"source": "watch", "level": "warn", "key": "notice.watch.recovered", "params": {}}
    assert out == {"ok": True, "event": event["seq"], "message": None}


def _person(user):
    return api.Principal("user", -1, user)


def _alerts(env, user="owner", **over):
    return api.dispatch(env.ctx, env.principal(user) if user == "root" else _person(user),
                        {"op": "notice.list", **over})


def test_the_platforms_alerts_are_its_warn_and_critical_notices_without_a_card_newest_first(env):
    """What the incidents page lists: a push about one of them leads there, not to the feed."""
    unit = {"host": "stage", "unit": "digipaddock-cabinet.service", "reason": "kd-cabinet: boom"}
    card = env.card(project="admin", assignee="@sysadmin")
    first = env.call("root", _notice())["event"]
    env.call("root", _notice(source="release", level="info", key="notice.release.healthy", params={"release": "r1"}))
    env.call("digi-exec", _exec_result(card.id))
    env.call("root", _notice(source="alert", level="warn", key="notice.alert.unit", params=unit, card=card.id))
    last = env.call("root", _notice(source="alert", level="critical", key="notice.alert.unit", params=unit))["event"]
    out = _alerts(env)
    assert [a["seq"] for a in out["items"]] == [last, first] and out["next"] is None
    assert out["items"][0] == {"seq": last, "ts": env.now, "source": "alert", "level": "critical",
                               "key": "notice.alert.unit", "params": unit}
    assert out["items"][1]["source"] == "watch" and out["items"][1]["level"] == "warn"
    assert _alerts(env, "root")["items"] == out["items"]


def test_the_alerts_page_by_the_sequence_number(env):
    seqs = [env.call("root", _notice(params={"n": n}))["event"] for n in range(5)]
    page = _alerts(env, limit=2)
    assert [a["seq"] for a in page["items"]] == seqs[:2:-1] and page["next"] == seqs[3]
    page = _alerts(env, limit=2, before=page["next"])
    assert [a["seq"] for a in page["items"]] == [seqs[2], seqs[1]] and page["next"] == seqs[1]
    page = _alerts(env, limit=2, before=page["next"])
    assert [a["seq"] for a in page["items"]] == [seqs[0]] and page["next"] is None
    whole = _alerts(env, limit=5)  # a page exactly as long as what is left ends the list
    assert [a["seq"] for a in whole["items"]] == seqs[::-1] and whole["next"] is None
    for bad in ({"limit": 0}, {"limit": 101}, {"limit": True}, {"before": -1}, {"before": "5"}, {"before": 10**20}):
        with pytest.raises(api.ApiError) as exc:
            _alerts(env, **bad)
        assert exc.value.kind == "invalid", bad


def test_the_alerts_are_read_with_the_settings_only(env):
    """The rule the feed has for the platform's notices: a partner and a viewer never see them."""
    env.call("root", _notice())
    for user in ("anna", "vera"):
        with pytest.raises(api.ApiError) as exc:
            _alerts(env, user)
        assert exc.value.kind == "refused", user
    assert _code(env, "digi-exec", {"op": "notice.list"}) == "refused"


def test_the_alerts_are_found_by_their_own_index(env):
    """The journal grows by tens of thousands of events without a card; the page reads only the notices."""
    names = {r[0] for r in env.store._conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "events_platform_notice" in names
    env.call("root", _notice())
    plan = " ".join(str(r[-1]) for r in env.store._conn.execute(
        "EXPLAIN QUERY PLAN " + store.PLATFORM_NOTICES, (10**9, 5)))
    assert "events_platform_notice" in plan


def test_a_card_notice_is_a_thread_message_and_one_event(env):
    card = env.card(project="admin", assignee="@sysadmin")
    before = env.store.last_seq()
    out = env.call("digi-exec", _exec_result(card.id))
    (message,) = [m for m in env.store.thread_messages(card.thread, 0) if m["kind"] == "notice"]
    assert message["author"] == "system:exec" and message["seq"] == out["message"]
    assert message["payload"]["level"] == "info" and message["payload"]["source"] == "exec"
    assert view.message_text(message) == "✅ @sysadmin · node-a · код 0, 1,5 с: uptime"
    events = env.store.events_since(before)
    assert [e["kind"] for e in events].count("notice") == 1
    added = [e for e in events if e["kind"] == "message.added"]
    assert len(added) == 1 and added[0]["payload"]["via"] == "notice"


def test_exec_and_forge_post_only_their_source_on_a_card(env):
    card = env.card()
    assert _code(env, "digi-exec", _notice(source="alert", card=card.id)) == "refused"
    assert _code(env, "digi-exec", _exec_result("")) in ("refused", "invalid")
    request = _notice(source="forge", level="critical", key="notice.forge.merge_refused",
                      params={"why": "x"}, card=card.id)
    assert _code(env, "digi-forge", request) == "refused"
    assert _code(env, "digi", _notice()) == "refused"  # the base user's executors post nothing of the kind
    request.update(level="warn")
    assert env.call("digi-forge", request)["message"] is not None


def test_a_notice_on_a_missing_card_is_not_found(env):
    assert _code(env, "digi-exec", _exec_result("KD-999")) == "not_found"


def test_unknown_key_or_large_params_are_refused(env):
    assert _code(env, "root", _notice(key="notice.watch.nothing")) == "invalid"
    assert _code(env, "root", _notice(key="push.digest.title")) == "invalid"
    assert _code(env, "root", _notice(params={f"p{i}": "x" for i in range(17)})) == "invalid"
    assert _code(env, "root", _notice(params={"lines": "x" * 2001})) == "invalid"
    assert _code(env, "root", _notice(params={"lines": ["x"]})) == "invalid"
    assert _code(env, "root", _notice(level="loud")) == "invalid"
    assert _code(env, "root", _notice(source="usage")) == "refused"


def test_principal_of_knows_the_forge_user(env):
    assert env.principal("digi-forge").kind == "forge"


def test_forge_reads_nothing_of_the_board(env):
    card = env.card()
    for request in ({"op": "card.list"}, {"op": "card.get", "card": card.id},
                    {"op": "thread.messages", "card": card.id}, {"op": "session.list"}):
        assert _code(env, "digi-forge", request) == "refused", request


def test_cardless_notice_is_seen_only_with_settings(env):
    env.call("root", _notice())
    owner = api.event_filter(env.ctx, api.Principal("user", -1, "owner"))(env.store.events_since(0))
    vera = api.event_filter(env.ctx, api.Principal("user", -1, "vera"))(env.store.events_since(0))
    assert [e["kind"] for e in owner].count("notice") == 1
    assert [e["kind"] for e in vera].count("notice") == 0


def test_post_notice_never_raises(env, capsys):
    assert cabinet_client.post_notice("watch", "warn", "notice.watch.recovered", {}, client=env.client("root"))

    class Down:
        def call(self, request):
            raise cabinet_client.CabinetError("unavailable", "no socket")

    assert cabinet_client.post_notice("watch", "warn", "notice.watch.recovered", {}, client=Down()) is False
    assert "kd-notice" in capsys.readouterr().err


# --- push -------------------------------------------------------------------------------------------------------


def _rows(env, n):
    ids = n.on_event(env.store.events_since(0)[-1])
    return [env.store.notification(i) for i in ids]


def _last_notice(env):
    return [e for e in env.store.events_since(0) if e["kind"] == "notice"][-1]


def test_critical_notice_pushes_now_even_in_quiet_hours(env):
    n = notify.Notifier(env.store, env.users, None)
    env.users.set_prefs("owner", {"quiet": "22:00-08:00"})
    env.now = "2026-10-14T20:30:00Z"  # 23:30 UTC3
    params = {"host": "stage", "unit": "digipaddock-cabinet.service", "reason": "kd-cabinet: boom"}
    env.call("root", _notice(source="alert", level="critical", key="notice.alert.unit", params=params))
    ids = n.on_event(_last_notice(env))
    (row,) = [env.store.notification(i) for i in ids]
    # the platform's own alert has no card: it leads to the alerts on the incidents page, never to the feed
    assert row["user"] == "owner" and row["due"] == env.now and row["url"] == notify.ALERTS_URL == "/incidents"
    assert row["title"] == i18n.t("ru", "push.notice.critical", card="")
    assert row["body"] == i18n.t("ru", "notice.alert.unit", **params)


def test_a_batched_platform_notice_is_no_line_of_the_monitors_batch(env):
    """The monitor's batch is of incidents P-n; an alert set to "every 30 minutes" joins the plain one."""
    n = notify.Notifier(env.store, env.users, None)
    env.users.set_prefs("owner", {"notice.warn": "batch"})
    env.call("root", _notice())
    ids = n.on_event(_last_notice(env))
    (row,) = [env.store.notification(i) for i in ids]
    assert row["kind"] == "batch" and row["tag"] == "batch" and row["url"] == notify.ALERTS_URL
    assert row["title"] == i18n.t("ru", "push.batch.title", count=1)


def test_warn_card_notice_pushes_to_controllers_of_the_project(env):
    n = notify.Notifier(env.store, env.users, None)
    card = env.card()
    env.call("digi-forge", _notice(source="forge", key="notice.forge.merge_refused", params={"why": "no approve"},
                                  card=card.id))
    ids = n.on_event(_last_notice(env))
    rows = [env.store.notification(i) for i in ids]
    assert [r["user"] for r in rows] == ["owner"]  # vera reads the card, anna may not control agents
    assert rows[0]["tag"] == "notice:forge" and rows[0]["url"].endswith(card.id)
    assert rows[0]["title"] == i18n.t("ru", "push.notice.warn", card=f" · {card.id}")


def test_info_notice_stays_in_the_feed(env):
    n = notify.Notifier(env.store, env.users, None)
    card = env.card(project="admin", assignee="@sysadmin")
    env.call("digi-exec", _exec_result(card.id))
    assert n.on_event(_last_notice(env)) == []
    assert notify.full_prefs({})["notice.info"] == "feed"
    assert notify.full_prefs({})["notice.warn"] == "now"


def test_root_posts_release_notices_on_a_card(env):
    """The health check of a release speaks as the root source ``release``."""
    card = env.card()
    out = env.call("root", {"op": "notice.post", "source": "release", "level": "critical",
                            "key": "notice.release.degraded", "card": card.id,
                            "params": {"rev": "abc1234", "failed": "cabinet_schema", "why": "schema 28 served"}})
    (message,) = [m for m in env.store.thread_messages(card.thread, 0) if m["kind"] == "notice"]
    assert message["author"] == "system:release" and message["seq"] == out["message"]
    assert "abc1234" in view.message_text(message) and "cabinet_schema" in view.message_text(message)
    env.call("root", {"op": "notice.post", "source": "release", "level": "info", "key": "notice.release.healthy",
                      "card": card.id, "params": {"rev": "abc1234", "checks": "session_host, nginx"}})
    with pytest.raises(api.ApiError, match="does not post"):
        env.call("digi-exec", {"op": "notice.post", "source": "release", "level": "info",
                              "key": "notice.release.healthy", "card": card.id, "params": {"rev": "a", "checks": ""}})
