"""kd-factory — the developer's factory operations through the base channel; the channel names the caller,
the cabinet checks the rest."""

import io
import json

import pytest

from kd import factory_cli
from kd.cabinet.client import CabinetError

REGISTRY = {"dev": {"user": "digi-dev"}, "analyst": {"user": "digi-analyst"}}
ENV = {"KD_AGENT": "dev", "KD_CALLER_USER": "digi-dev"}
OP = {"id": "a" * 32, "state": "pending", "phase": "", "agents": ["dev"], "revision": "b" * 40, "error": "",
      "result": {}}


class Client:
    def __init__(self, answer=None, error=None):
        self.calls, self.answer, self.error = [], answer or {}, error

    def call(self, request):
        self.calls.append(request)
        if self.error:
            raise self.error
        return self.answer


@pytest.fixture
def registry(tmp_path):
    path = tmp_path / "agents.json"
    path.write_text(json.dumps(REGISTRY), encoding="utf-8")
    return str(path)


def run(argv, client, registry, environ=ENV):
    out, err = io.StringIO(), io.StringIO()
    code = factory_cli.main(argv, environ=environ, client=client, registry=registry, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_reinstall_prints_id(registry):
    client = Client({"operation": OP})
    code, out, _ = run(["reinstall", "KD-50", "--agents", "dev,boss", "--key", "lead:1"], client, registry)
    assert code == 0
    assert client.calls == [{"op": "operation.request", "agent": "dev", "card": "KD-50", "kind": "agent.reinstall",
                             "agents": ["dev", "boss"], "key": "lead:1"}]
    assert out.splitlines()[0] == f"operation {'a' * 32} pending"
    assert f"--outcome wait_dependency --operation {'a' * 32}" in out


def test_plan_reads_operations(registry):
    client = Client({"decision": {"id": "d" * 32, "state": "open"}})
    steps = [{"verb": "agent.reinstall", "target": "factory", "agents": ["dev"]}]
    code, out, _ = run(["plan", "KD-50", "--operations", json.dumps(steps), "--intent", "Обновить", "--basis", "PR"],
                       client, registry)
    assert code == 0 and out.startswith(f"plan {'d' * 32} open")
    request = client.calls[0]
    assert request["op"] == "decision.open" and request["type"] == "admin_plan" and request["agent"] == "dev"
    assert request["payload"] == {"policy": "factory", "proposal": {
        "schema_version": 2, "task_id": "KD-50", "intent": "Обновить", "basis": "PR", "operations": steps}}


def test_status_shows_result_and_supersession(registry):
    done = {**OP, "state": "succeeded", "result": {"agents": {"dev": {"state": "ok", "stage": "check", "error": ""}}}}
    client = Client({"operation": {**OP, "state": "superseded"}, "current": {**done, "id": "c" * 32}})
    code, out, _ = run(["status", "a" * 32], client, registry)
    assert code == 0 and "superseded by " + "c" * 32 in out and "  dev: ok check" in out


@pytest.mark.parametrize("argv", [
    ["shell", "KD-50"],
    ["restart", "nginx"],
])
def test_unknown_subcommand_refused(registry, argv):
    client = Client()
    code, _, err = run(argv, client, registry)
    assert code == 2 and client.calls == []


@pytest.mark.parametrize("argv", [
    ["reinstall", "KD-50", "--agents", "dev", "--key", "k", "--path", "/opt/x"],
    ["reinstall", "KD-50", "--agents", "../dev", "--key", "k"],
    ["reinstall", "KD-50", "--agents", "dev", "--key", "k", "--command", "rm -rf /"],
    ["reinstall", "/etc/passwd", "--agents", "dev", "--key", "k"],
    ["reinstall", "KD-50", "--agents", "dev", "--key", "a b"],
])
def test_path_or_command_arguments_refused(registry, argv):
    client = Client()
    code, _, _ = run(argv, client, registry)
    assert code == 2 and client.calls == []


def test_caller_must_be_the_agents_own_user(registry):
    client = Client({"operation": OP})
    for environ in ({}, {"KD_AGENT": "dev", "KD_CALLER_USER": "digi-analyst"}, {"KD_AGENT": "ghost",
                                                                             "KD_CALLER_USER": "digi-ghost"}):
        code, _, err = run(["status", "a" * 32], client, registry, environ)
        assert code == 2 and client.calls == []


def test_install_names_component_and_mode_only(registry):
    """a component and a mode of the catalog — no path, argument or command travels."""
    client = Client({"operation": {**OP, "kind": "component.install", "agents": [],
                                   "params": {"component": "repo-attach", "mode": "check", "drain": ""}}})
    code, out, _ = run(["install", "KD-50", "--component", "repo-attach", "--mode", "check", "--key", "c:1"],
                       client, registry)
    assert code == 0
    assert client.calls == [{"op": "operation.request", "agent": "dev", "card": "KD-50",
                             "kind": "component.install", "component": "repo-attach", "mode": "check",
                             "key": "c:1"}]
    assert out.splitlines()[0] == f"operation {'a' * 32} pending repo-attach check revision {'b' * 12}"
    assert f"--outcome wait_dependency --operation {'a' * 32}" in out


@pytest.mark.parametrize("argv", [
    ["install", "KD-50", "--component", "../repo", "--mode", "check", "--key", "k"],
    ["install", "KD-50", "--component", "repo-attach", "--mode", "install --force", "--key", "k"],
    ["install", "KD-50", "--component", "repo-attach", "--mode", "check", "--key", "k", "--args", "x"],
    ["install", "KD-50", "--component", "/opt/x.sh", "--mode", "install", "--key", "k"],
    ["install", "KD-50", "--mode", "install", "--key", "k"],
])
def test_install_refuses_anything_but_names(registry, argv):
    client = Client()
    code, _, _ = run(argv, client, registry)
    assert code == 2 and client.calls == []


def test_status_shows_a_component_run(registry):
    done = {**OP, "state": "failed", "kind": "component.install", "agents": [], "error": "check_failed",
            "params": {"component": "repo-attach", "mode": "install", "drain": ""},
            "result": {"agents": {}, "component": {"code": 0, "tail": "ok", "check": 1,
                                                   "check_tail": "digipaddock-repo-attach.path не запущен"}}}
    code, out, _ = run(["status", "a" * 32], Client({"operation": done}), registry)
    assert code == 0 and "component repo-attach install" in out and "error check_failed" in out
    assert "  check: exit 1" in out and "    digipaddock-repo-attach.path не запущен" in out


def test_cabinet_refusal_and_conflict_codes(registry):
    code, _, err = run(["reinstall", "KD-50", "--agents", "dev", "--key", "k"],
                       Client(error=CabinetError("refused", "no grant")), registry)
    assert code == 2 and "no grant" in err
    code, _, _ = run(["reinstall", "KD-50", "--agents", "dev", "--key", "k"],
                     Client(error=CabinetError("conflict", "other payload")), registry)
    assert code == 3
