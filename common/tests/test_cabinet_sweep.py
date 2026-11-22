"""the pipeline sweep — what an event missed, outcomes of dead commands, turns that left nobody on the card;
never the turns that ended before the pipeline began to count."""

import pytest
from cabinet_env import CabinetEnv

from kd import pipeline_config, session_config
from kd.cabinet import api, problems
from test_cabinet_outcomes import begin, end, finish
from test_cabinet_waits import card, conts, dev_commands, waiting

HOSTED = session_config.HostConfig({"boss": 1, "dev": 2, "analyst": 1})


@pytest.fixture
def cab(tmp_path):
    env = CabinetEnv(tmp_path)
    env.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    api.install_host(env.ctx, lambda: HOSTED)
    env.ctx.pipeline = lambda: pipeline_config.Config(mode="enforce", since="2000-01-01T00:00:00Z")
    # the migration stamps the machine's clock; the tests' clock is earlier
    env.store._conn.execute("UPDATE meta SET value='2000-01-01T00:00:00Z' WHERE key='pipeline_since'")
    return env


def sweep(cab):
    return cab.call("digi-cabinet", {"op": "pipeline.sweep"})


def test_sweep_resolves_a_wait_whose_event_was_missed(cab):
    record = card(cab)
    (asked,), command = waiting(cab, record)
    cab.store._conn.execute("UPDATE decisions SET state='approved' WHERE id=?", (asked,))  # past the hook
    assert sweep(cab)["rechecked"] == 1
    assert [c["text"] for c in dev_commands(cab, record)] and len(conts(cab, record)) == 1
    sweep(cab)
    assert len(conts(cab, record)) == 1 and len(dev_commands(cab, record)) == 1


def test_sweep_voids_outcomes_of_dead_commands(cab):
    record = card(cab)
    command, _ = begin(cab, record)
    finish(cab, record)
    cab.store._conn.execute("UPDATE commands SET state='cancelled' WHERE id=?", (command,))
    assert sweep(cab)["voided"] == 1
    state = cab.store._conn.execute("SELECT state FROM turn_outcomes WHERE command=?", (command,)).fetchone()[0]
    assert state == "void"


def test_cancelled_continuation_leaves_a_problem(cab):
    record = card(cab)
    command, session = begin(cab, record)
    finish(cab, record)
    end(cab, command, session)
    [cont] = [c for c in dev_commands(cab, record) if c["kind"] == "continue"]
    cab.store.command_state(cont["id"], "cancelled", reason="scope_changed")
    assert sweep(cab)["problems"] == 1
    [problem] = problems.open_for(cab.store, record.id)
    assert (problem["kind"], problem["key"]) == ("missing_next_step", f"missing:{cont['id']}")
    assert sweep(cab)["problems"] == 0


@pytest.mark.parametrize("mode", ["observe", "enforce"])
def test_sweep_ignores_turns_before_since(cab, mode):
    cab.ctx.pipeline = lambda: pipeline_config.Config(mode=mode, since="2026-10-14T12:00:00Z")
    cab.store._conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('pipeline_since', ?)",
                            ("2026-10-14T12:00:00Z",))
    records = [card(cab) for _ in range(30)]  # their wakes ran and ended at 10:00, before since
    assert sweep(cab)["problems"] == 0
    assert all(problems.open_for(cab.store, r.id) == [] and dev_commands(cab, r) == [] for r in records)


def test_status_reports_counts(cab):
    record = card(cab)
    waiting(cab, record)
    other = card(cab)
    command, session = begin(cab, other)
    end(cab, command, session)
    status = cab.call("root", {"op": "pipeline.status"})
    assert (status["protocol"], status["mode"], status["open_waits"], status["open_problems"]) == (1, "enforce", 1, 1)
    assert status["pending"] == 0 and status["schema"] >= 11 and "continuations_hour" in status


# --- final review findings --------------------------------------------------------------------------------------
def test_a_stopped_turn_is_not_a_missing_next_step(cab):
    record = card(cab)
    command, session = begin(cab, record)
    stop = cab.store.enqueue({"key": "stop1", "agent": "dev", "thread": record.thread, "card": record.id,
                              "kind": "stop", "text": "stop"})["id"]
    cab.store.command_state(stop, "delivered", session=session)
    cab.store.command_state(stop, "done")
    end(cab, command, session, ok=False)
    assert sweep(cab)["problems"] == 0 and problems.open_for(cab.store, record.id) == []


def test_a_failed_turn_on_a_card_in_work_raises_missing_next_step(cab):
    """A turn that failed left nobody on the card — BOSS hears it at once."""
    record = card(cab)
    command, session = begin(cab, record)
    end(cab, command, session, ok=False)
    [problem] = problems.open_for(cab.store, record.id)
    assert (problem["kind"], problem["key"]) == ("missing_next_step", f"missing:{command}")


def test_the_sweep_raises_a_problem_for_a_turn_cut_by_a_host_restart(cab):
    record = card(cab)
    command, _ = begin(cab, record)
    cab.call("root", {"op": "host.report", "items": [{"type": "host", "phase": "started", "inflight": [command]}]})
    assert cab.store.command(command)["state"] == "failed"
    assert sweep(cab)["problems"] == 1
    [problem] = problems.open_for(cab.store, record.id)
    assert (problem["kind"], problem["key"]) == ("missing_next_step", f"missing:{command}")


def test_sweep_raises_for_a_superseded_outcome_the_turn_end_missed(cab):
    record = card(cab)
    command, session = begin(cab, record)
    finish(cab, record)
    cab.store.edit_card(record.id, None, {"description": "a new scope"}, actor="user:owner")
    end(cab, command, session)
    cab.store._conn.execute("DELETE FROM problems")  # as if the turn's end had raised nothing
    assert sweep(cab)["problems"] == 1


def test_sweep_withdraws_open_decisions_left_on_closed_cards(cab):
    """a decision opened on a card that closed before the rule existed (or under the old code) is
    withdrawn by the sweep; open decisions on live cards stay."""
    from kd.cabinet import decisions

    ledger = decisions.Decisions(cab.store)
    done = cab.card(status="In Progress", assignee="@dev")
    stale = ledger.open("question", card=done.id, payload={"text": "?"}, requested_by="agent:dev", now=cab.now)
    cab.store._conn.execute("UPDATE cards SET status='Done' WHERE id=?", (done.id,))  # closed without the hook
    cab.store._conn.commit()
    live = cab.card(status="In Progress", assignee="@dev")
    kept = ledger.open("question", card=live.id, payload={"text": "?"}, requested_by="agent:dev", now=cab.now)
    assert sweep(cab)["decisions"] == 1
    assert ledger.get(stale["id"])["state"] == "revoked" and ledger.get(kept["id"])["state"] == "open"
    assert sweep(cab)["decisions"] == 0
