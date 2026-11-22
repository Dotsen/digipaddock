"""kd-alert: a failed unit is a notice in the cabinet; a critical one is also duplicated to Telegram."""

import io
import os
import re
import subprocess
from pathlib import Path

import pytest

COMMON = Path(__file__).resolve().parent.parent
NOW = 1_000_000.0
ALERTS = "https://cabinet.example/incidents"  # where the cabinet lists the platform's alerts

JOURNAL = (
    "Started DigiPaddock\n"
    "kd-board watch: доска не прочитана — тик пропущен\n"
    "что-то постороннее\n"
    "kd-board watch: конфигурация не прочитана (OSError)\n"
    "хвост без причины\n"
)


class Sender:
    def __init__(self, ok=True):
        self.sent, self.ok = [], ok

    def __call__(self, text):
        self.sent.append(text)
        return self.ok


class Poster:
    def __init__(self, ok=True):
        self.posted, self.ok = [], ok

    def __call__(self, source, level, key, params):
        self.posted.append((source, level, key, params))
        return self.ok


def run(kd_alert, tmp_path, argv=("digipaddock-board-watch",), journal=lambda unit, inv=None: JOURNAL, env=None, now=NOW,
        sender=None, poster=None, pult=True, link=lambda: ALERTS):
    err = io.StringIO()
    sender = sender if sender is not None else Sender()
    poster = poster if poster is not None else Poster()
    pult_env = tmp_path / "pult.env"
    if pult:
        pult_env.write_text("x=1")
    code = kd_alert.main(
        list(argv), journal=journal, clock=lambda: now, stamp_dir=tmp_path / "run", sender=sender, poster=poster,
        link=link, hostname="stage", env=env or {}, pult_env=pult_env, stderr=err)
    return code, poster, sender, err.getvalue()


def test_service_failure_posts_a_critical_notice_and_a_telegram_duplicate(kd_alert, tmp_path):
    code, poster, sender, _ = run(kd_alert, tmp_path)
    assert code == 0
    assert poster.posted == [("alert", "critical", "notice.alert.unit", {
        "host": "stage", "unit": "digipaddock-board-watch.service",
        "reason": "kd-board watch: конфигурация не прочитана (OSError)"})]
    assert len(sender.sent) == 1
    assert (tmp_path / "run" / "alert-digipaddock-board-watch").exists()


def test_duplicate_is_plain_text_with_the_alerts_link(kd_alert, tmp_path):
    _, _, sender, _ = run(kd_alert, tmp_path, journal=lambda unit, inv=None: "kd-cabinet: <b>boom</b> & x\n",
                          argv=("digipaddock-cabinet",))
    (text,) = sender.sent
    assert text == "⚠️ stage · сбой digipaddock-cabinet.service: kd-cabinet: <b>boom</b> & x\n" + ALERTS
    assert "&lt;" not in text and "&amp;" not in text


def test_duplicate_goes_when_the_cabinet_is_down(kd_alert, tmp_path):
    code, _, sender, err = run(kd_alert, tmp_path, poster=Poster(ok=False))
    assert code == 0 and len(sender.sent) == 1
    assert "kd-board watch: конфигурация не прочитана" in err  # the journal keeps what the cabinet missed
    assert (tmp_path / "run" / "alert-digipaddock-board-watch").exists()


def test_both_down_still_logs_and_exits_zero(kd_alert, tmp_path):
    code, _, _, err = run(kd_alert, tmp_path, poster=Poster(ok=False), sender=Sender(ok=False))
    assert code == 0 and "сбой digipaddock-board-watch.service" in err
    assert not (tmp_path / "run" / "alert-digipaddock-board-watch").exists()  # nothing delivered: the next failure speaks


def test_no_pult_env_no_duplicate(kd_alert, tmp_path):
    code, poster, sender, _ = run(kd_alert, tmp_path, pult=False)
    assert code == 0 and len(poster.posted) == 1 and sender.sent == []


def test_no_cabinet_origin_no_link(kd_alert, tmp_path):
    _, _, sender, _ = run(kd_alert, tmp_path, link=lambda: "")
    assert not sender.sent[0].endswith("/incidents") and "\n" not in sender.sent[0]


def test_the_link_of_a_duplicate_is_the_alerts_of_the_cabinet(kd_alert, monkeypatch):
    """The cabinet lists the platform's alerts on the incidents page; the feed is the journal."""
    monkeypatch.setattr(kd_alert, "cabinet_origin", lambda: "https://cabinet.example")
    assert kd_alert.alerts_link() == ALERTS
    monkeypatch.setattr(kd_alert, "cabinet_origin", lambda: "")
    assert kd_alert.alerts_link() == ""


def test_unit_name_comes_from_monitor_unit(kd_alert, tmp_path):
    units = []
    _, poster, _, _ = run(kd_alert, tmp_path, argv=("digipaddock-board-sync",), env={"MONITOR_UNIT": "digipaddock-board-sync.service"},
                          journal=lambda unit, inv=None: units.append(unit) or JOURNAL)
    assert units == ["digipaddock-board-sync.service"] and poster.posted[0][3]["unit"] == "digipaddock-board-sync.service"


@pytest.mark.parametrize("name, unit", [("digipaddock-board-sync", "digipaddock-board-sync.service"),
                                        ("digipaddock-release", "digipaddock-release.service"),
                                        ("digipaddock-monitor", "digipaddock-monitor.service"),
                                        ("hindsight-api", "hindsight-api.service")])
def test_without_monitor_unit_the_name_names_the_unit(kd_alert, tmp_path, name, unit):
    units = []
    run(kd_alert, tmp_path, argv=(name,), env={"MONITOR_UNIT": "bad unit; rm"},
        journal=lambda u, inv=None: units.append(u) or "")
    assert units == [unit]


def test_reason_is_the_last_kd_line_of_the_invocation(kd_alert):
    assert kd_alert.reason_from(JOURNAL, "digipaddock-board-watch") == "kd-board watch: конфигурация не прочитана (OSError)"
    assert kd_alert.reason_from("a\nstart-agent: сервер не отвечает 30 с\n", "x") == "причина не найдена в журнале"
    assert kd_alert.reason_from("sysadmin: started\nsysadmin: running\n", "sysadmin") == "sysadmin: started"
    assert kd_alert.reason_from("ничего\n", "x") == "причина не найдена в журнале"
    traceback = "Traceback (most recent call last):\n  File x\nValueError: bad state\n"
    assert kd_alert.reason_from(traceback, "digipaddock-cabinet", own=True) == "ValueError: bad state"


def test_invocation_id_is_used_for_journal_and_result_is_shown(kd_alert, tmp_path):
    calls = []
    inv = "a" * 32
    _, poster, _, _ = run(kd_alert, tmp_path, journal=lambda unit, i=None: calls.append(i) or "kd-release: сбой: баг\n",
                          argv=("digipaddock-release",),
                          env={"MONITOR_INVOCATION_ID": inv, "MONITOR_SERVICE_RESULT": "exit-code", "MONITOR_EXIT_STATUS": "1"})
    assert calls == [inv]
    assert poster.posted[0][3]["reason"] == "kd-release: сбой: баг (результат: exit-code, код 1)"


@pytest.mark.parametrize("bad", ["", "xyz", "A" * 32, "a" * 31, "a" * 32 + "\n--x", "../" + "a" * 29])
def test_invalid_invocation_id_falls_back_to_unit_journal(kd_alert, tmp_path, bad):
    calls = []
    run(kd_alert, tmp_path, journal=lambda unit, i=None: calls.append(i) or JOURNAL, env={"MONITOR_INVOCATION_ID": bad})
    assert calls == [None]


def test_no_reason_mentions_result(kd_alert, tmp_path):
    _, poster, _, _ = run(kd_alert, tmp_path, journal=lambda unit, i=None: "", env={"MONITOR_SERVICE_RESULT": "timeout"})
    assert poster.posted[0][3]["reason"] == "причина не найдена в журнале (результат: timeout)"


def test_journal_failure_still_alerts(kd_alert, tmp_path):
    def broken(unit, inv=None):
        raise OSError("нет journalctl")

    code, poster, _, _ = run(kd_alert, tmp_path, journal=broken)
    assert code == 0 and "причина не найдена в журнале" in poster.posted[0][3]["reason"]


def test_fresh_stamp_means_silence(kd_alert, tmp_path):
    stamp = tmp_path / "run" / "alert-digipaddock-board-watch"
    stamp.parent.mkdir()
    stamp.write_text("")
    os.utime(stamp, (NOW - 59 * 60, NOW - 59 * 60))
    _, poster, sender, _ = run(kd_alert, tmp_path)
    assert poster.posted == [] and sender.sent == []
    _, poster, _, _ = run(kd_alert, tmp_path, now=NOW + 61 * 60)
    assert len(poster.posted) == 1 and stamp.stat().st_mtime == NOW + 61 * 60


def test_stamps_are_per_name(kd_alert, tmp_path):
    run(kd_alert, tmp_path)
    _, poster, _, _ = run(kd_alert, tmp_path, argv=("digipaddock-release",))
    assert len(poster.posted) == 1


@pytest.mark.parametrize("argv", [(), ("Bad Name",), ("../x",), ("a" * 33,), ("a", "b")])
def test_invalid_name_exits_2(kd_alert, tmp_path, argv):
    code, poster, sender, _ = run(kd_alert, tmp_path, argv=argv)
    assert code == 2 and poster.posted == [] and sender.sent == []


# --- the monitor's fallback (kd-alert kd-monitor) ---

MON_JOURNAL = (
    "Started kd-monitor\n"
    "kd-monitor: node-a · сертификаты: 10, ближайший example.org — 37 дн\n"
    "посторонняя строка\n"
    "kd-monitor: node-a · диск /: 96%\n"
)


def fresh_stamp(tmp_path, name="digipaddock-monitor"):
    stamp = tmp_path / "run" / f"alert-{name}"
    stamp.parent.mkdir(exist_ok=True)
    stamp.write_text("")
    os.utime(stamp, (NOW - 60, NOW - 60))
    return stamp


def test_monitor_fallback_posts_red_as_critical_and_accepted_as_info(kd_alert, tmp_path):
    stamp = fresh_stamp(tmp_path)
    j = MON_JOURNAL + "kd-monitor: принято: P-7: x\nkd-monitor: история снимков node-b: [Errno 13]\n"
    code, poster, sender, _ = run(kd_alert, tmp_path, argv=("digipaddock-monitor",), journal=lambda unit, inv=None: j,
                                  env={"MONITOR_SERVICE_RESULT": "exit-code", "MONITOR_EXIT_STATUS": "3"})
    assert code == 0
    assert poster.posted == [
        ("monitor", "critical", "notice.alert.fallback",
         {"host": "stage", "lines": "node-a · сертификаты: 10, ближайший example.org — 37 дн\nnode-a · диск /: 96%"}),
        ("monitor", "info", "notice.alert.accepted", {"host": "stage", "lines": "P-7: x"}),
        ("monitor", "info", "notice.alert.service", {"host": "stage", "lines": "история снимков node-b: [Errno 13]"}),
    ]
    assert len(sender.sent) == 1 and sender.sent[0].startswith("🔴 stage · монитор: сисадмин недоступен")
    assert stamp.stat().st_mtime == NOW - 60  # the stamp only from a real failure


def test_monitor_accepted_only_is_info_without_duplicate(kd_alert, tmp_path):
    _, poster, sender, _ = run(kd_alert, tmp_path, argv=("digipaddock-monitor",),
                               journal=lambda unit, inv=None: "kd-monitor: принято: P-7: x\n")
    assert [p[1] for p in poster.posted] == ["info"] and sender.sent == []
    assert not (tmp_path / "run" / "alert-digipaddock-monitor").exists()


def test_monitor_error_is_a_unit_failure_and_respects_the_stamp(kd_alert, tmp_path):
    j = "kd-monitor: node-a · диск /: 96%\nkd-monitor: ошибка: state.json недоступен\n"
    _, poster, _, _ = run(kd_alert, tmp_path, argv=("digipaddock-monitor",), journal=lambda unit, inv=None: j,
                          env={"MONITOR_SERVICE_RESULT": "exit-code"})
    assert poster.posted == [("monitor", "critical", "notice.alert.unit", {
        "host": "stage", "unit": "digipaddock-monitor.service",
        "reason": "ошибка: state.json недоступен\nnode-a · диск /: 96% (результат: exit-code)"})]
    _, poster, _, _ = run(kd_alert, tmp_path, argv=("digipaddock-monitor",), journal=lambda unit, inv=None: j, now=NOW + 60)
    assert poster.posted == []


def test_monitor_lines_are_capped(kd_alert, tmp_path):
    j = "".join(f"kd-monitor: s{i} · диск /: 9%\n" for i in range(15))
    _, poster, _, _ = run(kd_alert, tmp_path, argv=("digipaddock-monitor",), journal=lambda unit, inv=None: j)
    assert len(poster.posted[0][3]["lines"].splitlines()) == kd_alert.MONITOR_MAX_LINES


def test_cyrillic_reason_and_lines_fit_the_cabinets_params(kd_alert, tmp_path):
    # Final review: 2000 characters of «—» (3 bytes each) are 6 KB in UTF-8, over the cabinet's 4096 bytes for all
    # params; the cut is by the bytes the cabinet counts, not by characters.
    from kd.cabinet import notices
    _, poster, _, _ = run(kd_alert, tmp_path, argv=("digipaddock-cabinet",),
                          journal=lambda unit, inv=None: "kd-cabinet: " + "—\"ж" * 1000 + "\n")
    _, monitor, _, _ = run(kd_alert, tmp_path, argv=("digipaddock-monitor",), journal=lambda unit, inv=None: "".join(
        f"kd-monitor: s{i} · " + "—" * 400 + "\n" for i in range(11)))
    for _, _, _, params in poster.posted + monitor.posted:
        assert notices._params(params) == params
    assert poster.posted[0][3]["reason"].startswith("kd-cabinet: —\"ж—")
    assert monitor.posted[0][3]["lines"].startswith("s0 · ——")


def test_critical_duplicate_goes_before_the_cabinet_post(kd_alert, tmp_path):
    # Final review: a hung cabinet (accepted, never answers) must not hold the Telegram duplicate back.
    order = []
    sender, poster = Sender(), Poster()
    run(kd_alert, tmp_path, sender=lambda text: order.append("telegram") or sender(text),
        poster=lambda *a: order.append("cabinet") or poster(*a))
    assert order == ["telegram", "cabinet"]


def test_the_cabinet_post_waits_at_most_ten_seconds(kd_alert, monkeypatch):
    from kd.cabinet import client as cabinet_client
    made = []

    class Client:
        def __init__(self, timeout=None, **kw):
            made.append(timeout)

        def call(self, request):
            return {"ok": True}

    monkeypatch.setattr(cabinet_client, "Client", Client)
    post = kd_alert.cabinet_poster()
    assert post("alert", "critical", "notice.alert.unit", {"host": "h", "unit": "u", "reason": "r"}) is True
    assert made and all(t is not None and t <= 10 for t in made)


# --- files ---

def test_script_is_lf_python3():
    data = (COMMON / "bin/kd-alert").read_bytes()
    assert data.startswith(b"#!/usr/bin/env python3\n") and b"\r\n" not in data


def test_script_is_executable_in_git_index():
    out = subprocess.run(["git", "ls-files", "-s", "common/bin/kd-alert"], cwd=COMMON.parent,
                         capture_output=True, text=True)
    if out.returncode != 0 or not out.stdout.strip():
        pytest.skip("not in the git index yet")
    assert out.stdout.startswith("100755")


def test_alert_unit_runs_as_root_oneshot():
    unit = (COMMON / "systemd/digipaddock-alert@.service").read_text(encoding="utf-8")
    assert "Type=oneshot" in unit and "ExecStart=/usr/local/bin/kd-alert %i\n" in unit
    assert "TimeoutStartSec=60" in unit
    assert "User=" not in unit


@pytest.mark.parametrize("unit", ["digipaddock-cabinet.service", "digipaddock-cabinet-push.service", "digipaddock-session-host.service",
                                  "digipaddock-board-sync.service", "digipaddock-board-watch.service", "digipaddock-release.service"])
def test_unattended_units_alert_on_failure(unit):
    text = (COMMON / "systemd" / unit).read_text(encoding="utf-8")
    stem = unit.removesuffix(".service")
    assert f"OnFailure=digipaddock-alert@{stem}.service\n" in text.split("[Service]", 1)[0]


def test_stage_setup_installs_alert_unit_and_link():
    text = (COMMON / "install/stage_setup.sh").read_text(encoding="utf-8")
    assert 'ln -sfn "$REPO/common/bin/kd-alert" "$ALERT_LINK"' in text
    loop = re.search(r'^UNITS="(.*)"$', (COMMON / "install/render_host.sh").read_text(encoding="utf-8"), re.M).group(1).split()
    assert "digipaddock-alert@.service" in loop


def test_pult_env_path_is_the_products(kd_alert):
    assert kd_alert.PULT_ENV == "/etc/digipaddock/pult.env" and kd_alert.COMMON == "/opt/digipaddock/common"


# --- kd-alert --watch: what the cabinet cannot duplicate to Telegram itself -------------------------

BASE = "https://cabinet.example"


class Cabinet:
    """The cabinet's socket as root sees it: its state, its journal; ``down`` — it does not answer."""

    def __init__(self, events=(), cursor=None, down=False):
        self.events, self.state, self.down, self.calls, self.notices = list(events), {}, down, [], []
        if cursor is not None:
            self.state["alert-watch"] = cursor

    def __call__(self, request):
        self.calls.append(request)
        if self.down:
            raise Unavailable()
        op = request["op"]
        if op == "notice.post":
            self.notices.append(request)
            return {}
        if op == "state.get":
            return {"value": self.state.get(request["key"])}
        if op == "state.set":
            self.state[request["key"]] = request["value"]
            return {}
        if "before" in request:
            return {"events": sorted(self.events, key=lambda e: -e["seq"])[:request["limit"]]}
        found = [e for e in self.events if e["seq"] > request["after"]][:request["limit"]]
        return {"events": found, "last": found[-1]["seq"] if found else request["after"]}


class Unavailable(Exception):
    kind = "unavailable"


def failed(seq, agent="dev", card="KD-5", error="exit 1", was="working"):
    return {"seq": seq, "kind": "session.changed", "card": card,
            "payload": {"session": "s1", "agent": agent, "state": "failed", "was": was, "error": error}}


def denied(seq, card="KD-7"):
    return {"seq": seq, "kind": "notice", "card": card, "payload": {
        "source": "exec", "level": "warn", "key": "notice.exec.denied",
        "params": {"agent": "@sysadmin", "server": "node-x", "rule": "rm-root", "command": "rm -rf /"}}}


def watch(kd_alert, tmp_path, cabinet, now=NOW, sender=None, pult=True, socket_up=True, token_age=None):
    err = io.StringIO()
    sender = sender if sender is not None else Sender()
    pult_env = tmp_path / "pult.env"
    if pult:
        pult_env.write_text("x=1")
    code = kd_alert.watch(call=cabinet, socket_up=lambda: socket_up, clock=lambda: now, stamp_dir=tmp_path / "run",
                          sender=sender, base=BASE, hostname="stage", pult_env=pult_env, stderr=err,
                          token_age=lambda: token_age)
    return code, sender, err.getvalue()


@pytest.mark.parametrize("age", [None, 100.0, 334.9])
def test_watch_says_nothing_of_a_young_or_absent_claude_token(kd_alert, tmp_path, age):
    cabinet = Cabinet(cursor=4)
    _, sender, _ = watch(kd_alert, tmp_path, cabinet, token_age=age)
    assert cabinet.notices == [] and sender.sent == []


def test_watch_warns_a_month_before_the_claude_token_expires_once_a_day(kd_alert, tmp_path):
    """the token lives a year from its put — a warn notice from day 335, once in 24 h."""
    cabinet = Cabinet(cursor=4)
    watch(kd_alert, tmp_path, cabinet, token_age=340.2)
    (notice,) = cabinet.notices
    assert (notice["source"], notice["level"], notice["key"]) == ("alert", "warn", "notice.alert.claude_token")
    assert notice["params"] == {"host": "stage", "days": 25, "command": "kd-secret"}
    watch(kd_alert, tmp_path, cabinet, now=NOW + 3600, token_age=340.3)
    assert len(cabinet.notices) == 1
    watch(kd_alert, tmp_path, cabinet, now=NOW + 86400 + 1, token_age=341.2)
    assert len(cabinet.notices) == 2


def test_watch_raises_the_claude_token_to_critical_in_its_last_week(kd_alert, tmp_path):
    cabinet = Cabinet(cursor=4)
    _, sender, _ = watch(kd_alert, tmp_path, cabinet, token_age=360.0)
    (notice,) = cabinet.notices
    assert notice["level"] == "critical" and notice["params"]["days"] == 5
    (text,) = sender.sent
    assert "токен Claude" in text and "5 дн." in text and "put claude-token" in text
    assert text.endswith(f"\n{BASE}/incidents")


def test_watch_says_the_claude_token_expired_after_its_year(kd_alert, tmp_path):
    """Past the year it is not «expires in 0 days» — the agents cannot sign in."""
    cabinet = Cabinet(cursor=4)
    _, sender, _ = watch(kd_alert, tmp_path, cabinet, token_age=370.0)
    (notice,) = cabinet.notices
    assert (notice["level"], notice["key"]) == ("critical", "notice.alert.claude_token_expired")
    (text,) = sender.sent
    assert "истёк" in text and "put claude-token" in text and "0 дн." not in text


def test_a_failed_telegram_copy_of_the_token_notice_is_sent_next_minute(kd_alert, tmp_path):
    """The copy's own stamp, written only when it went out — not a day's wait after a failed send;
    the cabinet's notice stays once a day."""
    cabinet = Cabinet(cursor=4)
    _, failing, _ = watch(kd_alert, tmp_path, cabinet, sender=Sender(ok=False), token_age=360.0)
    assert len(failing.sent) == 1 and len(cabinet.notices) == 1
    _, sender, _ = watch(kd_alert, tmp_path, cabinet, now=NOW + 60, token_age=360.0)
    assert len(sender.sent) == 1 and len(cabinet.notices) == 1
    _, again, _ = watch(kd_alert, tmp_path, cabinet, now=NOW + 120, token_age=360.0)
    assert again.sent == []


def test_watch_starts_at_the_journal_end_and_sends_nothing_old(kd_alert, tmp_path):
    cabinet = Cabinet([failed(3), denied(4)])
    code, sender, _ = watch(kd_alert, tmp_path, cabinet)
    assert code == 0 and sender.sent == []
    assert cabinet.state["alert-watch"] == 4


def test_watch_duplicates_a_failed_session_and_a_denylist_refusal_in_one_message(kd_alert, tmp_path):
    cabinet = Cabinet([failed(5), failed(6, agent="analyst", card=""), denied(7), denied(8, card="")], cursor=4)
    code, sender, _ = watch(kd_alert, tmp_path, cabinet)
    assert code == 0
    (text,) = sender.sent
    assert text == (
        "⚠️ stage\n"
        f"Сессия @dev завершилась сбоем: exit 1 — {BASE}/cards/KD-5\n"
        f"Сессия @analyst завершилась сбоем: exit 1 — {BASE}/agents/analyst\n"
        f"⛔ @sysadmin · node-x · отказ: правило rm-root: rm -rf / — {BASE}/cards/KD-7\n"
        f"⛔ @sysadmin · node-x · отказ: правило rm-root: rm -rf / — {BASE}/incidents")  # no card: the alerts
    assert cabinet.state["alert-watch"] == 8


def test_watch_says_what_to_do_about_an_auth_failed_session(kd_alert, tmp_path):
    """Claude refused the credentials — the duplicate names the remedy, not «auth_failed»."""
    cabinet = Cabinet([failed(5, agent="boss", card="", error="auth_failed")], cursor=4)
    _, sender, _ = watch(kd_alert, tmp_path, cabinet)
    (text,) = sender.sent
    assert "@boss не может войти в Claude" in text and "put claude-token" in text and "auth_failed" not in text


def test_watch_skips_the_host_restart_a_repeated_failure_and_other_events(kd_alert, tmp_path):
    other = {"seq": 8, "kind": "notice", "card": "KD-7", "payload": {"key": "notice.exec.result", "params": {}}}
    ok = {"seq": 9, "kind": "session.changed", "card": "KD-5", "payload": {"agent": "dev", "state": "idle", "was": "working"}}
    cabinet = Cabinet([failed(5, error="host_restart"), failed(6, was="failed", error="exit 2"), other, ok], cursor=4)
    code, sender, _ = watch(kd_alert, tmp_path, cabinet)
    assert code == 0 and sender.sent == []
    assert cabinet.state["alert-watch"] == 9


def test_watch_cuts_a_long_list(kd_alert, tmp_path):
    cabinet = Cabinet([failed(n) for n in range(10, 25)], cursor=1)
    _, sender, _ = watch(kd_alert, tmp_path, cabinet)
    (text,) = sender.sent
    assert text.count("Сессия @dev") == 10 and text.endswith("\n… и ещё 5")


def test_watch_reads_every_page_of_the_journal(kd_alert, tmp_path, monkeypatch):
    monkeypatch.setattr(kd_alert, "WATCH_PAGE", 2)
    cabinet = Cabinet([failed(n) for n in range(2, 7)], cursor=1)
    watch(kd_alert, tmp_path, cabinet)
    assert cabinet.state["alert-watch"] == 6


def incident(seq, kind="opened", level="crit", **payload):
    base = {"incident": 3, "id": "P-3", "level": level, "value": "96%", "at": "2026-10-24T10:00:00Z"}
    if kind == "opened":
        base.update(key="app1:disk:/", server="app1", check="disk", subject="/")
    return {"seq": seq, "kind": f"incident.{kind}", "card": "", "payload": {**base, **payload}}


def test_watch_duplicates_every_new_critical_incident(kd_alert, tmp_path):
    """a new crit and warn -> crit go to Telegram on the next watch pass, a test one too."""
    test = incident(7, server="test", check="alert", subject="", key="test:alert:", id="P-4", incident=4,
                    value="ТЕСТ: проверка критичного алерта, серверы не затронуты")
    cabinet = Cabinet([incident(5), incident(6, kind="worse", id="P-2", incident=2, value="99%"), test], cursor=4)
    code, sender, _ = watch(kd_alert, tmp_path, cabinet)
    assert code == 0
    (text,) = sender.sent
    assert text == (
        "⚠️ stage\n"
        f"🔴 P-3 · app1 · disk /: 96% — {BASE}/incidents/P-3\n"
        f"🔴 P-2 · стало критичным: 99% — {BASE}/incidents/P-2\n"
        f"🔴 P-4 · test · alert: ТЕСТ: проверка критичного алерта, серверы не затронуты — {BASE}/incidents/P-4")


def test_watch_leaves_warnings_and_the_rest_of_an_incident_to_the_cabinet(kd_alert, tmp_path):
    cabinet = Cabinet([incident(5, level="warn"), incident(6, kind="worse", level="warn"),
                       incident(7, imported=True), incident(8, kind="better", level="warn"),
                       incident(9, kind="resolved", level="ok"), incident(10, kind="fix_failed")], cursor=4)
    code, sender, _ = watch(kd_alert, tmp_path, cabinet)
    assert code == 0 and sender.sent == []
    assert cabinet.state["alert-watch"] == 10


def test_watch_without_telegram_moves_on_and_keeps_the_text_in_the_journal(kd_alert, tmp_path):
    cabinet = Cabinet([failed(5)], cursor=4)
    code, sender, err = watch(kd_alert, tmp_path, cabinet, pult=False)
    assert code == 0 and sender.sent == []
    assert cabinet.state["alert-watch"] == 5 and "Сессия @dev завершилась сбоем" in err


def test_a_cabinet_that_does_not_answer_twice_in_a_row_is_duplicated_once_an_hour(kd_alert, tmp_path):
    down = Cabinet(down=True)
    _, sender, _ = watch(kd_alert, tmp_path, down)
    assert sender.sent == []  # one miss is a restart, not a hang
    _, sender, err = watch(kd_alert, tmp_path, down, now=NOW + 60)
    assert sender.sent == [f"⚠️ stage · кабинет не отвечает {kd_alert.CABINET_TIMEOUT} с\n{BASE}"]
    assert "кабинет не отвечает" in err
    _, sender, _ = watch(kd_alert, tmp_path, down, now=NOW + 120)
    assert sender.sent == []  # once an hour
    for now in range(int(NOW) + 180, int(NOW) + 60 + kd_alert.MIN_INTERVAL, 60):  # the watch runs every minute
        _, sender, _ = watch(kd_alert, tmp_path, down, now=now)
        assert sender.sent == []
    _, sender, _ = watch(kd_alert, tmp_path, down, now=NOW + 60 + kd_alert.MIN_INTERVAL)
    assert len(sender.sent) == 1  # still hanging an hour later


def test_an_answer_between_misses_starts_counting_again(kd_alert, tmp_path):
    watch(kd_alert, tmp_path, Cabinet(down=True))
    watch(kd_alert, tmp_path, Cabinet(cursor=1), now=NOW + 60)
    _, sender, _ = watch(kd_alert, tmp_path, Cabinet(down=True), now=NOW + 120)
    assert sender.sent == []


def test_a_cabinet_stopped_on_purpose_is_no_hang(kd_alert, tmp_path):
    for now in (NOW, NOW + 60, NOW + 120):
        _, sender, _ = watch(kd_alert, tmp_path, Cabinet(down=True), now=now, socket_up=False)
        assert sender.sent == []


def test_watch_mode_is_chosen_by_its_flag(kd_alert, monkeypatch):
    seen = []
    monkeypatch.setattr(kd_alert, "watch", lambda: seen.append(True) or 0)
    assert kd_alert.main(["--watch"]) == 0 and seen == [True]


def test_watch_units_run_as_root_every_minute_and_the_cabinet_installs_them():
    service = (COMMON / "systemd/digipaddock-alert-watch.service").read_text(encoding="utf-8")
    assert "Type=oneshot" in service and "ExecStart=/usr/local/bin/kd-alert --watch\n" in service
    assert "User=" not in service and "OnFailure=digipaddock-alert@digipaddock-alert-watch.service\n" in service
    timer = (COMMON / "systemd/digipaddock-alert-watch.timer").read_text(encoding="utf-8")
    assert "OnUnitActiveSec=1min" in timer
    setup = (COMMON / "install/cabinet_setup.sh").read_text(encoding="utf-8")
    units = re.search(r'^CABINET_UNITS="(.*)"$', setup, re.M).group(1).split()
    enabled = re.search(r'^ENABLED="(.*)"$', setup, re.M).group(1).split()
    assert {"digipaddock-alert-watch.service", "digipaddock-alert-watch.timer"} <= set(units) and "digipaddock-alert-watch.timer" in enabled
