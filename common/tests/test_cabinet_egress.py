"""The autonomy design, piece 2: the cabinet grants a card's session its project's probe exit while the owner's
consent on the card lives — the card assigned to the agent, in work, its approved brief unchanged — and stops a
live process that holds an exit its card no longer qualifies for."""

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import pytest
from cabinet_env import CabinetEnv

from kd import session_config
from kd.cabinet import api, briefs, config, decisions, dispatcher, egress, service

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "host" / "egress.json").read_text(
        encoding="utf-8"
    )
)


class Host:
    """A session host that records requests; ``status`` answers with the sessions the test puts there."""

    def __init__(self, *answers):
        self.calls, self.answers, self.sessions = [], list(answers), []

    async def call(self, request, timeout=10.0):
        self.calls.append(request)
        if request["op"] == "status":
            return {"ok": True, "sessions": self.sessions, "slots": {}, "jobs": []}
        answer = (
            self.answers.pop(0) if self.answers else {"ok": True, "state": "starting"}
        )
        if isinstance(answer, Exception):
            raise answer
        return answer


class Quiet:
    def catch_up(self, decisions):
        pass


@pytest.fixture
def env(tmp_path):
    e = CabinetEnv(
        tmp_path, repo_root=Path(__file__).resolve().parents[2]
    )  # the manifests: dev holds net.egress
    e.config = session_config.HostConfig({"dev": 2}, 600)
    e.ctx.llm_env_dir = str(tmp_path / "llm")
    document = {**FIXTURE, "projects": {"digipaddock": "probe"}}
    (tmp_path / "egress.json").write_text(json.dumps(document), encoding="utf-8")
    e.ctx.egress_path = str(tmp_path / "egress.json")
    api.install_host(e.ctx, lambda: e.config)
    e.svc = service.Service(
        e.ctx,
        config.load("/none", "/none", dev=True),
        users=e.users,
        decisions=decisions.Decisions(e.store),
        notifier=Quiet(),
        now=lambda: e.now,
    )
    return e


def _card(env, **over):
    return env.card(**{"status": "In Progress", "assignee": "@dev", **over})


def _edit(env, card, changes):
    return env.store.edit_card(card.id, None, changes, actor="user:owner")


def test_grant_needs_every_condition(env):
    card = _card(env)
    assert egress.grant(env.ctx, card, "dev") == "probe"
    for status in ("To Do", "Blocked"):
        assert egress.grant(env.ctx, _card(env, status=status), "dev") == "probe", (
            status
        )
    assert egress.grant(env.ctx, _card(env, status="Done"), "dev") == ""
    assert egress.grant(env.ctx, _card(env, status="Review"), "dev") == ""
    assert egress.grant(env.ctx, _card(env, assignee="@boss"), "dev") == ""
    assert egress.grant(env.ctx, card, "boss") == ""  # the exit lists dev only
    env.store.create_project("shop2", "shop2", "@dev", actor="user:owner")
    assert (
        egress.grant(env.ctx, _card(env, project="shop2"), "dev") == ""
    )  # its project has no exit


def test_an_agent_without_the_capability_gets_no_exit(env, tmp_path):
    # kd.launch refuses an exit to an agent whose policy lacks net.egress: a grant would fail every launch
    # every tracked agent holds it, so the one without is a manifest of this test
    tree = tmp_path / "tree"
    for agent in ("dev", "boss"):
        source = Path(env.ctx.repo_root) / "agents" / agent / "agent.json"
        manifest = json.loads(source.read_text("utf-8"))
        if agent == "boss":
            manifest["capabilities"].remove("net.egress")
        (tree / "agents" / agent).mkdir(parents=True)
        (tree / "agents" / agent / "agent.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
    env.ctx.repo_root = str(tree)
    probe = {**FIXTURE["exits"]["probe"], "agents": ["dev", "boss"]}
    document = {
        **FIXTURE,
        "exits": {"probe": probe},
        "projects": {"digipaddock": "probe"},
    }
    (tmp_path / "egress.json").write_text(json.dumps(document), encoding="utf-8")
    assert egress.grant(env.ctx, _card(env), "dev") == "probe"
    assert egress.grant(env.ctx, _card(env, assignee="@boss"), "boss") == ""


def test_no_file_or_a_broken_file_grants_nothing(env, tmp_path):
    card = _card(env)
    env.ctx.egress_path = str(tmp_path / "none.json")
    assert egress.grant(env.ctx, card, "dev") == ""
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    env.ctx.egress_path = str(tmp_path / "broken.json")
    assert egress.grant(env.ctx, card, "dev") == ""


def test_a_brief_must_be_approved_and_unchanged(env):
    card = env.briefed()  # v1, not approved
    card = _edit(env, card, {"status": "In Progress"})
    assert egress.grant(env.ctx, card, "dev") == ""
    briefs.approve(env.store, card, by="user:owner")
    card = env.store.card(card.id)
    assert egress.grant(env.ctx, card, "dev") == "probe"
    card = _edit(
        env, card, {"description": "Цель: другой отчёт."}
    )  # the agreed scope moved on
    assert egress.grant(env.ctx, card, "dev") == ""


def _command(env, card):
    return env.store.enqueue(
        {
            "key": f"test:{card.id}",
            "agent": "dev",
            "thread": card.thread,
            "card": card.id,
            "kind": "message",
            "text": f"[board] {card.id} work",
        }
    )


def _tick(env, host, now=None):
    return asyncio.run(dispatcher.tick(env.svc, host=host, now=now or env.when()))


def test_delivery_names_the_exit(env):
    card = _card(env)
    _command(env, card)
    host = Host()
    _tick(env, host)
    [request] = [c for c in host.calls if c["op"] == "deliver"]
    assert request["egress"] == "probe"


def test_a_chat_delivery_has_no_exit(env):
    chat = env.store.agent_thread("dev")
    env.store.enqueue(
        {
            "key": "test:chat",
            "agent": "dev",
            "thread": chat,
            "card": "",
            "kind": "chat",
            "text": "[chat] hi",
        }
    )
    host = Host()
    _tick(env, host)
    [request] = [c for c in host.calls if c["op"] == "deliver"]
    assert "egress" not in request


def test_an_older_host_gets_the_delivery_without_egress(env):
    card = _card(env)
    command = _command(env, card)
    host = Host(
        dispatcher.HostRefused("invalid", "unknown field for deliver"),  # the role's context limit, the newest
        dispatcher.HostRefused("invalid", "unknown field for deliver"),
        {"ok": True, "state": "queued"},
    )
    assert _tick(env, host)["delivered"] == 1
    delivers = [c for c in host.calls if c["op"] == "deliver"]
    assert [c.get("egress") for c in delivers] == ["probe", "probe", None]
    assert env.store.command(command["id"])["state"] == "delivered"


def test_an_older_host_drops_egress_before_failing_a_model_delivery():
    request = {"op": "deliver", "effort": "high", "egress": "probe"}
    retry = dispatcher._ordinary(
        request, dispatcher.HostRefused("invalid", "unknown field for deliver")
    )
    assert retry == {"op": "deliver", "effort": "high"}


def _live(env, card, exit_name="probe"):
    session = env.store.session_for(card.thread, "dev")
    return {
        "session": session["id"],
        "agent": "dev",
        "state": "working",
        "slot": "dev-1",
        "queued_inputs": 0,
        "card": card.id,
        "egress": exit_name,
    }


def _revokes(host):
    return [c for c in host.calls if c["op"] == "revoke"]


def test_sweep_revokes_the_exit_of_a_process_whose_consent_ended(env):
    # a revoke, never a stop command: the cabinet must not read it as the owner's stop (continuations, outcomes)
    card = _card(env)
    host = Host()
    host.sessions = [_live(env, card)]
    _edit(env, card, {"status": "Done"})
    _tick(env, host)
    session = env.store.session_for(card.thread, "dev")
    assert _revokes(host) == [
        {"op": "revoke", "session": session["id"], "egress": "probe"}
    ]
    assert not [c for c in env.store.commands(card=card.id) if c["kind"] == "stop"]
    _tick(env, host, now=env.when() + timedelta(seconds=dispatcher.SWEEP_S - 1))
    assert len(_revokes(host)) == 1  # one sweep per SWEEP_S


def test_sweep_leaves_a_consented_process(env):
    card = _card(env)
    host = Host()
    host.sessions = [_live(env, card)]
    _tick(env, host)
    assert _revokes(host) == []


def test_sweep_revokes_a_process_reassigned_away(env):
    card = _card(env)
    host = Host()
    host.sessions = [_live(env, card)]
    _edit(env, card, {"assignee": "@boss"})
    _tick(env, host)
    assert len(_revokes(host)) == 1


def test_an_older_host_refusing_revoke_does_not_stop_the_tick(env):
    card = _card(env)
    _command(env, card)
    host = Host()
    host.sessions = [_live(env, _card(env, status="Done"))]
    real = host.call

    async def call(request, timeout=10.0):
        if request["op"] == "revoke":
            raise dispatcher.HostRefused("invalid", "unknown op")
        return await real(request, timeout)

    host.call = call
    assert _tick(env, host)["delivered"] == 1


def _waiting(env, card):
    # the host names the exit a session that waits for a slot will start with
    return {**_live(env, card), "state": "waiting", "slot": "", "queued_inputs": 1}


def test_sweep_revokes_the_exit_of_a_waiting_session_whose_card_left_work(env):
    card = _card(env)
    host = Host()
    host.sessions = [_waiting(env, card)]
    _edit(env, card, {"status": "Done"})
    _tick(env, host)
    session = env.store.session_for(card.thread, "dev")
    assert _revokes(host) == [
        {"op": "revoke", "session": session["id"], "egress": "probe"}
    ]


def test_sweep_revokes_the_exit_of_a_waiting_session_once_the_file_drops_it(
    env, tmp_path
):
    card = _card(env)
    host = Host()
    host.sessions = [_waiting(env, card)]
    other = {**FIXTURE["exits"]["probe"], "port": 3141}
    document = {
        **FIXTURE,
        "exits": {"other": other},
        "projects": {"digipaddock": "other"},
    }
    (tmp_path / "egress.json").write_text(json.dumps(document), encoding="utf-8")
    _tick(env, host)
    session = env.store.session_for(card.thread, "dev")
    assert _revokes(host) == [
        {"op": "revoke", "session": session["id"], "egress": "probe"}
    ]


def test_sweep_leaves_a_consented_waiting_session(env):
    card = _card(env)
    host = Host()
    host.sessions = [_waiting(env, card)]
    _tick(env, host)
    assert _revokes(host) == []


def test_sweep_asks_the_host_nothing_without_exits(env, tmp_path):
    env.ctx.egress_path = str(tmp_path / "none.json")
    host = Host()
    _tick(env, host)
    assert not [c for c in host.calls if c["op"] == "status"]


# --- a card's egress:<code> label chooses the country of the exit -----------------------------------------

def _web(port, upstream):
    return {**FIXTURE["exits"]["probe"], "port": port, "upstream": upstream, "domains": ["*"]}


COUNTRIES = {
    **FIXTURE,
    "exits": {
        **FIXTURE["exits"],
        "web": _web(3141, "127.0.0.1:1156"),
        "web-ae": _web(3142, "127.0.0.1:1151"),
    },
    "projects": {"digipaddock": "probe"},
    "standing": "web",
    "countries": {"GB": "web", "AE": "web-ae"},
}


@pytest.fixture
def countries(env, tmp_path):
    (tmp_path / "egress.json").write_text(json.dumps(COUNTRIES), encoding="utf-8")
    return env


@pytest.mark.parametrize("status", ["Inbox", "To Do", "In Progress", "Review", "Blocked"])
def test_a_country_label_gives_its_exit_on_any_open_card(countries, status):
    card = _card(countries, status=status, labels=["egress:ae"])
    assert egress.choose(countries.ctx, card, "dev") == ("web-ae", "")


def test_a_country_label_needs_the_agents_open_card(countries):
    for over in ({"status": "Done"}, {"assignee": "@boss"}):
        assert egress.grant(countries.ctx, _card(countries, labels=["egress:ae"], **over), "dev") == "", over


def test_a_country_label_beats_the_project_exit_and_needs_no_approved_brief(countries):
    card = countries.briefed(labels=["spec:task-brief", "egress:ae"])  # v1, not approved
    card = _edit(countries, card, {"status": "In Progress"})
    assert egress.grant(countries.ctx, card, "dev") == "web-ae"
    assert egress.grant(countries.ctx, _card(countries), "dev") == "probe"  # no label: the project's, as before


def test_a_country_whose_exit_does_not_list_the_agent_is_gone(countries, tmp_path):
    document = {**COUNTRIES, "exits": {**COUNTRIES["exits"], "web-ae": {
        **COUNTRIES["exits"]["web-ae"], "agents": ["analyst"]}}}
    (tmp_path / "egress.json").write_text(json.dumps(document), encoding="utf-8")
    assert egress.choose(countries.ctx, _card(countries, labels=["egress:ae"]), "dev") == ("probe", "AE")


def test_a_country_gone_from_the_file_delivers_without_it_and_says_so(countries, tmp_path):
    card = _card(countries, labels=["egress:fr"])
    _command(countries, card)
    host = Host()
    _tick(countries, host)
    [request] = [c for c in host.calls if c["op"] == "deliver"]
    assert request["egress"] == "probe"  # the project's exit; without one the launch takes the standing exit
    [line] = [m for m in countries.store.thread_messages(card.thread) if m["kind"] == "status"]
    assert line["payload"] == {"key": "sys.session.egress_country_gone", "params": {"label": "egress:fr"}}


def test_delivery_names_the_country_exit(countries):
    card = _card(countries, labels=["egress:ae"])
    _command(countries, card)
    host = Host()
    _tick(countries, host)
    [request] = [c for c in host.calls if c["op"] == "deliver"]
    assert request["egress"] == "web-ae"


def test_sweep_leaves_a_turn_whose_country_changed(countries):
    card = _card(countries, labels=["egress:ae"])
    host = Host()
    host.sessions = [_live(countries, card, "web-ae")]
    _edit(countries, card, {"labels": ["egress:gb"]})
    _tick(countries, host)
    assert _revokes(host) == []
    # the card leaving work still ends the exit
    _edit(countries, card, {"status": "Done"})
    _tick(countries, host, now=countries.when() + timedelta(seconds=dispatcher.SWEEP_S))
    assert len(_revokes(host)) == 1


def _label(env, card, **request):
    record = env.store.card(card.id)
    return env.call("digi", {"op": "worker.edit", "agent": "dev", "card": card.id,
                             "revision": record.revision, **request})


def test_an_unknown_country_label_is_refused_with_the_known_ones(countries):
    card = _card(countries, status="In Progress")
    with pytest.raises(api.ApiError) as err:
        _label(countries, card, add_label=["egress:zz"])
    assert "egress:ae, egress:gb" in str(err.value)
    _label(countries, card, add_label=["egress:ae"])
    assert "egress:ae" in countries.store.card(card.id).labels


def test_a_second_country_label_is_refused(countries):
    card = _card(countries, status="In Progress", labels=["egress:ae"])
    with pytest.raises(api.ApiError, match="one egress: label"):
        _label(countries, card, add_label=["egress:gb"])
    _label(countries, card, add_label=["egress:gb"], remove_label=["egress:ae"])  # a change is one edit
    assert egress.grant(countries.ctx, countries.store.card(card.id), "dev") == "web"


def test_a_card_keeps_a_label_whose_country_left_the_file(countries, tmp_path):
    card = _card(countries, status="In Progress", labels=["egress:fr"])
    _label(countries, card, add_label=["plain"])
    assert {"egress:fr", "plain"} <= set(countries.store.card(card.id).labels)
