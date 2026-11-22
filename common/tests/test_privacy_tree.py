"""The repository's own privacy gate: the public tree names no real address, domain or secret."""

import shutil
import subprocess
from pathlib import Path

import pytest

from kd import privacy_scan

ROOT = Path(__file__).resolve().parents[2]


def _git_tree():
    if shutil.which("git") is None:
        return False
    probe = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--is-inside-work-tree"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return probe.returncode == 0 and probe.stdout.strip() == b"true"


@pytest.mark.skipif(not _git_tree(), reason="tracked files are listed by git")
def test_public_tree_has_no_private_literals():
    cfg = privacy_scan.load_config(ROOT / "privacy-scan.json")
    findings = privacy_scan.scan_tree(ROOT, cfg, tracked_only=True)
    assert findings == [], "\n".join(f"{f.path}:{f.line}: {f.kind}: {f.text}" for f in findings)
