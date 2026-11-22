from pathlib import Path

import pytest

from kd import naming
from sysadmin import denylist

CAUGHT = [
    # (rule, command, server)
    ("firewall-flush", "iptables -F", "example-prod"),
    ("firewall-flush", "iptables -t nat -F POSTROUTING", "example-prod"),
    ("firewall-flush", "ip6tables -X", "example-prod"),
    ("firewall-flush", "iptables -P INPUT DROP", "example-prod"),
    ("firewall-flush", "/usr/sbin/iptables --flush", "example-prod"),
    ("firewall-flush", "sudo -u root iptables -F", "node-a"),
    ("firewall-flush", "bash -c 'iptables -F'", "node-c"),
    ("firewall-flush", "nft flush ruleset", "node-c"),
    ("firewall-flush", "ufw disable", "node-d"),
    ("firewall-flush", "ufw --force reset", "node-d"),
    ("firewall-flush", "nft -f /tmp/rules", "node-c"),
    ("firewall-flush", "nft delete table inet filter", "node-c"),
    ("firewall-flush", "iptables-restore < /tmp/rules.v4", "node-c"),
    ("rsync-delete", "rsync -a --delete /tmp/x/ /var/www/api.example.test/", "example-prod"),
    ("rsync-delete", "rsync -av --delete-after src/ dst/", "node-c"),
    ("rm-critical", "rm -rf /", "node-c"),
    ("rm-critical", "rm -rf /*", "node-c"),
    ("rm-critical", "rm -r --no-preserve-root /", "node-c"),
    ("rm-critical", "rm -rf /var/www/api.example.test", "example-prod"),
    ("rm-critical", "rm -fr /backups/old", "node-b"),
    ("rm-critical", "rm -R /var/lib/mysql", "example-prod"),
    ("rm-critical", "rm --recursive --force /etc/nginx", "example-prod"),
    ("rm-critical", "cd /tmp && rm -rf /opt/", "node-c"),
    ("rm-critical", "rm -rf /var/*", "node-c"),
    ("rm-critical", "rm -rf //etc", "node-c"),
    ("rm-critical", "rm -rf /va*", "node-c"),
    ("rm-critical", "rm -rf /var/w*", "example-prod"),
    ("rm-critical", "mv /etc /etc.bak", "node-c"),
    ("find-destructive", "find /var/www -delete", "example-prod"),
    ("find-destructive", "find / -name *.conf -delete", "node-c"),
    ("find-destructive", "find /tmp -maxdepth 0 -exec reboot \\;", "example-stage"),
    ("find-destructive", "find /var/log -name *.gz -execdir rm {} +", "node-c"),
    ("disk-destroy", "mkfs.ext4 /dev/sdb1", "node-b"),
    ("disk-destroy", "dd if=/dev/zero of=/dev/sda bs=1M", "node-b"),
    ("disk-destroy", "wipefs -a /dev/vdb", "node-b"),
    ("disk-destroy", "fdisk /dev/vdb", "node-b"),
    ("disk-destroy", "parted /dev/vdb rm 1", "node-b"),
    ("db-write", 'mysql -e "DELETE FROM app_parsing_queue"', "example-prod"),
    ("db-write", "mysql deliveryalertprod -e 'UPDATE app_aggs SET active=0'", "example-prod"),
    ("db-write", 'mysql -e "SELECT 1; DROP TABLE app_aggs"', "example-prod"),
    ("db-write", 'mysql -e "KILL 12345"', "example-prod"),
    ("db-write", "mysql deliveryalertprod < /tmp/fix.sql", "example-prod"),
    ("db-write", "echo 'TRUNCATE t' | mysql", "example-prod"),
    ("db-write", "mysql", "example-prod"),
    ("db-write", 'sudo mysql --defaults-file=/etc/mysql/debian.cnf -e "GRANT ALL ON *.* TO x"', "example-stage"),
    ("db-write", "mysqladmin -f drop deliveryalertprod", "example-prod"),
    ("access-lockout", "sed -i 's/^PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config", "example-prod"),
    ("access-lockout", "echo ssh-ed25519 AAAA >> /root/.ssh/authorized_keys", "node-c"),
    ("access-lockout", "passwd root", "node-c"),
    ("access-lockout", "echo root:x | chpasswd", "node-c"),
    ("access-lockout", "usermod -L ubuntu", "node-a"),
    ("access-lockout", "systemctl stop ssh", "node-c"),
    ("access-lockout", "service ssh stop", "node-c"),
    ("networkd-restart", "systemctl restart systemd-networkd", "example-prod"),
    ("networkd-restart", "systemctl stop systemd-networkd.service", "node-f"),
    ("dry-run-only", "cd /var/www/api.example.test && ./venv/bin/python sync_manager.py sync", "example-prod"),
    ("code-write", "echo x > /var/www/api.example.test/app.py", "example-prod"),
    ("code-write", "echo x >>/opt/app/app/lib/shop.py", "node-c"),
    ("code-write", "tee /var/www/testapi.example.test/lib/x.py", "example-stage"),
    ("code-write", "sed -i 's/a/b/' /var/www/api.example.test/lib/x.py", "example-prod"),
    ("code-write", "cp /tmp/x.py /var/www/api.example.test/lib/x.py", "example-prod"),
    ("code-write", "mv /tmp/x.py /opt/app/app/x.py", "node-c"),
    ("code-write", "install -m 644 x.py /var/www/api.example.test/lib/", "example-prod"),
    ("code-write", "cd /var/www/testapi.example.test && echo evil > index.php", "example-stage"),
    ("code-write", "cd /var/www/site; sed -i s/a/b/ index.php", "example-prod"),
    ("code-write", "cd /var/www/x && rm -rf *", "example-stage"),
    ("code-write", "rm -f /var/www/api.example.test/app.py", "example-prod"),
    ("code-write", "truncate -s0 /var/lib/mysql/ibdata1", "example-prod"),
    ("code-write", ": > /var/lib/mysql/x.ibd", "example-prod"),
    ("backup-write", "echo x > /backups/important", "node-b"),
    ("backup-write", "rm /backups/db-2026-10-01.sql.gz", "node-b"),
    ("backup-write", "truncate -s0 /backups/x", "node-b"),
    ("git-force-push", "git push --force origin main", "node-c"),
    ("git-force-push", "git -C /opt/app push -f", "node-c"),
    ("git-force-push", "git push origin +main", "node-c"),
    ("socks-only", "curl -s https://www.shop-a.example/en", "example-prod"),
    ("socks-only", "curl https://api.eu.shop-b.example/orders", "example-prod"),
    ("socks-only", "wget -qO- https://merchant.marketxdash.example", "node-c"),
    ("socks-only", "timeout 10 curl -I https://shop-aapp.example", "node-c"),
    ("stage-power", "reboot", "example-stage"),
    ("stage-power", "shutdown -r +5", "example-stage"),
    ("stage-power", "systemctl reboot", "example-stage"),
    ("stage-power", "systemctl restart nginx; reboot", "example-stage"),
    ("cage-write", "echo x > /etc/sudoers.d/zz", "example-stage"),
    ("cron-wipe", "crontab -r", "example-prod"),
    ("kill-all", "kill -9 -1", "node-f"),
]

SAFE = [
    ("iptables -S", "example-prod"),
    ("iptables -t nat -L -n -v", "example-prod"),
    ("iptables -I INPUT -p tcp --dport 22 -s 203.0.113.10 -j ACCEPT", "node-d"),
    ("ufw allow from 203.0.113.10 to any port 22 proto tcp", "node-d"),
    ("nft list ruleset", "node-c"),
    ("rsync -av /var/www/api.example.test/ /backups/www/", "example-prod"),
    ("rsync -a --delay-updates src/ dst/", "node-c"),
    ("rm -rf /tmp/build-123", "node-c"),
    ("rm -rf /var/log/app/old", "example-prod"),
    ("rm /etc/nginx/sites-enabled/old.conf", "example-prod"),
    ("dd if=/dev/zero of=/dev/null bs=1M count=10", "node-c"),
    ('mysql -e "SELECT COUNT(*) FROM app_parsing_queue"', "example-prod"),
    ("mysql -t -e 'SHOW PROCESSLIST'", "example-prod"),
    ("systemctl restart mysql", "example-prod"),
    ("systemctl restart nginx", "example-prod"),
    ("systemctl status systemd-networkd", "example-prod"),
    ("networkctl status", "example-prod"),
    ("usermod -aG adm agent-ro", "node-c"),
    ("systemctl restart ssh", "node-c"),
    ("cd /var/www/api.example.test && sudo ./venv/bin/python sync_manager.py sync --dry-run", "example-prod"),
    ("./venv/bin/python sync_manager.py status", "example-prod"),
    ("cp -a /var/www/api.example.test /backups/www-2026-10-04", "example-prod"),
    ("tar czf /backups/app.tgz /opt/app", "node-c"),
    ("cp config.ini config.ini.bak", "example-prod"),
    (
        "cp /tmp/config.ini.new /var/www/api.example.test/config.ini.new"
        " && mv /var/www/api.example.test/config.ini.new /var/www/api.example.test/config.ini",
        "example-prod",
    ),
    ("chown -R www-data:www-data /var/www/api.example.test/logs", "example-prod"),
    ("git -C /opt/app pull --ff-only", "node-c"),
    ("git push origin main", "node-c"),
    ("git push -u origin feature", "node-c"),
    ("curl -s http://127.0.0.1:8005/health", "example-prod"),
    ("curl --socks5-hostname 127.0.0.1:1080 https://www.shop-a.example/en", "example-prod"),
    ("ALL_PROXY=socks5h://127.0.0.1:1080 curl https://www.marketx.example", "example-prod"),
    ("curl https://api.telegram.org/", "example-prod"),
    ("reboot", "example-prod"),
    ("systemctl reboot", "node-e"),
    ("journalctl -u nginx -n 50", "example-stage"),
    ("grep -c DELETE /var/log/mysql/mysql.log", "example-prod"),
    ("rm -rf /tmp/build-*", "node-c"),
    ("rm -f /var/log/app/old-*.log", "example-prod"),
    ("find /var/log -name *.gz -mtime +30 -print", "node-c"),
    ("mv /etc/nginx/sites-enabled/old.conf /root/", "example-prod"),
    ("mysqladmin processlist", "example-prod"),
    ("service nginx reload", "example-prod"),
    ("cd /var/www/api.example.test && cp config.ini config.ini.bak-2026-10-04", "example-prod"),
    ("rsync -a /var/www/api.example.test/ /backups/www-new/", "example-prod"),
    ("ls -la /backups > /tmp/list", "node-b"),
    # the formed commands of package.upgrade (kd.admin_operations.remote_argv)
    ("systemd-run --unit=kd-package-upgrade --collect --setenv=NEEDRESTART_SUSPEND=1 "
     "--setenv=DEBIAN_FRONTEND=noninteractive apt-get -q -y -o Dpkg::Options::=--force-confdef "
     "-o Dpkg::Options::=--force-confold full-upgrade", "example-prod"),
    ("systemd-run --unit=kd-package-upgrade --collect dnf -q -y upgrade", "node-c"),
    # the formed commands of cron.line, file.drop and gate.script (the helper writes, not the command)
    ("python3 -I /usr/local/libexec/agent-typed-op cron KD-7 set '0 3 * * * /usr/sbin/logrotate /etc/x.conf'",
     "example-prod"),
    ("python3 -I /usr/local/libexec/agent-typed-op cron KD-7 remove", "example-prod"),
    ("python3 -I /usr/local/libexec/agent-typed-op file /etc/logrotate.d app KD-7 set '/var/log/a.log {\n daily\n}\n'",
     "node-c"),
    ("python3 -I /usr/local/libexec/agent-typed-op file /etc/sysctl.d 60-kd.conf KD-7 remove", "node-c"),
    ("python3 -I /usr/local/libexec/agent-typed-op script off", "node-c"),
]


@pytest.mark.parametrize("rule,command,server", CAUGHT)
def test_rule_catches_its_example(rule, command, server):
    hit = denylist.check(command, server)
    assert hit is not None and hit.name == rule


@pytest.mark.parametrize("command,server", SAFE)
def test_safe_twin_passes(command, server):
    assert denylist.check(command, server) is None


def test_every_rule_has_an_example():
    # deployment-rules catches every command while the deployment's rules are broken: its own test below
    assert {rule for rule, _, _ in CAUGHT} == {r.name for r in denylist.RULES} - {"deployment-rules"}


def test_the_deployments_rules_come_from_its_overlay_file(tmp_path, monkeypatch):
    """Review 04.11.2026: no deployment's code trees, SOCKS-only hosts or dry-run tools in the public list."""
    host = tmp_path / "host.json"
    host.write_text((Path(naming.host_json_path())).read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("KD_HOST_JSON", str(host))
    denylist.deployment.cache_clear()
    try:
        # no file: the public rules only
        assert denylist.check("echo x >> /opt/app/lib.py", "node-c") is None
        assert denylist.check("curl https://www.shop-a.example/en", "node-c") is None
        assert denylist.check("./sync_manager.py sync", "node-c") is None
        assert denylist.check("echo x >> /var/www/site/lib.py", "node-c").name == "code-write"
        # a broken file: nothing runs
        (tmp_path / "sysadmin_rules.json").write_text('{"schema_version": 1, "code_trees": ["relative"]}')
        denylist.deployment.cache_clear()
        assert denylist.check("uptime", "node-c").name == "deployment-rules"
    finally:
        denylist.deployment.cache_clear()


def test_stage_power_needs_stage():
    assert denylist.check("reboot") is None
    assert denylist.check("reboot", "example-stage").name == "stage-power"


@pytest.mark.parametrize(
    "command,argv",
    [
        ("sudo -u root timeout 10 nice -n 5 iptables -F", ["iptables", "-F"]),
        ("env -i PATH=/bin bash -lc 'rm -rf /'", ["rm", "-rf", "/"]),
        ("FOO=1 systemd-run --unit=x --wait apt full-upgrade", ["apt", "full-upgrade"]),
        ("xargs -I{} rm -rf {}", ["rm", "-rf", "{}"]),
    ],
)
def test_command_argv_strips_wrappers(command, argv):
    assert denylist.command_argv(denylist.parse(command).segments[0]) == argv


def test_parse_splits_on_operators_inside_quotes():
    segments = denylist.parse("bash -c \"a; b && c | d $(e) `f`\"").segments
    assert [s[0] for s in segments] == ["bash", "b", "c", "d", "e", "f"]


# --- round 1 of fixes (the security review): bypasses and false positives ---------------

PROD = "example-prod"
STAGE = "example-stage"

BYPASS_R1 = [
    # A1: a # comment gives no exemptions
    ("A1", "db-write", "mysql deliveryalertprod < /tmp/fix.sql # -e SELECT 1", PROD),
    ("A1", "code-write", "cp /tmp/evil.php /var/www/api.example.test/lib/ # config.ini", PROD),
    ("A1", "code-write", "install -m 644 /tmp/evil.php /var/www/api.example.test/lib/ # config.ini", PROD),
    ("A1", "code-write", "rsync /tmp/evil.php /var/www/api.example.test/lib/ # config.ini", PROD),
    ("A1", "dry-run-only", "./venv/bin/python sync_manager.py sync # --dry-run", PROD),
    ("A1", "socks-only", "curl https://www.shop-a.example/en # --socks5-hostname", PROD),
    ("A1", "code-write", "cp evil '#' /var/www/api.example.test/lib/", PROD),  # a quoted '#' is not a comment
    # A2: several queries and client commands in mysql -e
    ("A2", "db-write", "mysql -e \"SELECT 1; SET PASSWORD FOR 'root'@'localhost' = 'x'\"", PROD),
    ("A2", "db-write", "mysql -e \"SELECT 1; SET GLOBAL general_log_file='/var/www/x/s.php'; SET GLOBAL general_log=1\"", PROD),
    ("A2", "db-write", "mysql -e 'SELECT 1; PREPARE s FROM 0x44524f50205441424c452078; EXECUTE s'", PROD),
    ("A2", "db-write", "mysql -e 'SELECT 1; system iptables -F'", PROD),
    ("A2", "firewall-flush", "mysql -e 'SELECT 1; \\! iptables -F'", PROD),  # the firewall rule fires first
    ("A2", "db-write", "mysql -e 'SELECT 1; \\! id'", PROD),
    ("A2", "db-write", "mysql -e 'SELECT 1; source /tmp/evil.sql'", PROD),
    ("A2", "db-write", "mysql -e 'SELECT 1\\! id'", PROD),
    # A3: the keywords of compound commands
    ("A3", "firewall-flush", "{ iptables -F; }", PROD),
    ("A3", "firewall-flush", "! iptables -F", PROD),
    ("A3", "firewall-flush", "eval iptables -F", PROD),
    ("A3", "rm-critical", "f(){ rm -rf /var/www; }; f", PROD),
    ("A3", "rm-critical", "function f { rm -rf /var/www; }; f", PROD),
    ("A3", "rm-critical", "if true; then rm -rf /var/www; fi", PROD),
    ("A3", "rm-critical", "for x in 1; do rm -rf /var/www; done", PROD),
    # A4: $'…' and $"…"
    ("A4", "rm-critical", "$'rm' -rf /var/www", PROD),
    ("A4", "firewall-flush", "$'iptables' -F", PROD),
    ("A4", "firewall-flush", '$"iptables" -F', PROD),
    # A5: su / runuser / doas / long sudo options
    ("A5", "firewall-flush", "su -c 'iptables -F'", PROD),
    ("A5", "firewall-flush", "su root -c 'iptables -F'", PROD),
    ("A5", "rm-critical", "runuser -u root -- rm -rf /var/www", PROD),
    ("A5", "firewall-flush", "runuser root -c 'iptables -F'", PROD),
    ("A5", "firewall-flush", "doas -u root iptables -F", PROD),
    ("A5", "firewall-flush", "sudo --user root iptables -F", PROD),
    ("A5", "firewall-flush", "bash -o pipefail -c 'iptables -F'", PROD),
    # A6: service systemd-networkd
    ("A6", "networkd-restart", "service systemd-networkd restart", PROD),
    ("A6", "networkd-restart", "service systemd-networkd stop", PROD),
    # A7: power targets and sysrq on the factory host
    ("A7", "stage-power", "systemctl start reboot.target", STAGE),
    ("A7", "stage-power", "systemctl isolate poweroff.target", STAGE),
    ("A7", "stage-power", "systemctl isolate rescue.target", STAGE),
    ("A7", "stage-power", "kexec -e", STAGE),
    ("A7", "stage-power", "echo b > /proc/sysrq-trigger", STAGE),
    # A8: /root/.ssh
    ("A8", "access-lockout", "rm -rf /root/.ssh", PROD),
    ("A8", "access-lockout", "mv /root/.ssh /root/.ssh.bak", PROD),
    # A9: find | xargs rm
    ("A9", "find-destructive", "find /var/www -type f | xargs rm -f", PROD),
    ("A9", "find-destructive", "find /var/www -type f -print0 | xargs -0 rm", PROD),
    ("A9", "find-destructive", "ls /var/www | xargs rm -rf", PROD),
    # xargs — always a prohibition, operands do not save it
    ("R2", "find-destructive", "find /x | xargs rm -f /tmp/a # /etc", PROD),
    ("R2", "find-destructive", "ls /var/www | xargs rm -rf /tmp/x", PROD),
    ("R2", "find-destructive", "xargs -0 rm /tmp/a", PROD),
    # if/while/until as wrappers
    ("R2", "rm-critical", "if rm -rf /var/www; then :; fi", PROD),
    ("R2", "firewall-flush", "while iptables -F; do :; done", PROD),
    ("R2", "rm-critical", "until rm -rf /var/www; do :; done", PROD),
    # --init-command runs before -e
    ("R2", "db-write", "mysql --init-command='DROP TABLE x' -e 'SELECT 1'", PROD),
    ("R2", "db-write", "mysql --init-command 'SET GLOBAL x=1' -e 'SELECT 1'", PROD),
    ("R2", "db-write", "mariadb --init-command=x -e 'SELECT 1'", PROD),
    # the -c of a nested command inside su/runuser is not taken for the wrapper's own -c
    ("R2", "rm-critical", "su root -c 'runuser -u x -- rm -rf /var/www'", PROD),
    ("R2", "rm-critical", "runuser -u root -- rm -rf /var/www", PROD),
    # B10: >| and &>
    ("B10", "code-write", "echo x >| /var/www/api.example.test/app.py", PROD),
    ("B10", "code-write", "echo x &> /var/www/api.example.test/app.py", PROD),
    ("B10", "code-write", "echo x &>> /var/www/api.example.test/app.py", PROD),
    # B11: config.ini.* — only .new/.tmp/.orig/.bak
    ("B11", "code-write", "cp /tmp/evil.php /var/www/example.test/config.ini.php", PROD),
    ("B11", "code-write", "cp /tmp/evil.php /var/www/example.test/config.ini.new.php", PROD),
    # B12: mv -t / cp -t
    ("B12", "rm-critical", "mv -t /tmp /etc", PROD),
    ("B12", "rm-critical", "mv --target-directory=/tmp /etc", PROD),
    ("B12", "code-write", "cp -t /var/www/api.example.test/lib/ /tmp/evil.php", PROD),
    # B13: cd -P / cd -L
    ("B13", "code-write", "cd -P /var/www/x && echo evil > index.php", PROD),
    ("B13", "code-write", "cd -L /var/www/x && rm -rf *", PROD),
    # B14: downloading and unpacking into protected directories
    ("B14", "code-write", "curl -o /var/www/x/a.php https://example.org/a.php", PROD),
    ("B14", "code-write", "curl -fsSo /var/www/x/a.php https://example.org/a.php", PROD),
    ("B14", "code-write", "curl --output /var/www/x/a.php https://example.org/a.php", PROD),
    ("B14", "code-write", "wget -O /var/www/x/a.php https://example.org/a.php", PROD),
    ("B14", "code-write", "wget -P /var/www/x https://example.org/a.php", PROD),
    ("B14", "code-write", "wget --directory-prefix=/var/www/x https://example.org/a.php", PROD),
    ("B14", "code-write", "tar xzf /tmp/a.tgz -C /var/www/x", PROD),
    ("B14", "code-write", "tar -C /opt/app --extract -f /tmp/a.tar", PROD),
    ("B14", "code-write", "tar xf a.tar --directory /var/www/x", PROD),
    ("B14", "code-write", "unzip -d /var/www/x /tmp/a.zip", PROD),
    ("B14", "code-write", "rsync -a --remove-source-files /var/www/x/ /tmp/y/", PROD),
    ("B14", "backup-write", "curl -o /backups/important https://example.org/a", PROD),
    # B15: system directories
    ("B15", "rm-critical", "rm -rf /usr/bin", PROD),
    ("B15", "rm-critical", "rm -rf /dev", PROD),
    ("B15", "rm-critical", "rm -rf /proc", PROD),
    ("B15", "rm-critical", "rm -rf /sys", PROD),
    ("B15", "rm-critical", "rm -rf /run", PROD),
    ("B15", "rm-critical", "rm -rf /boot/grub", PROD),
    # B16: loss of access, the simple forms
    ("B16", "access-lockout", "pkill sshd", PROD),
    ("B16", "access-lockout", "killall ssh", PROD),
    ("B16", "access-lockout", "systemctl stop ssh.socket", PROD),
    ("B16", "access-lockout", "systemctl disable sshd.socket", PROD),
    ("B16", "access-lockout", "systemctl mask 'ssh*'", PROD),
    ("B16", "access-lockout", "/etc/init.d/ssh stop", PROD),
    ("B16", "access-lockout", "usermod -p x root", PROD),
    ("B16", "access-lockout", "usermod -s /bin/false root", PROD),
    ("B16", "access-lockout", "usermod -e 1 root", PROD),
    ("B16", "access-lockout", "passwd -l root", PROD),
    ("B16", "access-lockout", "passwd -d root", PROD),
    ("B16", "access-lockout", "chage -E0 root", PROD),
    ("B16", "access-lockout", "userdel -r agent-ro", PROD),
    ("B16", "access-lockout", "mv /etc/ssh /etc/ssh.bak", PROD),
    # B17: the networkd counterparts
    ("B17", "networkd-restart", "netplan apply", PROD),
    ("B17", "networkd-restart", "ip rule flush", PROD),
    ("B17", "networkd-restart", "ip rule del from 10.0.0.1 table 100", PROD),
    # B18: the database counterparts
    ("B18", "db-write", "mariadb-admin -f drop deliveryalertprod", PROD),
    ("B18", "db-write", "mysqlimport deliveryalertprod /tmp/t.txt", PROD),
    # B19: git push --delete / :branch / --mirror
    ("B19", "git-force-push", "git push --delete origin main", "node-c"),
    ("B19", "git-force-push", "git push origin :main", "node-c"),
    ("B19", "git-force-push", "git push --mirror", "node-c"),
    ("B19", "git-force-push", "git push -d origin main", "node-c"),
]

SAFE_R1 = [
    # A1: a comment by itself is harmless
    ("A1", "ls /var/www # config.ini", PROD),
    ("A1", "mysql -e 'SELECT 1' # config.ini", PROD),
    # A2: one query, with a trailing ; too
    ("A2", "mysql -e 'SELECT 1;'", PROD),
    ("A2", 'mysql -e "SHOW PROCESSLIST;"', PROD),
    # A3, A5: wrappers do not get in the way of harmless commands
    ("A3", "if true; then ls /var/www; fi", PROD),
    ("A3", "! grep -q x /etc/hosts", PROD),
    ("A5", "su -c 'systemctl restart nginx'", PROD),
    ("A5", "runuser -u www-data -- ls /var/www", PROD),
    ("A5", "sudo --user root systemctl status nginx", PROD),
    # A6, A7
    ("A6", "service systemd-networkd status", PROD),
    ("A7", "systemctl restart nginx", STAGE),
    ("A7", "systemctl start reboot.target", PROD),
    ("A7", "kexec -l /boot/vmlinuz", STAGE),
    # A8
    ("A8", "ls /root", PROD),
    # A9: xargs with a harmless command or with an absolute path that other rules deal with
    ("A9", "find /var/log -name *.gz -print | xargs ls -l", PROD),
    # B10, B11: an atomic edit of config.ini
    ("B10", "echo x >| /tmp/out", PROD),
    ("B11", "cp /tmp/config.ini.tmp /var/www/api.example.test/config.ini.tmp", PROD),
    ("B11", "cp config.ini config.ini.orig-2", PROD),
    ("B11", "cd /var/www/api.example.test && cp config.ini config.ini.bak-2026-10-04", PROD),
    # B12, B13
    ("B12", "mv -t /tmp /etc/nginx/sites-enabled/old.conf", PROD),
    ("B13", "cd -P /tmp && ls", PROD),
    # B14
    ("B14", "curl -o /tmp/a.php https://example.org/a.php", PROD),
    ("B14", "wget -qO- https://example.org/a", PROD),
    ("B14", "tar czf /backups/app.tgz /opt/app", "node-c"),
    ("B14", "tar xzf /tmp/a.tgz -C /tmp/x", PROD),
    ("B14", "unzip -d /tmp/x /tmp/a.zip", PROD),
    ("B14", "rsync -a --remove-source-files /tmp/x/ /tmp/y/", PROD),
    # B15
    ("B15", "rm -rf /tmp/build-1", PROD),
    ("B15", "rm -rf /home/agent-ro/tmp-x", PROD),
    # B16
    ("B16", "systemctl restart ssh", PROD),
    ("B16", "systemctl stop nginx", PROD),
    ("B16", "pkill -f nginx", PROD),
    ("B16", "usermod -aG adm agent-ro", PROD),
    ("B16", "chage -l agent-ro", PROD),
    ("B16", "cat /etc/ssh/ssh_config", PROD),
    # B17
    ("B17", "ip rule show", PROD),
    ("B17", "ip -br addr", PROD),
    ("B17", "netplan get", PROD),
    # B18, B21
    ("B18", "mariadb-admin processlist", PROD),
    ("B21", "mysqladmin -u root -p status", PROD),
    ("B21", "mysqladmin --user=root -h localhost variables", PROD),
    ("B21", "mysqladmin -S /run/mysqld/mysqld.sock status", PROD),
    # B19
    ("B19", "git push origin feature", "node-c"),
    # B20: an atomic edit of config.ini in the site's directory
    ("B20", "cd /var/www/api.example.test && tee -a config.ini <<< '[x]'", PROD),
    ("B20", "cd /var/www/api.example.test && sed -i 's/x/y/' config.ini", PROD),
    ("B20", "cd /var/www/api.example.test && sed -i -e s/x/y/ config.ini", PROD),
    ("B20", "cd /var/www/api.example.test && grep '->' lib/x.py", PROD),
    ("B20", "cd /var/www/api.example.test && grep -c x lib/x.py 2>/dev/null", PROD),
]


@pytest.mark.parametrize("item,rule,command,server", BYPASS_R1)
def test_r1_bypass_is_caught(item, rule, command, server):
    hit = denylist.check(command, server)
    assert hit is not None and hit.name == rule, item


@pytest.mark.parametrize("item,command,server", SAFE_R1)
def test_r1_false_positive_passes(item, command, server):
    assert denylist.check(command, server) is None, item


@pytest.mark.parametrize(
    "command,argv",
    [
        ("su -c 'iptables -F'", ["iptables", "-F"]),
        ("su - root -c 'sudo -u x rm -rf /'", ["rm", "-rf", "/"]),
        ("runuser -u root -- rm -rf /var/www", ["rm", "-rf", "/var/www"]),
        ("doas -u root ls", ["ls"]),
        ("sudo --user root --host h ls", ["ls"]),
        ("bash -o pipefail -c 'ls -l'", ["ls", "-l"]),
        ("if true; then ls", ["ls"]),
        # the -c of a nested command (tar -zc …) is not confused with the wrapper's -c
        ("runuser -u root -- tar -zc /etc", ["tar", "-zc", "/etc"]),
        ("runuser -u root tar -zc /etc", ["tar", "-zc", "/etc"]),
        ("su root -c 'tar -zc /etc'", ["tar", "-zc", "/etc"]),
        ("while rm x", ["rm", "x"]),
    ],
)
def test_command_argv_r1_wrappers(command, argv):
    assert denylist.command_argv(denylist.parse(command).segments[-1]) == argv


def test_braces_split_only_as_words():
    assert denylist.parse("find . -exec rm {} +").segments == [["find", ".", "-exec", "rm", "{}", "+"]]


# --- the final review: A2 — the agents' cage (cage-write), A6 — cheap counterparts ----------

FINAL_CAUGHT = [
    # A2: writing, deleting, moving, permissions in the agents' home, sudoers, wrappers and units
    ("A2", "cage-write", 'echo "agent ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/zz', STAGE),
    ("A2", "cage-write", "usermod -aG sudo agent", STAGE),
    ("A2", "cage-write", "cp /tmp/x /etc/claude-code/managed-settings.json", STAGE),
    ("A2", "cage-write", "cp /tmp/x /srv/digipaddock-agents/sysadmin/.claude/settings.json", STAGE),
    ("A2", "cage-write", "echo x > /etc/digipaddock/servers.enabled", STAGE),
    ("A2", "cage-write", "sed -i s/a/b/ /usr/local/bin/agent-ro-gate", PROD),
    ("A2", "cage-write", "echo x > /etc/sudoers.d/agent-ro", PROD),
    ("A2", "cage-write", "rm -rf /home/digi", STAGE),
    ("A2", "cage-write", "rm -rf /home/digi-sysadmin", STAGE),  # M02: the sysadmin's own home
    ("A2", "cage-write", "cp /tmp/x /home/digi-sysadmin/.bashrc", STAGE),
    ("A2", "cage-write", "rm /etc/sudoers.d/digi-exec", STAGE),  # the kill switches — only by the owner's hand
    ("A2", "cage-write", ": > /etc/digipaddock/servers.enabled", STAGE),
    ("A2", "cage-write", "tee -a /etc/sudoers", PROD),
    ("A2", "cage-write", "visudo", PROD),
    ("A2", "cage-write", "visudo -f /etc/sudoers.d/x", PROD),
    ("A2", "cage-write", "gpasswd -a agent sudo", STAGE),
    ("A2", "cage-write", "adduser agent sudo", STAGE),
    ("A2", "cage-write", "usermod -G wheel,adm agent", "node-g"),
    ("A2", "cage-write", "usermod --groups=admin agent", STAGE),
    ("A2", "cage-write", "chmod 0777 /etc/sudoers.d/digi-exec", STAGE),
    ("A2", "cage-write", "chown agent /usr/local/bin/ssh-exec", STAGE),
    ("A2", "cage-write", "ln -sf /tmp/x /usr/local/bin/ssh-ro", STAGE),
    ("A2", "cage-write", "cp /tmp/x /etc/systemd/system/digipaddock-cabinet.service", STAGE),
    ("A2", "cage-write", "cp /tmp/x /opt/digipaddock/agents/sysadmin/bin/ssh_exec.py", STAGE),
    ("A2", "cage-write", "rm /home/digi-exec/.bashrc", STAGE),
    ("A2", "cage-write", "echo x >> /var/log/digipaddock/exec.jsonl", STAGE),
    ("A2", "cage-write", "cd /etc/sudoers.d && echo x > zz", STAGE),
    ("A2", "cage-write", "echo x > /home/agent-ro/.my.cnf", PROD),
    # recursively, by permissions or by a move — through the parent directory
    ("A2", "cage-write", "chown -R agent /opt", STAGE),
    ("A2", "cage-write", "chmod 0777 /home/agent-ro", PROD),
    ("A2", "cage-write", "rm -rf /var/log", STAGE),
    ("A2", "cage-write", "mv /usr/local/bin /tmp/b", STAGE),
    # A6: loss of access — closing port 22
    ("A6", "access-lockout", "iptables -A INPUT -p tcp --dport 22 -j DROP", PROD),
    ("A6", "access-lockout", "ip6tables -I INPUT 1 -p tcp --dport ssh -j REJECT", PROD),
    ("A6", "access-lockout", "iptables -I INPUT -p tcp -m multiport --dports 80,22 -j DROP", PROD),
    ("A6", "access-lockout", "iptables -t filter --insert INPUT -p tcp --destination-port 22 --jump DROP", PROD),
    ("A6", "access-lockout", "ufw deny 22", "node-d"),
    ("A6", "access-lockout", "ufw deny 22/tcp", "node-d"),
    ("A6", "access-lockout", "ufw reject ssh", "node-d"),
    ("A6", "access-lockout", "ufw deny OpenSSH", "node-d"),
    ("A6", "access-lockout", "ufw default deny incoming", "node-d"),
    # A6: flushing and replacing the firewall
    ("A6", "firewall-flush", "nft destroy table inet filter", "node-c"),
    ("A6", "firewall-flush", "netfilter-persistent flush", PROD),
    ("A6", "firewall-flush", "iptables-apply /etc/iptables/rules.v4", PROD),
    ("A6", "firewall-flush", "xtables-nft-multi iptables -F", PROD),
    ("A6", "firewall-flush", "xtables-legacy-multi ip6tables -X", PROD),
    # A6: disks
    ("A6", "disk-destroy", "mke2fs -t ext4 /dev/sdb1", "node-b"),
    ("A6", "disk-destroy", "mkfs.xfs -f /dev/sdb", "node-b"),
    ("A6", "disk-destroy", "mkswap /dev/sdb2", "node-b"),
    ("A6", "disk-destroy", "blkdiscard /dev/sdb", "node-b"),
    ("A6", "disk-destroy", "cryptsetup luksFormat /dev/sdb", "node-b"),
    ("A6", "disk-destroy", "cryptsetup erase /dev/sdb", "node-b"),
    ("A6", "disk-destroy", "cryptsetup open --type plain /dev/sdb wipe", "node-b"),
    ("A6", "disk-destroy", "lvremove -f vg0/data", "node-b"),
    ("A6", "disk-destroy", "vgremove vg0", "node-b"),
    ("A6", "disk-destroy", "pvremove /dev/sdb", "node-b"),
    ("A6", "disk-destroy", "wipefs -a /dev/sdb", "node-b"),
    # A6: wrappers
    ("A6", "firewall-flush", "chroot /host iptables -F", PROD),
    ("A6", "rm-critical", "chroot --userspec=root:root /host rm -rf /var/www", PROD),
    ("A6", "firewall-flush", "flock /tmp/l iptables -F", PROD),
    ("A6", "firewall-flush", "flock -w 10 /tmp/l -c 'iptables -F'", PROD),
    ("A6", "firewall-flush", "nsenter -t 1 -m -n iptables -F", PROD),
    ("A6", "rm-critical", "unshare -m rm -rf /var/www", PROD),
    ("A6", "firewall-flush", "taskset -c 0-3 iptables -F", PROD),
    ("A6", "firewall-flush", "taskset 3 iptables -F", PROD),
    ("A6", "firewall-flush", "watch -n 5 iptables -F", PROD),
    ("A6", "firewall-flush", "strace -f -o /tmp/kd-t iptables -F", PROD),
    ("A6", "firewall-flush", "script -qc 'iptables -F' /dev/null", PROD),
    ("A6", "firewall-flush", "chrt -f 99 iptables -F", PROD),
]

FINAL_SAFE = [
    # A2
    ("A2", "cat /etc/sudoers.d/digi-exec", STAGE),
    ("A2", "ls /srv/digipaddock-agents", STAGE),
    ("A2", "systemctl status digipaddock-cabinet", STAGE),
    ("A2", "visudo -c", STAGE),
    ("A2", "visudo -c -f /etc/sudoers.d/agent-ro", PROD),
    ("A2", "cp /etc/sudoers.d/digi-exec /tmp/kd-sudoers", STAGE),
    ("A2", "ls -la /home/digi > /tmp/kd-list", STAGE),
    ("A2", "cat /etc/systemd/system/digipaddock.service", STAGE),
    ("A2", "tail -n 50 /var/log/digipaddock/exec.jsonl", STAGE),
    ("A2", "gpasswd -a agent-ro adm", PROD),
    ("A2", "rm -rf /home/agent-ro/tmp-x", PROD),  # the safe counterpart of B15: agent-ro's non-hidden files are not the cage
    # A6
    ("A6", "iptables -S", PROD),
    ("A6", "ufw status", "node-d"),
    ("A6", "ufw allow 22", "node-d"),
    ("A6", "ufw deny 8080", "node-d"),
    ("A6", "ufw default allow outgoing", "node-d"),
    ("A6", "iptables -A INPUT -p tcp --dport 8080 -j DROP", PROD),
    ("A6", "iptables -I INPUT -p tcp --dport 22 -s 203.0.113.10 -j ACCEPT", "node-d"),
    ("A6", "nft list ruleset", "node-c"),
    ("A6", "lvs", "node-b"),
    ("A6", "blkid", "node-b"),
    ("A6", "cryptsetup status cryptroot", "node-b"),
    ("A6", "cryptsetup open /dev/sdb data", "node-b"),
    ("A6", "watch -n 5 df -h", PROD),
    ("A6", "chroot /host ls /var/log", PROD),
    ("A6", "flock /tmp/l -c uptime", PROD),
    ("A6", "taskset -c 0-3 nice -n 5 ls", PROD),
    ("A6", "strace -c ls", PROD),
    ("A6", "script -c uptime /dev/null", PROD),
    ("A6", "chrt -f 10 ls", PROD),
]


@pytest.mark.parametrize("item,rule,command,server", FINAL_CAUGHT)
def test_final_bypass_is_caught(item, rule, command, server):
    hit = denylist.check(command, server)
    assert hit is not None and hit.name == rule, (item, hit and hit.name)


@pytest.mark.parametrize("item,command,server", FINAL_SAFE)
def test_final_safe_twin_passes(item, command, server):
    hit = denylist.check(command, server)
    assert hit is None, (item, hit and hit.name)


CRON_WIPE = [
    ("crontab -r", PROD),
    ("crontab -u root -r", PROD),
    ("crontab -ir", "node-c"),
    ("rm /etc/cron.d/app", PROD),
    ("rm -f /var/spool/cron/crontabs/root", PROD),
    ("mv /etc/crontab /tmp/", "node-c"),
    ("echo x > /etc/crontab", PROD),
    ("echo '* * * * * root x' >> /etc/cron.d/app", PROD),
    ("truncate -s0 /var/spool/cron/crontabs/root", "node-g"),
    ("systemctl stop cron", PROD),
    ("systemctl disable crond", "node-g"),
    ("systemctl mask cron.service", PROD),
    ("service cron stop", "node-c"),
]

CRON_SAFE = [
    ("crontab -l", PROD),
    ("crontab -l -u root", PROD),
    ("cat /etc/crontab", PROD),
    ("ls -la /etc/cron.d", PROD),
    ("systemctl status cron", PROD),
    ("systemctl restart cron", PROD),
]


@pytest.mark.parametrize("command,server", CRON_WIPE)
def test_b9_cron_wipe_is_caught(command, server):
    hit = denylist.check(command, server)
    assert hit is not None and hit.name == "cron-wipe", hit and hit.name


@pytest.mark.parametrize("command,server", CRON_SAFE)
def test_b9_cron_reads_pass(command, server):
    assert denylist.check(command, server) is None


# --- the repeated review: leftovers (R1 — copying into a directory, R3 — flock -c, R4 — agent-ro's home, R5 — false positives) ---

RESIDUAL_CAUGHT = [
    ("R1", "cage-write", "cp /tmp/sudoers /etc/", STAGE),
    ("R1", "cage-write", "mv /tmp/sudoers /etc/", STAGE),
    ("R1", "cage-write", "cp -r /tmp/sudoers.d /etc/", STAGE),
    ("R1", "cage-write", "cp /tmp/ssh-exec /usr/local/bin/", STAGE),
    ("R1", "cage-write", "install -m 755 /tmp/ssh-exec /usr/local/bin/", STAGE),
    ("R1", "cage-write", "cp /tmp/digipaddock-cabinet.service /etc/systemd/system/", STAGE),
    ("R1", "cage-write", "cd /etc && cp /tmp/sudoers .", STAGE),
    ("R1", "cage-write", "cp /tmp/sudoers /etc", STAGE),  # /etc is the cage's parent, hence a directory
    ("R1", "cage-write", "cp /tmp/sudoers /etc/.", STAGE),
    ("R1", "cage-write", "cp -t /usr/local/bin /tmp/agent-ro-gate", STAGE),
    ("R1", "cage-write", "cp --target-directory=/usr/local/bin /tmp/agent-ro-gate", STAGE),
    ("R1", "cage-write", "cp /tmp/a /tmp/agent-launch /usr/local/bin", STAGE),  # several sources
    ("R1", "cage-write", "ln -s /tmp/ssh-ro /usr/local/bin/", STAGE),
    ("R1", "cage-write", "rsync /tmp/agent-sec-snapshot /usr/local/bin/", STAGE),
    ("R1", "cage-write", "cp -a /tmp/etc /", STAGE),  # the directory etc merges into /etc
    ("R1", "cage-write", "rsync -a /tmp/etc/ /etc/", STAGE),  # the contents merge into /etc
    ("R1", "code-write", "rsync -a /tmp/root/ /", STAGE),  # into the root: code-write fires earlier in the order
    ("R1", "cage-write", "cd / && cp -r /tmp/etc .", STAGE),
    ("R1", "code-write", "cp -r /tmp/www /var/", PROD),
    ("R1", "code-write", "cd /var && echo x > www/a.php", PROD),  # a relative path after a cd next to it
    ("R1", "code-write", "rsync -t /tmp/evil.php /var/www/x/a.php", PROD),  # for rsync -t does not name a directory
    ("R3", "firewall-flush", "flock -c 'iptables -F' /tmp/l", PROD),
    ("R3", "firewall-flush", "flock --command 'iptables -F' /tmp/l", PROD),
    ("R4", "cage-write", "cp /tmp/.my.cnf /home/agent-ro/", PROD),
    ("R4", "cage-write", "echo x > /home/agent-ro/.profile", PROD),
    ("R4", "cage-write", "cp /tmp/x /home/agent-ro/", PROD),  # the directory itself is the destination
    ("R4", "cage-write", "ln -sfn /tmp/x /home/agent-ro", PROD),
    ("R4", "cage-write", "rsync -a /tmp/home/ /home/agent-ro/", PROD),
    ("R5", "disk-destroy", "mkswap /dev/sdb2", "node-b"),
    ("R5", "access-lockout", "ufw deny 22", "node-d"),
    ("R5", "access-lockout", "ufw insert 1 deny 22", "node-d"),
]

RESIDUAL_SAFE = [
    ("R1", "cp /tmp/x /tmp/y/", PROD),
    ("R1", "cp -a /var/www/site /backups/x", PROD),
    ("R1", "cd /var/www/api.example.test && cp config.ini config.ini.bak-2026-10-04", PROD),
    ("R1", "cp /tmp/x.conf /etc/", PROD),
    ("R1", "cp /tmp/x /etc/nginx/sites-available/", PROD),
    ("R1", "rsync -a /var/www/api.example.test/ /backups/www/", PROD),
    ("R1", "cd /etc && cp nginx/nginx.conf /tmp/digipaddock-nginx.conf", STAGE),
    ("R1", "cp -r /tmp/x /", STAGE),  # /x is not the cage
    ("R4", "rm -rf /home/agent-ro/tmp-x", PROD),
    ("R4", "ls -la /home/agent-ro", PROD),
    ("R5", "mkswap /swapfile", PROD),
    ("R5", "systemctl kill -s HUP cron", PROD),
    ("R5", "ufw delete deny 22", "node-d"),
]


@pytest.mark.parametrize("item,rule,command,server", RESIDUAL_CAUGHT)
def test_residual_bypass_is_caught(item, rule, command, server):
    hit = denylist.check(command, server)
    assert hit is not None and hit.name == rule, (item, hit and hit.name)


@pytest.mark.parametrize("item,command,server", RESIDUAL_SAFE)
def test_residual_safe_twin_passes(item, command, server):
    hit = denylist.check(command, server)
    assert hit is None, (item, hit and hit.name)


def test_flock_command_before_lockfile_argv():
    assert denylist.command_argv(denylist.parse("flock -c 'ls -l' /tmp/l").segments[0]) == ["ls", "-l", "/tmp/l"]


@pytest.mark.parametrize("command", ["passwd digi", "passwd digi-exec", "passwd -d agent-ro"])
def test_cage_write_itself_covers_cage_passwords(command):
    # access-lockout fires first (any passwd); cage-write catches it on its own too
    rule = next(r for r in denylist.RULES if r.name == "cage-write")
    assert rule.match(denylist.parse(command), STAGE)


@pytest.mark.parametrize(
    "command,argv",
    [
        ("chroot /host iptables -F", ["iptables", "-F"]),
        ("chroot --userspec=0:0 /host ls", ["ls"]),
        ("flock -w 10 /tmp/l ls -l", ["ls", "-l"]),
        ("flock /tmp/l -c 'ls -l'", ["ls", "-l"]),
        ("nsenter -t 1 -m -n ls", ["ls"]),
        ("unshare -m --propagation private ls", ["ls"]),
        ("taskset -c 0-3 ls", ["ls"]),
        ("taskset 3 ls", ["ls"]),
        ("watch -n 5 df -h", ["df", "-h"]),
        ("strace -f -o /tmp/x -e trace=open ls", ["ls"]),
        ("script -qc 'ls -l' /dev/null", ["ls", "-l", "/dev/null"]),
        ("chrt -f 99 ls", ["ls"]),
        ("xtables-nft-multi iptables -S", ["iptables", "-S"]),
    ],
)
def test_command_argv_final_wrappers(command, argv):
    assert denylist.command_argv(denylist.parse(command).segments[-1]) == argv


# --- the cage of the snapshot and the baseline, protection against self-lockout ---------------

CAGE_SNAPSHOT = [
    "cp /tmp/x /usr/local/bin/agent-sec-snapshot", "chmod 777 /usr/local/bin/agent-sec-snapshot",
    "rm /usr/local/bin/agent-sec-snapshot", "cp /tmp/b.json /var/lib/digipaddock/monitor/baseline.json",
    "rm -rf /var/lib/digipaddock", "echo x > /var/lib/digipaddock/monitor/state.json",
]
STAGE_UNITS = ["systemctl stop digipaddock-monitor.timer", "systemctl disable --now digipaddock-session-host.service",
               "systemctl mask digipaddock-session-host.socket", "systemctl kill digipaddock-cabinet.service",
               "systemctl restart digipaddock-cabinet", "systemctl stop 'digipaddock*'"]
LOCKOUT_FW = [
    "iptables -I INPUT -j DROP", "iptables -A INPUT -p tcp -j REJECT", "iptables -I INPUT -s 0.0.0.0/0 -j DROP",
    "iptables -I INPUT -s 203.0.113.10 -j DROP", "ip6tables -I INPUT -s ::/0 -j DROP", "iptables -I INPUT --source any -j DROP",
    "ufw deny from any", "ufw insert 1 deny from 0.0.0.0/0", "ufw deny from 203.0.113.10", "ufw reject from ::/0 to any",
    "ufw enable", "ufw --force enable",
]
KILL_ALL = ["kill 1", "kill -9 -1", "kill -1", "kill -TERM -1", "pkill -u root", "killall -u root"]
# the brief had "ufw insert 1 deny from 198.51.100.0/24 to any port 22" — but that is ufw deny … port 22, which
# access-lockout prohibited before too (the owner's decision of 06.10: only prohibitions, nothing gets allowed) — here
# it is an address block of a subnet without a port, as in the playbook; port 22 is in LOCKOUT_EXTRA
ALLOWED_BLOCKS = ["iptables -I INPUT -s 198.51.100.9 -j DROP", "ufw insert 1 deny from 198.51.100.9",
                  "ufw insert 1 deny from 198.51.100.0/24", "kill 12345", "pkill -f http.server",
                  "systemctl restart fail2ban", "systemctl disable --now evil.service"]


@pytest.mark.parametrize("command", CAGE_SNAPSHOT)
def test_snapshot_and_monitor_state_are_cage(command):
    assert denylist.check(command, "node-f").name == "cage-write"


@pytest.mark.parametrize("command", STAGE_UNITS)
def test_stage_cage_units_cannot_be_stopped(command):
    assert denylist.check(command, STAGE).name == "cage-write"
    assert denylist.check(command, "node-f") is None  # other servers have no such units — the rule is for the factory host only


@pytest.mark.parametrize("command", LOCKOUT_FW)
def test_firewall_lockout_patterns(command):
    rule = denylist.check(command, "node-f")
    assert rule is not None and rule.name in ("access-lockout", "firewall-flush"), command


@pytest.mark.parametrize("command", KILL_ALL)
def test_kill_all_is_denied(command):
    assert denylist.check(command, "node-f").name == "kill-all"


@pytest.mark.parametrize("command", ALLOWED_BLOCKS)
def test_targeted_blocks_still_allowed(command):
    assert denylist.check(command, "node-f") is None


# a self-check: bypasses and forms that a careless model will write by itself
LOCKOUT_EXTRA = [
    ("access-lockout", "iptables -I INPUT -p tcp -j DROP"),
    ("access-lockout", "iptables -I INPUT ! -s 198.51.100.2 -j DROP"),  # "everyone except" covers the owner and the factory host
    ("access-lockout", "iptables -I INPUT -s !198.51.100.2 -j DROP"),  # the old form of negation
    ("access-lockout", "iptables -I INPUT -s 0/0 -j DROP"),
    ("access-lockout", "iptables -I INPUT -s 203.0.113.10/32 -j REJECT"),
    ("access-lockout", "iptables -I INPUT -s 203.0.113.0/24 -j DROP"),  # the subnet with the factory host
    ("access-lockout", "iptables -I INPUT -s 198.51.100.9,203.0.113.10 -j DROP"),
    ("access-lockout", "iptables --insert INPUT 1 --jump DROP"),
    ("access-lockout", "iptables -A input -j drop"),
    ("access-lockout", "sudo iptables -w -I INPUT -i eth0 -j DROP"),
    ("access-lockout", "ufw deny in from any"),
    ("access-lockout", "ufw deny from any to any port 20:30"),  # a range that contains 22
    ("access-lockout", "iptables -I INPUT -p tcp --dport 1:65535 -j DROP"),
    ("access-lockout", "iptables -I INPUT -p tcp -m multiport --dports 80,443,22 -j DROP"),
    ("access-lockout", "ufw reject from 203.0.113.10/32"),
    ("access-lockout", "ufw insert 1 deny from 203.0.113.0/24"),
    ("access-lockout", "ufw default deny incoming"),
    ("access-lockout", "ufw insert 1 deny from 198.51.100.0/24 to any port 22"),
    ("firewall-flush", "sudo ufw --force enable"),
    ("kill-all", "kill -- -1"),
    ("kill-all", "kill -s KILL -1"),
    ("kill-all", "kill -HUP 1"),
    ("kill-all", "sudo /bin/kill -9 -1"),
    ("kill-all", "pkill -9 -u root"),
    ("kill-all", "pkill -uroot sleep"),
    ("kill-all", "pkill --user=root"),
    ("kill-all", "pkill -u www-data,root"),
    ("kill-all", "pkill -U 0"),
    ("kill-all", "killall5 -9"),
]
STAGE_EXTRA = [
    "systemctl stop 'digipaddock-*'", "systemctl stop '*'", "systemctl --now disable digipaddock-monitor.timer",
    "systemctl try-restart digipaddock-session-host.service", "service digipaddock-cabinet stop", "pkill kd-session-host",
    "killall -9 kd-cabinet",
    "pkill -u digi",
    "pkill -u digi-sysadmin",
]
SAFE_EXTRA = [
    ("kill -1 12345", "node-f"),  # signal 1 (HUP) to process 12345
    ("kill -s 1 12345", "node-f"),
    ("kill -9 12345 23456", "node-f"),
    ("pkill -u www-data php-fpm", "node-f"),
    ("iptables -D INPUT -j DROP", "node-f"),  # removing a rule is not a lockout
    ("iptables -I INPUT -s 198.51.100.0/24 -j REJECT", "node-f"),
    ("iptables -I INPUT -p tcp --dport 22 -s 203.0.113.10 -j ACCEPT", "node-f"),
    ("ufw delete deny from any", "node-f"),
    ("ufw allow from any to any port 443", "node-f"),
    ("ufw deny from any to any port 8080", "node-f"),  # closing a port other than 22 is not a self-lockout
    ("iptables -I INPUT -p tcp -m multiport --dports 8080,9000:9100 -j DROP", "node-f"),
    ("ufw status verbose", "node-f"),
    ("systemctl stop nginx", STAGE),
    ("systemctl status digipaddock-cabinet", STAGE),
    ("systemctl restart fail2ban", STAGE),
    ("pkill -f http.server", STAGE),
    ("cat /var/lib/digipaddock/monitor/state.json", "node-f"),
    ("cp /var/lib/digipaddock/monitor/baseline.json /tmp/", "node-f"),
]


@pytest.mark.parametrize("rule,command", LOCKOUT_EXTRA)
def test_10b_extra_lockouts_are_caught(rule, command):
    hit = denylist.check(command, "node-f")
    assert hit is not None and hit.name == rule, command


@pytest.mark.parametrize("command", STAGE_EXTRA)
def test_10b_stage_cage_processes(command):
    assert denylist.check(command, STAGE).name == "cage-write"
    assert denylist.check(command, "node-f") is None


@pytest.mark.parametrize("command,server", SAFE_EXTRA)
def test_10b_safe_twins_pass(command, server):
    assert denylist.check(command, server) is None


# review round 1: ufw without from, a negated port, digipaddock-* in the cage, firewalld and PREROUTING
REVIEW1_CAUGHT = [
    ("access-lockout", "ufw deny in on eth0", "node-f"),
    ("access-lockout", "ufw deny to any", "node-f"),
    ("access-lockout", "ufw deny proto tcp to any", "node-f"),
    ("access-lockout", "ufw deny in proto tcp to any", "node-f"),
    ("access-lockout", "ufw deny in on eth0 to any", "node-f"),
    ("access-lockout", "ufw deny out to any", "node-f"),
    ("access-lockout", "ufw insert 1 reject out 443", STAGE),
    ("access-lockout", "ufw default deny outgoing", STAGE),
    ("access-lockout", "ufw deny 1:65535/tcp", "node-f"),
    ("access-lockout", "ufw deny to any app OpenSSH", "node-f"),
    ("access-lockout", "iptables -I INPUT -p tcp ! --dport 8080 -j DROP", "node-f"),
    ("access-lockout", "iptables -I INPUT -p tcp --dport ! 8080 -j DROP", "node-f"),
    ("access-lockout", "iptables -I INPUT -p tcp -m multiport ! --dports 8080,9000 -j DROP", "node-f"),
    ("access-lockout", "iptables -t raw -I PREROUTING -j DROP", "node-f"),
    ("access-lockout", "iptables -t mangle -A PREROUTING -s 0.0.0.0/0 -j DROP", "node-f"),
    ("access-lockout", "iptables -t raw -I PREROUTING -s 198.51.100.9 -p tcp --dport 22 -j DROP", "node-f"),
    ("access-lockout", "firewall-cmd --panic-on", "node-g"),
    ("access-lockout", "firewall-cmd --set-default-zone=drop", "node-g"),
    ("access-lockout", "firewall-cmd --set-default-zone block", "node-g"),
    ("access-lockout", "firewall-cmd --permanent --remove-service=ssh", "node-g"),
    ("access-lockout", "firewall-cmd --remove-port=22/tcp", "node-g"),
    ("cage-write", "rm /etc/systemd/system/digipaddock-monitor.timer", STAGE),
    ("cage-write", "echo x > /etc/systemd/system/digipaddock-monitor.timer", STAGE),
    ("cage-write", "cp /tmp/o.conf /etc/systemd/system/digipaddock-monitor.timer.d/override.conf", STAGE),
    ("cage-write", "cp /tmp/x /usr/local/bin/kd-monitor", STAGE),
]
REVIEW1_SAFE = [
    ("ufw deny 8080", "node-f"),
    ("ufw deny 8080/tcp", "node-f"),
    ("ufw deny Nginx", "node-f"),
    ("ufw deny to any port 8080", "node-f"),
    ("ufw deny proto tcp to any port 8080", "node-f"),
    ("ufw insert 1 deny from 198.51.100.9", "node-f"),
    ("ufw delete deny out to any", "node-f"),
    ("ufw default allow outgoing", STAGE),
    ("iptables -A INPUT -p tcp --dport 8080 -j DROP", "node-f"),
    ("iptables -t nat -A PREROUTING -p tcp --dport 80 -j DNAT --to-destination 10.0.0.2:8080", "node-f"),
    ("iptables -t raw -I PREROUTING -s 198.51.100.9 -j DROP", "node-f"),
    ("firewall-cmd --list-all", "node-g"),
    ("firewall-cmd --get-default-zone", "node-g"),
    ("firewall-cmd --permanent --remove-port=8080/tcp", "node-g"),
    ("cat /etc/systemd/system/digipaddock-monitor.timer", STAGE),
    ("systemctl cat digipaddock-monitor.timer", STAGE),
]


@pytest.mark.parametrize("rule,command,server", REVIEW1_CAUGHT)
def test_10b_review1_caught(rule, command, server):
    hit = denylist.check(command, server)
    assert hit is not None and hit.name == rule, (command, hit and hit.name)


@pytest.mark.parametrize("command,server", REVIEW1_SAFE)
def test_10b_review1_safe_twins(command, server):
    assert denylist.check(command, server) is None


# the final wave: ufw deny log, firewalld through zones/a target/a hyphen, the symlinks of the cage's timers
FINAL_WAVE_CAUGHT = [
    ("access-lockout", "ufw deny log 1:65535/tcp", "node-f"),
    ("access-lockout", "ufw insert 1 deny log-all 1:65535/tcp", "node-f"),
    ("access-lockout", "ufw reject log 22", "node-f"),
    ("access-lockout", "firewall-cmd --zone=drop --change-interface=eth0", "node-g"),
    ("access-lockout", "firewall-cmd --permanent --zone block --add-interface eth0", "node-g"),
    ("access-lockout", "firewall-cmd --zone=drop --change-zone=eth0", "node-g"),
    ("access-lockout", "firewall-cmd --permanent --zone=public --set-target=DROP", "node-g"),
    ("access-lockout", "firewall-cmd --permanent --zone=public --set-target=%%REJECT%%", "node-g"),
    ("access-lockout", "firewall-cmd --remove-port=20-30/tcp", "node-g"),
    ("access-lockout", "firewall-cmd --permanent --remove-port=1-65535/udp", "node-g"),
    ("cage-write", "rm /etc/systemd/system/timers.target.wants/digipaddock-monitor.timer", STAGE),
    ("cage-write", "rm -f /etc/systemd/system/multi-user.target.wants/digipaddock-session-host.service", STAGE),
    ("cage-write", "ln -sf /dev/null /etc/systemd/system/timers.target.wants/digipaddock-usage.timer", STAGE),
]
FINAL_WAVE_SAFE = [
    ("ufw deny log 8080", "node-f"),
    ("ufw insert 1 deny log-all 8080/tcp", "node-f"),
    ("firewall-cmd --zone=public --change-interface=eth0", "node-g"),
    ("firewall-cmd --zone=drop --list-all", "node-g"),
    ("firewall-cmd --permanent --zone=public --set-target=default", "node-g"),
    ("firewall-cmd --permanent --zone=public --set-target=ACCEPT", "node-g"),
    ("firewall-cmd --remove-port=8000-8100/tcp", "node-g"),
    ("firewall-cmd --permanent --remove-service=my-app", "node-g"),
    ("rm /etc/systemd/system/timers.target.wants/certbot.timer", STAGE),
    ("ls -l /etc/systemd/system/timers.target.wants/digipaddock-monitor.timer", STAGE),
]


@pytest.mark.parametrize("rule,command,server", FINAL_WAVE_CAUGHT)
def test_final_wave_caught(rule, command, server):
    hit = denylist.check(command, server)
    assert hit is not None and hit.name == rule, (command, hit and hit.name)


@pytest.mark.parametrize("command,server", FINAL_WAVE_SAFE)
def test_final_wave_safe_twins(command, server):
    assert denylist.check(command, server) is None


# what an app step forms — the application's account, in its directory, a catalog tool or the flag helper
APP_COMMANDS = [
    "runuser -u www-data -- env -C /var/www/testapi.example.test ./venv/bin/python tools/partner_admin.py set-type"
    " --partner-id 6 --type sales --force",
    "runuser -u www-data -- env -C /var/www/testapi.example.test python3 -I /usr/local/libexec/agent-app-flag"
    " config.ini partner clients_enabled 1",
    "systemctl reload testapi-partner.example.test.service",
]


@pytest.mark.parametrize("command", APP_COMMANDS)
@pytest.mark.parametrize("server", [STAGE, "node-c"])
def test_denylist_lets_the_app_commands_through(command, server):
    assert denylist.check(command, server) is None
