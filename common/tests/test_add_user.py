import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from kd import board_grant as grants
from kd.board_grant import BoardGrant, BoardGrantError, board_grant, grant_from_acl

COMMON = Path(__file__).resolve().parent.parent
TEXT = (COMMON / "install/add_user.sh").read_text(encoding="utf-8")
LIB = (COMMON / "install/lib/hindsight_client.sh").read_text(encoding="utf-8")
STAGE = (COMMON / "install/stage_setup.sh").read_text(encoding="utf-8")


def _sibling(name):
    """A sibling module from common/tests: --import-mode=importlib does not put the tests' directory into sys.path."""
    spec = importlib.util.spec_from_file_location(f"_kd_{name}", Path(__file__).with_name(f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# the same helper as in test_installers_common (the homes' pattern is wider now): one copy for both files
_installers = _sibling("test_installers_common")
_root_ops_on_agent_home = _installers._root_ops_on_agent_home
BASH = _installers.BASH


def test_user_without_sudo_and_homes_closed_to_each_other():
    # the base user and the prefix come from naming.sh, never a literal
    assert 'source "$REPO/common/install/lib/naming.sh"\nbase=$(kd_user base)\n' in TEXT
    assert "^$base-[a-z][a-z0-9-]{0,22}$" in TEXT
    # --user-group: the agent's installation needs a personal group (install -g "$user"), no reliance on USERGROUPS_ENAB
    assert 'id "$u" >/dev/null 2>&1 || useradd --create-home --user-group --shell /bin/bash "$u"' in TEXT
    assert "grep -qx -e sudo -e admin -e wheel" in TEXT
    assert not re.search(r"usermod[^\n]*\b(sudo|admin|wheel)\b", TEXT)
    assert 'runuser -u "$u" -- chmod 0750 "/home/$u"' in TEXT and 'runuser -u "$base" -- chmod 0750 "/home/$base"' in TEXT


WALK_START = "  runuser -u \"$base\" -- bash -euc '\n"
WALK_END = "' _ \"$backlog\" \"$dir_acl\" \"$file_acl\" \"$base\"\n"


def _walk_block():
    # A08 has a separate revocation walk before the legacy grant walk.
    start = TEXT.index(WALK_START, TEXT.index("if [ -n \"$dir_acl\" ]; then"))
    return TEXT[start:TEXT.index(WALK_END, start)]


def test_board_acl_contents_set_by_owner_not_root():
    # The contents of backlog/ are set by the base user ($4 of the walk) on its own files only: root never walks a directory an agent owns.
    assert "setfacl -R" not in TEXT and "setfacl -P" not in TEXT
    block = _walk_block()
    flat = re.sub(r"\\\n\s*", "", block)  # join continued lines as the inner bash does
    find = next(line.strip() for line in flat.splitlines() if line.lstrip().startswith("find "))
    assert find.startswith('find "$1" -mindepth 1 -xdev ')
    assert '\\( -type d -user "$4" -exec setfacl -m "$2" {} + \\)' in find
    assert '\\( -type f -user "$4" -exec setfacl -m "$3" {} + \\)' in find
    # Outside the walk root calls setfacl exactly once, on backlog/ itself, with the computed entries.
    calls = [line.strip() for line in TEXT.replace(block, "").splitlines()
             if line.strip().startswith("setfacl -")]
    assert calls == [
        'setfacl -x "u:$u,d:u:$u" "$backlog"',
        'setfacl -m "$dir_acl" "$backlog"',
    ]


def test_board_acl_walk_survives_root_cwd_and_foreign_dirs():
    block = _walk_block()
    # runuser without -l keeps root's cwd (/root 0700) and GNU find returns there before each -exec: cd / first.
    assert block.startswith(WALK_START + "    cd /\n")
    # Directories of other users (digi-dev may leave a mode-000 one) are pruned before any -exec.
    flat = re.sub(r"\\\n\s*", "", block)
    find = next(line.strip() for line in flat.splitlines() if line.lstrip().startswith("find "))
    prune = 'find "$1" -mindepth 1 -xdev \\( -type d ! -user "$4" -prune \\) -o \\( -type d -user "$4" -exec'
    assert find.startswith(prune) and find.count(" -o ") == 2 and find.count("-exec setfacl") == 2


def test_board_has_no_write_model():
    # every board write goes through an executor; the script grants read or nothing, never a group.
    assert "kd-agents" not in TEXT and "  write)" not in TEXT
    assert not any(cmd in TEXT for cmd in ("usermod", "groupadd", "gpasswd", "chown"))
    read = TEXT[TEXT.index("  read)\n"):TEXT.index("  none)\n")]
    assert "w" not in read.split("dir_acl=", 1)[1].split("\n")[0]
    # The board path comes from the resource table through kd.board_grant, not from a constant here.
    assert "/srv/digipaddock-agents" not in TEXT and "BOARD=" not in TEXT


def test_board_grant_is_computed_before_any_change():
    grant = TEXT.index("from kd.board_grant import main")
    for change in ("useradd ", 'setfacl -m "$dir_acl"', "runuser "):
        assert grant < TEXT.index(change), change
    assert "python3 -I -B -c" in TEXT  # isolated: no PYTHONPATH, user site or bytecode written into the code tree


def test_claude_same_version_as_agent_and_no_bun():
    assert 'runuser -u "$base" -- "/home/$base/.local/bin/claude" --version' in TEXT
    assert "install.sh | bash -s '$v'" in TEXT
    assert "bun" not in TEXT.lower()  # Bun ran only the Telegram plugin
    assert "env DISABLE_AUTOUPDATER=1" in TEXT
    # the version is compared after the installation: install.sh may have installed the wrong one
    got = TEXT.index('got=$(runuser -u "$u" -- "/home/$u/.local/bin/claude" --version')
    assert TEXT.index("install.sh | bash -s '$v'") < got < TEXT.index('[ "$got" = "$v" ] ||')


def test_add_user_installs_no_telegram_plugin():
    # no agent has a Telegram channel; the plugin is neither installed nor disabled.
    assert "telegram" not in TEXT.lower()
    assert "claude plugin" not in TEXT


def test_everything_in_homes_as_their_owner():
    assert _root_ops_on_agent_home(TEXT) == [] and _root_ops_on_agent_home(LIB) == []
    for line in TEXT.splitlines():
        if "curl -fsSL" in line:
            assert line.lstrip().startswith('runuser -u "$u" --'), line


def test_hindsight_client_is_shared_library():
    src = 'source "$REPO/common/install/lib/hindsight_client.sh"'
    assert src in TEXT and src in STAGE
    assert 'hindsight_client_for "$u"' in TEXT and 'hindsight_client_for "$u"' in STAGE
    body = LIB.split("hindsight_client_for() {", 1)[1]
    assert "return 1" in body and ">&2; exit" not in body  # a function: return, not an exit of the calling script
    assert "agents.users()" in STAGE


def _bash(script, *args):
    return subprocess.run([BASH, "-c", script, "_", *args], cwd=COMMON.parent, capture_output=True,
                          encoding="utf-8", errors="replace", timeout=30,
                          env={**os.environ, "KD_PY": Path(sys.executable).as_posix()})


# naming.sh on a machine without /usr/bin/python3: the real naming.py with the default prefix.
KD_USER = r"""kd_user() { MSYS_NO_PATHCONV=1 "$KD_PY" -I common/kd/naming.py user "$@" | tr -d '\r'; }
"""
NAMING = r"""source() {
  case $1 in
    */naming.sh) """ + KD_USER.strip() + r""" ;;
    *) log "source $*" ;;
  esac
}
"""


@pytest.mark.skipif(BASH is None, reason="нет рабочего bash")
@pytest.mark.skipif(Path("/etc/hindsight/coding-agent.env").exists(), reason="на машине есть сервер Hindsight")
def test_hindsight_client_returns_instead_of_exiting_caller():
    # under the caller's set -euo pipefail: a wrong user — code 1 and a line on stderr, the script lives on;
    # no /etc/hindsight/coding-agent.env (there is none here) — skipped with code 0
    script = (KD_USER + 'set -euo pipefail; REPO=.; source common/install/lib/hindsight_client.sh; '
              'hindsight_client_for "agent;id" || echo "rc=$?"; hindsight_client_for digi-dev && echo skipped')
    proc = _bash(script)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["rc=1", "skipped"]
    assert "the user must be digi or digi-<agent>" in proc.stderr


@pytest.mark.skipif(BASH is None, reason="нет рабочего bash")
@pytest.mark.parametrize("user", ["digi", "digi-", "digi-Dev", "digi-dev;id", "digi-dev/../x", "digi-" + "a" * 24, "agent-dev"])
def test_add_user_rejects_bad_name_before_anything(user):
    proc = _bash(NAMING + '. common/install/add_user.sh "$@"', user)
    assert proc.returncode == 2 and "the user must be digi-<agent>" in proc.stderr, proc.stderr
    assert proc.stdout == ""


# --- the board grant: decided by kd.board_grant from the compiled policy ----------------------------------------

BACKLOG = "/srv/digipaddock-agents/board/backlog"
BOARD_ROOT = "/srv/digipaddock-agents/board"
FIXTURE_AGENTS = COMMON / "tests/fixtures/policy/agents"


@pytest.fixture
def repo_root(registry_root):
    """registry_root plus the managed floor and the boss, dev and sysadmin fixture manifests."""
    floor = registry_root / grants.FLOOR
    floor.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(COMMON.parent / grants.FLOOR, floor)
    for source in sorted(FIXTURE_AGENTS.glob("*/agent.json")):
        target = registry_root / "agents" / source.parent.name / "agent.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return registry_root


def _add_manifest(root, name, profile, capabilities, sandbox):
    path = root / "agents" / name / "agent.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 2, "name": name, "profile": profile, "capabilities": capabilities,
        "runtime": {"sandbox_profile": sandbox, "working_directory": "work"},
    }
    path.write_text(json.dumps(manifest), encoding="utf-8")


def _edit_boss(root, change):
    path = root / "agents/boss/agent.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    change(manifest)
    path.write_text(json.dumps(manifest), encoding="utf-8")


def test_coordinator_gets_a_read_only_board_grant(repo_root):
    # board.read reads; board.coordinate adds no ACL at all (its writes go through kd-boss-board).
    assert board_grant("digi-boss", repo_root) == BoardGrant("read", BACKLOG)


def test_board_write_capability_gives_read_only(repo_root):
    # board.write writes through the kd-task executor; the user's own grant stays read.
    _add_manifest(repo_root, "builder", "developer", ["board.read", "board.write"], "developer-v1")
    assert board_grant("digi-builder", repo_root) == BoardGrant("read", BACKLOG)


def test_agent_without_a_board_capability_gets_no_grant(repo_root):
    _add_manifest(repo_root, "watcher", "sysadmin", ["servers.read"], "sysadmin-v1")
    assert board_grant("digi-watcher", repo_root) == BoardGrant("none", BACKLOG)


def test_dev_agent_gets_read_only_from_its_manifest(repo_root):
    # board.write in agents/dev/agent.json gives no file write since the change: read only
    assert board_grant("digi-dev", repo_root) == BoardGrant("read", BACKLOG)


def test_dev_agent_without_its_manifest_gets_no_grant(registry_root):
    with pytest.raises(BoardGrantError, match="no agent manifest runs as digi-dev"):
        board_grant("digi-dev", registry_root)


@pytest.mark.parametrize("user", ["digi-new", "digi-forge", "digi-exec", "agent-ro"])
def test_user_without_a_manifest_is_refused(repo_root, user):
    # No "as before" default for a new user: neither kd-agents nor any ACL without a manifest.
    with pytest.raises(BoardGrantError, match=f"(no agent manifest runs as |not an agent user: ')({user})"):
        board_grant(user, repo_root)


def test_new_agent_is_refused_when_no_manifest_exists(registry_root):
    with pytest.raises(BoardGrantError):
        board_grant("digi-boss", registry_root)


@pytest.mark.parametrize(
    "user", ["agent", "", "agent-", "Agent-boss", "digi-boss;id", "digi-boss\n", "agent-12345678901234567",
             "root", "../digi-boss", None])
def test_not_an_agent_user_is_refused(repo_root, user):
    with pytest.raises(BoardGrantError, match="not an agent user"):
        board_grant(user, repo_root)


@pytest.mark.parametrize("change", [
    pytest.param(lambda m: m["capabilities"].append("board.admin"), id="unknown-capability"),
    pytest.param(lambda m: m["capabilities"].append("board.write"), id="coordinator-with-raw-write"),
    pytest.param(lambda m: m["capabilities"].remove("board.coordinate"), id="coordinator-without-coordinate"),
    pytest.param(lambda m: m["capabilities"].remove("board.read"), id="missing-dependency"),
    pytest.param(lambda m: m.update(extra=True), id="unknown-field"),
    pytest.param(lambda m: m.update(user="digi-dev"), id="reserved-user"),
])
def test_manifest_problems_are_refused(repo_root, change):
    _edit_boss(repo_root, change)
    with pytest.raises(BoardGrantError, match="manifests"):
        board_grant("digi-boss", repo_root)


def test_policy_that_does_not_compile_is_refused(repo_root):
    # A floor deny that swallows a compiled allow: the compiler refuses, and so does the grant.
    floor = repo_root / grants.FLOOR
    data = json.loads(floor.read_text(encoding="utf-8"))
    data["permissions"]["deny"].append("Bash(kd-task view *)")
    floor.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(BoardGrantError, match="policy of boss"):
        board_grant("digi-boss", repo_root)


def test_broken_registry_is_refused(registry_root):
    (registry_root / "common/agent-resources.json").write_text("{", encoding="utf-8")
    with pytest.raises(BoardGrantError, match="registry"):
        board_grant("digi-dev", registry_root)


ENTRY = {"user": "digi-boss", "resource": "board-tasks", "path": BOARD_ROOT, "access": "read"}


def test_acl_entry_becomes_the_grant():
    other = {**ENTRY, "resource": "forge-journal", "path": "/var/log/digipaddock-forge"}
    assert grant_from_acl([other, ENTRY], "digi-boss", BOARD_ROOT, BACKLOG) == BoardGrant("read", BACKLOG)
    assert grant_from_acl([other], "digi-boss", BOARD_ROOT, BACKLOG) == BoardGrant("none", BACKLOG)


@pytest.mark.parametrize("acl", [
    pytest.param([ENTRY, {**ENTRY, "access": "write"}], id="two-entries"),
    pytest.param([{**ENTRY, "user": "digi-dev"}], id="other-user"),
    pytest.param([{**ENTRY, "path": "/srv/digipaddock-agents"}], id="other-path"),
    pytest.param([{**ENTRY, "access": "admin"}], id="unknown-access"),
    pytest.param([{**ENTRY, "access": "write"}], id="write-entry"),
    pytest.param([{**ENTRY, "access": "none"}], id="none-entry"),
    pytest.param([{key: value for key, value in ENTRY.items() if key != "access"}], id="no-access"),
])
def test_unexpected_board_entries_are_refused(acl):
    with pytest.raises(BoardGrantError):
        grant_from_acl(acl, "digi-boss", BOARD_ROOT, BACKLOG)


def test_cli_prints_one_grant_line(repo_root, capsys):
    assert grants.main([str(repo_root), "digi-boss"]) == 0
    out = capsys.readouterr()
    assert (out.out, out.err) == (f"read {BACKLOG}\n", "")


def test_cli_refusal_prints_nothing_on_stdout(repo_root, capsys):
    assert grants.main([str(repo_root), "digi-new"]) == 1
    out = capsys.readouterr()
    assert out.out == "" and out.err.startswith("board grant refused: ")


@pytest.mark.parametrize("argv", [[], ["root"], ["root", "digi-boss", "extra"]])
def test_cli_takes_exactly_root_and_user(argv, capsys):
    assert grants.main(argv) == 2
    assert capsys.readouterr().out == ""


# --- add_user.sh applies the grant: run with every system command replaced by a logging function -----------------

# State lives in files so that a second run sees what the first one did (idempotence). Nothing here runs as root or
# changes the machine: useradd, usermod, groupadd, gpasswd, setfacl, runuser and python3 are bash functions; the
# script calls none of the group commands, and any call shows up in the steps a test compares.
HARNESS = r"""
export PATH="/usr/bin:$PATH"
log() { printf '%s\n' "$*" >> "$KD_LOG"; }
id() {
  case "$*" in
    -u) echo 0 ;;
    digi) return 0 ;;
    "-nG $KD_USER") echo "$KD_USER $(cat "$KD_STATE/groups")" ;;
    "$KD_USER") [ -e "$KD_STATE/exists" ] ;;
    *) log "unexpected id $*"; return 1 ;;
  esac
}
useradd() { log "useradd $*"; : > "$KD_STATE/exists"; }
usermod() { log "usermod $*"; }
gpasswd() { log "gpasswd $*"; : > "$KD_STATE/groups"; }
groupadd() { log "groupadd $*"; }
setfacl() { log "setfacl $*"; }
find() { log "find root $*"; }
apt-get() { log "apt-get $*"; }
bash() {
  case "$1" in
    */common/install/cabinet_setup.sh) log "cabinet ${*:2}"; return 0 ;;
  esac
  command bash "$@"
}
@NAMING@hindsight_client_for() { log "hindsight $*"; }
runuser() {
  local who=$2
  shift 3
  case "$1" in
    bash) if [ "$2" = -euc ]; then log "walk $who ${*:5}"; return 0; fi ;;
    find) log "find $who ${*:2} from $PWD"; printf '%s\n' "$KD_FOUND"; return 0 ;;
    */claude) if [ "${2:-}" = --version ]; then echo "2.1.0 (Claude Code)"; return 0; fi ;;
  esac
  log "runuser $who $1"
}
python3() {
  log "grant ${*: -2}"
  if [ -n "$KD_ROOT" ]; then
    local args=() a
    for a in "$@"; do if [ "$a" = /opt/digipaddock ]; then args+=("$KD_ROOT"); else args+=("$a"); fi; done
    MSYS_NO_PATHCONV=1 "$KD_PY" "${args[@]}" | tr -d '\r'
    return "${PIPESTATUS[0]}"
  fi
  printf '%s\n' "$KD_GRANT"
  return "$KD_GRANT_RC"
}
if [ -n "$KD_MODE" ]; then
  . common/install/add_user.sh "$KD_USER" "$KD_MODE"
else
  . common/install/add_user.sh "$KD_USER"
fi
"""
HARNESS = HARNESS.replace("@NAMING@", NAMING)
BOARD_STEPS = ("grant ", "useradd ", "usermod ", "groupadd ", "gpasswd ", "setfacl ", "walk ", "find ", "unexpected ")
READ_ACL = "u:digi-boss:rX,d:u:digi-boss:rX"


class Machine:
    """One fake host for add_user.sh runs: the user, its groups and what the grant step answers."""

    def __init__(self, tmp_path, user, *, grant="", rc=0, root=None, groups="", exists=False, found=""):
        self.state = tmp_path / "state"
        self.state.mkdir()
        (self.state / "groups").write_text(groups, encoding="utf-8")
        if exists:
            (self.state / "exists").touch()
        self.log = tmp_path / "log"
        self.env = {**os.environ, "KD_LOG": self.log.as_posix(), "KD_STATE": self.state.as_posix(), "KD_USER": user,
                    "KD_GRANT": grant, "KD_GRANT_RC": str(rc), "KD_FOUND": found,
                    "KD_ROOT": root.as_posix() if root else "", "KD_PY": Path(sys.executable).as_posix(),
                    "KD_MODE": ""}

    def run(self, mode=""):
        """Run add_user.sh once; return the process and the board-related commands it issued, in order."""
        proc = subprocess.run([BASH, "-c", HARNESS], cwd=COMMON.parent,
                              env={**self.env, "KD_MODE": mode}, capture_output=True,
                              encoding="utf-8", errors="replace", timeout=120)
        lines = self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []
        self.log.unlink(missing_ok=True)
        self.all_steps = lines
        return proc, [line for line in lines if line.startswith(BOARD_STEPS)]


def _postflight(user):
    # The whole backlog/ as the user, started from / so that root's private cwd does not break find.
    return f"find {user} {BACKLOG} -writable -print -quit from /"


def _read_steps(user="digi-boss", acl=READ_ACL, *, fresh=True):
    return [
        f"grant /opt/digipaddock {user}",
        *([f"useradd --create-home --user-group --shell /bin/bash {user}"] if fresh else []),
        f"setfacl -m {acl} {BACKLOG}",
        f"walk digi {BACKLOG} {acl} {acl.split(',')[0]} digi",
        _postflight(user),
    ]


needs_bash = pytest.mark.skipif(BASH is None, reason="нет рабочего bash")


@needs_bash
def test_read_grant_is_a_named_read_entry(tmp_path):
    proc, steps = Machine(tmp_path, "digi-boss", grant=f"read {BACKLOG}").run()
    assert proc.returncode == 0, proc.stderr
    assert steps == _read_steps()
    assert not any("rw" in step for step in steps)


@needs_bash
def test_none_grant_sets_no_acl_and_checks_no_write(tmp_path):
    proc, steps = Machine(tmp_path, "digi-watcher", grant=f"none {BACKLOG}").run()
    assert proc.returncode == 0, proc.stderr
    assert steps == [
        "grant /opt/digipaddock digi-watcher",
        "useradd --create-home --user-group --shell /bin/bash digi-watcher",
        f"setfacl -x u:digi-watcher,d:u:digi-watcher {BACKLOG}",
        f"walk digi {BACKLOG} u:digi-watcher,d:u:digi-watcher u:digi-watcher digi",
        _postflight("digi-watcher"),
    ]


@needs_bash
def test_agent_user_joins_the_cabinet_group_except_on_revoke(tmp_path):
    # the board is the cabinet's socket; an agent user reaches it through the group digipaddock-cabinet,
    # which
    # cabinet_setup.sh grant adds. --revoke takes away the file grant only.
    machine = Machine(tmp_path, "digi-boss", grant=f"read {BACKLOG}")
    for mode in ("", "--prepare"):
        proc, _ = machine.run(mode)
        assert proc.returncode == 0, proc.stderr
        assert machine.all_steps.count("cabinet grant digi-boss") == 1
        assert machine.all_steps.index("cabinet grant digi-boss") > machine.all_steps.index("grant /opt/digipaddock digi-boss")
    proc, _ = machine.run("--revoke")
    assert proc.returncode == 0, proc.stderr
    assert not any(step.startswith("cabinet ") for step in machine.all_steps)


@needs_bash
def test_prepare_and_revoke_never_grant_board_access(tmp_path):
    machine = Machine(tmp_path, "digi-boss", grant=f"read {BACKLOG}")
    prepared, steps = machine.run("--prepare")
    assert prepared.returncode == 0, prepared.stderr
    assert not any("setfacl -m" in step or "usermod" in step for step in steps)
    assert f"setfacl -x u:digi-boss,d:u:digi-boss {BACKLOG}" in steps
    granted, steps = machine.run("--grant")
    assert granted.returncode == 0, granted.stderr
    assert steps.index(f"setfacl -x u:digi-boss,d:u:digi-boss {BACKLOG}") < steps.index(
        f"setfacl -m {READ_ACL} {BACKLOG}")
    revoked, steps = machine.run("--revoke")
    assert revoked.returncode == 0, revoked.stderr
    assert not any("setfacl -m" in step or "useradd" in step for step in steps)


@needs_bash
@pytest.mark.parametrize("grant, rc", [
    pytest.param("", 1, id="refused"),
    pytest.param(f"read {BACKLOG}", 1, id="refused-despite-output"),
    pytest.param("", 0, id="empty"),
    pytest.param(f"admin {BACKLOG}", 0, id="unknown-access"),
    pytest.param(f"write {BACKLOG}", 0, id="write"),
    pytest.param("read srv/digipaddock-agents/board/backlog", 0, id="relative-path"),
    pytest.param(f"read {BACKLOG};id", 0, id="shell-metacharacter"),
    pytest.param(f"read {BACKLOG} extra", 0, id="extra-word"),
    pytest.param(f"read {BACKLOG}\nnone {BACKLOG}", 0, id="two-lines"),
    pytest.param(f"read  {BACKLOG}", 0, id="double-space"),
])
def test_refused_or_malformed_grant_changes_nothing(tmp_path, grant, rc):
    proc, steps = Machine(tmp_path, "digi-boss", grant=grant, rc=rc).run()
    assert proc.returncode == 1, proc.stderr
    assert steps == ["grant /opt/digipaddock digi-boss"]
    assert "nothing changed" in proc.stderr and proc.stdout == ""


@needs_bash
def test_rerun_is_idempotent(tmp_path):
    machine = Machine(tmp_path, "digi-boss", grant=f"read {BACKLOG}")
    first, first_steps = machine.run()
    second, second_steps = machine.run()
    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    assert first_steps == _read_steps(fresh=True)
    assert second_steps == _read_steps(fresh=False)  # the same grant again, only the user already exists


@needs_bash
@pytest.mark.parametrize("grant", ["read", "none"])
@pytest.mark.parametrize("found, shown", [
    pytest.param(BACKLOG, BACKLOG, id="backlog"),
    pytest.param(f"{BACKLOG}/tasks/kd-7 - deep.md", f"{BACKLOG}/tasks/kd-7 - deep.md", id="card-file"),
    pytest.param(f"{BACKLOG}/drafts/x\x1b[31m.md", f"{BACKLOG}/drafts/x?[31m.md", id="control-character"),
])
def test_user_without_write_who_can_write_anywhere_fails_the_postflight(tmp_path, grant, found, shown):
    proc, steps = Machine(tmp_path, "digi-boss", grant=f"{grant} {BACKLOG}", found=found).run()
    assert proc.returncode == 1
    assert steps[-1] == _postflight("digi-boss")
    assert f"digi-boss can write to {shown} though its board access is {grant}" in proc.stderr
    assert "\x1b" not in proc.stderr


@pytest.fixture
def runtime_root(repo_root):
    """repo_root plus the kd package, so the real kd.board_grant answers the script's own python3 call."""
    shutil.copytree(COMMON / "kd", repo_root / "common/kd", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copyfile(COMMON / "naming.default.json", repo_root / "common/naming.default.json")
    return repo_root


@needs_bash
@pytest.mark.parametrize("user, expected", [
    ("digi-boss", lambda: _read_steps()),
    # the dev agent's board.write is a read grant too.
    ("digi-dev", lambda: _read_steps("digi-dev", READ_ACL.replace("digi-boss", "digi-dev"))),
])
def test_script_applies_the_real_board_grant(tmp_path, runtime_root, user, expected):
    proc, steps = Machine(tmp_path, user, root=runtime_root).run()
    assert proc.returncode == 0, proc.stderr
    assert steps == expected()


@needs_bash
def test_script_refuses_a_user_without_a_manifest(tmp_path, runtime_root):
    proc, steps = Machine(tmp_path, "digi-new", root=runtime_root).run()
    assert proc.returncode == 1
    assert steps == ["grant /opt/digipaddock digi-new"]
    assert "board grant refused: no agent manifest runs as digi-new" in proc.stderr
