"""the council as people and agents read it — the snapshot's round line and next step, the block's
operations (``card.councils``, ``council.cancel``), the rounds in the brief tab, the overview, the web routes."""

import council_env as ce
import pytest
from web_env import WebEnv

from kd.cabinet import api, council_rounds, councils, jobs, snapshot


@pytest.fixture
def env(tmp_path):
    env = ce.make(tmp_path)
    env.users.create("anna", "Anna", roles=[("partner", ["digipaddock"])], actor="root")
    return env


def _as(env, user, request):
    return api.dispatch(env.ctx, api.Principal("user", -1, user), request)


def _snap(env, card_id):
    return env.call(
        "digi-cabinet", {"op": "card.snapshot", "card": card_id, "format": "text"}
    )["text"]


def _next(env, card_id):
    return snapshot.next_step(snapshot.facts(env.ctx, env.store.card(card_id)))


def _reading(env):
    card = ce.draft(env)
    ce.handin(env, card.id)
    return card.id, ce.council_of(env, card.id)["id"]


def _needs_writer(env):
    card_id, council_id = _reading(env)
    ce.play(
        env,
        card_id,
        ce.block("needs_changes", [ce.finding(), ce.finding("minor", 2)]),
        ce.block(),
    )
    return card_id, council_id


# --- the snapshot ------------------------------------------------------------------------------------------------
def test_the_snapshot_names_the_round_and_how_to_read_it(env):
    card_id, _ = _reading(env)

    text = _snap(env, card_id)

    lines = text.splitlines()
    at = lines.index("council round: brief 1/3 running, open findings 0")
    assert lines[at - 1].startswith("council: ")  # after the composition
    assert f"council kd-work council {card_id}" in lines[-1]
    ce.play(env, card_id, None, None)
    assert "council round: brief 1/3 failed (no_critic), open findings 0" in _snap(
        env, card_id
    )


def test_a_card_without_a_council_has_no_round_line(env):
    plain = env.card()

    text = _snap(env, plain.id)

    assert "council round" not in text and "kd-work council" not in text


def test_the_round_line_counts_the_findings_left(env):
    card_id, _ = _needs_writer(env)

    assert "council round: brief 1/3 needs_writer, open findings 1" in _snap(
        env, card_id
    )


# --- the next step -----------------------------------------------------------------------------------------------
def test_next_step_council_running_and_writer(env):
    card_id, _ = _reading(env)

    step = _next(env, card_id)
    assert (step["what"], step["who"]) == ("council_running", "-")
    assert step["params"] == {"round": 1, "rounds": 3, "done": 0, "total": 2}
    assert (
        step["text"] == "the council reads the brief: round 1 of 3, 0 of 2 critics done"
    )

    ce.play(
        env,
        card_id,
        ce.block("needs_changes", [ce.finding(), ce.finding("minor", 2)]),
        ce.block(),
    )
    step = _next(env, card_id)
    assert (step["what"], step["who"]) == ("council_writer", "@analyst")
    assert step["params"] == {"round": 1, "rounds": 3, "findings": 1}
    assert (
        step["text"]
        == "@analyst answers the council: round 1 of 3, 1 findings unanswered"
    )


def test_the_brief_is_ready_again_after_a_clean_council(env):
    card_id, _ = _reading(env)
    ce.play(env, card_id, ce.block(), ce.block())

    assert _next(env, card_id)["what"] == "brief_ready"


def test_an_open_question_goes_before_the_council_step(env):
    card_id, _ = _needs_writer(env)
    env.call(
        "digi",
        {
            "op": "decision.open",
            "type": "question",
            "agent": "analyst",
            "card": card_id,
            "payload": {"text": "Which timeout?"},
        },
    )

    assert _next(env, card_id)["what"] == "wait_decision"


def test_the_page_reads_the_same_step(env):
    card_id, _ = _reading(env)

    found = _as(env, "owner", {"op": "card.get", "card": card_id})

    assert found["next"]["what"] == "council_running"


# --- the block ---------------------------------------------------------------------------------------------------
def test_card_councils_shows_rounds_findings_and_answers(env):
    card_id, council_id = _needs_writer(env)
    ce.started(env, card_id)
    ce.reply(env, card_id, {"r1.f1": "rejected"}, "r1.f1: by design")

    out = _as(env, "owner", {"op": "card.councils", "card": card_id})

    assert out["can_manage"] is True
    (seen,) = out["councils"]
    assert (seen["id"], seen["kind"], seen["state"], seen["end_reason"]) == (
        council_id,
        "brief",
        "needs_writer",
        "",
    )
    assert (seen["round"], seen["rounds"], seen["basis"]["version"]) == (1, 3, 1)
    (first,) = seen["rounds_list"]
    assert (first["round"], first["version"], first["state"]) == (1, 1, "ended")
    opus, gpt = first["runs"]
    assert (opus["n"], opus["model"], opus["state"], opus["verdict"]) == (
        1,
        "claude:opus",
        "done",
        "needs_changes",
    )
    assert opus["counts"] == {"critical": 0, "major": 1, "minor": 1}
    assert (opus["same_model"], gpt["same_model"]) == (True, False)
    assert opus["cost"] == 0.12 and opus["fail_reason"] == ""
    major, minor = opus["findings"]
    assert (major["id"], major["answer"], major["note"]) == (
        "r1.f1",
        "rejected",
        "by design",
    )
    assert (minor["id"], minor.get("answer")) == ("r1.f2", None)
    assert (gpt["verdict"], gpt["findings"], gpt["questions"]) == ("ready", [], [])


def test_card_councils_lists_the_newest_first_and_hides_other_projects(env):
    card_id, first = _reading(env)
    ce.play(env, card_id, None, None)
    ce.handin(env, card_id, description="Goal: a report, with a timeout.")
    second = ce.council_of(env, card_id)["id"]

    out = _as(env, "anna", {"op": "card.councils", "card": card_id})

    assert [c["id"] for c in out["councils"]] == [second, first]
    assert out["can_manage"] is False
    assert out["councils"][1]["rounds_list"][0]["runs"][0]["fail_reason"] == (
        "no final block"
    )
    hidden = ce.draft(env, project="admin")
    with pytest.raises(api.ApiError) as err:
        _as(env, "anna", {"op": "card.councils", "card": hidden.id})
    assert err.value.kind == "not_found"


def test_a_card_without_councils_has_an_empty_block(env):
    plain = env.card()

    out = _as(env, "owner", {"op": "card.councils", "card": plain.id})

    assert (out["councils"], out["can_manage"]) == ([], True)


def test_cancel_needs_settings(env):
    card_id, council_id = _reading(env)
    running = [r["job"] for r in ce.runs_of(env, card_id)]

    with pytest.raises(api.ApiError) as err:
        _as(env, "anna", {"op": "council.cancel", "council": council_id})
    assert err.value.kind == "refused"
    assert ce.council_of(env, card_id)["state"] == "running"

    out = _as(env, "owner", {"op": "council.cancel", "council": council_id})

    assert (out["council"]["state"], out["council"]["end_reason"]) == (
        "cancelled",
        "owner",
    )
    assert all(jobs.row(env.store, job)["state"] == "cancelled" for job in running)
    ended = [e for e in env.store.card_events(card_id) if e["kind"] == "council.ended"]
    assert ended[-1]["actor"] == "user:owner"
    assert ce.wakes(env, "analyst") == []


def test_cancel_refuses_what_is_not_an_open_council(env):
    card_id, council_id = _reading(env)
    ce.play(env, card_id, ce.block(), ce.block())
    other = env.card()

    for request, kind in (
        ({"council": council_id}, "refused"),  # ended already
        ({"council": "0" * 32}, "not_found"),
        ({"council": "nope"}, "not_found"),
        ({"council": council_id, "card": other.id}, "not_found"),  # of another card
    ):
        with pytest.raises(api.ApiError) as err:
            _as(env, "owner", {"op": "council.cancel", **request})
        assert err.value.kind == kind, request


# --- the brief tab and the overview ------------------------------------------------------------------------------
def test_brief_view_names_rounds_per_version(env):
    card_id, _ = _needs_writer(env)
    ce.handin(env, card_id, description="Goal: a report, with a timeout.")

    out = _as(env, "owner", {"op": "card.brief", "card": card_id})

    first, second = out["versions"]
    assert (first["council"]["round"], first["council"]["state"]) == (1, "ended")
    assert [
        (r["model"], r["state"], r["verdict"]) for r in first["council"]["runs"]
    ] == [
        ("claude:opus", "done", "needs_changes"),
        ("or-gpt5", "done", "ready"),
    ]
    assert (second["council"]["round"], second["council"]["state"]) == (2, "running")
    assert [r["state"] for r in second["council"]["runs"]] == ["queued", "queued"]
    assert (
        out["brief"]["state"] == "in_council" and out["brief"]["council"]["round"] == 2
    )


def test_brief_view_without_a_council_says_none(env):
    card = ce.draft(env, council=None)
    ce.handin(env, card.id)

    out = _as(env, "owner", {"op": "card.brief", "card": card.id})

    assert out["versions"][0]["council"] is None and "council" not in out["brief"]


def test_the_overview_shows_a_card_under_its_council(env):
    card_id, _ = _reading(env)
    plain = env.card(status="In Progress")

    work = {r["card"]: r for r in _as(env, "owner", {"op": "overview.get"})["work"]}

    assert work[card_id]["stage"] == "in_council"
    assert work[card_id]["council"] == {
        "kind": "brief",
        "round": 1,
        "rounds": 3,
        "state": "running",
    }
    assert "council" not in work[plain.id]


# --- over HTTP ---------------------------------------------------------------------------------------------------
def test_the_councils_over_http(tmp_path):
    env = ce.make(tmp_path)
    env.users.create("anna", "Anna", roles=[("partner", ["digipaddock"])], actor="root")
    w = WebEnv(tmp_path, env=env)
    card_id, council_id = _reading(env)
    w.login()

    r = w.client.get(f"/api/cards/{card_id}/councils")

    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"councils", "can_manage"} and body["can_manage"] is True
    assert [c["id"] for c in body["councils"]] == [council_id]
    assert body["councils"][0]["rounds_list"][0]["runs"][0]["model"] == "claude:opus"

    anna = w.other_client()
    me = w.login("anna", client=anna)
    assert anna.get(f"/api/cards/{card_id}/councils").json()["can_manage"] is False
    url = f"/api/cards/{card_id}/councils/{council_id}/cancel"
    assert anna.post(url, headers=w.headers(me["csrf"])).status_code == 403
    assert w.client.post(url).status_code == 403  # no CSRF token

    r = w.client.post(url, headers=w.headers())

    assert r.status_code == 200
    assert r.json()["councils"][0]["state"] == "cancelled"
    assert r.json()["councils"][0]["end_reason"] == "owner"
    other = env.card()
    wrong = f"/api/cards/{other.id}/councils/{council_id}/cancel"
    assert w.client.post(wrong, headers=w.headers()).status_code == 404


# --- the coordinator's reading (it holds no board.write: kd-boss-board council) ------------------------------
def _page(env, card_id, agent, **more):
    return env.call(
        "digi", {"op": "council.view", "agent": agent, "card": card_id, **more}
    )


def test_the_coordinator_reads_the_same_council_and_is_told_its_own_command(env):
    card = ce.draft(env)
    ce.handin(env, card.id)
    ce.play(env, card.id, ce.thorough(), ce.block())

    writer, boss = _page(env, card.id, "analyst"), _page(env, card.id, "boss")

    assert boss["chunk"] == writer["chunk"]
    assert f"more: kd-work council {card.id} --cursor " in writer["text"]
    assert f"more: kd-boss-board council --card {card.id} --cursor " in boss["text"]
    assert "kd-work" not in boss["text"].splitlines()[-1]
    one = _page(env, card.id, "boss", round=1)
    assert (
        f"more: kd-boss-board council --card {card.id} --round 1 --cursor "
        in one["text"]
    )


# --- the council of the code ----------------------------------------------------------------------------
A = "a" * 40
PULL = {"repo": "acme/web", "number": 7, "head": A}
FOUND = (ce.block("with_fixes", [ce.finding()]), ce.block("ready"))


def _pulled(env, **over):
    """A card whose pull request the council of the code reads."""
    card = ce.coding(env)
    out = ce.publish(env, ce.pull(card.id, **over))
    return card.id, out["council"]["id"]


def test_the_code_councils_view_names_the_pull(env):
    card_id, council_id = _pulled(env)

    [found] = _as(env, "owner", {"op": "card.councils", "card": card_id})["councils"]

    assert (found["id"], found["kind"]) == (council_id, "code")
    assert found["basis"] == {
        "repo": "acme/web",
        "project": "web",
        "number": 7,
        "head": A,
        "target": "main",
        "url": "https://forge.example.test/acme/web/pulls/7",
    }
    assert [(r["round"], r["head"], r["version"]) for r in found["rounds_list"]] == [
        (1, A, None)
    ]


def _ended(env, card_id, how):
    record, found = env.store.card(card_id), ce.council_of(env, card_id, "code")
    if how == "clean":
        ce.play(env, card_id, ce.block("ready"), ce.block("ready"), kind="code")
    elif how == "failed":
        ce.play(env, card_id, None, None, kind="code")
    elif how == "cancelled":
        _as(env, "owner", {"op": "council.cancel", "council": found["id"]})
    else:
        found = ce.play(env, card_id, *FOUND, kind="code")
        if how == "settled":
            answer = {"finding": "r1.f1", "answer": "rejected", "note": "by design"}
            council_rounds.answered(env.ctx, found, record, "dev", "t1", [answer])
        reason = {"settled": "rejected", "limited": "rounds"}[how]
        council_rounds._end(env.ctx, found, record, how, reason)


@pytest.mark.parametrize(
    "how, rejected",
    [("clean", 0), ("settled", 1), ("limited", 0), ("failed", 0), ("cancelled", 0)],
)
def test_a_pull_decision_says_how_its_council_ended(env, how, rejected):
    card_id, council_id = _pulled(env)

    _ended(env, card_id, how)

    assert councils.pull_review(env.store, card_id, PULL) == {
        "state": how,
        "council": council_id,
        "lines": 150,
        "min_lines": 100,
        "rejected": rejected,
    }


def test_a_pull_below_the_threshold_says_not_needed(env):
    card = ce.coding(env)
    ce.publish(env, ce.pull(card.id, lines=10))

    assert councils.pull_review(env.store, card.id, PULL) == {
        "state": "skipped",
        "council": None,
        "lines": 10,
        "min_lines": 100,
        "rejected": 0,
    }


def test_a_pull_no_council_read_says_so(env):
    card = ce.coding(env, council=None)
    ce.publish(env, ce.pull(card.id))
    assert (
        councils.pull_review(env.store, card.id, PULL) is None
    )  # no critics of the code: no line at all

    env.store.edit_card(
        card.id, None, {"council": ce.CODE}, actor="user:owner"
    )  # they came after the decision

    found = councils.pull_review(env.store, card.id, PULL)
    assert (found["state"], found["council"], found["lines"]) == ("none", None, 150)


def test_a_pull_decision_of_another_head_has_no_council_line(env):
    card_id, _ = _pulled(env)
    _ended(env, card_id, "clean")

    assert councils.pull_review(env.store, card_id, {**PULL, "head": "b" * 40}) is None
    assert councils.pull_review(env.store, "KD-999999", PULL) is None


def test_the_pull_decisions_view_carries_its_council(tmp_path):
    env = ce.make(tmp_path)
    w = WebEnv(tmp_path, env=env)
    card_id, council_id = _pulled(env)
    _ended(env, card_id, "clean")
    [decision] = ce.pr_decisions(env, card_id)
    w.login()

    view = w.client.get(f"/api/decisions/{decision['id']}").json()["view"]

    assert view["council"] == {
        "state": "clean",
        "council": council_id,
        "lines": 150,
        "min_lines": 100,
        "rejected": 0,
    }


def test_next_step_names_the_code_council(env):
    card_id, _ = _pulled(env)

    step = _next(env, card_id)
    assert (step["source"], step["what"], step["who"]) == (
        "derived",
        "council_running",
        "-",
    )
    assert step["params"] == {"round": 1, "rounds": 3, "done": 0, "total": 2}
    assert (
        step["text"]
        == "the council reads pull request #7: round 1 of 3, 0 of 2 critics done"
    )

    ce.play(env, card_id, *FOUND, kind="code")
    step = _next(env, card_id)
    assert (step["what"], step["who"]) == ("council_writer", "@dev")
    assert step["params"] == {"round": 1, "rounds": 3, "findings": 1}
    assert (
        step["text"] == "@dev answers the council: round 1 of 3, 1 findings unanswered"
    )

    found = ce.council_of(env, card_id, "code")
    council_rounds._end(env.ctx, found, env.store.card(card_id), "limited", "rounds")
    assert (
        _next(env, card_id)["what"] == "wait_decision"
    )  # the pull request is the owner's now


def test_a_turn_waiting_for_the_code_council_reads_as_the_council(env):
    from test_cabinet_outcomes import begin, end

    card_id, council_id = _pulled(env)
    command, session = begin(env, env.store.card(card_id), agent="dev", kind="signal")
    request = {
        "op": "turn.finish",
        "agent": "dev",
        "card": card_id,
        "outcome": "wait_dependency",
    }
    env.call(
        "digi",
        {**request, "councils": [council_id], "note": "PR #7: waits for the council"},
    )
    end(env, command, session)

    step = _next(env, card_id)

    assert (step["source"], step["what"], step["who"]) == (
        "recorded",
        "council_running",
        "-",
    )
    assert (
        step["text"]
        == "the council reads pull request #7: round 1 of 3, 0 of 2 critics done"
    )


def test_a_dead_session_goes_before_the_code_council(env):
    card_id, _ = _pulled(env)
    ce.play(env, card_id, *FOUND, kind="code")
    [wake] = ce.wakes(env, "dev")
    session = env.store.session_for(env.store.card(card_id).thread, "dev")["id"]
    for state in ("delivered", "started", "failed"):
        env.store.command_state(wake["id"], state, session=session)

    assert _next(env, card_id)["what"] == "run_failed"


def test_the_snapshot_names_the_code_round(env):
    card_id, _ = _pulled(env)

    lines = _snap(env, card_id).splitlines()

    assert (
        "council round: code 1/3 running, open findings 0 · PR #7 head aaaaaaaaaaaa"
        in lines
    )
    assert f"council kd-work council {card_id}" in lines[-1]
    ce.play(env, card_id, *FOUND, kind="code")
    assert (
        "council round: code 1/3 needs_writer, open findings 1 · PR #7 head aaaaaaaaaaaa"
        in _snap(env, card_id)
    )


def test_the_overview_shows_a_card_under_its_code_council(env):
    card_id, _ = _pulled(env)

    work = {r["card"]: r for r in _as(env, "owner", {"op": "overview.get"})["work"]}

    assert work[card_id]["stage"] == "In Progress"
    assert work[card_id]["council"] == {
        "kind": "code",
        "round": 1,
        "rounds": 3,
        "state": "running",
    }
    assert (
        ce.pr_decisions(env, card_id) == []
    )  # nothing of the pull request is in the owner's inbox yet


# --- the pull request a council holds back is shown in its block ---------------------
def test_an_open_code_council_shows_the_pull_request_it_holds(env):
    card_id, council_id = _pulled(env, checks=["static"], effect="deploys the site")

    [found] = _as(env, "owner", {"op": "card.councils", "card": card_id})["councils"]

    pull = found["pull"]
    assert (pull["number"], pull["head"], pull["target"], pull["effect"]) == (
        7,
        A,
        "main",
        "deploys the site",
    )
    assert (pull["checks"], pull["stale"], pull["cage"]) == (["static"], False, [])
    assert [f["path"] for f in pull["files"]] == ["src/app.py", "tests/test_app.py"]
    _ended(env, card_id, "clean")
    [found] = _as(env, "owner", {"op": "card.councils", "card": card_id})["councils"]
    assert "pull" not in found  # the decision shows it from here on


def test_the_held_pull_requests_diff_over_http(tmp_path):
    env = ce.make(tmp_path)
    w = WebEnv(tmp_path, env=env)
    text = "diff --git a/src/app.py b/src/app.py\n+one\n"
    card_id, council_id = _pulled(env, diff=text)
    w.login()

    r = w.client.get(f"/api/cards/{card_id}/councils/{council_id}/diff")

    assert r.status_code == 200
    assert r.json() == {"diff": text, "truncated": False, "head": A}
    other = env.card()
    wrong = f"/api/cards/{other.id}/councils/{council_id}/diff"
    assert w.client.get(wrong).status_code == 404
    brief_id, brief_council = _reading(env)
    of_brief = f"/api/cards/{brief_id}/councils/{brief_council}/diff"
    assert w.client.get(of_brief).status_code == 404


# --- a critic's run names the models that served its job --------------------------------------------------
def test_a_run_names_the_models_that_served_its_job(env):
    card_id, _ = _reading(env)
    (reading,) = _as(env, "owner", {"op": "card.councils", "card": card_id})["councils"][0]["rounds_list"]
    assert [run["served"] for run in reading["runs"]] == [[], []]  # nothing in the ledger while they read
    ce.play(env, card_id, ce.block(), ce.block())
    (done,) = _as(env, "owner", {"op": "card.councils", "card": card_id})["councils"][0]["rounds_list"]
    assert [run["served"] for run in done["runs"]] == [["claude-opus-5-5"], ["claude-opus-5-5"]]


def test_a_councils_view_reads_the_ledger_once(env):
    # the cost and the models of its critics' jobs are one read of the usage ledger, not one each
    card_id, _ = _reading(env)
    ce.play(env, card_id, ce.block(), ce.block())
    seen = []
    env.store._conn.set_trace_callback(seen.append)
    try:
        (found,) = _as(env, "owner", {"op": "card.councils", "card": card_id})["councils"]
    finally:
        env.store._conn.set_trace_callback(None)
    assert len([statement for statement in seen if "usage_log" in statement]) == 1, seen
    (done,) = found["rounds_list"]
    assert done["runs"][0]["cost"] == 0.12 and done["runs"][1]["cost"] > 0  # each job at its own model's price
    assert [run["served"] for run in done["runs"]] == [["claude-opus-5-5"]] * 2


def test_a_councils_ledger_read_uses_the_jobs_index(env):
    # without an index on a job's rows every council on a card is a scan of the whole usage ledger
    plan = env.store._conn.execute("EXPLAIN QUERY PLAN " + councils.RUN_USAGE, ('["job"]',)).fetchall()
    assert any("usage_log_job" in " ".join(str(v) for v in tuple(row)) for row in plan), plan
