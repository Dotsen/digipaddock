"""kd-secret: the owner puts a catalog secret on the host itself; status reports them all."""

import io
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from kd import host_secrets, install_catalog, naming

REPO = Path(__file__).resolve().parents[2]
HOST = naming.load_host(str(REPO / "host" / "host.example.json"))
CATALOG = install_catalog.secrets(host=HOST, prefix="digi")
POSIX = os.name == "posix" and shutil.which("bash") is not None


def _look(table):
    """look for the tests: (kind, user, group, mode) by catalog path from ``table``; missing otherwise."""
    return lambda path: table.get(path)


def test_put_secret_sends_the_shared_writer():
    text = (REPO / "common" / "install" / "put_secret.sh").read_text(encoding="utf-8")
    assert "secret_write.sh" in text and "<<'EOF'" not in text
    writer = (REPO / "common" / "install" / "secret_write.sh").read_text(encoding="utf-8")
    assert 'cd -P -- "$dir"' in writer and "real=$(/bin/pwd -P)" in writer and "mktemp ./.secret.XXXXXX" in writer


@pytest.mark.skipif(not POSIX, reason="symlinks and stat -c: Linux")
def test_secret_write_refuses_a_symlinked_dir(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    (tmp_path / "link").symlink_to(real)
    me = subprocess.run(["id", "-un"], capture_output=True, text=True, check=True).stdout.strip()
    group = subprocess.run(["id", "-gn"], capture_output=True, text=True, check=True).stdout.strip()
    proc = subprocess.run(
        ["bash", str(REPO / "common" / "install" / "secret_write.sh"), str(tmp_path / "link" / "s.env"),
         "{}:{}".format(me, group), "0600"],
        input="FORGE_TOKEN=x\n", capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 1 and "symlink" in proc.stderr
    assert list(real.iterdir()) == []


def test_status_reports_missing_and_wrong_mode():
    rows = {r["name"]: r for r in host_secrets.status(CATALOG, look=_look({
        "/etc/digipaddock/forge/digipaddock-dev.env": ("file", "root", "digi-forge", 0o640),
        "/etc/digipaddock/forge/digipaddock-merge.env": ("file", "root", "digi-forge", 0o644),
        "/etc/digipaddock/devcheck/digipaddock-ci.env": ("file", "root", "root", 0o600),
        "/etc/digipaddock/pult.env": ("link", "root", "root", 0o777),
    }))}
    assert rows["forge-dev"]["state"] == "ok"
    assert rows["forge-merge"]["state"] == "wrong" and "644" in rows["forge-merge"]["detail"]
    assert rows["review"]["state"] == "missing"
    assert rows["devcheck"]["state"] == "ok"
    assert rows["pult"]["state"] == "wrong" and rows["pult"]["wanted_by"] == []  # a symlink is never the secret
    assert rows["forge-dev"]["path"] == "/etc/digipaddock/forge/digipaddock-dev.env"


def test_status_reports_a_wrong_owner():
    rows = host_secrets.status(CATALOG[:1], look=_look({CATALOG[0].path: ("file", "root", "root", 0o640)}))
    assert rows[0]["state"] == "wrong" and "root:root" in rows[0]["detail"]


def test_status_json_is_one_line_per_secret():
    out = io.StringIO()
    code = host_secrets.main(["status", "--json"], catalog=CATALOG, is_root=True, look=_look({}), out=out,
                             err=io.StringIO())
    assert code == 0
    rows = json.loads(out.getvalue())
    assert [r["name"] for r in rows] == [s.name for s in CATALOG]
    assert {r["state"] for r in rows} == {"missing"}


def _put(argv, stdin, tmp_path, calls, out=None):
    err = io.StringIO()

    def run(cmd, value):
        calls.append((cmd, value))
        return 0

    code = host_secrets.main(argv, catalog=CATALOG, root=str(tmp_path), is_root=True, stdin=io.BytesIO(stdin),
                             run=run, look=_look({}), out=out or io.StringIO(), err=err)
    return code, err.getvalue()


def test_put_says_the_installers_check_with_its_arguments(tmp_path):
    """forge_setup.sh check needs the agent and its user — the hint gives them."""
    (tmp_path / "etc" / "digipaddock" / "forge").mkdir(parents=True)
    out = io.StringIO()
    assert _put(["put", "forge-dev"], b"FORGE_TOKEN=x\n", tmp_path, [], out=out)[0] == 0
    assert out.getvalue().rstrip().endswith("agents/dev/install/forge_setup.sh check dev digi-dev")
    (tmp_path / "etc" / "digipaddock" / "pult.env").parent.mkdir(parents=True, exist_ok=True)
    out = io.StringIO()  # kd-alert has no check of its own: no hint
    assert _put(["put", "pult"], b"PULT_BOT_TOKEN=x\nPULT_CHAT_ID=1\n", tmp_path, [], out=out)[0] == 0
    assert "check:" not in out.getvalue()


@pytest.mark.skipif(not POSIX, reason="stat -c and bash: Linux")
def test_secret_write_refuses_a_root_dir_others_can_write(tmp_path):
    """A root directory writable by its group or others — someone else could swap the file in it."""
    folder = tmp_path / "secrets"
    folder.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # the writer's own stat answers as for a root directory with mode 775; every other stat call is the real one
    (bin_dir / "stat").write_text('#!/bin/sh\ncase "$*" in\n  "-c %U .") echo root ;;\n  "-c %a .") echo 775 ;;\n'
                                  '  *) exec /usr/bin/stat "$@" ;;\nesac\n', encoding="utf-8")
    (bin_dir / "stat").chmod(0o755)
    proc = subprocess.run(
        ["bash", str(REPO / "common" / "install" / "secret_write.sh"), str(folder / "s.env"), "root:root", "0600"],
        input="FORGE_TOKEN=x\n", capture_output=True, text=True, timeout=30,
        env={**os.environ, "PATH": "{}:{}".format(bin_dir, os.environ.get("PATH", ""))},
    )
    assert proc.returncode == 1 and "is writable by group or others (775)" in proc.stderr
    assert list(folder.iterdir()) == []


def test_put_refuses_unknown_name_and_empty_stdin(tmp_path):
    calls = []
    assert _put(["put", "ghost"], b"FORGE_TOKEN=x\n", tmp_path, calls)[0] == 2
    assert _put(["put", "forge-dev"], b"", tmp_path, calls)[0] == 2
    code, err = _put(["put", "forge-dev"], b"just-a-token\n", tmp_path, calls)
    assert code == 2 and "FORGE_TOKEN=" in err  # the file's format, said plainly
    assert calls == []


def test_put_refuses_a_claude_token_systemd_would_read_otherwise(tmp_path):
    """An empty, quoted or extra-line token would reach sessions and jobs differently, or the journal."""
    (tmp_path / "etc" / "digipaddock").mkdir(parents=True)
    calls = []
    for value, said in ((b"CLAUDE_CODE_OAUTH_TOKEN=\n", "no value"),
                        (b'CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat01-q"\n', "quotes"),
                        (b"CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-q\nexport X=1\n", "only one")):
        code, err = _put(["put", "claude-token"], value, tmp_path, calls)
        assert code == 2 and said in err and "sk-ant-oat01-q" not in err
    assert calls == []


def test_put_restarts_the_running_units_that_read_the_secret(tmp_path):
    """Hindsight reads the token at start — a new one reaches it only by a restart."""
    (tmp_path / "etc" / "digipaddock").mkdir(parents=True)
    calls, out = [], io.StringIO()
    assert _put(["put", "claude-token"], b"CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-ok\n", tmp_path, calls, out=out)[0] == 0
    assert [c[0] for c in calls][1:] == [["systemctl", "try-restart", "hindsight-api.service", "hindsight-worker.service"]]
    assert calls[1][1] is None and "restarted (if running): hindsight-api.service hindsight-worker.service" in out.getvalue()
    calls = []
    assert _put(["put", "pult"], b"PULT_BOT_TOKEN=x\nPULT_CHAT_ID=1\n", tmp_path, calls)[0] == 0
    assert len(calls) == 1  # nothing to restart: kd-alert reads it on every run


def test_put_needs_the_directory_of_its_installer(tmp_path):
    calls = []
    code, err = _put(["put", "forge-dev"], b"FORGE_TOKEN=x\n", tmp_path, calls)
    assert code == 1 and "agents/dev/install/forge_setup.sh" in err and calls == []


def test_put_runs_the_shared_writer_with_the_catalog_owner_and_mode(tmp_path):
    (tmp_path / "etc" / "digipaddock" / "forge").mkdir(parents=True)
    calls = []
    code, _ = _put(["put", "forge-dev"], b"FORGE_TOKEN=x", tmp_path, calls)
    assert code == 0
    (cmd, value), = calls
    assert cmd[0] == "bash" and cmd[1].endswith("common/install/secret_write.sh")
    assert cmd[2:] == ["/etc/digipaddock/forge/digipaddock-dev.env", "root:digi-forge", "0640"]
    assert value == b"FORGE_TOKEN=x\n"  # one line ending, as an env file has


def test_stage_setup_links_kd_secret_by_the_hosts_name():
    text = (REPO / "common" / "install" / "stage_setup.sh").read_text(encoding="utf-8")
    assert "SECRET_LINK=/usr/local/bin/$(kd_cmd kd-secret)" in text
    assert 'ln -sfn "$REPO/common/bin/kd-secret" "$SECRET_LINK"' in text


def test_put_and_status_need_root(tmp_path):
    err = io.StringIO()
    assert host_secrets.main(["status"], catalog=CATALOG, root=str(tmp_path), is_root=False, look=_look({}),
                             out=io.StringIO(), err=err) == 1
    assert "root" in err.getvalue()


def test_put_llm_key_checks_its_line(tmp_path):
    """a provider key is one bare ANTHROPIC_AUTH_TOKEN= line, read by systemd and the root worker runner."""
    (tmp_path / "etc" / "digipaddock" / "llm").mkdir(parents=True)
    calls = []
    assert _put(["put", "llm-openrouter"], b"ANTHROPIC_AUTH_TOKEN=sk-or-v1-abc\n", tmp_path, calls)[0] == 0
    assert calls[0][0][2:] == ["/etc/digipaddock/llm/openrouter.env", "root:root", "0600"] and len(calls) == 1
    for value, said in ((b'ANTHROPIC_AUTH_TOKEN="x"\n', "quotes"), (b"ANTHROPIC_AUTH_TOKEN=x\nY=1\n", "one line"),
                        (b"ANTHROPIC_AUTH_TOKEN=\n", "no value")):
        code, err = _put(["put", "llm-fireworks"], value, tmp_path, [])
        assert code == 2 and said in err
