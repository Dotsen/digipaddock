"""hindsight_setup.sh: the agents' memory server; an installed one stays as it is."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "common" / "install" / "hindsight_setup.sh"
HS = REPO / "common" / "hindsight"
BASH = next((c for c in (r"C:\Program Files\Git\bin\bash.exe", shutil.which("bash")) if c and Path(c).exists()), None)


def _text():
    return SCRIPT.read_text(encoding="utf-8")


def _function(name):
    return _text().split(f"\n{name}() {{\n", 1)[1].split("\n}\n", 1)[0]


def _mode(mode):
    cases = _text()[_text().rindex("\ncase $mode in"):]
    return re.search(rf"^  {mode}\)(.*?);;", cases, re.M | re.S).group(1)


@pytest.mark.skipif(BASH is None, reason="no bash")
@pytest.mark.parametrize("args", [[], ["upgrade"], ["install", "x"], ["check", "--hindsight"]])
def test_usage(args):
    proc = subprocess.run([BASH, SCRIPT.as_posix(), *args], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 2 and "usage: hindsight_setup.sh install|check|disable" in proc.stderr


def test_usage_and_root_only():
    text = _text()
    root = text.index('[ "$(id -u)" = 0 ] || { echo "root is required" >&2; exit 1; }')
    assert root < text.index('. "$TREE/common/install/lib/naming.sh"') < text.rindex("\ncase $mode in")


def test_install_keeps_an_existing_server():
    install = _function("install_all")
    # the password, the tenant key and the venv are made once: a server that has them keeps them
    assert '[ -x "$VENV/bin/hindsight-api" ] || make_venv' in install
    assert '[ -f "$ENV" ] || write_env' in install
    # its one restart is for an env that just got new settings (test_install_restarts_nothing_on_an_installed_server)
    install = install.replace('[ "$env_added" = 0 ] || systemctl restart hindsight-api.service hindsight-worker.service', "")
    for act in ("openssl rand", "python3 -m venv", "CREATE ROLE", "CREATE DATABASE", "systemctl restart"):
        assert act not in install, act
    assert "python3 -m venv" in _function("make_venv") and "openssl rand" in _function("write_env")
    db = _function("server_db")
    for act in ("CREATE ROLE", "CREATE DATABASE"):
        assert act in db, act
    # the cluster restarts only for a config that differs (or a locale it cannot see yet, below)
    assert re.search(r'if ! same_conf; then\n[^\n]*\n\s+systemctl restart', db)
    # settings, not bytes: a header comment of an older tree restarts nothing
    assert "grep -v -e '^[[:space:]]*#' -e '^[[:space:]]*$'" in _function("settings_of")
    # the role is there before the env sets its password
    assert install.index("server_db") < install.index('[ -f "$ENV" ] || write_env')


def test_a_server_older_than_the_locale_restarts_before_the_database():
    """A server started before locale-gen — this run's or an earlier failed one's — does not
    see the locale: CREATE DATABASE with en_US.UTF-8 failed («invalid LC_COLLATE locale name»). Only a database
    still to be made restarts it: a live server whose locales an OS update regenerated keeps running."""
    db = _function("server_db")
    block = db[db.index("pg_database where datname = 'hindsight'"):db.index("CREATE DATABASE")]
    assert re.search(r'locale_newer_than_server && systemctl restart "postgresql@\$pg-main\.service"', block)
    newer = _function("locale_newer_than_server")
    assert "pg_postmaster_start_time()" in newer and "/usr/lib/locale/locale-archive" in newer
    assert 'stat -c %Y' in newer


def test_install_finishes_a_server_left_half_made():
    """A model download that failed left the venv and env in place; the next install
    warms the models and repairs PostgreSQL's side again — only the password, the key and the venv are kept."""
    install = _function("install_all")
    assert "fresh_server" not in _text()
    assert install.index("server_db") < install.index("warm_models") < install.index("systemctl enable --now")
    warm = _function("warm_models")
    # models in the cache load offline; only a missing one goes to the network
    assert warm.index("HF_HUB_OFFLINE=1") < warm.index("HF_HUB_OFFLINE=0")
    assert "&& return 0" in warm


def test_install_restarts_nothing_on_an_installed_server():
    install = _function("install_all")
    assert "systemctl enable --now hindsight-api.service hindsight-worker.service hindsight-backup.timer" in install
    assert "daemon-reload" in install
    # the one restart: an env that just got the example's new settings (a running server reads it at start)
    restarts = [line.strip() for line in install.splitlines() if "restart" in line and not line.strip().startswith("#")]
    assert restarts == ['[ "$env_added" = 0 ] || systemctl restart hindsight-api.service hindsight-worker.service']
    assert install.index("add_env_settings && env_added=1") < install.index("systemctl enable --now")
    assert install.index('[ -f "$ENV" ] || write_env') < install.index("add_env_settings")
    # units and the backup script are written only when they differ
    assert 'kd_render "$1" | cmp -s - "$2" || kd_install_rendered "$1" "$2" 0644' in _function("put_rendered")
    assert 'cmp -s "$1" "$2" || install -o root -g root -m "$3" "$1" "$2"' in _function("put_plain")


def test_versions_come_from_the_catalog():
    text = _text()
    assert "1.1.0" not in text and "2.14.1" not in text
    assert not re.search(r"postgresql@1[0-9]|/postgresql/1[0-9]/", text)
    for key in ("postgresql", "hindsight_api", "torch"):
        assert f"version {key})" in text, key
    venv = _function("make_venv")
    assert '"torch==$torch"' in venv and '"hindsight-api==$api"' in venv
    assert "--index-url https://download.pytorch.org/whl/cpu" in venv


def test_env_is_written_0600_atomically():
    env = _function("write_env")
    assert "umask 077" in env
    assert env.index('chown root:root "$tmp"') < env.index('chmod 0600 "$tmp"') < env.index('mv -f "$tmp" "$ENV"')
    assert re.search(r'> "\$ENV"', _text()) is None  # never written in place
    assert 's|@db_password@|' in env and 's|@tenant_key@|' in env


def _env_run(tmp_path, env_text, call):
    """missing_env_lines/add_env_settings of the script over a temporary env and the repository's example."""
    env = tmp_path / "coding-agent.env"
    env.write_bytes(env_text.encode())
    script = "\n".join([
        "set -euo pipefail",
        f"ENV='{env.as_posix()}'", f"HS='{HS.as_posix()}'",
        "chown() { :; }",  # not root here
        "missing_env_lines() {", _function("missing_env_lines"), "}",
        "add_env_settings() {", _function("add_env_settings"), "}",
        call,
    ])
    proc = subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=30)
    return proc, env


@pytest.mark.skipif(BASH is None, reason="no bash")
def test_an_older_env_gets_the_new_settings_and_keeps_its_own(tmp_path):
    """An env written by an older tree lacks a setting added since; install appends it — never a secret's line, never
    over a value the host already has."""
    old = ("HINDSIGHT_API_DATABASE_URL=postgresql://hindsight:s3cret@127.0.0.1:5432/hindsight\n"
           "HINDSIGHT_API_TENANT_API_KEY=k3y\n"
           "HINDSIGHT_API_LLM_MAX_CONCURRENT=4\n"
           "HINDSIGHT_API_REFLECT_WALL_TIMEOUT=1200")  # the host's own value, and no newline at the end
    proc, env = _env_run(tmp_path, old, "add_env_settings")
    assert proc.returncode == 0, proc.stderr
    lines = env.read_text(encoding="utf-8").splitlines()
    assert lines[:4] == old.splitlines()  # untouched, the missing last newline added
    added = lines[4:]
    assert "HINDSIGHT_API_AUDIT_LOG_ENABLED=true" in added and "HINDSIGHT_API_LLM_TRACE_RETENTION_DAYS=8" in added
    assert "HINDSIGHT_API_LLM_MAX_CONCURRENT=2" not in added and not any("REFLECT_WALL" in line for line in added)
    assert not any("@" in line or line.startswith("#") for line in added)
    assert "added" in proc.stdout and "HINDSIGHT_API_AUDIT_LOG_ENABLED" in proc.stdout and "k3y" not in proc.stdout
    # a second run has nothing to add: no restart
    again, _ = _env_run(tmp_path, env.read_text(encoding="utf-8"), "add_env_settings || echo nothing")
    assert again.returncode == 0 and again.stdout.strip() == "nothing"


@pytest.mark.skipif(BASH is None, reason="no bash")
def test_a_current_env_lacks_nothing(tmp_path):
    example = (HS / "coding-agent.env.example").read_text(encoding="utf-8")
    current = example.replace("@db_password@", "p").replace("@tenant_key@", "k")
    proc, _ = _env_run(tmp_path, current, "missing_env_lines")
    assert proc.returncode == 0 and proc.stdout == ""


def test_check_names_the_missing_settings():
    check = _function("check")
    assert "missing_env_lines" in check and "hindsight_setup.sh install adds them" in check


def test_added_settings_are_written_like_the_env():
    add = _function("add_env_settings")
    assert "umask 077" in add
    assert add.index('chown root:root "$tmp"') < add.index('chmod 0600 "$tmp"') < add.index('mv -f "$tmp" "$ENV"')


def test_the_password_never_rides_argv():
    text = _text()
    for line in text.splitlines():
        if "PASSWORD" in line:
            assert re.search(r"printf .*PASSWORD.*\| *psql_root", line), line


def test_role_settings_as_on_the_live_server():
    fresh = _function("server_db")
    for setting in ("hnsw.ef_search = 200", "hnsw.iterative_scan = strict_order", "hnsw.max_scan_tuples = 4000",
                    "statement_timeout = '600s'", "pg_trgm.similarity_threshold = 0.15"):
        assert f"ALTER ROLE hindsight SET {setting}" in fresh, setting
    assert "CREATE EXTENSION IF NOT EXISTS vector" in fresh and "CREATE EXTENSION IF NOT EXISTS pg_trgm" in fresh


def test_example_env_has_no_placeholder_but_secrets():
    body = [line for line in (HS / "coding-agent.env.example").read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")]
    assert set(re.findall(r"@[a-z_]+@", "\n".join(body))) == {"@db_password@", "@tenant_key@"}
    assert not any("<" in line for line in body)
    assert "HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2" in body
    assert "HINDSIGHT_API_RERANKER_LOCAL_MODEL=cross-encoder/mmarco-mMiniLMv2-L12-H384-v1" in body


def test_units_are_rendered_and_the_backup_installed():
    """10.8 of the rename review: hindsight-backup.timer (its zone is the host's) reaches the host rendered;
    So does the backup service (its directory is the host's)."""
    text = _text()
    assert re.search(r"for unit in hindsight-api\.service hindsight-worker\.service hindsight-backup\.timer; do\n"
                     r'\s+put_rendered "\$HS/\$unit" "\$UNITS/\$unit"', text)
    assert 'put_rendered "$HS/hindsight-backup.service" "$UNITS/hindsight-backup.service"' in text
    assert 'kd_render "$HS/hindsight-backup.service" | cmp -s - "$UNITS/hindsight-backup.service"' in text
    assert 'put_plain "$HS/hindsight-backup.sh" "$BACKUP" 0755' in text
    assert "@host:timezone@" in (HS / "hindsight-backup.timer").read_text(encoding="utf-8")
    render = (REPO / "common" / "install" / "render_host.sh").read_text(encoding="utf-8")
    present = re.search(r'^PRESENT="([^"]*)"', render, re.M).group(1).split()
    for unit in ("hindsight-api.service", "hindsight-worker.service", "hindsight-backup.timer",
                 "hindsight-backup.service"):
        assert f"common/hindsight/{unit}" in present, unit  # every release re-renders them


def test_the_dumps_go_to_the_hosts_backup_dir():
    """No deployment's path in the code — host.json's backup_dir (default /var/backups/digipaddock)."""
    service = (HS / "hindsight-backup.service").read_text(encoding="utf-8")
    assert "Environment=HINDSIGHT_DUMPS=@host:backup_dir@/hindsight" in service
    backup = (HS / "hindsight-backup.sh").read_text(encoding="utf-8")
    assert 'dir=${HINDSIGHT_DUMPS:?' in backup
    assert 'DUMPS=$(/usr/bin/python3 -I "$TREE/common/kd/naming.py" host backup_dir)/hindsight' in _text()
    assert '[ -d "$DUMPS" ] || problems+=' in _function("check")
    for path in (*HS.iterdir(), SCRIPT):
        assert "/backups/stage" not in path.read_text(encoding="utf-8"), path.name


def test_the_llm_units_read_the_hosts_claude_token():
    """Hindsight's Claude calls authenticate with the host's token (systemd reads it as root; the SDK passes
    the unit's environment to claude) — without the file, the base user's stored login as before."""
    for unit in ("hindsight-api.service", "hindsight-worker.service"):
        lines = (HS / unit).read_text(encoding="utf-8").splitlines()
        assert "EnvironmentFile=-/etc/digipaddock/claude.env" in lines, unit
        assert lines.index("EnvironmentFile=-/etc/digipaddock/claude.env") > lines.index(
            "EnvironmentFile=/etc/hindsight/coding-agent.env"), unit


def test_units_wait_for_any_postgresql_cluster():
    """postgresql.service, not one cluster's unit — every cluster unit is Before= it, and the major
    version of the catalog changes without the units."""
    for unit in ("hindsight-api.service", "hindsight-worker.service", "hindsight-backup.service"):
        text = (HS / unit).read_text(encoding="utf-8")
        assert "After=" in text and "postgresql.service" in text and "postgresql@" not in text, unit


def test_check_covers_the_server():
    check = _function("check")
    for piece in ('prereq.sh" check --hindsight', "same_conf", "pg_roles", "pg_database", "pg_extension", "pg_trgm vector",
                  "importlib.metadata", '"$api"', "HINDSIGHT_API_TENANT_API_KEY", "HINDSIGHT_API_DATABASE_URL",
                  "root:root 600", "@db_password@\\|@tenant_key@", "systemctl is-enabled", "systemctl is-active",
                  'curl -fsS -m 10 -o /dev/null "$HEALTH"'):
        assert piece in check, piece
    assert "\nHEALTH=http://127.0.0.1:9077/health\n" in _text() and "exit 1" in check


def test_disable_keeps_the_data():
    disable = _mode("disable")
    assert "systemctl disable --now hindsight-api.service hindsight-worker.service hindsight-backup.timer" in disable
    for banned in ("DROP", "rm ", "dropdb", "userdel"):
        assert banned not in disable, banned


def test_install_ends_with_the_check_and_the_clients_hint():
    install = _mode("install")
    assert install.index("install_all") < install.index("check")
    assert "stage_setup.sh" in _function("install_all")  # the agents' clients come with the next stage_setup.sh
