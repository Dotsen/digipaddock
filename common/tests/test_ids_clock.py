"""one card ID shape (kd.ids) and one time text (kd.clock) for every module."""
import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kd import clock, ids

ROOT = Path(__file__).resolve().parents[2]
# Code that may not import kd keeps its own copy (kd.ids docstring).
OWN_COPY = {
    "agents/sysadmin/gate/agent_ro_gate.py",
    "common/bin/kd-release",
    "agents/dev/dev_agent/pr.py",
    "agents/sysadmin/sysadmin/monitor/urls.py",
}


@pytest.mark.parametrize("value", ["KD-1", "KD-42", "KD-999999"])
def test_a_card_id_is_the_prefix_and_a_number(value):
    assert ids.CARD.fullmatch(value)


@pytest.mark.parametrize("value", ["KD-0", "KD-01", "KD-1234567", "kd-1", "KD-1\n", "KD-", "P-1", "KD-١"])
def test_anything_else_is_not_a_card_id(value):
    assert not ids.CARD.fullmatch(value)


def test_a_card_inside_text_is_found_whole():
    assert ids.IN_TEXT.search("[WIP] KD-12: a fix").group(0) == "KD-12"
    for text in ("KD-1234567: too long", "XKD-12: a word", "KD-0: no card"):
        assert ids.IN_TEXT.search(text) is None, text


def test_no_module_keeps_its_own_card_pattern():
    own = re.compile(r"KD-\[|KD-\\d")  # a pattern of the number, compiled or inline (re.fullmatch(r"KD-[...")
    found = sorted(
        str(path.relative_to(ROOT)).replace("\\", "/")
        for top in ("common/kd", "common/bin", "agents")
        for path in (ROOT / top).rglob("*")
        if path.is_file() and (path.suffix == ".py" or path.parent.name == "bin") and "tests" not in path.parts
        and own.search(path.read_text(encoding="utf-8", errors="replace"))
    )
    assert set(found) <= OWN_COPY, found


def test_iso_is_the_utc_second_with_a_z():
    assert clock.iso(0) == "1970-01-01T00:00:00Z"
    assert clock.iso(1.9) == "1970-01-01T00:00:01Z"
    utc3 = datetime(2026, 10, 29, 15, 30, 59, 999, tzinfo=timezone(timedelta(hours=3)))
    assert clock.iso(utc3) == "2026-10-29T12:30:59Z"
    assert clock.utcnow().tzinfo is timezone.utc


def test_the_hosts_zone_is_its_host_json_timezone(tmp_path, monkeypatch):
    host = json.loads((ROOT / "host" / "host.example.json").read_text("utf-8"))
    path = tmp_path / "host.json"
    path.write_text(json.dumps({**host, "timezone": "Etc/GMT-3"}))
    monkeypatch.setenv("KD_HOST_JSON", str(path))
    clock.zone.cache_clear()
    try:
        assert str(clock.zone()) == "Etc/GMT-3"
        assert clock.day(datetime(2026, 10, 29, 21, 30, tzinfo=timezone.utc)) == "2026-10-30"
        assert clock.day(datetime(2026, 10, 29, 21, 30, tzinfo=timezone.utc).timestamp()) == "2026-10-30"
        path.write_text(json.dumps({k: v for k, v in host.items() if k != "timezone"}))  # no key: UTC
        clock.zone.cache_clear()
        assert str(clock.zone()) == "UTC"
        assert clock.day(datetime(2026, 10, 29, 21, 30, tzinfo=timezone.utc)) == "2026-10-29"
    finally:
        clock.zone.cache_clear()


# A zone of its own: a fixed offset or a zone by name. Days, quiet hours and calendars are the host's.
OWN_ZONE = re.compile(r"timezone\(timedelta\(hours=|\bUTC[+-]\d|[+-](0[1-9]|1\d):00\b|\bEtc/GMT[+-]\d"
                      r"|\b(Europe|Asia|America|Africa|Australia|Pacific|Atlantic|Indian)/[A-Z][a-z]")


def test_no_code_keeps_a_zone_of_its_own():
    found = []
    run = subprocess.run(["git", "ls-files", "common", "agents"], cwd=ROOT, capture_output=True, text=True)
    if run.returncode != 0:  # a copy of the tree outside git (scripts/remote_pytest.sh)
        pytest.skip("not a git work tree")
    for rel in run.stdout.split():
        path = ROOT / rel
        if ("/tests/" in rel or path.suffix in (".md", ".png", ".ico", ".woff2") or rel.endswith(".test.ts")
                or ".example." in rel
                or not path.is_file()):
            continue
        for n, line in enumerate(path.read_text("utf-8", errors="replace").splitlines(), 1):
            if OWN_ZONE.search(line):
                found.append(f"{rel}:{n}: {line.strip()[:100]}")
    assert not found, "\n".join(found)


def test_an_alias_of_this_host_reads_as_its_card():
    # the fixture host (common/tests/fixtures/host/naming.json) takes OLD-<n>, its earlier prefix, for KD-<n>
    assert ids.ALIASES == ("OLD",)
    assert ids.normalize("see OLD-12 and (OLD-3), KD-4") == "see KD-12 and (KD-3), KD-4"
    for text in ("OLD-0", "OLD-1234567", "XOLD-12", "OLD-x", "old-12"):
        assert ids.normalize(text) == text, text
    assert not ids.CARD.fullmatch("OLD-12")  # an alias is accepted where people type, never stored


def test_a_value_that_is_an_alias_id_is_the_card():
    assert ids.canonical("OLD-12") == "KD-12" and ids.canonical("KD-12") == "KD-12"
    for value in ("see OLD-12", "OLD-12 ", "OLD-0", "XOLD-12", 12, None):
        assert ids.canonical(value) == value
    assert ids.canonical_all({"card": "OLD-3", "deps": ["OLD-1", "x"], "body": "OLD-2 done", "n": 2}) == {
        "card": "KD-3", "deps": ["KD-1", "x"], "body": "OLD-2 done", "n": 2}
