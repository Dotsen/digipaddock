import base64
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

COMMON = Path(__file__).resolve().parent.parent
INSTALL = COMMON / "install"


def _read(rel):
    return (COMMON / rel).read_text(encoding="utf-8")


def _mode(text, mode):
    cases = text[text.rindex("\ncase $mode in"):]
    return re.search(rf"^  {mode}\)(.*?);;", cases, re.M | re.S).group(1)


def test_host_values_are_read_by_an_assignment_that_set_e_sees():
    """Review M9: $(kd_host …) inside another command's argument hides its failure from set -e — the value
    becomes empty («digipaddock-board@»); an assignment of its own fails the script. A message text may name one."""
    lines = []
    for path in [*INSTALL.glob("*.sh"), *COMMON.parent.glob("agents/*/install/*.sh")]:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            uses = line.count("$(kd_host")
            assigned = len(re.findall(r"(?:^|&&|;)\s*[A-Za-z_][A-Za-z0-9_]*=[^\s]*\$\(kd_host", line))
            if uses > assigned and "echo \"" not in line:
                lines.append(f"{path.name}:{number}")
    assert lines == []


def test_installers_name_the_cabinets_group_by_the_product():
    """the board's socket group is digipaddock-cabinet (agent-host.json); a check that looked for a group
    named like a command (kd-cabinet) stopped the stage window's install of sysadmin."""
    grants = __import__("json").loads(_read("agent-host.json"))["group_grants"]
    assert {"group": "digipaddock-cabinet", "capability": "board.read"} in grants
    found = [f"{path.name}:{number}"
             for path in [*INSTALL.glob("*.sh"), *COMMON.parent.glob("agents/*/install/*.sh")]
             for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
             if re.search(r"id -nG|getent group|-aG|gpasswd|chgrp", line) and "kd-cabinet" in line]
    assert found == []


def _function(text, name):
    start = text.index(f"\n{name}() {{")
    return text[start:text.index("\n}\n", start)]


HOME_PATH = re.compile(r"/home/(digi|\$\{?u(ser)?\b)")  # /home/digi, /home/digi-dev, /home/$user, /home/$u


def _root_ops_on_agent_home(text):
    """The lines where root changes an owner/permissions or writes by a path in the home of an agent's user."""
    bad = []
    for line in text.splitlines():
        if line.lstrip().startswith("#") or not HOME_PATH.search(line):
            continue
        if re.match(r"\s*(chown|chmod|install|ln|cp|mv|tee)\b", line) or re.search(r">\s*\"?/home/", line):
            bad.append(line)
    return bad


def test_root_ops_check_sees_every_agent_home():
    # the pattern catches the homes of all agent users, not only /home/digi
    for line in ('chown digi-dev "/home/digi-dev/x"', 'chmod 0600 /home/$user/.claude/x', 'install -m 0600 f /home/${u}/x',
                 'echo x > "/home/$user/.env"', 'cp f /home/digi/x'):
        assert _root_ops_on_agent_home(line) == [line], line
    assert _root_ops_on_agent_home('runuser -u "$user" -- mkdir -p /home/$user/x') == []
    assert _root_ops_on_agent_home('chmod 0644 /home/example/x') == []


def test_stage_setup_never_touches_agent_home_as_root():
    text = _read("install/stage_setup.sh")
    assert _root_ops_on_agent_home(text) == []
    text = text.replace("install -d -o digi -g digi -m 0750 /var/log/digipaddock-board", "")
    assert "install -o digi" not in text and "install -d -o digi" not in text
    assert "as_agent" in text


def test_installers_link_commands_by_the_hosts_names():
    # a command on PATH is named by the host (naming.json "commands"); the target stays the tree's file
    for rel, words in (
        ("install/stage_setup.sh", ("alert", "usage", "board", "task", "release")),
        ("install/cabinet_setup.sh", ("cabinet",)),
        ("install/mirror_setup.sh", ("mirror",)),
        ("install/repo_setup.sh", ("repo-sync",)),
        ("install/session_host_setup.sh", ("session-host",)),
    ):
        text = _read(rel)
        for word in words:
            var = word.upper().replace("-", "_") + "_LINK"
            assert f"\n{var}=/usr/local/bin/$(kd_cmd kd-{word})\n" in text, (rel, word)
            assert f'ln -sfn "$REPO/common/bin/kd-{word}" "${var}"' in text, (rel, word)
        assert not [line for line in text.splitlines() if "/usr/local/bin/kd-" in line and not line.lstrip().startswith("#")]


def test_stage_setup_writes_the_hosts_names_once():
    text = _read("install/stage_setup.sh")
    assert "--commands) [ $# -ge 2 ] && [[ $2 =~ ^[a-z][a-z0-9]{1,7}$ ]] || usage; commands=$2; shift 2 ;;" in text
    assert "--cards) [ $# -ge 2 ] && [[ $2 =~ ^[A-Z][A-Z0-9]{1,5}$ ]] || usage; cards=$2; shift 2 ;;" in text
    assert '"${prefix:-digi}" "${commands:-kd}" "${cards:-KD}" > "$tmp"' in text
    assert "read -r _ host_commands host_cards < <(kd_names)" in text
    assert '[ -z "$commands" ] || [ "$commands" = "$host_commands" ]' in text
    assert '[ -z "$cards" ] || [ "$cards" = "$host_cards" ]' in text


def test_stage_setup_links_only_the_session_launcher():
    # no panel launcher or attach link; the session host runs the agents
    text = _read("install/stage_setup.sh")
    for gone in ("start-agent", "kd-attach"):
        assert gone not in text, gone
    assert 'ln -sfn "$REPO/common/bin/agent-launch" /usr/local/bin/agent-launch' in text
    assert "trap" in text and 'rm -f "$tmp"' in text


def test_stage_setup_installs_no_bun():
    text = _read("install/stage_setup.sh")
    assert "bun" not in text.lower() and "unzip" not in text  # unzip was the Bun installer's
    assert '[ -x "/home/$base/.local/bin/claude" ] || { echo "Claude Code did not install" >&2; exit 1; }' in text


def test_render_host_units_are_in_the_tree():
    host = _read("install/render_host.sh")
    loop = re.search(r'^UNITS="(.*)"$', host, re.M).group(1).split()
    for unit in loop:
        assert (COMMON / "systemd" / unit).is_file(), unit


def test_stage_setup_builds_no_backlog_and_installs_the_cabinet():
    # the board is the cabinet; the Backlog.md fork, its web and its CLI are gone
    text = _read("install/stage_setup.sh")
    for gone in ("BACKLOG", "backlog init", "/usr/local/bin/backlog", "common/board/", "kd-board-web", "backlog/.locks"):
        assert gone not in text, gone
    call = 'bash "$REPO/common/install/cabinet_setup.sh" install'
    assert text.count(call) == 1
    # the cabinet's origin is given once, at the first install (cabinet.json keeps it)
    assert 'bash "$REPO/common/install/cabinet_setup.sh" install ${origin:+--origin "$origin"}' in text
    assert "--origin) [ $# -ge 2 ] && [[ $2 =~ $ORIGIN_RE ]] || usage; origin=$2; shift 2 ;;" in text
    render = text.index('bash "$REPO/common/install/render_host.sh"')
    assert render < text.index(call) < text.index("systemctl enable --now digipaddock-board-sync.path")
    assert not (COMMON / "board" / "backlog").exists() and not (COMMON / "board" / "config.yml").exists()
    assert not (COMMON / "systemd" / "digipaddock-board-web.service").exists()


def test_stage_setup_board_directory_is_the_root_owned_snapshot():
    text = _read("install/stage_setup.sh")
    # kd-cabinet export (root) writes the Markdown snapshot and commits it: no other user owns a directory on its path
    assert ("install -d -o root -g root -m 0755 /srv/digipaddock-agents/board /srv/digipaddock-agents/board/backlog "
            "/srv/digipaddock-agents/board/backlog/tasks") in text
    assert 'install -d -o "$base"' not in text.replace('install -d -o "$base" -g "$base" -m 0750 /var/log/digipaddock-board', "")
    assert "git -C /srv/digipaddock-agents/board init -q" in text
    assert 'git config --global --get-all safe.directory | grep -qxF /srv/digipaddock-agents/board ||' in text
    assert 'ln -sfn "$REPO/common/bin/kd-board" "$BOARD_LINK"' in text
    assert 'ln -sfn "$REPO/common/bin/kd-task" "$TASK_LINK"' in text


# --- cabinet_setup.sh ----------------------------------------------------------------------------


def test_cabinet_setup_modes_user_group_and_units():
    text = _read("install/cabinet_setup.sh")
    assert text.startswith("#!/usr/bin/env bash") and "set -euo pipefail" in text
    assert "usage: cabinet_setup.sh install [--origin https://<host>] | check | disable | grant <user>" in text
    assert '[ "$(id -u)" = 0 ] || { echo "root is required" >&2; exit 1; }' in text
    assert "cabinet=$(kd_user cabinet)" in text and "base=$(kd_user base)" in text
    # the cabinet service user and the push sender's (global review 15.10.2026), both nologin
    assert 'for u in "$cabinet" "$push"; do' in text and 'push=$(kd_user push)' in text
    assert 'useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$u"' in text
    assert 'groupadd -f -r "$GROUP"' in text and "GROUP=digipaddock-cabinet" in text
    assert 'install -d -o "$cabinet" -g "$cabinet" -m 0750 "$DATA"' in text
    assert "DATA=/var/lib/digipaddock/cabinet" in text
    assert 'ln -sfn "$REPO/common/bin/kd-cabinet" "$CABINET_LINK"' in text
    units = re.search(r'^CABINET_UNITS="(.*)"$', text, re.M).group(1).split()
    assert units == ["digipaddock-openai-bridge.service", "digipaddock-model-keys.socket", "digipaddock-model-keys.service",
                     "digipaddock-cabinet.socket", "digipaddock-cabinet-http.socket", "digipaddock-cabinet.service", "digipaddock-cabinet-push.service",
                     "digipaddock-cabinet-export.service", "digipaddock-cabinet-export.timer", "digipaddock-board-sync.path", "digipaddock-board-grants.path",
                     "digipaddock-board-deliver.service", "digipaddock-board-deliver.timer",
                     "digipaddock-alert-watch.service", "digipaddock-alert-watch.timer",
                     "digipaddock-hindsight-status.service", "digipaddock-hindsight-status.timer"]
    for unit in units:
        assert (COMMON / "systemd" / unit).is_file(), unit
    assert 'kd_install_rendered "$REPO/common/systemd/$unit" "$UNITS/$unit" 0644' in text
    assert 'kd_render "$REPO/common/systemd/$unit" | cmp -s - "$UNITS/$unit"' in text
    enabled = re.search(r'^ENABLED="(.*)"$', text, re.M).group(1).split()
    assert enabled == ["digipaddock-openai-bridge.service", "digipaddock-model-keys.socket", "digipaddock-cabinet.socket", "digipaddock-cabinet-http.socket", "digipaddock-cabinet-push.service", "digipaddock-cabinet-export.timer",
                       "digipaddock-board-sync.path", "digipaddock-board-grants.path", "digipaddock-board-deliver.timer", "digipaddock-alert-watch.timer",
                       "digipaddock-hindsight-status.timer"]
    assert 'systemctl enable --now $ENABLED' in text
    # check asks the board itself, as the base user
    assert 'runuser -u "$base" -- "$CABINET_LINK" cards' in text
    # members: the base user, exec when present, every agent user of the registry (approve is retired)
    members = _function(text, "members")
    assert 'u=$(kd_user exec)' in members and "agents.users()" in members and "approve" not in members
    # grant: one user joins the group; the group comes from install
    assert 'usermod -a -G "$GROUP" "$user"' in text
    assert "systemctl disable --now $ENABLED digipaddock-cabinet.service" in _mode(text, "disable")


@pytest.mark.parametrize("argv", [[], ["bogus"], ["install", "x"], ["grant"], ["grant", "Bad User"], ["grant", "a", "b"],
                                  ["install", "--origin"], ["install", "--origin", "http://board.example"],
                                  ["install", "--origin", "https://board.example/path"], ["check", "--origin", "x"]])
def test_cabinet_setup_usage(argv):
    bash = shutil.which("bash")
    if bash is None or os.name == "nt":
        pytest.skip("bash")
    p = subprocess.run([bash, str(INSTALL / "cabinet_setup.sh"), *argv], capture_output=True, encoding="utf-8")
    assert p.returncode == 2 and "usage: cabinet_setup.sh" in p.stderr


# --- the cabinet's web — venv, settings, frontend build, health ------------------------------------------


def test_cabinet_setup_refuses_before_changing_anything():
    # no Node.js >= 20, or no cabinet.json and no --origin, or another origin than the stored one: nothing is touched
    text = _read("install/cabinet_setup.sh")
    install = _function(text, "install_all")
    first_change = install.index("useradd")
    for guard in ("need_node", "need_settings"):
        assert guard in install and install.index(guard) < first_change, guard
    node = _function(text, "need_node")
    assert "NODE_MIN=20" in text and '[ "$major" -ge "$NODE_MIN" ]' in node
    settings = _function(text, "need_settings")
    assert '[ -e "$SETTINGS" ] || [ -n "$origin" ]' in settings
    assert "passkey" in settings  # why another origin is refused: the keys are bound to it


def test_cabinet_setup_installs_the_venv_once_per_requirements():
    text = _read("install/cabinet_setup.sh")
    venv = _function(text, "venv")
    assert "VENV=/var/lib/digipaddock/venv/cabinet" in text
    assert 'python3 -m venv "$VENV"' in venv
    # every package hash-checked; the one source-only package builds with the locked setuptools,
    # never with a build backend fetched unchecked for an isolated build
    pip = '"$VENV/bin/pip" install --disable-pip-version-check --no-input -q --require-hashes --only-binary=:all:'
    assert f'{pip} -r "$BUILD_REQUIREMENTS"' in venv
    assert f'{pip} --no-binary=http-ece --no-build-isolation -r "$REQUIREMENTS"' in venv
    assert venv.index('"$BUILD_REQUIREMENTS"') < venv.index('-r "$REQUIREMENTS"')
    assert 'cat "$BUILD_REQUIREMENTS" "$REQUIREMENTS" | sha256sum' in text
    assert '"$VENV/.requirements.sha256"' in venv
    # the push and the board services run from it
    for unit in ("digipaddock-cabinet.service", "digipaddock-cabinet-push.service"):
        assert "ExecStart=/var/lib/digipaddock/venv/cabinet/bin/python3 -I " in _read(f"systemd/{unit}"), unit


def test_cabinet_requirements_are_a_full_lock_with_hashes():
    """Every package the venv gets, transitive ones too, is pinned and hash-checked; the lock is compiled
    from the hand-written pins in requirements.in."""
    lock = (COMMON / "cabinet" / "requirements.txt").read_text(encoding="utf-8")
    pins = {}
    for line in (COMMON / "cabinet" / "requirements.in").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            name, version = line.split("==")
            pins[name.lower().replace("_", "-")] = version
    locked = {}
    for block in re.split(r"\n(?=[A-Za-z0-9])", lock):
        head = block.split("\n", 1)[0]
        if not head or head.startswith("#"):
            continue
        match = re.match(r"([A-Za-z0-9._-]+)==([^ \\]+)", head)
        assert match, head
        assert "--hash=sha256:" in block, head
        locked[match.group(1).lower().replace("_", "-")] = match.group(2)
    assert pins and all(locked.get(name) == version for name, version in pins.items()), pins
    assert {"anyio", "pydantic-core", "cffi", "h11", "http-ece"} <= set(locked)
    build = (COMMON / "cabinet" / "requirements-build.txt").read_text(encoding="utf-8")
    assert re.search(r"^setuptools==[0-9.]+ \\\n +--hash=sha256:[0-9a-f]{64}", build, re.M)


def test_cabinet_setup_writes_settings_once_and_secrets_for_the_cabinet_only():
    text = _read("install/cabinet_setup.sh")
    settings = _function(text, "settings")
    assert "SETTINGS=/etc/digipaddock/cabinet.json" in text and "SECRETS=/etc/digipaddock/cabinet.env" in text
    assert 'install -o root -g root -m 0644 "$tmp" "$SETTINGS"' in settings
    assert "openssl rand -hex 32" in settings and "generate_vapid" in settings
    assert 'install -o root -g "$cabinet" -m 0640 "$tmp" "$SECRETS"' in settings
    # made once: a new secret signs everyone out, a new VAPID key drops every push subscription
    assert '[ -e "$SECRETS" ] ||' in settings or 'if [ ! -e "$SECRETS" ]' in settings
    assert 'chown root:"$cabinet" "$SECRETS"' in settings and 'chmod 0640 "$SECRETS"' in settings
    from kd.cabinet import config

    assert config.JSON == "/etc/digipaddock/cabinet.json" and config.ENV == "/etc/digipaddock/cabinet.env"


def test_cabinet_setup_builds_the_frontend_as_the_base_user_and_serves_a_root_copy():
    text = _read("install/cabinet_setup.sh")
    build = _function(text, "build_web")
    assert "WEB=/var/lib/digipaddock/cabinet-web" in text
    files = re.search(r'^WEB_FILES="(.*)"$', text, re.M).group(1).split()
    for name in files:
        assert (COMMON / "cabinet/web" / name).exists(), name
    assert {"package.json", "package-lock.json", "src", "messages", "public", "index.html"} <= set(files)
    assert not {"e2e", "node_modules", "dist", "playwright.config.ts"} & set(files)
    assert "npm ci --ignore-scripts --no-audit --no-fund && npm run build" in build
    assert 'runuser -u "$base" --' in build  # npm runs package code: never as root
    assert 'chown -hR root:root "$WEB/dist.new"' in build and 'mv "$WEB/dist.new" "$WEB/dist"' in build
    assert '"$WEB/.source.sha256"' in build  # a release without frontend changes does not rebuild
    nginx = _read("nginx/cabinet.conf")
    assert "root /var/lib/digipaddock/cabinet-web/dist;" in nginx


def test_cabinet_setup_check_asks_the_web_and_the_push_worker():
    check = _function(_read("install/cabinet_setup.sh"), "check")
    assert ("curl -sS --max-time 20 --unix-socket \"$HTTP_SOCKET\" -o /dev/null -w '%{http_code}' "
            "http://localhost/api/health") in check
    assert 'want "$HTTP_SOCKET" "$cabinet:www-data" 660' in check
    assert 'runuser -u "$cabinet" -- "$VENV/bin/python3" -I' in check  # the cabinet itself loads its settings
    assert "systemctl is-active --quiet digipaddock-cabinet-push.service" in check or "$ENABLED" in check


def test_cabinet_setup_install_opens_the_monitor_window_and_nginx():
    text = _read("install/cabinet_setup.sh")
    assert 'kd_expect_run "cabinet_setup.sh $*' in text
    install = _function(text, "install_all")
    assert 'bash "$REPO/common/install/board_setup.sh"' in install
    assert "retire_" not in text and "kd-board-digest" not in text  # the 09:15 digest is gone
    board = _read("install/board_setup.sh")
    assert 'install -o root -g root -m 0644 "$REPO/common/nginx/digipaddock-cabinet-proxy.conf" /etc/nginx/digipaddock-cabinet-proxy.conf' in board
    # the headers name the cabinet host: rendered from host/host.json
    assert 'kd_install_rendered "$REPO/common/nginx/digipaddock-cabinet-headers.conf" /etc/nginx/digipaddock-cabinet-headers.conf 0644' in board


def test_agent_users_and_exec_join_the_cabinet_group():
    assert 'bash "$REPO/common/install/cabinet_setup.sh" grant "$u"' in _read("install/add_user.sh")
    exec_setup = (COMMON.parent / "agents/sysadmin/install/exec_setup.sh").read_text(encoding="utf-8")
    assert 'bash "$CABINET" grant "$exec_user"' in exec_setup and '*" digipaddock-cabinet "*' in exec_setup


def test_put_secret_writes_as_directory_owner():
    text = _read("install/put_secret.sh")
    writer = _read("install/secret_write.sh")  # the remote part: shared with kd-secret put
    assert "stat -c %U" in writer
    assert 'runuser -u "$downer"' in writer
    assert "trap" in writer
    for rx in ("^/[A-Za-z0-9._/-]+$", "^[a-z_][a-z0-9_-]*:[a-z_][a-z0-9_-]*$", "^[0-7]{3,4}$"):
        assert rx in text
    # the secret goes only through stdin
    assert "cat > " in writer


def _launch_code() -> str:
    """agent-launch without its comment lines."""
    return "\n".join(ln for ln in _read("bin/agent-launch").splitlines() if not ln.lstrip().startswith("#"))


# claude starts through kd.secret_fd, which hands it the credential by a pipe
EXEC_CLAUDE = "exec /usr/bin/python3 -I /opt/digipaddock/common/kd/secret_fd.py claude"


def test_agent_launch_runs_only_the_strict_session():
    # the session host's headless launch is the only one: no panel, no Telegram channel, no sudo hop.
    text = _launch_code()
    for gone in ("TELEGRAM", "--channels", "sudo", "DIGIPADDOCK", "/usr/local/bin/digipaddock", "QUIET", "BACKLOG_CWD",
                 "channels/tg-", "mcp-needs-auth-cache", "launch_args", "session_settings"):
        assert gone not in text, gone
    assert "/usr/bin/python3 -I -c" in text and "agents.user_of(sys.argv[1])" in text
    assert '[[ $user == "$(id -un)" ]]' in text  # run as the agent's own user, never hopping to it
    assert text.index("dir=/srv/digipaddock-agents/$name") < text.index('cd "$dir"') < text.index('cd "$dir/work"')
    assert "/home/digi" not in text and "export PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" in text
    assert "bun" not in text.lower()  # Bun ran only the Telegram plugin
    assert text.count(EXEC_CLAUDE) == 1 and text.count("exec ") == 1
    launch = re.sub(r"\\\n\s*", "", text[text.index(EXEC_CLAUDE):])
    assert launch.startswith(EXEC_CLAUDE + ' --settings "${plan[3]}" --add-dir "$dir" "${model_args[@]}"'
                             ' "${effort_args[@]}" -p'
                             " --input-format stream-json --output-format stream-json --verbose")
    assert '--append-system-prompt "$SESSION_NOTE"' in launch


def test_agent_launch_keeps_console_quiet():
    # nobody reads a recap after idle time (stage, 05.10.2026)
    text = _launch_code()
    assert text.count("export CLAUDE_CODE_ENABLE_AWAY_SUMMARY=0") == 1
    assert text.index("CLAUDE_CODE_ENABLE_AWAY_SUMMARY=0") < text.index(EXEC_CLAUDE)


def test_put_secret_pins_directory_and_replaces_atomically():
    text = _read("install/put_secret.sh")
    writer = _read("install/secret_write.sh")
    assert 'cd -P -- "$dir"' in writer
    assert "stat -c %U ." in writer and "stat -c %a ." in writer
    assert "mktemp ./.secret.XXXXXX" in writer
    assert writer.count('mv -fT "$tmp" "./') == 2  # both branches
    assert "*/ && $path != */." in text


def test_put_secret_refuses_symlink_in_path_right_after_cd():
    text = _read("install/secret_write.sh")
    cd = text.index('cd -P -- "$dir"')
    # the pinned inode's path — once through getcwd() (coreutils); the builtin pwd -P parses the path string again
    real = text.index("real=$(/bin/pwd -P)")
    chk = text.index('[ "$real" = "$dir" ]')
    assert cd < real < chk < text.index('downer=$(stat -c %U .)')
    assert text.count("$(/bin/pwd -P)") == 1 and "$(pwd" not in text  # the path is parsed again nowhere else
    assert "has a symlink (→ $real)" in text
    assert 'written $real/$file' in text
    assert 'printf %03d "$(stat -c %a .)"' in text


BASH = next((c for c in (r"C:\Program Files\Git\bin\bash.exe", shutil.which("bash")) if c and Path(c).exists()), None)


def _run_put_secret(path):
    """put_secret.sh locally; ssh is replaced by a function that prints the remote command (the real ssh is not called)."""
    stub = 'ssh() { printf "%s\\n" "$2"; exit 99; }; source common/install/put_secret.sh "$@"'
    return subprocess.run(
        [BASH, "-c", stub, "_", "no-such-host.invalid", path, "agent:digi", "0600"],
        cwd=COMMON.parent, capture_output=True, encoding="utf-8", errors="replace", timeout=30,
    )


@pytest.mark.skipif(BASH is None, reason="нет рабочего bash")
@pytest.mark.parametrize("path", ["/a//b/.env", "/a/./b/.env", "//a/.env", "/./a/.env"])
def test_put_secret_rejects_non_canonical_path_locally(path):
    proc = _run_put_secret(path)
    assert proc.returncode == 2, proc.stderr
    assert "non-canonical path" in proc.stderr
    assert proc.stdout == ""  # it did not get to ssh


@pytest.mark.skipif(BASH is None, reason="нет рабочего bash")
def test_put_secret_ships_heredoc_verbatim():
    path = "/etc/digipaddock/review/digipaddock-review.env"
    proc = _run_put_secret(path)
    assert proc.returncode == 99, proc.stderr  # it got to the replaced ssh
    assert proc.stdout.rstrip("\n").endswith(" _ %s agent:digi 0600" % path)
    shipped = base64.b64decode(re.search(r"echo (\S+) \| base64 -d", proc.stdout).group(1))
    writer = _read("install/secret_write.sh")
    assert shipped.decode("utf-8") == writer.rstrip("\n") + "\n"
    syntax = subprocess.run([BASH, "-n"], input=shipped, capture_output=True, timeout=30)
    assert syntax.returncode == 0, syntax.stderr


def _run_launch_venv_block(tmp_path, name, uid):
    """The venv block of agent-launch as it is; stat is replaced (the owner's uid), the venv base is the test's directory."""
    text = _read("bin/agent-launch")
    start = text.index("venv=/opt/digipaddock-venv/$name")
    block = text[start:text.index("\nfi\n", start) + 4].replace("/opt/digipaddock-venv", '"$KD_BASE"', 1)
    script = tmp_path / "launch.sh"
    script.write_text('set -euo pipefail\nstat() { printf "%s\\n" "$KD_UID"; }\nname=$1\nPATH=/usr/bin:/bin\n'
                      + block + 'printf %s "$PATH"\n', encoding="utf-8", newline="\n")
    proc = subprocess.run([BASH, str(script), name], capture_output=True, encoding="utf-8", errors="replace", timeout=30,
                          env={**os.environ, "KD_BASE": (tmp_path / "venvs").as_posix(), "KD_UID": uid})
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.mark.skipif(BASH is None, reason="нет рабочего bash")
def test_agent_launch_prepends_root_owned_agent_venv_only(tmp_path):
    # the test tools' venv is the agent's own (/opt/digipaddock-venv/<name>, root): the sysadmin without a venv does not change
    (tmp_path / "venvs" / "dev" / "bin").mkdir(parents=True)
    (tmp_path / "venvs" / "nobin").mkdir()
    venv_bin = (tmp_path / "venvs" / "dev" / "bin").as_posix()
    assert _run_launch_venv_block(tmp_path, "dev", "0") == venv_bin + ":/usr/bin:/bin"
    assert _run_launch_venv_block(tmp_path, "dev", "1001") == "/usr/bin:/bin"  # not root's — not taken
    assert _run_launch_venv_block(tmp_path, "sysadmin", "0") == "/usr/bin:/bin"
    assert _run_launch_venv_block(tmp_path, "nobin", "0") == "/usr/bin:/bin"


def test_agent_launch_venv_after_base_path():
    text = _read("bin/agent-launch")
    block = text[text.index("venv=/opt/digipaddock-venv/$name"):]
    assert "! -L $venv" in block and '$(stat -c %u "$venv/bin") = 0' in block
    assert "export PATH=$venv/bin:$PATH" in block
    assert (text.index("export PATH=$HOME/.local/bin:")
            < text.index("venv=/opt/digipaddock-venv/$name") < text.index(EXEC_CLAUDE))


def test_agent_launch_pins_claude_code_version():
    # a Claude Code update — by hand after a run on a trial agent: it may change the semantics of --channels/--settings
    text = _read("bin/agent-launch")
    assert text.count("export DISABLE_AUTOUPDATER=1") == 1
    assert text.index("DISABLE_AUTOUPDATER=1") < text.index(EXEC_CLAUDE)


def test_board_nginx_conf_auth_and_limit_and_no_backlog_web():
    conf = _read("nginx/cabinet.conf")
    for line in ("server_name @host:cabinet_host@;",
                 "limit_req zone=kd_board burst=100 nodelay;",
                 "/etc/letsencrypt/live/@host:cabinet_host@/fullchain.pem", "location ^~ /.well-known/acme-challenge/"):
        assert line in conf, line
    # the Backlog.md web (6420) is gone; the root is the cabinet's frontend
    assert "6420" not in conf and "return 503" not in conf
    # the factory host's shared nginx: the map's name does not clash with other sites
    assert "$connection_upgrade" not in conf
    assert "limit_req_zone $binary_remote_addr zone=kd_board:1m rate=30r/s;" in _read("nginx/digipaddock-board-limit.conf")


def test_board_setup_makes_no_password_and_keeps_the_webroot_cert():
    text = _read("install/board_setup.sh")
    # no basic auth
    assert "openssl" not in text and "htpasswd" not in text and "kd-board.pass" not in text
    assert ("certbot certonly --webroot -w \"/var/www/$HOST\" -d \"$HOST\" --non-interactive --keep "
            "--deploy-hook 'systemctl reload nginx'") in text
    assert "HOST=$(kd_host cabinet_host)" in text and "kd_factory_host || exit 1" in text  # NAT too
    assert "nginx -t || { rm -f /etc/nginx/sites-enabled/$HOST.conf; exit 1; }" in text


def test_stage_setup_renders_units_and_floor_through_render_host():
    # the one-time move of the stage-1 Pult token is gone; units and the managed floor carry naming tokens
    # and are installed rendered by render_host.sh, which stage_setup.sh runs before enabling anything.
    text = _read("install/stage_setup.sh")
    host = _read("install/render_host.sh")
    assert "pult.env" not in text and "managed-settings" not in text.replace("# 6. ", "")
    call = 'bash "$REPO/common/install/render_host.sh"'
    assert text.count(call) == 1 and text.index(call) < text.index("systemctl enable")
    assert 'kd_install_rendered "$REPO/common/systemd/$unit" "$SYSTEMD/$unit" 0644' in host
    assert ('kd_install_rendered "$REPO/common/claude/managed-settings.template.json" '
            "/etc/claude-code/managed-settings.json 0644") in host
    assert '[ ! -e "$SYSTEMD/$unit" ] || kd_install_rendered "$REPO/$src" "$SYSTEMD/$unit" 0644' in host
    assert host.rstrip().endswith("systemctl daemon-reload")
    assert not [ln for ln in host.splitlines() if "systemctl" in ln and "daemon-reload" not in ln]


def test_stage_setup_installs_and_enables_board_units():
    text = _read("install/stage_setup.sh")
    host = _read("install/render_host.sh")
    loop = re.search(r'^UNITS="(.*)"$', host, re.M).group(1).split()
    for unit in ("digipaddock-board-sync.path", "digipaddock-board-sync.service", "digipaddock-board-sync.timer"):
        assert unit in loop, unit
    assert "digipaddock-board-digest.service" not in loop and "digipaddock-board-digest.timer" not in loop
    assert "digipaddock-board-web.service" not in host
    # the cabinet's units are installed by cabinet_setup.sh and re-rendered where present (a prefix change)
    present = re.search(r'^PRESENT="(.*)"$', host, re.M).group(1).split()
    for unit in ("digipaddock-cabinet.socket", "digipaddock-cabinet-http.socket", "digipaddock-cabinet.service", "digipaddock-cabinet-push.service",
                 "digipaddock-cabinet-export.service", "digipaddock-cabinet-export.timer"):
        assert f"common/systemd/{unit}" in present, unit
    assert "kd-admin-approval" not in host
    # the delivery of the owner's delegation clicks is the foundation's now (the approval setup is gone)
    for unit in ("digipaddock-board-grants.path", "digipaddock-board-deliver.service", "digipaddock-board-deliver.timer"):
        assert unit in loop, unit
    assert "install -d -o root -g root -m 0750 /var/lib/digipaddock/board" in text
    assert "systemctl enable --now digipaddock-board-sync.path digipaddock-board-sync.timer\n" in text and "kd-board-digest" not in text


def test_mirror_units_are_rendered_by_render_host_and_turned_on_by_mirror_setup():
    """render_host.sh keeps the units current; only mirror_setup.sh install turns the timer on."""
    loop = re.search(r'^UNITS="(.*)"$', _read("install/render_host.sh"), re.M).group(1).split()
    assert "digipaddock-mirror.service" in loop and "digipaddock-mirror.timer" in loop
    setup = _read("install/mirror_setup.sh")
    assert "systemctl enable --now digipaddock-mirror.timer" in _function(setup, "install_all")
    assert 'exit 3' in _function(setup, "install_all") and 'KEY=$KEYS/github_deploy' in setup
    assert 'ln -sfn "$REPO/common/bin/kd-mirror" "$MIRROR_LINK"' in setup
    assert 'touch "$KEYS/disabled"' in setup and '"$STATE/disabled"' not in setup and "systemctl disable --now digipaddock-mirror.timer" in setup
    service = _read("systemd/digipaddock-mirror.service")
    assert "Type=oneshot" in service and "OnFailure=digipaddock-alert@digipaddock-mirror.service" in service
    assert "Environment=HOME=/root" in service and "TimeoutStartSec=300" in service and "User=" not in service
    assert "ConditionPathExists=/etc/digipaddock/mirror/github_deploy" in service
    # the key is made before the window, the state directory only by mirror_setup.sh install: a release in between
    # starts nothing (it failed 226/NAMESPACE and raised kd-alert, P0 window)
    assert "ConditionPathIsDirectory=/var/lib/digipaddock/github-mirror" in service
    assert "ReadWritePaths=/var/lib/digipaddock/github-mirror" in service
    timer = _read("systemd/digipaddock-mirror.timer")
    assert "OnBootSec=5min" in timer and "OnUnitActiveSec=5min" in timer
    known = _read("install/github_known_hosts").splitlines()
    assert sorted(line.split()[1] for line in known) == ["ecdsa-sha2-nistp256", "ssh-ed25519", "ssh-rsa"]
    assert all(line.split()[0] == "github.com" for line in known)


def test_stage_setup_installs_the_release_service_on_every_host():
    """updating is the host's, not the developer agent's — a host without dev releases too. The request
    directory is root's until the forge module hands it to its user (kd-merge --release); base never takes it back."""
    text = _read("install/stage_setup.sh")
    loop = re.search(r'^UNITS="(.*)"$', (COMMON / "install/render_host.sh").read_text(encoding="utf-8"), re.M).group(1).split()
    assert "digipaddock-release.path" in loop and "digipaddock-release.service" in loop
    assert 'ln -sfn "$REPO/common/bin/kd-release" "$RELEASE_LINK"' in text
    install = text[text.index('\nbash "$REPO/common/install/render_host.sh"\n'):]
    assert "[ -d /var/lib/digipaddock/release ] || install -d -o root -g root -m 0750 /var/lib/digipaddock/release" \
        in install
    assert "install -d -o root -g root -m 0755 /var/lib/digipaddock/release-result" in install
    assert "install -d -o root -g root -m 0700 /var/lib/digipaddock/mirror" in install
    assert "systemctl enable --now digipaddock-release.path" in install
    check = text[text.index("stage_check() {"):text.index('\nif [ -n "$check" ]; then')]
    for said in ("digipaddock-release.path", "/var/lib/digipaddock/release", "/root/.ssh/forgejo_deploy",
                 "remote.origin.mirror"):
        assert said in check, said


def test_stage_setup_installs_hindsight_client_for_every_agent_user():
    text = _read("install/stage_setup.sh")
    lib = _read("install/lib/hindsight_client.sh")
    assert 'HINDSIGHT_PLUGIN_VERSION=$(/usr/bin/python3 -I "$REPO/common/kd/install_catalog.py" version hindsight_plugin)' in lib
    assert 'npx -y @vectorize-io/hindsight-coding-agents@$HINDSIGHT_PLUGIN_VERSION install claude-code --server self-hosted --api-url http://127.0.0.1:9077' in lib
    assert "HINDSIGHT_API_TENANT_API_KEY" in lib and "coding-agent.json" in lib
    # no key line — grep's 1 under set -eo pipefail would cut the script off silently, before the clear error a line below
    assert "/etc/hindsight/coding-agent.env | cut -d= -f2- || true)" in lib
    # the key — through the process environment, not through runuser's argv (other users see it in ps); the user itself
    # writes the config
    assert 'KD_KEY="$key" KD_CONFIG="$config" runuser -u "$u" -- python3 -' in lib and 'env KD_KEY=' not in lib
    assert "command -v node" in lib and "PATH=$HOME/.local/bin" in lib  # claude needs npx for claude mcp add
    assert 'mktemp -d -p "$HOME"' in lib  # npx not from /tmp: a foreign node_modules
    assert "[ -f /etc/hindsight/coding-agent.env ] || return 0" in lib
    # stage_setup: the block at the end (a network failure does not break the units), for agent and every user from agents.json
    assert text.index("# 6b.") > text.index("systemctl enable --now digipaddock-usage.timer")
    assert text.index("# 6b.") > text.index('echo "claude: ')
    assert 'source "$REPO/common/install/lib/hindsight_client.sh"' in text and "agents.users()" in text
    assert 'hindsight_client_for "$u" ||' in text and "users=$base" in text


def test_stage_setup_as_agent_calls_have_no_escaped_dollars():
    # as_agent = sudo -iu digi: the login shell expands \$x before the inner bash — $d/$rc get lost silently
    text = _read("install/stage_setup.sh")
    bad = [l for l in text.splitlines() if re.search(r'as_agent "', l) and "\\$" in l]
    assert bad == []
    lib = _read("install/lib/hindsight_client.sh")
    npx = [l for l in lib.splitlines() if "npx -y" in l and not l.lstrip().startswith("#")]
    assert len(npx) == 1 and npx[0].lstrip().startswith('sudo -u "$u" -H env HINDSIGHT_PLUGIN_VERSION=')
    assert "npx -y" not in text  # npx — only in the library
    assert "python3 -I -" in lib and "os.chmod(os.path.dirname(dst), 0o700)" in lib


def test_hindsight_lib_never_touches_agent_home_as_root():
    assert _root_ops_on_agent_home(_read("install/lib/hindsight_client.sh")) == []


def _block(text, head):
    """The nginx block from the line head to its matching "}" (the braces inside regexp quotes are paired)."""
    start = text.index(head)
    depth = 0
    for j in range(text.index("{", start), len(text)):
        depth += {"{": 1, "}": -1}.get(text[j], 0)
        if depth == 0:
            return text[start:j + 1]
    raise AssertionError(head)


def test_the_old_monitor_addresses_answer_404():
    # /monitor, /monitor/, /monitor/P-1 and /monitor/s/stage — 404 from nginx itself: without these the
    # cabinet's `location / { try_files $uri /index.html; }` would answer them 200
    conf = _read("nginx/cabinet.conf")
    https = conf.split("# --- https ---", 1)[1].split("# --- /https ---", 1)[0]
    assert _block(https, "location = /monitor {") == "location = /monitor { return 404; }"
    # ^~ stops the regex locations for /monitor/P-1 and /monitor/s/stage; no other location starts with /monitor
    assert _block(https, "location ^~ /monitor/ {") == "location ^~ /monitor/ { return 404; }"
    assert re.findall(r"location [=^~ ]*\"?\^?/monitor", https) == ["location = /monitor", "location ^~ /monitor"]
    assert "auth_basic" not in conf and "kd-monitor-web" not in conf and "/var/lib/digipaddock/monitor-web" not in conf


def test_worker_slots_install_touches_no_unit():
    text = (COMMON / "install/worker_slots.sh").read_text(encoding="utf-8")
    assert "systemctl" not in text and 'kd_expect_run "worker_slots.sh --install"' in text
    units = re.search(r'^UNITS="(.*)"$', (COMMON / "install/render_host.sh").read_text(encoding="utf-8"), re.M).group(1).split()
    assert not [u for u in units if u.startswith("kd-worker")]


def _copy_dist(tmp_path, make_dist):
    """build_web's copy of the base user's dist into root's dist.new, run in a scratch tree (chown stubbed)."""
    build = _function(_read("install/cabinet_setup.sh"), "build_web")
    start = build.index('  rm -rf "$WEB/dist.new" "$WEB/dist.old"')
    end = build.index('  if [ -d "$WEB/dist" ]')
    web, src = tmp_path / "web", tmp_path / "web" / "src"
    src.mkdir(parents=True)
    make_dist(src / "dist")
    script = "set -euo pipefail\nchown() { :; }\nWEB=$1; src=$2\n" + build[start:end]
    return subprocess.run(["bash", "-c", script, "_", str(web), str(src)], capture_output=True, text=True), web


@pytest.mark.skipif(sys.platform == "win32", reason="bash, symlinks and FIFOs")
def test_cabinet_setup_never_points_roots_chmod_through_the_builds_links(tmp_path):
    # review 04.11.2026: the base user owns the build; a dist that is a link, or holds one, is refused before root's
    # chmod -R could change what it names
    secret = tmp_path / "secret"
    secret.mkdir(mode=0o700)
    (secret / "key").write_text("k")
    (secret / "key").chmod(0o600)
    for make in (
        lambda d: d.symlink_to(secret),
        lambda d: (d.mkdir(), (d / "assets").symlink_to(secret)),
        lambda d: (d.mkdir(), os.mkfifo(d / "pipe")),
    ):
        shutil.rmtree(tmp_path / "web", ignore_errors=True)
        proc, web = _copy_dist(tmp_path, make)
        assert proc.returncode == 1 and "not installing" in proc.stderr, proc.stderr
        assert not (web / "dist.new").exists()
        assert stat.S_IMODE(secret.stat().st_mode) == 0o700 and stat.S_IMODE((secret / "key").stat().st_mode) == 0o600
    shutil.rmtree(tmp_path / "web")
    proc, web = _copy_dist(tmp_path, lambda d: (d.mkdir(), (d / "index.html").write_text("<html>")))
    assert proc.returncode == 0, proc.stderr
    assert (web / "dist.new" / "index.html").read_text() == "<html>"
