"""kd-admin-propose (the agent) and kd-admin-task (root, the owner's fallback)."""
import copy
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from kd import admin_approval, admin_cli, admin_grant, naming
from kd import admin_operations as ops

sys.path.insert(0, str(Path(__file__).resolve().parent))  # admin_samples, admin_board
from admin_board import AdminEnv  # noqa: E402
from admin_samples import CARD, CATALOG, ENABLED, INVENTORY, proposal  # noqa: E402

NOW = datetime(2026, 10, 9, 17, 0, tzinfo=timezone.utc)
SCRIPT_CATALOG = {**CATALOG, "verbs": CATALOG["verbs"] + ["script"]}
ONE_SERVER = CARD.replace("  - 'server:node-e'\n", "").replace("status: To Do", "status: Inbox")
AGENT = naming.agent_user("sysadmin")


@pytest.fixture
def env(tmp_path):
    (tmp_path / "proposals").mkdir()
    (tmp_path / "grants").mkdir()
    (tmp_path / "plan.json").write_text(json.dumps(proposal()), encoding="utf-8")
    # kd-admin-task: root drops to the cabinet user; the cabinet's catalog takes script steps too
    return AdminEnv(tmp_path, ONE_SERVER, user="digi-cabinet", catalog=SCRIPT_CATALOG)


def cabinet_json(env):
    path = env / "cabinet.json"
    path.write_text('{"schema_version": 1, "origin": "https://cab.example"}', encoding="utf-8")
    return str(path)


def run_propose(env, argv, user=AGENT, catalog_data=CATALOG):
    out, err = io.StringIO(), io.StringIO()
    code = admin_cli.propose_main(argv, user=user, client=env.board.as_user(AGENT), root=str(env / "proposals"),
                                  cabinet_json=cabinet_json(env),
                                  catalog=lambda: ops.Catalog.from_dict(copy.deepcopy(catalog_data)),
                                  inventory=lambda: INVENTORY, enabled=lambda: ENABLED, now=lambda: NOW,
                                  out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def service(env):
    paths = admin_approval.Paths(proposals=str(env / "proposals"), grants=str(env / "grants"))
    return admin_approval.Service(paths, catalog=lambda: ops.Catalog.from_dict(copy.deepcopy(CATALOG)),
                                  inventory=lambda: INVENTORY, enabled=lambda: ENABLED, client=env.board)


def run_task(env, argv):
    out, err = io.StringIO(), io.StringIO()
    code = admin_cli.task_main(argv, service=service(env), now=lambda: NOW, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_propose_saves_and_opens_the_owners_decision(env):
    code, out, err = run_propose(env, ["--task", "KD-123", "--file", str(env / "plan.json")])
    assert code == 0, err
    assert "the owner decides in the cabinet: https://cab.example/cards/KD-123" in out
    assert (env / "proposals" / "KD-123.json").exists()
    (decision,) = env.board.as_user("root").call({"op": "decision.list", "state": "open"})["decisions"]
    assert decision["type"] == "admin_plan" and decision["requested_by"] == "agent:sysadmin"
    assert decision["id"] in out
    assert run_propose(env, ["--task", "KD-123", "--file", str(env / "plan.json")])[0] == 0  # asked again: same one
    assert len(env.board.as_user("root").call({"op": "decision.list", "state": "open"})["decisions"]) == 1


def test_propose_from_another_user_is_refused(env):
    code, _, err = run_propose(env, ["--task", "KD-123", "--file", str(env / "plan.json")],
                               user=naming.agent_user("dev"))
    assert code == 2 and "only" in err
    assert not (env / "proposals" / "KD-123.json").exists()


@pytest.mark.parametrize("argv", [
    [], ["--task", "KD-123"], ["--file", "x"], ["--task", "KD-123", "--file", "x", "--agent", "sysadmin"],
    ["--ta", "KD-123", "--file", "x"],
])
def test_propose_usage_is_refused(env, argv):
    assert run_propose(env, argv)[0] == 2


def test_propose_invalid_plan_is_refused(env):
    (env / "plan.json").write_text(json.dumps(proposal(operations=[{"server": "node-c", "verb": "shell"}])),
                                   encoding="utf-8")
    code, _, err = run_propose(env, ["--task", "KD-123", "--file", str(env / "plan.json")])
    assert code == 2 and "shell steps are gone" in err


SCRIPT_PLAN = proposal(operations=[{"server": "node-c", "verb": "script", "script": "set -e\nnginx -t\n",
                                    "what": "Проверить конфиг", "effect": "Ничего", "rollback": "Не нужен",
                                    "risk": {"level": "low", "why": "Только проверка"}}])


def test_propose_refuses_a_script_the_denylist_or_classifier_catches(env, monkeypatch):
    # the sysadmin's denylist and critical classifier read every script step at proposal time
    argv = ["--task", "KD-123", "--json", json.dumps(SCRIPT_PLAN, ensure_ascii=False)]
    monkeypatch.setattr(admin_cli, "sysadmin_critical", lambda: (lambda command: None))
    monkeypatch.setattr(admin_cli, "sysadmin_deny", lambda: (lambda command, server: ("rm-root", "why")))
    code, _, err = run_propose(env, argv, catalog_data=SCRIPT_CATALOG)
    assert code == 2 and "denied by rule rm-root on node-c" in err
    monkeypatch.setattr(admin_cli, "sysadmin_deny", lambda: (lambda command, server: None))
    monkeypatch.setattr(admin_cli, "sysadmin_critical", lambda: (lambda command: "перезагрузка"))
    code, _, err = run_propose(env, argv, catalog_data=SCRIPT_CATALOG)
    assert code == 2 and "шаг 1 критичный (перезагрузка)" in err and "kd-admin-propose-action" in err
    assert not (env / "proposals" / "KD-123.json").exists()
    # rule 1 of the gate's policy, with the gate's own reason, before the denylist and the classifier
    plan = dict(SCRIPT_PLAN, operations=[dict(SCRIPT_PLAN["operations"][0], script="useradd bob\n")])
    code, _, err = run_propose(env, ["--task", "KD-123", "--json", json.dumps(plan, ensure_ascii=False)],
                               catalog_data=SCRIPT_CATALOG)
    assert code == 2 and "команда useradd: шлюз и доступ к серверу меняются только с машины владельца" in err
    assert not (env / "proposals" / "KD-123.json").exists()
    monkeypatch.setattr(admin_cli, "sysadmin_critical", lambda: (lambda command: None))
    code, out, err = run_propose(env, argv, catalog_data=SCRIPT_CATALOG)
    assert code == 0, err
    assert json.loads((env / "proposals" / "KD-123.json").read_text("utf-8"))["proposal"]["operations"][0]["script"] \
        == "set -e\nnginx -t\n"


def test_repository_classifier_recognizes_critical_commands():
    critical = admin_cli.sysadmin_critical()
    assert critical("systemctl stop mysql") is not None
    assert critical("uptime") is None


def test_propose_missing_file_or_card(env):
    assert run_propose(env, ["--task", "KD-123", "--file", str(env / "nope.json")])[0] == 2
    assert run_propose(env, ["--task", "KD-999", "--file", str(env / "plan.json")])[0] == 2


def test_propose_large_file_is_refused(env):
    (env / "plan.json").write_bytes(b" " * (64 * 1024 + 1))
    code, _, err = run_propose(env, ["--task", "KD-123", "--file", str(env / "plan.json")])
    assert code == 2 and "64 KiB" in err


def test_task_show_prints_the_hashes_and_operations(env):
    run_propose(env, ["--task", "KD-123", "--file", str(env / "plan.json")])
    code, out, _ = run_task(env, ["show", "KD-123"])
    assert code == 0
    assert "proposal_sha256" in out and "card_sha256" in out
    assert "node-c: перезапустить nginx.service" in out


def _hashes(out):
    found = dict(line.split(": ", 1) for line in out.splitlines() if line.endswith(tuple("0123456789abcdef"))
                 and ": " in line)
    return found["card_sha256"], found["proposal_sha256"]


def test_task_approve_needs_the_shown_proposal_hash(env):
    run_propose(env, ["--task", "KD-123", "--file", str(env / "plan.json")])
    assert run_task(env, ["approve", "KD-123"])[0] == 2
    code, _, err = run_task(env, ["approve", "KD-123", "--proposal-sha256", "0" * 64])
    assert code == 2 and "changed" in err
    assert not (env / "grants" / "KD-123.json").exists()
    _, out, _ = run_task(env, ["show", "KD-123"])
    card_sha, proposal_sha = _hashes(out)
    code, out, err = run_task(env, ["approve", "KD-123", "--proposal-sha256", proposal_sha])
    assert code == 0, err
    grant = json.loads((env / "grants" / "KD-123.json").read_text("utf-8"))
    assert grant["card_scope_sha256"] == card_sha and grant["state"] == "active"


def test_task_approve_ttl_bounds(env):
    run_propose(env, ["--task", "KD-123", "--file", str(env / "plan.json")])
    _, out, _ = run_task(env, ["show", "KD-123"])
    _, proposal_sha = _hashes(out)
    assert run_task(env, ["approve", "KD-123", "--proposal-sha256", proposal_sha, "--ttl-hours", "169"])[0] == 2
    assert run_task(env, ["approve", "KD-123", "--proposal-sha256", proposal_sha, "--ttl-hours", "24"])[0] == 0
    grant = json.loads((env / "grants" / "KD-123.json").read_text("utf-8"))
    assert grant["expires_at"] == "2026-10-10T17:00:00Z"


def test_task_revoke(env):
    run_propose(env, ["--task", "KD-123", "--file", str(env / "plan.json")])
    _, out, _ = run_task(env, ["show", "KD-123"])
    _, proposal_sha = _hashes(out)
    run_task(env, ["approve", "KD-123", "--proposal-sha256", proposal_sha])
    assert run_task(env, ["revoke", "KD-123"])[0] == 0
    assert json.loads((env / "grants" / "KD-123.json").read_text("utf-8"))["state"] == "revoked"
    journal = (env / "grants" / admin_grant.JOURNAL).read_text("utf-8").splitlines()
    assert [json.loads(line)["via"] for line in journal] == ["cli", "cli"]


def test_task_revoke_without_grant_is_refused(env):
    assert run_task(env, ["revoke", "KD-123"])[0] == 2


@pytest.mark.parametrize("argv", [[], ["show"], ["delete", "KD-123"], ["show", "KD-0"]])
def test_task_usage_is_refused(env, argv):
    assert run_task(env, argv)[0] == 2


ACTION = {"schema_version": 2, "task_id": "KD-123", "intent": "Убрать пир", "basis": "wg show: пир молчит",
          "servers": ["node-c"], "command": "wg set wg0 peer 'abc=' remove", "effect": "Пир отключён",
          "risk": {"level": "high", "why": "Связь с пиром пропадёт"}, "rollback": "Вернуть пир"}


def run_propose_action(env, argv, user=AGENT, deny=lambda command, alias: None):
    out, err = io.StringIO(), io.StringIO()
    code = admin_cli.propose_action_main(argv, user=user, deny=deny, client=env.board.as_user(AGENT),
                                         root=str(env / "proposals"), inventory=lambda: INVENTORY,
                                         enabled=lambda: ENABLED, now=lambda: NOW, out=out, err=err,
                                         cabinet_json=cabinet_json(env))
    return code, out.getvalue(), err.getvalue()


def test_propose_action_opens_the_owners_decision_and_prints_the_run_command(env):
    (env / "action.json").write_text(json.dumps(ACTION), encoding="utf-8")
    code, out, err = run_propose_action(env, ["--task", "KD-123", "--file", str(env / "action.json")])
    assert code == 0, err
    assert "https://cab.example/cards/KD-123" in out and "kd-admin-critical --task KD-123 --action " in out
    (decision,) = env.board.as_user("root").call({"op": "decision.list", "state": "open"})["decisions"]
    assert decision["type"] == "admin_critical" and decision["step_up"] is True


def test_propose_action_denied_or_foreign_user(env):
    (env / "action.json").write_text(json.dumps(ACTION), encoding="utf-8")
    argv = ["--task", "KD-123", "--file", str(env / "action.json")]
    assert run_propose_action(env, argv, deny=lambda command, alias: ("wg", "no"))[0] == 2
    assert run_propose_action(env, argv, user=naming.agent_user("dev"))[0] == 2
    assert list((env / "proposals").glob("*.action-*.json")) == []


def test_repository_denylist_refuses_owner_only_commands():
    deny = admin_cli.sysadmin_deny()
    assert deny("mkfs.ext4 /dev/sda1", "node-c") is not None
    assert deny("systemctl restart nginx.service", "node-c") is None


def test_propose_takes_the_plan_inline(env):
    code, out, err = run_propose(env, ["--task", "KD-123", "--json", json.dumps(proposal(), ensure_ascii=False)])
    assert code == 0, err
    assert (env / "proposals" / "KD-123.json").exists()


@pytest.mark.parametrize("argv", [
    ["--task", "KD-123", "--json", "{}", "--file", "x"], ["--task", "KD-123", "--json", "{" * (64 * 1024 + 1)],
])
def test_propose_inline_needs_one_source_and_the_limit(env, argv):
    code, _, err = run_propose(env, argv)
    assert code == 2 and not (env / "proposals" / "KD-123.json").exists()


def test_propose_action_takes_the_action_inline(env):
    code, out, err = run_propose_action(env, ["--task", "KD-123", "--json", json.dumps(ACTION, ensure_ascii=False)])
    assert code == 0, err
    assert "kd-admin-critical --task KD-123 --action " in out


# --- a plan of test environments comes back approved by the rule --------------------------------------------
def test_propose_of_a_test_zone_plan_says_it_is_approved_by_the_rule(tmp_path):
    from admin_samples import ZONE_CATALOG, zone_proposal

    (tmp_path / "proposals").mkdir()
    (tmp_path / "grants").mkdir()
    (tmp_path / "plan.json").write_text(json.dumps(zone_proposal()), encoding="utf-8")
    env = AdminEnv(tmp_path, ONE_SERVER, user="digi-cabinet", catalog=ZONE_CATALOG)
    code, out, err = run_propose(env, ["--task", "KD-123", "--file", str(env / "plan.json")],
                                 catalog_data=ZONE_CATALOG)
    assert code == 0, err
    grant = admin_grant.Store(str(env / "grants"), None).load("KD-123")
    assert grant.approved_by == admin_grant.TEST_ZONE
    assert (f"approved by the test-environment rule until {admin_grant.fmt(grant.expires_at)} (shop-test): run the "
            "steps now with kd-admin-run --task KD-123 --op <n>") in out
    assert "the owner decides" not in out
    assert env.board.as_user("root").call({"op": "decision.list", "state": "open"})["decisions"] == []
