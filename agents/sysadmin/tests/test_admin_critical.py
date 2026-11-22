"""kd-admin-critical runs an owner-approved exact command once; consume comes before SSH."""
import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gate.agent_ro_gate import Result
from sysadmin import admin_critical, execute

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "common"))
from kd import admin_action, admin_proposal, admin_signed, exec_envelope  # noqa: E402
from kd import admin_operations as ops  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "common" / "tests"))  # admin_board
from admin_board import AdminEnv  # noqa: E402 — the board is the cabinet

NOW = datetime.now(timezone.utc).replace(microsecond=0)
AGENT_UID = 4242
CARD = """---
id: KD-123
title: 'node-c: drop a WireGuard peer'
status: In Progress
assignee:
  - '@sysadmin'
labels:
  - 'server:node-c'
  - 'server:node-e'
project: admin
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Drop the stale peer.
<!-- SECTION:DESCRIPTION:END -->
"""
COMMAND = "wg set wg0 peer 'abc=' remove"


class Runner:
    def __init__(self, result=None):
        self.result = result or Result(0, b"done\n", False, False)
        self.calls, self.feeds = [], []

    def __call__(self, argv, timeout, limit, kill_on_limit, feed=None):
        self.calls.append(argv)
        self.feeds.append(list(feed) if feed else None)
        return self.result


class Consume:
    def __init__(self, store, card_reader, reply=None):
        self.store, self.card_reader, self.reply = store, card_reader, reply
        self.calls = []

    def __call__(self, task_id, proposal_id, action_sha256):
        self.calls.append((task_id, proposal_id, action_sha256))
        if self.reply is not None:
            return self.reply
        try:
            used = self.store.consume(task_id, proposal_id, action_sha256, self.card_reader(), NOW)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "action_id": used.action_id}


@pytest.fixture
def env(tmp_path, enabled_all):
    (tmp_path / "grants").mkdir()
    (tmp_path / "proposals").mkdir()
    (tmp_path / "enabled").write_text(enabled_all.read_text("utf-8"), encoding="utf-8")
    return AdminEnv(tmp_path, CARD, user="digi-exec")


def card(env):
    return env.board.card()


def approve(env, servers=("node-c",), command=COMMAND, now=NOW, store=False):
    """The owner's approval of the action; ``store`` — the action is stored as kd-admin-propose-action stores it
    and kept in the grant store as the cabinet keeps it on approval (what an envelope carries)."""
    c = card(env)
    data = {"schema_version": 2, "task_id": "KD-123", "intent": "x", "basis": "b", "servers": list(servers),
            "command": command, "effect": "e", "risk": {"level": "high", "why": "w"}, "rollback": "y"}
    action = admin_action.validate_action(data, ops.load_inventory(), ops.load_enabled(env / "enabled"),
                                          card_servers=c.servers)
    if store:
        admin_proposal.submit_action(str(env / "proposals"), json.dumps(data).encode("utf-8"), agent="sysadmin",
                                     card=c, inventory=ops.load_inventory(), enabled=ops.load_enabled(env / "enabled"),
                                     now=now, deny=lambda command, alias: None)
        stored = admin_proposal.load_action(str(env / "proposals"), "KD-123", action.proposal_id)
        admin_signed.write_action(str(env / "grants"), stored, action.as_dict(), action.proposal_id)
    grants = admin_action.ActionStore(str(env / "grants"), None)
    grants.approve(c, action, now=now, via="test", expect_card_sha256=c.scope_sha256,
                   expect_action_sha256=action.sha256)
    return action.proposal_id, grants


def run(env, argv, *, caller_uid=AGENT_UID, runner=None, consume=None, sent=None, envelopes=False):
    runner = runner or Runner()
    store = admin_action.ActionStore(str(env / "grants"), None)
    consume = consume or Consume(store, lambda: card(env))
    sent = [] if sent is None else sent
    out, err = io.BytesIO(), io.StringIO()
    log = env / "exec.jsonl"
    environ = {} if caller_uid is None else {"KD_CALLER_UID": str(caller_uid)}
    code = admin_critical.main(argv, env=environ, client=env.board, grants=str(env / "grants"),
                               enabled_path=env / "enabled", log_path=log, runner=runner,
                               notify=lambda *notice: sent.append(notice) or True, out=out, err=err, now=lambda: NOW,
                               agent_uid=AGENT_UID, approve_uid=None, consume=consume, envelopes=envelopes)
    records = [json.loads(line) for line in log.read_text("utf-8").splitlines()] if log.exists() else []
    return code, out.getvalue(), err.getvalue(), runner.calls, consume.calls, records


def test_runs_the_exact_command_once(env):
    pid, store = approve(env)
    code, out, err, calls, consumed, log = run(env, ["--agent", "sysadmin", "--task", "KD-123", "--action", pid])
    assert code == 0, err
    assert len(calls) == 1 and calls[0][-1].endswith(COMMAND) or COMMAND in calls[0][-1]
    assert len(consumed) == 1
    start = [r for r in log if r["event"] == "start"][0]
    assert start["kind"] == "critical" and start["task_id"] == "KD-123" and start["proposal_id"] == pid
    assert start["action_id"] == store.load("KD-123", pid).action_id
    again = run(env, ["--task", "KD-123", "--action", pid])
    assert again[0] == 2 and again[3] == [] and "consumed" in again[2]


def test_critical_run_posts_to_the_grants_card(env):
    pid, _ = approve(env)
    sent = []
    code, *_ = run(env, ["--agent", "sysadmin", "--task", "KD-123", "--action", pid], sent=sent)
    assert code == 0 and len(sent) == 1
    assert (sent[0][1], sent[0][2]["agent"], sent[0][3]) == ("notice.exec.result", "@sysadmin", "KD-123")


def test_group_runs_in_order(env):
    pid, _ = approve(env, servers=("node-c", "node-e"))
    code, out, err, calls, _, log = run(env, ["--task", "KD-123", "--action", pid])
    assert code == 0, err
    assert len(calls) == 2
    starts = [r for r in log if r["event"] == "start"]
    assert [r["server"] for r in starts] == ["node-c", "node-e"] and all(r["kind"] == "critical" for r in starts)


def test_admin_critical_sends_an_envelope_per_server(env):
    """each server gets its own envelope of the one signed action; the gate word is the remote command."""
    pid, grants = approve(env, servers=("node-c", "node-e"), store=True)
    runner = Runner()
    code, out, err, calls, consumed, log = run(env, ["--task", "KD-123", "--action", pid], runner=runner,
                                               envelopes=True)
    assert code == 0, err
    assert [argv[-1] for argv in calls] == [exec_envelope.GATE_RUN] * 2 and len(consumed) == 1
    envelopes = [json.loads(feed[0]) for feed in runner.feeds]
    assert [e["server"] for e in envelopes] == ["node-c", "node-e"]
    assert all(e["kind"] == "action" and e["command"] == COMMAND and "step" not in e for e in envelopes)
    used = grants.load("KD-123", pid)
    assert envelopes[0]["payload"]["proposal_id"] == pid and envelopes[0]["grant"]["action_id"] == used.action_id
    assert envelopes[0]["proposal"]["servers"] == ["node-c", "node-e"] and envelopes[0]["assertion"] is None
    starts = [r for r in log if r["event"] == "start"]
    assert [r["server"] for r in starts] == ["node-c", "node-e"] and all(r["envelope"] is True for r in starts)
    # one server: the same, directly
    pid, _ = approve(env, store=True)
    runner = Runner()
    code, _, err, calls, _, log = run(env, ["--task", "KD-123", "--action", pid], runner=runner, envelopes=True)
    assert code == 0, err
    assert calls[0][-1] == exec_envelope.GATE_RUN and json.loads(runner.feeds[0][0])["server"] == "node-c"


def test_admin_critical_without_a_kept_action_refuses_before_consume(env):
    pid, _ = approve(env)  # approved, but the cabinet kept no copy of the signed action
    code, _, err, calls, consumed, log = run(env, ["--task", "KD-123", "--action", pid], envelopes=True)
    assert code == 2 and calls == [] and consumed == [] and "a new approval is needed" in err
    assert [r["event"] for r in log] == ["admin_refused"]
    code, _, err, calls, consumed, _ = run(env, ["--task", "KD-123", "--action", pid], envelopes=False)
    assert code == 0 and len(calls) == 1 and len(consumed) == 1  # without envelopes the grant's command is enough


def test_admin_critical_refuses_a_kept_action_that_is_not_the_approved_one(env):
    pid, _ = approve(env, store=True)
    path = Path(admin_signed.action_path(str(env / "grants"), "KD-123", pid))
    body = json.loads(path.read_text("utf-8"))
    body["proposal"]["command"] = "reboot"
    path.write_text(json.dumps(body), encoding="utf-8")
    code, _, err, calls, consumed, log = run(env, ["--task", "KD-123", "--action", pid], envelopes=True)
    assert code == 2 and calls == [] and consumed == [] and f"is not action {pid}" in err
    assert [r["event"] for r in log] == ["admin_refused"]


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root reads any file")
def test_admin_critical_never_reads_the_proposals(env):
    pid, _ = approve(env, store=True)
    proposals = env / "proposals"
    proposals.chmod(0)
    try:
        code, _, err, calls, consumed, _ = run(env, ["--task", "KD-123", "--action", pid], envelopes=True)
    finally:
        proposals.chmod(0o750)
    assert code == 0, err
    assert len(calls) == 1 and len(consumed) == 1


@pytest.mark.parametrize("argv", [
    ["--task", "KD-123"], ["--action", "0" * 16], ["--task", "KD-123", "--action", "XYZ"],
    ["--task", "KD-123", "--action", "0" * 16, "reboot"], ["--task", "KD-123", "--action", "0" * 16, "--server", "a"],
])
def test_bad_syntax_never_reaches_ssh(env, argv):
    approve(env)
    code, _, _, calls, consumed, _ = run(env, argv)
    assert code == 2 and calls == [] and consumed == []


@pytest.mark.parametrize("caller_uid", [None, 0, AGENT_UID + 1])
def test_only_the_sysadmin_through_its_channel(env, caller_uid):
    pid, _ = approve(env)
    code, _, err, calls, consumed, _ = run(env, ["--task", "KD-123", "--action", pid], caller_uid=caller_uid)
    assert code == 2 and calls == [] and consumed == []


def test_without_approval_nothing_runs(env):
    code, _, err, calls, consumed, log = run(env, ["--task", "KD-123", "--action", "0" * 16])
    assert code == 2 and calls == [] and consumed == [] and "approv" in err
    assert log[-1]["event"] == "admin_refused"


def test_refused_consume_means_no_ssh(env):
    pid, _ = approve(env)
    code, _, err, calls, consumed, _ = run(env, ["--task", "KD-123", "--action", pid],
                                           consume=Consume(None, None, {"ok": False, "error": "service down"}))
    assert code == 2 and calls == [] and len(consumed) == 1 and "service down" in err


def test_consume_failure_raises_no_ssh(env):
    pid, _ = approve(env)

    def broken(task_id, proposal_id, action_sha256):
        raise OSError("no socket")

    broken.calls = []
    code, _, err, calls, _, _ = run(env, ["--task", "KD-123", "--action", pid], consume=broken)
    assert code == 2 and calls == []


def test_expired_approval_is_refused_before_consume(env):
    pid, _ = approve(env, now=NOW - timedelta(minutes=16))
    code, _, err, calls, consumed, _ = run(env, ["--task", "KD-123", "--action", pid])
    assert code == 2 and calls == [] and consumed == [] and "expired" in err


@pytest.mark.parametrize("old,new", [("Drop the stale peer.", "Drop all peers."), ("status: In Progress", "status: Done")])
def test_changed_card_is_refused_before_consume(env, old, new):
    pid, _ = approve(env)
    env.board.put(CARD.replace(old, new))
    code, _, _, calls, consumed, _ = run(env, ["--task", "KD-123", "--action", pid])
    assert code == 2 and calls == [] and consumed == []


def test_tampered_command_is_refused(env):
    pid, store = approve(env)
    path = Path(store.path("KD-123", pid))
    data = json.loads(path.read_text("utf-8"))
    data["command"] = "reboot"
    path.write_text(json.dumps(data), encoding="utf-8")
    code, _, err, calls, consumed, _ = run(env, ["--task", "KD-123", "--action", pid])
    assert code == 2 and calls == [] and consumed == [] and "sha256" in err


def test_denylist_before_consume(env, monkeypatch):
    pid, _ = approve(env)
    monkeypatch.setattr(execute, "_check", lambda command, alias: ("test-rule", "because"))
    code, _, _, calls, consumed, log = run(env, ["--task", "KD-123", "--action", pid])
    assert code == 126 and calls == [] and consumed == []
    assert log[-1]["event"] == "denied" and log[-1]["task_id"] == "KD-123"


def test_disabled_server_is_refused_before_consume(env):
    pid, _ = approve(env)
    (env / "enabled").write_text("example-stage\n", encoding="utf-8")
    code, _, _, calls, consumed, _ = run(env, ["--task", "KD-123", "--action", pid])
    assert code == 2 and calls == [] and consumed == []


def test_a_sudo_caller_is_not_a_channel_caller(env, monkeypatch):
    # the exec channel's service names the caller; SUDO_UID is any process's to set.
    pid, _ = approve(env)
    code, _, err, calls, consumed, _ = run(env, ["--task", "KD-123", "--action", pid], caller_uid=None)
    assert code == 2 and calls == [] and consumed == [] and "through its channel" in err


def test_consume_asks_the_cabinet_on_its_socket():
    from kd.cabinet import client as cabinet_client

    calls = []

    class Cabinet:
        def call(self, request):
            calls.append(request)
            return {"action_id": "x"}

    class Refusing:
        def call(self, request):
            raise cabinet_client.CabinetError("refused", "KD-1: action is consumed; a new approval is needed")

    assert admin_critical.socket_consume("KD-1", "a" * 16, "b" * 64, client=Cabinet()) == {"ok": True, "action_id": "x"}
    assert calls == [{"op": "action.consume", "task_id": "KD-1", "proposal_id": "a" * 16, "action_sha256": "b" * 64}]
    assert admin_critical.socket_consume("KD-1", "a" * 16, "b" * 64, client=Refusing()) == {
        "ok": False, "error": "KD-1: action is consumed; a new approval is needed"}
