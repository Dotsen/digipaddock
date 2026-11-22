"""prereq.sh: the host's packages before its first tree; vendors' repositories only named."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "common" / "install" / "prereq.sh"
BASH = next((c for c in (r"C:\Program Files\Git\bin\bash.exe", shutil.which("bash")) if c and Path(c).exists()), None)


def _text():
    return SCRIPT.read_text(encoding="utf-8")


@pytest.mark.skipif(BASH is None, reason="no bash")
@pytest.mark.parametrize("args", [[], ["upgrade"], ["check", "--pg"], ["check", "--hindsight", "x"]])
def test_prereq_usage(args):
    proc = subprocess.run([BASH, SCRIPT.as_posix(), *args], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 2 and "usage: prereq.sh" in proc.stderr


def test_prereq_installs_only_as_root():
    text = _text()
    assert '[ "$mode" = check ] || [ "$(id -u)" = 0 ] || { echo "root is required" >&2; exit 1; }' in text
    assert text.index('"$(id -u)" = 0') < text.index("apt-get install -y -q $missing")


def test_prereq_refreshes_the_package_lists_before_installing():
    """A fresh cloud image has stale or empty package lists — install would find nothing."""
    text = _text()
    assert text.index("apt-get update -q") < text.index("apt-get install -y -q $missing")
    assert text.index('if [ -n "$missing" ]; then') < text.index("apt-get update -q")  # only when installing


def test_prereq_install_leaves_the_vendors_part_to_the_owner():
    """After apt-get what is still missing is the vendors' (Node.js, PostgreSQL) — exit 3,
    the owner's step; 1 made bootstrap call it an installer failure."""
    text = _text()
    install = text[text.index('if [ "$mode" = install ]; then'):]
    assert "check || exit 3" in install


def test_prereq_never_adds_an_apt_source():
    text = _text()
    for banned in ("add-apt-repository", "sources.list.d", "apt-key", "gpg --dearmor"):
        assert banned not in text, banned
    # the vendors' commands are said, never run: every line naming them only builds a message
    for line in text.splitlines():
        if "nodesource" in line or "apt.postgresql.org.sh" in line:
            assert line.lstrip().startswith(("problems+=(", "local pgdg=", "#")), line


def test_prereq_takes_its_versions_from_the_catalog():
    text = _text()
    for key in ("node_major", "postgresql", "pgvector_min"):
        assert "version {})".format(key) in text or "version {}) &&".format(key) in text, key
    assert not re.search(r"setup_2[0-9]\.x|postgresql-1[0-9]\b", text)  # no pinned literal of its own
    assert 'dpkg --compare-versions "$have" ge "$pgvector_min"' in text


def test_prereq_packages_cover_what_the_installers_call():
    text = _text()
    packages = re.search(r'^PACKAGES="([^"]+)"', text, re.M).group(1).split()
    for need in ("git", "nginx", "certbot", "bubblewrap", "socat", "jq", "acl", "python3-venv", "curl", "sudo"):
        assert need in packages, need
