import os
from pathlib import Path
# The host's names: the synthetic naming.json, set before kd.naming is first imported.
os.environ["KD_NAMING_JSON"] = str(Path(__file__).resolve().parents[3] / "common" / "tests" / "fixtures" / "host" / "naming.json")

import pytest

HOST_FIXTURE = Path(__file__).resolve().parents[3] / "common" / "tests" / "fixtures" / "host" / "host.json"

# Set at import too: the registries render @host:…@ while the tests are collected.
os.environ["KD_HOST_JSON"] = str(HOST_FIXTURE)


@pytest.fixture(autouse=True)
def _host_json(monkeypatch):
    """Deployment values come from the release tree's host/host.json; tests use the synthetic one."""
    monkeypatch.setenv("KD_HOST_JSON", str(HOST_FIXTURE))
