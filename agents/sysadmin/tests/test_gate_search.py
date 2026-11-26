"""grep, zgrep, zcat, "|" only inside a pattern and ssh-keygen -lf in the agent-ro-gate gateway."""
import gzip
import io
import os
import posixpath
import random
import re
import shlex
import shutil
import sys
import time

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


# --- a pattern never tests a hidden value (review 26.11.2026) ------------------------------------

CONF = b"listen 80\npassword = hunter2\napi_token: Zq8xKw\nEnvironment=APPCONF=rawsecret \\\n  OTHER=rawother\nend\n"
# A pattern that matches only the real first character of a secret, and one that matches only a wrong one.
PROBES = [("^password *= *h", "^password *= *x"), ("api_token: Z", "api_token: Y"), ("APPCONF=r", "APPCONF=s"),
          ("OTHER=r", "OTHER=s")]
GREP = shutil.which("grep")
WITH_GREP = pytest.mark.skipif(GREP is None, reason="no grep binary here")


def _grep(path, *words):
    syntax = [] if "-F" in words else ["-E"]
    return gate.run_grep([GREP] + syntax + list(words) + ["--", str(path)], timeout=30)


@WITH_GREP
@pytest.mark.parametrize("right,wrong", PROBES)
@pytest.mark.parametrize("options", [["-c"], ["-l"], ["-v", "-c"], ["-n"], []])
def test_grep_answers_the_same_for_the_secrets_real_and_a_wrong_character(tmp_path, right, wrong, options):
    conf = tmp_path / "app.conf"
    conf.write_bytes(CONF)
    hit, miss = _grep(conf, *(options + ["-e", right])), _grep(conf, *(options + ["-e", wrong]))
    assert (hit.returncode, hit.output) == (miss.returncode, miss.output)
    assert b"hunter2" not in hit.output and b"rawsecret" not in hit.output and b"rawother" not in hit.output


@WITH_GREP
def test_grep_finds_what_cat_shows(tmp_path):
    conf = tmp_path / "app.conf"
    conf.write_bytes(CONF)
    assert _grep(conf, "-n", "-F", "-e", "password = ***").output == b"2:password = ***\n"
    assert _grep(conf, "-c", "-e", r"^ +OTHER=\*\*\*").output == b"1\n"  # the continued line of Environment=
    assert _grep(conf, "-n", "-e", "listen|end").output == b"1:listen 80\n6:end\n"  # the line numbers of the file
    assert _grep(conf, "-F", "-e", "hunter2").returncode == 1


@WITH_GREP
def test_zgrep_matches_the_masked_text_too(tmp_path):
    archive = tmp_path / "app.log.1.gz"
    archive.write_bytes(gzip.compress(b"GET /x?token=Zq8xKw 200\n" * 3))
    command = [GREP, "-E", "-c", "-e", "token=Z", "--", str(archive)]
    assert gate.run_grep(command, "zgrep", timeout=30).output == b"0\n"
    command[4] = r"token=\*\*\*"
    assert gate.run_grep(command, "zgrep", timeout=30).output == b"3\n"


@WITH_GREP
def test_grep_reports_a_missing_file_and_a_directory_and_honours_s(tmp_path):
    (tmp_path / "a.conf").write_bytes(b"x\n")
    words = [GREP, "-E", "-e", "x", "--", str(tmp_path / "a.conf"), str(tmp_path / "missing"), str(tmp_path)]
    result = gate.run_grep(words, timeout=30)
    assert result.returncode == 2
    assert result.output.startswith(b"%s:x\n" % str(tmp_path / "a.conf").encode())
    assert b"grep: %s: " % str(tmp_path / "missing").encode() in result.output
    assert b"grep: %s: " % str(tmp_path).encode() in result.output  # "Is a directory" (Windows: access denied)
    quiet = gate.run_grep([GREP, "-E", "-s"] + words[2:], timeout=30)
    assert quiet.returncode == 2 and b"grep: " not in quiet.output


@pytest.mark.parametrize("text", [
    CONF, b"x\n", b"", b"no newline at the end password=p", b"\n\n", b"a \\\n b \\\n", b"Environment=A=1 \\",
    b"mysql -pSECRET shop\nok\n", b"Authorization: Basic ZmFrZQ==\nBearer abcdefgh12345678\n",
    b"bot123456789:" + b"A" * 35 + b"\nhttps://u:p@h/\ncurl -u a:b\nSetCredential=x:y\n",
    b'Environment="OPTS=-a \\\n  -b" X=1\nPASSWORD=p\n', b"key rotation done\nmonkey=1\n",
])
def test_blind_is_the_line_masks_line_for_line(text):
    expected = gate._mask_lines(text, [0])
    assert gate.blind(text) == expected
    assert expected.count(b"\n") == text.count(b"\n")


# What every line mask must change, by its name in gate.LINE_MASKS and in its order.
MASK_SAMPLES = {
    "url_creds": [b"https://u:p@h/", b"x FTP://a:b@c y"],
    "bearer": [b"BEARER abcdefgh1", b"x bearer\tabcdefgh1"],
    "basic": [b"Authorization: Basic x", b'"authorization": "basic x"'],
    "telegram": [b"123456789:" + b"A" * 35, b"/bot1234567890:" + b"a-_9" * 8 + b"abc/x"],
    "mysql": [b"mysql -px", b"mysqldump -u root -pSECRET shop"],
    "opt_p": [b"x -p pw1", b"x --password pw", b"x\t-p\tpw1", b"x --pass pw", b"x --passwd pw"],
    "opt_u": [b"x -u a:b", b"x --user=a:b", b"x -u\ta:b", b"x -u=a:b", b"x --user a:b"],
    "environment": [b"Environment=A=1", b"  Environment = A=1 \\\n  B=2"],
    "set_credential": [b"SetCredential=a:b", b" SetCredential = id:value"],
    "key_value": [b"password=x", b"API-KEY: x", b"define('DB_PASSWORD', 'p')", b'"token" => "x"', b"key = x"],
}


def test_every_mask_has_its_own_hint():
    # blind() runs a mask only over the lines with its own hint: a match without the hint would stay unmasked
    assert [mask.name for mask in gate.LINE_MASKS] == list(MASK_SAMPLES)
    for mask in gate.LINE_MASKS:
        for sample in MASK_SAMPLES[mask.name]:
            assert mask.apply(sample, [0]) != sample, sample
            assert gate._hits(mask, sample.lower()), sample
    key_value = gate.LINE_MASKS[-1]
    for name in gate._SECRET_NAMES.split(b"|"):
        for word in (re.sub(rb"\[_-\]\?|_\?", b"", name), re.sub(rb"\[_-\]\?|_\?", b"_", name)):
            for sample in (b"%s=x" % word, b"x '%s', 'y'" % word.upper(), b'"%s" : x' % word):
                assert key_value.apply(sample, [0]) != sample, sample
                assert gate._hits(key_value, sample.lower()), sample


def test_a_line_mask_without_a_hint_is_refused():
    with pytest.raises(ValueError):
        gate._line_mask("new", gate._sub(re.compile(rb"x"), b"*"))
    assert all(mask.words or mask.hint for mask in gate.LINE_MASKS)


@pytest.mark.parametrize("line", [
    b'"GET /signin?next=/ HTTP/1.1" 200 "https://example.test/page" "Mozilla/5.0 (X11; Linux x86_64) en-US"',
    b"ssh -p22 host; id -u; mail a@example.test; https://example.test/a@b; key rotation; en-us=1; --verbose",
    b"signal; tokens=1; passing; secretary: x; sigterm, pwdx: 1, keys: 2", b"user@host ssh://host:22/x",
])
def test_ordinary_lines_hit_no_mask(line):
    assert not gate._hinted_lines(line)
    assert gate._mask_lines(line, [0]) == line


# Lines for the random mixes below: secrets of every kind, near misses and unit-file continuations.
LINE_KINDS = [sample for samples in MASK_SAMPLES.values() for sample in samples] + [
    b"", b"\\", b"x", b"password=hunter2", b"PASSWORD = 'x'", b"key: wg-public", b"monkey=1", b"signature=abc&sig=1",
    b"GET /signin HTTP/1.1", b'"Mozilla/5.0 (X11; en-US)"', b'"https://example.test/page"', b"https://example.test/a@b",
    b"user@host ssh://host", b"Environment=A=1 \\", b"  B=two \\", b"  C=3", b"X=1 \\", b"ssh -p 22 host", b"-p",
    b"curl -u a:b https://x", b"sudo -u digi", b"bearer short", b"Bearer", b"bot123456789:" + b"A" * 35,
    b"https://u:p@h/ password=x Bearer abcdefgh123 -p pw", b"Environment=A=1 \\\nSetCredential=a:b", b"token",
]


def test_blind_is_the_line_masks_on_random_mixes(monkeypatch):
    rng = random.Random(9481)
    monkeypatch.setattr(gate, "BLIND_BLOCK", 16)
    for _ in range(400):
        lines = [rng.choice(LINE_KINDS) for _ in range(rng.randint(0, 12))]
        text = b"\n".join(lines) + rng.choice([b"", b"\n"])
        expected = gate._mask_lines(text, [0])
        assert gate.blind(text) == expected, text
        size = rng.randint(1, 40)
        assert b"".join(gate.blind_stream([text[i:i + size] for i in range(0, len(text), size)], [])) == expected


def test_blind_stream_masks_across_any_chunk_boundary(monkeypatch):
    monkeypatch.setattr(gate, "BLIND_BLOCK", 16)
    data = (b"filler line\n" * 5 + b"Environment=A=1 \\\n  SECRETNAME=rawvalue\n" + b"password=hunter2\n") * 3
    for size in (1, 7, 16, 40):
        chunks = [data[i:i + size] for i in range(0, len(data), size)]
        out = b"".join(gate.blind_stream(chunks, []))
        assert out == gate._mask_lines(data, [0]), size
        assert b"rawvalue" not in out and b"hunter2" not in out


def test_blind_stream_stops_at_an_endless_line(monkeypatch):
    monkeypatch.setattr(gate, "BLIND_BLOCK", 16)
    monkeypatch.setattr(gate, "BLIND_LINE_MAX", 64)
    errors = []
    out = b"".join(gate.blind_stream([b"ok\n"] + [b"x" * 10] * 20, errors))
    assert out == b"ok\n" and errors and "дальше файл не читается" in errors[0]


@pytest.mark.parametrize("path, referrer, agent", [
    (b"/api/v1/items?page=2&sort=asc", b"-", b"Mozilla/5.0 (X11; Linux x86_64)"),
    (b"/api/v1/items?page=2&sort=asc", b"https://example.test/catalog?page=1", b"Mozilla/5.0 (X11; Linux x86_64)"),
    (b"/api/v1/items?page=2&sort=asc", b"-", b"Mozilla/5.0 (X11; Linux x86_64; en-US)"),
    (b"/signin?next=/account", b"-", b"Mozilla/5.0 (X11; Linux x86_64)"),
])
def test_blind_speed_on_an_access_log_without_secrets(path, referrer, agent):
    # 8 MB of one kind of line: running the masks over all of it (3-6 MB/s) takes 1.4-2.7 s; a line holding no mask's
    # hint costs a search of the bytes (about 0.1 s here), under a second even on a slow runner
    line = b'198.51.100.7 - - [05/Oct/2026:23:01:00 +0000] "GET %s HTTP/1.1" 200 5123 "%s" "%s"\n' % (
        path, referrer, agent)
    data = line * (8 * MB // len(line))
    start = time.monotonic()
    assert gate.blind(data) == data
    assert time.monotonic() - start < 1.0


@WITH_GREP
def test_main_greps_the_masked_text_and_counts_what_it_hid(tmp_path, monkeypatch):
    # the whole path through main() on any system: the roots and the binary are this machine's
    monkeypatch.setattr(gate, "check_path", lambda path, need_content: None)
    monkeypatch.setattr(gate, "materialize", lambda plan: [GREP] + plan.argv[1:])
    log = tmp_path / "app.log"
    log.write_bytes(SECRETS)
    path = str(log).replace("\\", "/")
    code, out, _ = run('grep -F "Oct  6 02:3" %s' % path)
    assert code == 0 and "hunter2" not in out and "s3cr3tpw" not in out and "abcdefgh12345678" not in out
    assert "password=***" in out and "https://***:***@example.com/x" in out and "Bearer ***" in out
    assert out.endswith("[agent-ro-gate: скрыто секретов: 3]\n")
    assert run("grep -c -e 'password=h' %s" % path) == run("grep -c -e 'password=x' %s" % path) == (1, "0\n", "")


@POSIX
def test_grep_through_the_gate_cannot_probe_a_masked_value(roots):
    log, etc = roots
    (etc / "app.conf").write_bytes(CONF)
    (log / "access.log").write_bytes(b'GET /reset?token=Zq8xKw HTTP/1.1" 200\n')
    for path, right, wrong in ((etc / "app.conf", "^password *= *h", "^password *= *x"),
                               (log / "access.log", "token=Z", "token=Y")):
        for options in ("-c", "-l", "-v", "-n"):
            hit = run("grep %s -e '%s' %s" % (options, right, path))
            assert hit == run("grep %s -e '%s' %s" % (options, wrong, path)), (path, options)
            assert "hunter2" not in hit[1] and "Zq8xKw" not in hit[1]
    code, out, _ = run("grep -F -n 'password = ***' %s/app.conf" % etc)
    assert code == 0 and out.startswith("2:password = ***\n")


# --- journalctl -g: no entry the masks change is a result (review 26.11.2026) -----------------------

PY = sys.executable


def _journal(text: bytes, code: int = 0) -> list:
    """A stand-in for journalctl that prints text (the newest entry first, as with --reverse) and exits with code."""
    return [PY, "-c", "import sys; sys.stdout.buffer.write(%r); sys.stdout.flush(); sys.exit(%d)" % (text, code)]


NEWEST_FIRST = (b"Nov 26 10:05:00 host app[1]: request five\n"
                b"Nov 26 10:04:00 host app[1]: request four\n"
                b"Nov 26 10:03:00 host app[1]: request three password=hunter2\n"
                b"Nov 26 10:02:00 host app[1]: request two, a message\n"
                b"                                 of two lines: token=Zq8xKw\n"
                b"Nov 26 10:01:00 host app[1]: request one\n")
WITHOUT_SECRETS = b"".join(line for line in NEWEST_FIRST.splitlines(True)
                           if b"three" not in line and b"two" not in line and b"token" not in line)


@pytest.mark.parametrize("count,flip", [(2, True), (2, False), (3, True), (None, False)])
def test_journal_grep_answers_as_if_the_secret_entries_had_not_matched(count, flip):
    # journalctl matched the raw text; an entry the masks change is dropped, so its match tells nothing
    seen = gate.run_journal_grep(_journal(NEWEST_FIRST), count, flip, timeout=30)
    assert seen == gate.run_journal_grep(_journal(WITHOUT_SECRETS), count, flip, timeout=30)
    assert b"hunter2" not in seen.output and b"two" not in seen.output and b"of two lines" not in seen.output


def test_journal_grep_counts_its_own_window_and_turns_it_back():
    result = gate.run_journal_grep(_journal(NEWEST_FIRST), 3, True, timeout=30)
    assert result.output == (b"Nov 26 10:01:00 host app[1]: request one\n"
                             b"Nov 26 10:04:00 host app[1]: request four\n"
                             b"Nov 26 10:05:00 host app[1]: request five\n")
    assert result.returncode == 0 and not result.truncated and not result.timed_out
    assert gate.run_journal_grep(_journal(NEWEST_FIRST), 1, False, timeout=30).output == \
        b"Nov 26 10:05:00 host app[1]: request five\n"


def test_journal_grep_with_only_secret_matches_answers_like_no_match():
    secret = gate.run_journal_grep(_journal(b"Nov 26 10:03:00 host app[1]: password=hunter2\n"), 5, True, timeout=30)
    nothing = gate.run_journal_grep(_journal(b"", 1), 5, True, timeout=30)
    assert secret == nothing == gate.Result(1, b"", False, False)
    # -q keeps journalctl from saying "-- No entries --"; should a version say it anyway, it says nothing here
    said = gate.run_journal_grep(_journal(b"-- No entries --\n"), 5, True, timeout=30)
    assert said == gate.Result(1, b"", False, False)


def test_journal_grep_stops_journalctl_once_its_window_is_full():
    endless = [PY, "-c", "import sys\nwhile True: sys.stdout.write('Nov 26 10:00:00 host app[1]: x\\n')"]
    start = time.monotonic()
    result = gate.run_journal_grep(endless, 2, True, timeout=30)
    assert result.output.count(b"\n") == 2 and result.returncode == 0 and not result.timed_out
    assert time.monotonic() - start < 10


def test_journal_grep_keeps_journalctls_own_failure_and_its_output_limit():
    failed = gate.run_journal_grep(_journal(b"Failed to add filter for units: No data available\n", 3), None, False,
                                   timeout=30)
    assert failed.returncode == 3 and b"Failed to add filter" in failed.output
    big = gate.run_journal_grep(_journal(b"Nov 26 10:00:00 host app[1]: x\n" * 100), None, False, timeout=30,
                                limit=320)
    assert big.truncated and big.output == b"Nov 26 10:00:00 host app[1]: x\n" * 10


@pytest.mark.parametrize("args,expected", [
    (["-u", "app", "-g", "x"], (["-u", "app", "-g", "x", "-q"], None, False)),
    (["-u", "app", "-g", "x", "-n", "5"], (["-u", "app", "-g", "x", "-n", "5", "--lines=all", "--reverse", "-q"],
                                           5, True)),
    (["-r", "--grep=x", "-n5"], (["-r", "--grep=x", "-n5", "--lines=all", "--reverse", "-q"], 5, False)),
    (["-g", "x", "--lines=+3"], (["-g", "x", "--lines=+3", "--lines=all", "-q"], 3, False)),
    (["-xeu", "ssh", "-g", "x"], (["-xeu", "ssh", "-g", "x", "--lines=all", "--reverse", "-q"], 1000, True)),
    (["-g", "x", "-n", "all"], (["-g", "x", "-n", "all", "-q"], None, False)),
    (["-g", "x", "--lines"], (["-g", "x", "--lines", "--lines=all", "--reverse", "-q"], 10, True)),
    (["-g", "x", "-n", "5", "--", "_PID=1"], (["-g", "x", "-n", "5", "--lines=all", "--reverse", "-q", "--", "_PID=1"],
                                             5, True)),
])
def test_journal_grep_plan(args, expected):
    words, count, flip = expected
    assert gate.journal_grep(args) == (words, count, flip)


def test_journal_without_grep_runs_as_before():
    assert gate.journal_grep(["-u", "app", "-n", "5"]) is None


@pytest.mark.parametrize("raw", [
    "journalctl -u app -g x -o cat", "journalctl -g x --output=export", "journalctl -g x --show-cursor",
])
def test_journal_grep_refuses_outputs_it_cannot_split_into_entries(raw):
    with pytest.raises(gate.Deny, match="-g"):
        gate.check(raw)


@pytest.mark.parametrize("raw", [
    "journalctl -u app -o json", "journalctl -o json-pretty -n 5", "journalctl --output=json-sse",
    "journalctl -g x -o json-seq", "journalctl -o short -o json", "systemctl status app -o json",
    "systemctl status app --output=json-pretty",
])
def test_json_output_is_refused(raw):
    # review 26.11.2026: a message with a non-printable byte (a colour code) comes out as an array of numbers —
    # "password=x" as [112,97,…], which no mask reads
    with pytest.raises(gate.Deny, match="json"):
        gate.check(raw)


@pytest.mark.parametrize("raw", [
    "journalctl -u app -g x -o short-iso", "journalctl -g x --output=verbose", "journalctl -u app -o cat",
    "journalctl --show-cursor -n 5", "journalctl -u app -o export", "systemctl status app -o cat",
])
def test_journal_grep_outputs_allowed(raw):
    assert gate.check(raw).argv == shlex.split(raw)


def test_main_runs_journal_grep_through_the_gate(monkeypatch):
    monkeypatch.setattr(gate, "materialize", lambda plan: _journal(NEWEST_FIRST) if plan.argv[0] == "journalctl"
                        else None)
    seen = []
    real = gate.run_journal_grep
    monkeypatch.setattr(gate, "run_journal_grep", lambda command, count, flip, **kw: seen.append((count, flip)) or
                        real(command, count, flip, **kw))
    code, out, _ = run("journalctl -u app -g request -n 2")
    assert code == 0 and seen == [(2, True)]
    assert out == "Nov 26 10:04:00 host app[1]: request four\nNov 26 10:05:00 host app[1]: request five\n"


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
