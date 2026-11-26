"""the data key and the MySQL user of kd-data on a target server (target_setup.sh, deploy_target.sh)."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SYSADMIN = ROOT / "agents" / "sysadmin"
TARGET = (SYSADMIN / "install/target_setup.sh").read_text(encoding="utf-8")
DEPLOY = (SYSADMIN / "install/deploy_target.sh").read_text(encoding="utf-8")
SETUP = ROOT / "common" / "install" / "data_read_setup.sh"
BASH = shutil.which("bash")


def secret_re():
    line = next(
        line for line in TARGET.splitlines() if line.startswith("DATA_SECRET_RE=")
    )
    return re.compile(line.split("=", 1)[1].strip("'"), re.I)


@pytest.mark.parametrize(
    "column",
    [
        "password",
        "account_password",
        "email_password",
        "passwd",
        "user_pwd",
        "pass",
        "smtp_pass",
        "secret",
        "secret2",
        "client_secret_hash",
        "webhook_secret",
        "token",
        "token_hash",
        "access_token",
        "cookies_json",
        "api_key",
        "apikey",
        "auth_key",
        "private_key",
        "privkey",
        "salt",
        "otp",
        "totp_secret",
        "credentials",
        "hashedValidator",
    ],
)
def test_secret_columns_are_hidden_by_name(column):
    assert secret_re().search(column)


@pytest.mark.parametrize(
    "column",
    [
        "id",
        "name",
        "brand_id",
        "status",
        "created_at",
        "passed",
        "passage",
        "webhook_url",
        "client_id",
        "partner_id",
        "host",
        "username",
        "photo",
    ],
)
def test_ordinary_columns_stay_visible(column):
    assert not secret_re().search(column)


def test_the_sysadmin_line_stays_and_the_data_line_forces_the_data_mode():
    sysadmin = 'printf \'restrict,command="%s",from="%s" %s\\n\' "$GATE" "$STAGE_IP" "$ro_key" > "$tmp"'
    data = 'printf \'restrict,command="%s --data",from="%s" %s\\n\' "$GATE" "$STAGE_IP" "$data_key" >> "$tmp"'
    assert sysadmin in TARGET and data in TARGET
    assert (
        TARGET.index(sysadmin)
        < TARGET.index(data)
        < TARGET.index('mv -f "$tmp" /home/agent-ro/.ssh/authorized_keys')
    )
    # the key, the databases and the hidden columns are checked before they reach authorized_keys or SQL
    for check in (
        "[[ $data_key =~ ^ssh-ed25519\\ [A-Za-z0-9+/]+={0,2}\\ agent-data@",
        "[[ $data_dbs =~ ^[A-Za-z0-9_]+( [A-Za-z0-9_]+)*$ ]]",
        "--data-hidden: <db>.<table>.<column>",
    ):
        assert check in TARGET
    assert TARGET.index("[[ $data_dbs =~") < TARGET.index("# 1. agent-ro")


def test_agent_data_gets_columns_without_secrets_and_two_connections():
    assert "WITH MAX_USER_CONNECTIONS 2;" in TARGET
    assert (
        "REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'agent_data'@'localhost';" in TARGET
    )
    assert "REGEXP '$DATA_SECRET_RE'" in TARGET
    assert '| data_grants "$db" >> "$grants"' in TARGET
    # its own cnf, never the sysadmin's; no default database: kd-data names it
    assert (
        'printf \'[client]\\nuser=agent_data\\npassword="%s"\\n\' "$pw" > "$tmp"'
        in TARGET
    )
    assert (
        'mv -f "$tmp" "$DATA_CNF"' in TARGET
        and "DATA_CNF=/home/agent-ro/.my-data.cnf" in TARGET
    )
    assert "DROP USER IF EXISTS 'agent_data'@'localhost'" in TARGET


def data_grants(lines):
    """Run target_setup.sh's own data_grants over lines of one database: (stdout, stderr)."""
    start = TARGET.index("data_grants() {")
    body = TARGET[start : TARGET.index("# --- end data_grants")]
    script = body + "data_grants db\n"
    done = subprocess.run(
        [BASH, "-c", script],
        input="".join(f"{line}\n" for line in lines),
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.splitlines(), done.stderr


@pytest.mark.skipif(BASH is None, reason="no bash")
def test_grants_hide_secret_columns_and_never_open_a_table_of_secrets():
    out, err = data_grants(
        [
            "orders|id status total||.",  # nothing hidden: the whole table
            "users|id email|password api_token|.",  # per column
            "api_tokens||token secret|.",  # secrets only: no grant at all (an empty middle field stays empty)
            "!weird|id||.",  # a table or column name that is not plain: skipped whole
            "t|password|x||.",  # a table named "t|password" (column x) if SQL missed it: one field too many
            "u|a|b|",  # a line cut short: no end mark
            "x`y|id||.",
        ]
    )
    assert out == [
        "GRANT SELECT ON `db`.`orders` TO 'agent_data'@'localhost';",
        "GRANT SELECT (`id`, `email`) ON `db`.`users` TO 'agent_data'@'localhost';",
    ]
    assert (
        "hidden db.users: password api_token" in err
        and "hidden db.api_tokens: token secret" in err
    )
    assert (
        err.count("skipped") == 4
        and "db.weird skipped" in err
        and "db.t skipped" in err
    )
    # the SQL marks a table name that is not plain too, and closes every line
    assert (
        "OR table_name NOT REGEXP '^[A-Za-z0-9_]+\\$'" in TARGET and "'|.')" in TARGET
    )


def test_self_check_of_the_data_mode():
    assert 'SSH_ORIGINAL_COMMAND=uptime "$GATE" --data' in TARGET
    assert "agent_data@localhost*)" in TARGET


def test_deploy_sends_the_data_key_only_to_servers_with_databases():
    assert (
        "data_dbs=$(list_field data_databases)" in DEPLOY
        and "list_field data_hidden_columns" in DEPLOY
    )
    assert 'cat "/home/$STAGE_DATA_USER/.ssh/agent_data_ed25519.pub"' in DEPLOY
    assert "agent-data@$STAGE$ ]]" in DEPLOY
    assert '$mysql_args $data_args"' in DEPLOY
    # the data user pins the same host key as the sysadmin and the exec user, written in its own name
    assert 'if [ -n "$data_user" ]; then pin_key "$data_user"; fi' in DEPLOY
    assert "chown" not in DEPLOY.split("pin_key() {")[1].split("\nEOF")[0]
    assert '$SYSADMIN_USER $STAGE_EXEC_USER $STAGE_DATA_USER"' in DEPLOY


@pytest.mark.skipif(BASH is None, reason="no bash")
@pytest.mark.parametrize(
    "script",
    [
        SYSADMIN / "install/target_setup.sh",
        SYSADMIN / "install/deploy_target.sh",
        SETUP,
    ],
)
def test_scripts_parse(script):
    assert (
        subprocess.run([BASH, "-n", str(script)], capture_output=True).returncode == 0
    )


def test_module_setup_keeps_the_key_from_agents():
    text = SETUP.read_text(encoding="utf-8")
    assert (
        'want "$KEY" "$data:$data" 600' in text
        and 'want "$HOME_DIR" "$data:$data" 700' in text
    )
    assert '! runuser -u "$user" -- test -r "$KEY"' in text
    assert 'install -d -o "$data" -g "$observe" -m 2750 "$JOURNAL"' in text
    assert 'systemctl stop "digipaddock-cap-$agent-data@*.service"' in text
