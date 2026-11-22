"""the push worker takes due notifications from the cabinet, sends them and reports the gateway's answer."""

import json

from cabinet_env import CabinetEnv

from kd.cabinet import api, notify, push_worker


def _env(tmp_path):
    env = CabinetEnv(tmp_path)
    for user in ("ok", "gone", "down"):
        env.users.create(
            user,
            user,
            roles=[("owner", "*")],
            actor="root",
            language="ru",
            timezone="UTC",
        )
        api.dispatch(
            env.ctx,
            api.Principal("user", -1, user),
            {
                "op": "push.subscribe",
                "endpoint": f"https://fcm.googleapis.com/fcm/send/{user}",
                "keys": {"p256dh": "k" * 20, "auth": "a" * 8},
                "device": user,
            },
        )
    n = notify.Notifier(env.store, env.users, None)
    card = env.card(project="web", status="In Progress")
    env.store.edit_card(card.id, card.revision, {"status": "Review"}, actor="agent:dev")
    assert len(n.on_event(env.store.events_since(0)[-1], None)) == 3
    return env, card


def test_one_pass_marks_sent_drops_gone_subscriptions_and_backs_off(tmp_path):
    env, card = _env(tmp_path)
    answers = {
        "https://fcm.googleapis.com/fcm/send/ok": 201,
        "https://fcm.googleapis.com/fcm/send/gone": 410,
        "https://fcm.googleapis.com/fcm/send/down": 0,
    }
    seen = []

    def send(subscription, payload, vapid_private, vapid_subject):
        seen.append((subscription, json.loads(payload), vapid_private, vapid_subject))
        return answers[subscription["endpoint"]]

    push_worker.run(
        env.client("digi-cabinet"),
        send=send,
        once=True,
        vapid_private="priv",
        vapid_subject="mailto:x",
    )
    assert len(seen) == 3 and all(s[2:] == ("priv", "mailto:x") for s in seen)
    subscription, payload, *_ = seen[0]
    assert set(subscription) == {"endpoint", "keys"} and set(subscription["keys"]) == {
        "p256dh",
        "auth",
    }
    assert (
        payload["tag"] == card.id
        and payload["url"] == f"/cards/{card.id}"
        and payload["title"]
    )
    (ok,) = env.store.notifications("ok")
    assert (
        ok["sent"] == env.now and env.store.subscriptions("ok")[0]["last_ok"] == env.now
    )
    assert env.store.subscriptions("gone") == []
    (down,) = env.store.notifications("down")
    assert down["attempts"] == 1 and down["sent"] == "" and down["result"] == "0"
    assert (
        env.call("digi-cabinet", {"op": "push.take", "limit": 10})["items"] == []
    )  # not again for 5 minutes
    env.now = "2026-10-14T10:05:01Z"
    (again,) = env.call("digi-cabinet", {"op": "push.take", "limit": 10})["items"]
    assert again["endpoint"] == "https://fcm.googleapis.com/fcm/send/down"


def test_each_gateway_answer_is_one_journal_line_without_the_subscription(tmp_path):
    import io

    env, card = _env(tmp_path)
    answers = {"ok": 201, "gone": 410, "down": 0}
    err = io.StringIO()
    push_worker.run(
        env.client("digi-cabinet"),
        send=lambda s, *a: answers[s["endpoint"].rsplit("/", 1)[1]],
        once=True,
        err=err,
    )
    lines = err.getvalue().splitlines()  # the journal said nothing of a send, good or bad
    assert len(lines) == 3 and all(line.startswith("kd-cabinet push: row ") for line in lines)
    assert all(f"tag={card.id}" in line and "fcm.googleapis.com" in line for line in lines)
    assert {line.rsplit(" ", 1)[1] for line in lines} == {"status=201", "status=410", "status=0"}
    assert not any(user in line for line in lines for user in ("/ok", "/gone", "/down", "k" * 20))


def test_a_stored_subscription_outside_the_push_services_is_never_contacted(tmp_path):
    env, _ = _env(tmp_path)
    # a row written before the check existed, or straight into the database
    env.store.subscribe(
        "ok", "https://10.0.0.1/x", "k" * 20, "a" * 8, "old", actor="root"
    )
    seen = []

    def send(subscription, payload, vapid_private, vapid_subject):
        seen.append(subscription["endpoint"])
        return 201

    push_worker.run(env.client("digi-cabinet"), send=send, once=True)
    assert seen and "https://10.0.0.1/x" not in seen
    assert "https://10.0.0.1/x" not in {
        s["endpoint"] for s in env.store.subscriptions("ok")
    }


def test_a_failing_send_is_status_0_and_the_loop_goes_on(tmp_path):
    env, _ = _env(tmp_path)

    def send(subscription, payload, vapid_private, vapid_subject):
        raise OSError("no route")

    push_worker.run(env.client("digi-cabinet"), send=send, once=True)
    assert all(
        r["attempts"] == 1
        for u in ("ok", "gone", "down")
        for r in env.store.notifications(u)
    )


def test_a_failing_send_names_its_reason_without_the_device(tmp_path):
    # A bare status=0 once hid an unresolvable host: the type and text of the failure, the
    # push
    # service's host instead of the endpoint, which addresses the device
    import io

    env, _ = _env(tmp_path)

    def send(subscription, payload, vapid_private, vapid_subject):
        raise OSError(
            "HTTPSConnectionPool(host='fcm.googleapis.com', port=443): Max retries exceeded with url: "
            f"{subscription['endpoint'].split('.com', 1)[1]} (Caused by NameResolutionError(\"Failed to resolve "
            "'fcm.googleapis.com' ([Errno -3] Temporary failure in name resolution)\")); "
            f"endpoint {subscription['endpoint']}")

    err = io.StringIO()
    push_worker.run(env.client("digi-cabinet"), send=send, once=True, err=err)
    lines = err.getvalue().splitlines()
    assert len(lines) == 3
    assert all("status=0 error=OSError: " in line and "Temporary failure in name resolution" in line for line in lines)
    assert not any(part in line for line in lines for part in ("/fcm/send", "/ok", "/gone", "/down"))
    assert all(len(line) < 600 for line in lines)


def test_after_five_failures_a_notification_is_given_up(tmp_path):
    env, _ = _env(tmp_path)
    for minute in range(5):
        env.now = f"2026-10-14T1{minute}:00:00Z"
        push_worker.run(env.client("digi-cabinet"), send=lambda *a: 500, once=True)
    env.now = "2026-10-14T16:00:00Z"
    assert env.call("digi-cabinet", {"op": "push.take", "limit": 10})["items"] == []


def test_push_take_and_sent_are_the_services_own(tmp_path):
    env, _ = _env(tmp_path)
    for user in ("digi-dev", "digi"):
        try:
            env.call(user, {"op": "push.take", "limit": 1})
        except api.ApiError as exc:
            assert exc.kind == "refused"
        else:
            raise AssertionError(user)


def _kd_cabinet():
    import importlib.util
    from importlib.machinery import SourceFileLoader
    from pathlib import Path

    loader = SourceFileLoader(
        "kd_cabinet", str(Path(__file__).resolve().parents[1] / "bin" / "kd-cabinet")
    )
    module = importlib.util.module_from_spec(
        importlib.util.spec_from_loader("kd_cabinet", loader)
    )
    loader.exec_module(module)
    return module


def test_kd_cabinet_push_runs_the_worker_with_the_hosts_vapid_key(monkeypatch):
    import io
    import types

    from kd.cabinet import config

    cab, seen = _kd_cabinet(), {}
    monkeypatch.setattr(
        push_worker, "run", lambda client, **kw: seen.update(client=client, **kw)
    )
    monkeypatch.setattr(  # push.env: the sender's own secret, not cabinet.env (global review 15.10.2026)
        config,
        "load_push",
        lambda: types.SimpleNamespace(vapid_private="priv", vapid_subject="mailto:o@x"),
    )
    client = object()
    assert cab.main(["push"], client=client) == 0
    assert seen["client"] is client and seen["send"] is push_worker.webpush_send
    assert (seen["vapid_private"], seen["vapid_subject"]) == ("priv", "mailto:o@x")

    def broken():
        raise config.ConfigError(
            "VAPID_PRIVATE_KEY, VAPID_PUBLIC_KEY and VAPID_SUBJECT are required"
        )

    monkeypatch.setattr(config, "load_push", broken)
    err = io.StringIO()
    assert cab.main(["push"], client=client, err=err) == 1 and "VAPID" in err.getvalue()


def test_webpush_send_maps_the_gateways_answer(monkeypatch):
    import types

    import pytest

    # the sender's library lives in the cabinet's venv; a host's system Python may not have it
    pywebpush = pytest.importorskip("pywebpush")
    calls = []

    def fake(**kw):
        calls.append(kw)
        if kw["subscription_info"]["endpoint"].endswith("gone"):
            raise pywebpush.WebPushException(
                "gone", response=types.SimpleNamespace(status_code=410)
            )
        if kw["subscription_info"]["endpoint"].endswith("down"):
            raise ConnectionError("no route")
        return types.SimpleNamespace(status_code=201)

    monkeypatch.setattr(pywebpush, "webpush", fake)
    sub = {
        "endpoint": "https://fcm.googleapis.com/fcm/send/ok",
        "keys": {"p256dh": "k", "auth": "a"},
    }
    payload = json.dumps({"title": "t", "urgency": "high"})
    assert push_worker.webpush_send(sub, payload, "priv", "mailto:o@x") == 201
    assert calls[0]["headers"] == {"Urgency": "high"} and calls[0]["ttl"] == 3600
    assert (
        calls[0]["vapid_claims"] == {"sub": "mailto:o@x"}
        and calls[0]["vapid_private_key"] == "priv"
    )
    assert (
        push_worker.webpush_send(
            {**sub, "endpoint": "https://fcm.googleapis.com/fcm/send/gone"},
            payload,
            "p",
            "s",
        )
        == 410
    )
    with pytest.raises(ConnectionError):  # the caller logs why the gateway was not reached
        push_worker.webpush_send(
            {**sub, "endpoint": "https://fcm.googleapis.com/fcm/send/down"},
            payload,
            "p",
            "s",
        )
