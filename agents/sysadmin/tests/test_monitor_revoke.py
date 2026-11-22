"""A login by a key whose access is gone closes as "access revoked" — never into the baseline, only when the
server's latest snapshot no longer allows the key, and the key's next login opens a new problem."""

from datetime import datetime, timedelta, timezone

from sysadmin.monitor import cabinet, seccheck, trust
from sysadmin.monitor.baseline import Baseline
from sysadmin.monitor.model import CheckResult
from sysadmin.monitor.registry import Registry

NOW = datetime(2026, 11, 11, 10, tzinfo=timezone.utc)
SERVER = "node-a"
IDENT = "promo@ED25519 SHA256:gone"
CFG = {"security": {"critical_servers": [], "pending_ips_max": 5}}
AK = "/home/deploy/.ssh/authorized_keys"


def snapshot(*fingerprints, broken=False):
    lines = [
        "# agent-sec-snapshot v1",
        f"keys\t{AK}\t{'a' * 64} 1",
        f"keyprints\t{AK}\t{'a' * 64} 1",
    ]
    lines += [f"keyprints\t{AK} ED25519 {fp}\t- deploy@ci" for fp in fingerprints]
    if broken:
        lines.append("keyprints\terror\tOSError: permission denied")
    return {
        "at": (NOW - timedelta(minutes=20)).isoformat(),
        "text": "\n".join(lines) + "\n",
    }


def opened(tmp_path, current=None):
    """A login of IDENT from an unknown address, as the tick leaves it: pending in sec, an open problem P-1."""
    reg = Registry.load(tmp_path / "state.json", NOW.date())
    sec = seccheck.sec_state(reg.state)
    baseline = Baseline.empty()
    baseline.set_server(SERVER, Baseline.make_server({}, {}, [], {}, NOW))
    login = seccheck.Login(
        NOW - timedelta(hours=1), "publickey", "promo", "198.51.100.4", IDENT
    )
    auth = seccheck.AuthResult([login], 0, set(), 0, None, [], None, 1)
    obs = seccheck.logins_obs(SERVER, auth, sec, baseline, CFG, NOW)
    reg.observe([CheckResult(SERVER, "logins", True, obs)], NOW)
    p = next(iter(reg.state["problems"].values()))
    p["id"] = "P-1"
    if current is not None:
        sec["current"][SERVER] = current
    return reg, baseline, p


def test_a_removed_key_closes_as_revoke_without_the_baseline(tmp_path):
    reg, baseline, p = opened(tmp_path, snapshot("SHA256:other"))
    ok, detail = trust._revoke(reg, {"incident": 1, "value": p["value"]}, "owner", NOW)
    assert ok and "снят" in detail and "в эталон не внесён" in detail
    assert reg.state["problems"] == {}
    assert IDENT not in seccheck.sec_state(reg.state)["pending"][SERVER]
    assert IDENT not in baseline.server(SERVER)["identities"]
    resolved = [i for i in cabinet.items_from(reg.journal) if i["kind"] == "resolve"]
    assert [i["how"] for i in resolved] == ["revoke"]


def test_a_key_still_allowed_is_refused_and_the_problem_stays(tmp_path):
    reg, _, p = opened(tmp_path, snapshot("SHA256:gone"))
    ok, detail = trust._revoke(reg, {"incident": 1, "value": p["value"]}, "owner", NOW)
    assert not ok and "ещё разрешён" in detail and AK in detail
    assert p["key"] in reg.state["problems"]
    assert IDENT in seccheck.sec_state(reg.state)["pending"][SERVER]


def test_without_a_usable_snapshot_nothing_closes(tmp_path):
    for current in (None, snapshot(broken=True)):
        reg, _, p = opened(tmp_path, current)
        ok, detail = trust._revoke(
            reg, {"incident": 1, "value": p["value"]}, "owner", NOW
        )
        assert not ok and "не проверить, что ключ снят" in detail
        assert p["key"] in reg.state["problems"]


def test_the_next_login_with_the_revoked_key_opens_a_new_problem(tmp_path):
    reg, baseline, p = opened(tmp_path, snapshot())
    assert trust._revoke(reg, {"incident": 1, "value": p["value"]}, "owner", NOW)[0]
    later = NOW + timedelta(hours=2)
    login = seccheck.Login(later, "publickey", "promo", "198.51.100.4", IDENT)
    auth = seccheck.AuthResult([login], 0, set(), 0, None, [], None, 1)
    obs = seccheck.logins_obs(
        SERVER, auth, seccheck.sec_state(reg.state), baseline, CFG, later
    )
    assert [o.subject for o in obs] == [IDENT]
    reg.observe([CheckResult(SERVER, "logins", True, obs)], later)
    assert list(reg.state["problems"]) == [p["key"]]


def test_only_a_login_by_a_key_and_only_the_signed_value(tmp_path):
    reg, _, p = opened(tmp_path, snapshot())
    ok, detail = trust._revoke(reg, {"incident": 1, "value": "другое"}, "owner", NOW)
    assert not ok and "после подписи" in detail
    p["key"] = p["key"].replace(IDENT, "root@password")
    reg.state["problems"] = {p["key"]: p}
    ok, detail = trust._revoke(reg, {"incident": 1, "value": p["value"]}, "owner", NOW)
    assert not ok and "только для входа по ключу" in detail
    assert not trust._revoke(reg, {"incident": 2}, "owner", NOW)[0]


def revoke(tmp_path, current, subject=IDENT):
    reg, _, p = opened(tmp_path, current)
    if subject != IDENT:
        key = p["key"].replace(IDENT, subject)
        reg.state["problems"] = {key: dict(p, key=key)}
        pending = seccheck.sec_state(reg.state)["pending"][SERVER]
        pending[subject] = pending.pop(IDENT)
    return trust._revoke(reg, {"incident": 1, "value": p["value"]}, "owner", NOW)


def with_lines(*lines):
    current = snapshot()
    current["text"] += "".join(line + "\n" for line in lines)
    return current


def test_a_snapshot_that_cannot_see_the_key_proves_nothing(tmp_path):
    """Review of a symlinked or unread key file, sshd letting keys in past the files the snapshot reads, a
    certificate — none of them proves the key gone."""
    cases = (
        ("keys\t/root/.ssh/authorized_keys\tsymlink -> /etc/keys/root", "не прочитал"),
        ("keys\t/home/ci/.ssh/authorized_keys\tbig 2000000", "не прочитал"),
        ("sshd\tauthorizedkeyscommand\t/usr/bin/keys %u", "authorizedkeyscommand"),
        ("sshd\ttrustedusercakeys\t/etc/ssh/ca.pub", "trustedusercakeys"),
        (
            "sshd\tauthorizedkeysfile\t.ssh/authorized_keys /etc/ssh/keys/%u",
            "AuthorizedKeysFile",
        ),
    )
    for line, why in cases:
        ok, detail = revoke(tmp_path, with_lines(line))
        assert not ok and why in detail, detail
    ok, detail = revoke(tmp_path, snapshot(), subject="promo@ED25519-CERT SHA256:gone")
    assert not ok and "сертификату" in detail
    harmless = with_lines(
        "sshd\tauthorizedkeyscommand\tnone",
        "sshd\tauthorizedkeysfile\t.ssh/authorized_keys .ssh/authorized_keys2",
        "keys\t/home/old/.ssh/authorized_keys\t-",
    )
    assert revoke(tmp_path, harmless)[0]


def test_a_snapshot_older_than_the_login_or_too_old_proves_nothing(tmp_path):
    before = snapshot()
    before["at"] = (
        NOW - timedelta(hours=2)
    ).isoformat()  # the login was an hour ago: its key may be newer
    ok, detail = revoke(tmp_path, before)
    assert not ok and "старше последнего входа" in detail
    reg, _, p = opened(tmp_path, snapshot())
    sec = seccheck.sec_state(reg.state)
    sec["pending"][SERVER][IDENT]["last"] = (NOW - timedelta(hours=6)).isoformat()
    sec["current"][SERVER]["at"] = (NOW - timedelta(hours=4)).isoformat()
    ok, detail = trust._revoke(reg, {"incident": 1, "value": p["value"]}, "owner", NOW)
    assert not ok and "старше 3 ч" in detail and p["key"] in reg.state["problems"]
