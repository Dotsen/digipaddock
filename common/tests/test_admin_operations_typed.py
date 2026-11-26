"""the typed steps cron.line, file.drop and gate.script on the stage side."""

import copy
import sys
from pathlib import Path

import pytest

from kd import admin_operations as ops
from kd import exec_policy

sys.path.insert(0, str(Path(__file__).resolve().parent))  # admin_samples
from admin_samples import CATALOG, ENABLED, INVENTORY, explain, proposal  # noqa: E402

HELPER = ["python3", "-I", "/usr/local/libexec/agent-typed-op"]


def catalog():
    data = copy.deepcopy(CATALOG)
    data["verbs"] += ["cron.line", "file.drop", "gate.script"]
    return ops.Catalog.from_dict(data)


def step(op, c=None):
    return ops.validate_operation(explain(op), c or catalog(), INVENTORY, ENABLED)


def argv(op):
    c = catalog()
    return ops.remote_argv(step(op, c), c)


def test_cron_line_forms_the_helpers_words():
    line = "*/5 * * * * /usr/local/bin/backup.sh"
    assert argv(
        {
            "server": "node-c",
            "verb": "cron.line",
            "mark": "KD-7",
            "action": "set",
            "line": line,
        }
    ) == HELPER + ["cron", "KD-7", "set", line]
    assert argv(
        {"server": "node-c", "verb": "cron.line", "mark": "KD-7", "action": "remove"}
    ) == HELPER + ["cron", "KD-7", "remove"]
    op = step(
        {
            "server": "node-c",
            "verb": "cron.line",
            "mark": "KD-7",
            "action": "set",
            "line": line,
        }
    )
    assert (
        op.as_dict()["line"] == line
        and "KD-7" in ops.mechanics(op)
        and line in ops.mechanics(op)
    )


def test_file_drop_forms_the_helpers_words():
    text = "vm.swappiness = 10\n"
    assert argv(
        {
            "server": "node-c",
            "verb": "file.drop",
            "dir": "/etc/sysctl.d",
            "name": "60-kd.conf",
            "mark": "KD-7",
            "action": "set",
            "text": text,
        }
    ) == HELPER + ["file", "/etc/sysctl.d", "60-kd.conf", "KD-7", "set", text]
    assert argv(
        {
            "server": "node-c",
            "verb": "file.drop",
            "dir": "/etc/logrotate.d",
            "name": "app",
            "mark": "KD-7",
            "action": "remove",
        }
    ) == HELPER + ["file", "/etc/logrotate.d", "app", "KD-7", "remove"]
    op = step(
        {
            "server": "node-c",
            "verb": "file.drop",
            "dir": "/etc/sysctl.d",
            "name": "60-kd.conf",
            "mark": "KD-7",
            "action": "set",
            "text": text,
        }
    )
    assert "/etc/sysctl.d/60-kd.conf" in ops.mechanics(op)


def test_gate_script_forms_the_helpers_words():
    assert argv(
        {"server": "node-c", "verb": "gate.script", "value": "off"}
    ) == HELPER + ["script", "off"]
    assert "выключить" in ops.mechanics(
        step({"server": "node-c", "verb": "gate.script", "value": "off"})
    )


@pytest.mark.parametrize(
    "op,why",
    [
        (
            {"server": "node-c", "verb": "cron.line", "mark": "KD-7", "action": "set"},
            "line",
        ),
        (
            {
                "server": "node-c",
                "verb": "cron.line",
                "mark": "KD-7",
                "action": "remove",
                "line": "x",
            },
            "line",
        ),
        (
            {
                "server": "node-c",
                "verb": "cron.line",
                "mark": "7",
                "action": "set",
                "line": "* * * * * a",
            },
            "mark",
        ),
        (
            {"server": "node-c", "verb": "cron.line", "mark": "KD-7", "action": "drop"},
            "action",
        ),
        (
            {
                "server": "node-c",
                "verb": "cron.line",
                "mark": "KD-7",
                "action": "set",
                "line": "0 4 * * * systemctl reboot",
            },
            "критичн",
        ),
        (
            {
                "server": "node-c",
                "verb": "cron.line",
                "mark": "KD-7",
                "action": "set",
                "line": "* * * * * echo x >> /etc/sudoers",
            },
            "sudoers",
        ),
        (
            {
                "server": "node-c",
                "verb": "cron.line",
                "mark": "KD-7",
                "action": "set",
                "line": "*/5 * * * * sh%iptables -F%reboot",
            },
            "%",
        ),
        (
            {
                "server": "node-c",
                "verb": "file.drop",
                "dir": "/etc/cron.d",
                "name": "x",
                "mark": "KD-7",
                "action": "set",
                "text": "x",
            },
            "каталог",
        ),
        (
            {
                "server": "node-c",
                "verb": "file.drop",
                "dir": "/etc/logrotate.d",
                "name": "x",
                "mark": "KD-7",
                "action": "set",
                "text": "/var/log/x {\n postrotate\n id\n endscript\n}\n",
            },
            "postrotate",
        ),
        (
            {
                "server": "node-c",
                "verb": "file.drop",
                "dir": "/etc/logrotate.d",
                "name": "x",
                "mark": "KD-7",
                "action": "set",
                "text": "/etc/shadow {\n create 0666 root root\n}\n",
            },
            "/var/log/",
        ),
        (
            {
                "server": "node-c",
                "verb": "file.drop",
                "dir": "/etc/sysctl.d",
                "name": "60-kd.conf",
                "mark": "KD-7",
                "action": "set",
                "text": "net.ipv4.ip_forward = 1\n",
            },
            "kd-admin-propose-action",
        ),
        (
            {
                "server": "node-c",
                "verb": "file.drop",
                "dir": "/etc/sysctl.d",
                "name": "x.conf",
                "mark": "KD-7",
                "action": "remove",
                "text": "a = 1",
            },
            "text",
        ),
        ({"server": "node-c", "verb": "gate.script", "value": "maybe"}, "value"),
        ({"server": "example-prod", "verb": "gate.script", "value": "off"}, "catalog"),
    ],
)
def test_typed_content_beyond_its_shape_is_refused(op, why):
    with pytest.raises(ops.ProposalError, match=why):
        step(op)


@pytest.mark.parametrize(
    "op",
    [
        {
            "server": "node-c",
            "verb": "cron.line",
            "mark": "KD-123",
            "action": "set",
            "line": "*/5 * * * * /usr/local/bin/backup.sh",
        },
        {"server": "node-c", "verb": "cron.line", "mark": "KD-123", "action": "remove"},
        {
            "server": "node-c",
            "verb": "file.drop",
            "dir": "/etc/sysctl.d",
            "name": "60-kd.conf",
            "mark": "KD-123",
            "action": "set",
            "text": "vm.swappiness = 10\n",
        },
    ],
)
def test_a_plan_has_no_cron_line_or_file_drop_on_the_factorys_own_host(op):
    # like a script, a root cron line or a sysctl/logrotate file on the home host reaches the cage itself
    data = proposal(operations=[explain(op)])
    with pytest.raises(ops.ProposalError, match="node-c is the factory's own host"):
        ops.validate_proposal(
            data, catalog(), INVENTORY, ENABLED, card_servers=("node-c",), home="node-c"
        )
    assert ops.validate_proposal(
        data, catalog(), INVENTORY, ENABLED, card_servers=("node-c",), home="other"
    )
    # gate.script stays: it only turns the host's script steps off or on
    switch = proposal(
        operations=[explain({"server": "node-c", "verb": "gate.script", "value": "off"})]
    )
    assert ops.validate_proposal(
        switch, catalog(), INVENTORY, ENABLED, card_servers=("node-c",), home="node-c"
    )


def test_the_stage_denylist_reads_a_cron_lines_command():
    line = "*/5 * * * * rsync -a /tmp/x/ /var/www/app/"
    data = proposal(
        operations=[
            explain(
                {
                    "server": "node-c",
                    "verb": "cron.line",
                    "mark": "KD-123",
                    "action": "set",
                    "line": line,
                }
            )
        ]
    )
    seen = []

    def deny(text, server):
        seen.append(text)
        return ("www-write", "no") if text.startswith("rsync") else None

    with pytest.raises(ops.ProposalError, match="www-write"):
        ops.validate_proposal(
            data,
            catalog(),
            INVENTORY,
            ENABLED,
            card_servers=("node-c",),
            deny=deny,
            critical=lambda c: None,
        )
    assert seen == ["rsync -a /tmp/x/ /var/www/app/"]


def test_the_formed_commands_pass_rule_1():
    c = catalog()
    for op in (
        {
            "server": "node-c",
            "verb": "cron.line",
            "mark": "KD-7",
            "action": "set",
            "line": "0 3 * * * /usr/sbin/logrotate /etc/logrotate.conf",
        },
        {
            "server": "node-c",
            "verb": "file.drop",
            "dir": "/etc/logrotate.d",
            "name": "app",
            "mark": "KD-7",
            "action": "set",
            "text": "/var/log/app.log {\n daily\n}\n",
        },
        {"server": "node-c", "verb": "gate.script", "value": "off"},
    ):
        command = ops.remote_command(step(op, c), c)
        assert (
            exec_policy.protect(command) is None
            and exec_policy.plan_step(1, command, ("*",)) is None
        ), command
