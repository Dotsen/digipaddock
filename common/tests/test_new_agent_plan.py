"""new_agent.sh --check/--plan (BOSS spec 2026-10-06, section 5.4): read-only install plan.

Every test builds a fixture system root: the repository sits at the deployed code root
inside it, next to the system files a check reads (accounts, the runtime agent registry,
the managed settings, unit templates, commands, the agent folders) and secret files that it
must never read. The manifests are the policy fixtures; the shared instruction template is a
synthetic one, so the plan snapshot follows the policy and the install steps, not later
template edits.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pytest

from kd import board_grant as board_grant_module
from kd import cap_grants, install_plan, launch as launch_module, naming, registry, specializations
from kd.agent_text import COMMON_SECTIONS
from kd.board_grant import BACKLOG_DIR, board_grant
from kd.install_plan import (
    HostView,
    PlanError,
    board_acl,
    build_plan,
    main,
    render_plan,
)
from kd.policy import render_policy
from kd.session_host import claude as session_claude

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures"
PLAN_FIXTURES = FIXTURES / "new_agent_plan"
POLICY_AGENTS = FIXTURES / "policy" / "agents"
SNAPSHOT = PLAN_FIXTURES / "boss.plan.txt"
RESOURCES = json.loads((REPO / registry.RESOURCES).read_text("utf-8"))
LAYOUT = RESOURCES["layout"]
AGENTS_ROOT = LAYOUT["agents"]
AGENTS_JSON = RESOURCES["resources"][install_plan.REGISTRY_RESOURCE]["path"]
BOARD = RESOURCES["resources"]["board-tasks"]["path"]
BACKLOG = f"{BOARD}/{BACKLOG_DIR}"
JOURNALS = [
    RESOURCES["resources"][name]["path"] for name in ("exec-journal", "forge-journal")
]
SCRIPT = REPO / "common/install/new_agent.sh"
ADD_USER = (REPO / install_plan.ADD_USER).read_text(encoding="utf-8")
HINDSIGHT_LIB = (REPO / "common/install/lib/hindsight_client.sh").read_text("utf-8")
MARKER = "SECRET-FIXTURE-MARKER"

PASSWD = """root:x:0:0:root:/root:/bin/bash
www-data:x:33:33:www-data:/var/www:/usr/sbin/nologin
digi:x:1000:1000::/home/digi:/bin/bash
digi-dev:x:1001:1001::/home/digi-dev:/bin/bash
digi-exec:x:998:998::/home/digi-exec:/usr/sbin/nologin
digi-forge:x:997:997::/home/digi-forge:/usr/sbin/nologin
"""
GROUP = """root:x:0:
sudo:x:27:
docker:x:999:
www-data:x:33:
digi:x:1000:
digi-dev:x:1001:
digi-exec:x:998:
digi-forge:x:997:
kd-agents:x:1002:digi
"""
COMMANDS = (
    "/usr/bin/apt-get",
    "/usr/bin/curl",
    "/usr/bin/flock",
    "/usr/bin/getfacl",
    "/usr/bin/node",
    "/usr/bin/pgrep",
    "/usr/bin/pkill",
    "/usr/bin/python3",
    "/usr/bin/setfacl",
    "/usr/bin/sudo",
    "/usr/bin/systemctl",
    "/usr/bin/systemd-tmpfiles",
    "/usr/local/bin/agent-launch",
    "/usr/local/bin/backlog",
    "/usr/local/bin/kd-alert",
    "/usr/sbin/apparmor_parser",
    "/usr/sbin/runuser",
    "/usr/sbin/useradd",
)
# What a check may read: no home, no secret, no sudoers file; and the one listing.
STATE_FILES = {
    install_plan.PASSWD,
    install_plan.GROUP,
    install_plan.FLOOR,
    install_plan.USERNS_SYSCTL,
    AGENTS_JSON,
    "/run/digipaddock-cabinet/local.sock",  # the board's projects, asked from the cabinet
}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def _template() -> str:
    # A synthetic shared template with the fixed section titles (kd.agent_text).
    return "\n\n".join(
        f"## {title}\n\nShared {title} rule for {{{{agent}}}}."
        for title in COMMON_SECTIONS
    )


@dataclass
class Host:
    root: Path
    repo: Path
    # the board's projects as the cabinet answers; None — the cabinet's socket below root is absent
    routes: Optional[dict] = field(default_factory=lambda: {"digipaddock": "@dev", "admin": "@sysadmin", "alert": "@owner"})

    def at(self, absolute: str) -> Path:
        return self.root.joinpath(*absolute.strip("/").split("/"))

    def _routes(self):
        return None if self.routes is None else lambda: dict(self.routes)

    def view(self) -> HostView:
        return HostView(self.root, routes=self._routes())

    def plan(self, name: str = "boss") -> install_plan.Plan:
        return build_plan(self.repo, self.view(), name)

    def run(self, mode: str, name: str = "boss") -> int:
        return main(["--repo", str(self.repo), "--root", str(self.root), mode, name], routes=self._routes())

    def manifest(self, name: str) -> dict:
        return json.loads((self.repo / "agents" / name / "agent.json").read_text())

    def set_manifest(self, name: str, data: dict) -> None:
        _write(self.repo / "agents" / name / "agent.json", json.dumps(data))

    def host_data(self) -> dict:
        return json.loads((self.repo / install_plan.HOST_DATA).read_text())

    def set_host_data(self, data: dict) -> None:
        _write(self.repo / install_plan.HOST_DATA, json.dumps(data))

    def set_runtime(self, data: dict) -> None:
        _write(self.at(AGENTS_JSON), json.dumps(data))

    def add_account(self, line: str, groups: dict[str, str] | None = None) -> None:
        """A passwd line, plus ``group -> member`` additions or new groups (gid:member)."""
        passwd = self.at(install_plan.PASSWD)
        _write(passwd, passwd.read_text() + line + "\n")
        text = self.at(install_plan.GROUP).read_text()
        for group, member in (groups or {}).items():
            if re.search(rf"^{group}:", text, flags=re.M):
                text = re.sub(
                    rf"^({group}:x:\d+:)(.*)$",
                    lambda m: m.group(1) + ",".join(filter(None, [m.group(2), member])),
                    text,
                    flags=re.M,
                )
            else:
                text += f"{group}:x:{member}\n"
        _write(self.at(install_plan.GROUP), text)


def _make_host(base: Path) -> Host:
    root = base / "root"
    repo = root.joinpath(*LAYOUT["code"].strip("/").split("/"))
    host = Host(root, repo)
    # The repository: registries, the files they name, the planned wrappers, host data.
    for relative in registry.REGISTRY_PATHS:
        source = REPO / relative
        if source.is_dir():
            shutil.copytree(source, repo / relative)
        else:
            _copy(source, repo / relative)
    shutil.copytree(REPO / "common/specializations", repo / "common/specializations")
    tracked = registry.load_registry(REPO)
    for relative in sorted(registry.referenced_files(tracked.capabilities)):
        if (REPO / relative).is_file():
            _copy(REPO / relative, repo / relative)
        else:
            _write(repo / relative, "#!/bin/sh\nexit 0\n")  # a planned wrapper
    for relative in (
        install_plan.HOST_DATA,
        install_plan.HINDSIGHT_MAP,  # the memory bank named in the instructions
        board_grant_module.FLOOR,  # kd.board_grant's floor
        *install_plan.CAP_RUNTIME,  # the channel client, handler, service and target
    ):
        _copy(REPO / relative, repo / relative)
    _write(repo / install_plan.TEMPLATE, _template())
    for name in ("analyst", "boss", "dev", "sysadmin"):
        _copy(
            POLICY_AGENTS / name / "agent.json", repo / "agents" / name / "agent.json"
        )
        _write(repo / "agents" / name / "CLAUDE.md", f"# {name}\n\nRole of {name}.\n")
    shutil.copytree(
        PLAN_FIXTURES / "boss", repo / "agents" / "boss", dirs_exist_ok=True
    )
    # The system: two installed agents with manifests, dev in the runtime registry.
    _write(host.at(install_plan.PASSWD), PASSWD)
    _write(host.at(install_plan.GROUP), GROUP)
    # The installed floor is the rendered template (render_host.sh).
    _write(host.at(install_plan.FLOOR), naming.rewrite(naming.render((REPO / board_grant_module.FLOOR).read_text("utf-8"))))
    _write(host.at(install_plan.USERNS_SYSCTL), "1\n")
    host.set_runtime({"dev": {"runtime": "generated", "user": "digi-dev"}})
    for unit in sorted((REPO / "common/systemd").iterdir()):
        _copy(unit, host.at(f"{install_plan.UNIT_DIR}/{unit.name}"))
    for command in COMMANDS:  # a command on PATH is linked by the host's name (stage_setup.sh)
        _write(host.at(naming.rewrite(command)), "#!/bin/sh\n")
    for directory in (
        install_plan.SUDOERS_DIR,
        BACKLOG,
        *JOURNALS,
        RESOURCES["resources"]["repo-checkouts"]["path"],  # made by repo_setup.sh before the analyst's grant
        f"{AGENTS_ROOT}/dev/work",
        f"{AGENTS_ROOT}/sysadmin/memory",
    ):
        host.at(directory).mkdir(parents=True, exist_ok=True)
    _write(host.at(f"{AGENTS_ROOT}/README"), "not an agent\n")
    # Secrets that a check must never read or print.
    for resource in tracked.resources.resources.values():
        if resource.secret and resource.path is not None:
            target = host.at(resource.path)
            if resource.kind == "dir":
                target = target / "token.env"
            _write(target, f"TOKEN={MARKER}\n")
    for home, item in (
        ("digi", ".claude/.credentials.json"),
        ("digi", ".hindsight/coding-agent.json"),
        ("digi", ".ssh/agent_ro_ed25519"),
        ("digi-dev", ".ssh/dev_ed25519"),
    ):
        _write(host.at(f"/home/{home}/{item}"), f"{MARKER}\n")
    _write(host.at("/etc/hindsight/coding-agent.env"), f"KEY={MARKER}\n")
    return host


@pytest.fixture
def host(tmp_path: Path) -> Host:
    return _make_host(tmp_path)


def _errors(host: Host, name: str = "boss") -> tuple[str, ...]:
    with pytest.raises(PlanError) as caught:
        host.plan(name)
    return caught.value.errors


def _assert_refused(host: Host, expected: str, name: str = "boss") -> None:
    errors = _errors(host, name)
    assert any(expected in error for error in errors), errors


def _actions(plan: install_plan.Plan) -> dict[tuple[str, str], str]:
    return {(a.kind, a.target): a.detail for a in plan.actions}


def _fingerprint(root: Path) -> dict[str, tuple]:
    """Every entry below root: kind, size, mode, mtime and content hash."""
    found = {}
    for directory, dirs, files in os.walk(root):
        for name in dirs + files:
            path = Path(directory) / name
            info = path.lstat()
            digest = (
                hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
            )
            found[path.relative_to(root).as_posix()] = (
                info.st_mode,
                info.st_size,
                info.st_mtime_ns,
                digest,
            )
    return found


def _symlinks_work(tmp_path: Path) -> bool:
    try:
        os.symlink("target", tmp_path / "probe")
    except (OSError, NotImplementedError):
        return False
    (tmp_path / "probe").unlink()
    return True


# --- the plan ----------------------------------------------------------------------------


def test_boss_plan_matches_the_snapshot(host: Host) -> None:
    actual = render_plan(host.plan())
    expected = SNAPSHOT.read_text(encoding="utf-8")
    if actual != expected:
        diff = difflib.unified_diff(
            expected.splitlines(keepends=True),
            actual.splitlines(keepends=True),
            "snapshot",
            "plan",
        )
        pytest.fail(f"review, then update {SNAPSHOT.name}\n{''.join(diff)}")


def test_a_check_without_the_cabinet_fails_naming_it(host: Host, capsys) -> None:
    # the board's projects come from the cabinet; its absence is an explicit error, never «no projects»
    host.routes = None  # no cabinet socket below the fixture root
    assert host.run("--check") == 1
    err = capsys.readouterr().err
    assert "registry: board projects: the cabinet did not answer" in err and "start digipaddock-cabinet first" in err


def test_a_project_routed_to_an_agent_the_repository_lacks_is_refused(host: Host) -> None:
    host.routes = {"digipaddock": "@dev", "web": "@ghost"}
    assert "registry: board projects: project web routes to unknown agent @ghost" in _errors(host)


def test_the_agents_data_is_closed_to_other_users(host: Host) -> None:
    """Global review 15.10.2026: memory/ and work/ are the agent's alone (0750), notes 0640; the folder stays root's."""
    text = render_plan(host.plan())
    assert "dir /srv/digipaddock-agents/boss: root:root 0755" in text
    assert "dir /srv/digipaddock-agents/boss/memory: digi-boss:digi-boss 0750" in text
    assert "file /srv/digipaddock-agents/boss/memory/notes.md: digi-boss:digi-boss 0640, created empty by digi-boss if absent" in text
    assert "dir /srv/digipaddock-agents/boss/work: digi-boss:digi-boss 0750" in text
    assert "0755" not in [line for line in text.splitlines() if "/boss/work" in line or "/boss/memory" in line]


def test_analyst_plan_installs_repo_read_without_a_guard(host: Host) -> None:
    # setup script, kd-repo link and the checkouts ACL; no capability sudoers, no guard hook, no git.
    text = render_plan(host.plan("analyst"))
    assert "common/install/repo_setup.sh" in text
    assert "repo.read: install, check and disable as root with analyst digi-analyst" in text
    assert "link /usr/local/bin/kd-repo: -> /opt/digipaddock/common/bin/kd-repo" in text
    assert "acl /srv/digipaddock-repos: repo-checkouts read: u:digi-analyst:rX d:u:digi-analyst:rX, recursive" in text
    assert text.index("setup /opt/digipaddock/common/install/repo_setup.sh") < text.index("acl /srv/digipaddock-repos:")
    assert "playbooks/spec/task-brief.md" in text
    # the copywriting and UX review texts install with it; ux-review needs repo.read, which analyst holds.
    assert "playbooks/spec/copywriting.md" in text and "playbooks/spec/ux-review.md" in text
    assert "playbooks/spec/site-copy.md" not in text and "playbooks/spec/ui-design.md" not in text
    # usage.read brings kd-usage show and the cost-report text.
    assert "playbooks/spec/cost-report.md" in text and "link /usr/local/bin/kd-usage:" in text
    assert "playbooks/spec/data-health.md" in text  # analyst holds data.read
    assert "kd-analyst-capabilities" not in text and "deny_guard" not in text and "Bash(git" not in text


def test_dev_gets_its_type_specializations_as_root_owned_playbooks(host: Host) -> None:
    text = render_plan(host.plan("dev"))
    assert "dir /srv/digipaddock-agents/dev/playbooks/spec: root:root 0755" in text
    registry_ = specializations.load_registry(host.repo / "common/specializations", require_root_owner=False)
    for name in ("docs", "implementer", "reviewer", "site-copy", "test-author", "tester", "ui-design"):
        spec = registry_.specs[name]
        assert (f"file /srv/digipaddock-agents/dev/playbooks/spec/{name}.md: root:root 0644"
                f" sha256={spec.sha256} bytes={len(spec.text.encode('utf-8'))}") in text
    assert "playbooks/spec/task-brief.md" not in text
    assert "playbooks/spec/copywriting.md" not in text and "playbooks/spec/ux-review.md" not in text
    assert "playbooks/spec/data-health.md" not in text
    for skill in registry_.skills.values():
        assert f'playbooks/skill/{skill.name}.md: root:root 0644 sha256={skill.sha256}' in text


def test_an_agent_without_the_capability_gets_no_specialization(host: Host) -> None:
    data = host.manifest("dev")
    data["capabilities"] = [c for c in data["capabilities"] if c not in ("git.develop", "git.mirror")]
    host.set_manifest("dev", data)
    assert "playbooks/spec" not in render_plan(host.plan("dev"))
    assert "playbooks/spec" not in render_plan(host.plan("boss"))


def test_a_broken_specialization_registry_is_a_plan_error(host: Host) -> None:
    (host.repo / "common/specializations/specializations.json").write_text("{", encoding="utf-8")
    assert any("specializations" in e for e in _errors(host, "dev"))


def test_plan_is_the_same_on_another_root(tmp_path: Path) -> None:
    first = render_plan(_make_host(tmp_path / "a").plan())
    second = render_plan(_make_host(tmp_path / "b").plan())
    assert first == second
    assert str(tmp_path) not in first


def test_order_states_where_each_right_is_granted(host: Host) -> None:
    plan = host.plan()
    kinds = [action.kind for action in plan.actions]
    # A08 prepares the user before any capability grant.
    board = kinds.index("board-grant")
    assert kinds.index("user") < board
    # the board grant is an ACL, never a group.
    assert kinds[board : board + 4] == [
        "board-grant",
        "acl",
        "acl",
        "verify",
    ]
    assert "after policy publication" in plan.actions[board].detail
    assert f"the board grant is deferred (step {board + 1})" in render_plan(plan)
    # channels replace the capability sudoers; the grants files, the switch, come last.
    assert [(a.kind, a.target) for a in plan.actions[board + 4 :]] == [
        ("file", "/etc/systemd/system/digipaddock-cap.target"),
        ("file", "/etc/systemd/system/digipaddock-cap-boss-base.socket"),
        ("file", "/etc/systemd/system/digipaddock-cap-boss-base@.service"),
        ("file", "/etc/systemd/system/digipaddock-cap-boss-data.socket"),
        ("file", "/etc/systemd/system/digipaddock-cap-boss-data@.service"),
        ("file", "/etc/systemd/system/digipaddock-cap-boss-host.socket"),
        ("file", "/etc/systemd/system/digipaddock-cap-boss-host@.service"),
        ("file", "/etc/systemd/system/digipaddock-cap-boss-self.socket"),
        ("file", "/etc/systemd/system/digipaddock-cap-boss-self@.service"),
        ("file", "/etc/tmpfiles.d/digipaddock-cap-boss.conf"),
        ("dir", "/etc/digipaddock/agent-bin/boss"),
        ("link", "/etc/digipaddock/agent-bin/boss/kd-boss-board"),
        ("link", "/etc/digipaddock/agent-bin/boss/kd-data"),
        ("link", "/etc/digipaddock/agent-bin/boss/kd-host-read"),
        ("link", "/etc/digipaddock/agent-bin/boss/kd-monitor-read"),
        ("link", "/etc/digipaddock/agent-bin/boss/kd-notes"),
        ("link", "/etc/digipaddock/agent-bin/boss/kd-nudge"),
        ("link", "/etc/digipaddock/agent-bin/boss/kd-task"),
        ("link", "/etc/digipaddock/agent-bin/boss/kd-worker"),
        ("unit", "digipaddock-cap.target"),
        ("unit", "digipaddock-cap-boss-base.socket"),
        ("unit", "digipaddock-cap-boss-data.socket"),
        ("unit", "digipaddock-cap-boss-host.socket"),
        ("unit", "digipaddock-cap-boss-self.socket"),
        ("file", "/etc/digipaddock/cap/boss/base.json"),
        ("file", "/etc/digipaddock/cap/boss/data.json"),
        ("file", "/etc/digipaddock/cap/boss/host.json"),
        ("file", "/etc/digipaddock/cap/boss/self.json"),
        ("verify", "boss"),
    ]
    # the group digipaddock-cabinet opens the board's socket only; what a caller may do there the cabinet
    # decides by
    # its principal (agents.json), so joining it during --prepare grants no right before the policy is published.
    socket_group = [i for i, a in enumerate(plan.actions) if (a.kind, a.target) == ("group", "digipaddock-cabinet")]
    assert len(socket_group) == 1 and kinds.index("user") < socket_group[0] < board
    grants = [i for i, k in enumerate(kinds) if k in ("acl", "group", "board-grant") and i not in socket_group]
    assert all(i >= board for i in grants)
    assert kinds.index("reload") < board


def test_board_grant_is_the_one_add_user_applies(host: Host) -> None:
    plan = host.plan()
    grant = board_grant("digi-boss", host.repo)
    assert (grant.access, grant.path) == ("read", BACKLOG)
    actions = [(a.kind, a.target, a.detail) for a in plan.actions if a.kind == "acl"]
    assert actions == [
        (
            "acl",
            BACKLOG,
            "u:digi-boss:rX,d:u:digi-boss:rX (by root, on the directory itself)",
        ),
        (
            "acl",
            f"{BACKLOG}/**",
            "directories u:digi-boss:rX,d:u:digi-boss:rX, files u:digi-boss:rX"
            " (only entries owned by digi, set by digi)",
        ),
    ]
    assert not any(a.target == BOARD for a in plan.actions)  # no grant on the root


def test_board_acl_strings_are_add_user_strings() -> None:
    directories, files = board_acl("read", "$u")
    for name, value in (("dir_acl", directories), ("file_acl", files)):
        assert f'{name}="{value}"\n' in ADD_USER
    assert board_acl("none", "$u") == ("", "")
    assert "case $access in\n  read)\n" in ADD_USER and "kd-agents" not in ADD_USER


def test_add_user_effects_are_the_script_effects() -> None:
    sources = ADD_USER + HINDSIGHT_LIB
    for needle in (
        'bash "$REPO/common/install/claude_setup.sh" install',
        "cd ~ && rm -f -- .local/bin/claude && rm -rf -- .local/share/claude",
        "mkdir -p ~/.config && chmod 0700 ~/.config",
        "@vectorize-io/hindsight-coding-agents",
        "~/.hindsight/coding-agent.json",
        install_plan.HINDSIGHT_ENV,
        "useradd --create-home --user-group --shell /bin/bash",
        'runuser -u "$u" -- chmod 0750 "/home/$u"',
        'runuser -u "$base" -- chmod 0750 "/home/$base"',
    ):
        assert needle in sources, needle
    # the group the user joins is the cabinet's socket group, as cabinet_setup.sh names it
    cabinet_setup = (REPO / "common/install/cabinet_setup.sh").read_text("utf-8")
    group = re.search(r"^GROUP=(\S+)$", cabinet_setup, re.M).group(1)
    assert ("group", group) in {(k, t) for k, t, _d in install_plan.ADD_USER_EFFECTS}
    described = " ".join(f"{t} {d}" for _k, t, d in install_plan.ADD_USER_EFFECTS)
    for needle in (
        "common/install/claude_setup.sh",
        "/opt/digipaddock-claude/claude",
        "@vectorize-io/hindsight-coding-agents",
        ".hindsight/coding-agent.json",
        ".local/bin/claude, .local/share/claude",
    ):
        assert needle in described, needle


def test_add_user_effects_are_listed_without_values(host: Host) -> None:
    actions = _actions(host.plan())
    key = actions[("file", "/home/digi-boss/.hindsight/coding-agent.json")]
    assert "API key (value not shown)" in key
    assert "network" in actions[("download", "/opt/digipaddock-claude/claude")]  # root's, outside the home
    assert not [k for k in actions if k[1].endswith("/.local/bin/claude")]
    assert not [k for k in actions if ".bun" in k[1]]  # Bun ran only the Telegram plugin
    assert "bun" not in (ADD_USER + HINDSIGHT_LIB).lower()
    assert "network" in actions[("plugin", "hindsight")]


def test_add_user_installs_no_telegram_plugin() -> None:
    # no agent has a Telegram channel; add_user.sh installs no plugin for one.
    assert "telegram" not in ADD_USER.lower()
    assert "claude-plugins-official" not in ADD_USER
    described = " ".join(f"{t} {d}" for _k, t, d in install_plan.ADD_USER_EFFECTS)
    assert "telegram" not in described.lower()


def test_plan_writes_no_panel_units_and_no_launch_sudoers(host: Host) -> None:
    # the session host runs every agent; no sudoers file, no panel unit, no channel.
    plan = host.plan()
    assert not any(f.path.startswith(f"{install_plan.SUDOERS_DIR}/") for f in plan.files)
    text = render_plan(plan)
    for word in (
        "sudoers.d",
        "digipaddock.service",
        "owner-chat",
        "channels/tg-",
        "telegram",
        "Telegram",
    ):
        assert word not in text, word
    assert not any(a.kind == "unit" and not a.target.startswith("digipaddock-cap") for a in plan.actions)


def test_owner_step_is_the_hosts_claude_token_before_the_session_host(host: Host) -> None:
    assert host.plan().owner_steps == (
        "if the host has no Claude token yet: claude setup-token, then kd-secret put claude-token"
        " (once for all agents; a login of digi-boss also works);"
        " the session host starts boss after session_host_setup.sh enable boss",
    )


def test_the_launcher_is_the_one_the_session_host_runs() -> None:
    assert install_plan.LAUNCHER == session_claude.LAUNCH


def test_plan_files_match_the_compiled_policy(host: Host) -> None:
    plan = host.plan()
    files = {f.path: f.content for f in plan.files}
    folder = f"{AGENTS_ROOT}/boss"
    policy = files[f"{folder}/.claude/policy.json"]
    settings = json.loads(files[f"{folder}/.claude/settings.json"])
    launch = json.loads(files[f"{folder}/.claude/launch-settings.json"])
    assert policy == render_policy(plan.policy)
    assert (
        launch_module.check_compiled(
            json.loads(policy), name="boss", user="digi-boss"
        ).model
        == plan.manifest.model
    )
    assert settings == plan.policy["settings"]
    assert launch == plan.policy["launch_settings"]
    assert launch["sandbox"]["network"]["allowedDomains"] == []


def test_sysadmin_plan_gets_its_own_user(host: Host) -> None:
    # M02: the sysadmin leaves the shared user; the board write comes from its manifest.
    plan = host.plan("sysadmin")
    actions = _actions(plan)
    assert actions[("user", "digi-sysadmin")].startswith(
        "common/install/add_user.sh digi-sysadmin --prepare"
    )
    assert ("user", "digi") not in actions
    assert actions[("board-grant", BACKLOG)].startswith("read:")
    assert actions[("service-user", "digi-exec")] == "present"
    paths = {f.path: f.content for f in plan.files}
    assert "/etc/sudoers.d/kd-sysadmin-launch" not in paths
    assert "/etc/sudoers.d/kd-sysadmin-capabilities" not in paths


def test_sysadmin_channels_replace_capability_sudoers(host: Host) -> None:
    # Grants files rendered with the agent's uid, socket and service
    # units, tmpfiles, agent-bin links for every self command; no sudoers file.
    host.add_account("digi-sysadmin:x:1003:1003::/home/digi-sysadmin:/bin/bash", {"digi-sysadmin": "1003:"})
    plan = host.plan("sysadmin")
    files = {f.path: f for f in plan.files}
    actions = _actions(plan)
    code = LAYOUT["code"]
    channels = {c["key"]: c for c in plan.policy["channels"]}
    assert sorted(channels) == ["base", "exec", "host", "self"]
    for key, channel in channels.items():
        grants = files[f"/etc/digipaddock/cap/sysadmin/{key}.json"]
        assert (grants.owner, grants.mode, grants.show) == ("root:root", "0644", True)
        assert grants.content == cap_grants.render("sysadmin", 1003, channel)
        assert f"/etc/systemd/system/digipaddock-cap-sysadmin-{key}.socket" in files
        assert f"/etc/systemd/system/digipaddock-cap-sysadmin-{key}@.service" in files
        assert ("unit", f"digipaddock-cap-sysadmin-{key}.socket") in actions
    assert b"SocketGroup=digi-sysadmin" in files["/etc/systemd/system/digipaddock-cap-sysadmin-exec.socket"].content
    assert b"User=digi-exec" in files["/etc/systemd/system/digipaddock-cap-sysadmin-exec@.service"].content
    assert "/etc/tmpfiles.d/digipaddock-cap-sysadmin.conf" in files
    links = {t: d for (k, t), d in actions.items() if k == "link" and "/agent-bin/" in t}
    assert links == {
        f"/etc/digipaddock/agent-bin/sysadmin/{command}": f"-> {code}/common/bin/kd-cap"
        for command in channels["self"]["commands"]
    }
    assert not any(path.startswith("/etc/sudoers.d/") for path in files)
    assert not any(kind == "remove" for kind, _ in actions)
    assert "sudo -n -l -U digi-sysadmin grants nothing" in actions[("verify", "sysadmin")]


def test_grants_of_a_user_not_yet_created_are_rendered_at_apply(host: Host) -> None:
    plan = host.plan()  # digi-boss does not exist yet
    assert not any(f.path.startswith("/etc/digipaddock/cap/") for f in plan.files)
    detail = _actions(plan)[("file", "/etc/digipaddock/cap/boss/self.json")]
    assert detail == "root:root 0644, rendered with the uid of digi-boss after its creation"


def test_module_setup_runs_after_user_data_and_before_any_grant(host: Host) -> None:
    plan = host.plan("dev")
    kinds = [a.kind for a in plan.actions]
    setups = [a for a in plan.actions if a.kind == "setup"]
    assert [(a.target, a.detail.split(":")[0]) for a in setups] == [
        ("/opt/digipaddock/common/install/data_read_setup.sh", "data.read"),
        ("/opt/digipaddock/agents/dev/install/forge_setup.sh", "forge.review"),
        (
            "/opt/digipaddock/agents/dev/install/develop_setup.sh",
            "git.develop",
        ),
        # after git.develop, whose key it uses
        ("/opt/digipaddock/agents/dev/install/mirror_read_setup.sh", "git.mirror"),
        ("/opt/digipaddock/common/install/host_read_setup.sh", "host.read"),
    ]
    first = kinds.index("setup")
    assert kinds.index("trust") < first and kinds[first + 1 : first + 3] == ["setup", "setup"]
    assert first + 3 <= kinds.index("board-grant")
    # The coordinator's module setups are the data user's and the host reader's
    assert [a.target for a in host.plan().actions if a.kind == "setup"] == [
        "/opt/digipaddock/common/install/data_read_setup.sh", "/opt/digipaddock/common/install/host_read_setup.sh"]


def test_dev_agent_gets_read_only_board_access(host: Host) -> None:
    # board.write adds no file write.
    actions = _actions(host.plan("dev"))
    assert actions[("board-grant", BACKLOG)].startswith(
        "read: decided by kd.board_grant"
    )
    assert actions[("acl", BACKLOG)].startswith("u:digi-dev:rX,d:u:digi-dev:rX")
    assert ("verify", "digi-dev") in actions
    assert actions[("service-user", "digi-forge")] == "present"


# --- read only and no secrets ------------------------------------------------------------


def test_successful_runs_change_no_byte(host: Host, capsysbinary) -> None:
    before = _fingerprint(host.root)
    assert host.run("--check") == 0
    assert host.run("--plan") == 0
    assert _fingerprint(host.root) == before
    out = capsysbinary.readouterr().out.decode()
    assert out.startswith("check ok: boss, ")
    assert "plan for agent boss" in out


def test_failing_runs_change_no_byte(host: Host, capsysbinary) -> None:
    _delete("/usr/bin/flock")(host)
    before = _fingerprint(host.root)
    assert host.run("--check") == 1
    assert host.run("--plan") == 1
    assert _fingerprint(host.root) == before
    captured = capsysbinary.readouterr()
    assert captured.out == b""
    assert b"missing dependency: command flock" in captured.err


def test_output_holds_no_secret(host: Host, capsysbinary) -> None:
    assert host.run("--check") == 0
    assert host.run("--plan") == 0
    _folder("ghost-agent")(host)
    assert host.run("--plan") == 1
    captured = capsysbinary.readouterr()
    for stream in (captured.out, captured.err):
        assert MARKER.encode() not in stream
    assert b"ghost-agent has no manifest" in captured.err


def test_check_reads_only_listed_state(host: Host) -> None:
    view = host.view()
    build_plan(host.repo, view, "boss")
    assert set(view.reads) == STATE_FILES
    assert view.listings == [AGENTS_ROOT]
    assert not any(path.startswith("/home/") for path in view.reads)


def test_host_view_never_reads_inside_a_home(host: Host) -> None:
    view = host.view()
    with pytest.raises(PlanError, match="inside a home"):
        view.read_bytes("/home/digi/.claude/.credentials.json")
    with pytest.raises(PlanError, match="inside a home"):
        view.read_json_object("/home/digi-dev/.claude.json")
    with pytest.raises(PlanError, match="never lists a home"):
        view.list_dir("/home/digi")
    assert view.reads == [] and view.listings == []


@pytest.mark.parametrize("path", ["relative", "/etc/../etc/passwd", "/etc//passwd", ""])
def test_host_view_takes_only_normalized_absolute_paths(host: Host, path: str) -> None:
    with pytest.raises(PlanError, match="normalized absolute path"):
        host.view().kind(path)


@pytest.mark.parametrize("name", ["../boss", "Boss", "boss/x", "-boss", "", "a" * 33])
def test_invalid_agent_name_is_refused_before_any_read(host: Host, name: str) -> None:
    view = host.view()
    with pytest.raises(PlanError, match="invalid agent name"):
        build_plan(host.repo, view, name)
    assert view.reads == [] and view.listings == []


def test_cli_refuses_extra_or_missing_arguments(host: Host) -> None:
    for argv in (
        ["--root", str(host.root)],
        ["--root", str(host.root), "--check", "boss", "--plan", "boss"],
        ["--root", str(host.root), "--check", "boss", "extra"],
        ["--root", str(host.root), "--chec", "boss"],
        ["--root", str(host.root), "--install", "boss"],
    ):
        with pytest.raises(SystemExit) as caught:
            main(argv)
        assert caught.value.code == 2, argv


# --- refusals: bad manifests -------------------------------------------------------------

Change = Callable[[Host], None]


def _manifest_change(**changes: object) -> Change:
    def change(host: Host) -> None:
        host.set_manifest("boss", {**host.manifest("boss"), **changes})

    return change


def _remove(relative: str) -> Change:
    return lambda host: (host.repo / relative).unlink()


def _playbooks_as_file(host: Host) -> None:
    shutil.rmtree(host.repo / "agents/boss/playbooks")
    _write(host.repo / "agents/boss/playbooks", "x")


# fmt: off
BAD_MANIFESTS = [
    ("unknown-field", _manifest_change(extra=True), "bad manifest: invalid manifest fields"),
    ("schema-version", _manifest_change(schema_version=3), "unsupported manifest schema"),
    ("user-field", _manifest_change(user="digi-forge"), "invalid manifest fields"),
    ("coordinator-writes", _manifest_change(capabilities=["board.read", "board.coordinate", "board.write"]), "coordinator capability conflict"),
    ("unknown-capability", _manifest_change(capabilities=["board.read", "board.coordinate", "shell.root"]), "unknown capability shell.root"),
    ("telegram-field", _manifest_change(telegram={"bot": "@FIXTURE_SYSADMIN_BOT"}), "invalid manifest fields"),
    ("name-mismatch", _manifest_change(name="bossy"), "does not match directory"),
    ("no-sandbox", _manifest_change(runtime={"sandbox_profile": "developer-v1", "working_directory": "work"}), "not allowed for profile coordinator"),
    ("no-role-text", _remove("agents/boss/CLAUDE.md"), "bad manifest: instructions"),
    ("foreign-playbook", lambda h: _write(h.repo / "agents/boss/playbooks/run.sh", "x"), "only regular .md files"),
    ("playbooks-file", _playbooks_as_file, "playbooks is not a directory"),
]
# fmt: on


@pytest.mark.parametrize(
    ("change", "expected"), [pytest.param(c, e, id=i) for i, c, e in BAD_MANIFESTS]
)
def test_bad_manifest_is_refused(
    host: Host, change: Change, expected: str, capsysbinary
) -> None:
    change(host)
    _assert_refused(host, expected)
    assert host.run("--check") == 1
    err = capsysbinary.readouterr().err.decode()
    assert err.startswith("check failed for boss:") and expected in err


def test_agent_without_manifest_is_refused(host: Host) -> None:
    _assert_refused(host, "no agents/ghost/agent.json", name="ghost")


def test_another_bad_manifest_blocks_the_check(host: Host) -> None:
    host.set_manifest("sysadmin", {**host.manifest("sysadmin"), "extra": 1})
    _assert_refused(host, "bad manifest")


def test_secret_runtime_registry_is_refused(host: Host) -> None:
    path = host.repo / registry.RESOURCES
    table = json.loads(path.read_text())
    table["resources"][install_plan.REGISTRY_RESOURCE]["secret"] = True
    _write(path, json.dumps(table))
    _assert_refused(host, "must be a public file path")


def test_runtime_registry_elsewhere_than_the_launcher_reads_is_refused(
    host: Host,
) -> None:
    path = host.repo / registry.RESOURCES
    table = json.loads(path.read_text())
    table["resources"][install_plan.REGISTRY_RESOURCE]["path"] = (
        "/etc/digipaddock/other.json"
    )
    _write(path, json.dumps(table))
    _assert_refused(host, "the file agent-launch reads")


# --- refusals: missing dependencies ------------------------------------------------------


def _delete(absolute: str) -> Change:
    def change(host: Host) -> None:
        path = host.at(absolute)
        shutil.rmtree(path) if path.is_dir() else path.unlink()

    return change


def _drop_account(user: str) -> Change:
    def change(host: Host) -> None:
        path = host.at(install_plan.PASSWD)
        lines = [
            ln for ln in path.read_text().splitlines() if not ln.startswith(f"{user}:")
        ]
        _write(path, "\n".join(lines) + "\n")

    return change


# fmt: off
MISSING = [
    ("managed-settings", _delete(install_plan.FLOOR), "managed settings"),
    ("planned-executor", _remove("common/bin/kd-nudge"), "common/bin/kd-nudge is not in the repository"),
    ("curl", _delete("/usr/bin/curl"), "command curl"),
    ("node", _delete("/usr/bin/node"), "command node"),
    ("sudo", _delete("/usr/bin/sudo"), "command sudo"),
    ("flock", _delete("/usr/bin/flock"), "command flock"),
    ("alert-command", _delete("/usr/local/bin/kd-alert"), "command kd-alert"),
    ("launcher", _delete("/usr/local/bin/agent-launch"), "missing dependency: /usr/local/bin/agent-launch"),
    ("package-installer", _delete("/usr/bin/apt-get"), "command apt-get"),
    ("alert-unit", _delete(f"{install_plan.UNIT_DIR}/digipaddock-alert@.service"), "unit /etc/systemd/system/digipaddock-alert@.service"),
    ("backlog-dir", _delete(BACKLOG), f"grant target {BACKLOG}"),
    ("agents-root", _delete(AGENTS_ROOT), "agents root"),
    ("tmpfiles", _delete("/usr/bin/systemd-tmpfiles"), "command systemd-tmpfiles"),
    ("base-user", _drop_account("digi"), "base user digi"),
    ("accounts", _delete(install_plan.GROUP), "/etc/passwd and /etc/group"),
]
# fmt: on


@pytest.mark.parametrize(
    ("change", "expected"), [pytest.param(c, e, id=i) for i, c, e in MISSING]
)
def test_missing_dependency_is_refused(
    host: Host, change: Change, expected: str
) -> None:
    change(host)
    _assert_refused(host, expected)


@pytest.mark.parametrize(
    ("agent", "resource", "setup"),
    [
        ("analyst", "repo-checkouts", "common/install/repo_setup.sh"),
        ("dev", "forge-journal", "agents/dev/install/forge_setup.sh"),
        ("sysadmin", "exec-journal", "agents/sysadmin/install/exec_setup.sh"),
    ],
)
def test_a_grant_target_the_capabilitys_setup_makes_may_be_missing(
    host: Host, agent: str, resource: str, setup: str
) -> None:
    """The empty-host run: the capability's setup makes the directory the read grant goes on, earlier in
    the same install — on a new host it is not there when the plan is drawn."""
    path = RESOURCES["resources"][resource]["path"]
    _delete(path)(host)
    text = render_plan(host.plan(agent))
    assert text.index(f"setup {LAYOUT['code']}/{setup}") < text.index(f"acl {path}:")


def test_a_grant_target_the_setup_makes_is_still_refused_as_a_file(host: Host) -> None:
    path = RESOURCES["resources"]["repo-checkouts"]["path"]
    _delete(path)(host)
    _write(host.at(path), "")
    assert any(f"grant target {path} is a file" in e for e in _errors(host, "analyst"))


def test_missing_package_becomes_an_install_action(host: Host) -> None:
    actions = [(a.kind, a.target) for a in host.plan().actions]
    assert actions[:2] == [("package", "bubblewrap"), ("package", "socat")]
    _write(host.at("/usr/bin/bwrap"), "")
    _write(host.at("/usr/bin/socat"), "")
    _write(host.at(install_plan.BWRAP_PROFILE), install_plan.BWRAP_PROFILE_TEXT)
    kinds = [a.kind for a in host.plan().actions]
    assert "package" not in kinds and "reload" not in kinds


def test_a_host_without_apparmor_gets_it_before_the_sandbox_profile(host: Host) -> None:
    """The empty-host run: a minimal Ubuntu has the kernel's user-namespace restriction but no AppArmor
    userland — writing /etc/apparmor.d/bwrap and reloading apparmor failed. The plan installs the package first."""
    host.at("/usr/sbin/apparmor_parser").unlink()
    actions = [(a.kind, a.target) for a in host.plan().actions]
    assert actions[0] == ("package", "apparmor")
    assert actions.index(("package", "apparmor")) < actions.index(("reload", "apparmor"))
    _write(host.at(install_plan.BWRAP_PROFILE), install_plan.BWRAP_PROFILE_TEXT)
    assert ("package", "apparmor") not in [(a.kind, a.target) for a in host.plan().actions]  # no profile to write


def test_existing_different_sandbox_profile_is_refused(host: Host) -> None:
    _write(host.at(install_plan.BWRAP_PROFILE), "profile bwrap flags=(unconfined) {}\n")
    _assert_refused(host, "differs from the sandbox profile")


def test_acl_tools_are_one_installable_package(host: Host) -> None:
    host.at("/usr/bin/setfacl").unlink()
    host.at("/usr/bin/getfacl").unlink()
    packages = [
        (item.kind, item.target)
        for item in host.plan().actions
        if item.kind == "package"
    ]
    assert packages == [
        ("package", "acl"),
        ("package", "bubblewrap"),
        ("package", "socat"),
    ]


# --- refusals: conflicts -----------------------------------------------------------------


def _runtime(data: dict) -> Change:
    return lambda host: host.set_runtime(data)


def _folder(name: str) -> Change:
    return lambda host: host.at(f"{AGENTS_ROOT}/{name}").mkdir(parents=True)


def _boss_account(
    home: str = "/home/digi-boss", uid: int = 1003, gid: int = 1003, **groups: str
) -> Change:
    """digi-boss with its own private group and the given ``group -> member`` extras."""

    def change(host: Host) -> None:
        extra = {"digi-boss": f"{gid}:", **groups}
        host.add_account(f"digi-boss:x:{uid}:{gid}::{home}:/bin/bash", extra)

    return change


def _both(*changes: Change) -> Change:
    def change(host: Host) -> None:
        for item in changes:
            item(host)

    return change


def _root_account(user: str) -> Change:
    def change(host: Host) -> None:
        path = host.at(install_plan.PASSWD)
        text = re.sub(
            rf"^{user}:x:\d+:\d+:", f"{user}:x:0:0:", path.read_text(), flags=re.M
        )
        _write(path, text)

    return change


# fmt: off
CONFLICTS = [
    ("unknown-folder", _folder("ghost-agent"), "installed agent ghost-agent has no manifest"),
    ("unknown-runtime", _runtime({"ghost": {"user": "digi"}}), "installed agent ghost has no manifest"),
    ("user-moved", _runtime({"dev": {"user": "digi-x"}}), "runs dev as digi-x, not digi-dev"),
    ("runtime-telegram-flag", _runtime({"dev": {"telegram": True, "user": "digi-dev"}}), "invalid entry 'dev'"),
    ("runtime-malformed", _runtime({"Ghost": {}}), "invalid entry"),
    ("runtime-extra-field", _runtime({"ghost": {"user": "digi", "bot": "x"}}), "invalid entry"),
    ("runtime-invalid-switch", _runtime({"dev": {"user": "digi-dev", "runtime": "legacy"}}), "invalid runtime"),
    ("folder-name", _folder("Bad Name"), "unexpected entry 'Bad Name'"),
    ("foreign-group", _boss_account(**{"kd-writers": "1099:digi-boss"}), "digi-boss is in group kd-writers, which is neither"),
    ("sudo-group", _boss_account(sudo="digi-boss"), "digi-boss is in group sudo"),
    # kd-agents wrote the board before the kd-task executor; a member is a conflict, not a writer.
    ("old-write-group", _boss_account(**{"kd-agents": "digi-boss"}), "digi-boss is in group kd-agents"),
    ("docker-group", _boss_account(docker="digi-boss"), "digi-boss is in group docker"),
    ("executor-group", _boss_account(**{"digi-exec": "digi-boss"}), "digi-boss is in group digi-exec"),
    ("base-group", _boss_account(digi="digi-boss"), "digi-boss is in group digi,"),
    ("shared-primary-group", lambda h: h.add_account("digi-boss:x:1003:1000::/home/digi-boss:/bin/bash"), "primary group of digi-boss is not its own group"),
    ("crowded-own-group", _boss_account(**{"digi-boss": "1003:digi"}), "primary group of digi-boss is not its own group"),
    ("foreign-home", _boss_account(home="/srv/boss"), "the home of digi-boss is not /home/digi-boss"),
    ("root-uid", _boss_account(uid=0, gid=0), "digi-boss has the root uid or group"),
    ("root-run-as", _root_account("digi"), "digi has the root uid or group"),
    ("root-service-user", _root_account("digi-forge"), "digi-forge has the root uid or group"),
    ("home-is-file", lambda h: _write(h.at("/home/digi-boss"), ""), "/home/digi-boss is not a directory"),
    ("path-entry", lambda h: _write(h.at("/usr/local/bin/kd-nudge"), ""), "/usr/local/bin/kd-nudge exists and is not the link"),
    ("work-is-file", lambda h: _write(h.at(f"{AGENTS_ROOT}/boss/work"), ""), "work is a file"),
    ("settings-is-dir", lambda h: h.at(f"{AGENTS_ROOT}/boss/.claude/settings.json").mkdir(parents=True), "settings.json is a dir"),
    ("policy-is-dir", lambda h: h.at(f"{AGENTS_ROOT}/boss/.claude/policy.json").mkdir(parents=True), "policy.json is a dir"),
    ("registry-is-dir", lambda h: (_delete(AGENTS_JSON)(h), h.at(AGENTS_JSON).mkdir()), "is a dir, not a regular file"),
    ("apparmor-profile-is-dir", lambda h: h.at(install_plan.BWRAP_PROFILE).mkdir(parents=True), "bwrap is a dir"),
    ("backlog-is-file", lambda h: (_delete(BACKLOG)(h), _write(h.at(BACKLOG), "")), f"grant target {BACKLOG} is a file"),
    ("agent-bin-is-file", lambda h: _write(h.at("/etc/digipaddock/agent-bin/boss"), ""), "agent-bin/boss is a file"),
    ("channel-grants-is-file", lambda h: _write(h.at("/etc/digipaddock/cap/boss"), ""), "cap/boss is a file"),
]
# fmt: on


@pytest.mark.parametrize(
    ("change", "expected"), [pytest.param(c, e, id=i) for i, c, e in CONFLICTS]
)
def test_conflict_is_refused(host: Host, change: Change, expected: str) -> None:
    change(host)
    _assert_refused(host, expected)


def test_check_runs_from_the_deployed_code_only(tmp_path: Path) -> None:
    host = _make_host(tmp_path)
    elsewhere = tmp_path / "checkout"
    shutil.copytree(host.repo, elsewhere)
    with pytest.raises(PlanError) as caught:
        build_plan(elsewhere, host.view(), "boss")
    assert any("is not the repository" in e for e in caught.value.errors)


def test_every_problem_is_reported_at_once(host: Host) -> None:
    _delete("/usr/bin/flock")(host)
    _folder("ghost-agent")(host)
    _boss_account(sudo="digi-boss")(host)
    errors = _errors(host)
    for text in ("command flock", "ghost-agent has no manifest", "digi-boss is in group sudo"):
        assert any(text in e for e in errors), errors


def test_existing_user_with_only_its_own_group_is_present(host: Host) -> None:
    _boss_account()(host)
    detail = _actions(host.plan())[("user", "digi-boss")]
    assert detail.startswith(
        f"{install_plan.ADD_USER} digi-boss --prepare: present, re-run"
    )


def test_install_writes_its_own_runtime_entry(host: Host) -> None:
    _runtime({"dev": {"runtime": "generated", "user": "digi-dev"}})(host)
    detail = _actions(host.plan())[("registry", AGENTS_JSON)]
    assert detail == 'set boss = {"runtime": "generated", "user": "digi-boss"}'


def test_planner_accepts_the_generated_runtime_it_will_install(host: Host) -> None:
    host.set_runtime(
        {
            "boss": {"runtime": "generated", "user": "digi-boss"},
            "dev": {"runtime": "generated", "user": "digi-dev"},
        }
    )
    assert host.plan().manifest.name == "boss"


# --- the host data -----------------------------------------------------------------------


def _host_data_change(**changes: object) -> Change:
    def change(host: Host) -> None:
        host.set_host_data({**host.host_data(), **changes})

    return change


# fmt: off
BAD_HOST_DATA = [
    ("unknown-field", _host_data_change(extra=[]), "fields must be exactly"),
    ("schema-version", _host_data_change(schema_version=1), "unsupported schema_version"),
    ("v1-agent-units", _host_data_change(agent_units=[]), "fields must be exactly"),
    ("v1-telegram-consumers", _host_data_change(telegram_consumers=[]), "fields must be exactly"),
    ("v2-agents-without-manifest", _host_data_change(agents_without_manifest=[]), "fields must be exactly"),
    ("v2-legacy-runtime", _host_data_change(legacy_runtime=[]), "fields must be exactly"),
    ("v2-schema", _host_data_change(schema_version=2), "unsupported schema_version"),
    ("unit-dependencies", _host_data_change(unit_dependencies={"units": []}), "exactly commands and units"),
    ("unit-dependency-name", _host_data_change(unit_dependencies={"commands": ["../x"], "units": []}), "unit commands: invalid names"),
    ("unit-dependency-file", _host_data_change(unit_dependencies={"commands": [], "units": ["x.socket"]}), "unit files: invalid names"),
    ("privileged-grant", _host_data_change(group_grants=[{"group": "sudo", "capability": "board.write"}]), "never a capability grant"),
    ("unknown-grant", _host_data_change(group_grants=[{"group": "kd-x", "capability": ["board.write"]}]), "unknown capability"),
]
# fmt: on


@pytest.mark.parametrize(
    ("change", "expected"), [pytest.param(c, e, id=i) for i, c, e in BAD_HOST_DATA]
)
def test_bad_host_data_is_refused(host: Host, change: Change, expected: str) -> None:
    change(host)
    _assert_refused(host, expected)


def test_tracked_host_data_is_valid() -> None:
    data = install_plan.load_host_data(REPO, registry.load_registry(REPO))
    # no group writes the board; the board socket's group (cabinet_setup.sh GROUP), a read right
    group = re.search(r"^GROUP=(\S+)$", (REPO / "common/install/cabinet_setup.sh").read_text("utf-8"), re.M).group(1)
    assert data.group_grants == {group: "board.read"}


def test_unit_dependencies_are_what_the_session_host_unit_uses() -> None:
    # the session host runs every agent; its failure alerts through digipaddock-alert@.
    data = install_plan.load_host_data(REPO, registry.load_registry(REPO))
    assert (data.unit_commands, data.unit_files) == (("kd-alert",), ("digipaddock-alert@.service",))
    host_unit = (REPO / "common/systemd/digipaddock-session-host.service").read_text("utf-8")
    assert "OnFailure=digipaddock-alert@digipaddock-session-host.service" in host_unit
    alert_unit = (REPO / "common/systemd/digipaddock-alert@.service").read_text("utf-8")
    assert "/usr/local/bin/kd-alert %i" in alert_unit



# --- symlinks (Linux; Windows needs extra rights to create them) -------------------------


def test_symlinked_state_and_path_entries(host: Host, tmp_path: Path) -> None:
    if not _symlinks_work(tmp_path):
        pytest.skip("symlinks cannot be created here")
    target = f"{LAYOUT['code']}/common/bin/kd-nudge"
    os.symlink(target, host.at("/usr/local/bin/kd-nudge"))
    assert _actions(host.plan())[("link", "/usr/local/bin/kd-nudge")] == (
        f"-> {target} (present)"
    )
    os.unlink(host.at("/usr/local/bin/kd-nudge"))
    os.symlink("/tmp/elsewhere", host.at("/usr/local/bin/kd-nudge"))
    _assert_refused(host, "is not the link")
    os.unlink(host.at("/usr/local/bin/kd-nudge"))
    os.symlink(host.at(BOARD), host.at(f"{AGENTS_ROOT}/linked"))
    _assert_refused(host, "is a symlink, not an agent folder")
    os.unlink(host.at(f"{AGENTS_ROOT}/linked"))
    real = host.at(AGENTS_JSON).with_name("real.json")
    host.at(AGENTS_JSON).rename(real)
    os.symlink(real, host.at(AGENTS_JSON))
    _assert_refused(host, "is a symlink, not a regular file")


# --- the shell entrypoint ----------------------------------------------------------------


def _bash() -> str | None:
    for candidate in (r"C:\Program Files\Git\bin\bash.exe", shutil.which("bash")):
        if candidate and Path(candidate).exists():
            try:
                probe = subprocess.run([candidate, "-c", "true"], timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if probe.returncode == 0:
                return candidate
    return None


BASH = _bash()


def test_entrypoint_is_a_thin_finite_argv_call() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    code = [
        ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")
    ]
    assert code[0] == "set -euo pipefail"
    assert text.count("exec /usr/bin/python3 -I -c ") == 2
    assert '"$common" --root / "$1" "$2"' in text
    assert (
        'common=$(CDPATH= cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")/.." >/dev/null'
        in text
    )
    # No effect of its own: the only redirections are the usage text and the silenced cd.
    body = "\n".join(ln for ln in code if not ln.lstrip().startswith('echo "usage:'))
    assert ">" not in body.replace(">&2", "").replace(">/dev/null", "")
    for word in ("eval", "sudo", "rm ", "setfacl", "visudo", "systemctl", "useradd"):
        assert word not in body, word


@pytest.mark.skipif(BASH is None, reason="no working bash")
@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--check"],
        ["--plan"],
        ["--install", "boss", "--owner-id"],
        ["--install", "boss", "--owner-id", "738"],
        ["--migrate", "boss"],
        ["--check", "boss", "x"],
        ["boss", "--check"],
    ],
)
def test_entrypoint_refuses_other_argv_before_python(argv: list[str]) -> None:
    # --owner-id is gone with the agents' Telegram chats.
    proc = subprocess.run(
        [BASH, str(SCRIPT), *argv], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 2, proc.stderr
    assert proc.stderr.startswith(
        "usage: new_agent.sh --check <agent> | --plan <agent>"
    )
    assert proc.stdout == ""


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or not Path("/usr/bin/python3").exists(),
    reason="the entrypoint runs /usr/bin/python3 on a Linux host",
)
def test_entrypoint_runs_the_check() -> None:
    proc = subprocess.run(
        ["bash", str(SCRIPT), "--check", "no-such-agent"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 1
    assert "no agents/no-such-agent/agent.json" in proc.stderr


def test_checklist_names_only_the_modes_the_entrypoint_accepts() -> None:
    checklist = (REPO / "docs/agents/new-agent-checklist.md").read_text("utf-8")
    named = set(re.findall(r"new_agent\.sh (--[a-z-]+)", checklist))
    assert named == {"--check", "--plan", "--install", "--disable"}
    usage = re.search(r'echo "usage: (.*)" >&2', SCRIPT.read_text("utf-8"))
    assert usage is not None
    assert named == set(re.findall(r"--(?:check|plan|install|disable)\b", usage[1]))


# --- the owner's language is the deployment's (host.json owner_language) --------------------------
def test_the_owner_language_comes_from_host_json(host: Host, tmp_path: Path, monkeypatch) -> None:
    assert "owner_language" not in host.host_data()  # no longer the code's: agent-host.json schema 4
    template = _template().replace("Shared Cabinet rule for {{agent}}.", "Write to the owner in {{owner_language}}.")
    _write(host.repo / install_plan.TEMPLATE, template)
    claude = lambda: {g.path: g.content for g in host.plan("dev").files}["/srv/digipaddock-agents/dev/CLAUDE.md"]  # noqa: E731
    assert b"in Russian" in claude()  # the fixture's host is the first deployment's: ru
    data = json.loads(Path(naming.host_json_path()).read_text("utf-8"))
    for language, written in (("en", b"in English"), ("de", None), ("", None)):
        path = tmp_path / f"host-{language or 'empty'}.json"
        path.write_text(json.dumps({**data, "owner_language": language}), "utf-8")
        monkeypatch.setenv("KD_HOST_JSON", str(path))
        if written:
            assert written in claude()
        else:
            _assert_refused(host, "owner_language", "dev")



def test_texts_are_installed_with_host_values_and_overlay_includes(host: Host) -> None:
    # the role text and playbooks name no host; the plan renders them from host.json and the tree
    _write(host.repo / "agents/dev/CLAUDE.md", "# dev\n\nHome: {{host.stage_alias}}.\n\n{{include:agents/dev/site.md}}\n")
    _write(host.repo / "agents/dev/site.md", "## Site\n\nCabinet {{host.cabinet_origin}}.\n")
    _write(host.repo / "agents/dev/playbooks/pr.md", "KD-<n> — {{host.cabinet_origin}}/cards/KD-<n>\n")
    _write(host.repo / "agents/dev/playbooks/pr.example.md", "an example, never installed\n")
    plan = host.plan("dev")
    files = {generated.path: generated.content for generated in plan.files}
    claude = files["/srv/digipaddock-agents/dev/CLAUDE.md"].decode("utf-8")
    assert "Home: example-stage." in claude and "## Site\n\nCabinet https://cabinet.example.test." in claude
    assert files["/srv/digipaddock-agents/dev/playbooks/pr.md"] == b"KD-<n> \xe2\x80\x94 https://cabinet.example.test/cards/KD-<n>\n"
    assert not any(path.endswith("pr.example.md") for path in files)


def test_the_memory_section_follows_the_memory_service(host: Host) -> None:
    # the same condition as the Hindsight plugin of add_user.sh — its env on the host
    claude = lambda: {g.path: g.content for g in host.plan("dev").files}["/srv/digipaddock-agents/dev/CLAUDE.md"]  # noqa: E731
    assert b"\n## Memory\n" in claude()
    host.at(install_plan.HINDSIGHT_ENV).unlink()
    without = claude().decode("utf-8")
    assert "\n## Memory\n" not in without and "\n## Notes" in without


def test_a_text_naming_a_missing_include_refuses_the_plan(host: Host) -> None:
    _write(host.repo / "agents/dev/CLAUDE.md", "# dev\n\n{{include:agents/dev/site.md}}\n")
    with pytest.raises(PlanError, match="instructions"):
        host.plan("dev")
