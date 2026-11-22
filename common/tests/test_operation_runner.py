"""kd-operation-run — the root runner of agent.reinstall on the factory host: each role is verified,
installed only when it differs, checked and verified again; the host's slots stay; the result is one file."""

import json
from pathlib import Path

import pytest

from kd import install_apply, operation_runner

OP = "e" * 32
REV = "b" * 40
NEW_AGENT = "/opt/kd/common/install/new_agent.sh"
HOST_SETUP = "/opt/kd/common/install/session_host_setup.sh"


class Host:
    """The commands the runner runs, answered as scripted: (argv tail) -> return code."""

    def __init__(self, codes=None, slots=None, slots_after=None, release=()):
        self.codes, self.calls = dict(codes or {}), []
        self.slots = [dict(slots or {"analyst": 1, "dev": 2})]
        self.slots_after = slots_after
        self.release = list(release)  # digipaddock-release.service active answers, then inactive
        self.slept = 0.0

    def run(self, argv):
        self.calls.append(tuple(argv))
        if argv[1:2] == ["--install"] and self.slots_after is not None:
            self.slots = [dict(self.slots_after)]
        return self.codes.get(tuple(argv[1:]), 0)

    def host_slots(self):
        return dict(self.slots[0])

    def release_active(self):
        return self.release.pop(0) if self.release else False

    def sleep(self, s):
        self.slept += s


def _state(tmp_path, agents=("dev",), revision=REV):
    ops = tmp_path / "operations"
    ops.mkdir(parents=True)
    (ops / f"{OP}.request.json").write_text(json.dumps({
        "operation": OP, "kind": "agent.reinstall", "agents": list(agents), "revision": revision, "timeout": 1800,
        "started": 1.0, "phase": "installing", "batch": {"n": 1, "agents": list(agents)}, "batches": [list(agents)],
        "done": {}}))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "REVISION").write_text(REV + "\n")
    return tmp_path


def _run(tmp_path, host):
    result = operation_runner.run(OP, state_dir=str(tmp_path), repo=tmp_path / "repo", run_cmd=host.run,
                                  host_slots=host.host_slots, release_active=host.release_active, sleep=host.sleep,
                                  new_agent=NEW_AGENT, host_setup=HOST_SETUP, batch=1)
    saved = json.loads((tmp_path / "operations" / f"{OP}.batch1.json").read_text())
    assert saved == result
    return result


def _cmds(host):
    return [c[1:] for c in host.calls]


def test_unchanged_role_skips_install(tmp_path):
    host = Host()
    result = _run(_state(tmp_path), host)
    assert result["state"] == "succeeded"
    assert result["agents"] == {"dev": {"state": "unchanged", "stage": "verify", "error": ""}}
    assert ("--install", "dev") not in _cmds(host)
    assert _cmds(host)[-1] == ("check",)


def test_install_check_verify_order(tmp_path):
    host = Host({("--verify", "dev"): 3})
    codes = iter([3, 0])  # differs before, matches after

    def run(argv):
        host.calls.append(tuple(argv))
        return next(codes) if argv[1:] == ["--verify", "dev"] else 0

    host.run = run
    result = _run(_state(tmp_path), host)
    assert _cmds(host) == [("--verify", "dev"), ("--install", "dev"), ("--check", "dev"), ("--verify", "dev"),
                           ("check",)]
    assert host.calls[0][0] == NEW_AGENT and host.calls[-1][0] == HOST_SETUP
    assert result["state"] == "succeeded" and result["agents"]["dev"] == {"state": "ok", "stage": "verify",
                                                                           "error": ""}


def test_revision_moved_refused(tmp_path):
    host = Host()
    result = _run(_state(tmp_path, agents=("analyst", "dev"), revision="c" * 40), host)
    assert result["state"] == "failed" and result["reason"] == "revision_moved"
    assert host.calls == []
    assert {a["state"] for a in result["agents"].values()} == {"skipped"}


def test_waits_for_release_then_gives_up(tmp_path):
    host = Host(release=[True] * 1000)
    result = _run(_state(tmp_path), host)
    assert result["state"] == "failed" and result["reason"] == "release_busy" and host.calls == []
    assert host.slept >= operation_runner.RELEASE_WAIT


def test_revision_moved_during_the_release_wait_refused(tmp_path):
    """Review I4: a release that ran while the runner waited moved the tree — an older one is never installed."""
    state = _state(tmp_path)
    host = Host(release=[True, True])
    sleeps = [0]

    def sleep(s):
        sleeps[0] += 1
        if sleeps[0] == 2:
            (tmp_path / "repo" / "REVISION").write_text("c" * 40 + "\n")

    host.sleep = sleep
    result = _run(state, host)
    assert result["state"] == "failed" and result["reason"] == "revision_moved" and host.calls == []


def test_host_slots_changed_fails(tmp_path):
    host = Host({("--verify", "dev"): 3}, slots_after={"analyst": 1, "dev": 1})
    verify = iter([3, 0])
    run = host.run

    def scripted(argv):
        if argv[1:] == ["--verify", "dev"]:
            host.calls.append(tuple(argv))
            return next(verify)
        return run(argv)

    host.run = scripted
    result = _run(_state(tmp_path), host)
    assert result["state"] == "failed"
    assert result["agents"]["dev"]["state"] == "failed" and result["agents"]["dev"]["stage"] == "slots"


def test_failed_check_names_role_and_stage(tmp_path):
    host = Host({("--verify", "analyst"): 3, ("--check", "analyst"): 1})
    result = _run(_state(tmp_path, agents=("analyst", "dev")), host)
    assert result["state"] == "failed" and result["reason"] == "install_failed"
    assert result["agents"]["analyst"] == {"state": "failed", "stage": "check", "error": "new_agent.sh --check: exit 1",
                                           "tail": "", "batch": 1}  # run_cmd captures no output
    assert result["agents"]["dev"]["state"] == "skipped"
    assert ("--install", "dev") not in _cmds(host)


def test_failed_install_names_role(tmp_path):
    host = Host({("--verify", "dev"): 3, ("--install", "dev"): 1})
    result = _run(_state(tmp_path), host)
    assert result["agents"]["dev"]["stage"] == "install" and result["state"] == "failed"


def test_host_check_failure_fails_the_operation(tmp_path):
    host = Host({("check",): 1})
    result = _run(_state(tmp_path), host)
    assert result["state"] == "failed" and result["reason"] == "host_check_failed"
    assert result["agents"]["dev"]["state"] == "unchanged"


def test_missing_request_writes_nothing(tmp_path):
    (tmp_path / "operations").mkdir()
    with pytest.raises(operation_runner.RunnerError):
        operation_runner.run(OP, state_dir=str(tmp_path), repo=tmp_path, run_cmd=lambda a: 0,
                             host_slots=dict, release_active=lambda: False, sleep=lambda s: None, batch=1)
    assert not list((tmp_path / "operations").iterdir())


# --- install_apply: --verify and the outdated roles ---------------------------------------------
class Ops:
    def __init__(self, matching):
        self.matching = set(matching)

    def matches(self, plan):
        return plan in self.matching


def test_outdated_agents_lists_mismatch(tmp_path):
    registry = tmp_path / "agents.json"
    registry.write_text(json.dumps({
        "analyst": {"runtime": "generated", "user": "digi-analyst"},
        "dev": {"runtime": "generated", "user": "digi-dev"},
        "boss": {"runtime": "generated", "user": "digi-boss"},
        "probe": {"user": "digi"},
    }))

    def plan_of(name):
        if name == "boss":
            raise install_apply.PlanError("manifest changed")
        return name

    found = install_apply.outdated_agents(Path("."), ops=Ops({"analyst"}), plan_of=plan_of,
                                          registry_path=str(registry))
    assert found == ["boss", "dev"]


def test_verify_mode_exit_codes(monkeypatch):
    monkeypatch.setattr(install_apply, "build_plan", lambda repo, view, name: name)
    monkeypatch.setattr(install_apply, "SystemOperations", lambda repo: Ops({"dev"}))
    assert install_apply.verify_main("dev", Path(".")) == 0
    assert install_apply.verify_main("analyst", Path(".")) == 3


# --- one batch of the request's roles ------------------------------------------------------------
def test_a_batch_installs_only_its_roles_into_its_own_file(tmp_path):
    state = _state(tmp_path, agents=("analyst", "dev"))
    path = state / "operations" / f"{OP}.request.json"
    request = json.loads(path.read_text())
    request.update(batch={"n": 2, "agents": ["dev"]}, batches=[["analyst"], ["dev"]], done={})
    path.write_text(json.dumps(request))
    host = Host()
    result = operation_runner.run(OP, state_dir=str(state), repo=state / "repo", run_cmd=host.run,
                                  host_slots=host.host_slots, release_active=host.release_active, sleep=host.sleep,
                                  new_agent=NEW_AGENT, host_setup=HOST_SETUP, batch=2)
    saved = json.loads((state / "operations" / f"{OP}.batch2.json").read_text())
    assert saved == result and result["batch"] == 2 and result["state"] == "succeeded"
    assert set(result["agents"]) == {"dev"} and not (state / "operations" / f"{OP}.result.json").exists()
    assert not any("analyst" in call for call in host.calls)
    with pytest.raises(operation_runner.RunnerError):
        operation_runner.run(OP, state_dir=str(state), repo=state / "repo", run_cmd=host.run,
                             host_slots=host.host_slots, release_active=host.release_active, sleep=host.sleep,
                             new_agent=NEW_AGENT, host_setup=HOST_SETUP, batch=3)


def test_the_runner_asks_the_cabinet_after_the_release_wait(tmp_path):
    """Review M6: a grant revoked while the runner waited for a release — nothing is installed."""
    host = Host(release=[True, True])
    asked = []

    def authorize(operation_id, agents):
        asked.append((operation_id, list(agents), len(host.release)))
        return "grant_revoked"

    result = operation_runner.run(OP, state_dir=str(_state(tmp_path)), repo=tmp_path / "repo", run_cmd=host.run,
                                  host_slots=host.host_slots, release_active=host.release_active, sleep=host.sleep,
                                  new_agent=NEW_AGENT, host_setup=HOST_SETUP, authorize=authorize, batch=1)
    assert asked == [(OP, ["dev"], 0)]  # after the wait, right before the first install
    assert result["state"] == "failed" and result["reason"] == "grant_revoked" and host.calls == []
    assert result["agents"]["dev"]["state"] == "skipped"


def test_the_runners_own_authorization_answers_a_reason(monkeypatch):
    from kd.cabinet import client as cabinet_client

    answers = [{"allowed": True, "reason": ""}, {"allowed": False, "reason": "revision_moved; rm -rf"},
               cabinet_client.CabinetError("unavailable", "down")]

    class Client:
        def call(self, request):
            assert request == {"op": "operation.authorize", "operation": OP, "agents": ["dev"]}
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

    monkeypatch.setattr(cabinet_client, "Client", Client)
    assert operation_runner._authorize(OP, ["dev"]) == ""
    assert operation_runner._authorize(OP, ["dev"]) == "refused"
    assert operation_runner._authorize(OP, ["dev"]) == "authorize_unavailable"
