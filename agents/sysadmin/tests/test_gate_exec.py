import importlib.util
import io
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from gate import agent_ro_gate as gate

GATE = Path(gate.__file__)
PY = sys.executable


# --- mysql -----------------------------------------------------------------------

def test_mysql_select_runs_with_agent_ro_defaults_and_time_limit():
    plan = gate.check("mysql -e \"SELECT COUNT(*) FROM app_parsing_queue WHERE status = 'failed'\"")
    assert plan.argv[:3] == [
        "mysql", "--defaults-file=/home/agent-ro/.my.cnf", "--init-command=SET SESSION MAX_EXECUTION_TIME=30000",
    ]
    assert plan.argv[-2:] == ["-e", "SELECT COUNT(*) FROM app_parsing_queue WHERE status = 'failed'"]


@pytest.mark.parametrize("raw", [
    'mysql -t -e "SHOW PROCESSLIST"',
    'mysql -e "SELECT id FROM t WHERE time > 30"',
    'mysql -D deliveryalertprod -e "EXPLAIN SELECT 1"',
    'mysql deliveryalertprod -N -B -e "DESCRIBE app_aggs"',
])
def test_mysql_reads_allowed(raw):
    assert gate.check(raw).argv[0] == "mysql"


@pytest.mark.parametrize("raw", [
    "mysql",
    'mysql -e "DELETE FROM t"',
    'mysql -e "UPDATE t SET a=1"',
    "mysql -e \"SELECT * INTO OUTFILE '/tmp/x' FROM t\"",
    'mysql -e "SELECT * FROM t FOR UPDATE"',
    'mysql -u root -e "SELECT 1"',
    'mysql -h 10.0.0.1 -e "SELECT 1"',
    'mysql --defaults-file=/root/.my.cnf -e "SELECT 1"',
    'mysql -e "SELECT 1" > /tmp/x',
    "mysql -e 'SELECT 1' -e 'DROP TABLE t'",
    'mysql "bad-db!" -e "SELECT 1"',
    "mysql -e 'SELECT 1 \\! cat /var/www/api.example.test/config.ini'",
    "mysql -e 'SELECT 1 \\T /tmp/x'",
    "mysql -e 'SELECT 1 \\. /tmp/x.sql'",
    "mysql -e \"SELECT load_file('/etc/shadow')\"",
    'mysql -e "SELECT sleep(100)"',
])
def test_mysql_writes_and_foreign_options_denied(raw):
    with pytest.raises(gate.Deny):
        gate.check(raw)


# --- root forms ------------------------------------------------------------------

def fake_which(name, path=None):
    return "/usr/bin/" + name


def test_sudoers_lines_are_exact_and_escaped():
    lines = gate.sudoers_lines(which=fake_which, digest=lambda path: "0" * 64)
    assert "agent-ro ALL=(root) NOPASSWD: /usr/bin/wg show" in lines
    assert "agent-ro ALL=(root) NOPASSWD: /usr/bin/dmesg -T --level\\=err\\,warn" in lines
    assert "agent-ro ALL=(root) NOPASSWD: /usr/bin/du -h --max-depth\\=1 /var/lib/mysql" in lines
    assert not any("*" in line for line in lines)
    assert len([line for line in lines if line.startswith("agent-ro ")]) == len(gate.SUDO_EXACT)


def test_sudoers_skips_commands_missing_on_server():
    # the digest is faked: otherwise the snapshot's line would be dropped by the read of
    # /usr/local/bin/agent-sec-snapshot, not by the command's absence
    def which(name, path=None):
        return None if name == "docker" else name if name.startswith("/") else "/usr/bin/" + name
    lines = gate.sudoers_lines(which=which, digest=lambda path: "0" * 64)
    assert not any("docker" in line for line in lines)
    assert len([ln for ln in lines if ln.startswith("agent-ro ")]) == \
        len([f for f in gate.SUDO_EXACT if f[0] != "docker"])
    assert any(ln.endswith("sha256:" + "0" * 64 + " /usr/local/bin/agent-sec-snapshot \"\"") for ln in lines)


def test_materialize_resolves_paths_and_prefixes_sudo():
    which = {"sudo": "/usr/bin/sudo", "wg": "/usr/bin/wg", "uptime": "/usr/bin/uptime"}.get
    assert gate.materialize(gate.Plan(["wg", "show"], True), which=lambda n, path=None: which(n)) == [
        "/usr/bin/sudo", "-n", "/usr/bin/wg", "show"]
    assert gate.materialize(gate.Plan(["uptime"], False), which=lambda n, path=None: which(n)) == ["/usr/bin/uptime"]
    with pytest.raises(gate.Deny, match="нет команды"):
        gate.materialize(gate.Plan(["needs-restarting", "-r"], False), which=lambda n, path=None: which(n))


def test_root_only_commands_list_their_exact_forms():
    with pytest.raises(gate.Deny, match="wg show all latest-handshakes"):
        gate.check("wg show wg0 private-key")


# --- execution ------------------------------------------------------------------

def test_run_limited_returns_output_and_code():
    result = gate.run_limited([PY, "-c", "import sys; print('ok'); sys.exit(3)"], timeout=30)
    assert result.returncode == 3 and result.output.strip() == b"ok"
    assert not result.truncated and not result.timed_out


def test_run_limited_cuts_endless_output_without_hanging():
    started = time.monotonic()
    result = gate.run_limited([PY, "-c", "import sys\nwhile True: sys.stdout.write('x' * 65536)"],
                              timeout=30, limit=100000)
    assert result.truncated and len(result.output) == 100000
    assert time.monotonic() - started < 20


def test_run_limited_kills_on_timeout():
    started = time.monotonic()
    result = gate.run_limited([PY, "-c", "import time; time.sleep(60)"], timeout=1)
    assert result.timed_out
    assert time.monotonic() - started < 15


def test_run_limited_can_cut_output_without_killing_the_command():
    code = "import sys; sys.stdout.write('x' * 300000); sys.stdout.flush(); print('done'); sys.exit(4)"
    result = gate.run_limited([PY, "-c", code], timeout=30, limit=1000, kill_on_limit=False)
    assert result.truncated and len(result.output) == 1000
    assert result.returncode == 4 and not result.timed_out  # the command ran to the end


POSIX = pytest.mark.skipif(os.name != "posix", reason="сигналы и наследование труб — POSIX")


@POSIX
def test_run_limited_sigkills_process_that_ignores_sigterm():
    code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    started = time.monotonic()
    result = gate.run_limited([PY, "-c", code], timeout=1)
    assert result.timed_out and result.returncode == -9
    assert time.monotonic() - started < 15


@POSIX
def test_run_limited_does_not_hang_on_grandchild_holding_the_pipe():
    code = "import subprocess; subprocess.Popen(['sleep', '30']); print('parent done')"
    started = time.monotonic()
    result = gate.run_limited([PY, "-c", code], timeout=30)
    assert b"parent done" in result.output and result.returncode == 0
    assert time.monotonic() - started < 10


def run_main(raw, monkeypatch, result=None):
    out, err, calls = io.BytesIO(), io.BytesIO(), []
    monkeypatch.setattr(gate, "materialize", lambda plan, which=None: ["/usr/bin/" + plan.argv[0]] + plan.argv[1:])
    monkeypatch.setattr(
        gate, "run_limited",
        lambda argv, timeout=gate.TIMEOUT, limit=gate.LIMIT, env=None:
            calls.append((argv, env)) or (result or gate.Result(0, b"up 3 days\n", False, False)))
    code = gate.main([], {"SSH_ORIGINAL_COMMAND": raw}, out, err)
    return code, out.getvalue(), err.getvalue().decode("utf-8"), calls


def test_main_runs_allowed_command_without_shell(monkeypatch):
    code, out, err, calls = run_main("uptime", monkeypatch)
    assert code == 0 and out == b"up 3 days\n" and err == ""
    assert calls == [(["/usr/bin/uptime"], gate.CHILD_ENV)]


def test_main_denies_with_126_and_reason(monkeypatch):
    code, out, err, calls = run_main("cat /etc/shadow", monkeypatch)
    assert code == 126 and calls == [] and "отказ" in err


def test_main_without_command_is_denied(monkeypatch):
    code, _, _, calls = run_main("", monkeypatch)
    assert code == 126 and calls == []


def test_main_survives_undecodable_bytes_from_c_locale(monkeypatch):
    # this is how Python 3.6 with the C locale passes Cyrillic from SSH_ORIGINAL_COMMAND
    code, _, err, calls = run_main("cat /root/\udcd0\udcb0", monkeypatch)
    assert code == 126 and calls == [] and "отказ" in err


def test_main_reports_truncation_and_timeout(monkeypatch):
    code, out, _, _ = run_main("uptime", monkeypatch, gate.Result(-15, b"x" * 10, True, False))
    assert code == 0 and "обрезан".encode() in out
    code, out, _, _ = run_main("uptime", monkeypatch, gate.Result(-9, b"", False, True))
    assert code == 124 and "таймаут".encode() in out


def test_sudoers_mode_prints_lines(monkeypatch):
    out = io.BytesIO()
    monkeypatch.setattr(gate, "sudoers_lines", lambda which=None: ["# x", "agent-ro ALL=(root) NOPASSWD: /usr/bin/wg show"])
    assert gate.main(["--sudoers"], {}, out, io.BytesIO()) == 0
    assert out.getvalue().decode().endswith("/usr/bin/wg show\n")


VERMIN = "import sys; from vermin.main import main; sys.argv[0] = 'vermin'; main()"  # `-m vermin` is not supported


@pytest.mark.skipif(importlib.util.find_spec("vermin") is None, reason="vermin не установлен (requirements-dev.txt)")
def test_gate_source_is_python36_compatible():
    proc = subprocess.run([PY, "-c", VERMIN, "--no-tips", str(GATE)], capture_output=True, text=True)
    match = re.search(r"Minimum required versions:.*?3\.(\d+)", proc.stdout)
    assert match and int(match.group(1)) <= 6, proc.stdout + proc.stderr


# --- mysql_sql / the exception for < and > (the reviewer's additions) -------------------------

@pytest.mark.parametrize("args,expected", [
    (["-e", "select 1"], "select 1"),
    (["-eselect 1"], "select 1"),
    (["--execute=select 1"], "select 1"),
    (["--execute", "select 1"], "select 1"),
    (["-e"], ""),
    (["--execute"], ""),
    (["-t"], None),
])
def test_mysql_sql_forms_agree_with_denylist(args, expected):
    assert gate.mysql_sql(args) == expected


@pytest.mark.parametrize("raw", [
    "mysql -e 'select 1>0'",
    "mysql -e'select 1>0'",
    "mysql --execute='select 1>0'",
    "mysql --execute 'select 1>0'",
    "mysql -e 'select 1<2'",
])
def test_redirect_chars_allowed_in_sql_whatever_the_form(raw):
    assert gate.check(raw).argv[0] == "mysql"


@pytest.mark.parametrize("raw", [
    "mysql -e 'select 1' > /tmp/x",
    "mysql -e 'select 1' >/tmp/x",
    "mysql --execute='select 1' >/tmp/x",
    "mysql -e'select 1' >/tmp/x",
    "mysql -e 'select 1>0' -D 'a>b'",
    "mysql -e",
])
def test_redirect_chars_outside_sql_denied(raw):
    with pytest.raises(gate.Deny):
        gate.check(raw)


# --- fix round 1: comments in SQL, the process group ------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT sleep/**/(5)", 'SELECT 1 INTO/**/OUTFILE "x"', "SELECT /*+ MAX_EXECUTION_TIME(0) */ 1",
    "SELECT /*!50000 sleep(5) */", "SELECT 1 # x", "SELECT 1 -- x", "SELECT 1 --",
])
def test_mysql_comments_denied(sql):
    with pytest.raises(gate.Deny, match="комментар"):
        gate.check("mysql -e '%s'" % sql)


@pytest.mark.parametrize("sql", ["SELECT 1", "SHOW TABLES", "SELECT a-b FROM t", "SELECT a--b FROM t"])
def test_mysql_plain_sql_allowed(sql):
    assert gate.check("mysql -e '%s'" % sql).argv[0] == "mysql"


@POSIX
def test_run_limited_kills_grandchildren_on_timeout(tmp_path):
    pidfile = tmp_path / "pid"
    code = ("import subprocess, sys, time\n"
            "p = subprocess.Popen(['sleep', '60'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "open(%r, 'w').write(str(p.pid))\n"
            "time.sleep(60)" % str(pidfile))
    result = gate.run_limited([PY, "-c", code], timeout=2)
    assert result.timed_out
    pid = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(pid, 9)
        pytest.fail("внук пережил run_limited")
