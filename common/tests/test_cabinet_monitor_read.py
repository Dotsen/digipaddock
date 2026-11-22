"""agents with monitor.read read the monitor's incidents and servers through the base executor."""

import sys
from pathlib import Path

import pytest

from kd.cabinet import api

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cabinet_env import CabinetEnv  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
AT = "2026-10-14T13:00:00+03:00"
READS = [
    {"op": "incident.get", "incident": "P-1"},
    {"op": "incident.query", "status": "all"},
    {"op": "incident.list"},
    {"op": "server.list"},
    {"op": "server.get", "alias": "app1"},
]


@pytest.fixture
def env(tmp_path):
    e = CabinetEnv(tmp_path, repo_root=ROOT)  # the repository's manifests: boss and analyst hold monitor.read
    servers = [{"alias": "app1", "host": "10.0.0.1", "unreachable_count": 0}]
    rows = [{"subject": "/", "level": "crit", "value": "96%", "text": "/: 96%"}]
    e.call("digi-sysadmin", {"op": "monitor.push", "items": [
        {"key": "s1", "kind": "sync", "at": AT, "servers": servers,
         "checks": [{"server": "app1", "check": "disk", "rows": rows}],
         "changes": [{"server": "app1", "check": "disk", "subject": "/", "level": "crit", "prev": "ok",
                      "value": "96%", "text": "/: 96%", "at": AT}]},
        {"key": "o1", "kind": "open", "problem": {
            "key": "app1:disk:/", "server": "app1", "check": "disk", "subject": "/", "level": "crit",
            "max_level": "crit", "value": "96%", "text": "/: 96%", "opened_at": AT, "repeats": 0}}]})
    return e


def executor(env, agent, request):
    return env.call("digi", {**request, "agent": agent})


@pytest.mark.parametrize("agent", ["boss", "analyst"])
def test_an_agent_with_monitor_read_reads_incidents_and_servers_through_the_executor(env, agent):
    got = executor(env, agent, {"op": "incident.get", "incident": "P-1"})
    assert (got["incident"]["id"], got["incident"]["status"], got["incident"]["server"]) == ("P-1", "open", "app1")
    assert [e["kind"] for e in got["timeline"]] == ["incident.opened"]
    assert [i["id"] for i in executor(env, agent, {"op": "incident.query"})["items"]] == ["P-1"]
    assert [s["alias"] for s in executor(env, agent, {"op": "server.list"})["items"]] == ["app1"]
    server = executor(env, agent, {"op": "server.get", "alias": "app1"})
    assert [(h["prev"], h["level"]) for h in server["history"]] == [("ok", "crit")]


def test_boss_links_a_card_to_an_incident_with_its_label(env):
    # AC6: kd-boss-board set-labels --add P-<n>; the incident's cards come from that label
    card = env.card(project="admin", assignee="@sysadmin")
    env.call("digi", {"op": "boss.set-labels", "agent": "boss", "task": card.id, "revision": card.revision,
                     "reason": "работа по инциденту", "add": ["P-1"], "remove": []})
    got = executor(env, "boss", {"op": "incident.get", "incident": "P-1"})
    assert [c["id"] for c in got["cards"]] == [card.id]


@pytest.mark.parametrize("request_", READS, ids=lambda r: r["op"])
@pytest.mark.parametrize("agent", ["dev", "sysadmin", "nobody-here", ""])
def test_an_executor_naming_an_agent_without_monitor_read_is_refused(env, agent, request_):
    # dev and sysadmin hold no monitor.read in their manifests; an unknown or missing name is no agent
    with pytest.raises(api.ApiError) as raised:
        executor(env, agent, request_)
    assert raised.value.kind == "refused"


@pytest.mark.parametrize("request_", READS, ids=lambda r: r["op"])
def test_an_executor_without_an_agent_and_other_agents_users_are_refused(env, request_):
    with pytest.raises(api.ApiError):
        env.call("digi", request_)
    for user in ("digi-boss", "digi-analyst", "digi-dev"):  # an agent's own user is no executor
        with pytest.raises(api.ApiError):
            env.call(user, request_)


def test_a_manifest_the_cabinet_cannot_read_is_refused(tmp_path):
    e = CabinetEnv(tmp_path, repo_root=tmp_path / "no-tree")
    with pytest.raises(api.ApiError) as raised:
        executor(e, "boss", {"op": "server.list"})
    assert raised.value.kind == "refused"


WRITES = [
    {"op": "incident.ack", "incident": "P-1"},
    {"op": "incident.unack", "incident": "P-1"},
    {"op": "incident.mute", "incident": "P-1", "until": "2026-10-15T10:00:00Z"},
    {"op": "incident.unmute", "incident": "P-1"},
    {"op": "monitor_trust.propose", "incident": "P-1"},
    {"op": "monitor_trust.remove", "incident": "P-1"},
    {"op": "monitor_trust.take"},
    {"op": "monitor_trust.report"},
    {"op": "incident.link", "incident": "P-1", "card": "KD-1"},
    {"op": "incident.handoff", "incident": "P-1", "project": "admin"},
    {"op": "incident.retry", "incident": "P-1"},
    {"op": "server.check.request", "alias": "app1"},
    {"op": "server.check.take"},
    {"op": "monitor.push", "items": []},
    {"op": "monitor.settings.get"},
    {"op": "monitor.test.get"},
]


@pytest.mark.parametrize("request_", WRITES, ids=lambda r: r["op"])
@pytest.mark.parametrize("agent", ["boss", "analyst"])
def test_monitor_read_opens_no_write(env, agent, request_):
    before = env.store._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    with pytest.raises(api.ApiError) as raised:
        executor(env, agent, request_)
    assert raised.value.kind == "refused"
    assert env.store._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before
    status = env.store._conn.execute("SELECT status, muted_until FROM incidents WHERE n=1").fetchone()
    assert tuple(status) == ("open", "")
