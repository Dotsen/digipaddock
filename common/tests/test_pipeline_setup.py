"""pipeline_setup.sh — the script's contract, read from its text and a bash run (as session_host_setup's)."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

COMMON = Path(__file__).resolve().parents[1]
SCRIPT = COMMON / "install" / "pipeline_setup.sh"
TEXT = SCRIPT.read_text(encoding="utf-8")
BASH = next((c for c in (r"C:\Program Files\Git\bin\bash.exe", shutil.which("bash")) if c and Path(c).exists()), None)


def _mode(mode: str) -> str:
    cases = TEXT[TEXT.rindex("\ncase $mode in"):]
    return re.search(rf"^  {mode}\)(.*?);;", cases, re.M | re.S).group(1)


@pytest.mark.skipif(BASH is None, reason="no working bash")
@pytest.mark.parametrize("args", [[], ["bogus"], ["enable", "now"], ["check", "dev"], ["observe"]])
def test_setup_usage_and_root_only(args):
    proc = subprocess.run([BASH, str(SCRIPT), *args], capture_output=True, text=True)
    assert proc.returncode == 2 and "usage:" in proc.stderr
    assert TEXT.index('"$(id -u)" = 0') < TEXT.index("pipeline write")


def test_check_asks_the_cabinet_kd_work_and_every_hosted_agent():
    body = TEXT[TEXT.index("check() {"):TEXT.index("report() {")]
    assert "kd-cabinet\" pipeline status" in body and "'protocol: 1'" in body
    assert "grep -q 'kd-work finish KD-' \"$REPO/common/bin/kd-work\"" in body
    assert "kd-work finish KD-" in (COMMON / "bin" / "kd-work").read_text(encoding="utf-8")
    assert 'for agent in $agents' in body and 'new_agent.sh" --check "$agent"' in body
    assert '"$agent: ' in body  # the reason names the agent


def test_enable_writes_enforce_only_after_a_clean_check():
    enable = _mode("enable").strip().splitlines()
    assert [line.strip() for line in enable[:3]] == ["check", "report", "pipeline write enforce"]
    assert "set -euo pipefail" in TEXT  # a failed report stops the script before the write
    assert "pipeline write observe" in _mode("disable") and "check" not in _mode("disable")
    assert "kd-cabinet\" pipeline status" in _mode("status")


def test_the_writer_is_the_pipeline_config_module(tmp_path):
    from kd import pipeline_config

    etc = tmp_path / "pipeline.json"
    assert pipeline_config.main(["write", "enforce"], etc=str(etc)) == 0
    assert pipeline_config.load(str(etc)).enforcing
    assert pipeline_config.main(["write", "observe"], etc=str(etc)) == 0
    assert pipeline_config.load(str(etc)).mode == "observe"
    assert pipeline_config.main(["write", "turbo"], etc=str(etc)) == 2
    assert "from kd.pipeline_config import main" in TEXT


def test_check_refuses_when_the_hosted_agents_cannot_be_read():
    body = TEXT[TEXT.index("check() {"):TEXT.index("report() {")]
    assert "if ! agents=$(hosted); then" in body and "the host's agent list is not readable" in body
    assert 'for agent in $agents' in body
