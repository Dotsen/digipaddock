"""KD-9474: the one Claude Code every launch with a secret runs is root's, outside every agent's home."""

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from kd import claude_code, install_catalog, secret_fd

ROOT = Path(__file__).resolve().parents[2]
SETUP = (ROOT / "common/install/claude_setup.sh").read_text(encoding="utf-8")
BIN = claude_code.BINARY


def _node(kind, uid=0, mode=0o755):
    bits = {"dir": stat.S_IFDIR, "file": stat.S_IFREG, "link": stat.S_IFLNK}[kind]
    return os.stat_result((bits | mode, 1, 1, 1, uid, 0, 0, 0, 0, 0))


def _host(**changes):
    """An lstat over the installed chain; ``changes`` maps a path to a node, or None for a missing one."""
    nodes = {
        "/": _node("dir"),
        "/opt": _node("dir"),
        claude_code.DIR: _node("dir"),
        BIN: _node("file"),
    }
    nodes.update(changes)

    def lstat(path):
        node = nodes.get(path)
        if node is None:
            raise FileNotFoundError(2, "No such file or directory", path)
        return node

    return lstat


def test_the_binary_is_root_owned_outside_every_home():
    assert (
        BIN == "/opt/digipaddock-claude/claude"
        and claude_code.DIR == "/opt/digipaddock-claude"
    )
    assert "/home" not in BIN and ".local" not in BIN
    assert secret_fd.root_program(BIN, lstat=_host()) is None


@pytest.mark.parametrize(
    "lstat, why",
    [
        pytest.param(_host(**{BIN: None}), f"{BIN} is missing", id="missing"),
        pytest.param(
            _host(**{BIN: _node("file", uid=1001)}),
            f"{BIN} is not root's",
            id="agent-owned",
        ),
        pytest.param(
            _host(**{BIN: _node("file", mode=0o775)}),
            "writable by others than root",
            id="group-writable",
        ),
        pytest.param(
            _host(**{BIN: _node("file", mode=0o757)}),
            "writable by others than root",
            id="world-writable",
        ),
        pytest.param(
            _host(**{BIN: _node("link", mode=0o777)}), f"{BIN} is a symlink", id="link"
        ),
        pytest.param(
            _host(**{BIN: _node("dir")}), f"{BIN} is not a regular file", id="directory"
        ),
        pytest.param(
            _host(**{BIN: _node("file", mode=0o644)}),
            f"{BIN} is not executable",
            id="not-executable",
        ),
        pytest.param(
            _host(**{claude_code.DIR: _node("dir", uid=1001)}),
            f"{claude_code.DIR} is not root's",
            id="dir-agent-owned",
        ),
        pytest.param(
            _host(**{claude_code.DIR: _node("dir", mode=0o777)}),
            "writable by others than root",
            id="dir-world-writable",
        ),
        pytest.param(
            _host(**{claude_code.DIR: _node("link", mode=0o777)}),
            f"{claude_code.DIR} is a symlink",
            id="dir-link",
        ),
        pytest.param(
            _host(**{"/opt": _node("dir", mode=0o1777)}),
            "/opt is writable by others than root",
            id="parent-world-writable",
        ),
        pytest.param(
            _host(**{"/opt": _node("file")}),
            "/opt is not a directory",
            id="parent-not-a-directory",
        ),
    ],
)
def test_a_program_another_user_could_replace_gets_no_secret(lstat, why):
    assert why in (secret_fd.root_program(BIN, lstat=lstat) or "")


@pytest.mark.parametrize(
    "path",
    [
        "claude",
        "./claude",
        "/opt/digipaddock-claude/../claude",
        "/opt//digipaddock-claude/claude",
        "/opt/digipaddock-claude/claude/",
        "/home/digi-dev/.local/bin/../../../opt/digipaddock-claude/claude",
        "",
        None,
    ],
)
def test_only_a_plain_absolute_path_is_a_program(path):
    # never a lookup on PATH, never a path whose meaning depends on a link or a ..
    assert "not a plain absolute path" in secret_fd.root_program(path, lstat=_host())


def test_the_old_home_copy_is_refused():
    home = "/home/digi-dev/.local/bin/claude"
    lstat = _host(
        **{"/home": _node("dir"), "/home/digi-dev": _node("dir", uid=1001, mode=0o750)}
    )
    assert secret_fd.root_program(home, lstat=lstat) == "/home/digi-dev is not root's"


PINNED = {
    "claude_code": "2.1.289",
    "claude_code_sha256_amd64": "a" * 64,
    "claude_code_sha256_arm64": "b" * 64,
}


def test_verify_accepts_the_pinned_root_binary():
    assert (
        claude_code.problems(
            lstat=_host(), digest=lambda path: "a" * 64, machine="x86_64", pinned=PINNED
        )
        == []
    )
    assert (
        claude_code.problems(
            lstat=_host(),
            digest=lambda path: "b" * 64,
            machine="aarch64",
            pinned=PINNED,
        )
        == []
    )


def test_verify_refuses_another_build_than_the_pinned_one():
    found = claude_code.problems(
        lstat=_host(), digest=lambda path: "c" * 64, machine="x86_64", pinned=PINNED
    )
    assert found == [
        f"{BIN} is not Claude Code 2.1.289 of common/install/versions.json (sha256);"
        " claude_setup.sh install"
    ]


def test_verify_refuses_a_binary_not_root_owned_without_reading_it():
    read = []
    found = claude_code.problems(
        lstat=_host(**{BIN: _node("file", uid=1001)}),
        digest=read.append,
        machine="x86_64",
        pinned=PINNED,
    )
    assert found == [f"{BIN} is not root's; claude_setup.sh install"] and read == []


def test_verify_refuses_an_architecture_without_a_pinned_build():
    found = claude_code.problems(
        lstat=_host(), digest=lambda path: "a" * 64, machine="riscv64", pinned=PINNED
    )
    assert found == ["no pinned Claude Code build for riscv64"]


def test_the_digest_reads_the_file(tmp_path):
    path = tmp_path / "claude"
    path.write_bytes(b"x" * 100000)
    assert claude_code.sha256(str(path)) == hashlib.sha256(b"x" * 100000).hexdigest()


def test_versions_pin_a_build_per_architecture():
    pinned = install_catalog.versions()
    assert re.fullmatch(r"\d+\.\d+\.\d+", pinned["claude_code"])
    for arch in claude_code.ARCHES.values():
        assert re.fullmatch(r"[0-9a-f]{64}", pinned[f"claude_code_sha256_{arch}"]), arch


# --- the launches: the absolute path, no home on PATH ------------------------------------------------------------


def _code(rel):
    return "\n".join(
        ln
        for ln in (ROOT / rel).read_text(encoding="utf-8").splitlines()
        if not ln.lstrip().startswith("#")
    )


def test_every_launch_execs_the_root_binary_and_never_a_home_copy():
    launcher = _code("common/bin/agent-launch")
    assert f"secret_fd.py {BIN} --settings" in launcher
    assert (
        "export PATH=/opt/digipaddock-claude:/usr/local/bin:/usr/bin:/bin\n" in launcher
    )
    assert claude_code.PATH == "/opt/digipaddock-claude:/usr/local/bin:/usr/bin:/bin"
    for rel in (
        "common/bin/agent-launch",
        "common/kd/worker_runner.py",
        "common/kd/session_host/probe.py",
        "common/kd/effort_probe.py",
        "common/kd/boss_probe.py",
        "common/install/add_user.sh",
        "common/install/stage_setup.sh",
        "common/install/lib/hindsight_client.sh",
    ):
        text = _code(rel)
        # the only mention of the home copy left is its removal by its owner (add_user.sh)
        kept = [ln for ln in text.splitlines() if ".local/bin" in ln]
        assert all("rm -f -- .local/bin/claude" in ln for ln in kept), rel
        assert "install.sh" not in text, (
            rel
        )  # the vendor's script never runs: root takes the verified binary


def test_an_agents_verify_fails_without_the_pinned_root_binary_or_with_a_home_copy():
    """new_agent.sh --verify (and the release's outdated roles) differ until root's binary is the pinned build and
    the home copy of an older install is gone: the release reinstalls every role once, and that removes it."""
    import inspect

    from kd import install_apply

    body = inspect.getsource(install_apply.SystemOperations.verify)
    assert (
        "claude = claude_code.problems()" in body
        and 'raise ApplyError("; ".join(claude))' in body
    )
    assert (
        '(".local/bin/claude", ".local/share/claude")' in body
        and "os.path.lexists" in body
    )


def test_every_python_entry_point_an_agent_runs_is_isolated():
    """An entry point that runs as the agent outside its sandbox (a channel command, the permission hook) loads
    nothing of the agent's home: the system python, -I (no ~/.local/lib .pth, no PYTHON* variables, no python3
    earlier on PATH). The ones left on env python3 run only as root, from root's units."""
    root_only = {"kd-alert", "kd-board", "kd-release", "kd-usage"}
    for path in sorted((ROOT / "common/bin").iterdir()) + sorted(
        ROOT.glob("agents/*/bin/*")
    ):
        if not path.is_file():  # a __pycache__ another test's import left
            continue
        first = path.read_bytes().split(b"\n", 1)[0]
        if b"python" not in first:
            continue
        if (
            path.name in root_only
            and path.parent.name == "bin"
            and path.parent.parent.name == "common"
        ):
            assert first == b"#!/usr/bin/env python3", path.name
            continue
        assert first.endswith(b"python3 -I") or first.endswith(b"python -I"), path
    for name in ("kd-notes", "kd-permlog"):
        assert (
            (ROOT / "common/bin" / name)
            .read_bytes()
            .startswith(b"#!/usr/bin/python3 -I\n")
        ), name


def test_the_probes_environment_has_no_home_on_path():
    from kd import effort_probe

    env = effort_probe.environment("x", "/home/example-x")
    assert (
        env["PATH"].endswith(claude_code.PATH)
        and ".local" not in env["PATH"]
        and "/home" not in env["PATH"]
    )


def test_the_digest_is_read_once_per_file_version(tmp_path, monkeypatch):
    path = tmp_path / "claude"
    path.write_bytes(b"a")
    reads = []
    real = claude_code._read_sha256
    monkeypatch.setattr(
        claude_code, "_read_sha256", lambda p: reads.append(p) or real(p)
    )
    monkeypatch.setattr(claude_code, "_SEEN", {})
    first = claude_code.sha256(str(path))
    assert (
        claude_code.sha256(str(path)) == first and len(reads) == 1
    )  # every role's verify of one release
    path.write_bytes(b"bb")  # another build: read again
    assert (
        claude_code.sha256(str(path)) == hashlib.sha256(b"bb").hexdigest()
        and len(reads) == 2
    )


def test_the_memory_service_finds_the_root_binary_before_any_home():
    for unit in ("hindsight-api.service", "hindsight-worker.service"):
        text = (ROOT / "common/hindsight" / unit).read_text(encoding="utf-8")
        path = re.search(r"^Environment=PATH=(\S+)", text, re.M).group(1).split(":")
        assert path[:2] == ["/opt/hindsight/venv/bin", claude_code.DIR], unit


# --- claude_setup.sh: the pinned build, checked before it replaces the binary --------------------------------------


def _function(name):
    return SETUP.split(f"\n{name}() {{", 1)[1].split("\n}", 1)[0]


def test_the_installer_names_the_same_place():
    assert SETUP.startswith("#!/usr/bin/env bash\n") and "set -euo pipefail" in SETUP
    assert f"\nDIR={claude_code.DIR}\n" in SETUP and "\nBIN=$DIR/claude\n" in SETUP
    assert '[ "$(id -u)" = 0 ] || { echo "root is required" >&2; exit 1; }' in SETUP
    assert "  install|check) ;;" in SETUP


def test_install_checks_the_download_before_it_replaces_the_binary():
    body = _function("install_bin")
    pinned = _function("pinned")
    assert (
        "version=$(catalog claude_code)" in pinned
        and 'want=$(catalog "claude_code_sha256_$key")' in pinned
    )
    assert (
        "x86_64) key=amd64 platform=linux-x64 ;;" in pinned
        and "aarch64) key=arm64 platform=linux-arm64 ;;" in pinned
    )
    assert (
        "[[ $version =~ ^[0-9]+\\.[0-9]+\\.[0-9]+$ && $want =~ ^[0-9a-f]{64}$ ]]"
        in pinned
    )
    assert body.lstrip().split("\n")[:2] == ["local tmp", "  pinned"]
    assert (
        '"$RELEASES/$version/$platform/claude"' in body and "--proto '=https'" in body
    )
    assert "\nRELEASES=https://downloads.claude.ai/claude-code-releases\n" in SETUP
    assert (
        'tmp=$(mktemp -p "$DIR" .claude.XXXXXX)' in body
    )  # the same directory: the rename is atomic
    check = body.index('if [ "$(sha "$tmp")" != "$want" ]; then')
    assert check < body.index('chmod 0755 "$tmp"') < body.index('mv -fT "$tmp" "$BIN"')
    assert 'install -d -o root -g root -m 0755 "$DIR"' in body
    assert '[ ! -L "$DIR" ]' in body.split("install -d", 1)[0]
    # root never runs what it downloaded: neither the vendor's script nor the binary
    assert "| bash" not in SETUP and "install.sh" not in SETUP
    assert not re.search(r'^\s*(exec\s+)?"?\$\{?(tmp|BIN)\b', SETUP, re.M)


def test_check_names_a_binary_that_is_not_roots_or_not_the_pinned_build():
    body = _function("check")
    assert '[ "$(owner_mode "$DIR")" = "root:root 755 directory" ]' in body
    assert '[ "$(owner_mode "$BIN")" != "root:root 755 regular file" ]' in body
    assert 'elif [ "$(sha "$BIN")" != "$want" ]; then' in body
    assert "exit 1" in body and 'echo "claude: ok ($version)"' in body
    for change in ("install ", "curl", "mv ", "chmod", "chown", "rm "):
        assert change not in body, change


# claude_setup.sh with every system command a function: nothing runs as root, nothing is downloaded
HARNESS = r"""
log() { printf '%s\n' "$*" >> "$KD_LOG"; }
id() { echo 0; }
uname() { echo "$KD_MACHINE"; }
stat() { case $3 in /opt/digipaddock-claude) echo "$KD_DIR" ;; */claude) echo "$KD_BIN" ;; *) log "stat $*" ;; esac; }
sha256sum() { case $1 in */claude) echo "$KD_SHA  $1" ;; *) echo "$KD_DOWNLOAD  $1" ;; esac; }
/usr/bin/python3() { case ${*: -1} in claude_code) echo 2.1.289 ;; claude_code_sha256_amd64) echo "$KD_WANT" ;; *) return 2 ;; esac; }
install() { log "install $*"; }
mktemp() { echo /opt/digipaddock-claude/.claude.test; }
curl() { log "curl $*"; }
chown() { log "chown $*"; }
chmod() { log "chmod $*"; }
mv() { log "mv $*"; }
rm() { log "rm $*"; }
. common/install/claude_setup.sh "$KD_MODE"
"""
WANT = "a" * 64
ROOT_BIN = "root:root 755 regular file"


BASH = next(
    (
        c
        for c in (r"C:\Program Files\Git\bin\bash.exe", shutil.which("bash"))
        if c and Path(c).exists()
    ),
    None,
)
needs_bash = pytest.mark.skipif(BASH is None, reason="no working bash")


def _setup(
    tmp_path,
    mode,
    *,
    binary=ROOT_BIN,
    sha=WANT,
    download=WANT,
    directory="root:root 755 directory",
    machine="x86_64",
):
    log = tmp_path / "log"
    env = {
        **os.environ,
        "KD_LOG": log.as_posix(),
        "KD_MODE": mode,
        "KD_DIR": directory,
        "KD_BIN": binary,
        "KD_SHA": sha,
        "KD_DOWNLOAD": download,
        "KD_WANT": WANT,
        "KD_MACHINE": machine,
    }
    proc = subprocess.run(
        [BASH, "-c", HARNESS],
        cwd=ROOT,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    return proc, log.read_text(encoding="utf-8").splitlines() if log.exists() else []


@needs_bash
def test_setup_check_accepts_the_pinned_root_binary(tmp_path):
    proc, steps = _setup(tmp_path, "check")
    assert (proc.returncode, proc.stdout, steps) == (0, "claude: ok (2.1.289)\n", []), (
        proc.stderr
    )


@needs_bash
@pytest.mark.parametrize(
    "change, why",
    [
        pytest.param(
            {"binary": "digi-dev:digi-dev 755 regular file"},
            "must be a file root:root 755",
            id="agent-owned",
        ),
        pytest.param(
            {"binary": "root:root 777 symbolic link"},
            "must be a file root:root 755",
            id="link",
        ),
        pytest.param(
            {"binary": "root:root 775 regular file"},
            "must be a file root:root 755",
            id="group-writable",
        ),
        pytest.param({"binary": ""}, "must be a file root:root 755", id="missing"),
        pytest.param(
            {"sha": "c" * 64},
            "not Claude Code 2.1.289 of versions.json (sha256)",
            id="another-build",
        ),
        pytest.param(
            {"directory": "digi:digi 755 directory"},
            "/opt/digipaddock-claude: must be a directory",
            id="dir",
        ),
        pytest.param(
            {"machine": "riscv64"},
            "no pinned Claude Code build for riscv64",
            id="architecture",
        ),
    ],
)
def test_setup_check_refuses_what_a_launch_must_not_exec(tmp_path, change, why):
    proc, steps = _setup(tmp_path, "check", **change)
    assert proc.returncode == 1 and why in proc.stderr, proc.stderr
    assert steps == []  # a check changes nothing


@needs_bash
def test_setup_install_keeps_the_pinned_binary(tmp_path):
    proc, steps = _setup(tmp_path, "install")
    assert proc.returncode == 0, proc.stderr
    assert steps == ["install -d -o root -g root -m 0755 /opt/digipaddock-claude"]


@needs_bash
def test_setup_install_replaces_another_build_with_the_checked_download(tmp_path):
    # the check after the install still sees the stand-in's old sha256: exit 1 is the harness, the steps are the point
    proc, steps = _setup(tmp_path, "install", sha="c" * 64)
    tmp = "/opt/digipaddock-claude/.claude.test"
    assert steps == [
        "install -d -o root -g root -m 0755 /opt/digipaddock-claude",
        f"curl -fsSL --proto =https --retry 3 -o {tmp} "
        "https://downloads.claude.ai/claude-code-releases/2.1.289/linux-x64/claude",
        f"chown root:root {tmp}",
        f"chmod 0755 {tmp}",
        f"mv -fT {tmp} /opt/digipaddock-claude/claude",
    ], proc.stderr


@needs_bash
def test_setup_install_never_installs_a_download_of_another_sha256(tmp_path):
    proc, steps = _setup(tmp_path, "install", binary="", download="d" * 64)
    assert (
        proc.returncode == 1
        and "the download's sha256 is not the one of versions.json" in proc.stderr
    )
    assert not any(step.startswith(("mv ", "chmod ", "chown ")) for step in steps)
    assert (
        steps[-1] == "rm -f /opt/digipaddock-claude/.claude.test"
    )  # the download is removed


def test_the_base_phase_installs_and_checks_it():
    from kd import bootstrap

    text = (ROOT / "common/kd/bootstrap.py").read_text(encoding="utf-8")
    assert 'sh("common/install/claude_setup.sh", "check")' in text
    assert 'sh("common/install/claude_setup.sh", "install")' in text
    assert bootstrap.PHASES.index("base") < bootstrap.PHASES.index("agents")


def test_versions_json_is_valid_json_with_the_new_keys():
    data = json.loads(
        (ROOT / "common/install/versions.json").read_text(encoding="utf-8")
    )
    assert {"claude_code_sha256_amd64", "claude_code_sha256_arm64"} <= set(data)
