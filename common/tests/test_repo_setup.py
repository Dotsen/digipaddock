"""Module setup of repo.read: the script's contract, read from its text and a bash run."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

INSTALL = Path(__file__).resolve().parents[1] / "install"
SCRIPT = INSTALL / "repo_setup.sh"
TEXT = SCRIPT.read_text(encoding="utf-8")
BASH = next((c for c in (r"C:\Program Files\Git\bin\bash.exe", shutil.which("bash")) if c and Path(c).exists()), None)


def _mode(text: str, mode: str) -> str:
    cases = text[text.rindex("\ncase $mode in"):]
    return re.search(rf"^  {mode}\)(.*?);;", cases, re.M | re.S).group(1)


@pytest.mark.skipif(BASH is None, reason="no working bash")
@pytest.mark.parametrize("args", [[], ["bogus", "analyst", "digi-analyst"], ["install", "../x", "digi-analyst"],
                                  ["check", "analyst", "Kit Analyst"]])
def test_usage_is_refused_before_anything(args):
    proc = subprocess.run([BASH, str(SCRIPT), *args], capture_output=True, text=True)
    assert proc.returncode == 2 and "usage:" in proc.stderr


def test_install_verifies_with_its_own_check():
    assert "check" in [line.strip() for line in _mode(TEXT, "install").splitlines()]
    assert _mode(TEXT, "check").strip() == "check"
    assert "\ncd /\n" in TEXT and TEXT.index("\ncd /\n") < TEXT.index("runuser -u")


def test_the_key_belongs_to_the_repo_user_and_the_agent_never_appears_in_sudo_or_acl():
    assert "repo=$(kd_user repo)" in TEXT
    assert "useradd --system" in TEXT and "/usr/sbin/nologin" in TEXT
    assert "ssh-keygen -q -t ed25519" in TEXT and "repo_sync_ed25519" in TEXT
    assert 'install -d -o "$repo" -g "$repo" -m 0750 "$CHECKOUTS"' in TEXT
    assert "setfacl" not in TEXT and "sudoers" not in TEXT  # the agent's grant is the plan's ACL step, not this script's


def test_preflight_requires_the_branch_the_sync_will_clone():
    # Final review: ls-remote exits 0 for a reachable repository without refs/heads/main; the sync
    # would then fail every ten minutes. --exit-code makes the missing branch a preflight error.
    assert "git ls-remote --exit-code" in TEXT
    assert "refs/heads/main" in TEXT and "branch main" in TEXT
    assert "kd_install_rendered" in TEXT and "digipaddock-repo-sync.timer" in TEXT and "digipaddock-repo-sync.service" in TEXT
    assert "systemctl start digipaddock-repo-sync.service" in TEXT
    assert "systemctl disable --now digipaddock-repo-sync.timer" in _mode(TEXT, "disable")


def test_a_key_the_owner_has_not_added_is_the_owners_step():
    """The copies' deploy key is the owner's to add in the forge — exit 3, which
    new_agent.sh and bootstrap pass on as the owner's step with the key printed above."""
    preflight = TEXT.split("\npreflight() {\n", 1)[1].split("\n}\n", 1)[0]
    assert '[ "$missing" = 0 ] || exit 3' in preflight and 'cat "$KEY.pub" >&2' in preflight


def test_render_host_re_renders_the_sync_units_when_present():
    text = (INSTALL / "render_host.sh").read_text(encoding="utf-8")
    present = re.search(r'^PRESENT="([^"]*)"', text, re.M).group(1).split()
    assert "common/systemd/digipaddock-repo-sync.service" in present and "common/systemd/digipaddock-repo-sync.timer" in present
