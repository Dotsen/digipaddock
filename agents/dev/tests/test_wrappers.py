import os
import shutil
import subprocess
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "bin"


def test_kd_forge_refuses_merge_and_reaches_the_executor_only_through_its_channel():
    t = (BIN / "kd-forge").read_text(encoding="utf-8")
    assert "merge) echo" in t and "exit 2" in t
    assert "EXEC=/opt/digipaddock/agents/dev/bin/forge_exec.py" in t
    # the forge channel, not sudo; the channel's service runs the executor as the forge user.
    assert "CAP=/opt/digipaddock/common/bin/kd-cap\n" in t
    assert t.count('exec "$CAP" run forge "$EXEC" --agent "${KD_AGENT:-unknown}"') == 2
    assert "sudo" not in t and "kd_user" not in t
    assert "journalctl -u digipaddock-release on the factory host" in t


def test_kd_forge_passes_publish_to_the_executor():
    t = (BIN / "kd-forge").read_text(encoding="utf-8")
    assert "  prs|pr|comments|publish|check|status) ;;" in t and "publish <project> <N>" in t
    assert "status <project> <sha>" in t


def test_kd_forge_comment_goes_through_stdin():
    t = (BIN / "kd-forge").read_text(encoding="utf-8")
    assert '[ "$4" = --file ]' in t and 'comment "$2" "$3" <&3 3<&-' in t
    assert "--file" not in t.split('comment "$2" "$3"', 1)[1].split("\n", 1)[0]  # the executor gets stdin only


def test_kd_forge_comment_file_only_from_agent_tmp_checked_on_the_open_file():
    # kd-forge runs outside the sandbox (excludedCommands): unchecked, a symlink from tmp/ to
    # ~/.claude/.credentials.json, a bot token or the Hindsight key would leave in a PR. The name goes through
    # realpath -e, then the opened file itself through /proc/$$/fd/3: swapping the name between the check and the
    # open does not work
    t = (BIN / "kd-forge").read_text(encoding="utf-8")
    block = t.split("  comment)", 1)[1].split(";;", 1)[0]
    assert "TMP_DIR=/srv/digipaddock-agents/dev/work/tmp\n" in t
    assert 'p=$(realpath -e -- "$5"' in block and '[[ $p == "$TMP_DIR"/* ]]' in block
    assert '[[ $real == "$TMP_DIR"/* ]]' in block
    assert block.index("realpath -e") < block.index('exec 3< "$p"') < block.index('readlink -- "/proc/$$/fd/3"') \
        < block.index('exec "$CAP" run forge')
    assert '< "$5"' not in t


def test_kd_merge_passes_merge_first():
    t = (BIN / "kd-merge").read_text(encoding="utf-8")
    assert 'forge_exec.py --agent "${KD_AGENT:-unknown}" merge "$@"' in t and "40 символов" in t
    assert "exec /opt/digipaddock/common/bin/kd-cap run forge /opt/digipaddock/agents/dev/bin/forge_exec.py " in t
    assert "sudo" not in t and "kd_user" not in t


def test_executor_is_isolated_python_with_tight_umask():
    t = (BIN / "forge_exec.py").read_text(encoding="utf-8")
    assert t.startswith("#!/usr/bin/python3 -I") and "os.umask(0o077)" in t and 'sys.stdin.reconfigure(encoding="utf-8"' in t


# --- Beyond the brief: how the kd-forge wrapper behaves on refusals (it never gets to sudo) ---

def _bash():
    """A working bash: Git Bash on Windows (System32\\bash.exe without a WSL distribution is no good)."""
    for candidate in (r"C:\Program Files\Git\bin\bash.exe", shutil.which("bash")):
        if candidate and Path(candidate).exists():
            try:
                if subprocess.run([candidate, "-c", "true"], capture_output=True, timeout=10).returncode == 0:
                    return candidate
            except (OSError, subprocess.TimeoutExpired):
                pass
    return None


BASH = _bash()


def _run(*args):
    proc = subprocess.run([BASH, (BIN / "kd-forge").as_posix(), *args], capture_output=True, timeout=30)
    return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")


@pytest.mark.skipif(BASH is None, reason="нет рабочего bash")
@pytest.mark.parametrize("args,needle", [
    (["merge", "digipaddock", "12", "b" * 40], "kd-merge"),        # merge goes only through kd-merge (the owner's card)
    (["merge", "digipaddock", "12", "b" * 40, "--release"], "kd-merge"),
    ([], "usage"),
    (["wat", "digipaddock"], "usage"),
    (["selfcheck", "digipaddock"], "usage"),                        # selfcheck is the installer's step, not the agent's
    (["comment", "digipaddock", "12"], "usage"),                    # without --file
    (["comment", "digipaddock", "12", "--body", "x"], "usage"),
    (["comment", "digipaddock", "12", "--file", "/nonexistent/kd-x", "extra"], "usage"),
    (["comment", "digipaddock", "12", "--file", "/nonexistent/kd-x"], "not readable"),
])
def test_kd_forge_refusals_never_reach_the_channel(args, needle):
    rc, out, err = _run(*args)
    assert rc == 2 and needle in err and out == ""


@pytest.mark.skipif(BASH is None, reason="нет рабочего bash")
def test_kd_forge_help_mentions_full_sha():
    rc, out, _ = _run("--help")
    assert rc == 0 and "40 characters" in out and "kd-merge" in out


# The channel client has an absolute path: a refusal that reached it would fail with 126/127, not 2.


@pytest.mark.skipif(BASH is None, reason="нет рабочего bash")
def test_kd_forge_comment_file_outside_agent_tmp_never_reaches_the_channel(tmp_path):
    body = tmp_path / "reply.md"
    body.write_text("ответ", encoding="utf-8")
    rc, out, err = _run("comment", "digipaddock", "12", "--file", body.as_posix())
    assert rc == 2 and "must be in /srv/digipaddock-agents/dev/work/tmp/" in err and out == "", err


@pytest.mark.skipif(BASH is None or os.name == "nt", reason="симлинки: только POSIX")
def test_kd_forge_comment_symlink_to_secret_is_refused_before_reading(tmp_path):
    secret = tmp_path / "home" / ".claude" / ".credentials.json"
    secret.parent.mkdir(parents=True)
    secret.write_text('{"token": "x"}', encoding="utf-8")
    link = tmp_path / "kd-reply-12.md"
    link.symlink_to(secret)
    rc, out, err = _run("comment", "digipaddock", "12", "--file", str(link))
    assert rc == 2 and "must be in /srv/digipaddock-agents/dev/work/tmp/" in err and out == "", err
    assert "token" not in err
