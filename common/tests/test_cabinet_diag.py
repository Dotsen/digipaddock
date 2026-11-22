"""R31, the cabinet keeps diagnostics — diag.record writes one message per (card, target, key) and reuse
window, diag.recent answers the stored one and the card's count of the hour; only the executor, only for the
card's single assignee on an approved card."""

import pytest
from cabinet_env import CabinetEnv

from kd.cabinet import api, view

AT = "2026-10-14T10:00:00Z"


@pytest.fixture
def env(tmp_path):
    return CabinetEnv(tmp_path)


def _record(card, **over):
    request = {"op": "diag.record", "card": card, "agent": "dev", "target": "hindsight-api", "probe": "http-health",
               "key": "hindsight-api", "area": "sysadmin", "outcome": {"status": 200, "ms": 12}, "at": AT,
               "reuse_s": 600}
    request.update(over)
    return request


def _recent(card, **over):
    request = {"op": "diag.recent", "card": card, "agent": "dev", "target": "hindsight-api", "key": "hindsight-api",
               "within_s": 600}
    request.update(over)
    return request


def _messages(env, card):
    return [m for m in env.store.thread_messages(env.store.card(card).thread, 0) if m["kind"] == "diagnostic"]


def test_diag_message_in_thread_has_the_structured_payload(env):
    card = env.card()
    out = env.call("digi", _record(card.id))
    (message,) = _messages(env, card.id)
    assert message["author"] == "system:diag" and out["reused"] is False
    assert message["payload"]["diag"] == {"target": "hindsight-api", "op": "http-health", "key": "hindsight-api",
                                          "status": 200, "ms": 12, "at": AT, "agent": "dev", "area": "sysadmin"}
    assert out["record"] == message["payload"]["diag"]
    assert message["payload"]["key"] == "diag.result.http"
    assert view.message_text(message) == f"Диагностика hindsight-api: HTTP 200 · {AT} · по запросу @dev"
    kinds = [e["kind"] for e in env.store.card_events(card.id)]
    assert kinds.count("diag.recorded") == 1


def test_a_failed_probe_and_a_missing_target_in_words(env):
    card = env.card()
    env.call("digi", _record(card.id, outcome={"category": "timeout", "ms": 3000}))
    env.call("digi", _record(card.id, target="mysql", key="mysql", probe="", area="",
                            outcome={"category": "capability_missing", "ms": 0}))
    texts = [view.message_text(m) for m in _messages(env, card.id)]
    assert texts == [f"Диагностика hindsight-api: нет ответа (таймаут) · {AT} · по запросу @dev",
                     "Диагностики mysql нет в каталоге — нужна платформенная работа (@boss)"]


def test_diag_record_is_idempotent_by_key(env):
    card = env.card()
    first = env.call("digi", _record(card.id))
    again = env.call("digi", _record(card.id, outcome={"status": 500, "ms": 3}, at="2026-10-14T10:05:00Z"))
    assert again["reused"] is True and again["record"] == first["record"] and again["message"] == first["message"]
    assert len(_messages(env, card.id)) == 1
    env.call("digi", _record(card.id, key="after-fix"))  # another key
    env.call("digi", _record(card.id, target="hindsight-worker"))  # another target, the same key
    env.now = "2026-10-14T10:10:01Z"
    env.call("digi", _record(card.id))  # past the window
    assert len(_messages(env, card.id)) == 4
    assert [e["kind"] for e in env.store.card_events(card.id)].count("diag.recorded") == 4


def test_diag_recent_answers_the_stored_record_and_the_hours_count(env):
    card = env.card()
    assert env.call("digi", _recent(card.id)) == {"ok": True, "record": None, "count_hour": 0}
    env.call("digi", _record(card.id))
    env.call("digi", _record(card.id, key="b"))
    out = env.call("digi", _recent(card.id))
    assert out["record"]["status"] == 200 and out["count_hour"] == 2
    env.now = "2026-10-14T10:30:00Z"
    assert env.call("digi", _recent(card.id))["record"] is None  # past the window, still in the hour
    assert env.call("digi", _recent(card.id))["count_hour"] == 2
    env.now = "2026-10-14T11:00:01Z"
    assert env.call("digi", _recent(card.id))["count_hour"] == 0


@pytest.mark.parametrize("over", [
    {"assignee": "@sysadmin"}, {"status": "Inbox"}, {"status": "Done"}, {"status": "Blocked"},
], ids=["other-assignee", "inbox", "done", "blocked"])
def test_only_the_assignee_of_an_approved_card(env, over):
    card = env.card(**over)
    for request in (_record(card.id), _recent(card.id)):
        with pytest.raises(api.ApiError) as exc:
            env.call("digi", request)
        assert exc.value.kind == "refused" and "not your approved card" in str(exc.value)
    assert _messages(env, card.id) == []


@pytest.mark.parametrize("user", ["digi-dev", "digi-boss", "digi-cabinet"])
def test_only_the_executor_records(env, user):
    card = env.card()
    with pytest.raises(api.ApiError) as exc:
        env.call(user, _record(card.id))
    assert exc.value.kind == "refused"


@pytest.mark.parametrize("over", [
    {"target": "Bad"}, {"key": "a b"}, {"probe": "shell"}, {"area": "A"}, {"agent": "ghost"},
    {"outcome": {"status": 200}}, {"outcome": {"status": 99, "ms": 1}}, {"outcome": {"status": True, "ms": 1}},
    {"outcome": {"category": "weird", "ms": 1}}, {"outcome": {"category": "capability_missing", "ms": 0}},
    {"outcome": {"status": 200, "ms": 1, "body": "x"}}, {"at": "yesterday"}, {"reuse_s": -1},
    {"outcome": {"rows": 3, "ms": 1}}, {"probe": "cabinet-query", "outcome": {"status": 200, "ms": 1}},
    {"probe": "cabinet-query", "outcome": {"rows": -1, "ms": 1}},
], ids=["target", "key", "op", "area", "agent", "no-ms", "status-range", "status-bool", "category",
       "missing-with-op", "extra", "at", "reuse", "rows-of-http", "status-of-query", "rows-range"])
def test_diag_record_validates_its_request(env, over):
    card = env.card()
    with pytest.raises(api.ApiError) as exc:
        env.call("digi", _record(card.id, **over))
    assert exc.value.kind == "invalid"


# --- diag.query, the cabinet's own read-only queries ---------------------------------------------------

ENDPOINT = "https://fcm.googleapis.com/fcm/send/device-token-abc123"
P256DH, AUTH = "BPublicKeyOfTheBrowser", "authSecretOfTheBrowser"


def _query(card, **over):
    request = {"op": "diag.query", "card": card, "agent": "dev", "query": "push"}
    request.update(over)
    return request


def _push_state(env):
    s = env.store
    env.users.create("owner", "owner", roles=[("owner", "*")], actor="root", language="ru", timezone="UTC")
    s.subscribe("owner", ENDPOINT, P256DH, AUTH, "Chrome · Android", actor="user:owner")
    conn = s._conn
    conn.execute("INSERT INTO notification_prefs (user, kind, mode) VALUES ('owner', 'quiet', '23:00-08:00')")
    for n in range(1, 46):
        conn.execute("INSERT INTO notifications (user, kind, tag, title, body, url, due) VALUES (?,?,?,?,?,?,?)",
                     ("owner", "card.message", f"KD-{n}", f"Новое в KD-{n}", "secret body text", "/cards/KD-1", AT))
    conn.commit()
    s.notifications_sent([{"id": 45, "endpoint": ENDPOINT, "ok": True, "status": 201},
                          {"id": 44, "endpoint": ENDPOINT, "ok": False, "status": 500}])


def test_push_query_lists_subscriptions_prefs_and_the_newest_rows(env):
    card = env.card()
    _push_state(env)
    lines = env.call("digi", _query(card.id))["lines"]
    sections = [line.split("\t")[0] for line in lines]
    assert sections.count("sub") == 1 and sections.count("pref") == 1
    assert sections.count("note") == 40 and sections.count("delivery") == 2
    sub = lines[0]
    assert sub.startswith("sub\t1\tuser=owner host=fcm.googleapis.com created=") and "device=Chrome · Android" in sub
    newest = next(line for line in lines if line.startswith("note\t"))
    assert newest.startswith("note\t45\t") and "title=Новое в KD-45" in newest
    assert "delivery\t45\tsubscription=1 sent=" in "\n".join(lines)
    assert "result=500 attempts=1" in next(line for line in lines if line.startswith("delivery\t44\t"))
    assert "pref\towner\tkind=quiet mode=23:00-08:00" in lines


def test_push_query_never_shows_the_endpoint_its_keys_or_a_body(env):
    card = env.card()
    _push_state(env)
    text = "\n".join(env.call("digi", _query(card.id))["lines"])
    for secret in ("device-token-abc123", "/fcm/send", P256DH, AUTH, "secret body text"):
        assert secret not in text


def test_push_query_on_an_empty_cabinet_is_no_lines(env):
    card = env.card()
    assert env.call("digi", _query(card.id)) == {"ok": True, "lines": []}


def test_query_only_for_the_assignee_of_an_approved_card_and_only_the_executor(env):
    for over in ({"assignee": "@sysadmin"}, {"status": "Review"}):
        card = env.card(**over)
        with pytest.raises(api.ApiError) as exc:
            env.call("digi", _query(card.id))
        assert exc.value.kind == "refused"
    card = env.card()
    with pytest.raises(api.ApiError) as exc:
        env.call("digi-dev", _query(card.id))
    assert exc.value.kind == "refused"


@pytest.mark.parametrize("over", [{"query": "users"}, {"query": "SELECT 1"}, {"sql": "SELECT 1"}, {"agent": "ghost"},
                                  {"query": ["push"]}],
                         ids=["unknown", "sql-as-name", "extra", "agent", "not-a-string"])
def test_query_validates_its_request(env, over):
    card = env.card()
    with pytest.raises(api.ApiError) as exc:
        env.call("digi", _query(card.id, **over))
    assert exc.value.kind == "invalid"


def test_a_query_result_in_words(env):
    card = env.card()
    env.call("digi", _record(card.id, target="cabinet-push", key="cabinet-push", probe="cabinet-query", area="dev",
                            outcome={"rows": 43, "ms": 5}))
    (message,) = _messages(env, card.id)
    assert message["payload"]["diag"]["rows"] == 43
    assert view.message_text(message) == f"Диагностика cabinet-push: строк — 43 · {AT} · по запросу @dev"
