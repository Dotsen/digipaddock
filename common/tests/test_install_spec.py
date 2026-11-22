"""The installation an overlay declares: host/install.json and host/projects.json."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from kd import install_spec, overlay

REPO = Path(__file__).resolve().parents[2]
HOST = REPO / "host"
ROLES = {"analyst", "boss", "dev", "sysadmin"}


def _install(tmp_path, **change):
    data = json.loads((HOST / "install.example.json").read_text(encoding="utf-8"))
    for key, value in change.items():
        if value is None:
            del data[key]
        else:
            data[key] = value
    path = tmp_path / "install.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.mark.parametrize("change", [
    {"docker": True},
    {"roles": {"ghost": {"slots": 1}}},
    {"roles": {"dev": {"slots": 0}}},
    {"roles": {"dev": {"slots": True}}},
    {"components": ["docker"]},
    {"pipeline": "turbo"},
    {"naming": {"prefix": "digi", "commands": "kd", "cards": "kd"}},
    {"naming": {"prefix": "digi", "commands": "digi", "cards": "KD"}},
    {"naming": {"prefix": "kd", "commands": "acme", "cards": "KD"}},  # users kd-dev next to the repository's kd-task
    {"naming": {"prefix": "digi", "commands": "git", "cards": "KD"}},  # a prefix of the host's own tools
    {"hindsight": "yes"},
    {"behind_nat": None},
    {"schema_version": 2},
])
def test_install_spec_refusals(tmp_path, change):
    with pytest.raises(install_spec.InstallSpecError):
        install_spec.load_install(_install(tmp_path, **change), known_roles=ROLES)


def test_examples_are_valid():
    spec = install_spec.load_install(HOST / "install.example.json", known_roles=ROLES)
    assert spec.roles == {"analyst": 1, "boss": 1, "dev": 2, "sysadmin": 1}
    assert spec.naming == {"prefix": "digi", "commands": "kd", "cards": "KD"}
    assert spec.pipeline == "enforce" and spec.hindsight is True and spec.behind_nat is False
    projects = install_spec.load_projects(HOST / "projects.example.json", roles=spec.roles)
    assert projects["admin"].route == "@sysadmin" and projects["admin"].repo is None
    assert install_spec.release_project(projects) == "digipaddock"


def test_the_code_trees_roles_are_the_known_ones():
    assert install_spec.known_roles(REPO) == ROLES


def test_projects_refuse_a_route_to_a_role_the_host_has_not(tmp_path):
    data = json.loads((HOST / "projects.example.json").read_text(encoding="utf-8"))
    data["projects"]["admin"]["route"] = "@analyst"
    path = tmp_path / "projects.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(install_spec.InstallSpecError, match="admin"):
        install_spec.load_projects(path, roles={"dev": 2, "sysadmin": 1})
    data["projects"]["admin"]["route"] = "@boss"  # the coordinator never takes a project's cards
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(install_spec.InstallSpecError):
        install_spec.load_projects(path, roles={"boss": 1, "dev": 2, "sysadmin": 1})


def test_projects_refuse_a_bad_passport(tmp_path):
    data = json.loads((HOST / "projects.example.json").read_text(encoding="utf-8"))
    data["projects"]["digipaddock"]["repo"]["dir"] = "../escape"
    path = tmp_path / "projects.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(install_spec.InstallSpecError, match="digipaddock"):
        install_spec.load_projects(path)


def test_repos_json_matches_add_repo_passport(tmp_path):
    sys.path.insert(0, str(REPO / "agents" / "dev"))
    from dev_agent import repos

    projects = install_spec.load_projects(HOST / "projects.example.json")
    data = install_spec.repos_json(projects)
    assert set(data) == {"digipaddock", "digipaddock-deploy"}
    path = tmp_path / "repos.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    loaded = repos.load(str(path))
    assert loaded["digipaddock"].release == "digipaddock" and loaded["digipaddock-deploy"].role == "overlay"
    assert loaded["digipaddock"].profiles == (("unit", True),)


RELEASE_FILES = ("agents/sysadmin/inventory.json", "agents/sysadmin/monitor/config.json",
                 "agents/sysadmin/operations.json", "agents/sysadmin/playbooks/security.md",
                 "agents/sysadmin/servers.md", "common/install/forgejo_known_hosts", "host/private_scan.json")


def _overlay_tree(tmp_path, release=True):
    tree = tmp_path / "overlay"
    (tree / "host").mkdir(parents=True)
    for name in ("host", "overlay", "install", "projects"):
        shutil.copy(HOST / "{}.example.json".format(name), tree / "host" / "{}.json".format(name))
    for rel in RELEASE_FILES if release else ():
        (tree / rel).parent.mkdir(parents=True, exist_ok=True)
        (tree / rel).write_text('{"literals": ["x"]}' if rel.endswith(".json") else "x\n", encoding="utf-8")
    return tree


def test_overlay_check_names_the_files_a_release_needs(tmp_path):
    """kd-release refuses an overlay without them — check says so before a push."""
    tree = _overlay_tree(tmp_path, release=False)
    out = _cli("check", str(tree))
    assert out.returncode == 3
    for rel in RELEASE_FILES:
        assert rel in out.stdout, rel
    assert overlay.check_overlay(tree) == []  # a release's own validation of the overlay is unchanged
    init = _cli("init", str(tmp_path / "new"))
    assert init.returncode == 0 and "common/install/forgejo_known_hosts" in init.stdout  # said at once
    assert not (tmp_path / "new" / "scripts").exists()  # no code of the overlay's, the scanner is the tree's


def _cli(*args):
    return subprocess.run([sys.executable, "-I", str(REPO / "common" / "kd" / "overlay.py"), *args],
                          capture_output=True, text=True, timeout=60)


def test_overlay_check_cli_codes(tmp_path):
    tree = _overlay_tree(tmp_path)
    ok = _cli("check", str(tree))
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "overlay: ok" in ok.stdout
    (tree / "host" / "install.json").write_text('{"schema_version": 1, "roles": {}}', encoding="utf-8")
    bad = _cli("check", str(tree))
    assert bad.returncode == 3 and "host/install.json" in bad.stdout


def test_check_overlay_reads_install_and_projects(tmp_path):
    tree = _overlay_tree(tmp_path)
    assert overlay.check_overlay(tree) == []
    data = json.loads((tree / "host" / "projects.json").read_text(encoding="utf-8"))
    data["projects"]["admin"]["done_by"] = "ghost"
    (tree / "host" / "projects.json").write_text(json.dumps(data), encoding="utf-8")
    assert any(e.startswith("host/projects.json") for e in overlay.check_overlay(tree))


def test_an_overlay_without_install_json_stays_valid(tmp_path):
    tree = _overlay_tree(tmp_path)
    (tree / "host" / "install.json").unlink()
    (tree / "host" / "projects.json").unlink()
    assert overlay.check_overlay(tree) == []


def test_overlay_init_starts_the_sysadmins_files_from_their_examples(tmp_path):
    """After init only what no example can be is missing: the forge's host key (the closed scanner and
    its list start from their examples too)."""
    target = tmp_path / "new"
    assert _cli("init", str(target)).returncode == 0
    for rel, example in (("agents/sysadmin/inventory.json", "agents/sysadmin/inventory.example.json"),
                         ("agents/sysadmin/monitor/config.json", "agents/sysadmin/monitor/config.example.json"),
                         ("agents/sysadmin/operations.json", "agents/sysadmin/operations.example.json"),
                         ("agents/sysadmin/playbooks/security.md", "agents/sysadmin/playbooks/security.example.md"),
                         ("agents/sysadmin/servers.md", "agents/sysadmin/servers.example.md"),
                         ("host/private_scan.json", "host/private_scan.example.json")):
        assert (target / rel).read_bytes() == (REPO / example).read_bytes(), rel
    check = _cli("check", str(target))
    missing = sorted(line.split()[1] for line in check.stdout.splitlines() if line.startswith("missing:"))
    assert missing == ["common/install/forgejo_known_hosts"]
    unfilled = [line for line in check.stdout.splitlines() if line.startswith("host/private_scan.json:")]
    assert len(unfilled) == 1  # the example's list is no deployment's: see the next test
    assert check.stdout.count("\n") == len(missing) + 1  # nothing else is wrong with the examples


def test_overlay_check_refuses_the_example_scanners_list(tmp_path):
    """A list still holding the example's literals refuses the release — the code tree's own
    example list names them (release: privacy). check says it before a push, and init's list is no exception."""
    tree = _overlay_tree(tmp_path)
    assert overlay.release_needs(tree) == []
    example = json.loads((HOST / "private_scan.example.json").read_text(encoding="utf-8"))["literals"]
    (tree / "host" / "private_scan.json").write_text(json.dumps({"literals": ["deploy.test", example[0]]}),
                                                     encoding="utf-8")
    said = overlay.release_needs(tree)
    assert len(said) == 1 and said[0].startswith("host/private_scan.json: ") and example[0] in said[0]
    assert _cli("check", str(tree)).returncode == 3


def test_the_example_closed_scanner_finds_its_literals(tmp_path):
    """The tree's closed scanner with the example list — kd-release runs it with --list."""
    listing = REPO / "host" / "private_scan.example.json"
    literals = json.loads(listing.read_text(encoding="utf-8"))["literals"]
    (tmp_path / "found.txt").write_text("x = 'www.{}.test'\n".format(literals[0]), encoding="utf-8")
    (tmp_path / "inside.txt").write_text("x = '1{}0'\n".format(literals[0]), encoding="utf-8")  # inside a word: no
    scan = [sys.executable, "-I", str(REPO / "common" / "kd" / "private_scan.py"), "--list", str(listing)]
    found = subprocess.run(scan + [str(tmp_path / "found.txt")], capture_output=True, text=True, timeout=30)
    assert found.returncode == 1 and found.stdout.strip().endswith("found.txt:1: " + literals[0])
    clean = subprocess.run(scan + [str(tmp_path / "inside.txt")], capture_output=True, text=True, timeout=30)
    assert clean.returncode == 0 and clean.stdout == ""
    broken = subprocess.run(scan[:-1] + [str(tmp_path / "found.txt"), str(tmp_path / "found.txt")],
                            capture_output=True, text=True, timeout=30)
    assert broken.returncode == 2  # a list that is not {"literals": [...]}


def test_overlay_init_keeps_existing_files(tmp_path):
    target = tmp_path / "new"
    (target / "host").mkdir(parents=True)
    (target / "host" / "host.json").write_text("mine\n", encoding="utf-8")
    out = _cli("init", str(target))
    assert out.returncode == 0, out.stderr
    assert (target / "host" / "host.json").read_text(encoding="utf-8") == "mine\n"
    for name in ("overlay", "install", "projects", "contours"):
        assert (target / "host" / "{}.json".format(name)).read_text(encoding="utf-8") == \
            (HOST / "{}.example.json".format(name)).read_text(encoding="utf-8")
    assert "host/host.json" in out.stdout  # kept, said so


def test_egress_is_a_component(tmp_path):
    """The probe exits (host/egress.json) install as the component egress."""
    assert "egress" in install_spec.COMPONENTS
    install = install_spec.load_install(_install(tmp_path, components=["egress"]))
    assert install.components == ("egress",)
