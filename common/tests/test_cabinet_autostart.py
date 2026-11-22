"""A brief of size S starts by itself — the analyst's handoff with ``size:S`` approves the version and
moves the card to To Do on its implementer in the same write; the exceptions keep the owner's "Approve"."""

import pytest

from kd import session_config
from kd.cabinet import api, autostart, briefs, commands, i18n, users
from cabinet_env import CabinetEnv

HOSTED = session_config.HostConfig(
    {"analyst": 1, "dev": 1, "boss": 1, "sysadmin": 1}, 600
)
SPECS = {
    "@dev": frozenset({"implementer", "docs"}),
    "@analyst": frozenset({"task-brief"}),
    "@sysadmin": frozenset(),
    "@boss": frozenset(),
}
OPERATION = {
    "verb": "agent.reinstall",
    "target": "factory",
    "agents": ["dev"],
    "what": "w",
    "effect": "e",
    "risk": {"level": "low", "why": "y"},
    "rollback": "r",
}


@pytest.fixture
def env(tmp_path):
    e = CabinetEnv(tmp_path)
    users.Users(e.store).create(
        "owner", "Владелец", roles=[("owner", "*")], actor="root"
    )
    api.install_host(e.ctx, lambda: HOSTED)
    e.ctx._specs = SPECS
    return e


def _handoff(
    env,
    size="S",
    project="digipaddock",
    to="@dev",
    spec="spec:implementer",
    actor="agent:analyst",
    **extra,
):
    """The analyst's ``kd-task handoff --to <to> --add-label brief-ready --add-label size:<size>``."""
    card = env.card(
        status="Inbox", assignee="@analyst", labels=["spec:task-brief"], project=project
    )
    begin = env.store.last_seq()
    labels = (
        ["brief-ready"] + ([f"size:{size}"] if size else []) + ([spec] if spec else [])
    )
    changes = {
        "description": "Цель: отчёт.",
        "acceptance": [{"text": "Есть отчёт", "checked": False}],
        "assignee": to,
        "labels_remove": ["spec:task-brief"],
        "labels_add": labels,
        **extra,
    }
    env.store.edit_card(card.id, None, changes, actor=actor)
    return env.store.card(card.id), begin


def _commands(env, card_id):
    return [
        (c["agent"], c["kind"]) for c in env.store.commands() if c["card"] == card_id
    ]


def _notices(env, card_id, begin):
    return [
        e["payload"]["key"]
        for e in env.store.events_since(begin)
        if e["kind"] == "notice" and e["card"] == card_id
    ]


def test_an_s_brief_starts_on_its_implementer_without_the_owner(env):
    card, begin = _handoff(env)
    assert (card.status, card.assignee) == ("To Do", "@dev")
    [v1] = briefs.versions(env.store, card.id)
    assert v1["approved_by"] == autostart.ACTOR
    assert ("dev", "assigned") in _commands(env, card.id)
    assert _notices(env, card.id, begin) == [autostart.STARTED]


def test_the_coordinators_propose_the_start_is_moot_once_the_card_left_inbox(env):
    card, _ = _handoff(env)
    hosted = lambda agent: True  # noqa: E731
    reasons = {
        (c["agent"], c["kind"]): commands.stale_reason(env.store, c, hosted)
        for c in env.store.commands()
        if c["card"] == card.id
    }
    assert reasons == {
        ("boss", "signal"): commands.BRIEF_LEFT_INBOX,
        ("dev", "assigned"): None,
    }
    waiting, _ = _handoff(env, size="M")
    [signal] = [
        c
        for c in env.store.commands()
        if c["card"] == waiting.id and c["agent"] == "boss"
    ]
    assert commands.stale_reason(env.store, signal, hosted) is None


def test_the_owner_is_told_in_the_thread_in_his_language(env):
    card, begin = _handoff(env)
    [notice] = [e for e in env.store.events_since(begin) if e["kind"] == "notice"]
    text = i18n.render("ru", notice["payload"])
    assert "v1" in text and "@dev" in text and "«Одобрить»" in text and "{" not in text
    assert "{" not in i18n.render("en", notice["payload"])


@pytest.mark.parametrize("size", ["M", "L", None])
def test_m_l_and_unsized_briefs_wait_for_the_owner(env, size):
    card, begin = _handoff(env, size=size)
    assert (
        card.status == "Inbox"
        and not briefs.versions(env.store, card.id)[0]["approved_by"]
    )
    assert ("boss", "signal") in _commands(
        env, card.id
    )  # the coordinator proposes the start
    assert _notices(env, card.id, begin) == []


@pytest.mark.parametrize(
    "extra, reason",
    [
        ({"operations": [OPERATION]}, "operations"),
        ({"dependencies": ["KD-999"]}, "blocked"),
    ],
)
def test_an_s_brief_with_an_exception_keeps_the_approval_and_says_why(
    env, extra, reason
):
    card, begin = _handoff(env, **extra)
    assert (
        card.status == "Inbox"
        and not briefs.versions(env.store, card.id)[0]["approved_by"]
    )
    assert ("boss", "signal") in _commands(env, card.id)
    assert _notices(env, card.id, begin) == [f"notice.brief.size_s_{reason}"]


def test_server_work_is_never_started_by_itself(env):
    card, begin = _handoff(env, project="admin", to="@sysadmin", spec=None)
    assert card.status == "Inbox"
    assert _notices(env, card.id, begin) == ["notice.brief.size_s_servers"]
    named, begin = _handoff(
        env, servers=["web-1"]
    )  # code work that names a managed server
    assert named.status == "Inbox"
    assert _notices(env, named.id, begin) == ["notice.brief.size_s_servers"]


def test_a_card_not_yet_on_its_implementer_keeps_the_approval(env):
    """The analyst's own edit with brief-ready and size:S (no handoff): the start would only move the status."""
    card, begin = _handoff(env, to="@analyst", spec=None)
    assert (card.status, card.assignee) == ("Inbox", "@analyst")
    assert _notices(env, card.id, begin) == ["notice.brief.size_s_blocked"]


def _rework(env, card):
    """The owner's "for rework" on the card's page: back to the analyst in Inbox."""
    revision = env.store.card(card.id).revision
    api.dispatch(
        env.ctx,
        api.Principal("user", -1, "owner"),
        {
            "op": "user.card.act",
            "card": card.id,
            "action": "brief",
            "revision": revision,
            "comment": "это не S",
        },
    )
    return env.store.card(card.id)


def _again(env, card, labels):
    env.store.edit_card(
        card.id,
        None,
        {
            "description": "Цель: отчёт, v2.",
            "assignee": "@dev",
            "labels_remove": ["spec:task-brief"],
            "labels_add": labels,
        },
        actor="agent:analyst",
    )
    return env.store.card(card.id)


def test_a_rework_drops_the_size_and_a_new_m_size_waits_for_the_owner(env):
    card, _ = _handoff(env)
    back = _rework(env, card)
    assert (back.status, back.assignee) == ("Inbox", "@analyst")
    assert not [v for v in back.labels if v.startswith("size:")]
    after = _again(env, back, ["brief-ready", "size:M", "spec:implementer"])
    assert after.status == "Inbox" and [
        v["approved_by"] for v in briefs.versions(env.store, card.id)
    ] == [autostart.ACTOR, ""]


def test_a_size_left_on_the_card_never_starts_a_new_version(env):
    """``size:S`` the write did not give — set earlier by someone else — is not the analyst's size of this brief."""
    card = env.card(
        status="Inbox", assignee="@analyst", labels=["spec:task-brief", "size:S"]
    )
    after = _again(env, card, ["brief-ready", "spec:implementer"])
    assert (
        after.status == "Inbox"
        and not briefs.versions(env.store, card.id)[0]["approved_by"]
    )


def test_two_sizes_on_the_card_keep_the_approval(env):
    card = env.card(
        status="Inbox", assignee="@analyst", labels=["spec:task-brief", "size:M"]
    )
    after = _again(env, card, ["brief-ready", "size:S", "spec:implementer"])
    assert after.status == "Inbox"


def test_a_taker_without_the_specialization_keeps_the_approval(env):
    card, begin = _handoff(env, spec="spec:tester")
    assert card.status == "Inbox"
    assert _notices(env, card.id, begin) == ["notice.brief.size_s_blocked"]


def test_only_the_analysts_handoff_starts_it(env):
    card, begin = _handoff(env, actor="user:owner")
    assert card.status == "Inbox" and _notices(env, card.id, begin) == []


def test_every_reason_has_its_text_in_both_catalogs():
    for lang in ("ru", "en"):
        for key in [autostart.STARTED] + [
            f"notice.brief.size_s_{r}" for r in autostart.REASONS
        ]:
            assert i18n.t(lang, key, version=1, agent="@dev") != key
