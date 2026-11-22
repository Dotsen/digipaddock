"""Recognized critical commands are refused in a script step at proposal with a hint to
propose the exact one."""

import pytest

from sysadmin import critical


@pytest.mark.parametrize(
    "command, group",
    [
        ("iptables -I INPUT -s 198.51.100.2 -j DROP", "сетевой доступ"),
        ("sudo nft add rule inet filter input drop", "сетевой доступ"),
        ("ip route replace default via 10.0.0.1", "сетевой доступ"),
        ("systemctl restart wg-quick@wg0", "сетевой доступ"),
        ("mysql -e 'DELETE FROM orders WHERE id=1' appdb", "производственная БД"),
        ("mysql appdb < dump.sql", "производственная БД"),
        ("psql -c 'alter table x add column y int'", "производственная БД"),
        ("rm -rf /var/lib/mysql-old", "удаление данных"),
        ("rm -fr build", "удаление данных"),
        ("rm /srv/backups/2026-10-01.sql.gz", "удаление данных"),
        ("find /var/log -name '*.gz' -delete", "удаление данных"),
        ("dd if=/dev/zero of=/dev/sdb bs=1M", "удаление данных"),
        ("mkfs.ext4 /dev/sdb1", "диски и файловые системы"),
        ("umount /mnt/data", "диски и файловые системы"),
        ("reboot", "перезагрузка или остановка"),
        ("shutdown -r +5", "перезагрузка или остановка"),
        ("systemctl stop mysql", "перезагрузка или остановка"),
        ("systemctl disable --now nginx.service", "перезагрузка или остановка"),
        ("cd /opt/app && docker compose down", "перезагрузка или остановка"),
        ("true; reboot", "перезагрузка или остановка"),
    ],
)
def test_recognized_critical(command, group):
    assert critical.classify(command) == group


@pytest.mark.parametrize(
    "command",
    [
        "uptime",
        "printf '%s\\n' 'Unattended-Upgrade::Automatic-Reboot \"true\";' > /etc/apt/apt.conf.d/52app-auto-updates",
        "mysql -e 'SELECT count(*) FROM orders' appdb",
        "rm /tmp/kd-x.txt",
        "systemctl restart nginx.service",
        "systemctl status mysql",
        "apt-get -q update",
        "ip addr show",
        "docker ps",
        "journalctl -u ssh --since today",
    ],
)
def test_ordinary_commands_pass(command):
    assert critical.classify(command) is None


@pytest.mark.parametrize(
    "command, group",
    [
        ("bash -c 'sleep 1; /sbin/reboot'", "перезагрузка или остановка"),
        (
            "sudo -n bash -c 'systemctl --now mask ssh.service'",
            "перезагрузка или остановка",
        ),
        ("service mysql stop", "перезагрузка или остановка"),
        ("docker-compose -f x.yml down", "перезагрузка или остановка"),
        ("docker container rm -f web", "перезагрузка или остановка"),
        ("init 6", "перезагрузка или остановка"),
        ("ip a add 10.0.0.2/24 dev eth0", "сетевой доступ"),
        ("route del default", "сетевой доступ"),
        ("netplan apply", "сетевой доступ"),
        ("systemctl reload nftables", "сетевой доступ"),
        ("redis-cli flushall", "производственная БД"),
        ("mysql -e 'set global max_connections=500'", "производственная БД"),
        ("ls /srv | xargs rm -r", "удаление данных"),
        ("find /srv/old -type f -exec rm {} +", "удаление данных"),
        ("truncate -s 0 /var/log/nginx/access.log", "удаление данных"),
        ("rm --recursive old", "удаление данных"),
        ("rm /tmp/../var/lib/x", "удаление данных"),
        ("docker volume prune -f", "удаление данных"),
        ("mount -o rw,remount /", "диски и файловые системы"),
        ("lvremove -y vg0/old", "диски и файловые системы"),
    ],
)
def test_wrapped_and_other_forms(command, group):
    assert critical.classify(command) == group


@pytest.mark.parametrize(
    "command",
    [
        "truncate -s 0 /tmp/kd-x.log",
        "ip -br link show",
        "ip route get 198.51.100.3",
        "systemctl restart mysql",
        "systemctl status wg-quick@wg0",
        "docker compose ps",
        "find /var/log -name '*.gz' -mtime +30 -print",
        "mount | grep data",
        "rm -f /tmp/kd-a /tmp/kd-b",
    ],
)
def test_ordinary_neighbours_pass(command):
    assert critical.classify(command) is None


@pytest.mark.parametrize(
    "command",
    [
        "iptables -S",
        "iptables -t nat -S",
        "iptables -t mangle -L -n -v",
        "iptables -nvL INPUT --line-numbers",
        "iptables --list-rules f2b-sshd",
        "ip6tables -S 2>/dev/null",
        "sudo iptables -S | grep 22",
        "iptables-save",
        "iptables-save -t nat | grep DNAT",
        "nft list ruleset",
        "nft -a list table inet filter",
        "wg",
        "wg show",
        "wg show all latest-handshakes",
        "ufw status verbose",
        "ufw status numbered",
        "ufw app list",
        "firewall-cmd --list-all",
        "firewall-cmd --zone=public --list-ports",
        "firewall-cmd --permanent --zone public --list-all",
        "firewall-cmd --state",
        "firewall-cmd --get-active-zones",
        "firewall-cmd --query-port=443/tcp",
    ],
)
def test_firewall_reads_pass(command):
    assert critical.classify(command) is None


@pytest.mark.parametrize(
    "command",
    [
        "iptables -N kd",
        "iptables -X",
        "iptables -nL -Z",
        "iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE",
        "iptables -L $CHAIN",
        "iptables -S -- -F",
        "iptables -L $(echo -Z)",
        "echo -Z | xargs iptables -L",
        "iptables-save > /etc/iptables/rules.v4",
        "iptables-restore < /tmp/rules",
        "nft list ruleset ';' flush ruleset",
        "nft list ruleset \\; delete table inet filter",
        "nft -f /tmp/rules.nft",
        "wg set wg0 peer abc= remove",
        "wg show wg0 private-key",
        "wg-quick down wg0",
        "ufw allow 443/tcp",
        "ufw --force status",
        "firewall-cmd --add-port=443/tcp",
        "firewall-cmd --zone public --remove-service=http",
        "firewall-cmd --reload",
        "firewall-cmd --direct --passthrough ipv4 -A INPUT -j DROP",
    ],
)
def test_firewall_changes_stay_critical(command):
    assert critical.classify(command) == "сетевой доступ"


@pytest.mark.parametrize(
    "command",
    [
        # the output of a read redirected into the network configuration
        "nft list ruleset > /etc/nftables.conf",
        "wg show > /etc/wireguard/wg0.conf",
        "iptables -S >/etc/iptables/rules.v4",
        "ufw status >> /etc/ufw/user.rules",
        "firewall-cmd --list-all 1>/etc/firewalld/zones/public.xml",
        "nft list ruleset > $F",
        "cd /etc/iptables && iptables-save > rules.v4",
        # the same write without a redirect
        "nft list ruleset | tee /etc/nftables.conf",
        "cp /tmp/wg0.conf /etc/wireguard/wg0.conf",
        "sed -i s/10.0/10.1/ /etc/network/interfaces",
        "echo 'network: {}' > /etc/netplan/50-cloud-init.yaml",
        "install -m 600 /tmp/x /etc/NetworkManager/system-connections/eth0.nmconnection",
        "truncate -s 0 /etc/sysconfig/iptables",
        # the units of SSH and network access, their drop-ins and enabling links
        "nft list ruleset > /etc/systemd/system/ssh.socket",
        "echo '[Socket]' > /etc/systemd/system/ssh.socket.d/override.conf",
        "printf x | tee /etc/systemd/system/sshd.service.d/port.conf",
        "cp /tmp/digipaddock-ssh.socket /lib/systemd/system/ssh.socket",
        "cp /tmp/ssh.socket /etc/systemd/system/",
        "sed -i s/22/2222/ /usr/lib/systemd/system/ssh.socket",
        "ln -sf /dev/null /etc/systemd/system/ssh.service",
        "rm /etc/systemd/system/sockets.target.wants/ssh.socket",
        "echo x > /run/systemd/system/systemd-networkd.service.d/x.conf",
        "echo x > /etc/systemd/system/NetworkManager.service.d/x.conf",
        "cd /etc/systemd/system && echo x > wg-quick@wg0.service.d/x.conf",
        "echo x > /etc/systemd/system/multi-user.target.wants/nftables.service",
        "echo 'Port 2222' >> /etc/ssh/sshd_config",
        "echo x > /etc/ssh/sshd_config.d/50-kd.conf",
        "echo 'sshd: ALL' >> /etc/hosts.deny",
        "systemctl edit ssh.socket",
        "systemctl revert ssh",
        "systemctl set-property sshd.service CPUQuota=1%",
        "systemctl enable --now ssh.socket",
        # fourth review: link and enable take a unit file's path; an option's value is not the verb
        "systemctl link /var/tmp/ssh.socket",
        "systemctl enable /var/tmp/ssh.socket",
        "systemctl --root / link /var/tmp/ssh.socket",
        "systemctl --root=/ enable --now /home/digi/sshd.service",
        "systemctl -M .host link /var/tmp/ssh.socket",
        "systemctl link /var/tmp/wg-quick@wg1.service",
    ],
)
def test_a_write_into_the_network_configuration_is_critical(command):
    assert critical.classify(command) == "сетевой доступ"


@pytest.mark.parametrize(
    "command",
    [
        "iptables -S 2>&1 | grep 22",
        "wg show >/dev/null 2>&1",
        "nft list ruleset > /tmp/kd-rules.txt",
        "iptables-save > /tmp/kd-rules.v4",
        "cat /etc/wireguard/wg0.conf | grep -c Peer",
        "cp /etc/nftables.conf /tmp/digipaddock-nftables.conf",
        "cat /etc/systemd/system/ssh.socket.d/override.conf",
        "systemctl cat ssh.socket",
        "systemctl restart ssh",
        "systemctl status /var/tmp/ssh.socket",
        "systemctl link /var/tmp/digipaddock-test.service",
        "systemctl --root / status ssh",
        "cp /lib/systemd/system/ssh.socket /tmp/digipaddock-ssh.socket",
        "echo x > /etc/systemd/system/digipaddock-test.service",
        "grep Port /etc/ssh/sshd_config",
    ],
)
def test_reading_the_network_configuration_is_ordinary(command):
    assert critical.classify(command) is None


@pytest.mark.parametrize(
    "command",
    ["systemctl --job-mode replace stop nginx", "systemctl -p MainPID kill mysql", "systemctl --root / disable nginx.service"],
)
def test_an_option_value_does_not_hide_a_stop(command):
    assert critical.classify(command) == "перезагрузка или остановка"


# the deployment's own site units are critical to stop; their patterns are the catalog's critical_units
# (overlay data), the code keeps only generic ones.
@pytest.mark.parametrize(
    "command",
    ["systemctl stop testapi.example.test.service", "systemctl disable --now app-consumer.service"],
)
def test_catalog_site_units_are_critical_to_stop(command):
    assert critical.classify(command) == "перезагрузка или остановка"


def test_unreadable_catalog_makes_every_unit_stop_critical(monkeypatch, tmp_path):
    from kd import admin_operations

    monkeypatch.setattr(admin_operations, "CATALOG_PATH", tmp_path / "missing.json")
    assert critical.classify("systemctl stop foo.service") == "перезагрузка или остановка"
    assert critical.classify("systemctl restart foo.service") is None


def test_no_deployment_literals_in_critical():
    from pathlib import Path

    text = (Path(critical.__file__)).read_text(encoding="utf-8")
    assert "scraper-" not in text and "token-api" not in text
