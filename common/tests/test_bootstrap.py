"""bootstrap.sh: the installers in order, a check before every install."""

import io
import json
import shutil
from pathlib import Path

import pytest

from kd import bootstrap, install_spec, naming

REPO = Path(__file__).resolve().parents[2]
HOST = REPO / "host"
OPT = "/opt/digipaddock"


def _install(**change):
    return install_spec.load_install(HOST / "install.example.json")._replace(**change)


def _projects():
    return install_spec.load_projects(HOST / "projects.example.json")


def _host():
    return naming.load_host(str(HOST / "host.example.json"))


WHERE = bootstrap.Where(code="/src/code", overlay="/src/overlay", scratch="/tmp/scratch")


@pytest.fixture(autouse=True)
def _no_host_naming(tmp_path, monkeypatch):
    """A host without /etc/digipaddock/naming.json unless a test writes one (the stage running these has its own)."""
    monkeypatch.setenv("KD_NAMING_JSON", str(tmp_path / "host-naming.json"))


def _steps(install=None, where=WHERE):
    return bootstrap.steps(install or _install(), _projects(), where, host=_host(), opt=OPT)


class Fake:
    """A runner: every command recorded; ``fail`` — substrings of the commands that exit 1 (a check while it is
    in ``until`` fails only until one of the install commands of its step ran)."""

    def __init__(self, fail=(), apply_fails=()):
        self.calls, self.fail, self.apply_fails, self.fixed = [], tuple(fail), tuple(apply_fails), set()

    def __call__(self, cmd, quiet=False):
        joined = " ".join(cmd)
        self.calls.append(joined)
        if any(s in joined for s in self.apply_fails):
            return 1
        return 1 if any(s in joined and s not in self.fixed for s in self.fail) else 0


def _all(steps, run, plan=False):
    out, err = io.StringIO(), io.StringIO()
    rc = bootstrap.run_phases(bootstrap.PHASES, steps, run=run, out=out, err=err, plan=plan)
    return rc, out.getvalue(), err.getvalue()


def _applies(steps):
    return {" ".join(cmd) for phase in steps.values() for step in phase for cmd in step.apply}


def test_all_on_an_installed_host_installs_nothing():
    steps = _steps()
    run = Fake()
    rc, out, err = _all(steps, run)
    assert rc == 0, err
    assert run.calls and not set(run.calls) & _applies(steps)
    for banned in ("cabinet_setup.sh install", "systemctl restart", "--install", " enable "):
        assert not any(banned in c for c in run.calls), banned
    assert not any("install" in c.split() for c in run.calls)
    assert "  ok" in out


def test_all_stops_at_the_first_manual_step():
    run = Fake(fail=["--probe secret forge-dev"])
    rc, out, err = _all(_steps(), run)
    assert rc == 3
    assert "kd-secret put forge-dev" in err and "bootstrap.sh secrets" in err
    after = run.calls[run.calls.index(next(c for c in run.calls if "secret forge-dev" in c)) + 1:]
    assert after == []  # nothing of keys, logins or check ran


def test_a_failed_check_installs_and_checks_again():
    steps = {"base": _steps()["base"]}
    run = Fake(fail=["stage_setup.sh --check"])
    stage = next(s for s in steps["base"] if "stage_setup.sh" in " ".join(s.check))
    original = run.__call__

    def fixing(cmd, quiet=False):
        rc = original(cmd, quiet)
        if "stage_setup.sh --prefix" in " ".join(cmd):
            run.fixed.add("stage_setup.sh --check")
        return rc

    out, err = io.StringIO(), io.StringIO()
    assert bootstrap.run_phases(["base"], steps, run=fixing, out=out, err=err) == 0, err.getvalue()
    check, apply = " ".join(stage.check), " ".join(stage.apply[0])
    assert run.calls[:3] == [check, apply, check]
    assert "installed" in out.getvalue()
    # still failing after the install: the owner's turn
    run = Fake(fail=["stage_setup.sh --check"])
    err = io.StringIO()
    assert bootstrap.run_phases(["base"], steps, run=run, out=io.StringIO(), err=err) == 3
    assert "bootstrap.sh base" in err.getvalue()
    # the installer itself failed
    run = Fake(fail=["stage_setup.sh --check"], apply_fails=["stage_setup.sh --prefix"])
    err = io.StringIO()
    assert bootstrap.run_phases(["base"], steps, run=run, out=io.StringIO(), err=err) == 1
    assert "failure" in err.getvalue()


def test_plan_lists_and_runs_no_apply():
    steps = _steps()
    run = Fake(fail=[""])  # every check fails
    rc, out, _ = _all(steps, run, plan=True)
    assert rc == 0
    assert not set(run.calls) & _applies(steps)
    assert "to install" in out and "manual" in out


def test_steps_follow_install_json():
    install = _install(roles={"analyst": 1, "boss": 1, "sysadmin": 1}, hindsight=False,
                       components=("transcribe", "worker-slots"))
    steps = _steps(install)
    names = [s.name for phase in steps.values() for s in phase]
    assert steps["hindsight"] == []
    assert not any("dev" == n.split()[-1] for n in names)
    secrets = " ".join(n for n in names if n.startswith("secret"))
    for absent in ("forge-dev", "forge-merge", "review", "devcheck"):
        assert absent not in secrets, absent
    applies = _applies(steps)
    assert not any("review_setup.sh" in a or "devcheck_setup.sh" in a for a in applies)
    assert any(a.endswith("transcribe_setup.sh install") for a in applies)
    assert any(a.endswith("worker_slots.sh --install") for a in applies)
    # behind_nat is read by kd_factory_host from install.json itself (Task 4): no flag
    assert not any("--behind-nat" in a for a in _applies(_steps(_install(behind_nat=True))))
    hosted = [a for a in _applies(_steps()) if "session_host_setup.sh enable dev" in a]
    assert hosted == [f"bash {OPT}/common/install/session_host_setup.sh enable dev --slots 2"]
    full = _applies(_steps())
    assert f"bash {OPT}/common/install/stage_setup.sh --prefix digi --commands kd --cards KD" in full


def test_hindsight_clients_rerun_stage_setup_only_when_missing():
    """stage_setup.sh installs the cabinet again: an installed server with its clients reruns nothing."""
    server, client = _steps()["hindsight"]
    assert [" ".join(c) for c in server.apply] == [f"bash {OPT}/common/install/hindsight_setup.sh install"]
    assert " ".join(client.check) == "test -f /home/digi/.hindsight/coding-agent.json"
    assert [" ".join(c) for c in client.apply] == [f"bash {OPT}/common/install/stage_setup.sh"]


def test_the_first_tree_comes_from_the_given_code_with_the_overlays_host():
    tree = _steps()["tree"]
    assert len(tree) == 1 and " ".join(tree[0].check) == f"test -f {OPT}/REVISION"
    assert list(tree[0].apply[0]) == [
        "env", "KD_HOST_JSON=/src/overlay/host/host.json", "/usr/bin/python3", "-I", "/src/code/common/bin/kd-release",
        "--bootstrap", "digipaddock", "--repos", "/tmp/scratch/repos.json",
        "--known-hosts", "/src/overlay/common/install/forgejo_known_hosts"]


def _opt(tmp_path, revision):
    opt = tmp_path / "opt"
    (opt / "host").mkdir(parents=True)
    shutil.copy(HOST / "install.example.json", opt / "host" / "install.json")
    shutil.copy(HOST / "projects.example.json", opt / "host" / "projects.json")
    shutil.copy(HOST / "host.example.json", opt / "host" / "host.json")
    if revision:
        (opt / "REVISION").write_text("abc1234\n", encoding="utf-8")
    return opt


def test_tree_needs_overlay_and_code_only_before_the_first_tree(tmp_path):
    out, err = io.StringIO(), io.StringIO()
    opt = _opt(tmp_path, revision=False)
    assert bootstrap.main(["tree"], run=Fake(), out=out, err=err, opt=str(opt), is_root=lambda: True) == 2
    assert "--overlay" in err.getvalue() and "--code" in err.getvalue()
    # given both: the registry for kd-release is written from projects.json
    seen = {}

    def run(cmd, quiet=False):
        if "--repos" in cmd:
            seen["repos"] = json.loads(Path(cmd[cmd.index("--repos") + 1]).read_text(encoding="utf-8"))
        return 1 if list(cmd[:2]) == ["test", "-f"] and not seen else 0

    argv = ["tree", "--overlay", str(opt), "--code", str(REPO)]
    assert bootstrap.main(argv, run=run, out=io.StringIO(), err=err, opt=str(opt), is_root=lambda: True) == 0
    assert seen["repos"] == install_spec.repos_json(_projects())
    # a tree is there: neither flag is needed, given ones are ignored with a word
    opt = _opt(tmp_path / "second", revision=True)
    err = io.StringIO()
    assert bootstrap.main(["tree"], run=Fake(), out=io.StringIO(), err=err, opt=str(opt), is_root=lambda: True) == 0
    assert bootstrap.main(argv, run=Fake(), out=io.StringIO(), err=err, opt=str(opt), is_root=lambda: True) == 0
    assert "not needed" in err.getvalue()


@pytest.mark.parametrize("argv", [[], ["deploy"], ["all", "--overlay"], ["base", "--plan", "x"],
                                  ["all", "--overlay", "a"], ["all", "--code", "b"]])
def test_usage(argv, tmp_path):
    err = io.StringIO()
    opt = _opt(tmp_path, revision=True)
    assert bootstrap.main(argv, run=Fake(), out=io.StringIO(), err=err, opt=str(opt), is_root=lambda: True) == 2
    assert "usage: bootstrap.sh" in err.getvalue()


def test_only_root(tmp_path):
    err = io.StringIO()
    opt = _opt(tmp_path, revision=True)
    assert bootstrap.main(["check"], run=Fake(), out=io.StringIO(), err=err, opt=str(opt),
                          is_root=lambda: False) == 1
    assert "root is required" in err.getvalue()


def test_check_runs_every_check_and_installs_nothing():
    steps = _steps()
    run = Fake(fail=["new_agent.sh --verify boss", "--probe hosted dev 2"])
    out, err = io.StringIO(), io.StringIO()
    assert bootstrap.run_phases(["check"], steps, run=run, out=out, err=err) == 1
    assert not set(run.calls) & _applies(steps)
    assert any("new_agent.sh --verify sysadmin" in c for c in run.calls)  # went on past the failures
    assert any(c.endswith("kd-cabinet health") for c in run.calls)
    assert "agent boss" in err.getvalue() and "dev in the session host" in err.getvalue()


def test_keys_name_where_each_public_key_goes(tmp_path):
    keys = _steps()["keys"]
    assert [s.show for s in keys][0] == ("/root/.ssh/forgejo_deploy.pub",)
    shown = " ".join(" ".join(s.show) for s in keys)
    assert "/home/digi-repo/.ssh/repo_sync_ed25519.pub" in shown  # analyst reads repositories (repo.read)
    assert "/home/digi-dev/.ssh/dev_ed25519.pub" in shown  # dev develops (git.develop)
    # final review I2: nothing on the host makes the mirror's key — its installer asks the owner for it
    assert "github_deploy" not in shown
    out = io.StringIO()
    assert bootstrap.run_phases(["keys"], {"keys": keys}, run=Fake(), out=out, err=io.StringIO()) == 0
    assert "the install makes it" in out.getvalue()  # a key not made yet


def test_agents_are_checked_by_their_installed_state():
    """Final review C1: --check only says a role could be installed — true for any role on a fresh host;
    --verify says the installed role is what this code installs."""
    agents = _steps()["agents"]
    assert [s.check for s in agents[:-1]] == [
        ("bash", f"{OPT}/common/install/new_agent.sh", "--verify", role) for role in sorted(_install().roles)]
    assert not any("new_agent.sh --check" in " ".join(s.check) for phase in _steps().values() for s in phase)


def test_an_installers_own_owner_step_stops_with_3():
    """Final review I2: an installer that exits 3 stopped at the owner's step and said which (a key to add in the
    forge): bootstrap's 3, not a failure."""
    steps = {"components": [bootstrap.Step("components", "component mirror", ("check",), (("install",),))]}
    err = io.StringIO()
    rc = bootstrap.run_phases(["components"], steps, run=lambda cmd, quiet=False: 3 if cmd == ("install",) else 1,
                              out=io.StringIO(), err=err)
    assert rc == 3
    assert "owner's step — component mirror: the installer's lines above" in err.getvalue()
    assert "failure" not in err.getvalue() and "bootstrap.sh components" in err.getvalue()


def test_components_check_their_install_and_the_owners_parts_come_with_the_logins():
    """Final review I2: kd-review's token and signer and the worker slots (their owner's Claude login) are the
    owner's; components checks only what its installers make, the rest waits for the logins."""
    steps = _steps()
    review = next(s for s in steps["components"] if s.name == "component review")
    assert review.check == ("bash", f"{OPT}/agents/dev/install/review_setup.sh", "check", "installed")
    assert not any("worker_slots.sh" in " ".join(s.check) for s in steps["components"])
    names = [s.name for s in steps["logins"]]
    # the agents' Claude login is the host's token, a secret of the phase before — no login per user
    assert not any(n.startswith("Claude Code login") for n in names)
    assert "secret claude-token" in [s.name for s in steps["secrets"]]
    assert "component worker-slots" in names and "kd-review signer" in names
    signer = steps["logins"][names.index("kd-review signer")]
    assert signer.check == ("bash", f"{OPT}/agents/dev/install/review_setup.sh", "check") and signer.apply == ()
    assert "kd-cabinet user add" in signer.manual and "kd-review signers add" in signer.manual
    bare = _steps(_install(components=("transcribe",)))
    assert not {"component worker-slots", "kd-review signer"} & {s.name for s in bare["logins"]}


class Cabinet:
    def __init__(self, items):
        self.items, self.calls = items, []

    def call(self, request):
        self.calls.append(request)
        if request["op"] == "project.list":
            return {"items": self.items}
        return {"project": {}}


def test_projects_create_the_missing_and_update_the_different():
    items = [{"id": "digipaddock", "name": "DigiPaddock", "route": "@dev", "memory_bank": "", "done_by": "dev"},
             {"id": "admin", "name": "Admin", "route": "@owner", "memory_bank": "", "done_by": "sysadmin"},
             {"id": "other", "name": "Other", "route": "@owner", "memory_bank": "", "done_by": ""}]
    cabinet = Cabinet(items)
    path = HOST / "projects.example.json"
    err = io.StringIO()
    assert bootstrap.projects("check", str(path), client=cabinet, err=err) == 1
    assert "digipaddock-deploy" in err.getvalue() and "admin" in err.getvalue()
    assert [c["op"] for c in cabinet.calls] == ["project.list"]  # check writes nothing
    cabinet.calls.clear()
    assert bootstrap.projects("apply", str(path), client=cabinet, err=io.StringIO()) == 0
    writes = [c for c in cabinet.calls if c["op"] != "project.list"]
    assert writes == [
        {"op": "project.create", "id": "digipaddock-deploy", "name": "DigiPaddock deployment", "route": "@dev"},
        {"op": "project.update", "id": "digipaddock-deploy", "done_by": "dev"},  # create takes no done_by
        {"op": "project.update", "id": "admin", "name": "Servers", "route": "@sysadmin"},
    ]
    assert not any(c.get("id") == "other" for c in writes)  # the cabinet's own projects stay


def _overlay_with(tmp_path, **install_change):
    overlay = _opt(tmp_path / "overlay", revision=False)
    path = overlay / "host" / "install.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(install_change)
    path.write_text(json.dumps(data), encoding="utf-8")
    return overlay


def test_a_role_outside_the_code_tree_refuses_before_any_command(tmp_path):
    """install.json is read with the code tree's agents — a role it lacks refuses before apt-get."""
    roles = dict(json.loads((HOST / "install.example.json").read_text(encoding="utf-8"))["roles"], ghost={"slots": 1})
    overlay = _overlay_with(tmp_path, roles=roles)
    run, err = Fake(), io.StringIO()
    argv = ["all", "--overlay", str(overlay), "--code", str(REPO)]
    assert bootstrap.main(argv, run=run, out=io.StringIO(), err=err, opt=str(tmp_path / "opt"),
                          is_root=lambda: True) == 3
    assert run.calls == []
    assert "roles.ghost" in err.getvalue() and "not an agent of the code tree" in err.getvalue()


def test_the_rerun_hint_before_the_first_tree_names_the_overlay_and_the_code(tmp_path):
    """Before the first tree the phase cannot run without --overlay and --code — the hint says them."""
    overlay = _opt(tmp_path / "overlay", revision=False)
    argv = ["all", "--overlay", str(overlay), "--code", str(REPO)]
    again = f"bootstrap.sh preflight --overlay {overlay} --code {REPO}"
    err = io.StringIO()  # the check still fails after the install: the owner's step
    assert bootstrap.main(argv, run=Fake(fail=["prereq.sh check"]), out=io.StringIO(), err=err,
                          opt=str(tmp_path / "opt"), is_root=lambda: True) == 3
    assert again in err.getvalue()
    err = io.StringIO()  # the installer failed
    assert bootstrap.main(argv, run=Fake(fail=["prereq.sh check"], apply_fails=["prereq.sh install"]),
                          out=io.StringIO(), err=err, opt=str(tmp_path / "opt"), is_root=lambda: True) == 1
    assert again in err.getvalue()
    # once there is a tree the phase reads everything from it: no flags
    opt = _opt(tmp_path / "second", revision=True)
    err = io.StringIO()
    assert bootstrap.main(["base"], run=Fake(fail=["stage_setup.sh --check"]), out=io.StringIO(), err=err,
                          opt=str(opt), is_root=lambda: True) == 3
    assert "then again: bootstrap.sh base\n" in err.getvalue()


def test_a_plan_before_the_first_tree_checks_nothing_of_the_tree(tmp_path):
    """On an empty host the steps after tree have no tree to run from — the plan says them
    without running their checks, and its check phase runs nothing."""
    overlay = _opt(tmp_path / "overlay", revision=False)
    opt = tmp_path / "opt"
    run, out, err = Fake(fail=[""]), io.StringIO(), io.StringIO()
    argv = ["all", "--plan", "--overlay", str(overlay), "--code", str(REPO)]
    assert bootstrap.main(argv, run=run, out=out, err=err, opt=str(opt), is_root=lambda: True) == 0
    of_tree = [c for c in run.calls if str(opt) in c or OPT in c]
    assert run.calls and of_tree == [f"test -f {opt}/REVISION"], of_tree  # only the tree step's own check
    assert "  failed" not in err.getvalue()
    text = out.getvalue()
    assert "to install  foundation" in text and "manual      secret forge-dev" in text
    assert text.index("== preflight") < text.index("to install  foundation") < text.index("== check")
    # with a tree the plan checks every phase as before
    tree = _opt(tmp_path / "tree", revision=True)
    run = Fake(fail=[""])
    assert bootstrap.main(["all", "--plan"], run=run, out=io.StringIO(), err=io.StringIO(), opt=str(tree),
                          is_root=lambda: True) == 0
    assert any("stage_setup.sh --check" in c for c in run.calls)


def test_a_key_not_made_yet_is_said_without_naming_a_wrong_phase(tmp_path):
    """Root's key comes with preflight, not «after the agents and components phases»."""
    out = io.StringIO()
    bootstrap._show([str(tmp_path / "forgejo_deploy.pub")], out)
    assert out.getvalue() == f"    {tmp_path / 'forgejo_deploy.pub'} — not there yet, the install makes it\n"


def test_install_json_names_must_be_the_hosts(tmp_path, monkeypatch):
    """The names are the host's from its first install — install.json naming others refuses."""
    opt = _opt(tmp_path, revision=True)  # install.example.json: digi, kd, KD
    host = tmp_path / "naming.json"
    monkeypatch.setenv("KD_NAMING_JSON", str(host))
    host.write_text(json.dumps({"schema_version": 1, "prefix": "kit", "commands": "kd", "cards": "KD",
                                "card_aliases": ["OLD"]}), encoding="utf-8")
    run, err = Fake(), io.StringIO()
    assert bootstrap.main(["check"], run=run, out=io.StringIO(), err=err, opt=str(opt), is_root=lambda: True) == 3
    assert run.calls == []
    assert "prefix digi" in err.getvalue() and "prefix kit" in err.getvalue() and str(host) in err.getvalue()
    host.write_text(json.dumps({"schema_version": 1, "prefix": "digi", "commands": "kd", "cards": "KD",
                                "card_aliases": ["OLD"]}), encoding="utf-8")
    assert bootstrap.main(["check"], run=Fake(), out=io.StringIO(), err=io.StringIO(), opt=str(opt),
                          is_root=lambda: True) == 0


def test_check_runs_the_overlay_check_on_the_release_tree():
    """The check phase checks the release tree's overlay as kd-release would, before the health."""
    checks = [" ".join(s.check) for s in _steps()["check"]]
    assert f"/usr/bin/python3 -I {OPT}/common/kd/overlay.py check {OPT}" in checks
    assert checks[-1] == f"/usr/bin/python3 -I {OPT}/common/bin/kd-cabinet health"


def test_wrapper_is_root_only_and_isolated():
    text = (REPO / "common" / "install" / "bootstrap.sh").read_text(encoding="utf-8")
    assert '[ "$(id -u)" = 0 ] || { echo "root is required" >&2; exit 1; }' in text
    assert 'exec /usr/bin/python3 -I "$TREE/common/kd/bootstrap.py" "$@"' in text
    assert text.index('"$(id -u)" = 0') < text.index("exec /usr/bin/python3")


def test_egress_component_runs_its_installer():
    steps = _steps(_install(components=("egress",)))
    applies = _applies(steps)
    assert any(a.endswith("egress_setup.sh install") for a in applies)
    checks = {" ".join(s.check) for phase in steps.values() for s in phase}
    assert any(c.endswith("egress_setup.sh check") for c in checks)
