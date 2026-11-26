"""kd-host-read: the host reader's argv forms, journal filter and file reads."""

from __future__ import annotations

import gzip
import io
import json
import os
import sys
from pathlib import Path

import pytest

from kd import host_read
from kd.host_read import Catalog

ROOT = Path(__file__).resolve().parents[2]
POSIX = pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW") or sys.platform == "win32", reason="Linux file reads")


def _catalog(roots=None) -> Catalog:
    return host_read.parse_catalog(
        {
            "schema_version": 1,
            "units": ["digipaddock-cabinet-push.service", "digipaddock-release.service", "nginx.service"],
            "full_journal": ["sysadmin"],
            "roots": roots if roots is not None else {"attention": "/var/lib/digipaddock/attention"},
            "private_comm": ["sudo", "su", "runuser", "sshd"],
            "private_identifiers": ["sudo", "su", "runuser", "sshd"],
            "audit_identifiers": ["kd-permission"],
        }
    )


def _entry(msg, unit="digipaddock-cabinet-push.service", transport="stdout", comm="python3", ident=None, us=1_761_100_000_000_000):
    entry = {
        "__REALTIME_TIMESTAMP": str(us),
        "_SYSTEMD_UNIT": unit,
        "_TRANSPORT": transport,
        "_COMM": comm,
        "_PID": "42",
        "MESSAGE": msg,
    }
    if ident:
        entry["SYSLOG_IDENTIFIER"] = ident
    return json.dumps(entry)


def _run(argv, *, agent="dev", catalog=None, lines=(), command=None, disabled=frozenset()):
    out, seen = io.StringIO(), []

    def journal(cmd):
        seen.append(cmd)
        return iter(lines)

    def run(cmd):
        seen.append(cmd)
        return command(cmd) if command else (0, b"ok\n", b"")

    rc = host_read.main(argv, agent=agent, catalog=catalog or _catalog(), disabled=disabled, journal=journal, command=run, out=out)
    return rc, out.getvalue(), seen


def test_catalog_in_the_repository_loads() -> None:
    catalog = host_read.load_catalog(ROOT / "common/coordination/host-read.json")
    assert "digipaddock-cabinet-push.service" in catalog.units
    assert "kd-permission" in catalog.audit_identifiers
    assert not any(u.startswith(("hindsight", "digipaddock-monitor")) for u in catalog.units)


@pytest.mark.parametrize(
    "root",
    ["relative/path", "/var/lib/digipaddock/*", "/var/lib/digipaddock/attention/", "/var/lib/../etc",
     "/var/lib/digipaddock", "/var/lib/digipaddock/admin-grants", "/etc/digipaddock", "/var/log",
     "/var/log/journal/x", "/home/digi-dev", "/var/lib/digipaddock/boss-promotions",
     "/var/lib/digipaddock/boss-promotions/decisions"],
)
def test_catalog_rejects_relative_glob_or_closed_root(root) -> None:
    with pytest.raises(ValueError):
        _catalog(roots={"x": root})


@pytest.mark.parametrize("name", ["", "Nginx", "a/b", "-x", "a b"])
def test_catalog_rejects_a_bad_root_name(name) -> None:
    with pytest.raises(ValueError):
        _catalog(roots={name: "/var/log/nginx/board"})


def test_journal_builds_trusted_filter() -> None:
    rc, _, seen = _run(["journalctl", "-u", "digipaddock-cabinet-push", "-n", "20", "--since", "-1h"])
    assert rc == 0
    (cmd,) = seen
    assert cmd[:2] == ["/usr/bin/journalctl", "--no-pager"]
    assert "-o" in cmd and cmd[cmd.index("-o") + 1] == "json" and "-r" in cmd
    assert "--since=-1h" in cmd
    tail = cmd[cmd.index("_SYSTEMD_UNIT=digipaddock-cabinet-push.service"):]
    assert tail == ["_SYSTEMD_UNIT=digipaddock-cabinet-push.service", "+", "UNIT=digipaddock-cabinet-push.service", "_PID=1"]
    assert not any(a.startswith(("-n", "--lines")) for a in cmd)  # the executor counts what it keeps


def test_journal_prints_kept_entries_oldest_first() -> None:
    lines = [_entry("second", us=2_000_000), _entry("first", us=1_000_000)]  # journalctl -r: newest first
    rc, out, _ = _run(["journalctl", "-u", "digipaddock-cabinet-push.service", "-o", "cat"], lines=lines)
    assert rc == 0 and out == "first\nsecond\n"


def test_journal_limits_kept_entries() -> None:
    lines = [_entry(f"m{i}") for i in range(10)]
    _, out, _ = _run(["journalctl", "-u", "digipaddock-cabinet-push", "-n", "3", "-o", "cat"], lines=lines)
    assert out.splitlines() == ["m2", "m1", "m0"]


def test_journal_drops_pam_under_public_unit() -> None:
    lines = [
        _entry("pam_unix(runuser:session): session opened for user digi", unit="digipaddock-release.service", transport="syslog", comm="runuser", ident="runuser"),
        _entry("released 0e5295f", unit="digipaddock-release.service", transport="stdout", comm="runuser"),
        _entry("COMMAND=/bin/true", unit="digipaddock-release.service", transport="syslog", comm="sudo", ident="fake"),
        _entry("spoofed", unit="digipaddock-release.service", transport="syslog", comm="python3", ident="sshd"),
    ]
    _, out, _ = _run(["journalctl", "-u", "digipaddock-release", "-o", "cat"], lines=lines)
    assert out == "released 0e5295f\n"


def test_journal_drops_kd_permission_for_sysadmin() -> None:
    lines = [_entry("tool Bash cat /x", unit="init.scope", transport="syslog", comm="python3", ident="kd-permission"),
             _entry("Accepted publickey", unit="ssh.service", transport="syslog", comm="sshd", ident="sshd"),
             _entry("kernel line", unit="", transport="kernel", comm="")]
    _, out, seen = _run(["journalctl", "-o", "cat"], agent="sysadmin", lines=lines)
    # the private audit is dropped for everyone; sysadmin keeps the authentication records (spec 5, item 15);
    # the input is newest first, the output oldest first
    assert out == "kernel line\nAccepted publickey\n"
    assert not any("=" in a and a.startswith(("_SYSTEMD_UNIT", "UNIT")) for a in seen[0])


def test_journal_sysadmin_full_without_unit() -> None:
    rc, _, seen = _run(["journalctl", "-u", "ssh.service", "-n", "5"], agent="sysadmin")
    assert rc == 0 and "_SYSTEMD_UNIT=ssh.service" in seen[0]


@pytest.mark.parametrize("unit", ["digipaddock-monitor", "hindsight-worker", "digipaddock-cap-dev-base@1.service", "digipaddock-cabinet*", "-kd"])
def test_journal_unit_not_listed(unit) -> None:
    rc, out, seen = _run(["journalctl", "-u", unit])
    assert rc in (host_read.EXIT_DENIED, host_read.EXIT_USAGE) and not seen
    assert "--units" in out or "usage" in out


def test_journal_needs_a_unit_for_an_ordinary_agent() -> None:
    rc, out, seen = _run(["journalctl", "-n", "5"])
    assert rc == host_read.EXIT_DENIED and not seen and "--units" in out


@pytest.mark.parametrize(
    "extra",
    [["+"], ["_COMM=sshd"], ["--file=/var/log/journal/x"], ["-D", "/tmp"], ["--directory=/tmp"], ["--namespace=x"],
     ["-f"], ["--follow"], ["-m"], ["--root=/"], ["--image=x"], ["-o", "json"], ["-o", "export"], ["--output=verbose"],
     ["-n", "0"], ["-n", "99999"], ["-p", "9"], ["--since", "$(id)"], ["-k"], ["--user"], ["-M", "x"]],
)
def test_journal_refuses_forms_outside_the_filter(extra) -> None:
    rc, _, seen = _run(["journalctl", "-u", "digipaddock-cabinet-push", *extra])
    assert rc == host_read.EXIT_USAGE and not seen


def test_journal_units_lists_the_catalog() -> None:
    rc, out, seen = _run(["journalctl", "--units"])
    assert rc == 0 and not seen and "digipaddock-cabinet-push.service" in out.splitlines()


def test_systemctl_status_forces_lines0() -> None:
    rc, _, seen = _run(["systemctl", "status", "digipaddock-cabinet-push"])
    assert rc == 0
    (cmd,) = seen
    assert cmd[0] == "/usr/bin/systemctl" and "--no-pager" in cmd and "--lines=0" in cmd and "--no-ask-password" in cmd
    assert cmd[-1] == "digipaddock-cabinet-push"


@pytest.mark.parametrize(
    "argv",
    [["restart", "nginx"], ["start", "x"], ["daemon-reload"], ["--user", "status"], ["status", "--user"],
     ["status", "-H", "h"], ["status", "--host=h"], ["status", "-M", "m"], ["status", "--root=/"],
     ["status", "-n", "50", "x"], ["status", "--lines=50", "x"], ["edit", "x"], ["status", "x;id"], []],
)
def test_systemctl_refuses_actions_and_transports(argv) -> None:
    rc, _, seen = _run(["systemctl", *argv])
    assert rc == host_read.EXIT_USAGE and not seen


def test_systemctl_list_forms() -> None:
    rc, _, seen = _run(["systemctl", "list-units", "--failed", "--no-legend", "digipaddock-*"])
    assert rc == 0 and seen[0][-5:] == ["list-units", "--failed", "--no-legend", "--", "digipaddock-*"]


def test_sys_reads_are_fixed() -> None:
    rc, _, seen = _run(["sys", "ps"])
    assert rc == 0 and seen[0][0].endswith("/ps") and "args" not in " ".join(seen[0])
    rc, _, seen = _run(["sys", "ss", "-K"])
    assert rc == host_read.EXIT_USAGE and not seen


def test_output_capped() -> None:
    big = b"x" * (host_read.LIMIT_BYTES + 10)
    rc, out, _ = _run(["systemctl", "cat", "nginx"], command=lambda cmd: (0, big, b""))
    assert rc == 0 and len(out.encode()) <= host_read.LIMIT_BYTES + 100 and out.endswith("… truncated (1 MiB)\n")


def test_journal_output_capped_stops_reading() -> None:
    lines = (_entry("y" * 1000) for _ in range(5000))
    rc, out, _ = _run(["journalctl", "-u", "digipaddock-cabinet-push", "-n", "5000", "-o", "cat"], lines=lines)
    assert rc == 0 and out.startswith(host_read.CUT_EARLIER)


def test_disabled_agent_refused() -> None:
    rc, out, seen = _run(["sys", "uptime"], disabled=frozenset({"dev"}))
    assert rc == host_read.EXIT_DENIED and not seen and "switched off" in out


def test_no_agent_refused() -> None:
    rc, _, seen = _run(["sys", "uptime"], agent="")
    assert rc == host_read.EXIT_DENIED and not seen


def test_unknown_command_is_usage() -> None:
    rc, out, _ = _run(["find", "/"])
    assert rc == host_read.EXIT_USAGE and "usage" in out


# --- files (Linux: O_NOFOLLOW, /proc/self/fd) -------------------------------------------------


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "attention"
    root.mkdir()
    (root / "a.json").write_text('{"card": "KD-1"}\nsecond\nthird\n')
    with gzip.open(root / "old.log.1.gz", "wt") as f:
        f.write("gz one\ngz two\n")
    secret = tmp_path / "push.env"
    secret.write_text("KEY=secret\n")
    return tmp_path, root, secret


def _files(argv, root):
    return _run(argv, catalog=_catalog(roots={"attention": str(root)}))


@POSIX
def test_file_cat_head_tail_grep(tree) -> None:
    _, root, _ = tree
    assert _files(["cat", str(root / "a.json")], root)[1] == '{"card": "KD-1"}\nsecond\nthird\n'
    assert _files(["head", "-n", "1", str(root / "a.json")], root)[1] == '{"card": "KD-1"}\n'
    assert _files(["tail", "-n", "2", str(root / "a.json")], root)[1] == "second\nthird\n"
    assert _files(["grep", "-n", "sec", str(root / "a.json")], root)[1] == "2:second\n"
    assert _files(["grep", "-c", "-i", "THIRD", str(root / "a.json")], root)[1] == "1\n"
    rc, out, _ = _files(["ls", str(root)], root)
    assert rc == 0 and out.split() == ["a.json", "old.log.1.gz"]


@POSIX
def test_file_gz(tree) -> None:
    _, root, _ = tree
    assert _files(["cat", str(root / "old.log.1.gz")], root)[1] == "gz one\ngz two\n"
    assert _files(["grep", "two", str(root / "old.log.1.gz")], root)[1] == "gz two\n"


@POSIX
def test_file_symlink_out_refused(tree) -> None:
    _, root, secret = tree
    (root / "link").symlink_to(secret)
    rc, out, _ = _files(["cat", str(root / "link")], root)
    assert rc == host_read.EXIT_DENIED and "secret" not in out


@POSIX
def test_file_dotdot_refused(tree) -> None:
    _, root, _ = tree
    rc, out, _ = _files(["cat", f"{root}/../push.env"], root)
    assert rc == host_read.EXIT_DENIED and "secret" not in out


@POSIX
def test_file_device_fifo_refused(tree) -> None:
    _, root, _ = tree
    os.mkfifo(root / "fifo")
    rc, _, _ = _files(["cat", str(root / "fifo")], root)
    assert rc == host_read.EXIT_DENIED


@POSIX
def test_file_relative_path_is_usage(tree) -> None:
    _, root, _ = tree
    rc, _, _ = _files(["cat", "a.json"], root)
    assert rc == host_read.EXIT_USAGE


# --- final review findings ------------------------------------------------------------------

STATUS = (
    "● nginx.service - A high performance web server\n"
    "     Loaded: loaded (/usr/lib/systemd/system/nginx.service; enabled; preset: enabled)\n"
    "     Active: active (running) since Wed 2026-10-22 10:00:00 UTC; 1h ago\n"
    "   Main PID: 1234 (nginx)\n"
    "     CGroup: /system.slice/nginx.service\n"
    "             ├─1234 \"nginx: master process /usr/sbin/nginx\"\n"
    "             └─1235 \"mysql -psecret1\"\n"
    "\n"
    "● session-5.scope - Session 5 of User root\n"
    "     Loaded: loaded (/run/systemd/transient/session-5.scope; transient)\n"
    "     CGroup: /user.slice/user-0.slice/session-5.scope\n"
    "             │ └─998 \"bash\"\n"
    "             └─999 \"mysql -psecret2\"\n"
)


def test_systemctl_status_drops_the_process_tree() -> None:
    rc, out, _ = _run(["systemctl", "status", "nginx", "session-5.scope"], command=lambda cmd: (0, STATUS.encode(), b""))
    assert rc == 0 and "secret" not in out and "bash" not in out
    assert "   Main PID: 1234 (nginx)\n" in out and "● session-5.scope - Session 5 of User root\n" in out
    assert "     CGroup: /system.slice/nginx.service (processes not shown)\n" in out


def test_journal_time_limit_is_an_error_with_what_was_read() -> None:
    def slow():
        yield _entry("newest")
        yield _entry("older")
        raise host_read.TimedOut()

    out = io.StringIO()
    rc = host_read.main(["journalctl", "-u", "digipaddock-cabinet-push", "-o", "cat"], agent="dev", catalog=_catalog(),
                        journal=lambda cmd: slow(), out=out)
    assert rc == host_read.EXIT_ERROR
    assert out.getvalue() == f"older\nnewest\n… timed out after {host_read.TIMEOUT} s: narrow the read (--since, -n, -g)\n"


@POSIX
def test_journal_source_raises_when_the_time_is_up(monkeypatch) -> None:
    monkeypatch.setattr(host_read, "TIMEOUT", 0.5)
    lines = []
    with pytest.raises(host_read.TimedOut):
        for line in host_read.journal_source(["/bin/sh", "-c", "echo a; sleep 30"]):
            lines.append(line)
    assert lines == ["a\n"]


def test_journal_cut_keeps_the_newest_entries() -> None:
    lines = (_entry(f"m{i:04d} " + "y" * 1000) for i in range(3000))  # newest first
    rc, out, _ = _run(["journalctl", "-u", "digipaddock-cabinet-push", "-n", "5000", "-o", "cat"], lines=lines)
    assert rc == 0 and len(out.encode()) <= host_read.LIMIT_BYTES
    assert out.startswith("… earlier entries cut (1 MiB)\n") and out.splitlines()[-1].startswith("m0000 ")
    rc, out, _ = _run(["journalctl", "-u", "digipaddock-cabinet-push", "-n", "5000", "-o", "cat", "-r"],
                      lines=(_entry(f"m{i:04d} " + "y" * 1000) for i in range(3000)))
    assert out.splitlines()[0].startswith("m0000 ") and out.endswith("… older entries cut (1 MiB)\n")


def test_journal_asks_for_whole_fields() -> None:
    _, _, seen = _run(["journalctl", "-u", "digipaddock-cabinet-push"])
    assert "--all" in seen[0]


@POSIX
def test_a_broken_gz_is_one_line_not_a_traceback(tree) -> None:
    _, root, _ = tree
    (root / "bad.log.2.gz").write_bytes(b"\x1f\x8b\x08\x00broken")
    rc, out, _ = _files(["cat", str(root / "bad.log.2.gz")], root)
    assert rc == host_read.EXIT_ERROR and out.startswith("kd-host-read: ") and "Traceback" not in out


@POSIX
def test_grep_takes_a_pattern_after_e(tree) -> None:
    _, root, _ = tree
    (root / "dash.log").write_text("a -x flag\nplain\n")
    assert _files(["grep", "-e", "-x", str(root / "dash.log")], root)[1] == "a -x flag\n"


# Seen on the factory host: Claude Code checks an absolute path argument of any unknown command against the Read
# rules and its working directories and asks (dontAsk: refuses) even after the guard's allow; a named root is a
# relative token it lets through.


@POSIX
def test_a_path_by_root_name(tree) -> None:
    _, root, _ = tree
    assert _files(["cat", "attention/a.json"], root)[1] == '{"card": "KD-1"}\nsecond\nthird\n'
    rc, out, _ = _files(["ls", "attention"], root)
    assert rc == 0 and out.split() == ["a.json", "old.log.1.gz"]
    assert _files(["grep", "two", "attention/old.log.1.gz"], root)[1] == "gz two\n"


@POSIX
def test_a_root_name_never_leaves_its_root(tree) -> None:
    _, root, _ = tree
    rc, out, _ = _files(["cat", "attention/../push.env"], root)
    assert rc == host_read.EXIT_DENIED and "secret" not in out
    (root / "link").symlink_to(tree[2])
    rc, out, _ = _files(["cat", "attention/link"], root)
    assert rc == host_read.EXIT_DENIED and "secret" not in out


def test_an_unknown_root_name_is_usage() -> None:
    rc, out, _ = _run(["cat", "nope/x.log"])
    assert rc == host_read.EXIT_USAGE and "kd-host-read paths" in out


def test_paths_lists_names_and_paths() -> None:
    rc, out, _ = _run(["paths"])
    assert rc == 0 and out == "attention  /var/lib/digipaddock/attention\n"


# --- the read gateway's mask guards (KD-9481) and the host reader (KD-9483) ---------------------------
# The gateway refuses forms that reshape text past its masks. kd-host-read has no masks: what it may reach holds no
# secret by its catalog, so those refusals guard nothing here. What these tests pin is the part that still matters:
# the forms that would reach further stay refused, and a -g search answers nothing about a record it drops.


@pytest.mark.parametrize(
    "argv",
    [["systemctl", "show", "-P", "MainPID", "nginx"], ["cat", "-v", "attention/a.json"], ["cat", "-A", "attention/x"],
     ["journalctl", "-u", "nginx", "-o", "json-pretty"], ["journalctl", "-u", "nginx", "--output=json-sse"],
     ["sys", "ps", "-ww"], ["sys", "ps", "--cols=999"]],
)
def test_the_gateways_reshaping_forms_are_not_forms_of_the_host_reader(argv) -> None:
    rc, _, seen = _run(argv)
    assert rc == host_read.EXIT_USAGE and not seen


def test_systemctl_value_prints_what_show_prints_with_names() -> None:
    # no mask keys on the property's name here, so dropping the name hides nothing more and shows nothing more
    rc, _, seen = _run(["systemctl", "show", "--value", "-p", "MainPID", "nginx"])
    assert rc == 0 and seen[0][-4:] == ["--value", "--property=MainPID", "--", "nginx"]


def test_journal_grep_says_nothing_about_a_dropped_record() -> None:
    # journalctl matches -g; the reader drops private and audit records before it counts -n, and its exit code is 0
    # whatever matched: a private record that matched answers as if nothing had
    private = _entry("COMMAND=/bin/true", unit="digipaddock-release.service", transport="syslog", comm="sudo")
    audit = _entry("tool Bash x", unit="digipaddock-release.service", transport="syslog", ident="kd-permission")
    public = _entry("released x", unit="digipaddock-release.service", us=1)
    argv = ["journalctl", "-u", "digipaddock-release", "-g", "x", "-n", "1", "-o", "cat"]
    assert _run(argv, lines=[private, audit, public])[:2] == (0, "released x\n")
    assert _run(argv, lines=[private, audit])[:2] == _run(argv, lines=[])[:2] == (0, "")
