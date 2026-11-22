import io
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kd import clock

COMMON = Path(__file__).resolve().parent.parent
needs_symlink = pytest.mark.skipif(os.name == "nt", reason="симлинки: только POSIX")
P = "-srv-digipaddock-agents-sysadmin"
MODEL = "claude-opus-5-5"
UTC3 = timezone(timedelta(hours=3), "UTC+3")  # a UTC+3 host; its name is the heading's


class Cabinet:
    """The cabinet's ``usage.day``: fixed rows and first stamps; ``away`` — it does not answer."""

    def __init__(self, rows=(), since=None, away=False, agents=None, counted=None):
        self.rows, self.since, self.away, self.calls = list(rows), dict(since or {}), away, []
        self.agents, self.counted = agents, counted

    def call(self, request):
        self.calls.append(request)
        if self.away:
            raise OSError("connection refused")
        answer = {"rows": self.rows, "since": self.since}
        if self.agents is not None:
            answer["agents"] = self.agents
        if self.counted is not None:
            answer["counted"] = self.counted
        return answer


@pytest.fixture(autouse=True)
def quiet_cabinet(request, monkeypatch, tmp_path):
    """Every daily run here reads an empty cabinet unless the test passes its own; Hindsight accounting has not
    started (no release mark) and its /tmp is an empty folder of the test, never the host's."""
    if "kd_usage" in request.fixturenames:
        module = request.getfixturevalue("kd_usage")
        monkeypatch.setattr(module, "default_cabinet", lambda: Cabinet())
        monkeypatch.setattr(module, "RELEASE_MARK", str(tmp_path / "release" / "REVISION"))
        monkeypatch.setattr(module, "HINDSIGHT_TMP", str(tmp_path / "hstmp"))
        monkeypatch.setattr(module, "HINDSIGHT_OWNER", os.getuid())  # systemd's root, as the test's own user
        monkeypatch.setattr(clock, "zone", lambda: UTC3)  # the days here are of a UTC+3 host (host.json timezone)


def hosted(agent="dev", model="claude-sonnet-5-5", requests=4, sessions=1, jobs=0, cost=0.4):
    return {"agent": agent, "model": model, "requests": requests, "sessions": sessions, "jobs": jobs, "input": 10,
            "output": 20, "cache_read": 30, "cache_write": 40, "cost": cost}


class Out(io.StringIO):
    """kd-usage daily's stdout; ``sent`` — the day's text as one item (nothing goes to Telegram)."""

    @property
    def sent(self):
        text = self.getvalue()
        return [text[:-1] if text.endswith("\n") else text] if text else []


def line(ts="2026-10-05T10:49:18.123Z", msg="msg_1", req="req_1", session="s1", model=MODEL,
         inp=3, out=326, cw=7849, cr=26993, kind="assistant", usage=True):
    d = {"type": kind, "timestamp": ts, "sessionId": session, "requestId": req,
         "message": {"id": msg, "model": model}}
    if usage:
        d["message"]["usage"] = {"input_tokens": inp, "output_tokens": out,
                                 "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr}
    return json.dumps(d)


def put(root, project, rel, *lines):
    path = Path(root) / project / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_dedupe_same_message_and_request(kd_usage, tmp_path):
    put(tmp_path, P, "a.jsonl", line(), line(), line(msg="msg_2", req="req_1"), line(msg="msg_1", req="req_2"))
    put(tmp_path, P, "b.jsonl", line())  # the same answer in another file
    row = kd_usage.scan(tmp_path, "2026-10-05")[("sysadmin", MODEL)]
    assert row["requests"] == 3 and row["output"] == 326 * 3 and row["input"] == 9
    assert row["cache_write"] == 7849 * 3 and row["cache_read"] == 26993 * 3


def test_bad_lines_ignored_and_missing_numbers_zero(kd_usage, tmp_path):
    put(tmp_path, P, "a.jsonl", "не json", line(kind="user"), line(msg="m2", usage=False),
        json.dumps({"type": "assistant", "timestamp": "2026-10-05T10:00:00Z", "sessionId": "s",
                    "requestId": "r", "message": {"id": "m", "model": "x", "usage": {"output_tokens": 5}}}))
    rows = kd_usage.scan(tmp_path, "2026-10-05")
    assert rows == {("sysadmin", "x"): {"requests": 1, "sessions": 1, "input": 0, "output": 5,
                                         "cache_write": 0, "cache_read": 0}}


def test_projects_filter_and_recursion(kd_usage, tmp_path):
    put(tmp_path, "-home-agent-other", "a.jsonl", line())
    put(tmp_path, "-srv-digipaddock-agents-Bad", "a.jsonl", line())
    put(tmp_path, "-srv-digipaddock-agents-" + "a" * 33, "a.jsonl", line())
    put(tmp_path, "-srv-digipaddock-agents-ops", "sess/subagents/x.jsonl", line())
    assert list(kd_usage.scan(tmp_path, "2026-10-05")) == [("ops", MODEL)]


@needs_symlink
def test_symlinked_file_and_dir_are_skipped(kd_usage, tmp_path):
    real = put(tmp_path / "outside", "p", "a.jsonl", line())
    proj = tmp_path / "root" / P
    proj.mkdir(parents=True)
    (proj / "link.jsonl").symlink_to(real)
    (proj / "linkdir").symlink_to(real.parent, target_is_directory=True)
    (tmp_path / "root" / "-srv-digipaddock-agents-evil").symlink_to(real.parent, target_is_directory=True)
    assert kd_usage.scan(tmp_path / "root", "2026-10-05") == {}


def test_msk_day_attribution(kd_usage, tmp_path):
    put(tmp_path, P, "a.jsonl", line(ts="2026-10-04T21:30:00Z", msg="a", req="a"),
        line(ts="2026-10-04T20:59:59Z", msg="b", req="b"))
    assert kd_usage.scan(tmp_path, "2026-10-05")[("sysadmin", MODEL)]["requests"] == 1
    assert kd_usage.scan(tmp_path, "2026-10-04")[("sysadmin", MODEL)]["requests"] == 1


def test_sessions_are_distinct(kd_usage, tmp_path):
    put(tmp_path, P, "a.jsonl", line(msg="a", req="a", session="s1"), line(msg="b", req="b", session="s1"),
        line(msg="c", req="c", session="s2"))
    assert kd_usage.scan(tmp_path, "2026-10-05")[("sysadmin", MODEL)]["sessions"] == 2


def run(kd_usage, tmp_path, argv, now="2026-10-06T06:00:00Z"):
    out, err = Out(), io.StringIO()
    code = kd_usage.main(argv, root=tmp_path / "proj", data_dir=tmp_path / "data", now=kd_usage.parse_ts(now),
                         hostname="example-stage", stdout=out, stderr=err)
    return code, out, out.getvalue(), err.getvalue()


def test_daily_defaults_to_yesterday_msk_and_is_idempotent(kd_usage, tmp_path):
    put(tmp_path / "proj", P, "a.jsonl", line(ts="2026-10-05T10:00:00Z", msg="a", req="a"))
    store = tmp_path / "data" / "usage.jsonl"
    store.parent.mkdir()
    store.write_text(json.dumps({"day": "2026-10-04", "agent": "old", "model": "m", "requests": 1}) + "\n")
    for _ in range(2):
        code, sender, _, _ = run(kd_usage, tmp_path, ["daily"])
        assert code == 0 and len(sender.sent) == 1
    rows = [json.loads(x) for x in store.read_text().splitlines()]
    assert [(r["day"], r["agent"]) for r in rows] == [("2026-10-04", "old"), ("2026-10-05", "sysadmin")]
    assert rows[1]["model"] == MODEL and rows[1]["requests"] == 1 and rows[1]["sessions"] == 1
    assert list(store.parent.iterdir()) == [store]  # no temporary files are left


def test_daily_explicit_date_replaces_only_that_day(kd_usage, tmp_path):
    put(tmp_path / "proj", P, "a.jsonl", line(ts="2026-10-03T10:00:00Z", msg="a", req="a"))
    run(kd_usage, tmp_path, ["daily", "--date", "2026-10-03"])
    rows = [json.loads(x) for x in (tmp_path / "data" / "usage.jsonl").read_text().splitlines()]
    assert [r["day"] for r in rows] == ["2026-10-03"]


def test_message_format(kd_usage, tmp_path):
    put(tmp_path / "proj", P, "a.jsonl", line(msg="a", req="a", out=17000, cw=166000, cr=2470000, inp=150),
        line(msg="b", req="b", model="claude-opus-4-8", out=0, cw=0, cr=0, inp=0, session="s2"))
    _, sender, _, _ = run(kd_usage, tmp_path, ["daily"])
    assert sender.sent == [
        "📊 example-stage · tokens for 05.10 (UTC+3)\n"
        "sysadmin: 2 requests, 2 sessions · output 17k · cache: write 166k, read 2.47M · input 150"
        " · cache 94% · models: opus-4-8 1, opus-5-5 1"]  # on a tie — by name


def test_cache_hit_percent_and_none(kd_usage):
    assert kd_usage.cache_hit({"input": 100, "cache_read": 800, "cache_write": 100}) == 80
    assert kd_usage.cache_hit({"input": 0, "cache_read": 0, "cache_write": 0}) is None
    assert kd_usage.cache_hit({"input": 1, "cache_read": 2, "cache_write": 0}) == 67


def test_message_shows_cache_hit_and_warns(kd_usage, tmp_path):
    put(tmp_path / "proj", P, "a.jsonl", line(msg="a", req="a", out=17000, cw=166000, cr=2470000, inp=150))
    _, sender, _, _ = run(kd_usage, tmp_path, ["daily"])
    assert "· cache 94%" in sender.sent[0] and "⚠️" not in sender.sent[0]
    lines = [line(msg=f"m{i}", req=f"r{i}", cw=900, cr=100, inp=0) for i in range(20)]
    put(tmp_path / "proj", "-srv-digipaddock-agents-ops", "a.jsonl", *lines)
    _, sender, _, _ = run(kd_usage, tmp_path, ["daily"])
    ops = next(l for l in sender.sent[0].splitlines() if l.startswith("ops:"))
    assert "⚠️ cache 10%" in ops


def test_show_has_cache_hit_column(kd_usage, tmp_path):
    store = tmp_path / "data" / "usage.jsonl"
    store.parent.mkdir()
    store.write_text(json.dumps({"day": "2026-10-05", "agent": "sysadmin", "model": "m", "requests": 1, "sessions": 1,
                                 "input": 100, "output": 3, "cache_write": 100, "cache_read": 800}) + "\n")
    code, _, out, _ = run(kd_usage, tmp_path, ["show"])
    head, row = out.strip().splitlines()[:2]
    assert "cache_hit" in head and row.split()[-2:] == ["80%", "-"]


def test_show_has_the_cost_column(kd_usage, tmp_path):
    """cost — the row's cost_usd in dollars with two digits; no cost_usd and a hindsight row — «-»."""
    store = tmp_path / "data" / "usage.jsonl"
    store.parent.mkdir()
    base = {"day": "2026-10-05", "model": "m", "requests": 1, "sessions": 1, "input": 100, "output": 3,
            "cache_write": 100, "cache_read": 800}
    rows = [{**base, "agent": "analyst", "cost_usd": 1.23456}, {**base, "agent": "boss", "cost_usd": 0},
            {**base, "agent": "dev"}, {**base, "agent": "ops", "cost_usd": "1.5"},
            {**base, "agent": "hindsight", "category": "hindsight", "coverage": "full", "cost_usd": 4.2}]
    store.write_text("".join(json.dumps(r) + "\n" for r in rows))
    code, _, out, _ = run(kd_usage, tmp_path, ["show"])
    head, *lines = out.strip().splitlines()
    assert code == 0 and head.split()[-3:] == ["cache_hit", "cost", "data"]
    cost = {line.split()[1]: line.split()[10] for line in lines}
    assert cost == {"analyst": "$1.23", "boss": "$0.00", "dev": "-", "ops": "-", "hindsight": "-"}
    assert [line.split()[1] for line in lines] == ["analyst", "boss", "dev", "ops", "hindsight"]  # order as before


def test_fmt_numbers(kd_usage):
    f = kd_usage.fmt
    assert [f(0), f(999), f(1000), f(17999), f(999999), f(1000000), f(2470000)] == \
        ["0", "999", "1k", "17k", "999k", "1.00M", "2.47M"]


def test_empty_day_heartbeat(kd_usage, tmp_path):
    _, sender, _, _ = run(kd_usage, tmp_path, ["daily"])
    assert sender.sent == ["📊 example-stage · tokens for 05.10 (UTC+3): no agent worked"]


def test_text_is_plain(kd_usage, tmp_path):
    put(tmp_path / "proj", "-srv-digipaddock-agents-ops", "a.jsonl", line(model="claude-m<&>"))
    put(tmp_path / "proj", "-srv-digipaddock-agents-ops", "b.jsonl", line(model="claude-n", msg="x", req="x"))
    _, sender, _, _ = run(kd_usage, tmp_path, ["daily", "--date", "2026-10-05"])
    assert "m<&>" in sender.sent[0] and "&lt;" not in sender.sent[0]


def test_daily_prints_and_sends_nothing(kd_usage, tmp_path):
    """the day is saved and printed; the cabinet's 09:00 digest reads the ledger, Telegram hears nothing."""
    import inspect

    code, out, text, err = run(kd_usage, tmp_path, ["daily"])
    assert code == 0 and text.startswith("📊 example-stage · tokens for 05.10") and "no agent worked" not in err
    params = inspect.signature(kd_usage.main).parameters
    assert "sender" not in params and "pult_env" not in params
    assert not hasattr(kd_usage, "pult_sender") and not hasattr(kd_usage, "PULT_ENV")
    script = (COMMON / "bin/kd-usage").read_text(encoding="utf-8")
    assert "pult" not in script.lower()


def test_show_last_n_days(kd_usage, tmp_path):
    store = tmp_path / "data" / "usage.jsonl"
    store.parent.mkdir()
    rows = [{"day": d, "agent": "sysadmin", "model": "m", "requests": 1, "sessions": 1, "input": 2,
             "output": 3, "cache_write": 4, "cache_read": 5} for d in ("2026-09-28", "2026-09-30", "2026-10-05")]
    store.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    code, _, out, _ = run(kd_usage, tmp_path, ["show", "--days", "7"])
    assert code == 0 and out.startswith("day ")
    assert "2026-09-30" in out and "2026-10-05" in out and "2026-09-28" not in out


def test_show_adds_the_monitors_turns_as_their_own_row(kd_usage, tmp_path, monkeypatch):
    """The cabinet's monitor category is a row «agent/monitor» with its model, turns and cost."""
    store = tmp_path / "data" / "usage.jsonl"
    store.parent.mkdir()
    store.write_text(json.dumps({"day": "2026-10-05", "agent": "sysadmin", "model": MODEL, "requests": 9, "sessions": 3,
                                 "input": 100, "output": 3, "cache_write": 100, "cache_read": 800}) + "\n")
    cabinet = Cabinet()
    monitor = {"agent": "sysadmin", "category": "monitor", "model": "claude-sonnet-5-5", "turns": 2, "requests": 5,
               "input": 10, "output": 20, "cache_read": 30, "cache_write": 40, "cost": 0.123}
    original = cabinet.call

    def call(request):
        answer = original(request)
        if request["start"] == "2026-10-04T21:00:00Z":  # 05.10 UTC+3
            answer["categories"] = [monitor, {**monitor, "agent": "Bad Name"}]
        return answer

    cabinet.call = call
    monkeypatch.setattr(kd_usage, "default_cabinet", lambda: cabinet)
    code, _, out, _ = run(kd_usage, tmp_path, ["show", "--days", "2"])
    lines = out.strip().splitlines()
    assert code == 0 and len(cabinet.calls) == 2
    [row] = [line for line in lines if "sysadmin/monitor" in line]
    assert row.split()[:5] == ["2026-10-05", "sysadmin/monitor", "claude-sonnet-5-5", "5", "2"]
    assert row.split()[10] == "$0.12" and row.endswith("monitor · $0.12") and "Bad" not in out
    assert lines.index(row) > next(i for i, line in enumerate(lines) if line.split()[1] == "sysadmin")


def test_show_without_the_cabinet_keeps_the_files_rows(kd_usage, tmp_path, monkeypatch):
    monkeypatch.setattr(kd_usage, "default_cabinet", lambda: Cabinet(away=True))
    code, _, out, err = run(kd_usage, tmp_path, ["show"])
    assert code == 0 and out.startswith("day ") and "no category rows" in err


def test_show_without_store(kd_usage, tmp_path):
    code, _, out, _ = run(kd_usage, tmp_path, ["show"])
    assert code == 0 and out.strip()


def test_bad_args_exit_2(kd_usage, tmp_path):
    assert run(kd_usage, tmp_path, ["daily", "--date", "вчера"])[0] == 2
    assert run(kd_usage, tmp_path, [])[0] == 2


def test_script_is_lf_python3():
    data = (COMMON / "bin/kd-usage").read_bytes()
    assert data.startswith(b"#!/usr/bin/env python3\n") and b"\r" not in data


def test_script_is_executable_in_git_index():
    out = subprocess.run(["git", "ls-files", "-s", "common/bin/kd-usage"], cwd=COMMON.parent,
                         capture_output=True, text=True)
    if out.returncode != 0 or not out.stdout.strip():
        pytest.skip("файл ещё не в индексе git")
    assert out.stdout.split()[0] == "100755"


def test_units():
    service = (COMMON / "systemd/digipaddock-usage.service").read_text(encoding="utf-8")
    assert "Type=oneshot" in service and "ExecStart=/usr/local/bin/kd-usage daily\n" in service
    assert "TimeoutStartSec=300" in service and "User=" not in service
    assert "pult" not in service.lower() and "Пульт" not in service


def test_usage_timer_runs_after_midnight():
    """the cabinet's 09:00 digest shows yesterday's spending — the day is counted at 00:30."""
    timer = (COMMON / "systemd/digipaddock-usage.timer").read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 00:30:00 @host:timezone@\n" in timer and "Persistent=true" in timer  # the host's day
    assert "WantedBy=timers.target" in timer and "09:00" not in timer


def test_stage_setup_installs_units_link_and_enables_timer():
    text = (COMMON / "install/stage_setup.sh").read_text(encoding="utf-8")
    assert 'ln -sfn "$REPO/common/bin/kd-usage" "$USAGE_LINK"' in text
    loop = re.search(r'^UNITS="(.*)"$', (COMMON / "install/render_host.sh").read_text(encoding="utf-8"), re.M).group(1).split()
    assert "digipaddock-usage.service" in loop and "digipaddock-usage.timer" in loop
    assert text.index('bash "$REPO/common/install/render_host.sh"') < text.index("systemctl enable --now digipaddock-usage.timer")


def test_duplicate_keeps_max_per_field(kd_usage, tmp_path):
    put(tmp_path, P, "a.jsonl", line(out=10, cw=100, cr=5), line(out=326, cw=50, cr=9))
    row = kd_usage.scan(tmp_path, "2026-10-05")[("sysadmin", MODEL)]
    assert row["requests"] == 1 and row["output"] == 326 and row["cache_write"] == 100 and row["cache_read"] == 9


# --- transcripts in the homes of all agent users ---

def test_two_homes_are_summed_per_agent(kd_usage, tmp_path):
    a, d = tmp_path / "digi", tmp_path / "digi-dev"
    put(a, P, "a.jsonl", line(msg="a", req="a"))
    put(d, "-srv-digipaddock-agents-dev", "a.jsonl", line(msg="b", req="b"), line(msg="c", req="c"))
    put(a, "-srv-digipaddock-agents-dev", "probe.jsonl", line(msg="d", req="d"))  # claude -p under digi before the move
    put(d, "-srv-digipaddock-agents-dev", "dup.jsonl", line(msg="b", req="b"))     # the same answer — once
    rows = kd_usage.scan([a, d], "2026-10-05")
    assert rows[("sysadmin", MODEL)]["requests"] == 1 and rows[("dev", MODEL)]["requests"] == 3


def test_projects_roots_from_registry(kd_usage, tmp_path):
    reg = tmp_path / "agents.json"
    reg.write_text(json.dumps({"dev": {"user": "digi-dev", "telegram": True}}), encoding="utf-8")
    assert kd_usage.projects_roots(registry=str(reg)) == ["/home/digi/.claude/projects", "/home/digi-dev/.claude/projects"]
    reg.write_text("[]", encoding="utf-8")
    err = io.StringIO()
    assert kd_usage.projects_roots(registry=str(reg), stderr=err) == ["/home/digi/.claude/projects"]
    assert "agents.json" in err.getvalue()


def test_daily_scans_every_agent_home_by_default(kd_usage, tmp_path, monkeypatch):
    a, d = tmp_path / "a", tmp_path / "d"
    put(a, P, "x.jsonl", line(msg="a", req="a"))
    put(d, "-srv-digipaddock-agents-dev", "x.jsonl", line(msg="b", req="b"))
    monkeypatch.setattr(kd_usage, "projects_roots", lambda registry=None, stderr=None: [str(a), str(d)])
    sender = Out()
    assert kd_usage.main(["daily"], data_dir=tmp_path / "data", now=kd_usage.parse_ts("2026-10-06T06:00:00Z"), stdout=sender,
                         hostname="example-stage", stderr=io.StringIO()) == 0
    lines = sender.sent[0].splitlines()
    assert any(ln.startswith("dev:") for ln in lines) and any(ln.startswith("sysadmin:") for ln in lines)


def test_agent_session_counted_once_across_models(kd_usage, tmp_path):
    put(tmp_path / "proj", P, "a.jsonl", line(msg="a", req="a"),
        line(msg="b", req="b", model="claude-opus-4-8"))
    _, sender, _, _ = run(kd_usage, tmp_path, ["daily"])
    assert "2 requests, 1 session ·" in sender.sent[0]


@pytest.mark.parametrize("n,words", [(1, "1 request, 1 session"), (2, "2 requests, 2 sessions"),
                                     (5, "5 requests, 5 sessions"), (11, "11 requests, 11 sessions"),
                                     (21, "21 requests, 21 sessions"), (0, "0 requests, 0 sessions")])
def test_plurals(kd_usage, n, words):
    f = kd_usage.plural
    assert f"{f(n, kd_usage.REQUESTS)}, {f(n, kd_usage.SESSIONS)}" == words


def test_malformed_store_lines_skipped(kd_usage, tmp_path):
    store = tmp_path / "data" / "usage.jsonl"
    store.parent.mkdir()
    good = {"day": "2026-10-04", "agent": "old", "model": "m", "requests": 1}
    store.write_text("мусор\n[1]\n42\n" + json.dumps(good) + "\n", encoding="utf-8")
    assert "old" in run(kd_usage, tmp_path, ["show"])[2]
    run(kd_usage, tmp_path, ["daily"])
    assert [json.loads(x) for x in store.read_text(encoding="utf-8").splitlines()] == [good]


@needs_symlink
def test_fifo_is_skipped(kd_usage, tmp_path):
    proj = tmp_path / P
    proj.mkdir()
    os.mkfifo(proj / "pipe.jsonl")
    put(tmp_path, P, "ok.jsonl", line())
    assert kd_usage.scan(tmp_path, "2026-10-05")[("sysadmin", MODEL)]["requests"] == 1


def test_non_regular_file_is_skipped(kd_usage, tmp_path, monkeypatch):
    path = put(tmp_path, P, "a.jsonl", line())
    monkeypatch.setattr(kd_usage.stat, "S_ISREG", lambda mode: False)
    assert list(kd_usage.read_lines(str(path))) == []


@pytest.mark.parametrize("bad", ["вчера", "2026-13-40", "2026-02-30", "26-1-1"])
def test_invalid_date_exits_2(kd_usage, tmp_path, bad):
    assert run(kd_usage, tmp_path, ["daily", "--date", bad])[0] == 2


def test_common_path_is_the_products(kd_usage):
    assert kd_usage.COMMON_PATH == "/opt/digipaddock/common"


# --- a session from a subdirectory of the agent's folder (dev starts from work/) — the same agent ------------

def test_work_subdir_sums_into_agent_row(kd_usage, tmp_path):
    d = tmp_path / "digi-dev"
    put(d, "-srv-digipaddock-agents-dev", "a.jsonl", line(msg="a", req="a", session="s1"))
    put(d, "-srv-digipaddock-agents-dev-work", "b.jsonl", line(msg="b", req="b", session="s2"),
        line(msg="c", req="c", session="s2"))
    put(d, "-srv-digipaddock-agents-dev-work-DigiPaddock", "c.jsonl", line(msg="e", req="e", session="s3"))  # claude -p from the clone
    sessions = {}
    rows = kd_usage.scan([d], "2026-10-05", sessions, folders=["board", "dev", "sysadmin"])
    assert list(rows) == [("dev", MODEL)]
    assert rows[("dev", MODEL)]["requests"] == 4 and rows[("dev", MODEL)]["sessions"] == 3
    assert sessions == {"dev": {"s1", "s2", "s3"}}


def test_agent_of_takes_longest_folder_and_keeps_unknown_tail(kd_usage):
    f = kd_usage.agent_of
    folders = ["digipaddock", "dev", "x-work", "probe_agent"]
    assert f("-srv-digipaddock-agents-dev-work", folders) == "dev"
    assert f("-srv-digipaddock-agents-digipaddock-work", folders) == "digipaddock"
    assert f("-srv-digipaddock-agents-dev", folders) == "dev"
    assert f("-srv-digipaddock-agents-x-work", folders) == "x-work"          # an agent with -work in its name — not cut
    assert f("-srv-digipaddock-agents-probe-agent", folders) == "probe_agent"  # Claude Code replaced _ with -
    assert f("-srv-digipaddock-agents-digipaddockx", folders) == "digipaddockx"            # not a subdirectory of digipaddock
    assert f("-srv-digipaddock-agents-ops", folders) == "ops"                  # no folder — the tail as it is
    assert f("-srv-digipaddock-agents-ops", []) == "ops"


def test_agent_folders_lists_dirs_only(kd_usage, tmp_path):
    base = tmp_path / "srv"
    for name in ("dev", "sysadmin", "Bad"):
        (base / name).mkdir(parents=True)
    (base / "notes.txt").write_text("x", encoding="utf-8")
    assert kd_usage.agent_folders(base) == ["dev", "sysadmin"]
    assert kd_usage.agent_folders(tmp_path / "нет") == []


def test_daily_maps_work_dir_to_agent_via_srv_folders(kd_usage, tmp_path):
    proj = tmp_path / "proj"
    put(proj, "-srv-digipaddock-agents-dev", "a.jsonl", line(msg="a", req="a"))
    put(proj, "-srv-digipaddock-agents-dev-work", "b.jsonl", line(msg="b", req="b"))
    (tmp_path / "srv" / "dev" / "work").mkdir(parents=True)
    sender = Out()
    assert kd_usage.main(["daily"], root=proj, data_dir=tmp_path / "data", now=kd_usage.parse_ts("2026-10-06T06:00:00Z"),
                         stdout=sender, hostname="example-stage", stderr=io.StringIO(),
                         agents_dir=tmp_path / "srv") == 0
    assert "dev-work" not in sender.sent[0] and "dev: 2 requests" in sender.sent[0]
    rows = [json.loads(x) for x in (tmp_path / "data" / "usage.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(r["agent"], r["requests"]) for r in rows] == [("dev", 2)]


# --- C06: worker jobs (result records) in the pool owner's totals -----------------------------------------------

def record(job="a" * 32, agent="dev", model="claude-sonnet-5-5", ended="2026-10-05T12:00:00Z", cost=0.5):
    from datetime import datetime
    ts = datetime.fromisoformat(ended.replace("Z", "+00:00")).timestamp()
    return json.dumps({"schema_version": 1, "job": job, "agent": agent, "ended": ts,
                       "usage": {"model": model, "input": 1, "output": 2, "cache_write": 3, "cache_read": 4,
                                 "requests": 5, "cost_usd": cost}})


def test_worker_jobs_count_for_the_pool_owner(kd_usage, tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    (results / ("a" * 32 + ".json")).write_text(record(), encoding="utf-8")
    (results / ("b" * 32 + ".json")).write_text(record(job="b" * 32, ended="2026-10-06T00:30:00Z"), encoding="utf-8")  # 03:30 UTC+3 next day
    (results / ("c" * 32 + ".json")).write_text(record(job="c" * 32, cost=0.25), encoding="utf-8")
    (results / "junk.json").write_text("{", encoding="utf-8")
    (results / ("d" * 32 + ".json")).write_text("{", encoding="utf-8")
    (results / ("e" * 32 + ".json")).write_text(record(job="e" * 32, agent="../x"), encoding="utf-8")
    rows = kd_usage.worker_rows(results, "2026-10-05")
    assert rows == {("dev", "claude-sonnet-5-5"): {"requests": 10, "sessions": 2, "input": 2, "output": 4, "cache_write": 6,
                                                   "cache_read": 8, "jobs": 2, "cost_usd": 0.75}}
    assert kd_usage.worker_rows(tmp_path / "none", "2026-10-05") == {}


def test_daily_merges_worker_rows_into_the_agents_totals_and_names_them(kd_usage, tmp_path):
    put(tmp_path / "home", "-srv-digipaddock-agents-dev", "a.jsonl", line(model="claude-sonnet-5-5"))
    results = tmp_path / "results"
    results.mkdir()
    (results / ("a" * 32 + ".json")).write_text(record(), encoding="utf-8")
    sender = Out()
    kd_usage.main(["daily", "--date", "2026-10-05"], root=tmp_path / "home", data_dir=tmp_path / "data", stdout=sender,
                  hostname="h", results_dir=results, agents_dir=str(tmp_path / "none"))
    (row,) = [r for r in kd_usage.load(tmp_path / "data" / "usage.jsonl") if r["agent"] == "dev"]
    assert row["requests"] == 6 and row["jobs"] == 1 and row["cost_usd"] == 0.5 and row["sessions"] == 2
    assert "dev: 6 requests, 2 sessions" in sender.sent[0] and "worker: 1 job · $0.50" in sender.sent[0]
    assert "worker:" not in kd_usage.build_text({("dev", "m"): {f: 1 for f in kd_usage.FIELDS}}, "2026-10-05", "h")


# --- hosted turns and jobs from the cabinet's usage ledger ------------------------------------------

def daily(kd_usage, tmp_path, cabinet, day="2026-10-05"):
    sender, err = Out(), io.StringIO()
    results = tmp_path / "results"
    results.mkdir(exist_ok=True)
    code = kd_usage.main(["daily", "--date", day], root=tmp_path / "home", data_dir=tmp_path / "data", stdout=sender,
                         hostname="h", results_dir=results, agents_dir=str(tmp_path / "none"),
                         stderr=err, cabinet=cabinet)
    return code, sender.sent[0], err.getvalue(), {(r["agent"], r["model"]): r for r in
                                                  kd_usage.load(tmp_path / "data" / "usage.jsonl")}


def test_daily_takes_hosted_usage_from_the_cabinet(kd_usage, tmp_path):
    cabinet = Cabinet(rows=[hosted(requests=4, sessions=2, jobs=1, cost=0.4)], since={"dev": "2026-10-01T00:00:00Z"})
    code, text, err, rows = daily(kd_usage, tmp_path, cabinet)
    # the UTC3 day 05.10 is [04.10 21:00, 05.10 21:00) UTC
    assert cabinet.calls == [{"op": "usage.day", "start": "2026-10-04T21:00:00Z", "end": "2026-10-05T21:00:00Z"}]
    row = rows[("dev", "claude-sonnet-5-5")]
    assert (row["requests"], row["sessions"], row["jobs"], row["cost_usd"]) == (4, 3, 1, 0.4)
    assert (row["input"], row["output"], row["cache_read"], row["cache_write"]) == (10, 20, 30, 40)
    assert code == 0 and "dev: 4 requests, 3 sessions" in text and err == ""


def test_transcripts_after_the_cutover_are_not_counted_twice(kd_usage, tmp_path):
    put(tmp_path / "home", "-srv-digipaddock-agents-dev", "a.jsonl",
        line(ts="2026-10-05T08:00:00Z", msg="old", req="old", session="x", model="claude-sonnet-5-5", out=100),
        line(ts="2026-10-05T12:00:00Z", msg="new", req="new", session="y", model="claude-sonnet-5-5", out=7))
    put(tmp_path / "home", P, "b.jsonl", line(ts="2026-10-05T12:00:00Z", msg="s", req="s"))  # sysadmin: no cutover
    cabinet = Cabinet(rows=[hosted(requests=1, sessions=1, cost=0.1)], since={"dev": "2026-10-05T10:00:00Z"})
    _, text, _, rows = daily(kd_usage, tmp_path, cabinet)
    row = rows[("dev", "claude-sonnet-5-5")]
    assert (row["requests"], row["output"]) == (2, 100 + 20)  # the old transcript line and the cabinet's turn
    assert "dev: 2 requests, 2 sessions" in text  # session «x» of the files, one hosted session
    assert rows[("sysadmin", MODEL)]["requests"] == 1


def test_worker_results_after_the_cutover_are_not_counted_twice(kd_usage, tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    (results / ("a" * 32 + ".json")).write_text(record(ended="2026-10-05T08:00:00Z", cost=0.5), encoding="utf-8")
    (results / ("b" * 32 + ".json")).write_text(record(job="b" * 32, ended="2026-10-05T12:00:00Z", cost=0.25),
                                                encoding="utf-8")
    cabinet = Cabinet(rows=[hosted(requests=5, sessions=0, jobs=1, cost=0.25)], since={"dev": "2026-10-05T10:00:00Z"})
    _, text, _, rows = daily(kd_usage, tmp_path, cabinet)
    row = rows[("dev", "claude-sonnet-5-5")]
    assert (row["jobs"], row["cost_usd"], row["requests"]) == (2, 0.75, 10)  # job a from its record, job b once
    assert "worker: 2 job · $0.75" in text


def test_cabinet_away_falls_back_to_files_and_says_so(kd_usage, tmp_path):
    put(tmp_path / "home", "-srv-digipaddock-agents-dev", "a.jsonl",
        line(ts="2026-10-05T12:00:00Z", msg="new", req="new", model="claude-sonnet-5-5"))
    code, text, err, rows = daily(kd_usage, tmp_path, Cabinet(away=True))
    assert code == 0 and rows[("dev", "claude-sonnet-5-5")]["requests"] == 1  # the files, whole
    assert "the cabinet did not answer" in err and err.count("\n") == 1
    assert text.splitlines()[-1] == kd_usage.CABINET_AWAY


def test_cost_line_from_the_cabinet(kd_usage, tmp_path):
    cabinet = Cabinet(rows=[hosted(agent="analyst", model="claude-opus-5-5", requests=3, sessions=1, cost=1.234),
                            hosted(agent="analyst", model="claude-haiku-5-5", requests=1, sessions=1, cost=0.01),
                            hosted(agent="boss", cost=0.0)])
    _, text, _, _ = daily(kd_usage, tmp_path, cabinet)
    (analyst,) = [l for l in text.splitlines() if l.startswith("analyst:")]
    (boss,) = [l for l in text.splitlines() if l.startswith("boss:")]
    assert analyst.endswith(" · $1.24") and "worker:" not in analyst
    assert "$" not in boss


def test_a_session_on_two_models_is_one_session(kd_usage, tmp_path):
    """Final review: the cabinet counts an agent's sessions across its models once."""
    cabinet = Cabinet(rows=[hosted(model="claude-opus-5-5"), hosted(model="claude-haiku-5-5")],
                      agents={"dev": {"sessions": 1, "jobs": 0}})
    _, text, _, _ = daily(kd_usage, tmp_path, cabinet)
    assert "dev: 8 requests, 1 session" in text


def test_a_released_agents_panel_transcripts_are_counted_after_the_cutover(kd_usage, tmp_path):
    """Final review: only the Claude sessions the cabinet counted are left out of the files."""
    put(tmp_path / "home", "-srv-digipaddock-agents-dev", "a.jsonl",
        line(ts="2026-10-05T12:00:00Z", msg="panel", req="panel", session="panel", model="claude-sonnet-5-5", out=7),
        line(ts="2026-10-05T12:05:00Z", msg="hosted", req="hosted", session="hosted-1", model="claude-sonnet-5-5",
             out=9))
    cabinet = Cabinet(rows=[hosted(requests=1, sessions=1, cost=0.1)], since={"dev": "2026-10-05T10:00:00Z"},
                      agents={"dev": {"sessions": 1, "jobs": 0}}, counted={"dev": ["hosted-1"]})
    _, text, _, rows = daily(kd_usage, tmp_path, cabinet)
    row = rows[("dev", "claude-sonnet-5-5")]
    assert (row["requests"], row["output"]) == (2, 7 + 20)  # the panel's line and the cabinet's turn
    assert "dev: 2 requests, 2 sessions" in text


# --- the Hindsight server's own calls, one category beside the agents ---

HAIKU = "claude-haiku-4-5-20251001"
BOOT = "0123456789abcdef0123456789abcdef"
API, WORKER = "hindsight-api.service", "hindsight-worker.service"
DAY = "2026-10-05"  # UTC3: 2026-10-04T21:00:00Z .. 2026-10-05T21:00:00Z


def hs_unit(tmp, unit=API, began="2026-10-01T00:00:00Z", suffix="AbCdEf"):
    """systemd's PrivateTmp of a unit, started at ``began``: the outer folder's mtime."""
    outer = Path(tmp) / f"systemd-private-{BOOT}-{unit}-{suffix}"
    (outer / "tmp").mkdir(parents=True)
    outer.chmod(0o700)
    moment = datetime.fromisoformat(began.replace("Z", "+00:00")).timestamp()
    os.utime(outer, (moment, moment))
    return outer / "tmp"


def hs_put(inner, *lines, config="hindsight-claude-code-x1y2", name="s.jsonl"):
    return put(Path(inner) / config / "projects", "-opt-hindsight", name, *lines)


def hs_line(msg, out=10, model=HAIKU, ts="2026-10-05T10:00:00Z", cw=100, cr=0, inp=5):
    return line(ts=ts, msg=msg, req="r-" + msg, session="s-" + msg, model=model, inp=inp, out=out, cw=cw, cr=cr)


def released(tmp_path, when="2026-10-05T07:00:00Z"):
    mark = tmp_path / "release" / "REVISION"
    mark.parent.mkdir(exist_ok=True)
    mark.write_text("abc1234\n")
    moment = datetime.fromisoformat(when.replace("Z", "+00:00")).timestamp()
    os.utime(mark, (moment, moment))


def ledger(tmp_path):
    return [json.loads(x) for x in (tmp_path / "data" / "usage.jsonl").read_text().splitlines()]


def hs_rows(tmp_path, day=DAY):
    return [r for r in ledger(tmp_path) if r.get("category") == "hindsight" and r["day"] == day]


def test_api_and_worker_are_one_category_per_model_and_a_call_counts_once(kd_usage, tmp_path):
    api, worker = hs_unit(tmp_path, API), hs_unit(tmp_path, WORKER)
    hs_put(api, hs_line("a", out=10), hs_line("a", out=4), hs_line("b", out=20))  # streamed duplicate of "a"
    hs_put(worker, hs_line("c", out=30), hs_line("d", out=1, model="claude-sonnet-5-5"))
    hs_put(worker, hs_line("b", out=20), name="copy.jsonl")  # the same call seen twice
    rows, coverage = kd_usage.scan_hindsight(tmp_path, DAY)
    assert coverage == "full" and set(rows) == {HAIKU, "claude-sonnet-5-5"}
    assert rows[HAIKU]["requests"] == 3 and rows[HAIKU]["output"] == 60 and rows[HAIKU]["sessions"] == 3
    assert rows[HAIKU]["cache_write"] == 300 and rows[HAIKU]["input"] == 15
    assert rows["claude-sonnet-5-5"]["requests"] == 1


def test_only_hindsight_units_and_config_dirs_are_read(kd_usage, tmp_path):
    hs_put(hs_unit(tmp_path, "nginx.service"), hs_line("x"))
    api = hs_unit(tmp_path, API)
    hs_put(api, hs_line("y"), config="claude-other")
    hs_put(api, hs_line("z"))
    hs_put(tmp_path / "loose", hs_line("w"))
    rows, _ = kd_usage.scan_hindsight(tmp_path, DAY)
    assert rows[HAIKU]["requests"] == 1


def test_a_private_tmp_not_made_by_systemd_is_a_forgery(kd_usage, tmp_path, monkeypatch):
    """Review: /tmp is open to every account — a folder of another owner or not 0700 is never read, nor its mtime."""
    api = hs_unit(tmp_path, API)
    hs_put(api, hs_line("a"))
    api.parent.chmod(0o755)
    hs_put(hs_unit(tmp_path, WORKER), hs_line("b"))
    assert kd_usage.scan_hindsight(tmp_path, DAY) == (
        {HAIKU: {"requests": 1, "sessions": 1, "input": 5, "output": 10, "cache_write": 100, "cache_read": 0}},
        "partial")
    monkeypatch.setattr(kd_usage, "HINDSIGHT_OWNER", os.getuid() + 1)
    assert kd_usage.scan_hindsight(tmp_path, DAY) == ({}, "none")


@pytest.mark.parametrize("api_began, worker_began, coverage", [
    ("2026-10-04T20:00:00Z", "2026-10-01T00:00:00Z", "full"),      # both before 00:00 UTC3
    ("2026-10-04T21:00:00Z", "2026-10-01T00:00:00Z", "full"),      # exactly at the day's start
    ("2026-10-05T09:00:00Z", "2026-10-01T00:00:00Z", "partial"),   # api restarted during the day
    ("2026-10-05T21:30:00Z", "2026-10-01T00:00:00Z", "partial"),   # api restarted after the day: its part is lost
    ("2026-10-05T21:00:00Z", "2026-10-05T22:00:00Z", "none"),      # both started after the day's end
])
def test_coverage_follows_the_units_start(kd_usage, tmp_path, api_began, worker_began, coverage):
    hs_unit(tmp_path, API, began=api_began)
    hs_unit(tmp_path, WORKER, began=worker_began)
    assert kd_usage.scan_hindsight(tmp_path, DAY) == ({}, coverage)


def test_no_units_is_no_data_and_one_missing_unit_is_partial(kd_usage, tmp_path):
    assert kd_usage.scan_hindsight(tmp_path / "absent", DAY) == ({}, "none")
    hs_put(hs_unit(tmp_path, WORKER), hs_line("a"))
    rows, coverage = kd_usage.scan_hindsight(tmp_path, DAY)
    assert coverage == "partial" and rows[HAIKU]["requests"] == 1


def test_msk_day_bounds_and_unknown_model(kd_usage, tmp_path):
    hs_put(hs_unit(tmp_path, API), hs_line("in", ts="2026-10-04T21:00:00Z"),
           hs_line("out", ts="2026-10-04T20:59:59Z"),
           json.dumps({"type": "assistant", "timestamp": "2026-10-05T12:00:00Z", "requestId": "r",
                       "message": {"id": "m", "usage": {"output_tokens": 3}}}))
    hs_unit(tmp_path, WORKER)
    rows, _ = kd_usage.scan_hindsight(tmp_path, DAY)
    assert set(rows) == {HAIKU, "unknown"} and rows[HAIKU]["requests"] == 1 and rows["unknown"]["output"] == 3
    assert kd_usage.scan_hindsight(tmp_path, "2026-10-04")[0][HAIKU]["requests"] == 1


@needs_symlink
def test_hindsight_symlinks_are_skipped(kd_usage, tmp_path):
    real = hs_put(tmp_path / "elsewhere", hs_line("a")).parent.parent.parent  # the config dir
    api = hs_unit(tmp_path / "t", API)
    (api / "hindsight-claude-code-link").symlink_to(real, target_is_directory=True)
    worker_outer = tmp_path / "t" / f"systemd-private-{BOOT}-{WORKER}-ZzZzZz"
    worker_outer.symlink_to(api.parent, target_is_directory=True)
    rows, coverage = kd_usage.scan_hindsight(tmp_path / "t", DAY)
    assert rows == {} and coverage == "partial"  # the worker's symlinked tmp is no unit


def test_ledger_rows_zero_and_no_data(kd_usage):
    assert kd_usage.hindsight_ledger(DAY, {}, "full") == [
        {"day": DAY, "agent": "hindsight", "category": "hindsight", "coverage": "full", "model": "-",
         "requests": 0, "sessions": 0, "input": 0, "output": 0, "cache_write": 0, "cache_read": 0}]
    (none,) = kd_usage.hindsight_ledger(DAY, {}, "none")
    assert none == {"day": DAY, "agent": "hindsight", "category": "hindsight", "coverage": "none", "model": "-"}


def test_daily_writes_hindsight_beside_the_agents_and_reruns_replace(kd_usage, tmp_path):
    released(tmp_path)
    put(tmp_path / "proj", P, "a.jsonl", line(ts="2026-10-05T10:00:00Z", msg="a", req="a"))
    calls = [hs_line(f"m{i}", cw=900, cr=100, inp=0) for i in range(20)]  # a cache hit of 10 % over 20 calls
    hs_put(hs_unit(tmp_path / "hstmp", API), *calls)
    hs_unit(tmp_path / "hstmp", WORKER)
    for _ in range(2):
        code, _, text, _ = run(kd_usage, tmp_path, ["daily"])
        assert code == 0
    rows = ledger(tmp_path)
    assert [(r["agent"], r["model"]) for r in rows] == [("hindsight", HAIKU), ("sysadmin", MODEL)]
    (hs,) = hs_rows(tmp_path)
    assert hs["coverage"] == "full" and hs["requests"] == 20 and hs["cache_write"] == 18000
    (hs_line_text,) = [l for l in text.splitlines() if l.startswith("hindsight:")]
    assert "20 requests" in hs_line_text and "cache 10%" in hs_line_text and "⚠️" not in hs_line_text
    assert any(ln.startswith("sysadmin: 1 request") for ln in text.splitlines())


def test_daily_before_the_release_day_adds_no_hindsight(kd_usage, tmp_path):
    released(tmp_path, when="2026-10-05T07:00:00Z")
    hs_put(hs_unit(tmp_path / "hstmp", API), hs_line("a", ts="2026-10-04T10:00:00Z"))
    run(kd_usage, tmp_path, ["daily", "--date", "2026-10-04"])
    assert hs_rows(tmp_path, "2026-10-04") == []
    run(kd_usage, tmp_path, ["daily", "--date", DAY])  # the release day itself counts, from 00:00 UTC3
    run(kd_usage, tmp_path, ["daily", "--date", "2026-10-04"])  # and the start stays the earliest ledger day
    assert hs_rows(tmp_path, "2026-10-04") == [] and len(hs_rows(tmp_path)) == 1


def test_the_start_is_pinned_at_first_sight_and_a_later_release_does_not_move_it(kd_usage, tmp_path):
    """With no hindsight row yet, the start came from the newest release's REVISION mtime, so a release
    after midnight before the 00:30 run (stage, 21.10) moved it a day on and the release day was never counted."""
    released(tmp_path, when="2026-10-05T07:00:00Z")
    assert kd_usage.hindsight_since(tmp_path / "data") == DAY
    released(tmp_path, when="2026-10-06T21:11:00Z")  # 00:11 UTC3 on 07.10: a later release
    assert kd_usage.hindsight_since(tmp_path / "data") == DAY
    assert (tmp_path / "data" / "hindsight-since").read_text(encoding="utf-8").strip() == DAY


def test_a_start_written_by_hand_counts_from_its_day(kd_usage, tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "hindsight-since").write_text("2026-10-04\n", encoding="utf-8")
    released(tmp_path)  # the newest release is a day later
    hs_put(hs_unit(tmp_path / "hstmp", API), hs_line("a", ts="2026-10-04T10:00:00Z"))
    run(kd_usage, tmp_path, ["daily", "--date", "2026-10-04"])
    assert [r["requests"] for r in hs_rows(tmp_path, "2026-10-04")] == [1]


def test_without_a_release_mark_hindsight_is_not_counted(kd_usage, tmp_path):
    hs_put(hs_unit(tmp_path / "hstmp", API), hs_line("a"))
    run(kd_usage, tmp_path, ["daily"])
    assert hs_rows(tmp_path) == []


def test_no_data_is_marked_not_zero(kd_usage, tmp_path):
    released(tmp_path)
    _, _, text, _ = run(kd_usage, tmp_path, ["daily"])
    (hs,) = hs_rows(tmp_path)
    assert hs["coverage"] == "none" and "requests" not in hs and "input" not in hs
    assert "hindsight: no data" in text and "no agent worked" in text


def test_partial_day_is_marked(kd_usage, tmp_path):
    released(tmp_path)
    hs_put(hs_unit(tmp_path / "hstmp", API, began="2026-10-05T12:00:00Z"), hs_line("a", ts="2026-10-05T13:00:00Z"))
    hs_unit(tmp_path / "hstmp", WORKER)
    _, _, text, _ = run(kd_usage, tmp_path, ["daily"])
    (hs,) = hs_rows(tmp_path)
    assert hs["coverage"] == "partial" and hs["requests"] == 1
    assert "⚠️ partial data" in text


def test_a_worse_rerun_keeps_the_better_record(kd_usage, tmp_path):
    released(tmp_path)
    tmp = tmp_path / "hstmp"
    hs_put(hs_unit(tmp, API), hs_line("a"), hs_line("b"))
    hs_unit(tmp, WORKER)
    run(kd_usage, tmp_path, ["daily"])
    full = hs_rows(tmp_path)
    shutil.rmtree(tmp)  # both units restarted: the day's transcripts are gone
    _, _, text, _ = run(kd_usage, tmp_path, ["daily"])
    assert hs_rows(tmp_path) == full and "hindsight: 2 requests" in text
    hs_put(hs_unit(tmp, API, began="2026-10-05T12:00:00Z"), hs_line("c", ts="2026-10-05T13:00:00Z"))
    run(kd_usage, tmp_path, ["daily"])
    assert hs_rows(tmp_path) == full  # partial is worse than full


def test_a_smaller_partial_rerun_keeps_the_larger(kd_usage):
    big = kd_usage.hindsight_ledger(DAY, {HAIKU: {"requests": 2, "sessions": 2, "input": 9, "output": 9,
                                                  "cache_write": 9, "cache_read": 0}}, "partial")
    small = kd_usage.hindsight_ledger(DAY, {HAIKU: {"requests": 1, "sessions": 1, "input": 1, "output": 1,
                                                    "cache_write": 1, "cache_read": 0}}, "partial")
    full = kd_usage.hindsight_ledger(DAY, {}, "full")
    assert kd_usage.better_hindsight(big, small) is big and kd_usage.better_hindsight(small, big) is big
    assert kd_usage.better_hindsight(big, full) is full and kd_usage.better_hindsight(full, big) is full


def test_show_lists_hindsight_with_its_coverage(kd_usage, tmp_path):
    store = tmp_path / "data" / "usage.jsonl"
    store.parent.mkdir()
    rows = [{"day": "2026-10-04", "agent": "hindsight", "category": "hindsight", "coverage": "none", "model": "-"},
            {"day": DAY, "agent": "hindsight", "category": "hindsight", "coverage": "partial", "model": HAIKU,
             "requests": 3, "sessions": 3, "input": 100, "output": 3, "cache_write": 100, "cache_read": 800},
            {"day": DAY, "agent": "sysadmin", "model": "m", "requests": 1}]
    store.write_text("".join(json.dumps(r) + "\n" for r in rows))
    _, _, out, _ = run(kd_usage, tmp_path, ["show"])
    head, none, partial, agent = out.strip().splitlines()
    assert head.split()[-1] == "data"
    assert none.split()[3:] == ["-"] * 8 + ["no", "data"]
    assert partial.split()[-4:] == ["80%", "-", "partial", "data"]
    assert agent.split()[3:] == ["1", "0", "0", "0", "0", "0", "-", "-"]
