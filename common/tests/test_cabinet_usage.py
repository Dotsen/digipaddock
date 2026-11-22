"""usage per turn and per job in the cabinet — from Claude's cumulative model usage, never counted twice."""

import pytest

from kd import session_config
from kd.cabinet import api, usage
from cabinet_env import CabinetEnv

M = "claude-sonnet-5-5"
FIRST = {M: {"in": 8, "out": 817, "cache_read": 102123, "cache_write": 36355, "cost": 0.174}}
SECOND = {M: {"in": 16, "out": 1217, "cache_read": 261904, "cache_write": 42382, "cost": 0.234}}
THIRD = {M: {"in": 24, "out": 2374, "cache_read": 394323, "cache_write": 88636, "cost": 0.457}}


def test_delta_of_a_resumed_session_counts_only_the_new_turn():
    d = usage.delta(FIRST, SECOND)
    assert {k: v for k, v in d[M].items() if k != "cost"} == {"in": 8, "out": 400, "cache_read": 159781,
                                                               "cache_write": 6027}
    assert d[M]["cost"] == pytest.approx(0.06)


def test_a_smaller_total_starts_a_new_chain():
    fresh = {M: {"in": 2, "out": 10, "cache_read": 5, "cache_write": 1, "cost": 0.3}}
    assert usage.delta(THIRD, fresh) == fresh


def test_a_new_model_in_total_counts_whole():
    haiku = {"claude-haiku-5-5": {"in": 1, "out": 2, "cache_read": 3, "cache_write": 4, "cost": 0.01}}
    d = usage.delta(FIRST, {**FIRST, **haiku})
    assert d["claude-haiku-5-5"] == haiku["claude-haiku-5-5"]
    assert all(v == 0 for v in d[M].values())


def test_a_renamed_chain_counts_only_what_the_new_name_added():
    """Final review (Important 3): a process resumed on another model reports the whole history under the new
    name — the previous names' totals are what it carried over, not new usage (either direction)."""
    switched = {"or-x": {"in": 24, "out": 1017, "cache_read": 102223, "cache_write": 36455, "cost": 0.2}}
    d = usage.delta(FIRST, switched)
    assert {k: v for k, v in d["or-x"].items() if k != "cost"} == {"in": 16, "out": 200, "cache_read": 100,
                                                                    "cache_write": 100}
    assert d["or-x"]["cost"] == pytest.approx(0.026)
    back = {M: {"in": 30, "out": 1020, "cache_read": 102233, "cache_write": 36460, "cost": 0.21}}
    d = usage.delta(switched, back)
    assert {k: v for k, v in d[M].items() if k != "cost"} == {"in": 6, "out": 3, "cache_read": 10, "cache_write": 5}
    less = {"or-x": {"in": 1, "out": 2, "cache_read": 3, "cache_write": 4, "cost": 0.01}}
    assert usage.delta(FIRST, less) == {"or-x": {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0, "cost": 0}}


@pytest.mark.parametrize("bad", [
    None, [], {"models": {}}, {"models": {"Bad Model": {}}, "requests": 1},
    {"models": {M: {"in": -1, "out": 0, "cache_read": 0, "cache_write": 0, "cost": 0}}, "requests": 1},
    {"models": {M: {"in": True, "out": 0, "cache_read": 0, "cache_write": 0, "cost": 0}}, "requests": 1},
    {"models": {f"m{i}": dict.fromkeys(usage.FIELDS, 0) for i in range(9)}, "requests": 1},
    {"models": {}, "requests": -2},
])
def test_check_refuses_bad_shapes(bad):
    with pytest.raises(ValueError):
        usage.check(bad)


HOSTED = session_config.HostConfig({"analyst": 1}, 600)


@pytest.fixture
def env(tmp_path):
    e = CabinetEnv(tmp_path)
    api.install_host(e.ctx, lambda: HOSTED)
    return e


def _turn(env):
    card = env.card(assignee="@analyst", status="Inbox")
    env.store.edit_card(card.id, None, {"status": "To Do"}, actor="user:owner")
    [command] = env.store.commands(card=card.id)
    session = env.store.session_for(command["thread"], "analyst")
    return card, command, session


def _rows(env):
    return [dict(r) for r in env.store._conn.execute("SELECT * FROM usage_log ORDER BY id").fetchall()]


def test_record_turn_writes_rows_and_session_totals(env):
    card, command, session = _turn(env)
    assert usage.record_turn(env.store, session, command, {"models": FIRST, "requests": 6}, "2026-10-18T09:00:00Z") == 1
    session = env.store.session(session["id"])
    usage.record_turn(env.store, session, command, {"models": SECOND, "requests": 4}, "2026-10-18T09:05:00Z")
    rows = _rows(env)
    assert [(r["kind"], r["source"], r["card"], r["agent"], r["model"], r["requests"]) for r in rows] == [
        ("turn", session["id"], card.id, "analyst", M, 6), ("turn", session["id"], card.id, "analyst", M, 4)]
    assert rows[1]["output"] == 400 and rows[1]["cost"] == pytest.approx(0.06)
    after = env.store.session(session["id"])
    assert after["usage_total"] == SECOND
    assert after["usage"] == {"in": 16, "out": 1217, "cache_read": 261904, "cache_write": 42382, "cost": 0.234}


def test_a_missed_report_is_counted_by_the_next_turn(env):
    _, command, session = _turn(env)
    usage.record_turn(env.store, session, command, {"models": FIRST, "requests": 1}, "2026-10-18T09:00:00Z")
    # SECOND's report never reached the cabinet
    usage.record_turn(env.store, env.store.session(session["id"]), command, {"models": THIRD, "requests": 1},
                      "2026-10-18T09:10:00Z")
    rows = _rows(env)
    assert sum(r["output"] for r in rows) == THIRD[M]["out"]
    assert sum(r["cost"] for r in rows) == pytest.approx(THIRD[M]["cost"])


def test_a_turn_with_nothing_new_writes_no_row(env):
    _, command, session = _turn(env)
    usage.record_turn(env.store, session, command, {"models": FIRST, "requests": 1}, "2026-10-18T09:00:00Z")
    assert usage.record_turn(env.store, env.store.session(session["id"]), command, {"models": FIRST, "requests": 0},
                             "2026-10-18T09:01:00Z") == 0


def test_a_session_from_before_the_ledger_takes_its_first_total_as_the_baseline(env):
    """Final review: a session resumed after the migration reports its whole history, already counted from files."""
    _, command, session = _turn(env)
    env.store._conn.execute("UPDATE sessions SET usage_total='null', usage='{\"cost\": 1.5}' WHERE id=?",
                            (session["id"],))
    legacy = env.store.session(session["id"])
    assert legacy["usage_total"] is None
    assert usage.record_turn(env.store, legacy, command, {"models": FIRST, "requests": 6}, "2026-10-18T09:00:00Z") == 0
    after = env.store.session(session["id"])
    assert after["usage_total"] == FIRST and after["usage"] == {"cost": 1.5} and _rows(env) == []
    usage.record_turn(env.store, after, command, {"models": SECOND, "requests": 4}, "2026-10-18T09:05:00Z")
    [row] = _rows(env)
    assert row["output"] == 400 and row["requests"] == 4


def test_turn_report_with_usage_writes_the_ledger(env):
    card, command, session = _turn(env)
    env.store.command_state(command["id"], "delivered", session=session["id"])
    env.call("root", {"op": "host.report", "items": [
        {"type": "turn", "session": session["id"], "command": command["id"], "phase": "started"},
        {"type": "turn", "session": session["id"], "command": command["id"], "phase": "ended", "ok": True,
         "usage": {"models": FIRST, "requests": 6}},
    ]})
    assert [(r["kind"], r["requests"]) for r in _rows(env)] == [("turn", 6)]
    assert env.store.session(session["id"])["usage"]["cost"] == pytest.approx(0.174)


def test_session_item_with_legacy_usage_is_accepted_and_ignored(env):
    _, command, session = _turn(env)
    env.call("root", {"op": "host.report", "items": [
        {"type": "session", "session": session["id"], "state": "idle", "usage": {"in": 10, "out": 5}}]})
    assert env.store.session(session["id"])["usage"] == {}
    assert _rows(env) == []


def test_record_job_writes_its_rows(env):
    job = {"id": "j" * 32, "agent": "dev", "card": "KD-7"}
    assert usage.record_job(env.store, job, {"models": FIRST, "requests": 3}, "2026-10-18T10:00:00Z") == 1
    [row] = _rows(env)
    assert (row["kind"], row["source"], row["agent"], row["card"], row["requests"]) == ("job", "j" * 32, "dev", "KD-7", 3)


# --- a cabinet model's usage is priced by its table, never by what the process reported ----------------
OR = {"id": "or-x", "provider": "openrouter", "base_url": "", "upstream": "anthropic/claude-sonnet-4.5", "title": "T",
      "price_in": 2, "price_out": 10, "price_cache_read": 0.2, "price_cache_write": 2.5, "context": 200000}
OR_FIRST = {"or-x": {"in": 1_000_000, "out": 100_000, "cache_read": 0, "cache_write": 0, "cost": 99}}
OR_SECOND = {"or-x": {"in": 2_000_000, "out": 100_000, "cache_read": 0, "cache_write": 0, "cost": 99}}


def test_priced_replaces_the_reported_cost_with_the_tables():
    used = {"or-x": {"in": 1_000_000, "out": 0, "cache_read": 0, "cache_write": 0, "cost": 0}}
    assert usage.priced(used, OR) == {"or-x": {"in": 1_000_000, "out": 0, "cache_read": 0, "cache_write": 0,
                                               "cost": 2.0}}
    assert used["or-x"]["cost"] == 0  # a copy


def test_record_turn_with_prices_costs_the_turns_share_by_the_table(env):
    card, command, session = _turn(env)
    assert usage.record_turn(env.store, session, command, {"models": OR_FIRST, "requests": 2}, "2026-10-18T09:00:00Z",
                             prices=OR) == 1
    [row] = _rows(env)
    assert row["model"] == "or-x" and row["cost"] == pytest.approx(3.0)  # 1M in at $2 + 100k out at $10
    assert env.store.session(session["id"])["usage"]["cost"] == pytest.approx(3.0)
    usage.record_turn(env.store, env.store.session(session["id"]), command, {"models": OR_SECOND, "requests": 1},
                      "2026-10-18T09:05:00Z", prices=OR)
    assert _rows(env)[1]["cost"] == pytest.approx(2.0)  # the new million of input only
    assert env.store.session(session["id"])["usage"]["cost"] == pytest.approx(5.0)
    assert env.store.session(session["id"])["usage_total"] == OR_SECOND  # the raw total stays the baseline


def test_record_job_with_prices_costs_by_the_table(env):
    job = {"id": "j" * 32, "agent": "dev", "card": "KD-7"}
    assert usage.record_job(env.store, job, {"models": OR_FIRST, "requests": 3}, "2026-10-18T10:00:00Z", prices=OR) == 1
    [row] = _rows(env)
    assert row["model"] == "or-x" and row["cost"] == pytest.approx(3.0)


def test_a_turn_report_of_a_session_on_a_cabinet_model_is_priced(env):
    env.store.create_llm_model(OR, actor="user:owner")
    card, command, session = _turn(env)
    env.store.command_state(command["id"], "delivered", session=session["id"])
    env.call("root", {"op": "host.report", "items": [
        {"type": "session", "session": session["id"], "state": "working", "model": "or-x", "effort": ""},
        {"type": "turn", "session": session["id"], "command": command["id"], "phase": "started"},
        {"type": "turn", "session": session["id"], "command": command["id"], "phase": "ended", "ok": True,
         "usage": {"models": OR_FIRST, "requests": 2}},
    ]})
    [row] = _rows(env)
    assert row["model"] == "or-x" and row["cost"] == pytest.approx(3.0)
    assert env.store.session(session["id"])["usage"]["cost"] == pytest.approx(3.0)


def test_usage_day_groups_by_agent_and_model_with_sessions_and_jobs(env):
    _, command, session = _turn(env)
    usage.record_turn(env.store, session, command, {"models": FIRST, "requests": 6}, "2026-10-17T21:30:00Z")
    usage.record_turn(env.store, env.store.session(session["id"]), command, {"models": SECOND, "requests": 4},
                      "2026-10-18T09:00:00Z")
    usage.record_job(env.store, {"id": "j" * 32, "agent": "analyst", "card": ""}, {"models": FIRST, "requests": 2},
                     "2026-10-18T10:00:00Z")
    out = env.call("root", {"op": "usage.day", "start": "2026-10-17T21:00:00Z", "end": "2026-10-18T21:00:00Z"})
    [row] = out["rows"]
    assert (row["agent"], row["model"], row["requests"], row["sessions"], row["jobs"]) == ("analyst", M, 12, 1, 1)
    assert row["output"] == FIRST[M]["out"] + 400 + FIRST[M]["out"]
    assert out["since"] == {"analyst": "2026-10-17T21:30:00Z"}
    later = env.call("root", {"op": "usage.day", "start": "2026-10-18T09:30:00Z", "end": "2026-10-18T21:00:00Z"})
    assert later["rows"][0]["sessions"] == 0 and later["rows"][0]["jobs"] == 1
    for user in ("digi", "digi-analyst"):
        with pytest.raises(api.ApiError):
            env.call(user, {"op": "usage.day", "start": "2026-10-17T21:00:00Z", "end": "2026-10-18T21:00:00Z"})
    with pytest.raises(api.ApiError):
        env.call("root", {"op": "usage.day", "start": "yesterday", "end": "2026-10-18T21:00:00Z"})


def test_usage_day_counts_sessions_once_across_models_and_names_the_claude_sessions_it_counted(env):
    """Final review: kd-usage sums an agent's models — the session count and the counted transcripts come apart."""
    _, command, session = _turn(env)
    env.store.session_update(session["id"], runtime_session_id="0b9c3f1e-1111-2222-3333-444455556666")
    other = {"claude-haiku-5-5": {"in": 1, "out": 2, "cache_read": 3, "cache_write": 4, "cost": 0.001}}
    usage.record_turn(env.store, env.store.session(session["id"]), command, {"models": {**FIRST, **other},
                                                                             "requests": 6}, "2026-10-18T09:00:00Z")
    usage.record_job(env.store, {"id": "j" * 32, "agent": "analyst", "card": ""}, {"models": FIRST, "requests": 2},
                     "2026-10-18T10:00:00Z")
    out = env.call("root", {"op": "usage.day", "start": "2026-10-17T21:00:00Z", "end": "2026-10-18T21:00:00Z"})
    assert len(out["rows"]) == 2
    assert out["agents"] == {"analyst": {"sessions": 1, "jobs": 1}}
    assert out["counted"] == {"analyst": ["0b9c3f1e-1111-2222-3333-444455556666"]}
    assert out["categories"] == []  # a card's turn is not the monitor's


def _signal(env, key, text, space="chat"):
    command = env.call("digi-analyst", {"op": "command.enqueue", "agent": "analyst", "text": text, "key": key,
                                       **({"space": space} if space != "chat" else {})})["command"]
    return command, env.store.session_for(command["thread"], "analyst")


def test_usage_day_shows_the_monitors_turns_apart_before_and_after_its_thread(env):
    """The monitor's turns — in "Monitor", and before it its wakes in the chat — are a category of
    their own, already inside the agent's rows; the chat's other turns and card turns are not."""
    before, s1 = _signal(env, "m-old", "[монитор] P-7 открыта")  # the chat, before the thread
    usage.record_turn(env.store, s1, before, {"models": FIRST, "requests": 6}, "2026-10-18T09:00:00Z")
    other, _ = _signal(env, "boss-1", "[boss] directive x")  # the chat, not the monitor
    usage.record_turn(env.store, env.store.session(s1["id"]), other, {"models": SECOND, "requests": 4},
                      "2026-10-18T09:10:00Z")
    after, s2 = _signal(env, "m-new", "[монитор] P-8 открыта", space="monitor")
    haiku = {"claude-haiku-5-5": {"in": 1, "out": 2, "cache_read": 3, "cache_write": 4, "cost": 0.01}}
    usage.record_turn(env.store, s2, after, {"models": haiku, "requests": 2}, "2026-10-18T10:00:00Z")
    _, command, card_session = _turn(env)
    usage.record_turn(env.store, card_session, command, {"models": FIRST, "requests": 1}, "2026-10-18T11:00:00Z")
    out = env.call("root", {"op": "usage.day", "start": "2026-10-17T21:00:00Z", "end": "2026-10-18T21:00:00Z"})
    found = {(r["agent"], r["category"], r["model"]): r for r in out["categories"]}
    assert set(found) == {("analyst", "monitor", M), ("analyst", "monitor", "claude-haiku-5-5")}
    assert found[("analyst", "monitor", M)]["turns"] == 1 and found[("analyst", "monitor", M)]["requests"] == 6
    assert found[("analyst", "monitor", M)]["cost"] == pytest.approx(FIRST[M]["cost"])
    assert found[("analyst", "monitor", "claude-haiku-5-5")]["cost"] == pytest.approx(0.01)
    total = sum(r["cost"] for r in out["rows"])
    assert total == pytest.approx(FIRST[M]["cost"] + (SECOND[M]["cost"] - FIRST[M]["cost"]) + 0.01 + FIRST[M]["cost"])


# --- the version a subscription model's alias served is kept, and its change is a model event -----------
SMALL = {"in": 5, "out": 40, "cache_read": 0, "cache_write": 0, "cost": 0.001}
BIG = {"in": 90, "out": 900, "cache_read": 0, "cache_write": 0, "cost": 0.2}


def _versions(env):
    return [e["payload"]["version"] for e in env.store.events_since(0, 1000)
            if e["kind"] == "llm_model.changed" and "version" in e["payload"]]


def _on(env, model):
    card, command, session = _turn(env)
    env.store.session_update(session["id"], model=model)
    return command, env.store.session(session["id"])


def test_a_turn_on_a_subscription_model_keeps_the_version_it_served(env):
    command, session = _on(env, "claude:opus")
    assert usage.version(env.store, "claude:opus") == ""
    total = {"claude-haiku-4-5": SMALL, "claude-opus-5-5": BIG}
    usage.record_turn(env.store, session, command, {"models": total, "requests": 2}, "2026-11-11T09:00:00Z")
    assert usage.version(env.store, "claude:opus") == "claude-opus-5-5"
    assert usage.version(env.store, "claude:haiku") == ""  # a service model of the turn is nobody's version
    assert _versions(env) == [{"from": "", "to": "claude-opus-5-5"}]
    more = {"claude-haiku-4-5": SMALL, "claude-opus-5-5": {**BIG, "out": 1800, "cost": 0.4}}
    usage.record_turn(env.store, env.store.session(session["id"]), command, {"models": more, "requests": 1},
                      "2026-11-11T09:05:00Z")
    assert len(_versions(env)) == 1  # the same version again is no event
    newer = {**more, "claude-opus-6-0": BIG}
    usage.record_turn(env.store, env.store.session(session["id"]), command, {"models": newer, "requests": 1},
                      "2026-11-11T09:10:00Z")
    assert usage.version(env.store, "claude:opus") == "claude-opus-6-0"
    assert _versions(env)[-1] == {"from": "claude-opus-5-5", "to": "claude-opus-6-0"}


def test_a_turn_without_the_aliases_model_keeps_nothing(env):
    command, session = _on(env, "claude:opus")
    usage.record_turn(env.store, session, command, {"models": {"claude-haiku-4-5": BIG}, "requests": 1},
                      "2026-11-11T09:00:00Z")
    assert usage.version(env.store, "claude:opus") == "" and _versions(env) == []


@pytest.mark.parametrize("model", ["", "or-x"])
def test_a_session_on_its_own_or_a_cabinet_model_keeps_no_version(env, model):
    if model:
        env.store.create_llm_model(OR, actor="user:owner")
    command, session = _on(env, model)
    usage.record_turn(env.store, session, command, {"models": {"claude-opus-5-5": BIG}, "requests": 1},
                      "2026-11-11T09:00:00Z")
    assert _versions(env) == [] and env.store.get_state(usage.VERSION_STATE + model) is None


def test_a_reference_gone_from_the_catalog_keeps_nothing(env):
    with env.store.transaction():
        usage.saw(env.store, "claude:gone", ["claude-gone-1-0"], "2026-11-11T09:00:00Z")
    assert usage.version(env.store, "claude:gone") == "" and _versions(env) == []


def test_a_job_on_a_subscription_model_keeps_the_version(env):
    job = {"id": "j" * 32, "agent": "analyst", "card": "KD-7"}
    usage.record_job(env.store, job, {"models": {"claude-sonnet-5-5": BIG}, "requests": 3}, "2026-11-11T10:00:00Z",
                     model="claude:sonnet")
    assert usage.version(env.store, "claude:sonnet") == "claude-sonnet-5-5"
    usage.record_job(env.store, {**job, "id": "k" * 32}, {"models": {"claude-opus-5-5": BIG}, "requests": 3},
                     "2026-11-11T10:05:00Z")  # a job that names no model
    assert usage.version(env.store, "claude:opus") == ""
