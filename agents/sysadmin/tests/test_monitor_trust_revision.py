"""a signature accepts exactly the observed object, never a later replacement."""

from datetime import datetime, timezone
import hashlib

from sysadmin.monitor import trust
from sysadmin.monitor.baseline import Baseline
from sysadmin.monitor.model import CheckResult, Observation
from sysadmin.monitor.registry import Registry


def test_changed_value_after_signature_is_refused_without_changing_baseline(tmp_path):
    now = datetime(2026, 10, 23, 10, tzinfo=timezone.utc)
    reg = Registry.load(tmp_path / "state.json", now.date())
    observation = Observation(
        "web-1", "ports", "tcp:0.0.0.0:443", "warn", "old-process", "new listener"
    )
    reg.observe([CheckResult("web-1", "ports", True, [observation])], now)
    baseline = Baseline.empty()
    baseline.set_server("web-1", Baseline.make_server({}, {}, [], {}, now))
    reg.state["problems"][observation.key]["value"] = "different-process"
    ok, detail = trust._accept(
        reg, baseline, {"incident": 1, "value": "old-process"}, "owner", now
    )
    assert not ok and "после подписи" in detail
    assert not baseline.server("web-1")["ports"]
    assert observation.key in reg.state["problems"]


def test_unchanged_signed_value_is_applied_once_to_baseline(tmp_path):
    now = datetime(2026, 10, 23, 10, tzinfo=timezone.utc)
    reg = Registry.load(tmp_path / "state.json", now.date())
    observation = Observation(
        "web-1", "ports", "tcp:0.0.0.0:443", "warn", "expected-process", "new listener"
    )
    reg.observe([CheckResult("web-1", "ports", True, [observation])], now)
    baseline = Baseline.empty()
    baseline.set_server("web-1", Baseline.make_server({}, {}, [], {}, now))
    request = {"incident": 1, "value": "expected-process"}
    assert trust._accept(reg, baseline, request, "owner", now)[0]
    assert baseline.server("web-1")["ports"][observation.subject] == observation.value
    assert not trust._accept(reg, baseline, request, "owner", now)[0]


def test_accepted_keys_take_the_fingerprints_of_the_same_file_version(tmp_path):
    """After "Trust" the next change of the file names its keys against the accepted version."""
    now = datetime(2026, 10, 23, 10, tzinfo=timezone.utc)
    path, value = "/home/git/.ssh/authorized_keys", "b" * 64 + " 3"
    reg = Registry.load(tmp_path / "state.json", now.date())
    observation = Observation("web-1", "sec:keys", path, "warn", value, "keys changed")
    reg.observe([CheckResult("web-1", "sec:keys", True, [observation])], now)
    reg.state["sec"] = {"current": {"web-1": {"at": now.isoformat(), "text": (
        f"# agent-sec-snapshot v1\nkeys\t{path}\t{value}\nkeyprints\t{path}\t{value}\n"
        f"keyprints\t{path} ED25519 SHA256:new\t- dev@laptop\n")}}}
    baseline = Baseline.empty()
    baseline.set_server("web-1", Baseline.make_server({}, {}, [], {f"keys:{path}": "a" * 64 + " 2"}, now))
    assert trust._accept(reg, baseline, {"incident": 1, "value": value}, "owner", now)[0]
    assert baseline.server("web-1")["snapshot"] == {
        f"keys:{path}": value, f"keyprints:{path}": value, f"keyprints:{path} ED25519 SHA256:new": "- dev@laptop"}


def test_identical_display_prefix_does_not_accept_a_different_full_value(tmp_path):
    now = datetime(2026, 10, 23, 10, tzinfo=timezone.utc)
    reg = Registry.load(tmp_path / "state.json", now.date())
    original = "a" * 200 + "original"
    observation = Observation(
        "web-1", "ports", "tcp:0.0.0.0:443", "warn", original, "listener"
    )
    reg.observe([CheckResult("web-1", "ports", True, [observation])], now)
    baseline = Baseline.empty()
    baseline.set_server("web-1", Baseline.make_server({}, {}, [], {}, now))
    payload = {
        "incident": 1,
        "value": original[:200],
        "value_hash": hashlib.sha256(original.encode()).hexdigest(),
    }
    reg.state["problems"][observation.key]["value"] = "a" * 200 + "replacement"
    assert not trust._accept(reg, baseline, payload, "owner", now)[0]
