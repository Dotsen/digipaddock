import json
import os
import stat
from datetime import datetime, timedelta, timezone

import pytest

from sysadmin.monitor import baseline as bl
from sysadmin.monitor.baseline import Baseline

UTC3 = timezone(timedelta(hours=3))
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC3)


def srv(identities=None, ports=None, peers=(), snap=None):
    return Baseline.make_server(identities or {}, ports or {}, list(peers), snap or {}, NOW)


def test_load_missing_is_empty_with_stage_trusted(tmp_path):
    b = Baseline.load(tmp_path / "baseline.json")
    assert b.data == {"trusted": {"203.0.113.10": "stage"}, "servers": {}} and not b.broken and not b.dirty
    assert b.is_trusted("203.0.113.10") == "stage" and not b.has("example-stage")


def test_save_and_load_roundtrip_mode_0600(tmp_path):
    p = tmp_path / "baseline.json"
    b = Baseline.load(p)
    b.set_server("srv", srv({"root@ED25519 SHA256:a": "владелец"}, {"tcp:0.0.0.0:22": "sshd"}, ["wg0:AAA="], {"users:root": "0 /bin/bash"}))
    assert b.dirty
    b.save(p)
    assert not b.dirty and not (tmp_path / "baseline.json.tmp").exists()
    if os.name != "nt":
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
    c = Baseline.load(p)
    assert c.has("srv") and c.server("srv")["taken_at"] == NOW.isoformat()
    assert c.server("srv")["ports"] == {"tcp:0.0.0.0:22": "sshd"} and c.server("srv")["wgpeers"] == ["wg0:AAA="]


@pytest.mark.parametrize("raw", [b"{not json", b"\xff{", json.dumps({"servers": []}).encode(),
                                 json.dumps({"servers": {"a": "x"}}).encode()])
def test_broken_file_disables_and_never_saves(tmp_path, capsys, raw):
    p = tmp_path / "baseline.json"
    p.write_bytes(raw)
    b = Baseline.load(p)
    assert b.broken and not b.has("srv")
    assert "повреждён" in capsys.readouterr().err
    with pytest.raises(RuntimeError):
        b.save(p)
    assert p.read_bytes() == raw


def test_load_fills_missing_sections(tmp_path):
    p = tmp_path / "baseline.json"
    p.write_text(json.dumps({"trusted": {}, "servers": {"srv": {"identities": {"x": ""}}}}), encoding="utf-8")
    b = Baseline.load(p)
    assert b.is_trusted("203.0.113.10") == "stage"
    assert b.server("srv") == {"taken_at": None, "identities": {"x": ""}, "ports": {}, "wgpeers": [], "snapshot": {}}


def test_trust_cidr_ip_normalization_and_limits():
    b = Baseline.empty()
    assert b.trust("10.1.2.3/24", "офис") == "10.1.2.0/24"
    assert b.trust(" 203.0.113.7 ", "дом") == "203.0.113.7"
    assert b.trust("2001:db8:1::/112", "v6") == "2001:db8:1::/112"
    assert b.dirty
    assert b.is_trusted("10.1.2.200") == "офис" and b.is_trusted("203.0.113.7") == "дом"
    assert b.is_trusted("2001:db8:1::5") == "v6"
    assert b.is_trusted("10.1.3.1") is None and b.is_trusted("не ip") is None and b.is_trusted("") is None
    for bad in ("300.1.1.1", "0.0.0.0/0", "10.0.0.0/8", "10.1.0.0/15", "2001:db8:1::/64", "2001:db8::/32", "::/0"):
        with pytest.raises(ValueError):
            b.trust(bad, "x")
    assert b.trust("10.2.0.0/16", "провайдер") == "10.2.0.0/16"


def test_is_trusted_longest_prefix():
    b = Baseline.empty()
    b.trust("10.2.0.0/16", "провайдер")
    b.trust("10.2.5.0/24", "офис")
    b.trust("10.2.5.7", "сервер")
    assert b.is_trusted("10.2.5.7") == "сервер" and b.is_trusted("10.2.5.8") == "офис" and b.is_trusted("10.2.9.1") == "провайдер"


def test_untrust():
    b = Baseline.empty()
    b.trust("203.0.113.7", "дом")
    assert b.untrust("203.0.113.7/32") == "203.0.113.7" and b.untrust("203.0.113.7") is None
    assert b.is_trusted("203.0.113.7") is None
    with pytest.raises(ValueError):
        b.untrust("203.0.113.10")


def test_trust_refuses_to_relabel_stage_ip():
    """The label "stage" is a service one: with another label the factory host's IP would become trusted for any user (a web shell)."""
    b = Baseline.empty()
    for cidr in ("203.0.113.10", "203.0.113.10/32", " 203.0.113.10 "):
        with pytest.raises(ValueError, match="stage"):
            b.trust(cidr, "x")
    assert b.is_trusted("203.0.113.10") == "stage" and not b.dirty


def test_load_forces_stage_label(tmp_path):
    p = tmp_path / "baseline.json"
    p.write_text(json.dumps({"trusted": {"203.0.113.10": "owner-ok"}, "servers": {}}), encoding="utf-8")
    assert Baseline.load(p).is_trusted("203.0.113.10") == "stage"


def test_load_drops_bad_and_too_wide_trusted(tmp_path, capsys):
    p = tmp_path / "baseline.json"
    p.write_text(json.dumps({"trusted": {"0.0.0.0/0": "всё", "10.0.0.0/8": "широко", "не ip": "x", "::/0": "v6",
                                         "203.0.113.7": "дом", "198.51.100.1": 5}, "servers": {}}), encoding="utf-8")
    b = Baseline.load(p)
    assert not b.broken and b.data["trusted"] == {"203.0.113.7": "дом", "203.0.113.10": "stage"}
    assert b.is_trusted("198.51.100.1") is None and b.is_trusted("10.1.1.1") is None
    assert "0.0.0.0/0" in capsys.readouterr().err


@pytest.mark.parametrize("srv", [{"identities": None}, {"ports": []}, {"wgpeers": {}}, {"snapshot": "x"},
                                 {"taken_at": 5}, {"identities": {"a@k": None}}])
def test_load_bad_server_field_types_is_broken(tmp_path, capsys, srv):
    p = tmp_path / "baseline.json"
    p.write_text(json.dumps({"trusted": {}, "servers": {"a": srv}}), encoding="utf-8")
    b = Baseline.load(p)
    assert b.broken and not b.has("a") and "повреждён" in capsys.readouterr().err


def test_load_trusted_not_dict_is_broken(tmp_path, capsys):
    p = tmp_path / "baseline.json"
    p.write_text(json.dumps({"trusted": ["203.0.113.7"], "servers": {}}), encoding="utf-8")
    assert Baseline.load(p).broken


def test_is_trusted_ipv4_mapped_ipv6():
    b = Baseline.empty()
    b.trust("203.0.113.0/24", "офис")
    assert b.is_trusted("::ffff:203.0.113.7") == "офис" and b.is_trusted("::ffff:203.0.113.10") == "stage"
    assert b.is_trusted("::ffff:198.51.100.1") is None


def test_is_weak():
    assert bl.is_weak("root@password") and bl.is_weak("root@key ?")
    assert not bl.is_weak("root@ED25519 SHA256:abc")


def test_label_elsewhere_and_record_identity():
    b = Baseline.empty()
    b.set_server("a", srv({"root@ED25519 SHA256:k": "владелец"}))
    b.set_server("b", srv({"root@ED25519 SHA256:k": ""}))
    b.set_server("c", srv())
    assert b.label_elsewhere("root@ED25519 SHA256:k", "c") == ("владелец", 2)
    assert b.label_elsewhere("root@ED25519 SHA256:k", "a") == ("", 1)
    assert b.label_elsewhere("nobody@password", "c") is None
    b.dirty = False
    b.record_identity("c", "root@ED25519 SHA256:k", "владелец")
    assert b.server("c")["identities"] == {"root@ED25519 SHA256:k": "владелец"} and b.dirty
    assert b.records == [("c", "root@ED25519 SHA256:k", "владелец")]


def test_apply_records_onto_fresh_copy():
    stale = Baseline.empty()
    stale.set_server("a", srv())
    stale.record_identity("a", "root@ED25519 SHA256:k", "владелец")
    fresh = Baseline.empty()
    fresh.set_server("a", srv())
    fresh.trust("203.0.113.7", "дом")
    fresh.dirty = False
    bl.apply_records(fresh, stale.records)
    assert fresh.server("a")["identities"] == {"root@ED25519 SHA256:k": "владелец"} and fresh.dirty
    assert fresh.is_trusted("203.0.113.7") == "дом"
    gone = Baseline.empty()
    bl.apply_records(gone, stale.records)
    assert not gone.has("a") and not gone.dirty


def test_merge_server_keeps_accepted_identities():
    b = Baseline.empty()
    b.set_server("a", srv({"deploy@ED25519 SHA256:d": "dev2"}, {"tcp:0.0.0.0:22": "sshd"}))
    b.merge_server("a", srv({"root@ED25519 SHA256:k": "владелец"}, {"tcp:0.0.0.0:80": "nginx"}, ["wg0:X="], {"users:root": "0 /bin/bash"}))
    s = b.server("a")
    assert s["identities"] == {"deploy@ED25519 SHA256:d": "dev2", "root@ED25519 SHA256:k": "владелец"}
    assert s["ports"] == {"tcp:0.0.0.0:80": "nginx"} and s["wgpeers"] == ["wg0:X="] and s["snapshot"] == {"users:root": "0 /bin/bash"}
    b.merge_server("new", srv({"x": "y"}))
    assert b.server("new")["identities"] == {"x": "y"}


def test_merge_server_owner_label_beats_trusted_address_label():
    """A repeated baseline: the owner's label from accept --label is not overwritten by a trusted address's label."""
    b = Baseline.empty()
    b.set_server("a", srv({"deploy@ED25519 SHA256:d": "dev2"}))
    b.merge_server("a", srv({"deploy@ED25519 SHA256:d": "офис", "root@ED25519 SHA256:k": "владелец"}))
    assert b.server("a")["identities"] == {"deploy@ED25519 SHA256:d": "dev2", "root@ED25519 SHA256:k": "владелец"}


def test_accept_each_check():
    b = Baseline.empty()
    b.set_server("a", srv({"root@ED25519 SHA256:k": "владелец"}, {"tcp:0.0.0.0:22": "sshd"}, ["wg0:AAA="],
                          {"cron:/etc/cron.d/x": "h1", "users:bob": "1001 /bin/bash"}))
    b.set_server("c", srv())
    assert "владелец" in b.accept("logins", "c", "root@ED25519 SHA256:k", "")
    assert b.server("c")["identities"]["root@ED25519 SHA256:k"] == "владелец"
    b.accept("logins", "c", "deploy@ED25519 SHA256:z", "", label="dev2")
    assert b.server("c")["identities"]["deploy@ED25519 SHA256:z"] == "dev2"
    b.accept("ports", "a", "tcp:0.0.0.0:8080", "python3")
    assert b.server("a")["ports"]["tcp:0.0.0.0:8080"] == "python3"
    b.accept("wgpeers", "a", "wg1:BBB=", "новый")
    b.accept("wgpeers", "a", "wg1:BBB=", "новый")
    assert b.server("a")["wgpeers"] == ["wg0:AAA=", "wg1:BBB="]
    b.accept("sec:cron", "a", "/etc/cron.d/y", "h2")
    assert b.server("a")["snapshot"]["cron:/etc/cron.d/y"] == "h2"
    b.accept("sec:users", "a", "bob", bl.REMOVED)
    assert "users:bob" not in b.server("a")["snapshot"]
    assert b.dirty


def test_accept_weak_identity_not_recorded():
    b = Baseline.empty()
    b.set_server("a", srv())
    text = b.accept("logins", "a", "root@password", "с 198.51.100.2")
    assert "не заносится" in text and b.server("a")["identities"] == {}


def test_accept_refuses_other_checks_and_missing_baseline():
    b = Baseline.empty()
    b.set_server("a", srv())
    for check in ("updates", "posture", "load", "disk", "reboot", "health"):
        with pytest.raises(ValueError, match="ack"):
            b.accept(check, "a", "-", "x")
    with pytest.raises(ValueError, match="baseline"):
        b.accept("ports", "nope", "tcp:0.0.0.0:1", "x")
    with pytest.raises(ValueError, match="ack"):
        b.accept("logins", "a", "gap", "с 11:00")
    assert b.server("a")["identities"] == {}
    broken = Baseline(b.data, broken=True)
    with pytest.raises(ValueError, match="повреждён"):
        broken.accept("ports", "a", "tcp:0.0.0.0:1", "x")


# --- a unit's incident carries all its objects -----------------------------------------------------------

def test_accept_of_a_composite_unit_value_sets_and_removes_each_object():
    b = Baseline({"trusted": {}, "servers": {"s": srv(snap={"units:old.service.d/a.conf": "x" * 64})}})
    value = json.dumps({"x.service": "a" * 64, "multi-user.target.wants/x.service": "symlink -> y",
                        "old.service.d/a.conf": None}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    text = b.accept("sec:units", "s", "x.service", value)
    snap = b.server("s")["snapshot"]
    assert snap == {"units:x.service": "a" * 64, "units:multi-user.target.wants/x.service": "symlink -> y"}
    assert text == "юнит systemd x.service принят на s" and b.dirty


def test_accept_of_a_plain_unit_value_keeps_its_old_meaning():
    b = Baseline({"trusted": {}, "servers": {"s": srv()}})
    b.accept("sec:units", "s", "multi-user.target.wants/x.service", "symlink -> y")
    b.accept("sec:units", "s", "gone.service", bl.REMOVED)
    assert b.server("s")["snapshot"] == {"units:multi-user.target.wants/x.service": "symlink -> y"}
