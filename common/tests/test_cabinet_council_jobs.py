"""a critic's job — one the cabinet starts itself, on the critic's model, with no requester to wake."""

from pathlib import Path

import pytest
from cabinet_env import CabinetEnv

from kd import council, session_config, specializations, worker_packs, worker_slots
from kd.cabinet import api, council_rounds, jobs, users

REPO = Path(__file__).resolve().parents[2]
HOSTED = session_config.HostConfig({"analyst": 1, "dev": 2, "boss": 1}, 600)
M = "claude-sonnet-5-5"
USED = {
    "models": {
        M: {"in": 5, "out": 900, "cache_read": 1000, "cache_write": 200, "cost": 0.12}
    },
    "requests": 4,
}
OR = {
    "id": "or-gpt5",
    "provider": "openrouter",
    "base_url": "",
    "upstream": "openai/gpt-5",
    "title": "GPT-5",
    "price_in": 2,
    "price_out": 10,
    "price_cache_read": 0.2,
    "price_cache_write": 2.5,
    "context": 200000,
}


@pytest.fixture
def env(tmp_path):
    e = CabinetEnv(tmp_path)
    api.install_host(e.ctx, lambda: HOSTED)
    users.Users(e.store).create("owner", "Owner", roles=[("owner", "*")], actor="root")
    e.llm_dir = tmp_path / "llm"
    e.ctx.llm_env_dir = str(e.llm_dir)
    return e


@pytest.fixture
def ends(monkeypatch):
    """The jobs whose end reached the council."""
    seen = []
    monkeypatch.setattr(
        council_rounds, "on_job_end", lambda ctx, job: seen.append(job["id"])
    )
    return seen


def _job(card, n=1, **over):
    job = {
        "id": f"{n:032x}",
        "card": card,
        "agent": "analyst",
        "type": "analyst",
        "repo": None,
        "spec": "brief-critic",
        "skills": [],
        "prompt": {
            "spec": "brief-critic",
            "version": 1,
            "sha256": "0" * 64,
            "skills": {},
        },
        "limits": {"write": "none", "capabilities": [], "network": "none"},
        "brief": "Round 1 of 3.",
        "caller": "cabinet",
        "requester": "",
        "writer": False,
        "timeout": 3600,
        "model": "sonnet",
    }
    job.update(over)
    return job


def _report(env, *items):
    return env.call("root", {"op": "host.report", "items": list(items)})


def _signals(env):
    return [c for c in env.store.commands(limit=100) if c["kind"] == "signal"]


def _texts(env, card):
    thread = env.store.card(card).thread
    return [m for m in env.store.thread_messages(thread, 0) if m["kind"] == "text"]


def test_a_cabinet_job_runs_on_the_critics_model(env):
    card = env.card(status="Inbox")
    env.store.edit_card(card.id, None, {"effort": "high"}, actor="user:owner")

    job = jobs.enqueue_own(env.ctx, _job(card.id), model="claude:sonnet")

    definition = job["data"]["definition"]
    assert (job["state"], job["caller"], job["requester"]) == ("queued", "cabinet", "")
    assert definition["model"] == "claude:sonnet"
    assert definition["model_spec"]["provider"] == "subscription"
    assert definition["effort"] == "high"
    events = [e for e in env.store.card_events(card.id) if e["kind"] == "job.queued"]
    assert [e["actor"] for e in events] == ["system"]


def test_a_disabled_keyless_or_unknown_critic_is_refused(env):
    card = env.card(status="Inbox")
    env.store.create_llm_model(OR, actor="user:owner")

    with pytest.raises(api.ApiError) as no_key:
        jobs.enqueue_own(env.ctx, _job(card.id), model="or-gpt5")
    env.store.update_llm_model("or-gpt5", {"enabled": False}, actor="user:owner")
    with pytest.raises(api.ApiError) as disabled:
        jobs.enqueue_own(env.ctx, _job(card.id), model="or-gpt5")
    with pytest.raises(api.ApiError) as unknown:
        jobs.enqueue_own(env.ctx, _job(card.id), model="nope")

    assert [e.value.reason for e in (no_key, disabled, unknown)] == [
        "no_key",
        "model_disabled",
        "model_unknown",
    ]
    assert jobs.row(env.store, _job(card.id)["id"]) is None


def test_cabinet_jobs_have_their_own_day_limit(env, monkeypatch):
    card = env.card(status="Inbox", assignee="@analyst")
    real = council.limits()
    monkeypatch.setattr(
        council, "limits", lambda path=None: {**real, "jobs_per_day": 2}
    )
    for n in (1, 2):
        jobs.enqueue_own(env.ctx, _job(card.id, n), model="claude:sonnet")

    with pytest.raises(api.ApiError) as err:
        jobs.enqueue_own(env.ctx, _job(card.id, 3), model="claude:sonnet")

    assert err.value.kind == "rate"
    # the coordinator's own limit does not count what the cabinet started
    mine = _job(card.id, 4, caller="digi-boss", requester="boss", spec="brief-review")
    out = env.call(
        "digi",
        {
            "op": "job.submit",
            "job": mine,
            "limits": {"jobs_per_day": 1, "attempts_per_card_spec": 1},
        },
    )
    assert out["job"]["state"] == "queued"


def test_a_cabinet_job_ends_without_a_comment_or_a_wake(env, ends):
    card = env.card(status="Inbox")
    job = jobs.enqueue_own(env.ctx, _job(card.id), model="claude:sonnet")

    _report(
        env,
        {
            "type": "job",
            "job": job["id"],
            "state": "starting",
            "slot": "analyst/worker1",
        },
        {
            "type": "job",
            "job": job["id"],
            "state": "done",
            "comment": "Read the brief.",
            "usage": USED,
            "result": {"status": "done", "verdict": "ready", "findings": []},
        },
    )

    done = jobs.row(env.store, job["id"])
    assert done["state"] == "done" and done["data"]["result"]["verdict"] == "ready"
    assert _texts(env, card.id) == [] and _signals(env) == []
    (used,) = env.store._conn.execute("SELECT COUNT(*) FROM usage_log").fetchone()
    assert used == 1
    assert ends == [job["id"]]


def test_a_started_cabinet_job_tells_the_council(env, monkeypatch):
    card = env.card(status="Inbox")
    job = jobs.enqueue_own(env.ctx, _job(card.id), model="claude:sonnet")
    started = []
    monkeypatch.setattr(
        council_rounds, "on_job_started", lambda ctx, job: started.append(job["state"])
    )

    _report(
        env,
        {
            "type": "job",
            "job": job["id"],
            "state": "starting",
            "slot": "analyst/worker1",
        },
    )

    assert started == ["running"]


def test_a_cabinet_job_outlives_a_reassignment(env):
    card = env.card(status="Inbox", assignee="@dev")
    job = jobs.enqueue_own(env.ctx, _job(card.id), model="claude:sonnet")

    assert (
        jobs.stale_reason(env.store, job) is None
    )  # the analyst's job on the developer's card
    env.store.edit_card(card.id, None, {"status": "Done"}, actor="user:owner")

    assert jobs.stale_reason(env.store, job) == "card_done"


def test_cancel_own(env, ends):
    card = env.card(status="Inbox")
    queued = jobs.enqueue_own(env.ctx, _job(card.id, 1), model="claude:sonnet")
    running = jobs.enqueue_own(env.ctx, _job(card.id, 2), model="claude:sonnet")
    _report(
        env,
        {
            "type": "job",
            "job": running["id"],
            "state": "starting",
            "slot": "analyst/worker1",
        },
    )

    assert jobs.cancel_own(env.ctx, queued["id"], "superseded") == "cancelled"
    assert jobs.cancel_own(env.ctx, running["id"], "superseded") == "cancelling"
    assert jobs.cancel_own(env.ctx, queued["id"], "again") is None

    assert jobs.row(env.store, queued["id"])["reason"] == "superseded"
    assert ends == [queued["id"]]
    _report(env, {"type": "job", "job": running["id"], "state": "cancelled"})
    assert ends == [queued["id"], running["id"]]
    assert _texts(env, card.id) == []  # not even «the session was cancelled»


def test_nobody_submits_a_job_as_the_cabinet(env):
    card = env.card(status="Inbox", assignee="@analyst")

    with pytest.raises(api.ApiError) as err:
        env.call(
            "digi",
            {
                "op": "job.submit",
                "job": _job(card.id),
                "limits": {"jobs_per_day": 5, "attempts_per_card_spec": 5},
            },
        )

    assert err.value.kind == "invalid"


def test_the_critic_is_told_to_read_files_not_the_board():
    """A worker has no channel: a text that sends it to kd-task or kd-work sends it nowhere."""
    registry = specializations.load_registry(
        REPO / "common/specializations", require_root_owner=False
    )
    text = registry.specs["brief-critic"].text

    assert "kd-task" not in text and "kd-work" not in text and "kd-repo" not in text
    assert "`inputs/brief.md`" in text and "`inputs/council.md`" in text


def test_the_critic_is_told_what_its_block_may_weigh():
    """A block over the limit is not kept, and no finding of it: the text names the limit the three hops hold."""
    registry = specializations.load_registry(
        REPO / "common/specializations", require_root_owner=False
    )

    assert f"{jobs.RESULT_MAX // 1024} KiB" in registry.specs["brief-critic"].text


def test_the_registry_has_brief_critic():
    registry = specializations.load_registry(
        REPO / "common" / "specializations", require_root_owner=False
    )
    spec = registry.specs["brief-critic"]

    assert (spec.type, spec.version, spec.default) == ("analyst", 1, False)
    assert tuple(spec.result) == ("verdict", "findings", "questions", "missing_inputs")
    assert tuple(spec.requires) == ("repo.read",)
    prompt = worker_packs.build_prompt(
        registry,
        "brief-critic",
        (),
        card="KD-1",
        brief="Round 1 of 3.",
        agent_type="analyst",
    )
    assert prompt.provenance()["spec"] == "brief-critic"
    config = worker_slots.load_config(REPO / "common" / "worker" / "worker-slots.json")
    assert config.agents == ("dev", "analyst")


# --- what a critic reads travels with its job (a worker has no board) ----------------------------------------
INPUTS = {"brief.md": "# KD-1\n\nthe brief\n", "thread.md": "#1 @owner text: go\n"}
DONE = {
    "type": "job",
    "state": "done",
    "comment": "",
    "usage": USED,
    "result": {"status": "done"},
}


def test_a_cabinet_job_carries_its_inputs_to_the_host_and_drops_them_at_its_end(
    env, ends
):
    card = env.card(status="Inbox")

    job = jobs.enqueue_own(env.ctx, _job(card.id), model="claude:sonnet", inputs=INPUTS)

    assert job["data"]["inputs"] == INPUTS and "inputs" not in job["data"]["definition"]
    assert "the brief" not in str(jobs.view(job))
    request = jobs.prepare(env.ctx, job["id"], "")
    assert request["definition"]["inputs"] == INPUTS
    assert request["definition"]["id"] == job["id"] and request["agent"] == "analyst"
    _report(env, {**DONE, "job": job["id"]})
    done = jobs.row(env.store, job["id"])
    assert done["state"] == "done" and "inputs" not in done["data"]


def test_a_cancelled_cabinet_job_drops_its_inputs(env, ends):
    card = env.card(status="Inbox")
    job = jobs.enqueue_own(env.ctx, _job(card.id), model="claude:sonnet", inputs=INPUTS)

    assert jobs.cancel_own(env.ctx, job["id"], "owner") == "cancelled"

    assert "inputs" not in jobs.row(env.store, job["id"])["data"]


def test_a_job_without_inputs_goes_to_the_host_as_it_is(env):
    card = env.card(status="Inbox")
    job = jobs.enqueue_own(env.ctx, _job(card.id), model="claude:sonnet")

    assert "inputs" not in job["data"]
    assert "inputs" not in jobs.prepare(env.ctx, job["id"], "")["definition"]


@pytest.mark.parametrize(
    "inputs", [{}, {"brief.txt": "x"}, {"brief.md": ""}, {"brief.md": "x" * 300_000}]
)
def test_bad_inputs_refuse_a_cabinet_job(env, inputs):
    card = env.card(status="Inbox")

    with pytest.raises(api.ApiError) as bad:
        jobs.enqueue_own(env.ctx, _job(card.id), model="claude:sonnet", inputs=inputs)

    assert bad.value.kind == "invalid"
    assert jobs.row(env.store, _job(card.id)["id"]) is None


# --- a critic of the code reads a pull request's head ---------------------------------------------
PULL = {"number": 7, "head": "a" * 40, "target": "main"}


def _reader(card, n=1, **over):
    """A developer job that writes nothing: what a critic of the code is."""
    return _job(
        card, n, agent="dev", type="developer", repo="web", spec="reviewer", **over
    )


def test_a_pull_is_kept_in_the_definition_and_sent_to_the_host(env, ends):
    card = env.card(status="In Progress")

    job = jobs.enqueue_own(env.ctx, _reader(card.id), model="claude:sonnet", pull=PULL)

    assert job["data"]["definition"]["pull"] == PULL
    request = jobs.prepare(env.ctx, job["id"], "")
    assert request["definition"]["pull"] == PULL and request["agent"] == "dev"
    _report(env, {**DONE, "job": job["id"]})
    assert (
        jobs.row(env.store, job["id"])["data"]["definition"]["pull"] == PULL
    )  # the basis stays with the job


def test_a_job_without_a_pull_has_none_in_its_definition(env):
    card = env.card(status="In Progress")

    job = jobs.enqueue_own(env.ctx, _reader(card.id), model="claude:sonnet")

    assert "pull" not in job["data"]["definition"]


@pytest.mark.parametrize(
    "pull", ["7", {**PULL, "head": "a" * 39}, {**PULL, "target": "a..b"}, {}]
)
def test_a_bad_pull_refuses_a_cabinet_job(env, pull):
    card = env.card(status="In Progress")

    with pytest.raises(api.ApiError) as bad:
        jobs.enqueue_own(env.ctx, _reader(card.id), model="claude:sonnet", pull=pull)

    assert bad.value.kind == "invalid" and "pull" in str(bad.value)


def test_only_a_developer_job_that_writes_nothing_reads_a_pull(env):
    card = env.card(status="In Progress")

    with pytest.raises(
        api.ApiError
    ) as bad:  # the analyst's critic has no repository of its own
        jobs.enqueue_own(env.ctx, _job(card.id), model="claude:sonnet", pull=PULL)

    assert bad.value.kind == "invalid" and "writes nothing" in str(bad.value)


def test_head_missing_is_the_reason_a_council_waits_on():
    assert jobs.HEAD_MISSING == "head_missing"


# --- the critic of the code ---------------------------------------------------------------------------
def _registry():
    return specializations.load_registry(
        REPO / "common/specializations", require_root_owner=False
    )


def test_the_registry_has_code_critic():
    registry = _registry()
    spec = registry.specs["code-critic"]

    assert (spec.type, spec.version, spec.default) == ("developer", 3, False)
    assert tuple(spec.result) == ("verdict", "findings", "questions")
    assert tuple(spec.requires) == ("git.develop",)
    assert (spec.limits.write, spec.limits.capabilities, spec.limits.network) == (
        "none",
        (),
        "none",
    )
    prompt = worker_packs.build_prompt(
        registry,
        "code-critic",
        (),
        card="KD-1",
        brief="Round 1 of 3.",
        agent_type="developer",
    )
    assert prompt.provenance()["spec"] == "code-critic"


def test_code_critic_text_names_no_board_command():
    """A worker has no channel: a text that sends it to the board or to the forge sends it nowhere."""
    text = _registry().specs["code-critic"].text

    for command in ("kd-task", "kd-work", "kd-forge", "kd-pr", "kd-repo"):
        assert command not in text
    assert "`inputs/card.md`" in text and "`inputs/council.md`" in text
    assert "`git diff pr-base...pr-head`" in text and "`repo/`" in text
    assert "`ready`, `with_fixes` or `not_ready`" in text
    for field in (
        "`severity`",
        "`critical`",
        "`major`",
        "`minor`",
        "`location`",
        "`evidence`",
        "`scenario`",
        "`recommendation`",
    ):
        assert field in text


def test_the_code_critic_is_told_what_its_block_may_weigh():
    assert f"{jobs.RESULT_MAX // 1024} KiB" in _registry().specs["code-critic"].text


def _code_council(card_id, round_no=1):
    """A council of the code as its row reads: the basis is the pull request at its head."""
    return {
        "id": "c" * 32,
        "card": card_id,
        "kind": "code",
        "round": round_no,
        "rounds_max": 3,
        "critics": ["claude:opus", "claude:sonnet"],
        "basis": {
            "repo": "acme/web",
            "project": "web",
            "number": 7,
            "head": "a" * 40,
            "target": "main",
            "rounds": [],
        },
    }


def test_a_code_critics_definition_names_the_pull(tmp_path):
    import council_env as ce

    env = ce.make(tmp_path)
    card = env.card(status="In Progress", assignee="@dev", project="web")
    found = _code_council(card.id)

    plan = council_rounds._plan(env.ctx, "code")
    definition = council_rounds._definition(
        env.ctx, plan, found, env.store.card(card.id), 2, [], ("card.md",)
    )

    assert (
        definition["agent"],
        definition["type"],
        definition["repo"],
        definition["spec"],
    ) == ("dev", "developer", "web", "code-critic")
    assert definition["limits"]["write"] == "none" and definition["writer"] is False
    assert (definition["caller"], definition["requester"]) == ("cabinet", "")
    assert council_rounds._pull(found) == {
        "number": 7,
        "head": "a" * 40,
        "target": "main",
    }
    lines = definition["brief"].splitlines()
    assert lines[0] == f"council {'c' * 32} · code · round 1 of 3 · critic 2 of 2"
    assert lines[1] == f"card {card.id}: pull request web#7, head {'a' * 12} into main"
    assert "repo/" in lines[2] and "pr-head" in lines[2] and "pr-base" in lines[2]
    assert "inputs/card.md" in lines[2] and "inputs/council.md" not in lines[2]
    job = jobs.enqueue_own(
        env.ctx,
        definition,
        model="claude:sonnet",
        inputs={"card.md": "the card\n"},
        pull=council_rounds._pull(found),
    )
    assert (
        job["agent"] == "dev" and job["data"]["definition"]["pull"]["head"] == "a" * 40
    )


def test_a_brief_critics_job_is_on_no_pull():
    brief = {
        "kind": "brief",
        "basis": {"version": 2, "scope_sha": "0" * 64, "rounds": []},
    }

    assert council_rounds._pull(brief) is None


def test_a_code_critic_is_given_the_card(tmp_path):
    import council_env as ce
    from kd.cabinet import council_inputs

    env = ce.make(tmp_path)
    card = env.card(
        status="In Progress",
        assignee="@dev",
        project="web",
        description="Goal: a faster report.",
    )
    env.store.edit_card(
        card.id,
        None,
        {"acceptance": [{"text": "The report opens in a second", "checked": False}]},
        actor="agent:analyst",
    )
    record = env.store.card(card.id)

    files = council_inputs.build(env.store, _code_council(card.id), record)

    assert set(files) == {"card.md"}
    assert (
        files["card.md"].startswith(f"# {card.id}")
        and council_inputs.DATA in files["card.md"]
    )
    assert (
        "Goal: a faster report." in files["card.md"]
        and "The report opens in a second" in files["card.md"]
    )


def test_a_rounds_basis_in_words():
    from kd.cabinet import councils

    assert councils.basis_label("brief", {"version": 3, "scope_sha": "0" * 64}) == "v3"
    assert (
        councils.basis_label("code", {"head": "ab" * 20, "target": "main"})
        == "head " + "ab" * 6
    )
