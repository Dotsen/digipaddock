"""The dev.check root runner — Forgejo's PR and statuses, the checkout with the
read key, the sandboxed child unit per profile, the result file. Fakes for git, Forgejo and systemd-run: nothing
real starts."""

import json
import os

import pytest

from kd import ci_unit
from kd import devcheck_runner as dr
from kd.session_host import devcheck as host_devcheck

ID = "a" * 32
HEAD, BASE, DEPLOY = "b" * 40, "c" * 40, "d" * 40
PARAMS = {"card": "KD-50", "project": "digipaddock", "repo": "acme/app", "pr": 7, "head": HEAD, "base_sha": BASE,
          "target": "main", "profiles": ["unit", "e2e"], "contour": "app-ci", "timeout_s": 1800,
          "set": {"digipaddock-deploy": DEPLOY}}
HOST = {"forge_api": "https://forge.example.test/api/v1", "cabinet_origin": "https://cabinet.example.test",
        "forge_ssh_url": "ssh://git@forge.example.test/{repo}.git"}
REPOS = {"digipaddock": ("acme/app", "main"), "digipaddock-deploy": ("acme/app-deploy", "main")}


class Forge:
    def __init__(self, prs=None):
        self.prs = list(prs or [])
        self.statuses = []

    def pr(self, repo, number):
        answer = self.prs.pop(0) if self.prs else {"state": "open", "head": HEAD, "base": "main"}
        if isinstance(answer, Exception):
            raise answer
        return answer

    def status(self, repo, sha, *, context, state, description, url):
        self.statuses.append((sha, context, state, description, url))

    def final(self):
        return {(c, s) for _, c, s, _, _ in self.statuses if s != "pending"}


class Git:
    def __init__(self, heads=None, fetch_fails=0):
        self.calls, self.heads, self.fails = [], dict(heads or {}), fetch_fails

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[0] == "init":
            os.makedirs(argv[-1], exist_ok=True)
            return 0, ""
        directory = argv[1]
        if "fetch" in argv:
            if self.fails:
                self.fails -= 1
                return 128, "ssh: connect to host forge.example.test: Connection refused"
            return 0, ""
        if "rev-parse" in argv:
            name = os.path.basename(directory)
            return 0, self.heads.get(name, HEAD if name == "src" else DEPLOY) + "\n"
        return 0, ""


class Units:
    def __init__(self, codes=None):
        self.calls, self.codes = [], dict(codes or {})

    def __call__(self, argv):
        self.calls.append(list(argv))
        profile = ci_unit.command_of(argv)[4]  # python -I steps.py <project> <profile>
        rc = self.codes.get(profile, 0)
        out = "== pytest\nFAILED test_x - token=abc\n== pytest failed (exit 1)\n== counts {\"failed\": 1}" if rc else \
            "== pytest\n9 passed\n== counts {\"passed\": 9}"
        return rc, out


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setitem(dr.steps.STEPS, "digipaddock", {**dr.steps.STEPS["digipaddock"], "e2e": ()})  # a second profile
    state = tmp_path / "state"
    (state / host_devcheck.DIR).mkdir(parents=True)
    (state / host_devcheck.DIR / f"{ID}.request.json").write_text(json.dumps(
        {"check": ID, "params": PARAMS, "timeout": 2400, "started": 1.0}), encoding="utf-8")
    token = tmp_path / "digipaddock-ci.env"
    token.write_text("FORGE_TOKEN=" + "f" * 40 + "\n", encoding="utf-8")

    class Env:
        pass

    e = Env()
    e.tmp, e.state, e.token, e.forge, e.git, e.units = tmp_path, state, token, Forge(), Git(), Units()
    e.owned, e.deps_calls, e.clock = [], [], iter(range(1000, 100000, 7))
    e.deps = lambda check_id, project, checkout, sets, **kw: e.deps_calls.append(
        (project, checkout, dict(sets))) or e.budgets.append(kw.get("budget_s")) or {
        "venv": "/cache/venv-1", "node": "/cache/node-1"}
    e.budgets = []
    return e


def run(e, **over):
    kwargs = dict(state_dir=str(e.state), forge=lambda token: e.forge, git=e.git, units=e.units,
                  clock=lambda: next(e.clock), sleep=lambda s: None, work=str(e.tmp / "work"),
                  log_dir=str(e.tmp / "log"), token=str(e.token), deps=e.deps,
                  own=lambda path, **kw: e.owned.append((path, kw)), deployed="/opt/tree", host=HOST, repos=REPOS,
                  mask=lambda text: text.replace("token=abc", "token=***"), ci_user="digi-ci", observe_group="digi-observe",
                  tools=lambda: [])
    kwargs.update(over)
    return dr.run(ID, **kwargs)


def stored(e):
    return json.loads((e.state / host_devcheck.DIR / f"{ID}.result.json").read_text(encoding="utf-8"))


def test_no_token_is_environment_not_ready_without_statuses(env):
    env.token.write_text("", encoding="utf-8")
    result = run(env)
    assert result["class"] == "environment_not_ready" and "токен" in result["maintenance"]
    assert env.forge.statuses == [] and env.git.calls == [] and stored(env) == result


def test_head_moved_before_start_is_superseded(env):
    env.forge.prs = [{"state": "open", "head": "e" * 40, "base": "main"}]
    result = run(env)
    assert result["class"] == "superseded" and env.forge.statuses == [] and env.git.calls == []


def test_a_closed_pr_is_superseded(env):
    env.forge.prs = [{"state": "closed", "head": HEAD, "base": "main"}]
    assert run(env)["class"] == "superseded"


def test_pending_then_success_statuses_on_the_head(env):
    result = run(env)
    assert result["class"] == "passed" and result["log"] == f"devcheck/{ID}.log"
    first = env.forge.statuses[:2]
    assert {(c, s) for _, c, s, _, _ in first} == {("devcheck/unit", "pending"), ("devcheck/e2e", "pending")}
    assert env.forge.final() == {("devcheck/unit", "success"), ("devcheck/e2e", "success")}
    assert all(sha == HEAD and url == "https://cabinet.example.test/cards/KD-50"
               for sha, _, _, _, url in env.forge.statuses)
    assert all(len(d) <= dr.DESCRIPTION_MAX for _, _, _, d, _ in env.forge.statuses)
    assert [p["profile"] for p in result["profiles"]] == ["unit", "e2e"]
    assert result["profiles"][0]["counts"] == {"passed": 9} and result["profiles"][0]["status"] == "success"


def test_first_red_profile_stops_the_rest_with_error_statuses(env):
    env.units.codes = {"unit": 1}
    result = run(env)
    assert result["class"] == "checks_failed" and len(env.units.calls) == 1
    assert env.forge.final() == {("devcheck/unit", "failure"), ("devcheck/e2e", "error")}
    assert [d for _, c, s, d, _ in env.forge.statuses if c == "devcheck/e2e" and s == "error"][0].startswith(
        "не запускался")
    assert [p["status"] for p in result["profiles"]] == ["failure"]


def test_masked_tail_in_the_result(env):
    env.units.codes = {"unit": 1}
    result = run(env)
    assert "token=***" in result["error_short"] and "token=abc" not in result["error_short"]
    log = (env.tmp / "log" / f"{ID}.log").read_text(encoding="utf-8")
    assert "token=***" in log and "token=abc" not in log


def test_no_masks_no_output(env):
    env.units.codes = {"unit": 1}
    result = run(env, mask=lambda text: None)
    assert result["error_short"] == dr.UNMASKED and "token=abc" not in (env.tmp / "log" / f"{ID}.log").read_text(
        encoding="utf-8")


def test_head_moved_during_the_run_is_superseded(env):
    env.forge.prs = [{"state": "open", "head": HEAD, "base": "main"}, {"state": "open", "head": "e" * 40,
                                                                         "base": "main"}]
    result = run(env)
    assert result["class"] == "superseded"
    assert env.forge.final() == {("devcheck/unit", "error"), ("devcheck/e2e", "error")}
    assert {sha for sha, *_ in env.forge.statuses} == {HEAD}


def test_fetch_failure_retries_once_then_external_unavailable(env):
    env.git.fails = 1
    assert run(env)["class"] == "passed"
    env2_calls = [c for c in env.git.calls if "fetch" in c]
    assert len(env2_calls) == 4  # the candidate twice, its target and the set's component once
    env.git = Git(fetch_fails=2)
    env.forge.statuses = []
    result = run(env, git=env.git)
    assert result["class"] == "external_unavailable" and "Connection refused" in result["error_short"]
    assert env.forge.final() == {("devcheck/unit", "error"), ("devcheck/e2e", "error")}


def test_forge_unavailable_twice_is_external_unavailable(env):
    env.forge.prs = [dr.ForgeUnavailable("down"), dr.ForgeUnavailable("down")]
    assert run(env)["class"] == "external_unavailable"


def test_the_checkout_fetches_the_pull_ref_and_the_sets_base(env):
    run(env)
    fetches = [c for c in env.git.calls if "fetch" in c]
    assert fetches[0][-2:] == ["ssh://git@forge.example.test/acme/app.git", "refs/pull/7/head"]
    assert fetches[1][-2:] == ["ssh://git@forge.example.test/acme/app.git", "refs/heads/main"]  # the PR's target
    assert fetches[2][-2:] == ["ssh://git@forge.example.test/acme/app-deploy.git", "refs/heads/main"]
    work = str(env.tmp / "work" / ID)
    assert (work, {"user": "digi-ci", "recursive": True}) in env.owned
    assert env.deps_calls == [("digipaddock", os.path.join(work, "src"), {"digipaddock-deploy": os.path.join(work, "sets",
                                                                                                 "digipaddock-deploy")})]


def test_the_checkout_marks_the_base_the_check_is_about(env):
    """the history step reads refs/kd/base..HEAD — the base_sha of the request, not the target's tip."""
    run(env)
    src = str(env.tmp / "work" / ID / "src")
    assert ["-C", src, "cat-file", "-e", f"{BASE}^{{commit}}"] in env.git.calls
    assert ["-C", src, "update-ref", "refs/kd/base", BASE] in env.git.calls


def test_a_target_that_lost_the_base_is_superseded(env):
    git = env.git

    def lost(argv):
        return (1, "") if "cat-file" in argv else git(argv)

    result = run(env, git=lost)
    assert result["class"] == "superseded" and "no longer holds" in result["error_short"]
    assert not [c for c in git.calls if "update-ref" in c]


def test_a_set_component_that_moved_is_superseded(env):
    env.git.heads = {"digipaddock-deploy": "e" * 40}
    result = run(env)
    assert result["class"] == "superseded" and "digipaddock-deploy" in result["error_short"]


def test_dependencies_not_prepared(env):
    def deps(*args, **kw):
        raise dr.DepsFailed("pip: no matching distribution")

    result = run(env, deps=deps)
    assert result["class"] == "dependency_update_required" and "no matching" in result["error_short"]
    assert env.units.calls == []


def test_child_unit_properties_are_exact(env):
    run(env)
    argv = env.units.calls[0]
    work = str(env.tmp / "work" / ID)
    src = os.path.join(work, "src")
    assert argv[:7] == ["systemd-run", "--unit", host_devcheck.child_unit(ID), "--wait", "--pipe", "--collect",
                        "--quiet"]
    props = [argv[i + 1] for i, a in enumerate(argv) if a == "-p"]
    # no User=: the unit starts as root with what the wrapper needs, the ci user is taken inside its own pid namespace
    assert props == ["CapabilityBoundingSet=CAP_SYS_ADMIN CAP_SETUID CAP_SETGID CAP_SETPCAP", "NoNewPrivileges=yes",
                     "TimeoutStopSec=15", "PrivateIPC=yes", ci_unit.SHM, "PrivateNetwork=yes", "PrivateTmp=yes",
                     "ProtectSystem=strict",
                     "ProtectHome=yes", f"ReadWritePaths={work}", "CPUQuota=200%",
                     "MemoryMax=3G", "RuntimeMaxSec=1200",
                     "InaccessiblePaths=" + " ".join("-" + p for p in dr.INACCESSIBLE)]
    assert [a for a in argv if a.startswith("--setenv=")] == [
        "--setenv=USER=digi-ci", "--setenv=LOGNAME=digi-ci", f"--setenv=HOME={os.path.join(work, 'home')}",
        "--setenv=CI=1"]
    tail = argv[argv.index("--") + 1:]
    assert tail == [ci_unit.UNSHARE, "--pid", "--fork", "--kill-child", "--mount-proc", "--",
                    ci_unit.SETPRIV, "--reuid=digi-ci", "--regid=digi-ci", "--init-groups", "--inh-caps=-all",
                    "--bounding-set=-all", "--no-new-privs", "--", ci_unit.ENV, f"--chdir={src}", "--",
                    "/cache/venv-1/bin/python", "-I", "/opt/tree/common/kd/devcheck_steps.py", "digipaddock", "unit", src,
                    f"digipaddock-deploy={os.path.join(work, 'sets', 'digipaddock-deploy')}"]


def test_no_isolation_tools_is_environment_not_ready_before_anything_runs(env):
    result = run(env, tools=lambda: [ci_unit.SETPRIV])
    assert result["class"] == "environment_not_ready" and ci_unit.SETPRIV in result["maintenance"]
    assert env.forge.statuses == [] and env.git.calls == [] and env.units.calls == [] and env.deps_calls == []


def test_a_short_timeout_limits_the_child(env):
    params = dict(PARAMS, timeout_s=300)
    (env.state / host_devcheck.DIR / f"{ID}.request.json").write_text(json.dumps(
        {"check": ID, "params": params, "timeout": 900, "started": 1.0}), encoding="utf-8")
    run(env)
    assert "RuntimeMaxSec=300" in env.units.calls[0]


def test_log_and_result_paths(env):
    result = run(env)
    log = env.tmp / "log" / f"{ID}.log"
    assert log.exists() and (str(log), {"group": "digi-observe"}) in env.owned
    assert result == stored(env) and result["check"] == ID
    assert not (env.tmp / "work" / ID).exists()  # cleaned up


def test_cleanup_failure_asks_for_maintenance(env):
    def remove(path):
        raise OSError("busy")

    result = run(env, remove=remove)
    assert result["class"] == "passed" and "очистка" in result["maintenance"]


def test_a_cancel_marked_by_the_host_ends_cancelled(env):
    path = env.state / host_devcheck.DIR / f"{ID}.request.json"
    request = json.loads(path.read_text(encoding="utf-8"))

    def units(argv):
        path.write_text(json.dumps({**request, "cancel": "superseded"}), encoding="utf-8")
        return 143, "killed"

    result = run(env, units=units)
    assert result["class"] == "superseded"
    assert env.forge.final() == {("devcheck/unit", "error"), ("devcheck/e2e", "error")}


def test_a_stop_signal_closes_the_statuses(env):
    path = env.state / host_devcheck.DIR / f"{ID}.request.json"
    request = json.loads(path.read_text(encoding="utf-8"))

    def units(argv):
        path.write_text(json.dumps({**request, "cancel": "card_left"}), encoding="utf-8")
        raise dr.Terminated()

    result = run(env, units=units)
    assert result["class"] == "cancelled" and stored(env) == result
    assert env.forge.final() == {("devcheck/unit", "error"), ("devcheck/e2e", "error")}
    assert not (env.tmp / "work" / ID).exists()


def test_a_runner_fault_still_ends_the_statuses(env):
    def deps(*args, **kw):
        raise RuntimeError("boom")

    result = run(env, deps=deps)
    assert result["class"] == "environment_not_ready" and "RuntimeError: boom" in result["error_short"]
    assert env.forge.final() == {("devcheck/unit", "error"), ("devcheck/e2e", "error")} and stored(env) == result


def test_a_profile_without_steps_is_environment_not_ready(env, monkeypatch):
    monkeypatch.setitem(dr.steps.STEPS, "digipaddock", {"unit": ()})
    result = run(env)
    assert result["class"] == "environment_not_ready" and "e2e" in result["error_short"]
    assert env.forge.statuses == [] and env.git.calls == []


def test_a_refused_token_asks_for_maintenance(env):
    env.forge.prs = [dr.ForgeRefused("Forgejo 401")]
    result = run(env)
    assert result["class"] == "environment_not_ready" and "токен" in result["maintenance"]


def test_the_forgejo_client():
    seen = []

    class Answer:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return self.body

    def opener(req, timeout):
        seen.append((req.get_method(), req.full_url, req.data, req.get_header("Authorization")))
        if req.full_url.endswith("/pulls/8"):
            raise dr.urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
        if req.full_url.endswith("/pulls/9"):
            raise dr.urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
        if req.full_url.endswith("/pulls/10"):
            raise dr.urllib.error.URLError("refused")
        return Answer(json.dumps({"number": 7, "state": "closed", "merged": True, "head": {"sha": HEAD.upper()},
                                  "base": {"ref": "main"}}).encode() if req.get_method() == "GET" else b"{}")

    forge = dr.Forgejo("t" * 40, base="https://forge.example.test/api/v1", opener=opener)
    assert forge.pr("acme/app", 7) == {"state": "merged", "head": HEAD, "base": "main"}
    assert forge.pr("acme/app", 8) is None
    with pytest.raises(dr.ForgeRefused):
        forge.pr("acme/app", 9)
    with pytest.raises(dr.ForgeUnavailable):
        forge.pr("acme/app", 10)
    forge.status("acme/app", HEAD, context="devcheck/unit", state="success", description="ok", url="https://c/x")
    method, url, data, auth = seen[-1]
    assert (method, url) == ("POST", f"https://forge.example.test/api/v1/repos/acme/app/statuses/{HEAD}")
    assert json.loads(data) == {"state": "success", "context": "devcheck/unit", "description": "ok",
                                "target_url": "https://c/x"}
    assert auth == "token " + "t" * 40
    with pytest.raises(dr.ForgeRefused):
        dr.Forgejo("t" * 40, base="http://forge.example.test/api/v1")


def test_no_request_is_an_error(env):
    with pytest.raises(dr.RunnerError):
        dr.run("f" * 32, state_dir=str(env.state), forge=None, git=None, units=None, clock=None, deps=None,
               own=None)
    with pytest.raises(dr.RunnerError):
        dr.run("../x", state_dir=str(env.state), forge=None, git=None, units=None, clock=None, deps=None, own=None)


def test_final_status_not_written_is_external_unavailable(env):
    class Flaky(Forge):
        def status(self, repo, sha, *, context, state, description, url):
            if state != "pending":
                raise dr.ForgeUnavailable("down")
            super().status(repo, sha, context=context, state=state, description=description, url=url)

    env.forge = Flaky()
    assert run(env)["class"] == "external_unavailable"


def test_main_usage():
    assert dr.main(["x"]) == 2 and dr.main(["dev.check"]) == 2


# --- final review 28.10: the runner owns its tests' lifetime and its own limit --------------------------------------
def test_a_stopped_runner_stops_its_tests_before_the_cleanup(env):
    order = []

    def units(argv):
        raise dr.Terminated()

    def remove(path):
        order.append(("remove", path))
        import shutil

        shutil.rmtree(path)

    result = run(env, units=units, remove=remove, stop=lambda names: order.append(("stop", list(names))))
    assert result["class"] == "host_lost"
    assert order[0] == ("stop", [host_devcheck.child_unit(ID), host_devcheck.deps_unit(ID)])
    assert [kind for kind, _ in order] == ["stop", "remove"]


def test_a_runner_fault_stops_its_tests_too(env):
    stopped = []

    def deps(*args, **kw):
        raise RuntimeError("boom")

    assert run(env, deps=deps, stop=stopped.append)["class"] == "environment_not_ready"
    assert stopped == [[host_devcheck.child_unit(ID), host_devcheck.deps_unit(ID)]]


def test_a_normal_end_stops_nothing(env):
    stopped = []
    assert run(env, stop=stopped.append)["class"] == "passed" and stopped == []


def test_the_dependencies_get_the_grace_the_tests_leave(env):
    run(env)
    # the request's timeout 2400 less the end's reserve, less the tests' own 1800, less what already ran
    assert env.budgets and 0 < env.budgets[0] <= 2400 - dr.STOP_S - 1800


def test_dependencies_that_ate_the_runners_limit_are_not_the_candidates_red(env):
    def deps(check_id, project, checkout, sets, **kw):
        env.clock = iter(range(3300, 100000, 7))  # past the runner's own limit: 1000 + 2400 - STOP_S
        return {"venv": "/cache/venv-1", "node": "/cache/node-1"}

    result = run(env, deps=deps)
    assert result["class"] == "environment_not_ready" and env.units.calls == []
    assert env.forge.final() == {("devcheck/unit", "error"), ("devcheck/e2e", "error")}


def test_slow_dependencies_are_environment_not_ready(env):
    def deps(*args, **kw):
        raise dr.DepsSlow("venv: pip … cut at 300 s")

    result = run(env, deps=deps)
    assert result["class"] == "environment_not_ready" and "300 s" in result["error_short"]
    assert {d for _, _, s, d, _ in env.forge.statuses if s == "error"} == {
        "не запускался: зависимости не успели подготовиться"}
