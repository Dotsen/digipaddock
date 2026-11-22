"""kd-monitor-read — the monitor's reads for boss and the analyst, every value masked, fail closed."""

import io
import json
import sys
from pathlib import Path

import pytest

from kd import monitor_read, redact
from kd.cabinet.client import CabinetError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cabinet_env import CabinetEnv  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
AT = "2026-10-14T13:00:00+03:00"
PEM = "-----BEGIN " + "PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END " + "PRIVATE KEY-----"  # split: the tree scan
SECRETS = ("hunter2-db-secret", "tok-9f8e7d6c5b4a", "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC")


@pytest.fixture
def env(tmp_path):
    e = CabinetEnv(tmp_path, repo_root=ROOT)
    servers = [{"alias": "srv-a", "host": "10.0.0.1", "unreachable_count": 0}]
    rows = [{"subject": "/", "level": "crit", "value": "96%", "text": "/: 96% DB_PASSWORD=hunter2-db-secret"},
            {"subject": "/var", "level": "ok", "value": "40%", "text": ""}]
    changes = [{"server": "srv-a", "check": "disk", "subject": "/", "level": "warn", "prev": "ok", "value": "91%",
                "text": "", "at": "2026-10-14T12:00:00+03:00"},
               {"server": "srv-a", "check": "disk", "subject": "/", "level": "warn", "prev": "warn", "value": "92%",
                "text": "same level", "at": "2026-10-14T12:30:00+03:00"},
               {"server": "srv-a", "check": "disk", "subject": "/", "level": "crit", "prev": "warn", "value": "96%",
                "text": "Authorization: Bearer tok-9f8e7d6c5b4a", "at": AT}]
    e.call("digi-sysadmin", {"op": "monitor.push", "items": [
        {"key": "s1", "kind": "sync", "at": AT, "servers": servers,
         "checks": [{"server": "srv-a", "check": "disk", "rows": rows}], "changes": changes},
        {"key": "o1", "kind": "open", "problem": {
            "key": "srv-a:disk:/", "server": "srv-a", "check": "disk", "subject": "/", "level": "crit",
            "max_level": "crit", "value": "96%", "text": "disk full " + PEM, "opened_at": AT, "repeats": 0}},
        {"key": "o2", "kind": "open", "problem": {
            "key": "srv-b:reach:-", "server": "srv-b", "check": "reach", "subject": "-", "level": "warn",
            "max_level": "warn", "value": "", "text": "slow", "opened_at": AT, "repeats": 0}}]})
    e.card_id = e.card(title="Диск srv-a", labels=["P-1"], project="admin", assignee="@sysadmin").id
    return e


def run(env, argv, agent="boss", user=None):
    out, err = io.StringIO(), io.StringIO()
    code = monitor_read.main(argv, env={"KD_AGENT": agent, "KD_CALLER_USER": user or f"digi-{agent}"},
                             client=env.client("digi"), registry_path=str(env.registry), repo_root=ROOT,
                             out=out, err=err)
    return code, out.getvalue(), err.getvalue()


# --- AC3: what an agent sees ----------------------------------------------------------------------------
def test_incident_shows_status_level_server_check_value_timeline_and_cards(env):
    code, out, err = run(env, ["incident", "P-1"], agent="analyst")
    assert (code, err) == (0, "")
    first = out.splitlines()[0]
    assert first == "P-1 · crit (max crit) · open · server srv-a · check disk / · value 96%"
    assert "timeline (1):" in out and "incident.opened by system:monitor" in out
    assert f"cards (1):\n  {env.card_id} · " in out and "Диск srv-a" in out
    assert "current: crit 96%" in out


def test_incidents_filter_by_status_server_level_and_period(env):
    code, out, _ = run(env, ["incidents"])
    assert code == 0 and "incidents (active): 2 — crit 1, warn 1" in out
    _, out, _ = run(env, ["incidents", "--server", "srv-b"])
    assert "P-2 · warn" in out and "P-1 ·" not in out
    _, out, _ = run(env, ["incidents", "--level", "crit"])
    assert "P-1 · crit" in out and "P-2" not in out
    _, out, _ = run(env, ["incidents", "--status", "resolved"])
    assert out.splitlines() == ["incidents (resolved): 0 — crit 0, warn 0"]
    _, out, _ = run(env, ["incidents", "--since", "2026-10-15"])
    assert "incidents (active): 0" in out
    _, out, _ = run(env, ["incidents", "--until", "2026-10-14", "--q", "slow"])
    assert "P-2 · warn" in out and "P-1 ·" not in out


def test_incidents_page_on_with_the_printed_cursor(env):
    _, out, _ = run(env, ["incidents", "--limit", "1"])
    more = out.splitlines()[-1]
    assert more.startswith("more: kd-monitor-read incidents --status active --limit 1 --cursor ")
    _, out2, _ = run(env, more.split(" ", 2)[2].split(" "))
    assert "P-2" in out2 and "more:" not in out2


def test_server_shows_checks_open_incidents_cards_and_the_weeks_changes(env):
    code, out, _ = run(env, ["server", "srv-a"])
    assert code == 0
    assert out.splitlines()[0].startswith("server srv-a · crit · reachable")
    assert "checks (2):\n  crit disk / 96%" in out
    assert "open incidents (1):\n  P-1 · crit · open · srv-a" in out
    assert "history (status changes, 7 days): 2 of 2" in out and "same level" not in out
    assert "warn→crit 96%" in out and "ok→warn 91%" in out
    _, every, _ = run(env, ["server", "srv-a", "--all"])
    assert "history (every row, 7 days): 3 of 3" in every and "same level" in every
    _, later, _ = run(env, ["server", "srv-a", "--since", "2026-10-14T09:30:00Z"])
    assert "2 of 2" not in later and "1 of 1" in later


def test_the_next_page_command_is_never_cut(env):
    query = "x" * 199
    args = monitor_read.parse(["incidents", "--server", "srv-a", "--level", "crit", "--q", query, "--limit", "1"])
    answer = {"total": 2, "counts": {}, "items": [], "next_cursor": "c" * 300}
    more = monitor_read.render(args, answer).splitlines()[-1]
    assert more.endswith("--cursor " + "c" * 300) and query in more


def test_servers_lists_every_server(env):
    code, out, _ = run(env, ["servers"])
    assert code == 0 and out.startswith("srv-a · crit · reachable")
    assert "open incidents 1" in out


def test_json_is_the_raw_answer(env):
    code, out, _ = run(env, ["incident", "P-1", "--json"])
    assert code == 0 and json.loads(out)["incident"]["id"] == "P-1"


# --- AC4: masks, fail closed ----------------------------------------------------------------------------
@pytest.mark.parametrize("argv", [["incident", "P-1"], ["incident", "P-1", "--json"], ["server", "srv-a"],
                                  ["server", "srv-a", "--json"], ["incidents", "--json"]])
def test_passwords_tokens_and_keys_are_masked(env, argv):
    code, out, _ = run(env, argv)
    assert code == 0 and out
    assert not [s for s in SECRETS if s in out]


def test_without_the_masks_nothing_is_printed(env, monkeypatch):
    monkeypatch.setattr(redact, "text", lambda value: None)
    for argv in (["incident", "P-1"], ["servers"], ["incidents", "--json"]):
        code, out, err = run(env, argv)
        assert (code, out) == (1, "") and "masks are unavailable" in err


def test_a_failing_masker_mid_answer_prints_nothing(env, monkeypatch):
    real, calls = redact.text, []

    def flaky(value):
        calls.append(value)
        return None if len(calls) > 5 else real(value)

    monkeypatch.setattr(redact, "text", flaky)
    code, out, _ = run(env, ["server", "srv-a"])
    assert (code, out) == (1, "")


# --- who may call ---------------------------------------------------------------------------------------
def test_an_agent_without_monitor_read_is_refused_before_the_cabinet(env):
    code, out, err = run(env, ["servers"], agent="dev")
    assert (code, out) == (2, "") and "monitor.read" in err


@pytest.mark.parametrize("channel", [{}, {"KD_AGENT": "boss"}, {"KD_AGENT": "boss", "KD_CALLER_USER": "digi-dev"}])
def test_a_caller_the_channel_does_not_name_as_the_agents_own_user_is_refused(env, channel):
    out, err = io.StringIO(), io.StringIO()
    code = monitor_read.main(["servers"], env=channel, client=env.client("digi"), registry_path=str(env.registry),
                             repo_root=ROOT, out=out, err=err)
    assert (code, out.getvalue()) == (2, "")


@pytest.mark.parametrize("argv", [["incident", "463"], ["incident", "P-1; rm"], ["server", "../etc"],
                                  ["incidents", "--limit", "101"], ["incidents", "--level", "ok"],
                                  ["incidents", "--since", "yesterday"], ["server", "srv-a", "--since",
                                                                          "2026-10-14T12:00:00+03:00"],
                                  ["ack", "P-1"], []])
def test_bad_arguments_are_refused(env, argv):
    code, out, err = run(env, argv)
    assert (code, out) == (2, "") and err.startswith("kd-monitor-read: usage")


class _Down:
    def __init__(self, kind):
        self.kind = kind

    def call(self, request):
        raise CabinetError(self.kind, "no")


@pytest.mark.parametrize("kind,code", [("refused", 2), ("not_found", 2), ("unavailable", 1)])
def test_cabinet_errors_map_to_exit_codes(env, kind, code):
    out, err = io.StringIO(), io.StringIO()
    got = monitor_read.main(["incident", "P-9"], env={"KD_AGENT": "boss", "KD_CALLER_USER": "digi-boss"},
                            client=_Down(kind), registry_path=str(env.registry), repo_root=ROOT, out=out, err=err)
    assert (got, out.getvalue()) == (code, "")


def test_the_request_names_the_channels_agent(env):
    seen = []

    class Spy:
        def call(self, request):
            seen.append(request)
            return {"ok": True, "items": []}

    monitor_read.main(["servers"], env={"KD_AGENT": "analyst", "KD_CALLER_USER": "digi-analyst"}, client=Spy(),
                      registry_path=str(env.registry), repo_root=ROOT, out=io.StringIO(), err=io.StringIO())
    assert seen == [{"op": "server.list", "agent": "analyst"}]
