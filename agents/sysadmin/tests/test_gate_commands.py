import hashlib
import shlex

import pytest

from gate import agent_ro_gate as gate

ALLOWED = [
    "uptime", "uptime -p", "free -m", "uname -a", "who -b", "last -n 20", "last -20", "lsblk", "lsblk -f /dev/sda",
    "df -h", "df -h /var", "ps aux", "ps -eo pid,%mem,cmd --sort=-%mem", "ps -ef", "ps -e", "ps auxf", "ps -o user=", "ps -C node", "ps -C node -o pid=", "ps -u postgres",
    "ps -p 1 -o lstart=", "ps -U nginx -o pid,etime", "ps --user postgres", "top -bn1", "top -b -n 1 -o %MEM",
    "ss -tln", "ss -s", "ss -tan state established", "ip addr", "ip -br link", "ip -6 route show table all",
    "ip rule list", "ip route get 198.51.100.3", "ip -s link show eth0",
    "systemctl status nginx --no-pager", "systemctl list-timers --all", "systemctl show -p ActiveState digipaddock",
    "systemctl is-failed testapi.example.test.service", "systemctl list-units --failed", "systemctl cat nginx",
    "journalctl -u nginx -n 200 --no-pager", 'journalctl --since "1 hour ago" -p err', "journalctl -xeu ssh",
    "journalctl -b -1 -k", "journalctl _SYSTEMD_UNIT=nginx.service -n 5", "journalctl --disk-usage",
    "journalctl -n all -u ssh", "crontab -l", "apt list --upgradable", "needs-restarting -r",
    "dpkg --audit",  # a package-state read that used to go to ssh-exec
]

DENIED = [
    "", "   ", "uptime; id", "uptime && id", "uptime | head", "echo $(id)", "echo `id`", "uptime\nid",
    "touch /tmp/x", "bash -c id", "/usr/bin/uptime", "sh", "python3 -c 1",
    "ps aux > /tmp/x", "free -s 1", "top", "top -n 1",
    "ps -uroot e", "ps -ppid1 e", "ps -Ctest e", "ps --pid=1 e", "ps e", "ps auxe", "ps eww", "ps -auxe", "ps -A e", "ps axe", "ps -o user", "ps -o etime", "ps -C node e", "ps -u root axe",
    "ss -K dst 198.51.100.2", "ss -D /tmp/dump", "ss -F /etc/shadow", "ss -N other",
    "ip route add default via 198.51.100.3", "ip link set eth0 down", "ip -batch /tmp/x", "ip xfrm state",
    "ip netns exec x id", "ip -n ns addr",
    "systemctl restart nginx", "systemctl stop nginx", "systemctl -H host status nginx", "systemctl --root=/ status",
    "systemctl", "systemctl status ../x",
    "journalctl --vacuum-size=1M", "journalctl --vacuum-time=1d", "journalctl --rotate", "journalctl --flush",
    "journalctl --relinquish-var", "journalctl -f", "journalctl --follow", "journalctl --cursor-file=/tmp/c",
    "journalctl -D /tmp", "journalctl --file=/etc/shadow", "journalctl -M other",
    "journalctl -n --vacuum-time=1d", "journalctl -n --rotate", "journalctl -u nginx -n --flush",
    "journalctl -u --rotate",
    "crontab -e", "crontab -r", "crontab -l -u www-data", "apt upgrade", "apt list --upgradable -o x",
    "last -f /etc/shadow", "who /etc/shadow", "uname -a extra",
    "wg show wg0 private-key", "wg showconf wg0", "iptables -F", "docker exec x sh", "dmesg -c",
    "agent-sec-snapshot --help", "agent-sec-snapshot x", "agent-hindsight-usage --days 3",
    "agent-hindsight-usage /tmp", "ufw status", "ufw enable", "ufw status numbered",
    "fail2ban-client status", "fail2ban-client set sshd unbanip 198.51.100.2", "dnf update", "dnf -q check-update",
    "dnf -q check-update --security", "dnf -C check-update --security", "dnf needs-restarting", "dnf needs-restarting -s",
    # only the exact read forms of dpkg; anything that installs, configures or forces is refused
    "dpkg", "dpkg -i /tmp/x.deb", "dpkg --configure -a", "dpkg --audit extra", "dpkg --audit --force-all",
    "dpkg --purge x", "dpkg -C",
]

ROOT_FORMS = [
    "wg show", "iptables -S", "ip6tables -t mangle -S", "crontab -l -u root", "ss -tlnpu", "docker ps",
    "nft list ruleset", "dmesg -T", "needrestart -b -r l", "certbot certificates", "du -sh /var/lib/mysql",
    "agent-sec-snapshot", "ufw status verbose", "fail2ban-client status sshd", "dnf -q -C check-update --security",
    "dnf needs-restarting -r", "agent-hindsight-usage",
    "ls -la /etc/sudoers.d",  # names, owners and modes only — /etc/sudoers.d is root 0750
]


@pytest.mark.parametrize("raw", ALLOWED)
def test_allowed_without_root(raw):
    plan = gate.check(raw)
    assert plan.argv[0] == raw.split()[0] and plan.sudo is False
    assert plan.argv == shlex.split(raw)


@pytest.mark.parametrize("raw", DENIED)
def test_denied(raw):
    with pytest.raises(gate.Deny):
        gate.check(raw)


@pytest.mark.parametrize("raw", ROOT_FORMS)
def test_exact_root_forms_go_through_sudo(raw):
    assert gate.check(raw) == gate.Plan(raw.split(), True)


def test_pipes_are_explained():
    with pytest.raises(gate.Deny, match="метасимвол"):
        gate.check("journalctl -n 5 | grep x")
    with pytest.raises(gate.Deny, match="метасимвол"):
        gate.check("uptime; id")


def test_parse_opts_clusters_and_values():
    opts, positional = gate.parse_opts(["-xeu", "nginx", "-n50", "--since", "1 hour ago", "x"], "xe", "un", (), ("--since",))
    assert opts == [("-x", None), ("-e", None), ("-u", "nginx"), ("-n", "50"), ("--since", "1 hour ago")]
    assert positional == ["x"]


def test_parse_opts_rejects_string_long_lists():
    for kwargs in ({"long_flags": "--all"}, {"long_valued": "--all"}, {"long_optional": "--all"}):
        with pytest.raises(TypeError):
            gate.parse_opts(["--a"], "", "", **kwargs)


@pytest.mark.parametrize("raw", ["journalctl -n ٣", "df /étc", "journalctl -u нгинкс"])
def test_non_ascii_digits_and_letters_are_denied(raw):
    with pytest.raises(gate.Deny):
        gate.check(raw)


def test_too_long_command_is_denied():
    with pytest.raises(gate.Deny, match="длинн") as exc:
        gate.check("uptime " + "a" * 5000)
    assert "aaaa" not in str(exc.value)


def test_top_w_cannot_raise_iterations():
    for raw in ("top -bn1 -w -n5", "top -bn1 -n5", "top -n5 -bn1"):
        with pytest.raises(gate.Deny):
            gate.check(raw)
    assert gate.check("top -bn1 -w").sudo is False


@pytest.mark.parametrize("raw", [
    "systemctl show -P Environment app.service", "systemctl show -PEnvironment app.service",
    "systemctl show -p Environment --value app.service", "systemctl show --value app.service",
    "systemctl show --property=Environment --value app.service", "systemctl show -lP ExecStart app.service",
])
def test_systemctl_value_without_its_name_is_refused(raw):
    # review 26.11.2026: a value printed without "Environment=" escapes the mask of a unit's environment
    with pytest.raises(gate.Deny, match="--value"):
        gate.check(raw)


@pytest.mark.parametrize("raw", [
    "systemctl show app.service", "systemctl show -p Environment app.service",
    "systemctl show --property=Environment,ExecStart app.service", "systemctl show -p ActiveState digipaddock",
    "systemctl cat app.service",
])
def test_systemctl_show_with_property_names_is_allowed(raw):
    assert gate.check(raw).argv == shlex.split(raw)


@pytest.mark.parametrize("raw", [
    "ps -eo pid,args:40", "ps -o args:20 -p 1", "ps axo cmd:30", "ps -O args:10", "ps -eo pid --cols 50",
    "ps aux --width=60", "ps -eo args --columns 30", "ps -eo pid,command:5",
])
def test_ps_column_width_is_refused(raw):
    # review 26.11.2026: a width the agent picks cuts "https://user:pass@host" before "@" — no mask sees a password
    with pytest.raises(gate.Deny, match="ширин"):
        gate.check(raw)
    assert "s3cr3tpw" in gate.redact(b"curl https://admin:s3cr3tpw\nnext line\n").decode()  # such a cut line


@pytest.mark.parametrize("raw", ["ps -eo pid,args", "ps auxww", "ps -eo pid,user,comm,rss,pcpu --sort=-rss"])
def test_ps_without_a_width_is_allowed(raw):
    assert gate.check(raw).argv == shlex.split(raw)


def test_journalctl_grep_allows_unicode_but_not_control_chars():
    assert gate.check("journalctl -u app -g ошибка").sudo is False
    with pytest.raises(gate.Deny):
        gate.check("journalctl -u нгинкс")
    with pytest.raises(gate.Deny):
        gate.check("journalctl -g \x01")


def _which(name, path=None):
    return name if name.startswith("/") else "/usr/bin/" + name


HEX = "0123456789abcdef" * 4


def test_sudoers_resolves_snapshot_by_absolute_path_with_empty_args():
    seen, hashed = [], []

    def which(name, path=None):
        seen.append(name)
        return _which(name)

    def digest(path):
        hashed.append(path)
        return HEX

    lines = gate.sudoers_lines(which, digest=digest)
    assert 'agent-ro ALL=(root) NOPASSWD: sha256:%s /usr/local/bin/agent-sec-snapshot ""' % HEX in lines
    assert 'agent-ro ALL=(root) NOPASSWD: sha256:%s /usr/local/bin/agent-hindsight-usage ""' % HEX in lines
    assert "agent-ro ALL=(root) NOPASSWD: /usr/bin/ufw status verbose" in lines  # ordinary ones — without a digest
    assert "agent-ro ALL=(root) NOPASSWD: /usr/bin/dnf -q -C check-update --security" in lines
    assert "/usr/local/bin/agent-sec-snapshot" in seen and "agent-sec-snapshot" not in seen
    assert "agent-hindsight-usage" not in seen
    # a digest — only for the binaries from SUDO_PATHS
    assert hashed == ["/usr/local/bin/agent-sec-snapshot", "/usr/local/bin/agent-hindsight-usage"]
    assert not any(ln.endswith(' ""') and "sha256:" not in ln for ln in lines)
    assert sum("sha256:" in ln for ln in lines) == 2


def test_sudoers_skips_snapshot_when_binary_absent():
    which = lambda name, path=None: None if name.startswith("/usr/local/") else _which(name)
    lines = gate.sudoers_lines(which, digest=lambda p: HEX)
    assert not any("agent-sec-snapshot" in ln or "agent-hindsight-usage" in ln for ln in lines)


@pytest.mark.parametrize("bad", [None, "", "zz" * 32, HEX[:-1]])
def test_sudoers_skips_snapshot_when_digest_unavailable(bad):
    lines = gate.sudoers_lines(_which, digest=lambda p: bad)
    assert not any("agent-sec-snapshot" in ln for ln in lines)
    assert "agent-ro ALL=(root) NOPASSWD: /usr/bin/ufw status verbose" in lines


def test_digest_is_sha256_of_file_and_none_when_unreadable(tmp_path):
    p = tmp_path / "bin"
    p.write_bytes(b"x" * 200000)  # more than one read chunk
    assert gate._digest(str(p)) == hashlib.sha256(b"x" * 200000).hexdigest()
    assert gate._digest(str(tmp_path / "missing")) is None


def test_materialize_snapshot_uses_absolute_path_under_sudo():
    assert gate.materialize(gate.check("agent-sec-snapshot"), _which) == ["/usr/bin/sudo", "-n", "/usr/local/bin/agent-sec-snapshot"]
    assert gate.materialize(gate.check("agent-hindsight-usage"), _which) == [
        "/usr/bin/sudo", "-n", "/usr/local/bin/agent-hindsight-usage"]


def test_sudoers_d_listing_is_the_only_root_ls(tmp_path):
    # one exact metadata form under sudo; a file inside or another listing stays an ordinary agent-ro ls
    assert gate.check("ls -la /etc/sudoers.d/agent-ro").sudo is False
    assert gate.check("ls -l /etc/sudoers.d").sudo is False
    with pytest.raises(gate.Deny):
        gate.check("cat /etc/sudoers.d/agent-ro x;")
    lines = gate.sudoers_lines(lambda name, path=None: "/usr/bin/" + name, digest=lambda p: "0" * 64)
    assert "agent-ro ALL=(root) NOPASSWD: /usr/bin/ls -la /etc/sudoers.d" in lines
