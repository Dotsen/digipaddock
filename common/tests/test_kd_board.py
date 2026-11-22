import sys
from pathlib import Path

import pytest

from kd import board
from kd.cabinet import client as cabinet_client

sys.path.insert(0, str(Path(__file__).resolve().parent))  # cabinet_env
from cabinet_env import CabinetEnv  # noqa: E402 — the board is the cabinet


def ev(old, new):
    """The changes between two {id: {title, status, assignees}} views — the shape ``changes_from`` gives."""
    events = []
    for tid in sorted(set(old) | set(new), key=lambda t: (len(t), t)):
        o, n = old.get(tid), new.get(tid)
        if n and not o:
            events.append({"kind": "new", "id": tid, **n, "old_status": None, "old_assignees": []})
        elif o and not n:
            events.append({"kind": "gone", "id": tid, **o, "old_status": o["status"], "old_assignees": o["assignees"]})
        elif o["status"] != n["status"] or o["assignees"] != n["assignees"]:
            kind = "status" if o["status"] != n["status"] else "assignee"
            events.append({"kind": kind, "id": tid, **n, "old_status": o["status"], "old_assignees": o["assignees"]})
    return events


def wakes(events):
    """(agent, text) of ``wake_items`` — what the cabinet turns into commands for the agents."""
    return [(w["agent"], w["text"]) for w in board.wake_items(events)]


@pytest.fixture
def cab(tmp_path):
    return CabinetEnv(tmp_path / "cabinet")


@pytest.fixture
def root(cab):
    return cab.client("root")  # kd-board runs as root


class Down:
    def call(self, request):
        raise cabinet_client.CabinetError("unavailable", "board unavailable")


def test_whole_string_validation_rejects_trailing_newline():
    # "$" in re lets a trailing \n through — names and numbers are checked whole (fullmatch)
    base = {"title": "t", "updated": "u"}
    assert wakes(ev({}, {"KD-7": dict(base, status="To Do", assignees=["sysadmin\n"])})) == []
    assert wakes(ev({}, {"KD-7\n": dict(base, status="To Do", assignees=["sysadmin"])})) == []


def test_wakes_only_agents_in_to_do():
    base = {"title": "t", "updated": "u"}
    events = ev({"KD-1": dict(base, status="Inbox", assignees=["sysadmin"]),
                 "KD-2": dict(base, status="To Do", assignees=["sysadmin"]),
                 "KD-4": dict(base, status="To Do", assignees=["owner"])},
                {"KD-1": dict(base, status="To Do", assignees=["sysadmin"]),      # approved → wake
                 "KD-2": dict(base, status="To Do", assignees=["dev"]),     # reassigned → wake dev
                 "KD-3": dict(base, status="To Do", assignees=["sysadmin"]),      # new → wake
                 "KD-4": dict(base, status="In Progress", assignees=["owner"]),   # the owner is not an agent
                 "KD-5": dict(base, status="Inbox", assignees=["sysadmin"])})     # waits for the word — no wake
    # the wake's text is fixed, without the title: other agents and the web write the title
    assert sorted(wakes(events)) == [("dev", "[board] KD-2 assigned to you"),
                                     ("sysadmin", "[board] KD-1 assigned to you"),
                                     ("sysadmin", "[board] KD-3 assigned to you")]
    assert wakes(ev({}, {"KD-9": dict(base, status="To Do", assignees=["Robert'); DROP"])})) == []
    # a number — only ASCII digits: "١" (the Arabic-Indic one) would pass \d
    assert wakes(ev({}, {"KD-١": dict(base, status="To Do", assignees=["sysadmin"])})) == []


def test_coordinator_events():
    base = {"title": "t", "updated": "u"}
    assert board.coordinator_events(ev({}, {"KD-1": dict(base, status="Inbox", assignees=[])}))
    assert board.coordinator_events(ev({}, {"KD-1": dict(base, status="To Do", assignees=["boss"])}))
    assert board.coordinator_events(ev({"KD-1": dict(base, status="In Progress", assignees=["x"])},
                                       {"KD-1": dict(base, status="Blocked", assignees=["x"])}))
    assert not board.coordinator_events(ev({}, {"KD-1": dict(base, status="To Do", assignees=["x"])}))
    assert not board.coordinator_events(ev({"KD-1": dict(base, status="Blocked", assignees=["x"])},
                                           {"KD-1": dict(base, status="Blocked", assignees=["y"])}))
    assert not board.coordinator_events(ev({"KD-1": dict(base, status="To Do", assignees=["boss"])}, {}))


def test_the_pult_renderers_and_the_digest_are_gone():
    """the cabinet is where people read the board; the Pult hears nothing from kd-board."""
    for name in ("lines", "messages", "digest", "digest_text", "retry_wakes", "queue_wakes", "snapshot", "wakes"):
        assert not hasattr(board, name), name


# --- the cabinet's events ----------------------------------------------------------------------


def test_changes_from_reads_new_cards_status_and_assignee_and_skips_the_rest(cab, root):
    first = cab.card(status="Inbox", assignee="", title="Первая")
    cab.store.edit_card(first.id, None, {"status": "To Do", "assignee": "@dev"}, actor="user:owner")
    cab.store.edit_card(first.id, None, {"assignee": "@sysadmin"}, actor="user:owner")
    cab.store.edit_card(first.id, None, {"comment": {"text": "x", "author": "@owner"}}, actor="user:owner")
    cab.store.edit_card(first.id, None, {"labels_add": ["P-1"], "project": "admin"}, actor="user:owner")
    cab.put("KD-90", status="Review", assignee="@dev", title="Импорт")
    events = root.call({"op": "events.since", "seq": 0, "limit": 500})["events"]
    changes = board.changes_from(events)
    assert [(c["kind"], c["id"], c["status"], c["assignees"], c["old_status"], c["old_assignees"]) for c in changes] == [
        ("new", first.id, "Inbox", [], None, []),
        ("status", first.id, "To Do", ["dev"], "Inbox", []),
        ("assignee", first.id, "To Do", ["sysadmin"], "To Do", ["dev"]),
        ("new", "KD-90", "Review", ["dev"], None, []),
    ]
    assert changes[0]["title"] == "Первая" and changes[-1]["title"] == "Импорт"


def test_a_card_wake_is_a_wake_item_and_not_the_coordinators(cab, root):
    """The owner's «take it into work» on a To Do card (and the brief request to the analyst in Inbox)."""
    card = cab.card(status="To Do", assignee="@dev", title="Кнопка")
    cab.store.wake_card(card.id, actor="user:owner", agent="@dev")
    cab.store.wake_card(card.id, actor="user:owner", agent="@analyst")
    events = root.call({"op": "events.since", "seq": 1, "limit": 500})["events"]
    changes = board.changes_from(events)
    assert [(c["kind"], c["id"], c["assignees"], c["title"]) for c in changes] == [
        ("wake", card.id, ["dev"], "Кнопка"), ("wake", card.id, ["analyst"], "Кнопка")]
    assert wakes(changes) == [("dev", f"[board] {card.id} assigned to you"),
                              ("analyst", f"[board] {card.id} assigned to you")]
    assert not board.coordinator_events(changes)


def test_a_persons_thread_message_wakes_the_agents_it_names_even_the_coordinator(cab, root):
    """The owner writes «@boss …» in a card's thread: BOSS (and any other named agent) is woken with a fixed
    line that says where to read; an agent's own message names nobody."""
    card = cab.card(status="In Progress", assignee="@owner", title="Зонтик")
    cab.store.add_message(card.id, author="user:owner", kind="text", body="@boss запусти @analyst", mentions=["@analyst", "@boss"])
    cab.store.add_message(card.id, author="agent:boss", kind="text", body="[boss] @analyst смотри", mentions=["@analyst"])
    events = root.call({"op": "events.since", "seq": 1, "limit": 500})["events"]
    changes = board.changes_from(events)
    assert [(c["kind"], c["assignees"], c["by"]) for c in changes] == [
        ("message", ["analyst"], "user:owner"), ("message", ["boss"], "user:owner")]
    assert wakes(changes) == [
        ("analyst", f"[board] {card.id}: a message for you in the thread — read it with kd-task view {card.id} and answer there"),
        ("boss", f"[board] {card.id}: a message for you in the thread — read it with kd-task view {card.id} and answer there")]
    assert not board.coordinator_events(changes)


def test_a_handed_over_card_wakes_its_new_agent_outside_inbox_or_when_a_person_gave_it():
    base = {"title": "t", "updated": "u"}
    person = dict(by="user:owner")
    agent = dict(by="agent:boss")
    assert wakes([dict(ev({"KD-1": dict(base, status="In Progress", assignees=["owner"])},
                          {"KD-1": dict(base, status="In Progress", assignees=["analyst"])})[0], **person)]) == [
        ("analyst", "[board] KD-1 assigned to you")]
    assert wakes([dict(ev({"KD-2": dict(base, status="Review", assignees=["dev"])},
                          {"KD-2": dict(base, status="Review", assignees=["sysadmin"])})[0], **agent)]) == [
        ("sysadmin", "[board] KD-2 assigned to you")]
    # the coordinator's routing in Inbox is not work yet; the owner's assignment there is
    inbox = ev({"KD-3": dict(base, status="Inbox", assignees=[])}, {"KD-3": dict(base, status="Inbox", assignees=["dev"])})[0]
    assert wakes([dict(inbox, **agent)]) == [] and wakes([dict(inbox, **person)]) == [("dev", "[board] KD-3 assigned to you")]
    done = ev({"KD-4": dict(base, status="Done", assignees=["dev"])}, {"KD-4": dict(base, status="Done", assignees=["sysadmin"])})[0]
    assert wakes([dict(done, **person)]) == []
    boss = ev({"KD-5": dict(base, status="To Do", assignees=["dev"])}, {"KD-5": dict(base, status="To Do", assignees=["boss"])})[0]
    assert wakes([dict(boss, **person)]) == []  # the coordinator's cards are watch's business


def test_the_coordinators_brief_order_in_inbox_wakes_the_analyst(cab, root):
    """BOSS routes a card to the analyst with ``spec:task-brief`` — the brief is Inbox work, so the analyst is
    woken at once; BOSS routing an Inbox card to its implementer still waits for the owner's word."""
    brief = cab.card(status="Inbox", assignee="@boss", title="ТЗ")
    cab.store.edit_card(brief.id, None, {"assignee": "@analyst", "labels_add": ["spec:task-brief"]}, actor="agent:boss")
    routed = cab.card(status="Inbox", assignee="", title="Маршрут")
    cab.store.edit_card(routed.id, None, {"assignee": "@dev", "labels_add": ["spec:implementer"]}, actor="agent:boss")
    events = root.call({"op": "events.since", "seq": 0, "limit": 500})["events"]
    assert wakes(board.changes_from(events)) == [("analyst", f"[board] {brief.id} assigned to you")]


def test_sync_moves_the_cursor_and_kicks_watch_without_any_sender(cab, root):
    """sync is the cursor and watch's kick; the agents' commands come from the cabinet with the event."""
    card = cab.card(status="Inbox", assignee="")
    cab.store.edit_card(card.id, None, {"status": "To Do", "assignee": "@dev"}, actor="user:owner")
    kicked = []
    assert board.sync(root, on_events=kicked.append) == 0
    assert cab.store.get_state("sync") == {"seq": 2} and len(kicked) == 1
    assert [c["kind"] for c in kicked[0]] == ["new", "status"]
    assert board.sync(root, on_events=kicked.append) == 0 and len(kicked) == 1  # nothing new: quiet
    for gone in ("send", "wake", "hosted", "now"):
        with pytest.raises(TypeError):
            board.sync(root, **{gone: None})


def test_sync_moves_the_cursor_over_events_that_are_not_news(cab, root):
    card = cab.card()
    kicked = []
    assert board.sync(root, on_events=kicked.append) == 0
    cab.store.edit_card(card.id, None, {"comment": {"text": "x", "author": "@owner"}}, actor="user:owner")
    assert board.sync(root, on_events=kicked.append) == 0
    # the comment is two events (its message.added and the card.changed): the cursor passes both
    assert len(kicked) == 1 and cab.store.get_state("sync") == {"seq": cab.store.last_seq()} == {"seq": 3}


def test_sync_reads_the_events_in_batches(cab, root, monkeypatch):
    monkeypatch.setattr(board, "EVENTS_BATCH", 2)
    for _ in range(5):
        cab.card(assignee="")
    kicked = []
    assert board.sync(root, on_events=kicked.append) == 0
    assert cab.store.get_state("sync") == {"seq": 5} and len(kicked) == 3


def test_coordinator_events_go_to_watch(cab, root):
    cab.card(assignee="@boss")
    kicked = []
    assert board.sync(root, on_events=kicked.append) == 0
    assert len(kicked) == 1 and board.coordinator_events(kicked[0])


def test_sync_with_the_cabinet_down_is_1_and_changes_nothing(capsys):
    assert board.sync(Down()) == 1
    assert "кабинет недоступен" in capsys.readouterr().err


def test_an_answered_question_wakes_who_asked():
    """The answer reaches the agent even when its session no longer waits."""
    did = "ab" * 16

    def approved(**payload):
        return [{"seq": 9, "kind": "decision.approved", "card": "KD-39", "actor": "user:owner",
                 "payload": {"decision": did, **payload}}]

    changes = board.changes_from(approved(type="question", requested_by="agent:analyst"))
    assert wakes(changes) == [
        ("analyst", f"[board] KD-39: your question {did} has an answer — read it with kd-work answer {did}")]
    assert board.coordinator_events(changes) is False
    boss = board.changes_from(approved(type="question", requested_by="agent:boss"))
    assert [agent for agent, _ in wakes(boss)] == ["boss"]
    assert wakes(board.changes_from(approved(type="promote", requested_by="agent:boss"))) == []
    assert wakes(board.changes_from(approved(type="question", requested_by="user:anna"))) == []
    assert wakes(board.changes_from(approved(type="question"))) == []


def test_every_change_the_rules_make_is_in_the_wake_table():
    # a change is direct (WAKES: its command and words) or «to work» (the card's status decides)
    d, a = "d" * 32, "a" * 16
    events = [
        {"kind": "card.created", "card": "KD-1", "actor": "user:owner", "payload": {"assignee": "@dev", "status": "To Do"}},
        {"kind": "card.wake", "card": "KD-1", "actor": "user:owner", "payload": {"agent": "@dev"}},
        {"kind": "message.added", "card": "KD-1", "actor": "user:owner", "payload": {"mentions": ["@dev"]}},
        {"kind": "decision.approved", "card": "KD-1", "actor": "user:owner",
         "payload": {"type": "question", "requested_by": "agent:dev", "decision": d}},
        {"kind": "decision.asked", "card": "KD-1", "actor": "user:owner",
         "payload": {"type": "promote", "requested_by": "agent:boss", "decision": d}},
        {"kind": "decision.approved", "card": "KD-1", "actor": "user:owner",
         "payload": {"type": "pr_approve", "requested_by": "agent:dev", "pr": 3, "mode": "merge", "head": "f" * 40}},
        {"kind": "decision.rejected", "card": "KD-1", "actor": "user:owner",
         "payload": {"type": "pr_approve", "requested_by": "agent:dev", "pr": 3}},
        {"kind": "decision.approved", "card": "KD-1", "actor": "user:owner",
         "payload": {"type": "admin_plan", "requested_by": "agent:sysadmin"}},
        {"kind": "decision.approved", "card": "KD-1", "actor": "user:owner",
         "payload": {"type": "admin_critical", "requested_by": "agent:sysadmin", "action": a}},
        {"kind": "card.changed", "card": "KD-1", "actor": "agent:analyst",
         "payload": {"before": {"status": "Inbox", "assignee": "@analyst", "labels": []},
                     "after": {"status": "Inbox", "assignee": "@dev", "labels": ["brief-ready"]}}},
        {"kind": "card.changed", "card": "KD-1", "actor": "user:owner",
         "payload": {"before": {"status": "Review", "assignee": "@dev"}, "after": {"status": "In Progress",
                                                                                  "assignee": "@dev"}}},
        {"kind": "card.changed", "card": "KD-1", "actor": "user:owner",
         "payload": {"before": {"status": "In Progress", "assignee": "@dev"}, "after": {"status": "Blocked",
                                                                                       "assignee": "@dev"}}},
    ]
    changes = board.changes_from(events)
    kinds = [c["kind"] for c in changes]
    assert set(kinds) == set(board.WAKES) | set(board.TO_WORK)
    for change, item in zip(changes, [board.wake_items([c]) for c in changes]):
        if change["kind"] in board.WAKES:  # a direct change is its agent's cue with the table's command
            assert [i["kind"] for i in item] == [board.WAKES[change["kind"]][0]]
            assert not board.coordinator_events([change])
