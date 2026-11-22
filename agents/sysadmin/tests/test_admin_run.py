"""kd-admin-run runs one operation of the owner's grant; every check happens before SSH."""
import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gate.agent_ro_gate import Result
from sysadmin import admin_run, execute, inventory

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "common"))
from kd import admin_grant, admin_proposal, admin_signed, exec_envelope  # noqa: E402
from kd import admin_operations as ops  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "common" / "tests"))  # admin_board
from admin_board import AdminEnv  # noqa: E402 — the board is the cabinet

NOW = datetime.now(timezone.utc).replace(microsecond=0)
AGENT_UID = 4242
CARD = """---
id: KD-123
title: 'node-c: restart nginx'
status: In Progress
assignee:
  - '@sysadmin'
labels:
  - 'server:node-c'
project: admin
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Restart nginx on node-c.
<!-- SECTION:DESCRIPTION:END -->
"""
EXPLAIN = {"what": "Шаг", "effect": "Эффект", "risk": {"level": "low", "why": "Причина"}, "rollback": "Не нужен"}
PROPOSAL = {
    "schema_version": 2, "task_id": "KD-123", "intent": "Перезапустить nginx", "basis": "nginx отдаёт 502",
    "operations": [
        {"server": "node-c", "verb": "package.refresh", **EXPLAIN},
        {"server": "node-c", "verb": "service.restart", "unit": "nginx.service", **EXPLAIN},
    ],
}


class Runner:
    def __init__(self, result=None):
        self.result = result or Result(0, b"done\n", False, False)
        self.calls, self.feeds = [], []

    def __call__(self, argv, timeout, limit, kill_on_limit, feed=None):
        self.calls.append(argv)
        self.feeds.append(list(feed) if feed else None)
        return self.result


def make_env(tmp_path, enabled_all, card=CARD):
    (tmp_path / "grants").mkdir()
    (tmp_path / "proposals").mkdir()
    tmp_path.joinpath("enabled").write_text(enabled_all.read_text("utf-8"), encoding="utf-8")
    return AdminEnv(tmp_path, card, user="digi-exec")


@pytest.fixture
def env(tmp_path, enabled_all):
    return make_env(tmp_path, enabled_all)


def grant(env, *, proposal=PROPOSAL, now=NOW, ttl=timedelta(hours=72), keep=True):
    """The owner's grant; ``keep`` — with the signed plan's copy the cabinet writes first."""
    card = env.board.card()
    p = ops.validate_proposal(proposal, ops.load_catalog(), ops.load_inventory(), ops.load_enabled(env / "enabled"),
                              card_servers=card.servers)
    if keep:
        admin_signed.write_plan(str(env / "grants"), admin_proposal.Stored("KD-123", "sysadmin", now, p.as_dict()),
                                p.as_dict(), p.sha256)
    return admin_grant.Store(str(env / "grants"), None).approve(
        card, p, now=now, ttl=ttl, via="test", expect_card_sha256=card.scope_sha256, expect_proposal_sha256=p.sha256)


def store_proposal(env, proposal=PROPOSAL, now=NOW):
    """The sysadmin's stored plan (kd-admin-propose): what the envelope carries to the target."""
    return admin_proposal.submit(str(env / "proposals"), json.dumps(proposal).encode("utf-8"), agent="sysadmin",
                                 card=env.board.card(), catalog=ops.load_catalog(), inventory=ops.load_inventory(),
                                 enabled=ops.load_enabled(env / "enabled"), now=now)


def run(env, argv, *, caller_uid=AGENT_UID, runner=None, now=NOW, envelopes=False, approve_uid=None):
    runner = runner or Runner()
    sent, out, err = [], io.BytesIO(), io.StringIO()
    log = env / "exec.jsonl"
    environ = {} if caller_uid is None else {"KD_CALLER_UID": str(caller_uid)}
    code = admin_run.main(argv, env=environ, client=env.board, grants=str(env / "grants"),
                          enabled_path=env / "enabled", log_path=log, runner=runner,
                          notify=lambda *notice: sent.append(notice) or True, out=out, err=err, now=lambda: now,
                          agent_uid=AGENT_UID, approve_uid=approve_uid, envelopes=envelopes)
    records = [json.loads(line) for line in log.read_text("utf-8").splitlines()] if log.exists() else []
    return code, out.getvalue(), err.getvalue(), runner.calls, sent, records


def test_runs_the_approved_operation_over_the_exec_key(env):
    g = grant(env)
    code, out, err, calls, sent, log = run(env, ["--agent", "sysadmin", "--task", "KD-123", "--op", "2"])
    assert code == 0, err
    (argv,) = calls
    assert argv[:5] == ["/usr/bin/ssh", "-F", "/dev/null", "-i", execute.EXEC_KEY]
    server = inventory.get("node-c", enabled_path=env / "enabled")
    assert argv[-1] == execute.remote_command(server, "systemctl restart nginx.service")
    assert [r["event"] for r in log] == ["start", "result"]
    start = log[0]
    assert start["task_id"] == "KD-123" and start["grant_id"] == g.grant_id and start["op_index"] == 2
    assert start["operation"] == {"server": "node-c", "verb": "service.restart", "unit": "nginx.service", **EXPLAIN}
    assert start["command"] == "systemctl restart nginx.service" and start["agent"] == "sysadmin"
    assert b"done" in out and len(sent) == 1
    assert (sent[0][0], sent[0][1], sent[0][3]) == ("info", "notice.exec.result", "KD-123")


def test_first_operation_refreshes_packages(env):
    grant(env)
    code, _, err, calls, _, _ = run(env, ["--agent", "sysadmin", "--task", "KD-123", "--op", "1"])
    assert code == 0, err
    assert calls[0][-1].endswith("apt-get -q update") or "apt-get -q update" in calls[0][-1]


@pytest.mark.parametrize("argv", [
    ["--task", "KD-123"], ["--op", "1"], ["--task", "KD-123", "--op", "0"], ["--task", "KD-123", "--op", "31"],
    ["--task", "KD-123", "--op", "1x"], ["--task", "kd-123", "--op", "1"],
    ["--task", "KD-123", "--op", "1", "node-c", "reboot"], ["--task", "KD-123", "--op", "1", "--server", "node-c"],
])
def test_bad_syntax_never_reaches_ssh(env, argv):
    grant(env)
    code, _, _, calls, _, _ = run(env, argv)
    assert code == 2 and calls == []


@pytest.mark.parametrize("caller_uid", [None, 0, AGENT_UID + 1])
def test_only_the_sysadmin_through_its_channel(env, caller_uid):
    grant(env)
    code, _, err, calls, _, _ = run(env, ["--task", "KD-123", "--op", "1"], caller_uid=caller_uid)
    assert code == 2 and calls == [] and "sysadmin" in err


def test_without_a_grant_nothing_runs(env):
    code, _, err, calls, _, log = run(env, ["--task", "KD-123", "--op", "1"])
    assert code == 2 and calls == [] and "approv" in err
    assert log and log[-1]["event"] == "admin_refused" and log[-1]["task_id"] == "KD-123"


def test_operation_out_of_the_grant_is_refused(env):
    grant(env)
    code, _, err, calls, _, _ = run(env, ["--task", "KD-123", "--op", "3"])
    assert code == 2 and calls == [] and "3" in err


def test_revoked_grant_is_refused(env):
    grant(env)
    admin_grant.Store(str(env / "grants"), None).revoke("KD-123", now=NOW, via="test")
    code, _, err, calls, _, _ = run(env, ["--task", "KD-123", "--op", "1"])
    assert code == 2 and calls == [] and "revoked" in err


def test_expired_grant_is_refused(env):
    grant(env, now=NOW - timedelta(hours=73))
    code, _, err, calls, _, _ = run(env, ["--task", "KD-123", "--op", "1"])
    assert code == 2 and calls == [] and "expired" in err


@pytest.mark.parametrize("old,new,why", [
    ("Restart nginx on node-c.", "Restart nginx and reboot node-c.", "scope"),
    ("status: In Progress", "status: Done", "status"),
    ("status: In Progress", "status: Blocked", "status"),
    ("project: admin", "project: digipaddock", "project"),
])
def test_changed_card_is_refused(env, old, new, why):
    grant(env)
    env.board.put(CARD.replace(old, new))
    code, _, err, calls, _, _ = run(env, ["--task", "KD-123", "--op", "1"])
    assert code == 2 and calls == [] and why in err


def test_server_no_longer_enabled_is_refused(env):
    grant(env)
    (env / "enabled").write_text("example-stage\n", encoding="utf-8")
    code, _, err, calls, _, _ = run(env, ["--task", "KD-123", "--op", "1"])
    assert code == 2 and calls == []


def test_tampered_grant_operation_is_refused(env):
    grant(env)
    path = env / "grants" / "KD-123.json"
    data = json.loads(path.read_text("utf-8"))
    data["operations"][1] = {"server": "node-c", "verb": "service.restart", "unit": "ssh.service"}
    path.write_text(json.dumps(data), encoding="utf-8")
    code, _, err, calls, _, _ = run(env, ["--task", "KD-123", "--op", "2"])
    assert code == 2 and calls == [] and "unit" in err


def test_unreadable_grant_store_is_refused(env):
    grant(env)
    (env / "grants" / "KD-123.json").write_text("{", encoding="utf-8")
    code, _, _, calls, _, _ = run(env, ["--task", "KD-123", "--op", "1"])
    assert code == 2 and calls == []


def test_denylist_still_applies_to_the_formed_command(env, monkeypatch):
    grant(env)
    monkeypatch.setattr(execute, "_check", lambda command, alias: ("test-rule", "because"))
    code, _, err, calls, _, log = run(env, ["--task", "KD-123", "--op", "2"])
    assert code == 126 and calls == []
    assert log[-1]["event"] == "denied" and log[-1]["task_id"] == "KD-123"


def test_unwritable_journal_blocks_the_run(env):
    grant(env)
    runner = Runner()
    with pytest.raises(OSError):
        admin_run.main(["--task", "KD-123", "--op", "1"], env={"KD_CALLER_UID": str(AGENT_UID)}, client=env.board,
                       grants=str(env / "grants"), enabled_path=env / "enabled",
                       log_path=env / "missing" / "exec.jsonl", runner=runner, notify=lambda *notice: True,
                       out=io.BytesIO(), err=io.StringIO(), now=lambda: NOW, agent_uid=AGENT_UID, approve_uid=None,
                       envelopes=False)  # not the host's envelope switch: the journal is what this checks
    assert runner.calls == []


def test_failed_remote_command_returns_its_code(env):
    grant(env)
    code, _, _, calls, _, log = run(env, ["--task", "KD-123", "--op", "2"],
                                    runner=Runner(Result(3, b"failed\n", False, False)))
    assert code == 3 and log[-1]["rc"] == 3


def test_a_sudo_caller_is_not_a_channel_caller(env):
    # the exec channel's service names the caller; SUDO_UID is any process's to set.
    grant(env)
    sent, out, err = [], io.BytesIO(), io.StringIO()
    runner = Runner()
    code = admin_run.main(["--task", "KD-123", "--op", "1"], env={"SUDO_UID": str(AGENT_UID)},
                          client=env.board, grants=str(env / "grants"), enabled_path=env / "enabled",
                          log_path=env / "exec.jsonl", runner=runner, notify=lambda *notice: sent.append(notice) or True,
                          out=out, err=err, now=lambda: NOW, agent_uid=AGENT_UID, approve_uid=None)
    assert code == 2 and runner.calls == [] and "through its channel" in err.getvalue()



SCRIPT = "set -e\ncat > /etc/nginx/conf.d/x.conf <<'EOF'\nserver_tokens off;\nEOF\nnginx -t\n"


def test_a_script_step_is_sent_verbatim_by_kd_admin_run(env):
    # the exact text the owner signed is the command; a shell step no longer exists
    script = dict(PROPOSAL, operations=[{"server": "node-c", "verb": "package.refresh", **EXPLAIN},
                                        {"server": "node-c", "verb": "script", "script": SCRIPT, **EXPLAIN}])
    grant(env, proposal=script)
    code, _, err, calls, _, log = run(env, ["--task", "KD-123", "--op", "2"])
    assert code == 0, err
    (argv,) = calls
    assert argv[-1] == SCRIPT and log[0]["command"] == SCRIPT and log[0]["operation"]["script"] == SCRIPT


def test_a_script_step_on_the_factory_host_is_refused_at_run(tmp_path, enabled_all):
    # review I2: the proposal is refused at propose time on the factory's own host, but a grant may hold such a
    # step anyway (another path, an older catalog) — the executor refuses it itself, before any ssh
    env = make_env(tmp_path, enabled_all, card=CARD.replace("server:node-c", "server:example-stage"))
    home = dict(PROPOSAL, operations=[{"server": "example-stage", "verb": "script", "script": SCRIPT, **EXPLAIN}])
    grant(env, proposal=home)
    code, _, err, calls, _, log = run(env, ["--task", "KD-123", "--op", "1"])
    assert code == 2 and calls == [] and "factory's own host" in err
    assert log[0]["event"] == "admin_refused" and "factory's own host" in log[0]["reason"]


def test_a_script_step_with_no_home_in_the_inventory_is_refused(env, monkeypatch, tmp_path):
    # the home alias fails closed: an inventory without it refuses every script step, typed steps still run
    inv = json.loads((Path(inventory.INVENTORY)).read_text(encoding="utf-8"))
    del inv["home"]
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(inv), encoding="utf-8")
    monkeypatch.setattr(inventory, "INVENTORY", path)
    monkeypatch.setattr(ops, "INVENTORY_PATH", path)
    script = dict(PROPOSAL, operations=[{"server": "node-c", "verb": "package.refresh", **EXPLAIN},
                                        {"server": "node-c", "verb": "script", "script": SCRIPT, **EXPLAIN}])
    grant(env, proposal=script)
    code, _, err, calls, _, _ = run(env, ["--task", "KD-123", "--op", "2"])
    assert code == 2 and calls == [] and "no home server" in err
    code, _, err, calls, _, _ = run(env, ["--task", "KD-123", "--op", "1"])
    assert code == 0, err


# --- the step goes to the target's exec gate as an envelope ------------------------------------------------

def test_admin_run_sends_the_gate_word_and_an_envelope_when_enabled(env):
    store_proposal(env)
    g = grant(env)
    runner = Runner()
    code, out, err, calls, sent, log = run(env, ["--agent", "sysadmin", "--task", "KD-123", "--op", "1"],
                                           runner=runner, envelopes=True)
    assert code == 0, err
    (argv,) = calls
    assert argv[-1] == exec_envelope.GATE_RUN == "agent-exec-gate run" and argv[-2] == "--"
    (feed,) = runner.feeds
    envelope = json.loads(feed[0])
    assert envelope["kind"] == "plan" and envelope["step"] == 1 and envelope["server"] == "node-c"
    assert envelope["command"] == "apt-get -q update" and envelope["task_id"] == "KD-123"
    assert envelope["payload"] == {"task_id": "KD-123", "proposal_sha256": g.proposal_sha256,
                                   "card_scope_sha256": g.card_scope_sha256}
    assert envelope["proposal"]["intent"] == PROPOSAL["intent"] and len(envelope["proposal"]["operations"]) == 2
    assert envelope["grant"]["id"] == g.grant_id and envelope["assertion"] is None  # the test grant is unsigned
    assert envelope["decision"]["id"] == "" != g.decision_id  # the cabinet's decision, never the grant's journal id
    assert log[0]["event"] == "start" and log[0]["envelope"] is True and log[0]["command"] == "apt-get -q update"
    assert b"done" in out and len(sent) == 1


def test_admin_run_sends_the_raw_command_when_disabled(env):
    store_proposal(env)
    grant(env)
    runner = Runner()
    code, _, err, calls, _, log = run(env, ["--task", "KD-123", "--op", "1"], runner=runner, envelopes=False)
    assert code == 0, err
    assert calls[0][-1] == "apt-get -q update" and runner.feeds == [None] and log[0]["envelope"] is False


def test_admin_run_keeps_the_signed_plan_after_a_new_proposal(env):
    # a newer proposal on the card replaced the stored plan and stranded the live grant's remaining steps
    # («the owner approved another plan»); the signed plan is kept under its hash
    store_proposal(env)
    g = grant(env)
    store_proposal(env, dict(PROPOSAL, intent="Другое намерение"))
    runner = Runner()
    code, _, err, calls, _, _ = run(env, ["--task", "KD-123", "--op", "2"], runner=runner, envelopes=True)
    assert code == 0, err
    envelope = json.loads(runner.feeds[0][0])
    assert envelope["proposal"]["intent"] == PROPOSAL["intent"]
    assert envelope["payload"]["proposal_sha256"] == g.proposal_sha256
    stored = admin_proposal.load(str(env / "proposals"), "KD-123")
    assert stored.data["intent"] == "Другое намерение"  # the card's current proposal is the new one


def test_admin_run_refuses_without_the_kept_signed_plan(env):
    # a grant approved before the cabinet kept its copy (and not backfilled) needs a new approval
    store_proposal(env)
    grant(env, keep=False)
    code, _, err, calls, sent, log = run(env, ["--task", "KD-123", "--op", "1"], envelopes=True)
    assert code == 2 and calls == [] and sent == [] and "a new approval is needed" in err
    assert [r["event"] for r in log] == ["admin_refused"] and "not kept" in log[0]["reason"]
    # without envelopes the grant's own copy of the step is enough, as before
    code, _, err, calls, _, _ = run(env, ["--task", "KD-123", "--op", "1"], envelopes=False)
    assert code == 0, err


def _copy(env, g):
    return Path(admin_signed.plan_path(str(env / "grants"), "KD-123", g.proposal_sha256))


def test_admin_run_refuses_a_kept_plan_with_another_hash(env):
    g = grant(env)
    path = _copy(env, g)
    body = json.loads(path.read_text("utf-8"))
    body["proposal"]["intent"] = "Другое намерение"
    path.write_text(json.dumps(body), encoding="utf-8")
    code, _, err, calls, _, log = run(env, ["--task", "KD-123", "--op", "1"], envelopes=True)
    assert code == 2 and calls == [] and "does not match the grant's hash" in err
    assert [r["event"] for r in log] == ["admin_refused"]


def test_admin_run_refuses_a_kept_plan_behind_a_link(env, tmp_path):
    g = grant(env)
    path = _copy(env, g)
    target = tmp_path / "elsewhere.json"
    target.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(target)
    code, _, err, calls, _, log = run(env, ["--task", "KD-123", "--op", "1"], envelopes=True)
    assert code == 2 and calls == [] and "not a regular file" in err
    assert [r["event"] for r in log] == ["admin_refused"]


def test_admin_run_refuses_a_kept_plan_of_another_owner(env):
    grant(env)
    # the store's writer is another user: the grant (and its copy) are not the cabinet's
    code, _, err, calls, _, log = run(env, ["--task", "KD-123", "--op", "1"], envelopes=True,
                                      approve_uid=os.getuid() + 1)
    assert code == 2 and calls == [] and "wrong owner" in err
    assert [r["event"] for r in log] == ["admin_refused"]


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root reads any file")
def test_admin_run_never_reads_the_proposals(env):
    # the exec user cannot read admin-proposals (exec_setup.sh check); the step runs from the grant store
    store_proposal(env)
    grant(env)
    proposals = env / "proposals"
    proposals.chmod(0)
    try:
        runner = Runner()
        code, _, err, calls, _, _ = run(env, ["--task", "KD-123", "--op", "1"], runner=runner, envelopes=True)
    finally:
        proposals.chmod(0o750)
    assert code == 0, err
    assert json.loads(runner.feeds[0][0])["proposal"]["intent"] == PROPOSAL["intent"]


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root reads any file")
def test_admin_run_names_a_missing_read_right(env):
    g = grant(env)
    _copy(env, g).chmod(0)
    code, _, err, calls, _, log = run(env, ["--task", "KD-123", "--op", "1"], envelopes=True)
    assert code == 2 and calls == [] and f"is not readable by {admin_grant.reader()}" in err
    assert "cannot be read" not in err and [r["event"] for r in log] == ["admin_refused"]


# --- app steps — a project environment's own tools, on the factory's home host too -----------------------

APP_CARD = CARD.replace("server:node-c", "server:example-stage")
APP_DIR = "/var/www/testapi.example.test"
APP = {"server": "example-stage", "user": "www-data", "dir": APP_DIR, "python": "./venv/bin/python",
       "tools": ["tools/partner_admin.py"], "flags": {"file": "config.ini", "sections": ["partner"]}}
APP_PLAN = dict(PROPOSAL, operations=[
    {"server": "example-stage", "verb": "app.tool", "app": "app-staging", "tool": "tools/partner_admin.py",
     "args": ["set-type", "--partner-id", "6", "--type", "sales", "--force"], **EXPLAIN},
    {"server": "example-stage", "verb": "app.flag", "app": "app-staging", "section": "partner",
     "key": "clients_enabled", "value": "1", **EXPLAIN},
    {"server": "example-stage", "verb": "service.reload", "unit": "testapi-partner.example.test.service", **EXPLAIN},
])
APP_COMMANDS = [
    f"runuser -u www-data -- env -C {APP_DIR} ./venv/bin/python tools/partner_admin.py set-type --partner-id 6"
    " --type sales --force",
    f"runuser -u www-data -- env -C {APP_DIR} python3 -I /usr/local/libexec/agent-app-flag config.ini partner"
    " clients_enabled 1",
    "systemctl reload testapi-partner.example.test.service",
]


def app_catalog(tmp_path, monkeypatch, **app_over):
    data = json.loads(Path(ops.CATALOG_PATH).read_text(encoding="utf-8"))
    data["verbs"] = sorted(set(data["verbs"]) | {"service.reload", "app.tool", "app.flag"})
    data["apps"] = {"app-staging": {**APP, **app_over}}
    path = tmp_path / "operations-apps.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(ops, "CATALOG_PATH", path)
    return path


def test_admin_run_runs_an_app_tool_step_on_the_home_host(tmp_path, enabled_all, monkeypatch):
    env = make_env(tmp_path, enabled_all, card=APP_CARD)
    app_catalog(tmp_path, monkeypatch)
    grant(env, proposal=APP_PLAN)
    server = inventory.get("example-stage", enabled_path=env / "enabled")
    for number, command in enumerate(APP_COMMANDS, 1):
        code, _, err, calls, _, log = run(env, ["--task", "KD-123", "--op", str(number)])
        assert code == 0, err
        assert calls[0][-1] == execute.remote_command(server, command)
        start = [r for r in log if r["event"] == "start"][-1]
        assert start["command"] == command and start["op_index"] == number
    assert start["operation"]["verb"] == "service.reload"


def test_admin_run_sends_an_app_step_with_its_argv_in_the_envelope(tmp_path, enabled_all, monkeypatch):
    env = make_env(tmp_path, enabled_all, card=APP_CARD)
    app_catalog(tmp_path, monkeypatch)
    store_proposal(env, proposal=APP_PLAN)
    grant(env, proposal=APP_PLAN)
    runner = Runner()
    code, _, err, _, _, _ = run(env, ["--task", "KD-123", "--op", "1"], runner=runner, envelopes=True)
    assert code == 0, err
    envelope = json.loads(runner.feeds[0][0])
    assert envelope["command"] == APP_COMMANDS[0] and envelope["server"] == "example-stage"
    step = envelope["proposal"]["operations"][0]
    # the target's gate has no catalog: the signed plan itself carries the words it will run
    assert " ".join(step["argv"]) == APP_COMMANDS[0] and step["verb"] == "app.tool"


def test_admin_run_refuses_an_app_step_whose_catalog_entry_changed(tmp_path, enabled_all, monkeypatch):
    env = make_env(tmp_path, enabled_all, card=APP_CARD)
    app_catalog(tmp_path, monkeypatch)
    grant(env, proposal=APP_PLAN)
    app_catalog(tmp_path, monkeypatch, dir="/var/www/other.example.test")
    code, _, err, calls, _, log = run(env, ["--task", "KD-123", "--op", "1"])
    assert code == 2 and calls == [] and "catalog entry changed" in err
    assert log[0]["event"] == "admin_refused"
    app_catalog(tmp_path, monkeypatch, tools=["tools/other.py"])
    code, _, err, calls, _, _ = run(env, ["--task", "KD-123", "--op", "1"])
    assert code == 2 and calls == []


# --- a grant by the test-zone rule runs only steps of a test environment ------------------------------------
ZONE_APP = dict(APP, test=True, reload=["testapi-partner.example.test.service"])


def rule_grant(env, proposal=APP_PLAN, now=NOW):
    card = env.board.card()
    p = ops.validate_proposal(proposal, ops.load_catalog(), ops.load_inventory(), ops.load_enabled(env / "enabled"),
                              card_servers=card.servers)
    admin_signed.write_plan(str(env / "grants"), admin_proposal.Stored("KD-123", "sysadmin", now, p.as_dict()),
                            p.as_dict(), p.sha256)
    return admin_grant.Store(str(env / "grants"), None).approve(
        card, p, now=now, ttl=timedelta(hours=24), via="rule", expect_card_sha256=card.scope_sha256,
        expect_proposal_sha256=p.sha256, approved_by=admin_grant.TEST_ZONE)


def test_a_rule_grant_runs_the_steps_of_its_test_environment(tmp_path, enabled_all, monkeypatch):
    env = make_env(tmp_path, enabled_all, card=APP_CARD)
    app_catalog(tmp_path, monkeypatch, **ZONE_APP)
    rule_grant(env)
    for number, command in enumerate(APP_COMMANDS, 1):
        runner = Runner()
        code, _, err, _, _, _ = run(env, ["--task", "KD-123", "--op", str(number)], runner=runner, envelopes=True)
        assert code == 0, err
        envelope = json.loads(runner.feeds[0][0])
        # no signature to send: the target's own list is what lets the step through
        assert envelope["assertion"] is None and envelope["command"] == command
        assert envelope["decision"]["user"] == "rule:test-env"


def test_a_rule_grant_stops_when_the_catalog_drops_the_mark(tmp_path, enabled_all, monkeypatch):
    env = make_env(tmp_path, enabled_all, card=APP_CARD)
    app_catalog(tmp_path, monkeypatch, **ZONE_APP)
    rule_grant(env)
    app_catalog(tmp_path, monkeypatch)  # the same entry, no longer a test environment
    for number in (1, 2, 3):
        code, _, err, calls, _, log = run(env, ["--task", "KD-123", "--op", str(number)])
        assert code == 2 and calls == []
        assert "is no longer a test environment's step; propose the plan again for the owner's signature" in err
    assert log[-1]["event"] == "admin_refused"


def test_a_rule_grant_never_runs_a_step_outside_the_zone(tmp_path, enabled_all, monkeypatch):
    # a grant crafted past the cabinet: the rule's name on a plan with a restart
    env = make_env(tmp_path, enabled_all, card=APP_CARD)
    app_catalog(tmp_path, monkeypatch, **ZONE_APP)
    mixed = dict(APP_PLAN, operations=APP_PLAN["operations"] + [
        {"server": "example-stage", "verb": "service.restart", "unit": "testapi-partner.example.test.service",
         **EXPLAIN},
        {"server": "example-stage", "verb": "package.refresh", **EXPLAIN}])
    rule_grant(env, proposal=mixed)
    for number in (4, 5):
        code, _, err, calls, _, _ = run(env, ["--task", "KD-123", "--op", str(number)])
        assert code == 2 and calls == [] and "no longer a test environment's step" in err
    code, _, err, calls, _, _ = run(env, ["--task", "KD-123", "--op", "1"])  # its zone steps still run
    assert code == 0 and len(calls) == 1


def test_an_owners_grant_is_not_bound_to_the_zone(tmp_path, enabled_all, monkeypatch):
    env = make_env(tmp_path, enabled_all, card=APP_CARD)
    app_catalog(tmp_path, monkeypatch)  # no test mark at all
    grant(env, proposal=APP_PLAN)
    code, _, err, calls, _, _ = run(env, ["--task", "KD-123", "--op", "1"])
    assert code == 0 and len(calls) == 1, err
