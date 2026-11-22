import os
from pathlib import Path

import pytest

from sysadmin import inventory

SPEC_ALIASES = {
    "example-prod", "example-stage", "node-c", "node-d", "node-a",
    "node-e", "node-f", "node-g", "node-b",
    # 05.10.2026, on the owner's word — from the application's server registry
    "node-h", "node-i", "node-j", "node-k", "node-l",
}


def test_inventory_matches_spec():
    servers = inventory.load()
    assert set(servers) == SPEC_ALIASES
    assert servers["example-prod"].host == "203.0.113.20"
    assert servers["example-stage"].host == "203.0.113.10"
    assert servers["node-g"].host == "203.0.113.36"
    assert servers["node-k"].host == "203.0.113.40"
    assert servers["node-l"].host == "203.0.113.41"


def test_only_jed1_goes_through_ubuntu_and_sudo():
    servers = inventory.load()
    assert servers["node-a"] == inventory.Server("node-a", "203.0.113.30", "ubuntu", True)
    others = [s for alias, s in servers.items() if alias != "node-a"]
    assert {s.exec_user for s in others} == {"root"}
    assert not any(s.exec_sudo for s in others)


@pytest.mark.parametrize("alias", ["node-mrs1", "../etc", "", "EXAMPLE-PROD", "-oProxyCommand=x"])
def test_unknown_or_bad_alias_is_refused(alias, enabled_all):
    with pytest.raises(inventory.InventoryError):
        inventory.get(alias, enabled_path=enabled_all)


def test_server_without_gate_is_refused(tmp_path):
    only_stage = tmp_path / "servers.enabled"
    only_stage.write_text("example-stage\n", encoding="utf-8")
    assert inventory.get("example-stage", enabled_path=only_stage).host == "203.0.113.10"
    with pytest.raises(inventory.InventoryError, match="не подключён"):
        inventory.get("example-prod", enabled_path=only_stage)


def test_missing_enabled_file_means_no_servers(tmp_path):
    assert inventory.enabled(tmp_path / "missing") == set()
    with pytest.raises(inventory.InventoryError, match="не подключён"):
        inventory.get("example-stage", enabled_path=tmp_path / "missing")


def test_zones_are_the_known_tz_of_the_servers(tmp_path):
    """10.9: a server's "tz" (an IANA name) is the zone of its clock and its applications' naive times."""
    import json

    path = tmp_path / "inventory.json"
    path.write_text(json.dumps({"servers": {
        "a": {"host": "203.0.113.1", "tz": "Europe/Berlin"},
        "b": {"host": "203.0.113.2", "tz": "../../etc/passwd"},
        "c": {"host": "203.0.113.3"},
        "d": {"host": "203.0.113.4", "tz": "Mars/Base"},
        "e": {"host": "203.0.113.5", "tz": 3},
    }}), encoding="utf-8")
    assert {alias: str(z) for alias, z in inventory.zones(path).items()} == {"a": "Europe/Berlin"}
    assert inventory.zones(tmp_path / "absent.json") == {}


def test_join_command_keeps_single_string():
    assert inventory.join_command(["journalctl -u nginx -n 5"]) == "journalctl -u nginx -n 5"


def test_join_command_quotes_argv():
    assert inventory.join_command(["journalctl", "--since", "1 hour ago"]) == "journalctl --since '1 hour ago'"


def test_ro_argv_targets_gate_user_with_pinned_host_keys(enabled_all, monkeypatch):
    # M02: the key lives in the home of the user that runs ssh-ro (digi-sysadmin; agent on the rollback).
    monkeypatch.setattr(inventory, "own_home", lambda: "/home/digi-sysadmin")
    argv = inventory.ro_argv(["example-prod", "uptime"], enabled_path=enabled_all)
    assert argv[:5] == ["/usr/bin/ssh", "-F", "/dev/null", "-i", "/home/digi-sysadmin/.ssh/agent_ro_ed25519"]
    assert "BatchMode=yes" in argv and "IdentitiesOnly=yes" in argv
    assert "StrictHostKeyChecking=yes" in argv  # deploy_target.sh puts the host keys there from the owner's known_hosts
    assert "UserKnownHostsFile=/home/digi-sysadmin/.ssh/known_hosts" in argv
    assert "GlobalKnownHostsFile=/dev/null" in argv
    assert "UpdateHostKeys=no" in argv
    assert argv[-3:] == ["agent-ro@203.0.113.20", "--", "uptime"]


def test_ro_argv_needs_command(enabled_all):
    with pytest.raises(inventory.InventoryError):
        inventory.ro_argv(["example-prod"], enabled_path=enabled_all)


def test_ro_argv_refuses_option_like_alias(enabled_all):
    with pytest.raises(inventory.InventoryError):
        inventory.ro_argv(["-oProxyCommand=x", "uptime"], enabled_path=enabled_all)


def test_ro_argv_dash_command_stays_one_element_after_separator(enabled_all):
    argv = inventory.ro_argv(["example-prod", "-oProxyCommand=x"], enabled_path=enabled_all)
    assert argv[-2:] == ["--", "-oProxyCommand=x"]


@pytest.mark.parametrize("args", [[""], ["  "], ["", " "]])
def test_empty_command_is_refused(args, enabled_all):
    with pytest.raises(inventory.InventoryError):
        inventory.ro_argv(["example-prod"] + args, enabled_path=enabled_all)


def test_alias_with_trailing_newline_is_refused(enabled_all):
    with pytest.raises(inventory.InventoryError):
        inventory.get("example-prod\n", enabled_path=enabled_all)


def test_corrupt_inventory_is_inventory_error(tmp_path):
    bad = tmp_path / "inventory.json"
    bad.write_text("{не json", encoding="utf-8")
    with pytest.raises(inventory.InventoryError):
        inventory.load(bad)
    with pytest.raises(inventory.InventoryError):
        inventory.load(tmp_path / "missing.json")


def test_unreadable_enabled_file_is_inventory_error(tmp_path):
    # a directory in place of the file: reading gives OSError, not "no file"
    with pytest.raises(inventory.InventoryError):
        inventory.enabled(tmp_path)


# --- several servers in one call: ssh-ro a,b,c '…' and ssh-ro all '…' (no for/$s loops — those go to a card)


def test_ro_targets_single_server(enabled_all):
    targets = inventory.ro_targets(["node-c", "uptime"], enabled_path=enabled_all)
    assert [a for a, _ in targets] == ["node-c"]
    assert targets[0][1] == inventory.ro_argv(["node-c", "uptime"], enabled_path=enabled_all)


def test_ro_targets_comma_list_keeps_order_and_drops_duplicates(enabled_all):
    targets = inventory.ro_targets(["node-h,node-f,node-h", "df -h"], enabled_path=enabled_all)
    assert [a for a, _ in targets] == ["node-h", "node-f"]


def test_ro_targets_all_is_enabled_servers_in_inventory_order(tmp_path):
    on = tmp_path / "servers.enabled"
    on.write_text("node-d\nexample-prod\nnode-mrs1\n", encoding="utf-8")
    targets = inventory.ro_targets(["all", "uptime"], enabled_path=on)
    assert [a for a, _ in targets] == ["example-prod", "node-d"]


@pytest.mark.parametrize("spec", ["node-c,,node-d", "node-c,../etc", ",", "node-c,node-mrs1"])
def test_ro_targets_bad_list_is_refused_before_anything_runs(spec, enabled_all):
    with pytest.raises(inventory.InventoryError):
        inventory.ro_targets([spec, "uptime"], enabled_path=enabled_all)


def test_ro_targets_all_with_nothing_enabled_is_refused(tmp_path):
    with pytest.raises(inventory.InventoryError):
        inventory.ro_targets(["all", "uptime"], enabled_path=tmp_path / "missing")


def test_ro_targets_needs_command(enabled_all):
    with pytest.raises(inventory.InventoryError):
        inventory.ro_targets(["node-c,node-d"], enabled_path=enabled_all)


# --- the group ssh-exec: the card names every server explicitly ---


def test_exec_aliases_keeps_order():
    assert inventory.exec_aliases("node-h,node-f,example-prod") == ["node-h", "node-f", "example-prod"]


@pytest.mark.parametrize("spec,why", [
    ("all", "all для ssh-exec не принимается"),
    ("node-c,all", "all для ssh-exec не принимается"),
    ("node-c,,node-d", "пустой"),
    ("node-c,", "пустой"),
    (",node-c", "пустой"),
    ("node-c", "от 2 до 15"),
    (",".join(f"n{i}" for i in range(16)), "от 2 до 15"),
    ("node-c,../etc", "неверное имя"),
    ("node-c,node-d,node-c", "дважды"),
    ("example-prod,node-c", "example-prod — только последним в списке"),
])
def test_exec_aliases_refuses_bad_lists(spec, why):
    with pytest.raises(inventory.InventoryError, match=why):
        inventory.exec_aliases(spec)


def test_exec_aliases_accepts_fifteen():
    spec = ",".join(f"n{i}" for i in range(15))
    assert len(inventory.exec_aliases(spec)) == 15


def test_exec_targets_are_inventory_servers(enabled_all):
    servers = inventory.exec_targets("node-a,example-prod", enabled_path=enabled_all)
    assert [(s.alias, s.host, s.exec_user, s.exec_sudo) for s in servers] == [
        ("node-a", "203.0.113.30", "ubuntu", True), ("example-prod", "203.0.113.20", "root", False)]


def test_exec_targets_refuse_unknown_or_not_enabled(tmp_path, enabled_all):
    with pytest.raises(inventory.InventoryError, match="'node-mrs1' нет в инвентаре"):
        inventory.exec_targets("node-c,node-mrs1", enabled_path=enabled_all)
    on = tmp_path / "on"
    on.write_text("node-c\n", encoding="utf-8")
    with pytest.raises(inventory.InventoryError, match="'node-d' ещё не подключён"):
        inventory.exec_targets("node-c,node-d", enabled_path=on)


def test_ssh_ro_wrapper_runs_python_isolated():
    # -I: agent cannot replace the wrapper through PYTHONPATH/PYTHONSTARTUP or the user site
    first = (Path(__file__).resolve().parent.parent / "bin" / "ssh_ro.py").read_text(encoding="utf-8").splitlines()[0]
    assert first == "#!/usr/bin/python3 -I"


def test_own_home_comes_from_the_password_database_not_the_environment(monkeypatch):
    pwd = pytest.importorskip("pwd")
    monkeypatch.setenv("HOME", "/tmp/evil")
    assert inventory.own_home() == pwd.getpwuid(os.getuid()).pw_dir


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "inventory.json"


def test_home_and_prod_come_from_the_inventory():
    assert inventory.home(FIXTURE) == ("example-stage", "203.0.113.10")
    assert inventory.prod(FIXTURE) == "example-prod"


def test_home_missing_or_unknown_is_inventory_error(tmp_path):
    bad = tmp_path / "inventory.json"
    bad.write_text('{"home": "nope", "servers": {"a": {"host": "203.0.113.1"}}}')
    with pytest.raises(inventory.InventoryError):
        inventory.home(bad)
