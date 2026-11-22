"""the cabinet socket API — principals and typed board operations."""

import sys
from pathlib import Path

import pytest

from kd.cabinet import api

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cabinet_env import CabinetEnv  # noqa: E402


@pytest.fixture
def env(tmp_path):
    return CabinetEnv(tmp_path)


def test_principals(env):
    assert env.principal("root").kind == "root"
    assert env.principal("digi").kind == "executor"
    assert env.principal("digi-dev") == api.Principal("agent", 2001, "digi-dev", "dev")
    assert env.principal("nobody") is None


def test_agent_user_reads_only(env):
    card = env.card()
    assert (
        env.call("digi-dev", {"op": "card.get", "card": card.id})["card"]["id"]
        == card.id
    )
    with pytest.raises(api.ApiError) as exc:
        env.call(
            "digi-dev",
            {
                "op": "worker.claim",
                "agent": "dev",
                "card": card.id,
                "revision": card.revision,
            },
        )
    assert exc.value.kind == "refused"


def test_executor_may_not_speak_for_boss_in_worker_ops_nor_for_dev_in_boss_ops(env):
    card = env.card()
    for request in (
        {"op": "worker.claim", "agent": "boss", "card": card.id, "revision": 1},
        {
            "op": "boss.comment",
            "agent": "dev",
            "task": card.id,
            "revision": 1,
            "reason": "x",
        },
    ):
        with pytest.raises(api.ApiError) as exc:
            env.call("digi", request)
        assert exc.value.kind == "refused"


def test_claim_is_evaluated_under_the_lock_and_conflicts_on_a_stale_revision(env):
    card = env.card()
    env.call(
        "digi",
        {
            "op": "boss.assign",
            "agent": "boss",
            "task": card.id,
            "revision": card.revision,
            "reason": "r",
            "assignee": "@sysadmin",
        },
    )
    with pytest.raises(api.ApiError) as exc:
        env.call(
            "digi",
            {
                "op": "worker.claim",
                "agent": "dev",
                "card": card.id,
                "revision": card.revision,
            },
        )
    assert exc.value.kind == "conflict" and env.store.card(card.id).status == "To Do"


def test_an_agents_board_writes_are_rate_limited(env, monkeypatch):
    """Global review 15.10.2026: the coordinator's limits for every agent — no flood of the feed, no churn."""
    monkeypatch.setattr(api, "AGENT_WRITES_PER_HOUR", 3)
    monkeypatch.setattr(api, "AGENT_CREATES_PER_DAY", 2)
    card = env.card(status="In Progress")
    for n in range(3):
        env.call("digi", {"op": "worker.edit", "agent": "dev", "card": card.id, "comment": f"step {n}"})
    with pytest.raises(api.ApiError) as exc:
        env.call("digi", {"op": "worker.edit", "agent": "dev", "card": card.id, "comment": "step 4"})
    assert "per hour" in str(exc.value)
    # another agent has its own count; the hour passes and the writes resume
    env.call("digi", {"op": "worker.create", "agent": "sysadmin", "title": "A", "project": "admin"})
    env.now = "2026-10-14T11:30:00Z"
    env.call("digi", {"op": "worker.edit", "agent": "dev", "card": card.id, "comment": "step 5"})
    env.call("digi", {"op": "worker.create", "agent": "sysadmin", "title": "B", "project": "admin"})
    with pytest.raises(api.ApiError) as exc:
        env.call("digi", {"op": "worker.create", "agent": "sysadmin", "title": "C", "project": "admin"})
    assert "per day" in str(exc.value)


def test_worker_edit_and_handoff_and_create(env):
    card = env.card(status="In Progress")
    out = env.call(
        "digi",
        {
            "op": "worker.edit",
            "agent": "dev",
            "card": card.id,
            "status": "Review",
            "final_summary": "done",
            "check_ac": [],
            "comment": "see PR",
        },
    )
    assert (
        out["changed"]
        and out["card"]["status"] == "Review"
        and out["card"]["summary"] == "[dev] done"
    )
    assert env.store.messages(card.id)[-1]["body"] == "[dev] see PR"
    env.call(
        "digi",
        {
            "op": "worker.handoff",
            "agent": "dev",
            "card": card.id,
            "to": "@boss",
            "comment": "yours",
        },
    )
    assert env.store.card(card.id).assignee == "@boss"
    created = env.call(
        "digi",
        {
            "op": "worker.create",
            "agent": "sysadmin",
            "title": "New",
            "project": "admin",
            "label": ["server:node-c"],
        },
    )
    assert (
        created["card"]["status"] == "Inbox"
        and created["card"]["created_by"] == "@sysadmin"
    )


def test_promote_only_for_the_cabinet_or_root(env):
    card = env.card(status="Inbox", assignee="@dev")
    with pytest.raises(api.ApiError):
        env.call(
            "digi",
            {"op": "card.promote", "card": card.id, "revision": 1, "reason": "go"},
        )
    out = env.call(
        "digi-cabinet",
        {"op": "card.promote", "card": card.id, "revision": 1, "reason": "go"},
    )
    assert (
        out["card"]["status"] == "To Do"
        and env.store.messages(card.id)[-1]["author"] == "user:owner"
    )


def test_promote_names_the_person_who_decided(env):
    # the decision's person (a partner may hold decide.promote) is the edit's actor and the comment's
    # author
    card = env.card(status="Inbox", assignee="@dev")
    env.call("digi-cabinet", {"op": "card.promote", "card": card.id, "revision": 1, "reason": "go", "by": "user:anna"})
    message = env.store.messages(card.id)[-1]
    assert (message["author"], message["body"]) == ("user:anna", "[anna] To Do: go")
    assert [e["actor"] for e in env.store.events_since(0) if e["kind"] == "card.changed"][-1] == "user:anna"
    for bad in ("anna", "user:", "user:Anna", "agent:dev", 7):
        with pytest.raises(api.ApiError) as caught:
            env.call("digi-cabinet", {"op": "card.promote", "card": card.id, "revision": 2, "reason": "go", "by": bad})
        assert caught.value.kind == "invalid"


def test_promote_of_a_proposal_says_whose_proposal_it_was(env):
    # the approver signs the move, the reason is the proposer's — the comment says both
    card = env.card(status="Inbox", assignee="@dev")
    env.call("digi-cabinet", {"op": "card.promote", "card": card.id, "revision": 1, "reason": "готово к работе",
                              "proposed_by": "@boss"})
    message = env.store.messages(card.id)[-1]
    assert (message["author"], message["body"]) == (
        "user:owner", "[owner] To Do: одобрено предложение @boss, исполнитель @dev. Основание @boss: готово к работе")
    for bad in ("@dev", "boss", "", 7):
        with pytest.raises(api.ApiError) as caught:
            env.call("digi-cabinet", {"op": "card.promote", "card": card.id, "revision": 2, "reason": "go",
                                      "proposed_by": bad})
        assert caught.value.kind == "invalid"


def test_unblock_pins_dependencies(env):
    dep = env.card(status="Done")
    card = env.card(status="Blocked", dependencies=[dep.id])
    out = env.call(
        "digi",
        {
            "op": "boss.unblock",
            "agent": "boss",
            "task": card.id,
            "revision": card.revision,
            "reason": "r",
        },
    )
    assert out["card"]["status"] == "To Do"


def test_root_state_events_and_acks(env):
    env.card()
    assert (
        env.call("root", {"op": "events.since", "seq": 0})["events"][0]["kind"]
        == "card.created"
    )
    env.call("root", {"op": "state.set", "key": "sync", "value": {"seq": 1}})
    assert env.call("root", {"op": "state.get", "key": "sync"})["value"] == {"seq": 1}
    env.call(
        "root",
        {"op": "attention.open.set", "events": [{"id": "a" * 32, "state": "pending"}]},
    )
    env.call("digi", {"op": "attention.ack", "agent": "boss", "event": "a" * 32, "action": "owner", "next_check": 30, "reason": "asked the owner"})
    assert [a["event"] for a in env.call("root", {"op": "attention.acks.take"})["events"]] == ["a" * 32]
    with pytest.raises(api.ApiError):
        env.call("digi", {"op": "state.get", "key": "sync"})


def test_unknown_op_and_missing_card(env):
    for request, kind in (
        ({"op": "card.drop"}, "invalid"),
        ({"op": "card.get", "card": "KD-99"}, "not_found"),
    ):
        with pytest.raises(api.ApiError) as exc:
            env.call("root", request)
        assert exc.value.kind == kind


def test_an_agent_user_speaking_for_another_agent_is_refused_before_the_card_is_read(
    env,
):
    # Review focus 2: digi-dev names sysadmin (or boss); the refusal comes before any card lookup.
    for request in (
        {"op": "worker.claim", "agent": "sysadmin", "card": "KD-99", "revision": 1},
        {
            "op": "boss.assign",
            "agent": "boss",
            "task": "KD-99",
            "revision": 1,
            "reason": "r",
            "assignee": "@dev",
        },
        {
            "op": "card.comment",
            "agent": "dev",
            "card": "KD-99",
            "text": "x",
            "author": "@dev",
        },
    ):
        with pytest.raises(api.ApiError) as exc:
            env.call("digi-dev", request)
        assert exc.value.kind == "refused"


def test_executor_must_name_a_registered_agent_with_its_own_user(env):
    card = env.card()
    for agent in ("ghost", "", None, "Dev"):
        with pytest.raises(api.ApiError) as exc:
            env.call(
                "digi",
                {"op": "worker.claim", "agent": agent, "card": card.id, "revision": 1},
            )
        assert exc.value.kind == "refused"


def test_card_list_is_compact_and_filtered(env):
    env.card(title="A", labels=["x"], servers=["node-c"])
    env.card(title="B", status="Review", assignee="@sysadmin")
    cards = env.call("digi-dev", {"op": "card.list", "status": "Review"})["cards"]
    assert [c["title"] for c in cards] == ["B"] and "description" not in cards[0]
    first = env.call("digi-dev", {"op": "card.list", "label": "x"})["cards"][0]
    assert (
        first["servers"] == ["node-c"]
        and first["revision"] == 1
        and first["dependencies"] == []
    )
    assert [
        c["title"]
        for c in env.call("root", {"op": "card.search", "text": "b"})["cards"]
    ] == ["B"]


def test_card_search_finds_a_card_by_its_id_first(env):
    """a query that is a card id returns that card, then the cards mentioning it."""
    cards = [env.card(title=f"card {i}") for i in range(1, 13)]
    follow_up = env.card(title=f"follow-up of {cards[0].id}")

    def search(text):
        return [c["id"] for c in env.call("root", {"op": "card.search", "text": text})["cards"]]

    assert search(cards[0].id) == [cards[0].id, follow_up.id]
    assert search(f"  {cards[0].id.lower()} ") == [cards[0].id, follow_up.id]
    assert search(cards[11].id) == [cards[11].id]
    assert search("KD-999") == []
    assert search("XX-1") == []
    # a bare number or #number is the card of this prefix too (owner, 19.10: «228» found nothing)
    assert search("12") == [cards[11].id]
    assert search(" #12 ") == [cards[11].id]
    assert search("1")[0] == cards[0].id and search("1").count(cards[0].id) == 1
    assert search("999") == []


def test_comment_is_signed_by_its_author(env):
    card = env.card()
    env.call(
        "digi",
        {
            "op": "card.comment",
            "agent": "boss",
            "card": card.id,
            "text": "directive x",
            "author": "@boss",
        },
    )
    env.call(
        "digi",
        {
            "op": "card.comment",
            "agent": "dev",
            "card": card.id,
            "text": "reply",
            "author": "@dev",
        },
    )
    messages = env.call("digi-dev", {"op": "thread.messages", "card": card.id})[
        "messages"
    ]
    assert [(m["author"], m["body"]) for m in messages] == [
        ("agent:boss", "[boss] directive x"),
        ("agent:dev", "[dev] reply"),
    ]
    with pytest.raises(api.ApiError) as exc:
        env.call(
            "digi",
            {
                "op": "card.comment",
                "agent": "dev",
                "card": card.id,
                "text": "x",
                "author": "@sysadmin",
            },
        )
    assert exc.value.kind == "refused"


def test_attention_open_events_for_boss(env):
    env.call(
        "root",
        {
            "op": "attention.open.set",
            "events": [{"id": "a" * 32, "rule": "stale", "subject": "KD-1"}],
        },
    )
    assert (
        env.call("digi", {"op": "attention.open.get", "agent": "boss"})["events"][0][
            "rule"
        ]
        == "stale"
    )
    for request in (
        {"op": "attention.open.get", "agent": "dev"},
        {"op": "attention.ack", "agent": "boss", "event": "x"},
    ):
        with pytest.raises(api.ApiError):
            env.call("digi", request)


def test_attention_ack_needs_an_open_event_and_is_idempotent(env):
    env.call(
        "root",
        {"op": "attention.open.set", "events": [{"id": "a" * 32, "state": "pending"}]},
    )
    ack = {"op": "attention.ack", "agent": "boss", "event": "a" * 32, "action": "owner", "next_check": 30, "reason": "asked the owner"}
    assert (
        env.call("digi", ack)["already"] is False
        and env.call("digi", ack)["already"] is True
    )
    assert (
        env.call("digi", {"op": "attention.open.get", "agent": "boss"})["events"][0][
            "state"
        ]
        == "acknowledged"
    )
    with pytest.raises(api.ApiError) as exc:
        env.call("digi", {**ack, "event": "b" * 32})
    assert exc.value.kind == "refused"


def test_boss_promote_needs_the_owners_grant_and_records_it(env):
    card = env.card(status="Inbox", assignee="@dev")
    promote = {
        "op": "boss.promote",
        "agent": "boss",
        "task": card.id,
        "revision": card.revision,
        "reason": "r",
    }
    for grant in (None, "x", "A" * 32):
        with pytest.raises(api.ApiError) as exc:
            env.call("digi", {**promote, **({"grant": grant} if grant else {})})
        assert exc.value.kind == "refused"
    assert env.call("digi", {**promote, "grant": "c" * 32})["card"]["status"] == "To Do"
    changed = [e for e in env.store.events_since(0) if e["kind"] == "card.changed"]
    assert [(e["actor"], e["payload"]["grant"]) for e in changed] == [
        ("agent:boss", "c" * 32)
    ]


def test_the_cabinet_may_post_only_a_boss_comment(env):
    card = env.card()
    env.call(
        "digi-cabinet",
        {
            "op": "boss.comment",
            "task": card.id,
            "revision": card.revision,
            "reason": "разрешено",
        },
    )
    assert env.store.messages(card.id)[0]["author"] == "agent:boss"
    with pytest.raises(api.ApiError) as exc:
        env.call(
            "digi-cabinet",
            {
                "op": "boss.assign",
                "task": card.id,
                "revision": 2,
                "reason": "r",
                "assignee": "@dev",
            },
        )
    assert exc.value.kind == "refused"


def test_the_retired_approval_user_is_nobody(env):
    # the cabinet took over the approval pages; digi-approve is removed and its uid means nothing
    names = {1002: "digi-approve", 1003: "digi-cabinet"}
    lookup = lambda uid: type("pw", (), {"pw_name": names[uid]})()  # noqa: E731
    assert api.principal_of(1002, getpwuid=lookup, registry=str(env.registry), base="digi") is None
    assert api.principal_of(1003, getpwuid=lookup, registry=str(env.registry), base="digi").kind == "cabinet"
    assert "approve" not in api.KINDS


def test_the_push_sender_holds_only_the_push_queue(env):
    """Global review 15.10.2026: the one cabinet process with network (the push sender, its own user) can take
    and report the queue — and nothing of the owner's authority: no approve, promote, state or projects."""
    names = {1004: "digi-push"}
    lookup = lambda uid: type("pw", (), {"pw_name": names[uid]})()  # noqa: E731
    assert api.principal_of(1004, getpwuid=lookup, registry=str(env.registry), base="digi").kind == "push"
    assert env.call("digi-push", {"op": "push.take", "limit": 5})["items"] == []
    for request in (
        {"op": "decision.approve", "ids": ["x"], "user": "owner", "revisions": {}, "signature": {"id": "x"}},
        {"op": "card.promote", "card": "KD-1", "revision": 1, "reason": "go"},
        {"op": "state.set", "key": "x", "value": {}},
        {"op": "attention.open.set", "events": []},
        {"op": "decision.reject", "id": "x", "user": "owner", "reason": "no"},
    ):
        with pytest.raises(api.ApiError) as exc:
            env.call("digi-push", request)
        assert exc.value.kind == "refused", request["op"]


# --- a worker's edit is checked against the revision it read -------------------------
def _draft(env):
    card = env.card(status="Inbox", assignee="@analyst")  # revision 1
    env.store.edit_card(card.id, 1, {"description": "текст владельца"}, actor="user:owner")  # revision 2
    return card


def test_a_stale_worker_edit_does_not_overwrite_the_owners_text(env):
    card = _draft(env)
    with pytest.raises(api.ApiError) as exc:
        env.call("digi", {"op": "worker.edit", "agent": "analyst", "card": card.id, "revision": 1,
                         "description": "старый черновик"})
    assert exc.value.kind == "conflict" and "kd-task view" in str(exc.value)
    assert env.store.card(card.id).description == "текст владельца"


def test_a_scope_change_needs_the_revision(env):
    card = _draft(env)
    for change in ({"description": "черновик"}, {"ac": ["проверка"]}, {"remove_ac": [1]}):
        with pytest.raises(api.ApiError) as exc:
            env.call("digi", {"op": "worker.edit", "agent": "analyst", "card": card.id, **change})
        assert exc.value.kind == "invalid" and "--revision" in str(exc.value)
    out = env.call("digi", {"op": "worker.edit", "agent": "analyst", "card": card.id, "comment": "читаю"})
    assert out["card"]["revision"] == 3  # a comment needs no revision


def test_a_current_revision_is_accepted(env):
    card = _draft(env)
    out = env.call("digi", {"op": "worker.edit", "agent": "analyst", "card": card.id, "revision": 2,
                           "description": "ТЗ"})
    assert (out["card"]["revision"], out["card"]["description"]) == (3, "ТЗ")


def test_a_stale_handoff_is_a_conflict_and_a_current_one_passes(env):
    card = _draft(env)
    with pytest.raises(api.ApiError) as exc:
        env.call("digi", {"op": "worker.handoff", "agent": "analyst", "card": card.id, "revision": 1, "to": "@dev"})
    assert exc.value.kind == "conflict"
    assert env.store.card(card.id).assignee == "@analyst"
    for bad in (True, "2"):
        with pytest.raises(api.ApiError) as exc:
            env.call("digi", {"op": "worker.handoff", "agent": "analyst", "card": card.id, "revision": bad, "to": "@dev"})
        assert exc.value.kind == "invalid"
    env.call("digi", {"op": "worker.handoff", "agent": "analyst", "card": card.id, "revision": 2, "to": "@dev"})
    assert env.store.card(card.id).assignee == "@dev"


# --- a question opens as text or as a form v1 ------------------------------------------
def _ask(env, card, payload):
    return env.call(
        "digi", {"op": "decision.open", "type": "question", "agent": "dev", "card": card.id, "payload": payload}
    )["decision"]


def test_an_agent_opens_a_form_question(env):
    from kd.cabinet import questions
    from question_samples import form

    card = env.card(status="Inbox")
    decision = _ask(env, card, form())
    assert decision["payload"] == questions.validate_form(form())
    assert decision["payload"]["questions"][1] == {
        "id": "details", "title": "Что ещё учесть?", "context": "", "type": "text",
        "required": False, "allow_custom": False, "options": [],
    }
    assert env.store.messages(card.id)[-1]["body"] == "Уточнения по проверке"
    assert _ask(env, card, form())["id"] == decision["id"]  # asked again: the same open question


def test_an_invalid_form_is_refused_before_any_write(env):
    from question_samples import form

    card = env.card(status="Inbox")
    start, messages = env.store.last_seq(), len(env.store.messages(card.id))
    bad = form()
    bad["questions"][0]["id"] = "Method"
    with pytest.raises(api.ApiError) as exc:
        _ask(env, card, bad)
    assert exc.value.kind == "invalid" and "questions[0].id" in str(exc.value)
    assert env.store.last_seq() == start and len(env.store.messages(card.id)) == messages
    assert env.call("root", {"op": "decision.list"})["decisions"] == []


def test_a_text_question_still_opens(env):
    card = env.card(status="Inbox")
    assert _ask(env, card, {"text": " Ок? "})["payload"] == {"text": "Ок?"}
    with pytest.raises(api.ApiError) as exc:
        _ask(env, card, {"text": "Ок?", "extra": 1})
    assert exc.value.kind == "invalid"


# --- C03: an ack names the action taken and when to look again ----------------------------
ACK = {"op": "attention.ack", "agent": "boss", "event": "a" * 32}


def _event_on(env, subject, created="2026-10-14T09:00:00Z"):
    env.call("root", {"op": "attention.open.set",
                      "events": [{"id": "a" * 32, "state": "pending", "rule": "blocked", "subject": subject,
                                  "created": created}]})


def _decision(env, decision_id):
    with env.store._tx():
        env.store._conn.execute(
            "INSERT INTO decisions (id, type, card, payload_sha, requested_by, decide_permission, step_up, created)"
            " VALUES (?,?,?,?,?,?,?,?)", (decision_id, "question", "KD-1", "x", "agent:dev", "decide", 0,
                                          "2026-10-22T08:00:00Z"))


def _kind(env, request):
    with pytest.raises(api.ApiError) as exc:
        env.call("digi", request)
    return exc.value.kind


def test_ack_needs_action_and_next_check(env):
    card = env.card()
    _event_on(env, card.id)
    full = {"action": "owner", "next_check": 10, "reason": "asked the owner in the chat"}
    for drop in ("action", "next_check", "reason"):
        assert _kind(env, {**ACK, **{k: v for k, v in full.items() if k != drop}}) == "invalid"
    for bad in ({"action": "edit:3", "next_check": 31}, {"action": "directive:" + "e" * 16, "next_check": 60},
                {"action": "owner", "next_check": 4}, {"action": "owner", "next_check": 1441},
                {"action": "owner", "next_check": "10"}, {"action": "owner", "next_check": True},
                {"action": "nudge"}, {"action": "card:KD-0"}, {"reason": "two\nlines"}, {"reason": " "},
                {"reason": "x" * 301}):
        assert _kind(env, {**ACK, **full, **bad}) == "invalid", bad
    decision = "d" * 32
    _decision(env, decision)
    answer = env.call("digi", {**ACK, "action": f"decision:{decision}", "next_check": 120,
                              "reason": "waits for the owner's decision"})
    assert answer["already"] is False
    assert env.call("root", {"op": "attention.acks.take"})["events"] == [
        {"event": "a" * 32, "action": f"decision:{decision}", "next_check": 120,
         "reason": "waits for the owner's decision"}]


def test_ack_refers_to_something_that_exists(env):
    card = env.card()
    _event_on(env, card.id)
    base = {**ACK, "next_check": 20, "reason": "checked"}
    for action in ("assist:" + "c" * 32, "decision:" + "d" * 32, "wait:" + "b" * 32, "job:" + "f" * 32,
                   "card:KD-99", "edit:1"):
        assert _kind(env, {**base, "action": action}) == "refused", action
    assert env.call("digi", {**base, "action": f"card:{card.id}"})["already"] is False


def test_ack_edit_needs_boss_own_write_at_that_revision(env):
    card = env.card()
    _event_on(env, card.id)
    base = {**ACK, "next_check": 15, "reason": "re-routed the card"}
    owner = env.store.edit_card(card.id, None, {"title": "by the owner"}, actor="user:owner")
    assert _kind(env, {**base, "action": f"edit:{owner.revision}"}) == "refused"
    boss = env.store.edit_card(card.id, None, {"title": "by boss"}, actor="agent:boss")
    assert _kind(env, {**base, "action": f"edit:{boss.revision + 1}"}) == "refused"
    later = env.store.edit_card(card.id, None, {"title": "by boss again"}, actor="agent:boss")
    assert _kind(env, {**base, "action": f"edit:{owner.revision}"}) == "refused"  # exact, not "at or after"
    assert env.call("digi", {**base, "action": f"edit:{boss.revision}"})["already"] is False
    assert later.revision == boss.revision + 1


def test_ack_edit_must_be_newer_than_the_event(env):
    card = env.card()
    boss = env.store.edit_card(card.id, None, {"title": "by boss"}, actor="agent:boss")
    _event_on(env, card.id, created="2099-01-01T00:00:00Z")
    base = {**ACK, "next_check": 15, "reason": "re-routed the card"}
    assert _kind(env, {**base, "action": f"edit:{boss.revision}"}) == "refused"


def test_a_card_typed_with_an_alias_of_the_host_is_the_card(env):
    # the fixture host's earlier prefix (OLD, naming.json card_aliases) names the same card
    card = env.card()
    old = "OLD-" + card.id.split("-")[1]
    assert env.call("root", {"op": "card.get", "card": old})["card"]["id"] == card.id
    assert [c["id"] for c in env.call("root", {"op": "card.search", "text": old.lower()})["cards"]][:1] == [card.id]
