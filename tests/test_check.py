"""Selection and reporting contracts of the explicit local check runner."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check.py"
SPEC = importlib.util.spec_from_file_location("explicit_checks", SCRIPT)
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


@pytest.fixture
def repository(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_example.py").write_text("def test_example(): pass\n")
    web = tmp_path / CHECK.WEB / "src"
    web.mkdir(parents=True)
    (web / "example.test.ts").write_text("// fixture\n")
    monkeypatch.setattr(CHECK, "ROOT", tmp_path)
    monkeypatch.setattr(
        CHECK, "source_info", lambda root: {"revision": "abc", "dirty": True}
    )
    return tmp_path


def test_profiles_do_not_implicitly_run_the_python_suite(repository):
    docs = CHECK.plan(["docs"], [], [], "HEAD", repository)
    assert [c["argv"] for c in docs] == [["git", "diff", "--check", "HEAD", "--"]]
    frontend = CHECK.plan(["frontend"], [], ["src/example.test.ts"], "HEAD", repository)
    assert len(frontend) == 3
    assert all("pytest" not in c["argv"] for c in frontend)
    assert frontend[1]["argv"][-2:] == ["--", "src/example.test.ts"]


def test_python_and_docs_checks_do_not_look_for_frontend_tools(repository, monkeypatch):
    def unexpected_lookup(*args, **kwargs):
        raise AssertionError("Frontend tools were not requested")

    monkeypatch.setattr(CHECK.shutil, "which", unexpected_lookup)
    CHECK.plan(["docs", "static", "full"], [], [], "HEAD", repository)
    CHECK.plan(["focused"], ["tests/test_example.py"], [], "HEAD", repository)


@pytest.mark.parametrize(
    "profiles, tests, web",
    [
        (["focused"], [], []),
        (["docs"], ["tests/test_example.py"], []),
        (["focused"], ["."], []),
        (["focused"], ["--collect-only"], []),
        (["focused"], ["../outside/test_secret.py"], []),
        (["frontend"], [], ["../outside.test.ts"]),
        (["docs"], [], ["src/example.test.ts"]),
        (["unknown"], [], []),
        ([], [], []),
    ],
)
def test_ambiguous_or_ignored_selections_are_errors(repository, profiles, tests, web):
    with pytest.raises(ValueError):
        CHECK.plan(profiles, tests, web, "HEAD", repository)


def test_focused_preserves_the_exact_node_id_and_deduplicates_profiles(repository):
    node = "tests/test_example.py::test_example[value]"
    commands = CHECK.plan(["focused", "focused"], [node], [], "HEAD", repository)
    assert len(commands) == 1
    assert commands[0]["argv"][-1] == node
    # pytest.ini supplies -q; doubling it would hide the successful test-count summary.
    assert "-q" not in commands[0]["argv"]


def test_full_is_explicit_and_shares_one_static_gate(repository):
    commands = CHECK.plan(["static", "full"], [], [], "HEAD~1", repository)
    assert len(commands) == 3
    assert commands[0]["argv"][-2:] == ["--base", "HEAD~1"]
    assert commands[1]["argv"][1:] == ["scripts/check_history.py"]
    assert commands[2]["argv"][1:3] == ["-m", "pytest"]
    assert "--durations=20" in commands[2]["argv"]
    assert "-n" not in commands[2]["argv"]


@pytest.mark.parametrize("workers", [2, 4])
def test_parallelism_is_explicit_and_keeps_files_together(repository, workers):
    commands = CHECK.plan(
        ["static", "focused", "full"],
        ["tests/test_example.py"],
        [],
        "HEAD",
        repository,
        workers=workers,
    )
    assert "-n" not in commands[0]["argv"] and "-n" not in commands[1]["argv"]
    for command in commands[2:]:
        argv = command["argv"]
        assert argv[argv.index("-n") + 1] == str(workers)
        assert "--dist=loadfile" in argv
    assert commands[2]["argv"][-1] == "tests/test_example.py"


@pytest.mark.parametrize(
    "profiles, workers",
    [(["docs"], 2), (["frontend"], 4), (["full"], 0), (["full"], 8)],
)
def test_ignored_or_unbounded_workers_are_rejected(repository, profiles, workers):
    with pytest.raises(ValueError):
        CHECK.plan(profiles, [], [], "HEAD", repository, workers=workers)


@pytest.mark.parametrize("workers, failing", [(1, False), (2, False), (2, True)])
def test_real_pytest_workers_isolate_state_and_preserve_failure(
    tmp_path, workers, failing
):
    pytest.importorskip("xdist")
    root = tmp_path / "checkout"
    (root / "scripts").mkdir(parents=True)
    (root / "tests").mkdir()
    shutil.copyfile(SCRIPT, root / "scripts/check.py")
    (root / "pytest.ini").write_text(
        "[pytest]\naddopts = -q --import-mode=importlib -p no:cacheprovider\n",
        encoding="utf-8",
    )
    # Identical DB/socket names must stay isolated across files and workers. Record
    # every case outside its temp dir so missing, duplicated or split-file work is visible.
    example = """import json, os, socket, sqlite3
from pathlib import Path
import pytest
@pytest.mark.parametrize("case", range(3))
def test_state(tmp_path, case):
    with sqlite3.connect(tmp_path / "cabinet.db") as db:
        db.execute("create table state (value integer)")
        db.execute("insert into state values (?)", (case,))
        assert db.execute("select value from state").fetchone() == (case,)
    if hasattr(socket, "SO_PEERCRED"):
        with socket.socket(socket.AF_UNIX) as sock:
            sock.bind(str(tmp_path / "local.sock"))
    here = Path(__file__)
    (here.parent / f"{here.stem}-{case}.json").write_text(json.dumps({
        "worker": os.environ.get("PYTEST_XDIST_WORKER", "main"), "tmp": str(tmp_path)
    }))
"""
    args = []
    for name in ("test_one", "test_two"):
        (root / f"tests/{name}.py").write_text(example, encoding="utf-8")
        args += ["--test", f"tests/{name}.py"]
    if failing:
        (root / "tests/test_failure.py").write_text(
            "def test_failure(): assert False\n"
        )
        args += ["--test", "tests/test_failure.py"]
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("PYTEST_")
    }
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/check.py",
            "--profile",
            "focused",
            "--workers",
            str(workers),
            "--scratch-root",
            "/tmp" if sys.platform.startswith("linux") else str(tmp_path),
            *args,
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    # xdist reports its -x interruption as 2; preserve that code rather than
    # normalizing every test failure to 1 or accepting a false successful run.
    expected_code = 2 if failing and workers > 1 else int(failing)
    assert proc.returncode == expected_code, proc.stdout + proc.stderr
    report = json.loads(next((root / "tmp/checks").glob("*/summary.json")).read_text())
    assert report["status"] == ("failed" if failing else "passed")
    assert report["exit_code"] == expected_code
    argv = report["commands"][0]["argv"]
    scratch = Path(argv[argv.index("--basetemp") + 1])
    assert not scratch.exists()  # private scratch is removed, including on failure
    assert Path(report["commands"][0]["log"]).is_file()
    if not failing:
        records = {
            p.stem: json.loads(p.read_text()) for p in (root / "tests").glob("*.json")
        }
        assert len(records) == 6
        assert len({r["tmp"] for r in records.values()}) == 6
        assert len({r["worker"] for r in records.values()}) == workers
        for name in ("test_one", "test_two"):
            assert (
                len({r["worker"] for k, r in records.items() if k.startswith(name)})
                == 1
            )


def test_ci_plans_what_github_ci_runs(repository):
    commands = CHECK.plan(["ci"], [], [], "origin/main", repository, workers=4)
    assert {c["profile"] for c in commands} == {"ci"}
    argv = [c["argv"][1:] for c in commands]
    assert argv[0][:2] == ["-I", "common/kd/privacy_scan.py"]
    assert argv[1] == ["scripts/check_changed.py", "--base", "origin/main"]
    assert argv[2][:3] == ["-m", "pytest", "-n"] and argv[2][3] == "4"
    assert argv[2][argv[2].index("-m", 1) + 1] == "not host"
    assert commands[2]["env"] == {"KD_HOST_JSON": "host/host.example.json"}
    assert [a[1:3] for a in argv[3:6]] == [
        ["common/kd/overlay.py", "init"],
        ["scripts/overlay_stand_ins.py", "{scratch}/overlay"],
        ["common/kd/overlay.py", "check"],
    ]
    assert all(a[-1] == "{scratch}/overlay" for a in argv[3:6])
    assert [a[2:] for a in argv[6:]] == [["run", "check"], ["test"], ["run", "build"]]
    assert all("env" not in c for i, c in enumerate(commands) if i != 2)


def test_ci_shares_the_static_gate_and_the_web_build_with_other_profiles(repository):
    commands = CHECK.plan(["static", "ci", "frontend"], [], [], "HEAD", repository)
    assert len(commands) == 10  # ci's static gate is static's; the history check is static's alone
    assert commands[0]["profile"] == "static"


def test_ci_execution_requires_linux(repository, monkeypatch):
    monkeypatch.setattr(CHECK.shutil, "which", lambda name: None)
    monkeypatch.setattr(CHECK.sys, "platform", "win32")
    with pytest.raises(SystemExit) as exc:
        CHECK.main(["--profile", "ci"])
    assert exc.value.code == 2
    assert not (repository / "tmp").exists()
    assert CHECK.main(["--profile", "ci", "--dry-run"]) == 0


def test_a_command_gets_its_env_and_the_private_scratch(repository):
    code = "import os, sys; print(os.environ['KD_HOST_JSON'], sys.argv[1])"
    report = CHECK.execute(
        [
            {
                "profile": "ci",
                "argv": [sys.executable, "-c", code, "{scratch}/overlay"],
                "env": {"KD_HOST_JSON": "host/host.example.json"},
            }
        ],
        ["ci"],
        repository,
        repository / "report",
    )
    host, overlay = Path(report["commands"][0]["log"]).read_text().split()
    assert host == "host/host.example.json"
    assert Path(overlay).parent.parent == repository / "report"
    assert "{scratch}" not in " ".join(report["commands"][0]["argv"])


def test_a_failure_names_its_command_before_the_log_tail(repository, capsys):
    argv = [sys.executable, "-c", "print('why it failed'); raise SystemExit(3)"]
    CHECK.execute(
        [{"profile": "ci", "argv": argv}], ["ci"], repository, repository / "report"
    )
    printed = capsys.readouterr().out
    assert f"Failed (3): {CHECK.shlex.join(argv)}" in printed
    assert printed.index("Failed (3)") < printed.index("why it failed")


def _ci_checkout(root):
    """A committed checkout with what the ci profile's first steps need."""
    repo = Path(__file__).resolve().parents[1]
    for rel in (
        "scripts/check.py",
        "scripts/check_changed.py",
        "common/kd/privacy_scan.py",
        "privacy-scan.json",
        ".gitignore",
    ):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(repo / rel, root / rel)
    (root / "pytest.ini").write_text(
        "[pytest]\ntestpaths = tests\naddopts = -q -p no:cacheprovider\n"
        "markers =\n    host: needs the real host\n",
        encoding="utf-8",
    )
    (root / "tests").mkdir()
    (root / "tests/test_failure.py").write_text(
        "def test_failure():\n    assert False\n", encoding="utf-8"
    )
    git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.test"]
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "checkout"], check=True)
    return git


def _run_ci(root):
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    proc = subprocess.run(
        [sys.executable, "scripts/check.py", "--profile", "ci", "--base", "HEAD"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    report = json.loads(next((root / "tmp/checks").glob("*/summary.json")).read_text())
    return proc, report


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or not shutil.which("git"),
    reason="the ci profile runs on Linux with git",
)
def test_ci_fails_on_a_failing_test_in_a_real_checkout(tmp_path):
    root = tmp_path / "checkout"
    _ci_checkout(root)
    proc, report = _run_ci(root)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert [c["returncode"] for c in report["commands"][:2]] == [0, 0]
    assert report["commands"][-1]["argv"][1:3] == ["-m", "pytest"]
    assert report["commands"][-1]["returncode"] == proc.returncode
    assert "Failed (1)" in proc.stdout and "test_failure" in proc.stdout
    # the run leaves nothing in the checkout but its logs under the ignored tmp/checks
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert status == ""


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or not shutil.which("git"),
    reason="the ci profile runs on Linux with git",
)
def test_ci_fails_on_a_token_in_a_tracked_file(tmp_path):
    root = tmp_path / "checkout"
    git = _ci_checkout(root)
    (root / "tests/test_failure.py").write_text("def test_ok():\n    pass\n")
    (root / "notes.txt").write_text("token " + "ghp_" + "A1b2" * 10 + "\n")
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "token"], check=True)
    proc, report = _run_ci(root)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert len(report["commands"]) == 1
    assert report["commands"][0]["argv"][1:3] == ["-I", "common/kd/privacy_scan.py"]


def test_dry_run_is_planned_and_creates_no_artifacts(repository, capsys):
    assert CHECK.main(["--profile", "full", "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "planned"
    assert not (repository / "tmp").exists()


def test_full_execution_requires_linux(repository, monkeypatch):
    monkeypatch.setattr(CHECK.sys, "platform", "win32")
    with pytest.raises(SystemExit) as exc:
        CHECK.main(["--profile", "full"])
    assert exc.value.code == 2
    assert not (repository / "tmp").exists()


def test_success_keeps_full_output_in_a_log_and_reports_counts(repository, capsys):
    command = {
        "profile": "focused",
        "argv": [sys.executable, "-c", "print('detail'); print('2 passed in 0.1s')"],
    }
    output = repository / "report"
    report = CHECK.execute([command], ["focused"], repository, output)
    assert report["status"] == "passed" and report["exit_code"] == 0
    assert report["source"] == {"revision": "abc", "dirty": True}
    assert "detail" in Path(report["commands"][0]["log"]).read_text()
    printed = capsys.readouterr().out
    assert "detail" not in printed and "2 passed" in printed
    assert json.loads((output / "summary.json").read_text())["status"] == "passed"


def test_default_scratch_stays_inside_the_report_and_is_removed(repository):
    code = "import os; print(os.environ['TMPDIR'])"
    report = CHECK.execute(
        [{"profile": "focused", "argv": [sys.executable, "-c", code]}],
        ["focused"],
        repository,
        repository / "report",
    )
    scratch = Path(Path(report["commands"][0]["log"]).read_text().strip())
    assert scratch.parent == repository / "report"
    assert not scratch.exists()


def test_failure_preserves_exit_code_stops_later_commands_and_bounds_output(
    repository, capsys
):
    commands = [
        {
            "profile": "focused",
            "argv": [sys.executable, "-c", "print('X' * 10000); raise SystemExit(7)"],
        },
        {
            "profile": "frontend",
            "argv": [sys.executable, "-c", "raise SystemExit('must not run')"],
        },
    ]
    report = CHECK.execute(
        commands, ["focused", "frontend"], repository, repository / "report"
    )
    assert report["status"] == "failed" and report["exit_code"] == 7
    assert report["planned_commands"] == 2 and len(report["commands"]) == 1
    assert Path(report["commands"][0]["log"]).stat().st_size > 10000
    assert len(capsys.readouterr().out) < 5000


def test_missing_tool_is_a_recorded_failure(repository):
    command = {"profile": "frontend", "argv": [str(repository / "missing-tool")]}
    report = CHECK.execute([command], ["frontend"], repository, repository / "report")
    assert (report["status"], report["exit_code"]) == ("failed", 127)


def test_interruption_is_recorded_as_failure(repository, monkeypatch):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(CHECK.subprocess, "run", interrupted)
    report = CHECK.execute(
        [{"profile": "focused", "argv": ["pytest"]}],
        ["focused"],
        repository,
        repository / "report",
    )
    assert (report["status"], report["exit_code"]) == ("failed", 130)
