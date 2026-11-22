import pytest

from sysadmin import notices


def test_result_notice_matches_the_catalog():
    assert notices.result("sysadmin", "example-stage", "systemctl restart x", 0, 2.13) == (
        "info", "notice.exec.result",
        {"agent": "@sysadmin", "server": "example-stage", "duration": "2,1", "command": "systemctl restart x"})


def test_result_notice_carries_the_masked_command():
    level, key, params = notices.result("sysadmin", "node-c", "curl -u u:p https://example.org/x", 0, 1.0)
    assert "u:***" in params["command"] and "u:p" not in params["command"]


def test_nonzero_and_timeout_keys():
    assert notices.result("sysadmin", "node-c", "false", 1, 0.2) == (
        "info", "notice.exec.failed", {"agent": "@sysadmin", "server": "node-c", "rc": 1, "duration": "0,2",
                                       "command": "false"})
    level, key, params = notices.result("sysadmin", "node-c", "sleep 1000", -9, 600.0, True)
    assert (level, key, params["duration"]) == ("info", "notice.exec.timeout", "600,0") and "rc" not in params


def test_denied_is_a_warn_notice():
    assert notices.denied("sysadmin", "example-prod", "echo <b> > /var/www/x", "code-write") == (
        "warn", "notice.exec.denied",
        {"agent": "@sysadmin", "server": "example-prod", "rule": "code-write", "command": "echo <b> > /var/www/x"})


def test_long_command_is_cut_to_500_characters():
    command = notices.result("sysadmin", "node-c", "x" * 5000, 0, 1.0)[2]["command"]
    assert command == "x" * 500 + "…"


def test_non_utf8_command_becomes_replacement_characters():
    assert notices.result("sysadmin", "node-c", "echo \udcff", 0, 1.0)[2]["command"] == "echo �"


def test_interrupted_notice():
    assert notices.interrupted("sysadmin", "node-c", "apt-get -y full-upgrade", 12.0, "SIGTERM") == (
        "info", "notice.exec.interrupted",
        {"agent": "@sysadmin", "server": "node-c", "reason": "SIGTERM", "duration": "12,0",
         "command": "apt-get -y full-upgrade"})


# --- a group ssh-exec: one notice per group ---

def test_batch_ok_notice():
    assert notices.batch_ok("sysadmin", "systemctl restart danted", ["node-a", "node-b", "node-c"]) == (
        "info", "notice.exec.batch_ok",
        {"agent": "@sysadmin", "count": 3, "command": "systemctl restart danted", "servers": "node-a, node-b, node-c"})


def test_batch_server_list_is_shortened_to_200_chars():
    servers = [f"node-{i:02d}-" + "x" * 24 for i in range(15)]  # 32 characters each, the longest name
    line = notices.batch_ok("sysadmin", "uptime", servers)[2]["servers"]
    assert len(line) <= 200 and line == ", ".join(servers[:5]) + " … ещё 10"


def test_batch_stopped_names_done_and_not_run():
    assert notices.batch_stopped("sysadmin", "systemctl restart danted", ["node-a"], ["node-c"], "node-b",
                                 "код 1") == (
        "info", "notice.exec.batch_failed",
        {"agent": "@sysadmin", "failed": "node-b", "why": "код 1", "command": "systemctl restart danted",
         "done": "node-a", "not_run": "node-c"})
    assert notices.batch_stopped("sysadmin", "uptime", ["a", "b"], ["c"], None, "время вышло") == (
        "info", "notice.exec.batch_stopped",
        {"agent": "@sysadmin", "why": "время вышло", "command": "uptime", "done": "a, b", "not_run": "c"})


def test_batch_stopped_on_first_or_last_server_shows_dash():
    assert notices.batch_stopped("sysadmin", "uptime", [], ["b", "c"], "a", "таймаут")[2]["done"] == "—"
    assert notices.batch_stopped("sysadmin", "uptime", ["a", "b"], [], "c", "прервано, SIGTERM")[2]["not_run"] == "—"


def test_batch_denied_is_a_warn_notice():
    assert notices.batch_denied("sysadmin", "reboot <now>", ["node-a", "example-stage"], "example-stage",
                                "stage-power") == (
        "warn", "notice.exec.batch_denied",
        {"agent": "@sysadmin", "rule": "stage-power", "server": "example-stage", "command": "reboot <now>",
         "servers": "node-a, example-stage"})


@pytest.mark.parametrize("build", [
    lambda c: notices.result("sysadmin", "node-c", c, 0, 1.0),
    lambda c: notices.denied("sysadmin", "node-c", c, "code-write"),
    lambda c: notices.batch_ok("sysadmin", c, ["a"]),
    lambda c: notices.batch_stopped("sysadmin", c, ["a"], ["c"], "b", "код 1"),
    lambda c: notices.batch_denied("sysadmin", c, ["a"], "a", "code-write"),
])
def test_every_notice_fits_the_cabinet_limits(build):
    """The cabinet refuses a value over 2000 characters and params over 4096 bytes of JSON (notice.post)."""
    import json
    params = build("ж" * 5000)[2]
    assert all(len(v) <= 2000 for v in params.values() if isinstance(v, str))
    assert len(json.dumps(params, ensure_ascii=False).encode("utf-8")) <= 4096
