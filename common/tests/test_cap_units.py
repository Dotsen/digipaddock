import pytest

from kd import cap_units


def test_socket_unit_is_group_only():
    name, text = cap_units.socket_unit("sysadmin", "exec", "digi-sysadmin")
    assert name == "digipaddock-cap-sysadmin-exec.socket"
    body = text.decode()
    assert "ListenStream=/run/digipaddock/cap-exec/sysadmin/exec.sock" in body
    assert (
        "SocketGroup=digi-sysadmin" in body
        and "SocketMode=0660" in body
        and "Accept=yes" in body
    )
    assert "PartOf=digipaddock-cap.target" in body and "WantedBy=digipaddock-cap.target" in body


def test_self_socket_path():
    assert (
        cap_units.socket_path("dev", "self") == "/run/digipaddock/cap/dev/self.sock"
    )


def test_host_channel_is_an_executor_channel():
    # kd-host-read runs as the host reader through its own channel, beside exec, forge and base
    assert cap_units.socket_path("dev", "host") == "/run/digipaddock/cap-exec/dev/host.sock"
    assert cap_units.grants_path("dev", "host") == "/etc/digipaddock/cap/dev/host.json"
    name, _ = cap_units.service_unit("dev", "host", "digi-observe", "/opt/digipaddock")
    assert name == "digipaddock-cap-dev-host@.service"


def test_service_unit_runs_as_channel_user_without_privileges():
    name, text = cap_units.service_unit("boss", "base", "digi", "/opt/digipaddock")
    body = text.decode()
    assert name == "digipaddock-cap-boss-base@.service"
    assert (
        "User=digi" in body
        and "NoNewPrivileges=yes" in body
        and "StandardInput=socket" in body
    )
    assert (
        "ExecStart=/usr/bin/python3 -I /opt/digipaddock/common/bin/kd-cap-serve boss base"
        in body
    )
    assert "StandardOutput=journal" in body and "RuntimeMaxSec=1h" in body
    # review 04.11.2026: one call — a kd-e2e run with its browser and tmpfs included — is bounded in memory and tasks
    assert "\nMemoryMax=3G\n" in body and "\nMemorySwapMax=0\n" in body and "\nTasksMax=1024\n" in body


def test_a_refusal_does_not_leave_a_failed_unit():
    # Seen on the factory host: the service answers a refusal with exit 126; a failed instance per refusal would reach
    # the sysadmin monitor (units check) as a crashed unit and keep the host degraded
    _, text = cap_units.service_unit("sysadmin", "self", "digi-sysadmin", "/opt/digipaddock")
    assert "\nSuccessExitStatus=126\n" in text.decode()


def test_tmpfiles_hide_expectations():
    name, text = cap_units.tmpfiles("sysadmin", "digi-sysadmin")
    assert name == "/etc/tmpfiles.d/digipaddock-cap-sysadmin.conf"
    assert (
        "d /run/digipaddock/cap-expect/sysadmin 0700 digi-sysadmin digi-sysadmin -"
        in text.decode()
    )


def test_rejects_unsafe_names():
    with pytest.raises(ValueError):
        cap_units.socket_unit("../x", "self", "digi-x")
    with pytest.raises(ValueError):
        cap_units.service_unit("x", "root", "root", "/opt/digipaddock")
    with pytest.raises(ValueError):
        cap_units.service_unit("x", "self", "digi-x", "relative/code")
