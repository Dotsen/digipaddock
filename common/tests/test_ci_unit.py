"""The ci user's units (KD-9473): each in a pid namespace and a /proc of its own, the ci user's identity taken by
setpriv inside it, never by systemd's User= — two units of one uid must not reach each other through
/proc/<pid>/cwd. Pure argv checks, and one live check on the host (root, systemd, util-linux)."""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from kd import ci_unit, vulnscan_daily
from kd import devcheck_runner as dr
from kd.session_host import devcheck as host_devcheck

ROOT = Path(__file__).resolve().parents[2]
ID, ID2, ID3 = "a" * 32, "b" * 32, "c" * 32
WRAPPER = [
    ci_unit.UNSHARE,
    "--pid",
    "--fork",
    "--kill-child",
    "--mount-proc",
    "--",
    ci_unit.SETPRIV,
    "--reuid=digi-ci",
    "--regid=digi-ci",
    "--init-groups",
    "--inh-caps=-all",
    "--bounding-set=-all",
    "--no-new-privs",
    "--",
]
CAPS = "CapabilityBoundingSet=CAP_SYS_ADMIN CAP_SETUID CAP_SETGID CAP_SETPCAP"


def _props(argv):
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-p"]


def test_the_unit_starts_as_root_with_only_what_the_wrapper_needs():
    argv = ci_unit.argv(
        "u-1",
        ["PrivateNetwork=yes"],
        ["/bin/true", "x"],
        user="digi-ci",
        env=["HOME=/h"],
        workdir="/w",
    )
    assert argv[:7] == [
        "systemd-run",
        "--unit",
        "u-1",
        "--wait",
        "--pipe",
        "--collect",
        "--quiet",
    ]
    # the command is its namespace's init, deaf to a SIGTERM it has no handler for: the SIGKILL comes soon
    assert _props(argv) == [
        CAPS,
        "NoNewPrivileges=yes",
        f"TimeoutStopSec={ci_unit.STOP_S}",
        # SysV IPC, POSIX message queues and /dev/shm are the unit's own: no channel to a parallel check
        "PrivateIPC=yes",
        "TemporaryFileSystem=/dev/shm:mode=1777,nosuid,nodev",
        "PrivateNetwork=yes",
    ]
    assert [a for a in argv if a.startswith("--setenv=")] == [
        "--setenv=USER=digi-ci",
        "--setenv=LOGNAME=digi-ci",
        "--setenv=HOME=/h",
    ]
    # the first "--" is systemd-run's: the unit runs unshare, which runs setpriv, which runs env as the ci user
    assert argv[argv.index("--") + 1 :] == WRAPPER + [
        ci_unit.ENV,
        "--chdir=/w",
        "--",
        "/bin/true",
        "x",
    ]
    assert ci_unit.command_of(argv) == ["/bin/true", "x"]


def test_without_a_working_directory_the_command_follows_setpriv():
    argv = ci_unit.argv("u-1", [], ["/usr/bin/python3", "-I", "s.py"], user="digi-ci")
    assert argv[argv.index("--") + 1 :] == WRAPPER + ["/usr/bin/python3", "-I", "s.py"]
    assert ci_unit.command_of(argv) == ["/usr/bin/python3", "-I", "s.py"]


@pytest.mark.parametrize(
    "prop",
    [
        "User=digi-ci",
        "Group=digi-ci",
        "DynamicUser=yes",
        "SupplementaryGroups=adm",
        "WorkingDirectory=/w",
        "CapabilityBoundingSet=~",
        "AmbientCapabilities=CAP_SYS_ADMIN",
        "NoNewPrivileges=no",
        "SecureBits=keep-caps",
        "PrivateIPC=no",
        "IPCNamespacePath=/proc/1/ns/ipc",
        "JoinsNamespaceOf=digipaddock-devcheck-x.service",
        "TemporaryFileSystem=/dev/shm:ro",
    ],
)
def test_identity_and_privilege_are_the_wrappers_alone(prop):
    # a User= unit is in the host's pid namespace again: the caller cannot bring it back
    with pytest.raises(ValueError):
        ci_unit.argv("u-1", [prop], ["/bin/true"], user="digi-ci")


@pytest.mark.parametrize(
    "kw",
    [
        {"user": ""},
        {"user": "root"},
        {"user": "-x"},
        {"user": "a b"},
        {"user": "digi-ci", "command": []},
        {"user": "digi-ci", "command": ["A=b", "/bin/true"]},
    ],
)
def test_a_bad_user_or_command_is_refused(kw):
    kw = {"command": ["/bin/true"], **kw}
    with pytest.raises(ValueError):
        ci_unit.argv("u-1", [], kw.pop("command"), **kw)


def test_missing_names_the_absent_tools():
    assert all(t.startswith("/usr/bin/") for t in ci_unit.TOOLS)
    assert ci_unit.missing(access=lambda path, mode: path != ci_unit.SETPRIV) == [
        ci_unit.SETPRIV
    ]
    assert ci_unit.missing(access=lambda path, mode: True) == []


def _deps():
    return dr.Deps(
        cache="/c",
        units=None,
        own=None,
        seal=None,
        owner=None,
        locker=None,
        ci_user="digi-ci",
    )


def _daily():
    return vulnscan_daily.Daily(
        units=None, git=None, client=None, repos={}, host={}, ci_user="digi-ci"
    )


SCAN = os.path.join(vulnscan_daily.DEPLOYED, "common", "kd", "vuln_scan.py")


@pytest.mark.parametrize(
    "argv, command",
    [
        (
            _deps()._unit(ID, "/c/.new-venv", ["pip", "download"], 60, network=True),
            ["pip", "download"],
        ),
        (
            _deps()._unit(ID, "/c/.new-venv", ["pip", "install"], 60, network=False),
            ["pip", "install"],
        ),
        (
            _daily()._unit("db", ["update"], network=True, writable="/osv", limit=60),
            ["/usr/bin/python3", "-I", SCAN, "update"],
        ),
        (
            _daily()._unit("scan-x", ["scan"], network=False, writable="", limit=60),
            ["/usr/bin/python3", "-I", SCAN, "scan"],
        ),
    ],
)
def test_every_ci_unit_of_the_neighbours_goes_through_the_wrapper(argv, command):
    assert not any(
        p.startswith(("User=", "Group=", "DynamicUser=")) for p in _props(argv)
    )
    assert CAPS in _props(argv) and "NoNewPrivileges=yes" in _props(argv)
    assert argv[argv.index("--") + 1 :][: len(WRAPPER)] == WRAPPER
    assert ci_unit.command_of(argv) == command


@pytest.mark.parametrize("module", ["devcheck_runner.py", "vulnscan_daily.py"])
def test_the_runners_start_no_unit_of_their_own(module):
    # a ratchet: every unit these modules start is ci_unit's — a systemd-run or User= of their own would put the ci
    # user back in the host's pid namespace, where another check reaches its work folder through /proc
    text = (ROOT / "common" / "kd" / module).read_text(encoding="utf-8")
    assert "systemd-run" not in text and "User=" not in text


# --- live: two real units of one uid on this host --------------------------------------------------------------------
def _live_user():
    """A non-root user whose group has its name (the builders give --regid the user's name): daemon on Debian."""
    import grp
    import pwd

    for name in ("daemon", "bin", "sys"):
        try:
            entry, group = pwd.getpwnam(name), grp.getgrnam(name)
        except KeyError:
            continue
        if entry.pw_uid != 0 and entry.pw_gid == group.gr_gid:
            return entry
    return None


LIVE = (
    sys.platform.startswith("linux")
    and hasattr(os, "geteuid")
    and os.geteuid() == 0
    and shutil.which("systemd-run") is not None
    and os.path.isdir("/run/systemd/system")
    and not ci_unit.missing()
)


def _named(argv, name):
    """The real builder's argv under a test unit's name: no unit of a real check's name on a live host."""
    argv = list(argv)
    argv[argv.index("--unit") + 1] = name
    return argv


def _pid_in(directory, uid, deadline):
    while time.monotonic() < deadline:
        for pid in filter(str.isdigit, os.listdir("/proc")):
            try:
                if (
                    os.stat(f"/proc/{pid}").st_uid == uid
                    and os.readlink(f"/proc/{pid}/cwd") == directory
                ):
                    return int(pid)
            except OSError:
                continue
        time.sleep(0.1)
    return None


@pytest.mark.host
@pytest.mark.skipif(
    not LIVE, reason="needs root, systemd, unshare and setpriv: the Linux host"
)
def test_a_ci_unit_cannot_reach_another_through_proc():
    user = _live_user()
    if user is None:
        pytest.skip("no non-root user with a group of its name")
    tag = uuid.uuid4().hex[:12]
    # not under /tmp: the units' /tmp is private
    base = tempfile.mkdtemp(prefix="kd-ci-unit-", dir="/run")
    os.chmod(base, 0o755)
    victim_dir, attacker_dir, probe_dir = (
        os.path.join(base, n) for n in ("victim", "attacker", "probe")
    )
    for path in (victim_dir, attacker_dir, probe_dir):
        os.mkdir(path)
        os.chown(path, user.pw_uid, user.pw_gid)
    deps = dr.Deps(
        cache=base,
        units=None,
        own=None,
        seal=None,
        owner=None,
        locker=None,
        ci_user=user.pw_name,
    )
    victim_unit = f"kd-test-ci-victim-{tag}"
    victim = subprocess.Popen(
        _named(deps._unit(ID, victim_dir, ["/bin/sleep", "30"], 60), victim_unit),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        pid = _pid_in(victim_dir, user.pw_uid, time.monotonic() + 10)
        assert pid, "the victim unit's process did not start"

        # the probe: the ci user with no capabilities and no_new_privs, pid 1 of its own namespace, its exit code
        probe = subprocess.run(
            _named(
                deps._unit(
                    ID3,
                    probe_dir,
                    [
                        "/bin/sh",
                        "-c",
                        "id -u; id -G; "
                        "grep -E '^(CapInh|CapPrm|CapEff|CapBnd|CapAmb|NoNewPrivs):' "
                        "/proc/self/status; printf pid1=; cat /proc/1/comm; pwd; exit 7",
                    ],
                    30,
                ),
                f"kd-test-ci-probe-{tag}",
            ),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )
        lines = probe.stdout.splitlines()
        assert probe.returncode == 7, probe.stdout + probe.stderr
        assert lines[0] == str(user.pw_uid)
        assert sorted(int(g) for g in lines[1].split()) == sorted(
            set(os.getgrouplist(user.pw_name, user.pw_gid))
        )
        caps = dict(line.split(":\t", 1) for line in lines[2:8])
        assert all(
            int(caps[k], 16) == 0
            for k in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
        ), caps
        assert (
            caps["NoNewPrivs"] == "1"
            and lines[8] == "pid1=sh"
            and lines[9] == probe_dir
        )

        def attack(name):
            return f"if [ -e /proc/{pid} ]; then echo seen; fi; echo x > /proc/{pid}/cwd/{name}"

        # the control: a unit of the same uid without the wrapper (User=, the host's pid namespace) does reach it
        control = subprocess.run(
            [
                "systemd-run",
                "--unit",
                f"kd-test-ci-control-{tag}",
                "--wait",
                "--pipe",
                "--collect",
                "--quiet",
                "-p",
                f"User={user.pw_name}",
                "-p",
                f"Group={user.pw_name}",
                "--",
                "/bin/sh",
                "-c",
                attack("control"),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert control.returncode == 0 and os.path.exists(
            os.path.join(victim_dir, "control")
        ), control.stderr

        attacker = subprocess.run(
            _named(
                deps._unit(ID2, attacker_dir, ["/bin/sh", "-c", attack("planted")], 30),
                f"kd-test-ci-attacker-{tag}",
            ),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert attacker.returncode != 0 and "seen" not in attacker.stdout
        assert not os.path.exists(os.path.join(victim_dir, "planted"))
    finally:
        # sleep, the namespace's init, ignores a stop's SIGTERM: SIGKILL ends it now, not after STOP_S
        for action in (["kill", "--signal=SIGKILL"], ["stop"]):
            subprocess.run(
                ["systemctl", *action, "--", victim_unit],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=60,
            )
        try:
            victim.wait(timeout=30)
        finally:
            shutil.rmtree(base, ignore_errors=True)


@pytest.mark.host
@pytest.mark.skipif(
    not LIVE, reason="needs root, systemd, unshare and setpriv: the Linux host"
)
def test_the_dropped_command_has_the_units_own_ipc_and_shm():
    """PrivateIPC= gives the unit (unshare, root) an IPC namespace; unshare makes only a pid and a mount namespace
    and setpriv none, so the ci user's command is in the unit's — and sees the unit's own /dev/shm."""
    user = _live_user()
    if user is None:
        pytest.skip("no non-root user with a group of its name")
    tag = uuid.uuid4().hex[:12]
    base = tempfile.mkdtemp(prefix="kd-ci-unit-", dir="/run")
    os.chmod(base, 0o755)
    work = os.path.join(base, "probe")
    os.mkdir(work)
    os.chown(work, user.pw_uid, user.pw_gid)
    deps = dr.Deps(cache=base, units=None, own=None, seal=None, owner=None, locker=None, ci_user=user.pw_name)
    marker = f"kd-test-ci-shm-{tag}"
    try:
        probe = subprocess.run(
            _named(
                deps._unit(ID3, work, ["/bin/sh", "-c", f"readlink /proc/self/ns/ipc; id -u; "
                                                        f"echo x > /dev/shm/{marker} && echo wrote"], 30),
                f"kd-test-ci-ipc-{tag}",
            ),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )
        lines = probe.stdout.splitlines()
        assert probe.returncode == 0, probe.stdout + probe.stderr
        assert lines[1] == str(user.pw_uid) and lines[2] == "wrote"
        assert lines[0] != os.readlink("/proc/self/ns/ipc")  # not the host's IPC namespace
        assert not os.path.exists(f"/dev/shm/{marker}")  # its /dev/shm was its own, gone with it
    finally:
        shutil.rmtree(base, ignore_errors=True)
        if os.path.exists(f"/dev/shm/{marker}"):
            os.unlink(f"/dev/shm/{marker}")


def test_the_installer_checks_the_wrappers_tools():
    setup = (ROOT / "common" / "install" / "devcheck_setup.sh").read_text(
        encoding="utf-8"
    )
    check = setup.split("\ncheck() {", 1)[1].split("\n}", 1)[0]
    assert f"  for tool in {' '.join(ci_unit.TOOLS)}; do\n" in check
    assert '[ -x "$tool" ] || problems+=("no $tool' in check


def test_the_child_names_are_the_hosts():
    # the host stops a check's units by these names (devcheck_cancel); the wrapper keeps them
    argv = _deps()._unit(ID, "/c/.new-venv", ["pip"], 60)
    assert argv[argv.index("--unit") + 1] == host_devcheck.deps_unit(ID)
