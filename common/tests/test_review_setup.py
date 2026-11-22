"""review_setup.sh and kd-review's units — the script's contract, read from its text and a bash run."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "agents" / "dev" / "install" / "review_setup.sh"
TEXT = SCRIPT.read_text(encoding="utf-8")
SYSTEMD = ROOT / "common" / "systemd"
BASH = next((c for c in (r"C:\Program Files\Git\bin\bash.exe", shutil.which("bash")) if c and Path(c).exists()), None)


def _mode(mode: str) -> str:
    cases = TEXT[TEXT.rindex("\ncase $mode in"):]
    return re.search(rf"^  {mode}\)(.*?);;", cases, re.M | re.S).group(1)


def test_render_host_knows_kd_review():
    render = (ROOT / "common" / "install" / "render_host.sh").read_text(encoding="utf-8")
    present = re.search(r'^PRESENT="([^"]*)"', render, re.M).group(1).split()
    assert "common/systemd/digipaddock-review.socket" in present and "common/systemd/digipaddock-review.service" in present


def test_every_release_restarts_a_running_kd_review():
    """Round-2 review: kd-review is long-running — the cabinet's install after each release (kd-release)
    restarts it, or the old code keeps posting Approves the new kd-merge does not count."""
    setup = (ROOT / "common" / "install" / "cabinet_setup.sh").read_text(encoding="utf-8")
    start = setup.index("\ninstall_all() {")
    install = setup[start:setup.index("\n}\n", start)]
    # a host without kd-review (review_setup.sh never ran) is no failure: set -e would stop the install
    review = "if systemctl is-active --quiet digipaddock-review.service; then systemctl restart digipaddock-review.service; fi"
    assert review in install
    # before the cabinet's restart: a new cabinet never hands a signature to the old approver (round 3)
    assert install.index(review) < install.index("systemctl restart digipaddock-cabinet.service")


def test_units_render_tokens_and_reach_only_the_cabinet():
    socket = (SYSTEMD / "digipaddock-review.socket").read_text(encoding="utf-8")
    for line in ("ListenStream=/run/digipaddock-review/review.sock", "SocketUser=root", "SocketGroup=@cabinet@",
                 "SocketMode=0660", "DirectoryMode=0755"):
        assert line in socket.splitlines(), line
    service = (SYSTEMD / "digipaddock-review.service").read_text(encoding="utf-8")
    for line in ("User=@review@", "Requires=digipaddock-review.socket", "OnFailure=digipaddock-alert@digipaddock-review.service", "UMask=0077",
                 "NoNewPrivileges=yes", "ProtectSystem=strict",
                 "ExecStart=/var/lib/digipaddock/venv/cabinet/bin/python -I "
                 "/opt/digipaddock/agents/dev/bin/kd-review serve"):
        assert line in service.splitlines(), line
    assert "ReadWritePaths" not in service  # the registry and the token are read only
    assert "IPAddressDeny" not in service  # it talks to Forgejo
    # final review: spent nonces outlive a restart — its own state directory, nothing else writable
    for line in ("StateDirectory=digipaddock/review", "StateDirectoryMode=0700"):
        assert line in service.splitlines(), line
    cabinet = (SYSTEMD / "digipaddock-cabinet.service").read_text(encoding="utf-8")
    assert "ReadWritePaths=-/run/digipaddock-review" in cabinet.splitlines()  # the cabinet connects to kd-review's socket


def test_review_setup_install_and_check():
    assert 'review=$(kd_user review)' in TEXT and 'cabinet=$(kd_user cabinet)' in TEXT
    assert re.search(r'useradd --system [^\n]*--shell /usr/sbin/nologin "\$review"', TEXT)
    install = _mode("install")
    assert "install_all" in install and "check" in install
    for path in ("$TOKEN_DIR", "$SIGNERS"):
        assert f'install -d -o "$review" -g "$review" -m 0700 "{path}"' in TEXT, path
    assert 'kd_install_rendered "$REPO/common/systemd/$unit" "$UNITS/$unit" 0644' in TEXT
    assert "systemctl enable --now digipaddock-review.socket" in TEXT
    assert "FORGE_TOKEN" not in TEXT.split("install_all()", 1)[1].split("\n}", 1)[0]  # the token is the owner's
    for needle in ('want "$TOKEN" "$review:$review" 600', 'want "$TOKEN_DIR" "$review:$review" 700',
                   'want "$SIGNERS" "$review:$review" 700', '-I -c "import webauthn"', "kd-review check",
                   "systemctl is-enabled --quiet digipaddock-review.socket"):
        assert needle in TEXT, needle
    disable = _mode("disable")
    # the service has no [Install]: it is stopped, only the socket is disabled
    assert "systemctl disable --now digipaddock-review.socket" in disable and "systemctl stop digipaddock-review.service" in disable
    assert "disable --now digipaddock-review.socket digipaddock-review.service" not in disable
    assert "userdel" not in TEXT and "rm " not in disable
    assert "kd-review signers add <user> --credential" in TEXT  # root adds one chosen key at a time


def test_review_setup_install_runs_under_the_monitors_expectation():
    # final review: the monitor takes what install changes into its baseline, as cabinet_setup.sh does
    head = TEXT.split("\nproblems=()", 1)[0]
    assert '[ "$mode" = install ] && [ -z "${KD_EXPECT_OPEN-}" ]' in head
    assert '. "$REPO/common/install/lib/expect.sh"' in head
    assert re.search(r'kd_expect_run "review_setup\.sh \$\* \(REVISION [^\n]*\\\n\s*bash "\$REPO/agents/dev/install/'
                     r'review_setup\.sh" "\$@"', head)


@pytest.mark.skipif(BASH is None, reason="no working bash")
@pytest.mark.parametrize("args", [[], ["bogus"], ["install", "x"], ["check", "--force"], ["check", "installed", "x"],
                                  ["install", "installed"], ["disable", "installed"]])
def test_review_setup_usage(args):
    proc = subprocess.run([BASH, SCRIPT.as_posix(), *args], capture_output=True, timeout=30)
    assert proc.returncode == 2 and b"usage" in proc.stderr


@pytest.mark.skipif(BASH is None, reason="no working bash")
def test_check_installed_leaves_the_owners_parts_out():
    """bootstrap's components phase checks what install made; the token and a signer
    are the owner's, checked with the logins."""
    assert re.search(r'^  check\) check "\$scope" ;;$', TEXT, re.M)
    proc = subprocess.run([BASH, SCRIPT.as_posix(), "check", "installed"], capture_output=True, timeout=30)
    assert proc.returncode != 2 and b"usage" not in proc.stderr  # a scope, not a usage error
