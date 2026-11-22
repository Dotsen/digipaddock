import os
import sys

import pytest
from pathlib import Path
# The host's names: the synthetic naming.json, set before kd.naming is first imported.
os.environ["KD_NAMING_JSON"] = str(Path(__file__).resolve().parents[3] / "common" / "tests" / "fixtures" / "host" / "naming.json")

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))                        # the dev_agent/ package
sys.path.insert(0, str(HERE.parents[1] / "common"))  # the kd/ package
sys.path.insert(0, str(HERE / "tests"))              # shared test helpers (test_forge_read.make_opener) under --import-mode=importlib


# Set at import too: modules read the registries (which render @host:…@) while being collected.
os.environ["KD_HOST_JSON"] = str(Path(__file__).resolve().parents[3] / "common" / "tests" / "fixtures" / "host" / "host.json")


@pytest.fixture(autouse=True)
def _host_json(monkeypatch):
    """Deployment values come from the release tree's host/host.json; tests use the synthetic one."""
    monkeypatch.setenv("KD_HOST_JSON", str(Path(__file__).resolve().parents[3] / "common" / "tests" / "fixtures" / "host" / "host.json"))
