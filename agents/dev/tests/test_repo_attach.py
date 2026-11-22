"""kd-repo-attach — the root unit runs add_repo.sh, and only it, for each of the cabinet's requests."""
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from dev_agent import repo_attach

ROOT = Path(__file__).resolve().parents[3]
ADD = "/opt/digipaddock/agents/dev/install/add_repo.sh"


class Runs:
    """The unit's runner: records argv, answers a code (or raises)."""

    def __init__(self, code=0, stderr=""):
        self.calls, self.code, self.stderr = [], code, stderr

    def __call__(self, argv):
        self.calls.append(argv)
        if isinstance(self.code, BaseException):
            raise self.code
        return repo_attach.Done(self.code, "", self.stderr)


class Refusal(Exception):
    """The cabinet client's error (kd.cabinet.client.CabinetError): ``kind`` says which."""

    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind


class Cabinet:
    def __init__(self, items, fail_result=None, down=0, result_down=0):
        self.items, self.answers, self.fail_result = list(items), [], fail_result
        self.down, self.result_down = down, result_down

    def call(self, request):
        if request["op"] == "project.repo.queue":
            if self.down:
                self.down -= 1
                raise ConnectionRefusedError("socket")
            return {"items": list(self.items)}
        assert request["op"] == "project.repo.result"
        if self.result_down:
            self.result_down -= 1
            raise Refusal("unavailable")
        if self.fail_result:
            raise Refusal(self.fail_result)
        self.answers.append(request)
        self.items = [i for i in self.items if i["id"] != request["id"]]
        return {}


@pytest.fixture
def reg(tmp_path):
    path = tmp_path / "repos.json"
    path.write_text(json.dumps({
        "digipaddock": {"repo": "example/digipaddock", "dir": "work/DigiPaddock", "release": "digipaddock"},
        "site": {"repo": "example-owner/Site", "dir": "work/Site"},
        "dots": {"repo": "example-owner/.dots", "dir": "work/.dots"}}), encoding="utf-8")
    return str(path)


def test_the_argv_is_add_repo_and_nothing_else(reg):
    assert repo_attach.command({"id": "web", "repo": "o/Web", "action": "bind"}, reg) == ["bash", ADD, "web", "o/Web"]
    assert repo_attach.command({"id": "site", "repo": "example-owner/Site", "action": "unbind"}, reg) == [
        "bash", ADD, "--remove", "site"]
    # a repository root bound by hand with a name the cabinet would not take still goes: unbind needs the project only
    assert repo_attach.command({"id": "dots", "repo": "example-owner/.dots", "action": "unbind"}, reg) == [
        "bash", ADD, "--remove", "dots"]
    # the same repository of a release project: a check that keeps root's release; never --allow-hooks
    argv = repo_attach.command({"id": "digipaddock", "repo": "example/digipaddock", "action": "bind"}, reg)
    assert argv == ["bash", ADD, "digipaddock", "example/digipaddock", "--release", "digipaddock"]
    assert ADD == "/opt/digipaddock/agents/dev/install/add_repo.sh"


@pytest.mark.parametrize("item", [
    {"id": "Web", "repo": "o/Web", "action": "bind"},
    {"id": "web;x", "repo": "o/Web", "action": "bind"},
    {"id": "--remove", "repo": "o/Web", "action": "unbind"},
    {"id": "web", "repo": "o/.claude", "action": "bind"},
    {"id": "web", "repo": "o/a..b", "action": "bind"},
    {"id": "web", "repo": "o/Web --allow-hooks", "action": "bind"},
    {"id": "web", "repo": "--allow-hooks/x", "action": "bind"},
    {"id": "web", "repo": None, "action": "bind"},
    {"id": "web", "repo": "o/Web", "action": "clone"},
    {"id": "digipaddock", "repo": "o/Other", "action": "bind"},
    {"id": "digipaddock", "repo": "example/digipaddock", "action": "unbind"},
])
def test_a_bad_or_forbidden_request_runs_nothing(reg, item):
    run = Runs()
    out = repo_attach.answer(item, run, reg)
    assert run.calls == [] and out["state"] == "error" and out["detail"].startswith("отказ:")


def test_an_odd_registry_runs_nothing(tmp_path):
    bad = tmp_path / "repos.json"
    run = Runs()
    for text in ("{", json.dumps({"web": {"repo": "o/Web", "dir": "work/Web", "release": True}})):
        bad.write_text(text, encoding="utf-8")
        assert repo_attach.answer({"id": "web", "repo": "o/Web", "action": "bind"}, run, str(bad))["state"] == "error"
    assert run.calls == []


@pytest.mark.parametrize("code, state", [(0, "connected"), (4, "hooks"), (5, "no_key"), (6, "no_access"),
                                         (2, "error"), (3, "error"), (1, "error"), (128, "error")])
def test_the_exit_code_is_the_answer(reg, code, state):
    run = Runs(code, "строка 1\nу dev нет доступа к ssh://…/o/Web.git (clone)\n")
    out = repo_attach.answer({"id": "web", "repo": "o/Web", "action": "bind"}, run, reg)
    assert out == {"op": "project.repo.result", "id": "web", "action": "bind", "repo": "o/Web", "state": state,
                   "detail": "" if state == "connected" else "строка 1; у dev нет доступа к ssh://…/o/Web.git (clone)"}


def test_unbind_answers_unbound_or_error(reg):
    item = {"id": "site", "repo": "example-owner/Site", "action": "unbind"}
    assert repo_attach.answer(item, Runs(0), reg)["state"] == ""
    assert repo_attach.answer(item, Runs(1, "boom"), reg)["state"] == "error"


def test_a_hung_or_missing_add_repo_is_an_error(reg):
    item = {"id": "web", "repo": "o/Web", "action": "bind"}
    hung = repo_attach.answer(item, Runs(subprocess.TimeoutExpired("bash", 300)), reg)
    assert hung["state"] == "error" and "300" in hung["detail"]
    assert repo_attach.answer(item, Runs(FileNotFoundError()), reg)["state"] == "error"


@pytest.mark.skipif(sys.platform == "win32", reason="process groups")
def test_spawn_kills_the_whole_group_on_timeout(tmp_path):
    """git under runuser under bash: a timeout kills the grandchildren too, the pipes close, the run returns."""
    marker = tmp_path / "child.pid"
    script = f"sleep 60 & echo $! > {marker}; wait"
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        repo_attach.spawn(["bash", "-c", script], timeout=3)
    assert time.monotonic() - started < 15
    child = int(marker.read_text().strip())
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        raise AssertionError("the grandchild outlived the timeout")
    done = repo_attach.spawn(["bash", "-c", "echo out; echo err >&2; exit 6"])
    assert (done.returncode, done.stdout.strip(), done.stderr.strip()) == (6, "out", "err")


@pytest.mark.skipif(sys.platform == "win32", reason="a POSIX root and environment")
def test_spawn_runs_in_a_clean_env_from_root_dir(tmp_path):
    done = repo_attach.spawn([sys.executable, "-c", "import os; print(os.getcwd(), sorted(os.environ))"])
    cwd, names = done.stdout.split(" ", 1)
    assert cwd == "/" and "LANG" in names and "PYTHONPATH" not in names


def test_take_answers_each_request_and_survives_a_refused_answer(reg):
    cab = Cabinet([{"id": "web", "repo": "o/Web", "action": "bind"},
                   {"id": "site", "repo": "example-owner/Site", "action": "unbind"}])
    assert repo_attach.take(cab, Runs(0), reg, err=io.StringIO()) == (2, False)
    assert [(a["id"], a["state"]) for a in cab.answers] == [("web", "connected"), ("site", "")]
    stale = Cabinet([{"id": "web", "repo": "o/Web", "action": "bind"},
                     {"id": "site", "repo": "example-owner/Site", "action": "unbind"}], fail_result="refused")
    pauses = []
    run = Runs(0)
    assert repo_attach.take(stale, run, reg, err=io.StringIO(), sleep=pauses.append) == (0, False)
    assert len(run.calls) == 2 and pauses == []  # a refusal is final and asked once; the next request goes on


def test_a_cabinet_restarting_is_asked_again(reg):
    pauses = []
    cab = Cabinet([{"id": "web", "repo": "o/Web", "action": "bind"}], down=2, result_down=1)
    assert repo_attach.take(cab, Runs(0), reg, err=io.StringIO(), sleep=pauses.append) == (1, False)
    assert pauses == [repo_attach.ASK_PAUSE] * 3
    gone = Cabinet([], down=repo_attach.ASK_TRIES)
    with pytest.raises(ConnectionRefusedError):  # the unit fails (kd-alert); the cabinet rings at its start
        repo_attach.take(gone, Runs(0), reg, err=io.StringIO(), sleep=lambda s: None)


def test_an_answer_the_cabinet_never_took_rings_again(tmp_path, reg):
    bell = tmp_path / "request"
    bell.write_text("x", encoding="utf-8")
    cab = Cabinet([{"id": "web", "repo": "o/Web", "action": "bind"}, {"id": "app", "repo": "o/App", "action": "bind"}],
                  result_down=repo_attach.ASK_TRIES)
    run = Runs(0)
    assert repo_attach.main(doorbell=str(bell), client=cab, lock_path=str(tmp_path / "lock"), run=run,
                            registry_path=reg, sleep=lambda s: None) == 0
    assert len(run.calls) == 1 and cab.answers == [] and bell.exists()  # the request waits, the bell rings again
    assert [i["id"] for i in cab.items] == ["web", "app"]


def test_main_takes_the_doorbell_off_first_and_reads_the_queue(tmp_path, reg):
    bell = tmp_path / "request"
    bell.write_text("x", encoding="utf-8")
    cab = Cabinet([{"id": "web", "repo": "o/Web", "action": "bind"}])
    assert repo_attach.main(doorbell=str(bell), client=cab, lock_path=str(tmp_path / "lock"), run=Runs(0),
                            registry_path=reg) == 0
    assert not bell.exists() and cab.answers[0]["state"] == "connected"
    # started without a bell (boot, by hand): the waiting requests are taken all the same
    cab = Cabinet([{"id": "web", "repo": "o/Web", "action": "bind"}])
    assert repo_attach.main(doorbell=str(bell), client=cab, lock_path=str(tmp_path / "lock"), run=Runs(6),
                            registry_path=reg) == 0
    assert cab.answers[0]["state"] == "no_access"


def test_a_bell_during_the_work_is_answered_in_the_same_start(tmp_path, reg):
    bell = tmp_path / "request"
    bell.write_text("x", encoding="utf-8")
    cab = Cabinet([{"id": "web", "repo": "o/Web", "action": "bind"}])

    def run(argv):  # the owner asks for another project meanwhile: the cabinet rings again
        if argv[2] == "web":
            cab.items.append({"id": "site", "repo": "example-owner/Site", "action": "unbind"})
            bell.write_text("x", encoding="utf-8")
        return repo_attach.Done(0)

    assert repo_attach.main(doorbell=str(bell), client=cab, lock_path=str(tmp_path / "lock"), run=run,
                            registry_path=reg) == 0
    assert [a["id"] for a in cab.answers] == ["web", "site"] and not bell.exists()


def test_requests_past_the_budget_ring_for_the_next_start(tmp_path, reg):
    bell = tmp_path / "request"
    cab = Cabinet([{"id": "web", "repo": "o/Web", "action": "bind"}, {"id": "app", "repo": "o/App", "action": "bind"}])
    now = iter([0, 0, 0, repo_attach.BUDGET + 1])  # the deadline, the round, the first request, the second
    assert repo_attach.main(doorbell=str(bell), client=cab, lock_path=str(tmp_path / "lock"), run=Runs(0),
                            registry_path=reg, clock=lambda: next(now)) == 0
    assert [a["id"] for a in cab.answers] == ["web"] and bell.exists()
    assert oct(bell.stat().st_mode & 0o777) == oct(0o660)


def test_a_doorbell_that_cannot_go_stops_the_run(tmp_path, reg):
    bell = tmp_path / "request"
    bell.mkdir()
    (bell / "x").write_text("", encoding="utf-8")
    cab = Cabinet([{"id": "web", "repo": "o/Web", "action": "bind"}])
    assert repo_attach.main(doorbell=str(bell), client=cab, lock_path=str(tmp_path / "lock"), run=Runs(0),
                            registry_path=reg) == 1
    assert cab.answers == [] and cab.items  # nothing taken


def test_units_and_bin():
    path = (ROOT / "common/systemd/digipaddock-repo-attach.path").read_text(encoding="utf-8")
    service = (ROOT / "common/systemd/digipaddock-repo-attach.service").read_text(encoding="utf-8")
    assert f"PathExists={repo_attach.DOORBELL}" in path and "Unit=digipaddock-repo-attach.service" in path
    assert "Type=oneshot" in service and "ExecStart=/usr/local/bin/kd-repo-attach" in service
    assert "RuntimeDirectory=digipaddock-repo-attach" in service and "OnFailure=digipaddock-alert@digipaddock-repo-attach.service" in service
    assert "StartLimitIntervalSec=0" in service
    sys.path.insert(0, str(ROOT / "common"))
    from kd.cabinet import server

    call = repo_attach.ASK_TRIES * server.TIMEOUT + (repo_attach.ASK_TRIES - 1) * repo_attach.ASK_PAUSE
    budget = repo_attach.BUDGET + repo_attach.T_ADD + 3 * call
    assert "TimeoutStartSec=1800" in service and budget < 1800
    binary = (ROOT / "agents/dev/bin/kd-repo-attach").read_text(encoding="utf-8")
    assert "from dev_agent.repo_attach import main" in binary
    cabinet = (ROOT / "common/systemd/digipaddock-cabinet.service").read_text(encoding="utf-8")
    assert "ReadWritePaths=-/var/lib/digipaddock/repo-attach" in cabinet


def test_the_doorbell_is_the_cabinets():
    sys.path.insert(0, str(ROOT / "common"))
    from kd.cabinet import projects_api

    assert projects_api.DOORBELL == repo_attach.DOORBELL
    assert projects_api.REPO.pattern == repo_attach.REPO_RE.pattern
    server = (ROOT / "common/kd/cabinet/server.py").read_text(encoding="utf-8")
    assert "if base.repo_requests():" in server and "projects_api.ring(ctx)" in server  # rings at its own start
