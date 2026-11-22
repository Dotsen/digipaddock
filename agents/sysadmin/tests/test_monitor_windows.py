"""the monitor takes a closed plan window into its baseline — what the step changed, except the dangerous —
closes the problems of those objects and reports to the cabinet; while a window is open it does not compare the
server's snapshot, listeners and peers."""

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from sysadmin.monitor import deliver, seccheck, windows
from sysadmin.monitor.baseline import Baseline
from sysadmin.monitor.model import CheckResult, Observation, load_config
from sysadmin.monitor.registry import Registry

UTC3 = timezone(timedelta(hours=3))
CONFIG = Path(__file__).resolve().parent / "fixtures" / "monitor-config.json"
DAY = datetime(2026, 10, 7, 14, 0, tzinfo=UTC3)
LINK = "multi-user.target.wants/x.service"


@pytest.fixture
def cfg():
    return load_config(CONFIG)


@pytest.fixture
def sp(tmp_path):
    return tmp_path / "state.json"


@pytest.fixture
def cab(monitor_cabinet):
    return monitor_cabinet


@pytest.fixture
def card(cab):
    return cab.env.card(project="admin", assignee="@sysadmin", status="In Progress").id


def baseline(tmp_path, snapshot=None, ports=None, server="node-a"):
    b = Baseline.empty()
    b.set_server(server, Baseline.make_server({}, ports or {}, [], snapshot or {}, DAY))
    b.save(tmp_path / "baseline.json")


def problem(sp, server, check, subject, value="x"):
    reg = Registry.load(sp, date(2026, 10, 7), local_ids=False)
    reg.observe(
        [
            CheckResult(
                server,
                check,
                True,
                [Observation(server, check, subject, "warn", value, "t")],
            )
        ],
        DAY - timedelta(hours=1),
    )
    reg.save(sp)


def window(cab, card, changes, server="node-a", run="r1", errors=()):
    wid = cab.env.call(
        "digi-exec",
        {
            "op": "monitor.window.open",
            "server": server,
            "card": card,
            "step": "shell",
            "run": run,
        },
    )["id"]
    cab.env.call(
        "digi-exec",
        {
            "op": "monitor.window.close",
            "id": wid,
            "changes": changes,
            "errors": list(errors),
        },
    )
    return wid


def apply(cab, tmp_path, sp, cfg):
    return windows.apply_closed(
        cab,
        tmp_path,
        DAY,
        cfg,
        lambda: deliver._reg(sp, DAY, cfg),
        lambda: deliver._state_lock(tmp_path),
        lambda reg: reg.save(sp),
    )


def row(cab, wid):
    r = cab.env.store._conn.execute(
        "SELECT state, result FROM monitor_windows WHERE id=?", (wid,)
    ).fetchone()
    return r["state"], json.loads(r["result"])


def snap(tmp_path, server="node-a"):
    return Baseline.load(tmp_path / "baseline.json").server(server)


def test_a_closed_window_goes_into_the_baseline_and_closes_its_problems(
    cab, card, tmp_path, sp, cfg
):
    baseline(tmp_path)
    problem(
        sp,
        "node-a",
        "sec:units",
        "x.service",
        seccheck.units_value({"x.service": "h1", LINK: "symlink -> /x"}),
    )
    wid = window(
        cab,
        card,
        {"units:x.service": [None, "h1"], "units:" + LINK: [None, "symlink -> /x"]},
    )
    lines = apply(cab, tmp_path, sp, cfg)
    assert snap(tmp_path)["snapshot"] == {
        "units:x.service": "h1",
        "units:" + LINK: "symlink -> /x",
    }
    assert (
        "node-a:sec:units:x.service"
        not in json.loads(sp.read_text(encoding="utf-8"))["problems"]
    )
    assert row(cab, wid) == (
        "applied",
        {"accepted": [["sec:units", "x.service"]], "dangerous": [], "detail": ""},
    )
    assert len(lines) == 1 and "node-a" in lines[0]


def test_dangerous_objects_stay_out_and_are_reported(cab, card, tmp_path, sp, cfg):
    baseline(tmp_path)
    wid = window(
        cab,
        card,
        {
            "keys:/root/.ssh/authorized_keys": [None, "a" * 64 + " 1"],
            "users:svc": [None, "0 /bin/bash"],
            "cron:/etc/cron.d/x": [None, "h"],
        },
    )
    apply(cab, tmp_path, sp, cfg)
    assert snap(tmp_path)["snapshot"] == {"cron:/etc/cron.d/x": "h"}
    state, result = row(cab, wid)
    assert state == "applied" and result["accepted"] == [["sec:cron", "/etc/cron.d/x"]]
    assert result["dangerous"] == [
        ["sec:keys", "/root/.ssh/authorized_keys"],
        ["sec:users", "svc"],
    ]


def test_two_windows_of_one_server_apply_in_order(cab, card, tmp_path, sp, cfg):
    baseline(tmp_path)
    a = window(cab, card, {"units:x.service": [None, "h1"]}, run="r1")
    b = window(cab, card, {"units:x.service": ["h1", "h2"]}, run="r2")
    apply(cab, tmp_path, sp, cfg)
    assert snap(tmp_path)["snapshot"] == {"units:x.service": "h2"}
    assert row(cab, a)[0] == row(cab, b)[0] == "applied"


def test_an_object_that_differed_before_the_step_is_left_to_its_incident(
    cab, card, tmp_path, sp, cfg
):
    baseline(tmp_path, snapshot={"units:x.service": "h0"})
    wid = window(cab, card, {"units:x.service": ["drift", "h1"]})
    apply(cab, tmp_path, sp, cfg)
    assert snap(tmp_path)["snapshot"] == {"units:x.service": "h0"}
    state, result = row(cab, wid)
    assert result["accepted"] == [] and "до шага" in result["detail"]


def test_listeners_and_peers_are_taken_when_they_appear(cab, card, tmp_path, sp, cfg):
    baseline(
        tmp_path,
        ports={"tcp:0.0.0.0:80": "nginx"},
        snapshot={"units:gone.service": "h"},
    )
    problem(sp, "node-a", "ports", "tcp:0.0.0.0:443")
    window(
        cab,
        card,
        {
            "ports:tcp:0.0.0.0:443": [None, "nginx"],
            "ports:tcp:0.0.0.0:80": ["nginx", None],
            "wgpeers:wg0:KEY=": [None, ""],
            "units:gone.service": ["h", None],
        },
    )
    apply(cab, tmp_path, sp, cfg)
    srv = snap(tmp_path)
    assert srv["ports"] == {
        "tcp:0.0.0.0:80": "nginx",
        "tcp:0.0.0.0:443": "nginx",
    }  # a gone listener stays
    assert srv["wgpeers"] == ["wg0:KEY="] and srv["snapshot"] == {}
    assert (
        "node-a:ports:tcp:0.0.0.0:443"
        not in json.loads(sp.read_text(encoding="utf-8"))["problems"]
    )


def test_switched_off_windows_change_nothing(cab, card, tmp_path, sp, cfg):
    baseline(tmp_path)
    cfg["security"]["plan_windows"] = False
    wid = window(cab, card, {"units:x.service": [None, "h1"]})
    apply(cab, tmp_path, sp, cfg)
    assert snap(tmp_path)["snapshot"] == {}
    assert row(cab, wid) == (
        "rejected",
        {"accepted": [], "dangerous": [], "detail": "выключено"},
    )


def test_a_server_without_a_baseline_takes_nothing_and_says_so(
    cab, card, tmp_path, sp, cfg
):
    baseline(tmp_path, server="node-b")
    wid = window(cab, card, {"units:x.service": [None, "h1"]})
    apply(cab, tmp_path, sp, cfg)
    assert row(cab, wid) == (
        "applied",
        {"accepted": [], "dangerous": [], "detail": "у сервера нет эталона"},
    )


def test_a_lost_report_is_repeated_with_the_same_result(
    cab, card, tmp_path, sp, cfg, monkeypatch
):
    baseline(tmp_path)
    wid = window(cab, card, {"units:x.service": [None, "h1"]})
    real = cab.call

    def lost(request):
        if request["op"] == "monitor.window.report":
            raise windows.cabinet.cabinet_client.CabinetError(
                "unavailable", "board unavailable"
            )
        return real(request)

    monkeypatch.setattr(cab, "call", lost)
    apply(cab, tmp_path, sp, cfg)
    assert row(cab, wid)[0] == "closed"
    monkeypatch.setattr(cab, "call", real)
    apply(cab, tmp_path, sp, cfg)
    assert row(cab, wid) == (
        "applied",
        {"accepted": [["sec:units", "x.service"]], "dangerous": [], "detail": ""},
    )


def test_open_windows_mark_their_servers_and_a_silent_cabinet_marks_none(cab, card):
    cab.env.call(
        "digi-exec",
        {
            "op": "monitor.window.open",
            "server": "node-a",
            "card": card,
            "step": "s",
            "run": "r",
        },
    )
    sec = {}
    windows.mark(sec, windows.active(cab), DAY)
    assert sec["plan_windows"] == {"node-a": DAY.isoformat()}
    cab.up = False
    windows.mark(sec, windows.active(cab), DAY)
    assert sec["plan_windows"] == {}


def test_switched_off_windows_leave_no_server_waiting(cab, card, cfg):
    """The kill switch acts at once: a step's changes alert as before, even while its window is still open."""
    cab.env.call(
        "digi-exec",
        {
            "op": "monitor.window.open",
            "server": "node-a",
            "card": card,
            "step": "s",
            "run": "r",
        },
    )
    cfg["security"]["plan_windows"] = False
    cab.calls.clear()
    assert windows.active(cab, cfg) == []
    assert cab.calls == []
    cfg["security"]["plan_windows"] = True
    assert windows.active(cab, cfg) == ["node-a"]


def test_key_fingerprints_stay_with_their_file(cab, card, tmp_path, sp, cfg):
    """Review 1: keyprints and pubkeys follow their keys file (accepted with it by the owner), never a window."""
    baseline(tmp_path)
    path = "/root/.ssh/authorized_keys"
    wid = window(
        cab,
        card,
        {
            "keys:" + path: [None, "a" * 64 + " 1"],
            "keyprints:" + path: [None, "a" * 64 + " 1"],
            f"keyprints:{path} ED25519 SHA256:abc": [None, "c"],
            "pubkeys:/root/.ssh/id_ed25519.pub": [None, "p"],
            "cron:/etc/cron.d/x": [None, "h"],
        },
    )
    apply(cab, tmp_path, sp, cfg)
    assert snap(tmp_path)["snapshot"] == {"cron:/etc/cron.d/x": "h"}
    assert row(cab, wid)[1] == {
        "accepted": [["sec:cron", "/etc/cron.d/x"]],
        "dangerous": [["sec:keys", path]],
        "detail": "",
    }


def test_a_unit_incident_closes_only_when_all_its_objects_are_in_the_baseline(
    cab, card, tmp_path, sp, cfg
):
    """Review 2: the step enables a unit whose file had drifted before it — the link is taken, the incident stays."""
    baseline(tmp_path, snapshot={"units:x.service": "f0"})
    problem(
        sp,
        "node-a",
        "sec:units",
        "x.service",
        seccheck.units_value({"x.service": "f1"}),
    )
    window(cab, card, {"units:" + LINK: [None, "symlink -> /x"]})
    apply(cab, tmp_path, sp, cfg)
    assert snap(tmp_path)["snapshot"] == {
        "units:x.service": "f0",
        "units:" + LINK: "symlink -> /x",
    }
    assert (
        "node-a:sec:units:x.service"
        in json.loads(sp.read_text(encoding="utf-8"))["problems"]
    )


def test_a_unit_incident_of_the_steps_own_objects_closes(cab, card, tmp_path, sp, cfg):
    baseline(tmp_path)
    problem(
        sp,
        "node-a",
        "sec:units",
        "x.service",
        seccheck.units_value({LINK: "symlink -> /x"}),
    )
    window(cab, card, {"units:" + LINK: [None, "symlink -> /x"]})
    apply(cab, tmp_path, sp, cfg)
    assert (
        "node-a:sec:units:x.service"
        not in json.loads(sp.read_text(encoding="utf-8"))["problems"]
    )


def test_a_new_home_without_keys_is_no_change(cab, card, tmp_path, sp, cfg):
    """Review 6: «-» (no file) and a missing line are one — no dangerous object to report."""
    baseline(tmp_path)
    wid = window(cab, card, {"keys:/home/x/.ssh/authorized_keys": [None, "-"]})
    apply(cab, tmp_path, sp, cfg)
    assert row(cab, wid) == ("applied", {"accepted": [], "dangerous": [], "detail": ""})


def test_a_window_opened_while_the_tick_polled_holds_that_servers_security(
    cab, card, cfg
):
    """Review 5: a step that began after the tick read the windows — its server's sec, listeners and peers wait."""
    window(
        cab, card, {"units:x.service": [None, "h"]}, server="node-b"
    )  # opened and closed during the poll
    cab.env.call(
        "digi-exec",
        {
            "op": "monitor.window.open",
            "server": "node-a",
            "card": card,
            "step": "s",
            "run": "r2",
        },
    )
    assert windows.late(cab, cfg, marked=[]) == {"node-a", "node-b"}
    assert windows.late(cab, cfg, marked=["node-a"]) == {"node-b"}
    results = [
        CheckResult(s, c, True, [Observation(s, c, "x", "warn", "v", "t")])
        for s in ("node-a", "node-c")
        for c in ("sec:units", "ports", "wgpeers", "posture", "disk")
    ]
    held = windows.hold(results, {"node-a"})
    assert [(r.server, r.check) for r in held if not r.ran] == [
        ("node-a", c) for c in ("sec:units", "ports", "wgpeers", "posture")
    ]
    assert all(r.ran for r in held if r.server == "node-c" or r.check == "disk")
    cfg["security"]["plan_windows"] = False
    assert windows.late(cab, cfg, marked=[]) == set()
