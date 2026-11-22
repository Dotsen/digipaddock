import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from dev_agent import repos

INSTALL = Path(__file__).resolve().parents[1] / "install"
ROOT = Path(__file__).resolve().parents[3]
TEXT = (INSTALL / "add_repo.sh").read_text(encoding="utf-8")
UTF8 = {**os.environ, "PYTHONIOENCODING": "utf-8"}  # Cyrillic in stderr reads the same on Windows and on Linux


def _heredoc(marker: str, opener: str) -> str:
    """Read the embedded Python between the quoted heredoc opener and its delimiter so the test can run it verbatim."""
    return TEXT.split(opener, 1)[1].split(f"\n{marker}\n", 1)[0]


def _bash():
    """A working bash: Git Bash on Windows (as in tests/test_shell_syntax.py)."""
    for candidate in (r"C:\Program Files\Git\bin\bash.exe", shutil.which("bash")):
        if candidate and Path(candidate).exists():
            try:
                if subprocess.run([candidate, "-c", "true"], capture_output=True, timeout=10).returncode == 0:
                    return candidate
            except (OSError, subprocess.TimeoutExpired):
                pass
    return None


def test_validates_args_and_board_project():
    assert "^[a-z][a-z0-9-]{0,31}$" in TEXT
    assert "<<'BOARD' || exit 2" in TEXT
    assert "config.yml" not in TEXT


def test_bash_checks_match_registry_regexes():
    # add_repo.sh must not write what repos.load rejects: otherwise the whole registry breaks, not one project
    for rx in (repos.PROJECT_RE, repos.REPO_RE, repos.DIR_RE):
        assert f"^{rx.pattern}$" in TEXT, rx.pattern
    assert 'release" = digipaddock' in TEXT and repos.RELEASES == {"digipaddock"}


@pytest.mark.parametrize("name,code", [
    ("DigiPaddock", 0), ("a.b-c_d", 0), (".claude", 0), ("x" * 64, 0),
    ("x" * 65, 2), ("..", 2), (".", 2), ("a..b", 2),
])
def test_clone_dir_check_agrees_with_registry(name, code):
    bash = _bash()
    if bash is None:
        pytest.skip("нет рабочего bash")
    line = next(ln for ln in TEXT.splitlines() if ln.startswith("[[ $dir =~"))
    p = subprocess.run([bash, "-c", f"dir=work/$1; name=$1\n{line}\nexit 0", "_", name],
                       capture_output=True, encoding="utf-8")
    assert p.returncode == code, p.stderr
    if code == 0:  # what bash let through, the registry accepts too
        assert repos.DIR_RE.fullmatch(f"work/{name}") and ".." not in name and name != "."


def _board_check(project, sock, monkeypatch=None, items=None):
    """The BOARD snippet as add_repo.sh runs it (argv: project, common, socket); ``items`` — the cabinet's
    project.list answer through a stand-in client. The exit text, or None when the project is on the board."""
    from kd.cabinet import client

    if items is not None:
        class Answer:
            def __init__(self, path):
                assert path == sock

            def call(self, request):
                assert request == {"op": "project.list"}
                return {"projects": {p["id"]: p["route"] for p in items}, "items": items}

        monkeypatch.setattr(client, "Client", Answer)
    snippet = _heredoc("BOARD", "<<'BOARD' || exit 2\n")
    saved = sys.argv
    sys.argv = ["-", project, str(ROOT / "common"), sock]
    try:
        exec(compile(snippet, "add_repo.sh:BOARD", "exec"), {"__name__": "__main__"})
    except SystemExit as exc:
        return str(exc.code)
    finally:
        sys.argv = saved
    return None


def test_board_project_is_asked_from_the_cabinet():
    # board projects are the cabinet's table, there is no routes.json any more
    assert "python3 -I - \"$project\" /opt/digipaddock/common /run/digipaddock-cabinet/local.sock <<'BOARD' || exit 2" in TEXT
    assert "routes.json" not in TEXT


@pytest.mark.parametrize("project,known", [("digipaddock", True), ("alert", True), ("her", False), ("nope", False)])
def test_board_project_check_against_cabinet_projects(monkeypatch, project, known):
    items = [{"id": "digipaddock", "name": "DIGIPADDOCK", "route": "@dev", "repo": ""},
             {"id": "alert", "name": "Alert.Example.Com", "route": "@owner", "repo": ""}]
    found = _board_check(project, "/run/digipaddock-cabinet/local.sock", monkeypatch, items)
    if known:
        assert found is None
    else:
        assert f"project {project} is not among" in found


def test_board_project_check_fails_loudly_without_cabinet(tmp_path):
    # a cabinet that did not answer is an explicit error, not "no such project" and not an empty list
    found = _board_check("digipaddock", str(tmp_path / "absent.sock"))
    assert found.startswith("the cabinet did not answer: the board's projects are not read")


def test_clone_is_done_as_agent_user_from_registry_with_bot_key_and_pinned_host_key():
    # dev runs as digi-dev; the user comes from /etc/digipaddock/agents.json, it is not hard-coded
    assert 'agents.user_of("dev")' in TEXT and "/opt/digipaddock/common" in TEXT
    assert 'id "$user" >/dev/null 2>&1' in TEXT
    assert 'key=/home/$user/.ssh/dev_ed25519' in TEXT
    assert 'runuser -u "$user" -- bash -euc' in TEXT and "runuser -u digi --" not in TEXT
    assert "/home/digi/.ssh" not in TEXT
    assert 'ssh -i $key -o IdentitiesOnly=yes' in TEXT
    assert "UserKnownHostsFile=/opt/digipaddock/common/install/forgejo_known_hosts" in TEXT
    assert "git clone" in TEXT
    # root's cwd (/root 0700) is closed to the agent's user — the runuser block goes to / first
    block = TEXT.split('runuser -u "$user" -- bash -euc', 1)[1]
    assert block.index("cd /") < block.index("git clone")


def test_ssh_options_match_forge_setup_and_kd_release():
    # the same set of -o as the installer's ROOT_SSH (and kd-release): the host key is only the one pinned in the
    # repository, without the system's known_hosts and without edits to it
    def opts(s):
        return {o.strip("\"')") for o in re.findall(r"-o (\S+)", s)}

    mine = opts(re.search(r'export GIT_SSH_COMMAND="(.*)"', TEXT).group(1))
    setup = (INSTALL / "forge_setup.sh").read_text(encoding="utf-8")
    root = re.search(r'^ROOT_SSH="(.*)"$', setup, re.M).group(1).replace("$KNOWN", "/opt/digipaddock/common/install/forgejo_known_hosts")
    assert mine == opts(root)
    assert {"GlobalKnownHostsFile=/dev/null", "UpdateHostKeys=no", "StrictHostKeyChecking=yes", "BatchMode=yes"} <= mine


def test_work_dir_is_checked_not_created():
    assert '[ -d "$agent_dir/work" ]' in TEXT and "mkdir" not in TEXT


def test_refuses_repo_hooks_and_mcp_without_flag():
    assert "--allow-hooks" in TEXT
    assert ".mcp.json" in TEXT and '"hooks"' in TEXT


def test_registry_outside_secrets_dir_written_atomically_and_projects_md_root():
    assert "REPOS=/etc/digipaddock/repos.json" in TEXT and "/etc/digipaddock/forge" not in TEXT
    assert "<<'REG'" in TEXT and "os.replace" in TEXT
    assert "playbooks/projects.md" in TEXT


LIB = str(INSTALL.parent)  # agents/dev — where REG imports the registry's own checks from


def _reg(path, project, repo, folder, release="", role="code", base="", targets="", current="", *paths):
    """The REG snippet as add_repo.sh runs it: lib, registry, project, repo, dir, release, role, base, targets,
    up-to-date (yes|no|""), up-to-date paths…"""
    snippet = _heredoc("REG", "<<'REG' || exit $?\n")
    return subprocess.run([sys.executable, "-c", snippet, LIB, str(path), project, repo, folder, release, role, base,
                           targets, current, *paths], capture_output=True, encoding="utf-8", env=UTF8)


def test_registry_write_is_idempotent(tmp_path):
    reg = tmp_path / "repos.json"
    args = [reg, "digipaddock", "example/digipaddock", "work/DigiPaddock", "digipaddock", "code"]
    assert _reg(*args).returncode == 0
    first = reg.read_text(encoding="utf-8")
    assert _reg(*args).returncode == 0
    assert reg.read_text(encoding="utf-8") == first
    assert json.loads(first) == {"digipaddock": {"repo": "example/digipaddock", "dir": "work/DigiPaddock", "release": "digipaddock"}}
    assert _reg(reg, "api", "example-owner/ExampleApi", "work/ExampleApi").returncode == 0
    assert set(json.loads(reg.read_text(encoding="utf-8"))) == {"digipaddock", "api"}
    # what was written reads back through the registry as it is
    loaded = repos.load(reg)
    assert loaded["api"].release is None and loaded["digipaddock"].dir == "/srv/digipaddock-agents/dev/work/DigiPaddock"


def test_projects_md_lists_registry(tmp_path):
    reg = tmp_path / "repos.json"
    reg.write_text(json.dumps({
        "digipaddock": {"repo": "example/digipaddock", "dir": "work/DigiPaddock", "release": "digipaddock",
                  "merge": {"require_up_to_date": False, "up_to_date_paths": ["common/kd/cabinet/schema.py"]}},
        "api": {"repo": "example-owner/ExampleApi", "dir": "work/ExampleApi", "base": "master",
                "targets": ["master", "dev"], "checks": {"profiles": {"ready": {}, "unit": {"required": True}}}}}),
        encoding="utf-8")
    out = tmp_path / "projects.md"
    snippet = _heredoc("PY", "<<'PY'\n")
    subprocess.run([sys.executable, "-c", snippet, str(reg), str(out)], check=True)
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# Projects"
    assert ("| board project | repository | clone | base | targets | required checks | current base for merge "
            "| release by kd-merge --release |") in lines
    assert ("| `api` | `example-owner/ExampleApi` | `work/ExampleApi` | `master` | `master`, `dev` | `unit` "
            "| yes | no |") in lines
    assert ("| `digipaddock` | `example/digipaddock` | `work/DigiPaddock` | `main` | `main` | — "
            "| when touching `common/kd/cabinet/schema.py` | yes |") in lines


PASSPORT = {"repo": "example/alert", "dir": "work/alert", "base": "master", "targets": ["master"],
            "checks": {"profiles": {"unit": {"required": True}}},
            "merge_effects": {"master": "deploy to the acceptance stage"}, "merge": {"require_up_to_date": True}}


def test_rebind_keeps_the_passport(tmp_path):
    """a rebind (re-run, or kd-repo-attach from the cabinet with its fixed argv) keeps the passport."""
    reg = tmp_path / "repos.json"
    reg.write_text(json.dumps({"alert": PASSPORT}), encoding="utf-8")
    assert _reg(reg, "alert", "example/alert", "work/alert").returncode == 0
    assert json.loads(reg.read_text(encoding="utf-8"))["alert"] == PASSPORT


def test_given_fields_replace_the_old(tmp_path):
    reg = tmp_path / "repos.json"
    reg.write_text(json.dumps({"alert": {**PASSPORT, "merge_effects": {}}}), encoding="utf-8")
    p = _reg(reg, "alert", "example/alert", "work/alert", "", "code", "dev", "dev,master", "no", "migrations/*")
    assert p.returncode == 0, p.stderr
    item = json.loads(reg.read_text(encoding="utf-8"))["alert"]
    assert (item["base"], item["targets"], item["checks"]) == ("dev", ["dev", "master"], PASSPORT["checks"])
    assert item["merge"] == {"require_up_to_date": False, "up_to_date_paths": ["migrations/*"]}
    r = repos.load(reg)["alert"]
    assert r.targets == ("dev", "master") and repos.needs_up_to_date(r, ["migrations/1.sql"])


def test_another_repository_drops_the_passport(tmp_path):
    reg = tmp_path / "repos.json"
    reg.write_text(json.dumps({"alert": PASSPORT}), encoding="utf-8")
    assert _reg(reg, "alert", "example/alert2", "work/alert2", "", "code", "", "", "yes").returncode == 0
    assert json.loads(reg.read_text(encoding="utf-8"))["alert"] == {
        "repo": "example/alert2", "dir": "work/alert2", "merge": {"require_up_to_date": True}}


@pytest.mark.parametrize("given", [
    ("digipaddock", "a/code", "work/code", "digipaddock", "code", "dev", "dev"),  # kd-release releases main
    ("alert", "example/alert", "work/alert", "", "code", "", "dev"),  # kept merge_effects name a dropped target
])
def test_a_passport_the_registry_refuses_is_not_written(tmp_path, given):
    reg = tmp_path / "repos.json"
    reg.write_text(json.dumps({"alert": PASSPORT, "digipaddock": {"repo": "a/code", "dir": "work/code",
                                                            "release": "digipaddock"}}), encoding="utf-8")
    before = reg.read_text(encoding="utf-8")
    p = _reg(reg, *given)
    assert p.returncode == 2 and "repos.json" in p.stderr
    assert reg.read_text(encoding="utf-8") == before and not (tmp_path / "repos.json.tmp").exists()


def test_root_never_writes_into_agent_home_or_work_directly():
    for line in TEXT.splitlines():
        if line.lstrip().startswith("#"):
            continue
        if re.search(r"/home/|/work/", line) and re.match(r"\s*(chown|chmod|install|ln|cp|mv|tee|git)\b", line):
            raise AssertionError(line)


# --- the codes kd-repo-attach reads and the unbind -------------------------------------------------
def test_the_codes_the_root_unit_reads():
    from dev_agent import repo_attach

    assert repo_attach.BIND_CODES == {0: "connected", 4: "hooks", 5: "no_key", 6: "no_access"}
    assert "exit 5; }" in TEXT and '[ -f "$key" ] && [ ! -L "$key" ]' in TEXT  # no key of dev's account
    assert 'git clone --quiet "$url" "$dir" || {' in TEXT and "(clone)\" >&2; exit 6; }" in TEXT
    assert 'g ls-remote --quiet "$url" HEAD >/dev/null || {' in TEXT and "(ls-remote)\" >&2; exit 6; }" in TEXT
    assert "exit 4" in TEXT  # hooks
    # a folder of that name that is no clone (a card worktree, say) is not "no access"
    assert 'elif [ -e "$dir" ] || [ -L "$dir" ]; then' in TEXT


def _unbind(reg, project):
    snippet = _heredoc("UNREG", "<<'UNREG'\n")
    return subprocess.run([sys.executable, "-c", snippet, str(reg), project], capture_output=True, encoding="utf-8",
                          env=UTF8)


def test_unbind_takes_only_the_entry_out(tmp_path):
    reg = tmp_path / "repos.json"
    reg.write_text(json.dumps({
        "digipaddock": {"repo": "example/digipaddock", "dir": "work/DigiPaddock", "release": "digipaddock"},
        "site": {"repo": "example-owner/Site", "dir": "work/Site"}}), encoding="utf-8")
    assert _unbind(reg, "site").returncode == 0
    assert set(json.loads(reg.read_text(encoding="utf-8"))) == {"digipaddock"}
    again = _unbind(reg, "site")  # idempotent
    assert again.returncode == 0 and "was not attached" in again.stdout
    refused = _unbind(reg, "digipaddock")  # the release project — root edits the registry by hand
    assert refused.returncode == 7 and set(json.loads(reg.read_text(encoding="utf-8"))) == {"digipaddock"}
    assert repos.load(reg)["digipaddock"].release == "digipaddock"


def test_unbind_mode_validates_and_keeps_the_clones():
    head = TEXT.split('if [ "${1:-}" = --remove ]; then', 1)[1].split("\nfi\n", 1)[0]
    assert "^[a-z][a-z0-9-]{0,31}$" in head and "[ $# -eq 2 ]" in head
    assert "write_table" in head and "rm " not in head and "runuser" not in head


def _args_snippet():
    start = TEXT.index("project=${1:?usage")
    end = TEXT.index('\npython3 -I - "$project"', start)
    return TEXT[start:end]


@pytest.mark.parametrize("args, code", [
    (["digipaddock-deploy", "a/deploy", "--release", "digipaddock", "--role", "overlay"], 0),
    (["digipaddock", "a/code", "--release", "digipaddock", "--role", "code"], 0),
    (["digipaddock-deploy", "a/deploy", "--release", "digipaddock", "--role", "deploy"], 2),
    (["digipaddock-deploy", "a/deploy", "--role", "overlay"], 2),  # an overlay belongs to a release
    (["digipaddock-deploy", "a/deploy", "--release", "digipaddock", "--role"], 2),
])
def test_role_argument(args, code):
    """add_repo.sh <project> <owner/repo> --release digipaddock --role code|overlay."""
    bash = _bash()
    if bash is None:
        pytest.skip("нет bash")
    p = subprocess.run([bash, "-c", "set -euo pipefail\n" + _args_snippet(), "add_repo.sh", *args],
                       capture_output=True, text=True, env=UTF8)
    assert p.returncode == code, p.stderr


@pytest.mark.parametrize("args, code", [
    (["alert", "a/alert", "--base", "master", "--targets", "master,dev", "--up-to-date", "yes",
      "--up-to-date-path", "a/*", "--up-to-date-path", "b.py"], 0),
    (["alert", "a/alert", "--base", "../x"], 2),
    (["alert", "a/alert", "--base", "x.lock"], 2),
    (["alert", "a/alert", "--targets", "master,,dev"], 2),
    (["alert", "a/alert", "--targets", "a..b"], 2),
    (["alert", "a/alert", "--up-to-date", "maybe"], 2),
    (["alert", "a/alert", "--up-to-date-path"], 2),
    (["alert", "a/alert", "--base"], 2),
])
def test_passport_arguments(args, code):
    bash = _bash()
    if bash is None:
        pytest.skip("нет bash")
    p = subprocess.run([bash, "-c", "set -euo pipefail\n" + _args_snippet(), "add_repo.sh", *args],
                       capture_output=True, text=True, env=UTF8)
    assert p.returncode == code, p.stderr


def test_branch_check_matches_the_registry():
    assert f"^{repos.BRANCH_RE.pattern}$" in TEXT and "*.lock" in TEXT
    # the registry's checks are the writer's: REG loads what it is about to write before it replaces the file
    reg = _heredoc("REG", "<<'REG' || exit $?\n")
    assert "registry.load(tmp)" in reg and reg.index("registry.load(tmp)") < reg.index("os.replace")


def test_registry_refuses_a_second_entry_of_a_role(tmp_path):
    """The registry repos.load would refuse breaks every project at once: the writer refuses it first."""
    path = tmp_path / "repos.json"
    path.write_text(json.dumps({"digipaddock": {"repo": "a/code", "dir": "work/code", "release": "digipaddock"}}))

    def reg(*args):
        return _reg(path, *args)

    assert reg("two", "a/other", "work/other", "digipaddock", "code").returncode == 2
    assert "two" not in json.loads(path.read_text())
    assert reg("digipaddock", "a/code", "work/code", "digipaddock", "code").returncode == 0  # the same project again
    assert reg("digipaddock-deploy", "a/deploy", "work/deploy", "digipaddock", "overlay").returncode == 0
    data = json.loads(path.read_text())
    assert data["digipaddock-deploy"]["role"] == "overlay" and "role" not in data["digipaddock"]
    assert repos.load(path)["digipaddock-deploy"].role == "overlay"


@pytest.mark.skipif(shutil.which("git") is None or shutil.which("bash") is None, reason="needs git and bash")
def test_rebinding_an_existing_clone_runs_nothing_its_config_names(tmp_path):
    """Review 04.11.2026: the clone's .git/config is the agent's writing and this block runs outside its sandbox — a
    hooks path, an fsmonitor or a reference-transaction hook planted there runs nothing on a rebind."""
    block = TEXT[TEXT.index("runuser -u \"$user\" -- bash -euc '") + len("runuser -u \"$user\" -- bash -euc '"):]
    block = block[:block.index("\n' _ \"$agent_dir/$dir\"")]
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "GIT_CONFIG_GLOBAL": "/dev/null"}
    origin, clone, marker = tmp_path / "origin.git", tmp_path / "clone", tmp_path / "ran"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True, env=env)
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, env=env, capture_output=True)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    evil = f"#!/bin/sh\necho ran >> {marker}\n"
    for name in ("reference-transaction", "post-index-change", "post-checkout"):
        (hooks / name).write_text(evil)
        (hooks / name).chmod(0o755)
    (tmp_path / "fsmonitor").write_text(evil)
    (tmp_path / "fsmonitor").chmod(0o755)
    with open(clone / ".git" / "config", "a") as config:
        config.write(f"[core]\n\thooksPath = {hooks}\n\tfsmonitor = {tmp_path / 'fsmonitor'}\n")
    p = subprocess.run(["bash", "-euc", block, "_", str(clone), str(origin), "/dev/null", "dev@example.test", "dev"],
                       env=env, capture_output=True, encoding="utf-8", cwd="/")
    assert p.returncode == 0, p.stderr
    assert not marker.exists()
    assert subprocess.run(["git", "-C", str(clone), "config", "user.email"], env=env, capture_output=True,
                          encoding="utf-8").stdout.strip() == "dev@example.test"
