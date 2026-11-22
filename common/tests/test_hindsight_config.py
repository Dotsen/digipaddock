import json
import re
from pathlib import Path

from kd import naming

HS = Path(__file__).resolve().parent.parent / "hindsight"


def test_postgres_conf_values():
    text = (HS / "postgresql-hindsight.conf").read_text(encoding="utf-8")
    for line in ("max_connections = 220", "shared_buffers = 256MB", "timezone = 'UTC'", "log_timezone = 'UTC'",
                 "log_min_duration_statement = 2000"):
        assert line in text, line
    assert "listen_addresses" not in text  # stays localhost by default


def test_env_example_has_required_keys_and_no_secrets():
    text = (HS / "coding-agent.env.example").read_text(encoding="utf-8")
    for key in ("HINDSIGHT_API_DATABASE_URL=", "HINDSIGHT_API_HOST=127.0.0.1", "HINDSIGHT_API_PORT=9077",
                "HINDSIGHT_API_WORKER_ENABLED=false", "HINDSIGHT_API_TENANT_EXTENSION=hindsight_api.extensions.builtin.tenant:ApiKeyTenantExtension",
                "HINDSIGHT_API_TENANT_API_KEY=", "HINDSIGHT_API_MCP_ENABLED=false", "HINDSIGHT_API_LLM_MAX_CONCURRENT=2",
                "HINDSIGHT_API_LLM_PROVIDER=claude-code", "HF_HUB_OFFLINE=1", "HINDSIGHT_API_LOG_LEVEL=info",
                # a page refresh fits behind consolidation; the cabinet's bank operations; a week of LLM traces
                "HINDSIGHT_API_REFLECT_WALL_TIMEOUT=900", "HINDSIGHT_API_AUDIT_LOG_ENABLED=true",
                "HINDSIGHT_API_AUDIT_LOG_RETENTION_DAYS=90", "HINDSIGHT_API_LLM_TRACE_RETENTION_DAYS=8"):
        assert key in text, key
    assert "@db_password@" in text and "@tenant_key@" in text and "sk-" not in text  # placeholders, not values


def test_units_limits_user_and_hygiene():
    api = (HS / "hindsight-api.service").read_text(encoding="utf-8")
    worker = (HS / "hindsight-worker.service").read_text(encoding="utf-8")
    for unit in (api, worker):
        for line in ("User=@base@", "WorkingDirectory=/opt/hindsight", "EnvironmentFile=/etc/hindsight/coding-agent.env",
                     "Restart=always", "RestartSec=30", "StartLimitIntervalSec=10min", "StartLimitBurst=5", "CPUWeight=30",
                     "MemorySwapMax=0", "PrivateTmp=yes", "NoNewPrivileges=yes", "RuntimeMaxSec=7d", "OOMScoreAdjust=800",
                     "LogFilterPatterns=~^Database connected:", "After=network-online.target postgresql.service",
                     "DISABLE_AUTOUPDATER=1"):
            assert line in unit, line
        assert "StartLimitIntervalSec=0" not in unit  # otherwise a crash loop every 5 s and the unit never goes failed
    assert "MemoryHigh=3G" in api and "MemoryMax=4G" in api and "ExecStart=/opt/hindsight/venv/bin/hindsight-api\n" in api
    assert "MemoryHigh=3584M" in worker and "MemoryMax=4608M" in worker  # peak 3.04 GiB
    assert "ExecStart=/opt/hindsight/venv/bin/hindsight-worker --http-host 127.0.0.1" in worker


def test_backup_script_atomic_and_verified():
    text = (HS / "hindsight-backup.sh").read_text(encoding="utf-8")
    assert "set -euo pipefail" in text and 'pg_dump -Fc' in text and 'pg_restore -l "$f.tmp"' in text
    assert 'mv "$f.tmp" "$f"' in text and "tail -n +8 | xargs -r rm" in text
    # a pg_dump/pg_restore failure leaves no half-written .tmp (after mv the trap is a no-op)
    assert text.index("trap 'rm -f \"$f.tmp\"' EXIT") == text.index("\n", text.index('f="$dir/')) + 1
    timer = (HS / "hindsight-backup.timer").read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 04:30:00 @host:timezone@" in timer and "Persistent=true" in timer
    present = re.search(r'^PRESENT="(.*)"$', (HS.parent / "install/render_host.sh").read_text(encoding="utf-8"), re.M)
    assert "common/hindsight/hindsight-backup.timer" in present.group(1).split()  # rendered where it is installed
    assert "User=postgres" in (HS / "hindsight-backup.service").read_text(encoding="utf-8")


def test_sshd_hstunnel_match_block():
    text = (HS / "sshd-hstunnel.conf").read_text(encoding="utf-8")
    assert text.startswith("Match User hstunnel\n")
    for line in ("AllowTcpForwarding local", "PermitOpen 127.0.0.1:9077", "PermitTTY no", "X11Forwarding no",
                 "ForceCommand /usr/sbin/nologin"):
        assert line in text, line
    # the drop-in is pulled in by the Include at the start of sshd_config: without `Match all` the global directives
    # would fall under Match
    assert text.rstrip().endswith("Match all")
    # the key option port-forwarding enables socket forwarding too, PermitOpen does not restrict it
    match_all = text.rstrip().rindex("Match all")
    for line in ("AllowStreamLocalForwarding no", "AllowAgentForwarding no", "PermitListen none"):
        assert line in text, line
        assert text.index(line) < match_all, line


def test_hindsight_template_opt_in_only_agents_dir():
    cfg = json.loads((HS / "coding-agent.json").read_text(encoding="utf-8"))
    assert cfg["serverMode"] == "self-hosted" and cfg["apiUrl"] == "http://127.0.0.1:9077"
    assert "apiToken" not in cfg  # stage_setup.sh puts the key in from /etc/hindsight/coding-agent.env
    assert cfg["optInOnly"] is True and cfg["optInPaths"] == ["/srv/digipaddock-agents"]
    assert cfg["retainSessions"] is False and cfg["autoInject"] == "pages"
    assert "autoReflect" not in cfg  # deprecated in 0.8.0 (a WARN on every call); autoInject is in force
    assert cfg["codebaseSurvey"] is False and cfg["autoSeed"] is False and cfg["gitIngest"] == "none"
    assert cfg["autoUpdate"] is False and cfg["pageRefreshEveryTurns"] == 30 and cfg["pageTriggerCron"] == "H H(3-5) * * *"
    # dev gets its OWN bank, not coding-agent::DigiPaddock: otherwise the bank is shared with the owner's local sessions
    # Generated agents start in work/: the bank would be named after it (coding-agent::work) and shared by
    # sysadmin and boss (M02 stage probe) — every agent folder maps to its own bank.
    assert cfg["mapPathToBank"] == {
        "/srv/digipaddock-agents/analyst": "coding-agent::analyst",
        "/srv/digipaddock-agents/boss": "coding-agent::boss",
        "/srv/digipaddock-agents/dev": "coding-agent::dev",
        "/srv/digipaddock-agents/sysadmin": "coding-agent::sysadmin",
    }
    assert cfg["banks"]["coding-agent::dev"] == {"gitIngest": "message", "autoSeed": True}
    # a host's own bank of an agent (host.json memory_banks) replaces the folder's, settings too
    own = naming.hindsight_config(cfg, {"dev": "coding-agent::old-dev"})
    assert own["mapPathToBank"]["/srv/digipaddock-agents/dev"] == "coding-agent::old-dev"
    assert own["banks"] == {"coding-agent::old-dev": {"gitIngest": "message", "autoSeed": True}}
    assert own["mapPathToBank"]["/srv/digipaddock-agents/boss"] == "coding-agent::boss"
    assert cfg["mapPathToBank"]["/srv/digipaddock-agents/dev"] == "coding-agent::dev"  # the template is not changed
