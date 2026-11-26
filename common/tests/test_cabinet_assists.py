"""help inside an approved task — the lead asks another role for one bounded read-only step, BOSS assigns
it, the helper answers on the lead's card (spec 2026-10-22 §8)."""

import pytest
from cabinet_env import CabinetEnv

from kd import session_config
from kd.cabinet import api, assists, continuations

BASE = "digi"  # the executors run as the base user; the agent comes from the channel
HOSTED = {"boss": 1, "dev": 2, "sysadmin": 1, "analyst": 1}
STEP = "прочитать копию состояния монитора и сверить номера инцидентов"


@pytest.fixture
def cab(tmp_path):
    env = CabinetEnv(tmp_path)
    api.install_host(env.ctx, lambda: session_config.HostConfig(dict(HOSTED)))  # the events queue commands
    return env


def card(cab, assignee="@dev", status="In Progress", **extra):
    return cab.store.create_card({"title": "Work", "project": "digipaddock", "status": status, "assignee": assignee,
                                  "created_by": "@owner", "acceptance": [{"text": "сверка", "checked": False}],
                                  **extra}, actor="user:owner")


def started(cab, record, agent="dev", thread=None, kind="assigned"):
    """A command of ``agent`` the host delivered and started in ``thread`` (the card's by default)."""
    thread = thread or record.thread
    session = cab.store.session_for(thread, agent)["id"]
    command = cab.store.enqueue({"key": f"k:{thread}:{agent}:{kind}", "agent": agent, "thread": thread,
                                 "card": record.id if thread == record.thread else "", "kind": kind,
                                 "text": f"[board] {record.id} {kind}"})["id"]
    for state in ("delivered", "started"):
        cab.store.command_state(command, state, session=session)
    return command


def request(cab, record, agent="dev", **over):
    body = {"op": "assist.request", "agent": agent, "card": record.id, "kind": "platform_read", "to": "sysadmin",
            "step": STEP, "ac": 1, "key": "kd9054-ac2"}
    body.update(over)
    return cab.call(BASE, body)


def assign(cab, assist_id, to="sysadmin", agent="boss"):
    return cab.call(BASE, {"op": "assist.assign", "agent": agent, "id": assist_id, "to": to, "reason": "по каталогу"})


def show(cab, assist_id, agent="sysadmin"):
    return cab.call(BASE, {"op": "assist.show", "agent": agent, "id": assist_id})


def reply(cab, assist_id, agent="sysadmin", **over):
    body = {"op": "assist.reply", "agent": agent, "id": assist_id, "state": "done", "evidence": "333 из 333",
            "limit": "только номера и ключи"}
    body.update(over)
    return cab.call(BASE, body)


def failing(expect, call, *args, **kw):
    with pytest.raises(api.ApiError) as exc:
        call(*args, **kw)
    assert exc.value.kind == expect, exc.value
    return str(exc.value)


def signals(cab, record, agent):
    return [c for c in cab.store.commands(card=record.id, states=["queued"]) if c["agent"] == agent]


def asked(cab):
    record = card(cab)
    started(cab, record)
    return record, request(cab, record)["assist"]


def test_request_from_running_turn_records_and_signals_boss(cab):
    record, row = asked(cab)
    assert row["state"] == "requested" and row["lead"] == "dev" and row["role"] == "sysadmin"
    assert row["basis"]["assignee"] == "@dev" and row["ac"] == 1
    [signal] = signals(cab, record, "boss")
    assert signal["kind"] == "signal" and signal["key"] == f"assist:{row['id']}:request"
    assert signal["thread"] == record.thread
    assert signal["text"] == assists.REQUEST_SIGNAL.format(card=record.id, lead="dev", short=row["id"][:8])
    kinds = [r[0] for r in cab.store._conn.execute("SELECT kind FROM events WHERE card=?", (record.id,))]
    assert "assist.requested" in kinds


def test_request_repeat_same_key_is_unchanged_other_payload_conflicts(cab):
    record, row = asked(cab)
    again = request(cab, record)
    assert again["unchanged"] is True and again["assist"]["id"] == row["id"]
    assert len(signals(cab, record, "boss")) == 1
    failing("conflict", request, cab, record, step="другой шаг")


def test_request_refused_without_running_turn(cab):
    record = card(cab)
    failing("refused", request, cab, record)  # no turn at all
    started(cab, record, thread=cab.store.agent_thread("dev"), kind="chat")
    failing("refused", request, cab, record)  # a turn in its chat, not on the card
    started(cab, record, agent="sysadmin", kind="message")
    failing("refused", request, cab, record, agent="sysadmin", to="dev", kind="code_read")  # not the card's lead


def test_request_refused_for_unknown_kind_own_role_and_closed_card(cab):
    record = card(cab)
    started(cab, record)
    failing("invalid", request, cab, record, kind="shell")
    failing("refused", request, cab, record, kind="code_read", to="dev")  # its own role
    failing("refused", request, cab, record, kind="server_read", to="analyst")  # not a role of the kind
    for status in ("Done", "Inbox"):
        other = card(cab, status=status)
        started(cab, other)
        failing("refused", request, cab, other)


def test_request_from_review_moves_card_back_to_in_progress(cab):
    record = card(cab, status="Review")
    started(cab, record)
    row = request(cab, record)["assist"]
    assert cab.store.card(record.id).status == "In Progress"
    assert row["basis"]["status"] == "In Progress"
    reasons = [r[0] for r in cab.store._conn.execute(
        "SELECT json_extract(payload, '$.reason') FROM events WHERE card=? AND kind='card.changed'", (record.id,))]
    assert any(r and f"help {row['id'][:8]}" in r for r in reasons)
    assert signals(cab, record, "dev") == []  # its own move wakes nobody: the lead's turn is running


def test_assign_queues_help_command_with_basis(cab):
    record, row = asked(cab)
    done = assign(cab, row["id"])["assist"]
    assert done["state"] == "assigned" and done["helper"] == "sysadmin"
    [signal] = signals(cab, record, "sysadmin")
    assert signal["kind"] == "signal" and signal["thread"] == record.thread
    assert signal["key"].startswith(f"assist:{row['id']}:help:sysadmin:")
    assert signal["basis"] == continuations.basis_of(cab.store, cab.store.card(record.id))
    assert signal["text"] == assists.HELP_SIGNAL.format(card=record.id, lead="dev", short=row["id"][:8], id=row["id"])
    assign(cab, row["id"])  # the same role again: no second command
    assert len(signals(cab, record, "sysadmin")) == 1


def test_assign_refused_for_lead_role_outside_kind_or_unhosted(cab):
    record, row = asked(cab)
    failing("refused", assign, cab, row["id"], to="dev")  # the lead
    failing("refused", assign, cab, row["id"], to="analyst")  # not a role of platform_read
    failing("refused", assign, cab, row["id"], agent="dev")  # only the coordinator assigns
    del HOSTED["sysadmin"]
    try:
        failing("refused", assign, cab, row["id"])
    finally:
        HOSTED["sysadmin"] = 1


def test_show_accepts_only_for_helper(cab):
    record, row = asked(cab)
    failing("refused", show, cab, row["id"])  # not assigned yet
    assign(cab, row["id"])
    failing("refused", show, cab, row["id"], agent="analyst")
    seen = show(cab, row["id"])
    assert seen["assist"]["state"] == "accepted" and seen["card"]["id"] == record.id
    assert seen["ac_text"] == "сверка"
    assert show(cab, row["id"])["assist"]["state"] == "accepted"


def test_reply_done_posts_assist_message(cab):
    record, row = asked(cab)
    assign(cab, row["id"])
    show(cab, row["id"])
    done = reply(cab, row["id"])
    assert done["assist"]["state"] == "done" and done["unchanged"] is False and done["late"] is False
    message = cab.store._conn.execute(
        "SELECT author, kind, body, payload FROM messages WHERE thread=? AND kind='assist'", (record.thread,)).fetchone()
    assert message["author"] == "agent:sysadmin" and message["body"] == "333 из 333"
    import json
    assert json.loads(message["payload"]) == {"assist": row["id"], "state": "done", "reason": "",
                                              "limit": "только номера и ключи", "source": "reported", "late": False}


def test_reply_failed_needs_reason(cab):
    record, row = asked(cab)
    assign(cab, row["id"])
    failing("invalid", reply, cab, row["id"], state="failed")
    failing("invalid", reply, cab, row["id"], state="failed", reason="tired")
    failing("invalid", reply, cab, row["id"], state="failed", reason="expired")  # the sweep's, not the helper's
    out = reply(cab, row["id"], state="failed", reason="capability_missing", evidence="нет доступа к файлу")
    assert out["assist"]["state"] == "failed" and out["assist"]["reason"] == "capability_missing"


def test_reply_repeat_and_conflict(cab):
    record, row = asked(cab)
    assign(cab, row["id"])
    reply(cab, row["id"])
    assert reply(cab, row["id"])["unchanged"] is True
    failing("refused", reply, cab, row["id"], evidence="иначе")


def test_reply_after_cancel_is_late_evidence(cab):
    record, row = asked(cab)
    assign(cab, row["id"])
    cab.store._conn.execute("UPDATE assists SET state='cancelled', reason='withdrawn' WHERE id=?", (row["id"],))
    out = reply(cab, row["id"])
    assert out["late"] is True and out["assist"]["state"] == "cancelled"
    import json
    [payload] = [json.loads(r[0]) for r in cab.store._conn.execute(
        "SELECT payload FROM messages WHERE thread=? AND kind='assist'", (record.thread,))]
    assert payload["late"] is True


@pytest.mark.parametrize("step", ["", "x" * 501, "две\nстроки"])
def test_step_limits(cab, step):
    record = card(cab)
    started(cab, record)
    failing("invalid", request, cab, record, step=step)


def test_evidence_limit(cab):
    record, row = asked(cab)
    assign(cab, row["id"])
    failing("invalid", reply, cab, row["id"], evidence="x" * 2001)


# --- help in the pipeline ---------------------------------------------------------------------
from test_cabinet_outcomes import begin, end, enforce  # noqa: E402 — the pipeline's turn helpers
from kd.cabinet import outcomes, waits  # noqa: E402


def lead_finish(cab, record, outcome="wait_dependency", note="жду помощь", **extra):
    return cab.call(BASE, {"op": "turn.finish", "agent": "dev", "card": record.id, "outcome": outcome, "note": note,
                           **extra})


def continuations_of(cab, record):
    return [c for c in cab.store.commands(card=record.id, states=["queued"])
            if c["agent"] == "dev" and c["kind"] == "continue"]


def problem_kinds(cab, record):
    return [r[0] for r in cab.store._conn.execute(
        "SELECT kind FROM problems WHERE card=? AND state='open'", (record.id,))]


def waiting_on_help(cab):
    """The lead asked for help in its turn and ended it waiting for that help; BOSS assigned it."""
    enforce(cab)
    record = card(cab)
    command, session = begin(cab, record)
    row = request(cab, record)["assist"]
    lead_finish(cab, record, assists=[row["id"]])
    end(cab, command, session)
    assign(cab, row["id"])
    return record, row, command


def test_wait_on_help_then_done_gives_one_continuation(cab):
    record, row, command = waiting_on_help(cab)
    assert continuations_of(cab, record) == []
    show(cab, row["id"])
    reply(cab, row["id"])
    [cont] = continuations_of(cab, record)
    assert cont["key"] == f"wait:{command}"
    assert cont["text"] == continuations.RESOLVED.format(id=record.id)
    reply(cab, row["id"])  # the same answer again
    assert len(continuations_of(cab, record)) == 1


def test_help_done_before_turn_end_resolves_on_activate(cab):
    enforce(cab)
    record = card(cab)
    command, session = begin(cab, record)
    row = request(cab, record)["assist"]
    assign(cab, row["id"])
    reply(cab, row["id"])
    lead_finish(cab, record, assists=[row["id"]])
    end(cab, command, session)
    [wait] = [w for w in waits.of_command(cab.store, command) if w["kind"] == "assist"]
    assert wait["state"] == "resolved"
    assert len(continuations_of(cab, record)) == 1


def test_help_failed_wakes_lead_ended_and_boss_problem(cab):
    record, row, command = waiting_on_help(cab)
    reply(cab, row["id"], state="failed", reason="capability_missing", evidence="путь закрыт песочницей")
    [cont] = continuations_of(cab, record)
    assert cont["text"] == continuations.ENDED.format(id=record.id, state="rejected")
    assert "assist_failed" in problem_kinds(cab, record)
    assert any(c["agent"] == "boss" and c["key"] == f"problem:{p}" for c in cab.store.commands(card=record.id)
               for (p,) in cab.store._conn.execute("SELECT id FROM problems WHERE kind='assist_failed'"))
    owner_questions = cab.store._conn.execute(
        "SELECT COUNT(*) FROM decisions WHERE card=? AND type='question'", (record.id,)).fetchone()[0]
    assert owner_questions == 0


def test_wait_dependency_without_names_takes_open_help(cab):
    record = card(cab)
    begin(cab, record)
    failing("refused", lead_finish, cab, record)  # neither a dependency nor help
    row = request(cab, record)["assist"]
    out = lead_finish(cab, record)
    assert [(w["kind"], w["ref"]) for w in out["waits"]] == [("assist", row["id"])]


def test_a_turn_that_asked_for_help_and_left_no_outcome_waits_for_that_help(cab):
    """the lead asked for help and ended its turn saying nothing — the answer still wakes it, and BOSS is
    not asked to route a card that only waits."""
    enforce(cab)
    record = card(cab)
    command, session = begin(cab, record)
    row = request(cab, record)["assist"]
    end(cab, command, session)
    assert problem_kinds(cab, record) == []
    [wait] = waits.of_command(cab.store, command)
    assert (wait["kind"], wait["ref"], wait["state"], wait["blocking"]) == ("assist", row["id"], "open", 1)
    assert outcomes.next_state(cab.ctx, cab.store.card(record.id)) == "waiting"
    assign(cab, row["id"])
    show(cab, row["id"])
    reply(cab, row["id"])
    [cont] = continuations_of(cab, record)
    assert cont["key"] == f"wait:{command}"
    assert cont["text"] == continuations.RESOLVED.format(id=record.id)


def test_the_wait_the_board_recorded_reads_as_such_in_the_snapshot(cab):
    enforce(cab)
    record = card(cab)
    command, session = begin(cab, record)
    request(cab, record)
    end(cab, command, session)
    assert "outcome: wait_dependency by @dev" in snapshot_text(cab, record)
    assert "recorded by the board: the turn asked for help and left no outcome" in snapshot_text(cab, record)


def test_a_turn_the_owner_stopped_waits_for_him_not_for_its_help(cab):
    enforce(cab)
    record = card(cab)
    command, session = begin(cab, record)
    request(cab, record)
    cab.store.enqueue({"key": "stop1", "agent": "dev", "thread": record.thread, "card": record.id, "kind": "stop",
                       "text": "[session] stop", "initiator": "user:owner"})
    end(cab, command, session)
    assert waits.of_command(cab.store, command) == [] and problem_kinds(cab, record) == []


def test_a_later_turn_without_an_outcome_keeps_the_waits_the_lead_recorded(cab):
    """The lead waits for its help and a dependency; a turn an owner's word started ends saying nothing — the
    recorded set stays whole, the board adds no wait of its own in its place."""
    enforce(cab)
    other = card(cab, assignee="@owner", status="To Do")
    record = card(cab, dependencies=[other.id])
    command, session = begin(cab, record)
    row = request(cab, record)["assist"]
    lead_finish(cab, record, assists=[row["id"]], cards=[other.id])
    end(cab, command, session)
    again, session = begin(cab, record, kind="message")
    end(cab, again, session)
    assert waits.of_command(cab.store, again) == [] and problem_kinds(cab, record) == []
    assert sorted((w["kind"], w["state"]) for w in waits.of_command(cab.store, command)) == [
        ("assist", "open"), ("card", "open")]


def test_a_turn_without_an_outcome_and_without_open_help_is_still_a_problem(cab):
    enforce(cab)
    record = card(cab)
    command, session = begin(cab, record)
    row = request(cab, record)["assist"]
    assign(cab, row["id"])
    reply(cab, row["id"])  # answered within the turn: nothing is open at its end
    end(cab, command, session)
    assert problem_kinds(cab, record) == ["missing_next_step"]
    assert waits.of_command(cab.store, command) == []


def test_review_and_done_refused_while_help_open(cab):
    record = card(cab)
    begin(cab, record)
    request(cab, record)
    assert "open help" in failing("refused", lead_finish, cab, record, outcome="review", note="готово")
    assert "open help" in failing("refused", lead_finish, cab, record, outcome="done", note="готово")
    # kd-task edit --status Review passes the same guards as the finish (KD-9479)
    assert "open help" in failing("refused", cab.call, BASE, {"op": "worker.edit", "agent": "dev",
                                                             "card": record.id, "status": "Review"})
    assert cab.store.card(record.id).status == "In Progress"


def test_reassign_cancels_help_and_drops_command(cab):
    record, row, command = waiting_on_help(cab)
    cab.store.edit_card(record.id, None, {"assignee": "@analyst"}, actor="user:owner")
    found = assists.row(cab.store, row["id"])
    assert found["state"] == "cancelled" and found["reason"] == "reassigned"
    [helper] = [c for c in cab.store.commands(card=record.id) if c["agent"] == "sysadmin"]
    from kd.cabinet import commands
    assert commands.stale_reason(cab.store, helper, lambda a: True) == commands.REASSIGNED
    assert continuations_of(cab, record) == []
    # the card moved on: the lead's wait ends quietly, BOSS is not asked to route it (final review M7)
    [wait] = [w for w in waits.of_command(cab.store, command) if w["kind"] == "assist"]
    assert wait["state"] == "cancelled"
    assert [r[0] for r in cab.store._conn.execute("SELECT kind FROM problems WHERE card=?", (record.id,))] == []


def test_scope_change_cancels_help(cab):
    record, row, command = waiting_on_help(cab)
    cab.store.edit_card(record.id, None, {"description": "новый объём"}, actor="user:owner")
    found = assists.row(cab.store, row["id"])
    assert found["state"] == "cancelled" and found["reason"] == "scope_changed"


def test_sweep_resignals_stalls_and_expires(cab):
    record, row, command = waiting_on_help(cab)
    other = request_open(cab)  # a second request nobody assigned
    for c in cab.store.commands(card=other["card"], states=["queued"]):
        if c["agent"] == "boss":  # BOSS had its turn and did not assign it
            session = cab.store.session_for(c["thread"], "boss")["id"]
            for state in ("delivered", "started", "done"):
                cab.store.command_state(c["id"], state, session=session)
    cab.now = "2026-10-14T10:11:00Z"
    outcomes.sweep(cab.ctx)
    boss = [c for c in cab.store.commands(card=other["card"]) if c["agent"] == "boss"]
    assert len(boss) == 2  # the request's signal and one again after 10 minutes
    outcomes.sweep(cab.ctx)
    assert len([c for c in cab.store.commands(card=other["card"]) if c["agent"] == "boss"]) == 2  # not twice
    show(cab, row["id"])
    cab.now = "2026-10-14T11:12:00Z"
    outcomes.sweep(cab.ctx)
    assert "assist_stalled" in problem_kinds(cab, record)
    cab.now = "2026-10-15T10:30:00Z"
    outcomes.sweep(cab.ctx)
    found = assists.row(cab.store, row["id"])
    assert found["state"] == "failed" and found["reason"] == "expired"
    [wait] = [w for w in waits.of_command(cab.store, command) if w["kind"] == "assist"]
    assert wait["state"] == "rejected"


def request_open(cab):
    record = card(cab)
    started(cab, record)
    return request(cab, record, key="other")["assist"]


def test_help_continuation_counts_in_the_pipeline_limits(cab):
    record, row, command = waiting_on_help(cab)
    for i in range(6):
        cab.store._conn.execute(
            "INSERT INTO continuations (key, card, agent, source, command, state, mark, reason, created)"
            " VALUES (?,?,?,?,?,'queued',?,'',?)", (f"old:{i}", record.id, "dev", "turn", f"c{i}", f"m{i}", cab.now))
    reply(cab, row["id"])
    [(state, reason, queued)] = [tuple(r) for r in cab.store._conn.execute(
        "SELECT state, reason, command FROM continuations WHERE key=?", (f"wait:{command}",))]
    assert (state, reason) == ("queued", "window")  # held to the window's end , not dropped
    assert cab.store.command(queued)["next_try"] > cab.now


# --- help in the snapshot and the card facts -----------------------------------------------
def help_wait(cab):
    enforce(cab)
    record = card(cab)
    command, session = begin(cab, record)
    row = request(cab, record)["assist"]
    lead_finish(cab, record, assists=[row["id"]])
    end(cab, command, session)
    return record, row


def snapshot_text(cab, record):
    return cab.call("digi-cabinet", {"op": "card.snapshot", "card": record.id, "format": "text"})["text"]


def test_snapshot_shows_open_help_and_its_step(cab):
    record, row = help_wait(cab)
    text = snapshot_text(cab, record)
    assert f"help {row['id'][:8]} requested @dev → @sysadmin platform_read: {STEP}" in text
    assert "next (recorded): wait_help @boss — " in text
    assign(cab, row["id"])
    text = snapshot_text(cab, record)
    assert "next (recorded): wait_help @sysadmin — " in text
    assert f"help {row['id'][:8]} assigned" in text


def test_card_facts_assists(cab):
    record, row = help_wait(cab)
    facts = outcomes.card_facts(cab.ctx, cab.store.card(record.id))
    [shown] = facts["assists"]
    assert shown["id"] == row["id"] and shown["role"] == "@sysadmin" and shown["state"] == "requested"
    assert shown["step"] == STEP and shown["kind"] == "platform_read" and shown["lead"] == "@dev"
    [wait] = [w for w in facts["waits"] if w["kind"] == "assist"]
    assert wait["assist"]["role"] == "@sysadmin" and wait["assist"]["state"] == "requested"


# --- final review findings ------------------------------------------------------------------------------------
def test_snapshot_keeps_the_ended_help_and_its_result(cab):
    """I1: after the help ends the lead and BOSS still read its state, reason and evidence in the snapshot."""
    record, row = help_wait(cab)
    assign(cab, row["id"])
    reply(cab, row["id"], state="failed", reason="capability_missing", evidence="нет чтения журнала службы")
    text = snapshot_text(cab, record)
    assert f"help {row['id'][:8]} failed capability_missing @dev → @sysadmin platform_read" in text
    assert "нет чтения журнала службы" in text


def test_assigned_without_an_answer_is_stalled(cab):
    """I2: a helper that never took the request (its session failed, its wake dropped) is chased like a taken one."""
    record, row, command = waiting_on_help(cab)
    cab.now = "2026-10-14T10:59:00Z"
    outcomes.sweep(cab.ctx)
    assert "assist_stalled" not in problem_kinds(cab, record)
    cab.now = "2026-10-14T11:01:00Z"
    outcomes.sweep(cab.ctx)
    assert "assist_stalled" in problem_kinds(cab, record)


def test_reassigned_help_drops_the_old_helper_wake(cab, tmp_path, monkeypatch):
    """I2: BOSS moves the help A → B → A: A's queued wake is dropped while B holds it and holds again for A."""
    catalog = tmp_path / "assist.json"
    catalog.write_text('{"schemaVersion": 1, "kinds": {"platform_read": {"roles": ["sysadmin", "analyst"]}}}',
                       encoding="utf-8")
    monkeypatch.setattr(assists, "CATALOG", catalog)
    from kd.cabinet import commands
    record, row = asked(cab)
    assign(cab, row["id"], to="sysadmin")
    [first] = signals(cab, record, "sysadmin")
    assign(cab, row["id"], to="analyst")
    assert commands.stale_reason(cab.store, cab.store.command(first["id"]), lambda a: True) == commands.ASSIST_MOVED
    [second] = signals(cab, record, "analyst")
    assert commands.stale_reason(cab.store, second, lambda a: True) is None
    assign(cab, row["id"], to="sysadmin")
    assert commands.stale_reason(cab.store, cab.store.command(second["id"]), lambda a: True) == commands.ASSIST_MOVED
    held = [c for c in signals(cab, record, "sysadmin")
            if commands.stale_reason(cab.store, c, lambda a: True) is None]
    assert len(held) == 1


def test_review_recorded_before_the_help_does_not_move_the_card(cab):
    """I3: finish --outcome review, then a help request in the same turn: at the turn's end the card stays in work."""
    enforce(cab)
    record = card(cab)
    command, session = begin(cab, record)
    lead_finish(cab, record, outcome="review", note="готово")
    row = request(cab, record)["assist"]
    end(cab, command, session)
    assert cab.store.card(record.id).status == "In Progress"
    assert assists.row(cab.store, row["id"])["state"] == "requested"
    assert "outcome_refused" in problem_kinds(cab, record)


def test_help_requests_count_in_the_agent_write_limit(cab, monkeypatch):
    """I4: a lead in a loop hits the per-agent write limit like any other board write."""
    record = card(cab)
    started(cab, record)
    request(cab, record)
    used = cab.store.count_events("agent:dev", "2026-10-14T09:00:00Z")
    monkeypatch.setattr(api, "AGENT_WRITES_PER_HOUR", used)
    assert "rate limit" in failing("refused", request, cab, record, key="another")


def test_a_late_reply_is_kept_once_with_its_own_event(cab):
    """I4: after a cancel one late answer is kept (an assist.late event behind it); a repeat changes nothing."""
    record, row = asked(cab)
    assign(cab, row["id"])
    cab.store.edit_card(record.id, None, {"status": "Review"}, actor="user:owner")  # withdrawn
    assert reply(cab, row["id"])["late"] is True
    again = reply(cab, row["id"])
    assert again["unchanged"] is True
    failing("refused", reply, cab, row["id"], evidence="другое")
    assert cab.store._conn.execute("SELECT COUNT(*) FROM messages WHERE thread=? AND kind='assist'",
                                   (record.thread,)).fetchone()[0] == 1
    assert cab.store._conn.execute("SELECT COUNT(*) FROM events WHERE kind='assist.late'").fetchone()[0] == 1


def test_a_reply_after_expiry_is_kept_as_late_evidence(cab):
    """M9: the helper answers after the request expired: the evidence stays in the thread, nothing resumes."""
    record, row = asked(cab)
    assign(cab, row["id"])
    cab.now = "2026-10-15T10:30:00Z"
    outcomes.sweep(cab.ctx)
    assert assists.row(cab.store, row["id"])["reason"] == "expired"
    out = reply(cab, row["id"])
    assert out["late"] is True and out["assist"]["state"] == "failed"


def test_server_change_is_asked_of_the_sysadmin_only(cab):
    """the one kind that is not a read — the sysadmin opens its own admin card with a plan made from the
    lead's rollout sheet. The request grants nothing: the plan waits for the owner's signature like any other."""
    assert assists.load_catalog()["server_change"] == frozenset({"sysadmin"})
    record = card(cab)
    started(cab, record)
    found = request(cab, record, kind="server_change", to="sysadmin", step="выкат 0.18 на staging: лист в PR #12")
    assert (found["assist"]["kind"], found["assist"]["state"]) == ("server_change", "requested")
    failing("refused", request, cab, record, kind="server_change", to="analyst", key="k2")
    failing("refused", request, cab, record, kind="server_change", to="dev", key="k3")  # its own role


# --- a rollout asked as help: the wait follows the helper's plan card (KD-9460) ---------------------------------
def plan_card(cab, **over):
    """The card the sysadmin opens for a rollout (``kd-task create``: its own, in Inbox)."""
    return card(cab, **{"assignee": "@sysadmin", "status": "Inbox", "project": "admin", "created_by": "@sysadmin",
                        **over})


def waiting_on_rollout(cab):
    """The lead waits for a server_change; BOSS gave it to the sysadmin, who took it and opened its own card."""
    enforce(cab)
    record = card(cab)
    command, session = begin(cab, record)
    row = request(cab, record, kind="server_change", step="выкат на staging: лист в заметках карточки",
                  key="rollout-staging")["assist"]
    lead_finish(cab, record, assists=[row["id"]])
    end(cab, command, session)
    assign(cab, row["id"])
    show(cab, row["id"])  # accepted
    plan = plan_card(cab)
    return record, row, command, plan


def plan_reply(cab, row, plan, **over):
    return reply(cab, row["id"], evidence=f"план на {plan.id}", limit="план предложен, ничего не выполнено",
                 card=plan.id, **over)


def test_a_rollout_wait_does_not_wake_the_lead_when_the_sysadmin_takes_it_or_opens_its_card(cab):
    record, row, command, plan = waiting_on_rollout(cab)
    assert continuations_of(cab, record) == []  # accepted: no wake
    out = plan_reply(cab, row, plan)
    assert out["assist"]["state"] == "done"
    assert continuations_of(cab, record) == []  # a plan awaiting the owner's signature is no outcome yet
    [wait] = waits.of_command(cab.store, command)
    assert (wait["kind"], wait["ref"], wait["state"], wait["blocking"]) == ("card", plan.id, "open", 1)
    lead = cab.store.card(record.id)
    assert lead.status == "Blocked" and plan.id in lead.dependencies  # what the lead did by hand before
    assert outcomes.next_state(cab.ctx, lead) == "waiting"
    kinds = [r[0] for r in cab.store._conn.execute("SELECT kind FROM events WHERE card=?", (record.id,))]
    assert "wait.followed" in kinds and "wait.resolved" not in kinds
    # the answer itself is in the lead's thread, as every help's is
    assert cab.store._conn.execute("SELECT COUNT(*) FROM messages WHERE thread=? AND kind='assist'",
                                   (record.thread,)).fetchone()[0] == 1


def test_a_rollout_wait_wakes_the_lead_once_when_the_plan_card_is_done(cab):
    record, row, command, plan = waiting_on_rollout(cab)
    plan_reply(cab, row, plan)
    cab.store.edit_card(plan.id, None, {"status": "Done"}, actor="user:owner")
    [cont] = continuations_of(cab, record)
    assert cont["key"] == f"wait:{command}" and cont["text"] == continuations.RESOLVED.format(id=record.id)


def test_a_rollout_whose_plan_card_is_cancelled_asks_boss_to_route_it(cab):
    """nothing is lost: a plan card closed without the work is a wait that needs routing, as any dependency's."""
    record, row, command, plan = waiting_on_rollout(cab)
    plan_reply(cab, row, plan)
    cab.store.edit_card(plan.id, None, {"status": "Cancelled", "outcome": "cancelled"}, actor="user:owner")
    assert continuations_of(cab, record) == []
    assert "wait_needs_routing" in problem_kinds(cab, record)


def test_a_rollout_refused_or_run_at_once_wakes_the_lead(cab):
    record, row, command, plan = waiting_on_rollout(cab)
    reply(cab, row["id"], state="failed", reason="capability_missing", evidence="нет инструмента в каталоге")
    [cont] = continuations_of(cab, record)
    assert cont["text"] == continuations.ENDED.format(id=record.id, state="rejected")
    other, again, command2, _ = waiting_on_rollout(cab)
    reply(cab, again["id"], evidence="KD-9: каждый шаг показал ok")  # a test stand: done in the helper's turn
    [cont] = continuations_of(cab, other)
    assert cont["text"] == continuations.RESOLVED.format(id=other.id)


def test_a_plan_card_answer_before_the_lead_ends_its_turn_follows_on_activate(cab):
    enforce(cab)
    record = card(cab)
    command, session = begin(cab, record)
    row = request(cab, record, kind="server_change", step="выкат на staging", key="rollout-staging")["assist"]
    lead_finish(cab, record, assists=[row["id"]])
    assign(cab, row["id"])
    plan = plan_card(cab)
    plan_reply(cab, row, plan)
    end(cab, command, session)
    [wait] = waits.of_command(cab.store, command)
    assert (wait["kind"], wait["ref"], wait["state"]) == ("card", plan.id, "open")
    assert continuations_of(cab, record) == []


def _answered_before_the_lead_finished(cab):
    """The sysadmin answers with its plan card while the lead's turn still runs: no wait of the lead yet."""
    enforce(cab)
    record = card(cab)
    command, session = begin(cab, record)
    row = request(cab, record, kind="server_change", step="выкат на staging", key="rollout-staging")["assist"]
    assign(cab, row["id"])
    plan = plan_card(cab)
    out = plan_reply(cab, row, plan)
    assert out["followed"] is False  # nothing moved: the lead had no wait on the help
    return record, row, command, session, plan


def test_a_finish_on_a_help_answered_with_a_plan_card_waits_for_that_card(cab):
    record, row, command, session, plan = _answered_before_the_lead_finished(cab)
    lead_finish(cab, record, assists=[row["id"]])
    end(cab, command, session)
    [wait] = waits.of_command(cab.store, command)
    assert (wait["kind"], wait["ref"], wait["state"]) == ("card", plan.id, "open")
    assert continuations_of(cab, record) == []
    cab.store.edit_card(plan.id, None, {"status": "Done"}, actor="user:owner")
    [cont] = continuations_of(cab, record)
    assert cont["text"] == continuations.RESOLVED.format(id=record.id)


def test_a_finish_on_a_help_whose_plan_card_is_done_resolves_at_once(cab):
    record, row, command, session, plan = _answered_before_the_lead_finished(cab)
    cab.store.edit_card(plan.id, None, {"status": "Done"}, actor="user:owner")
    lead_finish(cab, record, assists=[row["id"]])
    end(cab, command, session)
    [cont] = continuations_of(cab, record)
    assert cont["text"] == continuations.RESOLVED.format(id=record.id)


def test_a_reply_says_whether_the_leads_wait_moved(cab):
    record, row, command, plan = waiting_on_rollout(cab)
    assert plan_reply(cab, row, plan)["followed"] is True


def test_a_card_the_helper_opened_before_the_help_is_not_its_plan(cab):
    enforce(cab)
    old = plan_card(cab)  # a long-lived card of the sysadmin's
    cab.now = "2026-10-14T11:00:00Z"
    record = card(cab)
    command, session = begin(cab, record)
    row = request(cab, record, kind="server_change", step="выкат на staging", key="rollout-staging")["assist"]
    lead_finish(cab, record, assists=[row["id"]])
    end(cab, command, session)
    assign(cab, row["id"])
    failing("refused", plan_reply, cab, row, old)
    assert waits.of_command(cab.store, command)[0]["kind"] == "assist"


def test_a_plan_card_is_named_only_by_a_server_change_done_on_an_open_card(cab):
    record, row, command, plan = waiting_on_rollout(cab)
    failing("invalid", plan_reply, cab, row, plan, state="failed", reason="refused")
    failing("invalid", reply, cab, row["id"], card="KD-x")
    failing("not_found", reply, cab, row["id"], card="KD-99999")
    failing("refused", reply, cab, row["id"], card=record.id)  # the lead's own card
    closed = plan_card(cab, status="Done")
    failing("refused", reply, cab, row["id"], card=closed.id)
    # a mistyped number: a card that is not the helper's own plan of this help
    foreign = card(cab, assignee="@analyst", status="In Progress", project="admin")
    assert "you opened for this help" in failing("refused", reply, cab, row["id"], card=foreign.id)
    not_made = card(cab, assignee="@sysadmin", status="Inbox", project="admin")  # the owner's card, given to it
    failing("refused", reply, cab, row["id"], card=not_made.id)
    assert cab.store.card(record.id).status == "In Progress"  # the lead is not blocked on any of them
    read_record, read_row = asked(cab)  # a read kind opens no card
    assign(cab, read_row["id"])
    failing("refused", plan_reply, cab, read_row, plan)
    assert assists.row(cab.store, row["id"])["state"] == "accepted"  # nothing of the refused answers stayed
