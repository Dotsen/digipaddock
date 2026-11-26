"""Wakes of one card that come together reach its agent as one turn (KD-9460): the owner's answers on decisions
wait a short window, a word that comes while the agent's turn on the card runs waits for that turn's end, and the
ones that wait go in one delivery naming each; every command still ends — with that turn, or back in the queue."""

import asyncio

import pytest
from cabinet_env import CabinetEnv
from test_cabinet_dispatcher import Host, Quiet
from test_cabinet_prs import HEAD, HEAD2, _approve, _pr, _publish

from kd import board, session_config
from kd.cabinet import api, commands, config, decisions, dispatcher, service


@pytest.fixture
def env(tmp_path):
    e = CabinetEnv(tmp_path)
    e.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    api.install_host(e.ctx, lambda: session_config.HostConfig({"dev": 2}, 600))
    e.svc = service.Service(
        e.ctx,
        config.load("/none", "/none", dev=True),
        users=e.users,
        decisions=decisions.Decisions(e.store),
        notifier=Quiet(),
        now=lambda: e.now,
    )
    return e


def _tick(env, host):
    return asyncio.run(dispatcher.tick(env.svc, host=host, now=env.when()))


def _report(env, *items):
    return env.call("root", {"op": "host.report", "items": list(items)})


def _turn(env, command_id, phase, ok=True):
    command = env.store.command(command_id)
    item = {
        "type": "turn",
        "session": command["session"],
        "command": command_id,
        "phase": phase,
    }
    if phase == "ended":
        item["ok"] = ok
        if not ok:
            item["error"] = "boom"
    _report(env, item)


def _deliveries(host):
    return [c for c in host.calls if c.get("op") == "deliver"]


def _two_prs(env):
    card = env.card(status="Review")
    first = _publish(env, _pr(card.id))["decision"]
    second = _publish(
        env,
        _pr(
            card.id,
            number=13,
            head=HEAD2,
            url="https://forge.example/owner/repo/pulls/13",
        ),
    )
    return card, first, second["decision"]


def _wake(env, card, n):
    """A word of a person for dev in the card's thread, as the board makes one."""
    return env.store.enqueue(
        {
            "key": f"w:{card.id}:{n}",
            "agent": "dev",
            "thread": card.thread,
            "card": card.id,
            "kind": "message",
            "text": f"[board] {card.id}: a message for you ({n})",
        }
    )


def _start(env, card, kind="assigned"):
    """dev's turn on the card, delivered and started."""
    command = env.store.enqueue(
        {
            "key": f"t:{card.id}:{kind}",
            "agent": "dev",
            "thread": card.thread,
            "card": card.id,
            "kind": kind,
            "text": f"[board] {card.id} {kind}",
        }
    )
    _tick(env, Host())
    _turn(env, command["id"], "started")
    return command["id"]


def test_two_approvals_inside_the_window_are_one_turn_naming_both_prs(env):
    card, first, second = _two_prs(env)
    _approve(env, first)
    held = [c for c in env.store.commands(card=card.id) if c["kind"] == "message"]
    assert (
        len(held) == 1 and held[0]["next_try"] > env.now
    )  # the other PR still waits for him: this one waits
    host = Host()
    env.now = "2026-10-14T10:00:30Z"
    _tick(env, host)
    assert _deliveries(host) == []
    env.now = "2026-10-14T10:01:00Z"  # a minute later, the second PR — the last decision open on the card
    _approve(env, second)
    wakes = sorted(
        (c for c in env.store.commands(card=card.id) if c["kind"] == "message"),
        key=lambda c: c["created"],
    )
    joined, sent = wakes
    assert (
        sent["next_try"] <= env.now < joined["next_try"]
    )  # the last answer goes at once
    _tick(env, host)
    [delivery] = _deliveries(host)  # and takes the waiting one with it
    expected = [
        board.PR_WAKE["merge"].format(id=card.id, pr=n, sha7=h[:7])
        for n, h in ((12, HEAD), (13, HEAD2))
    ]
    assert delivery["text"] == commands.JOIN.join(expected)
    assert delivery["command"] == sent["id"]
    assert env.store.command(joined["id"])["state"] == "delivered"
    assert env.store.command(joined["id"])["reason"] == commands.MERGED + sent["id"]
    _turn(env, sent["id"], "started")
    _turn(env, sent["id"], "ended")
    assert [env.store.command(c["id"])["state"] for c in (sent, joined)] == [
        "done",
        "done",
    ]
    _tick(env, host)
    assert len(_deliveries(host)) == 1  # nothing more for dev: one turn


def test_the_only_decision_of_a_card_is_not_slowed(env):
    """A card with one PR: nothing can join its approval, so its wake goes at once."""
    card = env.card(status="Review")
    _approve(env, _publish(env, _pr(card.id))["decision"])
    [wake] = [c for c in env.store.commands(card=card.id) if c["kind"] == "message"]
    assert wake["next_try"] <= env.now
    host = Host()
    _tick(env, host)
    [delivery] = _deliveries(host)
    assert delivery["command"] == wake["id"]


def test_two_approvals_outside_the_window_are_two_turns(env):
    card, first, second = _two_prs(env)
    host = Host()
    _approve(env, first)
    env.now = "2026-10-14T10:02:00Z"
    _tick(env, host)
    [one] = _deliveries(host)
    assert "PR #12" in one["text"] and "PR #13" not in one["text"]
    _turn(env, one["command"], "started")
    _turn(env, one["command"], "ended")
    env.now = "2026-10-14T10:03:00Z"
    _approve(env, second)
    env.now = "2026-10-14T10:05:00Z"
    _tick(env, host)
    assert [d["text"].count("[board]") for d in _deliveries(host)] == [1, 1]
    assert "PR #13" in _deliveries(host)[1]["text"]


def test_words_that_come_while_the_turn_runs_wait_for_its_end_and_go_as_one(env):
    card = env.card(status="In Progress")
    running = _start(env, card)
    a, b = _wake(env, card, 1), _wake(env, card, 2)
    host = Host()
    _tick(env, host)
    assert (
        _deliveries(host) == []
    )  # the host would only queue them behind the running turn
    assert [env.store.command(c["id"])["state"] for c in (a, b)] == ["queued", "queued"]
    _turn(env, running, "ended")
    _tick(env, host)
    [delivery] = _deliveries(host)
    assert delivery["command"] == a["id"] and delivery["text"] == commands.JOIN.join(
        [a["text"], b["text"]]
    )


def test_a_word_for_an_idle_agent_is_sent_at_once(env):
    card = env.card(status="In Progress")
    wake = _wake(env, card, 1)
    host = Host()
    _tick(env, host)
    [delivery] = _deliveries(host)
    assert delivery["command"] == wake["id"] and delivery["text"] == wake["text"]


def test_a_turn_that_fails_gives_the_joined_wakes_back_to_the_queue(env):
    card = env.card(status="In Progress")
    a, b = _wake(env, card, 1), _wake(env, card, 2)
    host = Host()
    _tick(env, host)
    assert env.store.command(b["id"])["state"] == "delivered"
    _turn(env, a["id"], "started")
    _turn(env, a["id"], "ended", ok=False)
    assert env.store.command(a["id"])["state"] == "failed"
    after = env.store.command(b["id"])
    assert (
        after["state"] == "queued" and after["reason"] == ""
    )  # not lost: it goes again on its own
    _tick(env, host)
    assert (
        _deliveries(host)[-1]["command"] == b["id"]
        and _deliveries(host)[-1]["text"] == b["text"]
    )


class Failing(Host):
    """A host that answers its first ``times`` deliveries with ``error``, the next ones as usual."""

    def __init__(self, error, times=1):
        super().__init__()
        self.error, self.times = error, times

    async def call(self, request, timeout=10.0):
        if request.get("op") == "deliver" and self.times > 0:
            self.calls.append(request)
            self.times -= 1
            raise self.error
        return await super().call(request, timeout)


def test_a_refused_or_absent_host_leaves_the_joined_wakes_queued(env):
    card = env.card(status="In Progress")
    a, b = _wake(env, card, 1), _wake(env, card, 2)
    down = Failing(dispatcher.HostUnavailable("down"), times=10)
    _tick(env, down)
    assert [env.store.command(c["id"])["state"] for c in (a, b)] == ["queued", "queued"]
    assert commands.JOIN in _deliveries(down)[0]["text"]
    env.now = "2026-10-14T10:05:00Z"  # past the retry and the lease
    host = Failing(dispatcher.HostRefused("refused", "no"))
    _tick(env, host)
    assert env.store.command(a["id"])["state"] == "failed"
    # the joined one was never marked as sent with it: it goes on its own, in the same tick here
    assert (
        env.store.command(b["id"])["state"] == "delivered"
        and env.store.command(b["id"])["reason"] == ""
    )
    assert (
        _deliveries(host)[-1]["command"] == b["id"]
        and _deliveries(host)[-1]["text"] == b["text"]
    )


def test_a_host_restart_gives_the_joined_wakes_back(env):
    card = env.card(status="In Progress")
    a, b = _wake(env, card, 1), _wake(env, card, 2)
    _tick(env, Host())
    _report(env, {"type": "host", "phase": "started", "inflight": [a["id"]]})
    assert env.store.command(a["id"])["state"] == "failed"
    assert env.store.command(b["id"])["state"] == "queued"


def test_a_joined_wake_keeps_its_key(env):
    card = env.card(status="In Progress")
    a, b = _wake(env, card, 1), _wake(env, card, 2)
    _tick(env, Host())
    assert env.store.command(b["id"])["reason"] == commands.MERGED + a["id"]
    again = _wake(env, card, 2)  # the same key once more: the same command, nothing new
    assert again["id"] == b["id"] and len(env.store.commands(card=card.id)) == 2


def test_a_stale_wake_is_not_joined(env):
    card = env.card(status="In Progress")
    a = _wake(env, card, 1)
    stale = env.store.enqueue(
        {
            "key": "w:old",
            "agent": "dev",
            "thread": card.thread,
            "card": card.id,
            "kind": "answer",
            "text": f"[board] {card.id}: an answer",
            "basis": {"assignee": "@analyst"},
        }
    )
    host = Host()
    _tick(env, host)
    [delivery] = _deliveries(host)
    assert delivery["text"] == a["text"]
    assert env.store.command(stale["id"])["state"] == "cancelled"


# --- a joined command is in its lead's turn, not waiting (review of KD-9460) ---------------------------------
def _said(env, card, key, kind="message"):
    """A wake whose text is the board's fixed one for the card: two words of the owner read the same."""
    return env.store.enqueue(
        {
            "key": key,
            "agent": "dev",
            "thread": card.thread,
            "card": card.id,
            "kind": kind,
            "text": f"[board] {card.id}: the owner wrote to you"
            if kind == "message"
            else f"[board] {card.id}: the owner answered",
        }
    )


def _joined_turn(env, card):
    """An answer leads, the owner's word is joined to it; the lead's turn runs."""
    lead, word = _said(env, card, "w:answer", "answer"), _said(env, card, "w:word")
    host = Host()
    _tick(env, host)
    assert env.store.command(word["id"])["reason"] == commands.MERGED + lead["id"]
    _turn(env, lead["id"], "started")
    return lead, word, host


def test_a_word_written_while_the_joined_turn_runs_is_a_wake_of_its_own(env):
    card = env.card(status="In Progress")
    lead, word, host = _joined_turn(env, card)
    later = _said(env, card, "w:word:2")  # the same text as the joined one
    assert later["id"] != word["id"] and later["state"] == "queued"
    _turn(env, lead["id"], "ended")
    assert env.store.command(word["id"])["state"] == "done"
    _tick(env, host)
    assert _deliveries(host)[-1]["command"] == later["id"]


def test_a_brief_whose_joined_word_is_the_last_command_is_at_work(env):
    from kd.cabinet import briefs

    api.install_host(
        env.ctx, lambda: session_config.HostConfig({"dev": 2, "analyst": 1}, 600)
    )
    card = env.card(
        status="In Progress", assignee="@analyst", labels=[briefs.BRIEF_SPEC]
    )
    texts = [f"[board] {card.id}: an answer", f"[board] {card.id}: a word"]
    lead, word = (
        env.store.enqueue(
            {
                "key": f"a:{n}",
                "agent": "analyst",
                "thread": card.thread,
                "card": card.id,
                "kind": kind,
                "text": text,
            }
        )
        for n, (kind, text) in enumerate(zip(("answer", "message"), texts))
    )
    _tick(env, Host())
    assert env.store.command(word["id"])["reason"] == commands.MERGED + lead["id"]
    assert (
        briefs._preparing(env.store, env.store.card(card.id))["state"] == "in_progress"
    )


def test_a_continuation_while_the_joined_turn_runs_is_a_turn_of_its_own(env):
    from kd import pipeline_config
    from kd.cabinet import continuations

    env.ctx.pipeline = lambda: pipeline_config.Config(mode="enforce")
    card = env.card(status="In Progress")
    lead, word, host = _joined_turn(env, card)
    row = continuations.launch(
        env.ctx,
        key="wait:earlier",
        record=env.store.card(card.id),
        agent="dev",
        source="wait",
        text=continuations.RESOLVED.format(id=card.id),
    )
    assert row["command"] not in (lead["id"], word["id"])
    assert env.store.command(row["command"])["state"] == "queued"


# --- a delivery sent again carries what it carried the first time ---------------------------------------------
def test_a_delivery_sent_again_after_a_lost_answer_joins_nothing_new(env):
    """The host took the first send (its answer was lost) and answers the same command id again from its cache,
    whatever the text: a wake that came in between is not in that turn and must not be marked as if it were."""
    card = env.card(status="In Progress")
    a, b = _wake(env, card, 1), _wake(env, card, 2)
    lost = Failing(dispatcher.HostUnavailable("no answer"), times=1)
    _tick(env, lost)
    first = _deliveries(lost)[0]
    assert first["command"] == a["id"] and first["text"] == commands.JOIN.join(
        [a["text"], b["text"]]
    )
    c = _wake(env, card, 3)  # comes while the cabinet waits to send again
    env.now = "2026-10-14T10:05:00Z"  # past the retry and the leases
    _tick(env, lost)
    sent = [d for d in _deliveries(lost) if d["command"] == a["id"]]
    assert sent[-1]["text"] == first["text"]  # the same set as the first time
    assert env.store.command(b["id"])["reason"] == commands.MERGED + a["id"]
    assert not env.store.command(c["id"])["reason"].startswith(commands.MERGED)
    _turn(env, a["id"], "started")
    _turn(env, a["id"], "ended")
    assert env.store.command(b["id"])["state"] == "done"
    assert (
        env.store.command(c["id"])["state"] != "done"
    )  # never received: its own turn is still to come
    _tick(env, lost)
    assert any(d["command"] == c["id"] for d in _deliveries(lost))


def test_a_joined_wake_waits_for_its_lead_while_the_lead_is_sent_again(env):
    card = env.card(status="In Progress")
    a, b = _wake(env, card, 1), _wake(env, card, 2)
    down = Failing(dispatcher.HostUnavailable("no answer"), times=1)
    _tick(env, down)
    env.now = (
        "2026-10-14T10:01:10Z"  # b's lease is over, a's retry (backoff) not yet due
    )
    env.store._conn.execute(
        "UPDATE commands SET next_try=? WHERE id=?", ("2026-10-14T10:09:00Z", a["id"])
    )
    _tick(env, down)
    assert [d["command"] for d in _deliveries(down)] == [
        a["id"]
    ]  # b is not sent on its own meanwhile
    assert env.store.command(b["id"])["state"] == "queued"


# --- time held behind a running turn does not age a wake ------------------------------------------------------
def test_a_word_held_behind_a_long_turn_is_delivered_when_the_turn_ends(env):
    """Before the hold it would have waited at the host's queue behind that turn and run: held in the cabinet it
    must not expire for the time it was held."""
    card = env.card(status="In Progress")
    running = _start(env, card)
    env.now = "2026-10-14T10:01:00Z"
    word = _wake(env, card, 1)
    host = Host()
    env.now = "2026-10-15T16:00:00Z"  # 30 hours later the turn in front ends
    _tick(env, host)
    assert env.store.command(word["id"])["state"] == "queued"  # still held
    _turn(env, running, "ended")
    _tick(env, host)
    assert env.store.command(word["id"])["state"] == "delivered"
    assert _deliveries(host)[-1]["command"] == word["id"]


def test_a_word_nobody_held_still_expires(env):
    card = env.card(status="In Progress")
    word = _wake(env, card, 1)
    env.now = "2026-10-15T16:00:00Z"
    _tick(env, Host())
    after = env.store.command(word["id"])
    assert (after["state"], after["reason"]) == ("failed", "delivery_expired")
