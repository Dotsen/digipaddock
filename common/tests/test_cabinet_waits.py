"""waits resolve from their sources in the event's transaction; one continuation per complete set."""

import pytest
from cabinet_env import CabinetEnv

from kd import pipeline_config, session_config
from kd.cabinet import api, continuations, decisions, problems
from test_cabinet_outcomes import begin, end, finish, outcome_of, question
from test_cabinet_outcomes import card as new_card

HOSTED = session_config.HostConfig({"boss": 1, "dev": 2, "analyst": 1})


@pytest.fixture
def cab(tmp_path):
    env = CabinetEnv(tmp_path)
    env.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    api.install_host(env.ctx, lambda: HOSTED)
    env.ctx.pipeline = lambda: pipeline_config.Config(mode="enforce")
    return env


def card(cab, **kw):
    """A card whose wake on creation already ran: only what the test does is queued."""
    record = new_card(cab, **kw)
    for command in cab.store.commands(card=record.id, states=["queued"]):
        for state in ("delivered", "started", "done"):
            cab.store.command_state(command["id"], state)
    return record


def observe(cab):
    cab.ctx.pipeline = lambda: pipeline_config.Config(mode="observe")


def answer(cab, decision_id, text="Да"):
    cab.call("digi-cabinet", {"op": "decision.reply", "id": decision_id, "user": "owner", "text": text})


def reject(cab, decision_id, text="Нет"):
    cab.call("digi-cabinet", {"op": "decision.reject", "id": decision_id, "user": "owner", "text": text})


def waiting(cab, record, *, outcome="wait_user", asked=1, **extra):
    """A turn of @dev that asked ``asked`` questions and ended waiting for them; the questions and its command."""
    command, session = begin(cab, record)
    ids = [question(cab, record, text=f"q{n}") for n in range(asked)]
    finish(cab, record, outcome=outcome, note="waiting for the owner", **extra)
    end(cab, command, session)
    return ids, command


def dev_commands(cab, record):
    return [c for c in cab.store.commands(card=record.id, states=["queued"]) if c["agent"] == "dev"]


def conts(cab, record):
    return [dict(r) for r in cab.store._conn.execute(
        "SELECT * FROM continuations WHERE card=? ORDER BY created, rowid", (record.id,))]


def wait_states(cab, command):
    return [r[0] for r in cab.store._conn.execute("SELECT state FROM waits WHERE command=? ORDER BY ref", (command,))]


def test_answer_before_the_turn_end_resolves_at_activation(cab):
    record = card(cab)
    command, session = begin(cab, record)
    asked = question(cab, record)
    finish(cab, record, outcome="wait_user", note="waiting")
    answer(cab, asked)  # during the turn: the wait is still pending
    end(cab, command, session)
    assert wait_states(cab, command) == ["resolved"]
    assert len(dev_commands(cab, record)) == 1  # the answer's wake, which carries the continuation
    [cont] = conts(cab, record)
    assert cont["key"] == f"wait:{command}" and cont["state"] == "merged"


def test_two_forms_answer_out_of_order(cab):
    record = card(cab)
    (first, second), command = waiting(cab, record, asked=2)
    answer(cab, second)
    assert dev_commands(cab, record) == [] and sorted(wait_states(cab, command)) == ["open", "resolved"]
    answer(cab, first)
    [wake] = dev_commands(cab, record)
    assert wake["kind"] == "answer" and first in wake["text"]
    [cont] = conts(cab, record)
    assert (cont["state"], cont["command"]) == ("merged", wake["id"])


def test_any_mode_continues_on_one(cab):
    record = card(cab)
    (first, _), command = waiting(cab, record, asked=2, any=True)
    answer(cab, first)
    assert len(dev_commands(cab, record)) == 1 and conts(cab, record)[0]["key"] == f"wait:{command}"


def test_rejected_question_ends_the_wait(cab):
    record = card(cab)
    (asked,), command = waiting(cab, record)
    reject(cab, asked)
    assert wait_states(cab, command) == ["rejected"]
    [command_row] = dev_commands(cab, record)
    assert command_row["text"] == continuations.ENDED.format(id=record.id, state="rejected")


def test_expired_decision_ends_the_wait(cab):
    record = card(cab)
    (asked,), command = waiting(cab, record)
    decisions.Decisions(cab.store).expire_one(asked, now=cab.now)
    assert wait_states(cab, command) == ["expired"]
    assert [c["text"] for c in dev_commands(cab, record)] == [
        continuations.ENDED.format(id=record.id, state="expired")]


def _depending(cab):
    dep = card(cab, assignee="@sysadmin")
    record = card(cab)
    cab.store.edit_card(record.id, None, {"dependencies": [dep.id]}, actor="user:owner")
    command, session = begin(cab, cab.store.card(record.id))
    finish(cab, record, outcome="wait_dependency", note="waiting for the dependency")
    end(cab, command, session)
    return dep, cab.store.card(record.id), command


def test_dependency_done_continues_the_consumer(cab):
    dep, record, command = _depending(cab)
    cab.store.edit_card(dep.id, None, {"status": "Done"}, actor="user:owner")
    assert wait_states(cab, command) == ["resolved"]
    assert [c["text"] for c in dev_commands(cab, record)] == [continuations.RESOLVED.format(id=record.id)]


def test_missing_or_duplicate_dependency_needs_routing(cab):
    dep, record, command = _depending(cab)
    cab.store.edit_card(dep.id, None, {"status": "Done", "labels_add": ["duplicate-of:KD-77"]}, actor="user:owner")
    assert wait_states(cab, command) == ["needs_routing"]
    assert [p["kind"] for p in problems.open_for(cab.store, record.id)] == ["wait_needs_routing"]
    assert dev_commands(cab, record) == []


def test_unrelated_answer_resolves_nothing(cab):
    record = card(cab)
    (asked,), command = waiting(cab, record)
    other = decisions.Decisions(cab.store).open("question", card=record.id, payload={"text": "?"},
                                                requested_by="agent:analyst", now=cab.now)["id"]
    answer(cab, other)
    assert wait_states(cab, command) == ["open"] and conts(cab, record) == []


def test_observe_resolves_but_queues_nothing(cab):
    observe(cab)
    record = card(cab)
    (first, second), command = waiting(cab, record, asked=2)
    answer(cab, second)
    assert [c["kind"] for c in dev_commands(cab, record)] == ["answer"]  # the old wake, as before
    answer(cab, first)
    assert sorted(wait_states(cab, command)) == ["resolved", "resolved"]
    [cont] = conts(cab, record)
    assert cont["state"] == "observed"
    assert all(c["kind"] == "answer" for c in dev_commands(cab, record))


def test_late_outcome_of_an_old_command_does_not_overwrite(cab):
    record = card(cab)
    command, session = begin(cab, record)
    asked = question(cab, record)
    finish(cab, record, outcome="wait_user", note="waiting")
    cab.store.edit_card(record.id, None, {"assignee": "@sysadmin"}, actor="user:owner")
    end(cab, command, session)
    assert outcome_of(cab, command)["state"] == "superseded"
    answer(cab, asked)
    assert conts(cab, record) == []


# --- final review findings --------------------------------------------------------------------------------------
@pytest.mark.parametrize("change", [{"status": "Review"}, {"assignee": "@sysadmin"}, {"assignee": "@owner"}])
def test_a_problem_closes_when_the_card_leaves_its_holder(cab, change):
    record = card(cab)
    command, session = begin(cab, record)
    end(cab, command, session)
    assert [p["kind"] for p in problems.open_for(cab.store, record.id)] == ["missing_next_step"]
    cab.store.edit_card(record.id, None, change, actor="user:owner")
    assert problems.open_for(cab.store, record.id) == []


def test_every_kind_closes_when_the_holders_next_turn_starts(cab):
    cab.ctx.pipeline = lambda: pipeline_config.Config(mode="enforce", streak=1)
    record = card(cab)
    for _ in range(2):
        command, session = begin(cab, record)
        finish(cab, record)
        end(cab, command, session)
    # the second turn's continuation hit the limit as its outcome applied: that problem stays
    assert [p["kind"] for p in problems.open_for(cab.store, record.id)] == ["continuation_limit"]
    begin(cab, record)
    assert problems.open_for(cab.store, record.id) == []


def test_a_new_outcome_supersedes_the_older_waits(cab):
    from kd.cabinet import outcomes

    record = card(cab)
    (asked,), first = waiting(cab, record)
    command, session = begin(cab, record)
    finish(cab, record, outcome="wait_user", note="still waiting")
    end(cab, command, session)
    rows = cab.store._conn.execute("SELECT command, state, reason FROM waits WHERE card=? ORDER BY created, rowid",
                                   (record.id,)).fetchall()
    assert [tuple(r) for r in rows] == [(first, "cancelled", "superseded"), (command, "open", "")]
    facts = outcomes.card_facts(cab.ctx, cab.store.card(record.id))
    assert [(w["ref"], bool(w["id"])) for w in facts["waits"]] == [(asked, True)]
    answer(cab, asked)
    assert len(dev_commands(cab, record)) == 1


def test_only_an_answer_wake_is_held_back(cab):
    from kd.cabinet import waits

    record = card(cab)
    (first, _), command = waiting(cab, record, asked=2)
    queued = []

    def enqueue(spec):
        queued.append(spec)
        return {"id": f"c{len(queued)}"}

    specs = [{"agent": "dev", "card": record.id, "kind": kind, "text": kind} for kind in ("answer", "assigned")]
    waits.hook(cab.ctx)(cab.store, {"kind": "decision.approved", "card": record.id, "payload": {"decision": first}},
                        specs, enqueue)
    assert [s["kind"] for s in queued] == ["assigned"]
