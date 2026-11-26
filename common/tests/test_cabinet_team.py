"""An agent's state from facts — its sessions, commands, questions and cards, only those a person reads."""

import sys
from pathlib import Path

import pytest

from kd.cabinet import api, decisions, team

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cabinet_env import CabinetEnv  # noqa: E402

NOW = "2026-10-14T10:00:00Z"


@pytest.fixture
def env(tmp_path):
    return CabinetEnv(tmp_path)


def visible(env, projects=("digipaddock", "web", "admin")):
    return [api.compact(c) for c in env.store.cards() if c.project in projects]


def facts(env, name="dev", projects=("digipaddock", "web", "admin"), *, spaces=("chat", "monitor"), hosted=True):
    return team.facts(env.store, name, cards=visible(env, projects), spaces=spaces, hosted=hosted, now=env.now)


def command(env, card, agent="dev", n=1, state="queued", **extra):
    found = env.store.enqueue(
        {"key": f"t:{card.id}:{n}", "agent": agent, "thread": card.thread, "kind": "assigned", "text": f"go {n}"}
    )
    if state != "queued":
        session = env.store.session_for(card.thread, agent)
        env.store.command_state(found["id"], "delivered", session=session["id"])
        if state in ("started", "done", "failed"):
            env.store.command_state(found["id"], "started", session=session["id"])
        if state in ("done", "failed"):
            env.store.command_state(found["id"], state, session=session["id"], **extra)
    return env.store.command(found["id"])


def session(env, card, state, agent="dev", heartbeat=NOW):
    found = env.store.session_for(card.thread, agent)
    return env.store.session_update(found["id"], state=state, heartbeat=heartbeat)


def ask(env, card, agent="dev"):
    return decisions.Decisions(env.store).open(
        "question", card=card.id, payload={"text": "?"}, requested_by=f"agent:{agent}", now=env.when()
    )


def test_working_on_one_card_and_waiting_for_an_answer_on_another(env):
    busy = env.card(status="In Progress")
    asking = env.card(status="In Progress", title="Asks")
    command(env, busy, state="started")
    session(env, busy, "working")
    ask(env, asking)
    found = facts(env)
    assert found["state"] == "working"
    assert found["working"] == 1 and found["waiting_you"] == 1
    assert [q["card"] for q in found["questions"]] == [asking.id]
    assert found["questions"][0]["title"] == "Asks"
    assert found["cards"]["in_progress"] == 2


def test_the_analyst_preparing_a_brief_in_inbox_is_counted(env):
    card = env.card(status="Inbox", assignee="@analyst", labels=["spec:task-brief"])
    command(env, card, agent="analyst", state="started")
    session(env, card, "working", agent="analyst")
    found = facts(env, "analyst")
    assert found["cards"]["inbox"] == 1 and found["working"] == 1 and found["state"] == "working"


def test_a_command_waiting_for_a_slot_is_queued(env):
    card = env.card()
    command(env, card)
    found = facts(env)
    assert found["queued"] == 1 and found["state"] == "queued"
    other = env.card(title="Two")
    command(env, other, n=2, state="delivered")
    session(env, other, "queued")
    assert facts(env)["queued"] == 2


def test_a_failed_run_with_nothing_after_it_is_an_error_until_the_next_one(env):
    card = env.card(status="In Progress")
    command(env, card, state="failed", error="exit 1", reason="exit")
    found = facts(env)
    assert found["state"] == "error" and found["failed"] == 1
    assert found["failures"][0]["card"] == card.id and found["failures"][0]["reason"] == "exit"
    command(env, card, n=2)
    found = facts(env)
    assert found["failed"] == 0 and found["state"] == "queued"


def test_a_host_that_refused_is_unavailable(env):
    card = env.card(status="In Progress")
    found = env.store.enqueue({"key": "t:h", "agent": "dev", "thread": card.thread, "kind": "assigned", "text": "go"})
    env.store.command_state(found["id"], "failed", error="refused", reason="host_refused")
    assert facts(env)["state"] == "unavailable"


def test_a_stale_heartbeat_is_unknown(env):
    card = env.card(status="In Progress")
    session(env, card, "working", heartbeat="2026-10-14T09:55:00Z")
    found = facts(env)
    assert found["working"] == 0 and found["stale"] == 1 and found["state"] == "unknown"
    assert found["heartbeat"] == "2026-10-14T09:55:00Z"


def test_an_agent_off_the_host_is_unknown_but_keeps_its_cards(env):
    env.card(status="To Do")
    found = facts(env, hosted=False)
    assert found["state"] == "unknown" and found["cards"]["to_do"] == 1


def test_a_free_agent(env):
    assert facts(env)["state"] == "free"


def test_progress_and_the_last_action_are_apart(env):
    card = env.card(status="In Progress")
    env.store.add_message(card.id, author="agent:dev", kind="text", body="PR готов")
    env.now = "2026-10-14T10:05:00Z"
    env.store.add_message(card.id, author="agent:dev", kind="progress", body="шаг")
    found = facts(env)
    assert found["last_action"] == {"at": NOW, "card": card.id, "text": "PR готов"}
    assert found["progress"] == "2026-10-14T10:05:00Z"


def test_team_facts_count_only_readable_threads(env):
    hidden = env.card(project="digipaddock", status="In Progress")
    command(env, hidden, state="started")
    session(env, hidden, "working")
    ask(env, hidden)
    queued = env.card(project="digipaddock", title="Q")
    command(env, queued, n=2)
    failed = env.card(project="digipaddock", status="In Progress", title="F")
    command(env, failed, n=3, state="failed", error="x", reason="exit")
    env.store.add_message(hidden.id, author="agent:dev", kind="text", body="секрет")
    found = facts(env, projects=("web",))
    assert (found["working"], found["waiting_you"], found["queued"], found["failed"], found["stale"]) == (0, 0, 0, 0, 0)
    assert found["questions"] == [] and found["failures"] == []
    assert set(found["cards"].values()) == {0}
    assert found["last_action"] is None and found["heartbeat"] == "" and found["state"] == "free"


def test_the_agent_chat_counts_only_with_the_chat_right(env):
    thread = env.store.agent_thread("dev")
    found = env.store.session_for(thread, "dev")
    env.store.session_update(found["id"], state="working", heartbeat=NOW)
    assert facts(env, spaces=("chat",))["working"] == 1
    assert facts(env, spaces=())["working"] == 0


def test_monitor_counts_only_for_who_may_read_it(env):
    thread = env.store.agent_thread("dev", "monitor")
    found = env.store.session_for(thread, "dev")
    env.store.session_update(found["id"], state="working", heartbeat=NOW)
    assert facts(env, spaces=("chat", "monitor"))["working"] == 1
    assert facts(env, spaces=("chat",))["working"] == 0


def test_a_failure_on_a_done_card_or_a_card_handed_on_is_not_the_agents_error(env):
    done = env.card(status="In Progress")
    command(env, done, state="failed", error="exit 1", reason="exit")
    assert facts(env)["state"] == "error"
    env.store.edit_card(done.id, None, {"status": "Done"}, actor="user:owner")
    assert (facts(env)["state"], facts(env)["failed"]) == ("free", 0)
    handed = env.card(status="In Progress", title="Handed")
    command(env, handed, n=2, state="failed", error="exit 1", reason="exit")
    env.store.edit_card(handed.id, None, {"assignee": "@analyst"}, actor="user:owner")
    assert (facts(env)["state"], facts(env)["failed"]) == ("free", 0)


def test_a_request_handed_to_the_host_and_not_started_is_queued(env):
    card = env.card(status="To Do")
    command(env, card, state="delivered")
    found = facts(env)
    assert (found["queued"], found["state"]) == (1, "queued")


def test_hidden_rows_do_not_crowd_out_what_the_person_sees(env):
    seen = env.card(project="web", status="In Progress", title="Seen")
    env.store.add_message(seen.id, author="agent:dev", kind="text", body="видно")
    command(env, seen, state="failed", error="exit 1", reason="exit")
    hidden = env.card(project="digipaddock", status="In Progress", title="Hidden")
    for n in range(205):
        env.store.add_message(hidden.id, author="agent:dev", kind="text", body=f"скрыто {n}")
    for n in range(2, 1003):
        env.store.enqueue({"key": f"t:h:{n}", "agent": "dev", "thread": hidden.thread, "kind": "message", "text": "x"})
    found = facts(env, projects=("web",))
    assert found["last_action"]["text"] == "видно"
    assert found["failed"] == 1 and found["state"] == "error"


def test_a_thread_at_work_is_not_also_queued(env):
    card = env.card(status="In Progress")
    command(env, card, state="delivered")  # the next request, handed over while its session works
    session(env, card, "working")
    found = facts(env)
    assert (found["working"], found["queued"], found["state"]) == (1, 0, "working")


def test_the_card_page_confirms_work_by_the_same_threshold():
    """the card's indicator (RunState.svelte) and the Overview call a heartbeat fresh alike."""
    page = Path(__file__).resolve().parents[1] / "cabinet" / "web" / "src" / "components" / "RunState.svelte"
    assert f"FRESH_MS = {team.FRESH * 1000:_}" in page.read_text(encoding="utf-8")
