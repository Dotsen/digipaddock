"""the stores of the owner's decisions after kd-admin-approval — the cabinet writes the grants and the BOSS
decisions, the sysadmin's module makes its plan and grant directories, and the approve user is retired."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from kd import boss_promote, delegation

ROOT = Path(__file__).resolve().parents[2]
CABINET = (ROOT / "common/install/cabinet_setup.sh").read_text(encoding="utf-8")
EXEC_SETUP = (ROOT / "agents/sysadmin/install/exec_setup.sh").read_text(encoding="utf-8")
BASH = next((c for c in (r"C:\Program Files\Git\bin\bash.exe", shutil.which("bash")) if c and Path(c).exists()), None)


def _mode(text, mode):
    cases = text[text.rindex("\ncase $mode in"):]
    return re.search(rf"^  {mode}\)(.*?);;", cases, re.M | re.S).group(1)


def _function(text, name):
    start = text.index(f"\n{name}() {{")
    return text[start:text.index("\n}\n", start)]


def test_the_approval_service_is_gone():
    for relative in ("common/bin/kd-admin-approval", "common/kd/admin_web.py", "common/systemd/digipaddock-admin-approval.service",
                     "common/install/admin_approval_setup.sh", "common/tests/test_admin_web.py"):
        assert not (ROOT / relative).exists(), relative
    assert "admin_approval_setup.sh" not in EXEC_SETUP and "APPROVAL" not in EXEC_SETUP


@pytest.mark.skipif(BASH is None, reason="no working bash")
@pytest.mark.parametrize("args", [[], ["bogus", "sysadmin", "digi-sysadmin"], ["install", "sysadmin"],
                                  ["check", "sysadmin", "Kit Sys"], ["install", "sysadmin", "../x"]])
def test_exec_setup_usage_is_refused_before_anything(args):
    proc = subprocess.run([BASH, str(ROOT / "agents/sysadmin/install/exec_setup.sh"), *args],
                          capture_output=True, text=True)
    assert proc.returncode == 2 and "usage:" in proc.stderr


def test_exec_setup_makes_the_plan_and_grant_directories_for_the_cabinet():
    # the agent writes its plans and only the cabinet reads them (the cabinet user's own group); only the cabinet
    # writes grants and the exec user reads them by the setgid group
    install = _function(EXEC_SETUP, "install_all")
    assert 'cabinet=$(kd_user cabinet)' in EXEC_SETUP
    assert 'install -d -o "$user" -g "$cabinet" -m 2750 "$PROPOSALS"' in install
    assert 'install -d -o "$cabinet" -g "$exec_user" -m 2750 "$GRANTS"' in install
    check = _function(EXEC_SETUP, "check")
    assert 'want "$PROPOSALS" "$user:$cabinet" 2750' in check and 'want "$GRANTS" "$cabinet:$exec_user" 2750' in check
    for line in ('! runuser -u "$user" -- test -w "$GRANTS"', '! runuser -u "$exec_user" -- test -w "$GRANTS"',
                 '! runuser -u "$exec_user" -- test -r "$PROPOSALS"'):
        assert line in check, line
    assert _mode(EXEC_SETUP, "install").split() == ["install_all", "check"]


def test_cabinet_setup_gives_the_cabinet_the_decision_stores():
    stores = _function(CABINET, "stores")
    for line in ('install -d -o "$base" -g "$base" -m 0750 "$PROMOTIONS/proposals"',
                 'setfacl -m "u:$cabinet:rx" -m "d:u:$cabinet:r" "$PROMOTIONS/proposals"',
                 'install -d -o "$cabinet" -g "$cabinet" -m 0700 "$PROMOTIONS/decisions"',
                 'setfacl -m "u:$base:x" "$PROMOTIONS/decisions"',
                 'install -d -o "$cabinet" -g "$base" -m 2750 "$PROMOTIONS/grants"',
                 'setfacl -m "u:$cabinet:rx" -m "d:u:$cabinet:rw" "$BOARD_LOG"',
                 'setfacl -m "u:$cabinet:rw" "$BOARD_LOG/boss.jsonl"'):
        assert line in stores, line
    # the decisions directory is made before the base user's traverse right on it
    assert stores.index('-m 0700 "$PROMOTIONS/decisions"') < stores.index('setfacl -m "u:$base:x" "$PROMOTIONS/decisions"')
    check = _function(CABINET, "check")
    for line in ('want "$PROMOTIONS/decisions" "$cabinet:$cabinet" 710', 'want "$PROMOTIONS/grants" "$cabinet:$base" 2750',
                 'runuser -u "$cabinet" -- test -w "$BOARD_LOG/boss.jsonl"', '! runuser -u "$cabinet" -- test -w "$BOARD_LOG"',
                 'default:user:$cabinet:rw-', '! runuser -u "$base" -- test -r "$PROMOTIONS/decisions"',
                 '! runuser -u "$base" -- test -w "$PROMOTIONS/grants"'):
        assert line in check, line
    # the stores the cabinet writes are the ones its unit may write
    unit = (ROOT / "common/systemd/digipaddock-cabinet.service").read_text(encoding="utf-8")
    for store in (boss_promote.DECISIONS, delegation.GRANTS):
        assert store in unit, store


def test_cabinet_setup_no_longer_knows_the_approve_user():
    # The approval pages moved into the cabinet; the retirement step did its work
    assert "kd_user approve" not in CABINET and "kd-admin-approval" not in CABINET
