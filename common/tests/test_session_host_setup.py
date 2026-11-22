"""session_host_setup.sh and the host's units — the script's contract, read from its text and a bash run."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

COMMON = Path(__file__).resolve().parents[1]
SCRIPT = COMMON / "install" / "session_host_setup.sh"
TEXT = SCRIPT.read_text(encoding="utf-8")
SYSTEMD = COMMON / "systemd"
BASH = next((c for c in (r"C:\Program Files\Git\bin\bash.exe", shutil.which("bash")) if c and Path(c).exists()), None)


def _mode(mode: str) -> str:
    cases = TEXT[TEXT.rindex("\ncase $mode in"):]
    return re.search(rf"^  {mode}\)(.*?);;", cases, re.M | re.S).group(1)


def test_units_render_tokens_and_socket_group():
    socket = (SYSTEMD / "digipaddock-session-host.socket").read_text(encoding="utf-8")
    assert "SocketUser=root" in socket and "SocketGroup=@cabinet@" in socket and "SocketMode=0660" in socket
    assert "ListenStream=/run/digipaddock-session-host/host.sock" in socket
    service = (SYSTEMD / "digipaddock-session-host.service").read_text(encoding="utf-8")
    assert "Requires=digipaddock-session-host.socket" in service and "kd-session-host serve" in service
    assert "User=" not in service  # root: each session is systemd-run under the agent's own user
    render = (COMMON / "install" / "render_host.sh").read_text(encoding="utf-8")
    present = re.search(r'^PRESENT="([^"]*)"', render, re.M).group(1).split()
    assert "common/systemd/digipaddock-session-host.socket" in present and "common/systemd/digipaddock-session-host.service" in present
    for unit in ("digipaddock-session-host.socket", "digipaddock-session-host.service"):
        assert f'kd_install_rendered "$REPO/common/systemd/{unit}"' in TEXT or unit in TEXT


@pytest.mark.skipif(BASH is None, reason="no working bash")
@pytest.mark.parametrize("args", [[], ["bogus"], ["install", "x"], ["enable"], ["enable", "../x"], ["release", "A"],
                                  ["enable", "analyst", "more"], ["enable", "dev", "--slots"],
                                  ["enable", "dev", "--slots", "x"], ["enable", "dev", "--slots", "10"],
                                  ["enable", "dev", "--slot", "2"], ["release", "dev", "--slots", "2"]])
def test_setup_usage_and_root_only(args):
    proc = subprocess.run([BASH, str(SCRIPT), *args], capture_output=True, text=True)
    assert proc.returncode == 2 and "usage:" in proc.stderr
    assert '[ "$(id -u)" = 0 ] || { echo "root is required" >&2; exit 1; }' in TEXT
    assert TEXT.index('"$(id -u)" = 0') < TEXT.index("systemctl")


def test_install_creates_an_empty_config_and_verifies_with_its_own_check():
    install = _mode("install")
    assert "config init" in install and "systemctl enable --now digipaddock-session-host.socket" in install
    assert install.strip().splitlines()[-1].strip() == "check"
    assert "systemctl disable --now digipaddock-session-host.socket digipaddock-session-host.service" in _mode("disable")


def test_enable_does_not_touch_panel_units():
    """no panel to stop — enable checks the runtime and the login, then lists the agent."""
    enable = _mode("enable")
    for gone in ("stop_claude", "systemctl"):
        assert gone not in enable, gone
    order = [
        enable.index("generated"),  # only an agent on the generated runtime
        enable.index(".credentials.json"),  # with its Claude login
        enable.index('config add "$agent" ${slots'),  # the listing, after the shortcut of a listed agent
    ]
    assert order == sorted(order)
    assert "panel" not in TEXT


def test_enable_takes_the_hosts_claude_token_for_a_login():
    """the host's token (a root file, mode 600) is the agents' login; the user's stored one is the fallback."""
    enable = _mode("enable")
    assert "token=$(token_state)" in enable and enable.index("token_state") < enable.index(".credentials.json")
    assert "$(kd_cmd kd-secret) put claude-token" in enable
    assert "TOKEN=/etc/digipaddock/claude.env" in TEXT and '"0:600"' in TEXT and "! -L" in TEXT


def test_a_malformed_token_file_is_refused_by_enable_and_check():
    """A token file systemd would read otherwise than the runner (empty, quoted, extra lines) — enable
    refuses it rather than falling back to the login it would override, and check names it."""
    state = TEXT[TEXT.index("token_state() {"):TEXT.index("\n}\n", TEXT.index("token_state() {"))]
    assert '"$REPO/common/kd/claude_token.py" check "$TOKEN"' in state and "echo missing" in state
    enable = _mode("enable")
    assert re.search(r'"\$token" = missing \]; then[^\n]*\n\s*\[ -f "\$home/\.claude/\.credentials\.json" \]', enable)
    assert 'elif [ -n "$token" ]; then' in enable
    assert "put it again: $(kd_cmd kd-secret) put claude-token" in enable
    checker = TEXT[TEXT.index("check() {"):TEXT.index("\n}\n", TEXT.index("check() {"))]
    assert "token=$(token_state)" in checker and "put it again" in checker


def test_release_leaves_the_panel_off():
    """no panel to go back to — release unlists the agent and stops its sessions; it is not running after."""
    release = _mode("release")
    assert release.index("config remove") < release.index("stop_sessions")
    assert "#   release <agent>  off the session host:" in TEXT


def test_release_finds_the_sessions_by_cgroup():
    """A worker job's or a login's claude runs as the agent's user too: release stops only the host's sessions, in
    their slice (the host's own unit digipaddock-session-host.service is not a session)."""
    assert 'in_sessions "/proc/$pid/cgroup"' in TEXT and "pgrep -u" in TEXT
    assert "digipaddock-session-'" not in TEXT


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_only_the_slice_is_a_session(tmp_path):
    function = re.search(r"^in_sessions\(\) \{.*?^\}", TEXT, re.M | re.S).group(0)
    cases = {
        "session": "0::/digipaddock-sessions.slice/digipaddock-session-dev-1.service\n",
        "job": "0::/digipaddock-sessions.slice/digipaddock-session-job-0a1b2c3d4e5f.service\n",
        "host": "0::/system.slice/digipaddock-session-host.service\n",
        "old-panel": "0::/system.slice/digipaddock.service\n",
        "login": "0::/user.slice/user-2001.slice/session-4.scope\n",
    }
    for name, text in cases.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    script = function + "\n" + "".join(
        f'in_sessions "{(tmp_path / n).as_posix()}" && echo yes || echo no\n' for n in cases)
    script += f'in_sessions "{(tmp_path / "gone").as_posix()}" && echo yes || echo no\n'  # the process ended meanwhile
    run = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert run.stdout.split() == ["yes", "yes", "no", "no", "no", "no"], run.stderr


def test_cabinet_unit_reaches_host_socket():
    cabinet = (SYSTEMD / "digipaddock-cabinet.service").read_text(encoding="utf-8")
    assert "ReadWritePaths=-/run/digipaddock-session-host" in cabinet
    assert "SupplementaryGroups=@cabinet@" in cabinet  # the socket's group


def test_enable_passes_slots_to_the_config():
    """`enable <agent> --slots N` — a listed agent only changes its slots, a new one is listed with them."""
    enable = _mode("enable")
    update = enable.index('config add "$agent" "$slots"')
    assert update < enable.index("generated")
    assert enable.index('config add "$agent" ${slots:+"$slots"}') > enable.index(".credentials.json")


def test_install_puts_the_host_cli_on_roots_path():
    """The factory's notices tell root ``kd-session-host status|resume <agent>``: the name must resolve."""
    assert 'ln -sfn "$REPO/common/bin/kd-session-host" "$SESSION_HOST_LINK"' in _mode("install")
    assert "SESSION_HOST_LINK=/usr/local/bin/$(kd_cmd kd-session-host)" in SCRIPT.read_text("utf-8")
