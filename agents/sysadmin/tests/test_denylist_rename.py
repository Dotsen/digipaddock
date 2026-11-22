"""Frozen denylist (stage-1 spec §10): a rename (of users, of product and command names) renames, nothing
leaves the cage."""

from sysadmin import denylist, guard  # puts common/ on sys.path

from kd import naming

# The cage after the rename, with the host's names as tokens: @…@ users, {cmd} the host's command prefix.
TREES = (
    "/etc/sudoers", "/etc/sudoers.d", "/etc/claude-code", "/srv/digipaddock-agents", "/opt/digipaddock",
    "/opt/digipaddock-releases", "/home/@exec@", "/etc/@exec@", "/etc/digipaddock", "/home/@base@",
    "/home/@agent:sysadmin@",
    "/home/agent-ro/.*", "/home/agent-ro/",
    "/usr/local/bin/ssh-ro", "/usr/local/bin/ssh-exec", "/usr/local/bin/agent-launch",
    "/usr/local/bin/agent-ro-gate",
    "/var/log/digipaddock",
    "/etc/systemd/system/digipaddock-*", "/usr/local/bin/{cmd}-*",
    "/etc/systemd/system/*.wants/digipaddock-*",
    "/usr/local/bin/agent-sec-snapshot", "/var/lib/digipaddock",
)  # fmt: skip
USERS = {"@base@", "@exec@", "agent-ro", "@agent:sysadmin@"}


def _host(value):
    return naming.render(value).replace("{cmd}", naming.COMMANDS)


def test_cage_trees_are_the_frozen_list():
    assert denylist._CAGE_TREES == tuple(_host(p) for p in TREES)


def test_cage_users_are_the_frozen_set():
    assert denylist._CAGE_USERS == {_host(u) for u in USERS}


def test_cage_units_and_commands_are_the_products_and_the_hosts():
    assert denylist._CAGE_UNIT_PREFIXES == ("digipaddock-", naming.COMMANDS + "-")


def test_the_exec_and_forge_guards_cover_old_key_files_and_new_users():
    exec_rule = guard.ALWAYS[1][0]
    forge_rule = guard.ALWAYS[3][0]
    for text in (
        "agent_exec_ed25519",
        naming.service_user("exec"),
        "/home/" + naming.service_user("exec"),
    ):
        assert exec_rule.search(text), text
    host = naming.load_host()
    for text in (
        host["forge_dev_user"] + ".env",
        naming.service_user("forge"),
        "dev_ed25519",
        host["forge_merge_user"] + ".env",
    ):
        assert forge_rule.search(text), text


def test_guard_homes_come_from_naming():
    assert guard.AGENT_HOME == naming.home(naming.agent_user("sysadmin"))
    assert guard.BASE_HOME == naming.home(naming.base_user())
    assert not hasattr(guard, "LEGACY_HOME")
