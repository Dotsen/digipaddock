"""C03: the worker slot pool (common/worker/worker-slots.json, kd.worker_slots)."""

import json
import os
from pathlib import Path

import pytest

from kd import worker_policy
from kd import worker_slots as ws
from kd.worker_policy import Slot

REPO = Path(__file__).resolve().parents[2]
UIDS = {"root": 0, "dev": 1001}
ROOT = "/srv/digipaddock-workers"
CRED = "/home/digi-dev/.claude/.credentials.json"
D1, D2 = Slot.parse("dev", "worker1"), Slot.parse("dev", "worker2")


@pytest.fixture(autouse=True)
def no_host_token(tmp_path, monkeypatch):
    """The host's own Claude token is not this test's: an install here looks at a path of its own, so a
    host that has the real root file (mode 600, unreadable to the test user) does not fail the pool's tests."""
    monkeypatch.setattr(ws.claude_token, "PATH", str(tmp_path / "no-host-token"))


def config_data(**override):
    data = {
        "schema_version": 3,
        "requester": "boss",
        "model": "sonnet",
        "agents": {"dev": ["worker1", "worker2"]},
        "limits": {"jobs_per_day": 12, "attempts_per_card_spec": 3, "timeout_minutes": 60},
    }
    data.update(override)
    return data


def load(tmp_path, **override):
    path = tmp_path / "worker-slots.json"
    path.write_text(json.dumps(config_data(**override)), encoding="utf-8")
    return ws.load_config(path)


def healthy(config, root=ROOT):
    wants = ws.layout(config, root)
    entries = {
        w.path: ws.Entry(w.kind, UIDS[w.owner], w.mode, len(w.content or b""))
        for w in wants
    }
    contents = {w.path: w.content for w in wants if w.content is not None}
    return entries, contents


def check(config, entries, contents, present=None, root=ROOT):
    listing = present if present is not None else {
        root: list(config.agents),
        **{f"{root}/{a}": [s.name for s in config.slots_of(a)] for a in config.agents},
    }
    return ws.problems(
        config, UIDS, root=root, look=entries.get, read=contents.get, listdir=lambda p: listing[p]
    )


def test_tracked_config_names_two_slots_of_the_developer_and_of_the_analyst():
    config = ws.load_config(REPO / "common/worker/worker-slots.json")
    assert (config.requester, config.agents) == ("boss", ("dev", "analyst"))
    assert config.slots_of("dev") == (D1, D2)
    # the analyst's pool — where the critics of a brief's council read
    assert config.slots_of("analyst") == (Slot.parse("analyst", "worker1"), Slot.parse("analyst", "worker2"))
    assert config.slots == config.slots_of("dev") + config.slots_of("analyst")
    assert all(len(config.slots_of(a)) <= ws.MAX_SLOTS for a in config.agents)


@pytest.mark.parametrize(
    "override",
    [
        {"agents": {"dev": ["worker1", "worker2", "worker3"]}},
        {"agents": {"dev": []}},
        {"agents": {}},
        {"agents": {"dev": ["worker1", "worker1"]}},
        {"agents": {"dev": ["worker0"]}},
        {"agents": {"dev": ["worker1\n"]}},
        {"agents": {"dev": ["../etc"]}},
        {"agents": {"dev": [["worker1"]]}},
        {"agents": {"dev": "worker1"}},
        {"agents": ["dev"]},
        {"agents": {"boss": ["worker1"]}},
        {"agents": {"Dev": ["worker1"]}},
        {"agents": {"../x": ["worker1"]}},
        {"requester": "Boss"},
        {"schema_version": 1},
        {"limits": {"jobs_per_day": 12, "attempts_per_card_spec": 3}},
        {"limits": {"jobs_per_day": 0, "attempts_per_card_spec": 3, "timeout_minutes": 60}},
        {"limits": {"jobs_per_day": True, "attempts_per_card_spec": 3, "timeout_minutes": 60}},
        {"limits": {"jobs_per_day": 12, "attempts_per_card_spec": 3, "timeout_minutes": 999}},
        {"extra": 1},
    ],
)
def test_a_bad_config_is_refused(tmp_path, override):
    with pytest.raises(ws.SlotError):
        load(tmp_path, **override)


def test_two_agents_each_get_their_own_slots(tmp_path):
    config = load(tmp_path, agents={"dev": ["worker1", "worker2"], "analyst": ["worker1"]})
    assert config.agents == ("dev", "analyst")
    assert config.slots == (D1, D2, Slot.parse("analyst", "worker1"))
    assert config.slots_of("analyst") == (Slot.parse("analyst", "worker1"),)


def test_layout_is_folders_and_root_placeholders_under_each_agents_pool(tmp_path):
    config = load(tmp_path, agents={"dev": ["worker1", "worker2"], "analyst": ["worker1"]})
    wants = ws.layout(config, ROOT)
    by_path = {w.path: w for w in wants}
    assert (by_path[ROOT].owner, by_path[ROOT].mode) == ("root", 0o755)
    for agent in config.agents:
        assert (by_path[f"{ROOT}/{agent}"].owner, by_path[f"{ROOT}/{agent}"].mode) == ("root", 0o755)
    for slot in config.slots:
        assert (by_path[slot.root].owner, by_path[slot.root].mode) == ("root", 0o755)
        for folder in (slot.work, slot.repo, slot.tmp):
            want = by_path[folder]
            assert (want.kind, want.owner, want.mode) == ("dir", slot.agent, 0o700)
        md = by_path[f"{slot.work}/CLAUDE.md"]
        assert (md.kind, md.owner, md.mode) == ("file", "root", 0o644)
        assert md.content == ws.PLACEHOLDER.encode()
        assert by_path[f"{slot.work}/CLAUDE.local.md"].content == b""
    assert all(w.mode & 0o022 == 0 for w in wants)
    paths = [w.path for w in wants]
    assert paths.index(f"{ROOT}/dev") < paths.index(D1.root) < paths.index(D1.work) < paths.index(D1.repo)
    assert all(p == ROOT or any(p == f"{ROOT}/{a}" or p.startswith(f"{ROOT}/{a}/worker") for a in config.agents) for p in paths)


def test_layout_matches_the_worker_policy_slot_paths(tmp_path):
    config = load(tmp_path)
    paths = {w.path for w in ws.layout(config)}
    for slot in config.slots:
        same = worker_policy.Slot.parse(slot.agent, slot.name)
        assert {same.root, same.work, same.repo, same.tmp} <= paths


def test_a_healthy_pool_has_no_problems(tmp_path):
    config = load(tmp_path)
    assert check(config, *healthy(config)) == []


def test_every_drift_from_the_layout_is_reported(tmp_path):
    config = load(tmp_path)
    entries, contents = healthy(config)
    del entries[D1.tmp]
    entries[D1.repo] = ws.Entry("dir", 0, 0o700, 0)
    entries[D2.work] = ws.Entry("dir", 1001, 0o777, 0)
    contents[f"{D2.work}/CLAUDE.md"] = b"new rules"
    entries[f"{D2.work}/CLAUDE.local.md"] = ws.Entry("link", 1001, 0o777, 9)
    found = check(config, entries, contents)
    assert f"{D1.tmp}: missing" in found
    assert f"{D1.repo}: owner is not dev" in found
    assert f"{D2.work}: mode 777, expected 700" in found
    assert f"{D2.work}/CLAUDE.md: placeholder changed" in found
    assert f"{D2.work}/CLAUDE.local.md: not a regular file" in found


def test_a_linked_checkout_and_unregistered_entries_are_reported(tmp_path):
    config = load(tmp_path)
    entries, contents = healthy(config)
    entries[f"{D1.repo}/.git"] = ws.Entry("dir", 1001, 0o755, 0)
    entries[f"{D2.repo}/.git"] = ws.Entry("file", 1001, 0o644, 40)
    present = {ROOT: ["dev", "sysadmin"], f"{ROOT}/dev": ["worker1", "worker2", "worker3"]}
    found = check(config, entries, contents, present=present)
    assert any(p.startswith(f"{D2.repo}/.git: not a directory") for p in found)
    assert not any("worker1/work/repo/.git" in p for p in found)
    assert f"{ROOT}/dev/worker3: not a registered slot" in found
    assert f"{ROOT}/sysadmin: not a registered pool" in found


def test_a_missing_pool_is_reported_as_missing(tmp_path):
    config = load(tmp_path)

    def gone(_):
        raise FileNotFoundError

    found = ws.problems(config, UIDS, root=ROOT, look={}.get, read={}.get, listdir=gone)
    assert f"{ROOT}: missing" in found
    assert not any("not a registered" in p for p in found)


@pytest.mark.parametrize(
    "entry, needle",
    [
        (None, "missing"),
        (ws.Entry("link", 1001, 0o777, 10), "not a regular file"),
        (ws.Entry("file", 0, 0o600, 10), "not owned by dev"),
        (ws.Entry("file", 1001, 0o640, 10), "open to others"),
        (ws.Entry("file", 1001, 0o600, 0), "empty"),
    ],
)
def test_credential_preflight_refuses_a_missing_or_loose_login(entry, needle):
    found = ws.credential_problems("/home/digi-dev", 1001, "dev", look={CRED: entry}.get)
    assert any(needle in p for p in found), found


def test_the_login_hint_names_the_hosts_token_first():
    (line,) = ws.credential_problems("/home/digi-dev", 1001, "dev", look={}.get)
    assert "put the host's Claude token (kd-secret put claude-token)" in line
    assert line.endswith("or log in as dev (sudo -iu digi-dev env DISABLE_AUTOUPDATER=1 claude, then /login)")


def test_credential_preflight_passes_a_private_login_by_metadata_only():
    seen = []

    def look(path):
        seen.append(path)
        return ws.Entry("file", 1001, 0o600, 420) if path == CRED else None

    assert ws.credential_problems("/home/digi-dev", 1001, "dev", look=look) == []
    assert seen == [ws.claude_token.PATH, CRED]


def test_the_hosts_claude_token_is_the_login():
    """a root file, mode 600, not empty — the job authenticates with it; the user's login is not needed."""
    token = {ws.claude_token.PATH: ws.Entry("file", 0, 0o600, 120)}
    assert ws.credential_problems("/home/digi-dev", 1001, "dev", look=token.get, token_problem=lambda: "") == []


def test_a_broken_host_token_is_named_not_passed_over():
    """A token file there but loose or malformed would still reach the job (and override the login) —
    the preflight names it; its content is checked only when its metadata is right."""
    for loose in (ws.Entry("file", 0, 0o644, 120), ws.Entry("file", 1001, 0o600, 120), ws.Entry("link", 0, 0o777, 9),
                  ws.Entry("file", 0, 0o600, 0)):
        (line,) = ws.credential_problems("/home/digi-dev", 1001, "dev", look={ws.claude_token.PATH: loose}.get,
                                         token_problem=lambda: pytest.fail("content read of a loose file"))
        assert line.startswith(ws.claude_token.PATH) and "put it again: kd-secret put claude-token" in line, loose
    token = {ws.claude_token.PATH: ws.Entry("file", 0, 0o600, 120), CRED: ws.Entry("file", 1001, 0o600, 420)}
    (line,) = ws.credential_problems("/home/digi-dev", 1001, "dev", look=token.get, token_problem=lambda: "no value")
    assert line == f"{ws.claude_token.PATH}: no value — put it again: kd-secret put claude-token"


def test_enabled_lists_only_registered_slots(tmp_path):
    config = load(tmp_path, agents={"dev": ["worker1"]})
    path = tmp_path / "worker-slots.enabled"
    assert ws.enabled(config, str(path)) == ()
    path.write_text("dev/worker1\ndev/worker2\nanalyst/worker1\n", encoding="utf-8")
    assert ws.enabled(config, str(path)) == (D1,)
    path.write_text("", encoding="utf-8")
    assert ws.enabled(config, str(path)) == ()


@pytest.mark.parametrize("line", ["worker1", "dev/../etc", "dev/worker1/x", "Dev/worker1"])
def test_an_enabled_file_with_a_foreign_line_is_refused(tmp_path, line):
    config = load(tmp_path)
    path = tmp_path / "worker-slots.enabled"
    path.write_text(f"dev/worker1\n{line}\n", encoding="utf-8")
    with pytest.raises(ws.SlotError):
        ws.enabled(config, str(path))


def registry(tmp_path, data):
    path = tmp_path / "agents.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def test_the_agent_user_comes_only_from_an_installed_agent(tmp_path):
    config = load(tmp_path)
    good = {"dev": {"user": "digi-dev", "runtime": "generated", "telegram": True}}
    assert ws.agent_user(config, "dev", registry(tmp_path, good)) == "digi-dev"
    for data in (
        {},
        {"dev": {"user": "digi-dev", "runtime": "legacy"}},
        {"dev": {"runtime": "generated"}},
    ):
        with pytest.raises(ws.SlotError):
            ws.agent_user(config, "dev", registry(tmp_path, data))
    with pytest.raises(ws.SlotError, match="no slots"):
        ws.agent_user(config, "analyst", registry(tmp_path, good))


LINUX = pytest.mark.skipif(
    os.name == "nt" or not hasattr(os, "fchown"), reason="dir_fd and fchown: Linux"
)


def pool(tmp_path, login=True):
    (tmp_path / "etc").mkdir()
    paths = ws.Paths(
        root=str(tmp_path / "workers"),
        enabled=str(tmp_path / "etc" / "worker-slots.enabled"),
        state=str(tmp_path / "lib" / "worker"),
        audit=str(tmp_path / "log" / "worker"),
    )
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    if login:
        cred = home / ".claude" / ".credentials.json"
        cred.write_text('{"x": 1}', encoding="utf-8")
        os.chmod(cred, 0o600)
    me = (os.getuid(), os.getgid())
    return paths, {"root": me, "base": me, "dev": me}, {"dev": str(home)}


@LINUX
def test_install_builds_the_pool_and_enables_it(tmp_path):
    config = load(tmp_path)
    paths, ids, homes = pool(tmp_path)
    changed, left = ws.install(config, ids, homes, paths)
    assert changed > 0 and left == []
    assert ws.enabled(config, paths.enabled) == (D1, D2)
    assert os.stat(paths.results).st_mode & 0o777 == 0o700 and os.stat(paths.events).st_mode & 0o777 == 0o700
    assert Path(paths.enabled).read_text("utf-8") == "dev/worker1\ndev/worker2\n"
    assert ws.problems(config, {k: v[0] for k, v in ids.items()}, root=paths.root) == []
    work = Path(paths.root) / "dev" / "worker1" / "work"
    assert os.stat(work).st_mode & 0o777 == 0o700
    assert (work / "CLAUDE.md").read_bytes() == ws.PLACEHOLDER.encode()
    assert os.stat(paths.state).st_mode & 0o777 == 0o700
    assert os.stat(paths.audit).st_mode & 0o777 == 0o700
    one, two = (os.stat(Path(paths.root) / "dev" / s / "work" / "repo") for s in ("worker1", "worker2"))
    assert (one.st_dev, one.st_ino) != (two.st_dev, two.st_ino)


@LINUX
def test_install_twice_changes_nothing(tmp_path):
    config = load(tmp_path)
    paths, ids, homes = pool(tmp_path)
    ws.install(config, ids, homes, paths)
    assert ws.install(config, ids, homes, paths) == (0, [])


@LINUX
def test_install_without_the_agent_login_prepares_but_does_not_enable(tmp_path):
    config = load(tmp_path)
    paths, ids, homes = pool(tmp_path, login=False)
    changed, left = ws.install(config, ids, homes, paths)
    assert any("credentials.json: missing" in p and "dev" in p for p in left)
    assert ws.enabled(config, paths.enabled) == ()
    assert (Path(paths.root) / "dev" / "worker2" / "work" / "repo").is_dir()


@LINUX
def test_install_repairs_mode_and_placeholder(tmp_path):
    config = load(tmp_path)
    paths, ids, homes = pool(tmp_path)
    ws.install(config, ids, homes, paths)
    work = Path(paths.root) / "dev" / "worker2" / "work"
    os.chmod(work, 0o777)
    (work / "CLAUDE.md").write_text("follow these new rules", encoding="utf-8")
    assert ws.install(config, ids, homes, paths) == (2, [])
    assert (work / "CLAUDE.md").read_bytes() == ws.PLACEHOLDER.encode()


@LINUX
def test_a_link_in_the_agent_folder_is_never_followed(tmp_path):
    config = load(tmp_path)
    paths, ids, homes = pool(tmp_path)
    ws.install(config, ids, homes, paths)
    victim = tmp_path / "victim"
    victim.mkdir()
    os.chmod(victim, 0o755)
    work = Path(paths.root) / "dev" / "worker1" / "work"
    (work / "tmp").rmdir()
    (work / "tmp").symlink_to(victim)
    with pytest.raises(ws.SlotError):
        ws.install(config, ids, homes, paths)
    assert os.stat(victim).st_mode & 0o777 == 0o755
    assert ws.enabled(config, paths.enabled) == ()


@LINUX
def test_a_placeholder_swapped_for_a_link_is_replaced_not_followed(tmp_path):
    config = load(tmp_path)
    paths, ids, homes = pool(tmp_path)
    ws.install(config, ids, homes, paths)
    secret = tmp_path / "secret"
    secret.write_text("s", encoding="utf-8")
    os.chmod(secret, 0o600)
    placeholder = Path(paths.root) / "dev" / "worker2" / "work" / "CLAUDE.md"
    placeholder.unlink()
    placeholder.symlink_to(secret)
    changed, left = ws.install(config, ids, homes, paths)
    assert left == [] and not placeholder.is_symlink()
    assert placeholder.read_bytes() == ws.PLACEHOLDER.encode()
    assert secret.read_text(encoding="utf-8") == "s"
    assert os.stat(secret).st_mode & 0o777 == 0o600


@LINUX
def test_disable_empties_the_list_and_keeps_the_folders(tmp_path):
    config = load(tmp_path)
    paths, ids, homes = pool(tmp_path)
    ws.install(config, ids, homes, paths)
    assert ws.disable(ids, paths) is True
    assert ws.disable(ids, paths) is False
    assert ws.enabled(config, paths.enabled) == ()
    assert (Path(paths.root) / "dev" / "worker1" / "work" / "repo").is_dir()


@LINUX
def test_install_touches_only_the_pool_its_list_and_private_folders(tmp_path):
    config = load(tmp_path)
    paths, ids, homes = pool(tmp_path)
    before = set(tmp_path.rglob("*"))
    ws.install(config, ids, homes, paths)
    allowed = [Path(p) for p in (paths.root, paths.enabled, paths.state, paths.audit)]
    allowed += [tmp_path / "lib", tmp_path / "log"]
    new = set(tmp_path.rglob("*")) - before
    assert new and all(any(p == a or a in p.parents for a in allowed) for p in new)


def test_cli_refuses_a_non_root_caller_and_unknown_flags():
    assert ws.main(["--install"], euid=1000) == 2
    with pytest.raises(SystemExit) as exc:
        ws.main(["--install", "--force"], euid=0)
    assert exc.value.code == 2


def test_the_installer_adds_no_user_unit_or_privilege():
    text = (REPO / "common/kd/worker_slots.py").read_text("utf-8")
    text += (REPO / "common/install/worker_slots.sh").read_text("utf-8")
    for word in ("useradd", "adduser", "/etc/sudoers.d", "start-agent", "setfacl", "systemctl", "polkit"):
        assert word not in text, word


def test_the_shell_entry_runs_the_module_isolated():
    text = (REPO / "common/install/worker_slots.sh").read_text("utf-8")
    assert "/usr/bin/python3 -I -c" in text
    assert "from kd.worker_slots import main" in text
    assert "--check | --install | --disable" in text


# --- C05: worker model, base-owned state ---------------------------------------------------

def test_schema_3_names_the_worker_model(tmp_path):
    assert load(tmp_path).model == "sonnet"
    with pytest.raises(ws.SlotError):
        load(tmp_path, model="--model")
    with pytest.raises(ws.SlotError):
        load(tmp_path, schema_version=2)


def test_tracked_config_is_schema_3_with_a_model():
    assert ws.load_config(REPO / "common/worker/worker-slots.json").model == "sonnet"


def test_private_folders_belong_to_the_base_user_and_hold_results_and_events():
    wants = {w.path: w for w in ws.private_layout(ws.Paths())}
    assert (wants[ws.STATE_DIR].owner, wants[ws.STATE_DIR].mode) == ("base", 0o700)
    assert (wants[ws.RESULTS].owner, wants[ws.RESULTS].mode) == ("base", 0o700)
    assert (wants[ws.AUDIT_DIR].owner, wants[ws.AUDIT_DIR].mode) == ("base", 0o700)
    assert (wants[ws.EVENTS].owner, wants[ws.EVENTS].mode) == ("root", 0o700)


def test_private_layout_has_no_job_table_or_journal():
    # jobs live in the cabinet; the C04 table and journal stay on the host as an archive
    paths = {w.path for w in ws.private_layout(ws.Paths())}
    assert not any(p.endswith(("jobs.json", "jobs.json.lock", "worker.jsonl")) for p in paths)
    assert not hasattr(ws, "TABLE") and not hasattr(ws, "JOURNAL") and not hasattr(ws, "polkit_rule")


@LINUX
def test_install_keeps_the_c04_job_table_as_an_archive(tmp_path):
    config = load(tmp_path)
    paths, ids, homes = pool(tmp_path)
    Path(paths.state).mkdir(parents=True)
    table = Path(paths.state) / "jobs.json"
    table.write_text('{"jobs": {}}', encoding="utf-8")
    os.chmod(table, 0o644)
    changed, left = ws.install(config, ids, homes, paths)
    assert left == [] and changed > 0
    assert table.read_text("utf-8") == '{"jobs": {}}' and os.stat(table).st_mode & 0o777 == 0o644
    assert ws.install(config, ids, homes, paths) == (0, [])
