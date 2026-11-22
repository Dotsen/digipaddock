import json
import os

import pytest

from kd import cap_grants

SELF = {
    "schema_version": 1,
    "agent": "sysadmin",
    "key": "self",
    "uid": 1007,
    "run_as": "digi-sysadmin",
    "commands": {
        "backlog": {
            "program": "/usr/local/bin/backlog",
            "args": [],
            "subcommands": ["search", "task create", "task edit"],
            "env": {"BACKLOG_CWD": "/srv/digipaddock-agents/board"},
        },
        "kd-monitor": {
            "program": "/opt/digipaddock/agents/sysadmin/bin/kd-monitor",
            "args": [],
            "subcommands": ["list", "tick --help"],
            "env": {},
        },
        "ssh-ro": {
            "program": "/opt/digipaddock/agents/sysadmin/bin/ssh_ro.py",
            "args": [],
            "subcommands": [],
            "env": {},
        },
    },
    "programs": [],
}


def _grant(data=SELF, key="self"):
    return cap_grants.parse(json.loads(json.dumps(data)), "sysadmin", key)


def test_free_command_takes_any_argv():
    assert cap_grants.command_for(
        _grant(), "ssh-ro", ["node-c", "echo $(uptime)"]
    ).program.endswith("ssh_ro.py")


@pytest.mark.parametrize(
    "name, argv",
    [
        ("backlog", ["task", "edit", "KD-1", "-s", "Done"]),
        ("backlog", ["search", "x"]),
        ("kd-monitor", ["tick", "--help"]),
    ],
)
def test_subcommand_prefix_is_allowed(name, argv):
    assert cap_grants.command_for(_grant(), name, argv).name == name


@pytest.mark.parametrize(
    "name, argv, why",
    [
        ("backlog", ["task", "archive", "KD-1"], "subcommand"),
        ("backlog", ["tasks", "list"], "subcommand"),
        ("backlog", ["task"], "subcommand"),
        ("backlog", [], "subcommand"),
        ("kd-monitor", ["tick"], "subcommand"),
        ("ssh-exec", ["node-c", "true"], "not a command"),
    ],
)
def test_refusals(name, argv, why):
    with pytest.raises(cap_grants.Refused, match=why):
        cap_grants.command_for(_grant(), name, argv)


def test_exec_channel_programs():
    data = {
        **SELF,
        "key": "exec",
        "run_as": "digi-exec",
        "commands": {},
        "programs": ["/opt/digipaddock/agents/sysadmin/bin/ssh_exec.py"],
    }
    grant = _grant(data, "exec")
    assert cap_grants.program_for(
        grant, "/opt/digipaddock/agents/sysadmin/bin/ssh_exec.py"
    )
    with pytest.raises(cap_grants.Refused, match="not a program"):
        cap_grants.program_for(grant, "/bin/sh")


@pytest.mark.parametrize(
    "change, why",
    [
        ({"agent": "dev"}, "agent"),
        ({"key": "exec"}, "key"),
        ({"uid": "1007"}, "uid"),
        ({"uid": 0}, "uid"),
        ({"run_as": "root"}, "run_as"),
        ({"schema_version": 2}, "schema"),
        ({"extra": 1}, "keys"),
        (
            {
                "commands": {
                    "x": {"program": "rel", "args": [], "subcommands": [], "env": {}}
                }
            },
            "absolute",
        ),
        (
            {
                "commands": {
                    "x": {
                        "program": "/a/../b",
                        "args": [],
                        "subcommands": [],
                        "env": {},
                    }
                }
            },
            "absolute",
        ),
        (
            {
                "commands": {
                    "x": {
                        "program": "/x",
                        "args": [],
                        "subcommands": [],
                        "env": {"PATH": "/tmp"},
                    }
                }
            },
            "env",
        ),
        (
            {
                "commands": {
                    "x": {"program": "/x", "args": [], "subcommands": [" a"], "env": {}}
                }
            },
            "subcommands",
        ),
        (
            {
                "commands": {
                    "X/y": {"program": "/x", "args": [], "subcommands": [], "env": {}}
                }
            },
            "command names",
        ),
        ({"commands": {}}, "self channel"),
        ({"programs": ["/x"]}, "self channel"),
    ],
)
def test_parse_refuses_bad_files(change, why):
    with pytest.raises(cap_grants.GrantError, match=why):
        _grant({**SELF, **change})


def test_load_missing_file_means_switched_off(tmp_path):
    with pytest.raises(cap_grants.GrantError, match="switched off"):
        cap_grants.load("sysadmin", "self", str(tmp_path))


def test_load_refuses_non_json(tmp_path):
    (tmp_path / "sysadmin").mkdir()
    (tmp_path / "sysadmin" / "self.json").write_text("{")
    with pytest.raises(cap_grants.GrantError, match="JSON"):
        cap_grants.load("sysadmin", "self", str(tmp_path))


def test_render_round_trips(tmp_path):
    channel = {k: SELF[k] for k in ("key", "run_as", "commands", "programs")}
    data = cap_grants.render("sysadmin", 1007, channel)
    (tmp_path / "sysadmin").mkdir()
    (tmp_path / "sysadmin" / "self.json").write_bytes(data)
    assert cap_grants.load("sysadmin", "self", str(tmp_path)) == _grant()


@pytest.mark.skipif(os.name != "posix", reason="file modes: posix")
def test_load_refuses_a_grants_file_others_can_write(tmp_path):
    """Global review 15.10.2026: the loader checks the installer's file itself, not only its directory."""
    channel = {k: SELF[k] for k in ("key", "run_as", "commands", "programs")}
    (tmp_path / "sysadmin").mkdir()
    path = tmp_path / "sysadmin" / "self.json"
    path.write_bytes(cap_grants.render("sysadmin", 1007, channel))
    path.chmod(0o664)
    with pytest.raises(cap_grants.GrantError, match="writable"):
        cap_grants.load("sysadmin", "self", str(tmp_path))
    path.chmod(0o644)
    assert cap_grants.load("sysadmin", "self", str(tmp_path)) == _grant()


def test_render_refuses_what_load_would_refuse():
    with pytest.raises(cap_grants.GrantError):
        cap_grants.render(
            "sysadmin",
            1007,
            {
                "key": "self",
                "run_as": "root",
                "commands": SELF["commands"],
                "programs": [],
            },
        )
