"""The monitor's release window around root installers (common/install/lib/expect.sh)."""

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "common" / "install" / "lib" / "expect.sh"


def run(tmp_path, uid="0", begin_rc=0, env_open=""):
    log = tmp_path / "log"
    script = f"""
. "{LIB.as_posix()}"
id() {{ echo {uid}; }}
kd_expect_monitor() {{ echo "monitor $1 $2" >> "{log.as_posix()}"; [ "$1" = end ] || return {begin_rc}; }}
kd_expect_run "lbl x" bash -c 'echo "child $KD_EXPECT_OPEN" >> "{log.as_posix()}"; exit 3'
echo "rc=$?" >> "{log.as_posix()}"
"""
    env = (
        dict(os.environ, KD_EXPECT_OPEN=env_open)
        if env_open
        else {k: v for k, v in os.environ.items() if k != "KD_EXPECT_OPEN"}
    )
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    return log.read_text(encoding="utf-8").splitlines(), p.stderr


@pytest.mark.skipif(
    os.name == "nt" or not os.path.exists("/usr/bin/python3"), reason="POSIX shell"
)
def test_window_wraps_the_command_and_keeps_its_exit_code(tmp_path):
    lines, _ = run(tmp_path)
    assert lines == ["monitor begin lbl x", "child 1", "monitor end lbl x", "rc=3"]


@pytest.mark.skipif(
    os.name == "nt" or not os.path.exists("/usr/bin/python3"), reason="POSIX shell"
)
def test_window_never_blocks_the_install(tmp_path):
    # no monitor or the snapshot was not taken: the installation goes on, end is not called
    lines, err = run(tmp_path, begin_rc=1)
    assert lines == ["monitor begin lbl x", "child 1", "rc=3"] and "did not open" in err
    (tmp_path / "log").unlink()
    # a nested installer — inside the outer window
    assert run(tmp_path, env_open="1")[0] == ["child 1", "rc=3"]
    (tmp_path / "log").unlink()
    assert run(tmp_path, uid="1000")[0] == ["child ", "rc=3"]  # not root — no window


def test_root_installers_open_the_window():
    for path, call in (
        ("common/install/new_agent.sh", 'kd_expect_run "new_agent.sh $1 $2'),
        ("common/install/render_host.sh", 'kd_expect_run "render_host.sh'),
        ("common/install/stage_setup.sh", 'kd_expect_run "stage_setup.sh'),
        # users, units, nginx and the web of the cabinet — also run by kd-release after every release
        ("common/install/cabinet_setup.sh", 'kd_expect_run "cabinet_setup.sh'),
    ):
        text = (ROOT / path).read_text(encoding="utf-8")
        assert call in text and '[ -z "${KD_EXPECT_OPEN-}" ]' in text, path
    new_agent = (ROOT / "common/install/new_agent.sh").read_text(encoding="utf-8")
    # --check and --plan change nothing — they exit before the window
    assert new_agent.index("from kd.install_plan import main") < new_agent.index(
        "kd_expect_run"
    )
    deploy = (ROOT / "agents/sysadmin/install/deploy_target.sh").read_text(
        encoding="utf-8"
    )
    begin, setup, end = (
        deploy.index(s)
        for s in (
            "if monitor_window begin",
            "target_setup.sh --gate",
            "  monitor_window end",
        )
    )
    assert begin < setup < end
    assert re.search(
        r"cleanup\(\) \{\n  if \[ \"\$expect_open\" = 1 \]; then monitor_window end",
        deploy,
    )
