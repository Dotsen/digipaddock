"""automatic continuations — one command per key, nothing in observe, limits by window and by a streak
without a verified result, kept in the base across a restart."""

import pytest
from cabinet_env import CabinetEnv

from kd import pipeline_config, session_config
from kd.cabinet import continuations, problems, store

HEAD = "a" * 40


@pytest.fixture
def cab(tmp_path):
    env = CabinetEnv(tmp_path)
    env.ctx.host_config = lambda: session_config.HostConfig({"boss": 1, "dev": 2})
    env.ctx.pipeline = lambda: pipeline_config.Config(mode="enforce")
    return env


def card(cab):
    return cab.store.create_card({"title": "Work", "project": "digipaddock", "status": "In Progress", "assignee": "@dev",
                                  "created_by": "@owner"}, actor="user:owner")


def launch(cab, record, key, text=None, source="turn"):
    record = cab.store.card(record.id)
    return continuations.launch(cab.ctx, key=key, record=record, agent="dev", source=source,
                                text=text or continuations.CONTINUE.format(id=record.id))


def command(cab, record, key, initiator, kind="message"):
    """A command of dev in the card's thread from ``initiator``, run to the end."""
    made = cab.store.enqueue({"key": key, "agent": "dev", "thread": record.thread, "card": record.id, "kind": kind,
                              "text": f"[board] {record.id}: {key}", "initiator": initiator})
    for state in ("delivered", "started", "done"):
        cab.store.command_state(made["id"], state)


def finish_commands(cab, record):
    """Every queued command of the card done, so the next identical text is a new command, not the same one."""
    for command in cab.store.commands(card=record.id, states=["queued"]):
        for state in ("delivered", "started", "done"):
            cab.store.command_state(command["id"], state)


def pr(cab, record, head=HEAD):
    cab.store.pr_put({"repo": "owner/repo", "project": "digipaddock", "number": 12, "card": record.id, "title": "x",
                      "url": "https://forge.example/owner/repo/pulls/12", "head": head, "state": "open", "files": [],
                      "cage": [], "diff": "", "truncated": False, "release": False}, actor="forge")


def at(cab, minutes):
    cab.now = f"2026-10-14T{10 + minutes // 60:02d}:{minutes % 60:02d}:00Z"


def test_mark_ignores_notes_and_progress(cab):
    record = card(cab)
    cab.store.edit_card(record.id, None, {"ac_add": ["Критерий"]}, actor="user:owner")
    before = continuations.mark(cab.store, cab.store.card(record.id))
    assert len(before) == 16
    cab.store.edit_card(record.id, None, {"notes_append": "заметка"}, actor="agent:dev")
    cab.store.edit_card(record.id, None, {"comment": {"author": "@dev", "text": "иду"}}, actor="agent:dev")
    cab.store.edit_card(record.id, None, {"ac_check": [1]}, actor="agent:dev")
    cab.store.add_thread_message(record.thread, author="@dev", kind="progress", body="шаг 1", payload={"step": 1})
    assert continuations.mark(cab.store, cab.store.card(record.id)) == before
    pr(cab, record)
    after = continuations.mark(cab.store, cab.store.card(record.id))
    pr(cab, record, head="b" * 40)
    assert len({before, after, continuations.mark(cab.store, cab.store.card(record.id))}) == 3


def test_launch_is_idempotent_by_key(cab):
    record = card(cab)
    first = launch(cab, record, "cont:c1")
    again = launch(cab, record, "cont:c1")
    assert first["state"] == again["state"] == "queued" and first["command"] == again["command"]
    [command] = cab.store.commands(card=record.id)
    assert command["id"] == first["command"] and command["kind"] == "continue"
    basis = command["basis"]
    assert (basis["assignee"], basis["status"], basis["iteration"], basis["grant"]) == ("@dev", "In Progress", 0, "")
    assert len(basis["scope"]) == 16


def test_launch_merged_into_queues_nothing(cab):
    record = card(cab)
    row = continuations.launch(cab.ctx, key="cont:c2", record=record, agent="dev", source="turn",
                               text=continuations.CONTINUE.format(id=record.id), merged_into="cmd-1")
    assert (row["state"], row["command"]) == ("merged", "cmd-1") and cab.store.commands(card=record.id) == []


def test_launch_onto_the_same_text_still_queued_is_merged(cab):
    record = card(cab)
    first = launch(cab, record, "cont:c1")
    second = launch(cab, record, "cont:c2")
    assert second["state"] == "merged" and second["command"] == first["command"]
    assert len(cab.store.commands(card=record.id)) == 1


def test_launch_observe_queues_nothing(cab):
    cab.ctx.pipeline = lambda: pipeline_config.Config(mode="observe")
    record = card(cab)
    assert launch(cab, record, "cont:c1")["state"] == "observed"
    assert cab.store.commands(card=record.id) == []


def test_a_window_limited_continuation_is_queued_for_the_window_end(cab):
    """Over 6 in the hour the continuation waits for the window, it is not dropped; only queued rows
    count (merged and observed do not)."""
    record = card(cab)
    continuations.launch(cab.ctx, key="m0", record=record, agent="dev", source="turn", text="x", merged_into="c")
    cab.ctx.pipeline = lambda: pipeline_config.Config(mode="observe")
    launch(cab, record, "o0")
    cab.ctx.pipeline = lambda: pipeline_config.Config(mode="enforce")
    for n in range(6):
        at(cab, n)
        cab.store.add_thread_message(record.thread, author="@dev", kind="diagnostic", body="ok", payload={})
        assert launch(cab, record, f"cont:{n}")["state"] == "queued"
        finish_commands(cab, record)
    at(cab, 10)
    cab.store.add_thread_message(record.thread, author="@dev", kind="diagnostic", body="ok", payload={})
    row = launch(cab, record, "cont:6")
    assert (row["state"], row["reason"]) == ("queued", "window")
    assert cab.store.command(row["command"])["next_try"] == "2026-10-14T11:00:00Z"  # the oldest counted + 1 h
    assert problems.open_for(cab.store, record.id) == []


def test_the_streak_counts_only_turn_continuations(cab):
    """Continuations after a wait resolved are not the agent going round: they do not count in the streak."""
    record = card(cab)
    for n in range(2):
        assert launch(cab, record, f"cont:{n}")["state"] == "queued"
        finish_commands(cab, record)
    for n in range(3):
        assert launch(cab, record, f"wait:{n}", text=continuations.RESOLVED.format(id=record.id),
                      source="wait")["state"] == "queued"
        finish_commands(cab, record)
    assert launch(cab, record, "cont:2")["state"] == "queued"
    finish_commands(cab, record)
    assert launch(cab, record, "cont:3")["state"] == "limited"


def test_an_outside_command_resets_the_streak(cab):
    """A command from someone else (the owner, BOSS) is new input: the streak counts from it; the agent's own
    signal to itself is not."""
    record = card(cab)
    for n in range(3):
        at(cab, n)
        assert launch(cab, record, f"cont:{n}")["state"] == "queued"
        finish_commands(cab, record)
    at(cab, 5)
    command(cab, record, "self", "agent:dev", kind="signal")
    at(cab, 6)
    assert launch(cab, record, "cont:3")["state"] == "limited"
    at(cab, 7)
    command(cab, record, "owner", "user:owner")
    at(cab, 8)
    assert launch(cab, record, "cont:4")["state"] == "queued"


def test_a_second_streak_after_a_reset_raises_its_own_problem(cab):
    record = card(cab)
    for round_, start in enumerate((0, 10)):
        for n in range(3):
            at(cab, start + n)
            launch(cab, record, f"cont:{round_}:{n}")
            finish_commands(cab, record)
        at(cab, start + 4)
        assert launch(cab, record, f"cont:{round_}:x")["state"] == "limited"
        problems.resolve(cab.store, record.id, ("continuation_limit",), by="test")
        at(cab, start + 5)
        command(cab, record, f"boss{round_}", "agent:boss", kind="signal")
    assert len(cab.store._conn.execute("SELECT * FROM problems WHERE kind='continuation_limit'").fetchall()) == 2


def test_a_continuation_does_not_merge_into_a_pending_stop(cab):
    """The owner's stop wins — the continuation is recorded as stopped, nothing is queued after it."""
    record = card(cab)
    stop = cab.store.enqueue({"key": "stop1", "agent": "dev", "thread": record.thread, "card": record.id,
                              "kind": "stop", "text": "[session] stop", "initiator": "user:owner"})
    row = launch(cab, record, "cont:c1")
    assert (row["state"], row["command"]) == ("stopped", "")
    assert [c["id"] for c in cab.store.commands(card=record.id)] == [stop["id"]]


def test_streak_limit_resets_on_a_verified_result(cab):
    record = card(cab)
    for n in range(3):
        assert launch(cab, record, f"cont:{n}")["state"] == "queued"
        finish_commands(cab, record)
    assert launch(cab, record, "cont:3")["state"] == "limited"
    assert continuations.limit_reason(cab.ctx, cab.store.card(record.id)) == "streak"
    pr(cab, record)
    assert launch(cab, record, "cont:4")["state"] == "queued"


def test_limits_survive_reopen(cab, tmp_path):
    record = card(cab)
    for n in range(3):
        launch(cab, record, f"cont:{n}")
        finish_commands(cab, record)
    cab.store.close()
    cab.store = cab.ctx.store = store.Store.open(str(tmp_path / "cabinet.db"), clock=cab.clock)
    assert launch(cab, record, "cont:3")["state"] == "limited"
