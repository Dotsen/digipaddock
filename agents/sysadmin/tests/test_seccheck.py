from datetime import datetime, timedelta, timezone
import json

import pytest

from sysadmin.monitor import seccheck as sc
from sysadmin.monitor.baseline import Baseline

UTC3 = timezone(timedelta(hours=3))
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC3)
CFG = {"security": {
    "critical_servers": ["example-prod", "example-stage"], "pending_ips_max": 5,
    "critical_packages": ["openssh-server", "openssl", "libssl*", "linux-image-*", "sudo", "libc6", "nginx*",
                          "mysql-server*", "php*", "wireguard*", "systemd"],
    "updates": {"warn_days": 3, "critical_warn_days": 1, "critical_any_days": 7, "crit_days": 7}, "reboot_warn_days": 7,
    "eol": {"warn_days": 90, "dates": {"ubuntu": {"22.04": "2027-04-30", "24.04": "2029-04-30"},
                                       "almalinux": {"8": "2029-03-01"}}},
    "load": {"warn_x": 2, "crit_x": 4, "streak": 3},
    "units_ignore": ["snap-*.mount", "snap.*"], "ports_ignore": ["tcp:127.0.0.1:eph:*"],
}}


def lv(observations):
    return {o.subject: o.level for o in observations}


def state():
    return sc.sec_state({})


def base(*servers, trusted=()):
    b = Baseline.empty()
    for s in servers:
        b.set_server(s, Baseline.make_server({}, {}, [], {}, NOW))
    for ip, label in trusted:
        b.trust(ip, label)
    return b


# --- auth.log -----------------------------------------------------------------------

ISO = ("2026-10-06T11:58:01.111111+03:00 example-stage sshd[1]: Accepted publickey for agent-ro from 203.0.113.10 port 1 ssh2: ED25519 SHA256:ro\n"
       "2026-10-06T11:58:01.222222+03:00 example-stage agent-ro-gate: allow: uptime\n"
       "2026-10-06T11:58:02.000000+03:00 example-stage sshd[2]: Accepted publickey for root from 203.0.113.7 port 2 ssh2: ED25519 SHA256:own\n"
       "2026-10-06T11:58:03.000000+03:00 example-stage sshd[3]: Failed password for invalid user admin from 198.51.100.9 port 3 ssh2\n"
       "2026-10-06T11:58:03.500000+03:00 example-stage sshd[4]: Invalid user test from 198.51.100.10 port 4\n"
       "2026-10-06T11:58:04.000000+03:00 example-stage agent-ro-gate: deny (путь запрещён: /etc/shadow): cat /etc/shadow\n"
       "2026-10-06T11:58:05.000000+03:00 example-stage sshd[5]: Accepted password for root from 198.51.100.9 port 5 ssh2\n"
       "2026-10-06T11:58:06.000000+03:00 example-stage sshd-session[6]: Accepted publickey for deploy from 198.51.100.77 port 6 ssh2: RSA SHA256:new\n"
       "2026-10-06T11:58:07.000000+03:00 example-stage sshd[7]: pam_unix(sshd:session): session opened for user root(uid=0) by (uid=0)\n")
OLD = ("Oct  6 11:58:02 node sshd[2]: Accepted publickey for root from 203.0.113.7 port 2 ssh2: ED25519 SHA256:own\n"
       "Oct  6 11:58:02 node sshd[3]: Accepted publickey for root from 203.0.113.8 port 3 ssh2: ED25519 SHA256:own\n")
JOURNAL = "2026-10-06T11:58:02+0300 node-g sshd[2]: Accepted publickey for root from 203.0.113.7 port 2 ssh2: RSA SHA256:fuj\n"


def test_parse_ts_three_formats_and_shift():
    ts, rest = sc.parse_ts(ISO.splitlines()[2], NOW)
    assert ts == datetime(2026, 10, 6, 11, 58, 2, tzinfo=UTC3) and rest.startswith("example-stage sshd[2]")
    ts, _ = sc.parse_ts(JOURNAL.strip(), NOW)
    assert ts == datetime(2026, 10, 6, 11, 58, 2, tzinfo=UTC3)
    ts, _ = sc.parse_ts(OLD.splitlines()[0], NOW)
    assert ts == datetime(2026, 10, 6, 11, 58, 2, tzinfo=UTC3)
    # the traditional format is the server's local time: a server in UTC (the shift −3 h) → in UTC+3 it is 3 hours more
    ts, _ = sc.parse_ts(OLD.splitlines()[0], NOW, shift=timedelta(hours=-3))
    assert ts == datetime(2026, 10, 6, 14, 58, 2, tzinfo=UTC3)
    ts, _ = sc.parse_ts("Dec 31 23:59:59 node sshd[1]: x", NOW)
    assert ts.year == 2025
    assert sc.parse_ts("мусор без метки", NOW) == (None, "мусор без метки")


def test_parse_ts_year_by_server_local_time():
    """The year of a traditional timestamp is chosen by the server's local time, not by UTC+3."""
    # the server is in UTC: in UTC+3 it is already 2027, on the server still 31.12.2026
    ts, _ = sc.parse_ts("Dec 31 21:05:00 node sshd[1]: x", datetime(2027, 1, 1, 0, 10, tzinfo=UTC3),
                        shift=timedelta(hours=-3))
    assert ts == datetime(2027, 1, 1, 0, 5, tzinfo=UTC3)
    # the server is an hour ahead of UTC+3: in UTC+3 it is still 2026, on the server already 01.01.2027
    ts, _ = sc.parse_ts("Jan  1 00:30:00 node sshd[1]: x", datetime(2026, 12, 31, 23, 40, tzinfo=UTC3),
                        shift=timedelta(hours=1))
    assert ts == datetime(2026, 12, 31, 23, 30, tzinfo=UTC3)


def test_parse_ts_in_the_servers_zone_across_the_hour_change():
    """10.9: a line from before Berlin's hour change is read in its summer time, not by the shift of now."""
    from zoneinfo import ZoneInfo

    now = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
    line = "Oct 24 13:30:00 node sshd[1]: x"
    ts, rest = sc.parse_ts(line, now, shift=timedelta(hours=-2), zone=ZoneInfo("Europe/Berlin"))
    assert ts == datetime(2026, 10, 24, 11, 30, tzinfo=timezone.utc) and rest == "node sshd[1]: x"
    ts, _ = sc.parse_ts(line, now, shift=timedelta(hours=-2))
    assert ts == datetime(2026, 10, 24, 12, 30, tzinfo=timezone.utc)  # no zone in the inventory: an hour off


def test_parse_auth_logins_failed_denies_window():
    a = sc.parse_auth(ISO, None, [], NOW)
    assert [(l.user, l.ip, l.identity) for l in a.logins] == [
        ("agent-ro", "203.0.113.10", "agent-ro@ED25519 SHA256:ro"),
        ("root", "203.0.113.7", "root@ED25519 SHA256:own"),
        ("root", "198.51.100.9", "root@password"),
        ("deploy", "198.51.100.77", "deploy@RSA SHA256:new"),
    ]
    assert a.failed == 1 and a.failed_ips == {"198.51.100.9"} and a.denies == 1  # Invalid user is not counted twice
    assert a.last_ts == "2026-10-06T11:58:07+03:00" and a.last_lines == [ISO.splitlines()[-1]]
    assert a.first_ts == "2026-10-06T11:58:01+03:00" and a.lines == 9


def test_parse_auth_dedup_by_last_ts():
    a = sc.parse_auth(ISO, None, [], NOW)
    again = sc.parse_auth(ISO, a.last_ts, a.last_lines, NOW)
    assert again.logins == [] and again.failed == 0 and again.denies == 0 and again.last_ts == a.last_ts
    assert again.first_ts == a.first_ts and again.lines == 9  # the window still covers last_ts
    newer = ISO + "2026-10-06T11:58:08.000000+03:00 example-stage sshd[8]: Accepted publickey for root from 203.0.113.7 port 8 ssh2: ED25519 SHA256:own\n"
    third = sc.parse_auth(newer, a.last_ts, a.last_lines, NOW)
    assert len(third.logins) == 1 and third.last_ts == "2026-10-06T11:58:08+03:00"


def test_parse_auth_dedup_same_second():
    a = sc.parse_auth(OLD, None, [], NOW)
    assert len(a.logins) == 2 and len(a.last_lines) == 2
    assert sc.parse_auth(OLD, a.last_ts, a.last_lines, NOW).logins == []
    more = OLD + "Oct  6 11:58:02 node sshd[4]: Accepted publickey for root from 203.0.113.9 port 4 ssh2: ED25519 SHA256:own\n"
    c = sc.parse_auth(more, a.last_ts, a.last_lines, NOW)
    assert [l.ip for l in c.logins] == ["203.0.113.9"] and len(c.last_lines) == 3


def test_parse_auth_ignores_forged_client_text():
    """The client's text before authorization (a user name, a disconnect reason) must not look like a login or a refusal."""
    forged = ("2026-10-06T11:58:09+03:00 example-prod sshd[9]: Invalid user x sshd[1]: Accepted password for root "
              "from 198.51.100.7 port 1 from 198.51.100.8 port 5\n"
              "2026-10-06T11:58:10+03:00 example-prod sshd[10]: Received disconnect from 198.51.100.2 port 1:11: sshd[2]: "
              "Accepted publickey for agent-ro from 203.0.113.10 port 1 ssh2: ED25519 SHA256:evil\n"
              "2026-10-06T11:58:11+03:00 example-prod sshd[11]: Invalid user agent-ro-gate: deny from 198.51.100.2 port 1\n"
              "Oct  6 11:58:12 node sshd[12]: Received disconnect from 198.51.100.2 port 1:11: node agent-ro-gate: deny x\n")
    a = sc.parse_auth(forged, None, [], NOW)
    assert a.logins == [] and a.denies == 0 and a.failed == 0 and a.lines == 4


def test_parse_auth_failed_user_with_spaces():
    a = sc.parse_auth("2026-10-06T11:58:09+03:00 node sshd[9]: Failed password for invalid user a b from 198.51.100.2 port 1 ssh2\n"
                      "2026-10-06T11:58:10+03:00 node sshd[10]: Failed password for invalid user x from 198.51.100.9 port 1 "
                      "from 198.51.100.5 port 2 ssh2\n", None, [], NOW)
    assert a.failed == 2 and a.failed_ips == {"198.51.100.2", "198.51.100.5"}  # the address is the last "from … port", sshd writes it


def test_parse_auth_counts_rsyslog_repeated_failed():
    """rsyslog folds identical lines: "message repeated K times: [ Failed … ]" is K more attempts."""
    a = sc.parse_auth("Oct  6 11:58:02 node sshd[2]: Failed password for root from 198.51.100.2 port 2 ssh2\n"
                      "Oct  6 11:58:09 node sshd[2]: message repeated 5 times: [ Failed password for root from 198.51.100.2 "
                      "port 2 ssh2]\n"
                      "Oct  6 11:58:10 node sshd[3]: Invalid user x message repeated 9 times: [ Failed password for root "
                      "from 198.51.100.7 port 2 ssh2]\n", None, [], NOW)
    assert a.failed == 6 and a.failed_ips == {"198.51.100.2"}


def test_parse_auth_journal_short_iso_and_gate_lines_ignored():
    a = sc.parse_auth(JOURNAL + "[agent-ro-gate: скрыто секретов: 1]\n", None, [], NOW)
    assert [l.identity for l in a.logins] == ["root@RSA SHA256:fuj"]


# --- the rules -------------------------------------------------------------------

def login(ip, identity="root@ED25519 SHA256:own", user="root", method="publickey"):
    return sc.Login(NOW, method, user, ip, identity)


def auth(*logins, failed=0, failed_ips=(), denies=0):
    return sc.AuthResult(list(logins), failed, set(failed_ips), denies, None, [], None, len(logins))


def test_logins_trusted_ip_is_silent_and_records_identity():
    b = base("example-stage", trusted=[("203.0.113.0/24", "владелец")])
    st = state()
    assert sc.logins_obs("example-stage", auth(login("203.0.113.7")), st, b, CFG, NOW) == []
    assert b.server("example-stage")["identities"] == {"root@ED25519 SHA256:own": "владелец"}
    assert b.records == [("example-stage", "root@ED25519 SHA256:own", "владелец")]
    assert len(st["accepted"]) == 1 and "доверенного адреса 203.0.113.7" in st["accepted"][0]["text"]
    assert st["week"]["example-stage"]["logins"] == {"владелец": {"n": 1, "ips": ["203.0.113.7"]}}
    assert st["untrusted"] == []


def test_logins_agent_ro_from_stage_is_silent():
    b = base("node-f")
    st = state()
    a = auth(login("203.0.113.10", "agent-ro@ED25519 SHA256:ro", user="agent-ro"))
    assert sc.logins_obs("node-f", a, st, b, CFG, NOW) == []
    assert b.server("node-f")["identities"] == {"agent-ro@ED25519 SHA256:ro": "stage"}
    assert st["week"]["node-f"]["logins"] == {}  # service logins do not go into the weekly line


def test_logins_stage_ip_not_trusted_for_other_users():
    """A web shell on the factory host: a foreign key from its IP must not stay silent. root's exec key is enrolled at baseline."""
    b = base("example-prod")
    st = state()
    o = sc.logins_obs("example-prod", auth(login("203.0.113.10", "root@ED25519 SHA256:evil")), st, b, CFG, NOW)
    assert lv(o) == {"root@ED25519 SHA256:evil": "crit"} and b.records == []
    b.server("example-prod")["identities"]["root@ED25519 SHA256:exec"] = "stage"
    o = sc.logins_obs("example-prod", auth(login("203.0.113.10", "root@ED25519 SHA256:exec")), st, b, CFG, NOW)
    assert lv(o) == {"root@ED25519 SHA256:evil": "crit"}  # a known exec key — silence (except the sticky evil)
    assert st["untrusted"] == []  # a service login from the factory host is not a note for the digest


def test_logins_known_from_stage_ip_but_other_label_is_noted():
    b = base("node-f")
    b.server("node-f")["identities"]["root@ED25519 SHA256:own"] = "владелец"
    st = state()
    assert sc.logins_obs("node-f", auth(login("203.0.113.10")), st, b, CFG, NOW) == []
    assert st["untrusted"][0]["text"] == "node-f: «владелец» с 203.0.113.10"


def test_logins_known_identity_silent_any_ip_but_noted_when_untrusted():
    b = base("node-f", trusted=[("203.0.113.0/24", "владелец")])
    b.server("node-f")["identities"]["root@ED25519 SHA256:own"] = "владелец"
    st = state()
    assert sc.logins_obs("node-f", auth(login("198.51.100.77")), st, b, CFG, NOW) == []
    assert st["week"]["node-f"]["logins"]["владелец"]["ips"] == ["198.51.100.77"]
    assert st["accepted"] == [] and st["untrusted"][0]["text"] == "node-f: «владелец» с 198.51.100.77"
    assert sc.logins_obs("node-f", auth(login("203.0.113.7")), st, b, CFG, NOW) == []
    assert len(st["untrusted"]) == 1  # from a trusted address — not a note


def test_logins_known_elsewhere_silent_on_node_but_warn_on_critical():
    b = base("example-stage", "node-h", "example-prod")
    b.server("example-stage")["identities"]["deploy@ED25519 SHA256:d"] = "dev2"
    st = state()
    lg = login("198.51.100.77", "deploy@ED25519 SHA256:d", user="deploy")
    assert sc.logins_obs("node-h", auth(lg), st, b, CFG, NOW) == []
    assert b.server("node-h")["identities"]["deploy@ED25519 SHA256:d"] == "dev2"
    assert "«dev2» впервые на node-h с 198.51.100.77" in st["accepted"][0]["text"]
    o = sc.logins_obs("example-prod", auth(lg), st, b, CFG, NOW)
    assert lv(o) == {"deploy@ED25519 SHA256:d": "warn"} and "«dev2»" in o[0].text
    assert "deploy@ED25519 SHA256:d" not in b.server("example-prod")["identities"]


def test_logins_known_elsewhere_without_label_is_warn_on_critical():
    b = base("node-f", "example-prod")
    b.server("node-f")["identities"]["deploy@ED25519 SHA256:d"] = ""
    st = state()
    o = sc.logins_obs("example-prod", auth(login("198.51.100.77", "deploy@ED25519 SHA256:d", user="deploy")), st, b, CFG, NOW)
    assert lv(o) == {"deploy@ED25519 SHA256:d": "warn"} and "принят на других серверах" in o[0].text


def test_logins_pending_upgraded_when_accepted_elsewhere_later():
    b = base("node-f", "example-prod")
    st = state()
    lg = login("198.51.100.77", "deploy@ED25519 SHA256:d", user="deploy")
    assert lv(sc.logins_obs("example-prod", auth(lg), st, b, CFG, NOW)) == {"deploy@ED25519 SHA256:d": "crit"}
    b.server("node-f")["identities"]["deploy@ED25519 SHA256:d"] = "dev2"
    o = sc.logins_obs("example-prod", auth(lg), st, b, CFG, NOW)
    assert lv(o) == {"deploy@ED25519 SHA256:d": "warn"} and "«dev2»" in o[0].text


def test_logins_pending_elsewhere_resets_when_removed_elsewhere():
    """The key was removed from the other server — the sticky problem on a critical one is 🔴 again, without "accepted on other servers"."""
    b = base("node-f", "example-prod")
    b.server("node-f")["identities"]["deploy@ED25519 SHA256:d"] = "dev2"
    st = state()
    lg = login("198.51.100.77", "deploy@ED25519 SHA256:d", user="deploy")
    assert lv(sc.logins_obs("example-prod", auth(lg), st, b, CFG, NOW)) == {"deploy@ED25519 SHA256:d": "warn"}
    del b.server("node-f")["identities"]["deploy@ED25519 SHA256:d"]
    o = sc.logins_obs("example-prod", auth(), st, b, CFG, NOW)
    assert lv(o) == {"deploy@ED25519 SHA256:d": "crit"} and "других серверах" not in o[0].text and "dev2" not in o[0].text


def test_logins_unknown_levels_and_sticky():
    b = base("example-prod", "node-f")
    st = state()
    o = sc.logins_obs("example-prod", auth(login("198.51.100.77", "root@ED25519 SHA256:evil")), st, b, CFG, NOW)
    assert lv(o) == {"root@ED25519 SHA256:evil": "crit"}
    assert o[0].check == "logins" and "по ключу ED25519 SHA256:evil" in o[0].text and "198.51.100.77" in o[0].text
    o2 = sc.logins_obs("example-prod", auth(), st, b, CFG, NOW)  # the next tick without lines — it is still reported
    assert lv(o2) == {"root@ED25519 SHA256:evil": "crit"}
    assert st["pending"]["example-prod"]["root@ED25519 SHA256:evil"]["count"] == 1
    assert lv(sc.logins_obs("node-f", auth(login("198.51.100.78", "root@ED25519 SHA256:evil")), st, b, CFG, NOW)) == {"root@ED25519 SHA256:evil": "warn"}
    assert b.records == [] and st["accepted"] == []
    # accept is done (the identity is in the baseline) — pending cleans itself
    b.server("example-prod")["identities"]["root@ED25519 SHA256:evil"] = "x"
    assert sc.logins_obs("example-prod", auth(), st, b, CFG, NOW) == [] and st["pending"]["example-prod"] == {}


def test_logins_pending_cleared_when_recorded_same_tick():
    b = base("node-f", trusted=[("203.0.113.0/24", "владелец")])
    st = state()
    evil = "root@ED25519 SHA256:evil"
    assert lv(sc.logins_obs("node-f", auth(login("198.51.100.77", evil)), st, b, CFG, NOW)) == {evil: "warn"}
    assert sc.logins_obs("node-f", auth(login("203.0.113.7", evil)), st, b, CFG, NOW) == []  # enrolled by this tick
    assert st["pending"]["node-f"] == {} and b.server("node-f")["identities"][evil] == "владелец"


def test_logins_password_never_recorded():
    b = base("example-prod", "node-f", trusted=[("203.0.113.0/24", "владелец")])
    st = state()
    # from a trusted address — silence, but it does not get into the baseline
    assert sc.logins_obs("node-f", auth(login("203.0.113.7", "root@password", method="password")), st, b, CFG, NOW) == []
    assert b.server("node-f")["identities"] == {} and b.records == []
    # from an untrusted one — an alarm, even if someone wrote it into the baseline here and on another server
    b.server("node-f")["identities"]["root@password"] = "ошибка"
    b.server("example-prod")["identities"]["root@password"] = "ошибка"
    o = sc.logins_obs("node-f", auth(login("198.51.100.78", "root@password", method="password")), st, b, CFG, NOW)
    assert lv(o) == {"root@password": "warn"} and "по паролю" in o[0].text
    o = sc.logins_obs("example-prod", auth(login("198.51.100.78", "root@password", method="password")), st, b, CFG, NOW)
    assert lv(o) == {"root@password": "crit"}


def test_logins_pending_ips_capped_and_week_counters():
    b = base("node-f")
    st = state()
    logins = [login(f"198.51.100.{i}", "root@ED25519 SHA256:evil") for i in range(8)]
    sc.logins_obs("node-f", auth(*logins, failed=5, failed_ips=["198.51.100.3", "198.51.100.6"], denies=2), st, b, CFG, NOW)
    p = st["pending"]["node-f"]["root@ED25519 SHA256:evil"]
    assert p["count"] == 8 and len(p["ips"]) == 5 and p["first"] == NOW.isoformat()
    w = st["week"]["node-f"]
    assert w["failed"] == 5 and sorted(w["failed_ips"]) == ["198.51.100.3", "198.51.100.6"] and w["denies"] == 2
    sc.logins_obs("node-f", auth(failed=1, failed_ips=["198.51.100.3"]), st, b, CFG, NOW)
    assert w["failed"] == 6 and len(w["failed_ips"]) == 2


# --- ports / peers / the snapshot / the posture ----------------------------------------

SS = """Netid State  Recv-Q Send-Q Local Address:Port  Peer Address:Port Process
tcp   LISTEN 0      4096   0.0.0.0:22          0.0.0.0:*     users:(("sshd",pid=800,fd=3))
tcp   LISTEN 0      511    *:80                *:*           users:(("nginx",pid=900,fd=6),("nginx",pid=901,fd=6))
tcp   LISTEN 0      151    127.0.0.1:3306      0.0.0.0:*     users:(("mysqld",pid=1000,fd=23))
tcp   LISTEN 0      4096   0.0.0.0:41337       0.0.0.0:*     users:(("forgejo-runner",pid=1200,fd=8))
udp   UNCONN 0      0      0.0.0.0:51820       0.0.0.0:*
udp   UNCONN 0      0      0.0.0.0:49152       0.0.0.0:*
udp   UNCONN 0      0      127.0.0.53%lo:53    0.0.0.0:*     users:(("systemd-resolve",pid=500,fd=13))
"""
K1, K2 = "A" * 43 + "=", "B" * 43 + "="
SHA_A, SHA_B = "a" * 64, "b" * 64
WG = f"wg0\t{K1}\t10.0.0.2/32\nwg0\t{K2}\t10.0.0.3/32 fd00::3/128\nwg1\t{K1}\t(none)\n"
SNAP = ("# agent-sec-snapshot v1\n"
        "users\troot\t0 /bin/bash\nusers\tubuntu\t1000 /bin/bash\n"
        "groups\tsudo\tubuntu\ngroups\tdocker\t-\n"
        "keys\t/root/.ssh/authorized_keys\t" + SHA_A + " 2\nkeys\t/home/ubuntu/.ssh/authorized_keys\t-\n"
        "sudoers\t/etc/sudoers\ts1\nshadow\t/etc/shadow\tsh1\n"
        "sshd\tport\t22\nsshd\tpermitrootlogin\tprohibit-password\nsshd\tpasswordauthentication\tno\nsshd\tallowusers\troot\n"
        "sshd\tauthorizedkeyscommand\tnone\nsshd\ttrustedusercakeys\tnone\nsshd\tfile:/etc/ssh/sshd_config\tf1\n"
        "cron\t/etc/cron.d/backup\tc1\npersist\t/etc/ld.so.preload\t-\npersist\t/etc/rc.local\t-\n"
        "units\tdigipaddock-monitor.service\tu1\nunits\tmulti-user.target.wants/nginx.service\tsymlink -> /lib/systemd/system/nginx.service\n"
        "units\tsnap-certbot-1234.mount\tu5\n"
        "fw\tufw\tactive\nfw\tufw:default\tdeny (incoming), allow (outgoing), disabled (routed)\n"
        "fw\tufw:22/tcp ALLOW IN Anywhere\trule\nfw\tfail2ban\tactive\n")


def test_parse_listeners():
    assert sc.parse_listeners(SS) == {"tcp:0.0.0.0:22": "sshd", "tcp:[::]:80": "nginx", "tcp:127.0.0.1:3306": "mysqld",
                                      "tcp:0.0.0.0:eph:forgejo-runner": "forgejo-runner",
                                      "udp:127.0.0.53%lo:53": "systemd-resolve"}  # UDP without a process (wg) is skipped


def test_ports_obs_new_changed_loopback_and_ignore():
    base_ports = {"tcp:0.0.0.0:22": "sshd", "tcp:[::]:80": "nginx", "tcp:127.0.0.1:3306": "mysqld",
                  "tcp:0.0.0.0:1080": "danted", "tcp:0.0.0.0:eph:forgejo-runner": "forgejo-runner"}
    cur = dict(sc.parse_listeners(SS))
    cur["tcp:0.0.0.0:4444"] = "nc"
    cur["tcp:[::]:80"] = "python3"
    cur["tcp:127.0.0.1:9000"] = "php-fpm"
    cur["tcp:127.0.0.1:eph:node"] = "node"
    o = sc.ports_obs("example-prod", cur, base_ports, CFG)
    assert lv(o) == {"tcp:0.0.0.0:4444": "crit", "tcp:[::]:80": "crit", "tcp:127.0.0.1:9000": "warn",
                     "udp:127.0.0.53%lo:53": "warn"}  # loopback — ⚠️; tcp:127.0.0.1:eph:* is in ports_ignore
    assert "python3 вместо nginx" in [x.text for x in o if x.subject == "tcp:[::]:80"][0]
    assert lv(sc.ports_obs("node-f", cur, base_ports, CFG))["tcp:0.0.0.0:4444"] == "warn"
    assert sc.ports_obs("node-f", {"tcp:0.0.0.0:22": "-"}, base_ports, CFG) == []  # a disappearance and '-' are no alarm


def test_parse_wgpeers_and_new_peer():
    peers = sc.parse_wgpeers(WG)
    assert peers == [f"wg0:{K1}", f"wg0:{K2}", f"wg1:{K1}"]
    o = sc.wgpeers_obs("node-f", peers, [f"wg0:{K1}", f"wg1:{K1}", "wg2:gone"])
    assert lv(o) == {f"wg0:{K2}": "warn"} and o[0].text.startswith("новый пир wg0: BBBBBBBBBBBB")


def test_parse_snapshot_and_truncated_sections_are_errors():
    items, errors = sc.parse_snapshot(SNAP)
    assert items["users:root"] == "0 /bin/bash" and items["fw:ufw:22/tcp ALLOW IN Anywhere"] == "rule" and errors == {}
    items2, errors2 = sc.parse_snapshot(SNAP + "sshd\terror\tRuntimeError: sshd -T: код 1\nunits\terror\ttruncated\n"
                                        "fw\terror\ttruncated\n[agent-ro-gate: скрыто секретов: 0]\n")
    assert errors2 == {"sshd": "RuntimeError: sshd -T: код 1", "units": "truncated", "fw": "truncated"} and "users:root" in items2
    assert sc.parse_snapshot("мусор\n") is None and sc.parse_snapshot("") is None


def mutate(**changes):
    items, _ = sc.parse_snapshot(SNAP)
    for k, v in changes.items():
        if v is None:
            items.pop(k, None)
        else:
            items[k] = v
    return items


def test_snapshot_obs_levels():
    base_items, _ = sc.parse_snapshot(SNAP)
    cur = mutate(**{"users:evil": "1001 /bin/bash", "groups:sudo": "evil ubuntu", "keys:/root/.ssh/authorized_keys": SHA_B + " 3",
                    "sudoers:/etc/sudoers.d/x": "s2", "shadow:/etc/shadow": "sh2", "sshd:permitrootlogin": "yes",
                    "sshd:allowusers": None, "sshd:trustedusercakeys": "/etc/ssh/ca.pub", "sshd:file:/etc/ssh/sshd_config": "f2",
                    "cron:/etc/cron.d/backup": None, "persist:/etc/ld.so.preload": "p1", "persist:/etc/rc.local": "r1",
                    "units:evil.service": "u9", "units:snap-certbot-1234.mount": None, "units:snap-certbot-1300.mount": "u6",
                    "fw:ufw": "inactive", "fw:fail2ban": "inactive"})
    o = sc.snapshot_obs("example-prod", cur, {}, base_items, CFG)
    by = {(x.check, x.subject): x for x in o}
    assert by[("sec:users", "evil")].level == "crit" \
        and by[("sec:users", "evil")].text == "пользователь evil: добавлено, 1001 /bin/bash"
    assert by[("sec:groups", "sudo")].level == "crit" \
        and by[("sec:groups", "sudo")].text == "группа sudo: изменено, ubuntu → evil ubuntu"
    k = by[("sec:keys", "/root/.ssh/authorized_keys")]
    assert k.level == "crit" and k.value == SHA_B + " 3" \
        and k.text == "ключи входа SSH пользователя root (/root/.ssh/authorized_keys): изменено, строк 2 → 3"
    assert by[("sec:sudoers", "/etc/sudoers.d/x")].level == "crit"
    assert by[("sec:shadow", "/etc/shadow")].level == "warn"
    assert by[("sec:sshd", "permitrootlogin")].level == "crit"
    assert by[("sec:sshd", "allowusers")].level == "crit" and by[("sec:sshd", "allowusers")].value == sc.REMOVED
    assert by[("sec:sshd", "trustedusercakeys")].level == "crit"
    assert by[("sec:sshd", "file:/etc/ssh/sshd_config")].level == "crit"
    assert by[("sec:cron", "/etc/cron.d/backup")].level == "warn" and by[("sec:cron", "/etc/cron.d/backup")].value == sc.REMOVED
    assert by[("sec:persist", "/etc/ld.so.preload")].level == "crit" and by[("sec:persist", "/etc/rc.local")].level == "warn"
    assert by[("sec:units", "evil.service")].level == "warn"
    assert ("sec:units", "snap-certbot-1234.mount") not in by and ("sec:units", "snap-certbot-1300.mount") not in by
    assert by[("sec:fw", "ufw")].level == "crit" and by[("sec:fw", "fail2ban")].level == "crit"
    allow_in = mutate(**{"fw:ufw:default": "allow (incoming), allow (outgoing), disabled (routed)"})
    assert lv(sc.snapshot_obs("node-f", allow_in, {}, base_items, CFG)) == {"ufw:default": "crit"}
    assert ("sec:users", "root") not in by
    by2 = {(x.check, x.subject): x.level for x in sc.snapshot_obs("node-f", cur, {}, base_items, CFG)}
    assert by2[("sec:users", "evil")] == "warn" and by2[("sec:sshd", "permitrootlogin")] == "crit"
    assert by2[("sec:fw", "ufw")] == "warn" and by2[("sec:persist", "/etc/ld.so.preload")] == "crit"
    assert by2[("sec:sshd", "file:/etc/ssh/sshd_config")] == "warn"
    o3 = sc.snapshot_obs("example-prod", mutate(**{"fw:ufw:22/tcp ALLOW IN 198.51.100.2": "rule"}), {}, base_items, CFG)
    assert lv(o3) == {"ufw:22/tcp ALLOW IN 198.51.100.2": "warn"}


def test_snapshot_obs_skips_errored_sections():
    base_items, _ = sc.parse_snapshot(SNAP)
    cur = {k: v for k, v in base_items.items() if not k.startswith("sshd:")}
    assert sc.snapshot_obs("example-prod", cur, {"sshd": "ошибка"}, base_items, CFG) == []
    o = sc.snapshot_obs("example-prod", cur, {}, base_items, CFG)
    assert len(o) == 7 and all(x.check == "sec:sshd" and x.value == sc.REMOVED for x in o)


def test_snapshot_obs_describes_opaque_values():
    """The producer's values (symlink, big, error, `-`) are not passed off as a line count and do not collapse into "(contents)"."""
    base_items, _ = sc.parse_snapshot(SNAP)

    def text(server, key, value, base=base_items):
        o = sc.snapshot_obs(server, mutate(**{key: value}), {}, base, CFG)
        assert len(o) == 1, o
        return o[0].text

    t = text("node-f", "keys:/root/.ssh/authorized_keys", "symlink -> /etc/shadow")
    assert "symlink -> /etc/shadow" in t and "строк" not in t
    t = text("node-f", "keys:/root/.ssh/authorized_keys", "big 2000000")
    assert "big 2000000" in t and "строк" not in t
    assert "error PermissionError" in text("node-f", "keys:/root/.ssh/authorized_keys", "error PermissionError")
    assert text("node-f", "persist:/etc/ld.so.preload", SHA_A) == "автозапуск /etc/ld.so.preload: добавлено"
    assert text("node-f", "keys:/home/ubuntu/.ssh/authorized_keys", SHA_A + " 2") \
        == "ключи входа SSH пользователя ubuntu (/home/ubuntu/.ssh/authorized_keys): добавлено, строк 2"
    assert text("node-f", "keys:/root/.ssh/authorized_keys", "-") \
        == "ключи входа SSH пользователя root (/root/.ssh/authorized_keys): удалено, строк 2"
    assert text("node-f", "keys:/etc/ssh/keys/deploy", SHA_A + " 1") \
        == "ключи входа SSH /etc/ssh/keys/deploy: добавлено, строк 1"
    sym = dict(base_items, **{"units:multi-user.target.wants/nginx.service": "symlink -> /tmp/evil.service"})
    t = text("node-f", "units:multi-user.target.wants/nginx.service", "symlink -> /lib/systemd/system/nginx.service",
             base=sym)
    assert "symlink -> /tmp/evil.service → symlink -> /lib/systemd/system/nginx.service" in t
    assert text("node-f", "sshd:file:/etc/ssh/sshd_config", "f2").endswith(": изменено содержимое")
    assert text("node-f", "sudoers:/etc/sudoers", SHA_B, base=dict(base_items, **{"sudoers:/etc/sudoers": SHA_A})) \
        == "правила sudo /etc/sudoers: изменено содержимое"


# --- key fingerprints -------------------------------------------------------------------

AK = "/root/.ssh/authorized_keys"
KP = "keyprints:" + AK
HEAD = f"ключи входа SSH пользователя root ({AK})"


def keys_change(old_prints, new_prints, old=SHA_A + " 2", new=SHA_B + " 3", pair_old=True, pair_new=True,
                errors=None, server="node-f"):
    base_items = mutate(**{"keys:" + AK: old})
    if pair_old:
        base_items[KP] = old
    base_items.update({f"{KP} {k}": v for k, v in old_prints.items()})
    cur = mutate(**{"keys:" + AK: new})
    if pair_new:
        cur[KP] = new
    cur.update({f"{KP} {k}": v for k, v in new_prints.items()})
    return sc.snapshot_obs(server, cur, errors or {}, base_items, CFG)


def test_keys_incident_names_added_and_removed_keys():
    o = keys_change({"ED25519 SHA256:old": "- owner@pc", "RSA SHA256:same": "- ci"},
                    {"RSA SHA256:same": "- ci", "ED25519 SHA256:new1": "command,no-pty dev@laptop", "ECDSA SHA256:new2": "-"})
    assert [x.check for x in o] == ["sec:keys"]  # fingerprints are not separate problems
    assert o[0].value == SHA_B + " 3"  # the problem's value is as before: accept's signature does not change
    assert o[0].text == (f"{HEAD}: изменено, строк 2 → 3; добавлены ключи: ECDSA SHA256:new2, ED25519 SHA256:new1 "
                         "«dev@laptop» (принудительная команда, no-pty); удалён ключ ED25519 SHA256:old «owner@pc»")


def test_keys_incident_options_comments_and_same_set():
    o = keys_change({"RSA SHA256:same": "- ci"}, {"RSA SHA256:same": "from ci2"})
    assert o[0].text.endswith("; изменены опции или комментарий ключа RSA SHA256:same «ci2» (только с адресов from)")
    assert keys_change({"RSA SHA256:same": "- ci"}, {"RSA SHA256:same": "- ci"})[0].text.endswith(
        "строк 2 → 3; набор ключей тот же")


def test_keys_incident_many_keys_shortened_and_comment_printable():
    new = {f"ED25519 SHA256:k{i}": "- c" for i in range(5)}
    new["ED25519 SHA256:k0"] = "- evil\x1b]0;«x»"
    t = keys_change({}, new)[0].text
    assert t.endswith("; добавлены ключи: ED25519 SHA256:k0 «evil?]0;?x?», ED25519 SHA256:k1 «c», "
                      "ED25519 SHA256:k2 «c» и ещё 2")


def test_keys_incident_file_added_or_removed():
    t = keys_change({}, {"ED25519 SHA256:a": "- a"}, old="-", pair_old=False)[0].text
    assert t == f"{HEAD}: добавлено, строк 3; добавлен ключ ED25519 SHA256:a «a»"
    t = keys_change({"ED25519 SHA256:a": "- a"}, {}, new="-", pair_new=False)[0].text
    assert t == f"{HEAD}: удалено, строк 2; удалён ключ ED25519 SHA256:a «a»"


def test_keys_incident_without_matching_prints_says_so_or_stays_as_before():
    unknown = f"{HEAD}: изменено, строк 2 → 3; какие ключи изменились — не определить: в эталоне нет их отпечатков"
    assert keys_change({}, {"ED25519 SHA256:a": "- a"}, pair_old=False)[0].text == unknown  # a baseline from before fingerprints
    stale = keys_change({"ED25519 SHA256:a": "- a"}, {"ED25519 SHA256:a": "- a"}, pair_old=False)
    assert stale[0].text == unknown  # the baseline's fingerprints are of another file version
    plain = f"{HEAD}: изменено, строк 2 → 3"
    assert keys_change({}, {}, pair_new=False)[0].text == plain  # an old gateway's snapshot
    assert keys_change({}, {"ED25519 SHA256:a": "- a"}, errors={"keyprints": "truncated"})[0].text == plain


def test_info_sections_raise_nothing_by_themselves():
    base_items, _ = sc.parse_snapshot(SNAP)
    cur = dict(base_items, **{KP: SHA_A + " 2", f"{KP} ED25519 SHA256:a": "- a",
                              "pubkeys:/root/.ssh/id_ed25519.pub ED25519 SHA256:b": "root@box"})
    assert sc.snapshot_obs("example-prod", cur, {}, base_items, CFG) == []
    assert sc.snapshot_obs("example-prod", base_items, {}, cur, CFG) == []
    assert "keyprints" not in sc.SECTIONS and "pubkeys" not in sc.SECTIONS


def snap_text(*lines):
    return "# agent-sec-snapshot v1\n" + "".join(ln + "\n" for ln in lines)


def test_login_names_the_key_file_and_where_the_key_lies():
    b = base("node-f", "example-stage")
    st = state()
    st["current"]["node-f"] = {"at": NOW.isoformat(), "text": snap_text(
        "keyprints\t/home/git/.ssh/authorized_keys\t" + SHA_A + " 1",
        "keyprints\t/home/git/.ssh/authorized_keys ED25519 SHA256:d\tcommand deploy")}
    st["current"]["example-stage"] = {"at": NOW.isoformat(), "text": snap_text(
        "pubkeys\t/home/dev2/.ssh/jobs.pub ED25519 SHA256:d\tdev2@stage",
        "pubkeys\t/root/.ssh/other.pub ED25519 SHA256:x\t-")}
    o = sc.logins_obs("node-f", auth(login("198.51.100.77", "git@ED25519 SHA256:d", user="git")), st, b, CFG, NOW)
    assert o[0].text.endswith("; ключ из /home/git/.ssh/authorized_keys «deploy» (принудительная команда); "
                              "лежит на example-stage: /home/dev2/.ssh/jobs.pub «dev2@stage»")
    o = sc.logins_obs("node-f", auth(login("198.51.100.77", "root@ED25519 SHA256:zzz")), st, b, CFG, NOW)
    assert [x.text.endswith("всего 1)") for x in o if x.subject == "root@ED25519 SHA256:zzz"] == [True]


def test_key_places_and_report():
    current = {"node-f": {"at": "2026-10-06T11:00:00+03:00", "text": snap_text(
        "keyprints\t/home/git/.ssh/authorized_keys ED25519 SHA256:d\tcommand deploy",
        "keyprints\t/root/.ssh/authorized_keys RSA SHA256:r\t-")},
        "example-stage": {"at": "2026-10-06T11:05:00+03:00", "text": snap_text(
            "pubkeys\t/home/dev2/.ssh/jobs.pub ED25519 SHA256:d\tdev2@stage")},
        "broken": {"at": "x", "text": "мусор"}}
    places = sc.key_places(current)
    assert sorted(places) == ["SHA256:d", "SHA256:r"]
    b = base("node-f", "example-stage")
    b.server("example-stage")["identities"].update({"git@ED25519 SHA256:d": "dev2", "root@password": ""})
    b.server("node-f")["identities"]["root@ED25519 SHA256:old"] = ""
    accepted = sc.accepted_keys(b)
    assert accepted == {"SHA256:d": [("example-stage", "git", "ED25519", "dev2")],
                        "SHA256:old": [("node-f", "root", "ED25519", "")]}
    assert sc.keys_report(places, accepted, "jobs") == [
        "ED25519 SHA256:d",
        "  входит на node-f: /home/git/.ssh/authorized_keys «deploy» (принудительная команда)",
        "  лежит на example-stage: /home/dev2/.ssh/jobs.pub «dev2@stage»",
        "  вход принят: example-stage git «dev2»"]
    assert [ln for ln in sc.keys_report(places, accepted, "") if not ln.startswith(" ")] == \
        ["ED25519 SHA256:d", "ED25519 SHA256:old", "RSA SHA256:r"]
    assert sc.keys_report(places, accepted, "old")[1:] == ["  вход принят: node-f root"]
    assert sc.keys_report(places, accepted, "node-f")[0] == "ED25519 SHA256:d"  # the whole group: where else this key is
    assert sc.keys_report(places, accepted, "SHA256:r") == ["RSA SHA256:r", "  входит на node-f: /root/.ssh/authorized_keys"]
    assert sc.keys_report(places, accepted, "нет такого") == []


def test_accepting_keys_takes_the_prints_of_that_file_version():
    b = base("node-f")
    snap = b.server("node-f")["snapshot"]
    snap.update({"keys:" + AK: SHA_A + " 2", KP: SHA_A + " 2", f"{KP} ED25519 SHA256:old": "- a",
                 "keyprints:/home/x/.ssh/authorized_keys ED25519 SHA256:keep": "-"})
    text = snap_text(f"keys\t{AK}\t{SHA_B} 3", f"keyprints\t{AK}\t{SHA_B} 3", f"keyprints\t{AK} ED25519 SHA256:new\t- b")
    b.accept("sec:keys", "node-f", AK, SHA_B + " 3")
    sc.accept_keyprints(b, "node-f", AK, SHA_B + " 3", {"text": text})
    assert {k: v for k, v in snap.items() if k.startswith("keyprints:")} == {
        KP: SHA_B + " 3", f"{KP} ED25519 SHA256:new": "- b", "keyprints:/home/x/.ssh/authorized_keys ED25519 SHA256:keep": "-"}
    sc.accept_keyprints(b, "node-f", AK, SHA_A + " 9", {"text": text})  # the snapshot is already of another version — unknown
    assert not any(k.startswith(KP) for k in snap)


def test_snapshot_obs_home_without_keys_is_no_change():
    """P-350: a new service user — a new home without authorized_keys (`-`); no login by key was added."""
    base_items, _ = sc.parse_snapshot(SNAP)
    key = "keys:/home/digi-repo/.ssh/authorized_keys"
    assert sc.snapshot_obs("example-prod", mutate(**{key: "-"}), {}, base_items, CFG) == []
    assert sc.snapshot_obs("example-prod", base_items, {}, dict(base_items, **{key: "-"}), CFG) == []
    o = sc.snapshot_obs("example-prod", mutate(**{key: SHA_A + " 1"}), {}, dict(base_items, **{key: "-"}), CFG)
    assert [(x.level, x.text) for x in o] == [
        ("crit", "ключи входа SSH пользователя digi-repo (/home/digi-repo/.ssh/authorized_keys): добавлено, строк 1")]


def test_snapshot_obs_fw_transient_and_ufw_off():
    base_items, _ = sc.parse_snapshot(SNAP)
    assert lv(sc.snapshot_obs("example-prod", mutate(**{"fw:fail2ban": "activating"}), {}, base_items, CFG)) \
        == {"fail2ban": "warn"}
    assert lv(sc.snapshot_obs("example-prod", mutate(**{"fw:ufw": "reloading"}), {}, base_items, CFG)) == {"ufw": "warn"}
    # a disabled ufw prints no Default and no rules — one observation, not N+2
    off = {k: v for k, v in mutate(**{"fw:ufw": "inactive"}).items() if not k.startswith("fw:ufw:")}
    assert lv(sc.snapshot_obs("example-prod", off, {}, base_items, CFG)) == {"ufw": "crit"}


def test_parse_listeners_sanitizes_process_name():
    """The process sets its own name (prctl PR_SET_NAME) and ss does not escape it: into the baseline and the printout —
    without control characters."""
    ss = 'tcp   LISTEN 0      1      0.0.0.0:4444        0.0.0.0:*     users:(("x\x1b[31m\x07y",pid=9,fd=3))\n'
    ss += 'tcp   LISTEN 0      1      0.0.0.0:4445        0.0.0.0:*     users:(("' + "z" * 40 + '",pid=9,fd=3))\n'
    ss += 'tcp   LISTEN 0      1      0.0.0.0:4446        0.0.0.0:*     users:(("имя",pid=9,fd=3))\n'
    ss += 'tcp   LISTEN 0      1      0.0.0.0:4447        0.0.0.0:*     users:(("x\x1b[31m\ny",pid=9,fd=3))\n'
    out = sc.parse_listeners(ss)
    assert out["tcp:0.0.0.0:4447"] == "-"  # a newline breaks the ss line: the owner is not parsed
    assert out["tcp:0.0.0.0:4444"] == "x?[31m?y" and out["tcp:0.0.0.0:4445"] == "z" * 32
    assert out["tcp:0.0.0.0:4446"] == "???"  # non-ASCII — "?" too: the baseline's value is stable and printable
    assert all(not any(ord(ch) < 0x20 or ord(ch) > 0x7e for ch in v) for v in out.values())


def test_ports_ipv4_mapped_loopback_is_warn_on_critical():
    o = sc.ports_obs("example-prod", {"tcp:[::ffff:127.0.0.1]:9000": "php-fpm", "tcp:0.0.0.0:4444": "nc"}, {}, CFG)
    assert lv(o) == {"tcp:[::ffff:127.0.0.1]:9000": "warn", "tcp:0.0.0.0:4444": "crit"}


def test_parse_listeners_multiple_owners_sorted():
    ss = ('tcp   LISTEN 0      4096   0.0.0.0:22          0.0.0.0:*     users:(("systemd",pid=1,fd=50),("sshd",pid=800,fd=3))\n'
          'tcp   LISTEN 0      4096   0.0.0.0:23          0.0.0.0:*     users:(("sshd",pid=800,fd=3),("systemd",pid=1,fd=50))\n')
    assert sc.parse_listeners(ss) == {"tcp:0.0.0.0:22": "sshd+systemd", "tcp:0.0.0.0:23": "sshd+systemd"}


def test_posture_obs():
    items, _ = sc.parse_snapshot(SNAP)
    assert lv(sc.posture_obs("s", items, CFG)) == {"ssh-password": "ok", "ssh-root": "ok", "ufw": "ok", "fail2ban": "ok"}
    bad = mutate(**{"sshd:passwordauthentication": "yes", "sshd:permitrootlogin": "yes", "fw:ufw": "inactive", "fw:fail2ban": "absent"})
    o = sc.posture_obs("s", bad, CFG)
    assert lv(o) == {"ssh-password": "warn", "ssh-root": "warn", "ufw": "warn", "fail2ban": "warn"}
    assert "prohibit-password" in [x.text for x in o if x.subject == "ssh-root"][0]
    assert lv(sc.posture_obs("s", mutate(**{"fw:ufw": "absent"}), CFG))["ufw"] == "ok"
    assert lv(sc.posture_obs("s", mutate(**{"sshd:challengeresponseauthentication": "yes"}), CFG))["ssh-password"] == "warn"
    kbd = sc.posture_obs("s", mutate(**{"sshd:kbdinteractiveauthentication": "yes"}), CFG)
    assert lv(kbd)["ssh-password"] == "warn"
    assert "KbdInteractiveAuthentication" in [x.text for x in kbd if x.subject == "ssh-password"][0]


# --- updates / reboot / automatic updates / EOL / load ----------------------------------

APT = ("Listing...\n"
       "libgbm1/noble-updates 25.2.8-0ubuntu0.24.04.4 amd64 [upgradable from: 25.2.8-0ubuntu0.24.04.2]\n"
       "openssh-server/noble-updates,noble-security 1:9.6p1-3ubuntu13.5 amd64 [upgradable from: 1:9.6p1-3ubuntu13.4]\n"
       "linux-image-6.8.0-45-generic/noble-security 6.8.0-45.45 amd64 [upgradable from: 6.8.0-44.44]\n"
       "curl/noble-security 8.5.0-2ubuntu10.4 amd64 [upgradable from: 8.5.0-2ubuntu10.3]\n"
       "WARNING: apt does not have a stable CLI interface. Use with caution in scripts.\n")
DNF = ("kernel.x86_64    4.18.0-553.el8    baseos\nopenssl-libs.x86_64   1:1.1.1k-14.el8    baseos\n"
       "very-long-package-name-that-wraps.noarch\n                 1.2.3-4.el8    appstream\n")
NEEDRESTART = ("NEEDRESTART-VER: 3.6\nNEEDRESTART-KCUR: 6.8.0-44-generic\nNEEDRESTART-KEXP: 6.8.0-45-generic\n"
               "NEEDRESTART-KSTA: 3\nNEEDRESTART-SVC: cron.service\n")


def test_parse_apt_and_dnf_security():
    assert sc.parse_apt_security(APT) == {"libgbm1": False, "openssh-server": True,
                                          "linux-image-6.8.0-45-generic": True, "curl": True}
    assert sc.parse_dnf_security(DNF) == {"kernel": True, "openssl-libs": True, "very-long-package-name-that-wraps": True}
    assert sc.parse_dnf_security("") == {}
    # a wrap of the repository column only: "package.arch version" + the repo on the next line
    wrapped = "averyveryverylongpackagename-libs.x86_64 1:1.2.3-4.el8_10\n                            appstream\n"
    assert sc.parse_dnf_security(wrapped) == {"averyveryverylongpackagename-libs": True}


def test_updates_obs_levels_and_first_seen():
    st = state()
    pending = sc.parse_apt_security(APT)
    o = sc.updates_obs("s", pending, st, CFG, NOW)
    assert lv(o) == {"security": "ok"} and o[0].value == "3 security, старейшее 0 дн"
    assert set(st["updates"]["s"]) == {"openssh-server", "linux-image-6.8.0-45-generic", "curl"}
    o = sc.updates_obs("s", pending, st, CFG, NOW + timedelta(days=2))
    assert lv(o) == {"security": "warn"}  # a critical security one waits longer than a day
    assert o[0].text.startswith("security-обновлений 3, ждут до 2 дн: linux-image-6.8.0-45-generic, openssh-server, curl")
    assert lv(sc.updates_obs("s", pending, st, CFG, NOW + timedelta(days=8))) == {"security": "crit"}  # > crit_days
    st2 = state()
    only_curl = {"curl": True, "libgbm1": False}
    sc.updates_obs("s", only_curl, st2, CFG, NOW)
    assert lv(sc.updates_obs("s", only_curl, st2, CFG, NOW + timedelta(days=4))) == {"security": "warn"}
    assert lv(sc.updates_obs("s", only_curl, st2, CFG, NOW + timedelta(days=30))) == {"security": "warn"}  # not critical — not 🔴
    o = sc.updates_obs("s", {"libgbm1": False}, st2, CFG, NOW + timedelta(days=31))
    assert lv(o) == {"security": "ok"} and st2["updates"]["s"] == {}


def test_updates_critical_packages_without_security_flag():
    """Third-party repositories (MySQL, nginx.org, the php PPA) do not mark the suite as security: a critical package is ⚠️ after 7 days."""
    st = state()
    pending = {"nginx-core": False, "php8.3-fpm": False, "mysql-server-8.0": False, "libgbm1": False}
    o = sc.updates_obs("s", pending, st, CFG, NOW)
    assert lv(o) == {"security": "ok"} and o[0].value == "0 security + 3 критичных без пометки, старейшее 0 дн"
    assert set(st["updates"]["s"]) == {"nginx-core", "php8.3-fpm", "mysql-server-8.0"}
    assert lv(sc.updates_obs("s", pending, st, CFG, NOW + timedelta(days=6))) == {"security": "ok"}
    o = sc.updates_obs("s", pending, st, CFG, NOW + timedelta(days=7))
    assert lv(o) == {"security": "warn"}
    assert o[0].text.startswith("security-обновлений 0, критичных без пометки 3, ждут до 7 дн: mysql-server-8.0, nginx-core, php8.3-fpm")
    assert lv(sc.updates_obs("s", pending, st, CFG, NOW + timedelta(days=60))) == {"security": "warn"}  # without the mark — not 🔴
    assert st["updates_sec"]["s"] == []  # the weekly line counts security by this list, not by everything tracked


def test_updates_obs_keeps_security_list():
    st = state()
    sc.updates_obs("s", {"curl": True, "nginx-core": False, "openssh-server": True}, st, CFG, NOW)
    assert st["updates_sec"]["s"] == ["curl", "openssh-server"] and set(st["updates"]["s"]) == {"curl", "nginx-core", "openssh-server"}


def test_parse_needrestart_needs_restarting_and_reboot_obs():
    assert sc.parse_needrestart(NEEDRESTART) is True
    assert sc.parse_needrestart(NEEDRESTART.replace("KSTA: 3", "KSTA: 1")) is False
    assert sc.parse_needrestart(NEEDRESTART.replace("KSTA: 3", "KSTA: 0")) is None  # 0 = could not be determined
    assert sc.parse_needrestart("мусор") is None
    assert sc.parse_needs_restarting(1, "Core libraries or services have been updated since boot-up:\n  * kernel\nReboot is required\n") is True
    assert sc.parse_needs_restarting(0, "No core libraries or services have been updated since boot-up.\nReboot should not be necessary.\n") is False
    assert sc.parse_needs_restarting(1, "Error: Failed to synchronize cache\n") is None
    st = state()
    assert lv(sc.reboot_obs("s", True, st, CFG, NOW)) == {"-": "ok"}
    assert lv(sc.reboot_obs("s", True, st, CFG, NOW + timedelta(days=7))) == {"-": "warn"}
    assert lv(sc.reboot_obs("s", False, st, CFG, NOW + timedelta(days=8))) == {"-": "ok"} and "s" not in st["reboot"]


def test_autoupdates_obs():
    conf = 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n'
    three = "enabled\nenabled\nenabled\n"
    assert lv(sc.autoupdates_obs("s", three, conf, CFG)) == {"-": "ok"}
    assert lv(sc.autoupdates_obs("s", three, conf.replace('Upgrade "1"', 'Upgrade "7"'), CFG)) == {"-": "ok"}
    assert lv(sc.autoupdates_obs("s", "enabled\ndisabled\nenabled\n", conf, CFG)) == {"-": "warn"}
    assert lv(sc.autoupdates_obs("s", three, conf.replace('Upgrade "1"', 'Upgrade "0"'), CFG)) == {"-": "warn"}
    assert lv(sc.autoupdates_obs("s", "", "", CFG)) == {"-": "warn"}
    commented = '// APT::Periodic::Unattended-Upgrade "1";\nAPT::Periodic::Unattended-Upgrade "0";\n'
    assert lv(sc.autoupdates_obs("s", three, commented, CFG)) == {"-": "warn"}
    assert lv(sc.autoupdates_obs("s", three, '// APT::Periodic::Unattended-Upgrade "1";\n', CFG)) == {"-": "warn"}
    assert lv(sc.autoupdates_obs("s", three, '  APT::Periodic::Unattended-Upgrade "1";\n', CFG)) == {"-": "ok"}
    assert lv(sc.autoupdates_obs("s", "enabled\ndisabled\n", "", CFG, dnf=True)) == {"-": "ok"}  # dnf-automatic-install.timer
    assert lv(sc.autoupdates_obs("s", "disabled\nenabled\n", "[commands]\napply_updates = yes\n", CFG, dnf=True)) == {"-": "ok"}
    assert lv(sc.autoupdates_obs("s", "disabled\nenabled\n", "[commands]\napply_updates = no\n", CFG, dnf=True)) == {"-": "warn"}
    assert lv(sc.autoupdates_obs("s", "Failed to get unit file state\n", "", CFG, dnf=True)) == {"-": "warn"}


def test_os_release_and_eol():
    assert sc.parse_os_release('NAME="Ubuntu"\nID=ubuntu\nVERSION_ID="22.04"\n') == ("ubuntu", "22.04")
    assert sc.parse_os_release('ID="almalinux"\nVERSION_ID="8.10"\n') == ("almalinux", "8.10")
    assert lv(sc.eol_obs("s", "ubuntu", "24.04", CFG, NOW)) == {"-": "ok"}
    o = sc.eol_obs("s", "ubuntu", "22.04", CFG, datetime(2027, 3, 1, tzinfo=UTC3))
    assert lv(o) == {"-": "warn"} and "осталось 60 дн" in o[0].text
    o = sc.eol_obs("s", "ubuntu", "22.04", CFG, datetime(2027, 6, 1, tzinfo=UTC3))
    assert lv(o) == {"-": "warn"} and "истекла" in o[0].text
    assert lv(sc.eol_obs("s", "almalinux", "8.10", CFG, NOW)) == {"-": "ok"}  # by the major version
    o = sc.eol_obs("s", "freebsd", "14", CFG, NOW)
    assert lv(o) == {"-": "ok"} and o[0].value == "н/д"


def test_load_and_cores():
    assert sc.parse_cores("processor\t: 0\nmodel name: x\nprocessor\t: 1\n") == 2
    assert sc.parse_load1(" 12:00:05 up 3 days,  1 user,  load average: 4.50, 0.1, 0.1\n") == 4.5
    assert sc.parse_load1(" 12:00:05 up 3 days,  load average: 0,75, 0,10, 0,05\n") == 0.75
    assert sc.parse_load1("мусор") is None
    st = state()
    assert sc.load_obs("example-prod", 9.0, st, CFG) == []
    st["cores"]["example-prod"] = 2
    assert lv(sc.load_obs("example-prod", 9.0, st, CFG)) == {"-": "ok"}
    assert lv(sc.load_obs("example-prod", 9.0, st, CFG)) == {"-": "ok"}
    o = sc.load_obs("example-prod", 9.0, st, CFG)
    assert lv(o) == {"-": "crit"} and o[0].value == "9 на 2 ядер"
    assert lv(sc.load_obs("example-prod", 5.0, st, CFG)) == {"-": "warn"}
    assert lv(sc.load_obs("example-prod", 1.0, st, CFG)) == {"-": "ok"} and st["load_streak"]["example-prod"] == 0
    st["cores"]["node-f"] = 1
    for _ in range(3):
        o = sc.load_obs("node-f", 6.0, st, CFG)
    assert lv(o) == {"-": "warn"}


# --- what a plan window never takes into the baseline; a unit's own objects ------------------------------

@pytest.mark.parametrize("section,item,old,new,expected", [
    ("keys", "/root/.ssh/authorized_keys", None, "a" * 64 + " 1", True),
    ("sudoers", "/etc/sudoers.d/x", None, "b" * 64, True),
    ("groups", "sudo", "alice", "alice,bob", True),
    ("users", "svc", None, "0 /bin/bash", True),
    ("users", "svc", "1001 /bin/bash", "0 /bin/bash", True),
    ("users", "_chrony", None, "112 /usr/sbin/nologin", False),
    ("users", "root", "0 /bin/bash", "0 /bin/zsh", False),
    ("sshd", "passwordauthentication", "no", "yes", True),
    ("sshd", "file:/etc/ssh/sshd_config", "c" * 64, "d" * 64, False),
    ("persist", "/etc/ld.so.preload", None, "e" * 64, True),
    ("persist", "/etc/rc.local", None, "e" * 64, False),
    ("fw", "ufw", "active", "inactive", True),
    ("fw", "ufw:default", "deny (incoming)", "allow (incoming), allow (outgoing)", True),
    ("fw", "ufw:rule:22", None, "ALLOW", False),
    ("units", "x.service", None, "f" * 64, False),
    ("cron", "/etc/cron.d/x", None, "f" * 64, False),
])
def test_dangerous(section, item, old, new, expected):
    assert sc.dangerous(section, item, old, new) is expected


@pytest.mark.parametrize("item,unit", [
    ("x.service", "x.service"),
    ("multi-user.target.wants/x.service", "x.service"),
    ("sockets.target.requires/y.socket", "y.socket"),
    ("x.service.d/override.conf", "x.service"),
])
def test_unit_of(item, unit):
    assert sc.unit_of(item) == unit


# --- a systemd unit and its links are one observation ----------------------------------------------------

def test_a_unit_and_its_enablement_link_are_one_observation():
    items = {"units:cve-x-nft.service": "a" * 64,
             "units:multi-user.target.wants/cve-x-nft.service": "symlink -> /etc/systemd/system/cve-x-nft.service"}
    obs = sc.snapshot_obs("node-a", items, {}, {}, CFG)
    assert [(o.check, o.subject, o.level) for o in obs] == [("sec:units", "cve-x-nft.service", "warn")]
    assert json.loads(obs[0].value) == {
        "cve-x-nft.service": "a" * 64,
        "multi-user.target.wants/cve-x-nft.service": "symlink -> /etc/systemd/system/cve-x-nft.service"}
    assert obs[0].value == sc.units_value(json.loads(obs[0].value))
    assert obs[0].text == "юнит systemd cve-x-nft.service: добавлено; включён в multi-user.target"


def test_enabling_an_existing_unit_is_its_own_unit_observation():
    base = {"units:x.service": "a" * 64}
    items = dict(base, **{"units:timers.target.wants/x.service": "symlink -> /lib/systemd/system/x.service"})
    (o,) = sc.snapshot_obs("node-a", items, {}, base, CFG)
    assert o.subject == "x.service" and o.text == "юнит systemd x.service: включён в timers.target"
    assert json.loads(o.value) == {"timers.target.wants/x.service": "symlink -> /lib/systemd/system/x.service"}


def test_removing_a_unit_its_link_and_its_drop_in_is_one_observation():
    base = {"units:old.service": "a" * 64, "units:multi-user.target.wants/old.service": "symlink -> x",
            "units:old.service.d/override.conf": "b" * 64}
    (o,) = sc.snapshot_obs("node-a", {}, {}, base, CFG)
    assert o.subject == "old.service" and json.loads(o.value) == {
        "old.service": None, "multi-user.target.wants/old.service": None, "old.service.d/override.conf": None}
    assert o.text == ("юнит systemd old.service: удалено; выключен из multi-user.target; "
                      "drop-in override.conf: удалено")


def test_units_ignore_still_matches_the_unit_and_its_link():
    items = {"units:snap.lxd.daemon.service": "a" * 64,
             "units:multi-user.target.wants/snap.lxd.daemon.service": "symlink -> x"}
    assert sc.snapshot_obs("node-a", items, {}, {}, CFG) == []
