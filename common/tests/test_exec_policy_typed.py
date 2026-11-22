"""what a cron.line and a file.drop step may carry — one policy for the stage and the target's gate."""

import pytest

from kd import exec_policy as policy


@pytest.mark.parametrize(
    "line,command",
    [
        (
            "*/5 * * * * /usr/local/bin/backup.sh --quiet",
            "/usr/local/bin/backup.sh --quiet",
        ),
        (
            "0 3 * * 1-5 /usr/bin/find /var/log/app -name x -print",
            "/usr/bin/find /var/log/app -name x -print",
        ),
        ("@reboot /opt/app/start.sh", "/opt/app/start.sh"),
        ("@daily  certbot renew", "certbot renew"),
        ("30 2 1 jan,jul sun echo hi", "echo hi"),
    ],
)
def test_a_cron_line_is_a_schedule_and_a_command(line, command):
    assert policy.cron_problem(line) is None
    assert policy.cron_command(line) == command


@pytest.mark.parametrize(
    "line,why",
    [
        ("", "пуст"),
        ("* * * * *", "команд"),
        ("@reboot", "команд"),
        ("@sometimes echo x", "расписан"),
        ("61 * * * * echo x", "расписан"),
        ("* * * * * echo x\n* * * * * id", "одна строка"),
        ("* * * * * echo x\r", "одна строка"),
        ("* * * * * echo \x1b[31m", "невидим"),
        ("* * * * * echo ‮abc", "невидим"),
        ("SHELL=/bin/sh", "расписан"),
        ("* * * * * " + "x" * 1000, "длинн"),
        ("# KD-1 * * * * echo", "расписан"),
    ],
)
def test_a_cron_line_that_is_not_one_scheduled_command_is_refused(line, why):
    problem = policy.cron_problem(line)
    assert problem is not None and why in problem


def test_the_cron_command_is_read_by_rules_1_and_2():
    # a cron line is a command root runs later: the same rules as a script step read its command part
    assert (
        policy.cron_problem("* * * * * cat /tmp/k >> /root/.ssh/authorized_keys")
        is not None
    )
    assert "критичн" in policy.cron_problem("0 4 * * * systemctl reboot")
    assert (
        policy.cron_problem("0 4 * * * /usr/sbin/logrotate /etc/logrotate.conf") is None
    )


@pytest.mark.parametrize(
    "command",
    [
        "python3 -I /usr/local/libexec/agent-typed-op reboot KD-1",
        "set -e\ncd /usr/local/libexec\npython3 ./agent-typed-op reboot KD-2\n",
        "sudo /usr/local/libexec/agent-typed-op reboot KD-3",
    ],
)
def test_the_helpers_reboot_is_critical_in_a_plan_step(command):
    # system.reboot is a critical action: a script step that calls the helper's reboot does not skip that signature
    assert policy.classify(command) == policy.SHUTDOWN
    assert "критичный" in policy.plan_step(1, command)
    assert (
        policy.protect(command) is None
    )  # an action carries it: rule 1 lets it through


def test_a_word_reboot_elsewhere_is_not_the_helpers_reboot():
    command = "python3 -I /usr/local/libexec/agent-typed-op file /etc/logrotate.d app KD-1 set '# reboot note\n'"
    assert policy.classify(command) is None


@pytest.mark.parametrize(
    "directory,name,text",
    [
        (
            "/etc/sysctl.d",
            "60-kd.conf",
            "vm.swappiness = 10\n# comment\n\nnet.core.somaxconn=1024\n",
        ),
        (
            "/etc/sysctl.d",
            "61-kd.conf",
            "-net.ipv4.tcp_tw_reuse = 1\nkernel/pid_max = 4194304\n",
        ),
        (
            "/etc/logrotate.d",
            "app",
            "/var/log/app/*.log {\n  daily\n  rotate 14\n  compress\n  missingok\n}\n",
        ),
        (
            "/etc/logrotate.d",
            "mail",
            "/var/log/mail.log /var/log/include.log {\n  weekly  # mail stays here\n}\n",
        ),
    ],
)
def test_a_plain_sysctl_or_logrotate_file_may_be_dropped(directory, name, text):
    assert policy.drop_problem(directory, name, text) is None


@pytest.mark.parametrize(
    "directory,name,text,why",
    [
        ("/etc/cron.d", "x", "* * * * * root id\n", "каталог"),
        ("/etc/sysctl.d/../cron.d", "x.conf", "a = 1\n", "каталог"),
        ("/etc/sysctl.d", "x", "a = 1\n", ".conf"),
        ("/etc/sysctl.d", "../x.conf", "a = 1\n", "имя"),
        ("/etc/logrotate.d", ".hidden", "x {}\n", "имя"),
        ("/etc/sysctl.d", "x.conf", "", "пуст"),
        (
            "/etc/sysctl.d",
            "x.conf",
            "kernel.core_pattern = |/tmp/x %p\n",
            "kernel.core_pattern",
        ),
        ("/etc/sysctl.d", "x.conf", "kernel.modprobe = /tmp/m\n", "kernel.modprobe"),
        (
            "/etc/sysctl.d",
            "x.conf",
            "kernel.usermodehelper.bset = 1\n",
            "kernel.usermodehelper",
        ),
        ("/etc/sysctl.d", "x.conf", "fs.foo = a|b\n", "|"),
        ("/etc/sysctl.d", "x.conf", "just words\n", "ключ = значение"),
        ("/etc/sysctl.d", "x.conf", "a = 1\r\n", "невидим"),
        (
            "/etc/logrotate.d",
            "app",
            "/var/log/a.log {\n postrotate\n  id\n endscript\n}\n",
            "postrotate",
        ),
        (
            "/etc/logrotate.d",
            "app",
            "/var/log/a.log {\n  firstaction\n  id\n  endscript\n}\n",
            "firstaction",
        ),
        ("/etc/logrotate.d", "app", "include /tmp/evil\n", "include"),
        ("/etc/logrotate.d", "app", "/var/log/a.log {\n  PreRotate\n}\n", "prerotate"),
        ("/etc/logrotate.d", "app", "/var/log/a.log { mail x@example.test }\n", "mail"),
        ("/etc/logrotate.d", "app", "x" * (16 * 1024 + 1), "длинн"),
    ],
)
def test_a_drop_beyond_its_narrow_shape_is_refused(directory, name, text, why):
    problem = policy.drop_problem(directory, name, text)
    assert problem is not None and why in problem, problem
