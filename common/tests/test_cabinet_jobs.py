"""worker jobs live in the cabinet — submit, view, cancel, the host's reports, the outcome on the card."""

import json

import pytest
from cabinet_env import CabinetEnv

from kd import session_config
from kd.cabinet import api, jobs, users

HOSTED = session_config.HostConfig({"analyst": 1, "dev": 2, "boss": 1}, 600)
LIMITS = {"jobs_per_day": 12, "attempts_per_card_spec": 3}
M = "claude-sonnet-5-5"
USED = {"models": {M: {"in": 5, "out": 900, "cache_read": 1000, "cache_write": 200, "cost": 0.12}}, "requests": 4}


@pytest.fixture
def env(tmp_path):
    e = CabinetEnv(tmp_path)
    api.install_host(e.ctx, lambda: HOSTED)
    users.Users(e.store).create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    e.llm_dir = tmp_path / "llm"
    e.ctx.llm_env_dir = str(e.llm_dir)  # the host's provider key files: none unless a test writes one
    return e


def _job(card, n=1, **over):
    job = {
        "id": f"{n:032x}", "card": card, "agent": "dev", "type": "developer", "repo": "digipaddock", "spec": "implementer",
        "skills": ["tdd"], "prompt": {"spec": {"name": "implementer", "version": 1, "sha256": "0" * 64}, "skills": []},
        "limits": {"write": "repo", "capabilities": [], "network": "none"}, "brief": "Сделай по ТЗ.",
        "caller": "digi-boss", "requester": "boss", "writer": True, "timeout": 3600, "model": "sonnet",
    }
    job.update(over)
    return job


def _submit(env, job, limits=LIMITS, user="digi"):
    return env.call(user, {"op": "job.submit", "job": job, "limits": limits})


def _report(env, *items):
    return env.call("root", {"op": "host.report", "items": list(items)})


def _running(env, job_id, slot="dev/worker1"):
    _report(env, {"type": "job", "job": job_id, "state": "starting", "slot": slot})


def _view(env, job_id, caller="digi-boss"):
    return env.call("digi", {"op": "job.view", "job": job_id, "caller": caller})["job"]


def _kinds(env, card):
    return [e["kind"] for e in env.store.card_events(card) if e["kind"].startswith("job.")]


def _signals(env, agent="boss"):
    return [c for c in env.store.commands(limit=100) if c["agent"] == agent and c["kind"] == "signal"]


def _rows(env):
    return [dict(r) for r in env.store._conn.execute("SELECT * FROM usage_log").fetchall()]


def _texts(env, card):
    return [(m["author"], m["body"]) for m in env.store.thread_messages(env.store.card(card).thread, 0)
            if m["kind"] == "text"]


def test_submit_queues_a_job_and_an_event(env):
    card = env.card()
    out = _submit(env, _job(card.id))
    view = out["job"]
    assert (view["id"], view["card"], view["agent"], view["spec"], view["state"]) == (
        "0" * 31 + "1", card.id, "dev", "implementer", "queued")
    assert view["skills"] == ["tdd"] and view["write"] == "repo" and view["result"] is None
    assert _kinds(env, card.id) == ["job.queued"]


@pytest.mark.parametrize("bad", [
    {"id": "nothex"}, {"agent": "Dev!"}, {"writer": "yes"}, {"timeout": 5}, {"requester": "Boss"},
    {"brief": "x" * 50000}, {"extra": 1},
])
def test_submit_refuses_a_bad_definition(env, bad):
    card = env.card()
    with pytest.raises(api.ApiError) as err:
        _submit(env, _job(card.id, **bad))
    assert err.value.kind == "invalid"


# --- a job runs on the card's model and effort --------------------------------------------------------
OR = {"id": "or-x", "provider": "openrouter", "base_url": "", "upstream": "anthropic/claude-sonnet-4.5", "title": "T",
      "price_in": 2, "price_out": 10, "price_cache_read": 0.2, "price_cache_write": 2.5, "context": 200000}
SPEC = {"id": "or-x", "provider": "openrouter", "upstream": "anthropic/claude-sonnet-4.5", "base_url": ""}


def _definition_of(env, job_id):
    return jobs.row(env.store, job_id)["data"]["definition"]


def _on_model(env, key=True, **card_fields):
    env.store.create_llm_model(OR, actor="user:owner")
    if key:
        env.llm_dir.mkdir(exist_ok=True)
        (env.llm_dir / "openrouter.env").write_text("ANTHROPIC_AUTH_TOKEN=sk-or-test\n", encoding="utf-8")
    card = env.card()
    env.store.edit_card(card.id, None, {"model": "or-x", **card_fields}, actor="user:owner")
    return card


def test_submit_on_a_card_with_a_model_stores_its_spec_and_effort(env):
    card = _on_model(env, effort="high")
    job = _submit(env, _job(card.id))["job"]
    definition = _definition_of(env, job["id"])
    assert definition["model"] == "or-x" and definition["model_spec"] == SPEC and definition["effort"] == "high"


def test_submit_takes_the_projects_model_and_effort_when_the_card_has_none(env):
    env.store.create_llm_model(OR, actor="user:owner")
    env.llm_dir.mkdir(exist_ok=True)
    (env.llm_dir / "openrouter.env").write_text("ANTHROPIC_AUTH_TOKEN=sk-or-test\n", encoding="utf-8")
    card = env.card()
    env.store.update_project(card.project, actor="user:owner", model="or-x", effort="low")
    job = _submit(env, _job(card.id))["job"]
    definition = _definition_of(env, job["id"])
    assert definition["model_spec"] == SPEC and definition["effort"] == "low"


def test_submit_without_a_model_keeps_the_pools_model(env):
    card = env.card()
    job = _submit(env, _job(card.id))["job"]
    definition = _definition_of(env, job["id"])
    assert definition["model"] == "sonnet" and "model_spec" not in definition and definition["effort"] == ""


def test_submit_refuses_a_disabled_model(env):
    card = _on_model(env)
    env.store.update_llm_model("or-x", {"enabled": 0}, actor="user:owner")
    with pytest.raises(api.ApiError) as err:
        _submit(env, _job(card.id))
    assert err.value.kind == "refused" and err.value.reason == "model_disabled"
    assert jobs.row(env.store, _job(card.id)["id"]) is None


def test_submit_refuses_a_model_without_its_key_on_the_host(env):
    card = _on_model(env, key=False)
    with pytest.raises(api.ApiError) as err:
        _submit(env, _job(card.id))
    assert err.value.kind == "refused" and err.value.reason == "no_key"


def test_submit_refuses_an_agent_or_a_card_out_of_reach(env):
    with pytest.raises(api.ApiError) as err:
        _submit(env, _job("KD-404"))
    assert err.value.kind == "not_found"
    for user in ("digi-boss", "digi-dev"):
        with pytest.raises(api.ApiError):
            _submit(env, _job(env.card().id), user=user)


def test_a_second_writer_on_a_card_is_a_conflict(env):
    card = env.card()
    _submit(env, _job(card.id, 1))
    with pytest.raises(api.ApiError) as err:
        _submit(env, _job(card.id, 2))
    assert err.value.kind == "conflict"
    _submit(env, _job(card.id, 3, writer=False, spec="reviewer", limits={"write": "none", "capabilities": [],
                                                                         "network": "none"}))


def test_rate_and_retry_limits(env):
    first, second = env.card(), env.card()
    _submit(env, _job(first.id, 1, writer=False), limits={"jobs_per_day": 5, "attempts_per_card_spec": 1})
    with pytest.raises(api.ApiError) as err:
        _submit(env, _job(first.id, 2, writer=False), limits={"jobs_per_day": 5, "attempts_per_card_spec": 1})
    assert err.value.kind == "rate"
    _submit(env, _job(second.id, 3, writer=False), limits={"jobs_per_day": 2, "attempts_per_card_spec": 3})
    with pytest.raises(api.ApiError) as err:
        _submit(env, _job(second.id, 4, writer=False), limits={"jobs_per_day": 2, "attempts_per_card_spec": 3})
    assert err.value.kind == "rate"
    env.now = "2026-10-15T10:00:01Z"  # a day later the window is empty again
    _submit(env, _job(second.id, 5, writer=False), limits={"jobs_per_day": 2, "attempts_per_card_spec": 3})


def test_view_refuses_another_callers_job(env):
    card = env.card()
    job = _submit(env, _job(card.id))["job"]
    assert _view(env, job["id"])["state"] == "queued"
    assert _view(env, job["id"], caller="root")["id"] == job["id"]
    assert env.call("root", {"op": "job.view", "job": job["id"], "caller": "digi-x"})["job"]["id"] == job["id"]
    for job_id, caller in ((job["id"], "digi-other"), ("f" * 32, "digi-boss"), ("../x", "digi-boss")):
        with pytest.raises(api.ApiError) as err:
            _view(env, job_id, caller)
        assert err.value.kind in ("not_found", "invalid")


def test_cancel_of_a_queued_job_is_immediate(env):
    card = env.card()
    job = _submit(env, _job(card.id))["job"]
    out = env.call("digi", {"op": "job.cancel", "job": job["id"], "caller": "digi-boss", "reason": "не нужно"})
    assert out == {"ok": True, "state": "cancelled", "previous": "queued"}
    assert _view(env, job["id"])["reason"] == "не нужно"
    assert _kinds(env, card.id) == ["job.queued", "job.cancelled"]
    again = env.call("digi", {"op": "job.cancel", "job": job["id"], "caller": "digi-boss", "reason": "ещё раз"})
    assert again == {"ok": True, "state": "cancelled", "previous": "cancelled"}
    assert _signals(env) == [] and _texts(env, card.id) == []


def test_cancel_of_a_running_job_waits_for_the_host(env):
    card = env.card()
    job = _submit(env, _job(card.id))["job"]
    env.store._conn.execute("UPDATE jobs SET state='delivered' WHERE id=?", (job["id"],))
    _running(env, job["id"])
    assert _view(env, job["id"])["state"] == "running" and _view(env, job["id"])["slot"] == "dev/worker1"
    out = env.call("digi", {"op": "job.cancel", "job": job["id"], "caller": "digi-boss", "reason": "стоп"})
    assert out == {"ok": True, "state": "cancelling", "previous": "running"}
    _report(env, {"type": "job", "job": job["id"], "state": "cancelled", "reason": "stopped", "comment": ""})
    assert _view(env, job["id"])["state"] == "cancelled"
    [(author, body)] = _texts(env, card.id)
    assert author == "agent:dev" and body == "[dev] Сессия отменена."


def _ended(env, card, job_id, state="done", **extra):
    env.store._conn.execute("UPDATE jobs SET state='delivered' WHERE id=?", (job_id,))
    _running(env, job_id)
    item = {"type": "job", "job": job_id, "state": state, "reason": "", "comment": "Готово: 3 файла.",
            "usage": USED, "result": {"summary": "ok"}}
    item.update(extra)
    _report(env, item)


def test_done_report_comments_the_card_as_the_agent_wakes_the_requester_and_logs_usage(env):
    card = env.card()
    job = _submit(env, _job(card.id))["job"]
    _ended(env, card.id, job["id"])
    view = _view(env, job["id"])
    assert view["state"] == "done" and view["result"] == {"summary": "ok"} and view["ended"]
    assert view["usage"]["models"][M]["cost"] == pytest.approx(0.12)
    [(author, body)] = _texts(env, card.id)
    assert author == "agent:dev" and body == "[dev] Готово: 3 файла."
    [signal] = _signals(env)
    assert signal["thread"] == env.store.card(card.id).thread and signal["card"] == card.id
    assert signal["text"] == (f"[worker] job {job['id']} done card {card.id} spec implementer:"
                              f" run kd-worker result --job {job['id']}")
    [row] = _rows(env)
    assert (row["kind"], row["source"], row["agent"], row["card"], row["requests"]) == ("job", job["id"], "dev",
                                                                                        card.id, 4)
    assert _kinds(env, card.id) == ["job.queued", "job.started", "job.finished"]


def test_a_job_on_a_cabinet_model_is_priced_by_its_table(env):
    """a job whose definition carries a cabinet model's spec is costed by that model's prices; what the
    process reported is not money."""
    env.store.create_llm_model(
        {"id": "or-x", "provider": "openrouter", "base_url": "", "upstream": "anthropic/claude-sonnet-4.5",
         "title": "T", "price_in": 2, "price_out": 10, "price_cache_read": 0.2, "price_cache_write": 2.5,
         "context": 200000}, actor="user:owner")
    card = env.card()
    job = _submit(env, _job(card.id))["job"]
    data = env.store._conn.execute("SELECT data FROM jobs WHERE id=?", (job["id"],)).fetchone()[0]
    data = json.loads(data)
    data["definition"]["model_spec"] = {"id": "or-x", "provider": "openrouter",
                                        "upstream": "anthropic/claude-sonnet-4.5", "base_url": ""}
    env.store._conn.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(data), job["id"]))
    _ended(env, card.id, job["id"], usage={"models": {"or-x": {"in": 1_000_000, "out": 0, "cache_read": 0,
                                                                "cache_write": 0, "cost": 99}}, "requests": 2})
    [row] = _rows(env)
    assert row["model"] == "or-x" and row["cost"] == pytest.approx(2.0)


def test_cancelled_report_comments_without_waking(env):
    card = env.card()
    job = _submit(env, _job(card.id))["job"]
    _ended(env, card.id, job["id"], state="cancelled", comment="Частичный итог.")
    [(author, body)] = _texts(env, card.id)
    assert author == "agent:dev" and body == "[dev] Сессия отменена.\nЧастичный итог."
    assert _signals(env) == []
    assert len(_rows(env)) == 1


def test_a_root_job_wakes_nobody(env):
    card = env.card()
    job = _submit(env, _job(card.id, caller="root", requester=""), user="root")["job"]
    _ended(env, card.id, job["id"])
    assert _view(env, job["id"], caller="root")["state"] == "done"
    assert [c for c in env.store.commands(limit=100) if c["kind"] == "signal"] == []


def test_repeated_terminal_report_changes_nothing(env):
    card = env.card()
    job = _submit(env, _job(card.id))["job"]
    _ended(env, card.id, job["id"])
    before = _view(env, job["id"])
    _report(env, {"type": "job", "job": job["id"], "state": "failed", "reason": "late", "comment": "x",
                  "usage": USED, "result": None})
    assert _view(env, job["id"]) == before
    assert len(_texts(env, card.id)) == 1 and len(_signals(env)) == 1 and len(_rows(env)) == 1


def test_a_bad_job_report_is_refused(env):
    card = env.card()
    job = _submit(env, _job(card.id))["job"]
    for item in ({"type": "job", "job": job["id"], "state": "exploded"},
                 {"type": "job", "job": "f" * 32, "state": "done"},
                 {"type": "job", "job": job["id"], "state": "done", "usage": {"models": {}, "requests": -1}},
                 {"type": "job", "job": job["id"], "state": "starting", "slot": "../../etc"}):
        with pytest.raises(api.ApiError):
            _report(env, item)
    assert _view(env, job["id"])["state"] == "queued"


def test_a_cyrillic_result_is_measured_in_utf8_bytes_as_the_runner_does(env):
    """Final review: a block the runner kept (12 KiB of Cyrillic) is not refused for its 36 KiB of escapes."""
    card = env.card()
    job = _submit(env, _job(card.id))["job"]
    _running(env, job["id"])
    result = {"status": "done", "notes": "ж" * (6 * 1024)}
    _report(env, {"type": "job", "job": job["id"], "state": "done", "reason": "", "comment": "Готово.",
                  "usage": None, "result": result})
    assert _view(env, job["id"])["result"] == result
    big = _submit(env, _job(card.id, n=2))["job"]
    with pytest.raises(api.ApiError):
        _report(env, {"type": "job", "job": big["id"], "state": "done", "result": {"notes": "ж" * (jobs.RESULT_MAX // 2 + 512)}})


def test_host_restart_fails_inflight_jobs_and_requeues_the_rest(env):
    cards = [env.card() for _ in range(4)]
    running, cancelling, delivered, started_unreported = (
        _submit(env, _job(c.id, n))["job"]["id"] for n, c in enumerate(cards, 1))
    for job_id in (running, cancelling, delivered, started_unreported):
        env.store._conn.execute("UPDATE jobs SET state='delivered' WHERE id=?", (job_id,))
    _running(env, running)
    _running(env, cancelling, "dev/worker2")
    env.call("digi", {"op": "job.cancel", "job": cancelling, "caller": "digi-boss", "reason": "стоп"})
    _report(env, {"type": "host", "phase": "started", "inflight": [running, started_unreported]})
    assert _view(env, running)["state"] == "failed" and _view(env, running)["reason"] == "host_restart"
    assert _view(env, started_unreported)["state"] == "failed"
    assert _view(env, cancelling)["state"] == "cancelled"
    assert _view(env, delivered)["state"] == "queued"
    assert len(_signals(env)) == 2  # the two failed ones, once each
    assert all(_texts(env, c.id) for c in (cards[0], cards[3]))
    _report(env, {"type": "host", "phase": "started", "inflight": [running]})
    assert len(_signals(env)) == 2


def test_restart_texts_are_even(env):
    """Final review: a cancel that never ran posts nothing; a graceful stop of the host says what a crash says."""
    from kd.cabinet import jobs

    cards = [env.card() for _ in range(2)]
    never, stopped = (_submit(env, _job(c.id, n))["job"]["id"] for n, c in enumerate(cards, 1))
    env.store._conn.execute("UPDATE jobs SET state='delivered' WHERE id=?", (never,))
    env.call("digi", {"op": "job.cancel", "job": never, "caller": "digi-boss", "reason": "стоп"})
    _running(env, stopped)
    _report(env, {"type": "job", "job": stopped, "state": "failed", "reason": "host_restart", "comment": "",
                  "usage": None, "result": None})
    _report(env, {"type": "host", "phase": "started", "inflight": []})
    assert (_view(env, never)["state"], _view(env, never)["reason"]) == ("cancelled", "стоп")
    assert _texts(env, cards[0].id) == []
    assert any(jobs.RESTART_NOTE in body for _, body in _texts(env, cards[1].id))


def test_writer_is_free_after_the_job_ends(env):
    card = env.card()
    job = _submit(env, _job(card.id, 1))["job"]
    _ended(env, card.id, job["id"], state="failed", reason="exit 1", comment="Упала.")
    assert _submit(env, _job(card.id, 2))["job"]["state"] == "queued"


def test_job_report_leaves_the_cards_session_alone(env):
    card = env.card(status="Inbox")
    env.store.edit_card(card.id, None, {"status": "To Do"}, actor="user:owner")
    session = env.store.session_for(env.store.card(card.id).thread, "dev")
    env.store.session_update(session["id"], state="working")
    job = _submit(env, _job(card.id))["job"]
    _ended(env, card.id, job["id"])
    assert env.store.session(session["id"])["state"] == "working"
    assert [s["id"] for s in env.store.sessions(thread=env.store.card(card.id).thread)] == [session["id"]]


def test_list_shows_the_cards_jobs_to_a_person_not_to_an_agent(env):
    card = env.card()
    _submit(env, _job(card.id, 1, writer=False))
    env.now = "2026-10-14T10:01:00Z"
    _submit(env, _job(card.id, 2, writer=False))
    out = api.dispatch(env.ctx, api.Principal("user", -1, "owner"), {"op": "job.list", "card": card.id})
    assert [j["id"] for j in out["jobs"]] == [f"{2:032x}", f"{1:032x}"]
    assert "brief" not in out["jobs"][0] and "caller" not in out["jobs"][0]
    with pytest.raises(api.ApiError):
        env.call("digi-dev", {"op": "job.list", "card": card.id})


# --- a job on a model of the Claude subscription -------------------------------------------------------
OPUS = {"id": "claude:opus", "provider": "subscription", "upstream": "opus", "base_url": ""}


def test_a_job_of_a_card_on_a_subscription_model(env):
    """The definition carries the subscription model's spec; no key file is needed, and the usage is counted under
    the names the process reported, at their own cost."""
    card = env.card()
    env.store.edit_card(card.id, None, {"model": "claude:opus"}, actor="user:owner")
    job = _submit(env, _job(card.id))["job"]
    definition = _definition_of(env, job["id"])
    assert definition["model"] == "claude:opus" and definition["model_spec"] == OPUS
    _ended(env, card.id, job["id"])
    [row] = _rows(env)
    assert row["model"] == M and row["cost"] == pytest.approx(0.12)


def test_model_of_takes_an_override(env):
    """The council's critic runs on its own model, not the card's: ``model`` names it."""
    card = _on_model(env)
    job = _job(card.id)
    jobs._model_of(env.ctx, job, env.store.card(card.id), model="claude:haiku")
    assert job["model"] == "claude:haiku" and job["model_spec"]["upstream"] == "haiku"
    env.store.update_llm_model("or-x", {"enabled": 0}, actor="user:owner")
    plain = env.card()
    for ref, reason in (("or-x", "model_disabled"), ("nope", "model_unknown"), ("claude:nope", "model_unknown")):
        with pytest.raises(api.ApiError) as err:
            jobs._model_of(env.ctx, _job(plain.id), env.store.card(plain.id), model=ref)
        assert err.value.kind == "refused" and err.value.reason == reason
