"""grep, zgrep, zcat, "|" only inside a pattern and ssh-keygen -lf in the agent-ro-gate gateway."""
import gzip
import io
import os
import posixpath

import pytest

from gate import agent_ro_gate as gate

POSIX = pytest.mark.skipif(os.name != "posix", reason="настоящие файлы, симлинки и grep — POSIX")
MB = 1024 * 1024


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """(log, etc): temporary roots in place of /var/log and /etc; log is the root for zcat/zgrep."""
    log, etc = tmp_path / "log", tmp_path / "etc"
    log.mkdir()
    etc.mkdir()
    monkeypatch.setattr(gate, "ROOTS", ((str(log), True), (str(etc), True), ("/backups", False)))
    monkeypatch.setattr(gate, "LOG_ROOT", str(log))
    return log, etc


def big_text(target: bytes) -> bytes:
    """More than 1 MB of filler lines, then target, then more lines."""
    filler = b"".join(b"Oct  5 23:%02d:00 host sshd[1]: filler line %07d\n" % (i % 60, i) for i in range(30000))
    assert len(filler) > MB
    return filler + target + b"Oct  6 03:00:00 host sshd[1]: tail line\n"


def run(raw):
    out, err = io.BytesIO(), io.BytesIO()
    code = gate.main([], {"SSH_ORIGINAL_COMMAND": raw}, out, err)
    return code, out.getvalue().decode("utf-8"), err.getvalue().decode("utf-8")


# --- grep over the whole file, several paths ---------------------------------------------

@POSIX
def test_grep_finds_lines_after_the_first_megabyte(roots):
    log, _ = roots
    auth = log / "auth.log"
    auth.write_bytes(big_text(b"Oct  6 02:35:01 host sshd[2]: Accepted publickey for root\n"
                              b"Oct  6 02:36:01 host sshd[3]: Failed password for bob\n"))
    code, out, err = run('grep -F -n "Oct  6 02:35" %s' % auth)
    assert code == 0, err
    assert "30001:Oct  6 02:35:01 host sshd[2]: Accepted publickey for root" in out
    code, out, _ = run('grep -n -A 1 -e "Accepted|Failed" %s' % auth)
    assert code == 0 and "Accepted publickey" in out and "Failed password" in out and "tail line" in out
    assert "filler" not in out


@POSIX
def test_grep_takes_several_explicit_paths_and_checks_each(roots, monkeypatch):
    log, etc = roots
    (log / "a.log").write_text("needle one\n")
    (etc / "b.conf").write_text("hay\nneedle two\n")
    seen = []
    real_check = gate.check_path
    monkeypatch.setattr(gate, "check_path", lambda p, need_content: seen.append((p, need_content)) or
                        real_check(p, need_content))
    code, out, _ = run("grep -F -c needle %s/a.log %s/b.conf" % (log, etc))
    assert code == 0
    assert out.splitlines() == ["%s/a.log:1" % log, "%s/b.conf:1" % etc]
    assert seen == [("%s/a.log" % log, True), ("%s/b.conf" % etc, True)]


def test_grep_plan_is_normalized():
    assert gate.check('grep -F -n "Oct  6 02:35" /var/log/auth.log').argv == [
        "grep", "-F", "-n", "-e", "Oct  6 02:35", "--", "/var/log/auth.log"]
    # without -F/-E — always -E: "a|b" means an alternation
    assert gate.check('grep -n -A 5000 -e "Accepted|Failed" /var/log/auth.log').argv == [
        "grep", "-E", "-n", "-A", "5000", "-e", "Accepted|Failed", "--", "/var/log/auth.log"]
    assert gate.check("grep -ie --x /var/log/syslog").argv == ["grep", "-E", "-i", "-e", "--x", "--", "/var/log/syslog"]


ALLOWED = [
    "grep -F x /var/log/syslog", "grep -E 'a+' /var/log/syslog", "grep -inc x /var/log/syslog",
    "grep -m 5 -B 2 -C 3 -h -H -l -v -w -x -a -s -e x /var/log/syslog", "grep -m5 -ex /var/log/syslog",
    "grep --fixed-strings --ignore-case --line-number --count --no-filename --with-filename --files-with-matches "
    "--invert-match --word-regexp --line-regexp --text --no-messages --regexp=x /var/log/syslog",
    "grep -F -F x /var/log/syslog", "grep -E --extended-regexp x /var/log/syslog",
    "grep --max-count=1 --after-context 2 --before-context=3 --context=4 --regexp x /var/log/syslog",
    "grep -F ошибка /var/log/syslog", "grep -F x /etc/hosts /var/log/syslog",
    "zgrep -F -c 'Accepted publickey' /var/log/auth.log.2.gz /var/log/auth.log.3.gz",
    "zcat /var/log/auth.log.2.gz", "zcat /var/log/auth.log.2.gz /var/log/syslog.3.gz",
]


@pytest.mark.parametrize("raw", ALLOWED)
def test_search_forms_allowed(raw):
    assert gate.check(raw).sudo is False


# --- zgrep over the whole unpacked file, zcat/zgrep only for *.gz under /var/log --------

@POSIX
def test_zgrep_finds_lines_after_the_first_unpacked_megabyte(roots):
    log, _ = roots
    two, three = log / "auth.log.2.gz", log / "auth.log.3.gz"
    two.write_bytes(gzip.compress(big_text(b"Oct  6 02:35:01 host sshd[2]: Accepted publickey for root\n")))
    three.write_bytes(gzip.compress(b"nothing here\n"))
    assert two.stat().st_size < MB  # the match lies beyond the first megabyte of the unpacked text, not of the file
    code, out, err = run('zgrep -F -n "Accepted publickey" %s' % two)
    assert code == 0, err
    assert out == "30001:Oct  6 02:35:01 host sshd[2]: Accepted publickey for root\n"
    code, out, _ = run('zgrep -F -c "Accepted publickey" %s %s' % (two, three))
    assert code == 0 and out.splitlines() == ["%s:1" % two, "%s:0" % three]
    code, out, _ = run('zgrep -F -c -h "Accepted publickey" %s %s' % (two, three))
    assert out.splitlines() == ["1", "0"]
    code, out, _ = run("zgrep -F absent %s" % three)
    assert code == 1 and out == ""


@POSIX
def test_zgrep_reports_a_broken_archive(roots):
    log, _ = roots
    (log / "bad.gz").write_bytes(b"not gzip at all\n")
    (log / "good.gz").write_bytes(gzip.compress(b"needle\n"))
    code, out, _ = run("zgrep -F needle %s/bad.gz %s/good.gz" % (log, log))
    assert code == 2
    assert "ошибка распаковки" in out and "%s/good.gz:needle" % log in out


@POSIX
def test_zcat_prints_the_unpacked_file(roots):
    log, _ = roots
    (log / "x.gz").write_bytes(gzip.compress(b"line one\npassword=hunter2\n"))
    code, out, _ = run("zcat %s/x.gz" % log)
    assert code == 0 and out.startswith("line one\npassword=***\n") and "скрыто секретов: 1" in out
    assert gate.check("zcat %s/x.gz" % log).argv == ["zcat", "-cd", "--", "%s/x.gz" % log]
    assert gate.materialize(gate.check("zcat %s/x.gz" % log), lambda n, path=None: "/usr/bin/" + n)[0] == \
        "/usr/bin/gzip"


@POSIX
@pytest.mark.parametrize("command", ["zcat", "zgrep -F x"])
def test_zcat_zgrep_only_gz_under_log_root(roots, command):
    log, etc = roots
    (log / "plain.log").write_text("x\n")
    (etc / "x.gz").write_bytes(gzip.compress(b"x\n"))
    (log / "link.gz").symlink_to(etc / "x.gz")
    (log / "bad name.gz").write_bytes(gzip.compress(b"x\n"))
    for path in (log / "plain.log", etc / "x.gz", log / "link.gz"):
        with pytest.raises(gate.Deny, match=r"\*\.gz"):
            gate.check("%s %s" % (command, path))
    with pytest.raises(gate.Deny):
        gate.check("%s '%s'" % (command, log / "bad name.gz"))


# --- "|" only inside a pattern ----------------------------------------------------

PIPE_ALLOWED = [
    'journalctl -u ssh -g "Accepted|Failed" --since 2026-10-06', "journalctl --grep='a|b' -n 50",
    'grep -e "a|b" /var/log/auth.log', 'grep --regexp="a|b" /var/log/auth.log', 'grep -F -e "a|b" /var/log/auth.log',
    'zgrep -e "a|b" /var/log/auth.log.2.gz',
]


@pytest.mark.parametrize("raw", PIPE_ALLOWED)
def test_pipe_inside_pattern_passes(raw):
    plan = gate.check(raw)
    assert any("a|b" in word or "Accepted|Failed" in word for word in plan.argv)


@POSIX
def test_pipe_alternation_finds_both_in_grep_and_zgrep(roots):
    log, _ = roots
    (log / "auth.log").write_text("x alpha\ny\nz beta\n")
    (log / "auth.log.1.gz").write_bytes(gzip.compress(b"x alpha\ny\nz beta\n"))
    for raw in ('grep -e "alpha|beta" %s/auth.log', 'zgrep -e "alpha|beta" %s/auth.log.1.gz'):
        code, out, _ = run(raw % log)
        assert code == 0 and out == "x alpha\nz beta\n"


PIPE_DENIED = [
    'grep "a|b" /var/log/auth.log', 'zgrep "a|b" /var/log/auth.log.2.gz',
    "cat /var/log/auth.log | grep x", "cat /var/log/auth.log |grep x", "journalctl -n 5 | grep x",
    "grep -e x /var/log/auth.log | head", 'grep -e x "/var/log/a|b"', 'grep -e x -m "1|2" /var/log/auth.log',
    'journalctl -u "a|b"', "journalctl '_COMM=a|b'", 'journalctl --since "a|b"', 'ls "/var/log|x"',
    'mysql -e "SELECT a|b FROM t"', "uptime | head",
]


@pytest.mark.parametrize("raw", PIPE_DENIED)
def test_pipe_elsewhere_is_denied(raw):
    with pytest.raises(gate.Deny):
        gate.check(raw)


def test_pipe_in_positional_pattern_suggests_e():
    with pytest.raises(gate.Deny, match="-e"):
        gate.check('grep "a|b" /var/log/auth.log')
    with pytest.raises(gate.Deny, match="метасимвол"):
        gate.check("cat /var/log/auth.log | grep x")


# --- a closed list of options ---------------------------------------------------------

OPTIONS_DENIED = [
    "grep -r x /var/log", "grep -R x /var/log", "grep -d recurse x /var/log", "grep -D read x /var/log/syslog",
    "grep -f /etc/hosts /var/log/syslog", "grep -P x /var/log/syslog", "grep --include=*.log x /var/log",
    "grep --exclude=x x /var/log/syslog", "grep --recursive x /var/log", "grep --incl=x x /var/log/syslog",
    "grep --file=/etc/hosts /var/log/syslog", "grep -z x /var/log/syslog", "grep -5 x /var/log/syslog",
    "grep -m abc x /var/log/syslog", "grep -A x x /var/log/syslog", "grep -B -1 x /var/log/syslog",
    "grep -C 1k x /var/log/syslog", "grep --max-count=1a x /var/log/syslog", "grep --context=+3 x /var/log/syslog",
    "grep -m ٣ x /var/log/syslog",
    # -o: a bare fragment ('s3cr3t', ':pw@') carries no context the masks recognise
    "grep -o x /var/log/syslog", "grep -oE x /var/log/syslog", "grep --only-matching x /var/log/syslog",
    "zgrep -o x /var/log/a.gz",
    "grep -F -E x /var/log/syslog", "grep --fixed-strings -E x /var/log/syslog", "zgrep -FE x /var/log/a.gz",
    "zgrep -r x /var/log/a.gz", "zgrep -f /etc/hosts /var/log/a.gz", "zgrep -m x y /var/log/a.gz",
    "zgrep -Z x /var/log/a.gz",
    "zcat -f /var/log/a.gz", "zcat -d /var/log/a.gz", "zcat --stdout /var/log/a.gz", "zcat -- /var/log/a.gz",
    "zcat", "grep", "grep x", "grep -e x", "zgrep -F x",
]


@pytest.mark.parametrize("raw", OPTIONS_DENIED)
def test_options_outside_the_list_are_denied(raw):
    with pytest.raises(gate.Deny):
        gate.check(raw)


# --- forbidden paths and symlinks --------------------------------------------------

PATHS_DENIED = [
    "grep x /etc/shadow", "grep x /etc/gshadow", "grep -F x /root/.ssh/authorized_keys", "grep x /root/notes",
    "grep x /var/www/site/.env", "grep x /var/www/s/config.ini", "grep x /etc/wireguard/wg0.conf",
    "grep x /backups/db.sql", "grep x /home/u/notes", "grep x /tmp/x", "grep x var/log/syslog",
    "grep x /var/log/../../etc/shadow", "grep x /var/log/syslog /etc/shadow",
    "zgrep x /backups/db.sql.gz", "zgrep x /root/x.gz", "zgrep x /home/u/x.gz", "zgrep x /var/www/s/.env.gz",
    "zcat /backups/db.sql.gz", "zcat /home/u/x.gz", "zcat /root/x.gz", "zcat /etc/x.gz",
    "zcat /var/log/../../etc/shadow.gz",
]


@pytest.mark.parametrize("raw", PATHS_DENIED)
def test_forbidden_and_outside_paths_are_denied(raw):
    with pytest.raises(gate.Deny):
        gate.check(raw)


@pytest.mark.parametrize("raw,link,target", [
    ("grep -F x /var/www/site/link", "/var/www/site/link", "/var/www/site/.env"),
    ("grep -F x /var/www/site/link", "/var/www/site/link", "/etc/shadow"),
    ("grep -F x /var/log/link", "/var/log/link", "/home/u/notes"),
    ("zgrep -F x /var/log/link.gz", "/var/log/link.gz", "/var/www/site/.env"),
    ("zcat /var/log/link.gz", "/var/log/link.gz", "/root/x.gz"),
])
def test_symlinks_to_secrets_are_denied(monkeypatch, raw, link, target):
    monkeypatch.setattr(gate, "realpath", lambda p: target if p == link else posixpath.normpath(p))
    with pytest.raises(gate.Deny):
        gate.check(raw)


# --- a private key outside /var/log, redaction of what was found ------------------------------

KEY = (b"-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ\n"  # privacy-scan: allow (fake sample)
       b"-----END OPENSSH PRIVATE KEY-----\n")


@POSIX
def test_grep_refuses_a_file_with_a_private_key_outside_log_root(roots):
    log, etc = roots
    (etc / "key.pem").write_bytes(b"comment\n" + KEY)
    with pytest.raises(gate.Deny, match="PRIVATE KEY"):
        gate.check("grep -F b3Blbn %s/key.pem" % etc)
    code, out, err = run("grep -F b3Blbn %s/key.pem" % etc)
    assert code == 126 and out == "" and "PRIVATE KEY" in err
    (log / "key.log").write_bytes(KEY)  # /var/log is not checked: the brief decided so
    assert gate.check("grep -F b3Blbn %s/key.log" % log).argv[0] == "grep"
    (etc / "plain.conf").write_text("listen 80\n")
    assert gate.check("grep -F listen %s/plain.conf" % etc).argv[0] == "grep"


@POSIX
def test_private_key_marker_across_read_chunks_is_found(roots):
    _, etc = roots
    (etc / "big").write_bytes(b"x" * (MB - 5) + b"PRIVATE KEY" + b"\n")
    with pytest.raises(gate.Deny, match="PRIVATE KEY"):
        gate.check("grep -F x %s/big" % etc)


@POSIX
def test_private_key_check_skips_directories_and_missing_files(roots):
    _, etc = roots
    assert gate.check("grep -F x %s" % etc).argv[0] == "grep"
    assert gate.check("grep -F x %s/missing" % etc).argv[0] == "grep"


SECRETS = (b"Oct  6 02:35 app: db password=hunter2 ok\n"
           b"Oct  6 02:36 app: fetch https://admin:s3cr3tpw@example.com/x\n"
           b"Oct  6 02:37 app: header Authorization: Bearer abcdefgh12345678\n")


@POSIX
def test_found_secrets_are_masked_with_a_counter(roots):
    log, _ = roots
    (log / "app.log").write_bytes(big_text(SECRETS))
    (log / "app.log.1.gz").write_bytes(gzip.compress(big_text(SECRETS)))
    for raw in ('grep -F "Oct  6 02:3" %s/app.log', 'zgrep -F "Oct  6 02:3" %s/app.log.1.gz'):
        code, out, _ = run(raw % log)
        assert code == 0
        assert "hunter2" not in out and "s3cr3tpw" not in out and "abcdefgh12345678" not in out
        assert "password=***" in out and "https://***:***@example.com/x" in out and "Bearer ***" in out
        assert "[agent-ro-gate: скрыто секретов: 3]" in out


# --- root's key fingerprints --------------------------------------------------------

def test_ssh_keygen_fingerprints_go_through_sudo():
    raw = "ssh-keygen -lf /root/.ssh/authorized_keys"
    assert gate.check(raw) == gate.Plan(raw.split(), True)


@pytest.mark.parametrize("raw", [
    "ssh-keygen", "ssh-keygen -l -f /root/.ssh/authorized_keys", "ssh-keygen -lf /root/.ssh/id_rsa",
    "ssh-keygen -y -f /root/.ssh/id_rsa", "ssh-keygen -lf /etc/ssh/ssh_host_rsa_key.pub",
    "ssh-keygen -lf /root/.ssh/authorized_keys -v", "ssh-keygen -lvf /root/.ssh/authorized_keys",
    "ssh-keygen -lf /root/.ssh/authorized_keys2",
])
def test_other_ssh_keygen_forms_are_denied(raw):
    with pytest.raises(gate.Deny, match="точные формы"):
        gate.check(raw)


def test_sudoers_prints_the_ssh_keygen_line(monkeypatch):
    real = gate.sudoers_lines
    monkeypatch.setattr(gate, "sudoers_lines",
                        lambda: real(lambda n, path=None: n if n.startswith("/") else "/usr/bin/" + n,
                                     digest=lambda p: "0" * 64))
    out = io.BytesIO()
    assert gate.main(["--sudoers"], {}, out, io.BytesIO()) == 0
    assert "agent-ro ALL=(root) NOPASSWD: /usr/bin/ssh-keygen -lf /root/.ssh/authorized_keys\n" in \
        out.getvalue().decode()


@POSIX
def test_tail_refuses_a_file_with_a_private_key_outside_log_root(roots):
    # review 04.11.2026: tail -n +2 starts past the BEGIN line the PEM mask needs; cat and head start at the top
    log, etc = roots
    (etc / "site.pem").write_bytes(KEY)
    for raw in ("tail -n +2 %s/site.pem", "tail %s/site.pem", "tail -n 1 %s/site.pem"):
        with pytest.raises(gate.Deny, match="PRIVATE KEY"):
            gate.check(raw % etc)
    code, out, err = run("tail -n +2 %s/site.pem" % etc)
    assert code == 126 and out == "" and "PRIVATE KEY" in err
    (log / "key.log").write_bytes(KEY)  # /var/log is not checked, as for grep
    assert gate.check("tail -n 5 %s/key.log" % log).argv[0] == "tail"
    (etc / "plain.conf").write_text("listen 80\n")
    assert gate.check("tail -n +1 %s/plain.conf" % etc).argv[0] == "tail"
    code, out, err = run("cat %s/site.pem" % etc)  # cat starts at BEGIN: masked whole
    assert code == 0 and "b3Blbn" not in out


# --- zcat/zgrep in a test environment's journals -----------------------------------------------------------

def _test_envs(tmp_path, monkeypatch, envs, owner=None):
    path = tmp_path / "test-envs.json"
    path.write_text(__import__("json").dumps({"schema_version": 1, "envs": envs}), encoding="utf-8")
    monkeypatch.setattr(gate, "TEST_ENVS_JSON", str(path))
    monkeypatch.setattr(gate, "TEST_ENVS_OWNER", os.getuid() if owner is None else owner)
    return path


@POSIX
def test_zcat_reads_gz_in_a_test_environments_read_directory(roots, tmp_path, monkeypatch):
    log, etc = roots
    journal = etc / "app" / "logs" / "journal"
    journal.mkdir(parents=True)
    (journal / "write-2026-11-16.jsonl.gz").write_bytes(gzip.compress(b'{"rest": null}\n'))
    (etc / "app" / "other.gz").write_bytes(gzip.compress(b"x\n"))
    with pytest.raises(gate.Deny, match=r"\*\.gz"):
        gate.check("zcat %s/write-2026-11-16.jsonl.gz" % journal)
    _test_envs(tmp_path, monkeypatch, {"app-test": {"user": "www-data", "dir": str(etc / "app"),
                                                    "read": ["logs/journal"]}})
    assert gate.test_env_reads() == (str(journal),)
    code, out, _ = run("zcat %s/write-2026-11-16.jsonl.gz" % journal)
    assert code == 0 and out.startswith('{"rest": null}')
    gate.check("zgrep -F rest %s/write-2026-11-16.jsonl.gz" % journal)
    with pytest.raises(gate.Deny, match=r"\*\.gz"):  # the tree outside its read directory stays closed
        gate.check("zcat %s/other.gz" % (etc / "app"))


@POSIX
@pytest.mark.parametrize("envs, owner", [
    ({"a": {"dir": "/var/www/a", "read": ["../etc"]}}, None),
    ({"a": {"dir": "/var/www/a", "read": "logs"}}, None),
    ({"a": {"read": ["logs"]}}, None),
    ({"a": {"dir": "/var/www/a", "read": ["logs"]}}, 12345),  # not the owner the gate trusts
])
def test_an_untrusted_or_malformed_list_opens_no_gz(tmp_path, monkeypatch, envs, owner):
    _test_envs(tmp_path, monkeypatch, envs, owner)
    assert gate.test_env_reads() == ()


def test_no_list_opens_no_gz(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "TEST_ENVS_JSON", str(tmp_path / "absent.json"))
    assert gate.test_env_reads() == ()
