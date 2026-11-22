"""A turn ends waiting for a factory operation (``--operation <id>``); the operation's
end resolves the wait in its event's transaction and asks for one continuation, within the pipeline's limits."""

import sys
from pathlib import Path

import pytest

from kd import pipeline_config
from kd.cabinet import api, continuations, problems

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_cabinet_factory_plan import env  # noqa: E402,F401
from test_cabinet_outcomes import begin, end, finish, report  # noqa: E402

REV = "a" * 40
OK = {"dev": {"state": "ok", "stage": "verify", "error": ""}}


@pytest.fixture
def cab(env):
    env.ctx.pipeline = lambda: pipeline_config.Config(mode="enforce")
    env.ctx.deployed_revision = lambda: REV
    env.put("KD-50", project="digipaddock", assignee="@dev", status="In Progress")
    for command in env.store.commands(card="KD-50", states=["queued"]):
        for state in ("delivered", "started", "done"):
            env.store.command_state(command["id"], state)
    return env


def requested(cab, key="root:1", agents=("dev",)):
    return cab.call("root", {"op": "operation.request", "kind": "agent.reinstall", "agents": list(agents),
                             "key": key, "card": "KD-50"})["operation"]["id"]


def ended(cab, operation_id, state="succeeded", agents=OK, reason=""):
    report(cab, {"type": "operation", "operation": operation_id, "state": state, "phase": "installing",
                 "agents": agents, "reason": reason})


def waiting(cab, operation_id, command_session=None):
    record = cab.store.card("KD-50")
    command, session = command_session or begin(cab, record)
    finish(cab, record, outcome="wait_dependency", note="waits for the reinstall", operations=[operation_id])
    end(cab, command, session)
    return command


def wait_states(cab, command):
    return [r[0] for r in cab.store._conn.execute("SELECT state FROM waits WHERE command=?", (command,))]


def queued_continues(cab):
    return [c for c in cab.store.commands(card="KD-50", states=["queued"]) if c["kind"] == "continue"]


def conts(cab):
    return [dict(r) for r in cab.store._conn.execute(
        "SELECT * FROM continuations WHERE card='KD-50' ORDER BY created, rowid")]


def run_queued(cab, command_id):
    """The host takes a queued command and starts its turn."""
    session = cab.store.session_for(cab.store.card("KD-50").thread, "dev")["id"]
    cab.store.command_state(command_id, "delivered", session=session)
    report(cab, {"type": "turn", "session": session, "command": command_id, "phase": "started"})
    return command_id, session


def test_finish_operation_needs_wait_dependency_and_its_card(cab):
    op = requested(cab)
    record = cab.store.card("KD-50")
    begin(cab, record)
    with pytest.raises(api.ApiError) as exc:
        finish(cab, record, outcome="continue", note="x", operations=[op])
    assert exc.value.kind == "invalid"
    with pytest.raises(api.ApiError):
        finish(cab, record, outcome="wait_dependency", note="x", operations=["f" * 32])
    finish(cab, record, outcome="wait_dependency", note="x", operations=[op])  # no dependency card needed


def test_operation_done_before_wait_resolves_on_activate(cab):
    op = requested(cab)
    record = cab.store.card("KD-50")
    command, session = begin(cab, record)
    finish(cab, record, outcome="wait_dependency", note="waits", operations=[op])
    ended(cab, op)  # during the turn: the wait is still pending
    end(cab, command, session)
    assert wait_states(cab, command) == ["resolved"]
    assert [c["text"] for c in queued_continues(cab)] == [continuations.RESOLVED.format(id="KD-50")]


def test_operation_done_after_wait_one_continuation(cab):
    op = requested(cab)
    command = waiting(cab, op)
    assert wait_states(cab, command) == ["open"] and queued_continues(cab) == []
    ended(cab, op)
    assert wait_states(cab, command) == ["resolved"] and len(queued_continues(cab)) == 1


def test_repeated_event_no_second_continuation(cab):
    op = requested(cab)
    waiting(cab, op)
    ended(cab, op)
    ended(cab, op)  # the host's answer repeats the report
    cab.call("root", {"op": "pipeline.sweep"})
    assert len(queued_continues(cab)) == 1 and len(conts(cab)) == 1


def test_lost_signal_sweep_picks_result(cab):
    op = requested(cab)
    command = waiting(cab, op)
    cab.store._conn.execute("UPDATE operations SET state='succeeded' WHERE id=?", (op,))  # no event
    assert wait_states(cab, command) == ["open"]
    cab.call("root", {"op": "pipeline.sweep"})
    assert wait_states(cab, command) == ["resolved"] and len(queued_continues(cab)) == 1


def test_wait_follows_superseded_operation(cab):
    first = requested(cab, key="root:1")
    command = waiting(cab, first)
    second = requested(cab, key="root:2", agents=("boss", "dev"))
    assert cab.store._conn.execute("SELECT state FROM operations WHERE id=?", (first,)).fetchone()[0] == "superseded"
    assert wait_states(cab, command) == ["open"]  # it waits for the newer one now
    ended(cab, second, agents={**OK, "boss": {"state": "unchanged", "stage": "verify", "error": ""}})
    assert wait_states(cab, command) == ["resolved"] and len(queued_continues(cab)) == 1


def test_failed_operation_ends_set_not_success(cab):
    op = requested(cab)
    command = waiting(cab, op)
    ended(cab, op, "failed", {"dev": {"state": "failed", "stage": "check", "error": "exit 1"}}, "install_failed")
    assert wait_states(cab, command) == ["rejected"]
    assert [c["text"] for c in queued_continues(cab)] == [continuations.ENDED.format(id="KD-50", state="rejected")]


def test_unknown_operation_needs_routing(cab):
    op = requested(cab)
    command = waiting(cab, op)
    cab.store._conn.execute("DELETE FROM operations WHERE id=?", (op,))
    cab.call("root", {"op": "pipeline.sweep"})
    assert wait_states(cab, command) == ["needs_routing"]
    assert [p["kind"] for p in problems.open_for(cab.store, "KD-50")] == ["wait_needs_routing"]


def test_release_reinstall_chain_counts(cab):
    """C01: release → reinstall → continuation, again and again: a resolved wait is not the agent going round, the
    streak does not count it; a repeat spends nothing; the window of 6 per hour still holds."""
    config = cab.ctx.pipeline()
    assert (config.per_window, config.streak) == (6, 3)
    turn = None
    for n in range(1, 5):
        op = requested(cab, key=f"release:{n}")
        waiting(cab, op, turn)
        ended(cab, op)
        ended(cab, op)  # repeated: nothing more
        queued = queued_continues(cab)
        assert len(queued) == 1, n
        turn = run_queued(cab, queued[0]["id"])
    states = [c["state"] for c in conts(cab)]
    assert states == ["queued"] * 4
    assert problems.open_for(cab.store, "KD-50") == []
    print(f"C01 counts: queued={states.count('queued')} merged={states.count('merged')} "
          f"limited={states.count('limited')}")


def test_snapshot_shows_operation_wait(cab):
    op = requested(cab)
    waiting(cab, op)
    text = cab.call("digi-cabinet", {"op": "card.snapshot", "card": "KD-50", "format": "text"})["text"]
    assert f"operation {op[:8]} open" in text
    assert "waits for the reinstall" in text
    found = cab.call("digi-cabinet", {"op": "card.snapshot", "card": "KD-50", "format": "json"})["snapshot"]
    assert (found["next"]["source"], found["next"]["what"], found["next"]["who"]) == ("recorded", "wait_deps", "-")


# --- request keys (schema 18) and the release's deferred key ------------------------------------
def granted(cab, agents=("dev",)):
    from test_cabinet_factory_plan import STEP, approve, open_plan

    approve(cab, open_plan(cab, steps=({**STEP, "agents": list(agents)},)))


def lead(cab, key="lead:1", agents=("dev",)):
    return cab.call("digi", {"op": "operation.request", "agent": "dev", "card": "KD-50", "kind": "agent.reinstall",
                            "agents": list(agents), "key": key})["operation"]["id"]


def test_a_request_joined_from_another_card_is_waited_on_there(cab):
    """R5: root asked without a card; dev's request on KD-50 joins it — and its turn may wait on it."""
    granted(cab)
    by_root = cab.call("root", {"op": "operation.request", "kind": "agent.reinstall", "agents": ["dev"],
                                "key": "root:probe"})["operation"]["id"]
    assert lead(cab) == by_root
    command = waiting(cab, by_root)
    assert wait_states(cab, command) == ["open"]
    ended(cab, by_root)
    assert wait_states(cab, command) == ["resolved"]
    assert [c["state"] for c in conts(cab)] in (["queued"], ["merged"])  # the plan's approval queued a wake
    found = cab.call("digi", {"op": "operation.get", "agent": "dev", "id": by_root})
    assert found["operation"]["id"] == by_root  # dev may read what it joined


def test_a_key_names_its_operation_after_its_end(cab):
    """R6: a joined key stays with the operation it joined — a repeat after the end starts nothing new."""
    granted(cab)
    first = requested(cab, key="release:x1")
    assert lead(cab, key="lead:retry") == first
    ended(cab, first)
    assert lead(cab, key="lead:retry") == first
    with pytest.raises(api.ApiError) as exc:
        lead(cab, key="lead:retry", agents=("boss",))
    assert exc.value.kind in ("conflict", "refused")
    assert cab.store._conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 1


def test_a_turn_waits_on_the_release_key_before_its_request(cab):
    """R4: the release's request waits for the cabinet's update; the lead waits on its key meanwhile."""
    key = f"release:{REV[:7]}"
    command = waiting(cab, key)
    assert wait_states(cab, command) == ["open"]
    op = requested(cab, key=key)
    assert wait_states(cab, command) == ["open"]
    ended(cab, op)
    assert wait_states(cab, command) == ["resolved"] and len(queued_continues(cab)) == 1


def test_a_release_key_that_never_comes_needs_routing(cab):
    command = waiting(cab, f"release:{REV[:7]}")
    cab.now = "2026-10-14T10:19:00Z"
    cab.call("root", {"op": "pipeline.sweep"})
    assert wait_states(cab, command) == ["open"]
    cab.now = "2026-10-14T10:21:00Z"
    cab.call("root", {"op": "pipeline.sweep"})
    assert wait_states(cab, command) == ["needs_routing"]


def test_only_a_release_key_may_wait_unknown(cab):
    record = cab.store.card("KD-50")
    begin(cab, record)
    for ref in ("lead:other", "release:../x", "root:probe"):
        with pytest.raises(api.ApiError):
            finish(cab, record, outcome="wait_dependency", note="x", operations=[ref])


def test_v17_base_migrates_with_its_request_keys(tmp_path):
    import sqlite3

    from kd.cabinet import operations, schema, store

    path = str(tmp_path / "v17.db")
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(schema.DDL + "".join(schema.MIGRATIONS[v] for v in range(2, 18)))
    conn.execute("INSERT INTO meta VALUES ('schema_version', '17')")
    conn.execute("INSERT INTO meta VALUES ('next_card', '1')")
    conn.execute("INSERT INTO operations (id, key, kind, card, requested_by, agents, revision, payload_sha, created,"
                 " updated) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 ("e" * 32, "release:abc1234", "agent.reinstall", "KD-50", "release", '["dev"]', "abc1234", "s",
                  "2026-10-21T18:00:00Z", "2026-10-21T18:00:00Z"))
    conn.close()
    db = store.Store.open(path)
    assert db.schema_version() == schema.SCHEMA_VERSION  # 18 and every later step
    found = operations.request_row(db, "release:abc1234")
    assert (found["operation"], found["card"], found["requester"]) == ("e" * 32, "KD-50", "release")


def test_the_card_facts_say_what_an_operation_wait_and_a_pause_are(cab):
    """R8/R10: an operation wait names its roles and state, and a role an operation still holds says by whom, on
    which card, what it waits for and until when."""
    from kd.cabinet import outcomes

    op = requested(cab, agents=("analyst", "dev"))
    record = cab.store.card("KD-50")
    command, session = begin(cab, record)  # dev works a turn on KD-50
    report(cab, {"type": "session", "session": session, "state": "working"})
    report(cab, {"type": "operation", "operation": op, "state": "running", "phase": "draining",
                 "agents": {"analyst": {"state": "ok", "stage": "verify", "error": ""}}, "until": 1_800_000_000})
    finish(cab, record, outcome="wait_dependency", note="waits", operations=[op])
    facts = outcomes.card_facts(cab.ctx, record)
    assert facts["pauses"] == [{"agent": "@dev", "held": "operation", "operation": op, "card": "KD-50", "phase": "draining",
                                "until": "2027-01-15T08:00:00Z", "busy": [{"agent": "@dev", "card": "KD-50"}]}]
    end(cab, command, session)
    [wait] = outcomes.card_facts(cab.ctx, record)["waits"]
    assert wait["operation"] == {"id": op, "agents": ["analyst", "dev"], "state": "running", "phase": "draining",
                                 "done": ["analyst"], "component": ""}
    key_wait = outcomes.operation_view(cab.store, "release:abc1234")
    assert key_wait == {"id": "", "agents": [], "state": "awaited", "phase": "", "done": [], "component": ""}


# --- final review: a role held after a failure (I2), pauses by visibility (M3), busy per role (M4) ----------------
def test_a_role_held_after_a_failed_operation_shows_until_root_releases_it(cab):
    from kd.cabinet import outcomes

    op = requested(cab)
    report(cab, {"type": "operation", "operation": op, "state": "failed", "phase": "installing", "agents": OK,
                 "reason": "host_check_failed", "kept": ["dev"]})
    assert outcomes.pauses(cab.store) == [{"agent": "@dev", "held": "failed", "operation": op, "card": "KD-50",
                                           "phase": "installing", "until": "", "busy": []}]
    report(cab, {"type": "operation_release", "operation": op, "agent": "dev"})
    assert outcomes.pauses(cab.store) == []


def test_an_old_failed_result_without_kept_holds_what_the_host_held(cab):
    from kd.cabinet import outcomes

    op = requested(cab)
    report(cab, {"type": "operation", "operation": op, "state": "failed", "phase": "installing", "agents": {},
                 "reason": "runner_lost"})
    assert [p["agent"] for p in outcomes.pauses(cab.store)] == ["@dev"]


def test_a_later_operation_of_the_role_ends_the_hold(cab):
    from kd.cabinet import outcomes

    first = requested(cab)
    ended(cab, first, "failed", {"dev": {"state": "failed", "stage": "install", "error": "x"}}, "install_failed")
    second = requested(cab, key="root:2")
    ended(cab, second)
    assert outcomes.pauses(cab.store) == []


def test_a_pause_names_only_the_held_roles_turns_and_visible_cards(cab):
    from kd.cabinet import outcomes

    op = requested(cab, agents=("analyst", "dev"))
    record = cab.store.card("KD-50")
    command, session = begin(cab, record)
    report(cab, {"type": "session", "session": session, "state": "working"})
    report(cab, {"type": "operation", "operation": op, "state": "running", "phase": "draining"})
    by = {p["agent"]: p for p in outcomes.pauses(cab.store)}
    assert by["@analyst"]["busy"] == [] and by["@dev"]["busy"] == [{"agent": "@dev", "card": "KD-50"}]
    hidden = {p["agent"]: p for p in outcomes.pauses(cab.store, seen=lambda card: False)}
    assert hidden["@dev"]["card"] == "" and hidden["@dev"]["busy"] == [{"agent": "@dev", "card": ""}]
    end(cab, command, session)


def test_a_release_key_asked_for_another_card_does_not_resolve_this_wait(cab):
    """Review M8: the key names a release, not a card — a request of it on another card is not this card's
    operation: the wait needs routing instead of resolving on someone else's install."""
    key = f"release:{REV[:7]}"
    command = waiting(cab, key)
    cab.put("KD-51", project="digipaddock", assignee="@dev", status="In Progress")
    op = cab.call("root", {"op": "operation.request", "kind": "agent.reinstall", "agents": ["dev"], "key": key,
                           "card": "KD-51"})["operation"]["id"]
    cab.call("root", {"op": "pipeline.sweep"})
    assert wait_states(cab, command) == ["needs_routing"]
    ended(cab, op)
    assert queued_continues(cab) == []


def test_a_turn_waits_on_a_pair_release_key(cab):
    """the key of a pair release is ``release:<rev>+<deploy>``."""
    key = f"release:{REV[:7]}+1234567"
    command = waiting(cab, key)
    assert wait_states(cab, command) == ["open"]
