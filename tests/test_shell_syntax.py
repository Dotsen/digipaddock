"""bash -n for every shell script of the repository (syntax; behaviour is checked on servers)."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = sorted(
    list((ROOT / "common").glob("**/*.sh"))
    + list((ROOT / "agents").glob("**/*.sh"))
    + [ROOT / "common/bin/agent-launch", ROOT / "agents/sysadmin/bin/ssh-exec",
       ROOT / "agents/dev/bin/kd-forge", ROOT / "agents/dev/bin/kd-merge"]
)


def _bash():
    """A working bash: on Windows — Git Bash (System32\\bash.exe without a WSL distribution will not do)."""
    for candidate in (r"C:\Program Files\Git\bin\bash.exe", shutil.which("bash")):
        if candidate and Path(candidate).exists():
            try:
                ok = subprocess.run([candidate, "-c", "true"], capture_output=True, timeout=10).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                ok = False
            if ok:
                return candidate
    return None


BASH = _bash()


@pytest.mark.skipif(BASH is None, reason="нет рабочего bash")
@pytest.mark.parametrize(
    "script", [p for p in SCRIPTS if p.exists()], ids=lambda p: p.relative_to(ROOT).as_posix()
)
def test_bash_syntax(script):
    proc = subprocess.run([BASH, "-n", str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
