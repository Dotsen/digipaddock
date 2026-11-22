import importlib.util
import os
import shutil
from importlib.machinery import SourceFileLoader
from pathlib import Path
# The host's names: the synthetic naming.json, set before kd.naming is first imported.
os.environ["KD_NAMING_JSON"] = str(Path(__file__).resolve().parents[2] / "common" / "tests" / "fixtures" / "host" / "naming.json")

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
import sys
sys.path.insert(0, str(BIN.parent))  # the package common/kd
sys.path.insert(0, str(Path(__file__).resolve().parent))  # test helpers: cabinet_env, softauth, web_env
# Bind the kd package to this tree now: the scripts under test (kd-alert…) put the installed
# /opt/digipaddock/common first on sys.path, and on a host with a release a later first import would load it.
import kd  # noqa: E402,F401

REPO = BIN.parent.parent


SYSADMIN_FIXTURES = REPO / "agents" / "sysadmin" / "tests" / "fixtures"


@pytest.fixture(autouse=True)
def _overlay_files(monkeypatch):
    """The sysadmin's catalog and inventory come from the overlay; tests read their synthetic copies."""
    from kd import admin_operations

    monkeypatch.setattr(admin_operations, "CATALOG_PATH", SYSADMIN_FIXTURES / "operations.json")
    monkeypatch.setattr(admin_operations, "INVENTORY_PATH", SYSADMIN_FIXTURES / "inventory.json")


@pytest.fixture
def admin_proposal():
    """``make(env, task_id)``: put a ``server:node-c`` label on the card, store the sysadmin's schema-2 plan
    for it in ``env.admin`` and return the payload of an ``admin_plan`` decision."""
    import json as _json

    from admin_samples import CATALOG, ENABLED, INVENTORY, proposal
    from kd import admin_proposal as store
    from kd import admin_operations as ops
    from kd.cabinet import client as cabinet_client

    def make(env, task_id):
        env.put(task_id, servers=["node-c"])
        card = cabinet_client.admin_card(env.client("digi-cabinet"), task_id)
        plan = store.submit(env.admin.paths.proposals, _json.dumps(proposal(task_id=task_id)).encode("utf-8"),
                            agent="sysadmin", card=card, catalog=ops.Catalog.from_dict(_json.loads(_json.dumps(CATALOG))),
                            inventory=INVENTORY, enabled=ENABLED, now=env.when())
        return {"task_id": task_id, "proposal_sha256": plan.sha256, "card_scope_sha256": card.scope_sha256}

    return make


@pytest.fixture
def registry_root(tmp_path):
    """A repository root with a copy of the tracked agent registries and the files they name."""
    from kd import registry

    root = tmp_path / "repository"
    for relative in registry.REGISTRY_PATHS:
        source, target = REPO / relative, root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copyfile(source, target)
    tracked = registry.load_registry(REPO)
    for relative in sorted(registry.referenced_files(tracked.capabilities)):
        if (REPO / relative).is_file():
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO / relative, root / relative)
    return root


@pytest.fixture(scope="session")
def kd_alert():
    """The module common/bin/kd-alert (a file without .py)."""
    loader = SourceFileLoader("kd_alert", str(BIN / "kd-alert"))
    spec = importlib.util.spec_from_loader("kd_alert", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def kd_usage():
    """The module common/bin/kd-usage (a file without .py)."""
    loader = SourceFileLoader("kd_usage", str(BIN / "kd-usage"))
    spec = importlib.util.spec_from_loader("kd_usage", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def kd_task():
    """The module common/bin/kd-task (a file without .py)."""
    loader = SourceFileLoader("kd_task", str(BIN / "kd-task"))
    spec = importlib.util.spec_from_loader("kd_task", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def kd_boss_board():
    """common/bin/kd-boss-board as a module (file without .py)."""
    loader = SourceFileLoader("kd_boss_board", str(BIN / "kd-boss-board"))
    spec = importlib.util.spec_from_loader("kd_boss_board", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def kd_worker():
    """common/bin/kd-worker as a module (file without .py)."""
    loader = SourceFileLoader("kd_worker", str(BIN / "kd-worker"))
    spec = importlib.util.spec_from_loader("kd_worker", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def kd_nudge():
    """common/bin/kd-nudge as a module (file without .py)."""
    loader = SourceFileLoader("kd_nudge", str(BIN / "kd-nudge"))
    spec = importlib.util.spec_from_loader("kd_nudge", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def kd_work():
    """common/bin/kd-work as a module (file without .py)."""
    loader = SourceFileLoader("kd_work", str(BIN / "kd-work"))
    spec = importlib.util.spec_from_loader("kd_work", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def portable_flock(monkeypatch):
    """flock on Linux; elsewhere a process-local lock so attention and journal logic still run."""
    import threading
    from contextlib import contextmanager
    from types import SimpleNamespace

    from kd import activity_journal, attention

    if attention.fcntl is None:
        guard = threading.Lock()

        @contextmanager
        def locked(path):
            with guard:
                yield

        monkeypatch.setattr(attention, "_locked", locked)
        monkeypatch.setattr(activity_journal, "fcntl", SimpleNamespace(LOCK_EX=2, flock=lambda fd, op: None))


@pytest.fixture(scope="session")
def kd_permlog():
    """The module common/bin/kd-permlog (a file without .py)."""
    loader = SourceFileLoader("kd_permlog", str(BIN / "kd-permlog"))
    spec = importlib.util.spec_from_loader("kd_permlog", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


HOST_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "host" / "host.json"
# Set at import too: some test modules read the registries (which render @host:…@) while being collected.
os.environ["KD_HOST_JSON"] = str(HOST_FIXTURE)


@pytest.fixture(autouse=True)
def _host_json(monkeypatch):
    """Deployment values come from the release tree's host/host.json; tests use the synthetic one."""
    monkeypatch.setenv("KD_HOST_JSON", str(HOST_FIXTURE))
