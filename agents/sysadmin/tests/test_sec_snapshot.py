import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from gate import agent_sec_snapshot as snap

SNAPSHOT = Path(snap.__file__)
VERMIN = "import sys; from vermin.main import main; sys.argv[0] = 'vermin'; main()"
PASSWD = ("root:x:0:0:root:/root:/bin/bash\ndaemon:x:1:1::/usr/sbin:/usr/sbin/nologin\n"
          "agent-ro:x:998:998::/home/agent-ro:/bin/sh\nubuntu:x:1000:1000::/home/ubuntu:/bin/bash\n"
          "git:x:999:996::/var/lib/git:/bin/bash\nnobody:x:65534:65534::/nonexistent:/usr/sbin/nologin\n"
          "dyn:x:61500:61500::/:/usr/sbin/nologin\n")
GROUP = "root:x:0:\nsudo:x:27:ubuntu,deploy\ndocker:x:999:\nusers:x:100:ubuntu\n"
SSHD_T = ("port 22\npermitrootlogin without-password\npasswordauthentication no\npubkeyauthentication yes\n"
          "authorizedkeysfile .ssh/authorized_keys .ssh/authorized_keys2\nallowusers root\nallowusers deploy\n"
          "kbdinteractiveauthentication no\npermitemptypasswords no\nauthorizedkeyscommand none\n"
          "trustedusercakeys none\nusepam yes\n")
UFW = ("Status: active\nLogging: on (low)\nDefault: deny (incoming), allow (outgoing), disabled (routed)\n"
       "New profiles: skip\n\nTo                         Action      From\n--                         ------      ----\n"
       "22/tcp                     ALLOW IN    Anywhere\n80,443/tcp                 ALLOW IN    Anywhere\n"
       "22/tcp (v6)                ALLOW IN    Anywhere (v6)\n")


def fake_run(table):
    """table: {argv[0]: (rc, stdout)}; like subprocess.run(…, stdout=PIPE)."""
    def run(argv, **kw):
        rc, out = table.get(argv[0], (127, ""))
        return subprocess.CompletedProcess(argv, rc, out.encode(), b"")
    return run


OK_RUN = {"sshd": (0, SSHD_T), "ufw": (0, UFW), "systemctl": (0, "active\n")}


def which_all(name, path=None):
    return "/usr/sbin/" + name


def which_none(name, path=None):
    return None


@pytest.fixture
def tree(tmp_path):
    def w(rel, text=""):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p
    w("etc/passwd", PASSWD)
    w("etc/group", GROUP)
    w("root/.ssh/authorized_keys", "ssh-ed25519 AAA a\nssh-ed25519 BBB b\n")
    w("home/ubuntu/.ssh/authorized_keys", "ssh-ed25519 CCC c\n")
    w("var/lib/git/.ssh/authorized_keys", "ssh-ed25519 DDD d\n")
    (tmp_path / "home/agent-ro").mkdir(parents=True)
    w("etc/sudoers", "root ALL=(ALL) ALL\n")
    w("etc/sudoers.d/agent-ro", "agent-ro ALL=(root) NOPASSWD: /usr/bin/wg show\n")
    w("etc/shadow", "root:*:1::::::\n")
    w("etc/ssh/sshd_config", "PermitRootLogin prohibit-password\n")
    w("etc/ssh/sshd_config.d/00-hardening.conf", "PasswordAuthentication no\n")
    w("etc/crontab", "# m h\n")
    w("etc/cron.d/backup", "0 3 * * * root /usr/local/bin/backup\n")
    w("etc/cron.daily/logrotate", "#!/bin/sh\n/usr/sbin/logrotate /etc/logrotate.conf\n")
    w("var/spool/cron/crontabs/root", "# (/tmp/crontab.abc installed on Mon Oct  5 10:00:00 2026)\n@reboot /bin/true\n")
    w("etc/systemd/system/digipaddock-monitor.service", "[Unit]\n")
    w("etc/systemd/system/multi-user.target.wants/nginx.service")
    w("etc/systemd/system/multi-user.target.wants/fail2ban.service")
    w("etc/systemd/system/nginx.service.d/override.conf", "[Service]\nExecStart=\n")
    (tmp_path / "etc/systemd/system/getty.target.wants").mkdir()
    return tmp_path


def parse(text):
    assert text.splitlines()[0] == "# agent-sec-snapshot v1"
    out = {}
    for ln in text.splitlines()[1:]:
        s, i, v = ln.split("\t")
        out[s + ":" + i] = v
    return out


def test_sections_on_tree(tree):
    d = parse(snap.render(str(tree), fake_run(OK_RUN), which_all))
    assert d["users:root"] == "0 /bin/bash" and d["users:ubuntu"] == "1000 /bin/bash"
    assert d["users:agent-ro"] == "998 /bin/sh" and d["users:git"] == "999 /bin/bash"
    assert "users:daemon" not in d and "users:nobody" not in d and "users:dyn" not in d
    assert d["groups:sudo"] == "deploy ubuntu" and d["groups:docker"] == "-" and "groups:users" not in d
    assert re.fullmatch(r"[0-9a-f]{64} 2", d["keys:/root/.ssh/authorized_keys"])
    assert re.fullmatch(r"[0-9a-f]{64} 1", d["keys:/home/ubuntu/.ssh/authorized_keys"])
    assert re.fullmatch(r"[0-9a-f]{64} 1", d["keys:/var/lib/git/.ssh/authorized_keys"])
    assert d["keys:/home/agent-ro/.ssh/authorized_keys"] == "-"
    assert "keys:/root/.ssh/authorized_keys2" not in d
    assert re.fullmatch(r"[0-9a-f]{64}", d["sudoers:/etc/sudoers"]) and "sudoers:/etc/sudoers.d/agent-ro" in d
    assert re.fullmatch(r"[0-9a-f]{64}", d["shadow:/etc/shadow"])
    assert d["sshd:permitrootlogin"] == "prohibit-password" and d["sshd:port"] == "22"
    assert d["sshd:allowusers"] == "root deploy" and d["sshd:denyusers"] == "-"
    assert d["sshd:authorizedkeyscommand"] == "none" and d["sshd:usepam"] == "yes"
    assert re.fullmatch(r"[0-9a-f]{64}", d["sshd:file:/etc/ssh/sshd_config"])
    assert "sshd:file:/etc/ssh/sshd_config.d/00-hardening.conf" in d
    for k in ("cron:/etc/crontab", "cron:/etc/cron.d/backup", "cron:/etc/cron.daily/logrotate",
              "cron:/var/spool/cron/crontabs/root"):
        assert re.fullmatch(r"[0-9a-f]{64}", d[k]), k
    assert d["persist:/etc/ld.so.preload"] == "-" and d["persist:/etc/rc.local"] == "-"
    assert re.fullmatch(r"[0-9a-f]{64}", d["units:digipaddock-monitor.service"])
    assert re.fullmatch(r"[0-9a-f]{64}", d["units:multi-user.target.wants/nginx.service"])
    assert "units:multi-user.target.wants/fail2ban.service" in d and "units:getty.target.wants/" not in d
    assert re.fullmatch(r"[0-9a-f]{64}", d["units:nginx.service.d/override.conf"])
    assert d["fw:ufw"] == "active" and d["fw:ufw:default"] == "deny (incoming), allow (outgoing), disabled (routed)"
    assert d["fw:ufw:22/tcp ALLOW IN Anywhere"] == "rule" and d["fw:ufw:22/tcp (v6) ALLOW IN Anywhere (v6)"] == "rule"
    assert not any(k.startswith("fw:ufw:Logging") or k.startswith("fw:ufw:New profiles") or k.startswith("fw:ufw:To ") for k in d)
    assert d["fw:fail2ban"] == "active"


def test_values_do_not_contain_file_contents(tree):
    text = snap.render(str(tree), fake_run(OK_RUN), which_all)
    for secret in ("ssh-ed25519 AAA", "NOPASSWD", "root:*:1", "/usr/local/bin/backup", "@reboot", "ExecStart"):
        assert secret not in text


def test_cron_hash_ignores_comment_header(tree):
    a = parse(snap.render(str(tree), fake_run(OK_RUN), which_all))["cron:/var/spool/cron/crontabs/root"]
    (tree / "var/spool/cron/crontabs/root").write_text(
        "# (/tmp/crontab.zzz installed on Tue Oct  6 11:00:00 2026)\n@reboot /bin/true\n", encoding="utf-8")
    b = parse(snap.render(str(tree), fake_run(OK_RUN), which_all))["cron:/var/spool/cron/crontabs/root"]
    assert a == b
    (tree / "var/spool/cron/crontabs/root").write_text("@reboot /bin/false\n", encoding="utf-8")
    assert parse(snap.render(str(tree), fake_run(OK_RUN), which_all))["cron:/var/spool/cron/crontabs/root"] != a


def test_section_error_isolated_and_others_printed(tree):
    d = parse(snap.render(str(tree), fake_run({"sshd": (1, ""), "ufw": (0, UFW), "systemctl": (0, "active\n")}), which_all))
    assert d["sshd:error"].startswith("RuntimeError: sshd -T")
    assert "sshd:port" not in d and "users:root" in d and "fw:ufw" in d
    assert "keys:/root/.ssh/authorized_keys" in d  # without sshd -T the keys are looked for in the default places


def test_fw_absent_inactive_and_errors(tree):
    d = parse(snap.render(str(tree), fake_run({"sshd": (0, SSHD_T), "systemctl": (3, "inactive\n")}), which_none))
    assert d["fw:ufw"] == "absent" and d["fw:fail2ban"] == "absent"
    d = parse(snap.render(str(tree), fake_run({"sshd": (0, SSHD_T), "ufw": (0, "Status: inactive\n"), "systemctl": (3, "inactive\n")}), which_all))
    assert d["fw:ufw"] == "inactive" and not any(k.startswith("fw:ufw:") for k in d) and d["fw:fail2ban"] == "inactive"
    d = parse(snap.render(str(tree), fake_run({"sshd": (0, SSHD_T), "ufw": (1, "ERROR: problem running iptables"), "systemctl": (0, "active\n")}), which_all))
    assert d["fw:error"].startswith("RuntimeError: ufw status")
    d = parse(snap.render(str(tree), fake_run({"sshd": (0, SSHD_T), "ufw": (0, UFW), "systemctl": (1, "")}), which_all))
    assert d["fw:error"].startswith("RuntimeError: systemctl is-active")


def test_keys_symlink_and_big_file_not_hashed(tree):
    big = tree / "home/ubuntu/.ssh/authorized_keys2"
    big.write_bytes(b"x" * (snap.FILE_MAX + 1))
    d = parse(snap.render(str(tree), fake_run(OK_RUN), which_all))
    assert d["keys:/home/ubuntu/.ssh/authorized_keys2"] == "big %d" % (snap.FILE_MAX + 1)
    link = tree / "root/.ssh/authorized_keys2"
    try:
        os.symlink(str(tree / "etc/shadow"), link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink недоступен")
    d = parse(snap.render(str(tree), fake_run(OK_RUN), which_all))
    assert d["keys:/root/.ssh/authorized_keys2"].startswith("symlink -> ")
    assert not re.search(r"[0-9a-f]{64}", d["keys:/root/.ssh/authorized_keys2"])  # the target is printed, there is no hash


def test_units_symlink_shows_target(tree):
    link = tree / "etc/systemd/system/masked.service"
    try:
        os.symlink("/dev/null", link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink недоступен")
    assert parse(snap.render(str(tree), fake_run(OK_RUN), which_all))["units:masked.service"] == "symlink -> /dev/null"


def test_output_sorted_deterministic_and_tabs_escaped(tree):
    a = snap.render(str(tree), fake_run(OK_RUN), which_all)
    b = snap.render(str(tree), fake_run(OK_RUN), which_all)
    assert a == b
    for ln in a.splitlines()[1:]:
        assert ln.count("\t") == 2
    assert snap._value("a\tb\nc") == "a b c"
    assert snap._value("файл\udcff") == "файл�"  # a surrogate from os.listdir does not crash the snapshot


def test_truncated_per_section(tree, monkeypatch):
    full = snap.render(str(tree), fake_run(OK_RUN), which_all)
    users_block = [ln for ln in full.splitlines() if ln.startswith("users\t")]
    monkeypatch.setattr(snap, "LIMIT", len("# agent-sec-snapshot v1\n") + len("\n".join(users_block)) + 1 + 10)
    d = parse(snap.render(str(tree), fake_run(OK_RUN), which_all))
    assert d["users:root"] == "0 /bin/bash" and d["groups:error"] == "truncated" and d["fw:error"] == "truncated"
    assert "keys:/root/.ssh/authorized_keys" not in d


def test_high_uid_account_visible_and_keys_scanned(tree):
    with (tree / "etc/passwd").open("a", encoding="utf-8") as f:
        f.write("evil:x:200000:200000::/opt/evil:/bin/bash\nnfsnobody:x:4294967294:4294967294::/:/bin/bash\n")
    (tree / "opt/evil/.ssh").mkdir(parents=True)
    (tree / "opt/evil/.ssh/authorized_keys").write_text("ssh-ed25519 EEE e\n", encoding="utf-8")
    d = parse(snap.render(str(tree), fake_run(OK_RUN), which_all))
    assert d["users:evil"] == "200000 /bin/bash"
    assert d["users:nfsnobody"] == "4294967294 /bin/bash"  # nobody with a login shell is visible
    assert re.fullmatch(r"[0-9a-f]{64} 1", d["keys:/opt/evil/.ssh/authorized_keys"])


def test_login_shell_in_dynamic_uid_range_visible(tree):
    """The DynamicUser/nobody exemption is only for the branch "uid ≥ 1000": with a login shell an account is always visible."""
    with (tree / "etc/passwd").open("a", encoding="utf-8") as f:
        f.write("evil:x:62000:62000::/var/tmp/x:/bin/bash\n")
    (tree / "var/tmp/x/.ssh").mkdir(parents=True)
    (tree / "var/tmp/x/.ssh/authorized_keys").write_text("ssh-ed25519 EEE e\n", encoding="utf-8")
    d = parse(snap.render(str(tree), fake_run(OK_RUN), which_all))
    assert d["users:evil"] == "62000 /bin/bash"
    assert re.fullmatch(r"[0-9a-f]{64} 1", d["keys:/var/tmp/x/.ssh/authorized_keys"])
    assert "users:dyn" not in d and "users:nobody" not in d  # the same ranges with nologin are hidden


def test_keys_in_homes_of_nologin_service_accounts(tree):
    """ssh -N by key works with nologin too: keys are looked for in the homes of all passwd records, "-" — only for accounts."""
    with (tree / "etc/passwd").open("a", encoding="utf-8") as f:
        f.write("svc:x:120:120::/var/lib/svc:/usr/sbin/nologin\nwww-data:x:33:33::/var/www:/usr/sbin/nologin\n"
                "proc:x:121:121::/proc:/usr/sbin/nologin\n")
    (tree / "var/lib/svc/.ssh").mkdir(parents=True)
    (tree / "var/lib/svc/.ssh/authorized_keys").write_text("ssh-ed25519 FFF f\n", encoding="utf-8")
    (tree / "var/www").mkdir(parents=True)
    d = parse(snap.render(str(tree), fake_run(OK_RUN), which_all))
    assert re.fullmatch(r"[0-9a-f]{64} 1", d["keys:/var/lib/svc/.ssh/authorized_keys"])
    assert not any(k.startswith("keys:/var/www") for k in d)  # no keys — no line
    assert not any(k.startswith("keys:/usr/sbin") or k.startswith("keys:/proc") for k in d)  # daemon, a service home
    assert d["keys:/home/agent-ro/.ssh/authorized_keys"] == "-"  # accounts still get "-"


def test_home_normalisation_and_skips():
    assert snap._home("/", "/var/lib/svc/") == os.path.join("/", "var", "lib", "svc")
    assert snap._home("/", "//var/lib/svc") == os.path.join("/", "var", "lib", "svc")
    for home in ("/", "/nonexistent", "/proc", "/dev", "/sys", "/run", "/a/..", "relative", ""):
        assert snap._home("/", home) is None, home


def test_symlink_target_truncated(tree, monkeypatch):
    real_lstat = os.lstat
    link = str(tree / "root/.ssh/authorized_keys2")

    def fake_lstat(path, *a, **kw):
        if path == link:
            return os.stat_result((0o120777,) + (0,) * 9)
        return real_lstat(path, *a, **kw)
    monkeypatch.setattr(snap.os, "lstat", fake_lstat)
    monkeypatch.setattr(snap.os, "readlink", lambda path: "/x" * 150)
    value = snap._file_value(link)
    assert value == "symlink -> " + ("/x" * 150)[:120]
    assert len(value) <= len("symlink -> ") + 120


def test_groups_include_primary_gid_members(tree):
    with (tree / "etc/passwd").open("a", encoding="utf-8") as f:
        f.write("deploy:x:1001:27::/home/deploy:/bin/bash\n")
    (tree / "etc/group").write_text("sudo:x:27:ubuntu\ndocker:x:999:\n", encoding="utf-8")
    d = parse(snap.render(str(tree), fake_run(OK_RUN), which_all))
    assert d["groups:sudo"] == "deploy ubuntu" and d["groups:docker"] == "-"


def test_keys_line_count_only_for_hash(tree, monkeypatch):
    p = str(tree / "root/.ssh/authorized_keys")
    for special in ("other", "big 5"):
        monkeypatch.setattr(snap, "_sha", lambda path, skip, v=special: (v, 0))
        assert snap._file_value(p, lines=True) == special  # without the tail " 0"


def test_lstat_and_readlink_errors_are_not_absent(tree, monkeypatch):
    real_lstat = os.lstat
    denied = str(tree / "root/.ssh/authorized_keys")
    gone = str(tree / "root/.ssh/gone")

    def fake_lstat(path, *a, **kw):
        if path == denied:
            raise PermissionError(13, "denied")
        if path == gone:  # a symlink removed between lstat and readlink
            return os.stat_result((0o120777,) + (0,) * 9)
        return real_lstat(path, *a, **kw)
    monkeypatch.setattr(snap.os, "lstat", fake_lstat)
    assert snap._file_value(denied) == "error PermissionError"
    assert snap._file_value(gone) == "error FileNotFoundError"
    assert snap._file_value(str(tree / "root/.ssh/nope")) == "-"
    assert snap._file_value(str(tree / "etc/shadow/x")) == "-"  # NotADirectoryError (Linux) / ENOENT (Windows)


# throwaway keys made by ssh-keygen; fingerprints as `ssh-keygen -lf` prints them
ED = "AAAAC3NzaC1lZDI1NTE5AAAAIHj4ka59tV7PYSK7ZifGotuFkQ+AA/3SVKpkLEOH+kqi"
ED_FP = "SHA256:rkrXT9YGOEScAdN9+GHPuHv6w+pJySQRtqF/9eOcLgk"
RSA = ("AAAAB3NzaC1yc2EAAAADAQABAAAAgQCvJhHwCF8adMLO51Kur/nmxz2z7Af995rrahMkJEhgyUYT6m1V1HhXk9hWqp7VCbz65jzH32NY73lwyYxl"
       "XnWdtDhWqYmNNN7/obAadqHHkgqKGOsSvfZLd5jVLKfVRionZvX953EWel93UbHn833AMEtH7YCRnL1qgysgROIyLw==")
RSA_FP = "SHA256:QGzOwGFJjwIlWrNtllB75OKKWVdqVjyzCeLohan9EOc"
EC = ("AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBFCxKYw5Pjisj1f462DtbVnqZE9r+CGrFXGuLcct2d01ghJSw/um+TfBAA/"
      "YicfEwp+yiWtdCEAcbq4jKyPrx4U=")
EC_FP = "SHA256:puhfZSOzIj9B9IixvUzdvmqHXxhF6c/5TXHDXG3XIi8"
AUTH = "/root/.ssh/authorized_keys"


def test_keyprints_name_each_key_without_its_body(tree):
    (tree / "root/.ssh/authorized_keys").write_text(
        '# deploy keys\ncommand="echo \\"a b\\" > /tmp/x",no-pty,FROM="10.0.0.1" ssh-ed25519 %s dev@laptop\n'
        "ssh-rsa %s rsa key\n\necdsa-sha2-nistp256 %s\nssh-ed25519 AAA broken\nssh-rsa %s mismatched\n"
        % (ED, RSA, EC, ED), encoding="utf-8")
    text = snap.render(str(tree), fake_run(OK_RUN), which_all)
    d = parse(text)
    assert d["keyprints:" + AUTH] == d["keys:" + AUTH]  # the pair: these prints are of exactly this file
    assert d["keyprints:%s ED25519 %s" % (AUTH, ED_FP)] == "command,from,no-pty dev@laptop"
    assert d["keyprints:%s RSA %s" % (AUTH, RSA_FP)] == "- rsa key"
    assert d["keyprints:%s ECDSA %s" % (AUTH, EC_FP)] == "-"
    assert len([k for k in d if k.startswith("keyprints:%s " % AUTH)]) == 3  # broken and mismatched lines skipped
    for body in (ED, RSA[:40], EC[:40], "echo", "10.0.0.1", "/tmp/x"):
        assert body not in text
    assert re.fullmatch(r"[0-9a-f]{64} 1", d["keyprints:/home/ubuntu/.ssh/authorized_keys"])  # a file without keys
    assert not any(k.startswith("keyprints:/home/agent-ro") for k in d)  # no file — no line


def test_keyprints_comment_printable_and_short(tree):
    (tree / "root/.ssh/authorized_keys").write_text(
        "ssh-ed25519 %s \x1b[31mключ %s\n" % (ED, "x" * 200), encoding="utf-8")
    value = parse(snap.render(str(tree), fake_run(OK_RUN), which_all))["keyprints:%s ED25519 %s" % (AUTH, ED_FP)]
    flags, comment = value.split(" ", 1)
    assert flags == "-" and len(comment) == snap.COMMENT_MAX and re.fullmatch(r"[\x20-\x7e]+", comment)
    assert comment.startswith("?[31m????")


def test_key_line_options():
    assert snap._key_line('restrict,command="a,b c",permitopen="h:1",bogus ssh-rsa %s c' % RSA) == \
        ("RSA", RSA_FP, "command,other,permitopen,restrict", "c")
    assert snap._key_line('from="a b" ssh-ed25519 %s' % ED)[1:3] == (ED_FP, "from")
    assert snap._key_line("ssh-ed25519 %s  two words " % ED) == ("ED25519", ED_FP, "-", "two words")
    for bad in ("", "# ssh-ed25519 %s" % ED, "ssh-ed25519", "ssh-foo %s" % ED, "ssh-ed25519 %s" % RSA,
                "ssh-ed25519 %s!" % ED, 'command="x ssh-ed25519 %s' % ED):
        assert snap._key_line(bad) is None, bad


def test_pubkeys_outgoing_keys_of_homes(tree):
    (tree / "home/ubuntu/.ssh/id_ed25519.pub").write_text("ssh-ed25519 %s ubuntu@box\n" % ED, encoding="utf-8")
    (tree / "home/ubuntu/.ssh/id_ed25519").write_text("private part\nSECRET\n", encoding="utf-8")  # not a *.pub
    (tree / "root/.ssh/deploy.pub").write_text("ecdsa-sha2-nistp256 %s\n" % EC, encoding="utf-8")
    (tree / "root/.ssh/junk.pub").write_text("not a key\n", encoding="utf-8")
    text = snap.render(str(tree), fake_run(OK_RUN), which_all)
    d = parse(text)
    assert d["pubkeys:/home/ubuntu/.ssh/id_ed25519.pub ED25519 %s" % ED_FP] == "ubuntu@box"
    assert d["pubkeys:/root/.ssh/deploy.pub ECDSA %s" % EC_FP] == "-"
    assert not any("junk" in k for k in d) and "SECRET" not in text and ED not in text


def test_pubkeys_symlink_not_followed(tree):
    (tree / "etc/fake.pub").write_text("ssh-ed25519 %s other\n" % ED, encoding="utf-8")
    try:
        os.symlink(str(tree / "etc/fake.pub"), str(tree / "root/.ssh/link.pub"))
    except (OSError, NotImplementedError):
        pytest.skip("symlink недоступен")
    assert not any(k.startswith("pubkeys:") for k in parse(snap.render(str(tree), fake_run(OK_RUN), which_all)))


def test_key_sections_last_so_truncation_spares_the_rest(tree, monkeypatch):
    assert [n for n, _ in snap.SECTIONS][-2:] == ["keyprints", "pubkeys"]
    (tree / "root/.ssh/authorized_keys").write_text("ssh-ed25519 %s a\n" % ED, encoding="utf-8")
    full = snap.render(str(tree), fake_run(OK_RUN), which_all)
    head = [ln for ln in full.splitlines() if not ln.startswith(("keyprints\t", "pubkeys\t"))]
    monkeypatch.setattr(snap, "LIMIT", len("\n".join(head)) + 2)
    d = parse(snap.render(str(tree), fake_run(OK_RUN), which_all))
    assert d["fw:fail2ban"] == "active" and d["keyprints:error"] == "truncated" and d["pubkeys:error"] == "truncated"


def test_main_rejects_arguments(capsys):
    assert snap.main(["--help"]) == 2
    assert "без аргументов" in capsys.readouterr().err


@pytest.mark.skipif(importlib.util.find_spec("vermin") is None, reason="vermin не установлен (requirements-dev.txt)")
def test_snapshot_source_is_python36_compatible():
    proc = subprocess.run([sys.executable, "-c", VERMIN, "--no-tips", str(SNAPSHOT)], capture_output=True, text=True)
    match = re.search(r"Minimum required versions:.*?3\.(\d+)", proc.stdout)
    assert match and int(match.group(1)) <= 6, proc.stdout + proc.stderr
