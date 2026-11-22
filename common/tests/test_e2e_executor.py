import io
import json
import os
from pathlib import Path
import stat
import subprocess

import pytest

from kd import e2e


def prepared(tmp_path):
    root = tmp_path / "runtime"
    files = (
        "app/common/cabinet/web/node_modules/@playwright/test/cli.js",
        "app/common/cabinet/web/e2e/isolated.config.ts",
        "app/common/cabinet/web/e2e/hosts",
        "app/common/cabinet/web/e2e/nsswitch.conf",
        "app/common/cabinet/web/dist/index.html",
        "app/common/bin/kd-cabinet",
        "python/bin/python",
    )
    for name in files:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture", encoding="utf-8")
    (root / "app/tmp").mkdir()
    (root / "browsers").mkdir()
    (root / "revision.json").write_text(
        json.dumps({"commit": "a" * 40}), encoding="utf-8"
    )
    return root


def test_unprepared_or_unknown_runtime_does_not_start_a_process(tmp_path):
    with pytest.raises(e2e.Unavailable, match="missing"):
        e2e.runtime_info(tmp_path / "absent")
    with pytest.raises(ValueError, match="unknown"):
        e2e.run_suite("https://production.invalid/", runtime=tmp_path)
    root = prepared(tmp_path)
    assert e2e.runtime_info(root, require_root_owner=False)["commit"] == "a" * 40
    (root / "revision.json").write_text('{"commit":"HEAD"}', encoding="utf-8")
    with pytest.raises(e2e.Unavailable, match="full observed"):
        e2e.runtime_info(root, require_root_owner=False)


def test_namespace_has_no_host_network_or_secret_mounts():
    argv = e2e.namespace_argv(Path("/prepared/runtime"), Path("/private/run"))
    assert "--unshare-all" in argv and "--clearenv" in argv and "--cap-drop" in argv
    assert "--share-net" not in argv and "--no-sandbox" not in argv
    mounts = [
        argv[i + 1] for i, arg in enumerate(argv) if arg in ("--ro-bind", "--bind")
    ]
    assert (
        "/etc" not in mounts
        and "/" not in mounts
        and "/home" not in mounts
        and "/run" not in mounts
    )
    assert argv[-3:] == ["test", "--config", "e2e/isolated.config.ts"]
    # Name resolution uses only the trusted bundle, never the host's DNS files.
    for name, destination in (
        ("hosts", "/etc/hosts"),
        ("nsswitch.conf", "/etc/nsswitch.conf"),
    ):
        source = str(Path("/prepared/runtime/app/common/cabinet/web/e2e") / name)
        index = argv.index(source)
        assert argv[index - 1 : index + 2] == ["--ro-bind", source, destination]
        assert destination not in mounts


@pytest.mark.parametrize("bad_owner", [True, False])
def test_runtime_refuses_untrusted_ownership_or_group_writable_code(
    tmp_path, monkeypatch, bad_owner
):
    root = prepared(tmp_path)
    target = root / "app/common/bin/kd-cabinet"
    real = Path.lstat

    def metadata(path):
        fields = list(real(path))
        fields[4] = 1234 if path == target and bad_owner else 0
        fields[0] &= ~0o022
        if path == target and not bad_owner:
            fields[0] |= stat.S_IWGRP
        return os.stat_result(fields)

    monkeypatch.setattr(Path, "lstat", metadata)
    with pytest.raises(e2e.Unavailable, match="root-owned and read-only"):
        e2e.runtime_info(root)


@pytest.mark.skipif(os.name != "posix", reason="requires Linux filesystem objects")
def test_runtime_refuses_escaping_links_and_special_files(tmp_path):
    root = prepared(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("not part of the runtime", encoding="utf-8")
    link = root / "escape"
    link.symlink_to(outside)
    with pytest.raises(e2e.Unavailable, match="link escapes"):
        e2e.runtime_info(root, require_root_owner=False)
    link.unlink()
    os.mkfifo(root / "fifo")
    with pytest.raises(e2e.Unavailable, match="special file"):
        e2e.runtime_info(root, require_root_owner=False)


def test_namespace_cleanup_and_exit_are_reported(tmp_path, monkeypatch):
    root = prepared(tmp_path)
    runs = tmp_path / "runs"
    monkeypatch.setattr(e2e.sys, "platform", "linux")
    real = Path.is_file
    monkeypatch.setattr(
        Path, "is_file", lambda p: True if p.as_posix() == "/usr/bin/bwrap" else real(p)
    )
    seen = []

    class Process:
        def wait(self, timeout):
            return 1

    def popen(argv, **kwargs):
        seen.append((argv, kwargs))
        return Process()

    result = e2e.run_suite(
        "cabinet-smoke", runtime=root, runs=runs, popen=popen, require_root_owner=False
    )
    assert result["state"] == "failed" and result["exit_code"] == 1
    assert not list(runs.glob("e2e-*"))
    assert len(list(runs.glob("artifacts-*"))) == 1
    assert seen[0][1]["start_new_session"] and seen[0][1]["preexec_fn"] == e2e._limits


def test_timeout_kills_the_process_group_and_removes_run_data(tmp_path, monkeypatch):
    root = prepared(tmp_path)
    runs = tmp_path / "runs"
    monkeypatch.setattr(e2e.sys, "platform", "linux")
    real = Path.is_file
    monkeypatch.setattr(
        Path, "is_file", lambda p: True if p.as_posix() == "/usr/bin/bwrap" else real(p)
    )
    killed = []
    monkeypatch.setattr(
        e2e.os, "killpg", lambda *args: killed.append(args), raising=False
    )
    monkeypatch.setattr(e2e.signal, "SIGKILL", 9, raising=False)

    class Process:
        pid = 12345

        def wait(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired("bwrap", timeout)
            return -9

    result = e2e.run_suite(
        "cabinet-smoke",
        runtime=root,
        runs=runs,
        popen=lambda *args, **kwargs: Process(),
        require_root_owner=False,
    )
    assert result["state"] == "failed" and result["reason"] == "timeout"
    assert killed == [(12345, e2e.signal.SIGKILL)]
    assert not list(runs.glob("e2e-*"))


def test_executor_requires_a_channel_capability_and_approved_assigned_card(
    tmp_path, monkeypatch
):
    registry = tmp_path / "agents.json"
    registry.write_text(
        json.dumps({"dev": {"user": "digi-dev"}, "analyst": {"user": "digi-analyst"}}),
        encoding="utf-8",
    )
    repo = Path(__file__).parents[2]
    effects = []

    class Cabinet:
        card = {"assignee": "@dev", "status": "Inbox"}

        def call(self, request):
            if request["op"] == "card.get":
                return {"card": self.card}
            effects.append(request)
            return {}

    cabinet = Cabinet()

    def main(caller=("dev", "digi-dev")):
        return e2e.main(
            ["run", "cabinet-smoke", "--card", "KD-1"],
            caller=caller,
            client=cabinet,
            registry_path=registry,
            repo_root=repo,
            out=io.StringIO(),
        )

    def run(suite):
        effects.append("run")
        raise e2e.Unavailable("prepared runtime missing")

    monkeypatch.setattr(e2e, "run_suite", run)
    for caller in (None, ("dev", "digi-analyst")):
        with pytest.raises(ValueError, match="channel"):
            main(caller)
    with pytest.raises(ValueError, match="capability"):
        main(("analyst", "digi-analyst"))
    for card in (
        {"assignee": "@dev", "status": "Inbox"},
        {"assignee": "@dev", "status": "To Do"},  # review M2: not taken yet
        {"assignee": "@analyst", "status": "In Progress"},
    ):
        cabinet.card = card
        with pytest.raises(ValueError, match="assignee of an approved"):
            main()
    assert not effects
    cabinet.card = {"assignee": "@dev", "status": "In Progress"}
    assert main() == 4
    assert effects[0] == "run"
    assert effects[1]["op"] == "worker.edit"
    assert '"state": "blocked"' in effects[1]["append_notes"][0]


def _fake_bwrap(monkeypatch):
    monkeypatch.setattr(e2e.sys, "platform", "linux")
    real = Path.is_file
    monkeypatch.setattr(
        Path, "is_file", lambda p: True if p.as_posix() == "/usr/bin/bwrap" else real(p)
    )


def test_the_log_is_read_from_its_handle_not_from_what_the_run_left_in_its_place(tmp_path, monkeypatch):
    """Review 04.11.2026: the run folder is the namespace's writable tmp; a link (or a FIFO) put in the log's place
    is never opened — the executor reads back what it wrote."""
    root = prepared(tmp_path)
    runs = tmp_path / "runs"
    _fake_bwrap(monkeypatch)
    secret = tmp_path / "host-file"
    secret.write_text("bwrap: a line of a host file\n")

    class Process:
        def wait(self, timeout):
            return 1

    def popen(argv, stdout=None, **kwargs):
        folder = Path(argv[argv.index("/runtime/app/tmp") - 1])
        stdout.write(b"plain failure\n")
        stdout.flush()
        (folder / "executor.log").unlink()
        (folder / "executor.log").symlink_to(secret)
        return Process()

    result = e2e.run_suite("cabinet-smoke", runtime=root, runs=runs, popen=popen, require_root_owner=False)
    assert result["state"] == "failed"  # a read of the link's target would say "blocked"


def test_a_run_keeps_a_bounded_set_of_artifacts_and_only_the_newest_runs(tmp_path, monkeypatch):
    root = prepared(tmp_path)
    runs = tmp_path / "runs"
    _fake_bwrap(monkeypatch)
    monkeypatch.setattr(e2e, "MAX_ARTIFACTS", 3)
    monkeypatch.setattr(e2e, "KEEP_ARTIFACTS", 2)

    class Process:
        def wait(self, timeout):
            return 1

    def popen(argv, **kwargs):
        folder = Path(argv[argv.index("/runtime/app/tmp") - 1])
        for i in range(5):
            (folder / f"shot{i}.png").write_bytes(b"x")
        return Process()

    for _ in range(4):
        result = e2e.run_suite("cabinet-smoke", runtime=root, runs=runs, popen=popen, require_root_owner=False)
    assert len(result["artifacts"]) == 3 and result["artifacts_dropped"] == 3  # 5 shots + the log, 3 kept
    assert len(list(runs.glob("artifacts-*"))) == 2
