"""Module setup scripts of the sysadmin capabilities (M02): the steps lifted from agent_setup.sh."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
INSTALL = ROOT / "agents/sysadmin/install"
MONITOR = (INSTALL / "monitor_setup.sh").read_text(encoding="utf-8")
BASH = next(
    (
        c
        for c in (r"C:\Program Files\Git\bin\bash.exe", shutil.which("bash"))
        if c and Path(c).exists()
    ),
    None,
)


def _function(text: str, name: str) -> str:
    return re.search(rf"^{name}\(\) \{{[^\n]*\n.*?^\}}\n", text, re.M | re.S).group(0)


def _mode(text: str, mode: str) -> str:
    cases = text[text.rindex("\ncase $mode in") :]
    return re.search(rf"^  {mode}\)(.*?);;", cases, re.M | re.S).group(1)


def _capability(name: str) -> dict:
    return json.loads((ROOT / f"common/agent-capabilities/{name}.json").read_text("utf-8"))


@pytest.mark.skipif(BASH is None, reason="no working bash")
@pytest.mark.parametrize("script", ["monitor_setup.sh"])
@pytest.mark.parametrize(
    "args",
    [
        [],
        ["bogus", "sysadmin", "digi-sysadmin"],
        ["install", "../x", "digi-sysadmin"],
        ["check", "sysadmin", "Agent Sys"],
    ],
)
def test_usage_is_refused_before_anything(script: str, args: list[str]) -> None:
    proc = subprocess.run([BASH, str(INSTALL / script), *args], capture_output=True, text=True)
    assert proc.returncode == 2 and "usage:" in proc.stderr


@pytest.mark.parametrize("text", [MONITOR], ids=["monitor"])
def test_install_verifies_with_its_own_check(text: str) -> None:
    install = [line.strip() for line in _mode(text, "install").splitlines() if line.strip()]
    assert install[-1] == "check"
    assert _mode(text, "check").strip() == "check"


# --- monitor.manage -----------------------------------------------------------------------


def test_monitor_setup_is_the_lifecycle_step_of_monitor_manage() -> None:
    entry = _capability("monitor.manage")
    assert entry["setup"] == "agents/sysadmin/install/monitor_setup.sh"
    assert entry["lifecycle"] == {
        "install": ["setup", "wrappers", "channels"],
        "check": ["setup", "wrappers", "channels"],
        "disable": ["channels", "setup"],
    }


def test_the_timer_stops_before_the_state_changes_owner_and_starts_after() -> None:
    # A tick running during the move would write into a half-moved state (Review Focus 1). the timers stop
    # first and a running tick finishes — stopped mid-run it is a failed unit and a kd-alert page; only a tick still
    # running past its unit's timeout is stopped.
    install = _function(MONITOR, "install_all")
    timers = install.index("systemctl stop digipaddock-monitor.timer digipaddock-monitor-check.timer ")
    wait = install.index("wait_idle\n")
    stop = install.index("systemctl stop digipaddock-monitor.service digipaddock-monitor-check.service ")
    chown = install.index("chown -hR")
    start = install.index("systemctl enable --now digipaddock-monitor.timer digipaddock-monitor-check.timer")
    assert timers < wait < stop < chown < start
    idle = _function(MONITOR, "wait_idle")
    assert "for unit in digipaddock-monitor.service digipaddock-monitor-check.service; do" in idle
    assert "systemctl show -p ActiveState --value" in idle and "activating|active|deactivating|reloading" in idle
    unit = (ROOT / "agents/sysadmin/systemd/digipaddock-monitor.service").read_text("utf-8")
    assert "TimeoutStartSec=620" in unit and "i < 620" in idle  # waits no longer than the tick may run


def test_state_follows_the_agent_user_without_following_links() -> None:
    install = _function(MONITOR, "install_all")
    assert 'install -d -o "$user" -g "$user" -m 0750 "$STATE"' in install
    assert 'chown -hR "$user:$user" "$STATE"' in install
    # Never a dereferencing chown: -R alone or with -L would follow links planted in the old state.
    assert not re.search(r"chown\s+-(R|\w*L)", MONITOR)


def test_both_units_run_as_the_agent_user_through_a_drop_in() -> None:
    install = _function(MONITOR, "install_all")
    assert 'write_dropin "$DROPIN"' in install and 'write_dropin "$CHECK_DROPIN"' in install
    assert "DROPIN=/etc/systemd/system/digipaddock-monitor.service.d/user.conf" in MONITOR
    assert "CHECK_DROPIN=/etc/systemd/system/digipaddock-monitor-check.service.d/user.conf" in MONITOR
    check = _function(MONITOR, "check")
    assert 'want_dropin "$DROPIN"' in check and 'want_dropin "$CHECK_DROPIN"' in check
    assert 'systemctl is-enabled --quiet "$timer"' in check
    assert "for timer in digipaddock-monitor.timer digipaddock-monitor-check.timer; do" in check


def test_dropin_sets_the_agent() -> None:
    # the tick wakes the agent KD_AGENT names (a cabinet command for itself); a drop-in without it fails
    # check.
    write = _function(MONITOR, "write_dropin")
    assert ('printf "[Service]\\nUser=%s\\nGroup=%s\\nEnvironment=KD_AGENT=%s\\n" "$user" "$user" "$agent"'
            in write)
    want = _function(MONITOR, "want_dropin")
    assert 'grep -qx "Environment=KD_AGENT=$agent" "$file"' in want


def test_the_monitor_makes_no_pages_and_no_nginx_group() -> None:
    # /monitor/ is gone — the cabinet shows the servers and P-n
    assert "monitor-web" not in MONITOR and "groupadd" not in MONITOR and "gpasswd" not in MONITOR


def test_the_check_now_units_poll_every_15_seconds_as_a_oneshot() -> None:
    service = (ROOT / "agents/sysadmin/systemd/digipaddock-monitor-check.service").read_text("utf-8")
    timer = (ROOT / "agents/sysadmin/systemd/digipaddock-monitor-check.timer").read_text("utf-8")
    assert "ExecStart=/usr/local/bin/kd-monitor check-now" in service and "Type=oneshot" in service
    assert "OnFailure" not in service  # a failed poll every 15 s must not page kd-alert
    assert "OnCalendar=*:*:0/15" in timer
    main = (ROOT / "agents/sysadmin/systemd/digipaddock-monitor.service").read_text("utf-8")
    assert "TimeoutStartSec=620" in main and "TimeoutStartSec=620" in service
    # the wait for tick.lock and the unit's own tick fit the timeout (a tick may outlive its budget by one ssh call)
    from sysadmin.monitor import deliver  # agents/sysadmin is on the path (conftest)
    from sysadmin.monitor.model import load_config

    cfg = load_config(_monitor_config())
    longest = cfg["tick_budget_s"] + cfg["call_timeout_s"] + 60
    assert deliver.tick_lock_wait(cfg) >= longest and deliver.tick_lock_wait(cfg) + longest <= 620


def test_disable_stops_the_monitor_and_keeps_its_data() -> None:
    disable = _mode(MONITOR, "disable")
    assert "systemctl disable --now digipaddock-monitor.timer digipaddock-monitor-check.timer" in disable
    assert "rm " not in disable


def _monitor_config():
    """The deployment's monitor settings (overlay); the code repository alone has only the example."""
    real = ROOT / "agents/sysadmin/monitor/config.json"
    return real if real.is_file() else ROOT / "agents/sysadmin/monitor/config.example.json"


def test_monitor_config_names_no_user_home() -> None:
    # the monitor no longer reads the owner's Telegram allowlist from the agent's home
    config = json.loads(_monitor_config().read_text("utf-8"))
    assert "owner_access_file" not in config and "/home/" not in json.dumps(config) and "~" not in json.dumps(config)
    deliver = (ROOT / "agents/sysadmin/sysadmin/monitor/deliver.py").read_text("utf-8")
    assert "owner_access_file" not in deliver and "chat_id" not in deliver


# --- servers.read: the ro key moves to the agent user ------------------------------------

SERVERS = (INSTALL / "servers_setup.sh").read_text(encoding="utf-8")
EXEC = (INSTALL / "exec_setup.sh").read_text(encoding="utf-8")

KEY_HARNESS = r"""set -euo pipefail
OLD=$1 NEW=$2 LOG=$3
LEGACY_USER=digi user=digi-sysadmin KEY=agent_ro_ed25519 COMMENT=agent-ro@example-stage
runuser() { echo "runuser $2 ${*:4}" >> "$LOG"; shift 3; "$@"; }
ssh-keygen() { echo "ssh-keygen" >> "$LOG"; local f=${*: -1}; echo new-key > "$f"; echo new-pub > "$f.pub"; }
"""


def _move_key(tmp_path: Path, old: dict, new: dict):
    old_dir, new_dir, log = tmp_path / "old", tmp_path / "new", tmp_path / "log.txt"
    old_dir.mkdir()
    for name, text in old.items():
        (old_dir / name).write_text(text, encoding="utf-8")
    if new:
        new_dir.mkdir()
        for name, text in new.items():
            (new_dir / name).write_text(text, encoding="utf-8")
    script = tmp_path / "step.sh"
    script.write_text(
        KEY_HARNESS
        + "".join(_function(SERVERS, f) for f in ("own", "copy", "same", "drop_legacy", "move_key"))
        + "move_key\n",
        encoding="utf-8",
        newline="\n",
    )
    proc = subprocess.run(
        [BASH, str(script), old_dir.as_posix(), new_dir.as_posix(), log.as_posix()],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    files = lambda d: {p.name: p.read_text("utf-8") for p in d.iterdir()} if d.exists() else {}  # noqa: E731
    return proc, files(old_dir), files(new_dir), log.read_text("utf-8") if log.exists() else ""


LEGACY = {"agent_ro_ed25519": "k", "agent_ro_ed25519.pub": "p", "known_hosts": "h"}


@pytest.mark.skipif(BASH is None, reason="no working bash")
def test_the_legacy_key_moves_unchanged_and_leaves_the_old_home(tmp_path: Path) -> None:
    proc, old, new, log = _move_key(tmp_path, LEGACY, {})
    assert proc.returncode == 0, proc.stderr
    assert new == LEGACY  # the same key: the targets' gates keep accepting it
    assert old == {"known_hosts": "h"}  # not a secret, stays
    assert "ssh-keygen" not in log
    # The legacy user reads its files, the agent user writes its own: root touches neither.
    assert "runuser digi cat" in log and "runuser digi-sysadmin sh -c" in log


@pytest.mark.skipif(BASH is None, reason="no working bash")
def test_a_key_already_moved_is_kept_and_a_stale_twin_dropped(tmp_path: Path) -> None:
    proc, old, new, log = _move_key(tmp_path, LEGACY, dict(LEGACY))
    assert proc.returncode == 0, proc.stderr
    assert new == LEGACY and old == {"known_hosts": "h"} and "ssh-keygen" not in log


@pytest.mark.skipif(BASH is None, reason="no working bash")
def test_two_different_keys_stop_the_install(tmp_path: Path) -> None:
    proc, old, new, _ = _move_key(tmp_path, LEGACY, {**LEGACY, "agent_ro_ed25519": "other"})
    assert proc.returncode == 1 and "two different ro keys" in proc.stderr
    assert old == LEGACY  # nothing lost


@pytest.mark.skipif(BASH is None, reason="no working bash")
def test_a_rerun_after_the_move_changes_nothing(tmp_path: Path) -> None:
    proc, old, new, log = _move_key(tmp_path, {"known_hosts": "h"}, dict(LEGACY))
    assert proc.returncode == 0 and new == LEGACY and "ssh-keygen" not in log


@pytest.mark.skipif(BASH is None, reason="no working bash")
def test_no_key_anywhere_makes_a_new_one_and_says_so(tmp_path: Path) -> None:
    proc, _, new, log = _move_key(tmp_path, {}, {})
    assert proc.returncode == 0, proc.stderr
    assert new["agent_ro_ed25519"].strip() == "new-key" and "ssh-keygen" in log
    assert "deploy_target.sh" in proc.stderr


def _check(tmp_path: Path, enabled: str | None) -> str:
    old_dir, new_dir = tmp_path / "old", tmp_path / "new"
    old_dir.mkdir()
    new_dir.mkdir()
    for name in ("agent_ro_ed25519", "agent_ro_ed25519.pub"):
        (new_dir / name).write_text("k", encoding="utf-8")
    listed = tmp_path / "servers.enabled"
    if enabled is not None:
        listed.write_text(enabled, encoding="utf-8")
    script = tmp_path / "check.sh"
    script.write_text(
        KEY_HARNESS + f"ENABLED={listed.as_posix()}\nproblems=()\n"
        + "".join(_function(SERVERS, f) for f in ("own", "check")) + "check\n",
        encoding="utf-8",
        newline="\n",
    )
    proc = subprocess.run(
        [BASH, str(script), old_dir.as_posix(), new_dir.as_posix(), (tmp_path / "log.txt").as_posix()],
        capture_output=True, encoding="utf-8", errors="replace", timeout=60,
    )
    return proc.stderr


@pytest.mark.skipif(BASH is None, reason="no working bash")
@pytest.mark.parametrize("enabled", [None, "", "\n"])
def test_a_host_with_no_target_yet_needs_no_known_hosts(tmp_path: Path, enabled: str | None) -> None:
    """The empty-host run: the sysadmin's first install made a new ro key and then refused for the missing
    known_hosts — which only deploy_target.sh writes, after the install. With no target enabled there is none."""
    assert "known_hosts" not in _check(tmp_path, enabled)


@pytest.mark.skipif(BASH is None, reason="no working bash")
def test_an_enabled_target_needs_its_pinned_host_key(tmp_path: Path) -> None:
    assert "known_hosts (deploy_target.sh)" in _check(tmp_path, "node-a\n")


def test_servers_and_exec_setups_are_lifecycle_steps() -> None:
    read, execute = _capability("servers.read"), _capability("servers.execute")
    assert read["setup"] == "agents/sysadmin/install/servers_setup.sh"
    assert read["lifecycle"] == {"install": ["setup", "wrappers", "channels"], "check": ["setup", "wrappers", "channels"],
                                 "disable": ["channels", "setup"]}
    assert execute["setup"] == "agents/sysadmin/install/exec_setup.sh"
    assert execute["lifecycle"]["install"] == ["executor-user", "setup", "wrappers", "read-grants", "channels"]
    assert execute["lifecycle"]["disable"] == ["channels", "read-grants", "setup"]


def test_the_legacy_user_is_never_the_agent_user() -> None:
    assert '[ "$user" != "$LEGACY_USER" ]' in SERVERS


def test_exec_setup_keeps_the_journal_and_key() -> None:
    install = _function(EXEC, "install_all")
    # Names from naming.sh: the exec user writes, the base user's group reads.
    assert "base=$(kd_user base)\nexec_user=$(kd_user exec)\n" in EXEC
    assert 'runuser -u "$exec_user" -- ssh-keygen' in install
    assert 'install -d -o "$exec_user" -g "$base" -m 2750 "$JOURNAL"' in install
    assert "    su $exec_user $base\n" in EXEC and "    create 0640 $exec_user $base\n" in EXEC
    assert """printf '%s\\n' "$ROTATE" > "$LOGROTATE\"""" in install
    check = _function(EXEC, "check")
    assert 'want "$KEY" "$exec_user:$exec_user" 600' in check


def test_exec_setup_takes_the_pult_token_back_from_the_exec_user() -> None:
    """the executor reports in the card's thread; only root (kd-alert) reads pult.env."""
    install, check = _function(EXEC, "install_all"), _function(EXEC, "check")
    assert '"$PULT"' not in install and "setfacl -m" not in EXEC
    assert '! runuser -u "$exec_user" -- test -r "$PULT" || problems+=("$exec_user can read $PULT")' in check


def signed_copies_check(tmp_path: Path, names: list, have: str, readable: str, writable: str) -> list:
    """the check's loop over the signed copies in the grant store, with stat answering ``have`` and
    runuser's ``test -r``/``test -w`` answering ``readable``/``writable``; -> the problems found."""
    check = _function(EXEC, "check")
    start = check.index('    local copy\n    for copy in "$GRANTS"/*.signed-*.json; do\n')
    end = check.index("    done\n", start) + len("    done\n")
    loop = check[start:end]
    grants = tmp_path / "grants"
    grants.mkdir(parents=True)
    for name in names:
        (grants / name).write_text("{}", encoding="utf-8")
    harness = tmp_path / "copies.sh"
    harness.write_text(
        f"set -euo pipefail\nproblems=()\nGRANTS={grants.as_posix()}\ncabinet=cab exec_user=ex user=ag\n"
        f"stat() {{ echo '{have}'; }}\n"
        f"runuser() {{ shift 3; if [ \"$2\" = -r ]; then [ {readable} = yes ]; else [ {writable} = yes ]; fi; }}\n"
        + _function(EXEC, "want") + "run() {\n" + loop + "}\nrun\n"
        + '[ ${#problems[@]} -eq 0 ] || printf "%s\\n" "${problems[@]}"\n',
        encoding="utf-8", newline="\n",
    )
    proc = subprocess.run([BASH, str(harness)], capture_output=True, encoding="utf-8", errors="replace", timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.splitlines()


@pytest.mark.skipif(BASH is None, reason="no working bash")
def test_exec_setup_checks_the_signed_copies(tmp_path: Path) -> None:
    names = ["KD-1.signed-plan-" + "a" * 64 + ".json", "KD-1.json", "store.lock"]
    assert signed_copies_check(tmp_path / "ok", names, "cab:ex 640", "yes", "no") == []
    problems = signed_copies_check(tmp_path / "bad", names, "ag:ex 660", "no", "yes")
    assert len(problems) == 3 and all("KD-1.signed-plan-" in p for p in problems)
    assert "want cab:ex 640" in problems[0] and "ex cannot read" in problems[1] and "ag can write" in problems[2]
    assert signed_copies_check(tmp_path / "none", ["KD-1.json"], "ag:ex 660", "no", "yes") == []


@pytest.mark.skipif(BASH is None, reason="no working bash")
@pytest.mark.parametrize("script", ["servers_setup.sh", "exec_setup.sh"])
def test_new_setups_refuse_bad_usage(script: str) -> None:
    proc = subprocess.run([BASH, str(INSTALL / script), "install", "../x", "digi-sysadmin"], capture_output=True, text=True)
    assert proc.returncode == 2 and "usage:" in proc.stderr


def journal_check(tmp_path: Path, script: str, owner: str, mode: str) -> list[str]:
    """The installer's check lines for its journal, with stat reporting <owner> <mode>; -> the problems found."""
    lines = [ln for ln in _function(script, "check").splitlines() if '.jsonl"' in ln and "want " in ln]
    harness = tmp_path / "journal.sh"
    harness.write_text(
        f"set -euo pipefail\nproblems=()\nJOURNAL={tmp_path.as_posix()}\nexec_user=x base=y forge=x user=y\n"
        f"stat() {{ echo '{owner} {mode}'; }}\n" + _function(script, "want") + "\n".join(lines)
        + '\n[ ${#problems[@]} -eq 0 ] || printf "%s\n" "${problems[@]}"\n',
        encoding="utf-8", newline="\n",
    )
    for name in ("exec.jsonl", "forge.jsonl"):
        (tmp_path / name).write_text("", encoding="utf-8")
    proc = subprocess.run([BASH, str(harness)], capture_output=True, encoding="utf-8", errors="replace", timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.splitlines()


@pytest.mark.skipif(BASH is None, reason="no working bash")
@pytest.mark.parametrize("mode,ok", [("640", True), ("650", True), ("660", False), ("644", False), ("600", False)])
def test_the_exec_journal_takes_the_x_of_a_read_grants_acl_mask(tmp_path: Path, mode: str, ok: bool) -> None:
    """the monthly rotation makes the journal under the directory's default ACL (the agent's read grant);
    once setfacl recomputes the mask, stat shows 650. The install must not fail on it: nobody but the exec user
    writes either way."""
    assert (journal_check(tmp_path, EXEC, "x:y", mode) == []) is ok
