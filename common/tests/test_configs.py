import json
import re
from pathlib import Path

import pytest

from kd.registry import read_json_file

COMMON = Path(__file__).resolve().parent.parent
ROOT = COMMON.parent


def test_the_panel_runtime_is_gone():
    # agents run only on the session host; the panel launcher and the probe agent are gone.
    for path in ("bin/start-agent", "bin/kd-attach", "bin/kd-typing", "probe-agent", "install/add_agent.sh"):
        assert not (COMMON / path).exists(), path


def test_the_owners_deploy_from_the_pc_is_gone():
    """a release is only kd-release (main of both repositories); the owner pushes and asks
    ``kd-cabinet release <project>``. Only the internal history still names deploy_code.sh."""
    assert not (COMMON / "install/deploy_code.sh").exists()
    internal = ("docs/superpowers/", "tasks/", "CLAUDE.md")
    named = [str(p.relative_to(ROOT)).replace("\\", "/") for top in ("common", "agents", "docs", "scripts")
             for p in (ROOT / top).rglob("*") if p.is_file() and p.suffix in (".py", ".sh", ".md", ".json", "")
             and "deploy_code" in p.read_text(encoding="utf-8", errors="replace")]
    assert [p for p in named if not p.startswith(internal) and p != "common/tests/test_configs.py"] == []
    public = ROOT / "CLAUDE.public.md"  # the code repository's CLAUDE.md once split
    assert "deploy_code" not in (public if public.is_file() else ROOT / "CLAUDE.md").read_text(encoding="utf-8")


def test_needrestart_spares_the_session_host():
    # needrestart restarting digipaddock-session-host.service or a digipaddock-session-* unit would cut the running turns
    text = (COMMON / "needrestart/digipaddock-sessions.conf").read_text(encoding="utf-8")
    rules = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert rules == ["$nrconf{override_rc}{qr(^digipaddock-session)} = 0;"]
    pattern = re.compile(rules[0].split("qr(", 1)[1].split(")}", 1)[0])
    for unit in ("digipaddock-session-host.service", "digipaddock-session-0123abcd-1.service",
                 "digipaddock-session-job-0123456789ab.service", "digipaddock-sessions.slice"):
        assert pattern.search(unit), unit
    setup = (COMMON / "install/stage_setup.sh").read_text(encoding="utf-8")
    assert ('install -o root -g root -m 0644 "$REPO/common/needrestart/digipaddock-sessions.conf" '
            "/etc/needrestart/conf.d/digipaddock-sessions.conf") in setup


def test_managed_settings_floor_for_all_agents():
    perms = read_json_file(COMMON / "claude/managed-settings.template.json")["permissions"]
    # A managed defaultMode beats the agent's --settings (stage probe 11.10.2026), so the floor
    # leaves the mode to the compiled policy (dontAsk)
    assert "defaultMode" not in perms
    assert perms["disableAutoMode"] == "disable" and perms["disableBypassPermissionsMode"] == "disable"
    assert perms["blockReadsOutsideWorkingDirectories"] is True
    # The owner's consent to ssh-exec is the task grant its executor checks;
    # a floor ask would put a card on every call (a hook allow never overrides an ask).
    assert perms["ask"] == []
    assert {"Bash(sudo *)", "Bash(su *)", "Monitor", "Read(//home/digi/.claude/.credentials.json)",
            "Read(//etc/digi-exec/**)"} <= set(perms["deny"])
    assert "allow" not in perms  # the floor only restricts


def test_managed_settings_log_every_permission_card():
    """Every permission prompt of any agent goes to journald (kd-permlog); the hook only writes, decides nothing."""
    settings = read_json_file(COMMON / "claude/managed-settings.template.json")
    assert set(settings["hooks"]) == {"PermissionRequest"}
    (entry,) = settings["hooks"]["PermissionRequest"]
    assert entry["matcher"] == "*"
    assert entry["hooks"] == [{"type": "command", "command": "/opt/digipaddock/common/bin/kd-permlog", "timeout": 5}]
    assert (COMMON / "bin/kd-permlog").exists()


def test_managed_settings_floor_turns_prompt_suggestions_off():
    """Claude Code's prompt suggestion (ghost text after a turn) reads like the owner's words in a
    panel tail and in Telegram-less sessions; the floor turns it off and pins the env so
    no agent settings can turn it back on."""
    settings = read_json_file(COMMON / "claude/managed-settings.template.json")
    assert settings["promptSuggestionEnabled"] is False
    assert settings["env"]["CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION"] == "false"


def test_managed_settings_floor_turns_background_tasks_off():
    """A turn ends with Claude Code's result line: background work (a Bash run_in_background, a subagent in the
    background) finishes after it and Claude Code resumes on its own — a turn no cabinet command started, whose
    kd-work finish is refused while the host takes the process for idle (seen 20.10). The floor turns
    background tasks off for every agent session and worker."""
    settings = read_json_file(COMMON / "claude/managed-settings.template.json")
    assert settings["env"]["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"


def test_stage_setup_installs_jq():
    # without jq an agent parsed JSON through python3 -c — that is a permission prompt for the owner on every read
    text = (COMMON / "install/stage_setup.sh").read_text(encoding="utf-8")
    assert "command -v jq >/dev/null || DEBIAN_FRONTEND=noninteractive apt-get install -y jq" in text


def test_managed_settings_floor_leaves_web_to_the_launch_and_denies_agent_user_config():
    from kd.launch import WEB_TOOLS
    from kd.policy import PLATFORM_DENY

    perms = read_json_file(COMMON / "claude/managed-settings.template.json")["permissions"]
    # the web tools are denied by every compiled policy, and only a launch with an exit of every host
    # lifts that — which a managed deny would make impossible (nothing overrides the floor)
    assert not set(WEB_TOOLS) & set(perms["deny"])
    assert set(WEB_TOOLS) <= set(PLATFORM_DENY)
    # Claude Code's user settings, keys, PATH and the agent's shell profile — in the home of every agent user
    for home in ("digi", "digi-dev"):
        for path in (".claude/**", ".ssh/**", ".local/**", ".bun/**", ".config/**", ".bashrc", ".profile", ".bash_profile"):
            assert f"Edit(//home/{home}/{path})" in perms["deny"], (home, path)
    assert not any(rule.startswith("Write(") for rule in perms["deny"])  # Edit closes Write too


def test_managed_settings_floor_covers_every_agent_user():
    # dev runs under digi-dev — every floor rule about digi's home exists for digi-dev's home too
    deny = read_json_file(COMMON / "claude/managed-settings.template.json")["permissions"]["deny"]
    for rule in [r for r in deny if "//home/digi/" in r]:
        assert rule.replace("//home/digi/", "//home/digi-dev/") in deny, rule


def test_managed_settings_deny_editing_user_claude_json():
    # ~/.claude.json (the user's MCP servers, trust in folders) lies outside ~/.claude — a separate rule
    perms = read_json_file(COMMON / "claude/managed-settings.template.json")["permissions"]
    assert "Edit(//home/digi/.claude.json)" in perms["deny"]


def test_cabinet_units():
    from kd import naming

    socket = naming.render((COMMON / "systemd/digipaddock-cabinet.socket").read_text(encoding="utf-8"), "digi")
    for line in ("ListenStream=/run/digipaddock-cabinet/local.sock\n", "SocketUser=digi-cabinet\n", "SocketGroup=digipaddock-cabinet\n",
                 "SocketMode=0660\n", "DirectoryMode=0755\n", "RemoveOnStop=no\n", "WantedBy=sockets.target",
                 "FileDescriptorName=local\n"):
        assert line in socket, line
    # nginx (www-data) reaches the web on a socket of its own; the service learns which is which by name
    http = naming.render((COMMON / "systemd/digipaddock-cabinet-http.socket").read_text(encoding="utf-8"), "digi")
    for line in ("ListenStream=/run/digipaddock-cabinet-http/http.sock\n", "FileDescriptorName=http\n",
                 "Service=digipaddock-cabinet.service\n", "SocketUser=digi-cabinet\n", "SocketGroup=www-data\n",
                 "SocketMode=0660\n", "DirectoryMode=0755\n", "WantedBy=sockets.target"):
        assert line in http, line
    service = naming.render((COMMON / "systemd/digipaddock-cabinet.service").read_text(encoding="utf-8"), "digi")
    # Group is the socket's group: RuntimeDirectory is re-owned to User:Group recursively at every start
    for line in ("User=digi-cabinet\n", "Group=digipaddock-cabinet\n", "UMask=0077\n",
                 "ExecStart=/var/lib/digipaddock/venv/cabinet/bin/python3 -I /opt/digipaddock/common/bin/"
                 "kd-cabinet serve\n",
                 "Sockets=digipaddock-cabinet.socket digipaddock-cabinet-http.socket\n",
                 "Requires=digipaddock-cabinet.socket\n", "RuntimeDirectory=digipaddock-cabinet\n", "RuntimeDirectoryPreserve=yes\n",
                 "ReadWritePaths=/var/lib/digipaddock/cabinet\n", "RestrictAddressFamilies=AF_UNIX\n",
                 "IPAddressDeny=any\n", "MemoryMax=512M\n", "ProtectSystem=strict\n", "NoNewPrivileges=yes\n",
                 "CapabilityBoundingSet=\n", "PrivateTmp=yes\n", "ProtectHome=yes\n",
                 # global review 15.10.2026: the syscall filter and the namespace/clock/hostname locks
                 "SystemCallFilter=@system-service\n", "SystemCallArchitectures=native\n", "RestrictNamespaces=yes\n",
                 "RestrictRealtime=yes\n", "ProtectClock=yes\n", "ProtectHostname=yes\n",
                 # the sysadmin's proposals reach it by the cabinet user's own group (admin-proposals <agent>:<cabinet>)
                 "SupplementaryGroups=digi-cabinet\n"):
        assert line in service, line
    # the http socket is not required: units rendered before cabinet.json exists must not stop the board
    assert "digipaddock-cabinet-http.socket" not in service.split("[Service]")[0]
    # the socket starts the service: no [Install] of its own, and the socket path is the client's
    assert "[Install]" not in service
    from kd.cabinet import server
    assert server.SOCKET == "/run/digipaddock-cabinet/local.sock" and server.CHANGED.startswith("/run/digipaddock-cabinet/")
    assert server.NAMES == ("local", "http")


def test_cabinet_service_may_write_every_store_its_decisions_write():
    # the owner's decisions run in the cabinet (kd-admin-approval retired): admin grants, BOSS decisions
    # and
    # delegation grants, the BOSS audit journal; a store missing here is read-only (ProtectSystem=strict) -> EACCES
    from kd import boss_promote, delegation

    service = (COMMON / "systemd/digipaddock-cabinet.service").read_text(encoding="utf-8")
    writable = " ".join(line.split("=", 1)[1] for line in service.splitlines()
                        if line.startswith("ReadWritePaths=")).replace("-/", "/").split()
    for store in (boss_promote.DECISIONS, delegation.GRANTS, "/var/lib/digipaddock/admin-grants",
                  "/var/log/digipaddock-board/boss.jsonl", "/var/lib/digipaddock/cabinet/cabinet.db"):
        assert any(store == path or store.startswith(path + "/") for path in writable), store
    assert "ReadWritePaths=-/var/lib/digipaddock/admin-grants" in service  # only with the sysadmin's module
    assert not (COMMON / "systemd/digipaddock-admin-approval.service").exists()


def test_cabinet_push_unit():
    """the Web Push sender is a process of its own: its own user (global review 15.10.2026: the one
    cabinet process with network holds no owner authority on the socket), the socket group, the network."""
    from kd import naming

    unit = naming.render((COMMON / "systemd/digipaddock-cabinet-push.service").read_text(encoding="utf-8"), "digi")
    for line in ("User=digi-push\n", "Group=digipaddock-cabinet\n",
                 # push.env is root:<push user> 0640: with the socket group as the primary one, the
                 # user's own group must come back as a supplementary group (stage, 15.10: PermissionError)
                 "SupplementaryGroups=digi-push\n",
                 "ExecStart=/var/lib/digipaddock/venv/cabinet/bin/python3 -I /opt/digipaddock/common/bin/"
                 "kd-cabinet push\n", "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6\n", "ProtectSystem=strict\n",
                 # the gateways are public: nothing on this host or its private networks
                 "IPAddressDeny=localhost link-local multicast 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16\n",
                 # except the systemd-resolved stub — without DNS no push left the host (status 0)
                 "IPAddressAllow=127.0.0.53/32\n",
                 "SystemCallFilter=@system-service\n", "SystemCallArchitectures=native\n", "RestrictNamespaces=yes\n",
                 "After=digipaddock-cabinet.socket\n", "Restart=always\n", "NoNewPrivileges=yes\n", "CapabilityBoundingSet=\n",
                 "ProtectHome=yes\n", "PrivateTmp=yes\n", "WantedBy=multi-user.target"):
        assert line in unit, line
    assert "digi-cabinet" not in unit and "ReadWritePaths" not in unit  # not the cabinet's user; it writes nothing


def test_board_units():
    path = (COMMON / "systemd/digipaddock-board-sync.path").read_text(encoding="utf-8")
    # every card write in the cabinet touches its change stamp
    assert "PathChanged=/run/digipaddock-cabinet/changed\n" in path and "Unit=digipaddock-board-sync.service" in path
    sync = (COMMON / "systemd/digipaddock-board-sync.service").read_text(encoding="utf-8")
    assert "Type=oneshot" in sync and "ExecStart=/usr/local/bin/kd-board sync\n" in sync and "User=" not in sync
    timer = (COMMON / "systemd/digipaddock-board-sync.timer").read_text(encoding="utf-8")
    assert "OnCalendar=*:0/10" in timer and "Persistent=" not in timer
    # the 09:15 digest to the Pult is gone — the cabinet's 09:00 digest is the owner's morning summary
    assert not (COMMON / "systemd/digipaddock-board-digest.timer").exists()
    assert not (COMMON / "systemd/digipaddock-board-digest.service").exists()
    cli = (COMMON / "bin/kd-board").read_text(encoding="utf-8")
    assert "usage: kd-board sync | digest-signal | watch | deliver" in cli
    assert 'cmd == ["digest"]' not in cli and "pult" not in cli.lower()
    assert "OnFailure=digipaddock-alert@digipaddock-board-sync.service" in sync
    # the path unit kicks the service on every change: the standard start limit would take the path unit to failed
    assert "StartLimitIntervalSec=0" in sync


def test_grant_units_tell_the_coordinator_at_once():
    # the owner's "Allow" or "Withdraw" runs kd-nudge deliver now, not at the next watch tick
    from kd import delegation
    path = (COMMON / "systemd/digipaddock-board-grants.path").read_text(encoding="utf-8")
    svc = (COMMON / "systemd/digipaddock-board-deliver.service").read_text(encoding="utf-8")
    timer = (COMMON / "systemd/digipaddock-board-deliver.timer").read_text(encoding="utf-8")
    assert not (COMMON / "systemd/digipaddock-board-grants.service").exists()
    assert f"PathChanged={delegation.GRANTS}\n" in path and "Unit=digipaddock-board-deliver.service" in path
    assert "Type=oneshot" in svc and "ExecStart=/usr/local/bin/kd-board deliver\n" in svc and "User=" not in svc
    assert "OnFailure=digipaddock-alert@digipaddock-board-deliver.service" in svc and "StartLimitIntervalSec=0" in svc
    # a busy panel is retried every minute, not at the next watch tick; an idle run leaves no line in the journal
    assert "OnUnitActiveSec=1min" in timer and "OnBootSec=" in timer and "Unit=" not in timer
    assert "LogLevelMax=notice" in svc and "SyslogLevel=notice" in svc
    cli = (COMMON / "bin/kd-board").read_text(encoding="utf-8")
    assert 'if cmd == ["deliver"] and len(argv) == 1:\n        return _deliver_directives()' in cli
    # every wake is the cabinet's command — deliver only runs kd-nudge deliver, no wakes are queued
    assert "def _deliver(client):\n    return _deliver_directives()" in cli or "return _deliver_directives()" in cli
    assert "retry_wakes" not in cli and "_wakes_lock" not in cli


def test_release_units():
    path = (COMMON / "systemd/digipaddock-release.path").read_text(encoding="utf-8")
    svc = (COMMON / "systemd/digipaddock-release.service").read_text(encoding="utf-8")
    assert "PathExists=/var/lib/digipaddock/release/request.json" in path and "PathChanged" not in path
    assert "Unit=digipaddock-release.service" in path
    assert "OnFailure=digipaddock-alert@digipaddock-release.service" in svc and "StartLimitIntervalSec=0" in svc and "Type=oneshot" in svc
    assert "ExecStart=/usr/local/bin/kd-release" in svc and "TimeoutStartSec=3000" in svc
    assert "Environment=HOME=/root" in svc and "RuntimeDirectory=digipaddock-release" in svc


def test_managed_settings_board_and_hindsight():
    perms = read_json_file(COMMON / "claude/managed-settings.template.json")["permissions"]
    assert "/srv/digipaddock-agents/board" in perms["additionalDirectories"]
    assert {"Edit(//srv/digipaddock-agents/board/**)", "Read(//etc/digipaddock/pult.env)", "Read(//home/digi/.hindsight/**)",
            "Edit(//home/digi/.hindsight/**)", "Read(//etc/hindsight/**)",
            # Forgejo tokens (the dev agent's account, kd-merge) and the forge executor's account — for no agent (06.10.2026)
            "Read(//etc/digipaddock/forge/**)", "Read(//home/digi-forge/**)"} <= set(perms["deny"])


def test_managed_settings_deny_editing_monitor_state_and_pages():
    # only kd-monitor writes the registry, the baseline and the monitor's pages (the same uid agent): the floor closes
    # Edit/Write for the agent
    perms = read_json_file(COMMON / "claude/managed-settings.template.json")["permissions"]
    assert "Edit(//var/lib/digipaddock/**)" in perms["deny"]


def test_the_legacy_runtime_is_gone():
    # No agent runs the pre-manifest launch.
    root = COMMON.parent
    assert not list(root.glob("agents/*/install/agent_setup.sh"))
    assert not list(root.glob("agents/*/settings.json"))
    assert not list(root.glob("agents/*/launch-settings.json"))
    host = json.loads((root / "common/agent-host.json").read_text("utf-8"))
    assert "legacy_runtime" not in host and "agents_without_manifest" not in host


# --- user names come from the host prefix -----------------------------------------

ROOT_UNITS = {  # units that run as root on purpose
    "digipaddock-openai-bridge.service",  # reads the root-only OpenAI credential for the local transport
    "digipaddock-model-keys.service",  # independent owner signature check, root-only provider files
    "digipaddock-alert@.service", "digipaddock-usage.service", "digipaddock-board-sync.service",
    "digipaddock-board-watch.service", "digipaddock-board-deliver.service", "digipaddock-board-boss-digest.service", "digipaddock-release.service",
    "digipaddock-cabinet-export.service",  # the nightly snapshot and its .git in the root-owned board directory
    "digipaddock-session-host.service",  # each session is systemd-run under the agent's own user
    "digipaddock-alert-watch.service",  # the Telegram token is root's only
    "digipaddock-repo-attach.service",  # add_repo.sh — runuser of dev, root's repos.json and projects.md
    "digipaddock-hindsight-status.service",  # reads separate users' installed clients and the Hindsight API key
    "digipaddock-mirror.service",  # root's forge read key and the GitHub push key
    # the repo user's forge read key and the cabinet's card.create; its download and scans are child units of ci
    "digipaddock-vulnscan.service",
}


def _service_units():
    # The tracked unit files only: not the scratch copies under .worktrees/ and tmp/.
    units = [*ROOT.glob("common/systemd/*.service"), *ROOT.glob("agents/*/systemd/*.service")]
    return [*units, *(ROOT / "common/hindsight").glob("hindsight-*.service")]


def test_every_non_root_unit_runs_as_a_rendered_user():
    for path in _service_units():
        text = path.read_text("utf-8")
        users = re.findall(r"^User=(.*)$", text, re.M)
        if path.name in ROOT_UNITS or path.name == "hindsight-backup.service":
            continue
        # the cabinet, its push sender (global review 15.10.2026), the checkout refresher and
        # the approver of signed PRs and the voice transcription have their own service users;
        # every other unit runs as the base user
        service = {"digipaddock-repo-sync.service": ["@repo@"], "digipaddock-review.service": ["@review@"],
                   "digipaddock-cabinet.service": ["@cabinet@"], "digipaddock-cabinet-push.service": ["@push@"],
                   "digipaddock-transcribe@.service": ["@transcribe@"],
                   # a probe exit runs as the distribution's tinyproxy user from the start, never as root
                   "digipaddock-egress@.service": ["tinyproxy"]}
        expected = service.get(path.name, ["@base@"])
        assert users == expected, path  # an unrendered unit fails to start, never runs as root
        assert not re.search(r"/home/[a-z]", text), path  # homes only through @base@


def test_managed_settings_template_renders_without_old_names_and_guards_every_agent_home():
    from kd import naming

    text = naming.render((COMMON / "claude/managed-settings.template.json").read_text("utf-8"), "digi")
    data = json.loads(text)
    deny = data["permissions"]["deny"]
    for user in ("digi", "digi-dev", "digi-boss", "digi-sysadmin", "digi-exec", "digi-forge"):
        assert any(f"//home/{user}/" in rule for rule in deny), user
    assert "agent" not in re.sub(r"agent-ro|digipaddock-agents|coding-agent|agent_\w+", "", text)
    assert not (COMMON / "claude/managed-settings.json").exists()


def test_shell_names_users_only_through_naming_sh():
    pattern = re.compile(r"\bkit-(?:dev|boss|sysadmin|exec|forge)\b|\bUser=digi\b|/home/digi\b|-u digi\b")
    hits = [
        f"{p.relative_to(ROOT)}:{n}"
        for p in [*ROOT.glob("common/install/**/*.sh"), *ROOT.glob("agents/*/install/*.sh"),
                  *ROOT.glob("agents/*/bin/*"), *ROOT.glob("common/bin/*")]
        if p.is_file()
        for n, line in enumerate(p.read_text("utf-8", errors="replace").splitlines(), 1)
        if pattern.search(line)
    ]
    assert not hits, hits


def test_the_worker_units_are_retired():
    # the session host runs worker jobs; worker_slots.sh --install removes the installed copies
    for name in ("digipaddock-worker@.service", "digipaddock-worker-reconcile.service", "digipaddock-worker-reconcile.timer"):
        assert not (COMMON / "systemd" / name).exists(), name
    assert not (COMMON / "install/polkit/kd-worker.rules.template").exists()


# --- the cabinet's web behind nginx -------------------------------------------------

def _https():
    text = (COMMON / "nginx/cabinet.conf").read_text(encoding="utf-8")
    return text.split("# --- https ---", 1)[1].split("# --- /https ---", 1)[0]


def _location(text, head):
    start = text.index(head)
    return text[start:text.index("\n    }", start)]


def _server_level(https):
    # what stands outside every location block of the https server
    return re.sub(r"\n    location [^\n]*\{.*?\n    \}", "", https, flags=re.S)


def test_nginx_serves_the_cabinet_and_asks_no_password():
    https = _https()
    server = _server_level(https)
    assert "auth_basic" not in https and "htpasswd" not in https  # the cabinet signs people in itself
    # the static monitor pages are gone — their old addresses answer 404, not the app's index.html
    assert "location = /monitor { return 404; }" in https and "location ^~ /monitor/ { return 404; }" in https
    assert "kd-monitor-web" not in https and "monitor-web" not in https
    assert "root /var/lib/digipaddock/cabinet-web/dist;" in server
    assert "try_files $uri /index.html;" in _location(https, "location / {")
    assert "include /etc/nginx/digipaddock-cabinet-proxy.conf;" in _location(https, "location /api/ {")
    # global review 15.10.2026: the strict zone on the three sign-in POSTs alone — not on passkey options or logout
    for path in ("code", "password", "passkey"):
        auth = https[https.index(f"location = /api/auth/{path} {{"):]
        auth = auth[:auth.index("\n")]
        assert "limit_req zone=kd_auth" in auth and "include /etc/nginx/digipaddock-cabinet-proxy.conf;" in auth, path
    assert "location /api/auth/ {" not in https
    ws = _location(https, "location = /ws {")
    for line in ("proxy_pass http://unix:/run/digipaddock-cabinet-http/http.sock;", "proxy_set_header Upgrade $http_upgrade;",
                 "proxy_set_header Connection $kd_board_connection_upgrade;",
                 "proxy_set_header X-Forwarded-For $remote_addr;"):
        assert line in ws, line
    assert "client_max_body_size 11m;" in server and "limit_req zone=kd_board burst=100 nodelay;" in server
    # a message's files and text in one request — 100 MB of files pass nginx on the upload routes alone
    upload = _location(https, 'location ~ "^/api/(cards|agents)/[^/]+/files$" {')
    assert "client_max_body_size 101m;" in upload.splitlines()[0]
    assert "include /etc/nginx/digipaddock-cabinet-proxy.conf;" in upload.splitlines()[0]
    assert "kd-admin-approval" not in https


def test_nginx_diagram_frame_has_a_policy_of_its_own_and_the_cabinets_stays_strict():
    # mermaid draws with inline styles, so its page is another document — sandboxed (an opaque origin: no
    # session, no API), embedded by the cabinet alone, running the built scripts and loading nothing else
    frame = _location(_https(), "location = /diagram.html {")
    assert "digipaddock-cabinet-headers.conf" not in frame and "X-Frame-Options" not in frame
    csp = re.search(r'add_header Content-Security-Policy "([^"]+)" always;', frame).group(1)
    assert [part.strip() for part in csp.split(";")] == [
        "default-src 'none'", "script-src https://@host:cabinet_host@/assets/", "style-src 'unsafe-inline'",
        "frame-ancestors 'self'", "base-uri 'none'", "form-action 'none'", "sandbox allow-scripts",
    ]
    for line in ('add_header Strict-Transport-Security "max-age=31536000" always;',
                 "add_header X-Content-Type-Options nosniff always;", "add_header Referrer-Policy no-referrer always;",
                 'add_header Cache-Control "no-store" always;'):
        assert line in frame, line
    # the cabinet's own pages: no inline styles still, and the frame is reached as a same-origin document
    headers = (COMMON / "nginx/digipaddock-cabinet-headers.conf").read_text(encoding="utf-8")
    assert "unsafe-inline" not in headers and "default-src 'self'" in headers and "frame-src" not in headers


def test_nginx_lets_through_what_the_cabinet_takes_in_one_message():
    pytest.importorskip("fastapi")  # the stage's system python has no web packages (remote_pytest.sh)
    from kd.cabinet import api
    from kd.cabinet.web import files

    assert api.MESSAGE_FILES_MAX == 100 * 1024 * 1024 < files.BODY_MAX <= 101 * 1024 * 1024


def test_nginx_proxy_overwrites_the_client_address():
    # the cabinet rate-limits by the first X-Forwarded-For address: nginx sets it, never appends a client's value
    proxy = (COMMON / "nginx/digipaddock-cabinet-proxy.conf").read_text(encoding="utf-8")
    for line in ("proxy_pass http://unix:/run/digipaddock-cabinet-http/http.sock;", "proxy_set_header X-Forwarded-For $remote_addr;",
                 "proxy_set_header Host $host;", "proxy_set_header X-Forwarded-Proto https;", 'proxy_set_header Connection "";'):
        assert line in proxy, line
    assert "$proxy_add_x_forwarded_for" not in proxy
    pytest.importorskip("fastapi")  # the stage's system python has no web packages (remote_pytest.sh)
    from kd.cabinet.web import deps

    assert hasattr(deps, "client_ip")


def test_nginx_security_headers_reach_every_location_that_sets_its_own():
    # an add_header inside a location drops the server's ones: the cabinet's headers are one file included everywhere
    https = _https()
    headers = (COMMON / "nginx/digipaddock-cabinet-headers.conf").read_text(encoding="utf-8")
    csp = re.search(r'Content-Security-Policy "([^"]+)"', headers).group(1)
    for part in ("default-src 'self'", "script-src 'self'", "style-src 'self'", "frame-ancestors 'none'",
                 "base-uri 'none'", "connect-src 'self' wss://@host:cabinet_host@", "worker-src 'self'",
                 "media-src 'self';"):  # voice messages play from /api/files only
        assert part in csp, part
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    for line in ('Strict-Transport-Security "max-age=31536000" always;', "X-Content-Type-Options nosniff always;",
                 "X-Frame-Options DENY always;", "Referrer-Policy same-origin always;"):
        assert line in headers, line
    assert "include /etc/nginx/digipaddock-cabinet-headers.conf;" in _server_level(https)
    for block in re.findall(r"\n    location [^\n]*\{.*?\n    \}", https, flags=re.S):
        if "add_header" in block and "location = /diagram.html {" not in block:  # a set of its own: the next test
            assert "include /etc/nginx/digipaddock-cabinet-headers.conf;" in block, block
    assert 'add_header Cache-Control "no-store" always;' in _location(https, "location = /index.html {")
    assert 'add_header Service-Worker-Allowed "/" always;' in _location(https, "location = /sw.js {")
    # the stock mime.types has no .webmanifest — it went out as application/octet-stream
    assert "default_type application/manifest+json;" in _location(https, "location = /manifest.webmanifest {")


def test_nginx_keeps_the_old_links_working():
    https = _https()
    # Those links name a card by an earlier prefix — any card shape; the page takes the host's
    assert 'location ~ "^/board/([A-Z][A-Z0-9]{1,5}-[0-9]+)$" { return 301 /cards/$1; }' in https
    assert 'location ~ "^/admin/approvals/([A-Z][A-Z0-9]{1,5}-[0-9]+)" { return 301 /cards/$1; }' in https
    assert 'location ~ "^/boss/promotions/([0-9a-f]{32})$" { return 301 /decisions/by-proposal/$1; }' in https
    limits = (COMMON / "nginx/digipaddock-board-limit.conf").read_text(encoding="utf-8")
    assert "limit_req_zone $binary_remote_addr zone=kd_auth:1m rate=5r/m;" in limits
    assert "zone=kd_board:1m rate=30r/s;" in limits  # the card page alone is ~15 requests


# --- the cabinet's nginx logs in a directory of their own -------------------------------

def test_board_nginx_logs_live_in_their_own_directory():
    # a default ACL of the host reader on a directory that holds only the cabinet's logs covers create and rotation
    server = _server_level(_https())
    assert "access_log /var/log/nginx/board/access.log;" in server
    assert "error_log /var/log/nginx/board/error.log;" in server
    assert "cabinet.example.test.access.log" not in server


def test_board_nginx_logs_rotate_like_nginx_and_outside_its_stanza():
    text = (COMMON / "logrotate/digipaddock-board-nginx").read_text(encoding="utf-8")
    assert text.startswith("/var/log/nginx/board/*.log {")  # /var/log/nginx/*.log does not reach a subdirectory
    for line in ("daily", "rotate 14", "compress", "delaycompress", "create 0640 www-data adm", "sharedscripts"):
        assert f"\n\t{line}\n" in text, line
    assert "invoke-rc.d nginx rotate" in text


def test_board_setup_makes_the_log_directory_before_nginx_reads_the_site():
    text = (COMMON / "install/board_setup.sh").read_text(encoding="utf-8")
    made = text.index("install -d -o root -g adm -m 0755 /var/log/nginx/board")
    rotate = text.index("/etc/logrotate.d/digipaddock-board-nginx")
    check = text.index("nginx -t ||")
    assert made < check and rotate < check


def test_host_read_setup_never_gives_an_agent_the_journal_or_adm():
    text = (COMMON / "install/host_read_setup.sh").read_text(encoding="utf-8")
    assert 'usermod -aG systemd-journal "$observe"' in text
    assert "usermod -aG" not in text.replace('usermod -aG systemd-journal "$observe"', "")
    assert 'gpasswd -d "$u" "$g"' in text  # an agent user or the base user found in adm/systemd-journal is removed
    for closed in ("admin-grants", "/var/log/syslog", "/var/log/auth.log", "push.env", "cabinet.env"):
        assert closed in text.split("NEVER=", 1)[1].split(")", 1)[0], closed


def test_host_read_setup_counts_the_agents_of_a_host_with_none_yet():
    """The empty-host run: the first agent's install failed on the missing agents.json. A missing registry is
    no agent yet (kd.agents), a broken one still stops the script, the agent being installed is counted."""
    text = (COMMON / "install/host_read_setup.sh").read_text(encoding="utf-8")
    body = text.split("platform_users() {", 1)[1].split("\n}\n", 1)[0]
    assert "from kd import agents" in body and "agents.users()" in body and "json.load(open(" not in body
    assert '[ -z "$user" ] || echo "$user"' in body
    assert "users=$(platform_users | sort -u)" in text


def test_host_read_setup_grants_without_following_links():
    # admin-proposals is an agent's tree; root never walks it by path
    text = (COMMON / "install/host_read_setup.sh").read_text(encoding="utf-8")
    assert "setfacl -R" not in text
    assert '"$REPO/common/kd/acl_walk.py" "$LIB/$d" "$observe"' in text


def test_host_read_setup_grants_every_catalog_directory_under_var_lib():
    text = (COMMON / "install/host_read_setup.sh").read_text(encoding="utf-8")
    granted = set(text.split("GRANT_DIRS=(", 1)[1].split(")", 1)[0].split())
    catalog = json.loads((COMMON / "coordination/host-read.json").read_text(encoding="utf-8"))
    lib = "/var/lib/digipaddock/"
    readable_by_all = {"hindsight-since", "release-result", "usage.jsonl"}  # root 0644/0755 on the host
    under_lib = {r[len(lib):] for r in catalog["roots"].values() if r.startswith(lib)}
    assert under_lib - readable_by_all == granted
    assert "boss-promotions" not in under_lib  # decisions/ is the cabinet's: only proposals and grants are read


def test_host_read_setup_checks_homes_and_service_secrets():
    text = (COMMON / "install/host_read_setup.sh").read_text(encoding="utf-8")
    never = text.split("NEVER=", 1)[1].split(")", 1)[0]
    for closed in ("/etc/hindsight", "boss-promotions/decisions"):
        assert closed in never, closed
    assert "kd_user exec" in text and "kd_user forge" in text and "kd_user repo" in text  # their homes hold keys


def _case_block(text, mode):
    start = text.index(f"\n  {mode})")
    return text[start:text.index(";;", start)]


def test_host_read_lifecycle_disable_is_not_the_owner_switch():
    # Seen on the factory host: new_agent.sh --install revokes first (setup disable), so a disable that wrote the switch
    # turned host reads off on every reinstall; the switch is off/on, the lifecycle disable only stops calls
    text = (COMMON / "install/host_read_setup.sh").read_text(encoding="utf-8")
    assert "state add" not in _case_block(text, "disable")
    assert "state add" in _case_block(text, "off") and "state remove" in _case_block(text, "on")


@pytest.mark.parametrize("rel, raw_domains, domains", [
    # review 04.11.2026: the developer's sandbox reaches the forge only; the mirror is its executor's (git.mirror)
    ("common/agent-sandboxes/developer-v1.json", '["@host:forge_host@"]', '["forge.example.test"]'),
    ("common/agent-capabilities/git.develop.json", '["@host:forge_host@"]', '["forge.example.test"]'),
])
def test_forge_domain_is_a_host_token_rendered_on_read(rel, raw_domains, domains):
    raw = (ROOT / rel).read_text(encoding="utf-8")
    assert f'"network_domains": {raw_domains}' in raw, rel
    rendered = json.dumps(read_json_file(ROOT / rel))
    assert f'"network_domains": {domains}' in rendered and "@host" not in rendered, rel


def test_factory_serves_the_code_and_the_overlay_project():
    """digipaddock is the code, digipaddock-deploy the deployment's overlay — both are the factory's projects."""
    data = json.loads((COMMON / "coordination/factory.json").read_text(encoding="utf-8"))
    assert data["projects"] == ["digipaddock", "digipaddock-deploy"]


def test_dev_and_analyst_know_the_overlay_project():
    dev = (ROOT / "agents/dev/CLAUDE.md").read_text(encoding="utf-8")
    assert "`digipaddock-deploy` holds deployment values and internal docs" in dev
    assert "never copy its values into `digipaddock`" in dev
    analyst = (ROOT / "agents/analyst/CLAUDE.md").read_text(encoding="utf-8")
    assert "internal specs live in `digipaddock-deploy` under `docs/internal/`" in analyst
    assert "kd-repo show digipaddock-deploy" in analyst
