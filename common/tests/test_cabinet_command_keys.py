"""Command keys are the cabinet's (KD-9479): an agent's own commands live in a namespace of their own, on a card it
holds, within the continuations' window; the keys the cabinet itself writes later are not anyone's to take, and one
taken anyway (a row from before) never rolls back the write that needs it or stops the sweep."""

import hashlib

import pytest
from cabinet_env import CabinetEnv

from kd import pipeline_config, session_config
from kd.cabinet import api, commands, continuations, decisions, defers, store
from test_cabinet_outcomes import begin, end, finish, question
from test_cabinet_outcomes import card as new_card

HOSTED = session_config.HostConfig({"boss": 1, "dev": 2, "analyst": 1, "sysadmin": 1})
UNTIL = "2026-10-14T12:00:00Z"
SQUAT = "[self] taken"


@pytest.fixture
def cab(tmp_path):
    env = CabinetEnv(tmp_path)
    env.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    api.install_host(env.ctx, lambda: HOSTED)
    env.ctx.pipeline = lambda: pipeline_config.Config(mode="enforce")
    env.now = "2026-10-14T10:00:00Z"
    return env


def card(cab, **kw):
    """A card whose wake on creation already ran: only what the test does is queued."""
    record = new_card(cab, **kw)
    for command in cab.store.commands(card=record.id, states=["queued"]):
        for state in ("delivered", "started", "done"):
            cab.store.command_state(command["id"], state)
    return record


def squat(cab, key, record=None, agent="dev", state="done"):
    """A row of the agent's own that took ``key`` with other content before the cabinet needed it — one written
    before this release, so straight into the table (no event of its own)."""
    thread = record.thread if record else cab.store.agent_thread(agent)
    command_id = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    cab.store._conn.execute(
        "INSERT INTO commands (id, key, agent, thread, card, kind, text, initiator, state, next_try, created, updated,"
        " basis) VALUES (?,?,?,?,?,'signal',?,?,?,?,?,?,'{}')",
        (
            command_id,
            key,
            agent,
            thread,
            record.id if record else "",
            f"{SQUAT} {key}",
            f"agent:{agent}",
            state,
            cab.now,
            cab.now,
            cab.now,
        ),
    )
    return cab.store.command(command_id)


def squat_events(cab, agent, ahead=60):
    """The event wakes of ``agent`` the next ``ahead`` events would ask for, taken."""
    last = cab.store.last_seq()
    for seq in range(last + 1, last + ahead + 1):
        squat(cab, f"ev:{seq}:{agent}", agent=agent)


def queued(cab, record, agent="dev"):
    return [
        c
        for c in cab.store.commands(card=record.id, states=["queued"])
        if c["agent"] == agent
    ]


def self_signal(cab, key, card_id="", text="[self] look again", agent="dev"):
    request = {"op": "command.enqueue", "agent": agent, "text": text, "key": key}
    if card_id:
        request["card"] = card_id
    return cab.call(f"digi-{agent}", request)["command"]


def refused(call, *args, **kw):
    with pytest.raises(api.ApiError) as exc:
        call(*args, **kw)
    assert exc.value.kind == "refused", exc.value
    return str(exc.value)


# --- the op boundary --------------------------------------------------------------------------------------------
def test_an_agents_key_is_kept_in_its_own_namespace_and_a_repeat_is_the_same_command(
    cab,
):
    record = card(cab)
    first = self_signal(cab, "recheck-1", record.id)
    assert first["key"] == "agent:dev:recheck-1" and first["card"] == record.id
    assert self_signal(cab, "recheck-1", record.id)["id"] == first["id"]
    longest = self_signal(
        cab, "k" * 64, text="[self] the longest key"
    )  # the given 64 characters fit
    assert longest["key"] == "agent:dev:" + "k" * 64


@pytest.mark.parametrize(
    "key",
    [
        "time:abc",
        "wait:abc",
        "cont:abc",
        "ev:5:boss",
        "chat:3:4",
        "council:x:ended",
        "assist:x:help:dev:1",
        "voice:3:4:boss",
        "agent:boss:x",
    ],
)
def test_the_cabinets_own_key_prefixes_are_refused_to_every_caller(cab, key):
    record = card(cab)
    for user in ("digi-dev", "digi", "root"):
        why = refused(
            cab.call,
            user,
            {
                "op": "command.enqueue",
                "agent": "dev",
                "text": "x",
                "key": key,
                "card": record.id,
            },
        )
        assert "reserved" in why
    assert queued(cab, record) == []


def test_the_executors_wake_keys_still_work(cab):
    """kd-nudge and kd-work wake through kd.wake: a hash of the line, or ``withdrawn:<directive>``."""
    record = card(cab)
    for key in ("0123456789abcdef0123456789abcdef", "withdrawn:" + "a" * 32):
        command = cab.call(
            "digi",
            {
                "op": "command.enqueue",
                "agent": "dev",
                "text": f"[boss] {key}",
                "key": key,
                "card": record.id,
            },
        )["command"]
        assert command["key"] == key


def test_the_monitors_wake_of_its_own_agent_still_works(cab):
    """The sysadmin's monitor wakes its own agent in «Monitor» as that agent, many times an hour."""
    for n in range(10):
        command = cab.call(
            "digi-sysadmin",
            {
                "op": "command.enqueue",
                "agent": "sysadmin",
                "text": f"[monitor] {n}",
                "key": f"{n:032x}",
                "space": "monitor",
            },
        )["command"]
        assert command["card"] == "" and command["key"] == f"agent:sysadmin:{n:032x}"


# --- (e) a card it does not hold ---------------------------------------------------------------------------------
def test_an_agent_signals_only_a_card_it_holds(cab):
    others = card(cab, assignee="@analyst")
    why = refused(self_signal, cab, "k1", others.id)
    assert others.id in why and "yours" in why
    closed = card(cab, status="Done")
    assert closed.id in refused(self_signal, cab, "k2", closed.id)
    assert queued(cab, others, "dev") == [] and queued(cab, closed, "dev") == []


def test_a_self_signal_on_a_card_handed_on_is_stale(cab):
    record = card(cab)
    command = self_signal(cab, "k1", record.id)
    assert commands.stale_reason(cab.store, command, lambda a: True) is None
    cab.store.edit_card(record.id, None, {"assignee": "@analyst"}, actor="user:owner")
    assert (
        commands.stale_reason(
            cab.store, cab.store.command(command["id"]), lambda a: True
        )
        == commands.REASSIGNED
    )


def test_a_self_signal_queued_before_this_release_on_another_card_is_stale(cab):
    """A row an agent queued for itself on a card it did not hold (no namespace yet) is cancelled, not run."""
    others = card(cab, assignee="@analyst")
    found = squat(cab, "free-key", others)
    assert (
        commands.stale_reason(cab.store, found, lambda a: True) == commands.REASSIGNED
    )
    executor = cab.store.enqueue(
        {
            "key": "boss-line",
            "agent": "dev",
            "thread": others.thread,
            "card": others.id,
            "kind": "signal",
            "text": "[boss] look",
            "initiator": "digi",
        }
    )
    assert (
        commands.stale_reason(cab.store, executor, lambda a: True) is None
    )  # an executor's signal is not


# --- (d) the continuations' window -------------------------------------------------------------------------------
def test_self_signals_on_a_card_count_in_its_continuations_window(cab):
    record = card(cab)
    limit = cab.ctx.pipeline().per_window
    for n in range(limit):
        self_signal(cab, f"k{n}", record.id, text=f"[self] look {n}")
    assert "rate limit" in refused(
        self_signal, cab, "one-more", record.id, text="[self] once more"
    )
    assert (
        continuations.limit_reason(cab.ctx, record, "dev") == "window"
    )  # a continuation waits for the window too
    cab.now = "2026-10-14T11:00:01Z"
    self_signal(cab, "next-hour", record.id, text="[self] the next hour")


# --- (a) the sweep ------------------------------------------------------------------------------------------------
def test_a_taken_time_key_does_not_stop_the_sweep(cab):
    record = card(cab)
    defer = defers.place(cab.ctx, record, until=UNTIL, source="owner", by="user:owner")
    squat(cab, f"time:{defer['id']}", record)
    dep, waiting = card(cab, assignee="@analyst"), card(cab)
    cab.store.edit_card(
        waiting.id, None, {"dependencies": [dep.id]}, actor="user:owner"
    )
    command, session = begin(cab, waiting)
    finish(cab, waiting, outcome="wait_dependency", note="waiting for the dependency")
    end(cab, command, session)
    cab.store._conn.execute(
        "UPDATE cards SET status='Done' WHERE id=?", (dep.id,)
    )  # the event was missed
    cab.now = "2026-10-14T12:00:30Z"
    out = cab.call("digi-cabinet", {"op": "pipeline.sweep"})
    assert out["deferred"] == 1 and out["rechecked"] == 1
    [wake] = [c for c in queued(cab, record) if c["kind"] == "continue"]
    assert wake["key"] == f"time:{defer['id']}{store.KEY_TAKEN_SUFFIX}"
    assert wake["text"] == continuations.TIME.format(id=record.id)
    assert [c["key"] for c in queued(cab, waiting) if c["kind"] == "continue"] == [
        f"wait:{command}"
    ]
    assert defers.active(cab.store, record.id) is None


# --- (b) the owner's answer ---------------------------------------------------------------------------------------
def test_a_taken_answer_key_keeps_the_owners_answer_and_wakes_the_agent(cab):
    record = card(cab)
    command, session = begin(cab, record)
    asked = question(cab, record)
    finish(cab, record, outcome="wait_user", note="waiting for the owner")
    end(cab, command, session)
    squat(cab, f"wait:{command}", record)
    squat_events(cab, "dev")
    cab.call(
        "digi-cabinet",
        {"op": "decision.reply", "id": asked, "user": "owner", "text": "Да"},
    )
    assert decisions.Decisions(cab.store).get(asked)["state"] != "open"
    [wake] = queued(cab, record)
    assert wake["key"].endswith(store.KEY_TAKEN_SUFFIX)


def test_a_taken_wait_key_keeps_the_owners_close_of_the_dependency(cab):
    dep, record = card(cab, assignee="@analyst"), card(cab)
    cab.store.edit_card(record.id, None, {"dependencies": [dep.id]}, actor="user:owner")
    command, session = begin(cab, record)
    finish(cab, record, outcome="wait_dependency", note="waiting for the dependency")
    end(cab, command, session)
    squat(cab, f"wait:{command}", record)
    cab.store.edit_card(dep.id, None, {"status": "Done"}, actor="user:owner")
    assert cab.store.card(dep.id).status == "Done"
    [wake] = [c for c in queued(cab, record) if c["kind"] == "continue"]
    assert wake["key"] == f"wait:{command}{store.KEY_TAKEN_SUFFIX}"


# --- (c) the coordinator's wakes ----------------------------------------------------------------------------------
def test_taken_event_keys_do_not_block_the_coordinators_wakes(cab):
    record = card(cab)
    squat_events(cab, "boss")
    api.dispatch(
        cab.ctx,
        api.Principal("user", -1, "owner"),
        {"op": "user.card.comment", "card": record.id, "text": "@boss посмотри"},
    )
    assert any(m["body"] == "@boss посмотри" for m in cab.store.messages(record.id))
    [wake] = queued(cab, record, "boss")
    assert wake["kind"] == "message" and wake["key"].endswith(store.KEY_TAKEN_SUFFIX)


# --- the store --------------------------------------------------------------------------------------------------
def test_the_store_queues_a_system_command_past_a_taken_key_once(cab):
    record = card(cab)
    squat(cab, "cont:abc", record)
    spec = {
        "key": "cont:abc",
        "agent": "dev",
        "thread": record.thread,
        "card": record.id,
        "kind": "continue",
        "text": "[board] go on",
    }
    first = cab.store.enqueue(spec)
    assert first["key"] == "cont:abc" + store.KEY_TAKEN_SUFFIX
    assert cab.store.enqueue(spec)["id"] == first["id"]  # a repeat is the same command
    with pytest.raises(store.Conflict):
        cab.store.enqueue(
            spec, strict=True
        )  # the op boundary's own key is a conflict, as it was
