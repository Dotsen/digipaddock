"""dev.check — the lead's check of a PR head in its contour, one operation per key;
the host's report ends it with a class, a notice in the card's thread and the wait it ends."""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from kd import pipeline_config
from kd.cabinet import api, continuations, devchecks, operations, schema, store

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_cabinet_factory_plan import env  # noqa: E402,F401
from test_cabinet_outcomes import begin, end, finish, report  # noqa: E402

HEAD, BASE, OTHER = "b" * 40, "c" * 40, "d" * 40
MESSAGES = Path(__file__).resolve().parents[1] / "cabinet" / "web" / "messages"


@pytest.fixture
def cab(env, tmp_path):
    env.ctx.pipeline = lambda: pipeline_config.Config(mode="enforce")
    env.ctx.deployed_revision = lambda: "a" * 40
    path = tmp_path / "contours.json"
    path.write_text(json.dumps({"app-ci": {"host": "local", "components": ["digipaddock", "digipaddock-deploy"],
                                           "parallel": 2}}), encoding="utf-8")
    env.ctx.contours_path = str(path)
    env.put("KD-50", project="digipaddock", assignee="@dev", status="In Progress")
    for command in env.store.commands(card="KD-50", states=["queued"]):
        for state in ("delivered", "started", "done"):
            env.store.command_state(command["id"], state)
    return env


def ask(cab, user="digi-forge", **over):
    req = {"op": "devcheck.request", "card": "KD-50", "agent": "dev", "project": "digipaddock", "repo": "acme/app",
           "pr": 7, "head": HEAD, "base_sha": BASE, "target": "main", "profiles": ["unit"], "contour": "app-ci",
           "timeout_s": 1200, "set": {}}
    req.update(over)
    return cab.call(user, req)


def as_user(cab, request, user="owner"):
    return api.dispatch(cab.ctx, api.Principal("user", -1, user), request)


def refused(cab, **over):
    with pytest.raises(api.ApiError) as exc:
        ask(cab, **over)
    assert exc.value.kind == "refused", exc.value
    return str(exc.value)


def ended(cab, op_id, cls="passed", **extra):
    state = "succeeded" if cls == "passed" else "failed"
    report(cab, {"type": "devcheck", "check": op_id, "state": state, "class": cls,
                 "profiles": [{"profile": "unit", "status": "success" if cls == "passed" else "failure",
                               "counts": {"passed": 10, "failed": 0 if cls == "passed" else 2},
                               "duration_s": 61}], "log": f"devcheck/{op_id}.log", **extra})


def notices_of(cab, card="KD-50"):
    thread = cab.store.card(card).thread
    return [dict(r) for r in cab.store._conn.execute(
        "SELECT payload FROM messages WHERE thread=? AND kind='notice' ORDER BY seq", (thread,))]


def keys(cab):
    return [(json.loads(n["payload"])["key"], json.loads(n["payload"])["level"]) for n in notices_of(cab)]


def test_request_creates_one_operation_and_a_repeat_joins_it(cab):
    first = ask(cab)
    op = first["operation"]
    assert first["created"] is True
    assert op["kind"] == devchecks.KIND and op["agents"] == [] and op["grant_id"] == ""
    assert op["state"] == "pending" and op["requested_by"] == "agent:dev" and op["card"] == "KD-50"
    assert op["params"]["head"] == HEAD and op["params"]["contour"] == "app-ci"
    assert op["key"] == devchecks.key_of("digipaddock", HEAD, ["unit"], BASE, {})
    again = ask(cab)
    assert again["created"] is False and again["operation"]["id"] == op["id"]
    kinds = [r[0] for r in cab.store._conn.execute("SELECT kind FROM events WHERE kind LIKE 'operation.%'")]
    assert kinds == ["operation.queued"]


def test_key_depends_on_what_is_checked():
    base = devchecks.key_of("digipaddock", HEAD, ["unit"], BASE, {})
    assert base.startswith("devcheck:digipaddock:" + HEAD[:12] + ":") and operations.KEY.fullmatch(base)
    assert devchecks.key_of("digipaddock", HEAD, ["unit", "e2e"], BASE, {}) != base
    assert devchecks.key_of("digipaddock", HEAD, ["unit"], OTHER, {}) != base
    assert devchecks.key_of("digipaddock", HEAD, ["unit"], BASE, {"digipaddock-deploy": OTHER}) != base
    assert devchecks.key_of("digipaddock", HEAD, ["e2e", "unit"], BASE, {}) == devchecks.key_of(
        "digipaddock", HEAD, ["unit", "e2e"], BASE, {})


def test_a_passed_check_is_joined_again(cab):
    op = ask(cab)["operation"]
    ended(cab, op["id"])
    again = ask(cab)
    assert again["created"] is False and again["operation"]["id"] == op["id"]


def test_a_red_end_allows_a_new_attempt_up_to_five(cab):
    seen = []
    for n in range(devchecks.ATTEMPTS):
        op = ask(cab)["operation"]
        seen.append(op["key"])
        ended(cab, op["id"], "checks_failed")
    base = devchecks.key_of("digipaddock", HEAD, ["unit"], BASE, {})
    assert seen == [base] + [f"{base}:{n}" for n in range(2, devchecks.ATTEMPTS + 1)]
    assert "attempts" in refused(cab)


def test_refused_when_the_card_is_not_in_progress(cab):
    cab.put("KD-50", status="Review")
    refused(cab)


def test_refused_for_another_agents_card(cab):
    cab.put("KD-50", assignee="@analyst")
    refused(cab)
    cab.put("KD-50", assignee="@boss")
    refused(cab, agent="boss")


def test_refused_for_a_project_outside_the_contour(cab):
    refused(cab, project="admin")
    refused(cab, contour="nope")
    refused(cab, set={"admin": OTHER})


def test_refused_when_the_contour_is_in_maintenance(cab):
    cab.call("root", {"op": "contour.maintenance", "name": "app-ci", "reason": "cleanup failed"})
    assert "cleanup failed" in refused(cab)
    cab.call("root", {"op": "contour.release", "name": "app-ci"})
    assert ask(cab)["created"] is True


# --- a turn waits for a contour in maintenance -------------------------------------------------------
def contour_waits(cab, command):
    return [r[0] for r in cab.store._conn.execute("SELECT state FROM waits WHERE command=? AND kind='contour'",
                                                  (command,))]


def continues(cab):
    return [c["text"] for c in cab.store.commands(card="KD-50", states=["queued"]) if c["kind"] == "continue"]


def test_a_contour_wait_resolves_when_the_contour_is_released(cab):
    cab.call("root", {"op": "contour.maintenance", "name": "app-ci", "reason": "cleanup failed"})
    record = cab.store.card("KD-50")
    command, session = begin(cab, record)
    finish(cab, record, outcome="wait_dependency", note="the contour is in maintenance", contours=["app-ci"])
    end(cab, command, session)
    assert contour_waits(cab, command) == ["open"] and continues(cab) == []
    cab.call("digi-cabinet", {"op": "pipeline.sweep"})  # still in maintenance: still waiting
    assert contour_waits(cab, command) == ["open"]
    cab.call("root", {"op": "contour.release", "name": "app-ci"})
    assert contour_waits(cab, command) == ["resolved"]
    assert continues(cab) == [continuations.RESOLVED.format(id="KD-50")]


def test_finish_contour_goes_with_wait_dependency(cab):
    record = cab.store.card("KD-50")
    begin(cab, record)
    for outcome, named in (("continue", ["app-ci"]), ("wait_dependency", ["ghost"]), ("wait_dependency", ["A B"])):
        with pytest.raises(api.ApiError) as exc:
            finish(cab, record, outcome=outcome, note="x", contours=named)
        assert exc.value.kind == "invalid", (outcome, named)
    reply = finish(cab, record, outcome="wait_dependency", note="x", contours=["app-ci"])  # no dependency card needed
    assert [(w["kind"], w["ref"]) for w in reply["waits"]] == [("contour", "app-ci")]


def test_a_contour_gone_from_the_tree_needs_routing(cab, tmp_path):
    cab.call("root", {"op": "contour.maintenance", "name": "app-ci", "reason": "cleanup failed"})
    record = cab.store.card("KD-50")
    command, session = begin(cab, record)
    finish(cab, record, outcome="wait_dependency", note="the contour is in maintenance", contours=["app-ci"])
    end(cab, command, session)
    (tmp_path / "contours.json").write_text("{}", encoding="utf-8")  # a release took the contour out
    cab.call("digi-cabinet", {"op": "pipeline.sweep"})
    assert contour_waits(cab, command) == ["needs_routing"]


def test_only_the_forge_principal_requests(cab):
    for user in ("digi-dev", "digi", "digi-cabinet"):
        with pytest.raises(api.ApiError) as exc:
            ask(cab, user=user)
        assert exc.value.kind == "refused"


@pytest.mark.parametrize("over", [
    {"head": "x" * 40}, {"base_sha": "1234"}, {"pr": 0}, {"pr": True}, {"profiles": []}, {"profiles": ["Bad"]},
    {"profiles": ["unit", "unit"]}, {"timeout_s": 10}, {"timeout_s": 99999}, {"repo": "no-slash"},
    {"set": {"digipaddock": OTHER}}, {"set": {"digipaddock-deploy": "short"}}, {"extra": 1},
])
def test_invalid_request(cab, over):
    with pytest.raises(api.ApiError) as exc:
        ask(cab, **over)
    assert exc.value.kind == "invalid", exc.value


def test_factory_lane_never_sees_a_devcheck(cab):
    op = ask(cab)["operation"]
    assert operations.in_flight(cab.store) is None
    assert operations.next_due(cab.store, "2099-01-01T00:00:00Z") is None
    assert devchecks.next_due(cab.store, "app-ci", "2099-01-01T00:00:00Z")["id"] == op["id"]
    devchecks.delivered(cab.ctx, op["id"])
    assert operations.in_flight(cab.store) is None
    assert [o["id"] for o in devchecks.in_flight(cab.store, "app-ci")] == [op["id"]]
    assert devchecks.next_due(cab.store, "app-ci", "2099-01-01T00:00:00Z") is None


def test_host_request_names_the_check(cab):
    op = ask(cab)["operation"]
    request = devchecks.host_request(operations.row(cab.store, op["id"]), start=True)
    assert request == {"op": "devcheck", "check": op["id"], "start": True, "timeout": 1200 + devchecks.GRACE_S,
                       "params": op["params"]}


def test_report_running_then_passed_writes_one_info_notice(cab):
    op = ask(cab)["operation"]
    devchecks.delivered(cab.ctx, op["id"])
    report(cab, {"type": "devcheck", "check": op["id"], "state": "running"})
    assert operations.row(cab.store, op["id"])["state"] == "running"
    assert keys(cab) == []
    ended(cab, op["id"])
    done = operations.row(cab.store, op["id"])
    assert done["state"] == "succeeded" and done["error"] == ""
    assert done["result"]["class"] == "passed" and done["result"]["profiles"][0]["profile"] == "unit"
    assert keys(cab) == [("notice.devcheck.passed", "info")]
    params = json.loads(notices_of(cab)[0]["payload"])["params"]
    assert params["head"] == HEAD[:7] and params["pr"] == "7" and params["log"] == f"devcheck/{op['id']}.log"
    assert len(params) <= 16


def test_checks_failed_is_a_failed_operation_and_its_wait_ends(cab):
    op = ask(cab)["operation"]
    record = cab.store.card("KD-50")
    command, session = begin(cab, record)
    finish(cab, record, outcome="wait_dependency", note="waits for the check", operations=[op["id"]])
    end(cab, command, session)
    ended(cab, op["id"], "checks_failed", error_short="FAILED test_x")
    done = operations.row(cab.store, op["id"])
    assert done["state"] == "failed" and done["error"] == "checks_failed"
    assert keys(cab) == [("notice.devcheck.checks_failed", "warn")]
    assert "FAILED test_x" in json.loads(notices_of(cab)[0]["payload"])["params"]["tail"]
    texts = [c["text"] for c in cab.store.commands(card="KD-50", states=["queued"]) if c["kind"] == "continue"]
    assert len(texts) == 1 and texts[0].startswith(continuations.ENDED.split("(")[0].format(id="KD-50"))


def test_maintenance_in_a_report_marks_the_contour(cab):
    op = ask(cab)["operation"]
    devchecks.delivered(cab.ctx, op["id"])
    ended(cab, op["id"], "environment_not_ready", maintenance="cleanup of the last check failed")
    assert keys(cab) == [("notice.devcheck.environment_not_ready", "critical")]
    listed = cab.call("root", {"op": "contour.list"})["contours"]
    assert listed[0]["name"] == "app-ci" and listed[0]["state"] == "maintenance"
    assert listed[0]["reason"] == "cleanup of the last check failed"
    assert "cleanup of the last check failed" in refused(cab, head=OTHER)


def test_contour_list_shows_whats_running(cab):
    op = ask(cab)["operation"]
    devchecks.delivered(cab.ctx, op["id"])
    listed = cab.call("root", {"op": "contour.list"})["contours"][0]
    assert listed["state"] == "busy" and listed["capacity"] == 2
    assert listed["running"] == [{"operation": op["id"], "card": "KD-50", "project": "digipaddock", "pr": 7,
                                  "head": HEAD}]
    owner = as_user(cab, {"op": "contour.list"})["contours"]
    assert owner[0]["name"] == "app-ci"


def test_a_new_head_supersedes_the_older_check(cab):
    old = ask(cab)["operation"]
    new = ask(cab, head=OTHER)["operation"]
    assert operations.row(cab.store, old["id"])["error"] == "superseded"
    assert operations.row(cab.store, new["id"])["state"] == "pending"
    devchecks.delivered(cab.ctx, new["id"])
    newer = ask(cab, head="e" * 40)["operation"]  # one the host has: flagged, the host stops it
    assert operations.row(cab.store, new["id"])["params"]["cancel"] == "superseded"
    assert [o["id"] for o in devchecks.cancelling(cab.store)] == [new["id"]]
    assert devchecks.host_cancel(operations.row(cab.store, new["id"])) == {
        "op": "devcheck_cancel", "check": new["id"], "reason": "superseded"}
    assert operations.row(cab.store, newer["id"])["state"] == "pending"


def test_card_left_work_cancels_pending_and_flags_running(cab):
    pending = ask(cab)["operation"]
    running = ask(cab, profiles=["e2e"])["operation"]
    devchecks.delivered(cab.ctx, running["id"])
    cab.put("KD-50", status="Review")
    assert operations.row(cab.store, pending["id"])["error"] == "cancelled"
    assert operations.row(cab.store, running["id"])["params"]["cancel"] == "card_left"
    assert [o["id"] for o in devchecks.cancelling(cab.store)] == [running["id"]]


def notice_text(cab, lang="ru"):
    payload = json.loads(notices_of(cab)[-1]["payload"])
    text = json.loads((MESSAGES / f"{lang}.json").read_text(encoding="utf-8"))[payload["key"]]
    for name, value in payload["params"].items():
        text = text.replace("{" + name + "}", value)
    return text


def test_a_check_the_host_stopped_after_card_left_names_the_reason(cab):
    """the host's report carries no reason — the notice takes it from the cancel flag."""
    op = ask(cab)["operation"]
    devchecks.delivered(cab.ctx, op["id"])
    cab.put("KD-50", status="Review")
    ended(cab, op["id"], "cancelled")
    assert keys(cab) == [("notice.devcheck.cancelled", "info")]
    assert json.loads(notices_of(cab)[0]["payload"])["params"]["reason"] == "card_left"
    assert "(card_left)" in notice_text(cab) and "(card_left)" in notice_text(cab, "en")


def test_a_check_the_host_lost_after_a_cancel_flag_names_the_reason(cab):
    op = ask(cab)["operation"]
    devchecks.delivered(cab.ctx, op["id"])
    cab.call("root", {"op": "devcheck.cancel", "id": op["id"]})
    devchecks.answered(cab.ctx, op["id"], {"state": "unknown"})
    assert keys(cab) == [("notice.devcheck.cancelled", "info")]
    assert json.loads(notices_of(cab)[0]["payload"])["params"]["reason"] == "root"


def test_a_cancel_without_any_reason_says_dash(cab):
    op = ask(cab)["operation"]
    devchecks.delivered(cab.ctx, op["id"])
    ended(cab, op["id"], "cancelled")
    assert json.loads(notices_of(cab)[0]["payload"])["params"]["reason"] == "-"


def test_final_state_is_never_moved_again(cab):
    op = ask(cab)["operation"]
    ended(cab, op["id"], "checks_failed")
    ended(cab, op["id"])
    report(cab, {"type": "devcheck", "check": op["id"], "state": "running"})
    done = operations.row(cab.store, op["id"])
    assert done["state"] == "failed" and done["error"] == "checks_failed"
    assert len(notices_of(cab)) == 1


def test_host_lost_it(cab):
    op = ask(cab)["operation"]
    devchecks.delivered(cab.ctx, op["id"])
    devchecks.answered(cab.ctx, op["id"], {"state": "unknown"})
    assert operations.row(cab.store, op["id"])["error"] == "host_lost"
    assert keys(cab) == [("notice.devcheck.host_lost", "warn")]


def test_root_cancels_and_lists(cab):
    op = ask(cab)["operation"]
    cab.call("root", {"op": "devcheck.cancel", "id": op["id"]})
    assert operations.row(cab.store, op["id"])["error"] == "cancelled"
    listed = cab.call("root", {"op": "devcheck.list", "card": "KD-50"})["checks"]
    assert [c["id"] for c in listed] == [op["id"]]
    assert as_user(cab, {"op": "operation.list", "card": "KD-50"})["operations"] == []
    assert [c["id"] for c in as_user(cab, {"op": "devcheck.list", "card": "KD-50"})["checks"]] == [op["id"]]


def test_schema_31_migrates_a_30_database(tmp_path):
    path = str(tmp_path / "v30.db")
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(schema.DDL + "".join(schema.MIGRATIONS[v] for v in range(2, 31)))
    conn.execute("INSERT INTO meta VALUES ('schema_version', '30')")
    conn.execute("INSERT INTO meta VALUES ('next_card', '1')")
    conn.close()
    db = store.Store.open(path)
    assert db.schema_version() == schema.SCHEMA_VERSION  # 31 and every later step
    columns = {r[1] for r in db._conn.execute("PRAGMA table_info(contours)")}
    assert columns == {"name", "state", "deployed", "since", "reason", "updated"}


def test_every_notice_key_has_both_languages():
    for lang in ("ru", "en"):
        catalog = json.loads((MESSAGES / f"{lang}.json").read_text(encoding="utf-8"))
        for cls in devchecks.CLASSES:
            assert f"notice.devcheck.{cls}" in catalog, (lang, cls)
