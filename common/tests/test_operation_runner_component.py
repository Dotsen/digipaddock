"""kd-operation-run for ``component.install`` — the component and the mode map through the deployed tree's
own catalog to a fixed argv; the script, then its check; codes and masked tails in the batch result; the revision
checked before and after; nothing outside the catalog runs."""

import json
import shutil
from pathlib import Path

from kd import factory_ops, operation_runner

OP = "c" * 32
REV = "b" * 40
CATALOG = Path(factory_ops.CATALOG_PATH)


def _tree(tmp_path, component="repo-attach", mode="install", drain="", revision=REV, agents=()):
    ops = tmp_path / "state" / "operations"
    ops.mkdir(parents=True)
    (ops / f"{OP}.request.json").write_text(json.dumps({
        "operation": OP, "kind": "component.install", "agents": list(agents), "component": component,
        "mode": mode, "drain": drain, "revision": revision, "timeout": 1800, "started": 1.0,
        "phase": "installing", "done": {}, "batch": {"n": 1, "agents": []}, "batches": [[]]}))
    repo = tmp_path / "repo"
    (repo / "common" / "coordination").mkdir(parents=True)
    shutil.copy(str(CATALOG), str(repo / "common" / "coordination" / "factory.json"))
    for script in ("agents/dev/install/repo_attach_setup.sh", "common/install/transcribe_setup.sh",
                   "common/install/session_host_setup.sh"):
        (repo / script).parent.mkdir(parents=True, exist_ok=True)
        (repo / script).write_text("#!/bin/sh\n")
    (repo / "REVISION").write_text(REV + "\n")
    return tmp_path


class Box:
    def __init__(self, codes=None, outputs=None, on_call=None):
        self.codes, self.outputs, self.calls, self.on_call = dict(codes or {}), dict(outputs or {}), [], on_call

    def capture(self, argv):
        self.calls.append(list(argv))
        if self.on_call:
            self.on_call(argv)
        key = tuple(argv[1:]) if argv[0] != "systemctl" else tuple(argv)
        return self.codes.get(key, 0), self.outputs.get(key, "ok\n")


def _run(tmp, box, mask=lambda s: s.replace("hunter2", "***"), authorize=None, release=()):
    pending = list(release)
    result = operation_runner.run(
        OP, state_dir=str(tmp / "state"), repo=tmp / "repo", run_cmd=lambda a: 99, host_slots=dict,
        release_active=lambda: pending.pop(0) if pending else False, sleep=lambda s: None, batch=1,
        authorize=authorize, capture=box.capture, mask=mask)
    saved = json.loads((tmp / "state" / "operations" / f"{OP}.batch1.json").read_text())
    assert saved == result
    return result


def test_install_then_its_check(tmp_path):
    tmp = _tree(tmp_path)
    box = Box(outputs={("install",): "digipaddock-repo-attach.path enabled DB_PASSWORD=hunter2\n"})
    result = _run(tmp, box)
    script = str(tmp / "repo" / "agents" / "dev" / "install" / "repo_attach_setup.sh")
    assert box.calls == [[script, "install"], [script, "check"]]
    assert result["state"] == "succeeded" and result["agents"] == {} and result["batch"] == 1
    ran = result["component"]
    assert (ran["code"], ran["check"]) == (0, 0)
    assert "enabled" in ran["tail"] and "hunter2" not in ran["tail"]


def test_a_failed_script_skips_its_check(tmp_path):
    tmp = _tree(tmp_path)
    box = Box(codes={("install",): 3})
    result = _run(tmp, box)
    assert len(box.calls) == 1
    assert result["state"] == "failed" and result["reason"] == "install_failed"
    assert result["component"]["code"] == 3 and result["component"]["check"] is None


def test_a_failed_check_fails(tmp_path):
    result = _run(_tree(tmp_path), Box(codes={("check",): 1}, outputs={("check",): "digipaddock-repo-attach.path не запущен"}))
    assert result["state"] == "failed" and result["reason"] == "check_failed"
    assert "не запущен" in result["component"]["check_tail"]


def test_a_mode_without_check_runs_alone(tmp_path):
    box = Box()
    result = _run(_tree(tmp_path, mode="check"), box)
    assert len(box.calls) == 1 and box.calls[0][-1] == "check" and result["state"] == "succeeded"
    assert result["component"]["check"] is None


def test_the_host_restart_is_systemctl_then_the_host_check(tmp_path):
    box = Box()
    result = _run(_tree(tmp_path, component="session-host", mode="restart", drain="all"), box)
    assert box.calls[0] == ["systemctl", "restart", "digipaddock-session-host.service"]
    assert box.calls[1] == [str(tmp_path / "repo" / "common" / "install" / "session_host_setup.sh"), "check"]
    assert result["state"] == "succeeded"


def test_outside_the_catalog_nothing_runs(tmp_path):
    for component, mode in (("repo-attach", "disable"), ("nginx", "install"), ("../x", "install")):
        tmp = tmp_path / f"{component}-{mode}".replace("/", "_").replace(".", "_")
        tmp.mkdir()
        box = Box()
        result = _run(_tree(tmp, component=component, mode=mode), box)
        assert box.calls == [] and result["state"] == "failed" and result["reason"] == "not_in_catalog"


def test_a_script_reached_through_a_link_is_refused(tmp_path):
    tmp = _tree(tmp_path)
    script = tmp / "repo" / "agents" / "dev" / "install" / "repo_attach_setup.sh"
    script.unlink()
    script.symlink_to("/bin/true")
    box = Box()
    result = _run(tmp, box)
    assert box.calls == [] and result["reason"] == "not_in_catalog"


def test_revision_moved_before_runs_nothing(tmp_path):
    box = Box()
    result = _run(_tree(tmp_path, revision="d" * 40), box)
    assert box.calls == [] and result["reason"] == "revision_moved"


def test_revision_changed_under_the_run_is_no_success(tmp_path):
    tmp = _tree(tmp_path)

    def switch(argv):
        (tmp / "repo" / "REVISION").write_text("d" * 40 + "\n")

    result = _run(tmp, Box(on_call=switch))
    assert result["state"] == "failed" and result["reason"] == "revision_changed"
    assert result["component"]["code"] == 0


def test_refused_authorization_runs_nothing(tmp_path):
    asked = []

    def authorize(operation_id, agents):
        asked.append(list(agents))
        return "grant_revoked"

    box = Box()
    result = _run(_tree(tmp_path), box, authorize=authorize, release=[True])
    assert asked == [[]] and box.calls == [] and result["reason"] == "grant_revoked"


def test_a_script_that_cannot_start_is_a_known_failure(tmp_path):
    def capture(argv):
        raise PermissionError("not executable")

    box = Box()
    box.capture = capture
    result = _run(_tree(tmp_path), box)
    assert result["state"] == "failed" and result["reason"] == "launch_failed"
    assert result["component"]["code"] is None and "PermissionError" in result["component"]["tail"]


def test_no_masks_no_output(tmp_path):
    result = _run(_tree(tmp_path), Box(outputs={("install",): "secret"}), mask=lambda s: None)
    assert result["component"]["tail"] == operation_runner.UNMASKED


def test_the_tail_is_cut_after_masking(tmp_path):
    long = "x" * 5000 + "\nend DB_PASSWORD=hunter2\n"
    result = _run(_tree(tmp_path), Box(outputs={("install",): long}))
    tail = result["component"]["tail"]
    assert len(tail) == operation_runner.TAIL_MAX and tail.endswith("***\n")
