"""Every source of an agent's wake-up, with every agent on the session host, ends as one cabinet
command in the right thread — a card's thread for card work, the agent's chat for a signal without a card.
the cabinet is the only wake path."""

import functools
import io
import json
import sys
from pathlib import Path

import pytest

from kd import attention, attention_rules, board, directives, session_config, watch
from kd import wake as kd_wake
from kd.cabinet import api, users

sys.path.insert(0, str(Path(__file__).resolve().parent))  # cabinet_env, test_directive_flow
from cabinet_env import CabinetEnv  # noqa: E402
from test_directive_flow import Flow  # noqa: E402

HOST = session_config.HostConfig({"analyst": 1, "boss": 1, "dev": 1, "sysadmin": 1}, 600)
OWNER = api.Principal("user", -1, "owner")


@pytest.fixture
def env(tmp_path):
    e = CabinetEnv(tmp_path / "cabinet")
    users.Users(e.store).create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    api.install_host(e.ctx, lambda: HOST)
    e.ctx._specs = {"@dev": frozenset({"implementer"}), "@analyst": frozenset({"task-brief"}),
                    "@sysadmin": frozenset(), "@boss": frozenset()}
    e.wake = functools.partial(kd_wake.wake, config=HOST, client=e.client("digi"))
    return e


def _one(env, agent, *, card=None):
    """The agent's only command, and the thread it goes to: the card's or the agent's chat."""
    [command] = env.store.commands(agent=agent)
    thread = env.store.card(card).thread if card else env.store.agent_thread(agent)
    assert command["thread"] == thread and command["card"] == (card or "")
    return command


def _owner(env, request):
    return api.dispatch(env.ctx, OWNER, request)


def _act(env, card_id, action, **extra):
    revision = env.store.card(card_id).revision
    return _owner(env, {"op": "user.card.act", "card": card_id, "action": action, "revision": revision, **extra})


def _edit(env, card_id, changes, **extra):
    revision = env.store.card(card_id).revision
    return _owner(env, {"op": "user.card.edit", "card": card_id, "revision": revision, "changes": changes, **extra})


# --- the cabinet's own events -----------------------------------------------------------------
def test_to_do(env):
    card = env.card(status="Inbox", assignee="@dev")
    _edit(env, card.id, {"status": "To Do"}, override_reason="the board's status menu")
    assert _one(env, "dev", card=card.id)["text"] == f"[board] {card.id} assigned to you"


def test_take_into_work(env):
    card = env.card(status="Inbox", assignee="@dev")
    _act(env, card.id, "start")
    assert _one(env, "dev", card=card.id)["kind"] == "assigned"


def test_develop_a_brief(env):
    card = env.card(status="Inbox", assignee="@dev")
    _act(env, card.id, "brief")
    assert _one(env, "analyst", card=card.id)["kind"] in ("assigned", "wake")
    assert env.store.commands(agent="dev") == []


def test_a_mention(env):
    card = env.card(status="In Progress", assignee="@dev")
    _owner(env, {"op": "user.card.comment", "card": card.id, "text": "@sysadmin посмотри логи"})
    assert _one(env, "sysadmin", card=card.id)["text"] == board.MESSAGE_WAKE.format(id=card.id)


def test_an_assignee_change(env):
    card = env.card(status="In Progress", assignee="@dev")
    _edit(env, card.id, {"assignee": "@sysadmin"})
    assert _one(env, "sysadmin", card=card.id)["kind"] == "assigned"


def test_an_answer(env):
    card = env.card(status="In Progress", assignee="@dev")
    asked = env.call("digi", {"op": "decision.open", "type": "question", "agent": "dev", "card": card.id,
                             "payload": {"text": "Выкатывать сегодня?"}})["decision"]
    env.call("digi-cabinet", {"op": "decision.reply", "id": asked["id"], "user": "owner", "text": "Да"})
    command = _one(env, "dev", card=card.id)
    assert command["text"] == board.ANSWER_WAKE.format(id=card.id, decision=asked["id"])


def test_a_return_to_work(env):
    card = env.card(status="Review", assignee="@dev")
    _act(env, card.id, "return", comment="Поправь тест")
    assert _one(env, "dev", card=card.id)["text"] == board.CONTINUE_WAKE.format(id=card.id)


def test_a_ready_brief(env):
    card = env.card(status="Inbox", assignee="@analyst", labels=["spec:task-brief"])
    revision = env.store.card(card.id).revision
    env.store.edit_card(card.id, revision, {"assignee": "@dev", "labels_add": ["brief-ready"]}, actor="agent:analyst")
    assert _one(env, "boss", card=card.id)["text"] == board.BRIEF_WAKE.format(id=card.id)


# --- the executors' signals -------------------------------------------------------------------
class HostedFlow(Flow):
    """The directive flow with its wakes through the facade into the same cabinet."""

    def __init__(self, tmp_path, nudge, work):
        super().__init__(tmp_path, nudge, work)
        self.hosted_wake = functools.partial(kd_wake.wake, config=HOST, client=self.client)

    def wake(self, agent, text, urgent=False, card="", key=""):
        return self.hosted_wake(agent, text, card=card, key=key, urgent=urgent)


@pytest.fixture
def flow(tmp_path, kd_nudge, kd_work, portable_flock):
    f = HostedFlow(tmp_path, kd_nudge, kd_work)
    users.Users(f.cab.store).create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    api.install_host(f.cab.ctx, lambda: HOST)
    return f


def test_a_boss_directive_goes_to_the_card_thread(flow):
    flow.card()
    rc, out, _ = flow.direct()
    assert rc == 0 and out.endswith(" delivered\n")
    command = _one(flow.cab, "dev", card="KD-5")
    assert command["text"].startswith("[boss] directive ") and command["kind"] == "signal"


def test_a_directive_reply_goes_to_the_boss_chat(flow):
    flow.card()
    flow.direct()
    rc, _, _ = flow.work("reply", flow.only_id(), "--state", "accepted", "--message", "Беру")
    assert rc == 0
    assert _one(flow.cab, "boss")["text"] == "[directive] replies: run kd-nudge directives"


def test_a_directive_withdrawal_notice_goes_to_the_card_thread(flow):
    flow.card()
    flow.direct()
    directive_id = flow.only_id()
    [delivered] = flow.cab.store.commands(agent="dev")
    for state in ("delivered", "started", "done"):  # the agent took the directive in that minute
        flow.cab.store.command_state(delivered["id"], state)
    directives.withdraw(flow.store, directive_id, flow.now, notify=True)
    rc, out, _ = flow.nudge("deliver", channel=None)  # the watch's tick, not a channel
    assert rc == 0 and "withdrawal notices 1" in out
    notice = next(c for c in flow.cab.store.commands(agent="dev") if c["id"] != delivered["id"])
    assert notice["thread"] == flow.cab.store.card("KD-5").thread and notice["key"] == f"withdrawn:{directive_id}"


def _watch_paths(tmp_path):
    registry = tmp_path / "agents.json"
    registry.write_text(json.dumps({"boss": {"user": "digi-boss"}, "dev": {"user": "digi-dev"}}), encoding="utf-8")
    return watch.Paths(registry=str(registry), attention=str(tmp_path / "attention.json"))


def test_a_watch_attention_goes_to_the_boss_monitor(env, tmp_path, portable_flock):
    """an attention tick is the coordinator's work log, not its chat with the owner."""
    paths = _watch_paths(tmp_path)
    now = env.when()
    event = attention_rules.Event("in_progress_idle", "KD-9", "3", "boss")
    attention.observe(paths.attention, attention_rules.Report((event,), ()), now)
    assert watch.dispatch(paths, env.wake, None, [], now, io.StringIO())
    [command] = env.store.commands(agent="boss")
    assert command["text"] == watch.SIGNAL and command["card"] == ""
    assert command["thread"] == env.store.agent_thread("boss", "monitor")


def test_the_boss_digest_goes_to_the_boss_chat(env, tmp_path):
    assert watch.digest_signal(_watch_paths(tmp_path), env.wake) == 0
    assert _one(env, "boss")["text"] == watch.DIGEST_SIGNAL


def test_a_worker_result_goes_to_the_card_thread(env):
    """the cabinet itself ends a job and wakes its requester in the card's thread."""
    from test_cabinet_jobs import _ended, _job, _submit

    card = env.card(status="In Progress", assignee="@dev")
    job = _submit(env, _job(card.id))["job"]
    _ended(env, card.id, job["id"])
    assert _one(env, "boss", card=card.id)["text"].startswith(f"[worker] job {job['id']} done card {card.id}")


def test_a_monitor_alert_goes_to_the_sysadmin_chat(env):
    """the monitor's tick runs as the sysadmin's user and queues the command itself; another agent is refused."""
    own = env.client("digi-sysadmin")
    assert kd_wake.wake("sysadmin", "[монитор] node-x: диск 95 %", urgent=True, config=HOST, client=own) == "sent"
    assert kd_wake.wake("dev", "[монитор] not for dev", config=HOST, client=own) == "error"
    assert _one(env, "sysadmin")["text"] == "[монитор] node-x: диск 95 %"
