"""the release's request to the factory — the roles this code would install differently, asked for on
the card of the PR. as root — the owner signed the release, its roles' reinstall needs no grant of its own."""

from pathlib import Path

from kd import release_reinstall
from kd.cabinet.client import CabinetError


class Client:
    """Answers by op: ``answer`` for ``operation.request``, ``steps`` for ``operation.release_steps``;
    ``error`` is raised on every call, ``errors`` on the named ops only."""

    def __init__(self, answer=None, error=None, steps=None, errors=None):
        self.calls, self.answer, self.error, self.steps = [], answer, error, steps
        self.errors = errors or {}

    def call(self, request):
        self.calls.append(request)
        if self.error:
            raise self.error
        if request["op"] in self.errors:
            raise self.errors[request["op"]]
        if request["op"] == "operation.release_steps":
            return {"steps": list(self.steps or [])}
        return self.answer


def _run(card, client, outdated=("boss", "dev")):
    return release_reinstall.reinstall(Path("/opt/kd"), "abcdef1", card, outdated=lambda repo: list(outdated),
                                       client=client)


def test_release_requests_outdated_roles():
    client = Client({"operation": {"id": "f" * 32, "state": "pending"}})
    assert _run("KD-12", client) == {"state": "pending", "operation": "f" * 32, "agents": ["boss", "dev"],
                                     "components": []}
    assert client.calls == [{"op": "operation.request", "requester": "root", "card": "KD-12",
                             "kind": "agent.reinstall", "agents": ["boss", "dev"], "key": "release:abcdef1",
                             "revision": "abcdef1"},
                            {"op": "operation.release_steps", "card": "KD-12"}]


def test_nothing_outdated_asks_only_for_the_release_steps():
    client = Client()
    assert _run("KD-12", client, outdated=()) == {"state": "none", "components": []}
    assert [c["op"] for c in client.calls] == ["operation.release_steps"]
    client = Client()
    assert _run("", client, outdated=()) == {"state": "none", "components": []} and client.calls == []


def test_a_release_without_card_asks_as_root_without_one():
    """no card, no grant either way — the roles are asked for as root; a root release is the same."""
    client = Client({"operation": {"id": "f" * 32, "state": "pending"}})
    found = release_reinstall.reinstall(Path("/opt/kd"), "abcdef1", "", outdated=lambda repo: ["sysadmin"],
                                        client=client, release_id="abcdef1+1234567")
    assert found == {"state": "pending", "operation": "f" * 32, "agents": ["sysadmin"], "components": []}
    assert client.calls == [{"op": "operation.request", "requester": "root", "kind": "agent.reinstall",
                             "agents": ["sysadmin"], "key": "release:abcdef1+1234567", "revision": "abcdef1"}]


def test_uncovered_roles_are_a_notice_and_nothing_else_is():
    """The release stands either way; roles left on the old text are said where the owner reads, not only in
    result.json."""
    posted = []
    post = lambda source, level, key, params, card: posted.append((source, level, key, params, card)) or True
    found = {"state": "uncovered", "agents": ["boss", "dev"], "why": "KD-12: grant does not cover boss"}
    assert release_reinstall.uncovered_notice(found, "abcdef1+1234567", "KD-12", post=post) is True
    assert posted == [("release", "warn", "notice.release.reinstall_uncovered",
                       {"rev": "abcdef1+1234567", "agents": "boss, dev", "agents_csv": "boss,dev",
                        "why": "KD-12: grant does not cover boss"}, "KD-12")]
    for quiet in ({"state": "none"}, {"state": "pending", "agents": ["dev"]},
                  {"state": "deferred", "agents": ["dev"], "retry": True}, {"state": "uncovered", "agents": []}):
        assert release_reinstall.uncovered_notice(quiet, "abcdef1", "", post=post) is False, quiet
    assert len(posted) == 1


def test_release_uncovered_role_named():
    found = _run("KD-12", Client(error=CabinetError("refused", "KD-12: grant does not cover boss")))
    assert found == {"state": "uncovered", "agents": ["boss", "dev"], "why": "KD-12: grant does not cover boss",
                     "components": []}
    found = _run("KD-12", Client(error=OSError("no socket")))
    assert found["state"] == "deferred" and "cabinet" in found["why"]  # asked again after the update


def test_a_cabinet_that_could_not_take_it_is_asked_again():
    """the cabinet still on the old code (unavailable, an op it does not know) — kd-release asks again once
    the cabinet runs the new code; a refusal by the rules is never retried."""
    for error in (OSError("no socket"), CabinetError("unavailable", "catalog"),
                  CabinetError("invalid", "unknown operation")):
        found = _run("KD-12", Client(error=error))
        assert found["retry"] is True and found["state"] == "deferred", error
        assert found["key"] == "release:abcdef1" and found["agents"] == ["boss", "dev"], error
    for error in (CabinetError("refused", "no grant"), CabinetError("invalid", "agents is a non-empty list")):
        assert "retry" not in _run("KD-12", Client(error=error)), error


def test_reinstall_asks_for_release_steps_after_the_roles():
    """The component steps approved on ``revision: "release"`` are the release's to request — one per
    step, after the roles, on the released revision; the result names them."""
    client = Client({"operation": {"id": "f" * 32, "state": "pending"}},
                    steps=[{"component": "repo-attach", "mode": "check"}, {"component": "transcribe", "mode": "check"}])
    found = _run("KD-12", client)
    assert [c["op"] for c in client.calls] == ["operation.request", "operation.release_steps", "operation.request",
                                               "operation.request"]
    assert client.calls[2] == {"op": "operation.request", "requester": "release", "card": "KD-12",
                               "kind": "component.install", "component": "repo-attach", "mode": "check",
                               "key": "release:abcdef1:repo-attach:check", "revision": "abcdef1"}
    assert found["components"] == [
        {"component": "repo-attach", "mode": "check", "operation": "f" * 32, "state": "pending"},
        {"component": "transcribe", "mode": "check", "operation": "f" * 32, "state": "pending"}]
    assert found["state"] == "pending" and found["operation"] == "f" * 32


def test_a_release_step_the_cabinet_refuses_is_named_with_why():
    client = Client({"operation": {"id": "f" * 32, "state": "pending"}},
                    steps=[{"component": "repo-attach", "mode": "check"}],
                    errors={"operation.request": CabinetError("refused", "revision moved")})
    found = _run("KD-12", client, outdated=())
    assert found["state"] == "none"
    assert found["components"] == [{"component": "repo-attach", "mode": "check", "state": "uncovered",
                                    "why": "revision moved"}]


def test_an_old_cabinet_defers_the_release_steps_too():
    client = Client(errors={"operation.release_steps": CabinetError("invalid", "unknown operation")})
    found = _run("KD-12", client, outdated=())
    assert found["state"] == "deferred" and found["retry"] is True and found["key"] == "release:abcdef1"
    assert found["agents"] == [] and found["components"] == []


def test_release_id_keys_the_requests_and_the_revision_stays_the_code_s():
    """a pair release keys its operations by ``<rev>+<deploy>``; the revision is the code's."""
    client = Client({"operation": {"id": "f" * 32, "state": "pending"}}, steps=[{"component": "transcribe", "mode": "check"}])
    release_reinstall.reinstall(Path("/opt/kd"), "abcdef1", "KD-12", outdated=lambda repo: ["dev"], client=client,
                                release_id="abcdef1+1234567")
    requests = [c for c in client.calls if c["op"] == "operation.request"]
    assert [(c["key"], c["revision"]) for c in requests] == [
        ("release:abcdef1+1234567", "abcdef1"), ("release:abcdef1+1234567:transcribe:check", "abcdef1")]


def test_main_takes_the_release_id(monkeypatch, capsys):
    seen = {}

    def fake(repo, revision, card, *, outdated, client, release_id=None):
        seen.update(revision=revision, card=card, release_id=release_id)
        return {"state": "none"}

    monkeypatch.setattr(release_reinstall, "reinstall", fake)
    assert release_reinstall.main(["/opt/kd", "abcdef1", "KD-12", "--release-id", "abcdef1+1234567"]) == 0
    assert seen == {"revision": "abcdef1", "card": "KD-12", "release_id": "abcdef1+1234567"}
    assert release_reinstall.main(["/opt/kd", "abcdef1", "--release-id", "abcdef1+1234567"]) == 0
    assert seen["card"] == ""
    assert release_reinstall.main(["/opt/kd", "abcdef1", "--by-root", "--release-id", "abcdef1+1234567"]) == 0
    assert seen["card"] == ""  # an older kd-release's --by-root is taken and changes nothing
    assert release_reinstall.main(["/opt/kd", "abcdef1", "KD-12", "--release-id", "../x"]) == 2


def test_main_posts_the_uncovered_notice(monkeypatch, capsys):
    posted = []
    monkeypatch.setattr(release_reinstall, "reinstall",
                        lambda *a, **kw: {"state": "uncovered", "agents": ["dev"], "why": "no card"})
    assert release_reinstall.main(["/opt/kd", "abcdef1", "--release-id", "abcdef1+1234567"],
                                  post=lambda *a: posted.append(a) or True) == 0
    assert [(p[2], p[3]["rev"], p[4]) for p in posted] == [("notice.release.reinstall_uncovered", "abcdef1+1234567", "")]


# --- the host's release is pinned and restarted when its code changed -------------------------------
ROOT = Path(__file__).resolve().parents[2]
HOST_FILES = {"common/kd/session_host/core.py": "core", "common/bin/kd-session-host": "bin",
              "common/kd/cabinet/api.py": "api 1", "REVISION": "abc1234\n"}


def _release(base, name, files):
    for rel, text in files.items():
        path = base / name / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return base / name


def test_the_host_pins_its_release_only_from_a_release(tmp_path):
    base = tmp_path / "releases"
    tree = _release(base, "abc1234-1", {"common/kd/x.py": ""})
    assert release_reinstall.pin_host(tree) is False and not (base / "HOST").exists()
    (tree / "REVISION").write_text("abc1234\n")
    assert release_reinstall.pin_host(tree) is True
    assert release_reinstall.pinned_release(base / "other") == tree


def test_host_changed_reads_only_the_hosts_files(tmp_path):
    old = _release(tmp_path, "old", HOST_FILES)
    new = _release(tmp_path, "new", {**HOST_FILES, "common/kd/cabinet/api.py": "api 2"})
    assert not release_reinstall.host_changed(old, new)
    (new / "common/kd/session_host/core.py").write_text("core 2")
    assert release_reinstall.host_changed(old, new)
    assert release_reinstall.host_changed(tmp_path / "gone", new)


def test_host_changed_follows_the_hosts_imports(tmp_path):
    # review I1: a module the host's modules import (manifest under worker_slots) is the host's code too
    files = {**HOST_FILES, "common/kd/session_host/core.py": "from kd import worker_slots\n",
             "common/kd/worker_slots.py": "from kd import manifest\nfrom kd.cabinet import api\n",
             "common/kd/manifest.py": "m 1"}
    old = _release(tmp_path, "old", files)
    new = _release(tmp_path, "new", {**files, "common/kd/cabinet/api.py": "api 2"})
    assert not release_reinstall.host_changed(old, new)  # the cabinet restarts on its own api, the host only calls it
    (new / "common/kd/manifest.py").write_text("m 2")
    assert release_reinstall.host_changed(old, new)


def test_host_paths_cover_the_live_hosts_imports():
    paths = release_reinstall.host_paths(ROOT)
    for rel in ("common/kd/session_host/core.py", "common/kd/worker_slots.py", "common/kd/manifest.py",
                "common/kd/cap_proto.py", "common/kd/boss_board.py", "common/kd/cabinet/users.py"):
        assert rel in paths
    assert not {"common/kd/cabinet/api.py", "common/kd/cabinet/server.py", "common/kd/cabinet/store.py"} & set(paths)


def test_host_data_names_files_of_the_tree():
    missing = [p for p in release_reinstall.HOST_DATA if not p.startswith("host/") and not (ROOT / p).exists()]
    assert missing == []


def test_host_restart_asks_root_once_the_host_code_changed(tmp_path):
    old = _release(tmp_path, "old", HOST_FILES)
    new = _release(tmp_path, "new", {**HOST_FILES, "common/bin/kd-session-host": "bin 2"})
    client = Client(answer={"operation": {"id": "op1", "state": "pending"}})
    assert release_reinstall.host_restart(old, new, "abc1234+def5678", client) == {"state": "pending", "operation": "op1"}
    assert client.calls == [{"op": "operation.request", "requester": "root", "kind": "component.install",
                             "component": "session-host", "mode": "restart",
                             "key": "release:abc1234+def5678:host-restart", "revision": "abc1234"}]


def test_the_hosts_own_restart_key_is_never_a_release_steps_key(tmp_path):
    """a card's step session-host restart on revision ``release`` is keyed ``release:<id>:session-host:
    restart`` on that card; the host's own restart under the same key named another request — refused, and the
    health check failed session_host_code. Its own key joins the step's running operation instead."""
    old = _release(tmp_path, "old", HOST_FILES)
    new = _release(tmp_path, "new", {**HOST_FILES, "common/bin/kd-session-host": "bin 2"})
    client = Client(answer={"operation": {"id": "op1", "state": "pending"}},
                    steps=[{"component": "session-host", "mode": "restart"}])
    release_reinstall.reinstall(new, "abc1234", "KD-12", outdated=lambda repo: [], client=client,
                                release_id="abc1234+def5678")
    release_reinstall.host_restart(old, new, "abc1234+def5678", client)
    keys = [c["key"] for c in client.calls if c["op"] == "operation.request"]
    assert len(keys) == 2 and len(set(keys)) == 2, keys


def test_a_rollbacks_host_restart_has_a_key_of_its_own(tmp_path):
    """Review M3: the release rolled back to already used ``release:<id>:…`` — its restart ended long ago."""
    old = _release(tmp_path, "old", HOST_FILES)
    new = _release(tmp_path, "new", {**HOST_FILES, "common/bin/kd-session-host": "bin 2"})
    client = Client(answer={"operation": {"id": "op2", "state": "pending"}})
    assert release_reinstall.host_restart(old, new, "abc1234", client, rollback="1730000000.123")["operation"] == "op2"
    assert client.calls[0]["key"] == "rollback:abc1234:1730000000.123:host-restart"


def test_host_main_takes_a_rollback_stamp(tmp_path, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(release_reinstall, "host_restart",
                        lambda pinned, target, rid, client, rollback="": seen.append((rid, rollback)) or {"state": "none"})
    assert release_reinstall.host_main([str(tmp_path), "abc1234", "--rollback", "1730000000.123"]) == 0
    assert release_reinstall.host_main([str(tmp_path), "abc1234", "--rollback", "x; rm"]) == 2
    assert seen == [("abc1234", "1730000000.123")]


def test_host_restart_is_none_without_a_pin_on_its_own_release_or_unchanged(tmp_path):
    old = _release(tmp_path, "old", HOST_FILES)
    new = _release(tmp_path, "new", HOST_FILES)
    client = Client(answer={"operation": {"id": "op1", "state": "pending"}})
    for pinned in (None, new, old):
        assert release_reinstall.host_restart(pinned, new, "abc1234", client) == {"state": "none"}
    assert client.calls == []


def test_a_refused_host_restart_is_uncovered_with_why(tmp_path):
    old = _release(tmp_path, "old", HOST_FILES)
    new = _release(tmp_path, "new", {**HOST_FILES, "common/bin/kd-session-host": "bin 2"})
    out = release_reinstall.host_restart(old, new, "abc1234", Client(error=CabinetError("refused", "revision moved")))
    assert out == {"state": "uncovered", "why": "revision moved"}


def _host_bin():
    import importlib.machinery
    import importlib.util

    loader = importlib.machinery.SourceFileLoader("kd_session_host_bin", str(ROOT / "common/bin/kd-session-host"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_the_host_command_pins_the_release_it_runs_from(tmp_path, monkeypatch):
    host = _host_bin()
    tree = _release(tmp_path / "releases", "abc1234-1", {"REVISION": "abc1234\n", "common/kd/x.py": ""})
    monkeypatch.setattr(host, "COMMON", str(tree / "common"))
    host.pin_release()
    assert release_reinstall.pinned_release(tmp_path / "releases" / "next") == tree
    plain = tmp_path / "checkout"
    (plain / "common").mkdir(parents=True)
    monkeypatch.setattr(host, "COMMON", str(plain / "common"))
    host.pin_release()  # a checkout, not a release: nothing written
    assert not (tmp_path / "HOST").exists()
