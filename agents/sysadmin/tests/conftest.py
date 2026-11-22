import os
import sys
from datetime import timedelta, timezone
from pathlib import Path
# The host's names: the synthetic naming.json, set before kd.naming is first imported.
os.environ["KD_NAMING_JSON"] = str(Path(__file__).resolve().parents[3] / "common" / "tests" / "fixtures" / "host" / "naming.json")

import pytest

# the packages sysadmin/ and gate/ live in agents/sysadmin/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(1, str(Path(__file__).resolve().parents[3] / "common"))  # the package kd/


class MonitorCabinet:
    """The cabinet as kd-monitor reaches it: a real store behind the sysadmin agent's user, made on the
    first call; ``up = False`` — the cabinet is stopped (every call is «unavailable»)."""

    def __init__(self, path):
        self.path, self.up, self.calls, self._env = path, True, [], None

    @property
    def env(self):
        if self._env is None:
            common_tests = str(Path(__file__).resolve().parents[3] / "common" / "tests")
            if common_tests not in sys.path:
                sys.path.insert(0, common_tests)
            from cabinet_env import CabinetEnv

            self._env = CabinetEnv(self.path)
        return self._env

    def call(self, request):
        from kd.cabinet import client as cabinet_client

        self.calls.append(request["op"])
        if not self.up:
            raise cabinet_client.CabinetError("unavailable", "board unavailable (FileNotFoundError)")
        return self.env.client("digi-sysadmin").call(request)


@pytest.fixture
def monitor_cabinet(tmp_path, monkeypatch):
    from sysadmin.monitor import deliver

    cab = MonitorCabinet(tmp_path / "cabinet")
    monkeypatch.setattr(deliver, "cabinet_for", lambda: cab)
    return cab


@pytest.fixture
def enabled_all(tmp_path):
    """A servers.enabled file with every server of the inventory attached."""
    from sysadmin import inventory

    path = tmp_path / "servers.enabled"
    path.write_text("\n".join(sorted(inventory.load())) + "\n", encoding="utf-8")
    return path


# Set at import too: modules read the registries (which render @host:…@) while being collected.
os.environ["KD_HOST_JSON"] = str(Path(__file__).resolve().parents[3] / "common" / "tests" / "fixtures" / "host" / "host.json")


FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def _host_json(monkeypatch, tmp_path):
    """Deployment values come from the release tree's host/host.json; tests use the synthetic one, and
    never the host's own cabinet.json (on the stage it names the real cabinet). The inventory is the overlay's:
    tests read its synthetic copy."""
    from sysadmin import inventory
    from sysadmin.monitor import urls

    monkeypatch.setattr(inventory, "INVENTORY", FIXTURES / "inventory.json")
    from kd import admin_operations

    monkeypatch.setattr(admin_operations, "CATALOG_PATH", FIXTURES / "operations.json")
    monkeypatch.setattr(admin_operations, "INVENTORY_PATH", FIXTURES / "inventory.json")

    monkeypatch.setenv("KD_HOST_JSON", str(Path(__file__).resolve().parents[3] / "common" / "tests" / "fixtures" / "host" / "host.json"))
    monkeypatch.setattr(urls, "CABINET_JSON", str(tmp_path / "no-cabinet.json"))


@pytest.fixture(autouse=True)
def utc3_host(monkeypatch):
    """The monitor's clock is the host's zone (kd.clock.zone); these tests are of a UTC+3 host."""
    from kd import clock

    monkeypatch.setattr(clock, "zone", lambda: timezone(timedelta(hours=3), "UTC+3"))


@pytest.fixture(autouse=True)
def _plan_windows_off(monkeypatch):
    """The executor's plan window takes ssh calls of its own; the tests of the run expect the step's only.
    test_plan_window.py switches it on."""
    from sysadmin import execute, plan_window

    monkeypatch.setattr(execute, "WINDOWS", plan_window.Off())


@pytest.fixture(autouse=True)
def _envelopes_off(monkeypatch):
    """The host's envelopes flag (/etc/digipaddock/exec-envelopes) is the factory host's, not the tests': they run
    as a host without it. A test of the envelope's branch switches it on itself."""
    from kd import exec_envelope

    monkeypatch.setattr(exec_envelope, "enabled", lambda path=None: False)
