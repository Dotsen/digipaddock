"""system.reboot — a critical action whose command the stage forms: the helper's snapshot, then the reboot."""

import sys
from pathlib import Path

import pytest

from kd import admin_action, exec_policy
from kd import admin_operations as ops

sys.path.insert(0, str(Path(__file__).resolve().parent))  # admin_samples
from admin_samples import ENABLED, INVENTORY  # noqa: E402

REBOOT = "python3 -I /usr/local/libexec/agent-typed-op reboot KD-123"


@pytest.fixture(autouse=True)
def home(monkeypatch):
    monkeypatch.setattr(ops, "load_home", lambda path=None: "node-e")


def reboot(**over):
    data = {
        "schema_version": 2,
        "task_id": "KD-123",
        "intent": "Перезагрузить после обновления ядра",
        "basis": "needrestart: новое ядро",
        "servers": ["node-c"],
        "verb": "system.reboot",
        "effect": "Сервер недоступен 1–2 минуты",
        "risk": {"level": "high", "why": "Сервисы перезапустятся"},
        "rollback": "Нет: перезагрузка необратима",
    }
    data.update(over)
    return data


def validate(data, servers=("node-c", "node-g", "node-e", "example-prod")):
    return admin_action.validate_action(data, INVENTORY, ENABLED, card_servers=servers)


def test_the_reboot_verb_forms_its_command():
    action = validate(reboot())
    assert action.command == REBOOT and action.servers == ("node-c",)
    assert action.as_dict()["command"] == REBOOT and "verb" not in action.as_dict()
    # the stored proposal is validated again by the cabinet and the executor: the same action, the same hash
    assert validate(reboot()).sha256 == action.sha256
    assert exec_policy.protect(action.command) is None


def test_several_servers_production_last():
    assert (
        validate(reboot(servers=["node-c", "node-g", "example-prod"])).servers[-1]
        == "example-prod"
    )
    with pytest.raises(ops.ProposalError, match="last"):
        validate(reboot(servers=["example-prod", "node-c"]))


def test_the_factorys_own_host_is_never_rebooted_by_the_verb():
    with pytest.raises(ops.ProposalError, match="node-e"):
        validate(reboot(servers=["node-c", "node-e"]))


def test_an_unknown_home_refuses_the_verb(monkeypatch):
    def broken(path=None):
        raise ValueError("no home")

    monkeypatch.setattr(ops, "load_home", broken)
    with pytest.raises(ops.ProposalError, match="home"):
        validate(reboot())


@pytest.mark.parametrize(
    "over",
    [
        {"verb": "system.halt"},
        {"command": REBOOT},  # verb and command together
    ],
)
def test_a_verb_is_reboot_and_comes_without_a_command(over):
    with pytest.raises(ops.ProposalError):
        validate(reboot(**over))


def test_the_helper_command_by_hand_is_the_verbs_own():
    # the helper's reboot only through the verb: a hand-written command naming it (another card, extra words) is refused
    data = reboot()
    del data["verb"]
    data["command"] = REBOOT
    assert validate(data).command == REBOOT  # the very words the verb forms
    data["command"] = "python3 -I /usr/local/libexec/agent-typed-op reboot KD-999"
    with pytest.raises(ops.ProposalError, match="system.reboot"):
        validate(data)
    data["command"] = REBOOT + " now"
    with pytest.raises(ops.ProposalError, match="system.reboot"):
        validate(data)
