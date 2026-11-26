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
    "line",
    [
        "*/5 * * * * sh%iptables -F%reboot",
        "0 4 * * * /usr/local/bin/x.sh % reboot",
        "0 4 * * * date +\\\\%F",  # \\ is one backslash: the % after it is not escaped
        "@daily cat%",
    ],
)
def test_a_cron_line_with_an_unescaped_percent_is_refused(line):
    # cron ends the command at an unescaped % and feeds the rest to it on stdin: sh%iptables -F%reboot runs
    # sh with "iptables -F", "reboot" as its program — text rules 1 and 2 never read as a command
    problem = policy.cron_problem(line)
    assert problem is not None and "%" in problem, problem


def test_an_escaped_percent_is_literal_and_reaches_the_shell_as_one():
    line = "0 4 * * * /usr/local/bin/backup.sh --name db-\\%F"
    assert policy.cron_problem(line) is None
    assert policy.cron_command(line) == "/usr/local/bin/backup.sh --name db-%F"
    # the command cron hands to the shell is what rules 1 and 2 read
    assert "критичн" in policy.cron_problem("0 4 * * * echo \\% ; reboot")


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
            "vm.swappiness = 10\n# comment\n\nfs.file-max=100000\n",
        ),
        (
            "/etc/sysctl.d",
            "61-kd.conf",
            "-vm.overcommit_memory = 1\nkernel/pid_max = 4194304\n",
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
    "text,why",
    [
        ("net.ipv4.ip_forward = 1\n", "kd-admin-propose-action"),
        ("vm.swappiness = 10\nnet.core.somaxconn = 1024\n", "net.core.somaxconn"),
        ("net/ipv4/ip_forward = 1\n", "kd-admin-propose-action"),
        ("-net.ipv4.conf.all.rp_filter = 0\n", "kd-admin-propose-action"),
        ("net = 1\n", "kd-admin-propose-action"),
        # a glob in a key may stand for a refused one (systemd-sysctl expands it at boot)
        ("n*.ipv4.ip_forward = 1\n", "*"),
        ("kernel.core_patter* = |/tmp/x\n", "*"),
        ("*.modprobe = /tmp/m\n", "*"),
    ],
)
def test_a_sysctl_drop_that_changes_the_network_or_hides_its_key_is_refused(text, why):
    # net.* is applied at once (sysctl -p): network access changes only as a critical action the owner signs
    problem = policy.drop_problem("/etc/sysctl.d", "60-kd.conf", text)
    assert problem is not None and why in problem, problem


@pytest.mark.parametrize(
    "key",
    [
        "kernel.yama.ptrace_scope",
        "kernel/yama/ptrace_scope",
        "-kernel.yama.ptrace_scope",
        "fs.protected_symlinks",
        "fs.protected_hardlinks",
        "fs.protected_fifos",
        "fs.protected_regular",
        "fs.suid_dumpable",
        "kernel.kptr_restrict",
        "kernel.dmesg_restrict",
        "kernel.perf_event_paranoid",
        "kernel.unprivileged_bpf_disabled",
        "kernel.unprivileged_userns_clone",
        "kernel.apparmor_restrict_unprivileged_userns",
        "kernel.randomize_va_space",
        "kernel.sysrq",
        "kernel.io_uring_disabled",
        "vm.mmap_min_addr",
        "vm.unprivileged_userfaultfd",
        "dev.tty.ldisc_autoload",
        "dev.tty.legacy_tiocsti",
        "user.max_user_namespaces",
    ],
)
def test_a_sysctl_drop_that_weakens_a_kernel_protection_is_refused(key):
    # applied at once like net.*: loosening the kernel's own protections is a critical action the owner signs
    problem = policy.drop_problem("/etc/sysctl.d", "60-kd.conf", "vm.swappiness = 10\n%s = 0\n" % key)
    assert problem is not None and "kd-admin-propose-action" in problem, problem


def test_a_binfmt_handler_is_a_program_the_kernel_starts():
    problem = policy.drop_problem("/etc/sysctl.d", "60-kd.conf", "fs.binfmt_misc.register = x\n")
    assert problem is not None and "fs.binfmt_misc" in problem, problem


@pytest.mark.parametrize("key", ["kernel.pid_max", "kernel.panic", "fs.file-max", "vm.max_map_count", "kernel.shmmax"])
def test_ordinary_kernel_tunables_may_still_be_dropped(key):
    assert policy.drop_problem("/etc/sysctl.d", "60-kd.conf", "%s = 1\n" % key) is None


@pytest.mark.parametrize(
    "text",
    [
        "\"/var/log/app/access.log\" '/var/log/app/error.log' {\n  copytruncate\n}\n",
        "# the app's logs\n/var/log/a.log\n/var/log/b.log\n{\n  daily\n}\n",
        "/var/log/a.log {\n  daily\n}\n/var/log/b/*.log {\n  weekly\n  nocreate\n}\n",
        "compress\n/var/log/a.log {\n  daily\n  dateext\n}\n",
        "/var/log/nginx/*.log {\n  daily\n  rotate 7\n  delaycompress\n  notifempty\n  noolddir\n}\n",
    ],
)
def test_logrotate_stanzas_of_var_log_may_be_dropped(text):
    assert policy.drop_problem("/etc/logrotate.d", "app", text) is None, text


@pytest.mark.parametrize(
    "text,why",
    [
        ("/etc/shadow {\n  create 0666 root root\n}\n", "/var/log/"),
        ("/etc/shadow {\n  daily\n}\n", "/var/log/"),
        ('"/etc/shadow" {\n  daily\n}\n', "/var/log/"),
        ("/var/log/a.log /etc/shadow {\n  daily\n}\n", "/var/log/"),
        ("/var/log/a.log\n/etc/shadow\n{\n  daily\n}\n", "/var/log/"),
        ("/var/log/a.log # /etc/shadow {\n  daily\n}\n", "/var/log/"),
        ("/var/log/../../etc/shadow {\n  daily\n}\n", "/var/log/"),
        ("/var/log/.*/.*/etc/shadow {\n  daily\n}\n", "/var/log/"),  # .* matches ..
        ("/var/log/\\.\\./x {\n  daily\n}\n", "/var/log/"),
        ("/var/log*/x {\n  daily\n}\n", "/var/log/"),
        ("/var/log {\n  daily\n}\n", "/var/log/"),
        ("~/x.log {\n  daily\n}\n", "/var/log/"),
        ("/var/log/a.log\ndaily\n{\n  weekly\n}\n", "/var/log/"),  # a word before { is a file name
        ("/var/log/a.log {\n  daily\n}\n} /etc/shadow {\n  daily\n}\n", "}"),
        ("missingok {\n/etc/shadow\n}\n", "{"),
        ("/var/log/a.log {\n  daily\n  /etc/shadow\n}\n", "директив"),
        ("/var/log/a.log {\n  daily\n", "}"),
        ("/var/log/a.log\n", "{"),
        ("/var/log/a.log {\n  create 0666 root root\n}\n", "create"),
        ("/var/log/a.log {\n  create\n}\n", "create"),
        ("create 0640 root adm\n/var/log/a.log {\n  daily\n}\n", "create"),
        ("/var/log/a.log {\n  su root root\n}\n", "su"),
        ("/var/log/a.log {\n  olddir /etc\n}\n", "olddir"),
        ("/var/log/a.log {\n  createolddir 0777 root root\n}\n", "createolddir"),
        ("/var/log/a.log { copy }\n", "copy"),
    ],
)
def test_a_logrotate_stanza_beyond_var_log_or_with_file_options_is_refused(text, why):
    # a stanza on /etc/shadow with create 0666 would hand root's file to everyone; olddir, su and copy move or
    # write files where the step does not say
    problem = policy.drop_problem("/etc/logrotate.d", "app", text)
    assert problem is not None and why in problem, problem


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
