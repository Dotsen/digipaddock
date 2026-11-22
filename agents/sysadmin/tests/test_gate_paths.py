import os
import posixpath

import pytest

from gate import agent_ro_gate as gate

ALLOWED = [
    "cat /var/log/syslog", "tail -n 100 /var/log/app/api.log", "tail -n +5 /var/log/nginx/error.log",
    "head -n 10 /etc/hosts", "cat /etc/nginx/sites-enabled/api.example.test", "cat /proc/meminfo",
    "cat /sys/class/net/eth0/statistics/rx_bytes",
    "cat /var/www/app.example.test/docs/18_infrastructure_map.md",
    "cat /opt/app/docs/server.md", "ls -la /etc/wireguard", "ls -lh /backups", "stat /backups/db.sql.gz",
    "du -sh /var/log", "du -h --max-depth=1 /var/www", "find /var/log -maxdepth 2 -name *.gz -mtime +7",
    "find /backups -maxdepth 1 -type f -newer /backups/marker", "ls -la //var//log", "cat /etc/resolv.conf",
    "stat -c %s /var/log/syslog",
]

DENIED = [
    "cat /etc/shadow", "cat /etc/gshadow", "cat /etc/shadow-",
    "cat /var/www/api.example.test/config.ini", "cat /var/www/site/.env", "cat /var/www/site/.env.production",
    "cat /opt/app/.env", "cat /opt/app/daemon-shop.env", "cat /etc/proxyd.env",
    "cat /etc/wireguard/wg0.conf", "cat /etc/wireguard/privatekey",
    "cat /etc/digipaddock-backup.conf", "cat /etc/acme-backup.conf", "cat /etc/mysql/debian.cnf",
    "cat /etc/letsencrypt/live/example.test/privkey.pem", "cat /etc/ssl/private/x.key",
    "cat /var/www/api.example.test/openvpn/auth.txt",
    "cat /proc/1/environ", "cat /proc/self/environ", "cat /proc/1/root/etc/shadow", "ls /proc/self/cwd",
    "cat /proc/1/task/1/environ", "cat /proc/self/task/1/root/etc/hosts",
    "cat /proc/1/cmdline", "head -n 1 /proc/self/cmdline", "tail -n 1 /proc/1/task/1/cmdline", "cat /proc/kcore",
    "cat /proc/kmsg", "grep x /proc/1/cmdline", "cat /proc/1/stack",
    "cat /root/.ssh/id_rsa", "ls /root", "cat /home/agent-ro/.my.cnf", "ls /home/agent-ro/.ssh",
    "cat /var/log/../../etc/shadow", "cat /var/log/../log/syslog", "ls -la /etc/../root",
    "cat etc/hosts", "cat", "ls", "cat /backups/db.sql.gz", "head /backups/x",
    "cat /tmp/x", "cat /var/lib/mysql/x", "du -sh /var/lib/mysql/deliveryalertprod",
    "tail -f /var/log/syslog", "tail -F /var/log/syslog", "tail --follow /var/log/syslog",
    "tail -n abc /var/log/syslog",
    "find /etc -name x", "find /etc -maxdepth 1 -delete", "find /var/log -maxdepth 1 -exec cat {} +",
    "find /var/log -maxdepth 1 -fprint /tmp/x", "find /var/log -maxdepth 1 -newer /etc/shadow",
    "find /tmp -maxdepth 1", "du --files0-from=/etc/shadow", "du -X /etc/shadow /var/log",
]


@pytest.mark.parametrize("raw", ALLOWED)
def test_allowed(raw):
    assert gate.check(raw).sudo is False


@pytest.mark.parametrize("raw", DENIED)
def test_denied(raw):
    with pytest.raises(gate.Deny):
        gate.check(raw)


def test_symlink_to_secret_is_denied(monkeypatch):
    links = {"/var/www/site/link": "/etc/shadow"}
    monkeypatch.setattr(gate, "realpath", lambda p: links.get(p, posixpath.normpath(p)))
    with pytest.raises(gate.Deny, match="запрещён"):
        gate.check("cat /var/www/site/link")


def test_symlink_into_system_files_is_allowed(monkeypatch):
    target = {"/etc/resolv.conf": "/run/systemd/resolve/stub-resolv.conf",
              "/sys/class/net/eth0/mtu": "/sys/devices/pci0000:00/0000:00:03.0/net/eth0/mtu"}
    monkeypatch.setattr(gate, "realpath", lambda p: target.get(p, posixpath.normpath(p)))
    assert gate.check("cat /etc/resolv.conf").argv == ["cat", "/etc/resolv.conf"]
    assert gate.check("cat /sys/class/net/eth0/mtu").sudo is False


def test_symlink_out_of_roots_is_denied(monkeypatch):
    monkeypatch.setattr(gate, "realpath", lambda p: "/srv/data/dump.sql" if p == "/var/www/site/dump" else p)
    with pytest.raises(gate.Deny, match="за пределы"):
        gate.check("cat /var/www/site/dump")


@pytest.mark.skipif(os.name != "posix", reason="настоящие симлинки — POSIX")
def test_real_symlinks(tmp_path, monkeypatch):
    root = tmp_path / "www"
    root.mkdir()
    (tmp_path / "config.ini").write_text("x")
    (tmp_path / "other.txt").write_text("x")
    (root / "plain.txt").write_text("x")
    (root / "to_secret").symlink_to(tmp_path / "config.ini")
    (root / "to_other").symlink_to(tmp_path / "other.txt")
    monkeypatch.setattr(gate, "ROOTS", ((str(root), True),))
    assert gate.check(f"cat {root}/plain.txt").argv[0] == "cat"
    with pytest.raises(gate.Deny, match="запрещён"):
        gate.check(f"cat {root}/to_secret")
    with pytest.raises(gate.Deny, match="за пределы"):
        gate.check(f"cat {root}/to_other")


def test_backups_are_list_only():
    with pytest.raises(gate.Deny, match="листинг"):
        gate.check("cat /backups/db.sql.gz")
    assert gate.check("ls -la /backups").argv == ["ls", "-la", "/backups"]


@pytest.mark.parametrize("target", ["/backups/db.sql.gz", "/backups/prod-2026.sql"])
@pytest.mark.parametrize("raw", ["cat /var/www/site/dump", "head -n 100 /var/www/site/dump",
                                 "tail -n 5 /var/www/site/dump"])
def test_symlink_into_list_only_root_is_denied_for_content(monkeypatch, raw, target):
    monkeypatch.setattr(gate, "realpath", lambda p: target if p == "/var/www/site/dump" else p)
    with pytest.raises(gate.Deny, match="за пределы"):
        gate.check(raw)


def test_symlink_into_list_only_root_is_fine_for_listing(monkeypatch):
    monkeypatch.setattr(gate, "realpath", lambda p: "/backups/x" if p == "/var/www/site/dump" else p)
    assert gate.check("ls -la /var/www/site/dump").sudo is False


def test_reboot_required_is_the_only_file_of_run(monkeypatch):
    """Without needrestart, the monitor reads /run/reboot-required; the rest of /run (sockets, /run/user, secrets) is closed."""
    assert gate.check("cat /run/reboot-required").argv == ["cat", "/run/reboot-required"]
    for raw in ("cat /run/reboot-required.pkgs", "ls /run", "cat /run/user/0/x", "cat /run/reboot-required/../x",
                "cat /var/run/reboot-required"):
        with pytest.raises(gate.Deny):
            gate.check(raw)
    for target in ("/etc/shadow", "/run/user/0/secret"):  # replaced with a symlink
        monkeypatch.setattr(gate, "realpath", lambda p: target if p == "/run/reboot-required" else p)
        with pytest.raises(gate.Deny):
            gate.check("cat /run/reboot-required")


DENIED_MORE = [
    "cat /opt/app/docs/shop/vpn/node-1.conf", "cat /opt/app/vpn/x.conf",
    "cat /var/www/s/config.ini.bak", "cat /var/www/s/config.ini~", "cat /var/www/s/config.ini.old",
    "cat /var/www/s/.config.ini.swp", "cat /var/www/s/.env~", "cat /var/www/s/.env-production",
    "cat /var/www/s/.envrc", "cat /etc/shadow.bak", "cat /etc/gshadow.old",
    "cat /var/www/s/.git/config", "cat /var/www/s/.git-credentials",
    "cat /etc/ssh/ssh_host_ed25519_key", "cat /etc/ssh/ssh_host_rsa_key",
    "cat /etc/letsencrypt/accounts/x/directory/y/private_key.json", "cat /etc/nginx/.htpasswd",
    "find /var/log -maxdepth ٣",
]


@pytest.mark.parametrize("raw", DENIED_MORE)
def test_denied_more(raw):
    with pytest.raises(gate.Deny):
        gate.check(raw)


def test_other_git_files_stay_readable():
    assert gate.check("cat /var/www/s/.git/HEAD").sudo is False


DENIED_SECRET_NAMES = [
    "cat /etc/nginx/.htpasswd.bak", "cat /etc/nginx/.htpasswd-prod", "cat /etc/nginx/htpasswd",
    "cat /etc/nginx/htpasswd.old",
    "cat /var/www/s/config.ini-prod", "cat /var/www/s/config.ini_bak", "cat /var/www/s/#config.ini#",
    "cat /var/www/s/config.ini.example",
    "cat /var/www/s/.env_prod", "cat /var/www/s/.env_",
    "cat /var/www/s/.git/modules/sub/config", "cat /var/www/s/.git/modules/a/b/modules/c/config",
    "cat /var/www/s/.git/config.worktree",
]


@pytest.mark.parametrize("raw", DENIED_SECRET_NAMES)
def test_denied_secret_name_variants(raw):
    with pytest.raises(gate.Deny):
        gate.check(raw)


@pytest.mark.parametrize("raw", ["cat /var/www/x/.git/HEAD", "cat /opt/app/README.md", "cat /var/www/s/.git/modules/sub/HEAD"])
def test_ordinary_files_stay_readable(raw):
    assert gate.check(raw).sudo is False


EXTRA = '{"schema_version": 1, "forbidden": ["(^|/)app_keys\\\\.json$", "(^|/)api_log(/|$)"]}'


def test_a_deployments_own_secret_paths_come_from_its_root_owned_file(tmp_path, monkeypatch):
    """Review 04.11.2026: the public list holds no deployment's paths; target_setup.sh --forbidden installs them."""
    extra = tmp_path / "forbidden.json"
    assert gate.extra_forbidden(str(extra)) == ()  # no file: the public list only
    extra.write_text(EXTRA)
    os.chmod(extra, 0o644)
    monkeypatch.setattr(gate.os, "lstat", lambda p, _real=os.lstat: _as_root(_real(p)))
    patterns = gate.extra_forbidden(str(extra))
    monkeypatch.setattr(gate, "_extra", patterns)
    for raw in ("cat /var/www/site/app_keys.json", "ls /var/www/site/logs/api_log"):
        with pytest.raises(gate.Deny, match="путь запрещён"):
            gate.check(raw)
    assert gate.check("cat /var/www/site/README.md")


def _as_root(st, uid=0):
    return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, uid, uid, st.st_size,
                           int(st.st_atime), int(st.st_mtime), int(st.st_ctime)))


@pytest.mark.parametrize("problem", ["writable", "not-root", "garbage", "bad-regex", "link"])
def test_an_untrusted_deployment_file_refuses_every_path(tmp_path, monkeypatch, problem):
    extra = tmp_path / "forbidden.json"
    extra.write_text({"garbage": "{not json", "bad-regex": '{"schema_version": 1, "forbidden": ["("]}'}.get(problem, EXTRA))
    os.chmod(extra, 0o666 if problem == "writable" else 0o644)
    if problem == "link":
        (tmp_path / "real.json").write_text(EXTRA)
        extra.unlink()
        extra.symlink_to(tmp_path / "real.json")
    real, uid = os.lstat, 1000 if problem == "not-root" else 0
    monkeypatch.setattr(gate.os, "lstat", lambda p: _as_root(real(p), uid))
    with pytest.raises(gate.Deny, match="список запретов внедрения"):
        gate.extra_forbidden(str(extra))
    monkeypatch.setattr(gate, "EXTRA_FORBIDDEN", str(extra))
    monkeypatch.setattr(gate, "_extra", None)
    with pytest.raises(gate.Deny, match="список запретов внедрения"):
        gate.check("cat /var/www/site/README.md")
