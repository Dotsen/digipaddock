"""The health check after a release — each check in order, the cabinet checks retried while the
service restarts, the result as the release's notice on the card."""

import io
import json
import subprocess
import urllib.error
from pathlib import Path

import pytest

from kd import release_health
from kd.cabinet.client import CabinetError

CP = subprocess.CompletedProcess


def run_ok(argv, **kw):
    return CP(argv, 0, "", "")


def run_fail(argv, **kw):
    return CP(argv, 1 if argv[0] == "bash" else 0, "", "slot dev/1 missing\n")


class Http:
    def __init__(self, status=200):
        self.status, self.urls = status, []

    def __call__(self, url, timeout):
        self.urls.append(url)
        if self.status >= 400:
            raise urllib.error.HTTPError(url, self.status, "x", {}, None)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False



@pytest.fixture(autouse=True)
def no_host_runtime(tmp_path, monkeypatch):
    """The tests never see this host's own e2e runtime (stage has one)."""
    monkeypatch.setattr(release_health, "E2E_RUNTIME", tmp_path / "no-e2e-runtime")


class FakeClient:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), 0

    def call(self, request):
        self.calls += 1
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


STATUS = {"schema": 29, "pending": 0, "open_waits": 1}


def _releases(tmp_path, host=b"core"):
    """A release base: the host's release (pinned in HOST) and the one switched to."""
    base = tmp_path / "releases"
    for name, text in (("old", host), ("new", b"core")):
        path = base / name / "common/kd/session_host/core.py"
        path.parent.mkdir(parents=True)
        path.write_bytes(text)
    (base / "HOST").write_text(f"{base / 'old'}\n")
    return base / "new"


def test_run_reports_each_check_in_order_and_retries_the_cabinet(tmp_path):
    client = FakeClient([CabinetError("unavailable", "x"), CabinetError("unavailable", "x"), STATUS])
    naps = []
    out = release_health.run(_releases(tmp_path), origin="https://cab.example", schema_version=29, run=run_ok,
                             http=Http(), client=client, retries=3, pause=5, which=lambda name: "/usr/sbin/nginx",
                             sleep=naps.append, e2e_runtime=tmp_path)
    assert out["ok"] and [c["name"] for c in out["checks"]] == list(release_health.ORDER)
    assert client.calls == 3 and naps == [5, 5]
    assert all(c["detail"] == "" for c in out["checks"])


def test_run_names_the_failed_checks(tmp_path):
    client = FakeClient([{"schema": 28, "pending": 0, "open_waits": 0}])
    out = release_health.run(tmp_path, origin="https://cab.example", schema_version=29, run=run_fail, http=Http(502),
                             client=client, retries=1, pause=0, which=lambda name: None, e2e_runtime=tmp_path / "none")
    assert not out["ok"]
    assert release_health.failed(out) == ["session_host", "cabinet_http", "cabinet_schema"]
    by = {c["name"]: c for c in out["checks"]}
    assert "slot dev/1 missing" in by["session_host"]["detail"]
    assert by["nginx"]["detail"] == "skipped" and by["nginx"]["ok"]
    assert "HTTP 502" in by["cabinet_http"]["detail"]
    assert by["cabinet_schema"]["detail"] == "schema 28 served, 29 in the code"


def test_run_without_an_origin_skips_the_http_probe_and_gives_up_after_the_retries(tmp_path):
    client = FakeClient([CabinetError("unavailable", "down"), CabinetError("unavailable", "down")])
    out = release_health.run(tmp_path, origin=None, schema_version=29, run=run_ok, http=Http(), client=client,
                             retries=2, pause=0, which=lambda name: None)
    by = {c["name"]: c for c in out["checks"]}
    assert by["cabinet_http"]["detail"] == "skipped"
    assert release_health.failed(out) == ["cabinet_schema", "queues"] and "down" in by["queues"]["detail"]


def test_notice_is_info_when_healthy_and_critical_with_why_when_degraded():
    posted = []
    post = lambda *a: posted.append(a) or True  # noqa: E731
    healthy = {"ok": True, "checks": [{"name": "session_host", "ok": True, "detail": ""},
                                      {"name": "nginx", "ok": True, "detail": "skipped"}]}
    release_health.notice(healthy, "abc1234", "KD-7", post=post)
    assert posted[-1] == ("release", "info", "notice.release.healthy", {"rev": "abc1234", "checks": "session_host"}, "KD-7")
    degraded = {"ok": False, "checks": [{"name": "cabinet_schema", "ok": False, "detail": "schema 28 served, 29 in the code"},
                                        {"name": "queues", "ok": False, "detail": ""}]}
    release_health.notice(degraded, "abc1234", "", post=post)
    assert posted[-1] == ("release", "critical", "notice.release.degraded",
                          {"rev": "abc1234", "failed": "cabinet_schema, queues",
                           "why": "cabinet_schema: schema 28 served, 29 in the code; queues"}, "")


def test_main_posts_and_prints_the_result(monkeypatch, tmp_path):
    posted = []
    monkeypatch.setattr(release_health, "run", lambda tree, **kw: {"ok": False, "checks": [
        {"name": "nginx", "ok": False, "detail": "test failed"}]})
    from kd.cabinet import client as cabinet_client, config

    monkeypatch.setattr(cabinet_client, "post_notice", lambda *a, **kw: posted.append(a) or True)
    monkeypatch.setattr(config, "origin_of", lambda *a, **kw: None)
    out = io.StringIO()
    rc = release_health.main([str(tmp_path), "abc1234", "KD-7"], out=out)
    assert rc == 1 and posted[0][2] == "notice.release.degraded" and posted[0][4] == "KD-7"
    assert json.loads(out.getvalue())["ok"] is False
    assert release_health.main([str(tmp_path)]) == 2


def test_a_failed_postflight_is_a_failed_check_of_the_generated_runtime(tmp_path):
    """Review F4: a release whose generated agents will not start is never healthy."""
    out = release_health.run(tmp_path, origin=None, schema_version=29, run=run_ok, client=FakeClient([STATUS]),
                             retries=1, pause=0, which=lambda name: None, postflight="dev: instructions differ")
    assert not out["ok"] and release_health.failed(out) == ["generated_runtime"]
    assert "instructions differ" in {c["name"]: c for c in out["checks"]}["generated_runtime"]["detail"]
    ok = release_health.run(tmp_path, origin=None, schema_version=29, run=run_ok, client=FakeClient([STATUS]),
                            retries=1, pause=0, which=lambda name: None, postflight="")
    assert ok["ok"] and [c["name"] for c in ok["checks"]][0] == "session_host"


def test_main_takes_the_postflight_text_as_an_option(monkeypatch, tmp_path):
    posted = []
    monkeypatch.setattr(release_health, "run", lambda *a, **kw: {"ok": not kw.get("postflight"), "checks": [
        {"name": "generated_runtime", "ok": not kw.get("postflight"), "detail": kw.get("postflight") or ""}]})
    from kd.cabinet import client as cabinet_client
    monkeypatch.setattr(cabinet_client, "post_notice", lambda *a: posted.append(a) or True)
    assert release_health.main([str(tmp_path), "abc1234", "KD-1", "--postflight", "dev differs"], out=io.StringIO()) == 1
    assert posted[-1][2] == release_health.DEGRADED and "dev differs" in posted[-1][3]["why"]


def test_the_e2e_runtime_is_checked_where_the_host_has_one(tmp_path):
    """Review I5: kd-release refreshes the runtime; a refresh that failed leaves it stale — the release is degraded."""
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        stale = argv[-2:] == [str(tmp_path / "common/install/e2e_runtime_setup.sh"), "check"]
        return subprocess.CompletedProcess(argv, 1 if stale else 0, "", "e2e runtime stale: 1dac0f5" if stale else "")

    out = release_health.run(tmp_path, origin=None, schema_version=29, run=run, client=FakeClient([STATUS]),
                             retries=1, pause=0, which=lambda name: None, e2e_runtime=tmp_path)
    check = next(c for c in out["checks"] if c["name"] == "e2e_runtime")
    assert not out["ok"] and not check["ok"] and "stale" in check["detail"]
    absent = release_health.run(tmp_path, origin=None, schema_version=29, run=run_ok, client=FakeClient([STATUS]),
                                retries=1, pause=0, which=lambda name: None, e2e_runtime=tmp_path / "none")
    assert absent["ok"] and next(c for c in absent["checks"] if c["name"] == "e2e_runtime")["detail"] == "skipped"


# --- the session host on the released code ---------------------------------------------------------
def _host_check(tree, **kw):
    out = release_health.run(tree, origin=None, schema_version=29, run=run_ok, client=FakeClient([STATUS]),
                             retries=1, pause=0, which=lambda name: None, **kw)
    return next(c for c in out["checks"] if c["name"] == "session_host_code")


def test_the_host_on_an_older_release_with_the_same_code_is_healthy(tmp_path):
    assert _host_check(_releases(tmp_path)) == {"name": "session_host_code", "ok": True, "detail": ""}


def test_the_host_on_old_code_is_degraded_unless_its_restart_is_asked(tmp_path):
    tree = _releases(tmp_path, host=b"core 0")
    check = _host_check(tree)
    assert not check["ok"] and "хост сессий на другом коде" in check["detail"] and "old" in check["detail"]
    assert _host_check(tree, restart="op1")["ok"]


def test_a_host_without_a_pin_is_skipped(tmp_path):
    assert _host_check(tmp_path)["detail"] == "skipped"


def test_main_takes_the_restart_operation_as_an_option(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(release_health, "run", lambda *a, **kw: seen.append(kw.get("restart")) or {"ok": True, "checks": []})
    from kd.cabinet import client as cabinet_client
    monkeypatch.setattr(cabinet_client, "post_notice", lambda *a: True)
    assert release_health.main([str(tmp_path), "abc1234", "--host-restart", "op1"], out=io.StringIO()) == 0
    assert seen == ["op1"]


def _kd_cabinet():
    import runpy

    return runpy.run_path(str(Path(__file__).resolve().parents[1] / "bin" / "kd-cabinet"), run_name="kd_cabinet")


def test_health_command_posts_no_notice(monkeypatch):
    """kd-cabinet health: the release's checks without a card — no notice, the exit says healthy or not."""
    import io
    import json as json_

    from kd import release_health as rh
    from kd.cabinet import client as cabinet_client

    seen = {}

    def fake_run(tree, *, origin, schema_version, **_):
        seen["tree"] = Path(tree)
        return {"ok": False, "checks": [{"name": "session_host", "ok": True, "detail": ""},
                                        {"name": "nginx", "ok": False, "detail": "nginx -t: syntax"}]}

    monkeypatch.setattr(rh, "run", fake_run)
    monkeypatch.setattr(rh, "notice", lambda *a, **k: pytest.fail("health posts no notice"))
    monkeypatch.setattr(cabinet_client, "post_notice", lambda *a, **k: pytest.fail("health posts no notice"))
    mod = _kd_cabinet()
    out = io.StringIO()
    assert mod["main"](["health"], out=out, err=io.StringIO()) == 1
    assert seen["tree"] == Path(__file__).resolve().parents[2]
    text = out.getvalue()
    assert "ok      session_host" in text and "failed  nginx: nginx -t: syntax" in text
    out = io.StringIO()
    assert mod["main"](["health", "--json"], out=out, err=io.StringIO()) == 1
    assert json_.loads(out.getvalue())["checks"][1]["name"] == "nginx"
    monkeypatch.setattr(rh, "run", lambda tree, **_: {"ok": True, "checks": []})
    assert mod["main"](["health"], out=io.StringIO(), err=io.StringIO()) == 0
