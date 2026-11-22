"""a step of the sysadmin's approved plan — its window in the cabinet: the executor opens and closes it, the
monitor takes the closed ones into its baseline and reports, the card's thread hears it once per run."""

import sqlite3
import sys
from pathlib import Path

import pytest

from kd.cabinet import api, schema, store

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cabinet_env import CabinetEnv  # noqa: E402

EXEC, MONITOR = "digi-exec", "digi-sysadmin"
AT = "2026-10-14T13:00:00+03:00"


@pytest.fixture
def env(tmp_path):
    return CabinetEnv(tmp_path)


def card(env):
    return env.card(project="admin", assignee="@sysadmin", status="In Progress")


def opened(env, c, server="node-a", run="r1", step="shell"):
    return env.call(
        EXEC,
        {
            "op": "monitor.window.open",
            "server": server,
            "card": c.id,
            "step": step,
            "run": run,
        },
    )["id"]


def close(env, wid, changes=None, errors=(), failed=""):
    request = {
        "op": "monitor.window.close",
        "id": wid,
        "changes": changes or {},
        "errors": list(errors),
    }
    if failed:
        request["failed"] = failed
    return env.call(EXEC, request)


def report(env, wid, accepted=(), dangerous=(), ok=True, detail=""):
    return env.call(
        MONITOR,
        {
            "op": "monitor.window.report",
            "id": wid,
            "ok": ok,
            "accepted": [list(a) for a in accepted],
            "dangerous": [list(d) for d in dangerous],
            "detail": detail,
        },
    )


def notices(env, c):
    thread = env.store.card(c.id).thread
    return [
        m["payload"]
        for m in env.store.thread_messages(thread, 0)
        if m["kind"] == "notice"
    ]


def test_only_the_executor_and_root_open_and_close_windows(env):
    c = card(env)
    with pytest.raises(api.ApiError) as exc:
        env.call(
            MONITOR,
            {
                "op": "monitor.window.open",
                "server": "node-a",
                "card": c.id,
                "step": "shell",
                "run": "r",
            },
        )
    assert exc.value.kind == "refused"
    wid = env.call(
        "root",
        {
            "op": "monitor.window.open",
            "server": "node-a",
            "card": c.id,
            "step": "s",
            "run": "r",
        },
    )["id"]
    with pytest.raises(api.ApiError) as exc:
        env.call(
            MONITOR,
            {"op": "monitor.window.close", "id": wid, "changes": {}, "errors": []},
        )
    assert exc.value.kind == "refused"
    with pytest.raises(api.ApiError) as exc:
        env.call(EXEC, {"op": "monitor.window.take"})
    assert exc.value.kind == "refused"


def test_a_window_needs_a_card_and_a_server_alias(env):
    c = card(env)
    with pytest.raises(api.ApiError) as exc:
        env.call(
            EXEC,
            {
                "op": "monitor.window.open",
                "server": "node a",
                "card": c.id,
                "step": "s",
                "run": "r",
            },
        )
    assert exc.value.kind == "invalid"
    with pytest.raises(api.ApiError) as exc:
        env.call(
            EXEC,
            {
                "op": "monitor.window.open",
                "server": "node-a",
                "card": "KD-999",
                "step": "s",
                "run": "r",
            },
        )
    assert exc.value.kind == "not_found"


def test_an_open_window_is_active_and_expires_after_30_minutes(env):
    c = card(env)
    opened(env, c)
    assert env.call(MONITOR, {"op": "monitor.window.active"})["servers"] == ["node-a"]
    env.now = "2026-10-14T10:31:00Z"
    assert env.call(MONITOR, {"op": "monitor.window.active"})["servers"] == []
    assert (
        env.store._conn.execute("SELECT state FROM monitor_windows").fetchone()[0]
        == "expired"
    )
    assert [n["key"] for n in notices(env, c)] == ["notice.monitor.window_expired"]
    assert notices(env, c)[0]["params"] == {"server": "node-a"}


def test_the_monitor_takes_closed_windows_and_reports_once(env):
    c = card(env)
    wid = opened(env, c)
    assert (
        env.call(MONITOR, {"op": "monitor.window.take"})["windows"] == []
    )  # still open
    close(env, wid, {"units:x.service": [None, "a" * 64]}, errors=["cron"])
    (w,) = env.call(MONITOR, {"op": "monitor.window.take"})["windows"]
    assert (w["id"], w["server"], w["card"], w["changes"], w["errors"]) == (
        wid,
        "node-a",
        c.id,
        {"units:x.service": [None, "a" * 64]},
        ["cron"],
    )
    assert env.call(MONITOR, {"op": "monitor.window.active"})["servers"] == []
    report(env, wid, accepted=[("sec:units", "x.service")])
    assert env.call(MONITOR, {"op": "monitor.window.take"})["windows"] == []
    report(
        env, wid, accepted=[("sec:units", "x.service")]
    )  # a repeat after a lost answer changes nothing
    keys = [n["key"] for n in notices(env, c)]
    assert keys == ["notice.monitor.window_applied"]
    assert notices(env, c)[0]["params"] == {
        "servers": "node-a",
        "accepted": "units: x.service",
    }


def test_one_notice_per_run_after_its_last_window(env):
    c = card(env)
    a, b = opened(env, c, "node-a"), opened(env, c, "node-b")
    close(env, a, {"units:x.service": [None, "1"]})
    close(
        env,
        b,
        {"units:x.service": [None, "1"], "ports:tcp:0.0.0.0:443": [None, "nginx"]},
    )
    report(env, a, accepted=[("sec:units", "x.service")])
    assert notices(env, c) == []
    report(
        env,
        b,
        accepted=[("sec:units", "x.service"), ("ports", "tcp:0.0.0.0:443")],
        dangerous=[("sec:keys", "/root/.ssh/authorized_keys")],
    )
    got = notices(env, c)
    assert [n["key"] for n in got] == [
        "notice.monitor.window_applied",
        "notice.monitor.window_dangerous",
    ]
    assert got[0]["params"] == {
        "servers": "node-a, node-b",
        "accepted": "units: x.service; ports: tcp:0.0.0.0:443",
    }
    assert (
        got[1]["params"] == {"objects": "keys /root/.ssh/authorized_keys (node-b)"}
        and got[1]["level"] == "warn"
    )


def test_a_notice_names_five_objects_and_counts_the_rest(env):
    c = card(env)
    wid = opened(env, c)
    close(env, wid, {f"units:u{i}.service": [None, "1"] for i in range(7)})
    report(env, wid, accepted=[("sec:units", f"u{i}.service") for i in range(7)])
    (n,) = notices(env, c)
    assert (
        n["params"]["accepted"]
        == "; ".join(f"units: u{i}.service" for i in range(5)) + " +2"
    )


def test_nothing_changed_and_a_rejected_window_say_so(env):
    c = card(env)
    a, b = opened(env, c, "node-a"), opened(env, c, "node-b")
    close(env, a, {})
    close(env, b, {"units:x.service": [None, "1"]})
    report(env, a)
    report(env, b, ok=False, detail="выключено")
    assert [(n["key"], n["params"]) for n in notices(env, c)] == [
        ("notice.monitor.window_unchanged", {"servers": "node-a"}),
        (
            "notice.monitor.window_rejected",
            {"servers": "node-b", "detail": "выключено"},
        ),
    ]


def test_a_failed_window_is_never_taken(env):
    c = card(env)
    wid = opened(env, c)
    close(env, wid, failed="снимок не снят")
    assert env.call(MONITOR, {"op": "monitor.window.take"})["windows"] == []
    assert env.call(MONITOR, {"op": "monitor.window.active"})["servers"] == []


def push(env, *items):
    return env.call(MONITOR, {"op": "monitor.push", "items": list(items)})["results"]


def open_item(
    key,
    server="node-a",
    check="sec:keys",
    subject="/root/.ssh/authorized_keys",
    n_key="o1",
):
    problem = {
        "key": key,
        "server": server,
        "check": check,
        "subject": subject,
        "level": "crit",
        "max_level": "crit",
        "value": "x",
        "text": "ключи",
        "opened_at": AT,
        "repeats": 0,
    }
    return {"key": n_key, "kind": "open", "problem": problem}


def test_a_dangerous_object_links_its_incident_to_the_card_within_24_hours(env):
    c = card(env)
    wid = opened(env, c)
    close(env, wid, {"keys:/root/.ssh/authorized_keys": [None, "a" * 64 + " 1"]})
    report(env, wid, dangerous=[("sec:keys", "/root/.ssh/authorized_keys")])
    n = push(env, open_item("node-a:sec:keys:/root/.ssh/authorized_keys"))[0][
        "incident"
    ]
    linked = env.store._conn.execute(
        "SELECT card, linked_by FROM incident_cards WHERE incident=?", (n,)
    ).fetchall()
    assert [tuple(r) for r in linked] == [(c.id, "system:window")]
    kinds = [
        e["kind"]
        for e in env.call("digi-cabinet", {"op": "incident.get", "incident": n})[
            "timeline"
        ]
    ]
    assert "incident.window" in kinds
    env.now = "2026-10-15T10:01:00Z"  # 24 h later the same object opens unlinked
    push(
        env,
        {
            "key": "r1",
            "kind": "resolve",
            "problem_key": "node-a:sec:keys:/root/.ssh/authorized_keys",
            "how": "accept",
            "at": "2026-10-15T13:00:00+03:00",
        },
    )
    later = open_item("node-a:sec:keys:/root/.ssh/authorized_keys", n_key="o2")
    later["problem"]["opened_at"] = "2026-10-15T13:01:00+03:00"
    m = push(env, later)[0]["incident"]
    assert (
        m != n
        and env.store._conn.execute(
            "SELECT COUNT(*) FROM incident_cards WHERE incident=?", (m,)
        ).fetchone()[0]
        == 0
    )


def test_a_v35_base_migrates_to_the_windows(tmp_path):
    path = str(tmp_path / "v35.db")
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(schema.DDL + "".join(schema.MIGRATIONS[v] for v in range(2, 36)))
    conn.execute("INSERT INTO meta VALUES ('schema_version', '35')")
    conn.execute("INSERT INTO meta VALUES ('next_card', '1')")
    conn.close()
    db = store.Store.open(path)
    assert db.schema_version() == schema.SCHEMA_VERSION  # 36 and every later step
    assert db._conn.execute("SELECT COUNT(*) FROM monitor_windows").fetchone()[0] == 0
    assert (
        db._conn.execute("SELECT COUNT(*) FROM monitor_window_marks").fetchone()[0] == 0
    )


def test_what_a_window_left_to_its_incidents_is_said_with_its_reason(env):
    """A window that kept drifted objects or found no baseline is no "changed nothing that is checked"."""
    c = card(env)
    a, b = opened(env, c, "node-a"), opened(env, c, "node-b")
    close(env, a, {"units:x.service": ["f1", "f2"]})
    close(env, b, {"units:y.service": [None, "1"], "units:z.service": ["f1", "f2"]})
    report(env, a, detail="1 отличались от эталона ещё до шага — проверятся как обычно")
    report(
        env,
        b,
        accepted=[("sec:units", "y.service")],
        detail="1 отличались от эталона ещё до шага",
    )
    got = [(n["key"], n["params"]) for n in notices(env, c)]
    assert got == [
        (
            "notice.monitor.window_applied",
            {"servers": "node-b", "accepted": "units: y.service"},
        ),
        (
            "notice.monitor.window_partial",
            {
                "servers": "node-a, node-b",
                "detail": "1 отличались от эталона ещё до шага — проверятся как обычно; 1 отличались от эталона ещё до шага",
            },
        ),
    ]


def test_the_executor_may_name_its_window_and_repeat_the_open(env):
    """Review 9: a named window survives a lost answer — the same open again is the same window."""
    c = card(env)
    request = {
        "op": "monitor.window.open",
        "server": "node-a",
        "card": c.id,
        "step": "s",
        "run": "r",
        "id": "0123456789abcdef",
    }
    assert env.call(EXEC, request)["id"] == "0123456789abcdef"
    assert env.call(EXEC, request)["id"] == "0123456789abcdef"
    assert (
        env.store._conn.execute("SELECT COUNT(*) FROM monitor_windows").fetchone()[0]
        == 1
    )
    with pytest.raises(api.ApiError) as exc:
        env.call(EXEC, dict(request, server="node-b"))
    assert exc.value.kind == "conflict"
    with pytest.raises(api.ApiError) as exc:
        env.call(EXEC, dict(request, id="nothex"))
    assert exc.value.kind == "invalid"
