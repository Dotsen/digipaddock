"""BOSS hands a card whose work is checked to the owner's acceptance — Review, never Done."""

import pytest
from cabinet_env import CabinetEnv

from kd.cabinet import api, assists


@pytest.fixture
def env(tmp_path):
    env = CabinetEnv(tmp_path)
    env.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    return env


def _report(env, card, author="agent:sysadmin", body="прогон прошёл, проверка OK"):
    return env.store.add_message(card.id, author=author, kind="text", body=body)["seq"]


def _review(env, card, seq, **over):
    request = {
        "op": "boss.review",
        "agent": "boss",
        "task": card.id,
        "revision": env.store.card(card.id).revision,
        "reason": "работа проверена",
        "source": {"card": card.id, "seq": seq},
        **over,
    }
    return env.call("digi", request)


def _refused(env, card, seq, text, **over):
    with pytest.raises(api.ApiError) as exc:
        _review(env, card, seq, **over)
    assert text in str(exc.value)
    assert env.store.card(card.id).status != "Review"
    return exc.value


def test_boss_moves_its_own_checked_card_to_review_with_a_note(env):
    card = env.card(status="In Progress", assignee="@boss")
    seq = _report(env, card)
    found = _review(env, card, seq)["card"]
    after = env.store.card(card.id)
    assert found["status"] == after.status == "Review"
    note = env.store.messages(card.id)[-1]
    assert note["author"] == "agent:boss"
    assert (
        "работа проверена" in note["body"]
        and f"#{seq}" in note["body"]
        and "@sysadmin" in note["body"]
    )


def test_bosss_own_message_is_a_source_too(env):
    card = env.card(status="In Progress", assignee="@boss")
    seq = _report(
        env, card, author="agent:boss", body="по мониторингу: инцидентов нет, юнит в ok"
    )
    assert _review(env, card, seq)["card"]["status"] == "Review"


def test_another_agents_card_nobody_leads_goes_to_review(env):
    card = env.card(status="In Progress", assignee="@sysadmin")
    assert _review(env, card, _report(env, card))["card"]["status"] == "Review"


def test_an_unassigned_card_in_work_goes_to_review(env):
    card = env.card(status="In Progress", assignee="")
    assert _review(env, card, _report(env, card))["card"]["status"] == "Review"


def test_a_card_the_agent_works_on_is_refused(env):
    card = env.card(status="In Progress", assignee="@dev")
    seq = _report(env, card)
    env.store.enqueue(
        {
            "key": "w1",
            "agent": "dev",
            "thread": card.thread,
            "card": card.id,
            "kind": "message",
            "text": "x",
            "initiator": "user:owner",
        }
    )
    _refused(env, card, seq, "@dev works on it")


def test_a_card_whose_agent_waits_for_the_owner_is_refused(env):
    card = env.card(status="In Progress", assignee="@dev")
    seq = _report(env, card)
    env.call(
        "digi",
        {
            "op": "decision.open",
            "type": "question",
            "agent": "dev",
            "card": card.id,
            "payload": {"text": "какой вариант?"},
        },
    )
    _refused(env, card, seq, "question decision is open")


def test_the_owners_card_is_his(env):
    card = env.card(status="In Progress", assignee="@owner")
    _refused(env, card, _report(env, card), "held by @owner")


@pytest.mark.parametrize("status", ["To Do", "Inbox", "Review", "Done"])
def test_review_is_from_in_progress_or_blocked_only(env, status):
    card = env.card(status=status, assignee="@boss")
    seq = _report(env, card)
    with pytest.raises(api.ApiError) as exc:
        _review(env, card, seq)
    assert exc.value.kind == "refused" and "In Progress or Blocked" in str(exc.value)


def test_blocked_goes_to_review_only_with_every_dependency_done(env):
    dep = env.card(status="In Progress", assignee="@dev", title="Зависимость")
    card = env.card(status="Blocked", assignee="@boss", dependencies=[dep.id])
    seq = _report(env, card)
    _refused(env, card, seq, f"waits for {dep.id}")
    env.store.edit_card(dep.id, None, {"status": "Done"}, actor="user:owner")
    assert _review(env, card, seq)["card"]["status"] == "Review"


def test_an_open_pr_is_refused(env):
    card = env.card(status="In Progress", assignee="@boss")
    seq = _report(env, card)
    env.store.pr_put(
        {
            "repo": "o/r",
            "number": 7,
            "card": card.id,
            "project": "digipaddock",
            "url": "u",
            "title": f"{card.id}: x",
            "head": "a" * 40,
            "state": "open",
            "files": [],
            "cage": [],
            "diff": "",
            "truncated": False,
            "release": False,
        },
        actor="system",
    )
    _refused(env, card, seq, "PR #7 is not merged")


def test_open_help_is_refused(env, monkeypatch):
    card = env.card(status="In Progress", assignee="@boss")
    seq = _report(env, card)
    monkeypatch.setattr(
        assists, "open_for", lambda store, card_id, lead=None: [{"id": "ab" * 16}]
    )
    _refused(env, card, seq, "open help abababab")


def test_unchecked_acceptance_criteria_are_refused(env):
    card = env.card(
        status="In Progress",
        assignee="@boss",
        acceptance=[
            {"text": "есть отчёт", "checked": True},
            {"text": "есть копия", "checked": False},
        ],
    )
    _refused(env, card, _report(env, card), "acceptance criteria 2 are not checked")


def test_an_aggregate_with_open_stages_is_refused(env, monkeypatch):
    from kd.cabinet import stages

    card = env.card(status="In Progress", assignee="@boss")
    seq = _report(env, card)
    monkeypatch.setattr(stages, "open_children", lambda store, card_id: ["KD-77"])
    _refused(env, card, seq, "open stages KD-77")


@pytest.mark.parametrize(
    "source, text",
    [
        ({"card": "KD-999", "seq": 1}, "a message on"),
        ({"chat": 1}, "source is"),
        ("own", "source is"),
    ],
)
def test_the_source_is_a_message_on_the_card_itself(env, source, text):
    card = env.card(status="In Progress", assignee="@boss")
    _report(env, card)
    _refused(env, card, 1, text, source=source)


def test_a_missing_message_or_a_notice_is_no_source(env):
    card = env.card(status="In Progress", assignee="@boss")
    _refused(env, card, 99, "has no message #99")
    notice = env.store.add_message(
        card.id, author="system:pipeline", kind="text", body="ход закончился"
    )["seq"]
    _refused(env, card, notice, "notice of the cabinet")


def test_a_stale_revision_is_a_conflict(env):
    card = env.card(status="In Progress", assignee="@boss")
    seq = _report(env, card)
    error = _refused(env, card, seq, "not current", revision=card.revision - 1)
    assert error.kind == "conflict"


def test_only_boss_moves_to_review_here(env):
    card = env.card(status="In Progress", assignee="@boss")
    seq = _report(env, card)
    error = _refused(env, card, seq, "may not do this", agent="dev")
    assert error.kind == "refused"


def test_bosss_open_waits_on_its_own_card_end_with_the_move(env):
    card = env.card(status="In Progress", assignee="@boss")
    seq = _report(env, card)
    env.store._conn.execute(
        "INSERT INTO waits (id, card, agent, command, kind, ref, blocking, mode, state, created, updated) "
        "VALUES ('w1', ?, 'boss', 'c1', 'card', 'KD-5', 1, 'all', 'open', 't', 't')",
        (card.id,),
    )
    _review(env, card, seq)
    state = env.store._conn.execute(
        "SELECT state, reason FROM waits WHERE id='w1'"
    ).fetchone()
    assert tuple(state) == ("cancelled", "review")
