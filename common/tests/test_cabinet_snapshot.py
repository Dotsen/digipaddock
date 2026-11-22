"""a card's operational snapshot — its facts, the derived next step, 8 KiB, the card.snapshot op."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cabinet_env import CabinetEnv  # noqa: E402

import json  # noqa: E402

from kd.cabinet import api, decisions, probe_cards, snapshot  # noqa: E402


@pytest.fixture
def cab(tmp_path):
    return CabinetEnv(tmp_path)


def facts(cab, card_id):
    return snapshot.facts(cab.ctx, cab.store.card(card_id))


def turn(cab, card_id, agent="dev", *, to="done", kind="assigned", session_state=None):
    """A command of ``agent`` on the card moved to ``to``; its session in ``session_state``."""
    thread = cab.store.card(card_id).thread
    session = cab.store.session_for(thread, agent)["id"]
    command = cab.store.enqueue({"key": f"t:{card_id}:{to}:{kind}", "agent": agent, "thread": thread,
                                 "card": card_id, "kind": kind, "text": f"[board] {card_id} {kind}"})["id"]
    path = {"queued": [], "delivered": ["delivered"], "started": ["delivered", "started"],
            "done": ["delivered", "started", "done"], "failed": ["failed"]}[to]
    for state in path:
        cab.store.command_state(command, state, session=session, error="boom" if state == "failed" else "")
    if session_state:
        cab.store.session_update(session, state=session_state, heartbeat=cab.now)
    return command


def test_facts_of_the_9041_replica(cab):
    found = facts(cab, probe_cards.kd9041_like(cab.store))
    assert found["v"] == 1 and found["card"]["revision"] == cab.store.card("KD-9041").revision
    assert [(p["number"], p["state"], p["approved_head"], p["mode"]) for p in found["prs"]] == [
        (18, "merged", True, "release")]
    assert found["release"]["key"] == "notice.forge.release_degraded" and found["release"]["rev"] == "cf2d924"
    assert found["run"]["state"] == "done" and found["run"]["session"] == "exited" and found["queued"] == 0
    assert found["deps"]["total"] == found["children"]["total"] == 0
    assert (found["result"]["ac_checked"], found["result"]["ac_total"]) == (4, 9)
    assert found["messages"]["items"] and all(m["kind"] != "tool_call" for m in found["messages"]["items"])
    assert found["watermark"] == cab.store.last_seq() and len(found["scope"]["sha"]) == 12


def test_facts_of_the_9032_replica(cab):
    found = facts(cab, probe_cards.kd9032_like(cab.store))
    assert found["waits"]["open"] == []
    assert found["waits"]["answered"][0]["type"] == "question"
    assert found["waits"]["answered"][0]["state"] == "approved"
    assert found["deps"]["total"] == 0 and found["card"]["status"] == "Blocked"


def test_replicas_next_steps(cab):
    assert snapshot.next_step(facts(cab, probe_cards.kd9041_like(cab.store)))["what"] == "turn_ended"
    assert snapshot.next_step(facts(cab, probe_cards.kd9032_like(cab.store)))["what"] == "blocked_unexplained"


def test_children_by_parent(cab):
    goal = cab.card()
    child = cab.store.edit_card(cab.card().id, None, {"parent": goal.id}, actor="user:owner")
    cab.card()
    assert [c.id for c in cab.store.cards(parent=goal.id)] == [child.id]
    assert facts(cab, goal.id)["children"]["items"][0]["id"] == child.id


def next_of(cab, card_id):
    return snapshot.next_step(facts(cab, card_id))


def test_next_done(cab):
    assert next_of(cab, cab.card(status="Done").id)["what"] == "done"


def test_next_open_decision_waits_for_the_owner(cab):
    card = cab.card(status="In Progress")
    decisions.Decisions(cab.store).open("question", card=card.id, payload={"text": "?"}, requested_by="agent:dev",
                                        now=cab.now)
    found = next_of(cab, card.id)
    assert (found["what"], found["who"]) == ("wait_decision", "@owner")


def test_next_names_the_decision_the_owner_answers_first(cab):
    # one next step for the agents and the page — an agent's question before a PR, a promote and the rest
    cab.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    card = cab.card(status="Inbox")
    ledger = decisions.Decisions(cab.store)
    promote = ledger.open("promote", card=card.id, payload={}, requested_by="agent:boss", now=cab.now)
    question = ledger.open("question", card=card.id, payload={"text": "?"}, requested_by="agent:analyst",
                           now=cab.now)
    found = next_of(cab, card.id)
    assert (found["what"], found["who"]) == ("wait_decision", "@owner")
    assert found["decision"] == {"id": question["id"], "type": "question"}
    assert found["params"] == {"agent": "@analyst"}
    # a question on BOSS's proposal: its agent owes the answer, the decision stays the owner's after it
    ledger.ask(promote["id"], user="owner", text="Почему сейчас?", now=cab.now)
    found = next_of(cab, card.id)
    assert (found["what"], found["who"]) == ("asked", "@boss")
    assert found["decision"] == {"id": promote["id"], "type": "promote"}
    assert found["params"] == {"agent": "@boss"}
    assert "@boss answers the owner's question on promote" in found["text"]
    cab.store.add_message(card.id, author="agent:boss", kind="text", body="Потому что повторяется")
    assert next_of(cab, card.id)["decision"]["id"] == question["id"]


def test_the_page_reads_the_same_next_step(cab):
    # card.get carries the snapshot's next step: the page renders it and adds no rules of its own
    cab.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    card = cab.card(status="In Progress")
    asked = ask(cab, card.id)
    got = api.dispatch(cab.ctx, api.Principal("user", -1, "owner"), {"op": "card.get", "card": card.id})
    assert got["next"] == next_of(cab, card.id)
    assert got["next"]["decision"] == {"id": asked, "type": "question"}
    agent = api.Principal("agent", 2001, "digi-dev", "dev")
    assert "next" not in api.dispatch(cab.ctx, agent, {"op": "card.get", "card": card.id})


def test_every_next_step_has_params(cab):
    assert next_of(cab, cab.card(status="Done").id)["params"] == {}
    assert next_of(cab, cab.briefed().id)["params"]["version"] == 1


@pytest.mark.parametrize("to,session_state,what", [
    ("started", "working", "run_working"),
    ("queued", None, "run_queued"),
    ("failed", None, "run_failed"),
])
def test_next_follows_the_run(cab, to, session_state, what):
    card = cab.card(status="In Progress")
    turn(cab, card.id, to=to, session_state=session_state)
    found = next_of(cab, card.id)
    assert found["what"] == what
    assert found["who"] == ("@boss" if what == "run_failed" else "@dev")


def test_next_review_and_blocked(cab):
    assert next_of(cab, cab.card(status="Review").id)["what"] == "review"
    dep = cab.card()
    assert next_of(cab, cab.card(status="Blocked", dependencies=[dep.id]).id)["what"] == "wait_deps"
    found = next_of(cab, cab.card(status="Blocked").id)
    assert (found["what"], found["who"]) == ("blocked_unexplained", "@boss")


def test_next_in_inbox(cab):
    assert next_of(cab, cab.card(status="Inbox", assignee="").id)["what"] == "inbox"
    assert next_of(cab, cab.briefed().id)["what"] == "brief_ready"
    writing = cab.card(status="Inbox", assignee="@analyst", labels=["spec:task-brief"])
    turn(cab, writing.id, agent="analyst", to="queued")
    assert next_of(cab, writing.id)["what"] == "brief_writing"


def test_next_turn_ended_and_not_started(cab):
    card = cab.card(status="In Progress")
    turn(cab, card.id, to="done", session_state="exited")
    found = next_of(cab, card.id)
    assert (found["what"], found["who"]) == ("turn_ended", "-")
    assert "no continuation recorded" in found["text"]
    found = next_of(cab, cab.card(status="To Do").id)
    assert (found["what"], found["who"]) == ("not_started", "@dev")


def snap(cab, card_id, fmt="text"):
    return cab.call("digi-cabinet", {"op": "card.snapshot", "card": card_id, "format": fmt})


def size(value) -> int:
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def test_snapshot_of_the_9041_replica_fits(cab):
    card = probe_cards.kd9041_like(cab.store)
    text, found = snap(cab, card)["text"], snap(cab, card, "json")["snapshot"]
    assert size(text) <= snapshot.LIMIT and size(found) <= snapshot.LIMIT
    for part in ("#18 merged", "approved on this head (release)", "check failed", "turn ended",
                 "kd-task scope KD-9041", "kd-task result KD-9041", "AC 4/9"):
        assert part in text
    calls = [m["body"] for m in cab.store.messages(card) if m["kind"] == "tool_call"]
    assert not any(body in text for body in calls)
    assert text.splitlines()[0].startswith("KD-9041 · In Progress · @dev · ")


def test_snapshot_of_the_9032_replica_fits(cab):
    text = snap(cab, probe_cards.kd9032_like(cab.store))["text"]
    assert size(text) <= snapshot.LIMIT
    lines = text.splitlines()
    assert "Blocked" in lines[0]
    assert any(line.startswith("deps: none") for line in lines)
    assert any("answered: question" in line for line in lines)
    assert any(line.startswith("next (derived): blocked_unexplained @boss") for line in lines)
    assert "создай, пожалуйста, подзадачи" in text


def test_a_huge_newest_message_is_cut_not_dropped(cab):
    card = cab.card(status="In Progress")
    seq = cab.store.add_thread_message(card.thread, author="user:owner", kind="text", body="ж" * 20_000)["seq"]
    text = snap(cab, card.id)["text"]
    assert size(text) <= snapshot.LIMIT
    assert f"… (kd-task message {card.id} {seq})" in text
    found = snap(cab, card.id, "json")["snapshot"]
    assert found["messages"]["items"][0]["seq"] == seq and size(found["messages"]["items"][0]["text"]) < 800


def test_many_messages_keep_the_newest_and_point_to_the_older(cab):
    card = cab.card(status="In Progress")
    seqs = [cab.store.add_thread_message(card.thread, author="user:owner", kind="text", body=f"m{i} " + "я" * 600)["seq"]
            for i in range(30)]
    found = snap(cab, card.id, "json")["snapshot"]
    shown = [m["seq"] for m in found["messages"]["items"]]
    assert shown == sorted(shown, reverse=True) and shown[0] == seqs[-1]
    assert len(shown) <= snapshot.MESSAGES_SHOWN and found["messages"]["total"] == 30
    assert found["more"]["thread"] == f"kd-task thread {card.id} --before {shown[-1]}"
    assert size(snap(cab, card.id)["text"]) <= snapshot.LIMIT


def test_the_fit_trims_in_order_and_says_so(cab):
    goal = cab.card(status="In Progress")
    for i in range(14):
        cab.store.edit_card(cab.card(title=f"Подзадача {i} " + "т" * 90).id, None, {"parent": goal.id}, actor="user:owner")
    cab.store.add_thread_message(goal.thread, author="user:owner", kind="text", body="ш" * 300)
    built = snapshot.build(cab.ctx, cab.store.card(goal.id))
    assert len(built["children"]["items"]) == snapshot.LIST_SHOWN and built["children"]["total"] == 14
    assert built["more"]["children"] == f"kd-task list --parent {goal.id}"
    tight = snapshot.fit(built, limit=size(built) - 200)
    assert tight["trimmed"] == ["messages"] and tight["messages"]["items"] == []
    tighter = snapshot.fit(built, limit=size(built) - 1500)
    assert tighter["trimmed"][:2] == ["messages", "children"] and size(tighter) <= size(built) - 1500


def test_empty_card_says_none_in_every_section(cab):
    lines = snap(cab, cab.card(status="To Do").id)["text"].splitlines()
    for start in ("run: none", "waits: none open · answered: none", "PR: none", "release: none",
                  "deps: none · children: none", "messages: none"):
        assert any(line.startswith(start) for line in lines), start
    assert any(line.startswith("next (derived): not_started @dev") for line in lines)


def test_the_snapshot_names_the_cards_model_and_effort(cab):
    """a card on a cabinet model (its own, or its project's) and on an effort — one line each after the
    head, and the facts in the JSON form; a card on the subscription with the agent's own effort has neither."""
    cab.store.create_llm_model({"id": "or-x", "provider": "openrouter", "upstream": "anthropic/claude-sonnet-4.5",
                                "title": "Sonnet via OpenRouter", "price_in": 1, "price_out": 2,
                                "price_cache_read": 0.1, "price_cache_write": 1, "context": 200000, "base_url": ""},
                               actor="root")
    plain, card = cab.card(status="To Do"), cab.card(status="To Do")
    cab.store.edit_card(card.id, None, {"model": "or-x", "effort": "high"}, actor="user:owner")
    lines = snap(cab, card.id)["text"].splitlines()
    assert lines[1] == "model: or-x · openrouter" and lines[2] == "effort: high"
    assert lines[3].startswith("revision ")
    found = snap(cab, card.id, "json")["snapshot"]
    assert found["model"] == {"id": "or-x", "provider": "openrouter", "title": "Sonnet via OpenRouter", "enabled": 1}
    assert found["effort"] == "high"
    text = snap(cab, plain.id)["text"]
    assert "model:" not in text and "effort:" not in text
    assert snap(cab, plain.id, "json")["snapshot"]["model"] is None
    cab.store.update_llm_model("or-x", {"enabled": 0}, actor="root")
    assert "model: or-x · openrouter · disabled\n" in snap(cab, card.id)["text"]
    # the project's model and effort reach its cards the same way
    cab.store.update_project(plain.project, actor="user:owner", model="or-x", effort="low")
    lines = snap(cab, plain.id)["text"].splitlines()
    assert lines[1] == "model: or-x · openrouter · disabled" and lines[2] == "effort: low"


def test_a_person_without_the_project_gets_not_found(cab):
    cab.users.create("anna", "Анна", roles=[("partner", ["web"])], actor="root")
    card = cab.card(project="digipaddock")
    with pytest.raises(api.ApiError) as exc:
        api.dispatch(cab.ctx, api.Principal("user", -1, "anna"), {"op": "card.snapshot", "card": card.id})
    assert exc.value.kind == "not_found"


def test_json_format_is_the_fitted_dict(cab):
    card = probe_cards.kd9041_like(cab.store)
    found = snap(cab, card, "json")["snapshot"]
    assert found == snapshot.build(cab.ctx, cab.store.card(card))
    assert found["next"]["what"] == "turn_ended" and found["next"]["source"] == "derived"
    assert found["more"]["scope"] == f"kd-task scope {card}" and found["more"]["result"] == f"kd-task result {card}"
    with pytest.raises(api.ApiError) as exc:
        snap(cab, card, "yaml")
    assert exc.value.kind == "invalid"


def test_huge_labels_and_release_why_keep_the_head_and_the_next_step(cab):
    card = cab.card(status="In Progress", labels=[f"label-{i:02d}-" + "x" * 64 for i in range(60)])
    seq = cab.store.add_thread_message(
        card.thread, author="system:forge", kind="notice", body="", via="notice",
        payload={"key": "notice.forge.release_degraded", "level": "warn", "source": "forge",
                 "params": {"host": "h" * 300, "rev": "abc1234", "why": "п" * 2000}})["seq"]
    found = snap(cab, card.id, "json")["snapshot"]
    assert size(found) <= snapshot.LIMIT - 1
    assert size(", ".join(found["card"]["labels"])) <= 400 and found["card"]["labels"][-1].startswith("+")
    assert found["release"]["why"].endswith(f"… (kd-task message {card.id} {seq})")
    assert size(found["release"]["why"]) <= 520 and size(found["release"]["host"]) <= 120
    text = snap(cab, card.id)["text"]
    assert size(text) <= snapshot.LIMIT
    assert "\nnext (derived): " in text and "\nmore: scope " in text and "\nrelease: degraded abc1234 " in text


def test_in_progress_without_a_request_is_nobodys(cab):
    found = next_of(cab, cab.card(status="In Progress").id)
    assert (found["what"], found["who"]) == ("no_request", "@boss")
    assert "no request" in found["text"]


def test_a_dead_brief_session_in_inbox_is_stale_not_writing(cab):
    card = cab.card(status="Inbox", assignee="@analyst", labels=["spec:task-brief"])
    turn(cab, card.id, agent="analyst", to="started", session_state="working")
    assert next_of(cab, card.id)["what"] == "brief_writing"
    cab.now = "2026-10-14T10:30:00Z"
    found = next_of(cab, card.id)
    assert (found["what"], found["who"]) == ("run_stale", "@analyst")


def test_dependencies_and_children_out_of_sight_are_not_shown(cab):
    cab.users.create("anna", "Анна", roles=[("partner", ["digipaddock"])], actor="root")
    hidden = cab.card(project="web", title="Секретная подзадача")
    goal = cab.card(project="digipaddock", dependencies=[hidden.id])
    cab.store.edit_card(hidden.id, None, {"parent": goal.id}, actor="user:owner")
    request = {"op": "card.snapshot", "card": goal.id, "format": "json"}
    found = api.dispatch(cab.ctx, api.Principal("user", -1, "anna"), request)["snapshot"]
    assert found["children"] == {"total": 0, "items": []} and found["deps"]["items"] == []
    assert "Секретная" not in json.dumps(found, ensure_ascii=False)
    assert snap(cab, goal.id, "json")["snapshot"]["children"]["total"] == 1


def test_cancelled_request_reads_cancelled(cab):
    """a request the cabinet took back says so, with its reason."""
    card = cab.card(status="In Progress", assignee="@dev")
    command = turn(cab, card.id, to="queued")
    cab.store.command_state(command, "cancelled", reason="reassigned")
    step = next_of(cab, card.id)
    assert step["what"] == "turn_ended" and "cancelled (reassigned)" in step["text"]


# --- the recorded next step -------------------------------------------------------------------
from kd.cabinet import outcomes, problems  # noqa: E402


def recorded(cab, card_id, *, outcome="wait_user", note="ждём ответа владельца по схеме", waits=(), agent="dev",
             key="r"):
    """A final outcome of a turn of ``agent`` on the card and its open waits, as the turn's end leaves them."""
    record = cab.store.card(card_id)
    command = turn(cab, card_id, agent, to="done", kind="assigned" if key == "r" else key)
    cab.store._conn.execute(
        "INSERT INTO turn_outcomes (command, card, thread, agent, outcome, mode, note, basis, state, created, updated)"
        " VALUES (?,?,?,?,?,'all',?,'{}','final',?,?)",
        (command, card_id, record.thread, agent, outcome, note, cab.now, cab.now))
    for i, (kind, ref, blocking) in enumerate(waits):
        cab.store._conn.execute(
            "INSERT INTO waits (id, command, card, agent, kind, ref, blocking, mode, state, created, updated)"
            " VALUES (?,?,?,?,?,?,?,'all','open',?,?)",
            (f"w{i}{command[:8]}", command, card_id, agent, kind, ref, blocking, cab.now, cab.now))
    return command


def ask(cab, card_id, agent="dev"):
    return decisions.Decisions(cab.store).open("question", card=card_id, payload={"text": "?"},
                                               requested_by=f"agent:{agent}", now=cab.now)["id"]


def stuck(cab, card_id, kind="missing_next_step", key="m:1", agent="dev"):
    return problems.raise_problem(cab.ctx, key=key, record=cab.store.card(card_id), kind=kind, agent=agent,
                                  command="c1")


def test_recorded_wait_is_the_next_step(cab):
    card = cab.card(status="In Progress")
    asked = ask(cab, card.id)
    recorded(cab, card.id, waits=[("decision", asked, 1), ("card", "KD-77", 0)])
    text = snap(cab, card.id)["text"]
    assert "next (recorded): wait_decision @owner — ждём ответа владельца по схеме" in text
    assert f"outcome: wait_user by @dev {cab.now} — ждём ответа владельца по схеме (final)" in text
    assert f"waits: recorded: blocking — decision {asked[:8]} open; non-blocking — card KD-77 open · open:" in text


def test_recorded_dependency_wait_is_nobodys_move(cab):
    dep = cab.card(status="To Do")
    card = cab.card(status="Blocked", dependencies=[dep.id])
    recorded(cab, card.id, outcome="wait_dependency", note="ждём схему из соседней карточки",
             waits=[("card", dep.id, 1)])
    assert "next (recorded): wait_deps — ждём схему из соседней карточки" in snap(cab, card.id)["text"]


def test_problem_is_the_next_step(cab):
    card = cab.card(status="In Progress")
    stuck(cab, card.id)
    text = snap(cab, card.id)["text"]
    assert "next (recorded): missing_next_step @boss — " in text
    assert f"problems: missing_next_step @dev since {cab.now}" in text
    own = cab.card(status="In Progress", assignee="@boss")
    stuck(cab, own.id, key="m:2", agent="boss")
    assert "next (recorded): missing_next_step @owner — " in snap(cab, own.id)["text"]


def test_a_council_problem_is_the_next_step(cab):
    card = cab.card(status="Inbox")
    stuck(cab, card.id, kind="council_failed", key="council-failed:1", agent="analyst")
    text = snap(cab, card.id)["text"]
    assert "next (recorded): council_failed @boss — no critic of the council could start" in text
    assert "the work goes on without it, the coordinator checks why" in text


def test_without_pipeline_facts_the_next_step_is_derived(cab):
    card = cab.card(status="In Progress")
    turn(cab, card.id)
    text = snap(cab, card.id)["text"]
    assert "next (derived): turn_ended" in text and "outcome:" not in text and "problems:" not in text


def test_snapshot_with_pipeline_fits_8k(cab):
    card = probe_cards.kd9041_like(cab.store)
    note = ("очень длинная заметка о следующем шаге " * 10)[:300]
    recorded(cab, card, note=note, waits=[("card", f"KD-{900 + i}", i % 2) for i in range(20)])
    for i in range(5):
        stuck(cab, card, kind=schema_kinds()[i % 4], key=f"k:{i}")
    text, found = snap(cab, card)["text"], snap(cab, card, "json")["snapshot"]
    assert size(text) <= snapshot.LIMIT and size(found) <= snapshot.LIMIT
    assert "next (recorded): missing_next_step @boss" in text and f"waits kd-task waits {card}" in text
    assert found["more"]["waits"] == f"kd-task waits {card}"


def schema_kinds():
    from kd.cabinet import schema

    return schema.PROBLEM_KINDS


def test_card_facts_for_people(cab):
    cab.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    card = cab.card(status="In Progress")
    asked = ask(cab, card.id)
    recorded(cab, card.id, waits=[("decision", asked, 1)])
    stuck(cab, card.id)
    facts_ = outcomes.card_facts(cab.ctx, cab.store.card(card.id))
    assert facts_["mode"] == "observe" and facts_["outcome"]["outcome"] == "wait_user"
    assert facts_["outcome"]["note"] == "ждём ответа владельца по схеме" and facts_["outcome"]["state"] == "final"
    assert [{k: v for k, v in w.items() if k != "id"} for w in facts_["waits"]] == [
        {"kind": "decision", "ref": asked, "state": "open", "blocking": True, "reason": ""}]
    assert all(w["id"] for w in facts_["waits"])  # the page keys its list by the wait
    assert [(p["kind"], p["agent"]) for p in facts_["problems"]] == [("missing_next_step", "@dev")]
    got = api.dispatch(cab.ctx, api.Principal("user", -1, "owner"), {"op": "card.get", "card": card.id})
    assert got["pipeline"] == facts_
    assert "pipeline" not in cab.call("digi-cabinet", {"op": "card.get", "card": card.id})


def test_an_auth_failed_session_is_significant():
    """the snapshot's latest messages keep the line that says the agent cannot sign in to Claude."""
    from kd.cabinet import snapshot  # noqa: PLC0415

    line = {"kind": "status", "payload": {"key": "sys.session.auth_failed", "params": {}}}
    assert snapshot.significant(line)


def test_the_snapshot_names_a_subscription_model(cab):
    card = cab.card(status="To Do")
    cab.store.edit_card(card.id, None, {"model": "claude:sonnet"}, actor="user:owner")
    assert snap(cab, card.id)["text"].splitlines()[1] == "model: claude:sonnet · subscription"
    assert snap(cab, card.id, "json")["snapshot"]["model"] == {"id": "claude:sonnet", "provider": "subscription",
                                                               "title": "Claude Sonnet", "enabled": 1}


def test_the_snapshot_names_the_council(cab):
    card, plain = cab.card(status="To Do"), cab.card(status="To Do", project="web")
    cab.store.update_project(card.project, actor="user:owner",
                             council={"code": {"critics": ["claude:opus"], "rounds": 3, "min_lines": 100}})
    cab.store.edit_card(card.id, None, {"council": {"brief": {"critics": ["claude:opus"], "rounds": 3}}},
                        actor="user:owner")
    text = snap(cab, card.id)["text"]
    assert "\ncouncil: brief claude:opus ×3 · code claude:opus ×3 ≥100 lines\n" in text
    assert snap(cab, card.id, "json")["snapshot"]["council"] == {
        "brief": {"critics": ["claude:opus"], "rounds": 3},
        "code": {"critics": ["claude:opus"], "rounds": 3, "min_lines": 100}}
    assert "council:" not in snap(cab, plain.id)["text"]
    assert snap(cab, plain.id, "json")["snapshot"]["council"] == {}


def test_the_snapshot_of_a_card_on_an_alias_gone_from_the_catalog(cab):
    card = cab.card(status="To Do")
    cab.store._conn.execute(
        "UPDATE cards SET model='claude:old', council=? WHERE id=?",
        ('{"brief": {"critics": ["claude:old"], "rounds": 3}}', card.id))
    text = snap(cab, card.id)["text"]
    assert "\nmodel:" not in text and "\ncouncil: brief claude:old ×3\n" in text
    assert snap(cab, card.id, "json")["snapshot"]["model"] is None
