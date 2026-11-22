import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gate import agent_hindsight_usage as hu

SOURCE = Path(hu.__file__)
VERMIN = "import sys; from vermin.main import main; sys.argv[0] = 'vermin'; main()"
NOW = datetime(2026, 10, 21, 7, 0, tzinfo=timezone.utc)  # 10:00 UTC3
SECRET = "TRANSCRIPT-TEXT-MUST-NOT-LEAK"
MODEL = "claude-haiku-4-5-20251001"


def answer(ts, mid, rid, out, inp=10, cw=5, cr=100, session="s1", model=MODEL):
    return json.dumps({"type": "assistant", "timestamp": ts, "requestId": rid, "sessionId": session,
                       "message": {"id": mid, "model": model, "content": [{"type": "text", "text": SECRET}],
                                   "usage": {"input_tokens": inp, "output_tokens": out,
                                             "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr}}})


def private(root, unit, lines, mtime=None, name="x1"):
    """A unit PrivateTmp as systemd makes it, with one provider config dir and one transcript."""
    outer = root / ("systemd-private-boot-%s-%s" % (unit, name))
    projects = outer / "tmp" / "hindsight-claude-code-abc" / "projects" / "-opt-hindsight"
    projects.mkdir(parents=True)
    (projects / "s.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(str(outer), 0o700)
    if mtime is not None:
        os.utime(str(outer), (mtime, mtime))
    return outer


@pytest.fixture(autouse=True)
def utc3(monkeypatch):
    """The days here are of a factory host in UTC+3 (Etc/GMT-3) (target_setup.sh --timezone writes ZONE)."""
    monkeypatch.setattr(hu, "ZONE", "Etc/GMT-3")


@pytest.fixture
def tmp_root(tmp_path):
    root = tmp_path / "tmp"
    root.mkdir()
    return root


def render(tmp_root, ledger=None):
    return hu.render(str(tmp_root), str(ledger or tmp_root / "no-ledger"), now=NOW, owner=os.getuid())


posix_tz = pytest.mark.skipif(not hasattr(time, "tzset"), reason="TZ and tzset: POSIX only, as the targets")


@posix_tz
def test_host_day():
    assert hu.host_day("2026-10-20T20:59:59.123Z") == "2026-10-20"
    assert hu.host_day("2026-10-20T21:00:00Z") == "2026-10-21"
    assert hu.host_day("2026-10-20T21:00:00") == "2026-10-21"  # no zone — UTC
    assert hu.host_day("2026-10-21T00:30:00+03:00") == "2026-10-21"
    assert hu.host_day("2026-10-20T23:30:00-0100") == "2026-10-21"
    for bad in ("", "yesterday", None, 5, "2026-13-01T00:00:00Z"):
        assert hu.host_day(bad) is None


@posix_tz
def test_a_day_is_of_the_zone_target_setup_wrote(monkeypatch):
    # the factory host's zone (host.json timezone), as kd-usage counts its days; the process's TZ stays
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    monkeypatch.setattr(hu, "ZONE", "UTC")
    assert hu.host_day("2026-10-20T21:00:00Z") == "2026-10-20"
    monkeypatch.setattr(hu, "ZONE", "America/New_York")  # summer time: UTC-4 until 01.11.2026
    assert hu.host_day("2026-10-21T03:59:59Z") == "2026-10-20"
    assert hu.host_day("2026-10-21T04:00:00Z") == "2026-10-21"
    assert os.environ["TZ"] == "Asia/Tokyo"
    # the one line target_setup.sh rewrites
    assert len(re.findall(r'^ZONE = "UTC"  #', Path(hu.__file__).read_text("utf-8"), re.M)) == 1


def test_units_sources_and_total_without_text(tmp_root):
    started = datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc).timestamp()
    private(tmp_root, "hindsight-api.service", [
        answer("2026-10-20T10:00:00Z", "m1", "r1", out=3),
        answer("2026-10-20T10:00:01Z", "m1", "r1", out=40),  # streaming: the same call, fuller output
        answer("2026-10-20T22:00:00Z", "m2", "r2", out=7, session="s2"),  # 01:00 UTC3 — the next day
        '{"type": "user", "message": {"content": "%s"}}' % SECRET,
        "not json",
    ], mtime=started)
    private(tmp_root, "hindsight-worker.service", [
        answer("2026-10-20T11:00:00Z", "m3", "r3", out=50, session="s3"),
        answer("2026-10-20T10:00:02Z", "m1", "r1", out=40),  # the same call seen by both: counted once in total
    ], mtime=started)
    out = render(tmp_root)
    assert SECRET not in out
    lines = out.splitlines()
    assert lines[0] == hu.VERSION
    assert lines[1] == "now\t2026-10-21T07:00:00Z\tdays are Etc/GMT-3"
    api = [ln for ln in lines if ln.startswith("unit\thindsight-api.service\t")]
    assert len(api) == 1 and "started=2026-10-20T00:00:00Z jsonl=1 bytes=" in api[0] and "newest=20" in api[0]
    assert ("source\t2026-10-20 hindsight-api.service %s\trequests=1 sessions=1 input=10 output=40 cache_write=5 "
            "cache_read=100" % MODEL) in lines
    assert ("source\t2026-10-21 hindsight-api.service %s\trequests=1 sessions=1 input=10 output=7 cache_write=5 "
            "cache_read=100" % MODEL) in lines
    assert ("source\t2026-10-20 hindsight-worker.service %s\trequests=2 sessions=2 input=20 output=90 cache_write=10 "
            "cache_read=200" % MODEL) in lines
    assert ("total\t2026-10-20 %s\trequests=2 sessions=2 input=20 output=90 cache_write=10 cache_read=200" % MODEL) \
        in lines
    assert ("total\t2026-10-21 %s\trequests=1 sessions=1 input=10 output=7 cache_write=5 cache_read=100" % MODEL) \
        in lines


def test_missing_unit_fake_dirs_and_symlinks_are_skipped(tmp_root, tmp_path):
    good = private(tmp_root, "hindsight-api.service", [answer("2026-10-20T10:00:00Z", "m1", "r1", out=1)])
    fake = private(tmp_root, "hindsight-worker.service", [answer("2026-10-20T10:00:00Z", "m9", "r9", out=999)])
    os.chmod(str(fake), 0o755)  # not systemd's 0700 — a fake in the world-writable /tmp
    outside = tmp_path / "outside.jsonl"
    outside.write_text(answer("2026-10-20T10:00:00Z", "m8", "r8", out=888) + "\n", encoding="utf-8")
    link_dir = good / "tmp" / "hindsight-claude-code-abc" / "projects" / "-opt-hindsight"
    os.symlink(str(outside), str(link_dir / "link.jsonl"))
    out = render(tmp_root)
    assert "unit\thindsight-worker.service\tnone" in out.splitlines()
    assert "jsonl=1 " in out and "999" not in out and "888" not in out


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO — только POSIX")
def test_fifo_and_symlinked_dirs_are_skipped(tmp_root, tmp_path):
    outer = private(tmp_root, "hindsight-api.service", [answer("2026-10-20T10:00:00Z", "m1", "r1", out=1)])
    projects = outer / "tmp" / "hindsight-claude-code-abc" / "projects"
    os.mkfifo(str(projects / "-opt-hindsight" / "pipe.jsonl"))  # would block a plain open()
    other = tmp_path / "elsewhere"
    (other / "projects" / "p").mkdir(parents=True)
    (other / "projects" / "p" / "x.jsonl").write_text(answer("2026-10-20T10:00:00Z", "m7", "r7", out=777) + "\n")
    os.symlink(str(other), str(outer / "tmp" / "hindsight-claude-code-link"))
    os.symlink(str(other / "projects" / "p"), str(projects / "linked-dir"))
    out = render(tmp_root)
    assert "jsonl=1 " in out and "777" not in out


def test_ledger_comes_before_the_transcript_scan(tmp_root):
    lines = render(tmp_root).splitlines()
    assert lines.index("ledger\t-\tno file") < lines.index("unit\thindsight-api.service\tnone")


def test_an_unreadable_file_does_not_hide_the_others(tmp_root, monkeypatch):
    private(tmp_root, "hindsight-api.service", [answer("2026-10-20T10:00:00Z", "m1", "r1", out=1)])
    private(tmp_root, "hindsight-worker.service", [answer("2026-10-20T10:00:00Z", "m2", "r2", out=2)])
    real = hu.usage_line

    def flaky(raw):
        if b'"m2"' in raw:
            raise OSError("EIO")
        return real(raw)
    monkeypatch.setattr(hu, "usage_line", flaky)
    out = render(tmp_root)
    assert "source\t2026-10-20 hindsight-api.service" in out and "unit\terror" not in out


def test_no_private_tmp_at_all(tmp_root):
    lines = render(tmp_root / "missing").splitlines()
    assert "unit\thindsight-api.service\tnone" in lines and "unit\thindsight-worker.service\tnone" in lines
    assert not any(ln.startswith(("source\t", "total\t")) for ln in lines)
    assert "ledger\t-\tno file" in lines


def test_ledger_hindsight_rows_of_last_days(tmp_root, tmp_path):
    ledger = tmp_path / "usage.jsonl"
    rows = [
        {"day": "2026-10-20", "agent": "hindsight", "category": "hindsight", "coverage": "partial", "model": MODEL,
         "requests": 5, "sessions": 5, "input": 1, "output": 2, "cache_write": 3, "cache_read": 4},
        {"day": "2026-10-19", "agent": "hindsight", "category": "hindsight", "coverage": "none", "model": "-"},
        {"day": "2026-10-01", "agent": "hindsight", "category": "hindsight", "coverage": "full", "model": MODEL,
         "requests": 9},  # older than LEDGER_DAYS
        {"day": "2026-10-20", "agent": "dev", "model": MODEL, "requests": 77},
    ]
    ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\nbroken\n", encoding="utf-8")
    lines = [ln for ln in render(tmp_root, ledger).splitlines() if ln.startswith("ledger\t")]
    assert lines == [
        "ledger\t2026-10-19 -\tcoverage=none requests=- sessions=- input=- output=- cache_write=- cache_read=-",
        "ledger\t2026-10-20 %s\tcoverage=partial requests=5 sessions=5 input=1 output=2 cache_write=3 cache_read=4"
        % MODEL,
    ]


def test_ledger_without_hindsight_rows(tmp_root, tmp_path):
    ledger = tmp_path / "usage.jsonl"
    ledger.write_text(json.dumps({"day": "2026-10-20", "agent": "dev", "model": MODEL}) + "\n", encoding="utf-8")
    assert "ledger\t-\tno hindsight rows since 2026-10-08" in render(tmp_root, ledger).splitlines()


def test_model_names_are_one_short_line(tmp_root):
    private(tmp_root, "hindsight-api.service",
            [answer("2026-10-20T10:00:00Z", "m1", "r1", out=1, model="evil\tmodel\nnext" + "x" * 200)])
    source = [ln for ln in render(tmp_root).splitlines() if ln.startswith("source\t")]
    assert len(source) == 1 and source[0].count("\t") == 2 and "evil model next" in source[0]
    assert len(source[0].split("\t")[1]) < 130


def test_a_failing_section_does_not_hide_the_other(tmp_root, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bad\nthing")
    monkeypatch.setattr(hu, "private_tmps", boom)
    lines = render(tmp_root).splitlines()
    assert "unit\terror\tRuntimeError: bad thing" in lines
    assert "ledger\t-\tno file" in lines


def test_output_passes_the_gate_masks_unchanged(tmp_root):
    from gate import agent_ro_gate as gate
    private(tmp_root, "hindsight-api.service", [answer("2026-10-20T10:00:00Z", "m1", "r1", out=1)])
    out = render(tmp_root).encode("utf-8")
    assert gate.redact_counted(out) == (out, 0)


def test_main_rejects_arguments(capsys):
    assert hu.main(["--days", "3"]) == 2
    assert "без аргументов" in capsys.readouterr().err


@pytest.mark.skipif(importlib.util.find_spec("vermin") is None, reason="vermin не установлен (requirements-dev.txt)")
def test_source_is_python36_compatible():
    proc = subprocess.run([sys.executable, "-c", VERMIN, "--no-tips", str(SOURCE)], capture_output=True, text=True)
    match = re.search(r"Minimum required versions:.*?3\.(\d+)", proc.stdout)
    assert match and int(match.group(1)) <= 6, proc.stdout + proc.stderr
