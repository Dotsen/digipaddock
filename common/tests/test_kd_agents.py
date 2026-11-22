import json

import pytest

from kd import agents


def test_default_user_and_registry(tmp_path):
    reg = tmp_path / "agents.json"
    assert agents.user_of("sysadmin", reg) == "digi"          # no file — everyone under digi
    assert agents.users(reg) == ["digi"]
    reg.write_text(json.dumps({"dev": {"user": "digi-dev", "runtime": "generated"}}), encoding="utf-8")
    assert agents.user_of("dev", reg) == "digi-dev" and agents.user_of("sysadmin", reg) == "digi"
    assert agents.users(reg) == ["digi", "digi-dev"]
    assert not hasattr(agents, "has_telegram")


def test_old_registry_telegram_flag_is_accepted(tmp_path):
    # An entry an install before the change wrote keeps working until the agent's next install drops the flag.
    reg = tmp_path / "agents.json"
    reg.write_text(json.dumps({"dev": {"user": "digi-dev", "telegram": False, "runtime": "generated"}}), "utf-8")
    assert agents.user_of("dev", reg) == "digi-dev" and agents.runtime_of("dev", reg) == "generated"


def test_registry_constants():
    assert agents.REGISTRY == "/etc/digipaddock/agents.json" and agents.DEFAULT_USER == "digi"


@pytest.mark.parametrize("bad", [
    {"dev": {"user": "root"}},
    {"dev": {"user": "Bad User"}},
    {"bad name": {"user": "agent-x"}},
    {"dev": "digi-dev"},
    {"dev": {"user": "digi-dev"}, "other": {"user": "digi-dev"}},   # a stop by user would hit the second agent
    [],
], ids=["root", "пробел", "имя", "не объект", "один пользователь у двух", "список"])
def test_invalid_registry(tmp_path, bad):
    reg = tmp_path / "agents.json"
    reg.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError):
        agents.user_of("dev", reg)


def test_bad_name_and_broken_file(tmp_path):
    reg = tmp_path / "agents.json"
    with pytest.raises(ValueError):
        agents.user_of("Bad", reg)
    reg.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="agents.json"):
        agents.users(reg)


@pytest.mark.parametrize("name", ["", "../x", "a b", "x" * 33, "1abc", "dev\n"])
def test_name_is_fullmatched(tmp_path, name):
    with pytest.raises(ValueError):
        agents.user_of(name, tmp_path / "agents.json")


def test_two_agents_may_share_default_user(tmp_path):
    # several agents live under digi (stage 1) — only the shared digi-<role> is forbidden
    reg = tmp_path / "agents.json"
    reg.write_text(json.dumps({"sysadmin": {"user": "digi"}, "probe": {}}), encoding="utf-8")
    assert agents.users(reg) == ["digi"] and agents.user_of("probe", reg) == "digi"


# --- runtime_of: the explicit switch to the generated launch (BOSS plan A04) ---


@pytest.mark.parametrize("data, name, expected", [
    (None, "sysadmin", "legacy"),
    ({}, "boss", "legacy"),
    ({"dev": {"user": "digi-dev", "telegram": True}}, "dev", "legacy"),
    ({"boss": {"user": "digi-boss", "runtime": "generated"}}, "boss", "generated"),
    ({"boss": {"user": "digi-boss", "runtime": "generated"}}, "sysadmin", "legacy"),
], ids=["no file", "no entry", "no field", "switched", "another agent switched"])
def test_runtime_is_generated_only_when_switched(tmp_path, data, name, expected):
    reg = tmp_path / "agents.json"
    if data is not None:
        reg.write_text(json.dumps(data), encoding="utf-8")
    assert agents.runtime_of(name, reg) == expected
    assert expected in (agents.LEGACY_RUNTIME, agents.GENERATED_RUNTIME)


@pytest.mark.parametrize("value", ["Generated", "legacy", "", " generated", True, 1, None, ["generated"], {}],
                         ids=repr)
def test_unknown_runtime_is_refused(tmp_path, value):
    reg = tmp_path / "agents.json"
    reg.write_text(json.dumps({"boss": {"user": "digi-boss", "runtime": value}}), encoding="utf-8")
    with pytest.raises(ValueError, match="agents.json: boss: неизвестный runtime"):
        agents.runtime_of("boss", reg)


@pytest.mark.parametrize("name", ["", "Boss", "../boss", "boss\n", "x" * 40, None])
def test_runtime_of_refuses_a_bad_name(tmp_path, name):
    with pytest.raises(ValueError):
        agents.runtime_of(name, tmp_path / "agents.json")


@pytest.mark.parametrize("text", ["[]", "{", '{"boss": {"user": "root"}}'])
def test_runtime_of_refuses_a_broken_registry(tmp_path, text):
    reg = tmp_path / "agents.json"
    reg.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        agents.runtime_of("boss", reg)


def test_agent_of_user_finds_the_agent_of_its_own_user(tmp_path):
    reg = tmp_path / "agents.json"
    reg.write_text(json.dumps({"sysadmin": {"user": "digi-sysadmin"}, "probe": {}, "dev": {"user": "digi-dev"}}))
    assert agents.agent_of_user("digi-sysadmin", reg) == "sysadmin"
    assert agents.agent_of_user("digi-dev", reg) == "dev"
    assert agents.agent_of_user("digi", reg) is None  # the base user hosts digipaddock and several agents
    assert agents.agent_of_user("root", reg) is None
    assert agents.agent_of_user("digi-boss", tmp_path / "none.json") is None


def test_names_lists_the_registered_agents(tmp_path):
    path = tmp_path / "agents.json"
    assert agents.names(path) == []
    path.write_text(json.dumps({"sysadmin": {"user": "digi-sysadmin"}, "boss": {"user": "digi-boss"}, "probe": {}}),
                    encoding="utf-8")
    assert agents.names(path) == ["boss", "probe", "sysadmin"]
