"""The sysadmin's executor wrappers reach their executors through the exec channel."""

from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "bin"
CAP = "/opt/digipaddock/common/bin/kd-cap"


@pytest.mark.parametrize(
    "wrapper, executor",
    [
        ("ssh-exec", "ssh_exec.py"),
        ("kd-admin-run", "admin_run.py"),
        ("kd-admin-critical", "admin_critical.py"),
    ],
)
def test_wrapper_runs_its_executor_through_the_exec_channel(wrapper, executor):
    text = (BIN / wrapper).read_text(encoding="utf-8")
    line = f'exec {CAP} run exec /opt/digipaddock/agents/sysadmin/bin/{executor} --agent "${{KD_AGENT:-unknown}}" "$@"\n'
    assert text.endswith(line)
    assert "sudo" not in text and "kd_user" not in text and "naming.sh" not in text
