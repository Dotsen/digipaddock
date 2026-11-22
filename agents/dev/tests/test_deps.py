"""kd-deps install — a project's locked dependencies in a card's working copy, with no package scripts."""

import io
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from dev_agent import deps

BIN = Path(__file__).resolve().parents[1] / "bin"
COMPOSER = [
    "composer",
    "install",
    "--no-scripts",
    "--no-plugins",
    "--no-interaction",
    "--no-progress",
    "--prefer-dist",
]
NPM = ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"]


@pytest.fixture
def work(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    return root


def tree(
    work,
    name="Shop.KD-12",
    *,
    locks=("composer.lock",),
    ignore="vendor/\nnode_modules/\n",
):
    path = work / name
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    for lock in locks:
        (path / lock).write_text("{}", encoding="utf-8")
    if ignore is not None:
        (path / ".gitignore").write_text(ignore, encoding="utf-8")
    return path


def test_a_composer_lock_plans_an_install_without_scripts_or_plugins(work):
    steps = deps.plan(str(tree(work)), str(work))
    assert [step.argv for step in steps] == [COMPOSER]
    env = steps[0].env
    assert (
        Path(env["COMPOSER_HOME"]).parent
        == Path(env["COMPOSER_CACHE_DIR"]).parent
        == work / ".cache" / "composer"
    )


def test_a_package_lock_plans_npm_ci_without_scripts(work):
    steps = deps.plan(str(tree(work, locks=("package-lock.json",))), str(work))
    assert [step.argv for step in steps] == [NPM]
    assert Path(steps[0].env["npm_config_cache"]) == work / ".cache" / "npm"


def test_both_locks_plan_both_installs_in_a_fixed_order(work):
    steps = deps.plan(
        str(tree(work, locks=("package-lock.json", "composer.lock"))), str(work)
    )
    assert [step.argv for step in steps] == [COMPOSER, NPM]


def test_the_session_proxy_reaches_the_package_manager(work, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:3140")
    assert (
        deps.plan(str(tree(work)), str(work))[0].env["HTTPS_PROXY"]
        == "http://127.0.0.1:3140"
    )


def test_a_tree_without_a_lock_file_is_refused(work):
    with pytest.raises(deps.Refused, match="composer.lock.*package-lock.json"):
        deps.plan(str(tree(work, locks=())), str(work))


def test_only_a_working_copy_right_under_work_installs(work, tmp_path):
    outside = tree(tmp_path, "elsewhere")
    with pytest.raises(deps.Refused, match="work/<имя>"):
        deps.plan(str(outside), str(work))
    nested = tree(work, "Shop")
    (nested / "sub").mkdir()
    (nested / "sub" / "composer.lock").write_text("{}", encoding="utf-8")
    with pytest.raises(deps.Refused, match="work/<имя>"):
        deps.plan(str(nested / "sub"), str(work))
    plain = work / "notes"
    plain.mkdir()
    (plain / "composer.lock").write_text("{}", encoding="utf-8")
    with pytest.raises(deps.Refused, match="git"):
        deps.plan(str(plain), str(work))


def test_a_dependency_folder_git_does_not_ignore_is_refused(work):
    with pytest.raises(deps.Refused, match=r"vendor.*\.gitignore"):
        deps.plan(str(tree(work, ignore=None)), str(work))
    with pytest.raises(deps.Refused, match=r"node_modules.*\.gitignore"):
        deps.plan(
            str(
                tree(
                    work, "Web.KD-13", locks=("package-lock.json",), ignore="vendor/\n"
                )
            ),
            str(work),
        )


def test_a_lock_file_that_is_a_link_is_refused(work, tmp_path):
    path = tree(work, locks=())
    target = tmp_path / "foreign.lock"
    target.write_text("{}", encoding="utf-8")
    try:
        os.symlink(str(target), str(path / "composer.lock"))
    except (OSError, NotImplementedError):
        pytest.skip("no symlinks for this account")
    with pytest.raises(deps.Refused, match="обычный файл"):
        deps.plan(str(path), str(work))


def run_main(argv, cwd, work, codes=()):
    calls, results = [], list(codes)

    def run(command, cwd=None, env=None):
        calls.append((command, cwd, env))
        return SimpleNamespace(returncode=results.pop(0) if results else 0)

    out, err = io.StringIO(), io.StringIO()
    code = deps.main(argv, run=run, cwd=str(cwd), work=str(work), out=out, err=err)
    return code, calls, out.getvalue(), err.getvalue()


def test_install_runs_exactly_the_planned_commands_in_the_tree(work):
    path = tree(work, locks=("composer.lock", "package-lock.json"))
    code, calls, out, err = run_main(["install"], path, work)
    assert (code, err) == (0, "")
    assert [(command, cwd) for command, cwd, _ in calls] == [
        (COMPOSER, str(path)),
        (NPM, str(path)),
    ]
    assert "composer.lock" in out and "package-lock.json" in out
    assert (work / ".cache" / "composer").is_dir() and (
        work / ".cache" / "npm"
    ).is_dir()


def test_the_first_failed_install_stops_with_its_code(work):
    path = tree(work, locks=("composer.lock", "package-lock.json"))
    code, calls, _, err = run_main(["install"], path, work, codes=[2])
    assert code == 2 and len(calls) == 1 and "composer" in err


@pytest.mark.parametrize(
    "argv", [[], ["update"], ["install", "--with-scripts"], ["install", "extra"]]
)
def test_there_is_one_form_only(work, argv):
    code, calls, _, err = run_main(argv, tree(work), work)
    assert code == 2 and calls == [] and "kd-deps install" in err


def test_a_refusal_runs_nothing(work):
    code, calls, _, err = run_main(["install"], tree(work, ignore=None), work)
    assert code == 2 and calls == [] and ".gitignore" in err


def test_the_wrapper_is_the_isolated_entry_of_the_module():
    text = (BIN / "kd-deps").read_text(encoding="utf-8")
    assert (
        text.startswith("#!/usr/bin/python3 -I")
        and "from dev_agent.deps import main" in text
    )


# --- review findings ---------------------------------------------------------------------------------------------
def test_only_the_tools_own_caches_are_created(work, monkeypatch):
    stray = work / ".cache" / "stray"
    monkeypatch.setenv("SOMETHING_ELSE", str(stray))
    code, _, _, _ = run_main(["install"], tree(work), work)
    assert (
        code == 0
        and (work / ".cache" / "composer" / "cache").is_dir()
        and not stray.exists()
    )


def test_composer_packages_land_in_vendor_whatever_the_project_configures(
    work, monkeypatch
):
    monkeypatch.setenv("COMPOSER_VENDOR_DIR", "lib")
    assert (
        deps.plan(str(tree(work)), str(work))[0].env["COMPOSER_VENDOR_DIR"] == "vendor"
    )


def test_a_dependency_folder_already_tracked_is_refused(work):
    path = tree(work)
    (path / "vendor").mkdir()
    (path / "vendor" / "autoload.php").write_text("<?php", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(path), "add", "-f", "vendor/autoload.php"], check=True
    )
    with pytest.raises(deps.Refused, match=r"vendor"):
        deps.plan(str(path), str(work))
